"""Plugin registry and discovery."""
from __future__ import annotations
from typing import Any
from .plugin_base import TransformationPlugin


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, TransformationPlugin] = {}

    def register(self, plugin: TransformationPlugin) -> None:
        if plugin.id in self._plugins:
            return
        self._plugins[plugin.id] = plugin

    def all(self) -> list[TransformationPlugin]:
        return list(self._plugins.values())

    def by_id(self, plugin_id: str) -> TransformationPlugin | None:
        return self._plugins.get(plugin_id)

    def resolve(self, source_stack: str, target_stack: str) -> TransformationPlugin | None:
        for p in self._plugins.values():
            if p.source_stack == source_stack and p.target_stack == target_stack:
                return p
        return None

    def describe_all(self) -> list[dict[str, Any]]:
        return [p.describe() for p in self._plugins.values()]


_REGISTRY: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    _REGISTRY = PluginRegistry()
    from .plugins.helidon_to_spring.plugin import HelidonToSpringPlugin
    from .plugins.oracle_to_postgres.plugin import OracleToPostgresPlugin
    _REGISTRY.register(HelidonToSpringPlugin())
    _REGISTRY.register(OracleToPostgresPlugin())
    return _REGISTRY


def reset_registry() -> None:
    """Test helper — force re-initialisation on next `get_registry()`."""
    global _REGISTRY
    _REGISTRY = None
