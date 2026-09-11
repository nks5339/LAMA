"""iter-13.94 — Cross-project leak via Factory Droid cwd (pinned-cwd branch).

Forensic context (do not delete):

    iter-13.90 closed the empty-project_id KB-filter leak.
    iter-13.92 closed the "Factory enabled, no cwd" branch.

    User report (iter-13.94):
        After both fixes shipped, TJHS SRS *still* cited
            `src/com/ahct/claims/action/ClaimsFlowAction.java:900`
        despite TJHS having ZERO ahct files in its KB. Quote:
        "I just try to create a srs within project tjhs. But I found
         you are still referring src/com/ahct/* package."

    Root cause:
        The remaining vulnerable branch is `factory_enabled AND
        factory_cwd` — when the project doc carries a Factory cwd, both
        `_gen_one_section` and `_build_shared_srs_system_prompt` told
        the LLM:

            "The legacy source tree is mounted under this cwd …
             OPEN the relevant file(s) and read the actual code."

        The Droid Computer's filesystem is SHARED across LAMA
        sessions: stale `legacy-code/src/com/ahct/*` from the PMIS
        pilot lives at, below, or alongside any pinned cwd. The model
        complied with "OPEN files" by `ls`/`find`-ing around and
        cited whatever it found.

    Fix (iter-13.94):
      • Shell reads (`cat`/`grep`/`sed`) are allowed ONLY against
        paths that appear VERBATIM in the LEGACY FILE INDEX.
      • Discovery tools (`ls`/`find`/`tree`/`fd`/glob) are FORBIDDEN.
      • Same contract is repeated in the LEGACY-SOURCE GROUNDING
        clause at the bottom of the system prompt.

    These tests lock that contract in place.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _read_srs_source() -> str:
    with open(os.path.join(BACKEND, "routes", "srs.py"), "r", encoding="utf-8") as f:
        return f.read()


def test_pinned_cwd_branch_bounds_shell_to_legacy_file_index():
    """Both the per-section and batched paths must carry the
    HARD-BOUNDED directive that restricts shell reads to paths
    listed VERBATIM in the LEGACY FILE INDEX."""
    src = _read_srs_source()
    n = src.count("FACTORY DROID FILESYSTEM ACCESS (HARD-BOUNDED)")
    assert n >= 2, (
        f"Expected at least 2 'HARD-BOUNDED' Factory FS blocks "
        f"(per-section + batched), found {n}. Without the bound the "
        "Droid will resume `ls`/`find`-ing around its cwd and leak "
        "foreign filenames (e.g. `src/com/ahct/*`) into the SRS."
    )


def test_pinned_cwd_branch_forbids_discovery_walkers():
    """The new bound MUST explicitly forbid `ls`/`find`/`tree`-style
    discovery — that's the actual leak vector, not `cat` itself."""
    src = _read_srs_source()
    assert "FORBIDDEN: `ls`, `find`, `tree`" in src, (
        "iter-13.94 forbid-discovery-walkers clause missing from the "
        "batched Factory FS block."
    )
    assert "FORBIDDEN tools — `ls`, `find`, `tree`" in src, (
        "iter-13.94 forbid-discovery-walkers clause missing from the "
        "per-section Factory FS block."
    )


def test_banned_open_files_directive_stays_banned():
    """The pre-iter-13.94 phrasing that caused the leak MUST NOT
    return. Specifically: telling the LLM to OPEN files for any
    workflow/rule/validation claim without binding to the index."""
    src = _read_srs_source()
    banned = "OPEN the relevant file(s) and read the actual code."
    assert banned not in src, (
        "Banned unconditional OPEN-files directive resurfaced — "
        "iter-13.94 removed it because the model used it as licence "
        "to discover and cite foreign paths."
    )


def test_legacy_source_grounding_clause_repeats_the_bound():
    """Defence in depth: the LEGACY-SOURCE GROUNDING clause at the end
    of the system prompt must repeat the 'shell reads only against
    indexed paths' rule, so it survives even if the LEGACY FILESYSTEM
    block is summarised away by mid-prompt truncation."""
    src = _read_srs_source()
    assert "PERMITTED ONLY against" in src, (
        "iter-13.94 'PERMITTED ONLY against' phrasing missing from the "
        "LEGACY-SOURCE GROUNDING clause."
    )
    assert "shared across LAMA sessions" in src, (
        "iter-13.94 grounding clause must call out that the Droid FS "
        "is shared across sessions (the user-facing root cause)."
    )

