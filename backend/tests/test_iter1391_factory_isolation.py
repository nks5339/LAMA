"""iter-13.91 — Regression tests for per-(tenant, project, agent)
Factory AI context isolation.

Forensic context (do not delete):

    The user reported "within factory ai it should create a project and
    tenant wise new context window. otherwise all getting merged up".

    Three root causes were identified in `backend/factory_orchestrator.py`:

      1. agent_key values contain dots (e.g. "srs.generate") and were
         written via a Mongo dot-path:
             $set: { "...sessions_by_agent.srs.generate": "<sid>" }
         Mongo treats the dot as a nested-doc path, so storage became
             { srs: { generate: "<sid>" } }
         while reads of `sessions_map.get("srs.generate")` returned
         None → the per-agent session cache was silently broken.

      2. The session-cache key was just `agent_key or "default"` — it
         had NO tenant or project namespace, so any future shared-doc
         refactor would cross-stitch contexts.

      3. Every project shared `computer_id=arindamdroid` with `cwd=""`,
         so the Droid's home directory was the de-facto shared
         filesystem context across all projects of all tenants.

    Fixes locked in by this suite:
      • _slug_key dot/dollar safety
      • _session_key produces t::… __p::… __a::… namespaced key
      • _resolve_cwd auto-scopes to <base>/<tenant>/<project>
      • explicit cfg.cwd still wins (power-user override)
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_slug_key_neutralises_mongo_path_chars():
    from factory_orchestrator import _slug_key
    assert _slug_key("srs.generate") == "srs__generate"
    assert _slug_key("arch.hld.deep") == "arch__hld__deep"
    assert _slug_key("$risky.key") == "_risky__key"
    assert _slug_key("path/with/slash") == "path_with_slash"
    assert _slug_key("") == "default"
    assert _slug_key("   ") == "default"


def test_session_key_is_namespaced_by_tenant_project_agent():
    from factory_orchestrator import _session_key
    k1 = _session_key("tenant_default", "pid-A", "srs.generate")
    k2 = _session_key("tenant_default", "pid-B", "srs.generate")
    k3 = _session_key("tenant_other",   "pid-A", "srs.generate")
    k4 = _session_key("tenant_default", "pid-A", "kb.graphify")
    # All four must be distinct — same agent across different scopes
    # must NEVER collide.
    assert len({k1, k2, k3, k4}) == 4
    assert k1 == "t::tenant_default__p::pid-A__a::srs__generate"
    # Dots flattened — safe for Mongo dot-path $set ops.
    assert "." not in k1


def test_session_key_falls_back_to_defaults_on_empty_inputs():
    from factory_orchestrator import _session_key
    k = _session_key("", "", "")
    assert k == "t::default__p::default__a::default"


def test_resolve_cwd_explicit_wins():
    from factory_orchestrator import _resolve_cwd
    # Power-user pinned an explicit cwd → keep it as-is.
    cwd = _resolve_cwd({"cwd": "/custom/path"}, tenant_id="t1", project_id="p1")
    assert cwd == "/custom/path"


def test_resolve_cwd_auto_scopes_per_tenant_project(monkeypatch):
    from factory_orchestrator import _resolve_cwd
    # iter-13.91.2 — auto-scope is opt-in via env-var (the operator must
    # have pre-created the base path on the Droid). When set, each
    # (tenant, project) tuple MUST produce a distinct path.
    monkeypatch.setenv("LAMA_FACTORY_WORKSPACE_BASE", "/srv/lama")
    cwd1 = _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="p1")
    cwd2 = _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="p2")
    cwd3 = _resolve_cwd({"cwd": ""}, tenant_id="t2", project_id="p1")
    # iter-13.91.8 collapsed the nested `<base>/<tenant>/<project>` into a
    # SINGLE-level `lama_<tenant>_<project>` directly under the base, because
    # Factory's sidebar only auto-lists folders one level deep and the nested
    # form left the project folder invisible. The shape changed; the guarantee
    # this test exists for -- one distinct path per (tenant, project) -- did
    # not. These assertions were never updated, so this suite has been red,
    # and therefore guarding nothing, ever since.
    assert cwd1 == "/srv/lama/lama_t1_p1"
    assert cwd2 == "/srv/lama/lama_t1_p2"
    assert cwd3 == "/srv/lama/lama_t2_p1"
    # The point of the test: no two tuples may collide.
    assert len({cwd1, cwd2, cwd3}) == 3


def test_resolve_cwd_host_anchored_is_empty(monkeypatch):
    """Factory's POST /sessions returns HTTP 400 for any `cwd` that does
    not pre-exist on the Droid Computer, so there must be a way to send NO
    cwd at all and let Factory default to the droid's HOME.

    iter-13.91.2 expressed that as "empty unless the env-var is set".
    iter-13.91.12 replaced it with an explicit `cfg["host_anchored"]` flag,
    which is a better contract: the caller states its intent rather than
    having it inferred from an absent env-var. This test follows the
    guarantee, not the old mechanism -- what must never regress is that
    SOME supported configuration yields an empty cwd.
    """
    from factory_orchestrator import _resolve_cwd
    monkeypatch.delenv("LAMA_FACTORY_WORKSPACE_BASE", raising=False)
    cwd = _resolve_cwd({"cwd": "", "host_anchored": True},
                       tenant_id="tenant_default", project_id="abc")
    assert cwd == "", (
        "host_anchored MUST yield an empty cwd so the caller omits the field "
        "and Factory uses the Droid's home — auto-scoping to a non-existent "
        'path triggers HTTP 400 Invalid cwd "..." '
        "(regression: iter-13.91 and iter-13.91.1)"
    )


def test_resolve_cwd_without_project_is_empty(monkeypatch):
    """The other path to an empty cwd: no project, nothing to scope to."""
    from factory_orchestrator import _resolve_cwd
    monkeypatch.delenv("LAMA_FACTORY_WORKSPACE_BASE", raising=False)
    assert _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="") == ""


def test_resolve_cwd_rejects_tilde_base_expands_or_falls_through(monkeypatch):
    """Operator-misconfigured `~`-prefixed base MUST NOT propagate to
    Factory — expand it first."""
    from factory_orchestrator import _resolve_cwd
    monkeypatch.setenv("LAMA_FACTORY_WORKSPACE_BASE", "~/should-be-expanded")
    cwd = _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="p1")
    assert not cwd.startswith("~"), f"tilde leaked into cwd: {cwd}"
    # After expansion the path MUST be absolute (or we return empty
    # rather than sending Factory an invalid value).
    assert cwd == "" or cwd.startswith("/")


def test_resolve_cwd_rejects_relative_base_falls_through(monkeypatch):
    """A misconfigured relative env-var MUST NOT reach Factory.

    iter-13.91.2 satisfied that by returning empty. iter-13.91.8 instead
    substitutes the built-in absolute default, which is strictly better:
    the operator still gets a working, isolated workspace rather than
    silently losing scoping because of a typo. The guarantee this test
    exists for is that the relative string never leaves the function.
    """
    from factory_orchestrator import _resolve_cwd
    monkeypatch.setenv("LAMA_FACTORY_WORKSPACE_BASE", "relative/path")
    cwd = _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="p1")

    assert "relative/path" not in cwd, f"relative base leaked into cwd: {cwd}"
    assert cwd.startswith("/"), f"cwd must be absolute, got: {cwd}"
    # Per-(tenant, project) scoping survives the fallback.
    assert cwd.endswith("/lama_t1_p1")


def test_resolve_cwd_expands_tilde_in_explicit_user_override(monkeypatch):
    """Power-user override `cwd=~/projects/foo` must also be expanded
    before being sent to Factory."""
    from factory_orchestrator import _resolve_cwd
    monkeypatch.setenv("HOME", "/home/test")
    cwd = _resolve_cwd({"cwd": "~/projects/foo"}, tenant_id="t1", project_id="p1")
    assert not cwd.startswith("~"), f"tilde leaked into cwd: {cwd}"
    assert cwd == "/home/test/projects/foo"


def test_resolve_cwd_returns_empty_when_project_id_missing():
    """If project_id is missing we MUST NOT invent a path — fall through
    to the Droid's default home and let the caller's caller decide."""
    from factory_orchestrator import _resolve_cwd
    assert _resolve_cwd({"cwd": ""}, tenant_id="t1", project_id="") == ""


def test_persist_session_id_uses_safe_slug(monkeypatch):
    """The Mongo $set path MUST be slugged — otherwise dots in
    agent_key get re-interpreted as nested-doc paths and the cache
    silently breaks."""
    import asyncio
    import factory_orchestrator as fo

    captured: dict = {}

    class _FakeProjects:
        async def update_one(self, q, u):
            captured["query"] = q
            captured["update"] = u
            return None

    monkeypatch.setattr(fo, "projects", _FakeProjects(), raising=True)

    asyncio.run(fo._persist_session_id("pid-1", "srs.generate", "sess-abc"))
    paths = list(captured["update"]["$set"].keys())
    # The agent_key dot MUST NOT survive into the Mongo path.
    assert "settings.factory_orchestrator.sessions_by_agent.srs.generate" not in paths
    assert "settings.factory_orchestrator.sessions_by_agent.srs__generate" in paths

