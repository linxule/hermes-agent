---
name: kimi-platform
description: Platform capability guide for Kimi (kimi.com / Moonshot AI) — DM and group-room messaging, file sharing, @mentions, and kimiim-cli group pull. Install in ~/.hermes/workspace/skills/kimi-platform/ so the agent self-discovers platform surface without operator explanation.
version: 1.0.0
author: linxule
license: MIT
metadata:
  hermes:
    tags: [Kimi, Platform, Messaging, GroupChat, FileSharing]
    related_skills: []
---

# Kimi Platform

You are connected to Kimi (kimi.com / Moonshot AI) via the Hermes `KimiAdapter`. This skill describes the platform surface available to you so you can reason about capabilities without the user having to explain them.

## Channels

### DM (ACP WebSocket)

You receive prompts and send replies on the Kimi DM channel via the Agent Client Protocol (ACP) WebSocket at `wss://www.kimi.com/api-claw/bots/agent-ws`. The channel carries:

- Inbound: `session/prompt` frames (user message + optional media blocks)
- Outbound: `agent_message_chunk` frames (streaming reply chunks, ≤3500 chars each) followed by `end_turn`

The adapter splits replies automatically at paragraph boundaries. You do not need to chunk responses manually.

### Group Rooms (Connect RPC)

Group messages arrive via a `Subscribe` bidi-stream from `https://www.kimi.com/api-ws/`. You can participate in all rooms the bot is a member of. Group messages include:

- `chatId`: room identifier
- `sender.shortId`: short ID of the sender (e.g. `abc12345`)
- `text` / `blocks`: message body (blocks may carry richer content than plain `text`)

Outbound group replies go via unary `SendMessage` RPC. Thread-level replies and @mention blocks are not yet wired on the outbound wire (Kimi Claw v0.25.0 limitation); replies land in the underlying room.

## @Mentions

In group rooms, users may @mention you using your short ID. Kimi encodes this as a `mention` block in the message payload. You can read the mention from the inbound `blocks` list. To mention a user in your reply, include `@<shortId>` in your text — the adapter will render it as a Kimi mention block on outbound (support varies by Kimi Claw version).

### Mention-gated mode

If the operator has set `group_require_mention: true`, the adapter only dispatches group messages that @mention you. Messages that do not mention you are silently filtered. This is a server-side policy knob — you do not need to check it yourself.

## Files and Media

### Receiving files

Inbound group messages may carry `resourceLink` blocks with Kimi file URIs (`kimi-file://<id>`). The adapter resolves these to local paths in `~/.hermes/cache/kimi_files/` using `GetFile` before dispatch. When your prompt contains a local file path in that directory, it arrived via Kimi file transfer.

### Sending files

Use the `send_document` tool or attach files via MEDIA: syntax in your response. The adapter:

1. Uploads the file to Kimi via `UploadFile` RPC.
2. Embeds the resulting `kimi-file://` URI in a `resource_link` block in `SendMessage`.

Supported MIME types: anything Kimi accepts (images, PDFs, code files, archives).

Image files can also be sent as inline image blocks via `send_image`.

## Group Pull via `kimiim-cli`

For group messages that are not pushed over the ACP WS (groups where the bot receives messages but the WS doesn't carry them), use the `kimiim-cli` skill if it is installed:

```bash
kimiim-cli list-messages <chat_id>      # poll recent group messages
kimiim-cli send-message <chat_id> <text>  # post a reply
```

`chat_id` follows the format `room:<uuid>` as reported in event metadata. You should prefer the adapter's normal dispatch path (messages arrive automatically) and fall back to `kimiim-cli` only when explicitly instructed or when the adapter has been configured with `enable_groups: false`.

## Session and Identity

- **DM session**: The adapter maintains a single persistent Hermes session for the DM channel, keyed to your bot identity + chat. Session context persists across Kimi WS reconnects (the adapter is in-process).
- **Group sessions**: Each room gets its own Hermes session (by default, also per-user when `group_sessions_per_user: true`). This means group room A and room B have separate conversation histories.
- **Your identity**: Your bot name and short ID are reported in `GetMe` at connect time. You can find them in logs (`Kimi: connected as <name>, shortId=<id>`). You can use them to recognize when a message is addressed to you.

## Outbound Formatting

Kimi renders Markdown in DM and group rooms. You can use:
- `**bold**`, `*italic*`, `` `code` ``, triple-backtick code blocks
- Numbered and bulleted lists
- Section headers (`##`, `###`)

Avoid deeply nested formatting — Kimi's rendering is optimized for chat, not long-form documents. Prefer concise chunked replies over one large response.

## Limits and Gotchas

- **Message size**: The adapter auto-chunks replies at 3500 characters at paragraph boundaries. Do not attempt to send one massive response — chunk naturally in your prose and the adapter handles splitting.
- **Thread replies**: Not available on outbound (`SendMessage` does not carry a thread field in Kimi Claw v0.25.0). Replies land in the room, not a thread.
- **WS reconnects**: If the DM WS closes (~60s idle), the adapter reconnects automatically. Your Hermes session survives; in-progress tool calls are not affected.
- **Group backoff**: If the Subscribe stream disconnects, the adapter reconnects with exponential backoff (floor 10s). Brief message gaps during reconnect are expected.
- **Self-message filter**: The adapter filters outbound messages you authored (prevents echo loops). Do not try to process your own sent messages.

## Quick reference

| Capability | Status |
|---|---|
| DM receive + reply | ✅ |
| Group receive + reply | ✅ |
| @mention detection | ✅ |
| File receive (kimi-file://) | ✅ |
| File send (upload + resource_link) | ✅ |
| Image send | ✅ |
| Thread-level replies | ❌ (not available, Kimi Claw v0.25.0) |
| Group message pull (kimiim-cli) | optional skill |
| Multi-bot (multiple identities) | ❌ (one bot per adapter instance) |
