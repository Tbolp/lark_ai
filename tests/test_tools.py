"""工具（Toolbox）: 白名单、执行、截断、参考文档。"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from bot.store import StateStore
from bot.tools import Toolbox, resolve_program


def test_resolve_program_fails_loudly_when_missing():
    """启动时定位可执行文件: 找不到就报错, 而不是每条分析失败一次。"""
    with pytest.raises(ValueError) as excinfo:
        resolve_program("definitely-not-a-real-command")
    assert "找不到" in str(excinfo.value)


def fake_program(tmp_path, body: str) -> str:
    """造一个假的可执行文件顶上 westock（测试不碰真的 CLI、不碰网络）。"""
    path = tmp_path / "westock"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def make_toolbox(tmp_path, *, body: str = 'echo "$@"', references=None,
                 timeout: float = 5.0, limit: int = 200, watchlist=None) -> Toolbox:
    return Toolbox(references=references or {}, command_timeout=timeout,
                   output_limit=limit, program_path=fake_program(tmp_path, body),
                   watchlist=watchlist)


def run(toolbox: Toolbox, name: str, arguments: dict, chat_id: str = "") -> str:
    return asyncio.run(toolbox.call(name, json.dumps(arguments), chat_id=chat_id))


def write_ref(tmp_path, name: str, text: str) -> str:
    path = tmp_path / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------- schema ----------

def test_schemas_describe_both_tools(tmp_path):
    names = [t["function"]["name"] for t in make_toolbox(tmp_path).schemas()]
    assert names == ["run_westock", "read_reference"]


def test_read_reference_enum_lists_whitelisted_names(tmp_path):
    refs = {("a", "commands"): "/tmp/x.md", ("b", "fields"): "/tmp/y.md"}
    schema = make_toolbox(tmp_path, references=refs).schemas()[1]["function"]["parameters"]
    assert schema["properties"]["skill"]["enum"] == ["a", "b"]
    assert schema["properties"]["name"]["enum"] == ["commands", "fields"]


# ---------- run_westock ----------

def test_runs_the_command_and_reports_exit_code(tmp_path):
    out = run(make_toolbox(tmp_path), "run_westock", {"command": "westock quote sh600519"})
    assert out.startswith("[exit=0]")
    assert "quote sh600519" in out            # 参数原样传给了程序


def test_rejects_command_without_the_program_prefix(tmp_path):
    out = run(make_toolbox(tmp_path), "run_westock", {"command": "quote sh600519"})
    assert "只允许 westock 命令" in out


def test_rejects_other_programs(tmp_path):
    out = run(make_toolbox(tmp_path), "run_westock", {"command": "curl https://x"})
    assert "只允许 westock 命令" in out


@pytest.mark.parametrize("command", [
    "westock quote sh600519 | head -5",
    "westock quote sh600519 && rm -rf /",
    "westock quote sh600519; date",
    "westock quote sh600519 > out.txt",
    "westock quote $(whoami)",
])
def test_rejects_shell_metacharacters(tmp_path, command):
    out = run(make_toolbox(tmp_path), "run_westock", {"command": command})
    assert "不支持管道、重定向和命令链" in out


def test_rejects_empty_command(tmp_path):
    assert "非空字符串" in run(make_toolbox(tmp_path), "run_westock", {"command": "  "})


def test_reports_nonzero_exit(tmp_path):
    out = run(make_toolbox(tmp_path, body='echo "bad code"; exit 3'),
              "run_westock", {"command": "westock quote nope"})
    assert out.startswith("[exit=3]")
    assert "bad code" in out


def test_reports_stderr(tmp_path):
    out = run(make_toolbox(tmp_path, body='echo "出错了" >&2; exit 1'),
              "run_westock", {"command": "westock quote sh600519"})
    assert "[stderr]" in out and "出错了" in out


def test_command_alone_is_allowed(tmp_path):
    out = run(make_toolbox(tmp_path), "run_westock", {"command": "westock"})
    assert out.startswith("[exit=0]")


def test_times_out_and_kills_the_process(tmp_path):
    out = run(make_toolbox(tmp_path, body="sleep 30", timeout=0.3),
              "run_westock", {"command": "westock quote sh600519"})
    assert "超时" in out


def test_truncates_long_output_keeping_head_and_tail(tmp_path):
    body = 'printf "HEAD"; i=0; while [ $i -lt 500 ]; do printf "x"; i=$((i+1)); done; printf "TAIL"'
    out = run(make_toolbox(tmp_path, body=body, limit=100), "run_westock",
              {"command": "westock quote sh600519"})
    assert len(out) < 300
    assert "HEAD" in out and "TAIL" in out
    assert "中间省略" in out


def test_missing_executable_is_reported(tmp_path):
    toolbox = Toolbox(references={}, command_timeout=5, output_limit=200,
                      program_path=str(tmp_path / "nope"))
    out = run(toolbox, "run_westock", {"command": "westock quote sh600519"})
    assert "起不了 westock 命令" in out


def test_concurrent_calls_overlap(tmp_path):
    """三条命令各睡 0.2 秒: 并发跑总耗时应该明显低于串行的 0.6 秒。"""
    toolbox = make_toolbox(tmp_path, body="sleep 0.2", timeout=5)

    async def scenario():
        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(toolbox.call("run_westock",
                                            json.dumps({"command": f"westock q{i}"}))
                               for i in range(3)))
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(scenario()) < 0.5


def test_cancelling_a_call_kills_the_process(tmp_path):
    """被取消（这一轮超时 / 退出）时子进程也要收掉, 不能留下没人管的 westock。"""
    pid_file = tmp_path / "pid"
    toolbox = make_toolbox(tmp_path, body=f'echo $$ > "{pid_file}"; sleep 30', timeout=30)

    async def scenario():
        task = asyncio.create_task(
            toolbox.call("run_westock", json.dumps({"command": "westock quote sh600519"})))
        for _ in range(300):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        pid = int(pid_file.read_text().strip())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(300):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        pytest.fail("取消之后子进程还活着")

    asyncio.run(scenario())


# ---------- read_reference ----------

def test_reads_a_whitelisted_reference(tmp_path):
    refs = {("demo", "commands"): write_ref(tmp_path, "commands", "命令清单")}
    out = run(make_toolbox(tmp_path, references=refs), "read_reference",
              {"skill": "demo", "name": "commands"})
    assert out == "命令清单"


def test_same_name_in_two_skills_does_not_cross(tmp_path):
    refs = {("a", "guide"): write_ref(tmp_path, "a", "a 的说明"),
            ("b", "guide"): write_ref(tmp_path, "b", "b 的说明")}
    toolbox = make_toolbox(tmp_path, references=refs)
    assert run(toolbox, "read_reference", {"skill": "a", "name": "guide"}) == "a 的说明"
    assert run(toolbox, "read_reference", {"skill": "b", "name": "guide"}) == "b 的说明"


def test_unknown_reference_lists_what_is_available(tmp_path):
    refs = {("demo", "commands"): write_ref(tmp_path, "commands", "x")}
    out = run(make_toolbox(tmp_path, references=refs), "read_reference",
              {"skill": "demo", "name": "nope"})
    assert "没有这份参考文档" in out and "demo/commands" in out


def test_reference_is_truncated(tmp_path):
    refs = {("demo", "big"): write_ref(tmp_path, "big", "H" + "x" * 500 + "T")}
    out = run(make_toolbox(tmp_path, references=refs, limit=100), "read_reference",
              {"skill": "demo", "name": "big"})
    assert "中间省略" in out


# ---------- 参数与未知工具 ----------

def test_bad_json_is_reported(tmp_path):
    out = asyncio.run(make_toolbox(tmp_path).call("run_westock", "{not json"))
    assert "不是合法 JSON" in out


def test_unknown_tool_is_reported(tmp_path):
    out = run(make_toolbox(tmp_path), "nope", {})
    assert "没有叫 nope 的工具" in out


# ---------- 关注列表工具 ----------

def make_watch_toolbox(tmp_path):
    """带状态库的工具盒（关注列表是唯一会写东西的工具）。"""
    return make_toolbox(tmp_path, watchlist=StateStore(str(tmp_path / "state.db")))


def test_watchlist_tool_is_only_offered_when_configured(tmp_path):
    plain = [t["function"]["name"] for t in make_toolbox(tmp_path).schemas()]
    with_store = [t["function"]["name"] for t in make_watch_toolbox(tmp_path).schemas()]
    assert plain == ["run_westock", "read_reference"]
    assert with_store == ["run_westock", "read_reference", "watchlist"]
    assert "没有配置关注列表" in run(make_toolbox(tmp_path), "watchlist", {"action": "list"},
                                    chat_id="oc_g")


def test_watchlist_tool_add_list_remove(tmp_path):
    toolbox = make_watch_toolbox(tmp_path)
    added = run(toolbox, "watchlist", {"action": "add", "code": "sh600519", "name": "贵州茅台"},
                chat_id="oc_g")
    assert "已关注 sh600519 贵州茅台" in added and "共 1 只" in added

    listed = run(toolbox, "watchlist", {"action": "list"}, chat_id="oc_g")
    assert "关注 1 只股票" in listed and "1. sh600519 贵州茅台" in listed

    removed = run(toolbox, "watchlist", {"action": "remove", "code": "sh600519"}, chat_id="oc_g")
    assert "已取消关注 sh600519 贵州茅台" in removed
    assert "还没有关注任何股票" in run(toolbox, "watchlist", {"action": "list"}, chat_id="oc_g")


def test_watchlist_tool_uses_the_target_session(tmp_path):
    """定时内部会话（timer:<目标>）读写的是那个目标会话的列表。"""
    store = StateStore(str(tmp_path / "state.db"))
    store.add_watch("oc_g", "sz300750", "宁德时代")
    toolbox = make_toolbox(tmp_path, watchlist=store)
    listed = run(toolbox, "watchlist", {"action": "list"}, chat_id="timer:oc_g")
    assert "sz300750 宁德时代" in listed


def test_watchlist_tool_reports_bad_arguments(tmp_path):
    toolbox = make_watch_toolbox(tmp_path)
    assert "action 只能是" in run(toolbox, "watchlist", {"action": "nope"}, chat_id="oc_g")
    assert "需要给出带市场前缀的代码" in run(toolbox, "watchlist", {"action": "add"},
                                            chat_id="oc_g")
    assert "不合法" in run(toolbox, "watchlist", {"action": "add", "code": "600519"},
                          chat_id="oc_g")
    assert "没有 chat_id" in run(toolbox, "watchlist",
                                 {"action": "add", "code": "sh600519"}, chat_id="")
