"""顶层流程: 读配置 + 事件监听 + 触发/拒绝处理 + 组装启动。

配置只在 main() 里读一次（config.json）, 之后一律按具名参数往下传。
run() 是入口: 校验凭据和 prompt -> 建状态库、发送器和消息源 -> 跑监听, 直到监听结束。

服务的范围是两份白名单（chats 群 / users 私聊用户, 见 lark_chat.classify）:
白名单里来的消息走 trigger_handler, 明确不该响应的走 reject_handler。
一次触发的路径: 把那句话（连同它的 chat_id）post 给会话注册表, 一轮跑完就把正文发回
同一个聊天 —— 轮在跑时就并进正在跑的那一轮, 所以一个聊天里不会出现第二条回复。
哪个聊天的上下文由 chat_id 决定（见 harness.ChatSessions）, 这里只管把消息交出去。

消息源有两个, 发的都是 trigger.received, 处理链路完全一样（这是刻意的 —— 对会话来说
"定时任务"和"人问一句话"没有区别, 只是发消息的人不同）:
  lark_chat.LarkChatSource  飞书长连接, 过 classify 之后发事件
  timer.TimerSource         每个整点给每个目标发一条 【定时播报】(config 里 timer_enabled)
定时消息进的是一条内部会话（chat_id 是 `timer:<目标>`）, 它的回复走 output_handler 的
另一条分支: 正文是 NO_PUSH 就不发, 否则发到目标会话并记进去重清单。

模型侧的东西（指令、工具、会话）都在启动时装配一次:
  skills.load_skills      把 config.json 里点名的技能读成正文 + 参考文档白名单
  skills.build_instructions  prompt 文件与各技能正文拼成指令（每次开始新对话时渲染一次,
                          所以 {datetime} 这类占位符不会冻在启动那一刻）
  store.StateStore         状态库（关注列表 / 私聊会话表 / 推送清单 / 失败计数）
  tools.Toolbox           run_westock / read_reference / watchlist 三个工具（含安全边界）
  harness.ChatSessions    openai SDK 的异步客户端 + 按 chat_id 的会话
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Callable

import openai

from core import events as core

from .events import TRIGGER, TRIGGER_REJECTED
from .harness import ChatSessions
from .lark_chat import LarkChatSource
from .lark_sender import LarkSender
from .logging_tee import setup_logging
from .prompts import check_prompt, render_prompt
from .skills import build_instructions, load_skills, reference_table
from .store import FAIL_STREAK_ALERT, StateStore
from .timer import DEFAULT_MESSAGE, TIMER_PREFIX, TimerSource, is_no_push, target_of
from .tools import Toolbox, resolve_program

# 配置文件固定在项目根目录（bot/ 的上一层）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")

# ---------- 配置 ----------

ENV_PREFIX = "env:"


def _read_json(path: str, what: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"读不到{what} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what} {path} 得是一个 JSON 对象")
    return data


def load_env_file(path: str) -> None:
    """把 KEY=VALUE 逐行注入 os.environ（已存在的变量不覆盖）。

    这样真正的密钥只写在配置文件同目录的 .env（已 gitignore）里, config.json 里只留
    一个 "env:NAME" 引用, 于是 config.json 可以安全地进 git。文件不存在就什么都不做,
    所以"只用环境变量、不用 .env"的部署方式也照常工作。只处理最常见的那几种写法:
    空行、# 注释、可选的 export 前缀、可选的引号。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"读不到 {path}: {exc}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key or key in os.environ:      # 真实环境变量优先, .env 只做兜底
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def _env_name(value: str) -> str | None:
    """值形如 "env:NAME" 就返回 NAME, 否则返回 None。"""
    if not value.startswith(ENV_PREFIX):
        return None
    name = value[len(ENV_PREFIX):].strip()
    if not name:
        raise ValueError(f'{ENV_PREFIX!r} 后面得跟环境变量名: {value!r}')
    return name


def _resolve_required(value: str, key: str, path: str) -> str:
    """必填字符串的 "env:NAME" 展开; 引用的环境变量缺失或为空时直接报错。"""
    name = _env_name(value)
    if name is None:
        return value
    resolved = os.environ.get(name, "")
    if not resolved.strip():
        env_file = os.path.join(os.path.dirname(os.path.abspath(path)), ".env")
        raise ValueError(
            f"{path} 里的 {key} 引用了环境变量 {name}, 但它没设置或为空"
            f"（可以写进 {env_file}）"
        )
    return resolved


def _resolve_optional(value: str) -> str:
    """可选字符串的 "env:NAME" 展开; 环境变量缺失就是空串（等价于没配）。"""
    name = _env_name(value)
    return os.environ.get(name, "") if name is not None else value


def _text(raw: dict, key: str, path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} 里的 {key} 得是非空字符串")
    return _resolve_required(value, key, path)


def _path(raw: dict, key: str, base: str, path: str) -> str:
    """路径字段: 相对路径按"配置文件所在目录"展开, 所以从哪启动都一样。"""
    value = _text(raw, key, path)
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))


def _path_or_default(raw: dict, key: str, base: str, path: str, default: str) -> str:
    """可选路径字段: 没写就用默认值（默认值是相对配置文件目录的路径）。"""
    value = raw.get(key, "")
    if value is None or value == "":
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{path} 里的 {key} 得是字符串")
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))


def _optional_text(raw: dict, key: str, path: str) -> str:
    """可选字符串: 没有就是空串, 有就得是字符串（支持 "env:NAME" 引用）。"""
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{path} 里的 {key} 得是字符串")
    return _resolve_optional(value)


def _text_or_default(raw: dict, key: str, path: str, default: str) -> str:
    """可选文本: 没写（或写成空白）就用默认值, 写了就得是非空字符串（支持 "env:NAME"）。"""
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{path} 里的 {key} 得是字符串")
    value = _resolve_optional(value)
    return value.strip() or default


def _bool(raw: dict, key: str, path: str, default: bool) -> bool:
    """可选开关: 没写就用默认值, 写了必须是 true / false（"yes" / 1 这类一律报错）。"""
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path} 里的 {key} 得是 true 或 false")
    return value


def _skills(raw: dict, key: str, path: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ValueError(f"{path} 里的 {key} 得是字符串数组")
    return value


def _positive_int(raw: dict, key: str, path: str, default: int) -> int:
    """可选的正整数; 没写就用默认值, 写了就得是正整数（秒 / 条数 / 字符数）。"""
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{path} 里的 {key} 得是正整数")
    return value


def _string_list(raw: dict, key: str, path: str) -> list[str]:
    """白名单数组: 元素必须是非空字符串（去掉首尾空格）; 没有这个键就是空数组。

    与 skills 不同, 这里不允许空字符串元素 —— 白名单里混进一个空串, 等于埋了个
    "谁的 id 都可能命中"的坑, 宁可启动时报错。
    """
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{path} 里的 {key} 得是字符串数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path} 里的 {key} 得是字符串数组（元素是非空字符串）: {item!r}")
        out.append(item.strip())
    return out


def load_config(path: str = CONFIG_PATH) -> dict:
    """读 config.json 并做基本校验; 有问题抛 ValueError（启动时就炸）。"""
    base = os.path.dirname(os.path.abspath(path))
    load_env_file(os.path.join(base, ".env"))   # 先补环境变量, 后面才好展开 "env:NAME"
    raw = _read_json(path, "配置文件")
    if "chat_id" in raw:
        raise ValueError(
            f"{path} 里的 chat_id 已经废弃（现在服务多个会话）: "
            f"改成 chats（群白名单, 如 [\"oc_...\"]）与 users（私聊用户白名单, 如 [\"ou_...\"]）"
        )
    cfg = {
        "chats": _string_list(raw, "chats", path),
        "users": _string_list(raw, "users", path),
        "bot_open_id": _text(raw, "bot_open_id", path),
        "app_id": _text(raw, "app_id", path),
        "app_secret": _text(raw, "app_secret", path),
        "domain": _optional_text(raw, "domain", path),
        "project_dir": _path(raw, "project_dir", base, path),
        "prompts_dir": _path(raw, "prompts_dir", base, path),
        "prompt_file": _text(raw, "prompt_file", path),
        # 模型侧: 缺了就没法跑, 所以 api_key / model 是必填
        "api_key": _text(raw, "api_key", path),
        "api_base_url": _optional_text(raw, "api_base_url", path),
        "model": _text(raw, "model", path),
        "turn_timeout": _positive_int(raw, "turn_timeout", path, 600),
        "api_timeout": _positive_int(raw, "api_timeout", path, 120),
        "command_timeout": _positive_int(raw, "command_timeout", path, 60),
        "tool_output_limit": _positive_int(raw, "tool_output_limit", path, 60000),
        "max_tool_rounds": _positive_int(raw, "max_tool_rounds", path, 40),
        "max_parallel_tools": _positive_int(raw, "max_parallel_tools", path, 8),
        "session_idle_timeout": _positive_int(raw, "session_idle_timeout", path, 600),
        "skills": _skills(raw, "skills", path),
        # 整点播报（默认关闭; 打开后每个整点给每个目标发一条消息）
        "timer_enabled": _bool(raw, "timer_enabled", path, False),
        "timer_message": _text_or_default(raw, "timer_message", path, DEFAULT_MESSAGE),
        # 状态库: 关注列表 + 私聊会话表 + 推送清单 + 失败计数（一个 SQLite 文件）
        "state_file": _path_or_default(raw, "state_file", base, path, "logs/state.db"),
        "log_file": _path(raw, "log_file", base, path),
    }
    # 两份白名单都空 = 机器人谁都不理, 这不是"配错了"而是"没配", 直接炸
    if not cfg["chats"] and not cfg["users"]:
        raise ValueError(f"{path} 里的 chats 与 users 不能都是空的（至少得有服务对象）")
    for key in ("project_dir", "prompts_dir"):
        if not os.path.isdir(cfg[key]):
            raise ValueError(f"{path} 里的 {key} 不是目录: {cfg[key]}")
    # 技能名在这里就解析成 SKILL.md 路径: 配置里写了却装不上, 启动时就炸
    cfg["skills"] = load_skills(cfg["skills"], project_dir=cfg["project_dir"])
    return cfg


def on_task_done(done: core.Done) -> None:
    """任务结束（同步 handler, 当场执行）: 没成功就记一行, 成功的不用吭声。"""
    if done.status != "ok":
        sys.stderr.write(f"[bot] 任务 {done.name} {done.status}（{done.elapsed:.1f}s）\n")


REJECT_COOLDOWN = 60.0
REJECT_TEXT = "⚠️ 当前会话未授权：本机器人只服务白名单里的群与用户。需要开通请联系管理员。"


def trigger_handler(*, sessions: ChatSessions, sender: LarkSender,
                    store: StateStore | None = None):
    """装配触发的处理流程: 上面这些是启动期定下来的东西, handler 只认 payload（见 core.events）。

    主要做两件事: 记一行触发日志, 把那句话（连同它的 chat_id）交给会话注册表 ——
    入轮 / 追加 / 丢弃 / 回退都在会话里决定, 正文由会话自己的输出回调发回同一个聊天。
    sender 只留着最后兜底: 万一这条路整个炸了, 至少能在那个聊天里回一句, 别让机器人哑掉。

    另外两件小事:
      - 定时消息（chat_type == "timer"）先把那条内部会话的上下文清掉, 保证每小时都是干净的一轮;
      - 白名单私聊来消息时, 顺手记下 open_id -> chat_id（整点播报的私聊目标就靠这张表）。
    """

    async def handle_trigger(event: dict) -> None:
        chat_id = event.get("chat_id") or ""
        text = event.get("text", "")
        shown = text if len(text) <= 120 else f"{text[:120]}…"
        sys.stderr.write(
            f"[bot] <- 触发 chat_id={chat_id} chat_type={event.get('chat_type')} "
            f"msg={event.get('message_id')} sender={event.get('sender_id')}: {shown!r}\n"
        )
        if not chat_id:                      # 没有 chat_id 就无处可回, 只能记一行
            sys.stderr.write("[bot] 触发消息没有 chat_id, 丢弃\n")
            return
        _remember_peer(store, event)
        if event.get("chat_type") == "timer":
            sessions.reset(chat_id)
        try:
            await sessions.post(chat_id, text)
        except Exception as exc:  # 后台任务出错不能让机器人哑掉
            sys.stderr.write(f"[bot] 分析异常 chat_id={chat_id}: {exc!r}\n")
            if chat_id.startswith(TIMER_PREFIX):
                # 定时会话的 chat_id 不是真的收件人, 发过去必然被飞书拒; 失败由
                # output_handler 那边记数并（必要时）发失败提示。
                sys.stderr.write("[bot] 定时会话出错, 不走兜底回消息\n")
                return
            try:
                await sender.send(chat_id,
                                  markdown=f"⚠️ 分析过程中出错了，请稍后重试。\n\n{exc!r}")
            except Exception as send_exc:
                sys.stderr.write(f"[bot] 连报错消息都没发出去: {send_exc!r}\n")

    return handle_trigger


def _remember_peer(store: StateStore | None, event: dict) -> None:
    """白名单私聊来消息时记下 open_id -> chat_id; 写失败只写日志, 不影响这条消息。"""
    if store is None or event.get("chat_type") != "p2p":
        return
    sender_id, chat_id = event.get("sender_id"), event.get("chat_id")
    if not sender_id or not chat_id:
        return
    try:
        store.remember_peer(sender_id, chat_id)
    except Exception as exc:          # store 内部已经兜了一层, 这里再兜一层
        sys.stderr.write(f"[bot] 记私聊会话失败: {exc!r}\n")


def reject_handler(*, sender: LarkSender, cooldown: float = REJECT_COOLDOWN,
                   clock: Callable[[], float] = time.monotonic):
    """装配拒绝的处理流程: 不在白名单里的会话回一句"未授权"提示。

    同一个 chat_id 在 cooldown 秒内只回一次（抑制时只记日志, 不再发消息）, 免得
    未授权的群/人反复 @ 就反复收到同一句话。拒绝不是错误, 这里只发提示、不发报错。
    """
    last_sent: dict[str, float] = {}

    async def handle_reject(event: dict) -> None:
        chat_id = event.get("chat_id") or ""
        sys.stderr.write(
            f"[bot] <- 拒绝 chat_id={chat_id} chat_type={event.get('chat_type')} "
            f"msg={event.get('message_id')} sender={event.get('sender_id')}: 未授权\n"
        )
        if not chat_id:
            return
        now = clock()
        if now - last_sent.get(chat_id, float("-inf")) < cooldown:
            sys.stderr.write(f"[bot] 未授权提示已抑制 chat_id={chat_id}\n")
            return
        last_sent[chat_id] = now
        try:
            await sender.send(chat_id, markdown=REJECT_TEXT)
        except Exception as exc:
            sys.stderr.write(f"[bot] 未授权提示没发出去 chat_id={chat_id}: {exc!r}\n")

    return handle_reject


def _first_line(text: str) -> str:
    """正文的第一行（推送清单里记的就是它; 太长由 store 那边再截）。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def output_handler(*, sender: LarkSender, store: StateStore,
                   clock: Callable[[], float] = time.time):
    """装配会话的输出回调: 一轮跑完的正文（或失败原因）回到它自己那个聊天。

    普通会话就是"原文回那个聊天"; 定时内部会话（chat_id 以 `timer:` 开头）多几条规则:

      - 这一轮是"分析完再决定发不发", 所以 ok=False（模型/工具/超时出错）只记日志,
        目标会话里保持安静 —— 只有连续失败到阈值时才提示一次, 免得配置坏了没人知道;
      - 正文是 `NO_PUSH`（模型判定本轮没有值得推的）就只记日志, 并发群里是空的;
      - 真发了的, 把"时间 + 一句话结论"记进去重清单, 下一轮带给模型;
      - 发送失败的**不记清单**, 下一轮还能再来一次。
    """
    alert_text = ("⚠️ 整点播报连续 {n} 轮失败，最近一次：{why}\n\n"
                  "详见机器人日志（logs/）。")

    async def handle_output(chat_id: str, text: str, ok: bool) -> None:
        if not chat_id.startswith(TIMER_PREFIX):
            await sender.send(chat_id, markdown=text if ok else f"⚠️ 分析失败\n\n{text}")
            return

        target = target_of(chat_id)
        if not ok:
            sys.stderr.write(f"[bot] 定时播报这一轮失败 target={target}: {_first_line(text)!r}\n")
            if store.note_failure(target):
                alert = alert_text.format(n=FAIL_STREAK_ALERT, why=_first_line(text))
                if await sender.send(target, markdown=alert):
                    store.mark_alerted(target)      # 发出去了才起算冷却
            return
        if is_no_push(text):
            sys.stderr.write(f"[bot] 定时播报无需推送 target={target}\n")
            store.clear_failures(target)
            return

        header = f"🕐 {time.strftime('%m-%d %H:%M', time.localtime(clock()))} 盯盘\n\n"
        message_id = await sender.send(target, markdown=header + text)
        if message_id:
            store.record_push(target, clock(), _first_line(text))
            store.clear_failures(target)
        else:
            sys.stderr.write(f"[bot] 定时播报发送失败, 这条不进去重清单 target={target}\n")

    return handle_output


async def run(*, chats: list[str], users: list[str], bot_open_id: str,
              app_id: str, app_secret: str,
              domain: str, project_dir: str,
              prompts_dir: str, prompt_file: str,
              api_key: str, api_base_url: str, model: str,
              turn_timeout: int, api_timeout: int, command_timeout: int,
              tool_output_limit: int, max_tool_rounds: int, max_parallel_tools: int,
              session_idle_timeout: int,
              timer_enabled: bool, timer_message: str, state_file: str,
              skills: list) -> None:
    """组装发送器和消息源, 然后一直监听, 直到监听结束。"""
    sys.stderr.write(f"[bot] 服务群 {', '.join(chats) or '无'}\n")
    sys.stderr.write(f"[bot] 服务用户（私聊）{', '.join(users) or '无'}, "
                     f"机器人 open_id={bot_open_id}\n")
    sys.stderr.write(f"[bot] 工作目录 {project_dir}, prompt 目录 {prompts_dir}\n")
    sys.stderr.write(f"[bot] 模型 {model} @ {api_base_url or 'SDK 默认'}\n")
    for skill in skills:
        sys.stderr.write(f"[bot] 技能 {skill.name}: {len(skill.body)} 字符正文, "
                         f"{len(skill.references)} 份参考文档（{skill.path}）\n")
    sys.stderr.write(f"[bot] 会话: 没有输入/输出 {session_idle_timeout}s 后清掉上下文; "
                     f"单轮上限 {turn_timeout}s / {max_tool_rounds} 次工具调用; "
                     f"同批最多并发 {max_parallel_tools} 个\n")

    # prompt 在这里再校验一次: 有问题应该现在炸, 而不是等到第一次要发消息
    prompt_path = check_prompt(prompts_dir, prompt_file)
    # 状态库: 关注列表 + 私聊会话表 + 推送清单 + 失败计数（建不了就启动失败, 退出码 2）
    store = StateStore(state_file)
    sys.stderr.write(f"[bot] 状态库 {state_file}\n")

    def instructions() -> str:
        """每段新对话开始时拼一次指令: prompt 文件（渲染占位符）+ 各技能正文。

        每次重新读盘, 所以改了 prompt 下一条新会话就生效; {datetime} 这类时间占位符
        也不会冻在进程启动那一刻（见 harness.ChatSession._ensure_system）。
        """
        return build_instructions(render_prompt(prompts_dir, prompt_file), skills)

    toolbox = Toolbox(references=reference_table(skills), command_timeout=command_timeout,
                      output_limit=tool_output_limit, program_path=resolve_program(),
                      watchlist=store)
    # 飞书 SDK 只认这三个; domain 空串表示用默认的国内站
    lark_config = {"app_id": app_id, "app_secret": app_secret, "domain": domain}
    sender = LarkSender(lark_config)
    chat = LarkChatSource(lark_config, chats=chats, users=users, bot_open_id=bot_open_id)

    # 会话注册表: 一个 chat_id 一条会话, 同一个聊天里连着问共享上下文, 长时间没动静就
    # 清掉历史重来。不同聊天之间互不可见（各自的 chat_id 就是各自的 key）。
    # 输出不走返回值, 由 output_handler 发回那个聊天（定时会话另有规则, 见它的说明）
    client = openai.AsyncOpenAI(api_key=api_key, base_url=api_base_url or None)
    sessions = ChatSessions(client=client, model=model, instructions=instructions,
                            toolbox=toolbox, turn_timeout=turn_timeout,
                            api_timeout=api_timeout, idle_timeout=session_idle_timeout,
                            max_tool_rounds=max_tool_rounds,
                            max_parallel_tools=max_parallel_tools,
                            on_output=output_handler(sender=sender, store=store))
    sys.stderr.write("[bot] 消息源 飞书官方 SDK 长连接, 发消息/改消息走官方 SDK\n")
    sys.stderr.write(f"[bot] prompt {prompt_path}\n")

    def timer_targets() -> list[str]:
        """整点的目标: 群白名单 + 白名单里已经见过的私聊。

        到点才枚举（不是启动时定死）—— 私聊会话是运行期学到的, 新用户第一次私聊之后
        下一个整点就能收到, 不用重启。去重, 保持配置顺序。
        """
        known = store.peers()
        targets = [*chats, *(known[open_id] for open_id in users if open_id in known)]
        seen: set[str] = set()
        return [target for target in targets if not (target in seen or seen.add(target))]

    timer = None
    if timer_enabled:
        timer = TimerSource(message=timer_message, targets=timer_targets,
                            watchlist=store.describe, recent=store.recent_pushes)
        sys.stderr.write("[bot] 整点播报: 每个整点给每个目标发一条（群白名单 + 见过的私聊）\n")

    # 事件 -> handler 的绑定集中在这一处; handler 的入参统一是 payload（见 core.events）
    core.on(TRIGGER, trigger_handler(sessions=sessions, sender=sender, store=store))
    core.on(TRIGGER_REJECTED, reject_handler(sender=sender))
    core.on(core.TASK_DONE, on_task_done)
    _log_registered()

    sys.stderr.write("[bot] 已就绪: 白名单群里 @ 机器人、白名单用户私聊即可触发 (Ctrl+C 结束)\n")
    if timer is not None:
        timer.start()
    try:
        # 收消息、判断该不该响应都在 lark_chat 里, 它自己 emit(TRIGGER / TRIGGER_REJECTED)
        await chat.run()
    finally:
        # 先停定时源（它可能正卡在睡眠里）
        if timer is not None:
            await timer.stop()
        # 收掉还挂着的后台任务（在跑的分析）, 让它清掉自己起的子进程
        await core.cancel_all()
        # 再把所有会话关掉（收计时器、停掉在跑的那一轮）
        await sessions.close_all()


def _log_registered() -> None:
    """启动时把"事件 -> handler"打出来, 免得注册关系变成隐式的。"""
    sys.stderr.write("[bot] 事件 -> handler:\n")
    for name, handlers in core.registered().items():
        sys.stderr.write(f"           {name}: {', '.join(handlers)}\n")


def main() -> None:
    """入口: 读 config.json, 拆成具名参数发给 run()。

    读配置 + 校验（凭据、prompt、技能、时区…）都发生在起监听之前,
    有问题就一句人话 + 退出码 2, 不带一堆 traceback。
    """
    if len(sys.argv) > 1:
        # 参数都搬进 config.json 了: 别让"看着像生效了其实被忽略"的命令行参数蒙混过去
        sys.stderr.write("[bot] 参数都在 config.json 里, 命令行不再接受参数: "
                         f"{' '.join(sys.argv[1:])}\n")
        raise SystemExit(2)
    try:
        cfg = load_config()
        setup_logging(cfg["log_file"])
        asyncio.run(run(
            chats=cfg["chats"],
            users=cfg["users"],
            bot_open_id=cfg["bot_open_id"],
            app_id=cfg["app_id"],
            app_secret=cfg["app_secret"],
            domain=cfg["domain"],
            project_dir=cfg["project_dir"],
            prompts_dir=cfg["prompts_dir"],
            prompt_file=cfg["prompt_file"],
            api_key=cfg["api_key"],
            api_base_url=cfg["api_base_url"],
            model=cfg["model"],
            turn_timeout=cfg["turn_timeout"],
            api_timeout=cfg["api_timeout"],
            command_timeout=cfg["command_timeout"],
            tool_output_limit=cfg["tool_output_limit"],
            max_tool_rounds=cfg["max_tool_rounds"],
            max_parallel_tools=cfg["max_parallel_tools"],
            session_idle_timeout=cfg["session_idle_timeout"],
            timer_enabled=cfg["timer_enabled"],
            timer_message=cfg["timer_message"],
            state_file=cfg["state_file"],
            skills=cfg["skills"],
        ))
    except KeyboardInterrupt:
        sys.stderr.write("\n[bot] 收到 Ctrl+C, 正在退出...\n")
        sys.stderr.write("[bot] 已停止\n")
    except ValueError as exc:
        sys.stderr.write(f"[bot] 配置有问题: {exc}\n")
        raise SystemExit(2) from None
    else:
        sys.stderr.write("[bot] 已停止\n")
