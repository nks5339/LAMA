"""iter-13.40 — Graphify routing matrix tests.

Validates the combinations the user requested:

  factory.ai ON  + graph KB ON  → enrichment routes via Factory (route="factory")
  factory.ai ON  + graph KB OFF → enrichment SKIPPED (no LLM call)
  factory.ai OFF + graph KB ON  + provider configured → route="console"
  factory.ai OFF + graph KB ON  + NO provider + no env key → SKIPPED cleanly
                                                              with reason
                                                              "no_llm_route_available"
  factory.ai OFF + graph KB OFF → enrichment SKIPPED (no LLM call)

We exercise the gating logic + the route detector directly, without
actually firing an LLM round-trip — that requires the full Mongo+OpenRouter
stack and would burn tokens in CI.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kb.kb_graph import _detect_available_llm_route, _graphify_enabled


pytestmark = pytest.mark.asyncio


def _proj_doc(factory_enabled: bool, app_key: str = "sk-fac-xxx"):
    if factory_enabled:
        return {"settings": {"factory_orchestrator": {"enabled": True, "app_key": app_key}}}
    return {"settings": {}}


# ---------- _detect_available_llm_route ----------


async def test_route_factory_when_orchestrator_enabled(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("db.projects.find_one", AsyncMock(return_value=_proj_doc(True))), \
         patch("db.model_providers.count_documents", AsyncMock(return_value=0)):
        r = await _detect_available_llm_route("pid-1")
    assert r == "factory"


async def test_route_console_when_provider_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("db.projects.find_one", AsyncMock(return_value=_proj_doc(False))), \
         patch("db.model_providers.count_documents", AsyncMock(return_value=1)):
        r = await _detect_available_llm_route("pid-2")
    assert r == "console"


async def test_route_env_when_only_openrouter_key_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    with patch("db.projects.find_one", AsyncMock(return_value=_proj_doc(False))), \
         patch("db.model_providers.count_documents", AsyncMock(return_value=0)):
        r = await _detect_available_llm_route("pid-3")
    assert r == "env"


async def test_route_none_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with patch("db.projects.find_one", AsyncMock(return_value=_proj_doc(False))), \
         patch("db.model_providers.count_documents", AsyncMock(return_value=0)):
        r = await _detect_available_llm_route("pid-4")
    assert r == "none"


# ---------- _graphify_enabled (toggle gating) ----------


async def test_graphify_disabled_by_explicit_override(monkeypatch):
    monkeypatch.setenv("LAMA_GRAPHIFY_ENRICH", "1")
    assert await _graphify_enabled("pid-x", override=False) is False


async def test_graphify_disabled_when_project_toggle_off(monkeypatch):
    monkeypatch.setenv("LAMA_GRAPHIFY_ENRICH", "1")
    with patch("kb.graph_config.is_graph_kb_enabled", AsyncMock(return_value=False)):
        assert await _graphify_enabled("pid-x") is False


async def test_graphify_enabled_when_project_toggle_on_and_env_truthy(monkeypatch):
    monkeypatch.setenv("LAMA_GRAPHIFY_ENRICH", "1")
    with patch("kb.graph_config.is_graph_kb_enabled", AsyncMock(return_value=True)):
        assert await _graphify_enabled("pid-x") is True


async def test_graphify_disabled_when_env_falsy(monkeypatch):
    monkeypatch.setenv("LAMA_GRAPHIFY_ENRICH", "0")
    with patch("kb.graph_config.is_graph_kb_enabled", AsyncMock(return_value=True)):
        assert await _graphify_enabled("pid-x") is False


