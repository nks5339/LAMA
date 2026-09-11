"""iter-13.99.1 — Cross-stage workspace isolation (llm.fabric_call hook).

Forensic context (do not delete):

    User request after iter-13.99 (SRS-only fix):
        "I think you have to do the same for codegen, architecture and
         datamodel etc all sections"

    Iter-13.99 had wired the workspace-isolation guard into
    routes/srs.py only. Architecture / CodeGen / DataModel / Living /
    Chat all called llm.fabric_call directly so none of them carried
    the guard. The leaked paths were just as likely to appear in HLD,
    LLD, OLTP DDL, service maps and codegen file headers.

    Fix shipped (iter-13.99.1):
      1. Extract every iter-13.99 sanitizer primitive into
         `backend/kb/workspace_isolation.py` (single source of truth).
      2. Hook `sanitize_llm_output` into `llm.fabric_call` at every
         return path (Factory, fabric, env-fallback). Every stage
         that calls fabric_call now inherits the output guard.
      3. Auto-prepend `workspace_directive_for_prompt(...)` to the
         first system message before every project-scoped LLM call so
         every stage also gets the prompt-level contract.
      4. routes/srs.py re-exports the symbols from the shared module
         (back-compat alias) so existing call sites and the leak-scan
         endpoint keep working unchanged.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ── Shared-module surface ─────────────────────────────────────────────
def test_shared_module_exports_match_routes_srs_aliases():
    """The names re-exported in routes/srs.py MUST come from the
    shared module so every stage that imports directly from
    kb.workspace_isolation behaves identically."""
    from kb.workspace_isolation import (
        workspace_root, workspace_slug, is_workspace_path,
        enforce_workspace_prefix, redact_foreign_paths,
        allowed_basenames, allowed_path_suffixes,
        load_allowed_path_index, sanitize_llm_output,
        workspace_root_for_project, workspace_directive_for_prompt,
        PATH_REGEX,
    )
    for sym in (
        workspace_root, workspace_slug, is_workspace_path,
        enforce_workspace_prefix, redact_foreign_paths,
        allowed_basenames, allowed_path_suffixes,
        load_allowed_path_index, sanitize_llm_output,
        workspace_root_for_project, workspace_directive_for_prompt,
    ):
        assert callable(sym)
    assert PATH_REGEX is not None


def test_workspace_root_matches_user_requested_naming():
    """`/lama-workspaces/{tid}__{pid}` exactly — the format the user
    asked for in the iter-13.99 ticket."""
    from kb.workspace_isolation import workspace_root
    assert workspace_root("tA", "pB") == "/lama-workspaces/tA__pB"


def test_hard_guard_blocks_basename_collision_leak_at_module_level():
    """The user-reported leak that defeats every suffix-only matcher."""
    from kb.workspace_isolation import (
        enforce_workspace_prefix, workspace_root, allowed_basenames,
    )
    root = workspace_root("tjhsT", "tjhsP")
    bn = allowed_basenames(["src/com/tjhs/login/LoginAction.java"])
    text = "src/com/ahct/login/controller/LoginAction.java:42"
    out, n = enforce_workspace_prefix(text, root, bn)
    assert n == 1, out
    assert "ahct" not in out


# ── Prompt-directive contract ─────────────────────────────────────────
def test_directive_lists_workspace_root_and_redaction_promise():
    """The directive must (a) name the workspace root verbatim and
    (b) promise redaction so the model knows non-conforming paths
    are wasted tokens."""
    from kb.workspace_isolation import workspace_directive_for_prompt
    d = workspace_directive_for_prompt("/lama-workspaces/X__Y")
    assert "/lama-workspaces/X__Y" in d
    assert "redacted" in d.lower()
    assert "iter-13.99" in d


def test_directive_empty_for_empty_root():
    """No workspace → no directive → caller can unconditionally concat."""
    from kb.workspace_isolation import workspace_directive_for_prompt
    assert workspace_directive_for_prompt("") == ""


# ── fabric_call middleware integration ────────────────────────────────
def test_fabric_call_imports_workspace_isolation_helpers():
    """The hook must reference the shared module — verify the import
    line exists so refactors don't silently bypass the guard."""
    with open(os.path.join(BACKEND, "llm.py"), "r", encoding="utf-8") as f:
        src = f.read()
    assert "from kb.workspace_isolation import" in src, (
        "llm.py no longer imports from kb.workspace_isolation — the "
        "cross-stage hook is broken. EVERY stage's LLM output is now "
        "going out un-sanitized."
    )
    assert "_sanitize_response_inplace" in src
    assert "workspace_directive_for_prompt" in src


def test_fabric_call_has_sanitizer_at_every_return_path():
    """The hook must wrap ALL three return paths: Factory, fabric,
    env-OpenRouter fallback. Any miss is a per-stage leak window."""
    with open(os.path.join(BACKEND, "llm.py"), "r", encoding="utf-8") as f:
        src = f.read()
    occ = src.count("_sanitize_response_inplace")
    # 1 def + 3 call sites (Factory / fabric / env-fallback) = 4 minimum.
    assert occ >= 4, (
        f"Expected ≥4 references to _sanitize_response_inplace "
        f"(definition + 3 return-path hooks), found {occ}. A return "
        "path is now skipping the guard."
    )


def test_fabric_call_auto_prepends_workspace_directive():
    """The prompt-prepend middleware must be present BEFORE the
    Factory routing branch so every stage's system message carries
    the workspace contract."""
    with open(os.path.join(BACKEND, "llm.py"), "r", encoding="utf-8") as f:
        src = f.read()
    assert "workspace_directive_for_prompt" in src
    assert "auto-prepend the workspace-isolation directive" in src
    # Ordering check: the prepend must happen BEFORE Factory routing.
    pre = src.find("auto-prepend the workspace-isolation directive")
    fac = src.find("route_via_factory_orchestrator")
    assert pre > 0 and fac > 0 and pre < fac, (
        "Workspace directive prepend must run BEFORE Factory routing."
    )


# ── sanitize_llm_output behaviour ─────────────────────────────────────
import asyncio


def test_sanitize_llm_output_is_a_no_op_when_project_id_empty():
    from kb.workspace_isolation import sanitize_llm_output
    out = asyncio.run(sanitize_llm_output("see src/foo/Bar.java", ""))
    assert out == "see src/foo/Bar.java"


def test_sanitize_llm_output_is_a_no_op_for_empty_content():
    from kb.workspace_isolation import sanitize_llm_output
    out = asyncio.run(sanitize_llm_output("", "any-pid"))
    assert out == ""

