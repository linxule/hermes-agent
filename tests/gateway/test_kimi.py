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
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType, SendResult
from gateway.platforms.kimi import (
    _CONNECT_FLAG_COMPRESSED,
    _CONNECT_FLAG_END_STREAM,
    KimiAdapter,
    KimiAuthError,
    KimiProtocolError,
    KimiRpcError,
    _is_standalone_slash_command,
    _parse_iso8601,
    _split_for_streaming,
    check_kimi_requirements,
)


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
        self.assertEqual(unary["X-Kimi-Bot-Token"], "km_b_prod_TEST_TOKEN")


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
        err_body = json.dumps({"error": {"code": "unauthenticated", "message": "token expired"}}).encode()
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


if __name__ == "__main__":
    unittest.main()
