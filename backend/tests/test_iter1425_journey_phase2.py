"""iter-14.25 — Phase 2 journey-KB wiring tests.

Covers:

* :func:`kb.journey_config.is_stage_enabled` — off/on gates + stages allowlist.
* :func:`kb.journey_materializer.journey_context_block` — returns empty
  when the gate is off; renders a scored, bounded slice when on.

We stub the two DB seams (``is_journey_kb_enabled``, ``journey_kb_stages``,
``load_journeys``) via monkeypatch so the tests do not need a live Mongo.
"""

from __future__ import annotations

import os
import sys
import pathlib

import pytest


# ---- Boilerplate: stub Mongo env before importing kb.* modules ------------
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


import kb.journey_config as jc  # noqa: E402
import kb.journey_materializer as jm  # noqa: E402


# ---- Fixtures --------------------------------------------------------------

def _api_journey(jid: str, route: str, classes, tables, columns=None,
                 fields=None) -> dict:
    return {
        "kind": "api",
        "journey_id": jid,
        "pivot_route": route,
        "seed": {"type": "Route", "verb": route.split(" ", 1)[0],
                 "path": route.split(" ", 1)[1]},
        "trace": [
            {"node_type": "Route", "name": route},
            {"node_type": "Class", "name": classes[0] if classes else ""},
        ],
        "endpoints": [route],
        "roles": [],
        "classes_touched": list(classes or []),
        "methods": [],
        "tables": list(tables or []),
        "columns": list(columns or []),
        "ui_fields": list(fields or []),
        "business_rules": [],
        "content_hash": "h_" + jid,
    }


def _ui_journey(jid: str, route: str, fields) -> dict:
    return {
        "kind": "ui", "journey_id": jid, "pivot_route": route,
        "seed": {"type": "JSPForm", "source": "/x.jsp"},
        "trace": [{"node_type": "JSPForm", "name": "/x.jsp"}],
        "endpoints": [route], "roles": [], "classes_touched": [],
        "methods": [], "tables": [], "columns": [],
        "ui_fields": list(fields or []),
        "business_rules": [], "content_hash": "hu_" + jid,
    }


# ---- is_stage_enabled ------------------------------------------------------

@pytest.mark.asyncio
async def test_is_stage_enabled_returns_false_when_master_toggle_off(monkeypatch):
    async def _enabled(pid):  # noqa: ARG001
        return False

    async def _stages(pid):  # noqa: ARG001
        return ["srs", "codegen"]

    monkeypatch.setattr(jc, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(jc, "journey_kb_stages", _stages)
    assert await jc.is_stage_enabled("p1", "codegen") is False


@pytest.mark.asyncio
async def test_is_stage_enabled_respects_stages_allowlist(monkeypatch):
    async def _enabled(pid):  # noqa: ARG001
        return True

    async def _stages(pid):  # noqa: ARG001
        return ["srs"]  # codegen deliberately excluded

    monkeypatch.setattr(jc, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(jc, "journey_kb_stages", _stages)
    assert await jc.is_stage_enabled("p1", "srs") is True
    assert await jc.is_stage_enabled("p1", "codegen") is False


@pytest.mark.asyncio
async def test_is_stage_enabled_rejects_blank_stage(monkeypatch):
    async def _enabled(pid):  # noqa: ARG001
        return True

    async def _stages(pid):  # noqa: ARG001
        return ["srs"]

    monkeypatch.setattr(jc, "is_journey_kb_enabled", _enabled)
    monkeypatch.setattr(jc, "journey_kb_stages", _stages)
    assert await jc.is_stage_enabled("p1", "") is False
    assert await jc.is_stage_enabled("p1", None) is False  # type: ignore[arg-type]


# ---- journey_context_block -------------------------------------------------

@pytest.mark.asyncio
async def test_journey_context_block_returns_empty_when_disabled(monkeypatch):
    async def _stage_off(pid, stage):  # noqa: ARG001
        return False

    monkeypatch.setattr(jm, "is_stage_enabled", _stage_off, raising=False)
    # Also swap the module attribute path used inside journey_context_block.
    monkeypatch.setattr(jc, "is_stage_enabled", _stage_off)

    out = await jm.journey_context_block(
        "p1", "codegen", seeds=["user", "register"],
    )
    assert out == ""


@pytest.mark.asyncio
async def test_journey_context_block_returns_empty_when_no_journeys(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    async def _empty(*a, **kw):  # noqa: ARG001
        return []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _empty)
    assert await jm.journey_context_block("p1", "codegen", seeds=["x"]) == ""


@pytest.mark.asyncio
async def test_journey_context_block_scores_and_renders_matching_journeys(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    hit = _api_journey("J-hit", "POST /api/register",
                       classes=["UserController"],
                       tables=["users"], columns=["users.email"])
    miss = _api_journey("J-miss", "GET /api/reports",
                        classes=["ReportController"],
                        tables=["reports"], columns=["reports.id"])

    async def _load(project_id, *, kind=None, limit=200):  # noqa: ARG001
        return [hit, miss] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    out = await jm.journey_context_block(
        "p1", "codegen",
        seeds=["register", "user", "users"],
        endpoint_hints=["/api/register"],
        max_chars=3000,
        max_journeys=5,
    )
    assert "# JOURNEY-KB SLICE" in out
    assert "J-hit" in out
    assert "POST /api/register" in out
    # Miss should not appear because seed/endpoint score for it was zero.
    assert "J-miss" not in out


@pytest.mark.asyncio
async def test_journey_context_block_returns_empty_when_no_seed_matches(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    miss = _api_journey("J-miss", "GET /api/reports", ["ReportController"],
                        ["reports"], ["reports.id"])

    async def _load(project_id, *, kind=None, limit=200):  # noqa: ARG001
        return [miss] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    out = await jm.journey_context_block(
        "p1", "codegen",
        seeds=["nomatch"], endpoint_hints=["/none"],
    )
    assert out == ""


@pytest.mark.asyncio
async def test_journey_context_block_truncates_at_max_chars(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    many = [
        _api_journey(f"J-{i}", f"POST /api/user/{i}",
                     [f"UserController{i}"], ["users"], ["users.email"])
        for i in range(60)
    ]

    async def _load(project_id, *, kind=None, limit=200):  # noqa: ARG001
        return many if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    out = await jm.journey_context_block(
        "p1", "codegen", seeds=["user"],
        max_chars=800, max_journeys=50,
    )
    assert "# JOURNEY-KB SLICE" in out
    # Truncation marker OR clean block boundary — either way, budget honoured.
    assert len(out) <= 900


@pytest.mark.asyncio
async def test_journey_context_block_prefers_endpoint_hit_over_seed_hit(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    seed_only = _api_journey("J-seed", "GET /api/misc",
                             ["UserService"], ["users"], ["users.email"])
    endpoint_only = _api_journey("J-ep", "POST /api/register",
                                 ["RegistrationController"], ["signups"],
                                 ["signups.email"])

    async def _load(project_id, *, kind=None, limit=200):  # noqa: ARG001
        return [seed_only, endpoint_only] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    out = await jm.journey_context_block(
        "p1", "codegen",
        seeds=["user"], endpoint_hints=["/api/register"],
        max_chars=4000, max_journeys=2,
    )
    idx_ep = out.find("J-ep")
    idx_seed = out.find("J-seed")
    assert idx_ep != -1
    assert idx_seed == -1 or idx_ep < idx_seed  # endpoint match ranks first


@pytest.mark.asyncio
async def test_journey_context_block_includes_ui_journeys(monkeypatch):
    async def _stage_on(pid, stage):  # noqa: ARG001
        return True

    api = _api_journey("J-api", "POST /api/register",
                       ["UserCtl"], ["users"], ["users.email"])
    ui = _ui_journey("J-ui", "POST /api/register",
                     fields=["username", "email", "password"])

    async def _load(project_id, *, kind=None, limit=200):  # noqa: ARG001
        if kind == "api":
            return [api]
        if kind == "ui":
            return [ui]
        return []

    monkeypatch.setattr(jc, "is_stage_enabled", _stage_on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    out = await jm.journey_context_block(
        "p1", "srs",
        seeds=["register", "email"], endpoint_hints=["/api/register"],
        max_chars=4000,
    )
    assert "J-api" in out
    assert "J-ui" in out


# ---- scoring helper --------------------------------------------------------

def test_score_journey_ranks_higher_on_endpoint_hit_than_seed_hit():
    j = _api_journey("J", "POST /api/register",
                     ["UserController"], ["users"], ["users.email"])
    ep_score = jm._score_journey_for_seeds(j, ["user"], ["/api/register"])
    seed_only_score = jm._score_journey_for_seeds(j, ["user"], [])
    assert ep_score > seed_only_score


def test_score_journey_returns_positive_when_no_seeds_supplied():
    j = _api_journey("J", "POST /x", ["C"], ["t"], ["t.a"])
    assert jm._score_journey_for_seeds(j, [], []) == 1
