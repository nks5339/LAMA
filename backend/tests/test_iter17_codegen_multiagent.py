"""
iter-17 — Multi-Agent CodeGen Pipeline (routes/codegen.py).

Endpoints under test:
  POST   /codegen/{pid}/multi-agent/start
  GET    /codegen/{pid}/multi-agent/state
  GET    /codegen/{pid}/multi-agent/envelopes
  PATCH  /codegen/{pid}/multi-agent/envelopes/{envelope_id}
  POST   /codegen/{pid}/multi-agent/envelopes/confirm
  POST   /codegen/{pid}/multi-agent/build-system
  GET    /codegen/{pid}/multi-agent/tasks
  PATCH  /codegen/{pid}/multi-agent/tasks/{task_id}
  POST   /codegen/{pid}/multi-agent/tasks/confirm
  GET    /codegen/{pid}/multi-agent/agent-runs
  POST   /codegen/{pid}/multi-agent/rerun
  POST   /codegen/{pid}/multi-agent/cancel
  GET    /codegen/{pid}/multi-agent/traceability

Fixture pattern mirrors backend/tests/test_iter1541_traceability_gate.py.
All Mongo collections replaced with FakeCollection; `chat_completion`
monkeypatched onto the codegen module so nothing hits a real LLM.
"""
import json
import os
import sys
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")


# ── Fake Mongo collection (async, in-memory) ──────────────────────────────
class _FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = list(rows)

    def sort(self, *_a, **_kw):
        return self

    def skip(self, n):
        self._rows = self._rows[n:]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    async def to_list(self, _n=None):
        return list(self._rows)


class FakeCollection:
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    def _match(self, r: Dict[str, Any], q: Dict[str, Any]) -> bool:
        for k, v in (q or {}).items():
            if r.get(k) != v:
                return False
        return True

    async def insert_one(self, doc):
        self.rows.append(dict(doc))

        class R:
            inserted_id = doc.get("_id")

        return R()

    async def find_one(self, q=None, *_a, **_kw):
        q = q or {}
        for r in self.rows:
            if self._match(r, q):
                return dict(r)
        return None

    def find(self, q=None, *_a, **_kw):
        q = q or {}
        return _FakeCursor([r for r in self.rows if self._match(r, q)])

    async def update_one(self, q, update, upsert=False):
        for r in self.rows:
            if self._match(r, q or {}):
                r.update((update or {}).get("$set", {}))

                class R:
                    matched_count = 1
                    modified_count = 1

                return R()
        if upsert:
            doc = dict(q or {})
            doc.update((update or {}).get("$set", {}))
            self.rows.append(doc)

            class R:
                matched_count = 0
                modified_count = 0
                upserted_id = "upserted"

            return R()

        class R:
            matched_count = 0
            modified_count = 0

        return R()

    async def update_many(self, q, update):
        n = 0
        for r in self.rows:
            if self._match(r, q or {}):
                r.update((update or {}).get("$set", {}))
                n += 1

        class R:
            matched_count = n
            modified_count = n

        return R()

    async def delete_many(self, q):
        before = len(self.rows)
        self.rows = [r for r in self.rows if not self._match(r, q or {})]

        class R:
            deleted_count = before - len(self.rows)

        return R()

    async def count_documents(self, q=None):
        q = q or {}
        return sum(1 for r in self.rows if self._match(r, q))

    async def create_index(self, *_a, **_kw):
        return "idx"


# ── Fabric call recorder ──────────────────────────────────────────────────
class FabricRecorder:
    """Records every fabric_call args and returns canned JSON per agent_key."""

    def __init__(self, canned: Dict[str, Any]):
        # canned: agent_key → dict OR list of dicts OR callable(args, kwargs) → dict
        self.canned = canned
        self.calls: List[Dict[str, Any]] = []
        self._call_counts: Dict[str, int] = {}

    async def __call__(self, *args, **kwargs):
        agent_key = kwargs.get("agent_key", "")
        self._call_counts[agent_key] = self._call_counts.get(agent_key, 0) + 1
        self.calls.append({
            "agent_key": agent_key,
            "project_id": kwargs.get("project_id", ""),
            "messages": kwargs.get("messages", []),
            "model": kwargs.get("model"),
        })
        payload = self.canned.get(agent_key)
        if callable(payload):
            payload = payload(args, kwargs)
        if payload is None:
            payload = {}
        # Some tests inject a raw string (refusal envelope).
        if isinstance(payload, str):
            return {"content": payload}
        return {"content": json.dumps(payload)}

    def agent_keys_seen(self) -> set:
        return set(self._call_counts.keys())


# ── Fixture ───────────────────────────────────────────────────────────────
@pytest.fixture
def rig(monkeypatch):
    """Returns (TestClient, module, recorder, fakes-dict)."""
    from routes import codegen as cg

    fakes = {
        "projects": FakeCollection(),
        "audit_log": FakeCollection(),
        "stage_context": FakeCollection(),
        "codegen_envelopes": FakeCollection(),
        "codegen_tasks": FakeCollection(),
        "codegen_agent_runs": FakeCollection(),
        "codegen_pipeline_state": FakeCollection(),
        "codegen_files": FakeCollection(),
        "prompts_col": FakeCollection(),
    }
    monkeypatch.setattr(cg, "projects", fakes["projects"])
    monkeypatch.setattr(cg, "audit_log", fakes["audit_log"])
    monkeypatch.setattr(cg, "stage_context_col", fakes["stage_context"])
    monkeypatch.setattr(cg, "codegen_envelopes", fakes["codegen_envelopes"])
    monkeypatch.setattr(cg, "codegen_tasks", fakes["codegen_tasks"])
    monkeypatch.setattr(cg, "codegen_agent_runs", fakes["codegen_agent_runs"])
    monkeypatch.setattr(cg, "codegen_pipeline_state", fakes["codegen_pipeline_state"])
    monkeypatch.setattr(cg, "codegen_files", fakes["codegen_files"])
    monkeypatch.setattr(cg, "prompts_col", fakes["prompts_col"])
    # `pipeline.require_stage_context` reads through its own module-level
    # `stage_context_col` alias — patch it there too so the endpoint's
    # synchronous 400-gate is fake-backed.
    import pipeline as _pipeline
    monkeypatch.setattr(_pipeline, "stage_context_col", fakes["stage_context"])

    # Default recorder — individual tests can swap the canned map.
    recorder = FabricRecorder({})
    monkeypatch.setattr(cg, "chat_completion", recorder)

    app = FastAPI()
    app.include_router(cg.router)
    client = TestClient(app)
    return client, cg, recorder, fakes


def _seed_frozen_architecture(fakes, pid="pid-abc"):
    fakes["projects"].rows.append({
        "id": pid, "name": "PMIS", "stage_status": {"CodeGen": "available"},
    })
    fakes["stage_context"].rows.append({
        "project_id": pid, "stage": "Architecture", "version": 1,
        "outputs": {
            "services": [{"id": "user-svc", "name": "user-service"}],
            "api_contracts": [{"path": "/api/users", "method": "GET"}],
            "target_backend": {"lang": "python", "framework": "fastapi"},
            "target_frontend": {"lang": "typescript", "framework": "react"},
        },
    })
    fakes["stage_context"].rows.append({
        "project_id": pid, "stage": "Discovery", "version": 1,
        "toon_summary": "entities:\n  - User\n  - Order",
    })


def _wait_for_status(cg, fakes, pid, target, timeout=2.0):
    """Background tasks in TestClient run inline / same event loop, so the
    state should already be at target by the time the endpoint call returns.
    This helper is defensive — a couple retries handle any last-writer race."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = None
        for r in fakes["codegen_pipeline_state"].rows:
            if r.get("project_id") == pid:
                doc = r
                break
        if doc and doc.get("status") == target:
            return doc
        time.sleep(0.02)
    # Final read — return whatever we have so assertions can print it.
    for r in fakes["codegen_pipeline_state"].rows:
        if r.get("project_id") == pid:
            return r
    return None


# ══════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════
def test_start_requires_architecture_frozen(rig):
    client, cg, rec, fakes = rig
    # Project exists but Architecture StageContext is missing.
    fakes["projects"].rows.append({"id": "p1", "name": "no-arch"})
    r = client.post("/codegen/p1/multi-agent/start")
    assert r.status_code == 400, r.text
    assert "Architecture" in r.text or "frozen" in r.text.lower()


def test_start_kicks_off_context_manager(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    rec.canned = {
        "codegen.context_manager": {
            "envelopes": [
                {
                    "envelope_id": "ENV-U-001",
                    "endpoint_method": "GET", "endpoint_path": "/api/users",
                    "controller_class": "UserController",
                    "controller_file": "backend/app/controllers/user.py",
                    "layer": "controller", "side": "backend",
                    "action": "NEW", "br_ids": ["BR-101"],
                },
                {
                    "envelope_id": "ENV-U-002",
                    "endpoint_method": "SCREEN",
                    "endpoint_path": "/users",
                    "controller_class": "UsersPage",
                    "controller_file": "frontend/src/pages/Users.tsx",
                    "layer": "page", "side": "frontend",
                    "action": "NEW", "br_ids": ["BR-102"],
                },
            ],
            "stats": {"total_envelopes": 2},
        },
    }
    r = client.post("/codegen/pid-abc/multi-agent/start")
    assert r.status_code == 202, r.text
    state = _wait_for_status(cg, fakes, "pid-abc", "envelopes_pending")
    assert state is not None
    assert state["status"] == "envelopes_pending"
    envs = [e for e in fakes["codegen_envelopes"].rows if e.get("project_id") == "pid-abc"]
    assert len(envs) == 2
    # Context Manager was invoked with the right agent_key
    assert "codegen.context_manager" in rec.agent_keys_seen()


def test_envelope_confirm_triggers_planner_with_both_coders(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    # Pre-seed envelopes + state so we jump straight to the confirm step.
    fakes["codegen_envelopes"].rows.append({
        "project_id": "pid-abc", "envelope_id": "ENV-BE",
        "side": "backend", "status": "draft", "br_ids": ["BR-1"],
        "endpoint_path": "/api/users", "action": "NEW", "layer": "controller",
    })
    fakes["codegen_envelopes"].rows.append({
        "project_id": "pid-abc", "envelope_id": "ENV-FE",
        "side": "frontend", "status": "draft", "br_ids": ["BR-2"],
        "endpoint_path": "/users", "action": "NEW", "layer": "page",
    })
    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "envelopes_pending",
    })

    rec.canned = {
        "codegen.build_tool_selector": {
            "be": "pip", "fe": "vite", "rationale": "target stack",
        },
        "codegen.planner": {
            "tasks": [
                {
                    "task_id": "TASK-001", "envelope_id": "ENV-BE",
                    "title": "User controller",
                    "target_path": "backend/app/controllers/user.py",
                    "layer": "controller", "phase": "scaffold",
                    "action": "NEW", "wave": 7,
                    "assigned_to": "coder_be", "br_ids": ["BR-1"],
                },
                {
                    "task_id": "TASK-002", "envelope_id": "ENV-FE",
                    "title": "Users page",
                    "target_path": "frontend/src/pages/Users.tsx",
                    "layer": "page", "phase": "scaffold",
                    "action": "NEW", "wave": 8,
                    "assigned_to": "coder_fe", "br_ids": ["BR-2"],
                },
            ],
            "waves": [{"wave": 7, "task_count": 1}, {"wave": 8, "task_count": 1}],
            "total_tasks": 2,
        },
    }

    r = client.post("/codegen/pid-abc/multi-agent/envelopes/confirm")
    assert r.status_code == 202, r.text
    state = _wait_for_status(cg, fakes, "pid-abc", "tasks_pending")
    assert state is not None
    assert state["status"] == "tasks_pending"
    assert state.get("build_system_be") == "pip"
    assert state.get("build_system_fe") == "vite"

    tasks = [t for t in fakes["codegen_tasks"].rows if t.get("project_id") == "pid-abc"]
    assigned = sorted({t["assigned_to"] for t in tasks})
    assert assigned == ["coder_be", "coder_fe"], assigned


def test_router_heuristic_overrides_planner(rig):
    _client, cg, _rec, _fakes = rig
    r = cg._route_task_to_coder
    assert r({"target_path": "backend/app/services/foo.py", "assigned_to": "coder_fe"}) == "coder_be"
    assert r({"target_path": "frontend/src/pages/Users.jsx", "assigned_to": "coder_be"}) == "coder_fe"
    # Ambiguous — README has no BE/FE signal, keep planner suggestion.
    assert r({"target_path": "README.md", "assigned_to": "coder_fe"}) == "coder_fe"
    # No signals at all → default coder_be.
    assert r({"target_path": "", "assigned_to": ""}) == "coder_be"


def test_task_confirm_invokes_both_coders_in_parallel(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    # Seed tasks + state.
    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "tasks_pending",
    })
    fakes["codegen_tasks"].rows.append({
        "project_id": "pid-abc", "task_id": "TASK-001",
        "envelope_id": "ENV-BE", "target_path": "backend/app/svc.py",
        "layer": "service", "assigned_to": "coder_be", "wave": 4,
        "status": "PENDING", "br_ids": ["BR-1"],
    })
    fakes["codegen_tasks"].rows.append({
        "project_id": "pid-abc", "task_id": "TASK-002",
        "envelope_id": "ENV-FE", "target_path": "frontend/src/pages/A.tsx",
        "layer": "page", "assigned_to": "coder_fe", "wave": 8,
        "status": "PENDING", "br_ids": ["BR-2"],
    })

    # Canned outputs for every agent hit in the flow.
    rec.canned = {
        "codegen.coder_be": "print('hi')  # BR: BR-1",
        "codegen.coder_fe": "export default function A(){return <div/>}",
        "codegen.verifier": {"confidence": 100, "verdict": "ACCEPT", "checks": []},
        "codegen.reviewer": {"verdict": "PASS", "issues": []},
        "codegen.tester": {"compilation_ready": True, "overall_score": 100},
        "codegen.traceability_gate": {"coverage_pct": 100.0, "missing_brs": []},
        "codegen.finalizer": {"status": "completed"},
    }

    r = client.post("/codegen/pid-abc/multi-agent/tasks/confirm")
    assert r.status_code == 202, r.text
    # After task confirm the pipeline runs to completion (100% coverage).
    state = _wait_for_status(cg, fakes, "pid-abc", "completed")
    assert state is not None, "pipeline never reached completed"
    assert state["status"] == "completed"

    keys = rec.agent_keys_seen()
    assert "codegen.coder_be" in keys, keys
    assert "codegen.coder_fe" in keys, keys


def test_rerun_clears_state_and_restarts(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    # Populate some prior state.
    fakes["codegen_envelopes"].rows.append({"project_id": "pid-abc", "envelope_id": "OLD"})
    fakes["codegen_tasks"].rows.append({"project_id": "pid-abc", "task_id": "OLD-T"})
    fakes["codegen_agent_runs"].rows.append({"project_id": "pid-abc", "agent": "old"})
    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "failed", "last_error": "prev run",
    })
    # Canned CM response so background task can complete without blowing up.
    rec.canned = {"codegen.context_manager": {"envelopes": []}}

    r = client.post("/codegen/pid-abc/multi-agent/rerun")
    assert r.status_code == 202, r.text
    # Envelopes and tasks got wiped; agent_runs pre-existing row was cleared,
    # but the CM+super_agent runs from the rerun landed.
    remaining_envs = [e for e in fakes["codegen_envelopes"].rows
                      if e.get("project_id") == "pid-abc"]
    remaining_tasks = [t for t in fakes["codegen_tasks"].rows
                       if t.get("project_id") == "pid-abc"]
    assert remaining_envs == []
    assert remaining_tasks == []
    old_runs = [r for r in fakes["codegen_agent_runs"].rows
                if r.get("agent") == "old"]
    assert old_runs == [], "old agent_run row should have been wiped"
    # State is back in flight (envelopes_pending).
    state = _wait_for_status(cg, fakes, "pid-abc", "envelopes_pending")
    assert state is not None
    assert state["status"] == "envelopes_pending"


def test_be_coder_refusal_blocks_task(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "tasks_pending",
    })
    # Trap: BE coder gets a task with a FE-looking path (planner mistake
    # + router edge case). The Coder is EXPECTED to refuse.
    fakes["codegen_tasks"].rows.append({
        "project_id": "pid-abc", "task_id": "TASK-BAD",
        "envelope_id": "ENV-1",
        # Extension is ambiguous (.md), but we FORCE assigned_to=coder_be.
        "target_path": "backend/component.md",
        "layer": "controller", "assigned_to": "coder_be", "wave": 1,
        "status": "PENDING", "br_ids": [],
    })

    rec.canned = {
        "codegen.coder_be": '{"refusal": true, "reason": "FE_FILE"}',
        "codegen.verifier": {"confidence": 100, "verdict": "ACCEPT"},
        "codegen.reviewer": {"verdict": "PASS"},
        "codegen.tester": {"compilation_ready": True, "overall_score": 100},
        "codegen.traceability_gate": {"coverage_pct": 100.0, "missing_brs": []},
        "codegen.finalizer": {"status": "completed"},
    }

    r = client.post("/codegen/pid-abc/multi-agent/tasks/confirm")
    assert r.status_code == 202
    _wait_for_status(cg, fakes, "pid-abc", "completed")

    tasks = [t for t in fakes["codegen_tasks"].rows if t.get("task_id") == "TASK-BAD"]
    assert tasks, "task disappeared"
    t = tasks[0]
    assert t["status"] == "BLOCKED", t
    assert "FE_FILE" in (t.get("error") or "")


def test_traceability_gate_blocks_when_enforced(rig, monkeypatch):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    monkeypatch.setenv("LAMA_BR_ENFORCE", "1")
    monkeypatch.setenv("LAMA_BR_MIN_COVERAGE", "100")

    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "tasks_pending",
    })
    # Envelope expects TWO BRs; task only cites ONE → 50% coverage.
    fakes["codegen_envelopes"].rows.append({
        "project_id": "pid-abc", "envelope_id": "ENV-BR",
        "br_ids": ["BR-1", "BR-2"], "side": "backend",
    })
    fakes["codegen_tasks"].rows.append({
        "project_id": "pid-abc", "task_id": "TASK-1",
        "envelope_id": "ENV-BR",
        "target_path": "backend/svc.py",
        "layer": "service", "assigned_to": "coder_be", "wave": 4,
        "status": "PENDING", "br_ids": ["BR-1"],  # missing BR-2
    })

    rec.canned = {
        "codegen.coder_be": "print('x')  # BR: BR-1",
        "codegen.verifier": {"confidence": 100, "verdict": "ACCEPT"},
        "codegen.reviewer": {"verdict": "PASS"},
        "codegen.tester": {"compilation_ready": True},
        # Deterministic gate math is authoritative — we don't need to
        # canned the LLM enrichment call.
    }

    r = client.post("/codegen/pid-abc/multi-agent/tasks/confirm")
    assert r.status_code == 202

    state = _wait_for_status(cg, fakes, "pid-abc", "traceability_gate")
    assert state is not None
    assert state["status"] == "traceability_gate", state
    assert state["br_coverage_pct"] < 100.0
    assert "BR-2" in state.get("br_missing") or ["BR-2"] == list(state.get("br_missing") or [])
    # Living must NOT be unlocked.
    proj = next(p for p in fakes["projects"].rows if p.get("id") == "pid-abc")
    assert (proj.get("stage_status") or {}).get("Living") != "available"


def test_audit_log_rows_written(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    rec.canned = {"codegen.context_manager": {"envelopes": []}}
    client.post("/codegen/pid-abc/multi-agent/start")
    _wait_for_status(cg, fakes, "pid-abc", "envelopes_pending")

    # Confirm envelopes.
    rec.canned["codegen.build_tool_selector"] = {"be": "pip", "fe": "vite", "rationale": "-"}
    rec.canned["codegen.planner"] = {"tasks": [], "waves": [], "total_tasks": 0}
    client.post("/codegen/pid-abc/multi-agent/envelopes/confirm")

    actions = {r.get("action") for r in fakes["audit_log"].rows}
    assert "multi_agent_start" in actions, actions
    assert "envelopes_confirmed" in actions, actions


def test_start_returns_409_when_already_running(rig):
    client, cg, rec, fakes = rig
    _seed_frozen_architecture(fakes)
    # Pre-existing state that is neither idle nor failed nor completed.
    fakes["codegen_pipeline_state"].rows.append({
        "project_id": "pid-abc", "status": "envelopes_pending",
    })
    r = client.post("/codegen/pid-abc/multi-agent/start")
    assert r.status_code == 409, r.text
