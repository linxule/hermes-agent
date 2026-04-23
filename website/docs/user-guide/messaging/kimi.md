# Kimi (Moonshot AI)

Connect Hermes to [Kimi](https://www.kimi.com/) (Moonshot AI's chat platform) using Kimi's bot protocol — supporting 1:1 DMs on Kimi web/mobile and group rooms under one bot identity.

## Overview

The Kimi adapter bridges two channels with a single `X-Kimi-Bot-Token` credential:

- **DM channel** (`wss://www.kimi.com/api-claw/bots/agent-ws`) — Kimi speaks [Zed Agent Client Protocol (ACP)](https://github.com/zed-industries/agent-client-protocol) JSON-RPC over WebSocket. Hermes responds to ACP handshake frames locally and converts `session/prompt` frames into the normal gateway pipeline. DM replies stream progressively via `agent_message_chunk` updates.
- **Group channel** (`https://www.kimi.com/api-ws/`) — Kimi exposes a [ConnectRPC](https://connectrpc.com/) surface for group IM (`kimi.gateway.im.v1.IMService`). Hermes opens a bidi-stream `Subscribe` call (all-rooms firehose) and posts outbound replies via unary `SendMessage`. Connect protocol's native JSON codec is used throughout — no protobuf bindings required.

Group-room participation requires OpenClaw-compatible runtime metadata headers on the Kimi IM RPC path (and the DM WebSocket path when Kimi routes group prompts through ACP). Default values in the adapter match the current Kimi Claw gate and can be overridden from config.

## Prerequisites

1. **Kimi account** — Sign in at [kimi.com](https://www.kimi.com/) (web or mobile).
2. **Bot token** — In Kimi's web interface, click **"Link existing OpenClaw"** and follow the flow. Kimi returns a token of shape `km_b_prod_...`. Copy it; you won't see it again. Token auth is at WebSocket upgrade and on every HTTP request.
3. **Dependencies** — The adapter requires `websockets` and `aiohttp` (both hermes core deps; no extra install needed). Hermes does not install or shell out to Kimi's `kimiim-cli`; the native adapter implements the required IM RPCs directly.

## Configuration

### Interactive setup

```bash
hermes gateway setup
```
Select **Kimi Claw** when prompted and paste your bot token.

### Manual configuration

Set in your `.env`:

```bash
KIMI_BOT_TOKEN=km_b_prod_...
# Optional
KIMI_ALLOWED_USERS=<your-kimi-user-id>     # Recommended for shared/public deployments
KIMI_GROUP_ALLOWED_USERS=room-uuid         # Optional room allowlist for group traffic
# KIMI_ALLOW_ALL_USERS=true                # Personal one-user bots only; skips allowlist
KIMI_HOME_CHANNEL=room:<uuid>              # Default target for cron deliveries
KIMI_ENABLE_GROUPS=true                    # Default: true
KIMI_ENABLE_DMS=true                       # Default: true
```

Or in `config.yaml`:

```yaml
platforms:
  kimi:
    enabled: true
    token: ${KIMI_BOT_TOKEN}
    extra:
      enable_dms: true
      enable_groups: true
      # Behaviour tuning
      user_message_prefix: "User Message From Kimi:\n"
      group_require_mention: false        # If true, only respond when @-mentioned
      # Sender-identity allowlist — bypasses the role-content filter for these
      # senders. Accepts short_ids (e.g. "u_bot") and/or Kimi user ids.
      # group_trusted_senders:
      #   - u_conductor
      # Policy for non-user-role senders (assistants, bots, systems):
      #   "off"           — drop all non-user-role messages (default; matches pre-existing behavior)
      #   "trusted_only"  — dispatch only if sender is in group_trusted_senders
      #   "mentions"      — dispatch only if sender @-mentions us (EXPERIMENTAL — see Authorization model)
      #   "all"           — dispatch all non-user-role messages (treat identically to USER role)
      # group_allow_bot_senders: off
      # Reconnect tuning
      ws_ping_interval: 15
      ws_ping_timeout: 60
      reconnect_max_s: 60
      # File upload/download tuning
      kimiapi_host: "https://www.kimi.com/api-claw"
      file_timeout_s: 120
      # file_download_dir: "/path/to/cache"  # Defaults under Hermes cache
      # Group-gate OpenClaw runtime spoof (defaults unlock groups)
      openclaw_version: "2026.3.13"       # Minimum gate version
      claw_version: "0.25.0"
      openclaw_plugins:
        - id: "kimi-claw"
          version: "0.25.0"
```

### Authorization model

Kimi authorization runs in two layers. The first gates the **user identity** (can this user talk to the bot at all); the second gates the **message content** (within an authorized room, which messages get dispatched to the agent).

**User allowlist (env vars, existing):**

- `KIMI_ALLOWED_USERS` — DM + group user allowlist (comma-separated user ids / short ids / `kimi:<id>` forms).
- `KIMI_GROUP_ALLOWED_USERS` — additional allowlist scoped to group rooms; accepts raw chat-uuids or prefixed `room:<uuid>`.
- `KIMI_ALLOW_ALL_USERS=true` — skips the allowlist entirely. Appropriate only for a personal one-user bot.

These run identically to Telegram/Discord authorization — unauthorized users are rejected before any adapter-specific processing.

**Sender / role policy (adapter extras, new):**

Within an authorized room, group events go through a role-based content filter: messages classified as `USER` role are dispatched, and `ASSISTANT` / `BOT` / `SYSTEM` messages default to silent drops. Two extras relax this gate for legitimate non-user-role senders (orchestrator bots, conductor personas, AI-drafted human messages):

- `group_trusted_senders` — a list of short_ids and/or Kimi user ids. Any sender in this list bypasses the role-content filter. This is the authoritative sender-identity gate — use it whenever you know the concrete identity of a bot you want to hear from.
- `group_allow_bot_senders` — policy for non-user-role senders that aren't in `group_trusted_senders`:
  - `"off"` (default) — drop all non-user-role messages. Matches pre-existing behavior.
  - `"trusted_only"` — drop unless sender is in `group_trusted_senders` (drops silently, logs at INFO).
  - `"mentions"` — dispatch only if the message @-mentions this bot. **EXPERIMENTAL.** Kimi's mention metadata may be client-provided rather than server-enriched (unverified as of this commit). Until the spoofability probe runs, a malicious sender could forge `mentions: [{short_id: <us>}]` to bypass this gate. For production authorization of bot senders, prefer `trusted_only` with an explicit allowlist. `"mentions"` is most useful on short-lived experimental deployments where the participant set is already controlled out-of-band.
  - `"all"` — dispatch every non-user-role message (treat identically to `USER` role). Equivalent to disabling the role filter entirely for bot senders.

Self-filter (drop messages this bot sent itself) still runs before either gate, so enabling any of these does NOT cause the bot to loop on its own replies.

Filter decisions that drop a message now log at **INFO** level, so operators can see gate behavior in normal logs without dialing up to DEBUG. Raw event dumps, keepalive pings, and content-shape diagnostics remain at DEBUG.

**Role-as-content vs sender-as-identity.** The `role` field classifies message CONTENT (USER/ASSISTANT/SYSTEM per OpenAI chat semantics), not sender IDENTITY. A human using AI-drafting tools can emit `role=ASSISTANT`, and a bot can emit `role=USER`. Use `group_trusted_senders` for authoritative gating; treat `role` as a best-effort content signal only.

## Chat-id format

Kimi exposes two distinct address spaces that the adapter normalises via a prefix scheme:

| Chat-id                         | Meaning                                          | Transport                |
|---------------------------------|--------------------------------------------------|--------------------------|
| `dm:im:kimi:main`               | The single 1:1 DM channel with this bot          | ACP WebSocket            |
| `room:<uuid>`                   | A group room                                     | Connect `SendMessage`    |
| `room:<uuid>/<thread-uuid>`     | Compatibility form accepted by Hermes           | Connect `SendMessage`    |

The `dm:` / `room:` prefix is what `send_message_tool`, cron delivery, and any adapter-aware tool consume — just pass the full prefixed id.

Kimi Claw v0.25.0's `SendMessageRequest` contains only `chatId` and `blocks`; there is no outbound `thread_id` field. Hermes accepts the thread-suffixed form for routing compatibility, but outbound group sends target the underlying room/chat id.

## Behaviour

### DM channel
- Opens and maintains one WebSocket per session.
- Handles ACP handshake (`initialize`, `session/new`) synthetically — no subprocess required.
- User prompts arrive as `session/prompt` frames, convert to `MessageEvent` with `source.chat_type="dm"`, and dispatch through the normal gateway pipeline (session routing, skills, model selection).
- Replies stream back as `agent_message_chunk` updates, chunked on paragraph/line boundaries for a fluid UX.
- A final `end_turn` response closes each round-trip; if omitted, Kimi's UI would spin forever.

### Group channel
- Opens one long-lived `Subscribe` call — all-rooms firehose.
- Server pushes `ChatMessageEvent` payloads; adapter self-filters (own messages, already-seen message_ids).
- Some Subscribe events carry only lightweight summary metadata. Hermes hydrates missing text with `ListMessages` before dispatching into the gateway.
- Dedup by `(chat_id, message_id)` is load-bearing — Kimi may replay recent history on reconnect.
- Outbound replies post unary `SendMessage` with `chatId` and text/resource-link blocks.
- Local files sent to group rooms are uploaded to Kimi's `/files:upload` endpoint and delivered as `kimi-file://` resource links.
- Inbound `kimi-file://` blocks are resolved through Kimi's file metadata endpoint and downloaded into Hermes' local Kimi file cache before entering the agent media pipeline.
- Optional `group_require_mention` mode only responds when the bot is @-mentioned.

## Group-room gating

To participate in group rooms, Kimi requires certain OpenClaw runtime metadata headers on IM RPC requests (minimum OpenClaw CalVer `2026.3.13`). The adapter sends these by default: `X-Kimi-Claw-Version`, `X-Kimi-OpenClaw-Version`, `X-Kimi-OpenClaw-Plugins`, `X-Kimi-OpenClaw-Skills`, and `X-Kimi-Claw-ID`. Override or omit them via `config.extra.openclaw_version`, `claw_version`, `claw_id`, `openclaw_plugins`, `openclaw_skills`.

## Kimi IM tool

When the `hermes-kimi` toolset is active, Hermes exposes a `kimi_im` tool for group-room operations that mirror the useful parts of Kimi's own `kimiim-cli` workflow:

- `me` — inspect the current Kimi identity.
- `get_group` — fetch room metadata.
- `list_members` — inspect room participants.
- `list_messages` — fetch recent or anchored group messages.
- `list_files` — list files shared in a room.
- `send_message` — send a short group message.

The tool uses the same native adapter and `KIMI_BOT_TOKEN`; it does not install Kimi's external scripts or write into `~/.openclaw`.

## Troubleshooting

**DM WebSocket closes with code 4001.** The bot token was revoked or rotated. Regenerate via Kimi's "Link existing OpenClaw" flow and restart the gateway.

**DM WebSocket upgrade returns HTTP 409 ("bot already connected").** Kimi enforces a single live WebSocket per bot token. If a previous gateway instance didn't cleanly close its socket (crash, kill -9, network disconnect mid-session), Kimi's server may briefly hold a "ghost" WS and reject new upgrades with 409. The adapter handles this by backing off for 60s on the first strike and 300s on subsequent strikes, rather than retrying the default 2s → 60s exponential — rapid reconnect attempts after a 409 can cause Kimi's routing layer to silently throttle inbound DM delivery to the bot for an extended period. Wait for the cooldown; don't manually restart the gateway during it. A successful connect resets the strike counter.

**DM WebSocket upgrade returns HTTP 403.** The bot token is valid but the account lacks permission to open the bot WebSocket surface. Treated as permanent — the adapter stops retrying DMs and surfaces a fatal status. Check your Kimi account's bot permissions.

**Group messages never arrive even though the bot is in the room.** The `X-Kimi-OpenClaw-Version` header didn't meet the minimum gate. Confirm `openclaw_version: "2026.3.13"` (or newer) is active in your config, and that `X-Kimi-OpenClaw-*` appear in the adapter's IM RPC headers.

**Subscribe stream keeps reconnecting.** Check Kimi's status — connect+json streaming uses HTTP/1.1 chunked transfer, so intermediaries that buffer aggressively can break long streams. Adjust `reconnect_max_s` to avoid backing off too aggressively.

**Tool-call output clutters Kimi DM UI.** Set `config.extra.user_message_prefix` to a shorter prefix, or configure skills to minimise tool chatter in user-facing summaries.

## Known limitations

**Multi-user DM session collapse.** Kimi's ACP `session/prompt.params` carries only `sessionId` and `prompt` by design — 1:1 DMs treat the sender as implicit, and `sessionId` itself is a single sentinel (`im:kimi:main`) so it can't be used to disambiguate users. kimi-claw's own client handles this by injecting a `[sender_short_id: X]` line into the prompt **text** when it forwards a group-room message over ACP (see its `user-message-prefix.js::withGroupRoomSenderShortId`); the adapter recognises that prefix and routes on it. For pure 1:1 DM traffic (the common case), there is no sender identity on the wire at all and the adapter falls back to a `kimi:dm:<sessionId>` identity + one-shot warning. Multi-user deployments should prefer the group channel (which has full structured sender metadata from Subscribe) until Kimi adds per-user identity to DM ACP params.

**No DM outbound outside a live WS.** The standalone `send_kimi_message` helper (used by cron and `send_message_tool` when no live gateway is running) only supports group rooms. DM replies require an active `agent-ws` session, which only exists while the gateway is connected.

**Kimi replay on Subscribe reconnect.** When the group `Subscribe` stream reconnects, Kimi replays a short window of recent events. The adapter dedupes by `(chat_id, message_id)` over a 2000-entry ring buffer, which covers typical replay windows. If your bot is in a very high-volume room and messages replay outside the window, duplicates can slip through — raise `_DEDUP_MAXLEN` in `gateway/platforms/kimi.py` if needed.

**Group file support is native; DM file sends are text fallback only.** Group-room sends can upload local files and send resource links. Inbound group `kimi-file://` links are resolved into local cache files for the agent. The standalone DM ACP path has no currently mapped proactive file-send surface outside a live websocket reply, so local DM attachments are represented as text links.

**Streaming group sends are unary for now.** The current Kimi Claw package exposes `SendMessageStream`, but Hermes currently sends group replies with unary `SendMessage`. This keeps the first adapter implementation aligned with Hermes' existing gateway send contract; streaming parity is a future delivery-semantics change.

**Room/thread admin affordances are intentionally not exposed.** The Kimi IM generated service includes broader room, thread, audience, and admin methods. Hermes exposes read/send basics via `kimi_im`; mutating/admin operations should be added only behind explicit approval and tests because they affect real Kimi rooms.

## Maintenance

Kimi Claw and Kimi IM are not published as stable third-party APIs. Treat the bundled Kimi Claw package as an observed protocol reference, and run this drift check when Kimi Claw updates:

```bash
source .venv/bin/activate
python scripts/check_kimi_claw_surface.py --json
```

The check downloads the current public `kimi-claw-latest.tgz`, verifies the IM RPC names, required headers, message block fields, upload/file-resolution hooks, and OpenClaw config keys that Hermes depends on. It intentionally does not run Kimi's installer.

## Security notes

- The bot token grants read + write across every DM and room Kimi has added the bot to. Treat it like an API key — store in env vars or your secrets manager, never commit.
- Kimi access is denied unless a user is allowed by pairing, allowlist, or an explicit global allow-all setting. Use `KIMI_ALLOWED_USERS` for shared/public deployments. `KIMI_ALLOW_ALL_USERS=true` is only appropriate for a personal one-user bot that is not exposed as a shared automation endpoint.

## Protocol reference

- [Connect protocol spec](https://connectrpc.com/docs/protocol)
- [Zed ACP spec](https://github.com/zed-industries/agent-client-protocol)
- Kimi IMService RPCs observed in the Kimi Claw v0.25.0 package: `GetMe`, `GetRoom`, `ListMembers`, `ListMessages`, `ListRoomFiles`, `SendMessage`, `SendMessageStream`, `Subscribe`, plus broader room/thread/audience/admin methods in the generated service.
