"""Graph-KB toggle (iter-13.31).

Single source of truth for the "use graph KB" feature flag. Resolution order:

1. Per-project override stored at ``projects.settings.use_graph_kb`` (bool).
   Set via ``PATCH /api/projects/{pid}/settings``.
2. Env var ``LAMA_USE_GRAPH_KB`` (truthy: ``1/true/yes/on``,
   falsy: ``0/false/no/off``). Default = **disabled** (opt-in) so the
   expensive graphify LLM enrichment (Anthropic Opus, agent_key
   ``kb.graphify``) never fires until the user explicitly turns the
   Sidebar toggle ON or sets ``LAMA_USE_GRAPH_KB=1``. Prior to this
   change the default was ON, which surprised users who hadn't touched
   the toggle yet (the Opus call still ran during Build KB).

When disabled, SRS / Architecture / CodeGen prompts skip the graph subgraph
block entirely and run on TOON + RAG only. The graph itself is still built
on Build KB (cheap, idempotent) — only the prompt injection is suppressed.
"""
from __future__ import annotations

import os
from typing import Optional

from db import projects

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_default() -> bool:
    raw = (os.environ.get("LAMA_USE_GRAPH_KB") or "").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    return False  # default OFF — graphify (Opus) is opt-in


async def is_graph_kb_enabled(project_id: Optional[str]) -> bool:
    """Return True iff graph-KB injection should run for this project."""
    if project_id:
        try:
            doc = await projects.find_one(
                {"id": project_id}, {"_id": 0, "settings": 1}
            )
            if doc:
                settings = doc.get("settings") or {}
                override = settings.get("use_graph_kb")
                if isinstance(override, bool):
                    return override
        except Exception:  # noqa: BLE001 — never block on lookup failure
            pass
    return _env_default()


def env_default_graph_kb() -> bool:
    """Synchronous accessor for the env-derived default (no project lookup)."""
    return _env_default()

