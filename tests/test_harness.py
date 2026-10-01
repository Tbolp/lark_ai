"""会话与工具循环（ChatSession / ChatSessions）: 全程用假的模型客户端, 不碰网络。"""

from __future__ import annotations

import asyncio
import json

from bot.harness import ChatSession, ChatSessions
from bot.tools import Toolbox
from helpers import FakeClient, message, response


class FakeToolbox:
    """记录并发峰值的假工具盒: 用来验证"一批调用是并发跑的、且不超过上限"。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self.calls: list[str] = []
        self.chat_ids: list[str] = []

    def schemas(self):
        return []

    async def call(self, name: str, arguments: str, chat_id: str = "") -> str:
        command = json.loads(arguments)["command"]
        self.calls.append(command)
        self.chat_ids.append(chat_id)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        return f"结果:{command}"


def make_session(tmp_path, script, outputs, *, toolbox=None, **overrides) -> ChatSession:
    body = 'echo "$@"'
    program = tmp_path / "westock"
    program.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    program.chmod(0o755)
    if toolbox is None:
        toolbox = Toolbox(references={}, command_timeout=5, output_limit=500,
                          program_path=str(program))

    async def on_output(text, ok):
        outputs.append((text, ok))

    kwargs = dict(client=FakeClient(script), model="fake-model", instructions="系统指令",
                  toolbox=toolbox, turn_timeout=30, api_timeout=10, idle_timeout=60,
                  on_output=on_output)
    kwargs.update(overrides)
    return ChatSession(**kwargs)


async def post_and_wait(session: ChatSession, text: str) -> None:
    await session.post(text)
    assert session.task is not None
    await session.task


# ---------- 基本一轮 ----------

def test_single_turn_emits_the_answer(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [response(message("贵州茅台 1237"))], outputs)
    asyncio.run(post_and_wait(session, "看下茅台"))
    assert outputs == [("贵州茅台 1237", True)]
    assert session.messages[0] == {"role": "system", "content": "系统指令"}
    assert session.messages[-1]["role"] == "assistant"


def test_tool_call_is_executed_and_result_is_fed_back(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [
        response(message(None, [("call_1", "run_westock",
                                 '{"command": "westock quote sh600519"}')])),
        response(message("结论")),
    ], outputs)
    asyncio.run(post_and_wait(session, "看下茅台"))
    assert outputs == [("结论", True)]
    roles = [m["role"] for m in session.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    tool_message = session.messages[3]
    assert tool_message["tool_call_id"] == "call_1"
    assert tool_message["content"].startswith("[exit=0]")
    assert "quote sh600519" in tool_message["content"]


def test_second_request_carries_the_tool_result(tmp_path):
    outputs: list = []
    client = FakeClient([
        response(message(None, [("call_1", "run_westock", '{"command": "westock quote sh600519"}')])),
        response(message("结论")),
    ])
    session = make_session(tmp_path, [], outputs)
    session.client = client
    asyncio.run(post_and_wait(session, "看下茅台"))
    sent = client.requests[1]["messages"]
    assert sent[-1]["role"] == "tool"
    assert client.requests[1]["tools"][0]["function"]["name"] == "run_westock"


def test_unknown_tool_does_not_break_the_turn(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [
        response(message(None, [("call_1", "nope", "{}")])),
        response(message("我换个说法")),
    ], outputs)
    asyncio.run(post_and_wait(session, "问题"))
    assert outputs == [("我换个说法", True)]
    assert "没有叫 nope 的工具" in session.messages[3]["content"]


def test_model_error_is_reported_and_context_dropped(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [RuntimeError("502 bad gateway")], outputs)
    asyncio.run(post_and_wait(session, "问题"))
    text, ok = outputs[0]
    assert ok is False and "调用模型失败" in text
    assert session.messages == []                 # 出了错就清上下文, 下一问从头来


def test_empty_answer_is_reported_as_failure(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [response(message(""))], outputs)
    asyncio.run(post_and_wait(session, "问题"))
    assert outputs[0][1] is False and "没有返回内容" in outputs[0][0]


def test_tool_round_limit_stops_a_loop(tmp_path):
    outputs: list = []
    script = [response(message(None, [("c", "run_westock", '{"command": "westock" }')]))
              for _ in range(5)]
    session = make_session(tmp_path, script, outputs, max_tool_rounds=3)
    asyncio.run(post_and_wait(session, "问题"))
    assert outputs[0][1] is False and "工具调用次数过多" in outputs[0][0]


# ---------- 追问并进正在跑的那一轮 ----------

def test_parallel_tool_calls_run_concurrently(tmp_path):
    outputs: list = []
    toolbox = FakeToolbox()
    session = make_session(tmp_path, [
        response(message(None, [("c1", "run_westock", '{"command": "westock quote sz300750"}'),
                                ("c2", "run_westock", '{"command": "westock technical sz300750"}'),
                                ("c3", "run_westock", '{"command": "westock fund flow sz300750"}')])),
        response(message("结论")),
    ], outputs, toolbox=toolbox)
    asyncio.run(post_and_wait(session, "看下宁德时代"))
    assert toolbox.peak == 3                     # 三条同时跑, 不是排队
    assert toolbox.calls == ["westock quote sz300750",
                             "westock technical sz300750",
                             "westock fund flow sz300750"]


def test_parallel_tool_calls_respect_the_limit(tmp_path):
    outputs: list = []
    toolbox = FakeToolbox(delay=0.05)
    session = make_session(tmp_path, [
        response(message(None, [("c1", "run_westock", '{"command": "westock a"}'),
                                ("c2", "run_westock", '{"command": "westock b"}'),
                                ("c3", "run_westock", '{"command": "westock c"}')])),
        response(message("结论")),
    ], outputs, toolbox=toolbox, max_parallel_tools=2)
    asyncio.run(post_and_wait(session, "问题"))
    assert toolbox.peak == 2                     # 上限生效


def test_tool_results_keep_the_order_of_the_calls(tmp_path):
    outputs: list = []
    toolbox = FakeToolbox(delay=0.01)
    session = make_session(tmp_path, [
        response(message(None, [("c1", "run_westock", '{"command": "westock one"}'),
                                ("c2", "run_westock", '{"command": "westock two"}')])),
        response(message("结论")),
    ], outputs, toolbox=toolbox)
    asyncio.run(post_and_wait(session, "问题"))
    tool_messages = [m for m in session.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]
    assert [m["content"] for m in tool_messages] == ["结果:westock one", "结果:westock two"]


def test_single_tool_call_skips_the_gather_path(tmp_path):
    outputs: list = []
    toolbox = FakeToolbox()
    session = make_session(tmp_path, [
        response(message(None, [("c1", "run_westock", '{"command": "westock one"}')])),
        response(message("结论")),
    ], outputs, toolbox=toolbox)
    asyncio.run(post_and_wait(session, "问题"))
    assert toolbox.peak == 1
    assert session.messages[3]["content"] == "结果:westock one"

def test_follow_up_joins_the_running_turn(tmp_path):
    outputs: list = []
    gate = asyncio.Event()

    async def first_call():
        await gate.wait()
        return response(message(None, [("call_1", "run_westock", '{"command": "westock" }')]))

    client = FakeClient([first_call, response(message("合并后的正文"))])
    session = make_session(tmp_path, [], outputs)
    session.client = client

    async def scenario():
        await session.post("先问这个")
        for _ in range(200):                       # 等第一次模型调用真的发出去
            if client.requests:
                break
            await asyncio.sleep(0.005)
        await session.post("再补一句")
        gate.set()
        await session.task

    asyncio.run(scenario())
    assert outputs == [("合并后的正文", True)]      # 一个聊天里只出一条回复
    sent = client.requests[1]["messages"]
    assert sent[-1] == {"role": "user", "content": "再补一句"}
    # 追问排在工具结果之后: assistant(tool_calls) 与 tool 仍然紧挨着
    assert [m["role"] for m in sent[-3:]] == ["assistant", "tool", "user"]


def test_follow_up_after_a_finished_turn_starts_a_new_one(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [response(message("第一答")), response(message("第二答"))],
                           outputs)

    async def scenario():
        await post_and_wait(session, "第一问")
        await post_and_wait(session, "第二问")

    asyncio.run(scenario())
    assert outputs == [("第一答", True), ("第二答", True)]
    assert [m["role"] for m in session.messages] == ["system", "user", "assistant",
                                                    "user", "assistant"]


# ---------- 历史与空闲 ----------

def test_history_is_trimmed_at_a_user_boundary(tmp_path):
    outputs: list = []
    script = [response(message(None, [("c1", "run_westock", '{"command": "westock" }')])),
              response(message("答一")),
              response(message("答二"))]
    session = make_session(tmp_path, script, outputs, max_history_messages=4)

    async def scenario():
        await post_and_wait(session, "问一")
        await post_and_wait(session, "问二")

    asyncio.run(scenario())
    roles = [m["role"] for m in session.messages]
    assert roles[0] == "system"
    assert "tool" not in roles                      # 切开时不能留下没有配对的 tool 消息
    assert roles.count("assistant") >= 1


def test_idle_clears_history_but_keeps_the_session(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [response(message("答"))], outputs, idle_timeout=0.05)

    async def scenario():
        await post_and_wait(session, "问题")
        assert session.messages
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert session.messages == []


def test_close_stops_the_running_turn(tmp_path):
    outputs: list = []
    gate = asyncio.Event()

    async def stuck():
        await gate.wait()
        return response(message("来不及了"))

    session = make_session(tmp_path, [stuck], outputs)

    async def scenario():
        await session.post("问题")
        await asyncio.sleep(0.02)
        await session.close()
        assert session.turn_active is False
        assert session.messages == []

    asyncio.run(scenario())
    assert outputs == []


def test_reset_clears_history_but_keeps_the_session(tmp_path):
    outputs: list = []
    session = make_session(tmp_path, [response(message("答一")), response(message("答二"))],
                           outputs)

    async def scenario():
        await post_and_wait(session, "问一")
        assert session.messages
        session.reset()
        assert session.messages == [] and session.pending == []
        await post_and_wait(session, "问二")

    asyncio.run(scenario())
    assert outputs == [("答一", True), ("答二", True)]
    assert [m["role"] for m in session.messages] == ["system", "user", "assistant"]


def test_instructions_callable_is_evaluated_per_conversation(tmp_path):
    """指令传函数时, 每段新对话重新求值一次（时间占位符不会冻在启动那一刻）。"""
    outputs: list = []
    calls: list[int] = []

    def instructions() -> str:
        calls.append(len(calls) + 1)
        return f"指令{calls[-1]}"

    session = make_session(tmp_path, [response(message("答一")), response(message("答二"))],
                           outputs, instructions=instructions)

    async def scenario():
        await post_and_wait(session, "问一")
        assert session.messages[0]["content"] == "指令1"
        session.reset()                      # 等价于空闲清空后的下一问
        await post_and_wait(session, "问二")
        assert session.messages[0]["content"] == "指令2"

    asyncio.run(scenario())
    assert calls == [1, 2]


def test_tool_call_carries_the_session_chat_id(tmp_path):
    """工具调用带上会话身份: 关注列表这类"按会话分开"的工具靠它。"""
    outputs: list = []
    toolbox = FakeToolbox(delay=0)
    session = make_session(tmp_path, [
        response(message(None, [("c1", "run_westock", '{"command": "westock one"}')])),
        response(message("结论")),
    ], outputs, toolbox=toolbox, chat_id="oc_p2p")
    asyncio.run(post_and_wait(session, "问题"))
    assert toolbox.chat_ids == ["oc_p2p"]


# ---------- 注册表 ----------

def test_sessions_are_reused_per_chat_id(tmp_path):
    toolbox = Toolbox(references={}, command_timeout=5, output_limit=100)

    async def on_output(chat_id, text, ok):
        pass

    sessions = ChatSessions(client=FakeClient([]), model="m", instructions=lambda: "指令",
                            toolbox=toolbox, turn_timeout=30, api_timeout=10,
                            idle_timeout=60, on_output=on_output)
    first = sessions.get("oc_a")
    assert sessions.get("oc_a") is first
    assert sessions.get("oc_b") is not first
    assert set(sessions.sessions) == {"oc_a", "oc_b"}
    asyncio.run(sessions.close_all())
    asyncio.run(sessions.close_all())              # 幂等


def test_output_is_bound_to_its_chat_id(tmp_path):
    toolbox = Toolbox(references={}, command_timeout=5, output_limit=100)
    seen: list = []

    async def on_output(chat_id, text, ok):
        seen.append((chat_id, text, ok))

    sessions = ChatSessions(client=FakeClient([]), model="m", instructions=lambda: "指令",
                            toolbox=toolbox, turn_timeout=30, api_timeout=10,
                            idle_timeout=60, on_output=on_output)
    session = sessions.get("oc_p2p")

    async def scenario():
        await session.on_output("正文", True)

    asyncio.run(scenario())
    assert seen == [("oc_p2p", "正文", True)]
