"""The CodeGen Verifier gate must honour the verdict it asked for.

Two contract defects between `codegen.verifier`'s seeded prompt and the code
that consumes its reply.

**The verdict was parsed and then thrown away.**

    score   = float(parsed.get("confidence", 0.0))
    verdict = str(parsed.get("verdict", "REJECT"))   # <- read
    passing = score >= 95.0                          # <- and never used

A model replying ``{"verdict": "REJECT", "confidence": 97}`` — "this file is
wrong, and I am confident about that" — was marked VERIFIED. The gate asked a
question, received the answer, and discarded it. A quality gate that can be
walked past by a confident rejection is not a gate.

**The prompt promised a passing band the code rejected.** The template said

    95-100: ACCEPT
    85-94:  ACCEPT_WITH_NOTES (Verifier passes it but records warnings)

while the code failed anything under 95. The model was told 85-94 would pass.
It does not. Either the model is being lied to, or the operator is — both are
bad, and the 95 floor is the documented contract ("rejects back to the
assigned Coder if below 95%"), so the prompt is the side that was wrong.

Every case below makes the gate STRICTER or leaves it unchanged. None weakens
it, which the ground rules forbid.
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

from routes.codegen import _verifier_outcome  # noqa: E402


# ── the passing case, unchanged ───────────────────────────────────────

def test_accept_at_or_above_the_floor_passes():
    status, score, reason = _verifier_outcome(
        {"verdict": "ACCEPT", "confidence": 97}
    )
    assert status == "VERIFIED"
    assert score == 97.0
    assert reason == ""


def test_exactly_at_the_floor_passes():
    """95 is the documented floor and is inclusive."""
    status, _, _ = _verifier_outcome({"verdict": "ACCEPT", "confidence": 95})
    assert status == "VERIFIED"


# ── the defect: a rejection must not pass ─────────────────────────────

def test_reject_verdict_fails_even_at_high_confidence():
    """The regression. Confidence measures certainty, not approval.

    A REJECT at 97 means "I am 97% sure this file is wrong", which is the
    strongest possible reason to fail it.
    """
    status, score, reason = _verifier_outcome(
        {"verdict": "REJECT", "confidence": 97}
    )
    assert status == "VERIFY_FAILED"
    assert score == 97.0
    assert "REJECT" in reason


@pytest.mark.parametrize("verdict", ["REJECT", "reject", "  Reject  "])
def test_reject_is_matched_case_and_whitespace_insensitively(verdict: str):
    """Models are inconsistent about casing; the gate must not be."""
    status, _, _ = _verifier_outcome({"verdict": verdict, "confidence": 99})
    assert status == "VERIFY_FAILED"


# ── uncertainty must be expressible, and must not pass ────────────────

def test_unverifiable_fails_rather_than_forcing_a_guess():
    """A model with no evidence needs a legal way to say so.

    Without one, the only options are to invent an ACCEPT or invent a
    REJECT. Both are hallucinations. UNVERIFIABLE is the honest answer and
    it fails closed, so a file is never accepted on a non-answer.
    """
    status, _, reason = _verifier_outcome(
        {"verdict": "UNVERIFIABLE", "confidence": 0,
         "summary": "envelope was not supplied"}
    )
    assert status == "VERIFY_FAILED"
    assert "UNVERIFIABLE" in reason


# ── the score floor, unchanged ────────────────────────────────────────

@pytest.mark.parametrize("confidence", [94.9, 85, 70, 0])
def test_below_the_floor_fails_regardless_of_verdict(confidence):
    status, _, reason = _verifier_outcome(
        {"verdict": "ACCEPT", "confidence": confidence}
    )
    assert status == "VERIFY_FAILED"
    assert "95" in reason


def test_accept_with_notes_no_longer_claims_to_pass():
    """The band the prompt used to promise. It fails, and always did.

    Pinned so that if someone re-adds ACCEPT_WITH_NOTES to the template
    they find out here rather than in production.
    """
    status, _, _ = _verifier_outcome(
        {"verdict": "ACCEPT_WITH_NOTES", "confidence": 90}
    )
    assert status == "VERIFY_FAILED"


# ── malformed input fails closed ──────────────────────────────────────

@pytest.mark.parametrize("parsed", [
    {},                                        # unparseable reply
    {"confidence": 99},                        # no verdict
    {"verdict": "ACCEPT"},                     # no confidence
    {"verdict": "ACCEPT", "confidence": "n/a"},  # non-numeric
    {"verdict": None, "confidence": None},
])
def test_missing_or_malformed_fields_fail_closed(parsed):
    """An empty dict is what the caller passes when parsing failed.

    Failing closed means a verifier that could not answer never silently
    approves a file.
    """
    status, _, _ = _verifier_outcome(parsed)
    assert status == "VERIFY_FAILED"


def test_confidence_above_one_hundred_is_clamped():
    """Models occasionally return 100.0 as "1.0" or overshoot to 150."""
    status, score, _ = _verifier_outcome(
        {"verdict": "ACCEPT", "confidence": 150}
    )
    assert score == 100.0
    assert status == "VERIFIED"


# ── the prompt and the code must agree ────────────────────────────────

def test_seeded_prompt_does_not_promise_a_band_the_code_rejects():
    seed = (_BACKEND / "seed.py").read_text(encoding="utf-8")
    start = seed.index('"key": "codegen.verifier"')
    template = seed[start:start + 4000]

    assert "ACCEPT_WITH_NOTES" not in template, (
        "the verifier prompt promises a passing band that _verifier_outcome "
        "rejects; the model is being told something untrue about what its "
        "reply will cause"
    )
