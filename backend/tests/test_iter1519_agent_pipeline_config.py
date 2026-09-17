"""
iter-15.19 — Tests for the Code Transformer "Agent Pipeline" click-through
config panel: viewing/editing/saving a per-transformation prompt+model
override for one agent (Super Agent / Context Manager / Planner / Coder /
Verifier / Tester), and rerunning the pipeline afterward.

These tests exercise the actual FastAPI endpoints in routes/tools.py via
TestClient, with every Mongo collection touched replaced by a lightweight
in-memory fake (same pattern as test_iter13100_agent_memory.py) so no
running Mongo or LLM/network access is required.
"""
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


# ──────────────────────────────────────────────────────────────────────
# In-memory fake collection (mirrors the bits of motor routes/tools.py
# actually calls: find_one, find, insert_one, update_one, delete_one,
# delete_many).
# ──────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def sort(self, *_args, **_kwargs) -> "_FakeCursor":
        return self

    async def to_list(self, _n=None):
        return list(self._rows)

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self) -> None:
        self.docs: List[Dict[str, Any]] = []

    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find_one(self, q: Dict[str, Any], _proj: Dict[str, Any] = None):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    def find(self, q: Dict[str, Any] = None, _proj: Dict[str, Any] = None) -> _FakeCursor:
        q = q or {}
        rows = [dict(d) for d in self.docs if self._match(d, q)]
        return _FakeCursor(rows)

    async def insert_one(self, doc: Dict[str, Any]):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("_id", "")})

    async def update_one(self, q: Dict[str, Any], upd: Dict[str, Any], upsert: bool = False):
        for d in self.docs:
            if self._match(d, q):
                if "$set" in upd:
                    d.update(upd["$set"])
                return
        if upsert:
            new = dict(q)
            if "$set" in upd:
                new.update(upd["$set"])
            self.docs.append(new)

    async def delete_one(self, q: Dict[str, Any]):
        for i, d in enumerate(self.docs):
            if self._match(d, q):
                del self.docs[i]
                return type("R", (), {"deleted_count": 1})
        return type("R", (), {"deleted_count": 0})

    async def delete_many(self, q: Dict[str, Any]):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, q)]
        return type("R", (), {"deleted_count": before - len(self.docs)})


@pytest.fixture
def client(monkeypatch):
    import db
    import routes.tools as tools_mod

    fake_prompts = FakeCollection()
    fake_transformations = FakeCollection()
    fake_agent_configs = FakeCollection()
    fake_envelopes = FakeCollection()
    fake_tasks = FakeCollection()
    fake_agent_runs = FakeCollection()
    fake_transform_files = FakeCollection()
    fake_audit = FakeCollection()
    fake_tools_kb = FakeCollection()

    fake_prompts.docs.append({
        "key": "tools.transformer.context_manager",
        "template": "GLOBAL DEFAULT CONTEXT MANAGER PROMPT",
        "description": "Discovers API/DB structure.",
    })
    fake_transformations.docs.append({
        "_id": "tx-1", "name": "demo", "status": "completed", "model": "",
    })

    for mod in (db, tools_mod):
        monkeypatch.setattr(mod, "prompts", fake_prompts, raising=True)
        monkeypatch.setattr(mod, "transformations", fake_transformations, raising=True)
        monkeypatch.setattr(mod, "transformer_agent_configs", fake_agent_configs, raising=True)
        monkeypatch.setattr(mod, "transformer_envelopes", fake_envelopes, raising=True)
        monkeypatch.setattr(mod, "transformer_tasks", fake_tasks, raising=True)
        monkeypatch.setattr(mod, "transformer_agent_runs", fake_agent_runs, raising=True)
        monkeypatch.setattr(mod, "transform_files", fake_transform_files, raising=True)
        monkeypatch.setattr(mod, "audit_log", fake_audit, raising=True)
        monkeypatch.setattr(mod, "tools_kb", fake_tools_kb, raising=True)

    # Rerun kicks off a background task that calls _run_multi_agent_transformation
    # (which itself calls fabric_call/LLM). Replace it with a no-op so the
    # test only asserts on the rerun endpoint's synchronous side effects
    # (clearing collections + resetting transformation status).
    async def _noop(*_args, **_kwargs):
        return None
    monkeypatch.setattr(tools_mod, "_run_multi_agent_transformation", _noop, raising=True)

    app = FastAPI()
    app.include_router(tools_mod.router)
    return TestClient(app)


def test_list_agents_returns_the_full_roster_with_global_default(client):
    """The roster is eleven agents.

    History of this assertion -- each expansion was deliberate:
      • six  -> seven (iter-15.62) `devops_expert`, the stagnation-triggered
        escalation persona inside the compile-fix loop.
      • seven -> nine (iter-18):
          - `validator`    the plan gate between Planner and Coder. It had
            been seeded with a prompt and a Console row since iter-16 but
            was missing from AGENT_PROMPT_KEYS, so `_get_effective_prompt`
            resolved it to "" and the pipeline never invoked it.
          - `devops_audit` the DevOps agent's proactive mode: a dependency
            /production-readiness audit of the GENERATED build manifests
            after compilation, distinct from the escalation persona above.
      • nine -> ten (iter-20): `regenerator`, the compile-fix loop's last
        escalation rung. The loop used to stop after ONE escalation --
        coder, devops_expert, give up -- which is the "not fixed in 2
        iterations" the operator reported. Rungs 0-2 all EDIT a file that
        may be past saving; this one rewrites it from the legacy original
        against the target stack's playbook.
      • ten -> eleven (iter-22): `diagnostician`, which reads a wall of raw
        build output and works out what actually broke. Same defect shape as
        `validator` above, one layer along: iter-19 gave it an
        AGENT_COMPLEXITY tier (`reasoning`, for the o-series) but no prompt
        and no entry in AGENT_PROMPT_KEYS, so it ran on the PLANNER's 9.5 KB
        wave-ordering plan rubric — a planning prompt doing a diagnosis job.
        Its own call site said so: "it kept the Planner's key only because
        there was no better one".
    """
    res = client.get("/tools/transformer/tx-1/agents")
    assert res.status_code == 200
    agents = {a["agent"]: a for a in res.json()["agents"]}
    assert set(agents.keys()) == {
        "super_agent", "context_manager", "planner", "coder", "verifier",
        "tester", "devops_expert", "validator", "devops_audit", "regenerator",
        "diagnostician",
    }
    assert agents["validator"]["llm_backed"] is True
    assert agents["regenerator"]["llm_backed"] is True
    assert agents["devops_audit"]["llm_backed"] is True
    cm = agents["context_manager"]
    assert cm["base_template"] == "GLOBAL DEFAULT CONTEXT MANAGER PROMPT"
    assert cm["effective_template"] == "GLOBAL DEFAULT CONTEXT MANAGER PROMPT"
    assert cm["is_overridden"] is False
    assert agents["super_agent"]["llm_backed"] is False
    assert agents["coder"]["llm_backed"] is True


def test_save_override_scoped_to_transformation_only(client):
    res = client.put(
        "/tools/transformer/tx-1/agents/context_manager",
        json={"prompt_template": "CUSTOM PROMPT FOR TX-1 ONLY", "model": "gpt-4o-mini"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["is_overridden"] is True
    assert body["effective_template"] == "CUSTOM PROMPT FOR TX-1 ONLY"
    assert body["effective_model"] == "gpt-4o-mini"

    # The shared global Prompt Library default must be untouched.
    global_check = client.get("/tools/transformer/tx-1/agents/planner")
    assert global_check.status_code == 200
    # planner has no override saved — should still report is_overridden False
    assert global_check.json()["is_overridden"] is False

    import routes.tools as tools_mod
    global_doc = None
    for d in tools_mod.prompts.docs:
        if d["key"] == "tools.transformer.context_manager":
            global_doc = d
    assert global_doc["template"] == "GLOBAL DEFAULT CONTEXT MANAGER PROMPT"


def test_reset_clears_override(client):
    client.put(
        "/tools/transformer/tx-1/agents/coder",
        json={"prompt_template": "TEMP OVERRIDE", "model": "claude-3"},
    )
    res = client.put("/tools/transformer/tx-1/agents/coder", json={"prompt_template": "", "model": ""})
    assert res.status_code == 200
    body = res.json()
    assert body["is_overridden"] is False
    assert body["effective_template"] == ""  # no global default seeded for "coder" in this fixture


def test_save_unknown_agent_404(client):
    res = client.put("/tools/transformer/tx-1/agents/not_a_real_agent", json={"prompt_template": "x", "model": ""})
    assert res.status_code == 404


def test_rerun_clears_pipeline_state_and_resets_status(client):
    import routes.tools as tools_mod
    tools_mod.transformer_envelopes.docs.append({"transform_id": "tx-1", "envelope_id": "E1"})
    tools_mod.transformer_tasks.docs.append({"transform_id": "tx-1", "task_id": "T1"})
    tools_mod.transformer_agent_runs.docs.append({"transform_id": "tx-1", "agent": "planner"})
    tools_mod.transform_files.docs.append({"transform_id": "tx-1", "type": "transformed", "path": "a.java"})
    tools_mod.transform_files.docs.append({"transform_id": "tx-1", "type": "source", "path": "src.java"})

    res = client.post("/tools/transformer/tx-1/rerun")
    assert res.status_code == 202
    assert res.json()["status"] == "started"

    assert tools_mod.transformer_envelopes.docs == []
    assert tools_mod.transformer_tasks.docs == []
    assert tools_mod.transformer_agent_runs.docs == []
    # Source files must survive a rerun — only transformed/error files are cleared.
    remaining_types = {d["type"] for d in tools_mod.transform_files.docs}
    assert remaining_types == {"source"}

    tx = tools_mod.transformations.docs[0]
    assert tx["status"] == "running"
    assert tx["phase"] == "super_agent"


def test_rerun_rejects_when_already_running(client):
    import routes.tools as tools_mod
    tools_mod.transformations.docs[0]["status"] = "running"
    res = client.post("/tools/transformer/tx-1/rerun")
    assert res.status_code == 400


def test_rerun_unknown_transform_404(client):
    res = client.post("/tools/transformer/does-not-exist/rerun")
    assert res.status_code == 404
