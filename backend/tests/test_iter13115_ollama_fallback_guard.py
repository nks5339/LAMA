"""
iter-13.115 guard — the Ollama-first fallback must not re-issue the same
prompt to the provider the primary route already failed on.

`fabric_chat_with_failover` re-raises timeouts WITHOUT walking the
remaining providers, so on a timeout only the pinned/default provider was
attempted. When that provider is the same Ollama one the fallback would
select, retrying it burns a second full timeout (600s) and cannot succeed.

Pins:
  • _is_ollama_shaped  — the shared local/Ollama predicate
  • _try_ollama_fallback honours `exclude_provider_ids`
"""
import os
import sys
from typing import Any, Dict, List

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

import llm  # noqa: E402


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def sort(self, *_a, **_kw): return self
    def __aiter__(self):
        self._it = iter(self._rows)
        return self
    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeProviders:
    def __init__(self, rows): self.rows = rows
    def find(self, q=None, *_a, **_kw):
        q = q or {}
        out = [r for r in self.rows
               if all(r.get(k) == v for k, v in q.items())]
        return _FakeCursor(out)
    async def find_one(self, q=None, *_a, **_kw):
        q = q or {}
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None


class FakeAgents:
    def __init__(self, rows=None): self.rows = rows or []
    async def find_one(self, q=None, *_a, **_kw):
        q = q or {}
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None
    async def update_one(self, *_a, **_kw):
        class R: modified_count = 0
        return R()


OLLAMA = {
    "id": "prov-ollama", "name": "Ollama (local)", "provider_type": "ollama",
    "base_url": "http://localhost:11434/v1", "is_active": True,
    "is_default": True, "priority": 1,
    "routing": {"low": "qwen3:4b", "medium": "qwen3.5:latest",
                "high": "qwen3.5:latest"},
}


# ── the shared predicate ──────────────────────────────────────────────
@pytest.mark.parametrize("prov,expected", [
    (OLLAMA, True),
    ({"provider_type": "custom", "base_url": "http://127.0.0.1:8080"}, True),
    ({"provider_type": "custom", "base_url": "http://host.docker.internal:1"}, True),
    ({"provider_type": "anthropic", "base_url": "https://api.anthropic.com/v1"}, False),
    (None, False),
    ({}, False),
])
def test_is_ollama_shaped(prov, expected):
    assert llm._is_ollama_shaped(prov) is expected


# ── the guard itself ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fallback_skips_the_provider_that_just_failed(monkeypatch):
    """The regression: primary WAS Ollama and timed out → do not retry it."""
    called: List[Dict[str, Any]] = []

    async def _never(**kw):
        called.append(kw)
        return {"content": "should not happen"}

    monkeypatch.setattr("fabric.model_fabric.fabric_chat", _never, raising=False)
    monkeypatch.setitem(sys.modules, "db", type(sys)("db"))
    sys.modules["db"].model_providers = FakeProviders([OLLAMA])
    sys.modules["db"].agent_configs = FakeAgents()

    out = await llm._try_ollama_fallback(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
        project_id="p1",
        kwargs={},
        exclude_provider_ids={"prov-ollama"},
    )

    assert out is None, "fallback must decline when the only Ollama provider already failed"
    assert called == [], "fallback must not re-issue the prompt to the failed provider"


@pytest.mark.asyncio
async def test_fallback_still_runs_when_a_different_provider_failed(monkeypatch):
    """Non-regression: a cloud primary failing must still reach Ollama."""
    called: List[Dict[str, Any]] = []

    async def _ok(**kw):
        called.append(kw)
        return {"content": "generated", "model": "qwen3:4b", "usage": {}}

    monkeypatch.setattr("fabric.model_fabric.fabric_chat", _ok, raising=False)
    monkeypatch.setitem(sys.modules, "db", type(sys)("db"))
    sys.modules["db"].model_providers = FakeProviders([OLLAMA])
    sys.modules["db"].agent_configs = FakeAgents()

    async def _no_sanitize(result, *_a, **_kw):
        return result
    monkeypatch.setattr(llm, "_sanitize_response_inplace", _no_sanitize)
    monkeypatch.setattr(llm, "_check_and_upgrade_model_for_context",
                        lambda **kw: _noop_upgrade(kw), raising=False)

    out = await llm._try_ollama_fallback(
        messages=[{"role": "user", "content": "hi"}],
        agent_key="tools.transformer.coder",
        project_id="p1",
        kwargs={},
        exclude_provider_ids={"prov-anthropic"},   # a DIFFERENT provider failed
    )

    assert out is not None, "fallback must still serve when another vendor failed"
    assert len(called) == 1


async def _noop_upgrade(kw):
    return kw.get("model_id", ""), False


# ── fan-out sizing ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_active_default_provider_is_local(monkeypatch):
    monkeypatch.setitem(sys.modules, "db", type(sys)("db"))

    sys.modules["db"].model_providers = FakeProviders([OLLAMA])
    assert await llm.active_default_provider_is_local() is True

    sys.modules["db"].model_providers = FakeProviders([{
        "id": "p-anthropic", "provider_type": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "is_active": True, "is_default": True,
    }])
    assert await llm.active_default_provider_is_local() is False


@pytest.mark.asyncio
async def test_local_default_is_not_fatal_when_lookup_fails(monkeypatch):
    """Best-effort: a broken DB lookup must not raise into the caller."""
    class Boom:
        async def find_one(self, *_a, **_kw):
            raise RuntimeError("mongo down")
    monkeypatch.setitem(sys.modules, "db", type(sys)("db"))
    sys.modules["db"].model_providers = Boom()
    assert await llm.active_default_provider_is_local() is False
