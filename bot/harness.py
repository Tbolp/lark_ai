"""harness: 按 chat_id 复用的会话 + 工具循环。

一次触发 = 一轮: 把用户那句话交给模型, 模型可以调工具, 结果回填, 直到它给出一个
不带工具调用的正文 —— 这一轮就结束, 正文推给 on_output。

和 codex 那版的差别:

  - 没有子进程、没有 JSON-RPC: 直接 HTTP 调模型（Chat Completions）, 工具循环是我们
    自己写的这几行;
  - 指令是一段 system 消息（可以传函数, 每段新对话重新求值一次 —— 时间类占位符
    就不会冻在进程启动那一刻）, 历史是我们自己维护的 messages;
  - 追问并进正在跑的一轮: codex 用 `turn/steer`, 这里在循环的每一轮开始前把待处理的
    输入插进 messages —— 插在工具结果之后, 不会破坏 tool_calls 与 tool 消息的配对;
  - 出错不再回退到别的路径: 报错就是报错, 顺手清掉上下文, 下一条消息从干净的会话开始。

会话是**活动驱动**的: 连续 idle_timeout 秒既没有输入也没有输出就清空历史（下一条
重新开始）, 只清历史、留着会话对象 —— 和 codex 那版"关进程、下次重开"等价。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any, Awaitable, Callable

from .tools import Toolbox

# 一轮里最多允许模型调多少次工具: 兜住"模型反复试同一条命令"这种死循环。
# 正常一次行情/筛选分析在 1~10 次之间, 40 足够宽松。
DEFAULT_MAX_TOOL_ROUNDS = 40

# 同一批工具调用最多同时跑几个。模型一轮里常常一口气发好几条相互独立的查询
# （技能文档也要求这样的查询同轮发出）, 串行跑就是白等; 但也不能不设上限 ——
# 命令打的是远程接口, 一次几十个并发既慢又容易触发风控。
DEFAULT_MAX_PARALLEL_TOOLS = 8


class ChatSession:
    """一个 chat_id 的会话: 一段消息历史 + 一个"当前有没有轮在跑"的状态机。

    对外的口子只有 post(): 空着就开一轮, 有轮在跑就把这句话并进正在跑的那一轮。
    所有结局（正文、失败原因）都从构造时传入的 on_output 出去。

    会话是"谁"由 chat_id 决定: 工具里要用到会话身份时（比如关注列表）拿它去查。
    指令可以是字符串, 也可以是函数 —— 是函数时每段新对话开始前求值一次, 所以
    prompt 里的时间类占位符不会冻结在进程启动那一刻。
    """

    def __init__(self, *, client: Any, model: str, instructions: str | Callable[[], str],
                 toolbox: Toolbox, turn_timeout: float, api_timeout: float,
                 idle_timeout: float, chat_id: str = "",
                 max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
                 max_parallel_tools: int = DEFAULT_MAX_PARALLEL_TOOLS,
                 max_history_messages: int = 200,
                 on_output: Callable[[str, bool], Awaitable[None]]) -> None:
        self.client = client
        self.model = model
        self.toolbox = toolbox
        self.chat_id = chat_id
        self.turn_timeout = turn_timeout
        self.api_timeout = api_timeout
        self.idle_timeout = idle_timeout
        self.max_tool_rounds = max_tool_rounds
        self.max_parallel_tools = max_parallel_tools
        self.max_history_messages = max_history_messages
        self.on_output = on_output
        # 指令在"一段新对话开始时"取一次: 改了 prompt / SKILL.md, 下一条新会话才生效;
        # 传函数时每次都会重新求值（时间类占位符跟着走）。
        self.instructions = instructions
        self.messages: list[dict[str, Any]] = []
        self.pending: list[str] = []          # 等下一轮（或正在跑的那一轮）处理的输入
        self.turn_active = False
        self.task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None

    # ---------- 入口 ----------

    async def post(self, user_text: str) -> None:
        """把一句话交给这个会话: 有轮在跑就并进去, 否则开一轮。"""
        text = user_text.strip()
        if not text:
            return
        self._touch_activity()
        async with self._lock:
            self.pending.append(text)
            if self.turn_active:
                sys.stderr.write("[harness] 追问已并入当前分析\n")
                return
            self.turn_active = True
        self.task = asyncio.create_task(self._drain())

    async def close(self) -> None:
        """收掉计时器和在跑的那一轮（关会话 / 进程退出时调用）。"""
        self._cancel_idle()
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.turn_active = False
        self.messages.clear()
        self.pending.clear()

    def reset(self) -> None:
        """清空这段对话, 保留会话对象（定时消息每轮要一份干净上下文, 不靠空闲超时）。

        轮正在跑的时候不清: 那说明上一轮还没结束, 定时消息本身会并进那一轮
        （见 _run_turn 里"追问已并入当前分析"那段）, 硬清历史反而会把配对关系打乱。
        """
        if self.turn_active:
            sys.stderr.write("[harness] 会话上有轮在跑, 不重置上下文\n")
            return
        self._cancel_idle()
        self.messages.clear()
        self.pending.clear()

    # ---------- 一轮的调度 ----------

    async def _drain(self) -> None:
        """把 pending 里的输入一批批处理掉, 直到没有新的为止。"""
        try:
            while True:
                async with self._lock:
                    if not self.pending:
                        self.turn_active = False
                        self._touch_activity()      # 转入空闲: 从这一刻开始计时
                        return
                    batch = self.pending[:]
                    self.pending.clear()
                ok, text = await self._run_turn(batch)
                if not ok:
                    # 出错就把上下文清掉: 半截的对话带着, 下一问还会踩同一个坑
                    self.messages.clear()
                await self._emit(text, ok)
                self._touch_activity()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                  # 兜底: 任何意外都不能让会话哑掉
            sys.stderr.write(f"[harness] 未预期的错误: {exc!r}\n")
            self.messages.clear()
            await self._emit(f"分析过程中出错了（{exc!r}）。", False)
            async with self._lock:
                self.turn_active = False

    async def _run_turn(self, batch: list[str]) -> tuple[bool, str]:
        """跑一轮: 模型 → 工具 → 模型 …, 直到它给出正文。返回 (成功?, 文本)。"""
        deadline = time.monotonic() + self.turn_timeout
        self._ensure_system()
        for text in batch:
            self.messages.append({"role": "user", "content": text})

        for _ in range(self.max_tool_rounds + 1):
            # 正在跑的这一轮里又来的追问: 插在工具结果之后（顺序合法, 且模型下一眼就看得到）
            async with self._lock:
                if self.pending:
                    extra = self.pending[:]
                    self.pending.clear()
                    self.messages.extend({"role": "user", "content": t} for t in extra)
                    sys.stderr.write(f"[harness] 追问已并入当前分析（{len(extra)} 条）\n")
            self._trim_history()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, f"分析超时（超过 {self.turn_timeout:g} 秒）。"
            try:
                message = await self._complete(remaining)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sys.stderr.write(f"[harness] 调用模型失败: {exc!r}\n")
                return False, f"调用模型失败：{exc}"

            calls = list(getattr(message, "tool_calls", None) or ())
            if calls:
                self.messages.append(_assistant_entry(message))
                for call, result in zip(calls, await self._call_tools(calls)):
                    self.messages.append({"role": "tool", "tool_call_id": call.id,
                                          "content": result})
                continue

            text = (getattr(message, "content", None) or "").strip()
            self.messages.append({"role": "assistant", "content": text})
            if not text:
                return False, "模型没有返回内容，请稍后重试。"
            return True, text
        return False, f"工具调用次数过多（超过 {self.max_tool_rounds} 次），已中止这一轮。"

    async def _complete(self, remaining: float) -> Any:
        """一次模型调用。超时取"这一轮剩多少"和"单次上限"里小的那个。"""
        timeout = min(remaining, self.api_timeout)
        response = await asyncio.wait_for(
            self.client.chat.completions.create(
                model=self.model,
                # 传副本: 历史是活列表, 这一轮后面还会往里追加, 别让客户端拿着它的引用
                messages=list(self.messages),
                tools=self.toolbox.schemas(),
                tool_choice="auto",
            ),
            timeout,
        )
        return response.choices[0].message

    async def _call_tools(self, calls: list[Any]) -> list[str]:
        """执行这一批工具调用, 返回与 calls **同序**的结果。

        模型一轮里发几条调用是常事（实测一次个股分析发了 6 条）, 它们之间没有依赖,
        所以并发跑; 上限 max_parallel_tools。gather 保序, 回填时 tool 消息的顺序与
        模型给的 tool_calls 一致 —— 顺序虽然不影响配对（靠 tool_call_id 认）, 但对齐
        了更好读、也方便对照日志。
        """
        if len(calls) == 1:
            return [await self._call_tool(calls[0])]
        sys.stderr.write(f"[harness] 并发执行 {len(calls)} 个工具调用"
                         f"（上限 {self.max_parallel_tools}）\n")
        gate = asyncio.Semaphore(self.max_parallel_tools)

        async def run(call: Any) -> str:
            async with gate:
                return await self._call_tool(call)

        return list(await asyncio.gather(*(run(call) for call in calls)))

    async def _call_tool(self, call: Any) -> str:
        """执行一次工具调用: 工具内部不抛异常, 这里再兜一层（名字/参数结构不对）。"""
        name = getattr(getattr(call, "function", None), "name", "") or ""
        arguments = getattr(getattr(call, "function", None), "arguments", "") or ""
        started = time.monotonic()
        try:
            result = await self.toolbox.call(name, arguments, chat_id=self.chat_id)
        except Exception as exc:                  # 不该发生, 真发生了也不能带停整轮
            sys.stderr.write(f"[harness] 工具 {name} 异常: {exc!r}\n")
            return f"工具执行出错: {exc!r}"
        sys.stderr.write(f"[harness] 工具 {name} {time.monotonic() - started:.1f}s "
                         f"{arguments[:160]}\n")
        return result

    # ---------- 历史 ----------

    def _ensure_system(self) -> None:
        if not self.messages:
            self.messages = [{"role": "system", "content": self._system_text()}]

    def _system_text(self) -> str:
        """取这次的指令文本: 传的是字符串就用它, 传的是函数就现在求值一次。"""
        if callable(self.instructions):
            return self.instructions()
        return self.instructions

    def _trim_history(self) -> None:
        """历史太长就在**用户消息处**切掉前面一段。

        切点必须落在 user 消息上: assistant(tool_calls) 和它后面的 tool 消息是一对,
        从中间切开会让下一次请求因为"tool 消息没有对应的 tool_calls"直接报错。
        """
        if len(self.messages) <= self.max_history_messages:
            return
        cut = len(self.messages) - self.max_history_messages
        index = next((i for i in range(max(1, cut), len(self.messages))
                      if self.messages[i].get("role") == "user"), None)
        if index is None:
            return
        dropped = index - 1
        self.messages = [self.messages[0], *self.messages[index:]]
        sys.stderr.write(f"[harness] 历史过长, 丢掉最早 {dropped} 条消息\n")

    # ---------- 输出 / 计时 ----------

    async def _emit(self, text: str, ok: bool) -> None:
        try:
            await self.on_output(text, ok)
        except Exception as exc:                  # 发送失败只记一行, 不能带停会话
            sys.stderr.write(f"[harness] 输出发送失败: {exc!r}\n")

    def _touch_activity(self) -> None:
        """有输入或输出就把空闲计时重置; 空闲到点就清空历史（下一问重新开始）。"""
        self._cancel_idle()
        if self.idle_timeout <= 0:
            return
        self._idle_handle = asyncio.get_running_loop().call_later(
            self.idle_timeout, self._on_idle)

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _on_idle(self) -> None:
        self._idle_handle = None
        if self.turn_active:               # 轮还在跑: 那是活动, 等它自己续上计时
            self._touch_activity()
            return
        if self.messages:
            sys.stderr.write(f"[harness] 空闲 {self.idle_timeout:g}s, 清掉这段上下文\n")
        self.messages.clear()


def _assistant_entry(message: Any) -> dict[str, Any]:
    """把模型的回复转成可以直接塞回历史的一条消息。

    reasoning_content 之类的字段不回传: 那是给日志看的, 不是对话的一部分。
    """
    return {
        "role": "assistant",
        "content": getattr(message, "content", None) or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ],
    }


class ChatSessions:
    """会话注册表: chat_id -> ChatSession。一个聊天一条, 各自独立、各自回收。"""

    def __init__(self, *, client: Any, model: str,
                 instructions: Callable[[], str], toolbox: Toolbox,
                 turn_timeout: float, api_timeout: float, idle_timeout: float,
                 max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
                 max_parallel_tools: int = DEFAULT_MAX_PARALLEL_TOOLS,
                 on_output: Callable[[str, str, bool], Awaitable[None]]) -> None:
        self.client = client
        self.model = model
        self.instructions = instructions
        self.toolbox = toolbox
        self.turn_timeout = turn_timeout
        self.api_timeout = api_timeout
        self.idle_timeout = idle_timeout
        self.max_tool_rounds = max_tool_rounds
        self.max_parallel_tools = max_parallel_tools
        self.on_output = on_output
        self.sessions: dict[str, ChatSession] = {}

    def get(self, chat_id: str) -> ChatSession:
        session = self.sessions.get(chat_id)
        if session is None:
            session = ChatSession(
                client=self.client, model=self.model, instructions=self.instructions(),
                toolbox=self.toolbox, chat_id=chat_id, turn_timeout=self.turn_timeout,
                api_timeout=self.api_timeout, idle_timeout=self.idle_timeout,
                max_tool_rounds=self.max_tool_rounds,
                max_parallel_tools=self.max_parallel_tools,
                on_output=self._binder(chat_id),
            )
            self.sessions[chat_id] = session
            sys.stderr.write(f"[harness] 新会话 chat_id={chat_id}"
                             f"（现有 {len(self.sessions)} 条）\n")
        return session

    async def post(self, chat_id: str, text: str) -> None:
        """把一句话交给这个 chat_id 的会话; 结局全在会话里处理（见 ChatSession.post）。"""
        await self.get(chat_id).post(text)

    def reset(self, chat_id: str) -> None:
        """把这个会话的上下文清掉（会话还没建就不用管）。定时消息每轮用一次。"""
        session = self.sessions.get(chat_id)
        if session is not None:
            session.reset()

    async def close_all(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()

    def _binder(self, chat_id: str) -> Callable[[str, bool], Awaitable[None]]:
        """给某条会话的输出回调绑上它的 chat_id（回调签名见 ChatSession）。"""
        async def send(text: str, ok: bool) -> None:
            await self.on_output(chat_id, text, ok)
        return send
