"""iter-13.83 — Structural-validator + emergency-scaffold contract tests.

The user complained that generated Java backend code lacked
``@RestController`` / ``@Service`` / ``@Entity`` annotations and was not
production-ready. These tests pin the contract that:

  1. ``file_templates.required_tokens_for`` returns the mandatory
     framework markers for every supported (file_type, backend_lang).
  2. ``_emergency_scaffold`` in ``routes.codegen`` always emits content
     that passes the structural validator (because Gap Recovery may
     never run if the user discards a broken first pass).
  3. The seeded ``codegen.service`` prompt template ``.format()``-s
     cleanly with every required slot AND contains the explicit
     framework markers (``@RestController``, ``@Service``,
     ``[ApiController]``, ``APIRouter``, ...) so the LLM cannot claim
     it was never told about them.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from codegen.file_templates import (  # noqa: E402
    FILE_TYPE_SKELETONS,
    REQUIRED_TOKENS,
    get_file_instruction,
    required_tokens_for,
)


# ---------------------------------------------------------------------------
# Local copy of routes.codegen._validate_structure (the actual one lives in
# routes/codegen.py which transitively imports motor — not available in the
# unit-test environment without an event loop). The implementation is a
# straight copy; the validator's behaviour is what we're pinning, not its
# call-site wiring.
# ---------------------------------------------------------------------------
def _validate_structure(content: str, file_type: str, backend_lang: str):
    if not content or len(content.strip()) < 40:
        return False, ["content too short / empty"]
    tokens = required_tokens_for(file_type or "", backend_lang or "")
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
# 1. The required-tokens table covers every framework marker the user
#    explicitly named in their complaint.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,marker", [
    (("controller", "java"),   "@RestController"),
    (("controller", "java"),   "@RequestMapping"),
    (("service",    "java"),   "@Service"),
    (("repository", "java"),   ".regex/JpaRepository|@Repository"),
    (("entity",     "java"),   "@Entity"),
    (("entity",     "java"),   "@Table"),
    (("errors",     "java"),   "@RestControllerAdvice"),
    (("security",   "java"),   "@Configuration"),
    (("bootstrap",  "java"),   "@SpringBootApplication"),
    (("controller", "python"), "APIRouter"),
    (("controller", "dotnet"), "[ApiController]"),
    (("controller", "nodejs"), "export default router"),
])
def test_required_tokens_cover_user_reported_gaps(key, marker):
    tokens = REQUIRED_TOKENS[key]
    assert marker in tokens, (
        f"REQUIRED_TOKENS[{key}] is missing {marker!r}; the user's complaint "
        f"about garbage code was exactly that this marker was absent from "
        f"generated files. Re-add it to file_templates.REQUIRED_TOKENS."
    )


# ---------------------------------------------------------------------------
# 2. Structural validator rejects the canonical garbage shapes.
# ---------------------------------------------------------------------------
def test_validator_rejects_java_controller_without_restcontroller():
    bad = (
        "package com.lama.x.web;\n"
        "public class ChangePwdController {\n"
        "    public void changePassword() {\n"
        "        throw new UnsupportedOperationException();\n"
        "    }\n"
        "}\n"
    )
    ok, missing = _validate_structure(bad, "controller", "java")
    assert not ok
    assert any("@RestController" in m for m in missing)


def test_validator_rejects_java_service_without_service_annotation():
    bad = (
        "package com.lama.x.service;\n"
        "public class FooService {\n"
        "    public Object doStuff() { return null; }\n"
        "}\n"
    )
    ok, missing = _validate_structure(bad, "service", "java")
    assert not ok
    assert any("@Service" in m for m in missing)


def test_validator_rejects_java_entity_without_jpa_annotations():
    bad = (
        "package com.lama.x.domain;\n"
        "public class Foo { Long id; String name; }\n"
    )
    ok, missing = _validate_structure(bad, "entity", "java")
    assert not ok
    assert any("@Entity" in m for m in missing)


def test_validator_accepts_well_formed_java_controller():
    good = (
        "package com.lama.x.web;\n"
        "import org.springframework.web.bind.annotation.*;\n"
        "@RestController\n"
        "@RequestMapping(\"/api/v1/orders\")\n"
        "public class OrderController {\n"
        "    @GetMapping(\"/{id}\") public String get(@PathVariable String id) { return id; }\n"
        "}\n"
    )
    ok, missing = _validate_structure(good, "controller", "java")
    assert ok, f"well-formed controller rejected: {missing}"


# ---------------------------------------------------------------------------
# 3. The deterministic emergency scaffold always passes the validator
#    for every (file_type, backend_lang) we ship a skeleton for. This is
#    the contract that prevents the "saved garbage" regression — when
#    the LLM fails the validator on the final attempt, codegen falls
#    back to this scaffold, so the scaffold MUST itself be valid.
# ---------------------------------------------------------------------------
def _import_emergency_scaffold():
    """Import _emergency_scaffold without dragging motor + asyncio in.

    routes.codegen unconditionally imports motor at module load; we can't
    avoid that by symbol-level import. So we synthesise the scaffold call
    by reading the function source via importlib and exec'ing it in a
    minimal namespace. Cheaper to keep in-test than to refactor the
    1900-line route module.
    """
    src_path = _BACKEND / "routes" / "codegen.py"
    text = src_path.read_text()
    # Pull out the helpers needed by _emergency_scaffold.
    def _grab(name):
        m = re.search(rf"^def {name}\([^)]*\)[^:]*:\n(?:[ \t].*\n|\n)+", text, re.MULTILINE)
        assert m, f"could not find def {name} in routes/codegen.py"
        return m.group(0)

    ns = {"re": re}
    for fn in ("_pascal", "_kebab", "_ep_parts", "_ep_method_name", "_emergency_scaffold"):
        exec(_grab(fn), ns)
    return ns["_emergency_scaffold"]


_emergency_scaffold = _import_emergency_scaffold()


_SCAFFOLD_CASES = [
    # (file_type, backend_lang, plan-extras)
    ("controller", "java",   {"resource": "order", "endpoints": ["GET /orders/{id}", "POST /orders"]}),
    ("service",    "java",   {"resource": "order", "endpoints": ["GET /orders/{id}"]}),
    ("entity",     "java",   {"table": "orders"}),
    ("repository", "java",   {"table": "orders"}),
    ("dto",        "java",   {"resource": "order"}),
    ("errors",     "java",   {}),
    ("security",   "java",   {}),
    ("bootstrap",  "java",   {}),
    ("test",       "java",   {"endpoints": ["GET /orders/{id}"]}),
    ("controller", "python", {"resource": "order", "endpoints": ["GET /orders/{id}"]}),
    ("controller", "dotnet", {"resource": "order", "endpoints": ["GET /orders/{id}"]}),
    ("bootstrap",  "dotnet", {}),
    ("controller", "nodejs", {"resource": "order", "endpoints": ["GET /orders/{id}"]}),
    ("bootstrap",  "nodejs", {}),
]


@pytest.mark.parametrize("ftype,lang,extras", _SCAFFOLD_CASES)
def test_emergency_scaffold_passes_structural_validator(ftype, lang, extras):
    proj = {"name": "p", "source_tech": "php", "target_tech": lang}
    svc = {
        "name": "order_svc",
        "backend_lang": lang,
        "tables": [extras.get("table")] if extras.get("table") else [],
        "api_endpoints": extras.get("endpoints", []),
        "dependencies": [],
    }
    path_ext = {"java": "java", "python": "py", "dotnet": "cs", "nodejs": "js"}[lang]
    file_def = {
        "path": f"services/order_svc/{ftype}.{path_ext}",
        "type": ftype,
        "language": lang,
        **extras,
    }
    body = _emergency_scaffold(file_def, svc, proj)
    assert body and len(body) >= 80, f"scaffold too short for ({ftype}, {lang}): {body!r}"
    ok, missing = _validate_structure(body, ftype, lang)
    assert ok, (
        f"DETERMINISTIC SCAFFOLD failed structural validator for "
        f"({ftype}, {lang}). Missing: {missing}. The scaffold is what "
        f"codegen falls back to when the LLM emits garbage; if the "
        f"scaffold itself is structurally invalid, broken code reaches "
        f"the user. Body was:\n{body}"
    )


# ---------------------------------------------------------------------------
# 4. The seeded codegen.service prompt template still formats cleanly
#    with every required slot AND names every framework marker.
# ---------------------------------------------------------------------------
def _load_codegen_service_template():
    src = (_BACKEND / "seed.py").read_text()
    m = re.search(r'"key":\s*"codegen\.service".*?"template":\s*"""(.*?)"""', src, re.DOTALL)
    assert m, "codegen.service prompt template not found in seed.py"
    return m.group(1)


def test_codegen_service_prompt_format_clean():
    tmpl = _load_codegen_service_template()
    slots = dict(
        project_name="p", source_tech="php", target_tech="java",
        backend_lang="java", service_name="s", service_responsibility="r",
        service_dependencies="", service_endpoints="", service_ddl="",
        graph_subgraph="", legacy_evidence="", service_lld="",
        api_contract="", srs_use_cases="", srs_nfr="",
        file_path="X.java", file_type="controller",
        file_type_instructions="...",
    )
    out = tmpl.format(**slots)
    assert len(out) > 5000


@pytest.mark.parametrize("marker", [
    "@RestController", "@Service", "@Entity", "@Repository",
    "@RestControllerAdvice", "@SpringBootApplication",
    "APIRouter", "[ApiController]",
    "mandatory_structural_markers",
])
def test_codegen_service_prompt_names_framework_markers(marker):
    tmpl = _load_codegen_service_template()
    assert marker in tmpl, (
        f"codegen.service prompt template no longer mentions {marker!r}. "
        f"Without this marker in the prompt the LLM has license to emit "
        f"unannotated classes; this was the iter-13.83 regression."
    )


# ---------------------------------------------------------------------------
# 5. Every Java skeleton in file_templates.FILE_TYPE_SKELETONS itself
#    passes the structural validator. The skeleton is shipped as the
#    "MANDATORY SKELETON" in the prompt — if the skeleton is malformed,
#    the LLM will faithfully reproduce malformed output.
# ---------------------------------------------------------------------------
_SKELETON_VALIDATABLE = {
    ("controller", "java"),
    ("service",    "java"),
    ("repository", "java"),
    ("entity",     "java"),
    ("dto",        "java"),
    ("errors",     "java"),
    ("security",   "java"),
    ("bootstrap",  "java"),
    ("controller", "python"),
    ("controller", "dotnet"),
    ("controller", "nodejs"),
}


@pytest.mark.parametrize("key", sorted(_SKELETON_VALIDATABLE))
def test_shipped_skeleton_passes_validator(key):
    skel = FILE_TYPE_SKELETONS[key]
    # Replace placeholder tokens so the validator sees realistic names.
    rendered = (
        skel
        .replace("<service>", "ordersvc")
        .replace("<resource>", "order")
        .replace("<resources>", "orders")
        .replace("<Resource>", "Order")
        .replace("<LegacyClass>", "OrderDao")
        .replace("<LegacyDao>", "OrderDao")
        .replace("<LegacyRole>", "ROLE_ADMIN")
        .replace("<LEGACY_ROLES>", "\"ROLE_ADMIN\"")
        .replace("<LEGACY_ROLE>", "ROLE_ADMIN")
        .replace("<legacy_table>", "orders")
        .replace("<legacy_column>", "name")
        .replace("<column>", "name")
        .replace("<Project>", "Ordersvc")
        .replace("<NN>", "01")
        .replace("<method>", "find")
        .replace("<ID>", "01")
    )
    ftype, lang = key
    ok, missing = _validate_structure(rendered, ftype, lang)
    assert ok, (
        f"FILE_TYPE_SKELETONS[{key}] does NOT pass its own structural "
        f"validator (missing: {missing}). Shipping a malformed skeleton "
        f"trains the LLM to omit framework markers."
    )

