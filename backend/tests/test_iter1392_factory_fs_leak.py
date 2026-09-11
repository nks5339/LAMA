"""iter-13.92 — Regression tests for the "Factory Droid filesystem leak".

Forensic context (do not delete):

    User report (iter-13.92):
        After iter-13.90 (KB scope) + iter-13.91 (Factory session
        namespace) were both shipped and verified clean, the user still
        saw paths like
            `legacy-code/src/com/ahct/claims/action/ClaimsFlowAction.java:900`
        appear in the TJHS SRS — despite TJHS having ZERO ahct files in
        its scoped KB. Quote: "but my source is fully diff".

    Root cause:
        `routes/srs.py` prompted the Factory Droid LLM with:

            "OPEN the underlying files (`cat`, `grep -n`,
             `sed -n 'A,Bp'`) when you need ground truth"

        The Droid Computer's home directory carries stale `legacy-code/`
        from previous LAMA projects (PMIS in this case). When the LLM
        complied with that directive without an explicit project cwd, it
        `cat`'d whatever it found and cited those foreign paths.

        Both `_gen_one_section` and `_build_shared_srs_system_prompt`
        emitted an unconditional `droid_hint` + a `LEGACY-SOURCE
        GROUNDING` clause that told the model to read the filesystem.

    Fix:
        • When Factory is enabled but `cwd` is NOT explicitly pinned,
          emit a DO-NOT-READ directive instead of an OPEN-FILES one.
        • Reword `LEGACY-SOURCE GROUNDING` so shell-tool access is
          gated on a Factory cwd being listed in the LEGACY CODEBASE
          LOCATION block.
        • These tests assert both prompt sites carry the prohibition.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _read_srs_source() -> str:
    path = os.path.join(BACKEND, "routes", "srs.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_per_section_prompt_carries_do_not_read_directive():
    """The per-section path (`_gen_one_section`) MUST emit a
    'DO NOT READ' directive for the Factory-enabled-but-no-cwd case."""
    src = _read_srs_source()
    assert "FACTORY DROID FILESYSTEM — DO NOT READ" in src, (
        "Per-section legacy block no longer carries the iter-13.92 "
        "prohibition — Factory Droid will resume cat'ing stale source "
        "and leak foreign file paths into the SRS."
    )


def test_batched_prompt_carries_do_not_read_directive():
    """The batched path (`_build_shared_srs_system_prompt`) MUST emit
    the same prohibition — otherwise the batched code path remains a
    leak vector even after the per-section path is fixed."""
    src = _read_srs_source()
    # Count occurrences — there must be at least 2 (per-section + batched).
    n = src.count("FACTORY DROID FILESYSTEM — DO NOT READ")
    assert n >= 2, (
        f"Expected at least 2 'DO NOT READ' blocks (per-section + "
        f"batched), found {n}. Batched-mode SRS will keep leaking "
        "cross-project paths from the Droid filesystem."
    )


def test_legacy_source_grounding_is_now_conditional():
    """The unconditional `OPEN the underlying files (cat, grep -n, sed)`
    directive MUST be gone. Shell-tool access is allowed ONLY when a
    Factory cwd is explicitly listed."""
    src = _read_srs_source()
    # The exact iter-13.91-and-earlier phrasing that caused the leak:
    banned = "OPEN the underlying files (`cat`, `grep -n`,"
    assert banned not in src, (
        "Banned unconditional shell-read directive resurfaced in "
        "routes/srs.py — iter-13.92 explicitly removed it because it "
        "caused cross-project file-path contamination."
    )


def test_legacy_source_grounding_pins_authority_on_legacy_file_index():
    """The replacement clause must explicitly anchor citations on the
    LEGACY FILE INDEX (the scope-checked KB) rather than the Droid
    filesystem."""
    src = _read_srs_source()
    assert "COMPLETE and AUTHORITATIVE list of source-file paths" in src, (
        "iter-13.92 replacement directive missing — the SRS prompt no "
        "longer pins citation authority on the scope-checked KB."
    )

