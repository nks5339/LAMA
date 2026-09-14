"""The deterministic BE/FE router must actually be deterministic.

The contract is that `_route_task_to_coder` overrides the Planner on
unambiguous paths, so a frontend file NEVER reaches the backend coder no
matter what the Planner said.

Found by driving the real pipeline: the guard was inert for the exact layout
the Planner itself produces. `_BE_PATH_PREFIXES` contains ``services/``, and
the deterministic planner emits frontend files as
``services/web/src/pages/panels.tsx``. That path matches the backend rule (it
starts with ``services/``) AND the frontend rule (it ends ``.tsx``), so it was
classified "ambiguous" and fell through to "trust the Planner".

Every frontend file in a real run was therefore routed by the Planner's
suggestion alone, with the deterministic override contributing nothing. A
PATCH forcing ``assigned_to: coder_be`` onto a ``.tsx`` file was accepted and
persisted.

The fix is that an unambiguous EXTENSION outranks a generic directory prefix.
``services/`` says almost nothing about a file; ``.tsx`` says everything.
``.ts`` and ``.js`` stay ambiguous on purpose, because a Node backend uses
them too, and there the Planner's suggestion is still the best signal
available.
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

from routes.codegen import _route_task_to_coder  # noqa: E402


def route(path: str, planner: str = "coder_be") -> str:
    return _route_task_to_coder({"target_path": path, "assigned_to": planner})


# ── the regression: the planner's own frontend layout ─────────────────

@pytest.mark.parametrize("path", [
    "services/web/src/pages/panels.tsx",
    "services/web/src/components/Table.jsx",
    "services/web/src/styles/app.css",
    "services/web/src/styles/theme.scss",
    "services/web/src/App.vue",
    "services/web/src/Page.svelte",
    "services/web/index.html",
])
def test_frontend_extension_beats_the_services_prefix(path: str):
    """These are the paths the deterministic planner really emits.

    Passing planner="coder_be" is the hostile case: even when the Planner
    is wrong, or an operator forces it through PATCH, the router must
    refuse to send a frontend file to the backend coder.
    """
    assert route(path, planner="coder_be") == "coder_fe"


@pytest.mark.parametrize("path", [
    "services/panel-service/src/main/java/com/lama/PanelController.java",
    "services/panel-service/src/main/kotlin/Panel.kt",
    "services/panel-service/pom.xml",
    "backend/app/routes/panels.py",
    "db/migrations/V1__init.sql",
    "services/claims/main.go",
])
def test_backend_files_still_route_to_the_backend_coder(path: str):
    """The hostile case in the other direction."""
    assert route(path, planner="coder_fe") == "coder_be"


# ── paths that are genuinely ambiguous stay with the planner ──────────

@pytest.mark.parametrize("path", [
    "services/web/src/api/panels.ts",     # could equally be a Node service
    "services/gateway/server.js",
])
def test_ts_and_js_remain_ambiguous_and_trust_the_planner(path: str):
    """`.ts` and `.js` are used by Node backends as well as frontends.

    Forcing a guess here would be worse than deferring: the Planner has
    the envelope and the service context, this function has a string.
    """
    assert route(path, planner="coder_fe") == "coder_fe"
    assert route(path, planner="coder_be") == "coder_be"


def test_unknown_path_with_no_planner_hint_defaults_to_backend():
    """Documented fallback. Backend is the safer default: the BE coder's
    refusal envelope catches a misroute, and most files are backend."""
    assert route("some/unknown/thing.bin", planner="") == "coder_be"


def test_empty_path_never_crashes():
    assert route("", planner="") in ("coder_be", "coder_fe")


# ── the invariant itself ──────────────────────────────────────────────

def test_router_only_ever_returns_a_real_coder():
    for p in ["", "x.java", "services/web/a.tsx", "weird", "a/b/c.ts"]:
        for planner in ["", "coder_be", "coder_fe", "nonsense", None]:
            out = _route_task_to_coder({"target_path": p, "assigned_to": planner})
            assert out in ("coder_be", "coder_fe"), (p, planner, out)


def test_a_frontend_file_can_never_be_forced_onto_the_backend_coder():
    """The contract, stated once, over the full frontend extension set."""
    for ext in (".jsx", ".tsx", ".css", ".scss", ".vue", ".svelte", ".html"):
        for prefix in ("services/web/", "services/ui/", "apps/web/", ""):
            path = f"{prefix}src/pages/thing{ext}"
            for planner in ("coder_be", "", "nonsense"):
                assert route(path, planner) == "coder_fe", (path, planner)
