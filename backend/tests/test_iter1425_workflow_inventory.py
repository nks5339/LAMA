"""iter-14.25.2 — Workflow-ID (WF-NN) extraction tests.

Covers ``routes.srs._extract_workflow_ids`` — the parser powering the
MANDATORY WORKFLOW INVENTORY block that fixes the "KB token not
covered: WF-01" coverage-scorer complaint on SRS §3 / §5.
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

from routes.srs import _extract_workflow_ids  # noqa: E402


def test_extract_workflow_ids_from_digest_format():
    """The exact shape emitted by ``kb.legacy_analyzer.build_analysis_digest``."""
    digest = (
        "**Workflows:**\n"
        "- **WF-01 Tender Approval** (actor: DM, states: DRAFT, SUBMITTED)\n"
        "- **WF-02 Bill Processing** (actor: Clerk, states: NEW, PAID)\n"
        "- **WF-03 Grievance Handling** (actor: Officer, states: OPEN, CLOSED)\n"
    )
    out = _extract_workflow_ids(digest)
    assert list(out.keys()) == ["WF-01", "WF-02", "WF-03"]
    assert out["WF-01"] == "Tender Approval"
    assert out["WF-02"] == "Bill Processing"
    assert out["WF-03"] == "Grievance Handling"


def test_extract_workflow_ids_handles_colon_and_dash_variants():
    digest = (
        "WF-01: Tender Approval\n"
        "WF-02 — Bill Processing\n"
        "WF-03 Grievance Handling\n"
    )
    out = _extract_workflow_ids(digest)
    assert set(out.keys()) == {"WF-01", "WF-02", "WF-03"}
    assert "Tender" in out["WF-01"]
    assert "Bill" in out["WF-02"]
    assert "Grievance" in out["WF-03"]


def test_extract_workflow_ids_dedupes_by_id():
    digest = (
        "First reference: WF-01 Tender Approval.\n"
        "Second reference: WF-01 (same workflow, different framing).\n"
    )
    out = _extract_workflow_ids(digest)
    assert list(out.keys()) == ["WF-01"]


def test_extract_workflow_ids_returns_empty_when_no_ids():
    assert _extract_workflow_ids("") == {}
    assert _extract_workflow_ids("No workflows here.") == {}


def test_extract_workflow_ids_caps_at_limit():
    digest = "\n".join(f"- **WF-{i:02d} Workflow {i}** (actor: X)" for i in range(80))
    out = _extract_workflow_ids(digest, cap=25)
    assert len(out) == 25
    assert "WF-00" in out
    assert "WF-24" in out
    assert "WF-25" not in out


def test_extract_workflow_ids_preserves_insertion_order():
    digest = (
        "- **WF-05 Payment** (actor: X)\n"
        "- **WF-01 Tender** (actor: Y)\n"
        "- **WF-03 Report** (actor: Z)\n"
    )
    out = _extract_workflow_ids(digest)
    # Insertion order = digest order, NOT numeric.
    assert list(out.keys()) == ["WF-05", "WF-01", "WF-03"]


def test_extract_workflow_ids_ignores_non_wf_prefixes():
    """`WFOO-01`, `WF01` (no dash), and `WF-` (no number) must all be ignored."""
    digest = "WFOO-01 fake\nWF01 also fake\nWF- placeholder\nWF-99 real"
    out = _extract_workflow_ids(digest)
    assert list(out.keys()) == ["WF-99"]
