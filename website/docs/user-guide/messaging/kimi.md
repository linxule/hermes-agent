# Kimi (Moonshot AI)

Connect Hermes to [Kimi](https://www.kimi.com/) (Moonshot AI's chat platform) using Kimi's bot protocol — supporting 1:1 DMs on Kimi web/mobile and group rooms under one bot identity.

## Overview

The Kimi adapter bridges two channels with a single `X-Kimi-Bot-Token` credential:

- **DM channel** (`wss://www.kimi.com/api-claw/bots/agent-ws`) — Kimi speaks [Zed Agent Client Protocol (ACP)](https://github.com/zed-industries/agent-client-protocol) JSON-RPC over WebSocket. Hermes responds to ACP handshake frames locally and converts `session/prompt` frames into the normal gateway pipeline. DM replies stream progressively via `agent_message_chunk` updates.
- **Group channel** (`https://www.kimi.com/api-ws/`) — Kimi exposes a [ConnectRPC](https://connectrpc.com/) surface for group IM (`kimi.gateway.im.v1.IMService`). Hermes opens a bidi-stream `Subscribe` call (all-rooms firehose) and posts outbound replies via unary `SendMessage`. Connect protocol's native JSON codec is used throughout — no protobuf bindings required.

Group-room participation requires spoofed `X-Kimi-OpenClaw-*` runtime headers on the DM WebSocket upgrade (Kimi enforces a minimum OpenClaw version gate). Default values in the adapter unlock group access out of the box.

## Prerequisites

1. **Kimi account** — Sign in at [kimi.com](https://www.kimi.com/) (web or mobile).
2. **Bot token** — In Kimi's web interface, click **"Link existing OpenClaw"** and follow the flow. Kimi returns a token of shape `km_b_prod_...`. Copy it; you won't see it again. Token auth is at WebSocket upgrade and on every HTTP request.
3. **Dependencies** — The adapter requires `websockets` and `aiohttp` (both hermes core deps; no extra install needed).

## Configuration

### Interactive setup

```bash
hermes gateway setup
```
Select **🌙 Kimi** when prompted and paste your bot token.

### Manual configuration

Set in your `.env`:

```bash
KIMI_BOT_TOKEN=km_b_prod_...
# Optional
KIMI_ALLOW_ALL_USERS=true                  # Skip allowlist (recommended — Kimi tokens are per-user)
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
      # Reconnect tuning
      ws_ping_interval: 15
      ws_ping_timeout: 60
      reconnect_max_s: 60
      # Group-gate OpenClaw runtime spoof (defaults unlock groups)
      openclaw_version: "2026.3.13"       # Minimum gate version
      claw_version: "0.25.0"
      openclaw_plugins: "kimi-claw"
```

## Chat-id format

Kimi exposes two distinct address spaces that the adapter normalises via a prefix scheme:

| Chat-id                         | Meaning                                          | Transport                |
|---------------------------------|--------------------------------------------------|--------------------------|
| `dm:im:kimi:main`               | The single 1:1 DM channel with this bot          | ACP WebSocket            |
| `room:<uuid>`                   | A group room                                     | Connect `SendMessage`    |
| `room:<uuid>/<thread-uuid>`     | A thread inside a group room                     | Connect `SendMessage`    |

The `dm:` / `room:` prefix is what `send_message_tool`, cron delivery, and any adapter-aware tool consume — just pass the full prefixed id.

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
- Dedup by `(chat_id, message_id)` is load-bearing — Kimi may replay recent history on reconnect.
- Outbound replies post unary `SendMessage` with the room id and optional `thread_id`.
- Optional `group_require_mention` mode only responds when the bot is @-mentioned.

## Group-room gating

To participate in group rooms, Kimi requires certain `X-Kimi-OpenClaw-*` runtime metadata headers on the WebSocket upgrade (minimum OpenClaw CalVer `2026.3.13`). The adapter sends these by default; override or omit them via `config.extra.openclaw_version`, `claw_version`, `claw_id`, `openclaw_plugins`, `openclaw_skills`.

## Troubleshooting

**DM WebSocket closes with code 4001.** The bot token was revoked or rotated. Regenerate via Kimi's "Link existing OpenClaw" flow and restart the gateway.

**DM WebSocket upgrade returns HTTP 409 ("bot already connected").** Kimi enforces a single live WebSocket per bot token. If a previous gateway instance didn't cleanly close its socket (crash, kill -9, network disconnect mid-session), Kimi's server may briefly hold a "ghost" WS and reject new upgrades with 409. The adapter handles this by backing off for 60s on the first strike and 300s on subsequent strikes, rather than retrying the default 2s → 60s exponential — rapid reconnect attempts after a 409 can cause Kimi's routing layer to silently throttle inbound DM delivery to the bot for an extended period. Wait for the cooldown; don't manually restart the gateway during it. A successful connect resets the strike counter.

**DM WebSocket upgrade returns HTTP 403.** The bot token is valid but the account lacks permission to open the bot WebSocket surface. Treated as permanent — the adapter stops retrying DMs and surfaces a fatal status. Check your Kimi account's bot permissions.

**Group messages never arrive even though the bot is in the room.** The `X-Kimi-OpenClaw-Version` header didn't meet the minimum gate. Confirm `openclaw_version: "2026.3.13"` (or newer) is active in your config, and that `X-Kimi-OpenClaw-*` appear in your adapter's WS upgrade headers.

**Subscribe stream keeps reconnecting.** Check Kimi's status — connect+json streaming uses HTTP/1.1 chunked transfer, so intermediaries that buffer aggressively can break long streams. Adjust `reconnect_max_s` to avoid backing off too aggressively.

**Tool-call output clutters Kimi DM UI.** Set `config.extra.user_message_prefix` to a shorter prefix, or configure skills to minimise tool chatter in user-facing summaries.

## Known limitations

**Multi-user DM session collapse.** Kimi's DM channel currently uses a single sentinel `sessionId` (`im:kimi:main`) across all users who DM the bot, and the public ACP frame schema doesn't consistently carry per-user identity in `session/prompt.params`. The adapter probes several plausible field shapes (`sender`, `user`, `author`, `userId`, `user_id`) and routes per-user when identity is present. When it isn't — currently the common case — all DM users collapse into one Hermes session. The adapter emits a one-shot warning log when this happens. For single-user bots this is a non-issue; for multi-user deployments, prefer the group channel until Kimi's DM schema exposes sender identity reliably.

**No DM outbound outside a live WS.** The standalone `send_kimi_message` helper (used by cron and `send_message_tool` when no live gateway is running) only supports group rooms. DM replies require an active `agent-ws` session, which only exists while the gateway is connected.

**Kimi replay on Subscribe reconnect.** When the group `Subscribe` stream reconnects, Kimi replays a short window of recent events. The adapter dedupes by `(chat_id, message_id)` over a 2000-entry ring buffer, which covers typical replay windows. If your bot is in a very high-volume room and messages replay outside the window, duplicates can slip through — raise `_DEDUP_MAXLEN` in `gateway/platforms/kimi.py` if needed.

## Security notes

- The bot token grants read + write across every DM and room Kimi has added the bot to. Treat it like an API key — store in env vars or your secrets manager, never commit.
- The default `KIMI_ALLOW_ALL_USERS=true` is safe for personal-bot usage because the token is scoped to one Kimi user account. For public-facing deployments, configure an allowlist via `KIMI_ALLOWED_USERS`.

## Protocol reference

- [Connect protocol spec](https://connectrpc.com/docs/protocol)
- [Zed ACP spec](https://github.com/zed-industries/agent-client-protocol)
- Kimi IMService RPCs extracted from the `kimiim-cli` binary: `GetMe`, `GetRoom`, `ListMembers`, `ListMessages`, `SendMessage`, `CreateThread`, `CreateInvitation`, `AcceptInvitation`, `NotifyOwner`, `Subscribe`, `SubscribeForAudience`, `ResumeMessageStream`.
