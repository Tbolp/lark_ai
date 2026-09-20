"""接入飞书: 长连接收群消息, 归一化, 过一道判断后直接发事件。

SDK 的 ws.Client.start() 是阻塞的, 所以要跑在自己的线程里; 回调也在那个线程执行,
因此这里只做"归一化 + 丢回事件循环 + 发事件", 业务处理全在事件循环上跑。
断线重连由 SDK 自己负责（auto_reconnect）。

飞书这边的判断都收在这个文件里: 哪些是消息事件（normalize）、哪条才算"@ 了机器人"
（is_mention）。过了判断的消息才 emit(MENTION), handler 只认 payload、不碰飞书字段。

归一化后的内部事件（handler 按这个形状读）:
    message_id / chat_id / message_type / sender_type / sender_id
    text        正文（@ 机器人的占位符已经去掉）
    mentions    [{"id": <open_id>, "name": <显示名>}]
"""

from __future__ import annotations

import asyncio
import json
import threading

import lark_oapi as lark
from core import events as core

from .events import MENTION


def is_mention(event: dict, *, chat_id: str, bot_open_id: str) -> bool:
    """只认: 目标群 + 文本消息 + 不是机器人发的 + @ 了机器人。"""
    if event.get("chat_id") != chat_id:
        return False
    if event.get("message_type") != "text" or event.get("sender_type") == "bot":
        return False
    return any((mention or {}).get("id") == bot_open_id
               for mention in event.get("mentions") or [])


def normalize(data) -> dict | None:
    """把 SDK 的消息事件归一化成内部事件; 不是消息事件就返回 None。"""
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    if message is None:
        return None

    try:
        text = json.loads(getattr(message, "content", None) or "{}").get("text") or ""
    except json.JSONDecodeError:
        text = str(getattr(message, "content", "") or "")

    mentions = []
    for mention in getattr(message, "mentions", None) or []:
        key = getattr(mention, "key", None)
        if key:
            text = text.replace(key, "")       # 正文里的 @_user_1 这类占位符
        raw_id = getattr(mention, "id", None)
        open_id = getattr(raw_id, "open_id", None) or (
            raw_id if isinstance(raw_id, str) else None)
        mentions.append({"id": open_id, "name": getattr(mention, "name", None)})

    return {
        "message_id": getattr(message, "message_id", None),
        "chat_id": getattr(message, "chat_id", None),
        "message_type": getattr(message, "message_type", None),
        "sender_type": getattr(sender, "sender_type", None),
        "sender_id": getattr(getattr(sender, "sender_id", None), "open_id", None),
        "text": text.strip(),
        "mentions": mentions,
    }


class LarkChatSource:
    """群里来的消息: 长连接收消息 -> 归一化 -> 判断是不是 @ 了机器人 -> 发事件。

    收所有机器人能收到的消息, 但只有 @ 了机器人的文本消息才 emit(MENTION)
    （判断规则见 is_mention）。断线重连交给 SDK。
    谁是 MENTION 的 handler 由 app 注册, 这里不关心。

    SDK 的 ws 模块在**导入时**就抓了一个全局事件循环
    （`lark_oapi/ws/client.py` 顶部的 `loop = asyncio.get_event_loop()`），
    而我们是在自己的事件循环里导入它的 —— 那样它拿到的是我们的主循环,
    在别的线程里 run_until_complete 会直接报 "This event loop is already running"。
    所以连接线程里先给它换一个属于这个线程的 loop, 客户端也在线程里建。
    """

    def __init__(self, config: dict, *, chat_id: str, bot_open_id: str,
                 log_level: lark.LogLevel = lark.LogLevel.INFO) -> None:
        self._config = dict(config)
        self._chat_id = chat_id
        self._bot_open_id = bot_open_id
        self._log_level = log_level
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def _on_sdk_event(self, data) -> None:
        """SDK 线程里的回调: 归一化后丢回事件循环（发事件必须在事件循环线程里）。"""
        event = normalize(data)
        if event is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._receive, event)

    def _receive(self, event: dict) -> None:
        """事件循环线程里收尾: 是 @ 触发就发事件（同步返回, handler 由框架调度）。"""
        if is_mention(event, chat_id=self._chat_id, bot_open_id=self._bot_open_id):
            core.emit(MENTION, event)

    def _run_client(self) -> None:
        """在专用线程里跑 SDK（它需要自己的事件循环）。"""
        from lark_oapi.ws import client as ws_client
        ws_client.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_client.loop)

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_sdk_event)
                   .build())
        kwargs: dict = {"event_handler": handler, "log_level": self._log_level}
        if self._config.get("domain"):
            kwargs["domain"] = self._config["domain"]
        self._client = lark.ws.Client(self._config["app_id"],
                                     self._config["app_secret"], **kwargs)
        try:
            self._client.start()
        finally:
            ws_client.loop.close()

    async def run(self) -> None:
        """连着收, 直到进程退出（返回表示"没有更多消息了"）。"""
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._run_client, name="lark-ws", daemon=True).start()
        # start() 没有对应的 stop(), 连接线程是 daemon: 进程退出时就结束了。
        await asyncio.Event().wait()
