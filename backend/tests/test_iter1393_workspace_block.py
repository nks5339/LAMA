"""iter-13.93 — Regression tests for the inline Virtual Workspace block.

Forensic context (do not delete):

    User request after iter-13.92:
        "why do I require it? you can ask it in srs prompt to do so.
         and show the path from where it should read. create same like
         structure in server space and do use it"

    Translation: don't make the operator pre-create directories on the
    Factory Droid. LAMA already has every source file sharded across
    `kb_chunks` (scoped by project_id + tenant_id). Build a virtual
    project workspace SERVER-SIDE and inline it into the SRS prompt —
    the LLM cites paths from THAT workspace, never from the Droid's
    home directory.

    The tests below lock in:
      • The workspace block is scoped per (tenant, project).
      • The pseudo-root path is namespaced
        (`/lama-workspaces/<tenant>__<project>`, iter-13.99 format).
      • Empty project_id → empty string (never a leak).
      • The signal-ranker prefers controllers/actions over tests/mocks.
      • Both prompt builders are wired to inject the workspace block.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_workspace_root_namespaces_by_tenant_and_project():
    from routes.srs import _workspace_root
    r1 = _workspace_root("tenant_default", "pid-A")
    r2 = _workspace_root("tenant_default", "pid-B")
    r3 = _workspace_root("tenant_other",   "pid-A")
    # iter-13.99 renamed this format deliberately (see the docstring on
    # routes/srs.py::_workspace_root): the root moved to `/lama-workspaces/`
    # (plural) and the separator became a DOUBLE underscore, so that an id
    # containing a single underscore cannot collide with another tuple.
    # These assertions were never updated, so this suite has been red --
    # and therefore guarding nothing -- ever since.
    assert r1 == "/lama-workspaces/tenant_default__pid-A"
    assert r2 == "/lama-workspaces/tenant_default__pid-B"
    assert r3 == "/lama-workspaces/tenant_other__pid-A"
    # Three distinct scopes → three distinct roots — guarantees no
    # cross-project filename collision in citations.
    assert len({r1, r2, r3}) == 3


def test_workspace_root_falls_back_to_default_slugs():
    from routes.srs import _workspace_root
    assert _workspace_root("", "") == "/lama-workspaces/default__default"


def test_signal_ranker_prefers_controllers_over_tests():
    from routes.srs import _is_high_signal_file
    s_controller = _is_high_signal_file("src/com/tjhs/ClaimsAction.java", "java")
    s_service = _is_high_signal_file("src/com/tjhs/ClaimsService.java", "java")
    s_test = _is_high_signal_file("src/test/com/tjhs/ClaimsActionTest.java", "java")
    s_generated = _is_high_signal_file("build/generated/Stub.java", "java")
    # Real business code outranks tests and generated artefacts.
    assert s_controller > s_test
    assert s_service > s_test
    assert s_controller > s_generated
    assert s_service > s_generated


@pytest.mark.asyncio
async def test_workspace_block_refuses_empty_project_id():
    """Defence-in-depth symmetry with _assert_kb_filter — an empty
    project_id MUST NOT trigger a workspace build."""
    from routes.srs import _build_legacy_workspace_block
    out = await _build_legacy_workspace_block("", {"tenant_id": "t"})
    assert out == ""


def test_per_section_prompt_wires_workspace_block():
    """Static guard: `_gen_one_section` must call the workspace builder."""
    src_path = os.path.join(BACKEND, "routes", "srs.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert src.count("_build_legacy_workspace_block(project_id, proj)") >= 2, (
        "Expected workspace builder to be called from BOTH per-section "
        "and batched prompt paths (≥2 occurrences). If you removed one, "
        "that path is regressing to TOON+RAG-only and will no longer "
        "give the LLM inline file contents."
    )


def test_workspace_block_renders_inline_filesystem_directive():
    """The block, when rendered, must explicitly tell the LLM that
    THIS is the source and NOT to use shell tools — otherwise the
    iter-13.92 do-not-read fix gets re-disarmed."""
    src_path = os.path.join(BACKEND, "routes", "srs.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "VIRTUAL LEGACY WORKSPACE" in src
    # The rendered directive is upper-case "DO NOT" (routes/srs.py:840).
    # The assertion's mixed case never matched.
    assert "DO NOT invoke shell tools" in src
    assert "/lama-workspaces/" in src

