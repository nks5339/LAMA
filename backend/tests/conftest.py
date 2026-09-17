"""Separate the unit suites from the ones that need a running server.

Before this file existed there was no conftest.py, no pytest.ini and no marker
anywhere in the repo, so `pytest backend/tests/` was the only way to run
anything and it reported 131 failures. 110 of those came from eight legacy
suites that drive the app over HTTP with `requests`:

    ConnectionError: HTTPConnectionPool(host='localhost', port=8382):
    Max retries exceeded with url: /api/health ... Connection refused

Nothing was broken. Nothing was listening. But a run that is permanently
131-red cannot tell you that a real regression just landed -- the signal is
buried in noise that never goes away, and the 21 genuine failures underneath
had been sitting there unnoticed.

So the eight are marked `integration` and skipped by default. A bare
`pytest backend/tests/` now exercises the in-process suites and is expected to
be green, which makes a new failure mean something again.

To run them, start the backend and opt in:

    uvicorn server:app --port 8382          # from backend/
    pytest backend/tests/ --run-integration

`REACT_APP_BACKEND_URL` overrides the base URL the suites target.

These suites are real coverage and are deliberately NOT deleted -- they are
the only end-to-end exercise of the pipeline over HTTP. They are gated, not
discarded.
"""
from __future__ import annotations

import os

import pytest

# The eight suites that require a live HTTP server. Identified by importing
# `requests` at module scope and building a BASE_URL from
# REACT_APP_BACKEND_URL -- every one of them fails with ConnectionError or
# MissingSchema when nothing is listening.
_LIVE_SERVER_SUITES = frozenset({
    "test_arch_codegen",
    "test_console",
    "test_datamodel",
    "test_iter10_ontology",
    "test_iter11_living_diff",
    "test_lama_v2",
    "test_lama_v4",
    "test_migrationos",
})

_SKIP_REASON = (
    "needs a live backend; start uvicorn and pass --run-integration "
    "(see backend/tests/conftest.py)"
)


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run the suites that require a live backend over HTTP.",
    )
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="Run the tests that reach the public internet (Maven Central).",
    )


def _integration_requested(config) -> bool:
    """True when the user has opted in, either by flag or by `-m integration`."""
    if config.getoption("--run-integration"):
        return True
    # `pytest -m integration` is an equally clear opt-in; honour it so the
    # marker is not selected and then immediately skipped.
    expr = config.getoption("-m", default="") or ""
    return "integration" in expr and "not integration" not in expr


# iter-22 — individual tests that reach the PUBLIC INTERNET, gated the same
# way and for a sharper reason than the eight suites above.
#
# The two live Maven Central tests in `test_iter21_dependency_resolution.py`
# used to run by default and skip themselves on a falsy result with the
# message "Maven Central unreachable". `search_artifact_for_class` documents
# itself as "returns None on any failure", so that message asserted a CAUSE
# the test had not established: None meant either the endpoint was slow or
# the prefix ladder / `_rank_candidates` / the fallback had regressed. A real
# regression in the only live exercise of that resolver would have shown up
# as a green run with a skip line.
#
# Measured on this tree: consecutive identical runs gave `1349 passed, 129
# skipped` and `1348 passed, 130 skipped` — the suite's own result was not
# reproducible. The cause is latency, not reachability: an `fc:` full-class
# query for a 23k-match class read-times out while a cheap query on the same
# host answers in under a second, so no probe cheaper than the real query can
# tell the two apart.
#
# Gated, not discarded — the conftest rule at the top of this file. The
# behaviour they were really protecting is now pinned offline in that same
# suite ("a failing query does not abandon the remaining prefixes"), which is
# where a correctness claim belongs.
_LIVE_NETWORK_TESTS = frozenset({
    "test_live_maven_central_resolves_an_uncurated_package",
    "test_live_version_lookup_skips_prereleases",
})

_SKIP_NETWORK_REASON = (
    "reaches Maven Central over the public internet; pass --run-network "
    "(see backend/tests/conftest.py)"
)


def pytest_collection_modifyitems(config, items):
    run_integration = _integration_requested(config)
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    skip_net = pytest.mark.skip(reason=_SKIP_NETWORK_REASON)

    run_network = config.getoption("--run-network")

    for item in items:
        if item.name in _LIVE_NETWORK_TESTS:
            # Deliberately NOT the `integration` marker: that one means "needs
            # the LAMA backend on :8382", and the session-autouse auth fixture
            # below skips the whole run when it is absent. These need the
            # public internet and nothing else, so they get their own flag.
            item.add_marker(pytest.mark.network)
            if not run_network:
                item.add_marker(skip_net)
            continue
        module = item.module.__name__.rsplit(".", 1)[-1] if item.module else ""
        if module not in _LIVE_SERVER_SUITES:
            continue
        item.add_marker(pytest.mark.integration)
        if not run_integration:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Bearer auth for the live-server suites.
#
# All eight predate iter-13.68, which introduced JWT auth and multi-tenancy.
# They build their own `requests.Session` and call /api/projects directly, so
# against a current backend every one of them dies in a fixture with:
#
#     AssertionError: {"detail":"Missing Authorization header"}
#     assert 401 == 200
#
# Rather than edit eight suites, attach the header centrally. This runs ONLY
# in integration mode, so it cannot affect the in-process suites.
#
# Credentials come from the same env vars seed.py::seed_tenancy reads when it
# creates the super-admin, with the same dev fallback, so a stock local stack
# works with no configuration.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _authenticate_live_server_suites(request):
    if not _integration_requested(request.config):
        yield
        return

    import requests

    base = (os.environ.get("REACT_APP_BACKEND_URL") or "http://localhost:8382").rstrip("/")
    user = os.environ.get("LAMA_SUPERADMIN_USER") or "superadmin"
    pwd = os.environ.get("LAMA_SUPERADMIN_PASS") or "lama-admin-2026"

    token = ""
    try:
        r = requests.post(f"{base}/api/auth/login",
                          json={"username": user, "password": pwd}, timeout=15)
        if r.status_code == 200:
            # The login route returns {"token": ...}, not {"access_token": ...}.
            token = (r.json() or {}).get("token") or ""
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live backend not reachable at {base}: {exc}")

    if not token:
        pytest.skip(
            f"could not authenticate against {base} as {user!r}. Set "
            "LAMA_SUPERADMIN_USER / LAMA_SUPERADMIN_PASS to match your deploy."
        )

    original = requests.Session.request

    def _with_bearer(self, method, url, **kw):
        headers = kw.get("headers") or {}
        if "Authorization" not in headers:
            headers = {**headers, "Authorization": f"Bearer {token}"}
            kw["headers"] = headers
        return original(self, method, url, **kw)

    requests.Session.request = _with_bearer
    try:
        yield
    finally:
        requests.Session.request = original
