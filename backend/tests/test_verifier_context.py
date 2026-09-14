"""The verifier must be given the evidence its rubric asks it to check against.

The seeded `codegen.verifier` prompt runs a nine-point rubric, and four of
those points are defined in terms of the envelope:

    3_contract_preservation  "Compare with the envelope. Every path MUST match
                              exactly."
    5_business_logic_check   "Compare method body against the envelope's
                              `business_logic_summary`"
    6_data_integrity         "all DB annotations / column names match the OLTP DDL"
    9_completeness           "All methods from the envelope exist in the file"

The call site sent only the file content and a task id. No envelope, no task
description, no DDL, no BR ids. Four of the nine checks were structurally
impossible.

Before the `UNVERIFIABLE` verdict existed the verifier had no legal way to say
so, so it guessed — and since it was reading well-formed code it mostly guessed
ACCEPT. A gate that cannot see what it is gating against and answers anyway is
a rubber stamp.

On a live run this showed up immediately: nine files came back UNVERIFIABLE
citing "without the TASK-XXXX envelope or contract details", which is the
verifier correctly reporting that it was asked to do an impossible job.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

TASK = {
    "task_id": "TASK-0007",
    "envelope_id": "ENV-0003",
    "title": "Route handler GET /api/claims/{id}",
    "description": "Controller for the claims lookup endpoint.",
    "layer": "controller",
    "target_path": "services/claims-service/src/main/java/.../ClaimsController.java",
    "source_path": "legacy/ClaimsAction.java",
    "br_ids": ["BR-201"],
}
ENVELOPE = {
    "envelope_id": "ENV-0003",
    "endpoint_method": "GET",
    "endpoint_path": "/api/claims/{id}",
    "business_logic_summary": "Looks up one claim by id and returns 404 when absent.",
    "db_tables": ["claims"],
    "br_ids": ["BR-201"],
    "acceptance_criteria": ["returns 404 for an unknown id"],
}


@pytest.mark.asyncio
async def test_verifier_receives_the_envelope_it_is_told_to_check_against(monkeypatch):
    from routes import codegen as cg

    captured: dict = {}

    async def fake_chat(**kw):
        captured["messages"] = kw["messages"]
        return {"content": '{"verdict":"ACCEPT","confidence":97,"issues":[]}'}

    class _Files:
        async def find_one(self, *a, **kw):
            return {"content": "package x;\npublic class ClaimsController {}\n"}

    class _Envs:
        async def find_one(self, *a, **kw):
            return ENVELOPE

    class _Tasks:
        async def update_one(self, *a, **kw):
            return None

    async def noop(*a, **kw):
        return "run-1"

    async def noop2(*a, **kw):
        return None

    monkeypatch.setattr(cg, "chat_completion", fake_chat)
    monkeypatch.setattr(cg, "codegen_files", _Files())
    monkeypatch.setattr(cg, "codegen_envelopes", _Envs())
    monkeypatch.setattr(cg, "codegen_tasks", _Tasks())
    monkeypatch.setattr(cg, "_log_codegen_agent_run", noop)
    monkeypatch.setattr(cg, "_update_codegen_agent_run", noop2)
    monkeypatch.setattr(cg, "_get_codegen_prompt", lambda k: _aval("SYSTEM"))

    await cg._run_verifier_for_codegen_task("pid", TASK, None)

    blob = " ".join(m["content"] for m in captured["messages"])

    # The four rubric points that need the envelope.
    assert "/api/claims/{id}" in blob, "endpoint path (contract preservation)"
    assert "Looks up one claim by id" in blob, "business_logic_summary (logic check)"
    assert "claims" in blob, "db_tables (data integrity)"
    assert "BR-201" in blob, "BR ids (traceability)"
    # And the task's own intent, so 'completeness' has a reference point.
    assert "Controller for the claims lookup endpoint." in blob
    assert "controller" in blob


def _aval(v):
    async def _f(*a, **kw):
        return v
    return _f()


@pytest.mark.asyncio
async def test_a_missing_envelope_does_not_break_verification(monkeypatch):
    """Envelope lookup failure must degrade, not crash the wave."""
    from routes import codegen as cg

    captured: dict = {}

    async def fake_chat(**kw):
        captured["messages"] = kw["messages"]
        return {"content": '{"verdict":"ACCEPT","confidence":96,"issues":[]}'}

    class _Files:
        async def find_one(self, *a, **kw):
            return {"content": "public class X {}"}

    class _Envs:
        async def find_one(self, *a, **kw):
            raise RuntimeError("mongo down")

    class _Tasks:
        async def update_one(self, *a, **kw):
            return None

    async def noop(*a, **kw):
        return "run-1"

    async def noop2(*a, **kw):
        return None

    monkeypatch.setattr(cg, "chat_completion", fake_chat)
    monkeypatch.setattr(cg, "codegen_files", _Files())
    monkeypatch.setattr(cg, "codegen_envelopes", _Envs())
    monkeypatch.setattr(cg, "codegen_tasks", _Tasks())
    monkeypatch.setattr(cg, "_log_codegen_agent_run", noop)
    monkeypatch.setattr(cg, "_update_codegen_agent_run", noop2)
    monkeypatch.setattr(cg, "_get_codegen_prompt", lambda k: _aval("SYSTEM"))

    await cg._run_verifier_for_codegen_task("pid", TASK, None)

    # The task's own fields still reach the model even without the envelope.
    blob = " ".join(m["content"] for m in captured["messages"])
    assert "TASK-0007" in blob
    assert "controller" in blob
