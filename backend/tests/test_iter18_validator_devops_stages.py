"""
iter-18 — Two stages wired into the multi-agent Transformer.

1. Validator: gates the Planner's task list BEFORE any coder token is
   spent. Was seeded with a prompt + Console row since iter-16 but had no
   entry in AGENT_PROMPT_KEYS, so `_get_effective_prompt` resolved it to
   "" and the pipeline never called it.
2. DevOps dependency audit: proactive production-readiness check of the
   GENERATED build manifests after compilation — distinct from the
   devops_expert escalation persona inside the compile-fix loop.

Both gates are non-fatal by contract: a REJECT / not-production-ready is
surfaced, never raised, because the operator already confirmed the plan
at the iter-15.28 gate.
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

from routes import tools as T  # noqa: E402


# ── registration ──────────────────────────────────────────────────────
def test_both_agents_are_registered():
    """The bug: validator had a prompt and a Console row but no key map."""
    assert T.AGENT_PROMPT_KEYS["validator"] == "tools.transformer.validator"
    assert T.AGENT_PROMPT_KEYS["devops_audit"] == "tools.transformer.devops_audit"
    assert T.AGENT_LLM_BACKED["validator"] is True
    assert T.AGENT_LLM_BACKED["devops_audit"] is True
    assert T.AGENT_LABELS["validator"] == "Validator"


# ── Validator: deterministic gate ─────────────────────────────────────
def _stub_agent_io(monkeypatch, llm_payload=None):
    async def _log(*_a, **_kw): return "run-1"
    async def _upd(*_a, **_kw): return None
    async def _prompt(*_a, **_kw): return "SYSTEM"
    async def _model(*_a, **_kw): return None
    async def _pid(*_a, **_kw): return "proj-1"
    async def _fabric(**_kw): return {"content": llm_payload or "{}"}
    monkeypatch.setattr(T, "_log_agent_run", _log)
    monkeypatch.setattr(T, "_update_agent_run", _upd)
    monkeypatch.setattr(T, "_get_effective_prompt", _prompt)
    monkeypatch.setattr(T, "_get_effective_model", _model)
    monkeypatch.setattr(T, "_project_id_for_transform", _pid)
    monkeypatch.setattr(T, "fabric_call", _fabric)

    class _Tx:
        updates: List[Dict[str, Any]] = []
        async def update_one(self, q, u, **_kw):
            _Tx.updates.append(u)
            class R: modified_count = 1
            return R()
    _Tx.updates = []
    monkeypatch.setattr(T, "transformations", _Tx())
    return _Tx


@pytest.mark.asyncio
async def test_validator_flags_duplicate_targets(monkeypatch):
    _stub_agent_io(monkeypatch, '{"verdict":"ACCEPT","issues":[],"summary":"ok"}')
    tasks = [
        {"task_id": "T1", "target_path": "src/A.java", "envelope_id": "E1", "wave": 0},
        {"task_id": "T2", "target_path": "src/A.java", "envelope_id": "E1", "wave": 0},
    ]
    out = await T._run_validator("tx1", tasks, [{"envelope_id": "E1"}], None)

    assert out["verdict"] == "REJECT", "a duplicate target silently overwrites work"
    assert any("Duplicate target_path" in d["issue"] for d in out["deterministic"])


@pytest.mark.asyncio
async def test_validator_flags_uncovered_envelope(monkeypatch):
    _stub_agent_io(monkeypatch, '{"verdict":"ACCEPT","issues":[],"summary":"ok"}')
    tasks = [{"task_id": "T1", "target_path": "src/A.java", "envelope_id": "E1", "wave": 0}]
    envelopes = [{"envelope_id": "E1"}, {"envelope_id": "E2"}]
    out = await T._run_validator("tx1", tasks, envelopes, None)

    assert "E2" in out["envelopes_without_task"]
    assert out["verdict"] == "ACCEPT", "a dropped envelope is MAJOR, not CRITICAL"


@pytest.mark.asyncio
async def test_validator_deterministic_beats_the_model(monkeypatch):
    """A CRITICAL structural defect cannot be voted away by the LLM."""
    _stub_agent_io(monkeypatch, '{"verdict":"ACCEPT","issues":[],"summary":"looks great"}')
    tasks = [{"task_id": "T1", "target_path": "", "envelope_id": "E1", "wave": 0}]
    out = await T._run_validator("tx1", tasks, [{"envelope_id": "E1"}], None)
    assert out["verdict"] == "REJECT"


@pytest.mark.asyncio
async def test_validator_survives_llm_failure(monkeypatch):
    """The deterministic gate must still stand when the model is down."""
    _stub_agent_io(monkeypatch)
    async def _boom(**_kw): raise RuntimeError("provider down")
    monkeypatch.setattr(T, "fabric_call", _boom)

    tasks = [{"task_id": "T1", "target_path": "src/A.java", "envelope_id": "E1", "wave": 0}]
    out = await T._run_validator("tx1", tasks, [{"envelope_id": "E1"}], None)
    assert out["verdict"] == "ACCEPT"
    assert "unavailable" in out["summary"]


# ── DevOps dependency audit ───────────────────────────────────────────
def _stub_files(monkeypatch, files):
    class _Cursor:
        def __init__(self, rows): self.rows = rows
        def __aiter__(self):
            self._it = iter(self.rows); return self
        async def __anext__(self):
            try: return next(self._it)
            except StopIteration: raise StopAsyncIteration
    class _TF:
        def find(self, *_a, **_kw): return _Cursor(files)
    monkeypatch.setattr(T, "transform_files", _TF())


def test_manifest_collection_picks_build_files():
    got = T._collect_manifests([
        {"path": "pom.xml", "content": ""},
        {"path": "web/package.json", "content": ""},
        {"path": "Api.csproj", "content": ""},
        {"path": "src/Main.java", "content": ""},
    ])
    assert sorted(m["path"] for m in got) == ["Api.csproj", "pom.xml", "web/package.json"]


@pytest.mark.asyncio
async def test_devops_flags_unpinned_and_floating(monkeypatch):
    _stub_agent_io(monkeypatch, '{"production_ready":true,"findings":[],"summary":"fine"}')
    _stub_files(monkeypatch, [
        {"path": "requirements.txt", "content": "fastapi==0.110.1\nrequests\n"},
        {"path": "package.json", "content": '{"dependencies":{"react":"latest"}}'},
    ])
    out = await T._run_devops_dependency_check("tx1", {"compilation_ready": True}, None)

    issues = " ".join(d["issue"] for d in out["deterministic"])
    assert "requests" in issues, "unpinned pip dependency must be reported"
    assert "react" in issues, "floating npm version must be reported"
    assert out["production_ready"] is False, "a CRITICAL finding blocks production-ready"


@pytest.mark.asyncio
async def test_devops_green_build_clean_manifests_is_production_ready(monkeypatch):
    _stub_agent_io(monkeypatch, '{"production_ready":true,"findings":[],"summary":"clean"}')
    _stub_files(monkeypatch, [
        {"path": "requirements.txt", "content": "fastapi==0.110.1\nhttpx==0.28.1\n"},
    ])
    out = await T._run_devops_dependency_check("tx1", {"compilation_ready": True}, None)
    assert out["deterministic"] == []
    assert out["production_ready"] is True


@pytest.mark.asyncio
async def test_devops_red_build_is_never_production_ready(monkeypatch):
    """A failing compile cannot be overridden by a cheerful model."""
    _stub_agent_io(monkeypatch, '{"production_ready":true,"findings":[],"summary":"fine"}')
    _stub_files(monkeypatch, [
        {"path": "requirements.txt", "content": "fastapi==0.110.1\n"},
    ])
    out = await T._run_devops_dependency_check("tx1", {"compilation_ready": False}, None)
    assert out["production_ready"] is False


@pytest.mark.asyncio
async def test_devops_reports_missing_manifest(monkeypatch):
    _stub_agent_io(monkeypatch, '{"production_ready":false,"findings":[],"summary":"none"}')
    _stub_files(monkeypatch, [{"path": "src/Main.java", "content": "class A{}"}])
    out = await T._run_devops_dependency_check("tx1", {"compilation_ready": True}, None)
    assert any("No build manifest" in d["issue"] for d in out["deterministic"])
