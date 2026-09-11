"""iter-13.95 — Server-side sanitizer for cross-project file-path leaks.

Forensic context (do not delete):

    iter-13.90 closed the empty-project_id KB-filter leak.
    iter-13.92 closed the "Factory enabled, no cwd" branch.
    iter-13.94 bounded the "Factory enabled WITH cwd" branch.

    User report (iter-13.95):
        "still the same issue persists" — TJHS SRS continued to cite
        `src/com/ahct/*` after iter-13.94. Root cause: prompt language
        alone cannot stop a leak whose *ingress* is upstream of the
        prompt. The `legacy_analysis` collection caches a JSON digest
        for 24h whose `business_rules[*].source` fields the deep-
        analyzer LLM hallucinates from training data (PMIS shapes are
        in its training corpus). That digest is injected into every
        SRS section prompt as "authoritative pre-pass — cite verbatim".

    Fix (iter-13.95):
      1. New helper `_redact_foreign_paths(text, allowed_basenames)`
         that strips any path-like token whose basename isn't in the
         scope-checked kb_files index.
      2. Applied in `_gen_one_section` AND `_build_shared_srs_system_prompt`
         to `analysis_digest`, `convo_text`, `summary`.
      3. Deep-analyzer prompt now carries an ALLOWED-FILES anchor so
         future analyses don't pollute Mongo in the first place.
      4. New `/api/kb/{pid}/leak-scan` + `/invalidate-analysis` endpoints
         so the operator can audit and bust the cached digest.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_redact_foreign_paths_strips_unknown_basenames():
    """iter-13.98 update — when callers pass `allowed_path_suffixes`
    the path matching becomes directory-discriminated. We still keep
    this test because it exercises the back-compat call-shape (the
    leak-scan endpoint uses it without suffixes)."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    files = ["controllers/ClaimsController.php", "models/Claim.php"]
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    text = (
        "See ClaimsController.php:42 for the SUBMIT handler. "
        "Per legacy-code/src/com/ahct/claims/action/ClaimsFlowAction.java:900 "
        "the rule fires. Also relevant: models/Claim.php::approve."
    )
    out, n = _redact_foreign_paths(text, bn, allowed_path_suffixes=suf)
    assert n == 1, f"expected 1 redaction, got {n} ({out!r})"
    assert "ahct" not in out
    # `ClaimsController.php:42` — bare basename mention, no `/`. Path
    # regex requires a `/` so this isn't even a candidate path and
    # survives untouched.
    assert "ClaimsController.php:42" in out
    # `models/Claim.php::approve` — 2-component path, suffix
    # `models/Claim.php` is in the suffix set → survives.
    assert "models/Claim.php::approve" in out
    assert "[redacted-foreign-path]" in out


def test_redact_handles_multiple_extensions_and_suffixes():
    """iter-13.98 — same test, now with explicit path-suffix matching."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    # Register the KB filenames with their FULL paths so the suffix
    # matcher has enough discrimination data.
    files = ["app.py", "db/schema.sql", "src/Main.java"]
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    text = (
        "files: app.py, app/util/helper.py, db/schema.sql:12-40, "
        "src/Main.java::run, foreign/path/Unknown.java"
    )
    out, n = _redact_foreign_paths(text, bn, allowed_path_suffixes=suf)
    # `app.py` — bare basename, no `/`, survives.
    assert "app.py" in out
    # `db/schema.sql:12-40` — 2-component suffix matches → survives.
    assert "schema.sql:12-40" in out
    # `src/Main.java::run` — 2-component suffix matches → survives.
    assert "Main.java::run" in out
    # `helper.py` and `Unknown.java` are foreign → redacted.
    assert "helper.py" not in out
    assert "Unknown.java" not in out
    assert n == 2


def test_empty_allowed_set_disables_redaction():
    """When BOTH sets are empty (KB load failed) we MUST NOT redact —
    the upstream `kb_file_count == 0` guard owns the empty-KB case,
    and silently stripping every path here would mask legitimate content.
    """
    from routes.srs import _redact_foreign_paths
    text = "see foo/bar.java:10 and baz/qux.py"
    out, n = _redact_foreign_paths(text, set(), allowed_path_suffixes=set())
    assert n == 0
    assert out == text


def test_redact_is_case_insensitive_on_basename():
    """Case-insensitivity now applies on the 2-component suffix too —
    register the file with a path so the suffix set has discrimination."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    files = ["src/dao/UserDAO.java"]
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    out, n = _redact_foreign_paths(
        "see src/dao/userdao.java:7", bn, allowed_path_suffixes=suf,
    )
    assert n == 0, f"case-insensitive path-suffix match failed ({out!r})"


def test_path_regex_does_not_eat_non_path_prose():
    """Plain words containing slashes (URLs, fractions) must not be
    redacted. We only care about tokens with a known source ext."""
    from routes.srs import (
        _redact_foreign_paths, _allowed_basenames, _allowed_path_suffixes,
    )
    files = ["a.java"]
    bn = _allowed_basenames(files)
    suf = _allowed_path_suffixes(files)
    text = "Visit https://example.com/docs or read 1/2 of the manual."
    out, n = _redact_foreign_paths(text, bn, allowed_path_suffixes=suf)
    assert n == 0
    assert out == text


def test_sanitizer_helpers_are_importable_from_srs_module():
    """The leak-scan endpoint imports these — make sure the contract holds."""
    from routes.srs import (
        _redact_foreign_paths,
        _allowed_basenames,
        _PATH_REGEX,
        _load_allowed_basenames,
    )
    assert callable(_redact_foreign_paths)
    assert callable(_allowed_basenames)
    assert callable(_load_allowed_basenames)
    # Smoke-test the regex compiles and matches a basic path.
    assert _PATH_REGEX.search("foo/bar.java")

