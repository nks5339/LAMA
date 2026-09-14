"""An audit row nobody can retrieve is not an audit trail.

`_audit_multi_agent` writes the project id as `entity_id` and does not set
`project_id`. `routes/audit.py::list_audit` filters on `project_id`. So every
state change the multi-agent CodeGen pipeline recorded was invisible on the
Audit page.

Measured on a live fixture run:

    audit_log rows total              117
    rows WITH project_id               87
    rows invisible to the Audit page   30
       29x codegen_multi_agent      <- the entire pipeline trail
        1x auth.login               <- correctly has no project

The helper's own docstring states the goal: "so admins can reconstruct the
pipeline from the audit log alone". They could not. The rows existed, the
iter-17 contract was satisfied on paper, and the one consumer could not see
any of them.

`auth.login` is the control: it legitimately has no project and must stay
retrievable without one.
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


@pytest.mark.asyncio
async def test_multi_agent_audit_rows_are_retrievable_by_project(monkeypatch):
    """The regression: the row must carry the field its reader filters on."""
    from routes import codegen as cg

    written: list = []

    class _Col:
        async def insert_one(self, doc):
            written.append(doc)

    monkeypatch.setattr(cg, "audit_log", _Col())

    await cg._audit_multi_agent("pid-abc", "envelopes_confirmed", {"count": 4})

    assert written, "nothing was written at all"
    row = written[0]

    assert row.get("project_id") == "pid-abc", (
        "the audit row has no project_id, so routes/audit.py::list_audit "
        "— which filters on exactly that field — cannot return it"
    )
    # entity_id is kept: other consumers and the existing rows use it.
    assert row.get("entity_id") == "pid-abc"
    assert row.get("entity") == "codegen_multi_agent"
    assert row.get("action") == "envelopes_confirmed"
    assert row.get("details") == {"count": 4}
    assert row.get("at")


@pytest.mark.asyncio
async def test_audit_write_failure_never_breaks_the_pipeline(monkeypatch):
    """Audit is observability. A failed write must not abort a run."""
    from routes import codegen as cg

    class _Broken:
        async def insert_one(self, doc):
            raise RuntimeError("mongo is down")

    monkeypatch.setattr(cg, "audit_log", _Broken())

    # Must not raise.
    await cg._audit_multi_agent("pid-abc", "tasks_confirmed", {})


@pytest.mark.asyncio
async def test_details_default_to_an_empty_dict(monkeypatch):
    from routes import codegen as cg

    written: list = []

    class _Col:
        async def insert_one(self, doc):
            written.append(doc)

    monkeypatch.setattr(cg, "audit_log", _Col())
    await cg._audit_multi_agent("pid-abc", "multi_agent_start")

    assert written[0]["details"] == {}
