"""Unit tests for the Kimi platform adapter.

Focus is on pure-function correctness: envelope codec, chat-id routing,
dedup, MessageEvent synthesis, slash-command detection. Live-network tests
(GetMe, Subscribe against real Kimi) are gated behind a
``KIMI_INTEGRATION_TOKEN`` env var and skipped by default.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageType, SendResult
from gateway.platforms.kimi import (
    _CONNECT_FLAG_COMPRESSED,
    _CONNECT_FLAG_END_STREAM,
    _DMInflight,
    _WS_MAX_FRAME_SIZE,
    KimiAdapter,
    KimiAuthError,
    KimiProtocolError,
    KimiRpcError,
    _extract_short_id_from_text,
    _extract_user_identity,
    _is_standalone_slash_command,
    _parse_iso8601,
    _split_for_streaming,
    check_kimi_requirements,
)
from gateway.session import SessionSource


class _FakeWSStatusError(Exception):
    """Mimic websockets 12+ upgrade-rejection exception shape."""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _cfg(**extra) -> PlatformConfig:
    """Test config factory."""
    defaults = {"enable_dms": True, "enable_groups": True}
    defaults.update(extra)
    return PlatformConfig(
        enabled=True,
        token="km_b_prod_TEST_TOKEN",
        extra=defaults,
    )


class HelpersTests(unittest.TestCase):
    def test_is_standalone_slash_command(self):
        self.assertTrue(_is_standalone_slash_command("/status"))
        self.assertTrue(_is_standalone_slash_command("  /new  "))
        self.assertTrue(_is_standalone_slash_command("/compact"))
        self.assertFalse(_is_standalone_slash_command("/status please"))
        self.assertFalse(_is_standalone_slash_command("hello /status"))
        self.assertFalse(_is_standalone_slash_command("hi"))

    def test_split_for_streaming_short(self):
        self.assertEqual(_split_for_streaming("hi", 100), ["hi"])

    def test_split_for_streaming_long(self):
        text = "a" * 8000
        chunks = _split_for_streaming(text, 3500)
        # Rejoining should preserve length (minus stripped whitespace between).
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))

    def test_split_for_streaming_respects_size_and_preserves_content(self):
        text = "para one.\n\npara two.\n\npara three.\n\n" + ("x" * 5000)
        chunks = _split_for_streaming(text, 3500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        # Length preservation (minus leading whitespace stripped between chunks).
        rejoined_len = sum(len(c) for c in chunks)
        self.assertGreaterEqual(rejoined_len, len(text) - 4 * len(chunks))

    def test_parse_iso8601(self):
        self.assertIsNotNone(_parse_iso8601("2026-04-23T16:53:48Z"))
        self.assertIsNotNone(_parse_iso8601("2026-04-23T16:53:48+00:00"))
        self.assertIsNone(_parse_iso8601("not-a-date"))
        self.assertIsNone(_parse_iso8601(""))


class RequirementsTests(unittest.TestCase):
    def test_dependencies_available(self):
        # websockets + aiohttp are hermes core deps — this should always pass.
        self.assertTrue(check_kimi_requirements())


class AdapterInitTests(unittest.TestCase):
    def test_config_parsing(self):
        cfg = _cfg(
            enable_dms=True,
            enable_groups=False,
            openclaw_version="2026.5.1",
            claw_id="custom-id-123",
            group_require_mention=True,
            user_message_prefix="FROM KIMI: ",
        )
        adapter = KimiAdapter(cfg)
        self.assertEqual(adapter._bot_token, "km_b_prod_TEST_TOKEN")
        self.assertTrue(adapter._enable_dms)
        self.assertFalse(adapter._enable_groups)
        self.assertEqual(adapter._openclaw_version, "2026.5.1")
        self.assertEqual(adapter._claw_id, "custom-id-123")
        self.assertTrue(adapter._group_require_mention)
        self.assertEqual(adapter._user_message_prefix, "FROM KIMI: ")

    def test_claw_id_auto_generated(self):
        adapter = KimiAdapter(_cfg())
        self.assertTrue(adapter._claw_id.startswith("hermes-kimi-"))
        self.assertEqual(len(adapter._claw_id), len("hermes-kimi-") + 16)

    def test_ws_upgrade_headers_include_spoof(self):
        adapter = KimiAdapter(_cfg())
        headers = adapter._ws_upgrade_headers()
        self.assertEqual(headers["X-Kimi-Bot-Token"], "km_b_prod_TEST_TOKEN")
        self.assertEqual(headers["X-Kimi-OpenClaw-Version"], "2026.3.13")
        self.assertIn("X-Kimi-Claw-ID", headers)
        self.assertIn("X-Kimi-OpenClaw-Plugins", headers)

    def test_ws_upgrade_headers_omit_empty_spoof(self):
        adapter = KimiAdapter(_cfg(openclaw_version="", openclaw_skills=""))
        headers = adapter._ws_upgrade_headers()
        self.assertNotIn("X-Kimi-OpenClaw-Version", headers)
        # Skills default is empty — should not be included.
        self.assertNotIn("X-Kimi-OpenClaw-Skills", headers)

    def test_http_headers_unary_vs_streaming(self):
        adapter = KimiAdapter(_cfg())
        unary = adapter._http_headers(streaming=False)
        streaming = adapter._http_headers(streaming=True)
        self.assertEqual(unary["Content-Type"], "application/json")
        self.assertEqual(streaming["Content-Type"], "application/connect+json")
        self.assertEqual(streaming["Accept"], "application/connect+json")
        self.assertEqual(unary["X-Kimi-Bot-Token"], "km_b_prod_TEST_TOKEN")
        self.assertEqual(unary["X-Kimi-Claw-Version"], "0.25.0")
        self.assertEqual(unary["X-Kimi-OpenClaw-Version"], "2026.3.13")
        self.assertIn("X-Kimi-Claw-ID", unary)
        self.assertEqual(
            json.loads(unary["X-Kimi-OpenClaw-Plugins"]),
            [{"id": "kimi-claw", "version": "0.25.0"}],
        )
        self.assertEqual(streaming["X-Kimi-OpenClaw-Plugins"], unary["X-Kimi-OpenClaw-Plugins"])

    def test_legacy_inventory_header_strings_are_normalized_to_json(self):
        adapter = KimiAdapter(_cfg(
            openclaw_plugins="kimi-claw",
            openclaw_skills="kimiim,worker-safety",
        ))

        headers = adapter._http_headers(streaming=False)

        self.assertEqual(
            json.loads(headers["X-Kimi-OpenClaw-Plugins"]),
            [{"id": "kimi-claw", "version": "0.25.0"}],
        )
        self.assertEqual(
            json.loads(headers["X-Kimi-OpenClaw-Skills"]),
            ["kimiim", "worker-safety"],
        )


class EnvelopeCodecTests(unittest.TestCase):
    def test_encode_envelope_data(self):
        adapter = KimiAdapter(_cfg())
        body = adapter._encode_envelope(b'{"hello": "world"}')
        self.assertEqual(body[0], 0)  # data flag
        length = struct.unpack(">I", body[1:5])[0]
        self.assertEqual(length, len(b'{"hello": "world"}'))
        self.assertEqual(body[5:], b'{"hello": "world"}')

    def test_encode_envelope_end_stream(self):
        adapter = KimiAdapter(_cfg())
        body = adapter._encode_envelope(b"{}", end_stream=True)
        self.assertEqual(body[0], _CONNECT_FLAG_END_STREAM)


class EnvelopeParserTests(unittest.IsolatedAsyncioTestCase):
    """Feed synthetic byte streams through the envelope parser."""

    async def _collect(self, adapter: KimiAdapter, stream: bytes):
        reader = _FakeStreamReader(stream)
        out = []
        async for msg in adapter._connect_envelope_parser(reader):
            out.append(msg)
        return out

    async def test_single_data_frame_then_end_stream(self):
        adapter = KimiAdapter(_cfg())
        data = b'{"ping":{}}'
        stream = (
            bytes([0]) + struct.pack(">I", len(data)) + data
            + bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", 2) + b"{}"
        )
        msgs = await self._collect(adapter, stream)
        self.assertEqual(msgs, [{"ping": {}}])

    async def test_multiple_data_frames(self):
        adapter = KimiAdapter(_cfg())
        parts = [b'{"ping":{}}', b'{"message":{"id":"x"}}']
        stream = b""
        for p in parts:
            stream += bytes([0]) + struct.pack(">I", len(p)) + p
        stream += bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", 2) + b"{}"
        msgs = await self._collect(adapter, stream)
        self.assertEqual(msgs, [{"ping": {}}, {"message": {"id": "x"}}])

    async def test_end_stream_with_auth_error_raises(self):
        adapter = KimiAdapter(_cfg())
        err_body = json.dumps({
            "error": {"code": "unauthenticated", "message": "token expired"}
        }).encode()
        stream = bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", len(err_body)) + err_body
        with self.assertRaises(KimiAuthError):
            await self._collect(adapter, stream)

    async def test_end_stream_with_rpc_error_raises(self):
        adapter = KimiAdapter(_cfg())
        err_body = json.dumps({"error": {"code": "invalid_argument", "message": "bad"}}).encode()
        stream = bytes([_CONNECT_FLAG_END_STREAM]) + struct.pack(">I", len(err_body)) + err_body
        with self.assertRaises(KimiRpcError):
            await self._collect(adapter, stream)

    async def test_compressed_frame_rejected(self):
        adapter = KimiAdapter(_cfg())
        stream = bytes([_CONNECT_FLAG_COMPRESSED]) + struct.pack(">I", 2) + b"{}"
        with self.assertRaises(KimiProtocolError):
            await self._collect(adapter, stream)

    async def test_malformed_json_rejected(self):
        adapter = KimiAdapter(_cfg())
        bad = b"{not json"
        stream = bytes([0]) + struct.pack(">I", len(bad)) + bad
        with self.assertRaises(KimiProtocolError):
            await self._collect(adapter, stream)


class _FakeStreamReader:
    """Minimal aiohttp.StreamReader stub for parser tests."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise asyncio.IncompleteReadError(self._data[self._pos:], n)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class ChatIdRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_prefix_routes_to_send_dm(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_dm = AsyncMock(return_value=SendResult(success=True))
        adapter._send_group = AsyncMock()
        result = await adapter.send("dm:im:kimi:main", "hello")
        self.assertTrue(result.success)
        adapter._send_dm.assert_awaited_once()
        adapter._send_group.assert_not_awaited()

    async def test_room_prefix_routes_to_send_group(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_dm = AsyncMock()
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        result = await adapter.send("room:abc-def", "hello")
        self.assertTrue(result.success)
        adapter._send_group.assert_awaited_once()
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[0], "abc-def")  # room_id
        # thread_id position (from metadata) should be None
        self.assertIsNone(call_args.args[3])

    async def test_room_thread_suffix_extracts_thread(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        await adapter.send("room:abc-def/thread-xyz", "hello")
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[0], "abc-def")
        self.assertEqual(call_args.args[3], "thread-xyz")

    async def test_thread_id_in_metadata(self):
        adapter = KimiAdapter(_cfg())
        adapter._send_group = AsyncMock(return_value=SendResult(success=True))
        await adapter.send("room:abc-def", "hello", metadata={"thread_id": "t-1"})
        call_args = adapter._send_group.call_args
        self.assertEqual(call_args.args[3], "t-1")

    async def test_unknown_prefix_fails_cleanly(self):
        adapter = KimiAdapter(_cfg())
        result = await adapter.send("weird:foo", "hello")
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)
        self.assertIn("unknown chat_id format", result.error)


class DedupTests(unittest.TestCase):
    def test_same_pair_deduped(self):
        adapter = KimiAdapter(_cfg())
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))
        self.assertTrue(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))

    def test_different_kinds_not_deduped(self):
        adapter = KimiAdapter(_cfg())
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat-1", "msg-1"))
        self.assertFalse(adapter._dedup_is_duplicate("dm", "chat-1", "msg-1"))

    def test_eviction_at_max(self):
        adapter = KimiAdapter(_cfg())
        maxlen = adapter._processed.maxlen
        # Fill + one overflow
        for i in range(maxlen + 1):
            adapter._dedup_is_duplicate("group", "chat", f"msg-{i}")
        # First key should be evicted now.
        self.assertFalse(adapter._dedup_is_duplicate("group", "chat", "msg-0"))


class MessageEventSynthesisTests(unittest.TestCase):
    def test_text_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="group",
            text="hello",
            message_id="mid-1",
            chat_id="room:abc",
            chat_name="Test Room",
            user_id="u-1",
            user_name="Alice",
        )
        self.assertEqual(event.text, "hello")
        self.assertEqual(event.message_type, MessageType.TEXT)
        self.assertEqual(event.source.platform, Platform.KIMI)
        self.assertEqual(event.source.chat_id, "room:abc")
        self.assertEqual(event.source.chat_type, "group")
        self.assertFalse(event.internal)

    def test_command_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="dm",
            text="/reset",
            message_id="mid-2",
            chat_id="dm:im:kimi:main",
            chat_name="Kimi DM",
            user_id="u-2",
            user_name=None,
        )
        self.assertEqual(event.message_type, MessageType.COMMAND)

    def test_photo_event(self):
        adapter = KimiAdapter(_cfg())
        event = adapter._build_message_event(
            kind="group",
            text="look at this",
            message_id="mid-3",
            chat_id="room:abc",
            chat_name=None,
            user_id="u-3",
            user_name=None,
            media_urls=["https://example/img.jpg"],
            media_types=["image/jpeg"],
        )
        self.assertEqual(event.message_type, MessageType.PHOTO)
        self.assertEqual(event.media_urls, ["https://example/img.jpg"])

    def test_auto_skill_passthrough(self):
        adapter = KimiAdapter(_cfg(auto_skill="test-skill"))
        event = adapter._build_message_event(
            kind="group",
            text="hello",
            message_id="mid-4",
            chat_id="room:abc",
            chat_name=None,
            user_id=None,
            user_name=None,
        )
        self.assertEqual(event.auto_skill, "test-skill")


class GroupEventParsingTests(unittest.IsolatedAsyncioTestCase):
    """Kimi Subscribe protobuf-JSON events are converted into Hermes messages."""

    async def test_protobuf_json_chat_message_event_dispatches_summary(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "id": "evt-1",
            "chatMessage": {
                "chatId": "chat-1",
                "messageId": "msg-1",
                "status": "STATUS_COMPLETED",
                "senderId": "user-1",
                "senderShortId": "u1",
                "roomId": "room-1",
                "summary": "hello from kimi",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "hello from kimi")
        self.assertEqual(event.source.chat_id, "room:chat-1")
        self.assertEqual(event.source.user_id, "user-1")
        self.assertEqual(event.source.user_name, "u1")

    async def test_generated_payload_shape_extracts_text_block(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "id": "evt-2",
            "payload": {
                "case": "chatMessage",
                "value": {
                    "chatId": "chat-2",
                    "messageId": "msg-2",
                    "status": 2,
                    "senderShortId": "u2",
                    "blocks": [
                        {
                            "id": "b1",
                            "content": {
                                "case": "text",
                                "value": {"content": "block text"},
                            },
                        }
                    ],
                },
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "block text")
        self.assertEqual(event.source.user_id, "kimi:u2")

    async def test_generating_status_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "id": "evt-3",
            "chatMessage": {
                "chatId": "chat-3",
                "messageId": "msg-3",
                "status": "STATUS_GENERATING",
                "summary": "partial",
            },
        })

        adapter.handle_message.assert_not_awaited()


class SendMessageShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_send_uses_kimi_blocks_request_shape(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-1"})  # type: ignore

        result = await adapter._send_group(
            "chat-1",
            "hello",
            reply_to="ignored",
            thread_id="ignored",
            metadata={},
        )

        self.assertTrue(result.success)
        adapter._rpc_unary.assert_awaited_once()
        method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(method, "SendMessage")
        self.assertEqual(body["chatId"], "chat-1")
        self.assertEqual(body["blocks"][0]["text"]["content"], "hello")
        self.assertNotIn("text", body)
        self.assertNotIn("chat_id", body)

    async def test_group_send_supports_attachment_only_resource_link(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(return_value={"messageId": "sent-2"})  # type: ignore

        result = await adapter._send_group(
            "chat-1",
            "",
            reply_to=None,
            thread_id=None,
            metadata={"attachments": [{
                "uri": "kimi-file://file-123",
                "name": "report.pdf",
                "mimeType": "application/pdf",
                "sizeBytes": 42,
            }]},
        )

        self.assertTrue(result.success)
        _method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(len(body["blocks"]), 1)
        resource = body["blocks"][0]["resourceLink"]
        self.assertEqual(resource["uri"], "kimi-file://file-123")
        self.assertEqual(resource["title"], "report.pdf")
        self.assertNotIn("text", body["blocks"][0])

    async def test_send_document_uploads_to_kimi_file_resource(self):
        adapter = KimiAdapter(_cfg())
        adapter._http_session = MagicMock()
        adapter._send_group = AsyncMock(return_value=SendResult(success=True, message_id="sent-3"))  # type: ignore
        uploaded = [{
            "uri": "kimi-file://file-456",
            "name": "notes.txt",
            "mimeType": "text/plain",
        }]

        with patch("gateway.platforms.kimi._upload_kimi_files", new=AsyncMock(return_value=uploaded)) as upload_mock:
            result = await adapter.send_document("room:chat-1", "/tmp/notes.txt", caption="see attached")

        self.assertTrue(result.success)
        upload_mock.assert_awaited_once()
        adapter._send_group.assert_awaited_once()
        args = adapter._send_group.await_args.args
        self.assertEqual(args[0], "chat-1")
        self.assertEqual(args[1], "see attached")
        self.assertEqual(adapter._send_group.await_args.kwargs["metadata"]["attachments"], uploaded)

    async def test_kimi_file_media_resolution_replaces_uri_with_local_path(self):
        adapter = KimiAdapter(_cfg())
        adapter._resolve_kimi_file_uri = AsyncMock(return_value={
            "localPath": "/tmp/kimi-file/report.pdf",
            "contentType": "application/pdf",
        })  # type: ignore

        urls, types = await adapter._resolve_kimi_file_media(
            ["kimi-file://12345678-1234-1234-1234-123456789abc", "https://example/a.png"],
            ["resource_link", "image/png"],
            message_id="msg-1",
        )

        self.assertEqual(urls, ["/tmp/kimi-file/report.pdf", "https://example/a.png"])
        self.assertEqual(types, ["application/pdf", "image/png"])


class GroupRpcHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_chat_info_fetches_room_and_members(self):
        adapter = KimiAdapter(_cfg())
        adapter._rpc_unary = AsyncMock(side_effect=[
            {"room": {"id": "room-1", "name": "Research Room"}},
            {"members": [{"id": "m1", "shortId": "alice"}]},
        ])  # type: ignore

        info = await adapter.get_chat_info("room:room-1")

        self.assertEqual(info["name"], "Research Room")
        self.assertEqual(info["members"], [{"id": "m1", "shortId": "alice"}])
        self.assertEqual(
            adapter._rpc_unary.await_args_list[0].args,
            ("GetRoom", {"roomId": "room-1"}),
        )
        self.assertEqual(adapter._rpc_unary.await_args_list[1].args[0], "ListMembers")
        self.assertEqual(adapter._rpc_unary.await_args_list[1].args[1]["roomId"], "room-1")

    async def test_empty_subscribe_event_hydrates_from_list_messages(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._rpc_unary = AsyncMock(return_value={
            "messages": [
                {
                    "senderId": "user-9",
                    "senderShortId": "u9",
                    "message": {
                        "id": "msg-9",
                        "blocks": [
                            {
                                "content": {
                                    "case": "text",
                                    "value": {"content": "hydrated text"},
                                },
                            }
                        ],
                    },
                }
            ]
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-9",
                "messageId": "msg-9",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "hydrated text")
        self.assertEqual(event.source.user_id, "user-9")
        self.assertEqual(event.source.user_name, "u9")
        method, body = adapter._rpc_unary.await_args.args
        self.assertEqual(method, "ListMessages")
        self.assertEqual(body["chatId"], "chat-9")
        self.assertEqual(body["startMessageId"], "msg-9")

    async def test_failed_hydration_does_not_dispatch_or_dedup_empty_event(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(side_effect=[
            KimiRpcError("temporary bad cursor"),
            {
                "id": "msg-10",
                "blocks": [
                    {
                        "content": {
                            "case": "text",
                            "value": {"content": "second replay text"},
                        },
                    }
                ],
            },
        ])  # type: ignore
        event = {
            "chatMessage": {
                "chatId": "chat-10",
                "messageId": "msg-10",
                "status": "STATUS_COMPLETED",
            },
        }

        await adapter._on_group_event(event)
        adapter.handle_message.assert_not_awaited()

        await adapter._on_group_event(event)
        adapter.handle_message.assert_awaited_once()
        delivered = adapter.handle_message.await_args.args[0]
        self.assertEqual(delivered.text, "second replay text")

    async def test_hydrated_self_message_is_filtered(self):
        adapter = KimiAdapter(_cfg())
        adapter._me_id = "bot-self"
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-self",
            "senderId": "bot-self",
            "blocks": [
                {
                    "content": {
                        "case": "text",
                        "value": {"content": "echo"},
                    },
                }
            ],
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-self",
                "messageId": "msg-self",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_not_awaited()

    async def test_non_user_group_message_role_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-bot",
                "messageId": "msg-bot",
                "status": "STATUS_COMPLETED",
                "role": "ROLE_ASSISTANT",
                "senderId": "assistant-1",
                "summary": "assistant echo",
            },
        })

        adapter.handle_message.assert_not_awaited()

    async def test_hydrated_non_user_group_message_role_is_not_dispatched(self):
        adapter = KimiAdapter(_cfg())
        adapter.handle_message = AsyncMock()  # type: ignore
        adapter._fetch_group_message = AsyncMock(return_value={
            "id": "msg-hydrated-bot",
            "role": "ASSISTANT",
            "senderId": "assistant-2",
            "blocks": [
                {
                    "content": {
                        "case": "text",
                        "value": {"content": "hydrated assistant echo"},
                    },
                }
            ],
        })  # type: ignore

        await adapter._on_group_event({
            "chatMessage": {
                "chatId": "chat-hydrated-bot",
                "messageId": "msg-hydrated-bot",
                "status": "STATUS_COMPLETED",
            },
        })

        adapter.handle_message.assert_not_awaited()


class ConfigIntegrationTests(unittest.TestCase):
    """Platform enum + env-var pickup via gateway.config."""

    def test_platform_enum_exists(self):
        self.assertEqual(Platform.KIMI.value, "kimi")

    def test_env_override_loads_token(self):
        from gateway.config import GatewayConfig, _apply_env_overrides
        cfg = GatewayConfig()
        with patch.dict(os.environ, {"KIMI_BOT_TOKEN": "km_b_prod_ENV_TEST"}, clear=False):
            _apply_env_overrides(cfg)
        kimi_cfg = cfg.platforms.get(Platform.KIMI)
        self.assertIsNotNone(kimi_cfg)
        self.assertEqual(kimi_cfg.token, "km_b_prod_ENV_TEST")
        self.assertTrue(kimi_cfg.enabled)


class AuthorizationIntegrationTests(unittest.TestCase):
    """Platform appears in the authorization maps in run.py."""

    def test_platform_in_allowlist_maps(self):
        # Lightweight smoke test — we read the file instead of importing
        # gateway.run (which has heavy side effects).
        import pathlib
        run_py = (
            pathlib.Path(__file__).parent.parent.parent
            / "gateway" / "run.py"
        )
        text = run_py.read_text()
        self.assertIn('Platform.KIMI: "KIMI_ALLOWED_USERS"', text)
        self.assertIn('Platform.KIMI: "KIMI_ALLOW_ALL_USERS"', text)

    def test_group_allowlist_accepts_raw_room_id(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(platforms={Platform.KIMI: PlatformConfig(enabled=True)})
        runner.adapters = {Platform.KIMI: SimpleNamespace(send=AsyncMock())}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        source = SessionSource(
            platform=Platform.KIMI,
            user_id="kimi:user-1",
            chat_id="room:chat-1",
            user_name="tester",
            chat_type="group",
        )

        with patch.dict(os.environ, {
            "KIMI_GROUP_ALLOWED_USERS": "chat-1",
            "KIMI_ALLOWED_USERS": "",
            "GATEWAY_ALLOWED_USERS": "",
            "GATEWAY_ALLOW_ALL_USERS": "",
        }, clear=False):
            self.assertTrue(runner._is_user_authorized(source))

    def test_group_allowlist_accepts_prefixed_room_id(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(platforms={Platform.KIMI: PlatformConfig(enabled=True)})
        runner.adapters = {Platform.KIMI: SimpleNamespace(send=AsyncMock())}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        source = SessionSource(
            platform=Platform.KIMI,
            user_id="kimi:user-1",
            chat_id="room:chat-1",
            user_name="tester",
            chat_type="group",
        )

        with patch.dict(os.environ, {
            "KIMI_GROUP_ALLOWED_USERS": "room:chat-1",
            "KIMI_ALLOWED_USERS": "",
            "GATEWAY_ALLOWED_USERS": "",
            "GATEWAY_ALLOW_ALL_USERS": "",
        }, clear=False):
            self.assertTrue(runner._is_user_authorized(source))


class UserIdentityExtractionTests(unittest.TestCase):
    """_extract_user_identity probes several plausible Kimi wire shapes."""

    def test_flat_userid(self):
        uid, name = _extract_user_identity({"userId": "u-123"})
        self.assertEqual(uid, "u-123")
        self.assertIsNone(name)

    def test_flat_user_id_snake(self):
        uid, _ = _extract_user_identity({"user_id": "u-456"})
        self.assertEqual(uid, "u-456")

    def test_nested_sender(self):
        uid, name = _extract_user_identity(
            {"sender": {"id": "u-789", "name": "Alice"}}
        )
        self.assertEqual(uid, "u-789")
        self.assertEqual(name, "Alice")

    def test_nested_user_with_display_name(self):
        uid, name = _extract_user_identity(
            {"user": {"userId": "u-abc", "display_name": "Bob"}}
        )
        self.assertEqual(uid, "u-abc")
        self.assertEqual(name, "Bob")

    def test_no_identity(self):
        uid, name = _extract_user_identity({"sessionId": "im:kimi:main"})
        self.assertIsNone(uid)
        self.assertIsNone(name)

    def test_non_dict_input(self):
        uid, name = _extract_user_identity(None)
        self.assertIsNone(uid)
        self.assertIsNone(name)
        uid, name = _extract_user_identity("string")
        self.assertIsNone(uid)
        self.assertIsNone(name)


class SenderShortIdPrefixTests(unittest.TestCase):
    """kimi-claw's [sender_short_id: X] text prefix (group-routed-over-ACP)."""

    def test_extracts_short_id_from_prefix_line(self):
        text = (
            "Message From Kimi Group Chat Room:\n"
            "[sender_short_id: u_abc123]\n"
            "hello there"
        )
        self.assertEqual(_extract_short_id_from_text(text), "u_abc123")

    def test_extracts_short_id_anywhere_in_text(self):
        # The kimi-claw injector places it after the group prefix; we accept
        # any line-start match so we're robust to prompt-text transforms.
        text = "prelude\n[sender_short_id: u_xyz]\nactual content"
        self.assertEqual(_extract_short_id_from_text(text), "u_xyz")

    def test_no_prefix_returns_none(self):
        self.assertIsNone(_extract_short_id_from_text("plain message"))
        self.assertIsNone(_extract_short_id_from_text(""))
        self.assertIsNone(_extract_short_id_from_text(None))  # type: ignore

    def test_empty_short_id_returns_none(self):
        # Malformed: empty content between brackets.
        self.assertIsNone(_extract_short_id_from_text("[sender_short_id: ]"))

    def test_strips_surrounding_whitespace(self):
        text = "[sender_short_id:   u_padded   ]"
        self.assertEqual(_extract_short_id_from_text(text), "u_padded")


class EnvelopeLengthCapTests(unittest.IsolatedAsyncioTestCase):
    """Connect envelope parser refuses oversize frames (I4 DoS guard)."""

    async def test_envelope_length_cap_rejects_oversize(self):
        adapter = KimiAdapter(_cfg())
        oversize = _WS_MAX_FRAME_SIZE + 1
        header = bytes([0x00]) + struct.pack(">I", oversize)
        reader = MagicMock()
        reader.readexactly = AsyncMock(return_value=header)
        with self.assertRaises(KimiProtocolError) as ctx:
            async for _ in adapter._connect_envelope_parser(reader):
                self.fail("should not yield")
        self.assertIn("exceeds max frame size", str(ctx.exception))

    async def test_envelope_length_at_cap_is_allowed(self):
        adapter = KimiAdapter(_cfg())
        body = b'{"ping":{}}'
        header = bytes([0x00]) + struct.pack(">I", len(body))
        reads = [header, body, b""]
        reader = MagicMock()

        async def _readexactly(n):
            if not reads:
                raise asyncio.IncompleteReadError(b"", n)
            chunk = reads.pop(0)
            if len(chunk) != n:
                # Simulate end-of-stream for the empty tail read
                raise asyncio.IncompleteReadError(chunk, n)
            return chunk

        reader.readexactly = _readexactly
        yielded = []
        with self.assertRaises(Exception):
            async for msg in adapter._connect_envelope_parser(reader):
                yielded.append(msg)
        self.assertEqual(yielded, [{"ping": {}}])


class DMInflightQueueTests(unittest.IsolatedAsyncioTestCase):
    """Overlapping DM prompts get FIFO end_turn responses (I2)."""

    async def test_send_dm_pops_oldest_inflight(self):
        adapter = KimiAdapter(_cfg())
        # Simulate two in-flight prompts on the same kimi_sid
        sid = "im:kimi:main"
        adapter._dm_inflight[sid] = deque([
            _DMInflight(kimi_sid=sid, req_id=101),
            _DMInflight(kimi_sid=sid, req_id=102),
        ])

        respond_mock = AsyncMock()
        adapter._dm_respond = respond_mock  # type: ignore
        adapter._dm_emit_chunk = AsyncMock()  # type: ignore
        adapter._ws = MagicMock()  # non-None sentinel

        # First reply should pop req_id=101
        await adapter._send_dm(sid, "reply one", reply_to=None, metadata={})
        self.assertEqual(respond_mock.await_args_list[0].args[0], 101)

        # Second reply should pop req_id=102 and leave the queue empty
        await adapter._send_dm(sid, "reply two", reply_to=None, metadata={})
        self.assertEqual(respond_mock.await_args_list[1].args[0], 102)
        self.assertNotIn(sid, adapter._dm_inflight)

    async def test_send_dm_no_inflight_is_harmless(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_respond = AsyncMock()  # type: ignore
        adapter._dm_emit_chunk = AsyncMock()  # type: ignore
        adapter._ws = MagicMock()
        result = await adapter._send_dm(
            "im:kimi:main", "reply", reply_to=None, metadata={}
        )
        self.assertTrue(result.success)

    async def test_session_cancel_cancels_active_processing_and_clears_inflight(self):
        adapter = KimiAdapter(_cfg())
        sid = "im:kimi:main"
        adapter._dm_inflight[sid] = deque([
            _DMInflight(kimi_sid=sid, req_id=101),
        ])
        adapter.cancel_session_processing = AsyncMock()  # type: ignore

        await adapter._dm_cancel_session(sid)

        adapter.cancel_session_processing.assert_awaited_once()
        self.assertNotIn(sid, adapter._dm_inflight)
        kwargs = adapter.cancel_session_processing.await_args.kwargs
        self.assertTrue(kwargs["release_guard"])
        self.assertTrue(kwargs["discard_pending"])


class WSUpgradeClassificationTests(unittest.IsolatedAsyncioTestCase):
    """_dm_ws_connect_once special-cases 401/403/409 (C2)."""

    async def test_403_returns_permanent(self):
        adapter = KimiAdapter(_cfg())
        with patch(
            "gateway.platforms.kimi.websockets.connect",
            side_effect=_FakeWSStatusError(403),
        ):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 3)

    async def test_409_first_strike_cools_off_60s(self):
        adapter = KimiAdapter(_cfg())
        sleep_mock = AsyncMock()
        with patch(
            "gateway.platforms.kimi.websockets.connect",
            side_effect=_FakeWSStatusError(409),
        ), patch("gateway.platforms.kimi.asyncio.sleep", sleep_mock):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 0)
        self.assertEqual(adapter._dm_409_strikes, 1)
        sleep_mock.assert_awaited_once_with(60.0)

    async def test_409_second_strike_cools_off_300s(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_409_strikes = 1  # prior strike
        sleep_mock = AsyncMock()
        with patch(
            "gateway.platforms.kimi.websockets.connect",
            side_effect=_FakeWSStatusError(409),
        ), patch("gateway.platforms.kimi.asyncio.sleep", sleep_mock):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 0)
        self.assertEqual(adapter._dm_409_strikes, 2)
        sleep_mock.assert_awaited_once_with(300.0)

    async def test_401_returns_permanent(self):
        adapter = KimiAdapter(_cfg())
        with patch(
            "gateway.platforms.kimi.websockets.connect",
            side_effect=_FakeWSStatusError(401),
        ):
            rc = await adapter._dm_ws_connect_once()
        self.assertEqual(rc, 3)


class LifecycleStatusTests(unittest.IsolatedAsyncioTestCase):
    """connect/disconnect drive base-class status + permanent auth sets fatal."""

    async def test_permanent_auth_triggers_fatal_error(self):
        adapter = KimiAdapter(_cfg())
        adapter._dm_ws_connect_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._dm_ws_loop()
        self.assertEqual(adapter._fatal_error_code, "kimi_dm_auth")
        self.assertFalse(adapter._fatal_error_retryable)

    async def test_closing_shutdown_does_not_set_fatal(self):
        """If _closing is already True (clean shutdown path), don't leak fatal."""
        adapter = KimiAdapter(_cfg())
        adapter._closing = True
        adapter._dm_ws_connect_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._dm_ws_loop()
        self.assertIsNone(adapter._fatal_error_code)

    async def test_groups_permanent_auth_triggers_fatal_error(self):
        adapter = KimiAdapter(_cfg())
        adapter._group_subscribe_once = AsyncMock(return_value=3)  # type: ignore
        await adapter._group_subscribe_loop()
        self.assertEqual(adapter._fatal_error_code, "kimi_groups_auth")


if __name__ == "__main__":
    unittest.main()
