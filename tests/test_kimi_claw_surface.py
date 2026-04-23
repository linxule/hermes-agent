import json
import tarfile

from scripts.check_kimi_claw_surface import (
    REQUIRED_CONFIG_PATHS,
    REQUIRED_FILES,
    REQUIRED_TOKENS,
    _extract,
    inspect_surface,
)


def _write_minimal_surface(root):
    for rel in REQUIRED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    for rel, tokens in REQUIRED_TOKENS.items():
        (root / rel).write_text("\n".join(tokens), encoding="utf-8")

    config_props = {}
    for section, key in REQUIRED_CONFIG_PATHS:
        section_schema = config_props.setdefault(section, {"properties": {}})
        section_schema["properties"][key] = {"type": "string"}

    (root / "package/openclaw.plugin.json").write_text(
        json.dumps({
            "id": "kimi-claw",
            "version": "0.25.0",
            "configSchema": {"properties": config_props},
        }),
        encoding="utf-8",
    )
    (root / "package/package.json").write_text(
        json.dumps({"name": "kimi-claw", "version": "0.25.0"}),
        encoding="utf-8",
    )


def test_inspect_surface_accepts_expected_kimi_claw_shape(tmp_path):
    _write_minimal_surface(tmp_path)

    result = inspect_surface(tmp_path)

    assert result["ok"] is True
    assert result["plugin_id"] == "kimi-claw"
    assert result["plugin_version"] == "0.25.0"
    assert result["missing_files"] == []
    assert result["missing_tokens"] == {}
    assert result["missing_config_paths"] == []


def test_inspect_surface_reports_protocol_drift(tmp_path):
    _write_minimal_surface(tmp_path)
    generated = tmp_path / "package/dist/src/im/proto/generated/index.js"
    generated.write_text("Subscribe\nSendMessage\n", encoding="utf-8")

    result = inspect_surface(tmp_path)

    assert result["ok"] is False
    assert "package/dist/src/im/proto/generated/index.js" in result["missing_tokens"]
    assert "IM_AUTH_HEADER=\"X-Kimi-Bot-Token\"" in result["missing_tokens"][
        "package/dist/src/im/proto/generated/index.js"
    ]


def test_extract_rejects_unsafe_tar_paths(tmp_path):
    tgz_path = tmp_path / "bad.tgz"
    with tarfile.open(tgz_path, "w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 0
        archive.addfile(info)

    try:
        _extract(tgz_path, tmp_path / "out")
    except ValueError as exc:
        assert "unsafe tar member path" in str(exc)
    else:
        raise AssertionError("unsafe tar member should have been rejected")
