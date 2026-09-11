"""iter-14.25.4 — Deterministic UC roster + glossary whitelist tests.

Covers the two anti-hallucination guardrails added to
``backend/routes/srs.py``:

* ``_build_uc_roster_from_workflows`` — WF-driven UC roster that kills
  the "§5 collapses into 4 generic CRUD stubs" failure mode observed
  on the CGHS pilot SRS.
* ``_build_glossary_roster`` — whitelist of allowed §1.3 acronyms /
  terms, so the LLM can't invent "PYY — Project Yearly Year" style
  padding.
* ``_extract_actors_from_digest`` — helper used to enrich the roster
  with primary-actor names.

Also verifies iter-14.25.4 reverted the SRS opt-out from iter-14.25.3
(Journey KB stages default = ``("srs", "codegen")`` again).
"""

from __future__ import annotations

import os
import sys
import pathlib

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")
BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routes.srs import (  # noqa: E402
    _build_glossary_roster,
    _build_uc_roster_from_workflows,
    _extract_actors_from_digest,
)
from kb import journey_config as jc  # noqa: E402


# ---------------------------------------------------------------------------
# CGHS-style fixture — mirrors the digest shape used by build_analysis_digest.
# ---------------------------------------------------------------------------

_CGHS_DIGEST = """**System Summary:** CGHS beneficiary services platform.
**Actors:** A-01 Beneficiary, A-02 Field Officer, A-03 District Magistrate

**Domain Entities:**
- **Chronic Outpatient** — a patient receiving ongoing outpatient care (tables: chronic_op, claims)
- **Cochlear Implant Beneficiary** — patient with cochlear implant follow-up (tables: cochlear_followup)
- **Annual Health Checkup** — yearly patient health assessment (tables: ahc_schedule)

**Workflows:**
- **WF-01 Annual Health Checkup** (actor: Beneficiary, states: SCHEDULED, ASSESSED, PAID)
- **WF-02 Chronic OP Registration** (actor: Beneficiary, states: NEW, REGISTERED, CLAIMED)
- **WF-03 Cochlear Follow-Up** (actor: Field Officer, states: DUE, ASSESSED, CLOSED)

**Business Rules:**
- BR-01: The system shall reject a claim if the beneficiary is not ABHA-verified. _(scope: claims, src: ClaimService.java:143)_
"""


# ---------------------------------------------------------------------------
# _extract_actors_from_digest
# ---------------------------------------------------------------------------

def test_extract_actors_from_digest_parses_inline_list():
    out = _extract_actors_from_digest(_CGHS_DIGEST)
    assert out == {
        "A-01": "Beneficiary",
        "A-02": "Field Officer",
        "A-03": "District Magistrate",
    }


def test_extract_actors_from_digest_empty_when_no_actors_line():
    assert _extract_actors_from_digest("") == {}
    assert _extract_actors_from_digest("no actors here") == {}


# ---------------------------------------------------------------------------
# _build_uc_roster_from_workflows
# ---------------------------------------------------------------------------

def test_uc_roster_one_per_workflow():
    roster = _build_uc_roster_from_workflows(_CGHS_DIGEST)
    assert [r["uc_id"] for r in roster] == ["UC-01", "UC-02", "UC-03"]
    assert [r["wf_id"] for r in roster] == ["WF-01", "WF-02", "WF-03"]


def test_uc_roster_carries_workflow_names_verbatim():
    roster = _build_uc_roster_from_workflows(_CGHS_DIGEST)
    names = [r["name"] for r in roster]
    assert "Annual Health Checkup" in names
    assert "Chronic OP Registration" in names
    assert "Cochlear Follow-Up" in names


def test_uc_roster_extracts_primary_actor_from_each_wf_row():
    roster = _build_uc_roster_from_workflows(_CGHS_DIGEST)
    per_wf = {r["wf_id"]: r["actor"] for r in roster}
    assert per_wf["WF-01"] == "Beneficiary"
    assert per_wf["WF-03"] == "Field Officer"


def test_uc_roster_actor_falls_back_to_not_evidenced():
    digest = "- **WF-42 Mystery Flow** (states: A, B)\n"
    roster = _build_uc_roster_from_workflows(digest)
    assert roster == [
        {"uc_id": "UC-01", "wf_id": "WF-42",
         "name": "Mystery Flow", "actor": "NOT_EVIDENCED"}
    ]


def test_uc_roster_empty_when_no_workflows():
    assert _build_uc_roster_from_workflows("") == []
    assert _build_uc_roster_from_workflows("no workflows here") == []


def test_uc_roster_respects_cap():
    digest = "\n".join(
        f"- **WF-{i:02d} Flow {i}** (actor: Actor{i}, states: A)"
        for i in range(50)
    )
    roster = _build_uc_roster_from_workflows(digest, cap=10)
    assert len(roster) == 10
    assert roster[0]["uc_id"] == "UC-01"
    assert roster[-1]["uc_id"] == "UC-10"


# ---------------------------------------------------------------------------
# _build_glossary_roster
# ---------------------------------------------------------------------------

def test_glossary_roster_includes_domain_entity_names():
    """Domain Entities from the digest must seed the whitelist."""
    out = _build_glossary_roster(_CGHS_DIGEST)
    assert "Chronic Outpatient" in out
    assert "Cochlear Implant Beneficiary" in out
    assert "Annual Health Checkup" in out


def test_glossary_roster_excludes_tech_acronyms():
    digest = (
        "The JSP page calls the JPA repository over HTTP with a JSON payload.\n"
        "The SQL query uses UUID keys. See MVC + DTO patterns.\n"
        "**Domain Entities:**\n"
        "- **CGHS Beneficiary** — a person registered in CGHS.\n"
    )
    out = _build_glossary_roster(digest)
    # None of the framework acronyms leak in.
    for tech in ("JSP", "JPA", "HTTP", "JSON", "SQL", "UUID", "MVC", "DTO"):
        assert tech not in out
    # Domain entity survives.
    assert "CGHS Beneficiary" in out


def test_glossary_roster_excludes_section_id_tokens():
    """WF01/UC01/BR01 (no dash) style tokens must never be glossary."""
    digest = "WF01 WF02 UC01 UC02 BR01 BR02 NFR01 NFR02 (repeated) WF01 UC01 BR01 NFR01"
    out = _build_glossary_roster(digest)
    for tok in ("WF01", "UC01", "BR01", "NFR01"):
        assert tok not in out


def test_glossary_roster_requires_repeat_for_acronym_candidates():
    """A one-off ALLCAPS token is not evidenced — skip it."""
    digest = "The ABHA identifier is used everywhere. ABHA verification is mandatory.\n"
    out = _build_glossary_roster(digest)
    assert "ABHA" in out  # appears twice
    # A hallucination-shaped token that appears only once is dropped.
    digest2 = "The PYY is mentioned once and never again."
    assert "PYY" not in _build_glossary_roster(digest2)


def test_glossary_roster_empty_for_empty_input():
    assert _build_glossary_roster("") == {}


def test_glossary_roster_caps_size():
    lines = " ".join(f"ACR{i:03d} ACR{i:03d}" for i in range(60))
    out = _build_glossary_roster(lines, cap=15)
    assert len(out) <= 15


# ---------------------------------------------------------------------------
# Journey KB default restore
# ---------------------------------------------------------------------------

def test_journey_default_stages_excludes_srs():
    # iter-14.25.8 — SRS opted out of journey KB again for CGHS §5 quality.
    assert jc._DEFAULT_STAGES == ("codegen",)


def test_env_default_stages_when_unset(monkeypatch):
    monkeypatch.delenv("LAMA_JOURNEY_KB_STAGES", raising=False)
    assert jc._env_default_stages() == ["codegen"]
