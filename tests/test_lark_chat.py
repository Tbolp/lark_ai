"""归一化与"该不该响应"的判断（lark_chat）。"""

from __future__ import annotations

from types import SimpleNamespace

from bot.lark_chat import classify, normalize
from helpers import normalized, raw_event, sdk_mention

BOT = "ou_bot"
CHATS = ["oc_ok"]
USERS = ["ou_ok"]


def classify_normalized(**kwargs) -> str | None:
    return classify(normalized(**kwargs), chats=CHATS, users=USERS, bot_open_id=BOT)


# ---------- normalize ----------

def test_normalize_passes_through_chat_type_and_thread():
    event = normalize(raw_event(chat_id="oc_x", chat_type="topic", thread_id="omt_1"))
    assert event is not None
    assert event["chat_id"] == "oc_x"
    assert event["chat_type"] == "topic"
    assert event["thread_id"] == "omt_1"
    assert event["message_type"] == "text"
    assert event["sender_type"] == "user"
    assert event["sender_id"] == "ou_sender"


def test_normalize_private_chat():
    event = normalize(raw_event(chat_type="p2p", chat_id="oc_p2p",
                                sender_open_id="ou_ok"))
    assert event["chat_type"] == "p2p"
    assert event["sender_id"] == "ou_ok"


def test_normalize_strips_mention_placeholder():
    data = raw_event(text="@_user_1 看下茅台",
                     mentions=[sdk_mention("@_user_1", "机器人", BOT)])
    event = normalize(data)
    assert event["text"] == "看下茅台"
    assert event["mentions"] == [{"id": BOT, "name": "机器人"}]


def test_normalize_returns_none_for_non_message_events():
    assert normalize(SimpleNamespace()) is None
    assert normalize(SimpleNamespace(event=None)) is None


# ---------- classify: 群 ----------

def test_whitelisted_group_with_mention_triggers():
    assert classify_normalized(chat_id="oc_ok", mentions=[{"id": BOT}]) == "trigger"


def test_whitelisted_group_without_mention_is_silent():
    assert classify_normalized(chat_id="oc_ok", mentions=[{"id": "ou_other"}]) is None
    assert classify_normalized(chat_id="oc_ok") is None


def test_stranger_group_with_mention_is_rejected():
    assert classify_normalized(chat_id="oc_stranger",
                               mentions=[{"id": BOT}]) == "reject"


def test_topic_chat_is_treated_like_a_group():
    assert classify_normalized(chat_id="oc_ok", chat_type="topic",
                               mentions=[{"id": BOT}]) == "trigger"
    assert classify_normalized(chat_id="oc_ok", chat_type="topic") is None


def test_missing_chat_type_is_treated_as_group():
    # 老事件里可能没有 chat_type: 按群处理（要 @ 才理）
    assert classify_normalized(chat_id="oc_ok", chat_type=None,
                               mentions=[{"id": BOT}]) == "trigger"
    assert classify_normalized(chat_id="oc_ok", chat_type=None) is None


# ---------- classify: 私聊 ----------

def test_whitelisted_user_private_chat_triggers_without_mention():
    assert classify_normalized(chat_id="oc_p2p", chat_type="p2p",
                               sender_id="ou_ok") == "trigger"


def test_stranger_private_chat_is_rejected():
    assert classify_normalized(chat_id="oc_p2p", chat_type="p2p",
                               sender_id="ou_stranger") == "reject"


def test_private_chat_without_sender_id_is_rejected():
    assert classify_normalized(chat_id="oc_p2p", chat_type="p2p",
                               sender_id=None) == "reject"


# ---------- classify: 静默 ----------

def test_image_message_is_silent_everywhere():
    assert classify_normalized(chat_id="oc_ok", message_type="image",
                               mentions=[{"id": BOT}]) is None
    assert classify_normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok",
                               message_type="image") is None


def test_bot_own_message_is_silent():
    assert classify_normalized(chat_id="oc_ok", sender_type="bot",
                               mentions=[{"id": BOT}]) is None
    assert classify_normalized(chat_id="oc_p2p", chat_type="p2p", sender_id="ou_ok",
                               sender_type="bot") is None


def test_empty_whitelists_reject_everyone():
    event = normalized(chat_id="oc_ok", mentions=[{"id": BOT}])
    assert classify(event, chats=[], users=[], bot_open_id=BOT) == "reject"
