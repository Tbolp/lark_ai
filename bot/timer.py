"""定时消息源: 每个整点往会话里发一条消息。

和 lark_chat.py 的分工完全一致 —— 这里只做"到点了 -> 归一化成一条消息 -> emit"，
怎么处理、回给谁, 全在 app 注册的 handler 里（trigger_handler / output_handler）。
也就是说"定时任务"不是另一种流程, 它就是一条消息, 只是发消息的人不是人。

几个要点:

  - **整点触发**: 每轮重新算"到下一个整点的距离", 不累积漂移; 上一轮跑过头越过整点就
    不补跑, 下一个整点照常;
  - **每个目标一条消息**: chat_id 用 `timer:<目标 chat_id>`, 于是每个目标有自己的一条
    内部会话, 既不碰目标会话的历史, 目标之间也互不干扰;
  - **目标之间错开几秒**: 免得同一秒把多个会话一起放开、一起去打 westock;
  - 消息里带上该目标的关注列表和最近推送过的东西 —— 都是状态库里的, 不是模型记忆。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Awaitable, Callable, Sequence

from core import events as core

from .events import TRIGGER

# 定时会话的 chat_id 前缀。真实的会话 id 是 oc_/ou_/om_ 开头, 撞不上。
TIMER_PREFIX = "timer:"

# 模型用它表示"这一轮没什么值得推的"（见 prompts/instructions.md 的类型二）。
NO_PUSH = "NO_PUSH"

# 目标之间错开多久发出。
STAGGER = 3.0

# config.json 里 timer_message 的默认文案。约定了"有值得推的就发, 没有就回 NO_PUSH"。
DEFAULT_MESSAGE = (
    "看看最近一段时间有没有对股市影响比较大的消息，有就整理成一条可以直接发到群里的消息，"
    "没有就只回一行 NO_PUSH。"
)


def next_hour(now: float) -> float:
    """下一个整点的 epoch 秒（严格在将来: 正好站在整点上时给下一个小时）。"""
    return (int(now // 3600) + 1) * 3600


def session_key(target: str) -> str:
    """某个目标会话的定时内部会话 key。"""
    return f"{TIMER_PREFIX}{target}"


def is_timer(chat_id: str) -> bool:
    """这个 chat_id 是不是定时内部会话。"""
    return (chat_id or "").startswith(TIMER_PREFIX)


def target_of(chat_id: str) -> str:
    """从定时内部会话 key 还原出目标会话（不是定时 key 就原样返回）。"""
    return chat_id[len(TIMER_PREFIX):] if is_timer(chat_id) else chat_id


def is_no_push(text: str) -> bool:
    """整条回复的**第一行**就是 NO_PUSH 就算静默。

    宽松匹配是有意的: 模型经常顺手补一句理由, 严格匹配"整条只有一行"会让
    `NO_PUSH（本轮没有值得推的）` 直接漏进群里; 第一行命中就静默, 剩下的内容记日志。
    """
    for line in (text or "").splitlines():
        line = line.strip().strip("*`# ")
        if not line:
            continue
        token = re.split(r"[\s:：,，。;；()（）\[\]【】]", line, maxsplit=1)[0]
        return token.upper() == NO_PUSH
    return False


def build_message(core_text: str, *, now: float, window_start: float,
                  recent: Sequence[tuple[float, str]], watchlist: str) -> str:
    """拼出这一轮发给模型的那条消息。

    config 里那句话是主体, 后面跟一段"系统附加信息": 时间窗口、关注列表、最近推过什么。
    这些是事实, 提示词里要求模型不要复述、不要写进正文。
    """
    lines = [f"【定时播报】{core_text}", "", "（系统附加信息，不用在回复里复述："]
    lines.append(f"现在是北京时间 {stamp(now)}；本轮只看 {stamp(window_start)} 之后出现的消息；")
    lines.append(f"本会话关注的股票：{watchlist}；")
    if recent:
        lines.append("最近已经推送过（没有新进展就不要重复推）：")
        lines.extend(f"- {stamp(ts)} {summary}" for ts, summary in recent)
    else:
        lines.append("最近还没有推送过任何消息。")
    lines.append("）")
    return "\n".join(lines)


def stamp(ts: float) -> str:
    """北京时间（机器本地时区）的 "09-28 14:00"。"""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


class TimerSource:
    """定时消息源: 每个整点给每个目标发一条消息事件。

    targets / watchlist / recent 都是回调, 到点时才问 —— 目标列表是"群白名单 + 已经见过的
    私聊", 私聊是运行期学到的, 不能启动时定死。
    """

    def __init__(self, *, message: str, targets: Callable[[], list[str]],
                 watchlist: Callable[[str], str],
                 recent: Callable[[str], list[tuple[float, str]]],
                 emit: Callable[[str, dict], None] = core.emit,
                 now: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 stagger: float = STAGGER) -> None:
        self.message = message
        self.targets = targets
        self.watchlist = watchlist
        self.recent = recent
        self.emit = emit
        self.now = now
        self.sleep = sleep
        self.stagger = stagger
        self.last_tick: float | None = None
        self.task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    def start(self) -> asyncio.Task:
        """起后台任务（要在事件循环里调）。"""
        self.task = asyncio.create_task(self.run(), name="timer-source")
        return self.task

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        """一直跑到被取消: 每轮睡到下一个整点, 到点发一批事件。

        距离每轮重算（不累积漂移）; 上一轮跑得太久、越过了一个整点, 那一个整点就不补了 ——
        连着补跑没有意义, 还容易把同一件事推两遍。
        """
        while True:
            now = self.now()
            delay = next_hour(now) - now
            sys.stderr.write(f"[timer] 下一次整点 {stamp(now + delay)}（{delay:.0f}s 后）\n")
            await self.sleep(delay)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # 一轮炸了不能让盯盘整体停掉（下一轮照跑）
                sys.stderr.write(f"[timer] 这一轮出错: {exc!r}\n")

    async def tick(self) -> None:
        """发一轮: 每个目标一条事件（目标之间错开几秒）。手工验收也是调这个。"""
        now = self.now()
        window_start = self.last_tick if self.last_tick is not None else now - 3600
        targets = list(self.targets())
        if not targets:
            sys.stderr.write("[timer] 到点了, 但还没有任何目标会话, 这一轮跳过\n")
            self.last_tick = now
            return
        for index, target in enumerate(targets):
            if index:
                await self.sleep(self.stagger)         # 错开: 别同一秒一起开跑
            event = self._event(target, now=self.now(), window_start=window_start)
            sys.stderr.write(f"[timer] 发出定时消息 target={target}（{len(event['text'])} 字符）\n")
            self.emit(TRIGGER, event)
        self.last_tick = now

    # ---------- 一条事件 ----------

    def _event(self, target: str, *, now: float, window_start: float) -> dict:
        """归一化成和 lark_chat.normalize 同一形状的事件（handler 只认这个形状）。"""
        return {
            "message_id": None,
            "chat_id": session_key(target),
            "chat_type": "timer",
            "thread_id": None,
            "message_type": "text",
            "sender_type": "timer",
            "sender_id": None,
            "text": build_message(self.message, now=now, window_start=window_start,
                                  recent=list(self.recent(target)),
                                  watchlist=self.watchlist(target)),
        }
