"""Kimi (kimi.com / Moonshot AI) platform adapter.

Bridges two channels under one bot identity using a single `X-Kimi-Bot-Token`
credential:

- DM channel:    ``wss://www.kimi.com/api-claw/bots/agent-ws``
  Kimi speaks Zed Agent Client Protocol (ACP) JSON-RPC over WebSocket. The
  adapter responds to ACP handshake frames locally (``initialize``,
  ``session/new``) and converts ``session/prompt`` frames into MessageEvent
  for dispatch through the normal gateway pipeline.

- Group channel: ``https://www.kimi.com/api-ws/``
  Kimi exposes a Connect RPC (https://connectrpc.com) surface for group IM.
  The adapter opens a bidi-stream ``Subscribe`` call (all-rooms firehose),
  translates inbound ``ChatMessageEvent`` payloads into MessageEvent, and
  sends outbound replies via unary ``SendMessage``. Both use
  ``application/json`` / ``application/connect+json`` content types — no
  protobuf bindings required on our side.

The adapter owns no subprocess. Messages flow directly into
``BasePlatformAdapter.handle_message`` via ``self._message_handler``.

Group-room participation requires spoofed ``X-Kimi-OpenClaw-*`` runtime headers
on the DM WebSocket upgrade (Kimi's server enforces a minimum OpenClaw version
of ``2026.3.13`` for group-chat gating). Defaults in ``_GROUP_GATE_DEFAULTS``
unlock group access out of the box; override via ``config.extra``.

References:
    - Connect protocol spec: https://connectrpc.com/docs/protocol
    - Zed ACP spec: https://github.com/zed-industries/agent-client-protocol
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

try:
    import aiohttp  # type: ignore
    _AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - hermes core already depends on aiohttp
    aiohttp = None  # type: ignore
    _AIOHTTP_AVAILABLE = False

try:
    import websockets  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore
    ConnectionClosed = Exception  # type: ignore
    _WEBSOCKETS_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_BASE_URL = "https://www.kimi.com/api-ws"
_DEFAULT_DM_WS_URL = "wss://www.kimi.com/api-claw/bots/agent-ws"
_IM_SERVICE = "kimi.gateway.im.v1.IMService"

# Connect protocol envelope flags (https://connectrpc.com/docs/protocol)
_CONNECT_FLAG_COMPRESSED = 0x01
_CONNECT_FLAG_END_STREAM = 0x02

# Kimi's DM channel uses a single sentinel sessionId across all WS lifecycles
_DM_SESSION_SENTINEL = "im:kimi:main"

# Chat-id prefix scheme used inside MessageEvent.source.chat_id
_CHATID_DM_PREFIX = "dm:"
_CHATID_ROOM_PREFIX = "room:"

# WS close codes that indicate permanent auth failure
_PERMANENT_WS_CODES = {4001}  # kimi-claw's auth-failed sentinel

# Group-gate header defaults. Kimi's server refuses WebSocket upgrades for
# group-room participation unless these OpenClaw runtime headers meet the
# minimum CalVer (2026.3.13). Defaults here unlock group access; cosmetic
# fields like claw_id auto-generate a unique value per adapter instance.
_GROUP_GATE_DEFAULTS = {
    "claw_version": "0.25.0",
    "openclaw_version": "2026.3.13",
    "openclaw_plugins": "kimi-claw",
    "openclaw_skills": "",
}

_SLASH_COMMAND_RE = re.compile(r"^/[a-z0-9_-]+$", re.IGNORECASE)
_DEFAULT_USER_MESSAGE_PREFIX = "User Message From Kimi:\n"

# Kimi's WS frames are large but finite; 4MB matches the bridge setting.
_WS_MAX_FRAME_SIZE = 4 * 1024 * 1024

# DM outbound chunking — Kimi's UI renders progressively so split long replies.
_DM_CHUNK_SIZE = 3500

# Unary RPC default timeout (seconds).
_RPC_TIMEOUT_S = 30.0

# Reconnect backoff bounds.
_RECONNECT_MIN_S = 2.0
_RECONNECT_MAX_S_DEFAULT = 60.0

# DM application-level keepalive interval. Kimi's server idle-closes the WS
# at ~60s when no ACP frames flow; WS-protocol PING frames do NOT satisfy
# its liveness check (observed code=1006 close at exactly 60s post-connect
# during an idle window). We emit `$/ping` JSON-RPC notifications well under
# the 60s window to reset the server's idle timer. `$/`-prefixed methods are
# the LSP/JSON-RPC convention for implementation-specific notifications and
# MUST be ignored by peers that don't recognize them.
_DM_APP_KEEPALIVE_S_DEFAULT = 25.0

# Dedup ring buffer size — covers Kimi's replay window on Subscribe reconnect.
_DEDUP_MAXLEN = 2000

MAX_MESSAGE_LENGTH = 8000  # Kimi UI handles long messages, but chunking is kinder


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class KimiAdapterError(Exception):
    """Base for all Kimi adapter errors."""


class KimiAuthError(KimiAdapterError):
    """Permanent authentication failure (HTTP 401/403, WS 4001)."""


class KimiTransientError(KimiAdapterError):
    """Transient error — caller should retry with backoff."""


class KimiRpcError(KimiAdapterError):
    """RPC returned a structured error (4xx with grpc-style body)."""


class KimiProtocolError(KimiAdapterError):
    """Malformed wire frame — treat as terminal for this connection."""


# ──────────────────────────────────────────────────────────────────────────────
# Requirements check
# ──────────────────────────────────────────────────────────────────────────────

def check_kimi_requirements() -> bool:
    """Return True if dependencies for the Kimi adapter are available.

    Mirrors other platforms' ``check_*_requirements`` pattern. We require:
    - ``websockets`` (for DM channel)
    - ``aiohttp`` (for group Connect RPC channel)
    Both are already hermes core dependencies; this check exists for
    defensive symmetry with other platforms and to fail fast with a clear
    message if someone strips the runtime.
    """
    if not _WEBSOCKETS_AVAILABLE:
        logger.error("Kimi adapter: websockets package not installed")
        return False
    if not _AIOHTTP_AVAILABLE:
        logger.error("Kimi adapter: aiohttp package not installed")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_standalone_slash_command(text: str) -> bool:
    return bool(_SLASH_COMMAND_RE.match(text.strip()))


def _first_text_block(params: Any) -> Optional[Dict[str, Any]]:
    """Return the first text block dict inside an ACP session/prompt params."""
    if not isinstance(params, dict):
        return None
    prompt = params.get("prompt")
    if not isinstance(prompt, list):
        return None
    for block in prompt:
        if isinstance(block, dict) and block.get("type") == "text":
            return block
    return None


def _extract_user_identity(params: Any) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort extract ``(user_id, user_name)`` from an ACP session/prompt.

    Kimi's wire format for DM user identity isn't publicly documented. We
    probe several plausible shapes without failing if none are present:

      - ``params["sender"]`` / ``params["user"]`` / ``params["author"]``:
        nested dicts with ``id`` / ``userId`` / ``name``
      - ``params["userId"]`` / ``params["user_id"]`` (flat)

    Returns ``(None, None)`` if no identity can be extracted; caller must
    fall back to a session-derived id and log the multi-user collapse
    limitation.
    """
    if not isinstance(params, dict):
        return None, None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    for key in ("sender", "user", "author"):
        obj = params.get(key)
        if isinstance(obj, dict):
            user_id = user_id or (
                obj.get("id") or obj.get("userId") or obj.get("user_id")
            )
            user_name = user_name or (
                obj.get("name") or obj.get("display_name") or obj.get("displayName")
            )
    user_id = user_id or params.get("userId") or params.get("user_id")
    return user_id, user_name


def _split_for_streaming(text: str, chunk_size: int) -> List[str]:
    """Split ``text`` for progressive DM streaming.

    Prefer paragraph boundaries, fall back to line boundaries, fall back to
    hard cuts. Avoids splitting mid-word where possible.
    """
    if len(text) <= chunk_size:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        # Try paragraph break first
        split_at = remaining.rfind("\n\n", 0, chunk_size)
        if split_at < chunk_size // 2:
            # Too early — try single newline
            split_at = remaining.rfind("\n", 0, chunk_size)
        if split_at < chunk_size // 2:
            # Fall back to last space in the window
            split_at = remaining.rfind(" ", 0, chunk_size)
        if split_at < chunk_size // 2:
            split_at = chunk_size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n").lstrip(" ")
    if remaining:
        chunks.append(remaining)
    return chunks


# ──────────────────────────────────────────────────────────────────────────────
# Internal state dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _DMInflight:
    """Tracks one in-flight DM session/prompt awaiting a final end_turn response."""
    kimi_sid: str
    req_id: Any
    started_at: float = field(default_factory=time.time)


@dataclass
class _ChatInfoCache:
    """Cached metadata for one Kimi room (TTL-refreshed on demand)."""
    room_id: str
    name: Optional[str] = None
    members: List[Dict[str, Any]] = field(default_factory=list)
    last_refresh_ts: float = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Adapter
# ──────────────────────────────────────────────────────────────────────────────

class KimiAdapter(BasePlatformAdapter):
    """Platform adapter for Kimi (kimi.com / Moonshot AI).

    Architecture: two concurrent long-lived tasks under one ``connect()``:
      - ``_dm_ws_loop``: maintains ACP WebSocket, synthesises handshake
        responses, dispatches ``session/prompt`` frames as MessageEvents,
        emits replies as ``agent_message_chunk`` updates.
      - ``_group_subscribe_loop``: maintains ``Subscribe {}`` Connect stream,
        translates inbound ChatMessageEvents into MessageEvents, routes
        outbound ``send()`` to ``SendMessage`` unary RPC.

    Both tasks reconnect independently with exponential backoff. A fatal
    auth failure stops only the affected task — DM and groups degrade
    independently.

    Chat-id scheme in MessageEvent.source.chat_id:
      - ``dm:im:kimi:main`` for the single DM channel (Kimi's sentinel
        sessionId)
      - ``room:<uuid>`` for group rooms; threads carried as
        ``SessionSource.thread_id`` per Hermes convention.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform.KIMI)

        # Credentials
        self._bot_token: str = (
            config.token
            or config.extra.get("bot_token", "")
            or os.getenv("KIMI_BOT_TOKEN", "")
        )

        # Endpoints
        self._base_url: str = config.extra.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        self._dm_ws_url: str = config.extra.get("dm_ws_url", _DEFAULT_DM_WS_URL)

        # Channel enable flags
        self._enable_dms: bool = bool(config.extra.get("enable_dms", True))
        self._enable_groups: bool = bool(config.extra.get("enable_groups", True))

        # Group-gate spoof headers
        self._claw_version: str = config.extra.get(
            "claw_version", _GROUP_GATE_DEFAULTS["claw_version"]
        )
        self._openclaw_version: str = config.extra.get(
            "openclaw_version", _GROUP_GATE_DEFAULTS["openclaw_version"]
        )
        self._claw_id: str = config.extra.get("claw_id") or (
            f"hermes-kimi-{uuid.uuid4().hex[:16]}"
        )
        self._openclaw_plugins: str = config.extra.get(
            "openclaw_plugins", _GROUP_GATE_DEFAULTS["openclaw_plugins"]
        )
        self._openclaw_skills: str = config.extra.get(
            "openclaw_skills", _GROUP_GATE_DEFAULTS["openclaw_skills"]
        )

        # Message handling
        self._user_message_prefix: str = config.extra.get(
            "user_message_prefix", _DEFAULT_USER_MESSAGE_PREFIX
        )
        self._disable_prefix: bool = bool(config.extra.get("disable_prefix", False))
        self._auto_skill: Optional[Any] = config.extra.get("auto_skill")
        self._channel_prompt: Optional[str] = config.extra.get("channel_prompt")
        self._group_require_mention: bool = bool(
            config.extra.get("group_require_mention", False)
        )

        # Reconnect tuning
        self._reconnect_max_s: float = float(
            config.extra.get("reconnect_max_s", _RECONNECT_MAX_S_DEFAULT)
        )
        self._ws_ping_interval: int = int(config.extra.get("ws_ping_interval", 15))
        self._ws_ping_timeout: int = int(config.extra.get("ws_ping_timeout", 60))
        self._dm_app_keepalive_s: float = float(
            config.extra.get("dm_app_keepalive_s", _DM_APP_KEEPALIVE_S_DEFAULT)
        )
        self._startup_grace_s: float = float(config.extra.get("startup_grace_s", 30))

        # Runtime state
        self._closing: bool = False
        self._startup_ts: float = 0.0
        self._dm_task: Optional[asyncio.Task] = None
        self._group_task: Optional[asyncio.Task] = None
        self._http_session: Optional[Any] = None  # aiohttp.ClientSession
        self._ws: Optional[Any] = None  # active DM WS

        # Bot identity (populated by GetMe on connect)
        self._me_id: Optional[str] = None
        self._me_short_id: Optional[str] = None
        self._me_name: Optional[str] = None

        # DM state
        # ACP synthetic session id (we generate; Kimi treats it as opaque)
        self._dm_fake_session_id: Optional[str] = None
        # In-flight DM prompts per sid (FIFO). Overlapping prompts are queued
        # so end_turn responses match their originating req_id in order.
        self._dm_inflight: Dict[str, "deque[_DMInflight]"] = {}
        # Kimi's actual sessionId parameter, observed from inbound frames
        self._dm_observed_kimi_sid: Optional[str] = None
        # 409 "bot already connected" strike count — resets on successful connect.
        # First strike sleeps 60s, subsequent strikes 300s to let Kimi's server-
        # side routing clear any ghost WS state from prior thrash cycles.
        self._dm_409_strikes: int = 0
        # One-shot warning guard for the multi-user DM collapse limitation.
        self._warned_dm_collapse: bool = False

        # Dedup of inbound events (keyed by (source_tag, message_id))
        self._processed: deque = deque(maxlen=_DEDUP_MAXLEN)
        self._processed_set: Set[Tuple[str, str]] = set()

        # Per-room cache
        self._rooms: Dict[str, _ChatInfoCache] = {}

    async def connect(self) -> bool:
        """Open HTTP session, fetch bot identity, spawn channel loops.

        Returns ``True`` if at least one enabled channel is viable.
        """
        if not self._bot_token:
            logger.error("Kimi: no bot_token configured (set config.token or KIMI_BOT_TOKEN)")
            return False
        if not check_kimi_requirements():
            return False

        if not self._acquire_platform_lock(
            "kimi-bot-token", self._bot_token, "Kimi bot token"
        ):
            return False

        self._closing = False
        self._startup_ts = time.time()
        self._http_session = aiohttp.ClientSession()

        # Fetch bot identity once — needed to filter self-authored group messages.
        try:
            me = await self._rpc_unary("GetMe", {})
            self._me_id = me.get("id")
            self._me_short_id = me.get("shortId")
            self._me_name = me.get("name")
            logger.info(
                "Kimi: connected as %s (shortId=%s, id=%s)",
                self._me_name, self._me_short_id, self._me_id,
            )
        except KimiAuthError as exc:
            logger.error("Kimi: GetMe auth failed: %s", exc)
            await self._cleanup_http()
            self._release_platform_lock()
            return False
        except Exception as exc:
            logger.warning("Kimi: GetMe failed (%s); continuing — loops will retry", exc)

        if self._enable_dms:
            self._dm_task = asyncio.create_task(self._dm_ws_loop(), name="kimi-dm")
        if self._enable_groups:
            self._group_task = asyncio.create_task(
                self._group_subscribe_loop(), name="kimi-group"
            )

        if not (self._dm_task or self._group_task):
            logger.error("Kimi: both channels disabled — nothing to do")
            await self._cleanup_http()
            self._release_platform_lock()
            return False

        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Cancel both loops, close WS + HTTP session."""
        self._closing = True
        self._mark_disconnected()

        tasks = [t for t in (self._dm_task, self._group_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        await self._cleanup_http()
        self._release_platform_lock()

    async def _cleanup_http(self) -> None:
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None

    # ──────────────────────────────────────────────────────────────────────
    # Public send / platform-surface overrides
    # ──────────────────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Route outbound message by chat_id prefix.

        - ``dm:<sid>``   → emit ACP ``agent_message_chunk`` frames + end_turn
        - ``room:<id>``  → POST ``SendMessage`` (optional thread_id)

        ``metadata`` keys:
          - ``thread_id``: group thread override (also accepted in chat_id as
            ``room:<id>/<thread>``)
          - ``mentions``: list of member short_ids to @-mention
        """
        if not content:
            return SendResult(success=True)

        metadata = metadata or {}
        formatted = self.format_message(content)

        try:
            if chat_id.startswith(_CHATID_DM_PREFIX):
                kimi_sid = chat_id[len(_CHATID_DM_PREFIX):]
                return await self._send_dm(kimi_sid, formatted, reply_to, metadata)
            if chat_id.startswith(_CHATID_ROOM_PREFIX):
                room_and_thread = chat_id[len(_CHATID_ROOM_PREFIX):]
                if "/" in room_and_thread:
                    room_id, thread_id = room_and_thread.split("/", 1)
                else:
                    room_id, thread_id = room_and_thread, metadata.get("thread_id")
                return await self._send_group(
                    room_id, formatted, reply_to, thread_id, metadata
                )
            return SendResult(
                success=False,
                error=f"Kimi: unknown chat_id format: {chat_id!r}",
                retryable=False,
            )
        except KimiAuthError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)
        except (KimiTransientError, asyncio.TimeoutError) as exc:
            return SendResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:
            logger.exception("Kimi: send failed")
            return SendResult(success=False, error=str(exc), retryable=False)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return ``{"name", "type", "chat_id", "members"?}``."""
        if chat_id.startswith(_CHATID_DM_PREFIX):
            return {
                "chat_id": chat_id,
                "name": "Kimi DM",
                "type": "dm",
            }
        if chat_id.startswith(_CHATID_ROOM_PREFIX):
            room_id = chat_id[len(_CHATID_ROOM_PREFIX):].split("/", 1)[0]
            cached = self._rooms.get(room_id)
            if cached is None or (time.time() - cached.last_refresh_ts) > 300:
                try:
                    room = await self._rpc_unary("GetRoom", {"room_id": room_id})
                except KimiRpcError:
                    return {"chat_id": chat_id, "name": room_id, "type": "group"}
                cached = _ChatInfoCache(
                    room_id=room_id,
                    name=room.get("name"),
                    members=room.get("members", []) or [],
                    last_refresh_ts=time.time(),
                )
                self._rooms[room_id] = cached
            return {
                "chat_id": chat_id,
                "name": cached.name or room_id,
                "type": "group",
                "members": cached.members,
            }
        return {"chat_id": chat_id, "name": chat_id, "type": "unknown"}

    def format_message(self, content: str) -> str:
        """Kimi renders markdown natively; pass through unchanged."""
        return content

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Kimi has no native typing indicator RPC. DMs already show a spinner
        from the open ACP session/prompt; groups have no API surface. No-op."""
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Minimal image support — for groups, include URL in SendMessage body.

        Full attachment upload (via Kimi's file upload endpoint) is left for a
        follow-up PR. For now we best-effort include the URL inline.
        """
        text_parts = []
        if caption:
            text_parts.append(caption)
        text_parts.append(image_url)
        return await self.send(chat_id, "\n".join(text_parts))

    # ──────────────────────────────────────────────────────────────────────
    # DM WebSocket loop
    # ──────────────────────────────────────────────────────────────────────

    async def _dm_ws_loop(self) -> None:
        """Maintain the DM ACP WebSocket with exponential reconnect backoff."""
        backoff = _RECONNECT_MIN_S
        while not self._closing:
            rc = await self._dm_ws_connect_once()
            if self._closing or rc == 3:
                # Permanent auth failure — surface it via runtime status so the
                # gateway supervisor can stop retrying and alert the operator.
                if rc == 3 and not self._closing:
                    logger.error("Kimi DM: permanent auth failure, stopping loop")
                    self._set_fatal_error(
                        "kimi_dm_auth",
                        "Kimi DM WebSocket permanent auth failure",
                        retryable=False,
                    )
                return
            if rc == 1:
                # Other terminal error — stop trying but don't claim auth.
                logger.error("Kimi DM: terminal error, stopping loop")
                return
            # rc == 0: transient — back off + retry.
            logger.info("Kimi DM: reconnecting in %.1fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2, self._reconnect_max_s)

    async def _dm_ws_connect_once(self) -> int:
        """One WS connection attempt.

        Return codes:
          0 → transient (retry)
          1 → other terminal error
          3 → permanent auth failure (don't retry)
        """
        headers = self._ws_upgrade_headers()
        logger.info("Kimi DM: dialing %s", self._dm_ws_url)
        try:
            async with websockets.connect(
                self._dm_ws_url,
                additional_headers=headers,
                ping_interval=self._ws_ping_interval,
                ping_timeout=self._ws_ping_timeout,
                max_size=_WS_MAX_FRAME_SIZE,
            ) as ws:
                logger.info("Kimi DM: connected")
                self._ws = ws
                self._dm_fake_session_id = None
                self._dm_observed_kimi_sid = None
                self._dm_inflight.clear()
                # Successful upgrade — clear any accumulated 409 cooldown strikes.
                self._dm_409_strikes = 0
                keepalive_task = asyncio.create_task(
                    self._dm_app_keepalive(ws), name="kimi-dm-keepalive"
                )
                try:
                    async for frame in ws:
                        if isinstance(frame, bytes):
                            logger.debug("Kimi DM: dropping %d-byte binary frame", len(frame))
                            continue
                        try:
                            msg = json.loads(frame)
                        except json.JSONDecodeError:
                            logger.warning("Kimi DM: non-JSON frame: %.200r", frame)
                            continue
                        if isinstance(msg, dict):
                            await self._dm_on_inbound_frame(msg)
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        # Expected — we just cancelled the keepalive. Outer-scope
                        # cancellation will propagate via the next await point.
                        pass
                    except Exception:
                        logger.debug(
                            "Kimi DM: keepalive task ended with exception",
                            exc_info=True,
                        )
                    self._ws = None
            return 0
        except ConnectionClosed as exc:
            code = getattr(exc, "code", None)
            logger.info("Kimi DM: WS closed code=%s reason=%r", code, getattr(exc, "reason", None))
            if code in _PERMANENT_WS_CODES:
                return 3
            return 0
        except asyncio.CancelledError:
            return 1
        except Exception as exc:
            status = (
                getattr(exc, "status_code", None)
                or getattr(getattr(exc, "response", None), "status_code", None)
            )
            if status == 401:
                logger.error("Kimi DM: WS upgrade 401 — bot token rejected")
                return 3
            if status == 403:
                logger.error("Kimi DM: WS upgrade 403 — bot forbidden")
                return 3
            if status == 409:
                # "Bot already connected" — Kimi's single-WS-per-token constraint.
                # Using the default 2s→60s exponential here produces reconnect
                # thrash that Kimi's routing layer can interpret as misbehavior
                # and silently throttle DM delivery to this bot for hours. Hard
                # cooldown so any ghost WS on the server side ages out first.
                self._dm_409_strikes += 1
                cooldown = 60.0 if self._dm_409_strikes == 1 else 300.0
                logger.warning(
                    "Kimi DM: WS upgrade 409 (ghost WS, strike %d) — cooling "
                    "off %.0fs before retry",
                    self._dm_409_strikes, cooldown,
                )
                try:
                    await asyncio.sleep(cooldown)
                except asyncio.CancelledError:
                    return 1
                return 0
            logger.warning("Kimi DM: connection error: %r", exc)
            return 0

    async def _dm_app_keepalive(self, ws: Any) -> None:
        """Emit `$/ping` JSON-RPC notifications to prevent Kimi's 60s idle close.

        Kimi's server idle-closes the WebSocket at ~60 seconds when no
        application-level ACP frames flow; WS-protocol PING frames (handled
        automatically by the `websockets` library) do NOT satisfy its
        liveness check. This was confirmed by observing code=1006 closes at
        exactly 60s post-connect during idle windows (no user messages, no
        outbound session/update notifications).

        `$/`-prefixed methods are the LSP / JSON-RPC convention for
        implementation-specific notifications that peers MUST silently
        ignore when unrecognized, so this is safe for any ACP-aware
        counterparty.
        """
        try:
            while True:
                await asyncio.sleep(self._dm_app_keepalive_s)
                try:
                    await ws.send(json.dumps(
                        {"jsonrpc": "2.0", "method": "$/ping", "params": {}},
                        separators=(",", ":"),
                    ))
                    logger.debug("Kimi DM: $/ping keepalive sent")
                except ConnectionClosed:
                    return
        except asyncio.CancelledError:
            return

    def _ws_upgrade_headers(self) -> Dict[str, str]:
        """Build headers for the DM WS upgrade, including group-gate spoof."""
        headers = {"X-Kimi-Bot-Token": self._bot_token}
        if self._claw_version:
            headers["X-Kimi-Claw-Version"] = self._claw_version
        if self._openclaw_version:
            headers["X-Kimi-OpenClaw-Version"] = self._openclaw_version
        if self._claw_id:
            headers["X-Kimi-Claw-ID"] = self._claw_id
        if self._openclaw_plugins:
            headers["X-Kimi-OpenClaw-Plugins"] = self._openclaw_plugins
        if self._openclaw_skills:
            headers["X-Kimi-OpenClaw-Skills"] = self._openclaw_skills
        return headers

    async def _dm_on_inbound_frame(self, msg: Dict[str, Any]) -> None:
        """Dispatch one ACP JSON-RPC frame from Kimi.

        Kimi's client sends:
          - ``initialize``          → respond with agent info
          - ``session/new``         → respond with a synthetic sessionId
          - ``session/prompt``      → convert to MessageEvent, dispatch
          - ``session/cancel``      → cancel in-flight reply (best-effort)
        """
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        # Observe Kimi's sessionId unconditionally — needed for outbound rewrites.
        sid = params.get("sessionId") if isinstance(params, dict) else None
        if isinstance(sid, str) and sid != self._dm_observed_kimi_sid:
            self._dm_observed_kimi_sid = sid
            logger.info("Kimi DM: observed sessionId=%s", sid)

        if method == "initialize":
            await self._dm_respond(req_id, {
                "protocolVersion": 1,
                "agentInfo": {"name": "hermes-agent", "version": "1.0"},
            })
            return

        if method == "session/new":
            self._dm_fake_session_id = str(uuid.uuid4())
            await self._dm_respond(req_id, {"sessionId": self._dm_fake_session_id})
            logger.info("Kimi DM: created synthetic session %s", self._dm_fake_session_id)
            return

        if method == "session/cancel":
            logger.info("Kimi DM: session/cancel for sid=%s", sid)
            # There's no in-flight cancel API in the gateway yet — log and ack.
            if req_id is not None:
                await self._dm_respond(req_id, None)
            return

        if method == "session/prompt":
            await self._dm_handle_prompt(msg)
            return

        # Unknown methods with ids: reply with method-not-found so the peer
        # doesn't hang. Notifications (no id): silently ignore.
        if req_id is not None:
            await self._dm_respond_error(
                req_id,
                code=-32601,
                message=f"method not found: {method}",
            )
        else:
            logger.debug("Kimi DM: ignoring notification method=%s", method)

    async def _dm_handle_prompt(self, msg: Dict[str, Any]) -> None:
        """Convert a session/prompt into a MessageEvent and dispatch."""
        req_id = msg.get("id")
        params = msg.get("params") or {}
        text_block = _first_text_block(params)
        if text_block is None:
            logger.warning("Kimi DM: session/prompt with no text block")
            if req_id is not None:
                await self._dm_respond(req_id, {"stopReason": "end_turn"})
            return

        text = text_block.get("text") if isinstance(text_block, dict) else ""
        if not isinstance(text, str):
            text = ""

        # Apply user-message prefix unless it's a standalone slash command.
        if (
            not self._disable_prefix
            and text
            and not _is_standalone_slash_command(text)
        ):
            text = self._user_message_prefix + text

        kimi_sid = self._dm_observed_kimi_sid or _DM_SESSION_SENTINEL
        chat_id = f"{_CHATID_DM_PREFIX}{kimi_sid}"
        message_id = str(req_id) if req_id is not None else f"dm-{uuid.uuid4().hex[:12]}"

        # Queue req_id so overlapping prompts get FIFO end_turn responses
        # instead of clobbering a previous in-flight (which would leave the
        # original prompt's Kimi UI spinner hanging forever).
        self._dm_inflight.setdefault(kimi_sid, deque()).append(
            _DMInflight(kimi_sid=kimi_sid, req_id=req_id)
        )

        # Best-effort extract a per-user identity so multi-user bots route
        # DMs to per-user sessions rather than collapsing everyone into one.
        # Kimi's wire format isn't publicly documented; we fall back to
        # sessionId-derived identity + a one-shot warning log if nothing was
        # provided.
        user_id, user_name = _extract_user_identity(params)
        if not user_id:
            if not self._warned_dm_collapse:
                logger.warning(
                    "Kimi DM: session/prompt carries no user identity — "
                    "multi-user bots on this adapter will collapse all DM "
                    "users into a single Hermes session. Sending will still "
                    "work; session state won't be isolated per user."
                )
                self._warned_dm_collapse = True
            user_id = f"kimi:dm:{kimi_sid}"

        event = self._build_message_event(
            kind="dm",
            text=text,
            message_id=message_id,
            chat_id=chat_id,
            chat_name="Kimi DM",
            user_id=user_id,
            user_name=user_name,
            raw=msg,
        )
        await self.handle_message(event)

    async def _dm_respond(self, req_id: Any, result: Any) -> None:
        """Send a JSON-RPC result back over the DM WS."""
        if self._ws is None or req_id is None:
            return
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except ConnectionClosed:
            logger.info("Kimi DM: WS closed while sending response")

    async def _dm_respond_error(self, req_id: Any, code: int, message: str) -> None:
        if self._ws is None or req_id is None:
            return
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except ConnectionClosed:
            pass

    async def _dm_emit_chunk(self, kimi_sid: str, text: str) -> None:
        """Emit one ACP ``agent_message_chunk`` update over the WS."""
        if self._ws is None:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": kimi_sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except ConnectionClosed:
            logger.info("Kimi DM: WS closed while emitting chunk")

    async def _send_dm(
        self,
        kimi_sid: str,
        content: str,
        reply_to: Optional[str],
        metadata: Dict[str, Any],
    ) -> SendResult:
        """Emit streamed ``agent_message_chunk`` frames + end_turn response.

        ``reply_to`` is not used on the DM channel (Kimi's ACP doesn't expose
        reply-to semantics for DMs — the UI threads all responses to the
        in-flight prompt). Ignored gracefully.
        """
        del reply_to, metadata  # unused on DM path
        if self._ws is None:
            return SendResult(success=False, error="Kimi DM: WS not connected", retryable=True)

        chunks = _split_for_streaming(content, _DM_CHUNK_SIZE)
        for chunk in chunks:
            await self._dm_emit_chunk(kimi_sid, chunk)

        # Pop the oldest in-flight prompt for this sid (FIFO) and close its
        # round-trip. If the queue empties, drop the mapping.
        queue = self._dm_inflight.get(kimi_sid)
        inflight: Optional[_DMInflight] = None
        if queue:
            inflight = queue.popleft()
            if not queue:
                self._dm_inflight.pop(kimi_sid, None)
        if inflight is not None and inflight.req_id is not None:
            await self._dm_respond(inflight.req_id, {"stopReason": "end_turn"})

        return SendResult(
            success=True,
            message_id=f"dm-{uuid.uuid4().hex[:12]}",
        )

    # ──────────────────────────────────────────────────────────────────────
    # Group Subscribe loop
    # ──────────────────────────────────────────────────────────────────────

    async def _group_subscribe_loop(self) -> None:
        """Maintain the global ``Subscribe`` stream with reconnect backoff."""
        backoff = _RECONNECT_MIN_S
        while not self._closing:
            rc = await self._group_subscribe_once()
            if self._closing or rc == 3:
                if rc == 3 and not self._closing:
                    logger.error("Kimi groups: permanent auth failure, stopping loop")
                    self._set_fatal_error(
                        "kimi_groups_auth",
                        "Kimi Subscribe stream permanent auth failure",
                        retryable=False,
                    )
                return
            if rc == 1:
                logger.error("Kimi groups: terminal error, stopping loop")
                return
            logger.info("Kimi groups: reconnecting in %.1fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2, self._reconnect_max_s)

    async def _group_subscribe_once(self) -> int:
        """One Subscribe stream session.

        Return codes match ``_dm_ws_connect_once``.
        """
        url = f"{self._base_url}/{_IM_SERVICE}/Subscribe"
        headers = self._http_headers(streaming=True)
        body = self._encode_envelope(b"{}")

        assert self._http_session is not None
        try:
            async with self._http_session.post(
                url,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    logger.error("Kimi groups: Subscribe auth failure HTTP %s", resp.status)
                    return 3
                if resp.status != 200:
                    logger.warning("Kimi groups: Subscribe HTTP %s", resp.status)
                    return 0
                try:
                    async for event in self._connect_envelope_parser(resp.content):
                        await self._on_group_event(event)
                except KimiAuthError as exc:
                    logger.error("Kimi groups: %s", exc)
                    return 3
                except (KimiTransientError, KimiProtocolError, KimiRpcError) as exc:
                    logger.warning("Kimi groups: stream error: %s", exc)
                    return 0
            return 0
        except asyncio.CancelledError:
            return 1
        except aiohttp.ClientError as exc:
            logger.warning("Kimi groups: HTTP client error: %r", exc)
            return 0
        except Exception:
            logger.exception("Kimi groups: unexpected error in Subscribe")
            return 0

    async def _on_group_event(self, event: Dict[str, Any]) -> None:
        """Handle one decoded envelope from the Subscribe firehose.

        Events observed from the wire:
          - ``{"ping": {}}``   — keepalive, ignored
          - ``{"message": {...}}`` or top-level message fields — real event

        Schema for real events (best-effort extracted; TYPE_STRING fields
        from buf.validate introspection):
          ``{
            "chat_id":   "<room-uuid>",
            "message_id": "<uuid>",
            "thread_id":  "<uuid>"?,
            "text":       "...",
            "sender": {
              "id":        "<uuid>",
              "short_id":  "<short>",
              "name":      "..."
            },
            "sent_at":    "<ISO8601>"?,
            "mentions":   [...]?,
            "reply_to":   {"message_id": "...", "text": "..."}?,
            "attachments": [{"url", "type", "name"}]?
          }``

        We tolerate schema drift — missing fields just drop details silently.
        """
        if not isinstance(event, dict):
            return
        if "ping" in event and len(event) == 1:
            logger.debug("Kimi groups: keepalive ping")
            return

        # Real events may be wrapped as {"message": {...}} or bare — accept both.
        msg = event.get("message") if isinstance(event.get("message"), dict) else event

        chat_id = msg.get("chat_id") or msg.get("chatId")
        message_id = msg.get("message_id") or msg.get("messageId")
        if not (chat_id and message_id):
            logger.debug("Kimi groups: event missing chat_id/message_id, skipping: %.200r", msg)
            return

        sender = msg.get("sender") or {}
        sender_id = sender.get("id") if isinstance(sender, dict) else None

        # Self-message filter.
        if sender_id and self._me_id and sender_id == self._me_id:
            return

        # Dedup (chat_id, message_id) — Kimi replays recent history on reconnect.
        if self._dedup_is_duplicate("group", chat_id, message_id):
            return

        # Startup grace — ignore events older than startup_ts - grace.
        sent_at = msg.get("sent_at") or msg.get("sentAt")
        if sent_at:
            event_ts = _parse_iso8601(sent_at)
            if event_ts and event_ts < (self._startup_ts - self._startup_grace_s):
                logger.debug("Kimi groups: skipping stale event %s (sent_at=%s)", message_id, sent_at)
                return

        text = msg.get("text") or ""
        thread_id = msg.get("thread_id") or msg.get("threadId")
        reply_to = msg.get("reply_to") or msg.get("replyTo") or {}
        reply_to_message_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
        reply_to_text = reply_to.get("text") if isinstance(reply_to, dict) else None

        attachments = msg.get("attachments") or []
        media_urls: List[str] = []
        media_types: List[str] = []
        for att in attachments if isinstance(attachments, list) else []:
            if not isinstance(att, dict):
                continue
            url = att.get("url")
            if url:
                media_urls.append(url)
                media_types.append(att.get("type", "file"))

        # Mention gate: if configured to require mentions and the message
        # doesn't reference us, ignore. Supports both numeric id and short_id.
        mentions = msg.get("mentions") or []
        if self._group_require_mention:
            mentioned_us = False
            for m in mentions if isinstance(mentions, list) else []:
                mid = m.get("id") if isinstance(m, dict) else m
                if mid in (self._me_id, self._me_short_id):
                    mentioned_us = True
                    break
            if not mentioned_us:
                logger.debug("Kimi groups: ignoring non-mention in room %s", chat_id)
                return

        chat_id_prefixed = f"{_CHATID_ROOM_PREFIX}{chat_id}"

        event_obj = self._build_message_event(
            kind="group",
            text=text,
            message_id=str(message_id),
            chat_id=chat_id_prefixed,
            chat_name=None,  # populated lazily via get_chat_info if needed
            user_id=sender.get("id") if isinstance(sender, dict) else None,
            user_name=sender.get("name") if isinstance(sender, dict) else None,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            media_urls=media_urls,
            media_types=media_types,
            raw=msg,
        )
        await self.handle_message(event_obj)

    async def _send_group(
        self,
        room_id: str,
        content: str,
        reply_to: Optional[str],
        thread_id: Optional[str],
        metadata: Dict[str, Any],
    ) -> SendResult:
        """POST unary ``SendMessage`` with text + optional attachments."""
        body: Dict[str, Any] = {"chat_id": room_id, "text": content}
        if thread_id:
            body["thread_id"] = thread_id
        if reply_to:
            body["reply_to_message_id"] = reply_to
        if "mentions" in metadata:
            body["mentions"] = metadata["mentions"]
        if "attachments" in metadata:
            body["attachments"] = metadata["attachments"]

        resp = await self._rpc_unary("SendMessage", body)
        return SendResult(
            success=True,
            message_id=resp.get("message_id") or resp.get("messageId"),
            raw_response=resp,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Connect RPC — unary + envelope streaming
    # ──────────────────────────────────────────────────────────────────────

    async def _rpc_unary(
        self,
        method: str,
        body: Dict[str, Any],
        *,
        timeout_s: float = _RPC_TIMEOUT_S,
    ) -> Dict[str, Any]:
        """POST ``/api-ws/{service}/{method}`` with JSON body, return response.

        Raises:
            KimiAuthError     on HTTP 401/403.
            KimiTransientError on HTTP 429/5xx/network errors.
            KimiRpcError      on other 4xx with JSON error body.
        """
        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        url = f"{self._base_url}/{_IM_SERVICE}/{method}"
        headers = self._http_headers(streaming=False)
        try:
            async with self._http_session.post(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                raw = await resp.read()
                if resp.status in (401, 403):
                    raise KimiAuthError(
                        f"{method}: auth failed (HTTP {resp.status}): {raw[:200]!r}"
                    )
                if resp.status == 429 or 500 <= resp.status < 600:
                    raise KimiTransientError(
                        f"{method}: HTTP {resp.status}: {raw[:200]!r}"
                    )
                if resp.status >= 400:
                    err_msg = raw.decode("utf-8", errors="replace")[:500]
                    raise KimiRpcError(f"{method}: HTTP {resp.status}: {err_msg}")
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise KimiProtocolError(
                        f"{method}: invalid JSON response: {exc}"
                    )
        except aiohttp.ClientError as exc:
            raise KimiTransientError(f"{method}: network error: {exc}")

    def _http_headers(self, *, streaming: bool) -> Dict[str, str]:
        """Shared HTTP headers for all Connect RPCs."""
        return {
            "Content-Type": (
                "application/connect+json" if streaming else "application/json"
            ),
            "Connect-Protocol-Version": "1",
            "X-Kimi-Bot-Token": self._bot_token,
            "Accept-Encoding": "identity",
            "User-Agent": "hermes-kimi-adapter/1.0",
        }

    def _encode_envelope(self, payload: bytes, *, end_stream: bool = False) -> bytes:
        """Encode one outbound Connect envelope: ``[flag:1B][len:4B BE][body]``."""
        flag = _CONNECT_FLAG_END_STREAM if end_stream else 0
        return bytes([flag]) + struct.pack(">I", len(payload)) + payload

    async def _connect_envelope_parser(
        self,
        reader: Any,  # aiohttp.StreamReader
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield JSON dicts from a chunked Connect streaming body.

        Handles:
          - partial envelopes (readexactly blocks correctly)
          - compressed flag bit (rejected — we negotiate uncompressed)
          - end-stream frame with optional ``error`` payload (raises appropriate
            KimiAuthError / KimiRpcError, or returns cleanly)
        """
        while True:
            try:
                header = await reader.readexactly(5)
            except asyncio.IncompleteReadError:
                # Stream closed mid-envelope — treat as transient.
                raise KimiTransientError("Subscribe stream closed mid-envelope")
            except aiohttp.ClientPayloadError as exc:
                raise KimiTransientError(f"Subscribe payload error: {exc}")

            flag = header[0]
            length = struct.unpack(">I", header[1:5])[0]
            # Defensive cap: the length prefix is 4 bytes big-endian (up to
            # 4 GB). An unbounded readexactly here would OOM on a hostile or
            # buggy upstream. Mirror the WS max-frame cap.
            if length > _WS_MAX_FRAME_SIZE:
                raise KimiProtocolError(
                    f"envelope length {length} exceeds max frame size "
                    f"{_WS_MAX_FRAME_SIZE}"
                )
            payload = b""
            if length:
                try:
                    payload = await reader.readexactly(length)
                except asyncio.IncompleteReadError:
                    raise KimiTransientError("Subscribe truncated envelope body")
                except aiohttp.ClientPayloadError as exc:
                    raise KimiTransientError(f"Subscribe payload error: {exc}")

            if flag & _CONNECT_FLAG_COMPRESSED:
                raise KimiProtocolError("Kimi sent compressed frame (unsupported)")

            try:
                msg = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KimiProtocolError(f"malformed envelope JSON: {exc}")

            if flag & _CONNECT_FLAG_END_STREAM:
                err = msg.get("error") if isinstance(msg, dict) else None
                if err:
                    code = err.get("code", "unknown")
                    message = err.get("message") or err.get("details") or ""
                    if code in ("unauthenticated", "permission_denied"):
                        raise KimiAuthError(f"{code}: {message}")
                    raise KimiRpcError(f"{code}: {message}")
                return  # clean end-of-stream

            if isinstance(msg, dict):
                yield msg

    # ──────────────────────────────────────────────────────────────────────
    # MessageEvent synthesis + dedup
    # ──────────────────────────────────────────────────────────────────────

    def _build_message_event(
        self,
        *,
        kind: str,  # "dm" | "group"
        text: str,
        message_id: str,
        chat_id: str,
        chat_name: Optional[str],
        user_id: Optional[str],
        user_name: Optional[str],
        thread_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        reply_to_text: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
        raw: Any = None,
    ) -> MessageEvent:
        msg_type = MessageType.TEXT
        if text.strip().startswith("/"):
            msg_type = MessageType.COMMAND
        if media_urls:
            # Best-effort mapping from MIME prefix to MessageType.
            first = (media_types or [""])[0].lower()
            if first.startswith("image"):
                msg_type = MessageType.PHOTO
            elif first.startswith("video"):
                msg_type = MessageType.VIDEO
            elif first.startswith("audio"):
                msg_type = MessageType.AUDIO
            else:
                msg_type = MessageType.DOCUMENT

        return MessageEvent(
            text=text,
            message_type=msg_type,
            source=self.build_source(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=("dm" if kind == "dm" else "group"),
                user_id=user_id,
                user_name=user_name,
                thread_id=thread_id,
            ),
            raw_message=raw,
            message_id=message_id,
            media_urls=list(media_urls or []),
            media_types=list(media_types or []),
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
            auto_skill=self._auto_skill,
            channel_prompt=self._channel_prompt,
            internal=False,
        )

    def _dedup_is_duplicate(self, kind: str, chat_id: str, message_id: str) -> bool:
        key = (f"{kind}:{chat_id}", str(message_id))
        if key in self._processed_set:
            return True
        # Evict oldest when full.
        if len(self._processed) >= self._processed.maxlen:
            old = self._processed[0]
            self._processed_set.discard(old)
        self._processed.append(key)
        self._processed_set.add(key)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Module helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_iso8601(text: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp to unix-seconds, or None on failure."""
    if not isinstance(text, str) or not text:
        return None
    try:
        from datetime import datetime
        # Normalize 'Z' suffix to +00:00 for fromisoformat.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Standalone send helper (for send_message_tool / cron paths outside gateway)
# ──────────────────────────────────────────────────────────────────────────────

async def send_kimi_message(
    config: PlatformConfig,
    chat_id: str,
    text: str,
    *,
    thread_id: Optional[str] = None,
) -> SendResult:
    """Send a message via Kimi without instantiating the full adapter.

    Used by cron delivery and ``send_message_tool`` when a live gateway
    adapter isn't available. Supports only group rooms (``room:<id>``) for
    now — DM sends require an active WS session.
    """
    if not chat_id.startswith(_CHATID_ROOM_PREFIX):
        return SendResult(
            success=False,
            error="Kimi: standalone send supports only group rooms",
            retryable=False,
        )
    token = config.token or config.extra.get("bot_token") or os.getenv("KIMI_BOT_TOKEN", "")
    if not token:
        return SendResult(success=False, error="Kimi: no bot_token configured", retryable=False)
    base_url = config.extra.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
    room_and_thread = chat_id[len(_CHATID_ROOM_PREFIX):]
    if "/" in room_and_thread:
        room_id, inline_thread = room_and_thread.split("/", 1)
    else:
        room_id, inline_thread = room_and_thread, None
    body: Dict[str, Any] = {"chat_id": room_id, "text": text}
    if thread_id or inline_thread:
        body["thread_id"] = thread_id or inline_thread
    url = f"{base_url}/{_IM_SERVICE}/SendMessage"
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "X-Kimi-Bot-Token": token,
        "User-Agent": "hermes-kimi-adapter/1.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_RPC_TIMEOUT_S),
            ) as resp:
                raw = await resp.read()
                if resp.status == 401 or resp.status == 403:
                    return SendResult(success=False, error=f"auth failed HTTP {resp.status}", retryable=False)
                if resp.status >= 400:
                    return SendResult(success=False, error=f"HTTP {resp.status}: {raw[:200]!r}", retryable=(resp.status >= 500))
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return SendResult(success=False, error=f"bad JSON: {exc}", retryable=False)
                return SendResult(
                    success=True,
                    message_id=data.get("message_id") or data.get("messageId"),
                    raw_response=data,
                )
    except aiohttp.ClientError as exc:
        return SendResult(success=False, error=f"network error: {exc}", retryable=True)
