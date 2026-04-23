#!/usr/bin/env python3
"""Check the Kimi Claw package surface Hermes depends on.

This is a maintainer drift check. It downloads the public Kimi Claw package,
inspects the generated JavaScript/protobuf surface, and reports whether the
headers, RPC paths, and config keys used by ``gateway.platforms.kimi`` still
exist.

It intentionally does not run Kimi's installer or install kimiim-cli.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_TGZ_URL = "https://cdn.kimi.com/kimi-claw/kimi-claw-latest.tgz"

REQUIRED_FILES = [
    "package/openclaw.plugin.json",
    "package/package.json",
    "package/dist/src/im/proto/generated/index.js",
    "package/dist/src/im/rpc-protocol.js",
    "package/dist/src/im/subscribe-runtime.js",
    "package/dist/src/im/send-message.js",
    "package/dist/src/im/inbound-message.js",
    "package/dist/src/openclaw-runtime-metadata.js",
    "package/dist/src/kimi-upload-tool.js",
    "package/dist/src/user-message-prefix.js",
]

REQUIRED_TOKENS = {
    "package/dist/src/im/proto/generated/index.js": [
        'IM_AUTH_HEADER="X-Kimi-Bot-Token"',
        "Subscribe",
        "SendMessage",
        "SendMessageStream",
        "GetMessages",
        "ListMessages",
        "ListMembers",
        "ListRoomFiles",
        "GetRoom",
        "GetMe",
    ],
    "package/dist/src/im/rpc-protocol.js": [
        "application/connect+json",
        "X-Kimi-Claw-Version",
        "x-kimi-claw-default-chat",
    ],
    "package/dist/src/openclaw-runtime-metadata.js": [
        "X-Kimi-Claw-ID",
        "X-Kimi-OpenClaw-Version",
        "X-Kimi-OpenClaw-Skills",
        "X-Kimi-OpenClaw-Plugins",
        "openclaw",
        "skills",
        "plugins",
    ],
    "package/dist/src/im/send-message.js": [
        "chatId",
        "blocks",
        "resourceLink",
    ],
    "package/dist/src/im/inbound-message.js": [
        "listMessages",
        "mapImChatMessageToPromptBlocks",
        "resource_link",
        "kimi-file://",
    ],
    "package/dist/src/kimi-upload-tool.js": [
        "/files:upload",
        "X-Kimi-Bot-Token",
        "kimi-file://",
    ],
    "package/dist/src/user-message-prefix.js": [
        "Message From Kimi Group Chat Room:",
        "sender_short_id",
    ],
}

REQUIRED_CONFIG_PATHS = [
    ("bridge", "token"),
    ("bridge", "kimiapiHost"),
    ("bridge", "outboundTransport"),
    ("bridge", "promptTimeoutMs"),
    ("bridge", "shell"),
    ("bridge", "terminalWs"),
    ("gateway", "agentId"),
    ("log", "enabled"),
]


def _read_text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=30) as response:
        dest.write_bytes(response.read())


def _extract(tgz_path: Path, root: Path) -> None:
    with tarfile.open(tgz_path, "r:gz") as archive:
        root_resolved = root.resolve()
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            if root_resolved not in (target, *target.parents):
                raise ValueError(f"unsafe tar member path: {member.name}")
        archive.extractall(root)


def _nested_has(obj: dict[str, Any], path: tuple[str, ...]) -> bool:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False
        cur = cur[key]
        if key != path[-1]:
            cur = cur.get("properties") if isinstance(cur, dict) and "properties" in cur else cur
    return True


def inspect_surface(root: Path) -> dict[str, Any]:
    missing_files = [rel for rel in REQUIRED_FILES if not (root / rel).is_file()]
    token_failures: dict[str, list[str]] = {}
    for rel, tokens in REQUIRED_TOKENS.items():
        path = root / rel
        if not path.is_file():
            token_failures[rel] = tokens
            continue
        text = _read_text(root, rel)
        missing = [token for token in tokens if token not in text]
        if missing:
            token_failures[rel] = missing

    plugin_json: dict[str, Any] = {}
    package_json: dict[str, Any] = {}
    config_failures: list[str] = []
    try:
        plugin_json = json.loads(_read_text(root, "package/openclaw.plugin.json"))
        schema_props = plugin_json.get("configSchema", {}).get("properties", {})
        for path in REQUIRED_CONFIG_PATHS:
            if not _nested_has(schema_props, path):
                config_failures.append(".".join(path))
    except Exception as exc:
        config_failures.append(f"openclaw.plugin.json parse failed: {exc}")

    try:
        package_json = json.loads(_read_text(root, "package/package.json"))
    except Exception:
        package_json = {}

    checks_ok = not missing_files and not token_failures and not config_failures
    return {
        "ok": checks_ok,
        "plugin_id": plugin_json.get("id"),
        "plugin_version": plugin_json.get("version") or package_json.get("version"),
        "package_name": package_json.get("name"),
        "missing_files": missing_files,
        "missing_tokens": token_failures,
        "missing_config_paths": config_failures,
        "checked_files": REQUIRED_FILES,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tgz-url", default=DEFAULT_TGZ_URL)
    parser.add_argument("--tgz-path", help="Use an already downloaded tgz instead of fetching.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="kimi-claw-surface.") as tmp:
        tmp_root = Path(tmp)
        tgz_path = Path(args.tgz_path) if args.tgz_path else tmp_root / "kimi-claw.tgz"
        if not args.tgz_path:
            _download(args.tgz_url, tgz_path)
        _extract(tgz_path, tmp_root)
        result = inspect_surface(tmp_root)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "OK" if result["ok"] else "DRIFT"
        print(f"Kimi Claw surface: {status}")
        print(f"package: {result.get('package_name')} {result.get('plugin_version')}")
        for key in ("missing_files", "missing_tokens", "missing_config_paths"):
            value = result[key]
            if value:
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
