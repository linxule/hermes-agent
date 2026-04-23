"""Kimi IM room inspection tool.

This mirrors the small set of kimiim-cli actions that matter when Hermes is
running in a Kimi Claw group room: inspect group metadata, members, recent
messages, shared files, and send a group message.
"""

import json
import logging
from typing import Any, Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


def _get_kimi_config():
    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    pconfig = config.platforms.get(Platform.KIMI)
    if not pconfig or not pconfig.enabled:
        return None
    return pconfig


def check_kimi_im_requirements() -> bool:
    try:
        pconfig = _get_kimi_config()
    except Exception:
        return False
    return bool(pconfig and (pconfig.token or pconfig.extra.get("bot_token")))


def _room_chat_id(room_id: str) -> str:
    return room_id if room_id.startswith("room:") else f"room:{room_id}"


def _raw_room_id(room_id: str) -> str:
    if room_id.startswith("room:"):
        return room_id[len("room:"):].split("/", 1)[0]
    return room_id.split("/", 1)[0]


def _raw_chat_id(chat_id: str) -> str:
    raw = chat_id[len("room:"):] if chat_id.startswith("room:") else chat_id
    if "/" in raw:
        return raw.split("/", 1)[1]
    return raw


async def kimi_im(
    action: str,
    room_id: str = "",
    chat_id: str = "",
    text: str = "",
    message_id: str = "",
    limit: int = 20,
    page_size: int = 100,
    task_id: Optional[str] = None,
) -> str:
    """Run a Kimi IM action."""
    del task_id

    pconfig = _get_kimi_config()
    if not pconfig:
        return json.dumps({"error": "Kimi platform is not configured."})

    from gateway.platforms.kimi import KimiAdapter

    adapter = KimiAdapter(pconfig)
    try:
        if action == "me":
            result = await adapter.get_me()
            return json.dumps(result, ensure_ascii=False)

        if action == "get_group":
            if not room_id:
                return json.dumps({"error": "room_id is required for get_group"})
            result = await adapter.get_group(_raw_room_id(room_id))
            return json.dumps(result, ensure_ascii=False)

        if action == "list_members":
            if not room_id:
                return json.dumps({"error": "room_id is required for list_members"})
            members = await adapter.list_group_members(
                _raw_room_id(room_id),
                page_size=page_size,
            )
            return json.dumps(
                {"members": members, "count": len(members)},
                ensure_ascii=False,
            )

        if action == "list_messages":
            target_chat_id = _raw_chat_id(chat_id or room_id)
            if not target_chat_id:
                return json.dumps({
                    "error": "chat_id or room_id is required for list_messages"
                })
            messages = await adapter.list_group_messages(
                target_chat_id,
                limit=limit,
                start_message_id=message_id or None,
            )
            return json.dumps(
                {"messages": messages, "count": len(messages)},
                ensure_ascii=False,
            )

        if action == "list_files":
            if not room_id:
                return json.dumps({"error": "room_id is required for list_files"})
            files = await adapter.list_group_files(
                _raw_room_id(room_id),
                page_size=page_size,
            )
            return json.dumps(
                {"files": files, "count": len(files)},
                ensure_ascii=False,
            )

        if action == "send_message":
            target_chat_id = chat_id or _room_chat_id(room_id)
            if not target_chat_id or target_chat_id == "room:":
                return json.dumps({
                    "error": "chat_id or room_id is required for send_message"
                })
            if not text:
                return json.dumps({"error": "text is required for send_message"})
            result = await adapter.send(_room_chat_id(target_chat_id), text)
            return json.dumps({
                "success": result.success,
                "message_id": result.message_id,
                "error": result.error,
                "retryable": result.retryable,
            }, ensure_ascii=False)

        return json.dumps({
            "error": f"Unknown action: {action}",
            "available_actions": [
                "me",
                "get_group",
                "list_members",
                "list_messages",
                "list_files",
                "send_message",
            ],
        })
    except Exception as exc:
        logger.warning("kimi_im action failed: %s", exc)
        return json.dumps({"error": str(exc)})
    finally:
        await adapter._cleanup_http()


KIMI_IM_SCHEMA: Dict[str, Any] = {
    "name": "kimi_im",
    "description": (
        "Inspect and interact with Kimi Claw group rooms: get group rules, "
        "list members, list recent messages, list shared files, or send a "
        "short group message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "me",
                    "get_group",
                    "list_members",
                    "list_messages",
                    "list_files",
                    "send_message",
                ],
                "description": "Kimi IM action to run.",
            },
            "room_id": {
                "type": "string",
                "description": "Kimi room id for group-level actions.",
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "Kimi chat/thread id. Use for list_messages or "
                    "send_message when known."
                ),
            },
            "text": {
                "type": "string",
                "description": "Message text for send_message.",
            },
            "message_id": {
                "type": "string",
                "description": "Optional start message id for list_messages.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum messages to return for list_messages.",
                "default": 20,
            },
            "page_size": {
                "type": "integer",
                "description": "Page size for member/file listing.",
                "default": 100,
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="kimi_im",
    toolset="kimi",
    schema=KIMI_IM_SCHEMA,
    handler=lambda args, **kw: kimi_im(
        action=args.get("action", ""),
        room_id=args.get("room_id", ""),
        chat_id=args.get("chat_id", ""),
        text=args.get("text", ""),
        message_id=args.get("message_id", ""),
        limit=args.get("limit", 20),
        page_size=args.get("page_size", 100),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_kimi_im_requirements,
    requires_env=["KIMI_BOT_TOKEN"],
    is_async=True,
)
