"""顶层流程: 读配置 + 事件监听 + @ 触发 + 组装启动。

配置只在 main() 里读一次（config.json）, 之后一律按具名参数往下传。
run() 是入口: 校验凭据和 prompt -> 建发送器和群消息源 -> 跑监听, 直到监听结束。
一次 @ 的路径: 跑完 codex 才发消息, 所以每次 @ 在群里只产生一条结果消息。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from core import events as core

from .codex import run_codex_analysis
from .codex_app_server import CodexAppServerSession, resolve_skills
from .events import MENTION
from .lark_chat import LarkChatSource
from .lark_sender import LarkSender
from .logging_tee import setup_logging
from .prompts import check_prompt, read_prompt

# 配置文件固定在项目根目录（bot/ 的上一层）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")

# @ 触发之间互斥: 正在分析时又来一条 @, 直接回一句"稍等", 不排队也不重复跑。
# 用一个 Event 当"有人在做分析"的标志: 检查 is_set() 和 set() 之间没有 await, 不会撞车。
MENTION_BUSY = asyncio.Event()


# ---------- 配置 ----------

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


def _text(raw: dict, key: str, path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} 里的 {key} 得是非空字符串")
    return value


def _path(raw: dict, key: str, base: str, path: str) -> str:
    """路径字段: 相对路径按"配置文件所在目录"展开, 所以从哪启动都一样。"""
    value = _text(raw, key, path)
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(base, value))


def _optional_text(raw: dict, key: str, path: str) -> str:
    """可选字符串: 没有就是空串, 有就得是字符串。"""
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{path} 里的 {key} 得是字符串")
    return value


def _skills(raw: dict, key: str, path: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ValueError(f"{path} 里的 {key} 得是字符串数组")
    return value


def load_config(path: str = CONFIG_PATH) -> dict:
    """读 config.json 并做基本校验; 有问题抛 ValueError（启动时就炸）。"""
    raw = _read_json(path, "配置文件")
    base = os.path.dirname(os.path.abspath(path))
    cfg = {
        "chat_id": _text(raw, "chat_id", path),
        "bot_open_id": _text(raw, "bot_open_id", path),
        "app_id": _text(raw, "app_id", path),
        "app_secret": _text(raw, "app_secret", path),
        "domain": _optional_text(raw, "domain", path),
        "project_dir": _path(raw, "project_dir", base, path),
        "prompts_dir": _path(raw, "prompts_dir", base, path),
        "mention_prompt": _text(raw, "mention_prompt", path),
        "codex_timeout": raw.get("codex_timeout"),
        "session_idle_timeout": raw.get("session_idle_timeout"),
        "skills": _skills(raw, "skills", path),
        "log_file": _path(raw, "log_file", base, path),
    }
    for key in ("codex_timeout", "session_idle_timeout"):
        value = cfg[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{path} 里的 {key} 得是正整数（秒）")
    for key in ("project_dir", "prompts_dir"):
        if not os.path.isdir(cfg[key]):
            raise ValueError(f"{path} 里的 {key} 不是目录: {cfg[key]}")
    # 技能名在这里就解析成 SKILL.md 路径: 配置里写了却装不上, 启动时就炸
    cfg["skills"] = resolve_skills(cfg["skills"], project_dir=cfg["project_dir"])
    return cfg


def on_task_done(done: core.Done) -> None:
    """任务结束（同步 handler, 当场执行）: 没成功就记一行, 成功的不用吭声。"""
    if done.status != "ok":
        sys.stderr.write(f"[bot] 任务 {done.name} {done.status}（{done.elapsed:.1f}s）\n")


def mention_handler(*, chat_id: str, project_dir: str, codex_timeout: int,
                    session: CodexAppServerSession,
                    skills: list[tuple[str, str]],
                    sender: LarkSender):
    """装配 @ 的处理流程: 上面这些是启动期定下来的东西, handler 只认 payload（见 core.events）。"""

    async def handle_mention(event: dict) -> None:
        """@ 触发的处理: 排他判断 -> 跑分析 -> 回帖（失败也要有回音）。"""
        text = event.get("text", "")
        sys.stderr.write(
            f"[bot] <- @触发 {event.get('message_id')} by {event.get('sender_id')}: {text!r}\n"
        )

        # 一次只跑一个 @: 检查 is_set() 和 set() 之间没有 await, 不会撞车。
        # 分析期间再有人 @, 就把那句话追加进正在跑的那一轮（codex 接着往下做）,
        # 追加不了（还没开跑 / 走的是 exec 回退）才回一句"稍等"。
        if MENTION_BUSY.is_set():
            if text and await session.steer(text, skills=skills):
                sys.stderr.write(f"[bot] → 追加到进行中的分析: {text!r}\n")
                return
            await sender.send(chat_id, text="上一条分析还在进行中，请稍等片刻再 @ 我。")
            return
        MENTION_BUSY.set()
        try:
            # 指令在会话上（建会话时就传进去了）, 这里只把群里那句话发过去
            ok, answer = await run_codex_analysis(
                text, project_dir=project_dir, codex_timeout=codex_timeout,
                session=session, skills=skills, label="mention",
            )
            await sender.send(chat_id, markdown=answer if ok else f"⚠️ 分析失败\n\n{answer}")
        except Exception as exc:  # 后台任务出错不能让机器人哑掉
            sys.stderr.write(f"[bot] @ 分析异常: {exc!r}\n")
            try:
                await sender.send(chat_id,
                                  markdown=f"⚠️ 分析过程中出错了，请稍后重试。\n\n{exc!r}")
            except Exception as send_exc:
                sys.stderr.write(f"[bot] 连报错消息都没发出去: {send_exc!r}\n")
        finally:
            MENTION_BUSY.clear()

    return handle_mention


async def run(*, chat_id: str, bot_open_id: str, app_id: str, app_secret: str,
              domain: str, project_dir: str,
              prompts_dir: str, mention_prompt: str,
              codex_timeout: int, session_idle_timeout: int,
              skills: list[tuple[str, str]]) -> None:
    """组装发送器和群消息源, 然后一直监听, 直到监听结束。"""
    sys.stderr.write(f"[bot] 监听群 {chat_id}, 机器人 open_id={bot_open_id}\n")
    sys.stderr.write(f"[bot] 工作目录 {project_dir}, prompt 目录 {prompts_dir}\n")
    sys.stderr.write(f"[bot] 技能 {', '.join(n for n, _ in skills) or '无'}\n")
    sys.stderr.write(f"[bot] codex 会话: 空闲 {session_idle_timeout}s 后关掉\n")

    # prompt 在这里再校验一次: 有问题应该现在炸, 而不是等到第一次要发消息
    prompt_path = check_prompt(prompts_dir, mention_prompt)
    # 飞书 SDK 只认这三个; domain 空串表示用默认的国内站
    lark_config = {"app_id": app_id, "app_secret": app_secret, "domain": domain}
    sender = LarkSender(lark_config)
    chat = LarkChatSource(lark_config, chat_id=chat_id, bot_open_id=bot_open_id)
    # 常驻会话: 群里连着问就是同一条 thread（能追问）, 空闲久了自动关掉重来
    session = CodexAppServerSession(project_dir=project_dir, codex_timeout=codex_timeout,
                                    idle_timeout=session_idle_timeout,
                                    instructions=lambda: read_prompt(prompts_dir,
                                                                     mention_prompt))
    sys.stderr.write("[bot] 群消息源 飞书官方 SDK 长连接, 发消息/改消息走官方 SDK\n")
    sys.stderr.write(f"[bot] prompt {prompt_path}\n")

    # 事件 -> handler 的绑定集中在这一处; handler 的入参统一是 payload（见 core.events）
    core.on(MENTION, mention_handler(
        chat_id=chat_id, project_dir=project_dir, codex_timeout=codex_timeout,
        session=session, skills=skills, sender=sender,
    ))
    core.on(core.TASK_DONE, on_task_done)
    _log_registered()

    sys.stderr.write("[bot] 已就绪: 群里 @ 机器人即可触发分析 (Ctrl+C 结束)\n")
    try:
        # 收消息、判断是不是 @ 触发都在 lark_chat 里, 触发时它自己 emit(MENTION)
        await chat.run()
    finally:
        # 收掉还挂着的后台任务（在跑的分析）, 让它清掉自己起的子进程
        await core.cancel_all()
        # 再把常驻的 codex 会话关掉（否则 app-server 进程会留着）
        await session.close()


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
            chat_id=cfg["chat_id"],
            bot_open_id=cfg["bot_open_id"],
            app_id=cfg["app_id"],
            app_secret=cfg["app_secret"],
            domain=cfg["domain"],
            project_dir=cfg["project_dir"],
            prompts_dir=cfg["prompts_dir"],
            mention_prompt=cfg["mention_prompt"],
            codex_timeout=cfg["codex_timeout"],
            session_idle_timeout=cfg["session_idle_timeout"],
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
