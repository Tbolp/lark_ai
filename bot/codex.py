"""调 codex 做一次分析, 返回 (是否成功, 文本)。跑完才返回, 中间过程不外发。

两条路:
  - app-server（默认）: 见 codex_app_server.py——常驻 session, 上下文在一条 thread 里累积;
  - exec（回退）: `codex exec --json`, app-server 起不来时用, 每次都是全新会话,
    技能只能靠 `$技能名` 标记。
两条路都只取最终正文, 结果由调用方发到飞书。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time

from .codex_app_server import READ_LINE_LIMIT, CodexAppServerSession, skill_mention
from .procs import decode_bytes, stop_process


async def _handle_codex_event(event: dict, texts: list[str]) -> None:
    """处理一条 codex exec --json 事件（只取模型正文, 工具调用等中间过程不要）。"""
    kind = event.get("type")
    if kind not in ("item.started", "item.updated", "item.completed"):
        return
    item = event.get("item")
    if not isinstance(item, dict):
        return
    if kind != "item.completed" or item.get("type") != "agent_message":
        return
    text = str(item.get("text") or "").strip()
    if not text:
        return
    texts.append(text)


async def _run_codex_exec(prompt: str, *, instructions: str, project_dir: str,
                          codex_timeout: int,
                          skills: list[tuple[str, str]],
                          label: str = "-") -> tuple[bool, str]:
    """codex exec --json: 逐行读 JSONL 事件, 模型正文在最后整块到达。

    exec 没有会话, 所以指令只能跟用户消息拼在一起发（app-server 那条路是指令在会话上）。
    """
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return False, "启动 Codex 失败：本机找不到 codex 命令。"

    cmd = [
        codex_bin,
        "exec",
        "--json",
        "-C",
        project_dir,
        "--skip-git-repo-check",
        "--color",
        "never",
        # exec 没有 skill 输入项, 只能靠 prompt 开头的 `$技能名` 显式调用
        "\n\n".join(part for part in (instructions, skill_mention(skills) + prompt) if part),
    ]
    sys.stderr.write(f"[codex] 启动分析 ({label}, exec 回退; 指令 {len(instructions)} 字符 + "
                     f"用户消息 {len(prompt)} 字符)\n")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 单独进程组, 超时能连同它起的子命令一起收掉
            limit=READ_LINE_LIMIT,
        )
    except OSError as exc:
        return False, f"启动 Codex 失败：{exc}"

    assert proc.stdout is not None and proc.stderr is not None
    stderr_lines: list[str] = []

    async def _drain_stderr() -> None:
        while raw := await proc.stderr.readline():
            text = decode_bytes(raw).rstrip()
            if text:
                stderr_lines.append(text)
                del stderr_lines[:-40]

    # stderr 必须同时在读, 否则 codex 写满了管道会卡住
    stderr_task = asyncio.create_task(_drain_stderr())

    texts: list[str] = []
    failures: list[str] = []
    timed_out = False
    deadline = time.monotonic() + codex_timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                # 工具在跑的时候没有新事件, 短暂等待后接着读
                raw = await asyncio.wait_for(proc.stdout.readline(), min(remaining, 1.0))
            except TimeoutError:
                continue
            if not raw:
                break
            line = decode_bytes(raw).strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.failed":
                detail = event.get("error") or event.get("message") or "turn.failed"
                if isinstance(detail, dict):
                    detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
                failures.append(str(detail).strip())
                continue
            await _handle_codex_event(event, texts)
    finally:
        await stop_process(proc)
        await asyncio.gather(stderr_task, return_exceptions=True)

    if timed_out:
        return False, f"Codex 分析超时（>{codex_timeout}s），请稍后重试。"

    answer = "\n\n".join(texts).strip()
    if answer:
        return True, answer
    if failures:
        return False, f"Codex 分析失败：{failures[-1][:600]}"
    if proc.returncode != 0:
        tail = "\n".join(stderr_lines)[-600:]
        return False, f"Codex 分析失败（exit={proc.returncode}）：\n{tail}"
    return False, "Codex 没有返回分析结果，请稍后重试。"


async def run_codex_analysis(prompt: str, *, project_dir: str, codex_timeout: int,
                             session: CodexAppServerSession,
                             skills: list[tuple[str, str]],
                             label: str = "-") -> tuple[bool, str]:
    """跑一次 codex 分析, 返回 (是否成功, 文本)。

    默认把消息发给常驻 session（能挂技能, 也记得前面聊过什么）;
    app-server 这条路过不去时回退到 codex exec --json（全新会话, 无上下文）。
    两条路都是跑完才返回, 中途的东西一律不外发。
    """
    result = await session.ask(prompt, skills=skills)
    if result is not None:
        return result
    return await _run_codex_exec(prompt, instructions=session.instructions(),
                                 project_dir=project_dir,
                                 codex_timeout=codex_timeout, skills=skills,
                                 label=label)
