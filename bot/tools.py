"""工具: 模型能调用的三件事。

  run_westock(command)                    跑一条 westock 命令, 把输出原样带回去
  read_reference(skill, name)             读某份技能 references/ 下的参考文档
  watchlist(action, code, name)           看 / 加 / 删"当前会话关注的股票"

安全边界写在这里, 不写进提示词 —— 提示词是"请求", 这里才是"约束":

  - run_westock 只放行 `westock ` 开头的**单条**命令, 且不允许管道、重定向、命令链
    （技能文档本来就要求"禁止终端截断输出", 这条在代码里落实, 不靠模型自觉）;
  - 命令用 create_subprocess_exec 直接执行（不过 shell）, argv 由 shlex 切好,
    所以就算放行了奇怪字符也没有 shell 来解释它;
  - read_reference 只认构造时登记过的那张表, 路径不来自模型;
  - watchlist 是**唯一的写操作**: 只写状态库里"这个会话的关注列表"那部分, 路径来自
    配置、不来自模型; 代码格式与每会话上限在 store 里校验（见 store.py）;
  - 两条路的输出都按 tool_output_limit 截断（头尾保留、中间标出省略了多少）,
    免得一次 `--fields all` 把上下文撑爆。

工具**永远不抛异常**: 出问题就把原因作为字符串返回给模型, 让它自己改。一轮分析不该
因为一条命令写错了就整轮失败。

需要"这是哪个会话"的工具从调用点拿到 chat_id（见 harness.ChatSession）; 定时内部会话
的 chat_id 是 `timer:<目标>`, 关注列表按去掉前缀后的目标会话存。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
from typing import Any

from .procs import decode_bytes, stop_process
from .timer import target_of

# 只放行这个程序: 两个技能（westock-data / westock-screener）都是打它
PROGRAM = "westock"

# 命令里不允许出现的字符: 管道 / 重定向 / 命令链 / 替换 / 换行。
# 不过 shell 并不意味着它们安全（它们会变成 westock 的参数, 得到莫名其妙的报错）,
# 这里明确拒掉, 给模型一句能看懂的话。
_FORBIDDEN = ("|", "&&", "||", ";", ">", "<", "`", "$(", "\n", "\r")

_TRUNCATED = "\n\n...[输出过长，中间省略 {omitted} 字符；如需完整内容请缩小查询范围]...\n\n"


def resolve_program(program: str = PROGRAM) -> str:
    """启动时把可执行文件定位到绝对路径。

    找不到就让进程起不来（退出码 2），而不是等每条分析都在工具里失败一次 —— 和技能
    找不到就报错是同一个道理。顺带也解决了 PATH：常驻进程（launchd / nohup）的 PATH
    常常和交互 shell 不一样，这里定死绝对路径就没这个坑了。
    """
    path = shutil.which(program)
    if not path:
        raise ValueError(f"找不到 {program} 命令（PATH 里没有）; 先把 westock CLI 装好")
    return path


def _truncate(text: str, limit: int) -> str:
    """超长输出保留头尾, 中间标出省略了多少 —— 直接砍尾会让模型看不到结尾的汇总行。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return text[:head] + _TRUNCATED.format(omitted=omitted) + text[-tail:]


class Toolbox:
    """一组工具: schema 给模型看, call() 负责执行并兜住所有异常。"""

    def __init__(self, *, references: dict[tuple[str, str], str],
                 command_timeout: float, output_limit: int,
                 program: str = PROGRAM, program_path: str | None = None,
                 watchlist: Any | None = None) -> None:
        self.references = references
        self.command_timeout = command_timeout
        self.output_limit = output_limit
        # program 用于白名单判定（模型写的是命令原文, 必须与技能文档一致）;
        # program_path 用于真正执行 —— 测试时指向一个假的可执行文件。
        self.program = program
        self.program_path = program_path or program
        # 关注列表的存储（StateStore）; 没配就不提供 watchlist 工具
        self.watchlist = watchlist
        self._schemas: list[dict[str, Any]] | None = None

    # ---------- schema ----------

    def schemas(self) -> list[dict[str, Any]]:
        """给模型的工具定义。read_reference 的文档名做成枚举, 模型的选题范围就是白名单。

        每次模型调用都会带上这份定义, 而它只取决于技能包（启动后不会变）, 所以建一次
        就缓存住 —— 一轮里要调好几次模型, 没必要重复拼。
        """
        if self._schemas is not None:
            return self._schemas
        refs: dict[str, list[str]] = {}
        for skill, name in sorted(self.references):
            refs.setdefault(skill, []).append(name)
        ref_help = "；".join(f"{skill}: {', '.join(names)}" for skill, names in refs.items())
        self._schemas = [
            {
                "type": "function",
                "function": {
                    "name": "run_westock",
                    "description": (
                        "执行一条 westock 命令并返回输出。command 要是完整的命令行，"
                        f"以 `{self.program} ` 开头，例如 `{self.program} quote sh600519`。"
                        "不支持管道、重定向和命令链。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": f"完整命令行，以 `{self.program} ` 开头",
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_reference",
                    "description": (
                        "读取技能的参考文档（命令语法、字段说明、场景速查等）。"
                        "只在不确定命令写法或字段含义时读，不要每次任务都读。"
                        f"可选值 —— {ref_help}"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string", "description": "技能名",
                                      "enum": sorted(refs)},
                            "name": {"type": "string", "description": "文档名",
                                     "enum": sorted({n for names in refs.values() for n in names})},
                        },
                        "required": ["skill", "name"],
                    },
                },
            },
        ]
        if self.watchlist is not None:
            self._schemas.append({
                "type": "function",
                "function": {
                    "name": "watchlist",
                    "description": (
                        "查看 / 添加 / 移除当前会话关注的股票。定时播报会额外看这些股票自己的新闻，"
                        "所以用户说「关注一下某某」时用它。action=add/remove 时要给带市场前缀的"
                        "代码（如 sh600519），名字先自己用 run_westock `westock search` 查到再一起给。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["list", "add", "remove"],
                                       "description": "list=看当前列表, add=加一只, remove=去掉一只"},
                            "code": {"type": "string",
                                     "description": "带市场前缀的代码, 如 sh600519; add/remove 必填"},
                            "name": {"type": "string",
                                     "description": "股票名称（可选, 只用于列表展示）"},
                        },
                        "required": ["action"],
                    },
                },
            })
        return self._schemas

    # ---------- 执行 ----------

    async def call(self, name: str, arguments: str, chat_id: str = "") -> str:
        """执行一次工具调用。arguments 是模型给的 JSON 字符串; 任何问题都变成一段说明。

        chat_id 是"这是哪个会话"（定时内部会话的 key 带 timer: 前缀）; 只有关心会话身份的
        工具用得到它, 其余工具忽略。
        """
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"参数不是合法 JSON: {exc}。请重新给出 {{\"command\": \"...\"}} 这样的参数。"
        if not isinstance(args, dict):
            return "参数得是一个 JSON 对象。"
        if name == "run_westock":
            return await self._run_westock(args.get("command"))
        if name == "read_reference":
            return self._read_reference(args.get("skill"), args.get("name"))
        if name == "watchlist":
            return self._watchlist(args.get("action"), args.get("code"), args.get("name"), chat_id)
        return f"没有叫 {name} 的工具。"

    # ---------- 关注列表 ----------

    def _watchlist(self, action: Any, code: Any, name: Any, chat_id: str) -> str:
        """看 / 加 / 删当前会话的关注列表。真正的校验与落盘在 store 里。"""
        if self.watchlist is None:
            return "没有配置关注列表（config.json 里的 state_file）。"
        if action not in ("list", "add", "remove"):
            return "action 只能是 list / add / remove。"
        # 定时内部会话（timer:<目标>）读写的还是那个目标会话的列表
        owner = target_of(chat_id)
        if action == "list":
            items = self.watchlist.watch(owner)
            if not items:
                return "这个会话还没有关注任何股票。"
            lines = [f"这个会话关注 {len(items)} 只股票："]
            lines.extend(f"{index}. {item['code']} {item['name']}".rstrip()
                         for index, item in enumerate(items, 1))
            return "\n".join(lines)
        if not isinstance(code, str) or not code.strip():
            return f"{action} 需要给出带市场前缀的代码，例如 sh600519。"
        if action == "add":
            ok, message = self.watchlist.add_watch(
                owner, code, name if isinstance(name, str) else "")
        else:
            ok, message = self.watchlist.remove_watch(owner, code)
        return message

    async def _run_westock(self, command: Any) -> str:
        if not isinstance(command, str) or not command.strip():
            return "command 得是非空字符串。"
        command = command.strip()
        if not command.startswith(f"{self.program} ") and command != self.program:
            return f"只允许 {self.program} 命令，且必须以 `{self.program} ` 开头，收到: {command!r}"
        bad = [ch for ch in _FORBIDDEN if ch in command]
        if bad:
            return (f"命令里不能出现 {' '.join(repr(ch) for ch in bad)}："
                    "不支持管道、重定向和命令链；取数输出也不要截断，条数交给 --limit/--offset。")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"命令解析失败（引号没配对？）: {exc}"
        if not argv or argv[0] != self.program:
            return f"只允许 {self.program} 命令，收到: {command!r}"
        # 执行用的是 program_path（测试时是假的）, 其余参数原样传
        argv[0] = self.program_path
        return await self._exec(argv, label=command)

    async def _exec(self, argv: list[str], *, label: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,          # 单独进程组: 超时能收掉整棵树
            )
        except OSError as exc:
            return f"起不了 {self.program} 命令: {exc}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.command_timeout)
        except TimeoutError:
            await stop_process(proc)
            return f"[超时 {self.command_timeout:g}s 已中止] {label}"
        except BaseException:
            # 被取消（这一轮超时 / 进程退出 / 同批的其他调用把 gather 带崩）也要收掉子进程。
            # 少了这一步, 并发批量里被取消的那几条会留下没人管的 westock 进程。
            await stop_process(proc)
            raise
        stdout = decode_bytes(out).strip()
        stderr = decode_bytes(err).strip()
        # 退出码放进输出里: 模型据此判断"没数据"和"命令写错了"是两回事
        parts = [f"[exit={proc.returncode}]"]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if len(parts) == 1:
            parts.append("(没有输出)")
        text = "\n".join(parts)
        if proc.returncode:
            sys.stderr.write(f"[tools] {label} 退出码 {proc.returncode}\n")
        return _truncate(text, self.output_limit)

    def _read_reference(self, skill: Any, name: Any) -> str:
        if not isinstance(skill, str) or not isinstance(name, str):
            return "skill 与 name 都得是字符串。"
        path = self.references.get((skill, name))
        if path is None:
            known = "、".join(f"{s}/{n}" for s, n in sorted(self.references))
            return f"没有这份参考文档: {skill}/{name}。可用的有: {known}"
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError as exc:
            return f"读不到 {skill}/{name}: {exc}"
        if not text:
            return f"{skill}/{name} 是空文件。"
        return _truncate(text, self.output_limit)
