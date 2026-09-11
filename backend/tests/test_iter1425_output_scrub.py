"""iter-14.25.5 — Post-LLM output-quality scrubber tests.

Covers ``routes.srs._scrub_section_output`` and
``routes.srs._digest_richness`` — the two helpers introduced to
attack the three failure modes observed on the CGHS Aug-21 SRS
regression:

1. ``[redacted-foreign-path]`` markers surfaced in Actor Source
   Evidence cells (from the workspace-prefix sanitizer).
2. Prompt-instruction template sentences echoed as section
   content ("For EVERY role found in the KB…").
3. Placeholder angle-bracket tokens (`<relative/path>`, `<role>`,
   `<Use Case Name>`) and ellipsis-only markdown cells left in
   final output.
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

from routes.srs import _scrub_section_output, _digest_richness  # noqa: E402


# ---------------------------------------------------------------------------
# _scrub_section_output — placeholder rewrites
# ---------------------------------------------------------------------------

def test_scrub_rewrites_redacted_foreign_path():
    text = "| A-01 | Panel Doctor | Assesses patients | [redacted-foreign-path] |\n"
    out, stats = _scrub_section_output(text)
    assert "[redacted-foreign-path]" not in out
    assert "NOT_EVIDENCED (path outside workspace)" in out
    assert stats["placeholder_rewrites"] >= 1


def test_scrub_rewrites_angle_bracket_placeholders():
    text = (
        "| A-01 | <role> | one sentence | <relative/path>::<symbol> |\n"
        "See file <file>:<line> for the check.\n"
    )
    out, stats = _scrub_section_output(text)
    for pat in ("<role>", "<relative/path>::<symbol>", "<file>:<line>"):
        assert pat not in out
    assert stats["placeholder_rewrites"] >= 3


def test_scrub_rewrites_use_case_name_placeholder():
    text = "### UC-01: <Use Case Name>\n#### Narrative\n"
    out, _ = _scrub_section_output(text)
    assert "<Use Case Name>" not in out
    assert "NOT_EVIDENCED" in out


# ---------------------------------------------------------------------------
# _scrub_section_output — instruction-echo removal
# ---------------------------------------------------------------------------

def test_scrub_removes_for_every_role_echo():
    text = (
        "## 3.1 Actor Definitions\n"
        "For EVERY role found in the KB (roles tables, auth / visibility checks, "
        "filter logic in any source file), produce a row in this table:\n\n"
        "| Actor ID | Actor Name |\n"
        "|---|---|\n"
        "| A-01 | Panel Doctor |\n"
    )
    out, stats = _scrub_section_output(text)
    assert "For EVERY role found in the KB" not in out
    assert stats["instruction_echoes_removed"] >= 1
    # Real content survives.
    assert "| A-01 | Panel Doctor |" in out


def test_scrub_removes_matrix_intro_echo():
    text = (
        "## 3.2 Actor → Use Case Mapping\n"
        "A single matrix table — rows = actors, columns = use case IDs (UC-01, UC-02 …).\n"
        "| Actor | UC-01 | UC-02 |\n"
    )
    out, stats = _scrub_section_output(text)
    assert "A single matrix table — rows = actors" not in out
    assert stats["instruction_echoes_removed"] >= 1


def test_scrub_removes_produce_a_row_echo():
    text = "produce a row in this table:\n\n| X | Y |\n"
    out, stats = _scrub_section_output(text)
    assert "produce a row in this table" not in out.lower()
    assert stats["instruction_echoes_removed"] >= 1


def test_scrub_preserves_legit_prose():
    """Legitimate narrative that mentions 'in the KB' mid-sentence stays."""
    text = (
        "Every working morning across empanelled hospitals, the Panel Doctor "
        "logs in and reviews her pending assessments — a workflow evidenced "
        "in the KB by FollowUpAction.execute().\n"
    )
    out, stats = _scrub_section_output(text)
    assert "Panel Doctor" in out
    assert "FollowUpAction.execute()" in out
    assert stats["instruction_echoes_removed"] == 0


# ---------------------------------------------------------------------------
# _scrub_section_output — ellipsis cells
# ---------------------------------------------------------------------------

def test_scrub_rewrites_ellipsis_only_cells():
    text = "| A-01 | Panel Doctor | … | ... |\n"
    out, stats = _scrub_section_output(text)
    assert "| … |" not in out
    assert "| ... |" not in out
    assert stats["empty_ellipsis_cells"] >= 2


# ---------------------------------------------------------------------------
# _scrub_section_output — safety / idempotency
# ---------------------------------------------------------------------------

def test_scrub_empty_input_is_safe():
    out, stats = _scrub_section_output("")
    assert out == ""
    assert sum(stats.values()) == 0


def test_scrub_is_idempotent():
    text = "[redacted-foreign-path] and <role> and | … |"
    once, _ = _scrub_section_output(text)
    twice, stats2 = _scrub_section_output(once)
    assert once == twice
    assert sum(stats2.values()) == 0


# ---------------------------------------------------------------------------
# _digest_richness
# ---------------------------------------------------------------------------

_RICH_DIGEST = """**System Summary:** CGHS beneficiary services.
**Actors:** A-01 Panel Doctor, A-02 Store Keeper, A-03 Beneficiary, A-04 CEO

**Domain Entities:**
- **Chronic OP** — outpatient care (tables: chronic_op)
- **Cochlear Follow-Up** — implant follow-up (tables: cochlear_followup)
- **AHC** — annual health checkup (tables: ahc_schedule)

**Workflows:**
- **WF-01 Annual Health Checkup** (actor: Beneficiary, states: SCHEDULED, ASSESSED)
- **WF-02 Chronic OP Registration** (actor: Beneficiary, states: NEW, CLAIMED)
- **WF-03 Cochlear Follow-Up** (actor: Panel Doctor, states: DUE, CLOSED)
- **WF-04 Drug Inventory** (actor: Store Keeper, states: NEW, RECEIVED)

**Business Rules:**
- BR-01: reject claim if not ABHA-verified.
"""

_THIN_DIGEST = "**Workflows:**\n- **WF-01** (states: A)\n"


def test_digest_richness_flags_thin_as_starved():
    r = _digest_richness(_THIN_DIGEST)
    assert r["starved"] is True
    assert r["workflows"] == 1
    assert r["actors"] == 0


def test_digest_richness_flags_rich_as_not_starved():
    r = _digest_richness(_RICH_DIGEST)
    assert r["starved"] is False
    assert r["workflows"] == 4
    assert r["actors"] == 4
    assert r["entities"] == 3


def test_digest_richness_empty_is_starved():
    r = _digest_richness("")
    assert r["starved"] is True
    assert r["chars"] == 0
