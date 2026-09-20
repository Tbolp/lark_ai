"""往飞书群里发消息 / 改写已发出的消息（飞书官方 SDK）。

发出去的形态: markdown 整段塞进 post 的 md 元素, 渲染交给服务端。
（这个形态是照着 lark-cli 的 --dry-run 输出复刻的, 不是猜的 —— 换实现时最容易在这里走样。）

异步: SDK 的 acreate/aupdate 本身就是协程, 不需要线程。

凭据文件里可选 `domain`（默认飞书国内站）:
  国内飞书   https://open.feishu.cn（默认）
  Lark 国际  https://open.larksuite.com
  本地假服务 http://127.0.0.1:8000   （测试用, 走的是同一套 SDK 代码路径）
"""

from __future__ import annotations

import json
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateMessageRequest, CreateMessageRequestBody,
                                 UpdateMessageRequest, UpdateMessageRequestBody)


def _content(markdown: str | None, text: str | None) -> tuple[str, str]:
    """返回 (msg_type, content)。markdown 走 post + md 元素, 纯文本走 text。"""
    if text is not None:
        return "text", json.dumps({"text": text}, ensure_ascii=False)
    if markdown is None:
        raise ValueError("text 与 markdown 必须二选一")
    payload = {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
    return "post", json.dumps(payload, ensure_ascii=False)


class LarkSender:
    """发新消息 / 改已发出的消息, 都走官方 SDK 的 HTTP 接口。

    send 成功返回 message_id（失败返回 None）, edit 成功返回 True。
    text 与 markdown 二选一。
    """

    def __init__(self, config: dict) -> None:
        """按本地凭据（app_id / app_secret / 可选 domain）建一个客户端。"""
        builder = (lark.Client.builder()
                   .app_id(config["app_id"]).app_secret(config["app_secret"]))
        if config.get("domain"):
            builder = builder.domain(config["domain"])
        self._client = builder.build()

    async def send(self, chat_id: str, *, text: str | None = None,
                   markdown: str | None = None) -> str | None:
        msg_type, content = _content(markdown, text)
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type(msg_type).content(content).build())
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id").request_body(body).build())
        resp = await self._client.im.v1.message.acreate(req)
        if not resp.success() or not resp.data:
            sys.stderr.write(f"[bot] 发送失败: code={resp.code} msg={resp.msg!r}\n")
            return None
        message_id = resp.data.message_id
        sys.stderr.write(f"[bot] 消息发送成功 {message_id}\n")
        return message_id

    async def edit(self, message_id: str, *, text: str | None = None,
                   markdown: str | None = None) -> bool:
        msg_type, content = _content(markdown, text)
        body = (UpdateMessageRequestBody.builder()
                .msg_type(msg_type).content(content).build())
        req = UpdateMessageRequest.builder().message_id(message_id).request_body(body).build()
        resp = await self._client.im.v1.message.aupdate(req)
        if not resp.success():
            sys.stderr.write(f"[bot] 改写失败: code={resp.code} msg={resp.msg!r}\n")
            return False
        return True
