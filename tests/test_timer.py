"""定时消息源: 整点计算 / 事件形状 / 消息内容 / 循环与取消（不碰网络、不碰真时钟）。"""

from __future__ import annotations

import asyncio

from bot.events import TRIGGER
from bot.timer import (DEFAULT_MESSAGE, STAGGER, TIMER_PREFIX, TimerSource, build_message,
                       is_no_push, next_hour, session_key, stamp, target_of)


class Clock:
    """可手动推进的假时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeEmit:
    """假的 core.emit: 只记下收到的事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def make_source(*, targets, clock=None, sleeps=None, events=None, message=DEFAULT_MESSAGE,
                watchlist=None, recent=None, **overrides):
    """造一个定时源: 时钟、sleep、emit 全是假的。"""
    clock = clock or Clock()
    sleeps = [] if sleeps is None else sleeps
    events = events if events is not None else FakeEmit()

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    source = TimerSource(
        message=message,
        targets=lambda: list(targets),
        watchlist=watchlist or (lambda chat_id: f"关注 {chat_id}"),
        recent=recent or (lambda chat_id: []),
        emit=events, now=clock, sleep=sleep, **overrides)
    return source, sleeps, events, clock


# ---------- 整点 ----------

def test_next_hour_is_strictly_in_the_future():
    assert next_hour(0) == 3600
    assert next_hour(3599.5) == 3600
    assert next_hour(3600) == 7200            # 正好站在整点上 -> 下一个小时
    assert next_hour(3600.5) == 7200
    assert next_hour(86400 - 1) == 86400      # 跨天
    assert next_hour(86400) == 86400 + 3600   # 跨天之后的那个整点


def test_session_key_and_target_of_round_trip():
    assert session_key("oc_a") == f"{TIMER_PREFIX}oc_a"
    assert target_of(session_key("oc_a")) == "oc_a"
    assert target_of("oc_a") == "oc_a"        # 不是定时 key 就原样返回


# ---------- NO_PUSH ----------

def test_is_no_push_matches_the_first_line():
    assert is_no_push("NO_PUSH")
    assert is_no_push("no_push")
    assert is_no_push("  NO_PUSH  ")
    assert is_no_push("**NO_PUSH**")
    assert is_no_push("NO_PUSH（本轮没有值得推的）")
    assert is_no_push("NO_PUSH: 没有大事\n第二行")
    assert not is_no_push("")
    assert not is_no_push("本轮没有 NO_PUSH 之外的内容")   # 第一行不是它
    assert not is_no_push("NO\nPUSH")                       # 不在同一行
    assert not is_no_push("有大事：某公司被立案调查")


# ---------- 消息内容 ----------

def test_build_message_carries_window_watchlist_and_recent():
    now, window = 2000.0, 1000.0
    text = build_message("看看有没有大事", now=now, window_start=window,
                         recent=[(900.0, "光模块出口管制落地")],
                         watchlist="sh600519 贵州茅台")
    assert text.startswith("【定时播报】看看有没有大事")
    assert f"现在是北京时间 {stamp(now)}" in text
    assert f"本轮只看 {stamp(window)} 之后" in text
    assert "本会话关注的股票：sh600519 贵州茅台" in text
    assert "- " in text and "光模块出口管制落地" in text
    assert "最近还没有推送过" not in text


def test_build_message_without_history_says_so():
    text = build_message("看看有没有大事", now=2000.0, window_start=1000.0, recent=[],
                         watchlist="（还没有关注任何股票）")
    assert "最近还没有推送过任何消息。" in text
    assert "（还没有关注任何股票）" in text


# ---------- 发一轮 ----------

def test_tick_emits_one_event_per_target():
    events = FakeEmit()
    source, sleeps, _, _ = make_source(targets=["oc_g", "oc_p"], events=events)
    asyncio.run(source.tick())

    assert [name for name, _ in events.events] == [TRIGGER, TRIGGER]
    first, second = (payload for _, payload in events.events)
    assert first["chat_id"] == "timer:oc_g"
    assert second["chat_id"] == "timer:oc_p"
    assert set(first) == {"message_id", "chat_id", "chat_type", "thread_id", "message_type",
                          "sender_type", "sender_id", "text"}
    assert first["chat_type"] == "timer" and first["sender_type"] == "timer"
    assert first["message_id"] is None and first["sender_id"] is None
    assert first["text"].startswith("【定时播报】")
    assert sleeps == [STAGGER]          # 第一条不等, 第二条前隔一下


def test_event_text_carries_this_targets_watchlist():
    events = FakeEmit()
    source, _, _, _ = make_source(
        targets=["oc_g"], events=events,
        watchlist=lambda chat_id: f"{chat_id} 关注的股票",
        recent=lambda chat_id: [(900.0, f"{chat_id} 推过的事")])
    asyncio.run(source.tick())
    _, payload = events.events[0]
    assert "oc_g 关注的股票" in payload["text"]
    assert "oc_g 推过的事" in payload["text"]


def test_tick_window_starts_at_the_previous_tick():
    clock = Clock(10_000.0)
    events = FakeEmit()
    source, _, _, _ = make_source(targets=["oc_g"], clock=clock, events=events)
    asyncio.run(source.tick())                       # 首轮: 往前推一小时
    assert f"本轮只看 {stamp(10_000 - 3600)} 之后" in events.events[0][1]["text"]

    clock.now = 20_000.0
    asyncio.run(source.tick())                       # 第二轮: 从上一轮那一刻起
    assert f"本轮只看 {stamp(10_000)} 之后" in events.events[1][1]["text"]


def test_tick_without_targets_does_nothing():
    source, sleeps, events, _ = make_source(targets=[])
    asyncio.run(source.tick())
    assert events.events == [] and sleeps == []
    assert source.last_tick is not None              # 窗口照样往前推


def test_tick_uses_the_window_for_each_target_once():
    events = FakeEmit()
    source, _, _, _ = make_source(targets=["oc_a", "oc_b"], events=events)
    asyncio.run(source.tick())
    assert [p["chat_id"] for _, p in events.events] == ["timer:oc_a", "timer:oc_b"]


# ---------- 循环 ----------

def test_run_waits_for_the_next_hour_then_emits():
    clock = Clock(1000.0)
    sleeps: list[float] = []
    events = FakeEmit()

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:                          # 第二轮睡下时假装被取消
            raise asyncio.CancelledError
        clock.now += delay                            # 睡到点

    source = TimerSource(message=DEFAULT_MESSAGE, targets=lambda: ["oc_g"],
                         watchlist=lambda chat_id: "", recent=lambda chat_id: [],
                         emit=events, now=clock, sleep=sleep)

    async def scenario():
        await asyncio.gather(source.start(), return_exceptions=True)

    asyncio.run(scenario())
    assert sleeps == [3600 - 1000, 3600]              # 先睡到整点, 之后再睡一小时
    assert len(events.events) == 1


def test_a_failed_round_does_not_stop_the_loop():
    clock = Clock(1000.0)
    sleeps: list[float] = []
    calls: list[int] = []

    def targets():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("解析目标失败")
        return ["oc_g"]

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
        clock.now += delay

    source = TimerSource(message=DEFAULT_MESSAGE, targets=targets,
                         watchlist=lambda chat_id: "", recent=lambda chat_id: [],
                         emit=FakeEmit(), now=clock, sleep=sleep)

    async def scenario():
        await asyncio.gather(source.start(), return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) == 2                            # 第一轮炸了, 第二轮照样跑
    assert sleeps == [3600 - 1000, 3600, 3600]


def test_stop_cancels_the_loop():
    source, _, _, _ = make_source(targets=["oc_g"])

    async def sleep(delay: float) -> None:
        await asyncio.sleep(3600)

    source.sleep = sleep

    async def scenario():
        source.start()
        await asyncio.sleep(0)
        await source.stop()
        assert source.task is None

    asyncio.run(scenario())
