"""测试用的小工具: 造事件、造假的外部依赖（不碰网络, 不起进程）。"""

from __future__ import annotations

import json
from types import SimpleNamespace


# ---------- 假的模型客户端 ----------

def message(content=None, tool_calls=None):
    """一条助手消息; tool_calls 用 (id, name, arguments) 三元组描述。"""
    calls = None
    if tool_calls:
        calls = [SimpleNamespace(id=cid, type="function",
                                 function=SimpleNamespace(name=name, arguments=args))
                 for cid, name, args in tool_calls]
    return SimpleNamespace(content=content, tool_calls=calls)


def response(msg):
    """一次模型调用的响应。"""
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeClient:
    """按剧本返回响应: 剧本里可以是响应对象, 也可以是无参协程函数（用来卡住某一次调用）。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if callable(item):
            item = await item()
        if isinstance(item, Exception):
            raise item
        return item


def raw_event(*, message_id: str = "om_1", chat_id: str = "oc_1",
              chat_type: str | None = "group", thread_id: str | None = None,
              message_type: str = "text", text: str = "看下茅台",
              mentions: list | None = None, sender_type: str = "user",
              sender_open_id: str | None = "ou_sender"):
    """造一个长得像 lark_oapi 回调数据的对象（normalize() 按属性读, SimpleNamespace 够用）。"""
    message = SimpleNamespace(
        message_id=message_id, chat_id=chat_id, chat_type=chat_type,
        thread_id=thread_id, message_type=message_type,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=mentions,
    )
    sender = SimpleNamespace(sender_type=sender_type,
                             sender_id=SimpleNamespace(open_id=sender_open_id))
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def sdk_mention(key: str, name: str, open_id: str):
    """SDK 的 mentions 元素: key 是正文里的 @_user_1 占位符, id 是 open_id。"""
    return SimpleNamespace(key=key, name=name, id=SimpleNamespace(open_id=open_id))


def normalized(*, chat_id: str = "oc_1", chat_type: str | None = "group",
               sender_id: str | None = "ou_sender", message_type: str = "text",
               sender_type: str = "user", text: str = "看下茅台",
               mentions: list | None = None) -> dict:
    """造一个**归一化之后**的事件（classify / handler 的入参）。"""
    return {
        "message_id": "om_1",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "thread_id": None,
        "message_type": message_type,
        "sender_type": sender_type,
        "sender_id": sender_id,
        "text": text,
        "mentions": list(mentions or []),
    }


class FakeSender:
    """假的 LarkSender: 只记下发过什么, 不真的发。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str | None, str | None]] = []
        self.fail = fail

    async def send(self, chat_id: str, *, text: str | None = None,
                   markdown: str | None = None) -> str | None:
        if self.fail:
            raise RuntimeError("发送失败（假 sender）")
        self.sent.append((chat_id, text, markdown))
        return "om_out"


class FakeSessions:
    """假的 ChatSessions: 只记下 (chat_id, text) 和 reset 过谁, 不碰模型。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.posts: list[tuple[str, str]] = []
        self.resets: list[str] = []
        self.fail = fail

    async def post(self, chat_id: str, user_text: str) -> None:
        if self.fail:
            raise RuntimeError("分析炸了（假 sessions）")
        self.posts.append((chat_id, user_text))

    def reset(self, chat_id: str) -> None:
        self.resets.append(chat_id)
