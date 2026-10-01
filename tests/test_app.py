"""配置解析（load_config）与两个 handler 的装配（trigger / reject）。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from bot.app import (REJECT_TEXT, load_config, output_handler, reject_handler,
                     trigger_handler)
from bot.store import StateStore
from helpers import FakeSender, FakeSessions, normalized


def write_config(tmp_path, **overrides) -> str:
    """在临时目录里写一份能通过校验的 config.json（技能目录也一起造上）。"""
    # 用只在项目里装着的技能名: 用户级的同名技能会先命中, 那样就测不到项目级这条路径
    skill_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# 技能", encoding="utf-8")
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "instructions.md").write_text("指令", encoding="utf-8")
    raw = {
        "chats": ["oc_a", "oc_b"],
        "users": ["ou_a"],
        "bot_open_id": "ou_bot",
        "app_id": "cli_x",
        "app_secret": "secret",
        "project_dir": ".",
        "prompts_dir": "prompts",
        "prompt_file": "instructions.md",
        "api_key": "sk-test",
        "model": "fake-model",
        "turn_timeout": 600,
        "session_idle_timeout": 60,
        "skills": ["demo-skill"],
        "log_file": "logs/bot.log",
    }
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------- load_config ----------

def test_load_config_parses_whitelists(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg["chats"] == ["oc_a", "oc_b"]
    assert cfg["users"] == ["ou_a"]
    assert cfg["bot_open_id"] == "ou_bot"
    assert cfg["model"] == "fake-model"
    skill, = cfg["skills"]
    assert skill.name == "demo-skill"
    assert skill.path == os.path.join(str(tmp_path), ".agents", "skills",
                                     "demo-skill", "SKILL.md")


def test_load_config_fills_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg["api_base_url"] == ""
    assert (cfg["api_timeout"], cfg["command_timeout"]) == (120, 60)
    assert (cfg["tool_output_limit"], cfg["max_tool_rounds"]) == (60000, 40)


def test_load_config_rejects_bad_numbers_and_missing_key(tmp_path):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, turn_timeout=0))
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, max_tool_rounds="10"))
    raw = write_config(tmp_path)
    data = json.loads(Path(raw).read_text(encoding="utf-8"))
    del data["api_key"]
    Path(raw).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(raw)


def test_load_config_allows_only_one_kind_of_whitelist(tmp_path):
    assert load_config(write_config(tmp_path, users=[]))["users"] == []
    assert load_config(write_config(tmp_path, chats=[]))["chats"] == []


def test_load_config_rejects_empty_whitelists(tmp_path):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, chats=[], users=[]))


def test_load_config_rejects_missing_whitelists(tmp_path):
    path = write_config(tmp_path)
    path_obj = Path(path)
    raw = json.loads(path_obj.read_text(encoding="utf-8"))
    del raw["chats"], raw["users"]
    path_obj.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_rejects_bad_elements(tmp_path):
    for bad in (["oc_a", ""], ["oc_a", 123], ["oc_a", "  "], "oc_a"):
        with pytest.raises(ValueError):
            load_config(write_config(tmp_path, chats=bad))
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, users=[None]))


def test_load_config_rejects_leftover_chat_id(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        load_config(write_config(tmp_path, chat_id="oc_old"))
    assert "chat_id" in str(excinfo.value)


def test_load_config_strips_whitelist_elements(tmp_path):
    cfg = load_config(write_config(tmp_path, chats=[" oc_a "]))
    assert cfg["chats"] == ["oc_a"]


# ---------- 密钥的环境变量引用 ----------

def test_load_config_reads_api_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "sk-from-env")
    cfg = load_config(write_config(tmp_path, api_key="env:DEMO_API_KEY"))
    assert cfg["api_key"] == "sk-from-env"


def test_load_config_reads_app_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_APP_SECRET", "secret-from-env")
    cfg = load_config(write_config(tmp_path, app_secret="env:DEMO_APP_SECRET"))
    assert cfg["app_secret"] == "secret-from-env"


def test_load_config_plain_value_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_UNUSED", "x")
    cfg = load_config(write_config(tmp_path, api_key="sk-plain"))
    assert cfg["api_key"] == "sk-plain"


def test_load_config_rejects_missing_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_MISSING", raising=False)
    with pytest.raises(ValueError) as excinfo:
        load_config(write_config(tmp_path, api_key="env:DEMO_MISSING"))
    assert "DEMO_MISSING" in str(excinfo.value)


def test_load_config_rejects_blank_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_BLANK", "   ")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, api_key="env:DEMO_BLANK"))


def test_load_config_reads_env_file_beside_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_FILE_KEY", raising=False)
    path = write_config(tmp_path, api_key="env:DEMO_FILE_KEY")
    (tmp_path / ".env").write_text('DEMO_FILE_KEY="sk-from-file"\n', encoding="utf-8")
    assert load_config(path)["api_key"] == "sk-from-file"


def test_env_file_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_WIN_KEY", "from-process")
    path = write_config(tmp_path, api_key="env:DEMO_WIN_KEY")
    (tmp_path / ".env").write_text("DEMO_WIN_KEY=from-file\n", encoding="utf-8")
    assert load_config(path)["api_key"] == "from-process"


def test_load_config_optional_env_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_DOMAIN", raising=False)
    cfg = load_config(write_config(tmp_path, domain="env:DEMO_DOMAIN"))
    assert cfg["domain"] == ""


# ---------- reject_handler ----------

def test_reject_handler_sends_once_per_cooldown():
    now = [1000.0]
    sender = FakeSender()
    handler = reject_handler(sender=sender, clock=lambda: now[0])
    event = normalized(chat_id="oc_stranger", mentions=[{"id": "ou_bot"}])

    asyncio.run(handler(event))
    assert sender.sent == [("oc_stranger", None, REJECT_TEXT)]

    now[0] += 30.0                       # 冷却期内: 只记日志, 不再发
    asyncio.run(handler(event))
    assert len(sender.sent) == 1

    now[0] += 31.0                       # 过了 60s: 可以再回一次
    asyncio.run(handler(event))
    assert len(sender.sent) == 2


def test_reject_handler_cooldown_is_per_chat():
    sender = FakeSender()
    handler = reject_handler(sender=sender, clock=lambda: 0.0)
    asyncio.run(handler(normalized(chat_id="oc_one", chat_type="p2p", sender_id="ou_x")))
    asyncio.run(handler(normalized(chat_id="oc_two", chat_type="p2p", sender_id="ou_y")))
    assert [chat_id for chat_id, _, _ in sender.sent] == ["oc_one", "oc_two"]


def test_reject_handler_survives_send_failure():
    handler = reject_handler(sender=FakeSender(fail=True), clock=lambda: 0.0)
    asyncio.run(handler(normalized(chat_id="oc_stranger")))   # 不抛异常就算过


def test_reject_handler_ignores_event_without_chat_id():
    sender = FakeSender()
    handler = reject_handler(sender=sender, clock=lambda: 0.0)
    asyncio.run(handler({"chat_id": "", "chat_type": "p2p"}))
    assert sender.sent == []


# ---------- trigger_handler ----------

def test_trigger_handler_passes_chat_id_and_text():
    sessions, sender = FakeSessions(), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender)
    event = normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok", text="看下茅台")
    asyncio.run(handler(event))
    assert sessions.posts == [("oc_p2p", "看下茅台")]
    assert sender.sent == []


def test_trigger_handler_falls_back_to_the_same_chat():
    sessions, sender = FakeSessions(fail=True), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender)
    asyncio.run(handler(normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok")))
    assert len(sender.sent) == 1
    chat_id, _, markdown = sender.sent[0]
    assert chat_id == "oc_p2p"
    assert "出错" in markdown


def test_trigger_handler_drops_event_without_chat_id():
    sessions, sender = FakeSessions(), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender)
    asyncio.run(handler(normalized(chat_id="", text="没有 chat_id")))
    assert sessions.posts == []
    assert sender.sent == []


# ---------- 定时播报的配置 ----------

def test_load_config_timer_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg["timer_enabled"] is False                       # 默认关: 其它配置不会被带上
    assert cfg["timer_message"]                                # 有默认文案, 不是空串
    assert cfg["state_file"] == os.path.join(str(tmp_path), "logs", "state.db")


def test_load_config_timer_values(tmp_path):
    cfg = load_config(write_config(tmp_path, timer_enabled=True,
                                   timer_message="盯一下 sh600519",
                                   state_file="state/other.db"))
    assert cfg["timer_enabled"] is True
    assert cfg["timer_message"] == "盯一下 sh600519"
    assert cfg["state_file"] == os.path.join(str(tmp_path), "state", "other.db")
    # 文案写成空白 -> 回落到默认文案, 不至于发一条空消息
    assert load_config(write_config(tmp_path, timer_message="   "))["timer_message"]


def test_load_config_rejects_bad_timer_fields(tmp_path):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, timer_enabled="yes"))
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, timer_message=123))
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, state_file=123))


# ---------- output_handler（普通会话 + 定时分支） ----------

def make_store(tmp_path, now: float = 1000.0):
    """临时目录里的真状态库 + 可手动推进的时钟。"""
    clock = [now]
    store = StateStore(str(tmp_path / "state.db"), now=lambda: clock[0])
    return store, clock


class NullSender:
    """发送永远失败（返回 None）的假发送器。"""

    async def send(self, chat_id: str, *, text=None, markdown=None):
        return None


def test_output_handler_keeps_the_normal_chat_behaviour(tmp_path):
    store, _ = make_store(tmp_path)
    sender = FakeSender()
    handler = output_handler(sender=sender, store=store)
    asyncio.run(handler("oc_p2p", "正文", True))
    asyncio.run(handler("oc_p2p", "炸了", False))
    assert sender.sent == [("oc_p2p", None, "正文"),
                           ("oc_p2p", None, "⚠️ 分析失败\n\n炸了")]


def test_output_handler_timer_push_sends_and_records(tmp_path):
    store, clock = make_store(tmp_path)
    sender = FakeSender()
    handler = output_handler(sender=sender, store=store, clock=lambda: clock[0])
    asyncio.run(handler("timer:oc_g", "光模块新规落地\n\n**为什么重要**：…", True))

    assert len(sender.sent) == 1
    chat_id, _, markdown = sender.sent[0]
    assert chat_id == "oc_g" and "盯盘" in markdown and "光模块新规落地" in markdown
    assert [summary for _, summary in store.recent_pushes("oc_g")] == ["光模块新规落地"]


def test_output_handler_timer_no_push_is_silent(tmp_path):
    store, _ = make_store(tmp_path)
    sender = FakeSender()
    handler = output_handler(sender=sender, store=store)
    asyncio.run(handler("timer:oc_g", "NO_PUSH\n没有值得推的", True))
    assert sender.sent == []
    assert store.recent_pushes("oc_g") == []


def test_output_handler_timer_does_not_record_a_failed_send(tmp_path):
    store, _ = make_store(tmp_path)
    handler = output_handler(sender=NullSender(), store=store)
    asyncio.run(handler("timer:oc_g", "大事发生", True))
    assert store.recent_pushes("oc_g") == []        # 没发出去 -> 下一轮还能再来一次


def test_output_handler_timer_alerts_after_three_failures(tmp_path):
    store, _ = make_store(tmp_path)
    sender = FakeSender()
    handler = output_handler(sender=sender, store=store)
    for _ in range(2):
        asyncio.run(handler("timer:oc_g", "调用模型失败：502 bad gateway", False))
    assert sender.sent == []                        # 前两次只写日志, 不打扰

    asyncio.run(handler("timer:oc_g", "调用模型失败：502 bad gateway", False))
    assert len(sender.sent) == 1
    chat_id, _, markdown = sender.sent[0]
    assert chat_id == "oc_g" and "连续 3 轮失败" in markdown

    asyncio.run(handler("timer:oc_g", "调用模型失败：502 bad gateway", False))
    assert len(sender.sent) == 1                    # 24 小时内不再提示


def test_output_handler_failure_alert_is_retried_if_it_cannot_be_sent(tmp_path):
    store, _ = make_store(tmp_path)
    handler = output_handler(sender=NullSender(), store=store)
    for _ in range(3):
        asyncio.run(handler("timer:oc_g", "调用模型失败", False))
    # 提示没发出去 -> 冷却不开始, 下一轮还会提示
    assert store.note_failure("oc_g") is True


def test_output_handler_no_push_clears_the_failure_streak(tmp_path):
    store, _ = make_store(tmp_path)
    sender = FakeSender()
    handler = output_handler(sender=sender, store=store)
    for _ in range(2):
        asyncio.run(handler("timer:oc_g", "挂了", False))
    asyncio.run(handler("timer:oc_g", "NO_PUSH", True))   # 这一轮是好的: 重新计数
    for _ in range(2):
        asyncio.run(handler("timer:oc_g", "挂了", False))
    assert sender.sent == []                              # 还没到连续 3 次


def test_output_handler_survives_a_send_exception(tmp_path):
    """真的发送异常由会话那边兜（_emit 捕获）; 这里只确认失败提示不会误记冷却。"""
    store, _ = make_store(tmp_path)
    handler = output_handler(sender=FakeSender(fail=True), store=store)
    asyncio.run(handler("timer:oc_g", "挂了", False))
    asyncio.run(handler("timer:oc_g", "挂了", False))
    with pytest.raises(RuntimeError):                 # 该提示了, 但发送炸了
        asyncio.run(handler("timer:oc_g", "挂了", False))
    assert store.note_failure("oc_g") is True         # 冷却没开始, 下一轮还会提示


# ---------- trigger_handler: 定时消息与私聊会话表 ----------

def test_trigger_handler_resets_the_timer_session(tmp_path):
    store, _ = make_store(tmp_path)
    sessions, sender = FakeSessions(), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender, store=store)
    asyncio.run(handler({"chat_id": "timer:oc_g", "chat_type": "timer",
                         "text": "【定时播报】看看有没有大事"}))
    assert sessions.resets == ["timer:oc_g"]           # 每小时一轮都从干净上下文开始
    assert sessions.posts == [("timer:oc_g", "【定时播报】看看有没有大事")]


def test_trigger_handler_does_not_reset_normal_sessions(tmp_path):
    store, _ = make_store(tmp_path)
    sessions, sender = FakeSessions(), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender, store=store)
    asyncio.run(handler(normalized(chat_id="oc_g", mentions=[{"id": "ou_bot"}])))
    assert sessions.resets == []


def test_trigger_handler_remembers_p2p_chats(tmp_path):
    store, _ = make_store(tmp_path)
    handler = trigger_handler(sessions=FakeSessions(), sender=FakeSender(), store=store)
    asyncio.run(handler(normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok")))
    assert store.peers() == {"ou_ok": "oc_p2p"}
    # 群消息不进这张表（整点播报的私聊目标只认私聊）
    asyncio.run(handler(normalized(chat_id="oc_g", chat_type="group", sender_id="ou_ok")))
    assert store.peers() == {"ou_ok": "oc_p2p"}


def test_trigger_handler_timer_failure_does_not_reply_to_the_internal_id(tmp_path):
    """定时会话的 chat_id 不是真收件人, 兜底消息不能往那儿发。"""
    sessions, sender = FakeSessions(fail=True), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender)
    asyncio.run(handler({"chat_id": "timer:oc_g", "chat_type": "timer", "text": "x"}))
    assert sender.sent == []


def test_trigger_handler_survives_a_broken_store(tmp_path):
    class BrokenStore:
        def remember_peer(self, open_id, chat_id):
            raise RuntimeError("库炸了")

    sessions, sender = FakeSessions(), FakeSender()
    handler = trigger_handler(sessions=sessions, sender=sender, store=BrokenStore())
    asyncio.run(handler(normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok")))
    assert sessions.posts == [("oc_p2p", "看下茅台")]      # 记账失败不影响这条消息
