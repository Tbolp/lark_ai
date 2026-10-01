"""发送日志: 带上 chat_id（群和私聊的发送交织时能对上号）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.lark_sender import LarkSender


class FakeResponse:
    """够用的假响应: success() / data / code / msg 四个口子（真的那套只看这几个）。"""

    def __init__(self, *, ok: bool = True, message_id: str = "om_out",
                 code: int = 0, msg: str = "ok") -> None:
        self._ok = ok
        self.data = SimpleNamespace(message_id=message_id) if ok else None
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._ok


class FakeMessageAPI:
    """假的 im.v1.message: 记下请求, 回一个写死的响应。"""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list = []

    async def acreate(self, req):
        self.requests.append(req)
        return self.response

    async def aupdate(self, req):
        self.requests.append(req)
        return self.response


def make_sender(response: FakeResponse) -> tuple[LarkSender, FakeMessageAPI]:
    sender = LarkSender({"app_id": "cli_x", "app_secret": "secret", "domain": ""})
    api = FakeMessageAPI(response)
    sender._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))
    return sender, api


def test_send_logs_chat_id(capsys):
    sender, api = make_sender(FakeResponse(message_id="om_out"))
    message_id = asyncio.run(sender.send("oc_p2p", markdown="正文"))
    assert message_id == "om_out"
    assert len(api.requests) == 1
    err = capsys.readouterr().err
    assert "消息发送成功 chat_id=oc_p2p om_out" in err


def test_send_failure_logs_chat_id(capsys):
    sender, _ = make_sender(FakeResponse(ok=False, code=99, msg="坏"))
    assert asyncio.run(sender.send("oc_group", markdown="正文")) is None
    err = capsys.readouterr().err
    assert "发送失败 chat_id=oc_group" in err
    assert "code=99" in err


def test_edit_failure_logs_chat_id(capsys):
    sender, _ = make_sender(FakeResponse(ok=False, code=88, msg="坏"))
    ok = asyncio.run(sender.edit("om_in", chat_id="oc_p2p", markdown="改过的正文"))
    assert ok is False
    err = capsys.readouterr().err
    assert "改写失败 chat_id=oc_p2p om_in" in err


def test_edit_without_chat_id_does_not_break(capsys):
    sender, _ = make_sender(FakeResponse(ok=False, code=88, msg="坏"))
    assert asyncio.run(sender.edit("om_in", markdown="改过的正文")) is False
    assert "改写失败 chat_id=- om_in" in capsys.readouterr().err
