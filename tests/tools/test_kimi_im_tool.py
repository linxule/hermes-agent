import json

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from tools import kimi_im_tool


class _FakeKimiAdapter:
    instances = []

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.cleaned = False
        _FakeKimiAdapter.instances.append(self)

    async def get_me(self):
        self.calls.append(("get_me",))
        return {"me": {"id": "bot-1"}}

    async def get_group(self, room_id):
        self.calls.append(("get_group", room_id))
        return {"id": room_id, "name": "Room"}

    async def list_group_members(self, room_id, *, page_size=100):
        self.calls.append(("list_group_members", room_id, page_size))
        return [{"id": "u1"}, {"id": "u2"}]

    async def list_group_messages(self, chat_id, **kwargs):
        self.calls.append(("list_group_messages", chat_id, kwargs))
        return [{"messageId": "m1"}]

    async def list_group_files(self, room_id, *, page_size=100):
        self.calls.append(("list_group_files", room_id, page_size))
        return [{"fileId": "f1"}]

    async def send(self, chat_id, text):
        self.calls.append(("send", chat_id, text))
        return SendResult(success=True, message_id="sent-1")

    async def _cleanup_http(self):
        self.cleaned = True


@pytest.fixture
def kimi_tool_env(monkeypatch):
    _FakeKimiAdapter.instances = []
    pconfig = PlatformConfig(enabled=True, token="km_b_prod_TEST", extra={})
    monkeypatch.setattr(kimi_im_tool, "_get_kimi_config", lambda: pconfig)

    import gateway.platforms.kimi as kimi_platform

    monkeypatch.setattr(kimi_platform, "KimiAdapter", _FakeKimiAdapter)
    return pconfig


@pytest.mark.asyncio
async def test_kimi_im_send_message_normalizes_room_id(kimi_tool_env):
    result = json.loads(await kimi_im_tool.kimi_im(
        action="send_message",
        room_id="room-123",
        text="hello",
    ))

    adapter = _FakeKimiAdapter.instances[-1]
    assert result == {
        "success": True,
        "message_id": "sent-1",
        "error": None,
        "retryable": False,
    }
    assert adapter.calls == [("send", "room:room-123", "hello")]
    assert adapter.cleaned is True


@pytest.mark.asyncio
async def test_kimi_im_list_messages_uses_chat_id(kimi_tool_env):
    result = json.loads(await kimi_im_tool.kimi_im(
        action="list_messages",
        chat_id="room:room-123/thread-456",
        message_id="m0",
        limit=10,
    ))

    adapter = _FakeKimiAdapter.instances[-1]
    assert result["count"] == 1
    assert adapter.calls == [
        (
            "list_group_messages",
            "thread-456",
            {"limit": 10, "start_message_id": "m0"},
        )
    ]
    assert adapter.cleaned is True


@pytest.mark.asyncio
async def test_kimi_im_group_helpers(kimi_tool_env):
    group = json.loads(await kimi_im_tool.kimi_im(
        action="get_group",
        room_id="room:room-1/thread-ignored",
    ))
    members = json.loads(await kimi_im_tool.kimi_im(
        action="list_members",
        room_id="room:room-1",
        page_size=50,
    ))
    files = json.loads(await kimi_im_tool.kimi_im(
        action="list_files",
        room_id="room:room-1",
        page_size=25,
    ))

    assert group == {"id": "room-1", "name": "Room"}
    assert members == {"members": [{"id": "u1"}, {"id": "u2"}], "count": 2}
    assert files == {"files": [{"fileId": "f1"}], "count": 1}
    assert _FakeKimiAdapter.instances[-3].calls == [("get_group", "room-1")]
    assert _FakeKimiAdapter.instances[-2].calls == [
        ("list_group_members", "room-1", 50)
    ]
    assert _FakeKimiAdapter.instances[-1].calls == [
        ("list_group_files", "room-1", 25)
    ]


@pytest.mark.asyncio
async def test_kimi_im_validation_errors(kimi_tool_env):
    missing_text = json.loads(await kimi_im_tool.kimi_im(
        action="send_message",
        room_id="room-1",
    ))
    unknown = json.loads(await kimi_im_tool.kimi_im(action="not_real"))

    assert missing_text["error"] == "text is required for send_message"
    assert unknown["error"] == "Unknown action: not_real"
    assert "send_message" in unknown["available_actions"]


@pytest.mark.asyncio
async def test_kimi_im_requires_config(monkeypatch):
    monkeypatch.setattr(kimi_im_tool, "_get_kimi_config", lambda: None)

    result = json.loads(await kimi_im_tool.kimi_im(action="me"))

    assert result == {"error": "Kimi platform is not configured."}
