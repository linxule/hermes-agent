"""Re-export shim — Kimi adapter has moved to ``plugins/kimi/``.

Kept while ``gateway.run._create_adapter`` still has an in-tree fallback;
delete once the plugin is the only path.
"""
from plugins.kimi.kimi_adapter import KimiAdapter, check_kimi_requirements  # noqa: F401
