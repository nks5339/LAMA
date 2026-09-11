"""iter-13.90 — Regression tests for cross-project KB leakage.

Forensic context (do not delete this comment):

    Three projects in production were observed with SRS documents citing
    `src/com/ahct/*` source paths that did NOT belong to their KB:

        | Project          | Tenant         | KB files | TOON ahct | SRS ahct |
        |------------------|----------------|----------|-----------|----------|
        | TJHS             | tenant_2a49…   |   689    |    0      |  396     |
        | TJHS_V3          | tenant_2a49…   |   688    |    0      |   77     |
        | TEST_Project_…   | tenant_default |     0    |    0      |  229     |

    Root cause: the LEGACY FILE INDEX builder inside `_gen_one_section`
    (and the equivalent in `_build_shared_srs_system_prompt`) used:

        kb_files.find({"project_id": project_id} if project_id else {})

    When `project_id` was empty / falsy, the filter collapsed to `{}` and
    Mongo returned EVERY file across EVERY tenant. Those filenames were
    then injected into the SRS prompt as authoritative "cite VERBATIM"
    evidence — exactly the cross-tenant leakage the user reported.

    Fix (iter-13.89 + iter-13.90):
      • Refuse to build the file-index block when project_id is empty.
      • Always scope queries by `project_id` (no `else {}` fallback).
      • Add a `tenant_id` filter as defence-in-depth.
      • Centralise the filter construction in
        `routes.srs._assert_kb_filter()`.

    These tests lock that contract in place.
"""
import os
import sys
import pytest
from fastapi import HTTPException

# Make `from routes.srs import ...` work whether pytest is invoked from
# repo root or backend/.
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_assert_kb_filter_rejects_empty_project_id():
    """The whole point of the helper: empty project_id MUST raise."""
    from routes.srs import _assert_kb_filter
    for bad in ("", "   ", None):
        with pytest.raises(HTTPException) as exc_info:
            _assert_kb_filter(bad)
        assert exc_info.value.status_code == 500
        assert "project_id" in exc_info.value.detail


def test_assert_kb_filter_returns_pid_only_when_no_tenant():
    """When the project doc has no tenant_id, only project_id is filtered."""
    from routes.srs import _assert_kb_filter
    f = _assert_kb_filter("abc")
    assert f == {"project_id": "abc"}
    f2 = _assert_kb_filter("abc", {})
    assert f2 == {"project_id": "abc"}
    f3 = _assert_kb_filter("abc", {"tenant_id": ""})
    assert f3 == {"project_id": "abc"}


def test_assert_kb_filter_pins_tenant_id_when_present():
    """Defence in depth: if the project carries a tenant_id, pin it."""
    from routes.srs import _assert_kb_filter
    f = _assert_kb_filter("abc", {"tenant_id": "tenant_xyz"})
    assert f == {"project_id": "abc", "tenant_id": "tenant_xyz"}


def test_assert_kb_filter_strips_whitespace():
    """Whitespace-only project_id must be treated as empty."""
    from routes.srs import _assert_kb_filter
    with pytest.raises(HTTPException):
        _assert_kb_filter("   \t  \n ")


def test_no_unscoped_kb_files_query_in_srs_module():
    """Static guard: no `find({})` / `else {}` against kb_files/kb_entities/kb_chunks.

    This catches the *next* time someone copy-pastes the iter-13.88 pattern
    that caused the leak. Does not check every line — only the SRS module
    where the breach actually occurred.
    """
    src_path = os.path.join(BACKEND, "routes", "srs.py")
    with open(src_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # The legacy buggy pattern in raw form. Banned outright in CODE
    # (commented references in the post-mortem docstring are fine — they
    # serve as the historical record of the bug).
    banned_substrings = [
        '{"project_id": project_id} if project_id else {}',
        "{'project_id': project_id} if project_id else {}",
    ]
    offenders: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        # Strip leading whitespace and skip pure-comment lines.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for needle in banned_substrings:
            if needle in line:
                offenders.append((lineno, line.rstrip()))
                break
    assert not offenders, (
        "Banned cross-project-leak pattern resurfaced in routes/srs.py:\n"
        + "\n".join(f"  L{n}: {ln}" for n, ln in offenders)
        + "\nUse _assert_kb_filter(project_id, proj) instead."
    )


def test_load_srs_context_rejects_empty_project_id(monkeypatch):
    """`_load_srs_context` is the SRS hot-path entry. It MUST refuse early
    when project_id is empty so no downstream Mongo query ever sees `{}`.
    """
    import asyncio
    from routes.srs import _load_srs_context
    from fastapi import HTTPException as _HTTPException

    async def _run():
        with pytest.raises(_HTTPException) as exc:
            await _load_srs_context("", None)
        assert exc.value.status_code == 400
        assert "project_id" in exc.value.detail

    # iter-13.91 — use asyncio.run unconditionally; the previous
    # get_event_loop() pattern was deprecated in 3.10 and produced a
    # "no current event loop" RuntimeError when this test ran after
    # any other suite that closed the default loop.
    asyncio.run(_run())


