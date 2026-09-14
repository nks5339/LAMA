"""iter-13.84 — Per-service "must be independently runnable" contract.

The user's promise: after CodeGen, EVERY service folder must be a fully
self-contained, production-ready, build-and-run-able project — i.e.
`cd services/<svc> && <build-tool> run` works without referring to any
sibling service. This file pins that contract for every supported
backend language.

Each service plan MUST include:
  • a build manifest at the service root (pom.xml / requirements.txt /
    package.json / .csproj / go.mod / composer.json / Gemfile / Cargo.toml)
  • a bootstrap entrypoint that boots the framework
  • an app config (application.yml / .env.example / appsettings.json / ...)
  • a Dockerfile so the service can be containerised standalone
  • a README

When the service exposes endpoints, the plan additionally guarantees:
  • >= 1 controller per resource group
  • >= 1 service per resource group
  • >= 1 entity per legacy table
  • >= 1 repository per legacy table
  • a test file

When the service exposes ZERO endpoints (library / shared-kernel kind)
the plan MUST NOT generate HTTP-only files (SecurityConfig, controllers,
tests) — pinned because the user previously saw a meaningless
RootController in a library service (iter-13.81.16 regression).

And — most importantly — every emergency-scaffold output for the
manifest files MUST be parseable by the target stack's build tool
(JSON-loadable for package.json + appsettings.json, well-formed XML
for pom.xml + .csproj, valid YAML for application.yml, etc.).
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Import the codegen helpers without dragging motor / asyncio in. The
# pattern (already used by test_codegen_structural.py) is to read the
# source, regex-extract each helper, and exec into a clean namespace.
# ---------------------------------------------------------------------------
def _grab_def(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\([^)]*\)[^:]*:\n(?:[ \t].*\n|\n)+", src, re.MULTILINE)
    assert m, f"could not find def {name}() in routes/codegen.py"
    return m.group(0)


def _grab_assign(src: str, name: str) -> str:
    """Extract a module-level constant assignment, e.g. `_FOO: str = "bar"`.

    The extracted helpers read module globals that the exec namespace does not
    otherwise contain. Hardcoding their values here would silently rot the day
    someone changes them in codegen.py, so read the real assignment instead --
    the whole point of this harness is to test the real source.
    """
    m = re.search(rf"^{name}\s*(?::[^=]+)?=\s*.+$", src, re.MULTILINE)
    assert m, f"could not find module global {name} in routes/codegen.py"
    return m.group(0)


def _load_codegen_helpers():
    text = (_BACKEND / "routes" / "codegen.py").read_text()
    ns: dict = {"re": re, "Dict": dict, "List": list}
    # The scaffolds read these module globals (codegen.py:81-83). Without them
    # every extracted function raises NameError at call time.
    for const in (
        "_ACTIVE_PROJECT_SLUG", "_ACTIVE_JAVA_GROUP", "_ACTIVE_JAVA_GROUP_PATH",
    ):
        exec(_grab_assign(text, const), ns)
    # Order matters — helpers depend on each other.
    for fn in (
        "_pascal", "_kebab", "_resource_of", "_group_endpoints_by_resource",
        "_ep_parts", "_ep_method_name",
        "_emergency_scaffold", "_backend_files_plan",
    ):
        exec(_grab_def(text, fn), ns)
    return ns


_NS = _load_codegen_helpers()
_backend_files_plan = _NS["_backend_files_plan"]
_emergency_scaffold = _NS["_emergency_scaffold"]


# ---------------------------------------------------------------------------
# Fixtures: one realistic service per supported backend language.
# ---------------------------------------------------------------------------
def _svc(lang: str, *, endpoints=True) -> dict:
    base = {
        "name": "orders",
        "backend_lang": lang,
        "responsibility": "manages purchase orders",
        "tables": ["orders", "order_items"] if endpoints else [],
        "api_endpoints": (
            ["GET /orders/{id}", "POST /orders", "PUT /orders/{id}",
             "DELETE /orders/{id}", "GET /orders/{id}/items"]
            if endpoints else []
        ),
        "dependencies": ["users"],
    }
    return base


_LANGS = ["java", "python", "nodejs", "dotnet", "go"]


# ---------------------------------------------------------------------------
# 1. Every service plan ships the universal "independently runnable"
#    artefacts: build manifest + bootstrap + config + Dockerfile + README.
# ---------------------------------------------------------------------------
_MANIFEST_FILE = {
    "java":   "pom.xml",
    "python": "requirements.txt",
    "nodejs": "package.json",
    "dotnet": ".csproj",
    "go":     "go.mod",
}

_APP_CONFIG_HINT = {
    "java":   "application.yml",
    "python": ".env.example",
    "nodejs": ".env.example",
    "dotnet": "appsettings.json",
    "go":     ".env.example",
}

_BOOTSTRAP_HINT = {
    "java":   "Application.java",
    "python": "main.py",
    "nodejs": "server.js",
    "dotnet": "Program.cs",
    "go":     "main.go",
}


@pytest.mark.parametrize("lang", _LANGS)
def test_service_plan_has_build_manifest(lang):
    plan = _backend_files_plan(_svc(lang))
    paths = [f["path"] for f in plan]
    assert any(p.endswith(_MANIFEST_FILE[lang]) for p in paths), (
        f"{lang} service plan missing {_MANIFEST_FILE[lang]} — "
        f"service won't be independently buildable. paths={paths!r}"
    )


@pytest.mark.parametrize("lang", _LANGS)
def test_service_plan_has_bootstrap_entrypoint(lang):
    plan = _backend_files_plan(_svc(lang))
    paths = [f["path"] for f in plan]
    assert any(p.endswith(_BOOTSTRAP_HINT[lang]) for p in paths), (
        f"{lang} plan missing bootstrap entry {_BOOTSTRAP_HINT[lang]}"
    )


@pytest.mark.parametrize("lang", _LANGS)
def test_service_plan_has_app_config(lang):
    plan = _backend_files_plan(_svc(lang))
    paths = [f["path"] for f in plan]
    assert any(_APP_CONFIG_HINT[lang] in p for p in paths), (
        f"{lang} plan missing app-config {_APP_CONFIG_HINT[lang]}"
    )


@pytest.mark.parametrize("lang", _LANGS)
def test_service_plan_has_dockerfile_and_readme(lang):
    plan = _backend_files_plan(_svc(lang))
    paths = [f["path"] for f in plan]
    assert any(p.endswith("Dockerfile") for p in paths), f"no Dockerfile for {lang}"
    assert any(p.endswith("README.md") for p in paths), f"no README for {lang}"


# ---------------------------------------------------------------------------
# 2. When endpoints + tables are present, controller / service /
#    entity / repository / test slots are populated.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", _LANGS)
def test_service_plan_covers_controllers_services_entities_repos(lang):
    plan = _backend_files_plan(_svc(lang))
    types = {f.get("type") for f in plan}
    for required in ("controller", "service", "entity", "repository"):
        assert required in types, (
            f"{lang} plan missing {required!r} type entries; "
            f"got types={sorted(t for t in types if t)}"
        )
    # Tests are required for every backend language.
    assert "test" in types, f"{lang} plan missing test type entries"


@pytest.mark.parametrize("lang", ["java", "python", "nodejs", "dotnet", "go"])
def test_one_controller_per_resource_group(lang):
    plan = _backend_files_plan(_svc(lang))
    # All endpoints in our fixture share the `/orders` resource so we
    # expect exactly ONE controller. Add a second resource and re-check.
    svc = _svc(lang)
    svc["api_endpoints"].append("GET /invoices/{id}")
    plan2 = _backend_files_plan(svc)
    ctrls1 = [f for f in plan  if f.get("type") == "controller"]
    ctrls2 = [f for f in plan2 if f.get("type") == "controller"]
    assert len(ctrls1) == 1, f"{lang}: expected 1 controller for single-resource fixture, got {len(ctrls1)}"
    assert len(ctrls2) == 2, f"{lang}: expected 2 controllers after adding a 2nd resource, got {len(ctrls2)}"


@pytest.mark.parametrize("lang", _LANGS)
def test_one_entity_and_repo_per_table(lang):
    plan = _backend_files_plan(_svc(lang))
    entities = [f for f in plan if f.get("type") == "entity"]
    repos    = [f for f in plan if f.get("type") == "repository"]
    # 2 tables in the fixture → 2 entities + 2 repos.
    assert len(entities) == 2, f"{lang}: 1 entity per table expected, got {len(entities)}"
    assert len(repos)    == 2, f"{lang}: 1 repo per table expected, got {len(repos)}"


# ---------------------------------------------------------------------------
# 3. Library / no-API service must NOT carry HTTP-only files
#    (iter-13.81.16 regression: RootController in a library service).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["java", "python", "nodejs", "dotnet", "go"])
def test_library_service_omits_http_only_files(lang):
    svc = _svc(lang, endpoints=False)
    svc["name"] = "shared-kernel"
    svc["kind"] = "library"
    plan = _backend_files_plan(svc)
    types = {f.get("type") for f in plan}
    assert "controller" not in types, (
        f"{lang} library plan unexpectedly has a controller: "
        f"{[f['path'] for f in plan if f.get('type')=='controller']}"
    )
    assert "security" not in types, f"{lang} library plan should NOT carry SecurityConfig"
    assert "test"     not in types, f"{lang} library plan should NOT carry tests"


# ---------------------------------------------------------------------------
# 4. The emergency-scaffold output for every build manifest is parseable
#    by the target stack's build tool. This is the contract that
#    guarantees `<build-tool> run` won't choke on a malformed manifest
#    when the LLM fails and we fall back to deterministic scaffolding.
# ---------------------------------------------------------------------------
def _scaffold_for(path: str, lang: str, ftype: str = "config") -> str:
    file_def = {"path": path, "type": ftype, "language": lang}
    svc = _svc(lang)
    proj = {"name": "p", "source_tech": "php", "target_tech": lang}
    return _emergency_scaffold(file_def, svc, proj)


def test_scaffold_pom_xml_is_well_formed_xml():
    body = _scaffold_for("services/orders/pom.xml", "java")
    root = ET.fromstring(body)
    # spring-boot-starter-parent + spring-boot-maven-plugin must be present
    assert "spring-boot-starter-parent" in body
    assert "spring-boot-maven-plugin" in body
    # And it should declare java.version
    assert "<java.version>" in body


def test_scaffold_requirements_txt_lists_runnable_stack():
    body = _scaffold_for("services/orders/requirements.txt", "python")
    for pkg in ("fastapi", "uvicorn", "sqlalchemy", "pydantic", "psycopg2"):
        assert pkg in body, f"requirements.txt missing {pkg}"


def test_scaffold_package_json_is_valid_json_and_runnable():
    body = _scaffold_for("services/orders/package.json", "nodejs")
    data = json.loads(body)
    assert "scripts" in data and "start" in data["scripts"]
    assert "express" in data.get("dependencies", {})
    assert data.get("engines", {}).get("node", "").startswith(">=")


def test_scaffold_csproj_is_well_formed_xml():
    body = _scaffold_for("services/orders/Orders.csproj", "dotnet")
    root = ET.fromstring(body)
    assert root.tag == "Project"
    assert "Microsoft.NET.Sdk.Web" in root.get("Sdk", "")


def test_scaffold_go_mod_is_well_formed():
    body = _scaffold_for("services/orders/go.mod", "go")
    assert body.splitlines()[0].startswith("module ")
    assert "go 1.22" in body
    assert "github.com/gin-gonic/gin" in body


def test_scaffold_appsettings_json_is_valid_json():
    body = _scaffold_for("services/orders/appsettings.json", "dotnet")
    data = json.loads(body)
    assert "ConnectionStrings" in data and "Default" in data["ConnectionStrings"]


def test_scaffold_application_yml_is_valid_yaml():
    pytest.importorskip("yaml")
    import yaml
    body = _scaffold_for("services/orders/src/main/resources/application.yml", "java")
    data = yaml.safe_load(body)
    assert data["spring"]["application"]["name"] == "orders"
    assert data["server"]["port"] == 8080


def test_scaffold_dockerfile_per_language_has_workdir_and_entrypoint():
    for lang in _LANGS:
        body = _scaffold_for(f"services/orders/Dockerfile", lang, ftype="dockerfile")
        assert "WORKDIR /app" in body, f"{lang} Dockerfile missing WORKDIR"
        assert any(tok in body for tok in ("ENTRYPOINT", "CMD")), f"{lang} Dockerfile missing CMD/ENTRYPOINT"
        assert "EXPOSE" in body, f"{lang} Dockerfile missing EXPOSE"


# ---------------------------------------------------------------------------
# 5. The /env.example scaffold lists the minimum env vars any service
#    needs to actually start (port, DB URL, log level). Production-ready.
# ---------------------------------------------------------------------------
def test_scaffold_env_example_has_minimum_runtime_envs():
    body = _scaffold_for("services/orders/.env.example", "python")
    for var in ("PORT", "DATABASE_URL", "LOG_LEVEL"):
        assert var in body, f".env.example missing {var}"


# ---------------------------------------------------------------------------
# 6. Workflow / approval / validation seeds — pin that the codegen
#    legacy-retrieval seeds include the workflow/approval vocabulary
#    so state-machine and approval-chain code gets surfaced into the
#    prompt. This is iter-13.84's explicit user-asked contract.
# ---------------------------------------------------------------------------
def test_codegen_seeds_include_workflow_and_approval_keywords():
    text = (_BACKEND / "routes" / "codegen.py").read_text()
    # Find the legacy_seeds list inside _gen_one_file's backend branch.
    m = re.search(r"legacy_seeds = list\(ddl_target_tables\) \+ \[(.*?)\]",
                  text, re.DOTALL)
    assert m, "legacy_seeds construction not found in routes/codegen.py"
    body = m.group(1)
    for kw in ("workflow", "approval", "state", "transition",
               "status", "validation", "validator", "rule",
               "permission", "audit"):
        assert f'"{kw}"' in body, (
            f"legacy_seeds missing {kw!r} — deep-legacy retrieval "
            f"won't surface state-machine / approval code for codegen."
        )


def test_codegen_consumes_business_rule_srs_sections():
    """The codegen.service prompt should be fed not just use cases but
    also the business-rule (specific_requirements), validation, and
    actors SRS sections. Without these the LLM can't enforce 100%
    validation-rule + workflow + approval-chain parity."""
    text = (_BACKEND / "routes" / "codegen.py").read_text()
    for sec in ("specific_requirements", "validation_verification",
                "actors_use_case_inventory"):
        assert sec in text, (
            f"codegen does not consume SRS section {sec!r}; "
            "business-rule + validation + approval-chain parity is at "
            "risk. Surface it inside _gen_one_file."
        )

