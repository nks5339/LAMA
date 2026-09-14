"""Every URL the frontend calls must resolve to a registered backend route.

This exists because of a defect that shipped silently. In ``routes/kb.py`` the
handler ``build_progress`` sat directly beneath the previous handler's closing
brace:

    backend/routes/kb.py:1581      }            <- end of _build_kb_impl's return
    backend/routes/kb.py:1582  async def build_progress(project_id: str):

The ``@router.get("/{project_id}/build-progress")`` line had been deleted along
with the blank line that separates every other handler in the file. The function
body was untouched and perfectly correct, so nothing looked wrong on review and
no test noticed -- there was no test that could.

The cost was user-visible. ``BuildKBProgressDialog.jsx`` polls that path every
1-2 seconds from the moment a user clicks "Build Knowledge Base", so every poll
returned 404 and the progress dialog could never advance past its opening state.

A missing decorator is invisible to pyflakes, ruff and vulture -- the function is
still defined, still referenced by nothing, and still syntactically perfect. The
only thing that can catch it is asking whether the caller's URL exists. So that
is what this test does, for every call site rather than just the one that broke.

Path existence only. Method is deliberately NOT asserted: several helpers build a
URL string for an ``<a href>`` or a hand-rolled ``fetch``, so the HTTP verb cannot
be read reliably from the call site and asserting it produces false failures.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_API_JS = _BACKEND.parent / "frontend" / "src" / "lib" / "api.js"

# `api.get("/x")`, `api.post(`/x/${y}`)`, ... -- the axios instance is mounted on
# the /api prefix already, so these paths are relative to it.
_AXIOS_CALL = re.compile(
    r"""\bapi\.(?:get|post|put|patch|delete)\(\s*[`"']([^`"']+)[`"']""",
    re.VERBOSE,
)
# `${API}/kb/${projectId}/owl-export` -- absolute builders for href/fetch.
_API_TEMPLATE = re.compile(r"\$\{API\}(/[^`\"'\s?]*)")


def _normalise(path: str) -> str:
    """Reduce a path to a comparable shape.

    Frontend interpolations (``${projectId}``) and backend path params
    (``{project_id}``) both become ``{}`` so the two sides can be compared
    without caring what the parameter happens to be named.
    """
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    path = path.split("?")[0].rstrip("/")
    return path or "/"


def _frontend_paths() -> set[str]:
    src = _API_JS.read_text(encoding="utf-8")
    found: set[str] = set()
    for m in _AXIOS_CALL.finditer(src):
        raw = m.group(1)
        if raw.startswith("/"):
            found.add(_normalise(f"/api{raw}"))
    for m in _API_TEMPLATE.finditer(src):
        found.add(_normalise(f"/api{m.group(1)}"))
    return found


def _backend_paths() -> set[str]:
    from server import app

    return {_normalise(getattr(r, "path", "")) for r in app.routes if getattr(r, "path", "")}


def _is_served(frontend_path: str, backend_paths: set[str]) -> bool:
    """Does any backend route serve this frontend path?

    An exact match is the common case. The wrinkle is that a ``{}`` on the
    frontend side does not always mean a path parameter -- it can also be a
    selector interpolated over a fixed set of literals. ``startLivingJob``
    builds ``/living/jobs/start/${kind}`` where ``kind`` is only ever
    ``selenium``, ``jmeter``, ``drift`` or ``srs-diff``, and the backend
    registers each of those as its own literal route.

    So a frontend ``{}`` is allowed to match either a backend ``{}`` or a
    literal segment. Every other segment must match exactly, and the segment
    count must be equal -- which is what keeps a genuinely absent route (the
    missing ``build-progress`` decorator) failing.
    """
    if frontend_path in backend_paths:
        return True
    want = frontend_path.split("/")
    for candidate in backend_paths:
        got = candidate.split("/")
        if len(got) != len(want):
            continue
        if all(w == g or w == "{}" for w, g in zip(want, got)):
            return True
    return False


@pytest.mark.skipif(not _API_JS.exists(), reason="frontend/src/lib/api.js not present")
def test_every_frontend_api_path_has_a_backend_route():
    frontend = _frontend_paths()
    backend = _backend_paths()

    assert frontend, "extracted zero paths from api.js -- the regexes have drifted"

    missing = sorted(p for p in frontend if not _is_served(p, backend))
    assert not missing, (
        "frontend/src/lib/api.js calls "
        f"{len(missing)} path(s) that no backend route serves, so every such call "
        "404s at runtime:\n  " + "\n  ".join(missing)
    )


def test_the_kb_build_progress_route_is_registered():
    """The specific regression: a handler that lost its decorator.

    Kept as its own named test so that if it ever breaks again the failure
    names the endpoint directly instead of appearing as one line inside a
    list of many.
    """
    assert "/api/kb/{}/build-progress" in _backend_paths(), (
        "GET /api/kb/{project_id}/build-progress is not registered. The handler "
        "kb.py::build_progress has probably lost its @router.get decorator again "
        "-- BuildKBProgressDialog.jsx polls this path and will 404 on every poll."
    )
