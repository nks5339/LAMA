"""iter-14.25.1 — Per-UC journey grounding pack tests.

Covers:

* ``parse_uc_oneliners`` — three markdown formats (bullet+dash, colon,
  pipe-table).
* ``_seeds_from_uc_line`` — stop-word filtering + endpoint extraction.
* ``_render_uc_pack`` — labelled per-UC block with api + ui journey.
* ``journey_grounding_for_use_cases`` — gate off, empty inputs, best
  api+ui picked, ui pivot-route bonus, truncation at ``max_ucs`` and
  ``max_chars``, evidence-gap markers when no match.
"""

from __future__ import annotations

import os
import sys
import pathlib

import pytest


os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import kb.journey_config as jc  # noqa: E402
import kb.journey_materializer as jm  # noqa: E402


# ---- Fixtures --------------------------------------------------------------

def _api(jid, route, classes, tables, columns=None, roles=None, brs=None):
    return {
        "kind": "api", "journey_id": jid, "pivot_route": route,
        "seed": {"type": "Route"}, "trace": [{"node_type": "Route", "name": route}],
        "endpoints": [route], "roles": list(roles or []),
        "classes_touched": list(classes or []), "methods": [],
        "tables": list(tables or []), "columns": list(columns or []),
        "ui_fields": [], "business_rules": list(brs or []),
        "content_hash": "h_" + jid,
    }


def _ui(jid, route, fields, source="/x.jsp"):
    return {
        "kind": "ui", "journey_id": jid, "pivot_route": route,
        "seed": {"type": "JSPForm", "source": source},
        "trace": [{"node_type": "JSPForm", "name": source}],
        "endpoints": [route], "roles": [], "classes_touched": [],
        "methods": [], "tables": [], "columns": [],
        "ui_fields": list(fields or []), "business_rules": [],
        "content_hash": "hu_" + jid,
    }


# ---- parse_uc_oneliners ----------------------------------------------------

def test_parse_uc_oneliners_bullet_dash_format():
    text = (
        "## 3.3 Use Case One-Liners\n"
        "  - UC-01 — *The applicant submits a new permit request.*\n"
        "  - UC-02 — *The District Magistrate approves permits.*\n"
    )
    out = jm.parse_uc_oneliners(text)
    assert list(out.keys()) == ["UC-01", "UC-02"]
    assert "applicant submits" in out["UC-01"]
    assert "District Magistrate approves" in out["UC-02"]


def test_parse_uc_oneliners_colon_format():
    text = (
        "UC-04: The finance clerk records daily receipts.\n"
        "UC-05: The reviewer forwards flagged bills.\n"
    )
    out = jm.parse_uc_oneliners(text)
    assert set(out.keys()) == {"UC-04", "UC-05"}
    assert "finance clerk records" in out["UC-04"]


def test_parse_uc_oneliners_pipe_table_row():
    text = (
        "| UC ID | Description |\n"
        "|-------|-------------|\n"
        "| UC-10 | The applicant renews her permit before expiry. |\n"
        "| UC-11 | The Magistrate signs off on renewals. |\n"
    )
    out = jm.parse_uc_oneliners(text)
    assert "UC-10" in out and "UC-11" in out
    assert "renews her permit" in out["UC-10"]


def test_parse_uc_oneliners_deduplicates_and_caps():
    text = "\n".join(f"- UC-{i:02d} — description text of use case {i}"
                     for i in range(80))
    out = jm.parse_uc_oneliners(text, cap=20)
    assert len(out) == 20
    assert "UC-00" in out


def test_parse_uc_oneliners_empty_input_returns_empty():
    assert jm.parse_uc_oneliners("") == {}
    assert jm.parse_uc_oneliners(None) == {}  # type: ignore[arg-type]


# ---- _seeds_from_uc_line ---------------------------------------------------

def test_seeds_from_uc_line_strips_stopwords_and_short_tokens():
    seeds, eps = jm._seeds_from_uc_line(
        "The applicant submits a new permit request via /api/permit/submit."
    )
    assert "the" not in seeds  # stop word
    assert "a" not in seeds    # too short
    assert "applicant" in seeds
    assert "submits" in seeds
    assert "permit" in seeds
    assert eps == ["/api/permit/submit"]


def test_seeds_from_uc_line_returns_empty_for_blank():
    assert jm._seeds_from_uc_line("") == ([], [])


# ---- _render_uc_pack -------------------------------------------------------

def test_render_uc_pack_emits_api_and_ui_and_no_gap_when_both_present():
    api = _api("A1", "POST /api/permit", ["PermitCtl", "PermitSvc"],
               ["permits", "users"], ["permits.id", "permits.status"],
               roles=["applicant"], brs=["BR-001", "BR-002"])
    ui = _ui("U1", "POST /api/permit", ["applicant_aadhaar", "category"])
    out = jm._render_uc_pack("UC-01", "The applicant submits a permit", api, ui)
    assert "UC-01 GROUNDING PACK" in out
    assert "route=POST /api/permit" in out
    assert "PermitCtl" in out
    assert "permits.id" in out
    assert "BR-001" in out
    assert "applicant_aadhaar" in out
    assert "EVIDENCE GAP" not in out


def test_render_uc_pack_emits_evidence_gap_when_api_missing():
    out = jm._render_uc_pack("UC-01", "Some text", None, None)
    assert "UC-01 GROUNDING PACK" in out
    assert "no api_journey" in out.lower()
    assert "no ui_journey" in out.lower()


# ---- journey_grounding_for_use_cases --------------------------------------

@pytest.mark.asyncio
async def test_grounding_returns_empty_when_gate_off(monkeypatch):
    async def _off(pid, stage):  # noqa: ARG001
        return False

    monkeypatch.setattr(jc, "is_stage_enabled", _off)
    out = await jm.journey_grounding_for_use_cases(
        "p1", {"UC-01": "The applicant submits."},
    )
    assert out == ""


@pytest.mark.asyncio
async def test_grounding_returns_empty_when_no_uc_lines(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    out = await jm.journey_grounding_for_use_cases("p1", {})
    assert out == ""


@pytest.mark.asyncio
async def test_grounding_returns_empty_when_no_journeys(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    async def _empty(pid, *, kind=None, limit=200):  # noqa: ARG001
        return []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _empty)
    out = await jm.journey_grounding_for_use_cases(
        "p1", {"UC-01": "The applicant submits."},
    )
    assert out == ""


@pytest.mark.asyncio
async def test_grounding_binds_best_api_and_ui_per_uc(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    permit_api = _api("A-permit", "POST /api/permit",
                      ["PermitCtl"], ["permits"], ["permits.id", "permits.status"],
                      roles=["applicant"], brs=["BR-P-001"])
    bill_api = _api("A-bill", "POST /api/bill",
                    ["BillCtl"], ["bills"], ["bills.id"],
                    roles=["clerk"], brs=["BR-B-001"])
    permit_ui = _ui("U-permit", "POST /api/permit",
                    ["applicant_name", "aadhaar"])
    bill_ui = _ui("U-bill", "POST /api/bill", ["bill_no"])

    async def _load(pid, *, kind=None, limit=200):  # noqa: ARG001
        if kind == "api":
            return [permit_api, bill_api]
        if kind == "ui":
            return [permit_ui, bill_ui]
        return []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _load)

    uc_lines = {
        "UC-01": "The applicant submits a new permit request.",
        "UC-02": "The finance clerk records the daily bill.",
    }
    out = await jm.journey_grounding_for_use_cases("p1", uc_lines)

    # Header contract text must be present.
    assert "PER-UC JOURNEY GROUNDING PACKS" in out

    # Each UC gets its own pack, and the RIGHT journeys are bound.
    uc01_block = out.split("UC-01 GROUNDING PACK")[1].split("UC-02 GROUNDING PACK")[0]
    uc02_block = out.split("UC-02 GROUNDING PACK")[1]
    assert "POST /api/permit" in uc01_block
    assert "applicant_name" in uc01_block  # correct ui pack
    assert "POST /api/permit" not in uc02_block  # no cross-contamination
    assert "POST /api/bill" in uc02_block
    assert "bill_no" in uc02_block


@pytest.mark.asyncio
async def test_grounding_prefers_ui_pivoting_to_picked_api_route(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    api = _api("A", "POST /api/permit", ["PermitCtl"], ["permits"],
               ["permits.id"], roles=["applicant"])
    # Two ui candidates — both mention "permit" but only one pivots to the route.
    matching_ui = _ui("U-good", "POST /api/permit", ["a", "b"])
    stray_ui = _ui("U-stray", "GET /api/permit/list", ["c", "d"])

    async def _load(pid, *, kind=None, limit=200):  # noqa: ARG001
        if kind == "api":
            return [api]
        if kind == "ui":
            return [stray_ui, matching_ui]  # order stray first on purpose
        return []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _load)
    out = await jm.journey_grounding_for_use_cases(
        "p1", {"UC-01": "The applicant submits the permit request."},
    )
    # The matching_ui (POST /api/permit) MUST win due to the pivot bonus.
    uc_block = out.split("UC-01 GROUNDING PACK")[1]
    assert "route=POST /api/permit\n" in uc_block
    assert "a, b" in uc_block  # matching ui fields, not stray


@pytest.mark.asyncio
async def test_grounding_emits_gap_marker_when_no_journey_matches(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    unrelated = _api("A-x", "POST /api/reports", ["ReportCtl"], ["reports"])

    async def _load(pid, *, kind=None, limit=200):  # noqa: ARG001
        return [unrelated] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _load)
    out = await jm.journey_grounding_for_use_cases(
        "p1", {"UC-01": "The applicant submits a permit."},
    )
    uc_block = out.split("UC-01 GROUNDING PACK")[1]
    assert "no api_journey matched" in uc_block
    assert "no ui_journey matched" in uc_block


@pytest.mark.asyncio
async def test_grounding_truncates_at_max_ucs(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    api = _api("A", "POST /api/x", ["Ctl"], ["t"], ["t.a"], brs=["BR-1"])

    async def _load(pid, *, kind=None, limit=200):  # noqa: ARG001
        return [api] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _load)
    uc_lines = {f"UC-{i:02d}": f"desc {i}" for i in range(10)}
    out = await jm.journey_grounding_for_use_cases(
        "p1", uc_lines, max_ucs=3, max_chars=10000,
    )
    # Only 3 UC packs plus the truncation marker.
    assert out.count("── UC-") == 3
    assert "pack truncated at max_ucs" in out


@pytest.mark.asyncio
async def test_grounding_truncates_at_max_chars(monkeypatch):
    async def _on(pid, stage):  # noqa: ARG001
        return True

    api = _api("A", "POST /api/x", ["Ctl"], ["t"], ["t.a", "t.b", "t.c"],
               brs=["BR-1"])

    async def _load(pid, *, kind=None, limit=200):  # noqa: ARG001
        return [api] if kind == "api" else []

    monkeypatch.setattr(jc, "is_stage_enabled", _on)
    monkeypatch.setattr(jm, "load_journeys", _load)
    uc_lines = {f"UC-{i:02d}": f"desc {i}" for i in range(50)}
    out = await jm.journey_grounding_for_use_cases(
        "p1", uc_lines, max_ucs=200, max_chars=1200,
    )
    assert "budget exhausted" in out
    assert len(out) <= 1400  # header + one or two packs + trailer
