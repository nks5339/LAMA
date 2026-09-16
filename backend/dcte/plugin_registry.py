"""Plugin registry and discovery."""
from __future__ import annotations
from typing import Any
from .plugin_base import TransformationPlugin


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, TransformationPlugin] = {}
        # Selected by `resolve` when no registered plugin claims the pair.
        # Deliberately NOT in `_plugins`: it must never appear in the plugin
        # list the UI renders as concrete "Source → Target" transformations.
        self._fallback: TransformationPlugin | None = None

    def register(self, plugin: TransformationPlugin) -> None:
        if plugin.id in self._plugins:
            return
        self._plugins[plugin.id] = plugin

    def register_fallback(self, plugin: TransformationPlugin) -> None:
        self._fallback = plugin

    def all(self) -> list[TransformationPlugin]:
        return list(self._plugins.values())

    def by_id(self, plugin_id: str) -> TransformationPlugin | None:
        return self._plugins.get(plugin_id)

    def resolve(self, source_stack: str, target_stack: str) -> TransformationPlugin | None:
        """Best plugin for a pair: an exact deterministic match, else the
        generic AI fallback.

        iter-21 — the fallback exists because the stack catalogue offers many
        more pairs than there are hand-written transformers, and the engine
        raises on an unresolved pair. Without it every stack added to the
        dropdown would be selectable and dead on start.

        Exact matches keep priority, so Helidon → Spring Boot and Oracle →
        PostgreSQL still run their deterministic transformers.
        """
        for p in self._plugins.values():
            if p.source_stack == source_stack and p.target_stack == target_stack:
                return p
        return self._fallback

    def resolve_exact(self, source_stack: str, target_stack: str) -> TransformationPlugin | None:
        """Deterministic match only — no fallback. Used to tell the operator
        whether a pair has a real transformer behind it."""
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
    from .plugins.generic_ai.plugin import GenericAiPlugin
    _REGISTRY.register(HelidonToSpringPlugin())
    _REGISTRY.register(OracleToPostgresPlugin())
    _REGISTRY.register_fallback(GenericAiPlugin())
    return _REGISTRY


def reset_registry() -> None:
    """Test helper — force re-initialisation on next `get_registry()`."""
    global _REGISTRY
    _REGISTRY = None
