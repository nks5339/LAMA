"""iter-13.85 — Frontend codegen contract tests.

Before iter-13.85 frontend files bypassed every structural check the
backend enjoyed:
  • ``_validate_structure`` looked up ``(file_type, backend_lang)`` —
    frontend services have no ``backend_lang`` → every .tsx file
    silently passed validation.
  • ``FILE_TYPE_SKELETONS`` / ``REQUIRED_TOKENS`` had no frontend keys
    so the LLM was never shown a few-shot for `page` / `api_client` /
    `RoleGate` / routes / store / bootstrap.
  • ``_emergency_scaffold`` had no React branch → if the LLM failed on
    `Dashboard.tsx`, the fallback was `// LAMA emergency scaffold for
    Dashboard.tsx` (a single comment, doesn't compile).

This file pins the new contract:
  1. The frontend file plan ships every artefact needed for `npm run
     dev` standalone (package.json, tsconfig.json, index.html,
     main.tsx, api/client.ts, store/auth.ts, routes.tsx, RoleGate.tsx,
     >= 1 Page.tsx, Dockerfile, README).
  2. Every frontend emergency scaffold passes the FRONTEND_REQUIRED_TOKENS
     validator for its (ctype, framework) pair.
  3. The scaffolded package.json + tsconfig.json + index.html parse
     cleanly with their respective parsers.
  4. FRONTEND_REQUIRED_TOKENS covers `page`, `api_client`, `role_guard`,
     `route_config`, `store`, `bootstrap` for React, with equivalent
     coverage for Angular / Vue / Svelte where supported.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from codegen.file_templates import (  # noqa: E402
    FRONTEND_FILE_TYPE_SKELETONS,
    FRONTEND_REQUIRED_TOKENS,
    frontend_required_tokens_for,
    get_frontend_instruction,
)


# ---------------------------------------------------------------------------
# Local copy of routes.codegen._validate_structure (frontend branch).
# ---------------------------------------------------------------------------
def _validate_structure_frontend(content: str, ctype: str, framework: str):
    if not content or len(content.strip()) < 40:
        return False, ["content too short / empty"]
    tokens = frontend_required_tokens_for(ctype, framework)
    if not tokens:
        return True, []
    missing = []
    for tok in tokens:
        if tok.startswith(".regex/"):
            pat = tok[len(".regex/"):]
            try:
                if not re.search(pat, content):
                    missing.append(f"pattern `{pat}`")
            except re.error:
                continue
        else:
            if tok not in content:
                missing.append(f"`{tok}`")
    return (len(missing) == 0), missing


# ---------------------------------------------------------------------------
# Pull _frontend_files_plan + _frontend_emergency_scaffold + helpers from
# routes/codegen.py without dragging motor in (same pattern as the
# standalone tests).
# ---------------------------------------------------------------------------
def _grab_def(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\([^)]*\)[^:]*:\n(?:[ \t].*\n|\n)+",
                  src, re.MULTILINE)
    assert m, f"could not find def {name}() in routes/codegen.py"
    return m.group(0)


def _load_codegen_helpers():
    text = (_BACKEND / "routes" / "codegen.py").read_text()
    ns: dict = {"re": re, "Dict": dict, "List": list}
    for fn in (
        "_pascal", "_kebab", "_resource_of", "_group_endpoints_by_resource",
        "_ep_parts", "_ep_method_name",
        "_frontend_emergency_scaffold", "_emergency_scaffold",
        "_frontend_files_plan",
    ):
        exec(_grab_def(text, fn), ns)
    return ns


_NS = _load_codegen_helpers()
_frontend_files_plan = _NS["_frontend_files_plan"]
_frontend_emergency_scaffold = _NS["_frontend_emergency_scaffold"]


# ---------------------------------------------------------------------------
# 1. Frontend plan ships every artefact needed for `npm run dev` standalone.
# ---------------------------------------------------------------------------
def _frontend_svc(pages=None, dependencies=None):
    # Use sentinels so an explicit empty list isn't replaced with the
    # default — important for the Dashboard-fallback test.
    return {
        "name": "web",
        "frontend": True,
        "framework": "react",
        "pages": [] if pages is None else pages,
        "dependencies": ["orders", "users"] if dependencies is None else dependencies,
    }


def test_frontend_plan_has_build_manifest_and_entrypoint():
    plan = _frontend_files_plan(_frontend_svc())
    paths = [f["path"] for f in plan]
    for required in (
        "frontend/package.json",
        "frontend/tsconfig.json",
        "frontend/index.html",
        "frontend/src/main.tsx",
        "frontend/Dockerfile",
        "frontend/README.md",
    ):
        assert required in paths, (
            f"frontend plan missing {required!r}; without it the user "
            f"cannot `npm install && npm run dev` the UI standalone."
        )


def test_frontend_plan_has_api_client_store_routes_role_guard():
    plan = _frontend_files_plan(_frontend_svc())
    paths = [f["path"] for f in plan]
    for required in (
        "frontend/src/api/client.ts",
        "frontend/src/store/auth.ts",
        "frontend/src/routes.tsx",
        "frontend/src/components/RoleGate.tsx",
    ):
        assert required in paths, f"frontend plan missing {required!r}"


def test_frontend_plan_generates_one_page_per_resource():
    plan = _frontend_files_plan(_frontend_svc(pages=["Orders", "Invoices", "Users"]))
    page_paths = [f["path"] for f in plan
                  if f["path"].startswith("frontend/src/pages/")]
    assert sorted(page_paths) == sorted([
        "frontend/src/pages/Orders.tsx",
        "frontend/src/pages/Invoices.tsx",
        "frontend/src/pages/Users.tsx",
    ])


def test_frontend_plan_falls_back_to_dashboard_when_no_pages_or_deps():
    plan = _frontend_files_plan(_frontend_svc(pages=[], dependencies=[]))
    page_paths = [f["path"] for f in plan
                  if f["path"].startswith("frontend/src/pages/")]
    assert page_paths == ["frontend/src/pages/Dashboard.tsx"]


# ---------------------------------------------------------------------------
# 2. Every frontend emergency scaffold passes the structural validator.
# ---------------------------------------------------------------------------
_SCAFFOLD_CTYPES = [
    "page", "api_client", "role_guard", "route_config", "store", "bootstrap",
]


@pytest.mark.parametrize("ctype", _SCAFFOLD_CTYPES)
def test_frontend_emergency_scaffold_passes_validator(ctype):
    file_def = {
        "path": f"frontend/src/pages/Dashboard.tsx" if ctype == "page"
                else f"frontend/src/{ctype}.tsx",
        "ctype": ctype,
        "type": "route" if ctype == "page" else "service",
        "page_name": "Dashboard" if ctype == "page" else "",
    }
    svc = {"name": "web", "frontend": True, "framework": "react"}
    proj = {"name": "demo", "source_tech": "php", "target_tech": "react"}
    body = _frontend_emergency_scaffold(
        file_def["path"], ctype, file_def, svc, proj,
    )
    assert body and len(body) >= 80, f"empty scaffold for ctype={ctype}"
    ok, missing = _validate_structure_frontend(body, ctype, "react")
    assert ok, (
        f"FRONTEND emergency scaffold for ctype={ctype!r} FAILED "
        f"structural validator (missing: {missing}). The scaffold is "
        f"what we ship when the LLM fails — if the scaffold itself is "
        f"malformed, broken .tsx reaches the user.\nBody:\n{body}"
    )


# ---------------------------------------------------------------------------
# 3. Config scaffolds (package.json + tsconfig.json + index.html) parse.
# ---------------------------------------------------------------------------
def _scaffold_path(path: str, ctype: str = "scaffold") -> str:
    file_def = {"path": path, "ctype": ctype, "type": "config"}
    svc = {"name": "web", "frontend": True, "framework": "react"}
    proj = {"name": "demo", "source_tech": "php", "target_tech": "react"}
    return _frontend_emergency_scaffold(path, ctype, file_def, svc, proj)


def test_frontend_package_json_is_valid_json_and_runnable():
    body = _scaffold_path("frontend/package.json")
    data = json.loads(body)
    assert "react" in data["dependencies"]
    assert "react-dom" in data["dependencies"]
    assert "react-router-dom" in data["dependencies"]
    assert "@tanstack/react-query" in data["dependencies"]
    assert "zustand" in data["dependencies"]
    assert "vite" in data["devDependencies"]
    assert "typescript" in data["devDependencies"]
    assert "dev" in data["scripts"]
    assert data["scripts"]["dev"] == "vite"


def test_frontend_tsconfig_is_valid_json_with_strict_mode():
    body = _scaffold_path("frontend/tsconfig.json")
    data = json.loads(body)
    co = data["compilerOptions"]
    assert co.get("strict") is True
    assert co.get("jsx") == "react-jsx"
    assert co.get("paths", {}).get("@/*") == ["src/*"]


def test_frontend_index_html_wires_main_tsx_root():
    body = _scaffold_path("frontend/index.html")
    assert "<div id=\"root\"></div>" in body
    assert "/src/main.tsx" in body
    assert "<!doctype html>" in body.lower() or "<!DOCTYPE html>" in body


# ---------------------------------------------------------------------------
# 4. FRONTEND_REQUIRED_TOKENS coverage. Every ctype the plan emits must
#    have at least one mandatory marker on the React side.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ctype", _SCAFFOLD_CTYPES)
def test_react_required_tokens_defined(ctype):
    assert (ctype, "react") in FRONTEND_REQUIRED_TOKENS, (
        f"FRONTEND_REQUIRED_TOKENS has no entry for (react, {ctype}). "
        f"Without it the structural validator silently passes any .tsx "
        f"content for this ctype."
    )
    assert FRONTEND_REQUIRED_TOKENS[(ctype, "react")], "token list is empty"


@pytest.mark.parametrize("framework", ["angular", "vue", "svelte"])
def test_alternative_frontend_frameworks_have_at_least_one_marker(framework):
    fw_keys = [k for k in FRONTEND_REQUIRED_TOKENS if k[1] == framework]
    assert fw_keys, (
        f"FRONTEND_REQUIRED_TOKENS has no entries for {framework!r}; "
        f"users who pick this framework get NO structural validation."
    )


# ---------------------------------------------------------------------------
# 5. The shipped FRONTEND_FILE_TYPE_SKELETONS themselves pass the
#    validator (after placeholder substitution). The skeleton is what
#    the prompt shows the LLM as "MANDATORY SKELETON" — a malformed
#    skeleton trains the LLM to omit framework markers.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(FRONTEND_FILE_TYPE_SKELETONS))
def test_shipped_frontend_skeleton_passes_validator(key):
    skel = FRONTEND_FILE_TYPE_SKELETONS[key]
    rendered = (
        skel
        .replace("<Resource>", "Order")
        .replace("<resource>", "order")
        .replace("<resources>", "orders")
        .replace("<NN>", "01")
        .replace("<legacy_template>", "orders.jsp")
        .replace("<LEGACY_ROLE>", "ROLE_ADMIN")
    )
    ctype, framework = key
    ok, missing = _validate_structure_frontend(rendered, ctype, framework)
    assert ok, (
        f"FRONTEND_FILE_TYPE_SKELETONS[{key}] does NOT pass its own "
        f"validator (missing: {missing}). Shipping a malformed skeleton "
        f"trains the LLM to omit framework markers."
    )


# ---------------------------------------------------------------------------
# 6. get_frontend_instruction(component_type, framework) attaches the
#    skeleton few-shot and names the markers — so the LLM is told
#    BEFORE generation what the validator will look for.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ctype", _SCAFFOLD_CTYPES)
def test_get_frontend_instruction_includes_skeleton(ctype):
    instr = get_frontend_instruction(ctype, "react")
    assert "MANDATORY SKELETON" in instr, (
        f"get_frontend_instruction(ctype={ctype!r}, react) doesn't ship "
        f"the few-shot skeleton; LLM has no template to follow."
    )
    # And the rendered skeleton must itself name at least one of the
    # required tokens so the validator + the few-shot stay in sync.
    tokens = [t for t in frontend_required_tokens_for(ctype, "react")
              if not t.startswith(".regex/")]
    if tokens:
        assert any(t in instr for t in tokens), (
            f"none of the required literal tokens ({tokens}) appears in "
            f"the skeleton instruction for ctype={ctype}"
        )


def test_get_frontend_instruction_back_compat_default_framework():
    """Older call sites that didn't pass `framework` still get the
    React skeleton (default)."""
    instr = get_frontend_instruction("page")
    assert "MANDATORY SKELETON" in instr
    assert "useQuery" in instr or "useState" in instr


