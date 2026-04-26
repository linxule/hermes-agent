"""End-to-end integration tests for the bundled ``plugins/kimi/`` plugin.

These tests exercise the full vertical:

  bundled plugin discovery
    → ``register(ctx)`` invocation
      → ``ctx.register_platform_adapter(Platform.KIMI, ...)``
        → ``gateway.platforms.registry._FACTORIES[Platform.KIMI]`` populated
          → ``GatewayRunner._create_adapter(Platform.KIMI, cfg)`` returns a
            real :class:`KimiAdapter`

The companion unit tests in ``tests/hermes_cli/test_plugins.py``
(``TestPluginPlatformAdapterRegistry``) only cover the registry hook in
isolation with a fake adapter. This file confirms the actual Kimi plugin in
``plugins/kimi/`` wires through that hook end-to-end against the real
``KimiAdapter`` class.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_platform_factory_registry():
    """Wipe the platform-adapter registry around each test.

    Discovery loads bundled plugins (including kimi) every time. The
    registry survives across tests in the same process unless we clear it,
    which would let prior runs mask real failures.
    """
    from gateway.platforms import registry

    registry._FACTORIES.clear()
    try:
        yield
    finally:
        registry._FACTORIES.clear()


@pytest.fixture(autouse=True)
def _clear_kimi_plugin_module_cache():
    """Force a fresh import of the kimi plugin between tests.

    ``PluginManager._load_directory_module`` registers the plugin under
    ``hermes_plugins.kimi`` in ``sys.modules``. Leaving that around means a
    second discovery call short-circuits at import time and the
    ``register()`` we depend on never re-runs.
    """
    yield
    for mod_name in list(sys.modules):
        if mod_name.startswith("hermes_plugins.kimi"):
            del sys.modules[mod_name]


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so we control plugins.enabled."""
    home = tmp_path / "hermes_test"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _enable_kimi_plugin(hermes_home: Path) -> None:
    """Write ``plugins.enabled: [kimi]`` into the test HERMES_HOME config."""
    cfg_path = hermes_home / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    plugins_cfg = cfg.setdefault("plugins", {})
    enabled = plugins_cfg.setdefault("enabled", [])
    if "kimi" not in enabled:
        enabled.append("kimi")
    cfg_path.write_text(yaml.safe_dump(cfg))


# ── Tests ──────────────────────────────────────────────────────────────────


class TestKimiPluginEnd2End:
    """The kimi plugin loads, registers, and dispatches via the registry hook."""

    def test_plugin_yaml_present_and_parses(self):
        """The bundled kimi plugin ships a valid manifest at the documented path."""
        from hermes_cli.plugins import PluginManager

        repo_root = Path(__file__).resolve().parents[2]
        manifest_file = repo_root / "plugins" / "kimi" / "plugin.yaml"
        assert manifest_file.exists(), "plugins/kimi/plugin.yaml missing"

        data = yaml.safe_load(manifest_file.read_text())
        assert data["name"] == "kimi"
        assert data["kind"] == "standalone"
        # KIMI_BOT_TOKEN gates real connectivity; the manifest must declare it
        # so `hermes plugins list` and the runtime requirements check have
        # something to advertise.
        assert "KIMI_BOT_TOKEN" in data.get("requires_env", [])

        # Sanity-check the parser too — same file, parsed by the real loader.
        mgr = PluginManager()
        manifest = mgr._parse_manifest(
            manifest_file, manifest_file.parent, "bundled", ""
        )
        assert manifest is not None
        assert manifest.name == "kimi"
        assert manifest.kind == "standalone"

    def test_discovery_loads_kimi_and_registers_factory(self, hermes_home):
        """Real discovery wires the bundled kimi plugin into the registry."""
        _enable_kimi_plugin(hermes_home)

        from hermes_cli.plugins import PluginManager
        from gateway.config import Platform
        from gateway.platforms.registry import lookup_platform_factory

        mgr = PluginManager()
        mgr.discover_and_load()

        loaded = mgr._plugins.get("kimi")
        assert loaded is not None, (
            "kimi plugin not discovered — bundled scan may have skipped "
            "plugins/kimi/"
        )
        assert loaded.enabled, (
            f"kimi plugin discovered but not enabled: error={loaded.error!r}"
        )

        entry = lookup_platform_factory(Platform.KIMI)
        assert entry is not None, (
            "Platform.KIMI factory not registered — register() likely failed "
            "or did not call ctx.register_platform_adapter"
        )

        factory, requirements_check = entry
        assert callable(factory)
        # ``check_kimi_requirements`` is the function the plugin's
        # ``__init__.py`` passes through. Note: the plugin loader imports
        # the plugin under ``hermes_plugins.kimi`` while the in-tree shim
        # imports it as ``plugins.kimi.kimi_adapter`` — two different
        # ``sys.modules`` entries, so identity comparison would fail. We
        # check by name instead, which is what matters for behaviour.
        assert callable(requirements_check)
        assert requirements_check.__name__ == "check_kimi_requirements"
        assert requirements_check.__module__.endswith("kimi_adapter")

    def test_create_adapter_dispatches_to_kimi_plugin_factory(self, hermes_home, monkeypatch):
        """``GatewayRunner._create_adapter(Platform.KIMI, ...)`` builds a KimiAdapter via the plugin path.

        This is the load-bearing claim of the plugin variant — once
        discovery has run, the registry hook in ``_create_adapter`` short-
        circuits to the plugin factory instead of falling through to the
        in-tree ``elif Platform.KIMI`` branch.
        """
        _enable_kimi_plugin(hermes_home)
        # KIMI_BOT_TOKEN gates ``check_kimi_requirements`` returning True;
        # without it the registry hook would skip and we'd silently fall
        # through to the in-tree branch (which would also skip), making
        # the test pass for the wrong reason.
        monkeypatch.setenv("KIMI_BOT_TOKEN", "km_b_prod_TEST")

        from hermes_cli.plugins import PluginManager
        from gateway.config import Platform, PlatformConfig
        from gateway.run import GatewayRunner

        mgr = PluginManager()
        mgr.discover_and_load()
        assert mgr._plugins.get("kimi") and mgr._plugins["kimi"].enabled, (
            "kimi plugin must be loaded for this test to be meaningful"
        )

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = types.SimpleNamespace(
            group_sessions_per_user=False,
            thread_sessions_per_user=False,
        )

        cfg = PlatformConfig(
            enabled=True,
            token="km_b_prod_TEST",
            extra={"enable_dms": True, "enable_groups": True},
        )

        adapter = runner._create_adapter(Platform.KIMI, cfg)

        assert adapter is not None, (
            "_create_adapter returned None — registry hook did not dispatch "
            "to the kimi plugin factory"
        )
        # The adapter must be the one the plugin loader instantiated. The
        # plugin loader imports the plugin under ``hermes_plugins.kimi``, so
        # we check by class name + base class identity rather than by
        # ``isinstance`` against our own import (which lives under a
        # different ``sys.modules`` entry).
        assert adapter.__class__.__name__ == "KimiAdapter"
        from gateway.platforms.base import BasePlatformAdapter
        assert isinstance(adapter, BasePlatformAdapter)
        # Sanity: the class should resolve to the plugin module, not to
        # the shim or some other path.
        assert "kimi_adapter" in adapter.__class__.__module__

    def test_create_adapter_skips_when_plugin_requirements_fail(self, hermes_home):
        """If ``check_kimi_requirements`` returns False, the plugin path returns None.

        ``check_kimi_requirements`` exists to fail fast when the optional
        deps are unavailable. We monkey-patch it via the registry entry so
        we can drive the failure path without uninstalling websockets /
        aiohttp from the test environment.
        """
        _enable_kimi_plugin(hermes_home)

        from hermes_cli.plugins import PluginManager
        from gateway.config import Platform, PlatformConfig
        from gateway.platforms import registry
        from gateway.run import GatewayRunner

        mgr = PluginManager()
        mgr.discover_and_load()
        assert mgr._plugins.get("kimi") and mgr._plugins["kimi"].enabled

        # Replace the registry entry's requirements_check with one that
        # returns False. Keep the original factory so we'd notice if the
        # check was bypassed.
        original = registry._FACTORIES[Platform.KIMI]
        original_factory = original[0]
        registry._FACTORIES[Platform.KIMI] = (original_factory, lambda: False)

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = types.SimpleNamespace(
            group_sessions_per_user=False,
            thread_sessions_per_user=False,
        )

        cfg = PlatformConfig(enabled=True)
        adapter = runner._create_adapter(Platform.KIMI, cfg)

        assert adapter is None, (
            "When the registry's requirements_check returns False, "
            "_create_adapter must return None and not fall through to the "
            "in-tree branch."
        )

    def test_shim_module_re_exports_plugin_symbols(self):
        """``gateway.platforms.kimi`` keeps re-exporting the moved symbols."""
        # Backwards compatibility: any code that did
        #   from gateway.platforms.kimi import KimiAdapter
        # before the move must keep working until the in-tree fallback in
        # _create_adapter is deleted.
        from gateway.platforms import kimi as shim
        from plugins.kimi import kimi_adapter as plugin_module

        assert shim.KimiAdapter is plugin_module.KimiAdapter
        assert shim.check_kimi_requirements is plugin_module.check_kimi_requirements
