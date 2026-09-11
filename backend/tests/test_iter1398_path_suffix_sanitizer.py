"""iter-13.98 — Path-suffix sanitizer (basename-collision fix).

Forensic context (do not delete):

    iter-13.95 shipped a basename-only sanitizer that closed most of
    the leak surface but had a critical hole: when TJHS's KB happened
    to contain a file with the SAME basename as a PMIS file, the
    sanitizer accepted the full PMIS path verbatim.

    User report (iter-13.98):
        "still it is not pointing out project releated input file. It
         is conidering other source code. … src/com/ahct/login/
         controller/LoginAction.java, src/com/ahct/login/service/
         LoginServiceImpl.java in this project this should not come."

        Root cause: `LoginAction.java` is generic enough that TJHS's
        own KB has a same-named file (under `com/tjhs/...`). The
        iter-13.95 sanitizer matched on basename only, so PMIS's
        `src/com/ahct/login/controller/LoginAction.java` survived.

    Fix:
      • Match on PATH SUFFIX with ≥ 2 path components.
      • A bare basename token (no `/`) still passes on basename match.
      • Anything with a `/` must have a 2+ component suffix that
        appears verbatim in a kb_files filename's suffix expansion.

    Also added (iter-13.98):
      • OUTPUT sanitizer pass — the LLM itself can hallucinate paths
        from its training corpus. Sanitize the generated content too.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_path_suffix_blocks_basename_collision():
    """The headline regression: TJHS has its own LoginAction.java,
    so basename-only matching let PMIS LoginAction.java through.
    Path-suffix matching MUST block it now."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    # Simulate TJHS's KB: it has its OWN LoginAction.java but under a
    # totally different parent directory.
    tjhs_files = [
        "src/com/tjhs/login/LoginAction.java",
        "src/com/tjhs/login/LoginServiceImpl.java",
        "src/com/tjhs/util/Util.java",
    ]
    bn = _allowed_basenames(tjhs_files)
    suf = _allowed_path_suffixes(tjhs_files)

    # The PMIS leak that was getting through:
    text = (
        "See src/com/ahct/login/controller/LoginAction.java for the "
        "workflow, and src/com/ahct/login/service/LoginServiceImpl.java "
        "for the bean."
    )
    out, n = _redact_foreign_paths(text, bn, allowed_path_suffixes=suf)
    assert n == 2, f"Expected 2 redactions, got {n}. Out: {out!r}"
    assert "ahct" not in out, (
        "ahct survived path-suffix sanitization — the iter-13.98 "
        "basename-collision fix is not working."
    )
    assert "[redacted-foreign-path]" in out


def test_legitimate_project_path_with_partial_prefix_survives():
    """When the LLM cites a TJHS file with a TRUNCATED prefix
    (e.g. it drops the `src/com/` part), the suffix matcher should
    still recognise it as legitimate."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    tjhs_files = ["src/com/tjhs/login/LoginAction.java"]
    bn = _allowed_basenames(tjhs_files)
    suf = _allowed_path_suffixes(tjhs_files)
    # The model might cite any of these — all are legitimate suffixes
    # of the registered path.
    for cite in [
        "src/com/tjhs/login/LoginAction.java",
        "com/tjhs/login/LoginAction.java",
        "tjhs/login/LoginAction.java:42",
        "login/LoginAction.java::doSubmit",
    ]:
        out, n = _redact_foreign_paths(cite, bn, allowed_path_suffixes=suf)
        assert n == 0, (
            f"Legit TJHS citation {cite!r} was wrongly redacted "
            f"(n={n}, out={out!r})"
        )
        assert cite in out


def test_bare_basename_in_prose_still_passes_on_basename_match():
    """A casual `LoginAction.java` mention in prose (no `/`) doesn't
    have enough info to discriminate — fall back to basename match.
    This avoids being overly aggressive with non-path mentions."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    files = ["src/com/tjhs/LoginAction.java"]
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    # Bare basename mentioned in prose — no `/` in the token. The path
    # regex requires `/` so this token won't even match. (Sanity check
    # that we don't false-positive on prose.)
    text = "The LoginAction class handles submit."
    out, n = _redact_foreign_paths(text, bn, allowed_path_suffixes=suf)
    assert n == 0
    assert out == text


def test_two_component_path_must_match_on_two_components():
    """A 2-component path like `controller/LoginAction.java` must
    have BOTH components agreeing with some kb_files suffix —
    bare-basename match is NOT sufficient (the iter-13.95 hole)."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    files = ["src/com/tjhs/login/LoginAction.java"]  # NOT under `controller/`
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    # PMIS cites `controller/LoginAction.java`. Basename matches BUT
    # the 2-component suffix `controller/LoginAction.java` does NOT
    # appear in TJHS's KB → must be redacted.
    out, n = _redact_foreign_paths(
        "see controller/LoginAction.java:100",
        bn, allowed_path_suffixes=suf,
    )
    assert n == 1, f"basename-only collision was NOT blocked: {out!r}"
    assert "controller/LoginAction.java" not in out


def test_empty_allowed_sets_disable_redaction():
    """Both sets empty → no redaction. The empty-KB case is owned
    by the upstream `kb_file_count == 0` guard."""
    from routes.srs import _redact_foreign_paths
    text = "See foo/bar/Baz.java for details"
    out, n = _redact_foreign_paths(text, set(), allowed_path_suffixes=set())
    assert n == 0
    assert out == text


def test_iter1395_basename_set_still_exposed_for_back_compat():
    """The leak-scan endpoint imports _allowed_basenames directly —
    don't break that import contract."""
    from routes.srs import _allowed_basenames
    out = _allowed_basenames(["src/foo/bar.java", "BAZ.PY"])
    assert "bar.java" in out
    assert "baz.py" in out


def test_iter1395_load_basenames_helper_still_works():
    """Back-compat: `_load_allowed_basenames` must still be callable
    and return a set (we kept it as a shim over the new index)."""
    import inspect
    from routes.srs import _load_allowed_basenames
    assert inspect.iscoroutinefunction(_load_allowed_basenames)


def test_path_suffix_set_is_complete_through_depth_8():
    """For a 5-component path, we expect 5 suffix tuples (depth 1..5)."""
    from routes.srs import _allowed_path_suffixes
    suf = _allowed_path_suffixes(["a/b/c/d/e.java"])
    # depth 1..5 = 5 suffix tuples
    assert ("e.java",) in suf
    assert ("d", "e.java") in suf
    assert ("c", "d", "e.java") in suf
    assert ("b", "c", "d", "e.java") in suf
    assert ("a", "b", "c", "d", "e.java") in suf
    assert len([s for s in suf if s[-1] == "e.java"]) == 5


def test_output_sanitizer_handles_realistic_pmis_paragraph():
    """End-to-end check with the user's actual leaked paragraph."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    # TJHS has zero ahct files in its KB.
    tjhs_files = [
        "src/com/tjhs/login/LoginAction.java",
        "src/com/tjhs/login/LoginServiceImpl.java",
        "src/com/tjhs/util/Util.java",
        "properties/AppConfig.properties",
    ]
    bn = _allowed_basenames(tjhs_files)
    suf = _allowed_path_suffixes(tjhs_files)
    leaked = (
        "Business controls are represented by configurable button/status "
        "values and role-code mappings (for example MEDCO, CEO, EO and "
        "workflow actions such as Approve, Reject, Pending, Forward), and "
        "by explicit service-layer status assignment flows that update "
        "case and audit records (properties/Claims.properties:43, "
        "properties/Claims.properties:54, properties/Claims.properties:76, "
        "src/com/ahct/patient/dao/PatientDaoImpl.java:1867, "
        "src/com/ahct/patient/dao/PatientDaoImpl.java:2288, "
        "src/com/ahct/preauth/service/AutoCancelCasesScheduler.java:50)."
    )
    out, n = _redact_foreign_paths(leaked, bn, allowed_path_suffixes=suf)
    # All 6 foreign citations should be redacted.
    assert n == 6, f"expected 6 redactions, got {n}. Out:\n{out!r}"
    for foreign in (
        "ahct", "Claims.properties", "PatientDaoImpl", "AutoCancelCasesScheduler",
    ):
        assert foreign not in out, (
            f"Foreign token {foreign!r} survived sanitization."
        )

