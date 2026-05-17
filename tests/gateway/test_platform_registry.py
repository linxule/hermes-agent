"""Tests for the platform adapter registry and dynamic Platform enum."""

import os
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from gateway.platform_registry import PlatformRegistry, PlatformEntry, platform_registry
from gateway.config import Platform, PlatformConfig, GatewayConfig


# ── Platform enum dynamic members ─────────────────────────────────────────


class TestPlatformEnumDynamic:
    """Test that Platform enum accepts unknown values for plugin platforms."""

    def test_builtin_members_still_work(self):
        assert Platform.TELEGRAM.value == "telegram"
        assert Platform("telegram") is Platform.TELEGRAM

    def test_dynamic_member_created(self):
        p = Platform("irc")
        assert p.value == "irc"
        assert p.name == "IRC"

    def test_dynamic_member_identity_stable(self):
        """Same value returns same object (cached)."""
        a = Platform("irc")
        b = Platform("irc")
        assert a is b

    def test_dynamic_member_case_normalised(self):
        """Mixed case normalised to lowercase."""
        a = Platform("IRC")
        b = Platform("irc")
        assert a is b
        assert a.value == "irc"

    def test_dynamic_member_with_hyphens(self):
        """Registered plugin platforms with hyphens work once registered."""
        from gateway.platform_registry import platform_registry as _reg

        entry = PlatformEntry(
            name="my-platform",
            label="My Platform",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            source="plugin",
        )
        _reg.register(entry)
        try:
            p = Platform("my-platform")
            assert p.value == "my-platform"
            assert p.name == "MY_PLATFORM"
        finally:
            _reg.unregister("my-platform")

    def test_dynamic_member_rejects_unregistered(self):
        """Arbitrary strings are rejected to prevent enum pollution."""
        with pytest.raises(ValueError):
            Platform("totally-fake-platform")

    def test_dynamic_member_rejects_non_string(self):
        with pytest.raises(ValueError):
            Platform(123)

    def test_dynamic_member_rejects_empty(self):
        with pytest.raises(ValueError):
            Platform("")

    def test_dynamic_member_rejects_whitespace_only(self):
        with pytest.raises(ValueError):
            Platform("   ")


# ── PlatformRegistry ──────────────────────────────────────────────────────


class TestPlatformRegistry:
    """Test the PlatformRegistry itself."""

    def _make_entry(self, name="test", check_ok=True, validate_ok=True, factory_ok=True):
        adapter_mock = MagicMock()
        return PlatformEntry(
            name=name,
            label=name.title(),
            adapter_factory=lambda cfg, _m=adapter_mock: _m if factory_ok else (_ for _ in ()).throw(RuntimeError("factory error")),
            check_fn=lambda: check_ok,
            validate_config=lambda cfg: validate_ok,
            required_env=[],
            source="plugin",
        ), adapter_mock

    def test_register_and_get(self):
        reg = PlatformRegistry()
        entry, _ = self._make_entry("alpha")
        reg.register(entry)
        assert reg.get("alpha") is entry
        assert reg.is_registered("alpha")

    def test_get_unknown_returns_none(self):
        reg = PlatformRegistry()
        assert reg.get("nonexistent") is None

    def test_unregister(self):
        reg = PlatformRegistry()
        entry, _ = self._make_entry("beta")
        reg.register(entry)
        assert reg.unregister("beta") is True
        assert reg.get("beta") is None
        assert reg.unregister("beta") is False  # already gone

    def test_create_adapter_success(self):
        reg = PlatformRegistry()
        entry, mock_adapter = self._make_entry("gamma")
        reg.register(entry)
        result = reg.create_adapter("gamma", MagicMock())
        assert result is mock_adapter

    def test_create_adapter_unknown_name(self):
        reg = PlatformRegistry()
        assert reg.create_adapter("unknown", MagicMock()) is None

    def test_create_adapter_check_fails(self):
        reg = PlatformRegistry()
        entry, _ = self._make_entry("delta", check_ok=False)
        reg.register(entry)
        assert reg.create_adapter("delta", MagicMock()) is None

    def test_create_adapter_validate_fails(self):
        reg = PlatformRegistry()
        entry, _ = self._make_entry("epsilon", validate_ok=False)
        reg.register(entry)
        assert reg.create_adapter("epsilon", MagicMock()) is None

    def test_create_adapter_factory_exception(self):
        reg = PlatformRegistry()
        entry = PlatformEntry(
            name="broken",
            label="Broken",
            adapter_factory=lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        )
        reg.register(entry)
        # factory raises → create_adapter returns None instead of propagating
        assert reg.create_adapter("broken", MagicMock()) is None

    def test_create_adapter_no_validate(self):
        """When validate_config is None, skip validation."""
        reg = PlatformRegistry()
        mock_adapter = MagicMock()
        entry = PlatformEntry(
            name="novalidate",
            label="NoValidate",
            adapter_factory=lambda cfg: mock_adapter,
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        )
        reg.register(entry)
        assert reg.create_adapter("novalidate", MagicMock()) is mock_adapter

    def test_all_entries(self):
        reg = PlatformRegistry()
        e1, _ = self._make_entry("one")
        e2, _ = self._make_entry("two")
        reg.register(e1)
        reg.register(e2)
        names = {e.name for e in reg.all_entries()}
        assert names == {"one", "two"}

    def test_plugin_entries(self):
        reg = PlatformRegistry()
        plugin_entry, _ = self._make_entry("plugged")
        builtin_entry = PlatformEntry(
            name="core",
            label="Core",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            source="builtin",
        )
        reg.register(plugin_entry)
        reg.register(builtin_entry)
        plugin_names = {e.name for e in reg.plugin_entries()}
        assert plugin_names == {"plugged"}

    def test_re_register_replaces(self):
        reg = PlatformRegistry()
        entry1, mock1 = self._make_entry("dup")
        entry2 = PlatformEntry(
            name="dup",
            label="Dup v2",
            adapter_factory=lambda cfg: "v2",
            check_fn=lambda: True,
            source="plugin",
        )
        reg.register(entry1)
        reg.register(entry2)
        assert reg.get("dup").label == "Dup v2"


# ── ${VAR} env template resolution ────────────────────────────────────────


class TestEnvTemplateResolution:
    """Test ``${VAR}`` resolution applied by ``create_adapter`` to ``PlatformConfig``.

    External plugins receive ``PlatformConfig`` straight from the YAML loader
    (no substitution).  ``create_adapter`` resolves ``${VAR}`` literals in
    ``token`` / ``api_key`` before calling the factory, giving plugin adapters
    parity with built-in platforms whose tokens are resolved by
    ``_apply_env_overrides()`` in ``gateway/config.py``.
    """

    def test_helper_resolves_template_with_env_set(self):
        from gateway.platform_registry import _resolve_env_template

        with patch.dict(os.environ, {"MY_TEMPLATE_VAR": "secret-value"}, clear=False):
            assert _resolve_env_template("${MY_TEMPLATE_VAR}") == "secret-value"

    def test_helper_resolves_template_with_env_unset(self):
        from gateway.platform_registry import _resolve_env_template

        # Ensure the var is genuinely absent for this case.
        os.environ.pop("DEFINITELY_UNSET_VAR_4f7c2", None)
        assert _resolve_env_template("${DEFINITELY_UNSET_VAR_4f7c2}") == ""

    def test_helper_plain_string_passthrough(self):
        from gateway.platform_registry import _resolve_env_template

        assert _resolve_env_template("plain-literal-token") == "plain-literal-token"

    def test_helper_none_passthrough(self):
        from gateway.platform_registry import _resolve_env_template

        assert _resolve_env_template(None) is None

    def test_helper_empty_string_passthrough(self):
        from gateway.platform_registry import _resolve_env_template

        assert _resolve_env_template("") == ""

    def test_helper_partial_template_unchanged(self):
        """Only whole-field templates are resolved.  Prefixed/suffixed values pass through."""
        from gateway.platform_registry import _resolve_env_template

        with patch.dict(os.environ, {"X": "abc"}, clear=False):
            # Partial template — kept as-is to avoid surprising substring substitution.
            assert _resolve_env_template("prefix-${X}") == "prefix-${X}"
            assert _resolve_env_template("${X}-suffix") == "${X}-suffix"

    def test_helper_whitespace_tolerance(self):
        """Surrounding whitespace inside a template field still resolves."""
        from gateway.platform_registry import _resolve_env_template

        with patch.dict(os.environ, {"WHITESPACE_VAR": "ok"}, clear=False):
            assert _resolve_env_template("  ${WHITESPACE_VAR}  ") == "ok"

    def test_helper_non_string_passthrough(self):
        from gateway.platform_registry import _resolve_env_template

        # Non-strings (defensive: PlatformConfig.token is Optional[str], but
        # any future schema change shouldn't crash here).
        assert _resolve_env_template(123) == 123
        sentinel = object()
        assert _resolve_env_template(sentinel) is sentinel

    def test_helper_idempotent(self):
        """Already-resolved values don't match the template shape, so a second pass is a no-op."""
        from gateway.platform_registry import _resolve_env_template

        with patch.dict(os.environ, {"IDEMP_VAR": "resolved"}, clear=False):
            once = _resolve_env_template("${IDEMP_VAR}")
            twice = _resolve_env_template(once)
            assert once == "resolved"
            assert twice == "resolved"

    def test_create_adapter_resolves_token_template(self):
        """End-to-end: a PlatformConfig with a ``${VAR}`` token reaches the factory resolved."""
        reg = PlatformRegistry()
        captured = {}

        def factory(cfg):
            captured["token"] = cfg.token
            captured["api_key"] = cfg.api_key
            return MagicMock()

        reg.register(PlatformEntry(
            name="testresolve",
            label="TestResolve",
            adapter_factory=factory,
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        ))

        cfg = PlatformConfig(
            enabled=True,
            token="${E2E_TOKEN_VAR}",
            api_key="${E2E_API_KEY_VAR}",
        )
        with patch.dict(
            os.environ,
            {"E2E_TOKEN_VAR": "tok-123", "E2E_API_KEY_VAR": "key-456"},
            clear=False,
        ):
            adapter = reg.create_adapter("testresolve", cfg)

        assert adapter is not None
        assert captured["token"] == "tok-123"
        assert captured["api_key"] == "key-456"
        # The PlatformConfig itself is mutated in place (matches the
        # _apply_env_overrides() pattern for built-in platforms).
        assert cfg.token == "tok-123"
        assert cfg.api_key == "key-456"

    def test_create_adapter_passes_through_plain_token(self):
        """End-to-end: a plain literal token is unchanged by the registry."""
        reg = PlatformRegistry()
        captured = {}

        def factory(cfg):
            captured["token"] = cfg.token
            return MagicMock()

        reg.register(PlatformEntry(
            name="testplain",
            label="TestPlain",
            adapter_factory=factory,
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        ))

        cfg = PlatformConfig(enabled=True, token="literal-bot-token-xyz")
        reg.create_adapter("testplain", cfg)
        assert captured["token"] == "literal-bot-token-xyz"

    def test_validate_config_sees_resolved_token(self):
        """Substitution runs BEFORE validate_config — a plugin that checks
        bool(config.token) must see the resolved env value, not the literal.

        Regression guard against the ordering bug where validate would pass
        on a truthy "${UNSET_VAR}" literal and then the factory would receive
        an empty token.
        """
        reg = PlatformRegistry()
        seen_during_validate = {}

        def validate(cfg):
            seen_during_validate["token"] = cfg.token
            return bool(cfg.token)

        reg.register(PlatformEntry(
            name="testvalorder",
            label="TestValOrder",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=validate,
            source="plugin",
        ))

        # Case 1: env set → validate sees resolved value → passes
        cfg_ok = PlatformConfig(enabled=True, token="${VAL_ORDER_VAR}")
        with patch.dict(os.environ, {"VAL_ORDER_VAR": "real-token"}, clear=False):
            adapter = reg.create_adapter("testvalorder", cfg_ok)
        assert adapter is not None
        assert seen_during_validate["token"] == "real-token"

        # Case 2: env unset → validate sees "" → fails (caught loudly)
        seen_during_validate.clear()
        cfg_unset = PlatformConfig(enabled=True, token="${VAL_ORDER_UNSET}")
        os.environ.pop("VAL_ORDER_UNSET", None)
        adapter = reg.create_adapter("testvalorder", cfg_unset)
        assert adapter is None
        assert seen_during_validate["token"] == ""

    def test_create_adapter_resolves_extra_dict(self):
        """End-to-end: ``${VAR}`` literals inside ``config.extra`` are also resolved.

        The canonical plugin example (`adding-platform-adapters.md`) puts
        secondary settings in ``extra:``, so plugin authors who write
        ``extra: {token: ${MY_TOKEN}}`` should get parity with
        ``token: ${MY_TOKEN}``.
        """
        reg = PlatformRegistry()
        captured = {}

        def factory(cfg):
            captured["extra"] = dict(cfg.extra)
            return MagicMock()

        reg.register(PlatformEntry(
            name="testextra",
            label="TestExtra",
            adapter_factory=factory,
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        ))

        cfg = PlatformConfig(
            enabled=True,
            extra={
                "bot_token": "${EXTRA_BOT_TOKEN}",
                "channel": "${EXTRA_CHANNEL}",
                "non_template": "plain-channel-name",
                "numeric_keep": 42,
            },
        )
        with patch.dict(
            os.environ,
            {"EXTRA_BOT_TOKEN": "extra-tok-9", "EXTRA_CHANNEL": "#general"},
            clear=False,
        ):
            reg.create_adapter("testextra", cfg)

        assert captured["extra"]["bot_token"] == "extra-tok-9"
        assert captured["extra"]["channel"] == "#general"
        assert captured["extra"]["non_template"] == "plain-channel-name"
        assert captured["extra"]["numeric_keep"] == 42  # non-strings untouched

    def test_create_adapter_logs_warning_on_empty_resolution(self, caplog):
        """Empty env-var resolution emits WARNING so silent failures stay loud."""
        import logging as _logging

        reg = PlatformRegistry()
        reg.register(PlatformEntry(
            name="testwarn",
            label="TestWarn",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        ))

        os.environ.pop("WARN_UNSET_VAR", None)
        cfg = PlatformConfig(enabled=True, token="${WARN_UNSET_VAR}")
        with caplog.at_level(_logging.WARNING, logger="gateway.platform_registry"):
            reg.create_adapter("testwarn", cfg)
        warnings = [r for r in caplog.records if "WARN_UNSET_VAR" in r.getMessage()]
        assert len(warnings) == 1
        assert "is unset" in warnings[0].getMessage()
        assert warnings[0].levelno == _logging.WARNING

    def test_create_adapter_mutation_on_validation_failure(self):
        """If validate_config returns False, the config IS still mutated by
        substitution that ran before validation.

        Contract test: substitution is unconditional once check_fn passes.
        Callers that reuse a PlatformConfig fixture after validation failure
        should be aware the dataclass fields were mutated in place.
        """
        reg = PlatformRegistry()
        reg.register(PlatformEntry(
            name="testmutval",
            label="TestMutVal",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=lambda cfg: False,  # always rejects
            source="plugin",
        ))

        cfg = PlatformConfig(enabled=True, token="${MUT_VAL_VAR}")
        with patch.dict(os.environ, {"MUT_VAL_VAR": "before-rejection"}, clear=False):
            result = reg.create_adapter("testmutval", cfg)
        assert result is None
        assert cfg.token == "before-rejection"  # mutated even though validation failed

    def test_create_adapter_mutation_on_factory_failure(self):
        """If the factory raises, the config is still mutated (substitution
        already ran).  Documents the asymmetry with `check_fn`/`validate_config`
        failure paths."""
        reg = PlatformRegistry()
        reg.register(PlatformEntry(
            name="testmutfact",
            label="TestMutFact",
            adapter_factory=lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
            check_fn=lambda: True,
            validate_config=None,
            source="plugin",
        ))

        cfg = PlatformConfig(enabled=True, token="${MUT_FACT_VAR}")
        with patch.dict(os.environ, {"MUT_FACT_VAR": "resolved-then-boom"}, clear=False):
            result = reg.create_adapter("testmutfact", cfg)
        assert result is None
        assert cfg.token == "resolved-then-boom"  # mutated before factory raised

    def test_create_adapter_no_mutation_when_check_fails(self):
        """If check_fn returns False, substitution doesn't run at all.

        Documents the boundary: check_fn is the cheapest gate and runs before
        any mutation, so a misconfigured plugin doesn't accidentally mutate
        user configs as a side effect of probing.
        """
        reg = PlatformRegistry()
        reg.register(PlatformEntry(
            name="testnomut",
            label="TestNoMut",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: False,  # short-circuit early
            validate_config=None,
            source="plugin",
        ))

        cfg = PlatformConfig(enabled=True, token="${NOMUT_VAR}")
        with patch.dict(os.environ, {"NOMUT_VAR": "should-not-resolve"}, clear=False):
            result = reg.create_adapter("testnomut", cfg)
        assert result is None
        assert cfg.token == "${NOMUT_VAR}"  # untouched


# ── GatewayConfig integration ────────────────────────────────────────────


class TestGatewayConfigPluginPlatform:
    """Test that GatewayConfig parses and validates plugin platforms."""

    def test_from_dict_accepts_plugin_platform(self):
        data = {
            "platforms": {
                "telegram": {"enabled": True, "token": "test-token"},
                "irc": {"enabled": True, "extra": {"server": "irc.libera.chat"}},
            }
        }
        cfg = GatewayConfig.from_dict(data)
        platform_values = {p.value for p in cfg.platforms}
        assert "telegram" in platform_values
        assert "irc" in platform_values

    def test_get_connected_platforms_includes_registered_plugin(self):
        """Plugin platform with registry entry passes get_connected_platforms."""
        # Register a fake plugin platform
        from gateway.platform_registry import platform_registry as _reg

        test_entry = PlatformEntry(
            name="testplat",
            label="TestPlat",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=lambda cfg: bool(cfg.extra.get("token")),
            source="plugin",
        )
        _reg.register(test_entry)
        try:
            data = {
                "platforms": {
                    "testplat": {"enabled": True, "extra": {"token": "abc"}},
                }
            }
            cfg = GatewayConfig.from_dict(data)
            connected = cfg.get_connected_platforms()
            connected_values = {p.value for p in connected}
            assert "testplat" in connected_values
        finally:
            _reg.unregister("testplat")

    def test_get_connected_platforms_excludes_unregistered_plugin(self):
        """Plugin platform without registry entry is excluded."""
        data = {
            "platforms": {
                "unknown_plugin": {"enabled": True, "extra": {"token": "abc"}},
            }
        }
        cfg = GatewayConfig.from_dict(data)
        connected = cfg.get_connected_platforms()
        connected_values = {p.value for p in connected}
        assert "unknown_plugin" not in connected_values

    def test_get_connected_platforms_excludes_invalid_config(self):
        """Plugin platform with failing validate_config is excluded."""
        from gateway.platform_registry import platform_registry as _reg

        test_entry = PlatformEntry(
            name="badconfig",
            label="BadConfig",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=lambda cfg: False,  # always fails
            source="plugin",
        )
        _reg.register(test_entry)
        try:
            data = {
                "platforms": {
                    "badconfig": {"enabled": True, "extra": {}},
                }
            }
            cfg = GatewayConfig.from_dict(data)
            connected = cfg.get_connected_platforms()
            connected_values = {p.value for p in connected}
            assert "badconfig" not in connected_values
        finally:
            _reg.unregister("badconfig")

    def test_get_connected_resolves_token_template_before_validator(self):
        """Regression guard for the lifecycle split (Codex H4): a plugin
        validator that checks ``bool(config.token)`` would otherwise accept
        the truthy literal ``"${UNSET_VAR}"`` and report the platform as
        connected, while a later ``create_adapter`` call would resolve it
        to ``""`` and refuse to construct.  After this fix, the validator
        sees the resolved value and rejects the misconfigured platform.
        """
        from gateway.platform_registry import platform_registry as _reg

        seen_during_validate = {}

        def validate(cfg):
            seen_during_validate["token"] = cfg.token
            return bool(cfg.token)

        test_entry = PlatformEntry(
            name="tplifecycle",
            label="TPLifecycle",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=validate,
            source="plugin",
        )
        _reg.register(test_entry)
        try:
            # Case A: env unset — validator must see "" and reject
            os.environ.pop("LIFECYCLE_TOKEN_VAR", None)
            data_unset = {
                "platforms": {
                    "tplifecycle": {"enabled": True, "token": "${LIFECYCLE_TOKEN_VAR}"},
                }
            }
            cfg_unset = GatewayConfig.from_dict(data_unset)
            connected = cfg_unset.get_connected_platforms()
            assert "tplifecycle" not in {p.value for p in connected}, (
                "Plugin with ${UNSET_VAR} token should NOT be reported as "
                "connected: validator must see the empty resolved value, "
                "not the truthy literal ${LIFECYCLE_TOKEN_VAR}."
            )
            assert seen_during_validate["token"] == ""

            # Case B: env set — substitution resolves to a truthy value and
            # the platform IS reported as connected (passes the generic
            # token check at the top of ``_is_platform_connected``).
            with patch.dict(
                os.environ,
                {"LIFECYCLE_TOKEN_VAR": "real-token"},
                clear=False,
            ):
                data_ok = {
                    "platforms": {
                        "tplifecycle": {
                            "enabled": True,
                            "token": "${LIFECYCLE_TOKEN_VAR}",
                        },
                    }
                }
                cfg_ok = GatewayConfig.from_dict(data_ok)
                connected = cfg_ok.get_connected_platforms()
            assert "tplifecycle" in {p.value for p in connected}
            # The PlatformConfig stored on the GatewayConfig has been
            # mutated by substitution (this is a *contract*, not an
            # implementation detail: subsequent ``create_adapter`` calls
            # rely on the mutated value).
            assert cfg_ok.platforms[Platform("tplifecycle")].token == "real-token"
        finally:
            _reg.unregister("tplifecycle")

    def test_get_connected_resolves_extra_token_before_plugin_hook(self):
        """Plugin ``validate_config`` hooks see resolved ``${VAR}`` values
        when invoked via ``get_connected_platforms``.

        Without this fix, a plugin validator like
        ``lambda c: bool(c.extra.get("token"))`` would accept the truthy
        literal ``"${UNSET}"`` and report the platform as connected while
        ``create_adapter`` would later reject the same config.
        """
        from gateway.platform_registry import platform_registry as _reg

        seen = {}

        def validate(cfg):
            seen["token"] = cfg.extra.get("token")
            return bool(cfg.extra.get("token"))

        test_entry = PlatformEntry(
            name="tphookresolved",
            label="TPHookResolved",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=validate,
            source="plugin",
        )
        _reg.register(test_entry)
        try:
            # Env set → resolved → validator passes
            with patch.dict(
                os.environ,
                {"HOOK_RESOLVED_TOKEN": "real-tok"},
                clear=False,
            ):
                data_ok = {
                    "platforms": {
                        "tphookresolved": {
                            "enabled": True,
                            "extra": {"token": "${HOOK_RESOLVED_TOKEN}"},
                        },
                    }
                }
                cfg_ok = GatewayConfig.from_dict(data_ok)
                connected = cfg_ok.get_connected_platforms()
            assert "tphookresolved" in {p.value for p in connected}
            assert seen["token"] == "real-tok", (
                "validate_config must see resolved env value, not the "
                "literal ${VAR}"
            )

            # Env unset → resolves to "" → validator rejects
            os.environ.pop("HOOK_RESOLVED_UNSET", None)
            seen.clear()
            data_unset = {
                "platforms": {
                    "tphookresolved": {
                        "enabled": True,
                        "extra": {"token": "${HOOK_RESOLVED_UNSET}"},
                    },
                }
            }
            cfg_unset = GatewayConfig.from_dict(data_unset)
            connected = cfg_unset.get_connected_platforms()
            assert "tphookresolved" not in {p.value for p in connected}
            assert seen["token"] == ""
        finally:
            _reg.unregister("tphookresolved")


# ── Extended PlatformEntry fields ─────────────────────────────────────


class TestPlatformEntryExtendedFields:
    """Test the auth, message length, and display fields on PlatformEntry."""

    def test_default_field_values(self):
        entry = PlatformEntry(
            name="test",
            label="Test",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
        )
        assert entry.allowed_users_env == ""
        assert entry.allow_all_env == ""
        assert entry.max_message_length == 0
        assert entry.pii_safe is False
        assert entry.emoji == "🔌"
        assert entry.allow_update_command is True

    def test_custom_auth_fields(self):
        entry = PlatformEntry(
            name="irc",
            label="IRC",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            allowed_users_env="IRC_ALLOWED_USERS",
            allow_all_env="IRC_ALLOW_ALL_USERS",
            max_message_length=450,
            pii_safe=False,
            emoji="💬",
        )
        assert entry.allowed_users_env == "IRC_ALLOWED_USERS"
        assert entry.allow_all_env == "IRC_ALLOW_ALL_USERS"
        assert entry.max_message_length == 450
        assert entry.emoji == "💬"


# ── Cron platform resolution ─────────────────────────────────────────


class TestCronPlatformResolution:
    """Test that cron delivery accepts plugin platform names."""

    def test_builtin_platform_resolves(self):
        """Built-in platform names resolve via Platform() call."""
        p = Platform("telegram")
        assert p is Platform.TELEGRAM

    def test_plugin_platform_resolves(self):
        """Plugin platform names create dynamic enum members."""
        p = Platform("irc")
        assert p.value == "irc"

    def test_invalid_platform_type_rejected(self):
        """Non-string values are still rejected."""
        with pytest.raises(ValueError):
            Platform(None)


# ── platforms.py integration ──────────────────────────────────────────


class TestPlatformsMerge:
    """Test get_all_platforms() merges with registry."""

    def test_get_all_platforms_includes_builtins(self):
        from hermes_cli.platforms import get_all_platforms, PLATFORMS
        merged = get_all_platforms()
        for key in PLATFORMS:
            assert key in merged

    def test_get_all_platforms_includes_plugin(self):
        from hermes_cli.platforms import get_all_platforms
        from gateway.platform_registry import platform_registry as _reg

        _reg.register(PlatformEntry(
            name="testmerge",
            label="TestMerge",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
            emoji="🧪",
        ))
        try:
            merged = get_all_platforms()
            assert "testmerge" in merged
            assert "TestMerge" in merged["testmerge"].label
        finally:
            _reg.unregister("testmerge")

    def test_platform_label_plugin_fallback(self):
        from hermes_cli.platforms import platform_label
        from gateway.platform_registry import platform_registry as _reg

        _reg.register(PlatformEntry(
            name="labeltest",
            label="LabelTest",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
            emoji="🏷️",
        ))
        try:
            label = platform_label("labeltest")
            assert "LabelTest" in label
        finally:
            _reg.unregister("labeltest")


# ── apply_yaml_config_fn (PlatformEntry field + load_gateway_config dispatch) ──


class TestApplyYamlConfigFnField:
    """The hook field itself — defaults, custom values, signature."""

    def test_default_is_none(self):
        entry = PlatformEntry(
            name="test",
            label="Test",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
        )
        assert entry.apply_yaml_config_fn is None

    def test_accepts_callable(self):
        def _hook(yaml_cfg, platform_cfg):
            return None

        entry = PlatformEntry(
            name="test",
            label="Test",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            apply_yaml_config_fn=_hook,
        )
        assert entry.apply_yaml_config_fn is _hook
        # Sanity-check the signature contract.
        assert entry.apply_yaml_config_fn({"x": 1}, {"y": 2}) is None


class TestApplyYamlConfigFnDispatch:
    """End-to-end dispatch through load_gateway_config().

    Each test registers a temporary PlatformEntry, writes a config.yaml in
    a tmp HERMES_HOME, calls load_gateway_config(), and asserts the hook
    was invoked correctly.  Cleanup unregisters the entry.
    """

    def _write_config(self, tmp_path, content: str):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(content, encoding="utf-8")
        return hermes_home

    def _register_hook(self, name, hook_fn):
        from gateway.platform_registry import platform_registry as _reg

        entry = PlatformEntry(
            name=name,
            label=name.title(),
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
            apply_yaml_config_fn=hook_fn,
        )
        _reg.register(entry)
        return _reg

    def test_hook_can_mutate_environ(self, tmp_path, monkeypatch):
        """A hook that mutates os.environ has its env vars set after load."""
        env_var = "MYHOOKPLAT_FLAG"
        monkeypatch.delenv(env_var, raising=False)

        def _hook(yaml_cfg, platform_cfg):
            if "flag" in platform_cfg and not os.getenv(env_var):
                os.environ[env_var] = str(platform_cfg["flag"]).lower()
            return None

        reg = self._register_hook("myhookplat", _hook)
        try:
            home = self._write_config(
                tmp_path, "myhookplat:\n  flag: true\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            load_gateway_config()

            assert os.environ.get(env_var) == "true"
        finally:
            reg.unregister("myhookplat")
            os.environ.pop(env_var, None)

    def test_hook_returned_dict_merges_into_extra(self, tmp_path, monkeypatch):
        """A hook that returns a dict has it merged into PlatformConfig.extra."""

        def _hook(yaml_cfg, platform_cfg):
            return {"seeded_key": "seeded_value", "flag": platform_cfg.get("flag")}

        reg = self._register_hook("myextraplat", _hook)
        try:
            home = self._write_config(
                tmp_path, "myextraplat:\n  flag: yes\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            cfg = load_gateway_config()

            plat = Platform("myextraplat")
            assert plat in cfg.platforms
            extra = cfg.platforms[plat].extra
            assert extra.get("seeded_key") == "seeded_value"
            # flag value carried through from yaml_cfg arg.
            assert extra.get("flag") is True
        finally:
            reg.unregister("myextraplat")

    def test_hook_receives_full_yaml_and_platform_subdict(
        self, tmp_path, monkeypatch
    ):
        """Hook receives both the full yaml_cfg and its own platform sub-dict."""
        captured: dict = {}

        def _hook(yaml_cfg, platform_cfg):
            captured["yaml_cfg"] = yaml_cfg
            captured["platform_cfg"] = platform_cfg
            return None

        reg = self._register_hook("mycaptureplat", _hook)
        try:
            home = self._write_config(
                tmp_path,
                "top_level_key: 1\n"
                "mycaptureplat:\n"
                "  inner_key: deep\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            load_gateway_config()

            assert captured["yaml_cfg"].get("top_level_key") == 1
            assert captured["platform_cfg"] == {"inner_key": "deep"}
        finally:
            reg.unregister("mycaptureplat")

    def test_hook_exception_swallowed(self, tmp_path, monkeypatch):
        """A misbehaving hook never aborts load_gateway_config()."""

        def _bad_hook(yaml_cfg, platform_cfg):
            raise RuntimeError("plugin author bug")

        # Also register a well-behaved hook to ensure dispatch continues
        # iterating after a bad one.
        good_called = {"count": 0}

        def _good_hook(yaml_cfg, platform_cfg):
            good_called["count"] += 1
            return None

        from gateway.platform_registry import platform_registry as _reg
        _reg.register(PlatformEntry(
            name="mybadplat",
            label="MyBad",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
            apply_yaml_config_fn=_bad_hook,
        ))
        _reg.register(PlatformEntry(
            name="mygoodplat",
            label="MyGood",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
            apply_yaml_config_fn=_good_hook,
        ))
        try:
            home = self._write_config(
                tmp_path,
                "mybadplat:\n  k: v\n"
                "mygoodplat:\n  k: v\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            # Must not raise.
            from gateway.config import load_gateway_config
            load_gateway_config()

            assert good_called["count"] == 1
        finally:
            _reg.unregister("mybadplat")
            _reg.unregister("mygoodplat")

    def test_hook_skipped_when_platform_section_missing(
        self, tmp_path, monkeypatch
    ):
        """Hook is NOT called when the platform's YAML section is absent."""
        called = {"count": 0}

        def _hook(yaml_cfg, platform_cfg):
            called["count"] += 1
            return None

        reg = self._register_hook("myabsentplat", _hook)
        try:
            home = self._write_config(tmp_path, "telegram:\n  k: v\n")
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            load_gateway_config()

            assert called["count"] == 0
        finally:
            reg.unregister("myabsentplat")

    def test_hook_skipped_when_platform_section_not_dict(
        self, tmp_path, monkeypatch
    ):
        """Hook is NOT called when the platform's YAML section isn't a dict."""
        called = {"count": 0}

        def _hook(yaml_cfg, platform_cfg):
            called["count"] += 1
            return None

        reg = self._register_hook("mybadshapeplat", _hook)
        try:
            home = self._write_config(
                tmp_path, "mybadshapeplat: just-a-string\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            load_gateway_config()

            assert called["count"] == 0
        finally:
            reg.unregister("mybadshapeplat")

    def test_env_var_takes_precedence_when_hook_uses_getenv_guard(
        self, tmp_path, monkeypatch
    ):
        """The standard `not os.getenv(...)` guard preserves env > YAML."""
        env_var = "MYPRECPLAT_FLAG"
        monkeypatch.setenv(env_var, "preexisting")

        def _hook(yaml_cfg, platform_cfg):
            if "flag" in platform_cfg and not os.getenv(env_var):
                os.environ[env_var] = str(platform_cfg["flag"]).lower()
            return None

        reg = self._register_hook("myprecplat", _hook)
        try:
            home = self._write_config(
                tmp_path, "myprecplat:\n  flag: yaml-value\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config
            load_gateway_config()

            # Pre-existing env var was NOT clobbered by the hook.
            assert os.environ.get(env_var) == "preexisting"
        finally:
            reg.unregister("myprecplat")
            os.environ.pop(env_var, None)


class TestPluginPlatformSharedKeyBridge:
    """Plugin-registered platforms get the same shared-key bridging as built-ins.

    Without this, plugin authors using ``apply_yaml_config_fn`` would have to
    re-implement bridging for every common key (``unauthorized_dm_behavior``,
    ``notice_delivery``, ``reply_prefix``, ``require_mention``, ``dm_policy``,
    ``allow_from``, etc.) — defeating the hook's whole point of letting
    plugins focus on their *platform-specific* keys.
    """

    def _write_config(self, tmp_path, content: str):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(content, encoding="utf-8")
        return hermes_home

    def test_shared_keys_bridged_for_plugin_platform(self, tmp_path, monkeypatch):
        """A plugin platform's ``require_mention``/``dm_policy``/etc. flow into
        ``PlatformConfig.extra`` without the plugin needing its own bridge."""
        from gateway.platform_registry import platform_registry as _reg

        _reg.register(PlatformEntry(
            name="mysharedplat",
            label="MySharedPlat",
            adapter_factory=lambda cfg: None,
            check_fn=lambda: True,
            source="plugin",
        ))
        try:
            home = self._write_config(
                tmp_path,
                "mysharedplat:\n"
                "  require_mention: true\n"
                "  dm_policy: allow\n"
                "  reply_prefix: \"→ \"\n"
                "  allow_from: [\"alice\", \"bob\"]\n",
            )
            monkeypatch.setenv("HERMES_HOME", str(home))

            from gateway.config import load_gateway_config, Platform
            cfg = load_gateway_config()

            plat = Platform("mysharedplat")
            assert plat in cfg.platforms
            extra = cfg.platforms[plat].extra
            assert extra.get("require_mention") is True
            assert extra.get("dm_policy") == "allow"
            assert extra.get("reply_prefix") == "→ "
            assert extra.get("allow_from") == ["alice", "bob"]
        finally:
            _reg.unregister("mysharedplat")
