"""
Platform Adapter Registry
=========================

Central map of registered platform adapter factories. Populated by plugins
at import-time via :meth:`hermes_cli.plugins.PluginContext.register_platform_adapter`;
consulted by the gateway's ``_create_adapter`` dispatch before the in-tree
if/elif chain so plugin-provided adapters take precedence for the same
``Platform`` value.

The ``Platform`` enum stays closed by design — adding a new platform value
still requires an in-tree commit to ``gateway/config.py``. The hook is for
**factories**, not enum members. This mirrors the ``image_gen`` registry
pattern (``agent/image_gen_registry.py``).

Re-registration of the same ``Platform`` value overwrites the previous
entry and logs a warning. This makes hot-reload scenarios (tests, dev
loops) behave predictably while surfacing the (usually unintended) case
of two plugins claiming the same platform.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter

logger = logging.getLogger(__name__)

PlatformFactory = Callable[[PlatformConfig], BasePlatformAdapter]
RequirementsCheck = Callable[[], bool]


_FACTORIES: Dict[Platform, Tuple[PlatformFactory, Optional[RequirementsCheck]]] = {}
_lock = threading.Lock()


def register_platform_factory(
    platform: Platform,
    factory: PlatformFactory,
    requirements_check: Optional[RequirementsCheck] = None,
) -> None:
    """Register a platform adapter factory.

    Raises ``TypeError`` if ``platform`` is not a ``Platform`` enum value
    or ``factory`` / ``requirements_check`` are not callable. Re-registration
    overwrites the previous entry and logs a warning.
    """
    if not isinstance(platform, Platform):
        raise TypeError(
            f"register_platform_factory() expects a Platform enum value, "
            f"got {type(platform).__name__}"
        )
    if not callable(factory):
        raise TypeError(
            f"register_platform_factory() expects a callable factory, "
            f"got {type(factory).__name__}"
        )
    if requirements_check is not None and not callable(requirements_check):
        raise TypeError(
            f"register_platform_factory() requirements_check must be callable or None, "
            f"got {type(requirements_check).__name__}"
        )
    with _lock:
        existing = _FACTORIES.get(platform)
        _FACTORIES[platform] = (factory, requirements_check)
    if existing is not None:
        logger.warning(
            "Platform factory for %s re-registered (was %r)",
            platform.value,
            existing[0],
        )
    else:
        logger.debug("Registered platform factory for %s", platform.value)


def lookup_platform_factory(
    platform: Platform,
) -> Optional[Tuple[PlatformFactory, Optional[RequirementsCheck]]]:
    """Return the registered ``(factory, requirements_check)`` for *platform*, or None."""
    with _lock:
        return _FACTORIES.get(platform)


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _FACTORIES.clear()
