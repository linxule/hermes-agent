"""Kimi platform adapter plugin.

Registers :class:`KimiAdapter` against the platform-adapter registry hook so
that ``gateway.run._create_adapter`` discovers and dispatches Kimi platform
configs to this plugin's factory before falling through to any in-tree
fallback branch.

The adapter body lives in :mod:`plugins.kimi.kimi_adapter`. The ``register``
function below is the only public surface the Hermes plugin loader sees.
"""

from __future__ import annotations

from typing import Any

from gateway.config import Platform

from .kimi_adapter import KimiAdapter, check_kimi_requirements

__all__ = ["KimiAdapter", "check_kimi_requirements", "register"]


def register(ctx: Any) -> None:
    """Register the Kimi platform adapter factory with Hermes."""
    ctx.register_platform_adapter(
        Platform.KIMI,
        lambda config: KimiAdapter(config),
        requirements_check=check_kimi_requirements,
    )
