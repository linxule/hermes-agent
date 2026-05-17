"""
Platform Adapter Registry

Allows platform adapters (built-in and plugin) to self-register so the gateway
can discover and instantiate them without hardcoded if/elif chains.

Built-in adapters continue to use the existing if/elif in _create_adapter()
for now.  Plugin adapters register here via PluginContext.register_platform()
and are looked up first -- if nothing is found the gateway falls through to
the legacy code path.

Usage (plugin side):

    from gateway.platform_registry import platform_registry, PlatformEntry

    platform_registry.register(PlatformEntry(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: IRCAdapter(cfg),
        check_fn=check_requirements,
        validate_config=lambda cfg: bool(cfg.extra.get("server")),
        required_env=["IRC_SERVER"],
        install_hint="pip install irc",
    ))

Usage (gateway side):

    adapter = platform_registry.create_adapter("irc", platform_config)

``create_adapter`` also resolves ``${VAR}`` literals in
``PlatformConfig.token``, ``PlatformConfig.api_key``, and string values
inside ``PlatformConfig.extra`` against ``os.environ`` before invoking
the factory.  This gives external plugins parity with built-in platforms
whose tokens are resolved by ``gateway/config.py::_apply_env_overrides``.
See :func:`_resolve_env_template`.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Matches a whole-field docker-compose-style env template, e.g. "${MY_TOKEN}".
# Allows surrounding whitespace; env var names follow POSIX shell rules.
_ENV_TEMPLATE_RE = re.compile(r"^\s*\$\{([A-Za-z_][A-Za-z0-9_]*)\}\s*$")


def _resolve_env_template(value: Any) -> Any:
    """Resolve a docker-compose-style ``${VAR}`` literal via :func:`os.getenv`.

    Used internally by :py:meth:`PlatformRegistry.create_adapter` to give
    external-plugin adapters parity with built-in platforms whose tokens get
    resolved by ``gateway/config.py::_apply_env_overrides``.

    Behavior:

    - ``"${MY_TOKEN}"`` with ``MY_TOKEN=abc`` in the environment → ``"abc"``
    - ``"${MY_TOKEN}"`` with no such env var → ``""`` (caller can fall back)
    - ``"plain-string"`` → ``"plain-string"`` (no match, returned unchanged)
    - ``"prefix-${VAR}"`` → unchanged (only whole-field templates are resolved;
      partial-substring substitution is intentionally NOT supported to keep
      the contract simple)
    - ``None`` / non-string → returned unchanged
    - Already-resolved values → returned unchanged

    The helper is **idempotent against itself**: calling it twice in a row
    on the same value yields the same result, because resolved values don't
    match the ``${VAR}`` shape.  It does not promise idempotence against
    arbitrary future substitution chains.

    Deliberately rejected patterns (kept as literals):

    - ``$VAR`` (no braces) — too easy to confuse with regular strings
    - ``${MY-VAR}`` (hyphen) — not POSIX
    - ``${MY_VAR:-default}`` (bash defaults) — would require a tokenizer
    - ``"${A}${B}"`` (concatenation) — not in scope; whole-field only
    - ``"prefix-${VAR}-suffix"`` (partial substitution) — same reason
    - ``${!VAR}`` (shell-style indirect expansion) — not supported

    For non-trivial substitution (defaults, concatenation, nested), plugins
    should implement their own resolution via the ``apply_yaml_config_fn``
    hook at YAML load time rather than relying on this helper.
    """
    if not isinstance(value, str):
        return value
    match = _ENV_TEMPLATE_RE.match(value)
    if not match:
        return value
    return os.getenv(match.group(1), "")


def apply_env_template_substitutions(
    config: Any,
    *,
    label: Optional[str] = None,
) -> Any:
    """Resolve ``${VAR}`` literals in a ``PlatformConfig`` in place.

    Walks ``config.token``, ``config.api_key``, and string values inside
    ``config.extra``, replacing whole-field ``${VAR}`` templates with
    ``os.getenv(VAR, "")``.  Non-strings, ``None``, missing attributes,
    and non-dict ``extra`` are skipped safely.  Returns the same ``config``
    object (mutated) for chaining.

    Emits a WARNING when a template resolves to an empty string (env var
    unset) so silent misconfigurations stay loud.  *label* is the platform
    name/label used in the log message; falls back to ``"platform"``.

    Called from two places to keep them in sync:

    1. :py:meth:`PlatformRegistry.create_adapter` — resolves before the
       adapter factory sees the config (and before ``validate_config``).
    2. :py:meth:`gateway.config.GatewayConfig._is_platform_connected` for
       plugin-registered platforms — resolves before the generic
       ``token``/``api_key`` truthy check and before plugin ``is_connected``
       / ``validate_config`` callbacks see the config.  Without this,
       ``get_connected_platforms()`` and adapter construction can disagree
       on the same config (the literal ``"${UNSET}"`` is truthy in the
       former, empty in the latter).

    Built-in platforms reach their tokens via ``gateway/config.py::
    _apply_env_overrides`` at YAML load time, which already populates the
    field with the resolved env value, so calling this helper on a
    built-in is a no-op against any deployment that isn't itself using
    docker-compose-style templates in a built-in YAML config.

    Hot-reload note: this helper mutates ``config`` in place, which is
    irreversible for the lifetime of the ``GatewayConfig``.  If the
    referenced env var changes between gateway startup and a hot reload,
    the originally-resolved value sticks.  This matches the existing
    behavior of ``_apply_env_overrides`` for built-in platforms (env
    vars are read once at YAML load), so external plugins using this
    helper get the same semantics as built-ins — neither stricter nor
    weaker.
    """
    log_label = label or "platform"
    for attr in ("token", "api_key"):
        current = getattr(config, attr, None)
        resolved = _resolve_env_template(current)
        if resolved != current:
            setattr(config, attr, resolved)
            if isinstance(current, str) and resolved == "":
                logger.warning(
                    "Platform '%s': env var referenced by %s=%r is unset "
                    "or empty; resolved to empty string",
                    log_label, attr, current,
                )
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict):
        for key, value in list(extra.items()):
            if not isinstance(value, str):
                continue
            resolved = _resolve_env_template(value)
            if resolved != value:
                extra[key] = resolved
                if resolved == "":
                    logger.warning(
                        "Platform '%s': env var referenced by extra[%r]=%r "
                        "is unset or empty; resolved to empty string",
                        log_label, key, value,
                    )
    return config


@dataclass
class PlatformEntry:
    """Metadata and factory for a single platform adapter."""

    # Identifier used in config.yaml (e.g. "irc", "viber").
    name: str

    # Human-readable label (e.g. "IRC", "Viber").
    label: str

    # Factory callable: receives a PlatformConfig, returns an adapter instance.
    # Using a factory instead of a bare class lets plugins do custom init
    # (e.g. passing extra kwargs, wrapping in try/except).
    adapter_factory: Callable[[Any], Any]

    # Returns True when the platform's dependencies are available.
    check_fn: Callable[[], bool]

    # Optional: given a PlatformConfig, is it properly configured?
    # If None, the registry skips config validation and lets the adapter
    # fail at connect() time with a descriptive error.
    validate_config: Optional[Callable[[Any], bool]] = None

    # Optional: given a PlatformConfig, is the platform connected/enabled?
    # Used by ``GatewayConfig.get_connected_platforms()`` and setup UI status.
    # If None, falls back to ``validate_config`` or ``check_fn``.
    is_connected: Optional[Callable[[Any], bool]] = None

    # Env vars this platform needs (for ``hermes setup`` display).
    required_env: list = field(default_factory=list)

    # Hint shown when check_fn returns False.
    install_hint: str = ""

    # Optional setup function for interactive configuration.
    # Signature: () -> None (prompts user, saves env vars).
    # If None, falls back to _setup_standard_platform (needs token_var + vars)
    # or a generic "set these env vars" display.
    setup_fn: Optional[Callable[[], None]] = None

    # "builtin" or "plugin"
    source: str = "plugin"

    # Name of the plugin manifest that registered this entry (empty for
    # built-ins).  Used by ``hermes gateway setup`` to auto-enable the
    # owning plugin when the user configures its platform.
    plugin_name: str = ""

    # ── Auth env var names (for _is_user_authorized integration) ──
    # E.g. "IRC_ALLOWED_USERS" — checked for comma-separated user IDs.
    allowed_users_env: str = ""
    # E.g. "IRC_ALLOW_ALL_USERS" — if truthy, all users authorized.
    allow_all_env: str = ""

    # ── Message limits ──
    # Max message length for smart-chunking.  0 = no limit.
    max_message_length: int = 0

    # ── Privacy ──
    # If True, session descriptions redact PII (phone numbers, etc.)
    pii_safe: bool = False

    # ── Display ──
    # Emoji for CLI/gateway display (e.g. "💬")
    emoji: str = "🔌"

    # Whether this platform should appear in _UPDATE_ALLOWED_PLATFORMS
    # (allows /update command from this platform).
    allow_update_command: bool = True

    # ── LLM guidance ──
    # Platform hint injected into the system prompt (e.g. "You are on IRC.
    # Do not use markdown.").  Empty string = no hint.
    platform_hint: str = ""

    # ── Env-driven auto-configuration ──
    # Optional: read env vars, return a dict of ``PlatformConfig.extra`` fields
    # to seed when the platform is auto-enabled.  Called during
    # ``_apply_env_overrides`` BEFORE the adapter is constructed, so
    # ``gateway status`` etc. can reflect env-only configuration without
    # instantiating the adapter.  Return ``None`` (or an empty dict) to skip.
    # Signature: () -> Optional[dict[str, Any]]
    env_enablement_fn: Optional[Callable[[], Optional[dict]]] = None

    # ── YAML→env config bridge ──
    # Optional: translate this platform's ``config.yaml`` keys into env vars
    # and/or seed ``PlatformConfig.extra`` directly.  Lets a plugin own its
    # YAML config translation instead of forcing core ``gateway/config.py``
    # to know every platform's schema.
    #
    # Signature: (yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]
    # Called from ``load_gateway_config()`` after the generic shared-key loop
    # and before ``_apply_env_overrides``.  Mutating ``os.environ`` is allowed
    # (use ``not os.getenv(...)`` guards to preserve env > YAML precedence);
    # any returned dict is merged into ``PlatformConfig.extra``.  Exceptions
    # are caught and logged at debug level.
    # See website/docs/developer-guide/adding-platform-adapters.md for the
    # full contract and a worked example.
    apply_yaml_config_fn: Optional[Callable[[dict, dict], Optional[dict]]] = None

    # Optional: home-channel env var name for cron/notification delivery
    # (e.g. ``"IRC_HOME_CHANNEL"``).  When set, ``cron.scheduler`` treats this
    # platform as a valid ``deliver=<name>`` target and reads the env var to
    # resolve the default chat/room ID.  Empty = no cron home-channel support.
    cron_deliver_env_var: str = ""

    # ── Standalone (out-of-process) sending ──
    # Optional: async coroutine that delivers a message without a live
    # gateway adapter.  Called by ``tools/send_message_tool._send_via_adapter``
    # when ``cron`` runs in a separate process from the gateway and the
    # in-process adapter weakref is therefore ``None``.
    #
    # Signature:
    #     async (pconfig, chat_id, message, *, thread_id=None,
    #            media_files=None, force_document=False) -> dict
    #
    # Returns ``{"success": True, "message_id": ...}`` on success or
    # ``{"error": str}`` on failure.  Plugin authors typically open an
    # ephemeral connection / acquire a fresh OAuth token, send, and close.
    # Without this hook, plugin platforms cannot serve as cron ``deliver=``
    # targets when the gateway is not co-resident with the cron process.
    standalone_sender_fn: Optional[Callable[..., Awaitable[dict]]] = None


class PlatformRegistry:
    """Central registry of platform adapters.

    Thread-safe for reads (dict lookups are atomic under GIL).
    Writes happen at startup during sequential discovery.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        """Register a platform adapter entry.

        If an entry with the same name exists, it is replaced (last writer
        wins -- this lets plugins override built-in adapters if desired).
        """
        if entry.name in self._entries:
            prev = self._entries[entry.name]
            logger.info(
                "Platform '%s' re-registered (was %s, now %s)",
                entry.name,
                prev.source,
                entry.source,
            )
        self._entries[entry.name] = entry
        logger.debug("Registered platform adapter: %s (%s)", entry.name, entry.source)

    def unregister(self, name: str) -> bool:
        """Remove a platform entry.  Returns True if it existed."""
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Optional[PlatformEntry]:
        """Look up a platform entry by name."""
        return self._entries.get(name)

    def all_entries(self) -> list[PlatformEntry]:
        """Return all registered platform entries."""
        return list(self._entries.values())

    def plugin_entries(self) -> list[PlatformEntry]:
        """Return only plugin-registered platform entries."""
        return [e for e in self._entries.values() if e.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def create_adapter(self, name: str, config: Any) -> Optional[Any]:
        """Create an adapter instance for the given platform name.

        Returns None if:

        - No entry registered for *name*
        - ``check_fn()`` returns False (missing deps) — no mutation occurs
        - ``validate_config()`` returns False (misconfigured) — substitution
          has already mutated ``config``
        - The factory raises an exception — substitution has already mutated
          ``config``

        **Mutates ``config`` in place.**  After ``check_fn`` passes, the
        registry resolves ``${VAR}`` literals in ``config.token``,
        ``config.api_key``, and string values inside ``config.extra`` via
        :func:`os.getenv`.  This runs **before** ``validate_config`` so
        plugin authors who write ``validate_config = lambda c: bool(c.token)``
        see the resolved value, not the literal.

        Idempotent: non-template values pass through unchanged, and a
        repeated call on an already-substituted ``config`` is a no-op.
        """
        entry = self._entries.get(name)
        if entry is None:
            return None

        if not entry.check_fn():
            hint = f" ({entry.install_hint})" if entry.install_hint else ""
            logger.warning(
                "Platform '%s' requirements not met%s",
                entry.label,
                hint,
            )
            return None

        # Resolve "${VAR}" literals before validate_config and the factory
        # see them.  Same helper is also called by
        # gateway.config.GatewayConfig._is_platform_connected so plugin
        # is_connected / validate_config callbacks see resolved values
        # whether they run at YAML load time or at adapter construction.
        # See: website/docs/developer-guide/adding-platform-adapters.md
        # § "Configuration values are passed raw".
        apply_env_template_substitutions(config, label=entry.label)

        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    logger.warning(
                        "Platform '%s' config validation failed",
                        entry.label,
                    )
                    return None
            except Exception as e:
                logger.warning(
                    "Platform '%s' config validation error: %s",
                    entry.label,
                    e,
                )
                return None

        try:
            adapter = entry.adapter_factory(config)
            return adapter
        except Exception as e:
            logger.error(
                "Failed to create adapter for platform '%s': %s",
                entry.label,
                e,
                exc_info=True,
            )
            return None


# Module-level singleton
platform_registry = PlatformRegistry()
