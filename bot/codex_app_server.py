"""codex app-server 客户端（stdio 上的 JSON-RPC）。

一个常驻 session = 一个 app-server 进程 + 一条 thread。群里每来一条 @ 就往这条
thread 里发一个 turn, 所以 codex 记得前面问过什么（追问能接上）。

指令（prompt）在 thread/start 时作为 developer instructions 传进去 —— 相当于
system prompt: 它不在对话历史里, 不会被压缩掉, 每轮自动生效。用户那句话则是每轮的输入。

空闲超过 idle_timeout 秒没有新消息就把进程关掉, 下一条消息重新起一个 —— 会话的边界
由"有没有人来问"决定, 不存 threadId（不 resume: 进程一停, 上下文就结束了）。
工具调用、推理这些中间过程不取, 发到飞书群里的只有最终答案。
这个文件只管协议细节, "什么时候用它、用不了怎么办" 在 codex.py 里。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from typing import Callable

from .procs import decode_bytes, stop_process

# 读子进程 stdout 时的单行上限; asyncio 默认只有 64 KB, codex 的事件行可能更长
READ_LINE_LIMIT = 4 * 1024 * 1024

# 起 app-server 的握手超时（超过就回退到 codex exec）
APPSERVER_INIT_TIMEOUT = 25.0

# 技能搜索目录: 用户级 / 项目级（跟 codex 自己的发现规则一致）
SKILL_ROOTS = ("~/.agents/skills", ".agents/skills")


def resolve_skills(names: list[str], *, project_dir: str) -> list[tuple[str, str]]:
    """把配置里的技能名解析成 (name, SKILL.md 路径); 有一个找不到就报错。

    启动时校验用: 配置里写了技能却装不上, 宁可现在炸, 也不要每条分析都少了个技能。
    """
    roots = [os.path.expanduser(SKILL_ROOTS[0]), os.path.join(project_dir, SKILL_ROOTS[1])]
    found: list[tuple[str, str]] = []
    for name in names:
        for root in roots:
            path = os.path.join(root, name, "SKILL.md")
            if os.path.isfile(path):
                found.append((name, path))
                break
        else:
            raise ValueError(f"找不到技能 {name}（找过 {'、'.join(roots)}）")
    return found


def skill_mention(skills: list[tuple[str, str]]) -> str:
    """prompt 开头的 `$技能名` 显式调用标记（入参是 resolve_skills() 的结果）。"""
    return "".join(f"${name} " for name, _ in skills)


def input_items(user_text: str, skills: list[tuple[str, str]]) -> list[dict]:
    """turn 的输入项: 群里那句话 + 显式挂上的技能（指令在 thread 上, 不在这里）。

    文本开头的 `$技能名` 是显式调用标记; 技能项让 app-server 直接把完整的
    SKILL.md 指令注入进来, 不用模型自己按名字去找（后者更慢也更容易漏）。
    """
    if not skills:
        return [{"type": "text", "text": user_text}]
    items: list[dict] = [{"type": "text", "text": skill_mention(skills) + user_text}]
    items += [{"type": "skill", "name": name, "path": path} for name, path in skills]
    return items


class CodexAppServerSession:
    """常驻会话: 一个 app-server 进程 + 一条 thread, 一条条往里发消息。

    对外的三个口子:
      ensure_started()  进程不在（或者已经死了）就起一个, initialize + thread/start
      ask()             发一条 turn, 一直读到 turn/completed, 返回最终正文
      close()           关掉进程; 空闲超时、超时、出错都走这里, 下次 ask 自然新开一条
    """

    def __init__(self, *, project_dir: str, codex_timeout: float, idle_timeout: float,
                 instructions: Callable[[], str]) -> None:
        self.project_dir = project_dir
        self.codex_timeout = codex_timeout
        self.idle_timeout = idle_timeout
        # 建会话时才调它一次: 改了 prompt 文件, 下一条新会话生效, 已建立的会话不受影响
        self.instructions = instructions
        self.proc: asyncio.subprocess.Process | None = None
        self.inbox: "asyncio.Queue[dict | None]" = asyncio.Queue()
        self.readers: list[asyncio.Task] = []
        self.stderr_lines: list[str] = []
        self.next_id = 1
        self.thread_id = ""
        self.turn_id = ""                  # 正在跑的那条 turn（追问要指定它）
        self.items: dict[str, dict] = {}   # item_id -> {"text": str, "phase": str|None}
        self.turn_status = ""
        self.error = ""
        self._idle_handle: asyncio.TimerHandle | None = None

    # ---------- 传输 ----------
    async def _start_proc(self) -> bool:
        codex_bin = shutil.which("codex")
        if not codex_bin:
            return False
        try:
            self.proc = await asyncio.create_subprocess_exec(
                codex_bin, "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_dir,
                start_new_session=True,
                limit=READ_LINE_LIMIT,
            )
        except OSError as exc:
            sys.stderr.write(f"[codex] app-server 启动失败: {exc}\n")
            return False
        # 每起一个新进程都要换一个新队列: 上一个进程结束时读线程会往里放一个 None,
        # 留着它的话新进程的第一个请求会立刻读到"流结束"
        self.inbox = asyncio.Queue()
        self.readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        return True

    async def _read_stdout(self) -> None:
        """把 app-server 的 JSON-RPC 消息塞进 inbox; 流结束时放一个 None。"""
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while raw := await self.proc.stdout.readline():
                line = decode_bytes(raw).strip()
                if not line:
                    continue
                try:
                    await self.inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            await self.inbox.put(None)

    async def _read_stderr(self) -> None:
        """只留最近几十行 stderr, 失败时用来解释原因。"""
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while raw := await self.proc.stderr.readline():
                text = decode_bytes(raw).rstrip()
                if text:
                    self.stderr_lines.append(text)
                    del self.stderr_lines[:-40]
        except (OSError, ValueError):
            pass

    async def _close_proc(self) -> None:
        """收掉进程和两个读管道; 可以重复调用。"""
        for reader in self.readers:
            reader.cancel()
        if self.readers:
            await asyncio.gather(*self.readers, return_exceptions=True)
            self.readers = []
        if self.proc is not None:
            await stop_process(self.proc)
            self.proc = None

    async def _send(self, payload: dict) -> bool:
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self.proc.stdin.write(line.encode("utf-8"))
            await self.proc.stdin.drain()
            return True
        except (OSError, ValueError):
            return False

    async def _request(self, method: str, params: dict, timeout: float) -> dict | None:
        """发一个请求并等它的响应; 期间收到的事件照常处理。"""
        req_id = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        if not await self._send(payload):
            return None
        return await self._pump(lambda msg: msg.get("id") == req_id, timeout)

    async def _pump(self, done, timeout: float) -> dict | None:
        """读消息直到 done(msg) 为真; 超时或进程结束返回 None。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(self.inbox.get(), min(remaining, 0.5))
            except TimeoutError:
                continue
            if msg is None:
                return None
            await self._dispatch(msg)
            # 先处理再判断: 状态类通知（如 turn/completed）是在处理里更新的
            if done(msg):
                return msg

    async def _dispatch(self, msg: dict) -> None:
        """把一条 app-server 通知归拢成"最终正文 / 失败原因 / 结束状态"。"""
        method = msg.get("method")
        if not method:
            # 我们自己发的请求的响应。只在这里记错误: turn/steer 是"发完不管"的,
            # 它的响应回到这里, 不能当成服务端的反向请求去回。
            if msg.get("error"):
                sys.stderr.write(f"[codex] 请求出错: "
                                 f"{json.dumps(msg['error'], ensure_ascii=False)[:200]}\n")
            return
        if "id" in msg:                     # 服务端反向请求（审批等）
            await self._answer_server_request(msg)
            return
        params = msg.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                self.items[item.get("id")] = {"text": item.get("text") or "",
                                              "phase": item.get("phase")}
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self.turn_status = (turn.get("status") or "").strip()
            self.turn_id = ""
            turn_error = turn.get("error") or {}
            if isinstance(turn_error, dict) and turn_error.get("message"):
                self.error = str(turn_error["message"])
        elif method == "error":
            detail = params.get("error") or params.get("message") or params
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            self.error = str(detail)

    async def _answer_server_request(self, msg: dict) -> None:
        """机器人场景没人能点审批, 一律拒绝（与 codex exec 非交互时的行为一致）。"""
        method = str(msg.get("method") or "")
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "applyPatchApproval", "execCommandApproval"):
            result: dict = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        else:
            await self._send({"jsonrpc": "2.0", "id": msg.get("id"),
                              "error": {"code": -32601, "message": f"unsupported: {method}"}})
            return
        sys.stderr.write(f"[codex] 拒绝审批请求 {method}\n")
        await self._send({"jsonrpc": "2.0", "id": msg.get("id"), "result": result})

    # ---------- 主流程 ----------
    async def ensure_started(self) -> bool:
        """进程在、thread 在、还活着就直接用; 否则起一个新的。"""
        if self.proc is not None and self.proc.returncode is None and self.thread_id:
            return True
        await self._close_proc()        # 收拾可能残留的旧进程
        self.thread_id = ""
        self.stderr_lines = []

        if not await self._start_proc():
            return False
        hello = await self._request("initialize", {
            "clientInfo": {"name": "stock-codex-bot", "title": "stock-codex-bot",
                           "version": "1.0"},
        }, APPSERVER_INIT_TIMEOUT)
        if not hello or "result" not in hello:
            sys.stderr.write("[codex] app-server 握手失败, 回退到 codex exec\n")
            await self._close_proc()
            return False
        await self._send({"jsonrpc": "2.0", "method": "initialized"})

        # 指令走 developer instructions（system prompt 那一层）, 不进对话历史
        params: dict = {"cwd": self.project_dir}
        instructions = self.instructions()
        if instructions:
            params["developerInstructions"] = instructions
        started = await self._request("thread/start", params, APPSERVER_INIT_TIMEOUT)
        thread_id = (((started or {}).get("result") or {}).get("thread") or {}).get("id")
        if not thread_id:
            sys.stderr.write("[codex] app-server thread/start 失败, 回退到 codex exec\n")
            await self._close_proc()
            return False
        self.thread_id = thread_id
        sys.stderr.write(f"[codex] 新 session (thread {thread_id})\n")
        return True

    async def ask(self, user_text: str, *, skills: list[tuple[str, str]]
                  ) -> tuple[bool, str] | None:
        """把群里那句话作为一轮输入发进当前 thread, 返回 (是否成功, 文本)。

        返回 None 表示 app-server 这条路过不去（起不来 / 握手失败）, 调用方可以回退到 exec。
        """
        self._cancel_idle()
        if not await self.ensure_started():
            return None

        # 每轮开始前清空: 上一次的回答不能混进这一次的正文
        self.items.clear()
        self.turn_status = ""
        self.error = ""

        turn = await self._request("turn/start", {
            "threadId": self.thread_id,
            "input": input_items(user_text, skills),
        }, APPSERVER_INIT_TIMEOUT)
        if not turn or "result" not in turn:
            await self.close()
            return False, f"Codex 分析失败：{json.dumps(turn, ensure_ascii=False)[:600]}"
        self.turn_id = (((turn or {}).get("result") or {}).get("turn") or {}).get("id") or ""

        # turn/completed 一到就收工; 期间 _dispatch 把最终正文收进 items
        if await self._pump(lambda msg: self.turn_status != "", self.codex_timeout) is None:
            # 超时: 那边可能还挂着一条 in-flight turn, 整条会话不要了（下次自然新开）
            await self.close()
            return False, f"Codex 分析超时（>{self.codex_timeout}s），请稍后重试。"

        answer = "\n\n".join(self.answer_texts()).strip()
        if answer:
            self._schedule_idle()
            return True, answer

        error = self.error or (f"Codex 分析中断（{self.turn_status}）"
                               if self.turn_status in ("failed", "interrupted") else "")
        await self.close()
        if error:
            tail = "\n".join(self.stderr_lines)[-600:]
            return False, f"Codex 分析失败：{error[:600]}\n{tail}" if tail else f"Codex 分析失败：{error[:600]}"
        return False, "Codex 没有返回分析结果，请稍后重试。"

    async def close(self) -> None:
        """关掉进程（空闲超时、turn 超时、出错都走这里）。"""
        self._cancel_idle()
        self.thread_id = ""
        self.turn_id = ""
        await self._close_proc()

    async def steer(self, user_text: str, *, skills: list[tuple[str, str]]) -> bool:
        """把一句话追加到**正在跑的那条 turn** 里（群里有人插话时用）。

        和 ask() 的区别: 不新起一轮, 也不等结果 —— 那句话会作为同一轮的补充输入,
        codex 接着往下跑, 最终答案还是一条。返回 False 表示当前没有可追加的 turn
        （还没开始 / 已经跑完 / 走的是 exec 回退）, 调用方该按"稍等"处理。

        这里只往 stdin 写、不读: 读管道归 ask() 的泵独占, 响应回到 _dispatch 里丢掉。
        """
        if not self.turn_id or self.proc is None or self.proc.returncode is not None:
            return False
        req_id = self.next_id
        self.next_id += 1
        sent = await self._send({
            "jsonrpc": "2.0", "id": req_id, "method": "turn/steer",
            "params": {
                "threadId": self.thread_id,
                "input": input_items(user_text, skills),
                "expectedTurnId": self.turn_id,
            },
        })
        if sent:
            sys.stderr.write(f"[codex] 追问已追加到当前分析（turn {self.turn_id}）\n")
        return sent

    # ---------- 空闲超时 ----------
    def _schedule_idle(self) -> None:
        """一条消息处理完就开始计时: idle_timeout 秒内没人来就自己关掉。"""
        self._cancel_idle()
        self._idle_handle = asyncio.get_running_loop().call_later(self.idle_timeout,
                                                                  self._on_idle)

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _on_idle(self) -> None:
        self._idle_handle = None
        sys.stderr.write(f"[codex] 空闲 {self.idle_timeout:.0f}s, 关掉 session\n")
        asyncio.create_task(self.close())

    def answer_texts(self) -> list[str]:
        """取最终答案: 带 final_answer 标记的就只取它, 否则取全部正文。"""
        finals = [item["text"] for item in self.items.values()
                  if item.get("phase") == "final_answer" and item.get("text")]
        if finals:
            return finals
        return [item["text"] for item in self.items.values() if item.get("text")]
