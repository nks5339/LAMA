"""iter-14.21 — CodeGen fixes: merge cleanup, Ollama cloud routing,
LangGraph confidence loop.

Tests are unit-level and mock Mongo + HF so they run in the standard
pytest suite without a live backend.
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make `backend/` importable when pytest is launched from repo root.
_HERE = os.path.dirname(__file__)
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# db.py requires MONGO_URL / DB_NAME at import time. Match the convention
# used by test_iter1414_confidence_langgraph.py.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")


# ══════════════════════════════════════════════════════════════════════
# Fix #2 — Ollama cloud endpoint resolver
# ══════════════════════════════════════════════════════════════════════
class TestOllamaCloudRouting:
    def test_local_url_stays_local_without_key(self):
        from fabric.model_fabric import _resolve_ollama_endpoint
        provider = {"base_url": "http://localhost:11434/v1", "api_key": ""}
        url, is_cloud = _resolve_ollama_endpoint(provider, "llama3.1:8b")
        assert not is_cloud
        assert "localhost" in url

    def test_cloud_tag_routes_to_cloud(self):
        from fabric.model_fabric import _resolve_ollama_endpoint
        provider = {"base_url": "http://localhost:11434/v1", "api_key": "sk-x"}
        url, is_cloud = _resolve_ollama_endpoint(provider, "gpt-oss:120b-cloud")
        assert is_cloud
        assert "ollama.com" in url

    def test_api_key_with_localhost_url_switches_to_cloud(self):
        """The most common field failure: user sets OLLAMA_API_KEY but
        forgets to change base_url away from localhost."""
        from fabric.model_fabric import _resolve_ollama_endpoint
        provider = {"base_url": "http://localhost:11434/v1", "api_key": "key123"}
        url, is_cloud = _resolve_ollama_endpoint(provider, "qwen3-coder:30b")
        assert is_cloud
        assert "ollama.com" in url

    def test_ollama_com_url_treated_as_cloud(self):
        from fabric.model_fabric import _resolve_ollama_endpoint
        provider = {"base_url": "https://ollama.com/v1", "api_key": "k"}
        url, is_cloud = _resolve_ollama_endpoint(provider, "llama3:latest")
        assert is_cloud
        assert url == "https://ollama.com/v1"

    def test_env_flag_forces_cloud(self, monkeypatch):
        from fabric.model_fabric import _resolve_ollama_endpoint
        monkeypatch.setenv("LAMA_OLLAMA_CLOUD", "1")
        provider = {"base_url": "http://localhost:11434/v1", "api_key": ""}
        _url, is_cloud = _resolve_ollama_endpoint(provider, "llama3:latest")
        assert is_cloud

    @pytest.mark.asyncio
    async def test_resolve_model_sets_bearer_for_cloud_ollama(self):
        """Full resolve_model contract: cloud Ollama gets Authorization
        header and the base_url is rewritten to ollama.com."""
        from fabric import model_fabric

        provider = {
            "id": "p1", "provider_type": "ollama", "is_active": True,
            "is_default": True,
            "base_url": "http://localhost:11434/v1",
            "api_key": "test-key-xyz",
            "routing": {"low": "gpt-oss:20b-cloud",
                        "medium": "gpt-oss:20b-cloud",
                        "high": "gpt-oss:120b-cloud"},
            "models": [{"id": "gpt-oss:20b-cloud"}],
        }
        agent = {"key": "codegen.service", "complexity": "medium"}

        ac_col = MagicMock()
        ac_col.find_one = AsyncMock(return_value=agent)
        ac_col.update_one = AsyncMock()
        mp_col = MagicMock()
        mp_col.find_one = AsyncMock(return_value=provider)
        with patch("db.agent_configs", ac_col), patch("db.model_providers", mp_col):
            model_id, base_url, headers = await model_fabric.resolve_model("codegen.service")

        assert model_id == "gpt-oss:20b-cloud"
        assert "ollama.com" in base_url
        assert headers.get("Authorization") == "Bearer test-key-xyz"


# ══════════════════════════════════════════════════════════════════════
# Fix #3 — LangGraph confidence loop
# ══════════════════════════════════════════════════════════════════════
class TestConfidenceLoop:
    def test_default_max_iterations_is_three(self, monkeypatch):
        monkeypatch.delenv("LAMA_CODEGEN_MAX_ITERATIONS", raising=False)
        from codegen.confidence_graph import default_max_iterations
        assert default_max_iterations() == 3

    def test_default_max_iterations_env_override(self, monkeypatch):
        monkeypatch.setenv("LAMA_CODEGEN_MAX_ITERATIONS", "7")
        from codegen.confidence_graph import default_max_iterations
        assert default_max_iterations() == 7

    def test_default_max_iterations_clamped(self, monkeypatch):
        monkeypatch.setenv("LAMA_CODEGEN_MAX_ITERATIONS", "99")
        from codegen.confidence_graph import default_max_iterations
        assert default_max_iterations() == 10

    @pytest.mark.asyncio
    async def test_loop_converges_at_threshold(self):
        from codegen.confidence_graph import run_confidence_loop
        # Returns 96% first shot → should converge on iter 1, no regen.
        score_calls = []
        regen_calls = []

        async def score_fn(it):
            score_calls.append(it)
            return {"overall_score": 96.0, "targets": [], "below_threshold": 0}

        async def regen_fn(_):
            regen_calls.append(True)
            return {"applied": 0, "failed": 0}

        r = await run_confidence_loop(
            threshold=95.0, max_iterations=3,
            score_fn=score_fn, regen_fn=regen_fn,
        )
        assert r.converged is True
        assert r.final_score == 96.0
        assert len(score_calls) == 1
        assert len(regen_calls) == 0

    @pytest.mark.asyncio
    async def test_loop_stops_at_max_iterations(self):
        """Below-threshold indefinitely → stop at exactly max_iterations."""
        from codegen.confidence_graph import run_confidence_loop
        score_calls = []

        async def score_fn(it):
            score_calls.append(it)
            return {"overall_score": 80.0,
                    "targets": [{"file_path": "a.java", "score": 60}],
                    "below_threshold": 1}

        async def regen_fn(_):
            return {"applied": 1, "failed": 0}

        r = await run_confidence_loop(
            threshold=95.0, max_iterations=3,
            score_fn=score_fn, regen_fn=regen_fn,
        )
        assert r.converged is False
        # score_fn is called once per iteration → exactly max_iterations.
        assert len(score_calls) == 3
        assert r.iterations[-1].route_taken in ("max_iter", "converged")

    @pytest.mark.asyncio
    async def test_loop_stops_when_no_targets(self):
        from codegen.confidence_graph import run_confidence_loop
        calls = {"score": 0, "regen": 0}

        async def score_fn(_):
            calls["score"] += 1
            return {"overall_score": 90.0, "targets": [], "below_threshold": 0}

        async def regen_fn(_):
            calls["regen"] += 1
            return {"applied": 0, "failed": 0}

        r = await run_confidence_loop(
            threshold=95.0, max_iterations=3,
            score_fn=score_fn, regen_fn=regen_fn,
        )
        assert r.converged is False    # 90 < 95
        assert calls["score"] == 1     # empty targets → break immediately
        assert calls["regen"] == 0

    @pytest.mark.asyncio
    async def test_loop_improves_across_iterations(self):
        """Simulate improvement: 80 → 90 → 97 (converges on iter 3)."""
        from codegen.confidence_graph import run_confidence_loop
        scores = iter([80.0, 90.0, 97.0])

        async def score_fn(_):
            v = next(scores)
            return {
                "overall_score": v,
                "targets": [{"file_path": "x.java"}] if v < 95 else [],
                "below_threshold": 1 if v < 95 else 0,
            }

        async def regen_fn(_):
            return {"applied": 1, "failed": 0}

        r = await run_confidence_loop(
            threshold=95.0, max_iterations=3,
            score_fn=score_fn, regen_fn=regen_fn,
        )
        assert r.converged is True
        assert r.final_score == 97.0
        # 3 score calls, 2 regen calls (iters 1 and 2)
        assert sum(1 for it in r.iterations if it.route_taken == "regenerated") >= 1


# ══════════════════════════════════════════════════════════════════════
# Fix #3 — HF scorer graceful fallback
# ══════════════════════════════════════════════════════════════════════
class TestHFConfidenceFallback:
    @pytest.mark.asyncio
    async def test_score_file_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("LAMA_CODEGEN_HF_ENABLED", "0")
        from codegen import hf_confidence
        hf_confidence.reset_cache()
        v = await hf_confidence.score_file("proj1", "public class Foo {}")
        assert v is None

    @pytest.mark.asyncio
    async def test_score_file_returns_none_when_no_corpus(self, monkeypatch):
        """No kb_chunks for the project → returns None (not 0), so the
        semantic axis is treated as pass-through."""
        monkeypatch.setenv("LAMA_CODEGEN_HF_ENABLED", "1")
        from codegen import hf_confidence
        hf_confidence.reset_cache()

        fake_col = MagicMock()
        fake_col.find = MagicMock(return_value=MagicMock(
            limit=MagicMock(return_value=MagicMock(
                to_list=AsyncMock(return_value=[])))))
        with patch("db.kb_chunks", fake_col):
            v = await hf_confidence.score_file("no-corpus-proj", "code")
        assert v is None


# ══════════════════════════════════════════════════════════════════════
# Fix #1 — Merge cleanup helper
# ══════════════════════════════════════════════════════════════════════
class TestMergeCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_deletes_source_service_files(self):
        from routes import architecture

        # Fake collections.
        codegen_files_mock = MagicMock()
        codegen_files_mock.delete_many = AsyncMock(
            return_value=SimpleNamespace(deleted_count=42),
        )
        runs_cursor = MagicMock()
        runs_cursor.sort = MagicMock(return_value=runs_cursor)
        runs_cursor.to_list = AsyncMock(return_value=[])
        codegen_runs_mock = MagicMock()
        codegen_runs_mock.find = MagicMock(return_value=runs_cursor)
        codegen_runs_mock.update_one = AsyncMock()
        audit_mock = MagicMock()
        audit_mock.insert_one = AsyncMock()

        with patch.object(architecture, "codegen_files", codegen_files_mock), \
             patch.object(architecture, "codegen_runs", codegen_runs_mock), \
             patch.object(architecture, "audit_log", audit_mock):
            stats = await architecture._cleanup_merged_service_artifacts(
                "proj1", ["svc-a", "svc-b", "svc-c", "svc-d"], "ceots",
            )

        assert stats["codegen_files_deleted"] == 42
        # Delete filter should target the 4 source names (merged_name not
        # in source list so all 4 are victims).
        args, _ = codegen_files_mock.delete_many.call_args
        assert set(args[0]["service_name"]["$in"]) == {
            "svc-a", "svc-b", "svc-c", "svc-d",
        }
        audit_mock.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_preserves_merged_name(self):
        """When merged_name is one of the source names, we must not
        delete files under that name."""
        from routes import architecture

        codegen_files_mock = MagicMock()
        codegen_files_mock.delete_many = AsyncMock(
            return_value=SimpleNamespace(deleted_count=0),
        )
        runs_cursor = MagicMock()
        runs_cursor.sort = MagicMock(return_value=runs_cursor)
        runs_cursor.to_list = AsyncMock(return_value=[])
        codegen_runs_mock = MagicMock()
        codegen_runs_mock.find = MagicMock(return_value=runs_cursor)
        audit_mock = MagicMock()
        audit_mock.insert_one = AsyncMock()

        with patch.object(architecture, "codegen_files", codegen_files_mock), \
             patch.object(architecture, "codegen_runs", codegen_runs_mock), \
             patch.object(architecture, "audit_log", audit_mock):
            await architecture._cleanup_merged_service_artifacts(
                "proj1", ["ceots", "svc-b", "svc-c"], "ceots",
            )

        args, _ = codegen_files_mock.delete_many.call_args
        assert "ceots" not in args[0]["service_name"]["$in"]
        assert set(args[0]["service_name"]["$in"]) == {"svc-b", "svc-c"}
