"""整点那条消息的完整处理链路（端到端, 中间一层都不换）。

真实的: 定时源 -> core 事件 -> trigger_handler -> 会话 -> 工具 -> output_handler -> 状态库。
假的只有两头的"外面": 模型客户端（FakeClient）和发送器（FakeSender）—— 也就是说这里跑的
正是线上那条链, 只是没有网络、没有飞书。发送和消费分开, 才可能这样单独验证消费侧。
"""

from __future__ import annotations

import asyncio
import stat

import pytest

from bot import events as bot_events
from bot.app import output_handler, trigger_handler
from bot.harness import ChatSession, ChatSessions
from bot.store import StateStore
from bot.timer import TimerSource
from bot.tools import Toolbox
from core import events as core
from helpers import FakeClient, FakeSender, message, response

GROUP = "oc_group"
OTHER = "oc_other"
SESSION_KEY = f"timer:{GROUP}"


@pytest.fixture(autouse=True)
def clean_handlers():
    """每条用例前后把全局事件注册清干净（那张表是模块级的）。"""
    core.reset()
    yield
    core.reset()


def fake_program(tmp_path) -> str:
    """顶掉真的 westock: 只回一行能认出来的输出。"""
    path = tmp_path / "westock"
    path.write_text('#!/bin/sh\necho "新闻列表" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def make_world(tmp_path, script, *, targets=None):
    """把一条完整链路搭起来（除了模型和发送器, 全是真的）。"""
    store = StateStore(str(tmp_path / "state.db"))
    sender = FakeSender()
    client = FakeClient(script)
    toolbox = Toolbox(references={}, command_timeout=5, output_limit=500,
                      program_path=fake_program(tmp_path), watchlist=store)
    sessions = ChatSessions(client=client, model="fake-model", instructions=lambda: "系统指令",
                            toolbox=toolbox, turn_timeout=30, api_timeout=10, idle_timeout=60,
                            on_output=output_handler(sender=sender, store=store))
    core.on(bot_events.TRIGGER, trigger_handler(sessions=sessions, sender=sender, store=store))
    source = TimerSource(message="看看有没有大事", targets=lambda: list(targets or [GROUP]),
                         watchlist=store.describe, recent=store.recent_pushes,
                         emit=core.emit, stagger=0)
    return store, sender, client, sessions, source


async def wait_for(sessions: ChatSessions, key: str, *, timeout: float = 5.0,
                   after: asyncio.Task | None = None) -> ChatSession:
    """等 trigger_handler 那条后台任务把会话建起来并跑完这一轮。

    emit 是异步派发的（core.emit 只负责起协程）, 所以这里要轮询等它出现。
    `after` 用来跳过上一轮那条已经跑完的任务（同一个会话对象会一直留着它）。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    session = sessions.get(key)
    while session.task is None or session.task is after:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("定时事件没有被处理（会话一直没有起轮）")
        await asyncio.sleep(0.002)
    await session.task
    return session


def test_timer_message_is_handled_and_pushed(tmp_path):
    """有值得推的内容: 走完整链路 -> 工具真的跑了 -> 正文发到群里 -> 记进去重清单。"""
    store, sender, client, sessions, source = make_world(tmp_path, [
        response(message(None, [("c1", "run_westock",
                                 '{"command": "westock news list sh600519 --limit 5"}')])),
        response(message("大盘与关注股都没大事，只有一条产业消息值得看一眼。")),
    ])
    store.add_watch(GROUP, "sh600519", "贵州茅台")

    async def scenario():
        await source.tick()
        session = await wait_for(sessions, SESSION_KEY)

        # 1) 这一轮确实是按定时消息跑起来的, 而且关注列表被带进了消息
        assert session.messages[0] == {"role": "system", "content": "系统指令"}
        assert session.messages[1]["role"] == "user"
        assert session.messages[1]["content"].startswith("【定时播报】")
        assert "sh600519 贵州茅台" in session.messages[1]["content"]
        # 2) 工具真的被执行了（命令白名单、子进程那条路都过了一遍）
        assert session.messages[3]["role"] == "tool"
        assert session.messages[3]["content"].startswith("[exit=0]")
        assert "新闻列表" in session.messages[3]["content"]
        # 3) 正文发到的是目标群, 不是内部会话 id
        assert len(sender.sent) == 1
        chat_id, _, markdown = sender.sent[0]
        assert chat_id == GROUP
        assert "盯盘" in markdown and "只有一条产业消息值得看一眼" in markdown
        # 4) 这一条记进去重清单, 下一轮会带给模型
        assert [summary for _, summary in store.recent_pushes(GROUP)] == [
            "大盘与关注股都没大事，只有一条产业消息值得看一眼。"]

        await sessions.close_all()

    asyncio.run(scenario())


def test_no_push_round_stays_quiet(tmp_path):
    """模型判定没什么值得推的: 群里什么都不发, 也不记清单。"""
    store, sender, client, sessions, source = make_world(tmp_path, [response(message("NO_PUSH"))])

    async def scenario():
        await source.tick()
        await wait_for(sessions, SESSION_KEY)
        assert sender.sent == []
        assert store.recent_pushes(GROUP) == []
        await sessions.close_all()

    asyncio.run(scenario())


def test_each_round_starts_from_a_clean_context(tmp_path):
    """两轮之间上下文清干净: 第二轮的消息里只有这一轮的【定时播报】。"""
    store, sender, client, sessions, source = make_world(tmp_path, [
        response(message("第一轮正文")),
        response(message("第二轮正文")),
    ])

    async def scenario():
        await source.tick()
        first = await wait_for(sessions, SESSION_KEY)
        await source.tick()
        session = await wait_for(sessions, SESSION_KEY, after=first.task)

        user_messages = [m["content"] for m in session.messages if m["role"] == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].startswith("【定时播报】")
        assert [chat_id for chat_id, _, _ in sender.sent] == [GROUP, GROUP]
        await sessions.close_all()

    asyncio.run(scenario())


def test_each_target_gets_its_own_watchlist(tmp_path):
    """群和私聊各一份关注列表: 每条定时消息带的都是它自己那份。"""
    store, sender, client, sessions, source = make_world(
        tmp_path, [response(message("NO_PUSH")), response(message("NO_PUSH"))],
        targets=[GROUP, OTHER])
    store.add_watch(GROUP, "sh600519", "贵州茅台")
    store.add_watch(OTHER, "sz300750", "宁德时代")

    async def scenario():
        await source.tick()
        first = await wait_for(sessions, SESSION_KEY)
        second = await wait_for(sessions, f"timer:{OTHER}")
        assert "sh600519 贵州茅台" in first.messages[1]["content"]
        assert "sz300750 宁德时代" in second.messages[1]["content"]
        assert "sz300750" not in first.messages[1]["content"]
        await sessions.close_all()

    asyncio.run(scenario())


def test_timer_failure_does_not_message_the_group_but_counts(tmp_path):
    """这一轮彻底失败: 群里安静, 但失败计数记下了（连续 3 次才会提示）。"""
    store, sender, client, sessions, source = make_world(
        tmp_path, [RuntimeError("502 bad gateway")])

    async def scenario():
        await source.tick()
        await wait_for(sessions, SESSION_KEY)
        assert sender.sent == []
        assert store.recent_pushes(GROUP) == []
        # 计数记下了: 后面两次失败就该提示了
        assert store.note_failure(GROUP) is False
        assert store.note_failure(GROUP) is True
        await sessions.close_all()

    asyncio.run(scenario())
