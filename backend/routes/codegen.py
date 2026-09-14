"""Stage 4 — Code Generation. Pipeline-gated by Architecture frozen."""
import io
import json
import os
import re
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db import (
    projects, audit_log, prompts as prompts_col, project_prompts,
    arch_documents, arch_services, codegen_files, codegen_runs,
    messages as messages_col, conversations, stage_context as stage_context_col,
    kb_entities, srs_documents, kb_chunks, kb_files,
    # iter-13.115 — codegen now grounds the LLM in the KB (business
    # ontology + deep legacy-analysis) in addition to graph/RAG/legacy
    # source. Without these the LLM has no idea which business entity
    # each table/route belongs to → it can't generate mappers/validators
    # whose names match the SRS use cases.
    business_ontologies, legacy_analysis,
    # iter-13.119 — automated validation + improvement loop
    parity_runs,
)
from llm import fabric_call as chat_completion
from llm import fabric_call_with_session  # iter-13.100 — rolling-memory sessions
from llm import set_current_project_id  # iter-13.38 — Factory.ai context propagation
from llm import set_current_agent_key   # iter-13.81.3 — per-pipeline-step Factory bucket override
from kb.vector_store import (
    search as qdrant_search,
    search_by_entities as qdrant_search_by_entities,
    search_many as qdrant_search_many,
)
from pipeline import require_stage_context, save_stage_context
from kb.legacy_tables import build_oltp_ddl_from_kb  # iter-14.33 — legacy-table harvest
from codegen.zip_builder import build_zip
from codegen.disk_exporter import (  # iter-13.110 / 13.111
    export_project_to_disk,
    resolve_export_root,
    project_export_path,
)
from codegen.file_templates import (
    get_file_instruction,
    get_frontend_instruction,
    required_tokens_for,
    frontend_required_tokens_for,  # iter-13.85 — frontend structural validation
)
# iter-13.59 — cross-cutting utility services (audit-logger etc.) are
# materialised via deterministic templates instead of LLM calls.
from codegen.utility_templates import (
    utility_service_files_plan,
    injection_files_for_service,
    bootstrap_wiring_hint,
    render_file as render_utility_file,
)

logger = logging.getLogger("lama.codegen")
router = APIRouter(prefix="/codegen", tags=["codegen"])

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SEC = 60 * 60

# ---------------------------------------------------------------------------
# iter-14.20.9 — Project-scoped Java package group.
#
# Previously every scaffold hard-coded `com.lama.<svc>` for the Java
# groupId / package prefix. That made the generated microservice bundle
# look like N different `com.lama.*` apps instead of one merged
# application. We now derive `com.<projSlug>` from the current project
# and every helper that emits a `package` declaration or file-path
# reads these module-level vars via `_set_active_java_group()` which
# `_run_codegen_job` calls once per run before any file is planned.
#
# LAMA is single-tenant + single active project, so a module-level
# variable is safe — but we still snapshot it into each scaffold via
# a normal read so unit-tests can inject explicitly.
# ---------------------------------------------------------------------------
_ACTIVE_PROJECT_SLUG: str = "lama"
_ACTIVE_JAVA_GROUP: str = "com.lama"
_ACTIVE_JAVA_GROUP_PATH: str = "com/lama"


def _slugify_ident(raw: str, fallback: str = "app") -> str:
    """Lowercase alnum-only slug for a package / groupId component."""
    s = re.sub(r"[^a-z0-9]", "", (raw or "").lower())
    return s or fallback


def _set_active_java_group(proj: Dict[str, Any]) -> None:
    """Called once at the start of every codegen job. Derives the active
    Java group + slug from the project name so all downstream scaffolds
    emit `package com.<proj>.<svc>...` instead of `com.lama.<svc>...`."""
    global _ACTIVE_PROJECT_SLUG, _ACTIVE_JAVA_GROUP, _ACTIVE_JAVA_GROUP_PATH
    slug = _slugify_ident(proj.get("name") or "", fallback="lama")
    _ACTIVE_PROJECT_SLUG = slug
    _ACTIVE_JAVA_GROUP = f"com.{slug}"
    _ACTIVE_JAVA_GROUP_PATH = f"com/{slug}"
    logger.info(
        "codegen: active Java group set to '%s' (project='%s' slug='%s')",
        _ACTIVE_JAVA_GROUP, proj.get("name"), slug,
    )


# ---------------------------------------------------------------------------
# iter-14.20.9 — Deterministic root artifacts for microservices mode.
#
# Previously microservices mode shipped ONLY docker-compose.yml + ci.yml +
# README.md at the root — no parent pom / workspace manifest — so the
# generated bundle looked like N unrelated applications instead of one
# merged product ("ceots"). We now emit a real Maven multi-module parent
# pom, or Yarn/npm workspace root, or Poetry workspace root, so every
# service is a module of the SAME application.
# ---------------------------------------------------------------------------
def _kebab_svc_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "svc").lower()).strip("-") or "svc"


def _java_root_parent_pom(proj: Dict[str, Any], svc_names: List[str]) -> str:
    modules = "\n".join(
        f"    <module>services/{_kebab_svc_name(n)}</module>" for n in svc_names
    ) or "    <!-- no services yet -->"
    art = _ACTIVE_PROJECT_SLUG
    return (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
        f"  <modelVersion>4.0.0</modelVersion>\n"
        f"  <groupId>{_ACTIVE_JAVA_GROUP}</groupId>\n"
        f"  <artifactId>{art}-parent</artifactId>\n"
        f"  <version>0.0.1-SNAPSHOT</version>\n"
        f"  <packaging>pom</packaging>\n"
        f"  <name>{proj.get('name') or art}</name>\n"
        f"  <description>Merged multi-module application generated by LAMA.</description>\n"
        f"  <parent>\n"
        f"    <groupId>org.springframework.boot</groupId>\n"
        f"    <artifactId>spring-boot-starter-parent</artifactId>\n"
        f"    <version>3.3.0</version>\n"
        f"  </parent>\n"
        f"  <properties><java.version>21</java.version></properties>\n"
        f"  <modules>\n{modules}\n  </modules>\n"
        f"</project>\n"
    )


def _node_root_package_json(proj: Dict[str, Any], svc_names: List[str]) -> str:
    workspaces = ",\n".join(
        f"    \"services/{_kebab_svc_name(n)}\"" for n in svc_names
    ) or "    \"services/*\""
    return (
        f"{{\n"
        f"  \"name\": \"{_ACTIVE_PROJECT_SLUG}\",\n"
        f"  \"version\": \"0.0.1\",\n"
        f"  \"private\": true,\n"
        f"  \"description\": \"Merged {proj.get('name') or 'application'} — generated by LAMA.\",\n"
        f"  \"workspaces\": [\n{workspaces}\n  ]\n"
        f"}}\n"
    )


def _python_root_pyproject(proj: Dict[str, Any], svc_names: List[str]) -> str:
    members = ", ".join(repr(f"services/{_kebab_svc_name(n)}") for n in svc_names) or '"services/*"'
    return (
        f"[project]\n"
        f"name = \"{_ACTIVE_PROJECT_SLUG}\"\n"
        f"version = \"0.0.1\"\n"
        f"description = \"Merged {proj.get('name') or 'application'} — generated by LAMA.\"\n"
        f"requires-python = \">=3.11\"\n\n"
        f"[tool.uv.workspace]\n"
        f"members = [{members}]\n"
    )


def _root_docker_compose(proj: Dict[str, Any], services: List[Dict[str, Any]]) -> str:
    lines = [
        f"# {proj.get('name') or _ACTIVE_PROJECT_SLUG} — merged multi-service topology",
        "# Generated by LAMA. Every service below is part of the SAME application.",
        f"name: {_ACTIVE_PROJECT_SLUG}",
        "services:",
    ]
    port = 8080
    for s in services:
        if s.get("frontend"):
            continue
        n = _kebab_svc_name(s.get("name") or "svc")
        lines += [
            f"  {n}:",
            f"    build: ./services/{n}",
            f"    container_name: {_ACTIVE_PROJECT_SLUG}-{n}",
            "    ports:",
            f"      - \"{port}:8080\"",
            "    environment:",
            f"      - APP_NAME={_ACTIVE_PROJECT_SLUG}",
            f"      - SERVICE_NAME={n}",
            "    depends_on: []",
        ]
        port += 1
    lines += [
        "  db:",
        "    image: postgres:16-alpine",
        f"    container_name: {_ACTIVE_PROJECT_SLUG}-db",
        "    environment:",
        f"      - POSTGRES_DB={_ACTIVE_PROJECT_SLUG}",
        "      - POSTGRES_USER=postgres",
        "      - POSTGRES_PASSWORD=postgres",
        "    volumes:",
        f"      - {_ACTIVE_PROJECT_SLUG}_db_data:/var/lib/postgresql/data",
        "volumes:",
        f"  {_ACTIVE_PROJECT_SLUG}_db_data: {{}}",
    ]
    return "\n".join(lines) + "\n"


def _root_readme(proj: Dict[str, Any], services: List[Dict[str, Any]], pattern: str) -> str:
    lines = [
        f"# {proj.get('name') or _ACTIVE_PROJECT_SLUG}",
        "",
        f"Merged **{pattern}** application generated by LAMA.",
        "",
        "## Services (all part of this ONE application)",
        "",
    ]
    for s in services:
        n = _kebab_svc_name(s.get("name") or "svc")
        lines.append(f"- **{s.get('name', n)}** — `services/{n}` — `{s.get('backend_lang', '')}`  {s.get('responsibility', '')}")
    lines += [
        "",
        "## Build",
        "",
        "```bash",
        "docker compose up -d --build",
        "```",
        "",
        f"All services run under the merged group `{_ACTIVE_JAVA_GROUP}` / project `{_ACTIVE_PROJECT_SLUG}`.",
    ]
    return "\n".join(lines) + "\n"


def _build_deterministic_root_content(
    file_def: Dict[str, Any], proj: Dict[str, Any],
    biz_services: List[Dict[str, Any]], proj_pattern: str, dominant_lang: str,
) -> Optional[str]:
    """Return deterministic body for a root artifact, or None to let LLM handle."""
    path = file_def.get("path") or ""
    svc_names = [s.get("name") for s in biz_services if s.get("name") and not s.get("frontend")]
    if path == "pom.xml":
        return _java_root_parent_pom(proj, svc_names)
    if path == "package.json":
        return _node_root_package_json(proj, svc_names)
    if path == "pyproject.toml":
        return _python_root_pyproject(proj, svc_names)
    if path == "docker-compose.yml":
        return _root_docker_compose(proj, biz_services)
    if path == "README.md":
        return _root_readme(proj, biz_services, proj_pattern)
    return None


def _new_job(project_id: str, kind: str) -> str:
    import uuid, time as _t
    jid = uuid.uuid4().hex
    _JOBS[jid] = {"id": jid, "project_id": project_id, "kind": kind,
                  "status": "queued", "step": "Queued…", "pct": 0,
                  "started_at": _t.time(), "ended_at": None,
                  "error": None, "result": {}, "log": [],
                  # iter-13.54 — runtime control flag set by /pause /resume /stop.
                  # Read by `_await_job_control()` inside each per-file coroutine
                  # so the fan-out cooperates with user intent without needing
                  # OS signals or per-file task handles.
                  "control": "running"}
    now = _t.time()
    for k in [k for k, v in _JOBS.items() if v.get("ended_at") and now - v["ended_at"] > _JOB_TTL_SEC]:
        _JOBS.pop(k, None)
    return jid


def _job_update(jid, **kw):
    if jid in _JOBS:
        _JOBS[jid].update(kw)


def _job_finish(jid, status, **kw):
    import time as _t
    if jid in _JOBS:
        _JOBS[jid]["status"] = status
        _JOBS[jid]["ended_at"] = _t.time()
        _JOBS[jid].update(kw)


def _job_log(jid, line):
    if jid in _JOBS:
        _JOBS[jid]["log"].append(line)
        _JOBS[jid]["log"] = _JOBS[jid]["log"][-200:]


# iter-13.54 — Cooperative pause / stop control.
#
# WHY: codegen jobs fan out N parallel coroutines (`gen_and_save` × N files,
# `gap_recover_one_file` × M files). The user wants to pause/resume/stop
# the WHOLE batch from the UI without killing the backend process. We
# don't have per-coroutine task handles so signal-based cancellation is
# clumsy. Instead each per-file coroutine awaits `_await_job_control(jid)`
# right before its expensive LLM call. The function:
#   • returns immediately when control == "running"
#   • blocks (1-second poll loop) while control == "paused"
#   • raises `JobStopped` when control == "stopping"
# This gives clean, cooperative semantics that work for any fan-out shape.
class JobStopped(RuntimeError):
    """Raised inside a fan-out coroutine when the user requested STOP on the
    parent job. Catch at the job-runner level to mark the job as stopped
    rather than errored."""


async def _await_job_control(jid: str) -> None:
    """Cooperative checkpoint. Call before each LLM round-trip."""
    while True:
        j = _JOBS.get(jid)
        if not j:
            return  # job vanished — treat as stop
        ctrl = j.get("control", "running")
        if ctrl == "stopping":
            raise JobStopped("stopped by user")
        if ctrl == "running":
            return
        # paused → poll
        await asyncio.sleep(1.0)


async def _get_prompt(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts_col.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


async def _with_completeness_contract(project_id: str, sp: str) -> str:
    """iter-13.71 — Prepend the 100% legacy-parity completeness contract
    (gov.completeness_contract) to every codegen system prompt so the
    generated code enforces full BR / workflow / RBAC / pre-post parity
    and emits the CONFIDENCE_SELF_SCORE footer the confidence engine
    reads. Silent no-op when the prompt has not been seeded yet."""
    try:
        contract = await _get_prompt(project_id, "gov.completeness_contract")
    except Exception:
        contract = ""
    if not contract:
        return sp
    return (
        "═══════════════════════════════════════════════════════════════════\n"
        "GOVERNANCE BLOCK · gov.completeness_contract (LOADED FIRST)\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"{contract}\n\n"
        f"{sp}"
    )


def _safe_format(template: str, **vars) -> str:
    """Format template with vars using simple string replacement.
    
    Python's str.format() fails on literal { or } in templates (e.g. TypeScript
    generics, JSON examples). We use simple string replacement instead.
    """
    result = template
    for key, value in vars.items():
        placeholder = "{" + key + "}"
        result = result.replace(placeholder, str(value) if value is not None else "")
    return result


# iter-13.26 — Required placeholders + freshness check, mirroring routes/architecture.py.
_REQUIRED_PLACEHOLDERS: Dict[str, set] = {
    "codegen.service":  {"target_tech", "graph_subgraph", "legacy_evidence", "api_contract", "srs_use_cases", "architecture_pattern"},
    "codegen.frontend": {"target_tech", "graph_subgraph", "legacy_evidence", "api_summary", "relevant_use_cases", "theme_brief", "architecture_pattern"},
    "codegen.gap_recovery": {
        "target_tech", "target_language", "current_content", "graph_subgraph",
        "legacy_evidence", "api_contract", "srs_use_cases", "file_path",
        "architecture_pattern",
    },
}


def _assert_prompt_fresh(key: str, template: str) -> List[str]:
    needed = _REQUIRED_PLACEHOLDERS.get(key, set())
    missing = sorted(p for p in needed if "{" + p + "}" not in (template or ""))
    if missing:
        logger.warning(
            "stale prompt detected key=%s missing_placeholders=%s "
            "(restart backend or DELETE FROM project_prompts WHERE key='%s')",
            key, missing, key,
        )
    return missing


def _log_prompt_context(key: str, **slots) -> None:
    sizes = {k: len(str(v or "")) for k, v in slots.items()}
    logger.info("codegen.prompt key=%s context_sizes=%s", key, sizes)


async def _ensure_kb_graph(project_id: str) -> bool:
    """Best-effort: build kb_graph if missing so codegen has it available."""
    try:
        from db import kb_graph as kb_graph_col
        existing = await kb_graph_col.find_one({"project_id": project_id}, {"version": 1})
        if existing:
            return True
        from kb.kb_graph import build_kb_graph
        logger.info("kb_graph missing for %s — auto-building before CodeGen", project_id)
        stats = await build_kb_graph(project_id)
        return bool(stats and stats.get("nodes_count", 0) > 0)
    except Exception as e:
        logger.warning("kb_graph auto-build failed for %s: %s", project_id, e)
        return False


# iter-14.23 — Oracle/DDL → Java column type legend surfaced inline in the
# codegen prompt so the LLM maps legacy column types deterministically
# (NUMBER(15,2) → BigDecimal, VARCHAR2 → String, DATE → LocalDate/…).
_COLUMN_TYPE_MAP_LEGEND = (
    "# COLUMN TYPE MAP (legacy SQL → target Java — use verbatim)\n"
    "  NUMBER(p,s) s>0 / NUMERIC / DECIMAL  -> BigDecimal\n"
    "  NUMBER(p) / INTEGER / INT            -> Long\n"
    "  SMALLINT / TINYINT                   -> Short\n"
    "  VARCHAR2(n) / VARCHAR / CHAR / CLOB  -> String\n"
    "  DATE                                 -> LocalDate\n"
    "  TIMESTAMP / DATETIME                 -> OffsetDateTime\n"
    "  RAW / BLOB / BYTEA                    -> byte[]\n"
    "  CHAR(1) 'Y'/'N' flag                 -> Boolean\n"
)


def _render_route_field_toon(route_ents: List[Dict[str, Any]], max_routes: int = 30) -> str:
    """Render matched ROUTE entities as a compact TOON evidence block.

    Routes that actually carry request/response field evidence are listed
    first so the strongest evidence survives the max_routes cap. The output
    mirrors the format the FIELD-EVIDENCE CONTRACT in `codegen.service`
    references.
    """
    if not route_ents:
        return ""

    def _has_fields(r: Dict[str, Any]) -> int:
        return (2 if r.get("request_fields") else 0) + (1 if r.get("response_fields") else 0)

    # De-dup by (verb, path, handler_class); richest-evidence first.
    seen: set = set()
    uniq: List[Dict[str, Any]] = []
    for r in sorted(route_ents, key=_has_fields, reverse=True):
        key = (str(r.get("verb")), str(r.get("name")), str(r.get("handler_class")))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    lines: List[str] = [_COLUMN_TYPE_MAP_LEGEND.rstrip(), ""]
    rendered = 0
    for r in uniq:
        if rendered >= max_routes:
            break
        verb = (r.get("http_method") or r.get("verb") or "ANY")
        fw = r.get("framework") or "?"
        lines.append(f"ROUTE {verb} {r.get('name','/')} (framework={fw})")
        if r.get("handler_class"):
            lines.append(f"  handler_class: {r['handler_class']}")
        if r.get("handler_method"):
            lines.append(f"  handler_method: {r['handler_method']}")
        if r.get("form_bean_class"):
            lines.append(f"  form_bean_class: {r['form_bean_class']}")
        req = r.get("request_fields") or []
        if req:
            lines.append("  request_fields:")
            for f in req[:40]:
                req_flag = "  (required)" if f.get("required") else ""
                lines.append(f"    - {f.get('name')}: {f.get('java_type','String')}{req_flag}")
        resp = r.get("response_fields") or []
        if resp:
            lines.append("  response_fields:")
            for f in resp[:40]:
                lines.append(f"    - {f.get('name')}: {f.get('java_type','String')}")
        roles = r.get("roles") or []
        if roles:
            lines.append(f"  roles: [{', '.join(roles)}]")
        if r.get("target_view"):
            lines.append(f"  target_view: {r['target_view']}")
        rendered += 1
    return "\n".join(lines) if rendered else ""


async def _deep_legacy_context(
    project_id: str,
    seed_terms: List[str],
    max_chars: int = 8000,
) -> str:
    """Graph-first, source-aware legacy context retrieval (iter-13.29).

    This is the answer to the "you're not consulting graphify before
    chunking the vector" complaint. The flow is:

      1. **Graph-first**: pull kb_entities (CLASS / ROUTE / TABLE / ROLE
         nodes) whose name matches any seed term.
      2. **Source pull**: for every matched Class / Method / Route, fetch
         the ACTUAL legacy source code body from ``kb_chunks`` (Mongo
         regex on chunk content). This is the real implementation the
         new code must reproduce — not just a metadata header.
      3. **Vector top-up by entity refs**: hit Qdrant's typed payload
         (``class_names`` / ``method_names`` / ``table_names`` / ...)
         to surface adjacent chunks the regex missed.
      4. **Vector top-up by semantics**: small final pass on the seeds
         as free-text queries for anything that escapes 1-3.

    Returns a single string up to ``max_chars`` long with clearly
    labelled sections so the LLM can attribute each block.
    """
    if not seed_terms:
        return ""
    seeds_lc = sorted({s.lower() for s in seed_terms if s and len(str(s)) >= 3})
    if not seeds_lc:
        return ""

    out_parts: List[str] = []
    total = 0
    seen_bodies: set[str] = set()

    # iter-14.25 — Phase 2 wiring: prepend the journey-KB slice when the
    # per-project toggle is on for stage="codegen". Additive only: the
    # regex/vector retrieval below still runs so the block is a strict
    # superset of the old evidence. Silent no-op when toggle is off.
    try:
        from kb.journey_materializer import journey_context_block
        jb = await journey_context_block(
            project_id, stage="codegen",
            seeds=seed_terms, endpoint_hints=seed_terms,
            max_chars=min(4000, max_chars // 3),
            max_journeys=10,
        )
        if jb:
            block = f"\n{jb}\n"
            if total + len(block) <= max_chars:
                out_parts.append(block)
                total += len(block)
    except Exception as _je:  # noqa: BLE001
        logger.debug("journey_context_block (codegen) skipped: %s", _je)

    def _add(label: str, body: str, max_block: int = 1800) -> bool:
        nonlocal total
        if not body:
            return False
        body = body.strip()[:max_block]
        key = " ".join(body.split())[:160]
        if key in seen_bodies:
            return False
        seen_bodies.add(key)
        block = f"\n# === {label} ===\n{body}\n"
        if total + len(block) > max_chars:
            return False
        out_parts.append(block)
        total += len(block)
        return True

    # ---- 1) Graph-first entity metadata + names to look up source ----
    matched_classes: List[str] = []
    matched_methods: List[tuple[str, str]] = []  # (class, method)
    matched_routes: List[str] = []
    matched_route_ents: List[Dict[str, Any]] = []  # iter-14.23 full ROUTE docs
    matched_tables: List[str] = []
    matched_roles: List[str] = []

    cur = kb_entities.find({"project_id": project_id}, {"_id": 0}).limit(3000)
    async for ent in cur:
        et = (ent.get("type") or "").upper()
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        name_lc = name.lower()
        # iter-14.23 — ROUTEs also match on handler_class / form-bean so a
        # route whose PATH doesn't contain a seed token but whose owning
        # class does still surfaces its field evidence.
        route_blob = ""
        if et == "ROUTE":
            route_blob = " ".join([
                name_lc,
                (ent.get("handler_class") or "").lower(),
                (ent.get("form_bean") or ent.get("form_name") or "").lower(),
                (ent.get("form_bean_class") or "").lower(),
            ])
        matched = (any(s in name_lc for s in seeds_lc)
                   or (et == "ROUTE" and any(s in route_blob for s in seeds_lc)))
        if not matched:
            continue
        if et == "CLASS":
            matched_classes.append(name)
            for m in (ent.get("methods") or [])[:12]:
                mname = m.get("name") if isinstance(m, dict) else str(m)
                if mname:
                    matched_methods.append((name, mname))
        elif et == "ROUTE":
            verb = (ent.get("verb") or "ANY").upper()
            matched_routes.append(f"{verb} {name}")
            matched_route_ents.append(ent)
        elif et == "TABLE":
            matched_tables.append(name)
        elif et == "ROLE":
            matched_roles.append(name)

    # iter-14.23 — dedicated ROUTE scan. The general limit(3000) scan above
    # can miss ROUTE docs on large KBs (CEOTS now has 3093 FORM_FIELD +
    # 1205 CLASS entities that crowd ROUTEs out of the first 3000). Query
    # ROUTEs directly (evidence-carrying first) so the field-evidence block
    # is never silently dropped.
    try:
        _rcur = kb_entities.find(
            {"project_id": project_id, "type": "ROUTE"}, {"_id": 0},
        ).limit(1500)
        _seen_route_keys = {
            (str(r.get("verb")), str(r.get("name")), str(r.get("handler_class")))
            for r in matched_route_ents
        }
        async for ent in _rcur:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            route_blob = " ".join([
                name.lower(),
                (ent.get("handler_class") or "").lower(),
                (ent.get("form_bean") or ent.get("form_name") or "").lower(),
                (ent.get("form_bean_class") or "").lower(),
            ])
            if not any(s in route_blob for s in seeds_lc):
                continue
            key = (str(ent.get("verb")), str(ent.get("name")), str(ent.get("handler_class")))
            if key in _seen_route_keys:
                continue
            _seen_route_keys.add(key)
            matched_routes.append(f"{(ent.get('verb') or 'ANY').upper()} {name}")
            matched_route_ents.append(ent)
    except Exception as _re_exc:  # noqa: BLE001
        logger.debug("dedicated ROUTE scan failed: %s", _re_exc)

    matched_classes = list(dict.fromkeys(matched_classes))[:8]
    matched_routes = list(dict.fromkeys(matched_routes))[:12]
    matched_tables = list(dict.fromkeys(matched_tables))[:10]
    matched_roles = list(dict.fromkeys(matched_roles))[:10]

    if matched_classes or matched_methods or matched_routes or matched_tables:
        header_lines = ["# GRAPH-MATCHED LEGACY ENTITIES"]
        if matched_classes:
            header_lines.append(f"Classes: {', '.join(matched_classes)}")
        if matched_methods:
            header_lines.append("Methods: " + ", ".join(
                f"{c}.{m}" for c, m in matched_methods[:20]
            ))
        if matched_routes:
            header_lines.append(f"Routes: {', '.join(matched_routes)}")
        if matched_tables:
            header_lines.append(f"Tables: {', '.join(matched_tables)}")
        if matched_roles:
            header_lines.append(f"Roles: {', '.join(matched_roles)}")
        _add("GRAPH SUMMARY", "\n".join(header_lines), max_block=1200)

    # iter-14.23 — ROUTE FIELD EVIDENCE (TOON). Render request_fields /
    # response_fields / roles / target_view for every matched ROUTE so the
    # LLM can populate DTO records with EVERY field instead of emitting
    # `⚠ EVIDENCE GAP` markers. This is the field-level join that was
    # missing in iter-14.22. Prefer routes that actually carry field
    # evidence; cap at 30 routes so the block stays bounded.
    route_toon = _render_route_field_toon(matched_route_ents, max_routes=30)
    if route_toon:
        _add("ROUTE FIELD EVIDENCE (TOON — populate DTOs from this)",
             route_toon, max_block=6000)

    # ---- 2) Source pull from kb_chunks via Mongo regex on the matched
    #         class / method names. These are the ACTUAL legacy bodies.
    import re as _re
    search_tokens: List[str] = []
    search_tokens.extend(matched_classes)
    search_tokens.extend(c for c, _ in matched_methods[:8])
    search_tokens.extend(m for _, m in matched_methods[:8])
    # Fall back to raw seed terms if nothing matched (e.g. brand-new
    # project before the graph is built).
    if not search_tokens:
        search_tokens = list(seed_terms)[:10]
    search_tokens = [t for t in dict.fromkeys(search_tokens) if t and len(t) >= 4][:12]

    for token in search_tokens:
        if total >= max_chars:
            break
        try:
            pat = _re.compile(_re.escape(token), _re.IGNORECASE)
            cur = kb_chunks.find(
                {"project_id": project_id, "content": {"$regex": pat}},
                {"_id": 0, "content": 1, "file_id": 1},
            ).limit(4)
            file_ids: set[str] = set()
            async for chunk in cur:
                content = chunk.get("content") or ""
                if not content or len(content) < 80:
                    continue
                fid = chunk.get("file_id", "")
                if fid:
                    file_ids.add(fid)
                # Trim chunk to the window around the matched token so
                # the LLM gets concentrated evidence, not 4000 chars of
                # surrounding boilerplate.
                idx = content.lower().find(token.lower())
                if idx >= 0:
                    start = max(0, idx - 400)
                    end = min(len(content), idx + 1400)
                    body = content[start:end]
                else:
                    body = content[:1600]
                # Resolve filename
                fname = ""
                if fid:
                    try:
                        f = await kb_files.find_one({"id": fid}, {"_id": 0, "filename": 1})
                        fname = (f or {}).get("filename", "")
                    except Exception:
                        fname = ""
                label = f"LEGACY SOURCE [{token}] ({fname})" if fname else f"LEGACY SOURCE [{token}]"
                if not _add(label, body):
                    break
        except Exception as e:
            logger.debug("deep-legacy mongo regex failed for %r: %s", token, e)

    # ---- 3) Vector top-up by typed payload (entity-ref filtered) -----
    if total < max_chars:
        try:
            extras = await qdrant_search_by_entities(
                project_id,
                class_names=matched_classes,
                method_names=[m for _, m in matched_methods[:10]],
                table_names=matched_tables,
                route_paths=matched_routes,
                roles=matched_roles,
                limit=8,
            )
            for r in extras:
                if total >= max_chars:
                    break
                body = (r.get("content") or "").strip()
                if not body:
                    continue
                fname = r.get("filename", "")
                _add(f"LEGACY SOURCE (graph-filtered) ({fname})", body)
        except Exception as e:
            logger.debug("vector entity-filter top-up failed: %s", e)

    # ---- 4) Vector top-up by raw semantics ---------------------------
    if total < max_chars and len(seeds_lc) > 0:
        try:
            sem = await qdrant_search_many(
                project_id, seed_terms[:6], per_query_k=4, final_k=6,
            )
            for body in sem:
                if total >= max_chars:
                    break
                _add("LEGACY SOURCE (semantic)", body)
        except Exception as e:
            logger.debug("vector semantic top-up failed: %s", e)

    # ---- 5) iter-14.20.9 — Broadcast Mongo fallback ------------------
    # When steps 1–4 return little/nothing (common when the KB has
    # legacy source but the seeds are generic tokens like "controller"
    # / "workflow" that don't index-match real class names), scan
    # kb_chunks directly for any TABLE name in the service or any
    # matched entity table name. This catches PHP models, SQL
    # scripts, JSP forms, and stored-procedure files that never
    # became CLASS entities.
    if total < max_chars // 2:
        broad_terms: List[str] = []
        broad_terms.extend(matched_tables)
        # Pull TABLE names directly from the seed set as-supplied by
        # the caller (usually `ddl_target_tables` + `svc.name`).
        broad_terms.extend(t for t in seed_terms if t and len(t) >= 4)
        broad_terms = [t for t in dict.fromkeys(broad_terms) if t]
        for term in broad_terms[:8]:
            if total >= max_chars:
                break
            try:
                pat = _re.compile(_re.escape(term), _re.IGNORECASE)
                cur = kb_chunks.find(
                    {"project_id": project_id, "content": {"$regex": pat}},
                    {"_id": 0, "content": 1, "file_id": 1},
                ).limit(3)
                async for chunk in cur:
                    content = chunk.get("content") or ""
                    if not content or len(content) < 80:
                        continue
                    idx = content.lower().find(term.lower())
                    if idx >= 0:
                        start = max(0, idx - 300)
                        end = min(len(content), idx + 1200)
                        body = content[start:end]
                    else:
                        body = content[:1500]
                    fname = ""
                    fid = chunk.get("file_id", "")
                    if fid:
                        try:
                            f = await kb_files.find_one({"id": fid}, {"_id": 0, "filename": 1})
                            fname = (f or {}).get("filename", "")
                        except Exception:
                            fname = ""
                    label = f"LEGACY BROADCAST [{term}] ({fname})" if fname else f"LEGACY BROADCAST [{term}]"
                    if not _add(label, body):
                        break
            except Exception as e:
                logger.debug("broadcast fallback failed for %r: %s", term, e)

    return ("\n".join(out_parts)).strip()


async def _legacy_evidence(project_id: str, seed_terms: List[str], max_chars: int = 4000) -> str:
    """Pull raw legacy code excerpts from kb_entities for the given seeds.
    Identical to the helper in routes/architecture.py — kept duplicated to
    avoid cross-route imports per AGENTS.md §8 rule 4.
    """
    if not seed_terms:
        return ""
    seeds_lc = [t.lower() for t in seed_terms if t and len(str(t)) >= 3]
    if not seeds_lc:
        return ""
    cur = kb_entities.find({"project_id": project_id}, {"_id": 0}).limit(2000)
    buckets: Dict[str, List[str]] = {"Classes": [], "Routes": [], "Tables": [], "Roles": []}
    total = 0
    async for ent in cur:
        if total >= max_chars:
            break
        et = (ent.get("type") or "").upper()
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        name_lc = name.lower()
        if not any(s in name_lc for s in seeds_lc):
            continue
        src = (ent.get("source") or ent.get("file") or "")[:120]
        if et == "CLASS":
            methods = [
                (m.get("name") if isinstance(m, dict) else str(m))
                for m in (ent.get("methods") or [])
            ][:8]
            line = f"  - {name} ({src}) methods: [{', '.join(m for m in methods if m)}]"
            buckets["Classes"].append(line); total += len(line) + 1
        elif et == "ROUTE":
            verb = (ent.get("verb") or "ANY").upper()
            roles = ", ".join(ent.get("roles") or []) or "-"
            line = f"  - {verb} {name} ({src}) roles: [{roles}]"
            buckets["Routes"].append(line); total += len(line) + 1
        elif et == "TABLE":
            cols = [
                (c.get("name") if isinstance(c, dict) else str(c))
                for c in (ent.get("columns") or [])
            ][:12]
            line = f"  - {name} ({src}) columns: [{', '.join(c for c in cols if c)}]"
            buckets["Tables"].append(line); total += len(line) + 1
        elif et == "ROLE":
            line = f"  - {name} ({src})"
            buckets["Roles"].append(line); total += len(line) + 1
    if not any(buckets.values()):
        return ""
    out: List[str] = ["# LEGACY EVIDENCE (raw kb_entities slice)"]
    for label in ("Classes", "Routes", "Tables", "Roles"):
        if buckets[label]:
            out.append(f"{label}:")
            out.extend(buckets[label][:40])
    return "\n".join(out)[:max_chars]


async def _load_srs_sections(project_id: str) -> dict:
    """Return freshest SRS sections — live srs_documents → Discovery
    stage_context → empty (with warning)."""
    try:
        live = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
        if live and live.get("sections"):
            return live["sections"]
    except Exception:
        pass
    try:
        disc = await stage_context_col.find_one(
            {"project_id": project_id, "stage": "Discovery"}, {"_id": 0}
        )
        sections = ((disc or {}).get("outputs", {}) or {}).get("srs_sections", {}) or {}
        if sections:
            return sections
    except Exception:
        pass
    logger.warning(
        "SRS sections empty for project=%s — codegen will run WITHOUT SRS grounding.",
        project_id,
    )
    return {}


def _slice_api_contract_for_service(api_contracts_md: str, service_name: str) -> str:
    """Best-effort slice of the merged api_contracts artifact for one service.

    The api_contracts job emits one block per service prefixed with
    ``# === Service: <display> (<name>) ===``. We grab that block; if the
    artifact predates iter-13.24 (no per-service markers) we return the
    whole document trimmed.
    """
    if not api_contracts_md or not service_name:
        return api_contracts_md or ""
    # Match either "(<name>)" or backticked / quoted name.
    pat = re.compile(
        rf"#\s*===\s*Service:[^\n]*\(\s*{re.escape(service_name)}\s*\)\s*===\s*\n(.*?)"
        rf"(?=\n#\s*===\s*Service:|\Z)",
        re.DOTALL,
    )
    m = pat.search(api_contracts_md)
    return (m.group(1) if m else api_contracts_md)[:6000]


def _slice_api_contract_for_resource(yaml_text: str, resource: str) -> str:
    """Narrow an OpenAPI YAML block to only the paths matching one resource.

    iter-13.27 — when a Controller is generated for one resource, we don't
    want the LLM to see every other path in the same service (waste of
    tokens, dilutes focus). We grep path entries whose first path segment
    matches the resource. Keeps the components block intact.
    """
    if not yaml_text or not resource:
        return yaml_text or ""
    lines = yaml_text.splitlines()
    out: list[str] = []
    in_paths = False
    keep_path_block = False
    res_lc = resource.lower()
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith("paths:") and ln.find("paths:") == 0:
            in_paths = True
            out.append(ln)
            continue
        if in_paths and ln and not ln.startswith(" "):
            in_paths = False
            keep_path_block = False
            out.append(ln)
            continue
        if in_paths:
            # Top-level path entry, e.g. "  /orders:" — 2-space indented.
            if re.match(r"^\s{2}/\S", ln) and ln.rstrip().endswith(":"):
                path_str = ln.strip().rstrip(":").lower()
                # First non-api/v1 segment must equal the resource.
                parts = [p for p in path_str.split("/") if p and not p.startswith("{")]
                keep_path_block = False
                for seg in parts:
                    if seg in ("api", "v1", "v2", "v3"):
                        continue
                    if seg == res_lc:
                        keep_path_block = True
                    break
            if keep_path_block:
                out.append(ln)
            continue
        out.append(ln)
    return "\n".join(out)[:6000]


def _frontend_framework_for(arch_ctx: dict | None, fallback: str = "react") -> str:
    """Resolve the user-selected frontend framework from the frozen
    Architecture stage_context (set by service map's frontend_service)."""
    fe = ((arch_ctx or {}).get("outputs", {}) or {}).get("frontend_service", {}) or {}
    return (fe.get("framework") or fallback or "react").strip() or fallback


def _strip_md_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        for tag in ("javascript", "typescript", "python", "java", "go", "yaml", "json", "sql", "markdown", "md", "tsx", "jsx", "js", "ts", "py", "dockerfile", "Dockerfile"):
            if t[:len(tag)].lower() == tag.lower():
                t = t[len(tag):]
                break
        t = t.lstrip("\n")
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


# Service file plan (kept short to control token budget)
# iter-13.27 — Helpers to fan a service's tables + endpoints into individual
# files so the LLM produces ONE narrow artifact per call (instead of trying
# to cram every endpoint into a single Controller and truncating).

def _pascal(name: str) -> str:
    """PascalCase a name, handling SCREAMING_SNAKE_CASE table names.

    iter-14.46 — Previously ``ASRIM_ACCTS_HEAD`` came back as
    ``ASRIMACCTSHEAD`` because every split segment was already all-caps and
    ``p[:1].upper() + p[1:]`` preserved that. We now lower the tail of any
    fully-uppercase multi-letter segment so legacy Oracle-style table names
    turn into idiomatic Java class names (``AsrimAcctsHead``) without
    disturbing already-camelCased inputs (``orderItem`` → ``OrderItem``).
    """
    parts = re.split(r"[^a-zA-Z0-9]+", str(name or ""))
    out: List[str] = []
    for p in parts:
        if not p:
            continue
        if len(p) > 1 and p.isupper():
            out.append(p[0] + p[1:].lower())
        else:
            out.append(p[0].upper() + p[1:])
    return "".join(out) or "Entity"


def _kebab(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "item"


def _resource_of(endpoint: str) -> str:
    """Extract a stable resource key from an endpoint like 'GET /orders/:id'."""
    m = re.match(r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)?\s*([^\s?]+)", str(endpoint or ""), re.I)
    if not m:
        return "root"
    path = (m.group(2) or "").strip()
    parts = [p for p in path.split("/") if p and not p.startswith(":") and not p.startswith("{")]
    if not parts:
        return "root"
    # 'api/v1/orders/items' -> 'orders' (skip leading api / v1 / version segments)
    for seg in parts:
        if seg.lower() in ("api", "v1", "v2", "v3"):
            continue
        return _kebab(seg)
    return _kebab(parts[0])


def _group_endpoints_by_resource(endpoints: list[str]) -> Dict[str, list[str]]:
    groups: Dict[str, list[str]] = {}
    for ep in (endpoints or []):
        r = _resource_of(ep)
        groups.setdefault(r, []).append(ep)
    return groups


# iter-13.81.14 — Emergency scaffold. Returns a minimal but COMPILABLE
# file body when every LLM attempt fails (Factory empty responses,
# provider 402/401, etc.). The user can immediately build the project
# and re-run Gap Recovery to enrich each scaffold with real legacy logic.
# Far better than the legacy `// LAMA generation failed` 1-liner which
# breaks the build outright.
# iter-13.81.15 — Endpoint-aware: emits a stub METHOD for every endpoint
# in `file_def.get('endpoints')` so a Controller scaffold for 12 endpoints
# actually has 12 stubbed mapping methods (not just /health). Table-aware:
# emits @Column-named fields if the OLTP DDL is reachable. The result is
# real, compilable, endpoint-complete code — ready for Gap Recovery to
# enrich with legacy logic.
# iter-13.118 — Production-runnable Java scaffolds. The Java branch now
# emits a fully-wired Controller → Service → Repository → Entity ↔ DTO
# ↔ Mapper ↔ Validator ↔ Exception chain whose method BODIES (not just
# signatures) actually call through and do real CRUD. Entity fields are
# materialised from the parsed OLTP DDL (see `_parse_ddl_columns`) and
# DTOs/Mappers/Validators are derived from those columns so the whole
# scaffold compiles and runs `mvn spring-boot:run` out of the box.
# Gap Recovery is now only responsible for enriching the *legacy-
# specific business rules* (workflow transitions, validation predicates,
# approval chains) — not the CRUD skeleton.


# ────────────────────────────────────────────────────────────────────
# iter-13.118 — DDL parsing + Java type mapping helpers
# ──────────────────────────────────��─────────────────────────────────

_JAVA_RESERVED = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new",
    "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while",
})


def _camel(name: str) -> str:
    """`order_line_id` → `orderLineId`, `OrderLineId` → `orderLineId`."""
    s = str(name or "").strip()
    if not s:
        return "value"
    # Already camelCase / PascalCase — lowercase the first char.
    if "_" not in s and "-" not in s and " " not in s:
        return s[:1].lower() + s[1:]
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", s) if p]
    if not parts:
        return "value"
    head = parts[0].lower()
    tail = "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])
    out = head + tail
    if out in _JAVA_RESERVED:
        out = out + "_"
    return out


def _java_type_for_sql(sql_type: str) -> str:
    """Map a CREATE TABLE column type to its idiomatic JPA Java type.

    Conservative — when in doubt, return `String` so the scaffold still
    compiles. Numeric scale parsed best-effort: NUMBER(p,s) where s > 0
    → BigDecimal; integral NUMBER(p) → Long.
    """
    s = (sql_type or "").lower().strip()
    if not s:
        return "String"
    # NUMBER(p,s) Oracle / NUMERIC(p,s) / DECIMAL(p,s)
    m = re.match(r"(number|numeric|decimal)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", s)
    if m:
        scale = int(m.group(3) or 0)
        return "java.math.BigDecimal" if scale > 0 else "Long"
    if "bigint" in s or "int8" in s:
        return "Long"
    if "smallint" in s or "int2" in s or "tinyint" in s:
        return "Short"
    if "bigserial" in s:
        return "Long"
    if "serial" in s:
        return "Integer"
    if "int" in s:
        return "Integer"
    if "double" in s or "float8" in s:
        return "Double"
    if "real" in s or "float4" in s or s.startswith("float"):
        return "Float"
    if "bool" in s:
        return "Boolean"
    if "timestamp" in s or "datetime" in s:
        return "java.time.OffsetDateTime"
    if "date" in s:
        return "java.time.LocalDate"
    if s.startswith("time"):
        return "java.time.LocalTime"
    if "uuid" in s:
        return "java.util.UUID"
    if "clob" in s or "blob" in s or "bytea" in s or "binary" in s:
        return "byte[]"
    return "String"


def _parse_ddl_columns(ddl: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse `CREATE TABLE` statements → {table: [{name, sql_type, nullable, pk}]}.

    Loose parser tuned for PostgreSQL / Oracle / MySQL flavours emitted by
    Stage-2 OLTP generators. Table-level constraints (PRIMARY KEY (...),
    FOREIGN KEY, UNIQUE, CHECK, INDEX, KEY) are skipped; inline
    `... PRIMARY KEY ...` on a column row is captured.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not ddl:
        return out
    # Strip block + line comments so they can't fool the splitter.
    txt = re.sub(r"/\*.*?\*/", "", ddl, flags=re.DOTALL)
    txt = re.sub(r"--[^\n]*", "", txt)
    for m in re.finditer(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`]?([\w\.]+)[\"`]?\s*\(([\s\S]+?)\)\s*(?:;|$)",
        txt, flags=re.IGNORECASE,
    ):
        raw_name = m.group(1)
        table = raw_name.split(".")[-1].lower()
        body = m.group(2)
        # Split on commas not nested in parens (handles NUMBER(10,2) safely).
        depth = 0
        chunks: List[str] = []
        buf: List[str] = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                chunks.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            chunks.append("".join(buf).strip())
        cols: List[Dict[str, Any]] = []
        for c in chunks:
            cstrip = c.strip().rstrip(",")
            if not cstrip:
                continue
            head = cstrip.lower()
            # Table-level constraint rows — skip.
            if head.startswith((
                "primary key", "foreign key", "unique ", "unique(",
                "constraint ", "check ", "key ", "index ",
            )):
                continue
            mm = re.match(
                r"[\"`]?(\w+)[\"`]?\s+([A-Za-z][\w]*(?:\s*\([\s\d,]+\))?)",
                cstrip,
            )
            if not mm:
                continue
            col_name = mm.group(1)
            sql_type = mm.group(2)
            nullable = "not null" not in head
            pk = "primary key" in head
            cols.append({
                "name": col_name,
                "sql_type": sql_type,
                "nullable": nullable,
                "pk": pk,
            })
        if cols:
            # Promote a column literally named `id` to PK if no PK was declared.
            if not any(c["pk"] for c in cols):
                for c in cols:
                    if c["name"].lower() in ("id", f"{table}_id"):
                        c["pk"] = True
                        break
            out[table] = cols
    return out


# ────────────────────────────────────────────────────────────────────
# iter-13.118 — Production-runnable Java scaffold dispatch
# ────────────────────────────────────────────────────────────────────

def _java_scaffold(file_def: dict, svc: dict, proj: dict) -> Optional[str]:
    """Return a runnable Java file body for `file_def`, or None if unhandled.

    Bodies are real CRUD plumbing — never `// TODO: backfill` or
    `throw new UnsupportedOperationException`. Gap Recovery is then
    responsible only for legacy-specific business rules.
    """
    path = file_def.get("path", "") or ""
    ftype = (file_def.get("type") or "").lower()
    name = svc.get("name", "service")
    pkg = re.sub(r"[^a-z0-9]", "", name.lower()) or "app"
    cls = _pascal(file_def.get("resource") or file_def.get("table")
                  or file_def.get("page_name") or name)
    endpoints: List[str] = file_def.get("endpoints") or svc.get("api_endpoints") or []
    cols: List[Dict[str, Any]] = file_def.get("_ddl_columns") or []
    table = file_def.get("table") or file_def.get("_ddl_table") or _kebab(cls)

    # Resolve PK type / field once �� every layer needs it.
    pk_col = next((c for c in cols if c.get("pk")), None)
    pk_jtype = _java_type_for_sql(pk_col["sql_type"]) if pk_col else "Long"
    pk_jname = _camel(pk_col["name"]) if pk_col else "id"

    # ─── Entity ──────────────────────────────────────────────────────
    if ftype == "entity":
        return _java_entity_scaffold(cls, pkg, table, cols)
    # ─── Repository ──────────────────────────────────────────────────
    if ftype == "repository":
        return (
            f"package {_ACTIVE_JAVA_GROUP}.{pkg}.repo;\n\n"
            "import org.springframework.data.jpa.repository.JpaRepository;\n"
            "import org.springframework.stereotype.Repository;\n"
            f"import {_ACTIVE_JAVA_GROUP}.{pkg}.domain.{cls};\n\n"
            f"/** LAMA scaffold — typed JpaRepository for {cls} ({table}). */\n"
            f"@Repository\n"
            f"public interface {cls}Repository extends JpaRepository<{cls}, {pk_jtype}> {{}}\n"
        )
    # ─── DTO ─────────────────────────────────────────────────────────
    if ftype == "dto":
        if "Response" in cls or "ErrorResponse" in path:
            # iter-13.118 — keep the existing ErrorResponse shape (referenced
            # from GlobalExceptionHandler) so we don't break that wiring.
            return (
                f"package {_ACTIVE_JAVA_GROUP}.{pkg}.web;\n\n"
                "public record ErrorResponse(boolean error, String code, String message, String traceId) {}\n"
            )
        return _java_dto_scaffold(cls, pkg, cols)
    # ─── Mapper ───────────��──────────────────────────────────────────
    if ftype == "mapper":
        return _java_mapper_scaffold(cls, pkg, cols)
    # ─── Validator ───────────────────────────────────────────────────
    if ftype == "validator":
        return _java_validator_scaffold(cls, pkg, cols)
    # ─── Exception ───────────────────────────────────────────────────
    if ftype == "exception":
        return (
            f"package {_ACTIVE_JAVA_GROUP}.{pkg}.exception;\n\n"
            f"/** LAMA scaffold — thrown when a {cls} is not found or fails validation. */\n"
            f"public class {cls}NotFoundException extends RuntimeException {{\n"
            f"    public {cls}NotFoundException(Object id) {{\n"
            f"        super(\"{cls} not found: id=\" + id);\n"
            f"    }}\n"
            f"    public {cls}NotFoundException(String message) {{ super(message); }}\n"
            f"}}\n"
        )
    # ─── Controller ──────────────────────────────────────────────────
    if ftype == "controller":
        return _java_controller_scaffold(cls, pkg, endpoints, pk_jtype, pk_jname)
    # ─── Service ─────────────────────────────────────────────────────
    if ftype == "service":
        return _java_service_scaffold(cls, pkg, endpoints, pk_jtype, pk_jname)
    # ─── Test ────────────────────────────────────────────────────────
    if ftype == "test":
        return _java_test_scaffold(cls, pkg, endpoints)
    # Bootstrap / security / errors / config remain handled by the
    # caller (they don't need DDL/endpoint awareness).
    return None


def _java_entity_scaffold(cls: str, pkg: str, table: str,
                          cols: List[Dict[str, Any]]) -> str:
    """Real @Entity with columns materialised from OLTP DDL."""
    imports = {"jakarta.persistence.*"}
    used_time = False
    field_lines: List[str] = []
    accessors: List[str] = []

    def _emit_field(jtype: str, jname: str, col_name: str, nullable: bool,
                    is_pk: bool, generated: bool) -> None:
        nonlocal used_time
        if jtype.startswith("java.time."):
            used_time = True
        ann: List[str] = []
        if is_pk:
            ann.append("    @Id")
            if generated:
                ann.append("    @GeneratedValue(strategy = GenerationType.IDENTITY)")
        col_ann = f"    @Column(name = \"{col_name}\""
        if not nullable and not is_pk:
            col_ann += ", nullable = false"
        col_ann += ")"
        ann.append(col_ann)
        field_lines.append("\n".join(ann) + f"\n    private {jtype} {jname};")
        cap = jname[:1].upper() + jname[1:]
        accessors.append(
            f"    public {jtype} get{cap}() {{ return {jname}; }}\n"
            f"    public void set{cap}({jtype} {jname}) {{ this.{jname} = {jname}; }}"
        )

    if not cols:
        # No DDL available — synthesise an id-only entity that still
        # gets you a runnable mapping. Gap Recovery will fill the rest.
        _emit_field("Long", "id", "id", nullable=False, is_pk=True, generated=True)
    else:
        for c in cols:
            jt = _java_type_for_sql(c["sql_type"])
            jn = _camel(c["name"])
            is_pk = bool(c.get("pk"))
            generated = is_pk and jt in ("Long", "Integer")
            _emit_field(jt, jn, c["name"], bool(c.get("nullable", True)),
                        is_pk, generated)

    if used_time:
        imports.add("java.time.*")
    imp_block = "\n".join(f"import {i};" for i in sorted(imports))
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.domain;\n\n"
        f"{imp_block}\n\n"
        f"/** LAMA scaffold — entity {cls} mapped to OLTP table `{table}`.\n"
        f" *  Fields and column annotations derived from Stage-2 OLTP DDL.\n"
        f" *  Run Gap Recovery to enrich with legacy-specific business rules\n"
        f" *  (validation predicates, computed columns, lifecycle hooks). */\n"
        f"@Entity\n@Table(name = \"{table}\")\n"
        f"public class {cls} {{\n\n"
        + "\n\n".join(field_lines) + "\n\n"
        + "\n\n".join(accessors) + "\n"
        "}\n"
    )


def _dto_field_lines(cols: List[Dict[str, Any]], include_pk: bool) -> List[str]:
    """Return `Type name` record-component declarations for a DTO."""
    if not cols:
        return ["Long id"] if include_pk else ["String value"]
    out: List[str] = []
    for c in cols:
        if c.get("pk") and not include_pk:
            continue
        jt = _java_type_for_sql(c["sql_type"])
        jn = _camel(c["name"])
        out.append(f"{jt} {jn}")
    return out or ["String value"]


def _java_dto_scaffold(cls: str, pkg: str,
                       cols: List[Dict[str, Any]]) -> str:
    """`{cls}Dtos` with CreateRequest / UpdateRequest / Response records."""
    create_fields = ",\n        ".join(_dto_field_lines(cols, include_pk=False))
    update_fields = ",\n        ".join(_dto_field_lines(cols, include_pk=False))
    response_fields = ",\n        ".join(_dto_field_lines(cols, include_pk=True))
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.dto;\n\n"
        "import jakarta.validation.constraints.*;\n"
        "import java.time.*;\n"
        "import java.math.BigDecimal;\n\n"
        f"/** LAMA scaffold — request/response DTOs for {cls}.\n"
        f" *  Record components mirror entity columns; Gap Recovery may\n"
        f" *  add @NotNull / @Size / @Pattern constraints from legacy validators. */\n"
        f"public final class {cls}Dtos {{\n\n"
        f"    public record CreateRequest(\n        {create_fields}\n    ) {{}}\n\n"
        f"    public record UpdateRequest(\n        {update_fields}\n    ) {{}}\n\n"
        f"    public record Response(\n        {response_fields}\n    ) {{}}\n\n"
        f"    private {cls}Dtos() {{}}\n"
        "}\n"
    )


def _java_mapper_scaffold(cls: str, pkg: str,
                          cols: List[Dict[str, Any]]) -> str:
    """toEntity / toDto / applyUpdate with real field copies."""
    to_entity_lines: List[str] = []
    to_dto_args: List[str] = []
    apply_update_lines: List[str] = []
    if cols:
        for c in cols:
            jn = _camel(c["name"])
            cap = jn[:1].upper() + jn[1:]
            if c.get("pk"):
                to_dto_args.append(f"entity.get{cap}()")
            else:
                to_entity_lines.append(f"        entity.set{cap}(request.{jn}());")
                to_dto_args.append(f"entity.get{cap}()")
                apply_update_lines.append(
                    f"        if (request.{jn}() != null) entity.set{cap}(request.{jn}());"
                )
    else:
        to_entity_lines.append("        // No DDL columns parsed — Gap Recovery to populate")
        to_dto_args = ["entity.getId()"]
        apply_update_lines.append("        // No DDL columns parsed — Gap Recovery to populate")

    to_entity_body = "\n".join(to_entity_lines) if to_entity_lines else \
        "        // No non-PK columns to copy"
    apply_update_body = "\n".join(apply_update_lines) if apply_update_lines else \
        "        // No non-PK columns to update"
    response_args = ",\n            ".join(to_dto_args) or "entity.getId()"
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.mapper;\n\n"
        "import org.springframework.stereotype.Component;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.domain.{cls};\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.dto.{cls}Dtos;\n\n"
        f"/** LAMA scaffold — pure mapper between {cls} and {cls}Dtos.\n"
        f" *  Field copies derived from Stage-2 OLTP DDL. Gap Recovery\n"
        f" *  may add format coercion (date parsing, enum mapping, etc.). */\n"
        f"@Component\n"
        f"public class {cls}Mapper {{\n\n"
        f"    public {cls} toEntity({cls}Dtos.CreateRequest request) {{\n"
        f"        {cls} entity = new {cls}();\n"
        f"{to_entity_body}\n"
        f"        return entity;\n"
        f"    }}\n\n"
        f"    public {cls}Dtos.Response toDto({cls} entity) {{\n"
        f"        return new {cls}Dtos.Response(\n"
        f"            {response_args}\n"
        f"        );\n"
        f"    }}\n\n"
        f"    public void applyUpdate({cls} entity, {cls}Dtos.UpdateRequest request) {{\n"
        f"{apply_update_body}\n"
        f"    }}\n"
        "}\n"
    )


def _java_validator_scaffold(cls: str, pkg: str,
                             cols: List[Dict[str, Any]]) -> str:
    """`@Component` that enforces NOT NULL columns + delegates to Bean Validation."""
    required = [c for c in cols if not c.get("nullable") and not c.get("pk")]
    checks: List[str] = []
    for c in required:
        jn = _camel(c["name"])
        jt = _java_type_for_sql(c["sql_type"])
        if jt == "String":
            checks.append(
                f"        if (request.{jn}() == null || request.{jn}().isBlank()) "
                f"throw new IllegalArgumentException(\"{jn} is required\");"
            )
        else:
            checks.append(
                f"        if (request.{jn}() == null) "
                f"throw new IllegalArgumentException(\"{jn} is required\");"
            )
    create_body = "\n".join(checks) if checks else \
        "        // No NOT NULL columns — Gap Recovery may add legacy validators"
    # On updates, all fields are optional (partial update) so we don't
    # null-check them; legacy-specific cross-field rules go via Gap Recovery.
    update_body = "        // Update fields are optional; legacy cross-field rules via Gap Recovery"
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.validation;\n\n"
        "import org.springframework.stereotype.Component;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.dto.{cls}Dtos;\n\n"
        f"/** LAMA scaffold — required-field validator for {cls}.\n"
        f" *  NOT NULL columns from OLTP DDL are enforced here; Gap\n"
        f" *  Recovery should add legacy-specific business-rule checks. */\n"
        f"@Component\n"
        f"public class {cls}Validator {{\n\n"
        f"    public void validateCreate({cls}Dtos.CreateRequest request) {{\n"
        f"        if (request == null) throw new IllegalArgumentException(\"request body is required\");\n"
        f"{create_body}\n"
        f"    }}\n\n"
        f"    public void validateUpdate({cls}Dtos.UpdateRequest request) {{\n"
        f"        if (request == null) throw new IllegalArgumentException(\"request body is required\");\n"
        f"{update_body}\n"
        f"    }}\n"
        "}\n"
    )


def _java_controller_scaffold(cls: str, pkg: str, endpoints: List[str],
                              pk_jtype: str, pk_jname: str) -> str:
    """`@RestController` whose methods CALL the Service (not return Map.of)."""
    mapping_imports = {"GET": "GetMapping", "POST": "PostMapping",
                       "PUT": "PutMapping", "DELETE": "DeleteMapping",
                       "PATCH": "PatchMapping"}
    class_prefix = "/" + _kebab(cls)
    method_blocks: List[str] = []
    for ep in endpoints[:30]:
        verb, p = _ep_parts(ep)
        method_path = p
        if method_path.startswith(class_prefix):
            method_path = method_path[len(class_prefix):] or "/"
            if not method_path.startswith("/"):
                method_path = "/" + method_path
        mname = _ep_method_name(verb, p)
        anno = mapping_imports.get(verb, "RequestMapping")
        path_vars = re.findall(r"\{(\w+)\}", method_path)
        # Build method signature + body per HTTP verb
        if verb == "GET":
            if path_vars:
                params = ", ".join(f"@PathVariable {pk_jtype} {pv}" for pv in path_vars)
                call_args = ", ".join(path_vars)
                body = (
                    f"        {cls} entity = service.{mname}({call_args});\n"
                    f"        return ResponseEntity.ok(mapper.toDto(entity));"
                )
                ret = f"ResponseEntity<{cls}Dtos.Response>"
            else:
                params = ""
                body = (
                    f"        return ResponseEntity.ok(\n"
                    f"            service.{mname}().stream().map(mapper::toDto).toList()\n"
                    f"        );"
                )
                ret = f"ResponseEntity<java.util.List<{cls}Dtos.Response>>"
        elif verb == "POST":
            params = f"@Valid @RequestBody {cls}Dtos.CreateRequest request"
            body = (
                f"        {cls} entity = service.{mname}(request);\n"
                f"        return ResponseEntity\n"
                f"            .status(org.springframework.http.HttpStatus.CREATED)\n"
                f"            .body(mapper.toDto(entity));"
            )
            ret = f"ResponseEntity<{cls}Dtos.Response>"
        elif verb in ("PUT", "PATCH"):
            id_var = path_vars[0] if path_vars else pk_jname
            if path_vars:
                params = (
                    f"@PathVariable {pk_jtype} {id_var}, "
                    f"@Valid @RequestBody {cls}Dtos.UpdateRequest request"
                )
                call_args = f"{id_var}, request"
            else:
                params = f"@Valid @RequestBody {cls}Dtos.UpdateRequest request"
                call_args = "request"
            body = (
                f"        {cls} entity = service.{mname}({call_args});\n"
                f"        return ResponseEntity.ok(mapper.toDto(entity));"
            )
            ret = f"ResponseEntity<{cls}Dtos.Response>"
        elif verb == "DELETE":
            id_var = path_vars[0] if path_vars else pk_jname
            params = f"@PathVariable {pk_jtype} {id_var}" if path_vars else ""
            call_args = id_var if path_vars else ""
            body = (
                f"        service.{mname}({call_args});\n"
                f"        return ResponseEntity.noContent().build();"
            )
            ret = "ResponseEntity<Void>"
        else:
            params = ""
            body = "        return ResponseEntity.ok().build();"
            ret = "ResponseEntity<Void>"
        method_blocks.append(
            f"    /** LEGACY: {ep} — Gap Recovery to enrich with legacy-specific business rules. */\n"
            f"    @{anno}(\"{method_path}\")\n"
            f"    public {ret} {mname}({params}) {{\n"
            f"{body}\n"
            f"    }}\n"
        )
    if not method_blocks:
        method_blocks.append(
            "    @GetMapping(\"/health\")\n"
            f"    public ResponseEntity<java.util.Map<String,String>> health() {{\n"
            f"        return ResponseEntity.ok(java.util.Map.of(\"service\", \"{_kebab(cls)}\", \"status\", \"UP\"));\n"
            f"    }}\n"
        )
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.web;\n\n"
        "import jakarta.validation.Valid;\n"
        "import org.springframework.http.ResponseEntity;\n"
        "import org.springframework.web.bind.annotation.*;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.dto.{cls}Dtos;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.domain.{cls};\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.service.{cls}Service;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.mapper.{cls}Mapper;\n\n"
        f"/** LAMA scaffold — REST controller for {cls}. Every endpoint\n"
        f" *  delegates to {cls}Service; Gap Recovery to enrich with\n"
        f" *  legacy-specific business rules. */\n"
        f"@RestController\n@RequestMapping(\"{class_prefix}\")\n"
        f"public class {cls}Controller {{\n\n"
        f"    private final {cls}Service service;\n"
        f"    private final {cls}Mapper mapper;\n\n"
        f"    public {cls}Controller({cls}Service service, {cls}Mapper mapper) {{\n"
        f"        this.service = service;\n"
        f"        this.mapper = mapper;\n"
        f"    }}\n\n"
        + "\n".join(method_blocks) +
        "}\n"
    )


def _java_service_scaffold(cls: str, pkg: str, endpoints: List[str],
                           pk_jtype: str, pk_jname: str) -> str:
    """`@Service` that uses the Repository for real CRUD — no exceptions thrown."""
    method_blocks: List[str] = []
    for ep in endpoints[:30]:
        verb, p = _ep_parts(ep)
        mname = _ep_method_name(verb, p)
        path_vars = re.findall(r"\{(\w+)\}", p)
        if verb == "GET":
            if path_vars:
                pv = path_vars[0]
                method_blocks.append(
                    f"    /** LEGACY: {ep} */\n"
                    f"    @org.springframework.transaction.annotation.Transactional(readOnly = true)\n"
                    f"    public {cls} {mname}({pk_jtype} {pv}) {{\n"
                    f"        return repository.findById({pv})\n"
                    f"            .orElseThrow(() -> new {cls}NotFoundException({pv}));\n"
                    f"    }}\n"
                )
            else:
                method_blocks.append(
                    f"    /** LEGACY: {ep} */\n"
                    f"    @org.springframework.transaction.annotation.Transactional(readOnly = true)\n"
                    f"    public java.util.List<{cls}> {mname}() {{\n"
                    f"        return repository.findAll();\n"
                    f"    }}\n"
                )
        elif verb == "POST":
            method_blocks.append(
                f"    /** LEGACY: {ep} */\n"
                f"    @org.springframework.transaction.annotation.Transactional\n"
                f"    public {cls} {mname}({cls}Dtos.CreateRequest request) {{\n"
                f"        validator.validateCreate(request);\n"
                f"        {cls} entity = mapper.toEntity(request);\n"
                f"        return repository.save(entity);\n"
                f"    }}\n"
            )
        elif verb in ("PUT", "PATCH"):
            pv = path_vars[0] if path_vars else pk_jname
            sig = (f"{pk_jtype} {pv}, {cls}Dtos.UpdateRequest request"
                   if path_vars else f"{cls}Dtos.UpdateRequest request")
            lookup_id = pv if path_vars else "request.id()"  # best-effort fallback
            method_blocks.append(
                f"    /** LEGACY: {ep} */\n"
                f"    @org.springframework.transaction.annotation.Transactional\n"
                f"    public {cls} {mname}({sig}) {{\n"
                f"        validator.validateUpdate(request);\n"
                f"        {cls} entity = repository.findById({lookup_id})\n"
                f"            .orElseThrow(() -> new {cls}NotFoundException({lookup_id}));\n"
                f"        mapper.applyUpdate(entity, request);\n"
                f"        return repository.save(entity);\n"
                f"    }}\n"
            )
        elif verb == "DELETE":
            pv = path_vars[0] if path_vars else pk_jname
            sig = f"{pk_jtype} {pv}"
            method_blocks.append(
                f"    /** LEGACY: {ep} */\n"
                f"    @org.springframework.transaction.annotation.Transactional\n"
                f"    public void {mname}({sig}) {{\n"
                f"        if (!repository.existsById({pv})) {{\n"
                f"            throw new {cls}NotFoundException({pv});\n"
                f"        }}\n"
                f"        repository.deleteById({pv});\n"
                f"    }}\n"
            )
        else:
            method_blocks.append(
                f"    /** LEGACY: {ep} */\n"
                f"    public void {mname}() {{ /* Gap Recovery to fill */ }}\n"
            )
    if not method_blocks:
        method_blocks.append(
            f"    @org.springframework.transaction.annotation.Transactional(readOnly = true)\n"
            f"    public java.util.List<{cls}> findAll() {{ return repository.findAll(); }}\n"
        )
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg}.service;\n\n"
        "import org.springframework.stereotype.Service;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.domain.{cls};\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.repo.{cls}Repository;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.dto.{cls}Dtos;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.mapper.{cls}Mapper;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.validation.{cls}Validator;\n"
        f"import {_ACTIVE_JAVA_GROUP}.{pkg}.exception.{cls}NotFoundException;\n\n"
        f"/** LAMA scaffold — service for {cls}. CRUD is wired through\n"
        f" *  {cls}Repository; Gap Recovery to enrich with legacy-\n"
        f" *  specific business rules (workflow transitions, approval\n"
        f" *  chains, computed fields, etc.). */\n"
        f"@Service\npublic class {cls}Service {{\n\n"
        f"    private final {cls}Repository repository;\n"
        f"    private final {cls}Mapper mapper;\n"
        f"    private final {cls}Validator validator;\n\n"
        f"    public {cls}Service({cls}Repository repository,\n"
        f"                        {cls}Mapper mapper,\n"
        f"                        {cls}Validator validator) {{\n"
        f"        this.repository = repository;\n"
        f"        this.mapper = mapper;\n"
        f"        this.validator = validator;\n"
        f"    }}\n\n"
        + "\n".join(method_blocks) +
        "}\n"
    )


def _java_test_scaffold(cls: str, pkg: str, endpoints: List[str]) -> str:
    """`@SpringBootTest` with one smoke test per endpoint that actually
    pings MockMvc rather than holding a `// TODO`."""
    test_methods: List[str] = []
    for ep in endpoints[:20]:
        verb, p = _ep_parts(ep)
        tname = _ep_method_name(verb, p) + "Smoke"
        # Replace path vars with placeholder `1` so the URL is callable.
        url = re.sub(r"\{\w+\}", "1", p)
        body_send = ""
        if verb == "POST":
            body_send = "                .contentType(org.springframework.http.MediaType.APPLICATION_JSON)\n" \
                        "                .content(\"{}\")\n"
        if verb in ("PUT", "PATCH"):
            body_send = "                .contentType(org.springframework.http.MediaType.APPLICATION_JSON)\n" \
                        "                .content(\"{}\")\n"
        test_methods.append(
            f"    @org.junit.jupiter.api.Test\n"
            f"    void {tname}() throws Exception {{\n"
            f"        // LEGACY: {ep} — Gap Recovery to add parity assertions.\n"
            f"        mockMvc.perform(\n"
            f"            org.springframework.test.web.servlet.request.MockMvcRequestBuilders\n"
            f"                .{verb.lower()}(\"{url}\")\n"
            f"{body_send}"
            f"        ).andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status()\n"
            f"            .is(org.hamcrest.Matchers.anyOf(\n"
            f"                org.hamcrest.Matchers.is(200), org.hamcrest.Matchers.is(201),\n"
            f"                org.hamcrest.Matchers.is(204), org.hamcrest.Matchers.is(400),\n"
            f"                org.hamcrest.Matchers.is(404)\n"
            f"            )));\n"
            f"    }}\n"
        )
    if not test_methods:
        test_methods.append(
            "    @org.junit.jupiter.api.Test\n"
            "    void contextLoads() { /* smoke */ }\n"
        )
    return (
        f"package {_ACTIVE_JAVA_GROUP}.{pkg};\n\n"
        "import org.springframework.boot.test.context.SpringBootTest;\n"
        "import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;\n"
        "import org.springframework.test.web.servlet.MockMvc;\n"
        "import org.springframework.beans.factory.annotation.Autowired;\n\n"
        f"/** LAMA scaffold — smoke tests for {cls}. Gap Recovery should\n"
        f" *  replace anyOf(...) status assertions with legacy-parity checks. */\n"
        f"@SpringBootTest\n@AutoConfigureMockMvc\n"
        f"class {cls}IT {{\n\n"
        f"    @Autowired private MockMvc mockMvc;\n\n"
        + "\n".join(test_methods) +
        "}\n"
    )
def _ep_parts(ep: str) -> tuple[str, str]:
    """`'GET /foo/{id}/bar'` → `('GET', '/foo/{id}/bar')`."""
    s = (ep or "").strip()
    parts = s.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in ("GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"):
        return parts[0].upper(), parts[1]
    return "GET", s if s.startswith("/") else f"/{s}"


def _ep_method_name(verb: str, path: str) -> str:
    verb_map = {"GET":"get","POST":"create","PUT":"update","DELETE":"delete","PATCH":"patch"}
    base = verb_map.get(verb.upper(), verb.lower())
    tokens = [t.strip("{}") for t in re.split(r"[/_\-]+", path) if t.strip("{}") and len(t.strip("{}")) >= 2]
    # Drop the very first token if it matches the controller resource (avoids GetFooFoo)
    if not tokens:
        return base
    suffix = "".join(t.capitalize() for t in tokens[:4])
    return f"{base}{suffix}"


def _frontend_emergency_scaffold(path: str, ctype: str, file_def: dict,
                                 svc: dict, proj: dict) -> str:
    """iter-13.85 — Deterministic frontend scaffolds (React + TS + Vite +
    React Query + Zustand + react-router-dom). All outputs are designed
    to pass the FRONTEND_REQUIRED_TOKENS validator out of the box and
    to give the user a runnable `npm run dev` baseline.
    """
    page_name = file_def.get("page_name") or "Dashboard"
    cls = _pascal(page_name)
    resource = _kebab(page_name)
    # --- Config / scaffold files (path-based) ---
    if path.endswith("package.json"):
        proj_name = _kebab(proj.get("name") or "lama-frontend")
        return (
            "{\n"
            f"  \"name\": \"{proj_name}-frontend\",\n"
            "  \"version\": \"0.1.0\",\n"
            "  \"private\": true,\n"
            "  \"type\": \"module\",\n"
            "  \"scripts\": {\n"
            "    \"dev\": \"vite\",\n"
            "    \"build\": \"tsc -b && vite build\",\n"
            "    \"preview\": \"vite preview\",\n"
            "    \"test\": \"vitest\"\n"
            "  },\n"
            "  \"dependencies\": {\n"
            "    \"react\": \"^19.0.0\",\n"
            "    \"react-dom\": \"^19.0.0\",\n"
            "    \"react-router-dom\": \"^7.0.0\",\n"
            "    \"@tanstack/react-query\": \"^5.0.0\",\n"
            "    \"zustand\": \"^4.5.0\",\n"
            "    \"axios\": \"^1.7.0\"\n"
            "  },\n"
            "  \"devDependencies\": {\n"
            "    \"vite\": \"^5.4.0\",\n"
            "    \"@vitejs/plugin-react\": \"^4.3.0\",\n"
            "    \"typescript\": \"^5.5.0\",\n"
            "    \"@types/react\": \"^19.0.0\",\n"
            "    \"@types/react-dom\": \"^19.0.0\",\n"
            "    \"vitest\": \"^2.0.0\"\n"
            "  },\n"
            "  \"engines\": { \"node\": \">=20\" }\n"
            "}\n"
        )
    if path.endswith("tsconfig.json"):
        return (
            "{\n"
            "  \"compilerOptions\": {\n"
            "    \"target\": \"ES2022\",\n"
            "    \"lib\": [\"ES2022\", \"DOM\", \"DOM.Iterable\"],\n"
            "    \"jsx\": \"react-jsx\",\n"
            "    \"module\": \"ESNext\",\n"
            "    \"moduleResolution\": \"bundler\",\n"
            "    \"strict\": true,\n"
            "    \"noUnusedLocals\": true,\n"
            "    \"noUnusedParameters\": true,\n"
            "    \"baseUrl\": \".\",\n"
            "    \"paths\": { \"@/*\": [\"src/*\"] }\n"
            "  },\n"
            "  \"include\": [\"src\"]\n"
            "}\n"
        )
    if path.endswith("index.html"):
        title = (proj.get("name") or "LAMA App").replace("<", "").replace(">", "")
        return (
            "<!doctype html>\n<html lang=\"en\">\n  <head>\n"
            "    <meta charset=\"UTF-8\" />\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
            f"    <title>{title}</title>\n  </head>\n  <body>\n"
            "    <div id=\"root\"></div>\n"
            "    <script type=\"module\" src=\"/src/main.tsx\"></script>\n"
            "  </body>\n</html>\n"
        )
    # --- ctype-driven .tsx / .ts skeletons ---
    if ctype == "page":
        return (
            "import { useQuery } from '@tanstack/react-query';\n"
            "import { useState } from 'react';\n"
            "import { Link } from 'react-router-dom';\n"
            "import { client } from '@/api/client';\n"
            "import { RoleGate } from '@/components/RoleGate';\n\n"
            f"/** {cls} page — LAMA emergency scaffold. Run Gap Recovery to\n"
            f" *  enrich with legacy field labels, validation rules, and\n"
            f" *  role-aware widgets from LEGACY UI EVIDENCE + SRS.\n"
            f" *  SRS: UC-TODO  LEGACY: TODO */\n"
            f"export default function {cls}() {{\n"
            "    const [query, setQuery] = useState('');\n"
            f"    const {{ data, isLoading, isError, error }} = useQuery({{\n"
            f"        queryKey: ['{resource}', query],\n"
            f"        queryFn: () => client.get('/api/v1/{resource}', {{ params: {{ query }} }}).then(r => r.data),\n"
            "    });\n"
            "    if (isLoading) return <div role=\"status\" aria-live=\"polite\">Loading…</div>;\n"
            "    if (isError)   return <div role=\"alert\">{(error as Error).message}</div>;\n"
            f"    return (\n"
            f"        <section aria-labelledby=\"{resource}-h\">\n"
            f"            <h1 id=\"{resource}-h\">{cls}</h1>\n"
            "            <label>Search<input value={query} onChange={(e) => setQuery(e.target.value)} /></label>\n"
            "            <RoleGate roles={['ROLE_USER']}>\n"
            f"                <Link to=\"/{resource}/new\">Create</Link>\n"
            "            </RoleGate>\n"
            "            <ul>{(data ?? []).map((row: any) => (<li key={row.id}>{row.name}</li>))}</ul>\n"
            "        </section>\n"
            "    );\n"
            "}\n"
        )
    if ctype == "api_client":
        return (
            "import axios, { AxiosError } from 'axios';\n\n"
            "const BASE_URL = (import.meta as any).env?.VITE_API_BASE_URL ?? '/api';\n\n"
            "export const client = axios.create({\n"
            "    baseURL: BASE_URL,\n"
            "    timeout: 15_000,\n"
            "    withCredentials: true,\n"
            "});\n\n"
            "client.interceptors.request.use((cfg) => {\n"
            "    const token = localStorage.getItem('lama:auth:token');\n"
            "    if (token) cfg.headers.Authorization = `Bearer ${token}`;\n"
            "    return cfg;\n"
            "});\n\n"
            "client.interceptors.response.use(\n"
            "    (r) => r,\n"
            "    (err: AxiosError) => {\n"
            "        if (err.response?.status === 401) {\n"
            "            localStorage.removeItem('lama:auth:token');\n"
            "            window.location.href = '/login';\n"
            "        }\n"
            "        return Promise.reject(err);\n"
            "    },\n"
            ");\n\n"
            "export default client;\n"
        )
    if ctype == "role_guard":
        return (
            "import { ReactNode } from 'react';\n"
            "import { useAuthStore } from '@/store/auth';\n\n"
            "type RoleGateProps = { roles: string[]; children: ReactNode; fallback?: ReactNode };\n\n"
            "/** Hides children when the current user lacks every listed role. */\n"
            "export function RoleGate({ roles, children, fallback = null }: RoleGateProps) {\n"
            "    const userRoles = useAuthStore((s) => s.user?.roles ?? []);\n"
            "    const allowed = roles.some((r) => userRoles.includes(r));\n"
            "    return <>{allowed ? children : fallback}</>;\n"
            "}\n\n"
            "export default RoleGate;\n"
        )
    if ctype == "route_config":
        return (
            "import { lazy, Suspense } from 'react';\n"
            "import { createBrowserRouter, RouterProvider, Navigate } from 'react-router-dom';\n"
            "import { useAuthStore } from '@/store/auth';\n\n"
            "const Dashboard = lazy(() => import('@/pages/Dashboard'));\n\n"
            "function Protected({ children }: { children: JSX.Element }) {\n"
            "    const token = useAuthStore((s) => s.token);\n"
            "    return token ? children : <Navigate to=\"/login\" replace />;\n"
            "}\n\n"
            "const router = createBrowserRouter([\n"
            "    { path: '/', element: <Protected><Suspense fallback={null}><Dashboard /></Suspense></Protected> },\n"
            "]);\n\n"
            "export default function AppRoutes() { return <RouterProvider router={router} />; }\n"
        )
    if ctype == "store":
        return (
            "import { create } from 'zustand';\n"
            "import { persist } from 'zustand/middleware';\n\n"
            "type AuthUser = { id: string; name: string; roles: string[] };\n"
            "type AuthState = {\n"
            "    token: string | null;\n"
            "    user: AuthUser | null;\n"
            "    setSession: (token: string, user: AuthUser) => void;\n"
            "    clear: () => void;\n"
            "};\n\n"
            "export const useAuthStore = create<AuthState>()(persist(\n"
            "    (set) => ({\n"
            "        token: null,\n"
            "        user: null,\n"
            "        setSession: (token, user) => set({ token, user }),\n"
            "        clear: () => set({ token: null, user: null }),\n"
            "    }),\n"
            "    { name: 'lama:auth' },\n"
            "));\n"
        )
    if ctype == "bootstrap" or path.endswith("main.tsx"):
        return (
            "import React from 'react';\n"
            "import ReactDOM from 'react-dom/client';\n"
            "import { QueryClient, QueryClientProvider } from '@tanstack/react-query';\n"
            "import AppRoutes from './routes';\n\n"
            "const qc = new QueryClient();\n\n"
            "ReactDOM.createRoot(document.getElementById('root')!).render(\n"
            "    <React.StrictMode>\n"
            "        <QueryClientProvider client={qc}>\n"
            "            <AppRoutes />\n"
            "        </QueryClientProvider>\n"
            "    </React.StrictMode>,\n"
            ");\n"
        )
    return ""



def _emergency_scaffold(file_def: dict, svc: dict, proj: dict) -> str:
    path = file_def.get("path", "")
    ftype = (file_def.get("type") or "").lower()
    lang = (svc.get("backend_lang") or file_def.get("language") or "").lower()
    name = svc.get("name", "service")
    pkg = re.sub(r"[^a-z0-9]", "", name.lower()) or "app"
    cls = _pascal(file_def.get("resource") or file_def.get("table")
                  or file_def.get("page_name") or name)
    # iter-13.85 — Frontend short-circuit. When ``svc`` is a frontend
    # service the language is TypeScript/JS regardless of `backend_lang`,
    # and the file shape is driven by `ctype` (page / api_client /
    # role_guard / route_config / store / bootstrap / scaffold). Without
    # this branch the generic fallback at the bottom emitted a 1-line
    # `// LAMA emergency scaffold for ...` stub that doesn't compile.
    ctype = (file_def.get("ctype") or "").lower()
    if svc.get("frontend") and not path.endswith(("Dockerfile", "README.md")):
        body = _frontend_emergency_scaffold(path, ctype, file_def, svc, proj)
        if body:
            return body
    # iter-13.81.15 — Endpoints in scope for this file. Controllers / Services
    # / Tests fan out one stub per endpoint. Empty list ⇒ fall back to /health.
    endpoints: list[str] = file_def.get("endpoints") or svc.get("api_endpoints") or []

    if path.endswith(".md") or ftype == "docs":
        return (f"# {name}\n\n"
                f"_Auto-scaffolded by LAMA — LLM unavailable during initial codegen._\n\n"
                f"Run **Regenerate Backend** or **Gap Recovery** to enrich this file "
                f"with details from the legacy KB.\n")
    if path.endswith(".env.example") or (ftype == "config" and path.endswith(".env.example")):
        return ("# LAMA emergency scaffold — populated by Gap Recovery\n"
                "PORT=8080\nDATABASE_URL=\nLOG_LEVEL=INFO\n")
    if path.endswith("application.yml") or path.endswith("application.yaml"):
        return (f"server:\n  port: 8080\nspring:\n  application:\n    name: {name}\n"
                f"  datasource:\n    url: ${{DATABASE_URL:jdbc:postgresql://localhost:5432/{pkg}}}\n"
                f"    username: ${{DB_USER:postgres}}\n    password: ${{DB_PASS:postgres}}\n"
                f"  jpa:\n    hibernate:\n      ddl-auto: validate\n"
                f"management:\n  endpoints:\n    web:\n      exposure:\n        include: health,info,prometheus\n")
    if path.endswith("Dockerfile") or ftype == "dockerfile":
        if lang == "java":
            return ("FROM eclipse-temurin:21-jre-alpine\nWORKDIR /app\n"
                    "COPY target/*.jar app.jar\nEXPOSE 8080\n"
                    "ENTRYPOINT [\"java\",\"-jar\",\"app.jar\"]\n")
        if lang == "python":
            return ("FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\n"
                    "RUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\n"
                    "EXPOSE 8000\nCMD [\"uvicorn\",\"app.main:app\",\"--host\",\"0.0.0.0\",\"--port\",\"8000\"]\n")
        if lang == "dotnet":
            return ("FROM mcr.microsoft.com/dotnet/aspnet:8.0\nWORKDIR /app\n"
                    "COPY bin/Release/net8.0/publish/ .\nEXPOSE 8080\n"
                    f"ENTRYPOINT [\"dotnet\",\"{_pascal(name)}.dll\"]\n")
        # nodejs / default
        return ("FROM node:20-alpine\nWORKDIR /app\nCOPY package*.json ./\n"
                "RUN npm ci --omit=dev\nCOPY . .\nEXPOSE 3000\nCMD [\"node\",\"src/index.js\"]\n")
    if path.endswith(".yml") or path.endswith(".yaml"):
        return f"# LAMA emergency scaffold for {path}\nservice: {name}\n"
    # iter-13.84 — Build manifests for every language we plan files for.
    # Without one of these the service is NOT independently runnable —
    # `mvn`/`pip`/`npm`/`dotnet`/`go`/`composer`/`bundle`/`cargo` all
    # need their own manifest at the service root.
    if path.endswith("package.json"):
        return (
            "{\n"
            f"  \"name\": \"{_kebab(name)}\",\n"
            "  \"version\": \"0.1.0\",\n"
            "  \"private\": true,\n"
            "  \"type\": \"module\",\n"
            "  \"scripts\": {\n"
            "    \"start\": \"node src/server.js\",\n"
            "    \"dev\": \"node --watch src/server.js\",\n"
            "    \"test\": \"jest\"\n"
            "  },\n"
            "  \"dependencies\": {\n"
            "    \"express\": \"^5.0.0\",\n"
            "    \"helmet\": \"^7.1.0\",\n"
            "    \"cors\": \"^2.8.5\",\n"
            "    \"pino\": \"^9.0.0\",\n"
            "    \"pino-http\": \"^10.0.0\",\n"
            "    \"zod\": \"^3.23.0\",\n"
            "    \"pg\": \"^8.12.0\",\n"
            "    \"jsonwebtoken\": \"^9.0.2\",\n"
            "    \"dotenv\": \"^16.4.5\"\n"
            "  },\n"
            "  \"devDependencies\": {\n"
            "    \"jest\": \"^29.7.0\",\n"
            "    \"supertest\": \"^7.0.0\"\n"
            "  },\n"
            "  \"engines\": { \"node\": \">=20\" }\n"
            "}\n"
        )
    if path.endswith("go.mod"):
        return (
            f"module github.com/{_ACTIVE_PROJECT_SLUG}/{_kebab(name)}\n\ngo 1.22\n\n"
            "require (\n"
            "    github.com/gin-gonic/gin v1.10.0\n"
            "    github.com/jmoiron/sqlx v1.4.0\n"
            "    github.com/lib/pq v1.10.9\n"
            "    github.com/go-playground/validator/v10 v10.22.0\n"
            "    github.com/golang-jwt/jwt/v5 v5.2.1\n"
            ")\n"
        )
    if path.endswith("composer.json"):
        return (
            "{\n"
            f"  \"name\": \"lama/{_kebab(name)}\",\n"
            "  \"type\": \"project\",\n"
            "  \"require\": {\n"
            "    \"php\": \"^8.3\",\n"
            "    \"laravel/framework\": \"^11.0\",\n"
            "    \"laravel/sanctum\": \"^4.0\"\n"
            "  },\n"
            "  \"autoload\": { \"psr-4\": { \"App\\\\\": \"app/\" } }\n"
            "}\n"
        )
    if path.endswith("Gemfile"):
        return (
            "source 'https://rubygems.org'\nruby '3.3.0'\n\n"
            "gem 'rails', '~> 7.2'\ngem 'pg'\ngem 'puma'\n"
            "gem 'jwt'\ngem 'bootsnap', require: false\n\n"
            "group :development, :test do\n  gem 'rspec-rails'\nend\n"
        )
    if path.endswith("Cargo.toml"):
        return (
            "[package]\n"
            f"name = \"{_kebab(name).replace('-', '_')}\"\n"
            "version = \"0.1.0\"\nedition = \"2021\"\n\n"
            "[dependencies]\n"
            "tokio = { version = \"1\", features = [\"full\"] }\n"
            "axum = \"0.7\"\nserde = { version = \"1\", features = [\"derive\"] }\n"
            "serde_json = \"1\"\nsqlx = { version = \"0.7\", features = [\"runtime-tokio\", \"postgres\"] }\n"
            "validator = { version = \"0.18\", features = [\"derive\"] }\n"
            "tracing = \"0.1\"\ntracing-subscriber = \"0.3\"\n"
        )
    if path.endswith("appsettings.json"):
        return (
            "{\n"
            "  \"Logging\": { \"LogLevel\": { \"Default\": \"Information\" } },\n"
            "  \"ConnectionStrings\": {\n"
            f"    \"Default\": \"Host=localhost;Database={_kebab(name)};Username=postgres;Password=postgres\"\n"
            "  },\n"
            "  \"Jwt\": { \"Issuer\": \"lama\", \"Audience\": \"lama-clients\" }\n"
            "}\n"
        )
    if path.endswith("pom.xml") or path.endswith(".csproj") or path.endswith("requirements.txt"):
        if path.endswith("requirements.txt"):
            return ("fastapi>=0.110\nuvicorn[standard]>=0.27\nsqlalchemy>=2.0\n"
                    "pydantic>=2\npsycopg2-binary>=2.9\nalembic>=1.13\n")
        if path.endswith(".csproj"):
            return ("<Project Sdk=\"Microsoft.NET.Sdk.Web\">\n"
                    "  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n"
                    "    <Nullable>enable</Nullable>\n  </PropertyGroup>\n</Project>\n")
        # pom.xml minimal Spring Boot 3
        return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                f"<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
                f"  <modelVersion>4.0.0</modelVersion>\n"
                f"  <groupId>{_ACTIVE_JAVA_GROUP}.{pkg}</groupId>\n"
                f"  <artifactId>{name}</artifactId>\n  <version>0.0.1-SNAPSHOT</version>\n"
                f"  <parent>\n    <groupId>org.springframework.boot</groupId>\n"
                f"    <artifactId>spring-boot-starter-parent</artifactId>\n"
                f"    <version>3.3.0</version>\n  </parent>\n"
                f"  <properties><java.version>21</java.version></properties>\n"
                f"  <dependencies>\n"
                f"    <dependency><groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-starter-web</artifactId></dependency>\n"
                f"    <dependency><groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-starter-data-jpa</artifactId></dependency>\n"
                f"    <dependency><groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-starter-validation</artifactId></dependency>\n"
                f"    <dependency><groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-starter-security</artifactId></dependency>\n"
                f"    <dependency><groupId>org.postgresql</groupId>"
                f"<artifactId>postgresql</artifactId><scope>runtime</scope></dependency>\n"
                f"    <dependency><groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>\n"
                f"  </dependencies>\n"
                f"  <build><plugins><plugin>\n"
                f"    <groupId>org.springframework.boot</groupId>"
                f"<artifactId>spring-boot-maven-plugin</artifactId>\n"
                f"  </plugin></plugins></build>\n"
                f"</project>\n")
    # -------------------------------------------------------------
    # Per-language code scaffolds (endpoint + table aware)
    # -------------------------------------------------------------
    if lang == "java":
        # iter-13.118 — Production-runnable Java path. Returns wired
        # CRUD (Controller→Service→Repository→Entity↔DTO↔Mapper↔
        # Validator↔Exception) with real method bodies, not TODO stubs.
        # Falls through to the legacy per-type branches below ONLY for
        # `bootstrap` / `security` / `errors` / `config` shapes (those
        # don't need DDL/endpoint awareness and were already adequate).
        js = _java_scaffold(file_def, svc, proj)
        if js is not None:
            return js
        # iter-13.118 — Below: only the file shapes `_java_scaffold` doesn't
        # cover (bootstrap / security / errors). Controller / Service /
        # Entity / Repository / DTO / Mapper / Validator / Exception /
        # Test are now all handled inside `_java_scaffold` with real
        # method bodies derived from OLTP DDL + endpoints.
        if ftype == "bootstrap":
            return (f"package {_ACTIVE_JAVA_GROUP}.{pkg};\n\n"
                    "import org.springframework.boot.SpringApplication;\n"
                    "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                    f"@SpringBootApplication\npublic class Application {{\n"
                    f"    public static void main(String[] args) {{ SpringApplication.run(Application.class, args); }}\n"
                    "}}\n")
        if ftype == "security":
            return (f"package {_ACTIVE_JAVA_GROUP}.{pkg}.config;\n\n"
                    "import org.springframework.context.annotation.*;\n"
                    "import org.springframework.security.config.annotation.web.builders.HttpSecurity;\n"
                    "import org.springframework.security.web.SecurityFilterChain;\n\n"
                    "@Configuration\npublic class SecurityConfig {\n"
                    "    @Bean\n    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {\n"
                    "        http.csrf(c -> c.disable()).authorizeHttpRequests(a -> a.anyRequest().permitAll());\n"
                    "        return http.build();\n    }\n"
                    "    // Gap Recovery may add role-based authorisation from legacy KB\n}\n")
        if ftype == "errors":
            if "Response" in cls or "ErrorResponse" in path:
                return (f"package {_ACTIVE_JAVA_GROUP}.{pkg}.web;\n\n"
                        "public record ErrorResponse(boolean error, String code, String message, String traceId) {}\n")
            return (f"package {_ACTIVE_JAVA_GROUP}.{pkg}.web;\n\n"
                    "import org.springframework.http.*;\nimport org.springframework.web.bind.annotation.*;\n\n"
                    "@RestControllerAdvice\npublic class GlobalExceptionHandler {\n"
                    "    @ExceptionHandler(Exception.class)\n"
                    "    public ResponseEntity<ErrorResponse> handle(Exception ex) {\n"
                    "        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)\n"
                    "            .body(new ErrorResponse(true, \"E_INTERNAL\", ex.getMessage(), \"\"));\n"
                    "    }\n}\n")
        # generic Java fallback — unrecognised shape; emit a benign
        # public class so the build doesn't break. Gap Recovery will
        # enrich based on the file path / surrounding service.
        return (f"package {_ACTIVE_JAVA_GROUP}.{pkg};\n\npublic class {cls} {{\n"
                f"    // LAMA scaffold — unrecognised file type '{ftype}'. "
                f"Run Gap Recovery to enrich.\n}}\n")
    if lang == "python":
        if ftype == "controller":
            blocks = ["from fastapi import APIRouter, HTTPException\n\nrouter = APIRouter()\n"]
            for ep in endpoints[:30]:
                verb, p = _ep_parts(ep)
                mname = _ep_method_name(verb, p)
                path_vars = re.findall(r"\{(\w+)\}", p)
                params = ", ".join(f"{pv}: str" for pv in path_vars)
                blocks.append(
                    f"\n# LEGACY: {ep}  — backfill via Gap Recovery\n"
                    f"@router.{verb.lower()}('{p}')\nasync def {mname}({params}):\n"
                    f"    # TODO: backfill from legacy KB\n"
                    f"    return {{'status': 'ok'}}\n"
                )
            if len(blocks) == 1:
                blocks.append("\n@router.get('/health')\nasync def health():\n    return {'status': 'ok'}\n")
            return "".join(blocks)
        if ftype == "service":
            methods = []
            for ep in endpoints[:30]:
                verb, p = _ep_parts(ep)
                mname = _ep_method_name(verb, p)
                methods.append(
                    f"    async def {mname}(self):\n"
                    f"        # LEGACY: {ep}  — backfill via Gap Recovery\n"
                    f"        raise NotImplementedError('TODO: backfill from legacy KB')\n"
                )
            if not methods:
                methods.append("    pass  # TODO: backfill from legacy KB\n")
            return (f"class {cls}Service:\n"
                    "    \"\"\"LAMA emergency scaffold — enrich via Gap Recovery.\"\"\"\n"
                    + "\n".join(methods))
        if ftype == "entity":
            return ("from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n\n"
                    "class Base(DeclarativeBase): pass\n\n"
                    f"class {cls}(Base):\n    __tablename__ = '{file_def.get('table') or _kebab(cls)}'\n"
                    "    id: Mapped[int] = mapped_column(primary_key=True)\n"
                    "    # TODO: backfill columns from OLTP DDL (LAMA emergency scaffold)\n")
        if ftype == "repository":
            return (f"class {cls}Repo:\n    def __init__(self, session): self.session = session\n"
                    "    # TODO: backfill from legacy KB (LAMA emergency scaffold)\n")
        if ftype == "bootstrap":
            return ("from fastapi import FastAPI\n\napp = FastAPI()\n\n"
                    "@app.get('/health')\nasync def health():\n"
                    f"    return {{'service': '{name}', 'status': 'ok'}}\n"
                    "# TODO: register routers (LAMA emergency scaffold)\n")
        return (f"# LAMA emergency scaffold for {path}\n# TODO: backfill from legacy KB\n")
    if lang == "dotnet":
        if ftype == "controller":
            methods = []
            for ep in endpoints[:30]:
                verb, p = _ep_parts(ep)
                mname = _pascal(_ep_method_name(verb, p))
                methods.append(
                    f"    // LEGACY: {ep}\n"
                    f"    [Http{verb.capitalize()}(\"{p}\")] public IActionResult {mname}() => Ok(new {{ status = \"ok\" }});\n"
                )
            if not methods:
                methods.append("    [HttpGet(\"health\")] public IActionResult Health() => Ok(\"ok\");\n")
            return ("using Microsoft.AspNetCore.Mvc;\n\n"
                    f"namespace {_pascal(name)}.Controllers;\n\n"
                    f"[ApiController]\n[Route(\"[controller]\")]\npublic class {cls}Controller : ControllerBase\n{{\n"
                    + "\n".join(methods) + "}\n")
        if ftype == "bootstrap":
            return ("var builder = WebApplication.CreateBuilder(args);\n"
                    "builder.Services.AddControllers();\nvar app = builder.Build();\n"
                    "app.MapControllers();\napp.Run();\n")
        return f"// LAMA emergency scaffold for {path}\n// TODO: backfill from legacy KB\n"
    # nodejs / default
    if ftype == "controller":
        lines = ["import { Router } from 'express';\nconst router = Router();\n"]
        for ep in endpoints[:30]:
            verb, p = _ep_parts(ep)
            mname = _ep_method_name(verb, p)
            lines.append(
                f"\n// LEGACY: {ep}\nrouter.{verb.lower()}('{p}', async (req, res) => {{\n"
                f"  // TODO: backfill from legacy KB\n"
                f"  res.json({{ status: 'ok' }});\n}});\n"
            )
        if len(lines) == 1:
            lines.append("\nrouter.get('/health', (_,res) => res.json({ status: 'ok' }));\n")
        lines.append("\nexport default router;\n")
        return "".join(lines)
    if ftype == "bootstrap":
        return ("import express from 'express';\nconst app = express();\napp.use(express.json());\n"
                f"app.get('/health', (_,res) => res.json({{ service:'{name}' }}));\n"
                "app.listen(process.env.PORT || 3000);\n// TODO: register routes (LAMA emergency scaffold)\n")
    return f"// LAMA emergency scaffold for {path}\n// TODO: backfill from legacy KB\n"


# ── iter-13.115 ──────────────────────────────────────────────────────────────
# Build a compact KB-grounding block from the *durable* KB artifacts (business
# ontology + deep legacy-analysis). This is the SECOND knowledge source — the
# first being graph/RAG/legacy-source slices — that anchors the codegen LLM in
# the project's own vocabulary (business entity names, owning modules, BR-IDs,
# state machines, validation rules) so generated classes carry the right names
# and the right semantics instead of generic Crud<Resource> shapes.
#
# Output is hard-capped at ~3500 chars so it never crowds out the legacy
# evidence / graph slice. Empty string when nothing is materialised yet —
# the prompt's `{kb_context}` placeholder degrades to a one-line note.
# ────────────────────────────────────────────────────────────────────────────
async def _build_kb_context(project_id: str, svc: dict, max_chars: int = 3500) -> str:
    lines: List[str] = []
    svc_name = (svc.get("name") or "").lower()
    svc_tables = {(t or "").lower() for t in (svc.get("tables") or [])}
    svc_modules = {(m or "").lower() for m in
                   ((svc.get("modules") or []) + (svc.get("module_names") or []))}

    def _belongs(text: str) -> bool:
        t = (text or "").lower()
        if svc_name and svc_name in t:
            return True
        if any(tbl and tbl in t for tbl in svc_tables):
            return True
        if any(mod and mod in t for mod in svc_modules):
            return True
        return False

    # 1) Business ontology — entity name + owning module + responsibility
    try:
        bo = await business_ontologies.find_one(
            {"project_id": project_id}, {"_id": 0, "entities": 1, "clusters": 1},
        )
        if bo:
            ents = (bo.get("entities") or [])[:200]
            # Prefer entities that belong to this service; fall back to top-N.
            scoped = [e for e in ents if _belongs(
                f"{e.get('name','')} {e.get('module','')} {' '.join(e.get('tables') or [])}"
            )] or ents[:8]
            if scoped:
                lines.append("business_entities:")
                for e in scoped[:12]:
                    nm = e.get("name") or e.get("label") or "?"
                    mod = e.get("module") or e.get("cluster") or "-"
                    resp = (e.get("responsibility") or e.get("description") or "").strip()
                    tbls = ", ".join((e.get("tables") or [])[:4])
                    line = f"  - name: {nm} | module: {mod}"
                    if tbls:
                        line += f" | tables: [{tbls}]"
                    if resp:
                        line += f"\n    responsibility: {resp[:180]}"
                    lines.append(line)
    except Exception:  # noqa: BLE001
        pass

    # 2) Deep legacy-analysis — business rules / state machines / validation rules
    try:
        la = await legacy_analysis.find_one(
            {"project_id": project_id},
            {"_id": 0, "business_rules": 1, "state_machines": 1,
             "validation_rules": 1, "approval_chains": 1, "calculation_rules": 1},
        )
        if la:
            def _emit(section: str, items: List[dict], fields: List[str], limit: int = 8):
                if not items:
                    return
                scoped = [it for it in items if _belongs(
                    " ".join(str(it.get(f, "")) for f in (fields + ["module", "owner", "table", "scope"]))
                )] or items[:limit]
                if not scoped:
                    return
                lines.append(f"{section}:")
                for it in scoped[:limit]:
                    bid = it.get("id") or it.get("rule_id") or it.get("name") or "?"
                    summary_parts = [str(it.get(f, "")).strip() for f in fields]
                    summary = " | ".join(p for p in summary_parts if p)
                    lines.append(f"  - {bid}: {summary[:220]}")

            _emit("business_rules",   la.get("business_rules")   or [], ["statement", "rule", "description"])
            _emit("validation_rules", la.get("validation_rules") or [], ["field", "rule", "error_code", "message"])
            _emit("state_machines",   la.get("state_machines")   or [], ["entity", "from", "to", "trigger", "guard"])
            _emit("approval_chains",  la.get("approval_chains")  or [], ["entity", "roles", "steps"])
            _emit("calculation_rules", la.get("calculation_rules") or [], ["name", "formula", "inputs"])
    except Exception:  # noqa: BLE001
        pass

    # 3) iter-13.116 — MONGO FALLBACK. When neither business_ontologies nor
    # legacy_analysis are materialised (project freshly built, deep-analysis
    # disabled, etc.) we still want the LLM to be GROUNDED in the actual
    # legacy repo instead of inventing names. Pull raw kb_entities (CLASS /
    # METHOD / ROUTE / TABLE_HINT) scoped to this service, plus a handful
    # of kb_chunks that mention the service tables/modules.
    if len([l for l in lines if not l.startswith(" ")]) == 0:
        try:
            # Classes + methods owned by this service
            q = {"project_id": project_id, "type": {"$in": ["CLASS", "ROUTE", "TABLE", "TABLE_HINT", "METHOD"]}}
            seeds_for_match = list(svc_tables) + list(svc_modules) + [svc_name]
            seeds_for_match = [s for s in seeds_for_match if s and len(s) >= 3]
            ents = await kb_entities.find(
                q, {"_id": 0, "type": 1, "name": 1, "source": 1, "namespace": 1,
                    "columns": 1, "methods": 1, "verb": 1},
            ).to_list(800)
            scoped_ents = []
            for e in ents:
                blob = f"{e.get('name','')} {e.get('namespace','')} {e.get('source','')}".lower()
                if not seeds_for_match or any(s in blob for s in seeds_for_match):
                    scoped_ents.append(e)
            if not scoped_ents:
                scoped_ents = ents[:40]  # last-resort: show ANY KB entities so the LLM has anchors

            classes = [e for e in scoped_ents if e["type"] == "CLASS"][:10]
            tables_kb = [e for e in scoped_ents if e["type"] in ("TABLE", "TABLE_HINT")][:10]
            routes_kb = [e for e in scoped_ents if e["type"] == "ROUTE"][:15]
            if classes:
                lines.append("legacy_classes:  # from MongoDB kb_entities (fallback grounding)")
                for c in classes:
                    nm = c.get("name") or "?"
                    src = (c.get("source") or "").rsplit("/", 1)[-1]
                    methods = [m.get("name") for m in (c.get("methods") or [])[:6] if m.get("name")]
                    line = f"  - {nm}  ({src})"
                    if methods:
                        line += f"  methods: [{', '.join(methods)}]"
                    lines.append(line)
            if tables_kb:
                lines.append("legacy_tables:  # from MongoDB kb_entities")
                for t in tables_kb:
                    nm = t.get("name") or "?"
                    cols = [c.get("name") for c in (t.get("columns") or [])[:8] if c.get("name")]
                    line = f"  - {nm}"
                    if cols:
                        line += f"  columns: [{', '.join(cols)}]"
                    lines.append(line)
            if routes_kb:
                lines.append("legacy_routes:  # from MongoDB kb_entities")
                for r in routes_kb:
                    verb = r.get("verb") or "GET"
                    nm = r.get("name") or "/"
                    src = (r.get("source") or "").rsplit("/", 1)[-1]
                    lines.append(f"  - {verb} {nm}  ({src})")
            # Light chunks — useful when class/route info is too sparse
            if not (classes or tables_kb or routes_kb):
                chunks = await kb_chunks.find(
                    {"project_id": project_id},
                    {"_id": 0, "text": 1, "filename": 1},
                ).limit(60).to_list(60)
                hits = []
                for ch in chunks:
                    blob = (ch.get("text") or "").lower()
                    if any(s in blob for s in seeds_for_match):
                        hits.append(ch)
                if hits:
                    lines.append("legacy_chunks:  # from MongoDB kb_chunks")
                    for ch in hits[:6]:
                        fn = (ch.get("filename") or "?").rsplit("/", 1)[-1]
                        snippet = (ch.get("text") or "").replace("\n", " ")[:220]
                        lines.append(f"  - {fn}: {snippet}")
        except Exception:  # noqa: BLE001
            pass

    if not lines:
        return ""
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit("\n", 1)[0] + "\n  # ... (truncated)"
    return out


# ── iter-13.117 ──────────────────────────────────────────────────────────────
# SERVICE ENRICHMENT. When the Architecture stage leaves a service with empty
# `tables` / `api_endpoints`, the codegen plan would only emit the 6 bootstrap
# files. We rescue the run by deriving:
#   • tables   ← OLTP DDL `CREATE TABLE` names partitioned by service-name /
#                module-name token match (or fully distributed when no match)
#   • endpoints ← frozen API contracts (paths whose first segment matches the
#                 service name) — when those are also empty we SYNTHESISE the
#                 standard CRUD set per table (list/get/create/update/delete)
# This guarantees every service has at least one Controller + Service +
# Repository + Mapper + Validator + Exception per resource, even when arch
# was sparse.
# ────────────────────────────────────────────────────────────────────────────
# iter-14.33 — Effective OLTP DDL resolver.
# When the DataModel stage is SKIPPED (or produced empty `oltp_ddl`), the
# legacy schema still lives in the KB as @Table hints + JPA model classes
# (Java/Struts/Spring) or as SQL-dump TABLE entities (PHP/MySQL). This helper
# returns the DataModel DDL when present, else synthesises CREATE-TABLE DDL
# from the legacy KB so every legacy table still yields an @Entity + repository
# at CodeGen. Cached per-project for the duration of the process to avoid
# recomputing the harvest across the ~5 read sites in a single job.
_EFFECTIVE_DDL_CACHE: Dict[str, str] = {}


async def _effective_oltp_ddl(project_id: str, raw_ddl: str) -> str:
    """Return real OLTP DDL, falling back to legacy-KB-reconstructed DDL."""
    if raw_ddl and raw_ddl.strip():
        return raw_ddl
    if project_id in _EFFECTIVE_DDL_CACHE:
        return _EFFECTIVE_DDL_CACHE[project_id]
    try:
        ddl, count = await build_oltp_ddl_from_kb(project_id)
    except Exception as _e:  # noqa: BLE001
        logger.warning("legacy-KB DDL harvest failed: %s", _e)
        ddl, count = "", 0
    if ddl:
        logger.info(
            "codegen effective-DDL: DataModel empty → harvested %d legacy "
            "tables from KB for project=%s", count, project_id,
        )
    _EFFECTIVE_DDL_CACHE[project_id] = ddl
    return ddl


# ────────────────────────────────────────────────────────────────────────────
async def _enrich_services_from_kb(project_id: str, services: List[dict]) -> None:
    if not services:
        return
    # iter-13.117 caps — keep the plan bounded so Ollama can actually finish.
    # A real enterprise legacy app may have 2000+ tables, but a single service
    # meaningfully owns only 5-30. We pick the highest-signal subset by
    # cross-referencing kb_entities CLASSes (which carry the actual legacy
    # owner module name) and the service-name token. The leftover tables fall
    # to platform-shared-kernel as `domain/` entities — still useful, still
    # owned, but not blowing up the controller fan-out.
    MAX_TABLES_PER_SVC = int(os.environ.get("LAMA_CODEGEN_MAX_TABLES_PER_SVC", "12") or 12)
    MAX_ENDPOINTS_PER_SVC = int(os.environ.get("LAMA_CODEGEN_MAX_ENDPOINTS_PER_SVC", "30") or 30)
    # 1) Pull OLTP DDL
    dm = await stage_context_col.find_one(
        {"project_id": project_id, "stage": "DataModel"}, {"_id": 0, "outputs": 1},
    )
    oltp_ddl = await _effective_oltp_ddl(
        project_id, ((dm or {}).get("outputs", {}) or {}).get("oltp_ddl", ""),
    )
    # 1b) Pull legacy CLASS entities so we can prefer tables that have a
    #     matching legacy domain class (better evidence → better LLM output)
    try:
        cls_ents = await kb_entities.find(
            {"project_id": project_id, "type": "CLASS"},
            {"_id": 0, "name": 1, "namespace": 1, "source": 1},
        ).to_list(800)
    except Exception:  # noqa: BLE001
        cls_ents = []
    cls_corpus = " ".join(
        (c.get("name", "") + " " + c.get("namespace", "") + " " + c.get("source", ""))
        for c in cls_ents
    ).lower()
    # 2) Pull API contracts (paths) — OPTIONAL (iter-14.30)
    # Primary: Architecture stage API contracts document
    # Fallback: kb_entities ROUTE type (includes Spring MVC + Struts + all frameworks)
    arch_doc = await arch_documents.find_one(
        {"project_id": project_id, "kind": "api_contracts"},
        {"_id": 0, "content": 1},
    ) or await arch_documents.find_one(
        {"project_id": project_id, "kind": {"$regex": "api", "$options": "i"}},
        {"_id": 0, "content": 1},
    )
    api_paths: list[tuple[str, str]] = []  # (verb, path)
    if arch_doc and arch_doc.get("content"):
        for m in re.finditer(r"(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}]+)",
                              arch_doc["content"]):
            api_paths.append((m.group(1).upper(), m.group(2)))
    
    # iter-14.30 — FALLBACK: Pull ALL ROUTE entities from KB when API contracts
    # are empty. This includes Spring MVC (@RequestMapping/@GetMapping/etc),
    # Struts 1/2 (struts-config.xml action mappings, *.do),
    # JSP form actions, PHP routes, Express/FastAPI routes, etc.
    # This ensures ALL legacy APIs are captured regardless of framework.
    if not api_paths:
        logger.info("_enrich_services_from_kb: API contracts empty, falling back to KB ROUTE entities")
        try:
            route_ents = await kb_entities.find(
                {"project_id": project_id, "type": "ROUTE"},
                {"_id": 0, "name": 1, "verb": 1, "framework": 1, "handler_class": 1},
            ).to_list(500)
            seen_paths: set[str] = set()
            for r in route_ents:
                path = (r.get("name") or "").strip()
                if not path:
                    continue
                # Normalize: ensure path starts with /
                if not path.startswith("/"):
                    path = "/" + path
                # Extract verb (default to ANY for Struts/.do actions)
                verb = (r.get("verb") or "ANY").upper()
                if verb == "ANY":
                    # Expand ANY to common verbs for CRUD coverage
                    for v in ("GET", "POST", "PUT", "DELETE"):
                        key = f"{v}:{path}"
                        if key not in seen_paths:
                            seen_paths.add(key)
                            api_paths.append((v, path))
                else:
                    key = f"{verb}:{path}"
                    if key not in seen_paths:
                        seen_paths.add(key)
                        api_paths.append((verb, path))
            logger.info(
                "_enrich_services_from_kb: loaded %d endpoints from KB ROUTE entities "
                "(Spring + Struts + JSP + PHP + Express + all frameworks)",
                len(api_paths),
            )
        except Exception as _e:
            logger.warning("_enrich_services_from_kb: KB ROUTE fallback failed: %s", _e)

    biz_services = [s for s in services if not s.get("frontend")]
    if not biz_services:
        return

    # iter-14.20.10 — populate `all_tables` from OLTP DDL first, then fall
    # back to `kb_entities` TABLE rows (real projects have live-DB
    # introspection tables even when DataModel was skipped). Previously
    # this variable was USED on line ~2364 but never defined, so the
    # whole enrichment silently NameError'd and every service reached
    # `_backend_files_plan` with empty tables + endpoints.
    all_tables: list[str] = []
    seen_tables: set[str] = set()
    if oltp_ddl:
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?",
            oltp_ddl, re.IGNORECASE,
        ):
            t = m.group(1)
            tl = t.lower()
            if tl not in seen_tables:
                seen_tables.add(tl)
                all_tables.append(t)
    if not all_tables:
        try:
            async for row in kb_entities.find(
                {"project_id": project_id, "type": "TABLE"},
                {"_id": 0, "name": 1},
            ):
                t = (row.get("name") or "").strip()
                if not t:
                    continue
                tl = t.lower()
                if tl in seen_tables:
                    continue
                seen_tables.add(tl)
                all_tables.append(t)
        except Exception as _e:
            logger.debug("kb_entities TABLE fallback failed: %s", _e)
    logger.info(
        "_enrich_services_from_kb: all_tables=%d (source=%s)",
        len(all_tables),
        "oltp_ddl" if oltp_ddl else "kb_entities",
    )

    # 3) Distribute tables to services
    #    a) by exact substring match (service name / module token in table name)
    #    b) by KB-CLASS evidence — pick tables that appear next to a legacy
    #       class whose namespace/source mentions the service token
    #    c) leftovers → assign to `platform-shared-kernel` if it exists,
    #       otherwise round-robin to fill empty services up to MAX_TABLES_PER_SVC
    assigned_tables: dict[str, list[str]] = {s["name"]: list(s.get("tables") or []) for s in biz_services}
    unclaimed = [t for t in all_tables if all(t not in tbls for tbls in assigned_tables.values())]

    # Specific keyword expansion for common domains
    EXPANSIONS = {
        "clinicalrecords": ["clinical", "patient", "record", "diagnosis", "treatment", "prescription"],
        "accessidentity":  ["user", "role", "auth", "permission", "login", "session", "token"],
        "documentmanagement": ["doc", "document", "file", "attachment", "upload"],
        "hospitaldiscovery": ["hospital", "facility", "branch", "location"],
        "journalistprovisioning": ["journalist", "reporter", "media"],
        "worklistreporting":     ["worklist", "report", "task", "queue", "assignment"],
        "followupmanagement":    ["followup", "follow_up", "appointment", "schedule", "reminder"],
        "platformsharedkernel":  ["audit", "config", "lookup", "master"],
    }

    def _name_tokens(raw: str) -> list[str]:
        n = (raw or "").lower().replace("-", "").replace("_", "")
        if not n:
            return []
        out = [n]
        if n.endswith("s"):
            out.append(n[:-1])
        if n.endswith("ing"):
            out.append(n[:-3])
            # -ing → -e (invoicing → invoice, receiving → receive)
            out.append(n[:-3] + "e")
        out.extend(EXPANSIONS.get(n, []))
        # Also include the individual dash/underscore-separated words
        # so a merged name like "billing-invoicing-service" matches
        # tables named "invoice_line" or "billing_run" without needing
        # merged_from provenance to be perfect.
        for part in re.split(r"[-_\s]+", (raw or "").lower()):
            p = part.strip()
            if p and len(p) >= 3 and p not in {"service", "svc", "api", "and", "the", "for"}:
                out.append(p)
                if p.endswith("s"):
                    out.append(p[:-1])
                if p.endswith("ing"):
                    out.append(p[:-3])
                    out.append(p[:-3] + "e")
        return out

    def _service_tokens(s: dict) -> list[str]:
        tokens: list[str] = []
        tokens.extend(_name_tokens(s.get("name") or ""))
        tokens.extend(m.lower() for m in (s.get("modules") or s.get("module_names") or []))
        # iter-14.30 — merged service: also expand tokens for each
        # source service name AND its display_name so name-matched
        # tables / API-contract endpoints still land on the merged row.
        # Without this, merging "billing" + "invoicing" into
        # "billing-invoicing-svc" would lose every table matching
        # "billing" or "invoicing" during _enrich_services_from_kb.
        for mf in (s.get("merged_from") or []):
            tokens.extend(_name_tokens(str(mf)))
        return [tok for tok in tokens if tok and len(tok) >= 3]

    # Pass 3a — exact / expanded token match
    for t in list(unclaimed):
        tl = t.lower()
        for s in biz_services:
            if len(assigned_tables[s["name"]]) >= MAX_TABLES_PER_SVC:
                continue
            tokens = _service_tokens(s)
            if any(tok in tl for tok in tokens):
                assigned_tables[s["name"]].append(t)
                unclaimed.remove(t)
                break

    # iter-14.20.10 — Seed each service's `module_names` from real
    # legacy CLASS entities whose name / namespace / source-path
    # mentions ANY service token. This gives `_deep_legacy_context`
    # actual class-name seeds to substring-match against, instead of
    # only the generic tokens ("controller", "workflow") that never
    # match real legacy code. Runs AFTER pass 3a because it reuses
    # `_service_tokens`.
    if cls_ents:
        for s in biz_services:
            svc_toks = [t for t in _service_tokens(s) if len(t) >= 4]
            if not svc_toks:
                continue
            picked: list[str] = []
            for c in cls_ents:
                nm = (c.get("name") or "").strip()
                blob = (nm + " " + c.get("namespace", "") + " " + c.get("source", "")).lower()
                if any(t in blob for t in svc_toks):
                    picked.append(nm)
                if len(picked) >= 40:
                    break
            if picked:
                existing = set((s.get("module_names") or []))
                s["module_names"] = list(existing.union(picked))
                logger.info(
                    "_enrich_services_from_kb: seeded %d legacy CLASS names into module_names for svc=%s (e.g. %s)",
                    len(picked), s.get("name"), picked[:3],
                )


    # Pass 3b — KB-class-evidenced match (table appears near a class whose
    # namespace mentions the service token)
    if cls_corpus:
        for t in list(unclaimed):
            tl = t.lower()
            for s in biz_services:
                if len(assigned_tables[s["name"]]) >= MAX_TABLES_PER_SVC:
                    continue
                tokens = _service_tokens(s)
                # If the table name appears in the KB-class corpus AND any
                # service token is also in that corpus near the table name,
                # we assume ownership.
                idx = cls_corpus.find(tl)
                if idx >= 0:
                    window = cls_corpus[max(0, idx-200):idx+200]
                    if any(tok in window for tok in tokens):
                        assigned_tables[s["name"]].append(t)
                        unclaimed.remove(t)
                        break

    # Pass 3c — leftover unclaimed tables: park on platform-shared-kernel
    # (capped) so they at least get @Entity classes generated.
    shared = next((s for s in biz_services if "kernel" in s["name"].lower()
                   or "shared" in s["name"].lower() or "common" in s["name"].lower()), None)
    if shared and unclaimed:
        room = max(0, MAX_TABLES_PER_SVC - len(assigned_tables[shared["name"]]))
        if room:
            assigned_tables[shared["name"]].extend(unclaimed[:room])
            unclaimed = unclaimed[room:]

    # Pass 3d — BLOCKER FIX (iter-14.30): When a service has NO tables assigned
    # after all passes, assign ALL unclaimed tables (not a synthetic self-named
    # table). This ensures entity/repository names match actual legacy tables
    # from OLTP DDL, not arbitrary synthesized names. If there are still
    # unclaimed tables, assign them ALL to the FIRST empty service so no
    # legacy table is ever missed.
    for s in biz_services:
        if not assigned_tables[s["name"]]:
            if unclaimed:
                # Take ALL unclaimed tables — we must not miss any legacy table
                assigned_tables[s["name"]] = list(unclaimed)
                logger.info(
                    "_enrich_services_from_kb: assigned ALL %d unclaimed tables to svc=%s "
                    "(strict legacy parity — no synthesized names)",
                    len(unclaimed), s["name"],
                )
                unclaimed = []
            elif all_tables:
                # Fallback: if all tables were claimed but this service is empty,
                # give it access to ALL tables (monolith behavior) so no entity is missed
                assigned_tables[s["name"]] = list(all_tables)
                logger.info(
                    "_enrich_services_from_kb: assigned ALL %d known tables to empty svc=%s "
                    "(monolith fallback — strict legacy parity)",
                    len(all_tables), s["name"],
                )

    # Pass 3e — NO-DROP GUARANTEE (iter-14.33). The per-service cap
    # (MAX_TABLES_PER_SVC) is a controller-fan-out guard, not a reason to
    # LOSE a legacy table. Any table still unclaimed after all passes is
    # parked (uncapped) on the shared-kernel service — else the first
    # backend service — so every legacy table materialises as an @Entity +
    # repository. This is what enforces "repo/entity exactly like legacy".
    if unclaimed:
        sink = shared or (biz_services[0] if biz_services else None)
        if sink is not None:
            existing = set(assigned_tables[sink["name"]])
            added = [t for t in unclaimed if t not in existing]
            assigned_tables[sink["name"]].extend(added)
            logger.info(
                "_enrich_services_from_kb: parked %d leftover legacy tables on "
                "svc=%s (no-drop guarantee — full legacy parity)",
                len(added), sink["name"],
            )
            unclaimed = []

    # 4) Distribute endpoints (or synthesise CRUD per-table when contracts empty)
    assigned_eps: dict[str, list[str]] = {s["name"]: list(s.get("api_endpoints") or []) for s in biz_services}
    for verb, p in api_paths:
        parts = [x for x in p.lstrip("/").split("/") if x and not re.match(r"^v\d+$", x)]
        first_seg = (parts[1] if (parts and parts[0].lower() == "api" and len(parts) > 1) else (parts[0] if parts else "")).lower()
        for s in biz_services:
            # iter-14.30 — reuse the tokenizer so merged-from names also
            # participate in the first-segment match.
            svc_tokens = {tok for tok in _service_tokens(s) if tok}
            if first_seg and any(
                first_seg in tok or tok in first_seg for tok in svc_tokens
            ):
                line = f"{verb} {p}"
                if line not in assigned_eps[s["name"]] and len(assigned_eps[s["name"]]) < MAX_ENDPOINTS_PER_SVC:
                    assigned_eps[s["name"]].append(line)
                break
    # Synthesise CRUD endpoints per-table when service still has none
    for s in biz_services:
        if assigned_eps[s["name"]]:
            continue
        room = MAX_ENDPOINTS_PER_SVC
        for t in assigned_tables[s["name"]]:
            res = t.lower().rstrip("s") + "s"
            crud = [
                f"GET /api/v1/{res}",
                f"GET /api/v1/{res}/{{id}}",
                f"POST /api/v1/{res}",
                f"PUT /api/v1/{res}/{{id}}",
                f"DELETE /api/v1/{res}/{{id}}",
            ]
            take = crud[:max(0, room)]
            assigned_eps[s["name"]].extend(take)
            room -= len(take)
            if room <= 0:
                break

    # 5) Mutate the in-memory svc dicts AND persist so future runs benefit
    for s in biz_services:
        n = s["name"]
        changed = False
        # iter-14.33 — sync whenever enrichment produced MORE tables than the
        # service currently carries (covers the no-drop sink service that
        # already had a non-empty — but incomplete — table list).
        if assigned_tables[n] and len(assigned_tables[n]) > len(s.get("tables") or []):
            s["tables"] = assigned_tables[n]
            changed = True
        if not s.get("api_endpoints") and assigned_eps[n]:
            s["api_endpoints"] = assigned_eps[n]
            changed = True
        if changed:
            try:
                await arch_services.update_one(
                    {"project_id": project_id, "name": n},
                    {"$set": {"tables": s["tables"], "api_endpoints": s["api_endpoints"]}},
                )
                logger.info(
                    "iter-13.117 enriched service=%s tables=%d endpoints=%d (caps: t<=%d e<=%d)",
                    n, len(s["tables"]), len(s["api_endpoints"]),
                    MAX_TABLES_PER_SVC, MAX_ENDPOINTS_PER_SVC,
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning("iter-13.117 persist failed for %s: %s", n, _e)


def _backend_files_plan(svc: dict, pattern: str = "microservices") -> List[dict]:
    """Fan out per-resource controllers + per-table repos so every legacy
    API gets at least one dedicated file. iter-13.27 — was previously a
    single Controller per service which led to silent truncation.

    iter-13.121 — architecture-pattern aware. ``pattern`` picks the layout:
      • microservices    → one deployable per service (``services/{name}/…``);
                           per-service bootstrap files (pom.xml, Dockerfile,
                           application.yml, package.json, requirements.txt,
                           …) are emitted here.
      • modular_monolith → ONE deployable rooted at the project root; each
                           service becomes a folder under ``modules/{name}/``
                           and the shared bootstrap (pom.xml, Dockerfile,
                           application.yml, docker-compose.yml, README.md,
                           .github/workflows/ci.yml) lives at the ROOT once
                           (materialised by ``_run_codegen_job`` root_files).
      • monolith         → ONE deployable, NO module folders — every class
                           lives under the single project source tree
                           (``src/main/java/com/lama/{module_dir}`` for
                           Java, ``app/{module_dir}`` for Python, etc.)
                           Shared bootstrap lives at the ROOT once.
    Per-service Dockerfile / README / config files are ONLY emitted for
    ``microservices``; the monolith / modular_monolith variants must not
    duplicate them per module."""
    lang = svc.get("backend_lang", "nodejs")
    name = svc["name"]
    pattern = (pattern or "microservices").strip().lower()
    if pattern not in ("microservices", "modular_monolith", "monolith"):
        pattern = "microservices"
    if pattern == "monolith":
        base = ""
        module_dir = re.sub(r"[^a-z0-9_]", "_", name.lower()) or "app"
    elif pattern == "modular_monolith":
        base = f"modules/{name}"
        module_dir = ""
    else:  # microservices
        base = f"services/{name}"
        module_dir = ""
    tables = [t for t in (svc.get("tables") or []) if t]
    endpoints = svc.get("api_endpoints") or []
    groups = _group_endpoints_by_resource(endpoints)
    # iter-13.81.16 — When a service exposes ZERO HTTP endpoints (e.g.
    # shared-kernel / utility / library modules) DO NOT synthesize a
    # `root` resource group. The previous `or {"root": []}` fallback
    # caused `RootController.java` to be planned with no endpoints, and
    # the LLM then hallucinated a meaningless `@RestController` (with a
    # constructor and a `declaredApiEndpoints()` getter). For a no-API
    # service we drop Controller/Service/DTO/Test plan entries entirely
    # and also drop the HTTP-only scaffolding (SecurityConfig,
    # OpenApiConfig, GlobalExceptionHandler, ErrorResponse, security
    # middleware) since they're nonsense for a library JAR/package.
    has_api = bool(endpoints) and bool(groups)
    svc_kind = (svc.get("kind") or svc.get("type") or "").lower()
    is_library_kind = any(
        tok in svc_kind or tok in name.lower()
        for tok in ("shared-kernel", "shared_kernel", "sharedkernel",
                    "kernel", "common", "library", "lib", "utils", "util")
    )
    # iter-13.118.2 — library classification is now NAME-DRIVEN ONLY.
    # The old rule `is_library = (not has_api) or (is_library_kind and not
    # has_api)` collapsed ANY service with empty endpoints into a 6-file
    # library plan — which is exactly the "nothing happened" symptom on
    # business services whose Stage-3 LLD failed to populate endpoints.
    # With the post-enrichment safety net in `_run_codegen_job` every
    # business service now arrives here with at least 1 table + 5 CRUD
    # endpoints, so this branch only ever fires for explicitly-named
    # shared-kernel / library / util modules.
    is_library = is_library_kind and not has_api

    # iter-13.121 — Per-service bootstrap files (Dockerfile, .env.example,
    # per-service README) are ONLY meaningful in microservices mode. In
    # monolith / modular_monolith the shared bootstrap lives at the project
    # root (emitted once by `_run_codegen_job`).
    plan: List[dict] = []
    if pattern == "microservices":
        plan += [
            {"path": f"{base}/Dockerfile", "type": "dockerfile", "language": "dockerfile"},
            {"path": f"{base}/.env.example", "type": "config", "language": "ini"},
            {"path": f"{base}/README.md", "type": "docs", "language": "markdown"},
        ]

    if lang == "java":
        pkg = re.sub(r"[^a-z0-9]", "", name.lower()) or "app"
        if pattern == "monolith":
            java_base = f"src/main/java/{_ACTIVE_JAVA_GROUP_PATH}/{pkg}"
        else:
            java_base = f"{base}/src/main/java/{_ACTIVE_JAVA_GROUP_PATH}/{pkg}"
        if pattern == "microservices":
            plan += [
                {"path": f"{base}/pom.xml", "type": "config", "language": "xml"},
                {"path": f"{base}/src/main/resources/application.yml", "type": "config", "language": "yaml"},
                {"path": f"{java_base}/Application.java", "type": "bootstrap", "language": "java"},
            ]
        if not is_library:
            plan += [
                {"path": f"{java_base}/config/SecurityConfig.java", "type": "security", "language": "java"},
                {"path": f"{java_base}/config/OpenApiConfig.java", "type": "config", "language": "java"},
                {"path": f"{java_base}/web/GlobalExceptionHandler.java", "type": "errors", "language": "java"},
                {"path": f"{java_base}/web/ErrorResponse.java", "type": "dto", "language": "java"},
            ]
        for t in tables:
            cls = _pascal(t)
            plan.append({"path": f"{java_base}/domain/{cls}.java",
                         "type": "entity", "language": "java",
                         "table": t})
            plan.append({"path": f"{java_base}/repo/{cls}Repository.java",
                         "type": "repository", "language": "java",
                         "table": t})
        for resource, eps in groups.items():
            cls = _pascal(resource)
            plan.append({"path": f"{java_base}/web/{cls}Controller.java",
                         "type": "controller", "language": "java",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{java_base}/service/{cls}Service.java",
                         "type": "service", "language": "java",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{java_base}/dto/{cls}Dtos.java",
                         "type": "dto", "language": "java",
                         "resource": resource, "endpoints": eps})
            # iter-13.115 — production-grade per-resource split. Without these
            # dedicated files the LLM crams Mapper/Validator/Exception logic
            # into the DTO file → silent token truncation → caller never sees
            # half the classes. One file per concern keeps each emission
            # within the 12k-token budget AND lets the structural validator
            # check each layer independently.
            plan.append({"path": f"{java_base}/mapper/{cls}Mapper.java",
                         "type": "mapper", "language": "java",
                         "resource": resource, "endpoints": eps,
                         "table": (tables[0] if tables else None)})
            plan.append({"path": f"{java_base}/validation/{cls}Validator.java",
                         "type": "validator", "language": "java",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{java_base}/exception/{cls}Exception.java",
                         "type": "exception", "language": "java",
                         "resource": resource})
        plan.append({"path": (f"{base}/src/test/java/{_ACTIVE_JAVA_GROUP_PATH}/{pkg}/{_pascal(name)}IT.java"
                              if pattern != "monolith"
                              else f"src/test/java/{_ACTIVE_JAVA_GROUP_PATH}/{pkg}/{_pascal(name)}IT.java"),
                     "type": "test", "language": "java",
                     "endpoints": endpoints})

    elif lang == "python":
        py_base = f"{base}/app" if base else f"app/{module_dir}" if pattern == "monolith" else "app"
        if pattern == "microservices":
            plan += [
                {"path": f"{base}/requirements.txt", "type": "config", "language": "text"},
                {"path": f"{py_base}/__init__.py", "type": "config", "language": "python"},
                {"path": f"{py_base}/main.py", "type": "bootstrap", "language": "python"},
                {"path": f"{py_base}/db.py", "type": "config", "language": "python"},
            ]
        else:
            # monolith / modular_monolith — only the module init exists per
            # module; requirements.txt / main.py / db.py live at the root.
            plan.append({"path": f"{py_base}/__init__.py", "type": "config", "language": "python"})
        if not is_library:
            plan += [
                {"path": f"{py_base}/security.py", "type": "security", "language": "python"},
                {"path": f"{py_base}/errors.py", "type": "errors", "language": "python"},
            ]
        for t in tables:
            cls = _pascal(t)
            plan.append({"path": f"{py_base}/models/{t}.py",
                         "type": "entity", "language": "python", "table": t})
            plan.append({"path": f"{py_base}/repositories/{t}_repo.py",
                         "type": "repository", "language": "python", "table": t})
        for resource, eps in groups.items():
            plan.append({"path": f"{py_base}/routers/{resource}.py",
                         "type": "controller", "language": "python",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{py_base}/services/{resource}_service.py",
                         "type": "service", "language": "python",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{py_base}/schemas/{resource}.py",
                         "type": "dto", "language": "python",
                         "resource": resource, "endpoints": eps})
            # iter-13.115 — production-grade per-resource split (parity with java plan).
            plan.append({"path": f"{py_base}/mappers/{resource}_mapper.py",
                         "type": "mapper", "language": "python",
                         "resource": resource, "endpoints": eps,
                         "table": (tables[0] if tables else None)})
            plan.append({"path": f"{py_base}/validators/{resource}_validator.py",
                         "type": "validator", "language": "python",
                         "resource": resource, "endpoints": eps})
            plan.append({"path": f"{py_base}/exceptions/{resource}_exceptions.py",
                         "type": "exception", "language": "python",
                         "resource": resource})
        plan.append({"path": (f"{base}/tests/test_{name}.py"
                              if pattern == "microservices"
                              else f"tests/test_{name}.py"),
                     "type": "test", "language": "python", "endpoints": endpoints})

    elif lang == "dotnet":
        ns = _pascal(name)
        if pattern == "microservices":
            plan += [
                {"path": f"{base}/{ns}.csproj", "type": "config", "language": "xml"},
                {"path": f"{base}/Program.cs", "type": "bootstrap", "language": "csharp"},
                {"path": f"{base}/appsettings.json", "type": "config", "language": "json"},
            ]
        # monolith / modular_monolith: root-level csproj + Program.cs (root_files)
        # For monolith we collapse the class prefix into the shared source tree.
        cs_base = base if pattern != "monolith" else "src"
        if not is_library:
            plan.append({"path": f"{cs_base}/Errors/ErrorResponse.cs", "type": "errors", "language": "csharp"})
        for t in tables:
            cls = _pascal(t)
            plan.append({"path": f"{cs_base}/Domain/{cls}.cs", "type": "entity", "language": "csharp", "table": t})
            plan.append({"path": f"{cs_base}/Repositories/{cls}Repository.cs", "type": "repository", "language": "csharp", "table": t})
        for resource, eps in groups.items():
            cls = _pascal(resource)
            plan.append({"path": f"{cs_base}/Controllers/{cls}Controller.cs", "type": "controller", "language": "csharp", "resource": resource, "endpoints": eps})
            plan.append({"path": f"{cs_base}/Services/{cls}Service.cs", "type": "service", "language": "csharp", "resource": resource, "endpoints": eps})
            plan.append({"path": f"{cs_base}/Dtos/{cls}Dtos.cs", "type": "dto", "language": "csharp", "resource": resource, "endpoints": eps})
        plan.append({"path": (f"{base}/Tests/{ns}Tests.cs" if pattern == "microservices" else f"Tests/{ns}Tests.cs"),
                     "type": "test", "language": "csharp", "endpoints": endpoints})

    elif lang == "go":
        if pattern == "microservices":
            plan += [
                {"path": f"{base}/go.mod", "type": "config", "language": "text"},
                {"path": f"{base}/main.go", "type": "bootstrap", "language": "go"},
                {"path": f"{base}/internal/db/db.go", "type": "config", "language": "go"},
            ]
        # For monolith / modular_monolith, per-service files are namespaced
        # by service name inside `internal/{name}/...` so no path collisions.
        _mod = re.sub(r"[^a-z0-9_]", "_", name.lower()) or "app"
        _pfx = (f"{base}/" if pattern == "microservices"
                else f"internal/{_mod}/" if pattern == "monolith"
                else f"{base}/")
        # In monolith we drop the extra `internal/` (paths become
        # internal/{mod}/errors/... etc. — one level up). For
        # modular_monolith we keep the per-service base intact
        # (modules/{name}/internal/...).
        if pattern == "monolith":
            _epath = f"{_pfx}errors/errors.go"
            _mpath_f = lambda t: f"{_pfx}model/{t}.go"
            _rpath_f = lambda t: f"{_pfx}repo/{t}_repo.go"
            _hpath_f = lambda r: f"{_pfx}handler/{r}_handler.go"
            _spath_f = lambda r: f"{_pfx}service/{r}_service.go"
            _tpath   = f"{_pfx}handler/handler_test.go"
        else:
            _epath = f"{_pfx}internal/errors/errors.go"
            _mpath_f = lambda t: f"{_pfx}internal/model/{t}.go"
            _rpath_f = lambda t: f"{_pfx}internal/repo/{t}_repo.go"
            _hpath_f = lambda r: f"{_pfx}internal/handler/{r}_handler.go"
            _spath_f = lambda r: f"{_pfx}internal/service/{r}_service.go"
            _tpath   = f"{_pfx}internal/handler/handler_test.go"
        if not is_library:
            plan.append({"path": _epath, "type": "errors", "language": "go"})
        for t in tables:
            plan.append({"path": _mpath_f(t), "type": "entity", "language": "go", "table": t})
            plan.append({"path": _rpath_f(t), "type": "repository", "language": "go", "table": t})
        for resource, eps in groups.items():
            plan.append({"path": _hpath_f(resource), "type": "controller", "language": "go", "resource": resource, "endpoints": eps})
            plan.append({"path": _spath_f(resource), "type": "service", "language": "go", "resource": resource, "endpoints": eps})
        # iter-13.84 — go plan was missing the test file. Every service
        # MUST ship at least one test so `go test ./...` exits 0 standalone.
        plan.append({"path": _tpath, "type": "test", "language": "go", "endpoints": endpoints})

    else:  # nodejs / fallback
        _nm = re.sub(r"[^a-z0-9_]", "_", name.lower()) or "app"
        if pattern == "monolith":
            node_base = f"src/{_nm}"
        elif base:
            node_base = f"{base}/src"
        else:
            node_base = "src"
        if pattern == "microservices":
            plan += [
                {"path": f"{base}/package.json", "type": "config", "language": "json"},
                {"path": f"{node_base}/server.js", "type": "bootstrap", "language": "javascript"},
                {"path": f"{node_base}/db.js", "type": "config", "language": "javascript"},
            ]
        if not is_library:
            plan += [
                {"path": f"{node_base}/middleware/auth.js", "type": "security", "language": "javascript"},
                {"path": f"{node_base}/middleware/errors.js", "type": "errors", "language": "javascript"},
            ]
        for t in tables:
            plan.append({"path": f"{node_base}/models/{t}.js", "type": "entity", "language": "javascript", "table": t})
            plan.append({"path": f"{node_base}/repositories/{t}-repo.js", "type": "repository", "language": "javascript", "table": t})
        for resource, eps in groups.items():
            plan.append({"path": f"{node_base}/routes/{resource}.js", "type": "controller", "language": "javascript", "resource": resource, "endpoints": eps})
            plan.append({"path": f"{node_base}/services/{resource}-service.js", "type": "service", "language": "javascript", "resource": resource, "endpoints": eps})
            plan.append({"path": f"{node_base}/schemas/{resource}-schemas.js", "type": "dto", "language": "javascript", "resource": resource, "endpoints": eps})
        plan.append({"path": (f"{base}/tests/{name}.test.js" if pattern == "microservices" else f"tests/{name}.test.js"),
                     "type": "test", "language": "javascript", "endpoints": endpoints})

    # iter-13.81.16 — final safety net: a library/no-API service should
    # never carry a `test` plan entry (we'd otherwise prompt the LLM with
    # `(no endpoints in scope)` and it would hallucinate a smoke test
    # for a controller that doesn't exist).
    if is_library and not endpoints:
        plan = [p for p in plan if p.get("type") != "test"]

    return plan


def _frontend_files_plan(svc: dict) -> List[dict]:
    """One page per identified resource group (driven by `pages` field on
    the frontend_service if available, else by the union of every backend
    service's endpoint groups). iter-13.27 — was a fixed Dashboard-only plan."""
    base = "frontend"
    pages = svc.get("pages") or []
    # Fall back to consumers' resource groups when pages list is empty.
    if not pages:
        consumers = svc.get("dependencies") or []
        # Caller doesn't have backend service detail here — emit a Dashboard +
        # one page per consumed backend service name; better than just one.
        pages = [_pascal(c) for c in consumers] or ["Dashboard"]
    plan: List[dict] = [
        {"path": f"{base}/Dockerfile", "type": "dockerfile", "language": "dockerfile", "ctype": "scaffold"},
        {"path": f"{base}/package.json", "type": "config", "language": "json", "ctype": "scaffold"},
        {"path": f"{base}/tsconfig.json", "type": "config", "language": "json", "ctype": "scaffold"},
        {"path": f"{base}/index.html", "type": "config", "language": "html", "ctype": "scaffold"},
        {"path": f"{base}/src/main.tsx", "type": "bootstrap", "language": "typescript", "ctype": "scaffold"},
        {"path": f"{base}/src/api/client.ts", "type": "service", "language": "typescript", "ctype": "api_client"},
        {"path": f"{base}/src/store/auth.ts", "type": "service", "language": "typescript", "ctype": "store"},
        {"path": f"{base}/src/routes.tsx", "type": "config", "language": "typescript", "ctype": "route_config"},
        {"path": f"{base}/src/components/RoleGate.tsx", "type": "service", "language": "typescript", "ctype": "role_guard"},
        {"path": f"{base}/README.md", "type": "docs", "language": "markdown", "ctype": "scaffold"},
    ]
    for p in pages:
        cls = _pascal(p)
        plan.append({"path": f"{base}/src/pages/{cls}.tsx",
                     "type": "route", "language": "typescript",
                     "ctype": "page", "page_name": cls})
    return plan


async def _gen_one_file(project_id: str, svc: dict, file_def: dict, model: str, run_doc: dict, pattern: str = "microservices") -> str:
    """Generate one file via LLM. Returns content (may include error inline).

    iter-13.27 — fan-out aware: when ``file_def`` carries ``endpoints`` /
    ``table`` / ``resource`` keys (set by ``_backend_files_plan``), the
    prompt is narrowed to that subset so the LLM has room to produce a
    complete file for every legacy API instead of truncating one giant
    Controller.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    arch_ctx = await stage_context_col.find_one({"project_id": project_id, "stage": "Architecture"}, {"_id": 0})
    lld_full = ((arch_ctx or {}).get("outputs", {}) or {}).get("lld_content", "")
    api_contracts_full = ((arch_ctx or {}).get("outputs", {}) or {}).get("api_contracts", "") or ""
    if not api_contracts_full:
        api_doc = await arch_documents.find_one({"project_id": project_id, "type": "api_contracts"}, {"_id": 0})
        api_contracts_full = (api_doc or {}).get("content", "") or ""

    # Per-service LLD slice (best effort)
    svc_lld = ""
    m = re.search(rf"# Service:[^\n]*`{re.escape(svc['name'])}`(.*?)(?=\n# Service:|\Z)", lld_full, re.DOTALL)
    if m:
        svc_lld = m.group(1)[:4000]
    else:
        svc_lld = lld_full[:3000]

    # iter-13.26 — graph + legacy + freshest SRS
    await _ensure_kb_graph(project_id)
    srs_sections = await _load_srs_sections(project_id)
    # iter-13.84 — Surface MORE SRS sections so workflow / approval
    # / business-rule / validation parity is enforceable. Was previously
    # only `detailed_use_cases` + `non_functional_requirements`; this
    # left `specific_requirements` (business rules) and
    # `validation_verification` (validation matrix) + `actors_use_case_inventory`
    # (workflow + approval-chain roles) on the floor.
    _srs_use_cases     = srs_sections.get("detailed_use_cases", "") or srs_sections.get("use_cases", "") or ""
    _srs_specific      = srs_sections.get("specific_requirements", "") or ""
    _srs_validation    = srs_sections.get("validation_verification", "") or ""
    _srs_actors        = srs_sections.get("actors_use_case_inventory", "") or ""
    srs_use_cases = (
        (_srs_use_cases[:2400]
         + ("\n\n## BUSINESS RULES & WORKFLOW (from SRS §Specific Requirements)\n"
            + _srs_specific[:2000] if _srs_specific else "")
         + ("\n\n## VALIDATION & APPROVAL RULES (from SRS §Validation/Verification)\n"
            + _srs_validation[:1800] if _srs_validation else "")
         + ("\n\n## ACTORS / ROLES / APPROVAL CHAIN (from SRS §Actors)\n"
            + _srs_actors[:1500] if _srs_actors else "")
        )[:7000]
    )
    srs_nfr = (srs_sections.get("non_functional_requirements", "") or "")[:1800]
    target_tech = proj.get("target_tech", "") or ""

    # iter-13.27 — narrow the API contract / endpoints / DDL slice to this
    # file's scope so each LLM call is doing ONE small job, not the whole
    # service in one shot.
    file_endpoints: list = file_def.get("endpoints") or svc.get("api_endpoints") or []
    file_table: str = file_def.get("table", "") or ""
    file_resource: str = file_def.get("resource", "") or ""

    if svc.get("frontend"):
        template = await _get_prompt(project_id, "codegen.frontend")
        _assert_prompt_fresh("codegen.frontend", template)
        component_type = file_def.get("ctype", "page")
        page_name = file_def.get("page_name", "")
        api_summary = api_contracts_full[:3500] or "(no API contracts artifact yet — generate it in Stage 3)"
        try:
            from kb.graph_config import is_graph_kb_enabled
            if await is_graph_kb_enabled(project_id):
                from kb.graph_retriever import subgraph_yaml_for_terms
                fe_seeds = ["route", "controller", "form", "page", "role", "permission"]
                if page_name:
                    fe_seeds.append(page_name.lower())
                fe_graph_yaml = await subgraph_yaml_for_terms(
                    project_id, fe_seeds, max_chars=2500, hops=2, max_nodes=100,
                )
            else:
                fe_graph_yaml = ""
        except Exception:
            fe_graph_yaml = ""
        graph_block = fe_graph_yaml or "(no graph slice — disabled or not built)"
        legacy_seeds = ["jsp", "html", "form", "page", "view", "controller", "role"]
        if page_name:
            legacy_seeds.append(page_name.lower())
        # iter-13.81.14 — Deep frontend scan: include component_type +
        # endpoints + roles so the seeds cover the actual UI surface,
        # not just generic "page/view" tokens. Boosts deep_legacy_context
        # hit rate ~3-5x on real legacy projects.
        if component_type:
            legacy_seeds.append(component_type.lower())
        for ep in (svc.get("api_endpoints") or [])[:10]:
            # endpoint shapes: "GET /foo/bar" → seed "foo", "bar"
            for tok in re.split(r"[\s/]+", ep):
                tok = tok.strip("{}").lower()
                if tok and len(tok) >= 4 and tok not in legacy_seeds:
                    legacy_seeds.append(tok)
        # iter-13.29 — deep retrieval pulls REAL legacy JSP/HTML/JS source
        # via graph-first + Mongo regex + entity-typed vector top-up.
        # iter-13.81.14 — budget 4500→12000 (Factory cap is 320KB; we
        # were leaving ~95% of the legacy KB on the floor).
        legacy_block = await _deep_legacy_context(
            project_id, [s for s in dict.fromkeys(legacy_seeds) if s],
            max_chars=12000,
        ) or "(no legacy UI source matched — generate idiomatic scaffold for the target framework)"
        frontend_framework = _frontend_framework_for(arch_ctx, "react")
        # iter-13.69 — fetch user-uploaded Figma/design-mockup files and
        # forward them as a `{theme_brief}` block so the generated UI
        # follows the user's chosen theme/tokens.
        try:
            from routes.kb import get_theme_brief_for_codegen
            theme_brief = await get_theme_brief_for_codegen(project_id, max_chars=6000)
        except Exception:
            theme_brief = ""
        if not theme_brief:
            theme_brief = (
                "(no user-tagged design/figma files uploaded — derive the visual "
                "theme from legacy UI evidence and target-stack defaults)"
            )
        _log_prompt_context(
            "codegen.frontend",
            target_tech=target_tech,
            graph_subgraph=fe_graph_yaml,
            legacy_evidence=legacy_block,
            api_summary=api_summary,
            relevant_use_cases=srs_use_cases or svc_lld,
        )
        sp = _safe_format(
            template,
            project_name=proj.get("name", ""),
            source_tech=proj.get("source_tech", ""),
            target_tech=target_tech,
            frontend_framework=frontend_framework,
            component_type=component_type,
            file_path=file_def["path"],
            api_summary=api_summary,
            graph_subgraph=graph_block,
            legacy_evidence=legacy_block,
            relevant_use_cases=(srs_use_cases or svc_lld)[:7000],
            srs_nfr=srs_nfr,
            component_instructions=get_frontend_instruction(component_type, frontend_framework),
            theme_brief=theme_brief,
            architecture_pattern=pattern,
        )
    else:
        template = await _get_prompt(project_id, "codegen.service")
        _assert_prompt_fresh("codegen.service", template)
        dm_ctx = await stage_context_col.find_one({"project_id": project_id, "stage": "DataModel"}, {"_id": 0})
        oltp_ddl = await _effective_oltp_ddl(
            project_id, ((dm_ctx or {}).get("outputs", {}) or {}).get("oltp_ddl", ""),
        )
        # When the file is scoped to a single table, only inject THAT table's DDL.
        ddl_target_tables = [file_table] if file_table else (svc.get("tables") or [])
        relevant_ddl = "\n".join(
            ln for ln in oltp_ddl.split("\n")
            if any(t in ln for t in ddl_target_tables)
        )[:3000] or oltp_ddl[:2000]

        try:
            from kb.graph_config import is_graph_kb_enabled
            if await is_graph_kb_enabled(project_id):
                from kb.graph_retriever import subgraph_yaml_for_terms
                seeds = list(ddl_target_tables) + [svc.get("name", "")]
                if file_resource:
                    seeds.append(file_resource)
                seeds = [s for s in seeds if s]
                graph_yaml = await subgraph_yaml_for_terms(
                    project_id, seeds, max_chars=2800, hops=2, max_nodes=110,
                )
            else:
                graph_yaml = ""
        except Exception:
            graph_yaml = ""
        graph_block = graph_yaml or "(no graph slice — disabled or not built)"

        legacy_seeds = list(ddl_target_tables) + [
            svc.get("name", ""), file_resource, "controller", "service",
            # iter-13.84 — workflow / approval / state-machine / validation
            # vocabulary. The user emphasised that workflow + approval flow
            # + 100% validation parity MUST be preserved. The deep-legacy
            # retriever uses these tokens to find state-machine code,
            # approval-chain handlers, validator classes etc. in the KB.
            "workflow", "approval", "approver", "state", "transition",
            "status", "validate", "validation", "validator", "rule",
            "policy", "permission", "authorize", "audit",
        ]
        # iter-13.81.14 — Broaden the deep-scan seed set so we actually
        # pull the OWNING legacy classes/methods, not just generic
        # "controller/service" tokens that match every file.
        legacy_seeds.extend(svc.get("module_names") or [])
        legacy_seeds.extend(svc.get("modules") or [])
        for rd in (svc.get("routes_detail") or [])[:8]:
            if rd.get("class"):
                legacy_seeds.append(rd["class"])
            path = (rd.get("path") or "")
            for tok in re.split(r"[\s/]+", path):
                tok = tok.strip("{}").lower()
                if tok and len(tok) >= 4:
                    legacy_seeds.append(tok)
        # iter-13.29 — deep retrieval (graph + source + vector hybrid)
        # iter-13.81.14 — budget 5000→14000 chars.
        # iter-14.20.9 — budget 14000→24000 chars. User's legacy corpora
        # (CGHS / ceots) are large; 14K was clipping the retrieved
        # PHP/JSP/SQL evidence before the LLM could reproduce the
        # branch-conditions + validation rules verbatim. 24K keeps us
        # well under the 320K Factory.ai cap.
        legacy_block = await _deep_legacy_context(
            project_id, [s for s in dict.fromkeys(legacy_seeds) if s],
            max_chars=24000,
        )
        if not legacy_block:
            # iter-14.20.9 — Do NOT tell the LLM to "generate an idiomatic
            # scaffold" when retrieval is empty. That instruction was a
            # self-fulfilling prophecy that produced generic CRUD instead
            # of a legacy migration. Instead surface a HARD provenance
            # note that keeps the LLM anchored to OLTP DDL + LLD + API
            # contract and BANS invention of business rules.
            legacy_block = (
                "(LEGACY RETRIEVAL EMPTY for this file — the KB did not "
                "surface a raw legacy source excerpt for the seed set. "
                "You MUST still migrate rather than invent:\n"
                "  • Ground every branch/predicate in OLTP DDL columns + "
                "constraints (verbatim names).\n"
                "  • Ground every endpoint contract in the API CONTRACT "
                "SLICE below (operationId, schemas, x-legacy-origin).\n"
                "  • Ground every validation / calculation / approval "
                "rule in the KB CONTEXT + SRS use cases + NFRs.\n"
                "  • DO NOT emit generic CrudX shapes. DO NOT invent new "
                "tables, columns, or business rules. DO NOT emit TODO/"
                "FIXME/PARITY-RISK unless the escape valve conditions "
                "are met.)"
            )

        # API contract slice — when generating a Controller for a specific
        # resource, take only the operations whose path matches that resource.
        api_contract_slice = _slice_api_contract_for_service(api_contracts_full, svc.get("name", ""))
        if file_resource:
            api_contract_slice = _slice_api_contract_for_resource(api_contract_slice, file_resource) or api_contract_slice
        api_contract_slice = api_contract_slice[:4500]

        # iter-13.115 — KB grounding (business ontology + deep legacy-analysis).
        # Compact, service-scoped block so the LLM names mappers/validators/
        # exceptions after the real business entities (not generic CrudFoo).
        try:
            kb_context_block = await _build_kb_context(project_id, svc, max_chars=3500)
        except Exception:  # noqa: BLE001
            kb_context_block = ""
        kb_context_block = kb_context_block or (
            "(no business ontology / legacy-analysis materialised yet — "
            "fall back to GRAPHIFY SUBGRAPH + LEGACY CODE EVIDENCE for entity names)"
        )

        _log_prompt_context(
            "codegen.service",
            file=file_def["path"],
            target_tech=target_tech,
            graph_subgraph=graph_yaml,
            legacy_evidence=legacy_block,
            service_ddl=relevant_ddl,
            api_contract=api_contract_slice or "(no API contracts artifact yet — generate it in Stage 3)",
            srs_use_cases=srs_use_cases,
            srs_nfr=srs_nfr,
            kb_context=kb_context_block,
            endpoints_in_scope=", ".join(file_endpoints)[:300],
        )
        sp = _safe_format(
            template,
            project_name=proj.get("name", ""),
            source_tech=proj.get("source_tech", ""),
            target_tech=target_tech,
            service_name=svc.get("name", ""),
            service_responsibility=svc.get("responsibility", ""),
            backend_lang=svc.get("backend_lang", "nodejs"),
            service_lld=svc_lld,
            service_ddl=relevant_ddl or "(no DDL excerpt)",
            graph_subgraph=graph_block,
            legacy_evidence=legacy_block,
            kb_context=kb_context_block,
            api_contract=api_contract_slice or "(no API contracts artifact yet — generate it in Stage 3)",
            srs_use_cases=srs_use_cases or "(no SRS use cases — freeze SRS first)",
            srs_nfr=srs_nfr or "(no NFRs — freeze SRS first)",
            service_endpoints="\n".join(file_endpoints) if file_endpoints else "(no endpoints in scope)",
            service_dependencies=", ".join(svc.get("dependencies", []) or []),
            file_path=file_def["path"],
            file_type=file_def["type"],
            file_type_instructions=get_file_instruction(file_def["type"], svc.get("backend_lang", "nodejs")),
            architecture_pattern=pattern,
        )

    # iter-13.27 — one retry on empty / error responses. Bumped max_tokens
    # 4500→8000 because the new STRICT_VERIFIED prompt easily emits 5-7k
    # tokens of code for a Controller + DTOs in one file. Truncation was
    # the #1 cause of "fully loss of data" reports.
    # iter-13.81.14 — Bumped to 4 attempts with PROGRESSIVE TEMPERATURE
    # + an explicit "never return empty" coda on attempts 2-4. Also
    # bumped max_tokens 8000→12000 because real Spring Boot Controllers
    # with full DTOs + validation easily run 9-11k tokens. The previous
    # 8k cap was silently truncating responses to the "empty" branch.
    user_msg = (
        f"Generate {file_def['path']} now as PRODUCTION-READY code — not a "
        f"scaffold, not a stub, not a TODO list. Cover EVERY endpoint listed "
        f"in API ENDPOINTS verbatim — no skips. Output the COMPLETE file.\n\n"
        f"Hard rules for THIS emission:\n"
        f"  1. Preconditions / Postconditions / Business Rules / Validation / "
        f"     Field Specification blocks MUST be filled in-line with the real "
        f"     content extracted from LEGACY CODE EVIDENCE, GRAPHIFY SUBGRAPH, "
        f"     OLTP DDL and SRS USE CASES above. They are NEVER allowed to "
        f"     contain `TODO`, `FIXME`, `XXX`, `backfill`, or a bare file-path "
        f"     reference. If you can name the legacy artifact, you can read it "
        f"     above — implement it.\n"
        f"  2. Every public method body MUST contain real, executable target-"
        f"     stack code that reproduces the legacy branch-for-branch — never "
        f"     `throw new UnsupportedOperationException`, never `pass  # TODO`, "
        f"     never `panic('unimpl')`, never `return null  // TODO`.\n"
        f"  3. If a single low-level detail is genuinely absent from ALL four "
        f"     evidence blocks AND cannot be derived from the OLTP DDL or SRS, "
        f"     emit ONE `// PARITY-RISK: <one-line reason>` comment ON THAT "
        f"     LINE ONLY and STILL implement a defensible default behaviour "
        f"     consistent with the surrounding logic. PARITY-RISK is permitted "
        f"     a maximum of ONCE per file and is FORBIDDEN inside Preconditions "
        f"     / Postconditions / Business Rules sections.\n"
        f"  4. NEVER blank-out the file. NEVER emit `// TODO: backfill from "
        f"     legacy KB` — that exact phrase is on the consumer's reject list."
    )
    # iter-13.59 — When the user enabled a utility (e.g. audit-logger),
    # append a one-line wiring hint to the bootstrap-file prompt so the
    # generated server file actually registers the deterministic
    # middleware/aspect we injected. Cheap (~40 tokens / bootstrap file).
    if file_def.get("type") == "bootstrap" and not svc.get("frontend"):
        try:
            utils = await arch_services.find(
                {"project_id": project_id, "kind": "utility"}, {"_id": 0},
            ).to_list(20)
            hints = [
                bootstrap_wiring_hint(u, svc) for u in utils
                if (u.get("utility_config") or {}).get("inject_into_all", True)
            ]
            hints = [h for h in hints if h]
            if hints:
                user_msg += "\n\nUtility wiring (REQUIRED — add to this bootstrap):\n" + "\n".join(f"- {h}" for h in hints)
        except Exception as _e:
            logger.debug("bootstrap wiring-hint lookup failed: %s", _e)
    # iter-13.71 — inject the 100% legacy-parity completeness contract
    sp = await _with_completeness_contract(project_id, sp)
    last_err = ""
    # iter-13.76 — pick tier hint per template kind so the Console can
    # route first-time codegen through Sonnet (medium) and a later
    # "regenerate" / gap-recovery through Opus (high). Falls back to the
    # service key for any new prompt slot not covered here.
    _codegen_agent_key = "codegen.frontend" if svc.get("frontend") else "codegen.service"
    # iter-13.81.15 — Prompt-size telemetry up front. Real cause of empty
    # responses is usually a prompt that exceeds the model's effective
    # input limit (Factory's session cap; Sonnet's per-turn cap; etc.).
    # Logging this BEFORE we send makes the failure root cause visible.
    _sp_chars = len(sp)
    _sp_tokens_est = _sp_chars // 4  # rough ASCII token estimate
    logger.info("codegen prompt size for %s: chars=%d est_tokens=%d (svc=%s lang=%s)",
                file_def["path"], _sp_chars, _sp_tokens_est, svc.get("name"),
                svc.get("backend_lang"))

    # iter-13.81.15 — Progressive ATTEMPT plan. If the LLM keeps refusing,
    # the most likely cause is "prompt too big". Each attempt trims more
    # context so by attempt 4 the prompt is tight enough that ANY provider
    # has room to respond.
    #   attempt 1 → full prompt, max_tokens=14000
    #   attempt 2 → trim legacy_block by 50%, bump temperature
    #   attempt 3 → also trim graph + api_contract, drop completeness contract
    #   attempt 4 → bare-bones prompt: just file_path + endpoints + ddl
    _ATTEMPT_TEMPS = [0.1, 0.25, 0.4, 0.5]
    _ATTEMPT_MAX_TOK = [14000, 12000, 10000, 8000]
    # iter-13.83 — LOCAL list (not module-level). Previously this was
    # implicitly module-level via closure mutation (`_ATTEMPT_CODA[attempt] = ...`)
    # which made one file's structural-feedback bleed into the NEXT file's
    # prompts. Rebuild fresh per call.
    _ATTEMPT_CODA = [
        "",
        "\n\nIMPORTANT: Output the COMPLETE file body now. Do not return empty.",
        "\n\nCRITICAL: Two prior attempts returned empty. Output the runnable file body — "
        "if the legacy evidence is sparse, generate the canonical idiomatic scaffold for "
        f"`{svc.get('backend_lang') or 'target'}` with stubbed methods for every endpoint. "
        "EMPTY response is forbidden.",
        "\n\nFINAL ATTEMPT: Output a minimal but COMPILABLE skeleton for "
        f"`{file_def['path']}` ({file_def.get('type','file')}) in the target "
        f"stack `{svc.get('backend_lang') or 'target'}`. Empty / short responses "
        "will be discarded and replaced with deterministic scaffolding.",
    ]

    def _trim_prompt(original_sp: str, retain_pct: float) -> str:
        """Trim oversized blocks from the system prompt for retries.
        Drops the trailing portion of each large ``═══`` section to keep
        the prompt structure intact while freeing response budget."""
        if retain_pct >= 1.0:
            return original_sp
        target_max = max(8000, int(len(original_sp) * retain_pct))
        if len(original_sp) <= target_max:
            return original_sp
        # Find biggest section and trim it
        lines = original_sp.split("\n")
        # Walk backwards, drop lines until we're under target_max
        while len("\n".join(lines)) > target_max and len(lines) > 50:
            lines.pop(len(lines) // 2)  # drop from middle (preserves header + tail rules)
        return "\n".join(lines)

    for attempt_idx, temp in enumerate(_ATTEMPT_TEMPS):
        attempt = attempt_idx + 1
        # iter-13.81.18 — Once we've seen a HARD billing/auth error on
        # ANY prior file in this run, every subsequent LLM call would
        # fail the same way → stop wasting attempts. Skip to scaffold
        # immediately. The run-level flag is set below in the except
        # branch when we see HTTP 402 / CreditError / 401.
        try:
            if isinstance(run_doc, dict) and run_doc.get("_credit_exhausted"):
                last_err = run_doc.get("_credit_err") or "credit/auth error on prior file"
                break
        except Exception:
            pass
        # Progressive prompt size: 100% → 75% → 55% → 40%
        retain = [1.0, 0.75, 0.55, 0.40][attempt_idx]
        attempt_sp = _trim_prompt(sp, retain) if attempt > 1 else sp
        if attempt > 1:
            logger.info("codegen retry %d for %s: trimmed prompt %d→%d chars (retain=%.0f%%)",
                        attempt, file_def["path"], _sp_chars, len(attempt_sp), retain * 100)
        try:
            r = await chat_completion(
                messages=[{"role": "system", "content": attempt_sp},
                          {"role": "user", "content": user_msg + _ATTEMPT_CODA[attempt_idx]}],
                model=model, temperature=temp,
                max_tokens=_ATTEMPT_MAX_TOK[attempt_idx],
                timeout=300.0,
                agent_key=_codegen_agent_key,
                project_id=project_id,
            )
            content = _strip_md_fence(r.get("content", "") or "")
            content_len = len(content)
            # iter-13.116 — visible per-file provider/model trace.
            # Without this the operator has no proof of which provider actually
            # answered for any given file, and the UI just shows the final
            # bytes. Now every successful or empty response logs:
            #   codegen.provider=ollama model=qwen2.5-coder:7b len=8421 file=...
            try:
                _r_model = (r.get("model") or "").strip() or model or "?"
                logger.info(
                    "codegen.provider model=%s len=%d file=%s",
                    _r_model, content_len, file_def["path"],
                )
            except Exception:  # noqa: BLE001
                pass
            # iter-13.81.15 — Half-done detection. A response that ends
            # mid-statement (no closing brace for a class, or last char
            # is not `;`, `}`, `)`, or `\n`) is almost certainly a
            # truncated max_tokens cutoff. Treat as failure and retry
            # with bigger budget / smaller prompt on the next pass.
            looks_truncated = (
                content_len > 200
                and not content.rstrip().endswith(("}", ";", ")", "]", "\n>", "end", "*/"))
                and not content.rstrip()[-1].isalnum()
                and content.count("{") > content.count("}") + 2
            )
            if content and content_len >= 80 and not looks_truncated:
                # iter-13.82 — Structural validation: reject files that
                # lack the mandatory framework markers (@RestController,
                # @Service, APIRouter, [ApiController], ...). Feed the
                # missing-token list back into the next attempt so the
                # LLM knows exactly what to fix.
                # iter-13.85 — pass ctype + framework so frontend files
                # get validated against FRONTEND_REQUIRED_TOKENS.
                _is_frontend = bool(svc.get("frontend"))
                struct_ok, missing = _validate_structure(
                    content,
                    file_def.get("type", ""),
                    svc.get("backend_lang", ""),
                    ctype=file_def.get("ctype", "") if _is_frontend else "",
                    framework=(_frontend_framework_for(arch_ctx, "react")
                               if _is_frontend else ""),
                )
                if not struct_ok:
                    last_err = (
                        f"structural validation failed (attempt={attempt}) — "
                        f"missing required markers: {', '.join(missing)}"
                    )
                    logger.warning(
                        "codegen STRUCTURAL FAIL for %s attempt=%d missing=%s — retrying",
                        file_def["path"], attempt, missing,
                    )
                    if attempt < len(_ATTEMPT_TEMPS):
                        # Append the missing-tokens feedback to the
                        # next attempt's coda so the LLM can fix it.
                        _ATTEMPT_CODA[attempt] = (
                            (_ATTEMPT_CODA[attempt] or "")
                            + "\n\nSTRUCTURAL VALIDATOR REJECTED THE PRIOR ATTEMPT. "
                            f"The file MUST contain ALL of: {', '.join(missing)}. "
                            "Re-emit the file using the MANDATORY SKELETON above "
                            "as the structural template; keep every annotation, "
                            "import, package declaration and class header."
                        )
                        continue
                    # iter-13.83 — Last attempt failed structural check.
                    # PREVIOUSLY we accepted the garbage with just a
                    # warning header — this is what reached the user's
                    # filesystem as `public class XxxController { ... }`
                    # with no @RestController, no @Service, no @Entity.
                    # NEW behaviour: fall back to the deterministic
                    # emergency scaffold which IS guaranteed structurally
                    # valid (it ships the required framework annotations
                    # and one stub per endpoint). The scaffold is a
                    # COMPILABLE starting point that Gap Recovery can
                    # then enrich with the real legacy logic — strictly
                    # better than a hand-typed-looking class missing every
                    # framework marker.
                    logger.warning(
                        "codegen FINAL-ATTEMPT structural fail for %s (missing=%s) "
                        "— discarding LLM output and using deterministic scaffold",
                        file_def["path"], missing,
                    )
                    try:
                        scaffold = _emergency_scaffold(file_def, svc, proj)
                    except Exception as _se:
                        scaffold = ""
                        logger.warning("emergency scaffold failed for %s: %s",
                                       file_def["path"], _se)
                    if scaffold and len(scaffold) >= 80:
                        return (
                            f"// LAMA: deterministic scaffold (LLM output failed structural validation "
                            f"— missing {', '.join(missing)}; replaced with DDL-grounded scaffold). "
                            f"Run Gap Recovery to enrich with legacy logic.\n" + scaffold
                        )
                    # Scaffold also failed → last resort: tag the broken
                    # LLM content so the user can at least see what we got.
                    content = (
                        f"// LAMA: structural validator flagged missing markers: "
                        f"{', '.join(missing)} — run Gap Recovery to remediate.\n"
                        + content
                    )
                # iter-13.118 — CONTENT-QUALITY GATE.
                # The structural validator only checks framework markers
                # (`@RestController`, `@Service`, …); it CAN'T tell that
                # every method body is a `// TODO: backfill from legacy
                # KB` carpet or a `throw new UnsupportedOperationException`
                # cascade. With a small local model (e.g. qwen3-coder:30b)
                # that's the common failure mode — and it sneaks past
                # because the shell IS technically valid.
                #
                # The deterministic Java scaffold (`_java_scaffold`) now
                # produces real CRUD wired through Service/Repository/
                # Mapper/Validator/Exception with DDL-derived fields, so
                # it's STRICTLY better than the LLM's TODO-carpet output.
                # If the LLM emits ≥ 2 TODO-carpet markers, swap.
                #
                # Scoped to Java only because the deterministic scaffold
                # has only been upgraded for Java so far (iter-13.118).
                # Other languages keep the LLM output untouched.
                if (svc.get("backend_lang") or "").lower() == "java":
                    # iter-14.35 — single-source-of-truth for lazy-stub
                    # markers; keeps prompt/runtime/scoring in sync.
                    from codegen.parity_loop import _TODO_CARPET as _todo_carpet_markers
                    _todo_hits = sum(content.count(m) for m in _todo_carpet_markers)
                    if _todo_hits >= 2 and (file_def.get("type") or "").lower() in {
                        "controller", "service", "entity", "repository",
                        "dto", "mapper", "validator", "exception", "test",
                    }:
                        logger.warning(
                            "codegen QUALITY-GATE: %s passed structural check "
                            "but contains %d TODO-carpet markers — replacing "
                            "with deterministic scaffold",
                            file_def["path"], _todo_hits,
                        )
                        try:
                            _scaf = _emergency_scaffold(file_def, svc, proj)
                        except Exception as _qe:  # noqa: BLE001
                            _scaf = ""
                            logger.warning(
                                "quality-gate scaffold gen failed for %s: %s",
                                file_def["path"], _qe,
                            )
                        if _scaf and len(_scaf) >= 80:
                            return (
                                f"// LAMA: quality-gate scaffold (LLM emitted "
                                f"{_todo_hits} TODO/Unsupported placeholders — "
                                f"replaced with DDL-grounded scaffold). Run Gap "
                                f"Recovery to enrich legacy-specific business "
                                f"rules.\n" + _scaf
                            )
                if attempt > 1:
                    logger.info("codegen retry succeeded for %s (attempt %d, temp=%.2f, len=%d)",
                                file_def["path"], attempt, temp, content_len)
                # iter-13.81.20 — Bump per-run success counter so a
                # later 402 (after exhausting all retries) doesn't
                # short-circuit the whole run; see _credit_exhausted
                # handling below.
                try:
                    if isinstance(run_doc, dict):
                        run_doc["_factory_successes"] = int(
                            run_doc.get("_factory_successes", 0) or 0
                        ) + 1
                except Exception:
                    pass
                return content
            if looks_truncated:
                last_err = (f"truncated response (len={content_len}, attempt={attempt}, "
                            f"unbalanced braces: {content.count('{')} open / {content.count('}')} close)")
                logger.warning("codegen TRUNCATED response for %s attempt=%d %s — retrying",
                               file_def["path"], attempt, last_err)
                # Keep the truncated content as a fallback — it's still
                # better than the scaffold if we run out of retries.
                if attempt == len(_ATTEMPT_TEMPS):
                    # iter-13.83 — On final attempt, validate structure
                    # even on truncated content. If the truncation also
                    # ate the framework annotations (@RestController etc.)
                    # the file is unusable — fall back to scaffold.
                    _is_frontend = bool(svc.get("frontend"))
                    struct_ok, missing = _validate_structure(
                        content,
                        file_def.get("type", ""),
                        svc.get("backend_lang", ""),
                        ctype=file_def.get("ctype", "") if _is_frontend else "",
                        framework=(_frontend_framework_for(arch_ctx, "react")
                                   if _is_frontend else ""),
                    )
                    if not struct_ok:
                        logger.warning(
                            "codegen TRUNCATED + structurally-invalid for %s (missing=%s) "
                            "— using deterministic scaffold instead",
                            file_def["path"], missing,
                        )
                        try:
                            scaffold = _emergency_scaffold(file_def, svc, proj)
                        except Exception:
                            scaffold = ""
                        if scaffold and len(scaffold) >= 80:
                            return (
                                f"// LAMA: deterministic scaffold (LLM returned "
                                f"truncated AND structurally-invalid content; "
                                f"missing {', '.join(missing)}). Run Gap Recovery.\n"
                                + scaffold
                            )
                    # Truncated but structurally valid — keep it (a
                    # 90%-complete @RestController is more useful than
                    # a 100%-complete scaffold).
                    logger.warning("codegen accepting truncated content for %s on final attempt",
                                   file_def["path"])
                    return content + "\n// LAMA: response truncated — run Gap Recovery to complete.\n"
            else:
                last_err = f"empty/short response (len={content_len}, attempt={attempt}, temp={temp})"
                logger.warning("codegen short response for %s attempt=%d %s — retrying",
                               file_def["path"], attempt, last_err)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} (attempt={attempt})"
            logger.warning("codegen LLM error for %s attempt=%d: %s", file_def["path"], attempt, e)
            # iter-13.81.18 — Detect HARD provider failures that will
            # affect every subsequent file too. Setting the run-level
            # flag lets the next file skip its 4 retries entirely so the
            # whole run aborts in seconds instead of minutes, AND the
            # actual error is surfaced (not the misleading "LLM returned
            # empty 4×" footer).
            _emsg = (str(e) or "").lower()
            _is_credit = (
                "402" in _emsg
                or "insufficient credit" in _emsg
                or "creditE".lower() in _emsg  # CreditError
                or "no credits" in _emsg
                or "billing" in _emsg
                or "payment required" in _emsg
                or "quota exceeded" in _emsg
            )
            _is_auth = (
                "401" in _emsg
                or "unauthorized" in _emsg
                or "invalid api key" in _emsg
                or "authentication failed" in _emsg
            )
            if (_is_credit or _is_auth) and isinstance(run_doc, dict):
                # iter-13.81.20 — Don't short-circuit when Factory is the
                # primary route AND has succeeded for at least one earlier
                # file in this same run. The current file's failure may
                # just be Factory's per-call flakiness (transient 5xx /
                # RemoteProtocolError), and the cascade to fabric→
                # env-OpenRouter that ended in 402 is misleading — the
                # NEXT file's Factory call may well succeed. Only short-
                # circuit when the run has produced zero Factory successes
                # so far (then it really is "no funded provider anywhere").
                _factory_succeeded_count = int(run_doc.get("_factory_successes", 0) or 0)
                if _factory_succeeded_count > 0:
                    logger.warning(
                        "codegen file %s hit credit/auth error on fallback chain "
                        "(%s) but Factory has succeeded %d times this run — NOT "
                        "short-circuiting; allowing retry on next file.",
                        file_def["path"], type(e).__name__, _factory_succeeded_count,
                    )
                else:
                    run_doc["_credit_exhausted"] = True
                    run_doc["_credit_err"] = (
                        f"ALL LLM PROVIDERS REFUSED (first failing file: {file_def['path']}). "
                        f"Reason: {type(e).__name__}: {str(e)[:240]}. "
                        f"Top up credits in Console → Models OR for the env-var "
                        f"OPENROUTER_API_KEY, then click Regenerate Backend."
                    )
                    logger.error("codegen ABORT — %s", run_doc["_credit_err"])
                break  # exit retry loop, fall through to scaffold
    # iter-13.81.14 — All 4 LLM attempts failed. Try a deterministic
    # scaffold instead of writing a useless "// LAMA generation failed"
    # stub. The scaffold at least gives the user a compilable starting
    # point that the gap-recovery job can then enrich with real legacy
    # logic. Returning the scaffold is FAR better than a 1-line failure
    # comment because:
    #   • The downstream syntax check in gap_recovery actually passes
    #   • The user can build & run the project to validate wiring
    #   • Re-running CodeGen on this file uses the prior scaffold as
    #     a starting hint instead of an empty file
    try:
        scaffold = _emergency_scaffold(file_def, svc, proj)
        if scaffold and len(scaffold) >= 80:
            logger.warning("codegen falling back to deterministic scaffold for %s "
                           "after %d empty/error attempts (last_err=%s)",
                           file_def["path"], len(_ATTEMPT_TEMPS), last_err)
            return (f"// LAMA: emergency scaffold (LLM returned empty {len(_ATTEMPT_TEMPS)}× — "
                    f"run Gap Recovery to enrich with legacy logic).\n"
                    f"// last_err: {last_err}\n" + scaffold)
    except Exception as _se:
        logger.warning("emergency scaffold generation failed for %s: %s",
                       file_def["path"], _se)
    return f"// LAMA generation failed for {file_def['path']}: {last_err}\n"


async def _save_codegen_file(run_id: str, project_id: str, svc: dict, file_def: dict, content: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    existing = await codegen_files.find_one({"project_id": project_id, "file_path": file_def["path"]}, {"_id": 0})
    # iter-13.27 — don't clobber a previously-good file with a failure
    # marker. If this run produced only a "// LAMA generation failed" stub
    # AND the prior version was real content, keep the prior content and
    # only record the failure in audit_log / errors.
    is_failure_stub = (
        content.startswith("// LAMA generation failed")
        or content.startswith("// LAMA generation error")
        or content.startswith("// LAMA: empty content")
    )
    # iter-13.81.14 — Emergency scaffold marker. A scaffold is *better*
    # than a failure stub (it's at least compilable) but it's still
    # worse than previously-good content. Preserve prior content when a
    # scaffold would overwrite it.
    is_emergency_scaffold = content.startswith("// LAMA: emergency scaffold")
    if existing and (is_failure_stub or is_emergency_scaffold) and len((existing.get("content") or "").strip()) > 80:
        logger.warning(
            "codegen kept previous version of %s (LLM returned %s: %r)",
            file_def["path"],
            "failure stub" if is_failure_stub else "emergency scaffold",
            content[:120],
        )
        await codegen_files.update_one(
            {"project_id": project_id, "file_path": file_def["path"]},
            {"$set": {"updated_at": now, "last_error": content.strip()[:300]}},
        )
        return existing
    doc = {
        "id": (existing or {}).get("id") or __import__("uuid").uuid4().hex,
        "project_id": project_id,
        "service_id": svc.get("id", ""),
        "service_name": svc.get("name", ""),
        "file_path": file_def["path"],
        "content": content,
        "language": file_def.get("language", "text"),
        "file_type": file_def.get("type", "other"),
        "version": ((existing or {}).get("version", 0)) + 1,
        "edited": False,
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
    }
    await codegen_files.update_one(
        {"project_id": project_id, "file_path": file_def["path"]},
        {"$set": doc}, upsert=True,
    )
    return doc


# -----------------------------------------------------------
# A. generate all (job)
# -----------------------------------------------------------
async def _run_codegen_job(
    jid: str,
    project_id: str,
    model: str,
    only_service: str = None,
    only_services: Optional[List[str]] = None,
):
    # iter-13.120 — Multi-select codegen. `only_services` is a list of
    # service names; `only_service` is the legacy scalar. Coalesce both
    # into a canonical `only_names` list. When it's empty/None we
    # generate every service (back-compat with "Generate All").
    try:
        _job_update(jid, status="running", step="Loading architecture context…", pct=1)
        # Called for its side effect: raises HTTP 400 when Architecture is
        # not frozen. The returned context is not needed here -- this is
        # the stage gate, not a data read. Do NOT drop the call.
        await require_stage_context(project_id, "Architecture", "CodeGen")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        # iter-14.20.9 — Bind the active Java group / project slug BEFORE
        # any scaffold is planned so every subsequent `package com.<x>...`
        # emission and every `src/main/java/com/<x>/...` file path is
        # rooted at `com.<projectSlug>` instead of the legacy `com.lama`.
        _set_active_java_group(proj or {})
        only_names: List[str] = []
        if only_services:
            only_names = [s for s in only_services if s]
        elif only_service:
            only_names = [only_service]
        services_q: Dict[str, Any] = {"project_id": project_id}
        if only_names:
            services_q["name"] = {"$in": only_names} if len(only_names) > 1 else only_names[0]
        services = await arch_services.find(services_q, {"_id": 0}).to_list(100)
        if not services:
            _job_finish(jid, "error", error="No services to generate.")
            return

        # iter-14.21 — ORPHAN CLEANUP safety net. Whenever "Generate All"
        # runs (no explicit service filter) drop any codegen_files whose
        # service_name is not in the current arch_services list. This
        # covers the case where the operator merged services in
        # Architecture (see architecture._cleanup_merged_service_artifacts)
        # BUT also handles older projects whose merge predates that
        # cleanup helper, plus any hand-deleted service. Audit-logged.
        if not only_names:
            try:
                current_names = {s.get("name") for s in services if s.get("name")}
                orphans = await codegen_files.distinct(
                    "service_name",
                    {"project_id": project_id,
                     "service_name": {"$nin": list(current_names)}},
                )
                if orphans:
                    r = await codegen_files.delete_many({
                        "project_id": project_id,
                        "service_name": {"$in": orphans},
                    })
                    logger.info(
                        "codegen: dropped %d orphan files for services %s",
                        getattr(r, "deleted_count", 0), orphans,
                    )
                    await audit_log.insert_one({
                        "action":     "codegen.orphan_cleanup",
                        "project_id": project_id,
                        "at":         datetime.now(timezone.utc).isoformat(),
                        "details":    {"orphan_services": orphans,
                                       "files_deleted": getattr(r, "deleted_count", 0)},
                    })
            except Exception as _oe:  # noqa: BLE001
                logger.warning("codegen orphan cleanup skipped: %s", _oe)
        # iter-13.120 — Frontend safety net. Full-generation runs (no
        # explicit service filter) that end up with zero frontend
        # services means Architecture never produced one. Log a
        # warning AND surface it via the job log so the user knows
        # the frontend was skipped instead of silently getting a
        # backend-only build.
        if not only_names:
            _has_frontend = any(bool(s.get("frontend")) for s in services)
            if not _has_frontend:
                logger.warning(
                    "codegen: no frontend service found in Architecture for project=%s — "
                    "frontend files will NOT be generated",
                    project_id,
                )
                _job_log(
                    jid,
                    "⚠ No frontend service found in Architecture — frontend files will "
                    "NOT be generated. Add a frontend service in Stage 3 and re-run.",
                )

        # iter-13.59 — Split utility services (kind="utility") from
        # business services. Utility services emit deterministic files
        # (utility_service_files_plan) AND inject extra files into every
        # business service (injection_files_for_service) when their
        # `utility_config.inject_into_all` flag is true.
        biz_services  = [s for s in services if s.get("kind") != "utility"]
        util_services = [s for s in services if s.get("kind") == "utility"]
        active_injectors = [
            u for u in util_services
            if (u.get("utility_config") or {}).get("inject_into_all", True)
        ]

        # Plan all files
        all_files: List[tuple] = []  # (svc, filedef)
        # iter-13.117 — SERVICE ENRICHMENT. When the Architecture stage
        # leaves a service with empty `tables` / `api_endpoints` (common
        # when arch.lld didn't decompose the OLTP DDL per-service), the
        # codegen plan would only emit the 6 bootstrap files and skip every
        # Controller/Service/Repository/Mapper/Validator/Exception. We
        # rescue the run by deriving tables from the OLTP DDL and endpoints
        # from the frozen API contracts, then partitioning them by service
        # name / module-name matches. The user gets a real per-resource
        # production codebase even when arch was sparse.
        try:
            await _enrich_services_from_kb(project_id, biz_services)
        except Exception as _ee:  # noqa: BLE001
            logger.warning("service enrichment skipped: %s", _ee)

        # iter-13.118.2 — POST-ENRICHMENT SAFETY NET (iter-14.30 BLOCKER FIX).
        # Symptom this fixes: user clicks ↻ on a single Java service and
        # gets only 6 files (Dockerfile, .env.example, README.md, pom.xml,
        # application.yml, Application.java) because the service's
        # `tables` AND `api_endpoints` ended up empty. `_backend_files_plan`
        # treats that as a library — skips Controller / Service / DTO /
        # Mapper / Validator / Exception entirely → user sees "nothing
        # happened" despite a clean parity score.
        #
        # BLOCKER FIX (iter-14.30): NEVER synthesize arbitrary table names.
        # Instead, pull ALL tables from OLTP DDL and ALL endpoints from API
        # contracts. This ensures entity/repository names match legacy exactly.
        #
        # First, collect ALL tables from OLTP DDL for fallback
        _all_legacy_tables: List[str] = []
        _all_legacy_endpoints: List[str] = []
        try:
            _dm = await stage_context_col.find_one(
                {"project_id": project_id, "stage": "DataModel"}, {"_id": 0, "outputs": 1},
            )
            _oltp_ddl = await _effective_oltp_ddl(
                project_id, ((_dm or {}).get("outputs", {}) or {}).get("oltp_ddl", ""),
            )
            for _m in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?([A-Za-z_][A-Za-z0-9_]*)[\"`]?",
                _oltp_ddl, re.IGNORECASE,
            ):
                _t = _m.group(1)
                if _t and _t not in _all_legacy_tables:
                    _all_legacy_tables.append(_t)
            # Also get ALL API endpoints from architecture
            _arch_doc = await arch_documents.find_one(
                {"project_id": project_id, "kind": "api_contracts"},
                {"_id": 0, "content": 1},
            )
            if _arch_doc and _arch_doc.get("content"):
                for _ep_m in re.finditer(
                    r"(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-/{}]+)",
                    _arch_doc["content"]
                ):
                    _ep = f"{_ep_m.group(1)} {_ep_m.group(2)}"
                    if _ep not in _all_legacy_endpoints:
                        _all_legacy_endpoints.append(_ep)
            
            # iter-14.30 — FALLBACK: Pull ALL ROUTE entities from KB when API
            # contracts are empty. Includes Spring MVC + Struts + JSP + all frameworks.
            if not _all_legacy_endpoints:
                _route_ents = await kb_entities.find(
                    {"project_id": project_id, "type": "ROUTE"},
                    {"_id": 0, "name": 1, "verb": 1},
                ).to_list(500)
                _seen_eps: set = set()
                for _r in _route_ents:
                    _path = (_r.get("name") or "").strip()
                    if not _path:
                        continue
                    if not _path.startswith("/"):
                        _path = "/" + _path
                    _verb = (_r.get("verb") or "ANY").upper()
                    if _verb == "ANY":
                        # Expand to common verbs
                        for _v in ("GET", "POST", "PUT", "DELETE"):
                            _ep = f"{_v} {_path}"
                            if _ep not in _seen_eps:
                                _seen_eps.add(_ep)
                                _all_legacy_endpoints.append(_ep)
                    else:
                        _ep = f"{_verb} {_path}"
                        if _ep not in _seen_eps:
                            _seen_eps.add(_ep)
                            _all_legacy_endpoints.append(_ep)
                if _all_legacy_endpoints:
                    logger.info(
                        "codegen safety-net: loaded %d endpoints from KB ROUTE entities "
                        "(Spring + Struts + JSP + all frameworks)",
                        len(_all_legacy_endpoints),
                    )
            
            logger.info(
                "codegen safety-net: loaded %d legacy tables, %d legacy endpoints from upstream stages",
                len(_all_legacy_tables), len(_all_legacy_endpoints),
            )
        except Exception as _de:
            logger.warning("codegen safety-net: could not load legacy tables/endpoints: %s", _de)

        for _svc in biz_services:
            if _svc.get("frontend"):
                continue
            _nm = _svc.get("name") or "service"
            # Filter out blank / whitespace-only entries that were
            # persisted by an earlier buggy run.
            _tabs = [t for t in (_svc.get("tables") or []) if t and str(t).strip()]
            _eps  = [e for e in (_svc.get("api_endpoints") or []) if e and str(e).strip()]
            
            # BLOCKER FIX: Use ALL legacy tables instead of synthesizing
            if not _tabs:
                if _all_legacy_tables:
                    _tabs = list(_all_legacy_tables)
                    logger.info(
                        "codegen safety-net: assigned ALL %d legacy tables to svc=%s "
                        "(strict legacy parity — entity/repo names will match legacy exactly)",
                        len(_tabs), _nm,
                    )
                else:
                    # Absolute last resort: try to get from kb_entities
                    try:
                        _kb_tables = await kb_entities.find(
                            {"project_id": project_id, "type": "TABLE"},
                            {"_id": 0, "name": 1},
                        ).to_list(200)
                        _tabs = [t["name"] for t in _kb_tables if t.get("name")]
                        if _tabs:
                            logger.info(
                                "codegen safety-net: assigned %d KB tables to svc=%s",
                                len(_tabs), _nm,
                            )
                    except Exception:
                        pass
                    if not _tabs:
                        logger.error(
                            "codegen BLOCKER: svc=%s has NO tables from OLTP DDL, API contracts, or KB. "
                            "Stage 2 (DataModel) must be completed with valid OLTP DDL before CodeGen.",
                            _nm,
                        )
            
            # BLOCKER FIX: Use ALL legacy endpoints instead of synthesizing CRUD
            if not _eps:
                if _all_legacy_endpoints:
                    _eps = list(_all_legacy_endpoints)
                    logger.info(
                        "codegen safety-net: assigned ALL %d legacy endpoints to svc=%s "
                        "(strict legacy parity — no API will be missed)",
                        len(_eps), _nm,
                    )
                else:
                    # Generate CRUD only as absolute last resort, using REAL table names
                    for _t in _tabs[:10]:  # Cap at 10 tables to avoid explosion
                        _res = _t.lower().rstrip("s") + "s"
                        _crud = [
                            f"GET /api/v1/{_res}",
                            f"GET /api/v1/{_res}/{{id}}",
                            f"POST /api/v1/{_res}",
                            f"PUT /api/v1/{_res}/{{id}}",
                            f"DELETE /api/v1/{_res}/{{id}}",
                        ]
                        _eps.extend(_crud)
                    if _eps:
                        logger.warning(
                            "codegen safety-net: synthesised %d CRUD endpoints from %d real table names for svc=%s "
                            "(Stage 3 API contracts were empty)",
                            len(_eps), min(10, len(_tabs)), _nm,
                        )
            
            _svc["tables"] = _tabs
            _svc["api_endpoints"] = _eps

        # iter-13.121 — Resolve the ARCHITECTURE PATTERN once. Every biz
        # service shares the same pattern (persisted from
        # `parsed.recommended_pattern` in `routes/architecture.py`); the
        # merged-service branch carries it through as well. We pick the
        # first non-empty pattern seen and validate it against our known
        # values, falling back to "microservices" for legacy / unknown
        # docs. This value drives BOTH the per-service file plan AND the
        # root-level bootstrap files.
        proj_pattern = ""
        for _s in (biz_services + util_services):
            if _s.get("pattern"):
                proj_pattern = str(_s["pattern"]).strip().lower()
                break
        if proj_pattern not in ("microservices", "modular_monolith", "monolith"):
            proj_pattern = "microservices"
        logger.info("codegen pattern=%s services=%d",
                    proj_pattern, len(biz_services))
        _job_log(jid, f"Architecture pattern: {proj_pattern}")

        # ═══════════════════════════════════════════════════════════════════
        # BLOCKER FIX (iter-14.30): MERGED APPLICATION AGGREGATION
        # For monolith/modular_monolith patterns, ALL services must have
        # access to ALL tables and ALL endpoints. Otherwise, APIs get missed
        # in the legacy→modernize mapping and entities are incomplete.
        # ═══════════════════════════════════════════════════════════════════
        if proj_pattern in ("monolith", "modular_monolith"):
            # Collect ALL tables and endpoints across all services
            _all_merged_tables: set = set()
            _all_merged_endpoints: set = set()
            for _svc in biz_services:
                if _svc.get("frontend"):
                    continue
                _all_merged_tables.update(_svc.get("tables") or [])
                _all_merged_endpoints.update(_svc.get("api_endpoints") or [])
            
            # Also add any from the fallback sources
            _all_merged_tables.update(_all_legacy_tables)
            _all_merged_endpoints.update(_all_legacy_endpoints)
            
            _merged_tables_list = sorted([t for t in _all_merged_tables if t and str(t).strip()])
            _merged_endpoints_list = sorted([e for e in _all_merged_endpoints if e and str(e).strip()])
            
            logger.info(
                "codegen MERGED APPLICATION: pattern=%s aggregating %d tables, %d endpoints across all services",
                proj_pattern, len(_merged_tables_list), len(_merged_endpoints_list),
            )
            _job_log(
                jid,
                f"MERGED APPLICATION: Aggregating {len(_merged_tables_list)} tables, "
                f"{len(_merged_endpoints_list)} endpoints (strict legacy parity)"
            )
            
            # Assign ALL tables and endpoints to EVERY backend service
            # This ensures no API or entity is missed in merged mode
            for _svc in biz_services:
                if _svc.get("frontend"):
                    continue
                _svc["tables"] = _merged_tables_list
                _svc["api_endpoints"] = _merged_endpoints_list
                _svc["_merged_mode"] = True  # Flag for downstream processing

        for svc in biz_services:
            plan = (_frontend_files_plan(svc) if svc.get("frontend")
                    else _backend_files_plan(svc, proj_pattern))
            # Per-utility injection (deterministic files materialised
            # inside this business service to wire the cross-cutting
            # concern in — Aspect for Java, middleware for Python/Node/
            # Go, action filter for .NET).
            if not svc.get("frontend"):
                for u in active_injectors:
                    plan += injection_files_for_service(svc, u)
            for f in plan:
                all_files.append((svc, f))

        # iter-13.59 — Utility services' own files (README + ELK pipeline
        # assets). These never go through the LLM — render_file() is
        # called inline in gen_and_save when file_def["deterministic"].
        for util in util_services:
            for f in utility_service_files_plan(util):
                all_files.append((util, f))

        # iter-13.121 — Root-level files depend on the architecture
        # pattern. In microservices mode we ship the classic 3 root
        # files (docker-compose spins up every service; ci.yml matrix-
        # builds them; README documents topology). In monolith /
        # modular_monolith mode there is ONE deployable, so we ship the
        # single-project bootstrap at the root (per dominant backend
        # lang) instead of duplicating it under each service folder.
        def _dominant_backend_lang(_services):
            from collections import Counter
            _langs = [s.get("backend_lang", "").lower()
                      for s in _services if s.get("backend_lang")]
            return (Counter(_langs).most_common(1) or [("nodejs", 0)])[0][0]

        _root_svc = {
            "name": "_root", "frontend": False, "backend_lang": "any",
            "responsibility": "project root", "tables": [],
            "api_endpoints": [], "dependencies": [], "id": "",
        }
        if proj_pattern == "microservices":
            # iter-14.20.9 — For microservices we ALSO ship a merged
            # root deployable so every service is a module of the SAME
            # application (e.g. `com.ceots` parent pom, or a Yarn/npm
            # workspace root, or a Poetry uv workspace) instead of
            # looking like N unrelated projects.
            _dlang_ms = _dominant_backend_lang(biz_services) or "nodejs"
            root_files = [
                (_root_svc, {"path": "docker-compose.yml", "type": "compose",
                             "language": "yaml", "deterministic": True,
                             "_root_kind": "compose"}),
                (_root_svc, {"path": ".github/workflows/ci.yml", "type": "ci",
                             "language": "yaml"}),
                (_root_svc, {"path": "README.md", "type": "docs",
                             "language": "markdown", "deterministic": True,
                             "_root_kind": "readme"}),
            ]
            if _dlang_ms == "java":
                root_files.append((_root_svc, {
                    "path": "pom.xml", "type": "config", "language": "xml",
                    "deterministic": True, "_root_kind": "parent-pom",
                }))
            elif _dlang_ms == "python":
                root_files.append((_root_svc, {
                    "path": "pyproject.toml", "type": "config", "language": "toml",
                    "deterministic": True, "_root_kind": "py-workspace",
                }))
            elif _dlang_ms in ("nodejs", "node", "typescript", "javascript"):
                root_files.append((_root_svc, {
                    "path": "package.json", "type": "config", "language": "json",
                    "deterministic": True, "_root_kind": "node-workspace",
                }))
        else:
            _dlang = _dominant_backend_lang(biz_services) or "nodejs"
            _common = [
                (_root_svc, {"path": "README.md", "type": "docs", "language": "markdown"}),
                (_root_svc, {"path": "docker-compose.yml", "type": "compose", "language": "yaml"}),
                (_root_svc, {"path": ".github/workflows/ci.yml", "type": "ci", "language": "yaml"}),
                (_root_svc, {"path": "Dockerfile", "type": "dockerfile", "language": "dockerfile"}),
            ]
            if _dlang == "java":
                _root_java_group_path = _ACTIVE_JAVA_GROUP_PATH  # iter-14.20.9
                root_files = _common + [
                    (_root_svc, {"path": "pom.xml", "type": "config", "language": "xml"}),
                    (_root_svc, {"path": "src/main/resources/application.yml",
                                 "type": "config", "language": "yaml"}),
                    (_root_svc, {"path": f"src/main/java/{_root_java_group_path}/Application.java",
                                 "type": "bootstrap", "language": "java"}),
                ]
            elif _dlang == "python":
                root_files = _common + [
                    (_root_svc, {"path": "requirements.txt", "type": "config", "language": "text"}),
                    (_root_svc, {"path": "pyproject.toml", "type": "config", "language": "toml"}),
                    (_root_svc, {"path": "main.py", "type": "bootstrap", "language": "python"}),
                ]
            elif _dlang == "dotnet":
                _pname = _pascal((proj.get("name") or "app").replace(" ", ""))
                root_files = _common + [
                    (_root_svc, {"path": f"{_pname}.csproj", "type": "config", "language": "xml"}),
                    (_root_svc, {"path": "Program.cs", "type": "bootstrap", "language": "csharp"}),
                    (_root_svc, {"path": "appsettings.json", "type": "config", "language": "json"}),
                ]
            elif _dlang == "go":
                root_files = _common + [
                    (_root_svc, {"path": "go.mod", "type": "config", "language": "text"}),
                    (_root_svc, {"path": "main.go", "type": "bootstrap", "language": "go"}),
                ]
            else:  # nodejs / fallback
                root_files = _common + [
                    (_root_svc, {"path": "package.json", "type": "config", "language": "json"}),
                    (_root_svc, {"path": "src/index.ts", "type": "bootstrap", "language": "typescript"}),
                ]
        if not only_names:
            all_files.extend(root_files)

        # iter-13.118 — Parse OLTP DDL once per run, stamp each Java code
        # file_def with its table's parsed columns so `_java_scaffold`
        # (and any future emergency-scaffold path) can emit real entity
        # fields / DTO records / mapper field copies / validator NOT-NULL
        # checks instead of the iter-13 `// TODO: backfill columns from
        # OLTP DDL` placeholders. Files belonging to a resource without a
        # named table fall back to the first table the owning service
        # declared (so Controllers/Services/DTOs still get reasonable
        # column lists for typed CRUD).
        try:
            _dm_ctx = await stage_context_col.find_one(
                {"project_id": project_id, "stage": "DataModel"}, {"_id": 0},
            )
            _oltp_ddl = await _effective_oltp_ddl(
                project_id,
                ((_dm_ctx or {}).get("outputs", {}) or {}).get("oltp_ddl", "")
                or "",
            )
            _ddl_by_table = _parse_ddl_columns(_oltp_ddl)
            _ddl_table_count = len(_ddl_by_table)
            _ddl_files_stamped = 0
            _ddl_typed_files = {"controller", "service", "dto", "mapper",
                                 "validator", "exception", "entity",
                                 "repository", "test"}
            for _svc, _fd in all_files:
                if (_fd.get("language") or "").lower() != "java":
                    continue
                _explicit_tbl = (_fd.get("table") or "").lower()
                if _explicit_tbl and _explicit_tbl in _ddl_by_table:
                    _fd["_ddl_columns"] = _ddl_by_table[_explicit_tbl]
                    _fd["_ddl_table"] = _explicit_tbl
                    _ddl_files_stamped += 1
                elif (_fd.get("type") or "").lower() in _ddl_typed_files:
                    # Fall back to first service table that has DDL columns
                    # so per-resource files still get a sensible field list.
                    for _t in (_svc.get("tables") or []):
                        _tl = str(_t).lower()
                        if _tl in _ddl_by_table:
                            _fd["_ddl_columns"] = _ddl_by_table[_tl]
                            _fd["_ddl_table"] = _tl
                            _ddl_files_stamped += 1
                            break
            logger.info(
                "codegen DDL-column stamping: tables_parsed=%d files_stamped=%d",
                _ddl_table_count, _ddl_files_stamped,
            )
        except Exception as _ee:  # noqa: BLE001
            logger.warning("DDL-column stamping skipped: %s", _ee)

        # iter-13.118.1 — PRE-FLIGHT EVIDENCE AUDIT.
        # Before we start generating, score each service on whether the
        # upstream stages produced enough evidence for the LLM to actually
        # reproduce legacy logic. If a service has thin evidence the
        # generated output WILL be skeletal regardless of prompt strength
        # — surfacing it up-front lets the operator re-run Stage 1/2/3
        # instead of blaming codegen.
        #
        # Scored per service:
        #   • ddl_tables_owned       : count of svc.tables present in OLTP DDL
        #   • api_endpoint_count     : count of svc.api_endpoints (frozen)
        #   • module_token_count     : modules + module_names + routes_detail
        #   • lld_slice_present      : did arch.lld leave a per-svc slice?
        #   • srs_use_case_chars     : applicable use-case text length
        #   • legacy_corpus_match    : count of legacy kb_entities matching
        #                              this svc's tokens (CLASS / ROUTE / etc.)
        # Threshold (any TWO red flags → service marked thin_evidence):
        #   ddl_tables_owned == 0 AND service is not a frontend
        #   api_endpoint_count == 0 AND service is not a library/utility
        #   legacy_corpus_match < 3
        #   srs_use_case_chars < 400
        evidence_audit: List[Dict[str, Any]] = []
        try:
            _srs_sections_all = await _load_srs_sections(project_id)
            _srs_specific  = _srs_sections_all.get("specific_requirements", "") or ""
            _srs_uc_full   = (
                _srs_sections_all.get("detailed_use_cases", "")
                or _srs_sections_all.get("use_cases", "")
                or ""
            )
            for _svc in biz_services:
                _name_lc = (_svc.get("name") or "").lower()
                _tokens = set([_name_lc])
                _tokens.update((t or "").lower() for t in (_svc.get("tables") or []))
                _tokens.update((m or "").lower() for m in (_svc.get("module_names") or []) + (_svc.get("modules") or []))
                for _rd in (_svc.get("routes_detail") or []):
                    if _rd.get("class"):
                        _tokens.add(_rd["class"].lower())
                _tokens = {t for t in _tokens if t and len(t) >= 3}

                _ddl_owned = sum(
                    1 for _t in (_svc.get("tables") or [])
                    if str(_t).lower() in _ddl_by_table
                )
                _ep_count = len(_svc.get("api_endpoints") or [])
                _module_count = len(_svc.get("module_names") or []) + \
                                len(_svc.get("modules") or [])

                # LLD slice check (cheap — substring on the full LLD)
                _arch = await stage_context_col.find_one(
                    {"project_id": project_id, "stage": "Architecture"}, {"_id": 0},
                )
                _lld_full = ((_arch or {}).get("outputs", {}) or {}).get("lld_content", "") or ""
                _lld_slice = bool(re.search(
                    rf"# Service:[^\n]*`{re.escape(_svc.get('name','')) }`",
                    _lld_full,
                ))

                # SRS UC text relevant to this service (token-overlap)
                _uc_chars = 0
                if _srs_uc_full and _tokens:
                    _uc_lower = _srs_uc_full.lower()
                    for _tok in _tokens:
                        if _tok in _uc_lower:
                            # Count contiguous lines around the first match
                            _idx = _uc_lower.find(_tok)
                            _uc_chars += min(2000, len(_srs_uc_full) - _idx)
                            break
                _br_chars = 0
                if _srs_specific and _tokens:
                    _sp_lower = _srs_specific.lower()
                    for _tok in _tokens:
                        if _tok in _sp_lower:
                            _idx = _sp_lower.find(_tok)
                            _br_chars += min(1500, len(_srs_specific) - _idx)
                            break

                # Legacy corpus match — quick count via kb_entities
                _legacy_hits = 0
                try:
                    from db import kb_entities as _kbe
                    _cur = _kbe.find(
                        {"project_id": project_id}, {"_id": 0, "name": 1, "type": 1},
                    ).limit(2000)
                    async for _ent in _cur:
                        _n = (_ent.get("name") or "").lower()
                        if not _n:
                            continue
                        if any(tok in _n or _n in tok for tok in _tokens):
                            _legacy_hits += 1
                            if _legacy_hits > 25:
                                break
                except Exception:
                    pass

                # Thin-evidence verdict
                _is_frontend = bool(_svc.get("frontend"))
                _is_library = (
                    "lib" in (_svc.get("kind") or "").lower()
                    or "shared" in (_svc.get("kind") or "").lower()
                    or "utility" in (_svc.get("kind") or "").lower()
                )
                _red_flags: List[str] = []
                if _ddl_owned == 0 and not _is_frontend:
                    _red_flags.append("no_ddl_tables")
                if _ep_count == 0 and not _is_library and not _is_frontend:
                    _red_flags.append("no_api_endpoints")
                if _legacy_hits < 3:
                    _red_flags.append(f"thin_legacy_corpus({_legacy_hits})")
                if _uc_chars < 400:
                    _red_flags.append(f"thin_srs_use_cases({_uc_chars}c)")

                _verdict = "thin_evidence" if len(_red_flags) >= 2 else "ok"
                _row = {
                    "service": _svc.get("name") or "",
                    "frontend": _is_frontend,
                    "verdict": _verdict,
                    "red_flags": _red_flags,
                    "ddl_tables_owned": _ddl_owned,
                    "api_endpoint_count": _ep_count,
                    "module_count": _module_count,
                    "lld_slice_present": _lld_slice,
                    "srs_use_case_chars": _uc_chars,
                    "srs_business_rule_chars": _br_chars,
                    "legacy_corpus_match": _legacy_hits,
                }
                evidence_audit.append(_row)
                if _verdict == "thin_evidence":
                    logger.warning(
                        "codegen EVIDENCE-AUDIT thin_evidence svc=%s flags=%s "
                        "(ddl=%d eps=%d modules=%d lld=%s srs_uc=%dc srs_br=%dc legacy=%d) "
                        "→ generated output WILL be skeletal; re-run Stage 1/2/3 with deeper KB",
                        _svc.get("name"), _red_flags, _ddl_owned, _ep_count,
                        _module_count, _lld_slice, _uc_chars, _br_chars, _legacy_hits,
                    )
                else:
                    logger.info(
                        "codegen EVIDENCE-AUDIT ok svc=%s ddl=%d eps=%d legacy=%d srs_uc=%dc srs_br=%dc",
                        _svc.get("name"), _ddl_owned, _ep_count,
                        _legacy_hits, _uc_chars, _br_chars,
                    )
        except Exception as _ae:  # noqa: BLE001
            logger.warning("evidence audit skipped: %s", _ae)

        # iter-13.118.2 — Stamp planned_file_count onto evidence_audit so
        # the UI can surface "only 6 files generated" as a red flag. A
        # business service with < 12 planned files almost always means
        # tables/endpoints didn't materialise → either the safety net
        # above kicked in (logged WARNING) or this is genuinely a library
        # service. Either way the user should see the count.
        try:
            _planned_by_svc: Dict[str, int] = {}
            for _s, _fd in all_files:
                _planned_by_svc[_s.get("name") or "_root"] = \
                    _planned_by_svc.get(_s.get("name") or "_root", 0) + 1
            for _row in evidence_audit:
                _row["planned_file_count"] = _planned_by_svc.get(_row["service"], 0)
                # Promote to thin_evidence if file count is suspiciously low
                # for a non-frontend service (means the file plan collapsed
                # to bootstrap-only).
                if (not _row.get("frontend")
                        and _row["planned_file_count"] < 12
                        and _row["verdict"] == "ok"):
                    _row["verdict"] = "thin_evidence"
                    _row["red_flags"].append(
                        f"only_{_row['planned_file_count']}_files_planned"
                    )
                    logger.warning(
                        "codegen EVIDENCE-AUDIT promoted svc=%s to thin_evidence "
                        "— only %d files planned (no Controller/Service/Entity emitted)",
                        _row["service"], _row["planned_file_count"],
                    )
        except Exception as _pe:  # noqa: BLE001
            logger.warning("planned_file_count stamping skipped: %s", _pe)

        run = {
            "id": jid, "project_id": project_id, "status": "running",
            "services_total": len(services), "services_done": 0,
            "files_total": len(all_files), "files_done": 0,
            "errors": [], "github_commit": "",
            "started_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
            # iter-13.118.1 — evidence audit visible to Console + UI
            "evidence_audit": evidence_audit,
        }
        await codegen_runs.update_one({"id": jid}, {"$set": run}, upsert=True)

        # Clear old files for affected services
        if only_names:
            await codegen_files.delete_many(
                {"project_id": project_id, "service_name": {"$in": only_names}}
            )

        files_total = len(all_files)
        # iter-13.27 — compute legacy-API coverage so operators can see at
        # a glance whether every endpoint in the service map got a file.
        all_endpoints: set = set()
        covered_endpoints: set = set()
        for svc in services:
            for ep in (svc.get("api_endpoints") or []):
                all_endpoints.add(f"{svc['name']}::{ep}")
        for svc, fd in all_files:
            for ep in (fd.get("endpoints") or []):
                covered_endpoints.add(f"{svc['name']}::{ep}")
        if all_endpoints:
            uncovered = sorted(all_endpoints - covered_endpoints)
            logger.info(
                "codegen plan: services=%d files=%d endpoints_total=%d endpoints_covered=%d uncovered=%s",
                len(services), files_total, len(all_endpoints), len(covered_endpoints),
                uncovered[:10] + (["..."] if len(uncovered) > 10 else []),
            )
        completed = {"n": 0, "applied": 0, "failed": 0}
        # iter-13.81.14 — concurrency 3→2. With the bigger prompts shipped
        # this iteration (legacy_block 5k→14k, max_tokens 8k→12k, retries
        # 2→4 with expanding context), Factory.ai sessions started returning
        # empty 200s under parallel pressure. 2 keeps every call within
        # Factory's burst budget while doubling completion success rate
        # empirically (TJHS 132-file run: 11→127 successful files).
        sem = asyncio.Semaphore(2)

        async def gen_and_save(svc, file_def):
            async with sem:
                # iter-13.54 — cooperative pause/stop checkpoint
                await _await_job_control(jid)
                _job_log(jid, file_def["path"])
                if file_def.get("deterministic"):
                    # iter-14.20.9 — Root-level artifacts (parent pom /
                    # workspace root / docker-compose / README) rendered
                    # deterministically so the merged multi-service
                    # bundle is ONE application, not N unrelated apps.
                    if file_def.get("_root_kind"):
                        _dlang_root = _dominant_backend_lang(biz_services) or "nodejs"
                        _det = _build_deterministic_root_content(
                            file_def, proj, biz_services, proj_pattern, _dlang_root,
                        )
                        if _det:
                            content = _det
                            await _save_codegen_file(jid, project_id, svc, file_def, content)
                            completed["n"] += 1
                            pct = 2 + int((completed["n"] / max(1, files_total)) * 95)
                            _job_update(jid, step=f"[{completed['n']}/{files_total}] {file_def['path']}", pct=pct)
                            if completed["n"] % 5 == 0 or completed["n"] == files_total:
                                await codegen_runs.update_one({"id": jid}, {"$set": {"files_done": completed["n"]}})
                            return
                    # iter-13.59 — Utility-rendered file. Resolve the
                    # owning utility service (either `svc` itself when
                    # this is a utility-own file, or look it up by
                    # utility_type for an injected file).
                    if svc.get("kind") == "utility":
                        owner = svc
                        target = None
                    else:
                        owner = next(
                            (u for u in util_services
                             if u.get("utility_type") == file_def.get("utility_type")),
                            svc,
                        )
                        target = svc
                    content = render_utility_file(file_def, proj, owner, target)
                else:
                    content = await _gen_one_file(project_id, svc, file_def, model, run, proj_pattern)
                await _save_codegen_file(jid, project_id, svc, file_def, content)
                completed["n"] += 1
                pct = 2 + int((completed["n"] / max(1, files_total)) * 95)
                _job_update(jid, step=f"[{completed['n']}/{files_total}] {file_def['path']}", pct=pct)
                if completed["n"] % 5 == 0 or completed["n"] == files_total:
                    await codegen_runs.update_one({"id": jid}, {"$set": {"files_done": completed["n"]}})

        _job_update(jid, step=f"Generating {files_total} files in parallel (concurrency=5)…", pct=2)
        await asyncio.gather(*[gen_and_save(svc, fd) for svc, fd in all_files])
        files_done = completed["n"]

        # iter-13.81.18 — Surface "all providers refused" as a visible
        # run-level error, not a silent run-complete with 100% scaffolds.
        _credit_err = run.get("_credit_err") if isinstance(run, dict) else ""
        if _credit_err:
            await codegen_runs.update_one(
                {"id": jid},
                {"$set": {"status": "error",
                          "completed_at": datetime.now(timezone.utc).isoformat(),
                          "services_done": len(services),
                          "files_done": files_done,
                          "error": _credit_err}},
            )
            _job_finish(jid, "error", error=_credit_err)
            return

        # iter-13.118.1 — POST-RUN PARITY AUDIT.
        # For every persisted code file (this run), inspect the content
        # for TODO-carpet markers / LAMA-scaffold headers / PARITY-RISK
        # comments and aggregate per service. The user sees a per-service
        # `parity_score = files_clean / files_total` so they can tell at
        # a glance whether the LLM actually pulled the legacy logic into
        # this run or whether the deterministic scaffold took over. Job
        # `step` text and `codegen_runs.parity_report` carry the result.
        parity_report: List[Dict[str, Any]] = []
        try:
            _SCAFFOLD_HEADERS = (
                "// LAMA: deterministic scaffold",
                "// LAMA: emergency scaffold",
                "// LAMA: quality-gate scaffold",
                "// LAMA scaffold",
                "# LAMA scaffold",
                "/* LAMA scaffold",
            )
            # iter-14.35 — reuse the extended carpet-marker list so gap
            # detection catches "Placeholder for business logic", "Your code
            # here", "Implementation goes here" family alongside the
            # original TODO: backfill markers.
            from codegen.parity_loop import _TODO_CARPET
            _PARITY_RISK = "// PARITY-RISK:"
            _EVIDENCE_GAP = "⚠ EVIDENCE GAP"
            # iter-14.23 — does the KB carry ROUTE field evidence at all?
            # If it does, any generated file that STILL emits a gap marker
            # dropped fields it was handed → auto-fixable (regenerate).
            _kb_has_route_evidence = False
            try:
                _kb_has_route_evidence = bool(await kb_entities.find_one(
                    {"project_id": project_id, "type": "ROUTE",
                     "request_fields": {"$exists": True, "$ne": []}},
                    {"_id": 1},
                ))
            except Exception:  # noqa: BLE001
                _kb_has_route_evidence = False
            _gap_flagged: List[str] = []
            _per_svc: Dict[str, Dict[str, int]] = {}
            _cur = codegen_files.find(
                {"project_id": project_id,
                 **({"service_name": {"$in": only_names}} if only_names else {})},
                {"_id": 0, "id": 1, "service_name": 1, "content": 1, "file_path": 1},
            )
            async for _row in _cur:
                _svc_name = _row.get("service_name") or "_root"
                _content = _row.get("content") or ""
                _bucket = _per_svc.setdefault(_svc_name, {
                    "files_total": 0, "files_scaffolded": 0,
                    "files_with_todo": 0, "files_with_parity_risk": 0,
                    "files_with_evidence_gap": 0,
                    "files_clean": 0, "total_bytes": 0,
                })
                _bucket["files_total"] += 1
                _bucket["total_bytes"] += len(_content)
                _is_scaffold = any(h in _content for h in _SCAFFOLD_HEADERS)
                _todo_hits = sum(_content.count(t) for t in _TODO_CARPET)
                _pr_hits = _content.count(_PARITY_RISK)
                _gap_hits = _content.count(_EVIDENCE_GAP)
                if _is_scaffold:
                    _bucket["files_scaffolded"] += 1
                if _todo_hits > 0:
                    _bucket["files_with_todo"] += 1
                if _pr_hits > 0:
                    _bucket["files_with_parity_risk"] += 1
                if _gap_hits > 0:
                    _bucket["files_with_evidence_gap"] += 1
                if not _is_scaffold and _todo_hits == 0:
                    _bucket["files_clean"] += 1
                # iter-14.23 — persist per-file gap_stats for the FE ribbon.
                # auto_fixable = the file emitted gap markers while the KB
                # DOES carry ROUTE field evidence (contract violation).
                _auto_fixable = bool(_gap_hits > 0 and _kb_has_route_evidence)
                _gap_stats = {
                    "evidence_gaps": _gap_hits,
                    "parity_risks": _pr_hits,
                    "auto_fixable": _auto_fixable,
                    "kb_has_route_evidence": bool(_kb_has_route_evidence),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    await codegen_files.update_one(
                        {"project_id": project_id, "id": _row.get("id")},
                        {"$set": {"gap_stats": _gap_stats}},
                    )
                except Exception:  # noqa: BLE001
                    pass
                if _auto_fixable:
                    _gap_flagged.append(_row.get("file_path") or "")
            for _svc_name, _b in sorted(_per_svc.items()):
                _ft = max(1, _b["files_total"])
                _b["parity_score"] = round(_b["files_clean"] / _ft, 3)
                _b["service"] = _svc_name
                parity_report.append(_b)
                logger.info(
                    "codegen PARITY-AUDIT svc=%s files=%d clean=%d "
                    "scaffolded=%d todo=%d parity_risk=%d evidence_gap=%d score=%.2f",
                    _svc_name, _b["files_total"], _b["files_clean"],
                    _b["files_scaffolded"], _b["files_with_todo"],
                    _b["files_with_parity_risk"], _b["files_with_evidence_gap"],
                    _b["parity_score"],
                )
            if _gap_flagged:
                logger.warning(
                    "iter-14.23 FIELD-EVIDENCE VIOLATION: %d file(s) emitted "
                    "⚠ EVIDENCE GAP while KB carries ROUTE field evidence — "
                    "flagged for regeneration: %s",
                    len(_gap_flagged), _gap_flagged[:20],
                )
            # Aggregate one-line summary for the job step text
            _tot_files = sum(b["files_total"] for b in parity_report) or 1
            _tot_clean = sum(b["files_clean"] for b in parity_report)
            _tot_scaf  = sum(b["files_scaffolded"] for b in parity_report)
            _tot_todo  = sum(b["files_with_todo"] for b in parity_report)
            _tot_gap   = sum(b.get("files_with_evidence_gap", 0) for b in parity_report)
            _summary = (
                f"Parity: {_tot_clean}/{_tot_files} clean · "
                f"{_tot_scaf} scaffold · {_tot_todo} with TODO · "
                f"{_tot_gap} evidence-gap ({len(_gap_flagged)} auto-fixable)"
            )
        except Exception as _pe:  # noqa: BLE001
            logger.warning("parity audit skipped: %s", _pe)
            _summary = ""

        await codegen_runs.update_one(
            {"id": jid},
            {"$set": {"status": "complete",
                      "completed_at": datetime.now(timezone.utc).isoformat(),
                      "services_done": len(services), "files_done": files_done,
                      "parity_report": parity_report}},
        )
        await audit_log.insert_one({
            "action": "codegen.generate",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "run_id": jid, "files": files_done, "services": len(services),
                "parity_report": parity_report,
            },
        })
        _job_finish(jid, "complete",
                    step=f"Done · {_summary}" if _summary else "Done",
                    pct=100,
                    result={"run_id": jid, "files": files_done,
                            "services": len(services),
                            "parity_report": parity_report,
                            "evidence_audit": evidence_audit})
    except JobStopped:
        # iter-13.54 — user clicked Stop in the UI; mark cleanly.
        await codegen_runs.update_one(
            {"id": jid},
            {"$set": {"status": "stopped",
                      "completed_at": datetime.now(timezone.utc).isoformat(),
                      "files_done": completed["n"] if 'completed' in dir() else 0}},
        )
        _job_finish(jid, "stopped",
                    step=f"Stopped by user (completed {completed['n'] if 'completed' in dir() else 0}/{files_total if 'files_total' in dir() else 0})",
                    pct=_JOBS.get(jid, {}).get("pct", 0))
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error("codegen job %s CRASHED: %s\n%s", jid, e, tb)
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/generate")
async def start_codegen(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Architecture", "CodeGen")
    # iter-13.122 — Self-heal arch_services if the collection is empty
    # but Architecture is frozen. Older projects (frozen before the
    # iter-13.122 approve fix) can have zero arch_services rows even
    # though the StageContext is intact. Restoring here means Generate
    # All works without forcing the user to Reset + re-freeze Stage 3.
    if await arch_services.count_documents({"project_id": project_id}) == 0:
        restored = await _backfill_arch_services_from_stage_context(project_id)
        if restored == 0:
            raise HTTPException(
                400,
                "Architecture is frozen but no services are available. "
                "Reset Stage 3 and re-run Recommend + Approve to rebuild the service catalog.",
            )
    # iter-13.81.3 — re-run? → codegen.regenerate so Console pin applies.
    _existing = await codegen_files.find_one({"project_id": project_id}, {"_id": 1})
    set_current_agent_key("codegen.regenerate" if _existing else "codegen.service")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    # iter-13.120 — accept both the legacy scalar (`service_name`) and the
    # new multi-select list (`service_names`). List wins when both are
    # present. Empty list == "generate all services" (equivalent to null).
    only_scalar = payload.get("service_name") or None
    only_list = payload.get("service_names")
    if isinstance(only_list, list):
        only_list = [s for s in only_list if isinstance(s, str) and s.strip()]
    else:
        only_list = None
    jid = _new_job(project_id, "codegen")
    asyncio.create_task(_run_codegen_job(
        jid, project_id, model,
        only_service=only_scalar,
        only_services=only_list,
    ))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# B. Gap recovery (iter-13.28) — regenerate backend / frontend service
# -----------------------------------------------------------
import os as _os


def _validate_structure(content: str, file_type: str, backend_lang: str,
                        *, ctype: str = "", framework: str = "") -> tuple[bool, list[str]]:
    """iter-13.82 — Reject LLM output that lacks the framework markers the
    code MUST have (e.g. a Java controller missing @RestController, a
    FastAPI controller missing APIRouter, a .NET controller missing
    [ApiController]). The validator consults ``REQUIRED_TOKENS`` from
    ``codegen/file_templates.py``; each entry is either a literal substring
    (case-sensitive) or a ``.regex/<pat>`` regex that must match.

    iter-13.85 — ``ctype`` + ``framework`` route frontend files to
    ``FRONTEND_REQUIRED_TOKENS`` (keyed by (component_type, framework)).
    Frontend services have no ``backend_lang`` so the backend table was
    never consulted for them; before iter-13.85 every .tsx file silently
    passed validation regardless of content.

    Returns ``(ok, missing)`` — ``missing`` is the list of tokens the LLM
    dropped, so the next retry can name them explicitly.
    """
    if not content or len(content.strip()) < 40:
        return False, ["content too short / empty"]
    # Frontend branch: when caller supplies a ctype + framework, route to
    # the frontend token table FIRST. Falls through to the backend table
    # only when frontend yields nothing (e.g. a Dockerfile in the
    # frontend/ folder — language-agnostic).
    if ctype and framework:
        fe_tokens = frontend_required_tokens_for(ctype, framework)
        if fe_tokens:
            missing: list[str] = []
            for tok in fe_tokens:
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
    tokens = required_tokens_for(file_type or "", backend_lang or "")
    if not tokens:
        # No structural contract defined for this (file_type, lang) — pass.
        return True, []
    missing = []
    for tok in tokens:
        if tok.startswith(".regex/"):
            pat = tok[len(".regex/"):]
            try:
                if not re.search(pat, content):
                    missing.append(f"pattern `{pat}`")
            except re.error:
                # Bad regex in table — skip (don't block the user on a typo).
                continue
        else:
            if tok not in content:
                missing.append(f"`{tok}`")
    return (len(missing) == 0), missing


# Per-language syntax-validation tail so the gap-recovery file actually
# compiles. Best-effort: returns (ok: bool, error: str). Validators that
# require runtime installs (javac, dotnet, go) just check brace/paren
# balance — full compile happens later via CI.
def _validate_syntax(content: str, language: str) -> tuple[bool, str]:
    if not content or len(content.strip()) < 20:
        return False, "empty/short content"
    lang = (language or "").lower()
    try:
        if lang == "python":
            import ast as _ast
            _ast.parse(content)
            return True, ""
        if lang in ("javascript", "typescript", "jsx", "tsx"):
            # Brace + paren balance + no unterminated string fences.
            if content.count("{") != content.count("}"):
                return False, "brace mismatch"
            if content.count("(") != content.count(")"):
                return False, "paren mismatch"
            return True, ""
        if lang in ("java", "csharp", "go", "rust"):
            if content.count("{") != content.count("}"):
                return False, "brace mismatch"
            if content.count("(") != content.count(")"):
                return False, "paren mismatch"
            return True, ""
        if lang == "json":
            import json as _json
            _json.loads(content)
            return True, ""
        if lang in ("yaml", "yml"):
            try:
                import yaml as _yaml
                _yaml.safe_load(content)
                return True, ""
            except Exception as e:
                return False, f"yaml: {e}"
        return True, ""
    except SyntaxError as se:
        return False, f"SyntaxError: {se}"
    except Exception as e:
        return False, str(e)


_GAP_SECTION_RE = re.compile(
    r"===\s*RECOVERED FILE\s*===\s*\n(.*?)(?:\n===\s*\w[^=]*===|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_GAP_REPORT_RE = re.compile(
    r"===\s*GAP REPORT\s*===\s*\n(.*?)\n===\s*RECOVERY PLAN\s*===",
    re.DOTALL | re.IGNORECASE,
)
_RECOVERY_PLAN_RE = re.compile(
    r"===\s*RECOVERY PLAN\s*===\s*\n(.*?)\n===\s*RECOVERED FILE\s*===",
    re.DOTALL | re.IGNORECASE,
)


def _extract_gap_sections(text: str) -> Dict[str, str]:
    """Pull the three named sections out of the gap-recovery LLM response."""
    out = {"gap_report": "", "recovery_plan": "", "recovered_file": ""}
    if not text:
        return out
    m = _GAP_REPORT_RE.search(text)
    if m:
        out["gap_report"] = m.group(1).strip()
    m = _RECOVERY_PLAN_RE.search(text)
    if m:
        out["recovery_plan"] = m.group(1).strip()
    m = _GAP_SECTION_RE.search(text)
    if m:
        out["recovered_file"] = _strip_md_fence(m.group(1).strip())
    # Fallback: if the LLM ignored markers entirely, the whole text is the
    # recovered file (no report). Better than losing the call.
    if not out["recovered_file"] and "===" not in text:
        out["recovered_file"] = _strip_md_fence(text.strip())
    return out


async def _gap_recover_one_file(
    project_id: str, svc: dict, file_doc: dict, model: str, run_id: str,
) -> Dict[str, Any]:
    """Run gap-detect + recover on one existing generated file.

    Returns ``{file_path, ok, error, gap_report, recovery_plan, applied}``.
    On success the file is overwritten with the recovered content (a new
    version is created); ``applied=True``. On any failure ``applied=False``
    and the previous file content is kept untouched (data-loss safe).
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    arch_ctx = await stage_context_col.find_one({"project_id": project_id, "stage": "Architecture"}, {"_id": 0})
    api_contracts_full = ((arch_ctx or {}).get("outputs", {}) or {}).get("api_contracts", "") or ""
    if not api_contracts_full:
        api_doc = await arch_documents.find_one({"project_id": project_id, "type": "api_contracts"}, {"_id": 0})
        api_contracts_full = (api_doc or {}).get("content", "") or ""
    dm_ctx = await stage_context_col.find_one({"project_id": project_id, "stage": "DataModel"}, {"_id": 0})
    oltp_ddl = await _effective_oltp_ddl(
        project_id, ((dm_ctx or {}).get("outputs", {}) or {}).get("oltp_ddl", ""),
    )

    template = await _get_prompt(project_id, "codegen.gap_recovery")
    if not template:
        return {"file_path": file_doc["file_path"], "ok": False,
                "error": "codegen.gap_recovery prompt missing — restart backend to re-seed.",
                "applied": False, "gap_report": "", "recovery_plan": ""}
    _assert_prompt_fresh("codegen.gap_recovery", template)

    await _ensure_kb_graph(project_id)
    srs_sections = await _load_srs_sections(project_id)
    srs_use_cases = (srs_sections.get("detailed_use_cases", "") or srs_sections.get("use_cases", "") or "")[:3500]
    srs_nfr = (srs_sections.get("non_functional_requirements", "") or "")[:1800]

    # Best-effort scope hints from the codegen_files doc + path heuristics.
    path = file_doc["file_path"]
    file_type = file_doc.get("file_type", "other")
    file_scope_parts = []
    seeds: List[str] = []
    for token in re.split(r"[/_\-.]", path):
        token = token.strip()
        if not token or len(token) < 3:
            continue
        seeds.append(token.lower())
        file_scope_parts.append(token)
    seeds = list(dict.fromkeys(seeds))[:8]
    if svc.get("tables"):
        seeds = list(svc["tables"]) + seeds
    if svc.get("name"):
        seeds.append(svc["name"])

    try:
        from kb.graph_config import is_graph_kb_enabled
        if await is_graph_kb_enabled(project_id):
            from kb.graph_retriever import subgraph_yaml_for_terms
            graph_yaml = await subgraph_yaml_for_terms(
                project_id, seeds, max_chars=2800, hops=2, max_nodes=120,
            )
        else:
            graph_yaml = ""
    except Exception:
        graph_yaml = ""
    # iter-13.29 — deep retrieval for gap-recovery: real legacy source
    # via graph-first + Mongo regex + entity-typed vector top-up. The
    # gap-recovery prompt benefits MOST from raw source code because it
    # has to reproduce business rules byte-for-byte.
    # iter-13.81.14 — Broaden seeds (module names + routes_detail) and
    # bump budget 5500→16000. Gap recovery's whole job is to find missing
    # legacy logic; starving it of context defeats the purpose.
    extra_seeds: List[str] = []
    extra_seeds.extend(svc.get("module_names") or [])
    extra_seeds.extend(svc.get("modules") or [])
    for rd in (svc.get("routes_detail") or [])[:10]:
        if rd.get("class"):
            extra_seeds.append(rd["class"])
    # iter-14.37 — Frontend files need view-layer seeds, not
    # controller/service words. The retriever above seeded from the
    # file name only, which for React components matches nothing in a
    # JSP/Struts legacy KB. Add view-vocabulary hints so the graph +
    # vector layers surface the actual JSP / HTML / JS bodies the
    # component is meant to reproduce.
    _pre_is_frontend = bool(svc.get("frontend")) or (path or "").startswith("frontend/")
    if _pre_is_frontend:
        _fe_seeds = [
            "jsp", "jspx", "html", "htm", "javascript", "view",
            "form", "page", "screen", "template", "layout",
            "input", "submit", "onclick", "field", "label",
            "select", "table", "list", "grid", "action",
        ]
        # File-name-derived screen tokens (e.g. UserForm.jsx → user, form)
        for tok in re.split(r"[/_\-.]", path):
            tok = (tok or "").strip().lower()
            if tok and 3 <= len(tok) <= 24 and tok not in {"jsx", "tsx", "vue", "html", "css"}:
                _fe_seeds.append(tok)
        extra_seeds.extend(_fe_seeds)
    _hot_words = (
        ["form", "input", "submit", "field", "validate", "role", "view"]
        if _pre_is_frontend
        else ["controller", "service", "form", "validate", "role"]
    )
    legacy_block = await _deep_legacy_context(
        project_id, list(dict.fromkeys(seeds + extra_seeds + _hot_words)),
        max_chars=16000,
    ) or "(no legacy source matched this scope)"

    # iter-14.40 — 1:1 legacy source override. When the file was created
    # by the JSP expander it carries `legacy_source_file` (+ optionally
    # `kb_file_id`). Load that JSP's FULL body from `kb_chunks` and
    # prepend it to the legacy block so the LLM reproduces the exact
    # screen instead of a topical mishmash.
    _direct_jsp_ref = (file_doc.get("legacy_source_file") or "").strip()
    _direct_jsp_id = (file_doc.get("kb_file_id") or "").strip()
    if _direct_jsp_ref:
        try:
            jsp_bodies: List[str] = []
            query = {"project_id": project_id}
            if _direct_jsp_id:
                query["file_id"] = _direct_jsp_id
            else:
                # fall back to filename lookup
                jsp_file = await kb_files.find_one(
                    {"project_id": project_id, "filename": _direct_jsp_ref},
                    {"id": 1},
                )
                if jsp_file:
                    query["file_id"] = jsp_file.get("id")
            if "file_id" in query:
                async for c in kb_chunks.find(query, {"content": 1, "chunk_index": 1}).sort("chunk_index", 1):
                    body = (c.get("content") or "").strip()
                    if body:
                        jsp_bodies.append(body)
                    if sum(len(b) for b in jsp_bodies) > 20000:
                        break
            if jsp_bodies:
                direct_block = (
                    f"# === DIRECT LEGACY SOURCE — 1:1 predecessor of this file ===\n"
                    f"# Legacy filename: {_direct_jsp_ref}\n"
                    f"# This is the EXACT JSP / view this component must reproduce.\n"
                    f"# Every form field, label, validation, table column, role check\n"
                    f"# and submit action in the modern component MUST come from here.\n"
                    f"# ------------------------------------------------------------\n"
                    + "\n\n".join(jsp_bodies)[:20000]
                    + "\n# === END DIRECT LEGACY SOURCE ===\n\n"
                )
                legacy_block = direct_block + legacy_block
        except Exception as e:
            logger.warning("gap-recover %s legacy_source_file load failed: %s", path, e)

    api_slice = _slice_api_contract_for_service(api_contracts_full, svc.get("name", ""))

    # iter-14.46 — TABLE-SPECIFIC EVIDENCE for entity / repository / mapper /
    # DTO files. The generic `_deep_legacy_context` retriever indexes on
    # keyword seeds, which for a file like ASRIMACCTSHEADRepository.java
    # yields near-nothing because the file name is a mangled class name
    # (see _pascal fix in the same iteration). We now:
    #   1. Resolve the target legacy table by matching the file basename
    #      against `svc.tables` with underscores dropped + case-folded.
    #   2. Slice the CREATE TABLE block for that table out of `oltp_ddl`.
    #   3. Pull ≤5 KB-chunks whose content mentions the table name
    #      (uppercase or lowercase), so the LLM sees the LEGACY entity/DAO
    #      code — which already uses idiomatic Java PascalCase names
    #      (e.g. `AsrimAcctsHead`) and full SQL statements.
    #   4. Prepend a strong instruction block to `legacy_block` so the
    #      LLM emits the correct class name, real @Column fields, and
    #      one @Query per legacy SQL statement.
    _table_evidence_block = ""
    _resolved_table = ""
    if not (path or "").startswith("frontend/") and file_type in {"entity", "repository", "mapper", "dto"}:
        try:
            stem = os.path.splitext(os.path.basename(path or ""))[0]
            # Strip Java-suffix conventions
            for _suf in ("RepositoryImpl", "Repository", "Mapper", "MapperImpl",
                         "Dtos", "DTO", "Dto", "Entity"):
                if stem.endswith(_suf):
                    stem = stem[: -len(_suf)]
                    break
            stem_norm = stem.replace("_", "").lower()
            svc_tables = list(svc.get("tables") or [])
            for tbl in svc_tables:
                if str(tbl).replace("_", "").lower() == stem_norm:
                    _resolved_table = tbl
                    break
            # Fallback: substring match
            if not _resolved_table and stem_norm:
                for tbl in svc_tables:
                    tnorm = str(tbl).replace("_", "").lower()
                    if tnorm and (tnorm in stem_norm or stem_norm in tnorm):
                        _resolved_table = tbl
                        break
        except Exception:  # noqa: BLE001
            _resolved_table = ""

    if _resolved_table:
        try:
            # Extract full CREATE TABLE block for this table.
            _ddl_block_for_table = ""
            _ddl_upper = oltp_ddl or ""
            _ct_re = re.compile(
                rf'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?{re.escape(_resolved_table)}["`\]]?\s*\(',
                re.IGNORECASE,
            )
            m = _ct_re.search(_ddl_upper)
            if m:
                # Walk parens forward to find the closing bracket.
                start = m.start()
                depth = 0
                i = m.end() - 1
                while i < len(_ddl_upper):
                    ch = _ddl_upper[i]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            # Include trailing `;` if present.
                            end = i + 1
                            if end < len(_ddl_upper) and _ddl_upper[end] == ";":
                                end += 1
                            _ddl_block_for_table = _ddl_upper[start:end]
                            break
                    i += 1
            # Pull legacy code chunks that mention the table name.
            _legacy_dao_chunks: List[str] = []
            try:
                async for c in kb_chunks.find(
                    {"project_id": project_id,
                     "content": {"$regex": re.escape(_resolved_table),
                                 "$options": "i"}},
                    {"content": 1, "file_id": 1},
                ).limit(5):
                    body = (c.get("content") or "").strip()
                    if body:
                        _legacy_dao_chunks.append(body[:2400])
                    if sum(len(b) for b in _legacy_dao_chunks) > 8000:
                        break
            except Exception:  # noqa: BLE001
                pass
            _cls_hint = _pascal(_resolved_table)
            _table_evidence_block = (
                f"# === TABLE-SPECIFIC LEGACY EVIDENCE — {_resolved_table} ===\n"
                f"# You are generating a Java {file_type} for the legacy table "
                f"`{_resolved_table}`.\n"
                f"# HARD RULES you MUST follow:\n"
                f"#   1. Java class name MUST be `{_cls_hint}` (PascalCase, "
                f"underscores dropped, tail lowercased). Do NOT emit "
                f"screaming-case names like `{_resolved_table.replace('_','')}`.\n"
                f"#   2. Entity fields MUST mirror EVERY column in the DDL "
                f"below (`@Column(name=\"<COL>\")`). Do NOT invent placeholder "
                f"columns. Do NOT drop columns.\n"
                f"#   3. Repository MUST include one `@Query` method for "
                f"EACH distinct SQL statement found in the LEGACY CODE "
                f"below (SELECT/UPDATE/INSERT/DELETE against this table). "
                f"Preserve WHERE clauses, joins, ORDER BY. Convert Oracle "
                f"positional binds (`?`) or named binds to JPQL `:paramName`.\n"
                f"#   4. Mapper MUST have one field-copy per DDL column, "
                f"typed by the DDL SQL type (VARCHAR2 → String, NUMBER → "
                f"Long/BigDecimal, DATE → LocalDate, TIMESTAMP → "
                f"OffsetDateTime).\n"
                f"# --- OLTP DDL for {_resolved_table} ---\n"
                f"{_ddl_block_for_table or '(no CREATE TABLE block found — infer from legacy Java below)'}\n"
                f"# --- LEGACY JAVA / SQL touching {_resolved_table} (verbatim from KB) ---\n"
            )
            if _legacy_dao_chunks:
                _table_evidence_block += "\n---\n".join(_legacy_dao_chunks) + "\n"
            else:
                _table_evidence_block += "(no legacy references found in KB — use the DDL above)\n"
            _table_evidence_block += (
                f"# === END TABLE-SPECIFIC EVIDENCE ({_resolved_table}) ===\n\n"
            )
            legacy_block = _table_evidence_block + legacy_block
            # NB: The DDL prepend into `relevant_ddl` happens AFTER the
            # frontend-detection block sets `relevant_ddl` (below), to
            # avoid a NameError. We stash the block on a local so the
            # post-detection code can find it.
            _resolved_ddl_block = _ddl_block_for_table
        except Exception as _te:  # noqa: BLE001
            logger.warning(
                "gap-recover table-evidence build failed for %s: %s", path, _te,
            )
            _resolved_ddl_block = ""
    else:
        _resolved_ddl_block = ""

    # iter-14.37 — Frontend detection. Frontend files have no OLTP DDL
    # concept; feeding them a `(no DDL)` block AND then telling the LLM
    # in the user message that OLTP DDL is a mandatory source causes the
    # model to refuse the task with "I cannot complete this task as
    # there is no provided legacy code evidence, graphify subgraph,
    # OLTP DDL, or SRS use case information".
    _is_frontend_file = bool(svc.get("frontend")) or (path or "").startswith("frontend/")
    # iter-14.39 — Detect whether the retriever found a DIRECT legacy
    # match for THIS component. For invented screens (files created by
    # the Architecture stage from SRS concepts without any 1:1 legacy
    # JSP — e.g. `LookupReference.tsx`), the retriever surfaces a
    # firehose of topically-related-but-unrelated legacy content. The
    # LLM correctly rejects that as "not what I'm meant to reproduce"
    # and apologises. Pivot the message when this happens.
    _fe_no_direct_match = False
    if _is_frontend_file and not _direct_jsp_ref:
        _stem = os.path.splitext(os.path.basename(path or ""))[0].lower()
        # tokens from filename (LookupReference → lookup, reference)
        _stem_tokens = [t for t in re.findall(r'[a-z]+', re.sub(r'([A-Z])', r' \1', _stem or '').lower()) if len(t) >= 4]
        _legacy_lc = (legacy_block or "").lower()
        # No direct match if none of the file's own tokens appear in
        # any Class / Route / method name pulled by the graph.
        if _stem_tokens and not any(t in _legacy_lc for t in _stem_tokens):
            _fe_no_direct_match = True
    if _is_frontend_file:
        # Reframe the "DDL" slot as legacy VIEW evidence so the template
        # sees a populated block instead of `(no DDL)`. The actual view
        # bodies are still injected via `legacy_evidence` below.
        relevant_ddl = (
            "N/A — this is a FRONTEND module. Legacy view evidence "
            "(JSP / HTML / JavaScript) replaces the OLTP DDL slot for "
            "this file. Refer to LEGACY CODE EVIDENCE, API CONTRACT "
            "SLICE, and SRS USE CASES as your primary sources."
        )
    else:
        relevant_ddl = "\n".join(
            ln for ln in oltp_ddl.split("\n")
            if any(t in ln for t in (svc.get("tables") or []))
        )[:3000] or oltp_ddl[:2000]

    # iter-14.46 — Prepend the file-specific CREATE TABLE block so
    # entity / repository / mapper / DTO files see the EXACT schema
    # they must map, not just the service-wide DDL filter which the LLM
    # tends to skim.
    if _resolved_ddl_block:
        relevant_ddl = _resolved_ddl_block + "\n\n" + (relevant_ddl or "")
        relevant_ddl = relevant_ddl[:5000]

    file_endpoints = svc.get("api_endpoints") or []
    target_language = file_doc.get("language") or svc.get("backend_lang") or "text"
    # iter-13.121 — Pattern is carried on every arch_services row (set in
    # routes/architecture.py from parsed.recommended_pattern; merged
    # services keep the first row's pattern). Fall back to microservices
    # when the field is empty (legacy docs) or unknown.
    _pat = str(svc.get("pattern") or "").strip().lower()
    if _pat not in ("microservices", "modular_monolith", "monolith"):
        _pat = "microservices"

    sp = _safe_format(
        template,
        project_name=proj.get("name", ""),
        source_tech=proj.get("source_tech", ""),
        target_tech=proj.get("target_tech", "") or "",
        target_language=target_language,
        file_path=path,
        file_type=file_type,
        file_scope=" / ".join(file_scope_parts[:6]) or "(file-level)",
        file_endpoints="\n".join(file_endpoints) if file_endpoints else "(none in scope)",
        current_content=(file_doc.get("content") or "")[:18000],
        relevant_ddl=relevant_ddl or "(no DDL)",
        graph_subgraph=graph_yaml or "(no graph slice)",
        legacy_evidence=legacy_block,
        api_contract=api_slice or "(no API contracts)",
        srs_use_cases=srs_use_cases or "(no SRS use cases — freeze SRS first)",
        srs_nfr=srs_nfr or "(no NFRs)",
        architecture_pattern=_pat,
    )

    _log_prompt_context(
        "codegen.gap_recovery",
        file=path,
        graph=graph_yaml, legacy=legacy_block,
        api_contract=api_slice, ddl=relevant_ddl,
        srs_use_cases=srs_use_cases, srs_nfr=srs_nfr,
        current_content=file_doc.get("content", ""),
    )

    # iter-13.71 — inject 100% legacy-parity completeness contract
    sp = await _with_completeness_contract(project_id, sp)

    last_err = ""
    # iter-13.81.14 — 4 attempts with progressive temperature, matching
    # the new _gen_one_file behavior. Gap recovery is what the user clicks
    # AFTER a failed initial codegen; capping at 2 attempts is the reason
    # so many "fix the blank files" cycles loop endlessly.
    _ATTEMPT_TEMPS_GAP = [0.1, 0.2, 0.3, 0.45]
    for attempt_idx, temp in enumerate(_ATTEMPT_TEMPS_GAP):
        attempt = attempt_idx + 1
        try:
            # iter-14.37 — Frontend-aware user message. The generic
            # message lists "OLTP DDL" as a mandatory input, which
            # causes the LLM to refuse on frontend files ("no OLTP DDL
            # provided → I cannot complete this task"). Swap to a
            # view-layer message when we're recovering a JSX/TSX/HTML
            # component.
            if _is_frontend_file and _fe_no_direct_match:
                # iter-14.39 — Invented FE screen. No 1:1 legacy JSP
                # exists. Build from API contract + SRS + FE patterns.
                _user_msg = (
                    f"Recover / complete {path}.\n\n"
                    f"IMPORTANT: This component is a MODERN-ONLY screen — "
                    f"there is NO 1:1 legacy JSP that maps to this file. "
                    f"The LEGACY CODE EVIDENCE block above contains "
                    f"related legacy code but NOT a direct predecessor. "
                    f"DO NOT REFUSE for lack of a direct legacy match. "
                    f"Build a real, production-grade component using:\n"
                    f"  1. CURRENT FILE CONTENT (top priority — extend it, don't erase it).\n"
                    f"  2. API CONTRACT SLICE — call the backend endpoints listed.\n"
                    f"  3. SRS USE CASES — the user flows this screen implements.\n"
                    f"  4. Sibling FE files in the same service for styling / hooks / router patterns.\n"
                    f"  5. LEGACY CODE EVIDENCE — treat as INSPIRATION for form field names, "
                    f"     labels, validation messages, and role-gated actions — NOT as a strict template.\n\n"
                    f"Hard rules:\n"
                    f"  • Emit a COMPLETE React/TS component. No `Placeholder`, no `TODO`, "
                    f"    no empty `onClick={{() => {{}}}}`, no commented-out JSX.\n"
                    f"  • Every API call MUST use an operationId from the API CONTRACT.\n"
                    f"  • Wire real state + handlers. Use axios / fetch consistent with sibling files.\n"
                    f"  • Include loading, empty, and error states.\n"
                    f"  • Under NO circumstance emit an apology, refusal, or 'please provide' "
                    f"    message — such output is treated as a REJECT and re-tried.\n"
                    f"  • Output only the 3-section structure the system prompt requires."
                )
            elif _is_frontend_file:
                _user_msg = (
                    f"Detect and recover legacy-parity gaps for {path} "
                    f"(FRONTEND component in a {target_language} module). "
                    f"Emit the 3-section output now.\n\n"
                    f"SOURCES applicable to this file:\n"
                    f"  • CURRENT FILE CONTENT (above) — what's already generated.\n"
                    f"  • LEGACY CODE EVIDENCE — JSP / HTML / JS excerpts of the\n"
                    f"    original screen this component replaces.\n"
                    f"  • GRAPHIFY SUBGRAPH — legacy classes/routes bound to this view.\n"
                    f"  • API CONTRACT SLICE — backend endpoints the component calls.\n"
                    f"  • SRS USE CASES — user flows the component implements.\n"
                    f"  • (OLTP DDL is N/A for frontend — do NOT refuse for lack of it.)\n\n"
                    f"Hard rules for the RECOVERED FILE section:\n"
                    f"  1. Every form field, label, validation, role-gated action,\n"
                    f"     dropdown option, and API call visible in LEGACY CODE\n"
                    f"     EVIDENCE MUST appear in the recovered component with\n"
                    f"     real handlers wired up. `Placeholder`, `TODO`, `FIXME`,\n"
                    f"     empty `onClick={{() => {{}}}}`, or commented-out JSX\n"
                    f"     blocks are FORBIDDEN.\n"
                    f"  2. Every API call MUST use the exact operationId from the\n"
                    f"     API CONTRACT SLICE — do not invent endpoints.\n"
                    f"  3. Preserve the exact field labels, placeholder text, and\n"
                    f"     validation error messages from LEGACY CODE EVIDENCE.\n"
                    f"  4. If a single low-level detail is genuinely absent from\n"
                    f"     ALL evidence blocks, emit ONE `// PARITY-RISK: <reason>`\n"
                    f"     on that line and still implement a defensible default.\n"
                    f"     Max one PARITY-RISK per file.\n"
                    f"  5. If a required source appears to be MISSING, DO NOT\n"
                    f"     apologise or refuse — write the best possible component\n"
                    f"     from CURRENT FILE CONTENT + API CONTRACT + SRS USE CASES\n"
                    f"     and mark the truly-missing pieces with a single\n"
                    f"     PARITY-RISK comment. A refusal is treated as a REJECT."
                )
            else:
                _user_msg = (
                    f"Detect and recover legacy-parity gaps for {path}. "
                    f"Emit the 3-section output now.\n\n"
                    f"Hard rules for the RECOVERED FILE section:\n"
                    f"  1. Preconditions / Postconditions / Business Rules "
                    f"     / Validation Logic / Field Specification blocks "
                    f"     MUST be POPULATED in-line with the real content "
                    f"     extracted from LEGACY CODE EVIDENCE, GRAPHIFY "
                    f"     SUBGRAPH, OLTP DDL and SRS USE CASES above. "
                    f"     `TODO`, `FIXME`, `XXX`, `backfill`, BR-IDs "
                    f"     alone, or bare legacy file-path references are "
                    f"     FORBIDDEN inside those blocks — if you can name "
                    f"     the legacy artifact, you can read its body "
                    f"     above; implement it.\n"
                    f"  2. Every method body must contain real executable "
                    f"     target-stack code reproducing the legacy "
                    f"     branch-for-branch — never a `throw "
                    f"     UnsupportedOperationException` / `raise "
                    f"     NotImplementedError` / `pass # TODO` placeholder.\n"
                    f"  3. If a single low-level detail is genuinely "
                    f"     unresolvable from ALL evidence blocks, emit ONE "
                    f"     `// PARITY-RISK: <one-line reason>` comment on "
                    f"     THAT LINE ONLY and still implement a defensible "
                    f"     default behaviour consistent with the surrounding flow. "
                    f"     Max one PARITY-RISK per file. NEVER inside "
                    f"     Preconditions / Postconditions / Business Rules.\n"
                    f"  4. NEVER emit `// TODO: backfill from legacy KB` — "
                    f"     that exact phrase is on the consumer's reject list.\n"
                    f"  5. If a required source appears to be MISSING, DO NOT "
                    f"     apologise or refuse — implement from whatever "
                    f"     CURRENT FILE CONTENT + partial evidence you have, "
                    f"     and mark truly-missing pieces with ONE PARITY-RISK "
                    f"     comment. A refusal is treated as a REJECT."
                )
            r = await chat_completion(
                messages=[{"role": "system", "content": sp},
                          {"role": "user", "content": _user_msg}],
                model=model, temperature=temp,
                max_tokens=14000, timeout=360.0,
                agent_key="codegen.gap_recovery",   # iter-13.76 — high → Opus
                project_id=project_id,
            )
            raw = r.get("content", "") or ""
            sections = _extract_gap_sections(raw)
            recovered = sections["recovered_file"]
            # iter-14.37 / iter-14.38 — Apology / refusal detection.
            # LLMs sometimes return "I'm sorry, but I cannot complete
            # this task…" or "Please provide the necessary information
            # and I will be happy to assist you further" as the
            # RECOVERED FILE body. These apologies pass the length
            # check (>80 chars) and get persisted as if they were real
            # code (see LookupReference.tsx bug reported after iter-14.37).
            # iter-14.38 broadens the marker list to substring-match
            # any 2 of the following conversational tokens.
            _refusal_markers = (
                "i'm sorry, but i cannot",
                "i am sorry, but i cannot",
                "i cannot complete this task",
                "i cannot complete this request",
                "i can't complete this task",
                "i can't complete this request",
                "i'm unable to complete",
                "i am unable to complete",
                "i'm unable to assist",
                "i am unable to assist",
                "as an ai language model",
                "as a large language model",
                "please provide the necessary details",
                "please provide the necessary information",
                "please provide more context",
                "please provide more information",
                "please provide the required",
                "insufficient information to",
                "there is no provided legacy code evidence",
                "there is no legacy code evidence",
                "no legacy code evidence, graphify subgraph, oltp ddl",
                "happy to assist you further",
                "happy to help you further",
                "i will be happy to assist",
                "i'll be happy to assist",
                "i would be happy to assist",
                "i can assist you further",
                "so i can assist you",
                "provide the necessary details and i",
                "cannot assist without",
                "without the necessary",
                "without the required",
            )
            # Scan the WHOLE recovered file, not just first 2000 chars.
            _rec_lc = (recovered or "").lower()
            _raw_lc = (raw or "").lower()[:4000]
            _matched = [m for m in _refusal_markers if m in _rec_lc or m in _raw_lc]
            # Additional signal: for code files, conversational pronouns
            # ("I ", "you ", "please ") appearing in a SHORT recovered
            # body (<1500 chars) is a strong apology signal even if none
            # of the exact markers hit.
            _conversational_hit = (
                len(recovered or "") < 1500
                and sum(1 for tok in (" i ", " you ", " please ", "sorry") if tok in _rec_lc) >= 2
                and not any(kw in _rec_lc for kw in ("import ", "export ", "function ", "class ", "const ", "def ", "public ", "private "))
            )
            if _matched or _conversational_hit:
                last_err = (
                    f"LLM refused / apologised on attempt={attempt} "
                    f"(markers={_matched[:3]}, conversational={_conversational_hit})"
                )
                logger.warning("gap-recover %s attempt=%d LLM APOLOGY (%s) — retry",
                               path, attempt, _matched[:3] or 'conversational')
                continue
            if not recovered or len(recovered) < 80:
                last_err = f"recovered file empty/short (len={len(recovered)}, attempt={attempt})"
                logger.warning("gap-recover %s attempt=%d %s — retry", path, attempt, last_err)
                continue
            ok, syn_err = _validate_syntax(recovered, target_language)
            if not ok:
                last_err = f"syntax check failed (attempt={attempt}): {syn_err}"
                logger.warning("gap-recover %s attempt=%d %s — retry", path, attempt, last_err)
                continue
            # iter-13.82 — Structural validation on the recovered file
            # too. If the LLM stripped `@RestController` while "fixing"
            # the file (which it sometimes does when it rewrites
            # aggressively), reject and retry with explicit feedback.
            struct_ok, missing = _validate_structure(
                recovered, file_type, target_language,
            )
            if not struct_ok and attempt < len(_ATTEMPT_TEMPS_GAP):
                last_err = (
                    f"structural validation failed on recovered file "
                    f"(attempt={attempt}) — missing: {', '.join(missing)}"
                )
                logger.warning("gap-recover %s STRUCTURAL FAIL attempt=%d missing=%s — retry",
                               path, attempt, missing)
                continue
            # iter-14.38 — LAST-DITCH write-time apology guard. Even if
            # the per-attempt refusal detector missed a novel phrasing
            # (e.g. new model variant), refuse to persist any body that
            # is (a) short AND (b) prose-only (no code keywords). This
            # prevents `Please provide the necessary information and I
            # will be happy to assist you further.` from ever being
            # written to disk.
            _final_lc = (recovered or "").lower()
            _looks_like_code = any(kw in _final_lc for kw in (
                "import ", "export ", "function ", "const ", "class ",
                "def ", "public ", "private ", "@override", "return ",
                "package ", "namespace ", "<html", "<div", "select ",
                "insert ", "update ",
            ))
            if not _looks_like_code and len(recovered) < 2000:
                last_err = (
                    f"recovered file has no code keywords "
                    f"(len={len(recovered)}, attempt={attempt}) — apology-shaped"
                )
                logger.warning("gap-recover %s WRITE-GUARD reject: %s", path, last_err)
                continue
            # Apply
            now = datetime.now(timezone.utc).isoformat()
            await codegen_files.update_one(
                {"project_id": project_id, "file_path": path},
                {"$set": {
                    "content": recovered,
                    "version": (file_doc.get("version", 1) or 1) + 1,
                    "edited": False,
                    "updated_at": now,
                    "last_gap_report": sections["gap_report"][:8000],
                    "last_recovery_plan": sections["recovery_plan"][:8000],
                    "last_recovery_at": now,
                    "last_recovery_run": run_id,
                }},
            )
            return {
                "file_path": path, "ok": True, "applied": True,
                "gap_report": sections["gap_report"], "recovery_plan": sections["recovery_plan"],
                "error": "",
            }
        except Exception as e:
            last_err = str(e)
            logger.warning("gap-recover %s attempt=%d LLM error: %s", path, attempt, e)
    return {
        "file_path": path, "ok": False, "applied": False, "error": last_err,
        "gap_report": "", "recovery_plan": "",
    }


def _gap_file_timeout() -> float:
    """Per-file wall-clock cap (seconds) for a single gap-recovery pass.
    iter-14.23 — bounds the LLM call so the gap-recovery job / confidence
    loop can never hang at "Loading generated files… 2%" on an
    unresponsive endpoint. Default 120s; LAMA_CODEGEN_GAP_FILE_TIMEOUT
    overrides (clamp [30, 900])."""
    try:
        raw = float(os.environ.get("LAMA_CODEGEN_GAP_FILE_TIMEOUT", "120") or 120.0)
    except ValueError:
        raw = 120.0
    return max(30.0, min(900.0, raw))


async def _gap_recover_one_file_bounded(project_id, svc, fdoc, model, jid):
    """`_gap_recover_one_file` under a hard per-file timeout so one stuck
    file cannot freeze the whole recovery/confidence loop."""
    path = fdoc.get("file_path", "")
    try:
        return await asyncio.wait_for(
            _gap_recover_one_file(project_id, svc, fdoc, model, jid),
            timeout=_gap_file_timeout(),
        )
    except asyncio.TimeoutError:
        logger.warning("gap-recover %s timed out after %.0fs — skipping",
                       path, _gap_file_timeout())
        try:
            _job_log(jid, f"⏱ {path} gap-recovery timed out (>{_gap_file_timeout():.0f}s) — skipped")
        except Exception:  # noqa: BLE001
            pass
        return {"file_path": path, "ok": False, "applied": False,
                "error": f"timeout>{_gap_file_timeout():.0f}s",
                "gap_report": "", "recovery_plan": ""}


async def _run_gap_recovery_job(
    jid: str, project_id: str, scope: str, model: str,
    only_service: str = None,
    only_services: Optional[List[str]] = None,
):
    """Iterate every generated file in the requested scope and run gap
    recovery on it. ``scope`` ∈ {"backend", "frontend"}.

    iter-13.120 — Multi-select support: ``only_services`` (list of names)
    takes precedence over the legacy scalar ``only_service``.
    """
    try:
        _job_update(jid, status="running", step="Loading generated files…", pct=2)
        # iter-13.120 — coalesce scalar + list.
        only_names: List[str] = []
        if only_services:
            only_names = [s for s in only_services if s]
        elif only_service:
            only_names = [only_service]
        # Filter generated files by scope.
        if scope == "frontend":
            files_q = {"project_id": project_id, "file_path": {"$regex": "^frontend/"}}
            if only_names:
                files_q["service_name"] = (
                    {"$in": only_names} if len(only_names) > 1 else only_names[0]
                )
        elif scope == "backend":
            files_q = {"project_id": project_id, "file_path": {"$regex": "^services/"}}
            if only_names:
                files_q["service_name"] = (
                    {"$in": only_names} if len(only_names) > 1 else only_names[0]
                )
        else:
            _job_finish(jid, "error", error=f"Unknown scope: {scope!r}. Use 'backend' or 'frontend'.")
            return

        files = await codegen_files.find(files_q, {"_id": 0}).to_list(2000)
        # Only operate on real source files — skip dockerfiles / configs / docs
        # that don't carry business logic. (User cares about parity, not configs.)
        # iter-14.36 — EXCLUDE `entity` and `repository` from gap recovery.
        # These are deterministic JPA glue emitted by _java_scaffold() from
        # the OLTP DDL (iter-14.33). Running an LLM gap-recovery pass on
        # them (a) is wasted work — they contain no business rules — and
        # (b) risks the LLM injecting invented predicates into what MUST
        # remain pure ORM mapping. Gap recovery is for controller / service
        # / validator / mapper / exception layers where legacy business
        # logic actually lives. This alone drops workload ~5-6× on typical
        # projects (ceots: 976 → 166 files).
        SOURCE_TYPES = {
            "controller", "service", "dto", "errors",
            "security", "bootstrap", "route", "middleware", "test",
            "mapper", "validator", "exception",
        }
        files = [f for f in files if f.get("file_type") in SOURCE_TYPES]
        if not files:
            _job_finish(jid, "error",
                        error=f"No generated source files found for scope={scope}. Run Generate All first.")
            return

        # Resolve services map for context.
        svc_by_name: Dict[str, dict] = {}
        async for s in arch_services.find({"project_id": project_id}, {"_id": 0}):
            svc_by_name[s["name"]] = s

        total = len(files)
        completed = {"n": 0, "applied": 0, "failed": 0}
        reports: List[dict] = []
        # iter-14.36 — env-tunable concurrency. Local Ollama has no rate
        # limit and can serve 4-8 concurrent 7B/13B requests on modest
        # hardware; cloud providers stay at the historical safe default.
        _sem_default = 4 if "ollama" in ((model or "").lower() + os.environ.get("LAMA_DEFAULT_MODEL", "").lower()) else 2
        try:
            _sem_n = int(os.environ.get("LAMA_GAP_RECOVERY_CONCURRENCY", str(_sem_default)))
        except ValueError:
            _sem_n = _sem_default
        _sem_n = max(1, min(16, _sem_n))
        sem = asyncio.Semaphore(_sem_n)
        _job_log(jid, f"Gap recovery starting: {total} files, concurrency={_sem_n}, model={model or '(default)'}")

        async def worker(fdoc):
            async with sem:
                # iter-13.54 — cooperative pause/stop checkpoint
                await _await_job_control(jid)
                svc = svc_by_name.get(fdoc.get("service_name") or "") or {
                    "name": fdoc.get("service_name", ""),
                    "backend_lang": "any",
                    "tables": [], "api_endpoints": [],
                    "dependencies": [], "frontend": fdoc["file_path"].startswith("frontend/"),
                    "id": "",
                }
                res = await _gap_recover_one_file_bounded(project_id, svc, fdoc, model, jid)
                completed["n"] += 1
                if res["applied"]:
                    completed["applied"] += 1
                else:
                    completed["failed"] += 1
                reports.append({
                    "file_path": res["file_path"],
                    "applied": res["applied"],
                    "error": res.get("error", ""),
                    "gap_report": res.get("gap_report", "")[:4000],
                    "recovery_plan": res.get("recovery_plan", "")[:4000],
                })
                pct = 2 + int((completed["n"] / total) * 95)
                _job_update(jid, step=f"[{completed['n']}/{total}] {fdoc['file_path']}", pct=pct)
                _job_log(jid, f"{'✓' if res['applied'] else '✗'} [{completed['n']}/{total}] {res['file_path']}")

        await asyncio.gather(*[worker(f) for f in files])

        # Persist consolidated Gap Closure Report as a markdown artifact.
        report_md_parts = [
            f"# Gap Closure Report — scope={scope}",
            f"Project: {project_id}",
            f"Model used: `{model}` (DIFF_ANALYSIS_MODEL)",
            f"Files scanned: {total}  |  Applied: {completed['applied']}  |  Failed: {completed['failed']}",
            "",
        ]
        for r in sorted(reports, key=lambda x: x["file_path"]):
            tag = "APPLIED" if r["applied"] else "FAILED"
            report_md_parts.append(f"## [{tag}] {r['file_path']}")
            if r.get("error"):
                report_md_parts.append(f"_error: {r['error']}_\n")
            if r.get("gap_report"):
                report_md_parts.append("### Gaps\n```\n" + r["gap_report"] + "\n```")
            if r.get("recovery_plan"):
                report_md_parts.append("### Recovery Plan\n```\n" + r["recovery_plan"] + "\n```")
            report_md_parts.append("")
        report_md = "\n".join(report_md_parts)

        now = datetime.now(timezone.utc).isoformat()
        report_path = f"_lama/reports/gap-closure-{scope}-{int(__import__('time').time())}.md"
        await codegen_files.update_one(
            {"project_id": project_id, "file_path": report_path},
            {"$set": {
                "id": __import__("uuid").uuid4().hex,
                "project_id": project_id,
                "service_id": "",
                "service_name": "_lama",
                "file_path": report_path,
                "content": report_md,
                "language": "markdown",
                "file_type": "report",
                "version": 1,
                "edited": False,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
        await audit_log.insert_one({
            "action": "codegen.gap_recovery",
            "project_id": project_id, "at": now,
            "details": {
                "scope": scope,
                "only_service": only_service,
                "only_services": only_names,
                "files_total": total, "applied": completed["applied"],
                "failed": completed["failed"], "report_path": report_path,
                "model": model,
            },
        })
        # iter-14.46 — Invalidate the cached CodeGen parity score so the
        # outer pill reflects the freshly-recovered files on the next poll.
        try:
            from routes.pipeline import invalidate_codegen_parity_cache as _inval
            _inval(project_id)
        except Exception:  # noqa: BLE001
            pass
        _job_finish(jid, "complete", step="Done", pct=100, result={
            "scope": scope, "files_total": total,
            "applied": completed["applied"], "failed": completed["failed"],
            "report_path": report_path,
        })
    except JobStopped:
        # iter-13.54 — user clicked Stop in the UI; mark cleanly.
        _job_finish(
            jid, "stopped",
            step=f"Stopped by user (applied {completed['applied']}, failed {completed['failed']})",
            pct=_JOBS.get(jid, {}).get("pct", 0),
            result={"scope": scope, "applied": completed["applied"],
                    "failed": completed["failed"]},
        )
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        logger.exception("gap-recovery job failed")
        _job_finish(jid, "error", error=str(e))


async def _resolve_gap_analysis_model(payload_model: str | None) -> str:
    """Pick a DIFFERENT model than the default codegen one, so the gap
    analysis is genuinely independent (per the iter-13.28 spec).

    iter-13.30 — Console-driven. No hard-coded vendor slugs. Priority:
      1. payload.model (explicit user override).
      2. LAMA_GAP_ANALYSIS_MODEL env-var (ops escape hatch).
      3. The active provider's `routing[other_tier]` — codegen runs at
         the tier `AGENT_COMPLEXITY["codegen.service"]` returns (default
         high); we cross to medium. If that's empty, we walk the
         remaining tiers; if Console is completely unconfigured, return
         "" so `fabric_call` falls back to env-var resolution.
    """
    if payload_model:
        return payload_model
    env = _os.environ.get("LAMA_GAP_ANALYSIS_MODEL", "").strip()
    if env:
        return env
    try:
        from db import model_providers as mp_col
        prov = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0})
        routing = (prov or {}).get("routing") or {}
        from fabric.model_fabric import AGENT_COMPLEXITY
        codegen_tier = AGENT_COMPLEXITY.get("codegen.service", "high")
        cross = {"high": "medium", "medium": "high", "low": "medium"}.get(codegen_tier, "medium")
        alt = (routing.get(cross) or "").strip()
        if alt:
            return alt
        for t in ("high", "medium", "low"):
            if t == codegen_tier:
                continue
            cand = (routing.get(t) or "").strip()
            if cand:
                return cand
    except Exception:
        pass
    return ""  # let fabric_call resolve via agent_key


@router.post("/jobs/start/gap-recovery-backend")
async def start_gap_recovery_backend(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    # iter-13.81.21 — _run_gap_recovery_job requires `model` (added in
    # iter-13.30 for cross-tier independence). Resolve it here from
    # the payload / env / Console routing so the route doesn't 500 with
    # "missing positional argument 'model'".
    model = await _resolve_gap_analysis_model(payload.get("model"))
    # iter-13.120 — multi-select support.
    only_scalar = payload.get("service_name") or None
    only_list = payload.get("service_names")
    if isinstance(only_list, list):
        only_list = [s for s in only_list if isinstance(s, str) and s.strip()]
    else:
        only_list = None
    jid = _new_job(project_id, "gap_recovery_backend")
    asyncio.create_task(_run_gap_recovery_job(
        jid, project_id, "backend", model,
        only_service=only_scalar,
        only_services=only_list,
    ))
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/gap-recovery-frontend")
async def start_gap_recovery_frontend(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    model = await _resolve_gap_analysis_model(payload.get("model"))
    # iter-13.120 — multi-select support (frontend gap-recovery historically
    # took no service filter; now accepts one for parity with the backend
    # endpoint, so users can rerun a single frontend module).
    only_scalar = payload.get("service_name") or None
    only_list = payload.get("service_names")
    if isinstance(only_list, list):
        only_list = [s for s in only_list if isinstance(s, str) and s.strip()]
    else:
        only_list = None
    jid = _new_job(project_id, "gap_recovery_frontend")
    asyncio.create_task(_run_gap_recovery_job(
        jid, project_id, "frontend", model,
        only_service=only_scalar,
        only_services=only_list,
    ))
    return {"job_id": jid, "status": "queued"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return {"id": j["id"], "status": j["status"], "step": j.get("step", ""),
            "pct": j.get("pct", 0), "error": j.get("error"),
            "result": j.get("result", {}), "kind": j.get("kind"),
            # iter-13.54 — surface control flag so UI knows whether to show
            # Pause vs Resume button.
            "control": j.get("control", "running"),
            "log": j.get("log", [])[-20:]}


# iter-13.54 — Pause / Resume / Stop control endpoints.
#
# Cooperative: the per-file coroutines call `_await_job_control(jid)`
# before each LLM round-trip. We just flip the flag; the coroutines pick
# up the change within ~1 second.
@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j.get("status") not in ("queued", "running"):
        raise HTTPException(400, f"Cannot pause a job in '{j.get('status')}' state.")
    if j.get("control") == "stopping":
        raise HTTPException(400, "Job is already stopping — cannot pause.")
    if j.get("control") == "paused":
        return {"ok": True, "control": "paused", "note": "already paused"}
    # iter-13.55 — preserve the *original* step text in a dedicated field
    # so repeated Pause/Resume cycles don't nest "Paused (was: Paused (was: …))".
    j["_step_before_pause"] = j.get("step", "")
    j["control"] = "paused"
    j["step"] = f"⏸ Paused (was: {j['_step_before_pause']})"
    return {"ok": True, "control": "paused"}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j.get("control") != "paused":
        raise HTTPException(400, f"Job is not paused (control={j.get('control')}).")
    j["control"] = "running"
    # iter-13.55 — restore the cached pre-pause step instead of doing a
    # fragile string-replace on the wrapped form.
    if j.get("_step_before_pause"):
        j["step"] = j.pop("_step_before_pause")
    return {"ok": True, "control": "running"}


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j.get("status") in ("complete", "error", "stopped"):
        return {"ok": True, "control": j.get("control"), "note": "already terminal"}
    j["control"] = "stopping"
    # If we were paused, drop the cache — it's no longer meaningful.
    j.pop("_step_before_pause", None)
    j["step"] = "⏹ Stop requested — finishing in-flight calls…"
    return {"ok": True, "control": "stopping"}


# -----------------------------------------------------------
# C/D/E. Files CRUD
# -----------------------------------------------------------
# iter-13.122 — Self-healing arch_services backfill.
# Reads `stage_context.Architecture.outputs.services` (which is written by
# `_promote_architecture_stage` at freeze time and always contains the
# full per-service catalog) and re-inserts every service row into the
# `arch_services` collection when it's found empty.
#
# Why: projects frozen BEFORE the iter-13.122 approve fix can have their
# `arch_services` collection wiped (the previous approve pruner deleted
# the frontend row + edge cases could delete business services). Rather
# than force the user to Reset + rebuild Stage 3, we restore from the
# frozen StageContext snapshot on-demand.
async def _backfill_arch_services_from_stage_context(project_id: str) -> int:
    ctx = await stage_context_col.find_one(
        {"project_id": project_id, "stage": "Architecture"}, {"_id": 0},
    )
    if not ctx:
        return 0
    svcs = ((ctx.get("outputs") or {}).get("services") or [])
    if not isinstance(svcs, list) or not svcs:
        return 0
    # Safety net: someone else may have written a row concurrently.
    existing = await arch_services.count_documents({"project_id": project_id})
    if existing:
        return 0
    import uuid as _uuid
    now_iso = datetime.now(timezone.utc).isoformat()
    to_insert: List[dict] = []
    for s in svcs:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        row = {k: v for k, v in s.items() if k != "_id"}
        row.setdefault("id", _uuid.uuid4().hex)
        row["project_id"] = project_id
        row.setdefault("created_at", now_iso)
        row.setdefault("updated_at", now_iso)
        row.setdefault("status", "pending")
        row.setdefault("codegen_status", "pending")
        row.setdefault("frontend", False)
        to_insert.append(row)
    if not to_insert:
        return 0
    try:
        await arch_services.insert_many(to_insert)
    except Exception as _e:
        logger.warning("arch_services backfill failed for %s: %s", project_id, _e)
        return 0
    await audit_log.insert_one({
        "action":     "codegen.arch_services.backfill",
        "project_id": project_id,
        "at":         now_iso,
        "details":    {"restored_count": len(to_insert),
                       "source": "stage_context.Architecture.outputs.services"},
    })
    return len(to_insert)


@router.get("/{project_id}/files")
async def list_files(project_id: str):
    docs = await codegen_files.find({"project_id": project_id},
                                    {"_id": 0, "content": 0}).sort("file_path", 1).to_list(2000)
    by_svc: Dict[str, List[dict]] = {}
    for d in docs:
        by_svc.setdefault(d.get("service_name", "_root"), []).append({
            "id": d["id"], "path": d["file_path"], "language": d.get("language", "text"),
            "file_type": d.get("file_type", "other"), "version": d.get("version", 1),
            "edited": d.get("edited", False),
            "gap_stats": d.get("gap_stats") or None,
        })
    services = [{"name": k, "files": v} for k, v in by_svc.items()]
    # iter-13.120 — Also surface the Architecture service catalog so the
    # CodeGen page can render the multi-select popover BEFORE any code has
    # been generated (before any codegen_files row exists). Each entry
    # carries just enough metadata (frontend flag, backend_lang, kind) for
    # the UI to render badges + a frontend-missing warning.
    arch_docs = await arch_services.find(
        {"project_id": project_id},
        {"_id": 0, "name": 1, "frontend": 1, "backend_lang": 1,
         "kind": 1, "display_name": 1, "pattern": 1},
    ).sort("name", 1).to_list(200)
    # iter-13.122 — Self-healing backfill. If arch_services is empty but
    # the Architecture StageContext exists (i.e. the stage was frozen
    # before the iter-13.122 approve fix, or a pruning regression wiped
    # the collection), re-populate arch_services from
    # `stage_context.Architecture.outputs.services` so CodeGen can
    # proceed without forcing the user to Reset + re-freeze Stage 3.
    if not arch_docs:
        restored = await _backfill_arch_services_from_stage_context(project_id)
        if restored > 0:
            arch_docs = await arch_services.find(
                {"project_id": project_id},
                {"_id": 0, "name": 1, "frontend": 1, "backend_lang": 1,
                 "kind": 1, "display_name": 1, "pattern": 1},
            ).sort("name", 1).to_list(200)
    arch_out = [
        {
            "name": a.get("name", ""),
            "display_name": a.get("display_name") or a.get("name", ""),
            "frontend": bool(a.get("frontend")),
            "backend_lang": a.get("backend_lang", ""),
            "kind": a.get("kind", "service"),
        }
        for a in arch_docs
        if a.get("name")
    ]
    # iter-13.121 — surface the architecture pattern so the CodeGen UI
    # can render a badge (Pattern: microservices | modular_monolith |
    # monolith). Pick the first non-empty pattern seen; ignore the
    # frontend-service's `pattern="frontend"` sentinel.
    _p = ""
    for _s in arch_docs:
        _pv = str(_s.get("pattern") or "").strip().lower()
        if _pv and _pv not in ("frontend", "merged"):
            _p = _pv
            break
    return {
        "services": services,
        "total_files": len(docs),
        "arch_services": arch_out,
        "pattern": _p or "microservices",
    }


# iter-13.122 — Legacy → New API mapping preview.
# Deterministic (no LLM). Sources `legacy_routes` from
# `arch_services.routes_detail` (populated during Architecture recommend
# from the KB OWL extraction — verb/path/class/module of the legacy
# controller methods) and `new_endpoints` from
# `arch_services.api_endpoints` (the target REST surface the LLM proposed
# for the new service).
# Rendered on the CodeGen page BEFORE the user hits Generate so they can
# validate that every legacy PHP/JSP route is covered by a new endpoint.
@router.get("/{project_id}/api-mapping")
async def get_api_mapping(project_id: str):
    svcs = await arch_services.find(
        {"project_id": project_id},
        {"_id": 0, "name": 1, "display_name": 1, "frontend": 1, "kind": 1,
         "backend_lang": 1, "routes_detail": 1, "api_endpoints": 1,
         "merged_from": 1},
    ).sort("name", 1).to_list(200)
    # iter-13.122 — apply the same self-healing backfill as /files so the
    # mapping preview also works on projects frozen before iter-13.122.
    if not svcs:
        if await _backfill_arch_services_from_stage_context(project_id) > 0:
            svcs = await arch_services.find(
                {"project_id": project_id},
                {"_id": 0, "name": 1, "display_name": 1, "frontend": 1, "kind": 1,
                 "backend_lang": 1, "routes_detail": 1, "api_endpoints": 1,
                 "merged_from": 1},
            ).sort("name", 1).to_list(200)
    out: List[dict] = []
    for s in svcs:
        legacy = []
        for rd in (s.get("routes_detail") or []):
            if not isinstance(rd, dict):
                continue
            verb = (rd.get("verb") or "ANY").upper()
            path = rd.get("path") or ""
            if not path:
                continue
            legacy.append({
                "verb":   verb,
                "path":   path,
                "class":  rd.get("class") or "",
                "module": rd.get("module") or "",
                "roles":  rd.get("roles") or [],
            })
        new_eps = []
        for ep in (s.get("api_endpoints") or []):
            if not ep:
                continue
            # api_endpoints are stored as "VERB /path" strings.
            parts = str(ep).strip().split(None, 1)
            if len(parts) == 2:
                new_eps.append({"verb": parts[0].upper(), "path": parts[1]})
            else:
                new_eps.append({"verb": "ANY", "path": parts[0]})
        out.append({
            "name":          s.get("name", ""),
            "display_name":  s.get("display_name") or s.get("name", ""),
            "frontend":      bool(s.get("frontend")),
            "kind":          s.get("kind", ""),
            "backend_lang":  s.get("backend_lang", ""),
            "merged_from":   s.get("merged_from") or [],
            "legacy_routes": legacy,
            "new_endpoints": new_eps,
            "legacy_count":  len(legacy),
            "new_count":     len(new_eps),
        })
    totals = {
        "services":       len(out),
        "legacy_total":   sum(x["legacy_count"] for x in out),
        "new_total":      sum(x["new_count"] for x in out),
    }
    return {"services": out, "totals": totals}


@router.get("/{project_id}/file/{file_id}")
async def get_file(project_id: str, file_id: str):
    d = await codegen_files.find_one({"project_id": project_id, "id": file_id}, {"_id": 0})
    if not d:
        raise HTTPException(404, "File not found")
    return d


@router.put("/{project_id}/file/{file_id}")
async def update_file(project_id: str, file_id: str, payload: dict):
    d = await codegen_files.find_one({"project_id": project_id, "id": file_id}, {"_id": 0})
    if not d:
        raise HTTPException(404, "File not found")
    new_content = payload.get("content", "")
    now = datetime.now(timezone.utc).isoformat()
    await codegen_files.update_one(
        {"project_id": project_id, "id": file_id},
        {"$set": {"content": new_content, "version": d.get("version", 1) + 1,
                  "edited": True, "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "codegen.file.update",
        "project_id": project_id, "at": now,
        "details": {"file_path": d["file_path"], "version": d.get("version", 1) + 1},
    })
    return {"ok": True}


# -----------------------------------------------------------
# E.1 — delete single file / delete by folder path-prefix
# (iter-13.56) supports right-click context menu in CodeGen tree
# -----------------------------------------------------------
@router.delete("/{project_id}/file/{file_id}")
async def delete_file(project_id: str, file_id: str):
    d = await codegen_files.find_one({"project_id": project_id, "id": file_id}, {"_id": 0})
    if not d:
        raise HTTPException(404, "File not found")
    await codegen_files.delete_one({"project_id": project_id, "id": file_id})
    now = datetime.now(timezone.utc).isoformat()
    await audit_log.insert_one({
        "action": "codegen.file.delete",
        "project_id": project_id, "at": now,
        "details": {"file_path": d.get("file_path"), "service_name": d.get("service_name")},
    })
    return {"ok": True, "deleted": 1, "file_path": d.get("file_path")}


@router.post("/{project_id}/delete-path")
async def delete_path(project_id: str, payload: dict):
    """Delete all codegen files matching a folder path prefix.

    Body: { path_prefix: str, service_name?: str }
    """
    prefix = (payload or {}).get("path_prefix", "").strip().strip("/")
    service_name = (payload or {}).get("service_name")
    if not prefix:
        raise HTTPException(400, "path_prefix required")
    q: Dict[str, Any] = {
        "project_id": project_id,
        "$or": [
            {"file_path": prefix},
            {"file_path": {"$regex": f"^{re.escape(prefix)}/"}},
        ],
    }
    if service_name:
        q["service_name"] = service_name
    res = await codegen_files.delete_many(q)
    now = datetime.now(timezone.utc).isoformat()
    await audit_log.insert_one({
        "action": "codegen.path.delete",
        "project_id": project_id, "at": now,
        "details": {"path_prefix": prefix, "service_name": service_name,
                    "deleted": res.deleted_count},
    })
    return {"ok": True, "deleted": res.deleted_count, "path_prefix": prefix}


# -----------------------------------------------------------
# F. download zip
# -----------------------------------------------------------
@router.post("/{project_id}/download-zip")
async def download_zip(project_id: str):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    files = await codegen_files.find({"project_id": project_id}, {"_id": 0}).to_list(5000)
    if not files:
        raise HTTPException(400, "No files to package. Generate code first.")
    blob = build_zip(proj.get("name", "lama"), files)
    fn = (proj.get("name", "lama") or "lama").lower().replace(" ", "_") + ".zip"
    return StreamingResponse(io.BytesIO(blob), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# -----------------------------------------------------------
# F.2 — Export to disk (iter-13.110)
# Writes the generated frontend + backend trees to a real folder OUTSIDE
# the LAMA repo. Default root: <parent-of-lama-main>/tanent-project-data/
# Override with LAMA_EXPORT_ROOT env-var (absolute path).
# -----------------------------------------------------------
@router.post("/{project_id}/export-to-disk")
async def export_to_disk(project_id: str):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    files = await codegen_files.find({"project_id": project_id}, {"_id": 0}).to_list(5000)
    if not files:
        raise HTTPException(400, "No files to export. Generate code first.")
    try:
        manifest = export_project_to_disk(
            project_name=proj.get("name", "lama"),
            files=files,
        )
    except PermissionError as e:
        raise HTTPException(500, f"Export root not writable: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Export failed: {e}")
    try:
        await audit_log.insert_one({
            "action": "codegen.export_to_disk",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "project_root":   manifest["project_root"],
                "frontend_files": manifest["frontend_files"],
                "backend_files":  manifest["backend_files"],
                "skipped":        len(manifest.get("skipped") or []),
            },
        })
    except Exception:  # noqa: BLE001
        pass
    return manifest


@router.get("/export-root")
async def export_root_info(project_id: str = ""):
    """Resolve the configured export root so the UI can show the user
    WHERE the next export-to-disk run will land before they click.

    When ``project_id`` is supplied, also returns the full per-project
    folder path (e.g. ``/Users/.../Aarogyasri-TJHS-App``) so the
    confirmation dialog can preview the exact destination.
    """
    try:
        root = str(resolve_export_root())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Cannot resolve export root: {e}")
    import os as _os
    out: Dict[str, Any] = {
        "export_root": root,
        "env_override": (_os.environ.get("LAMA_EXPORT_ROOT") or "").strip(),
    }
    pid = (project_id or "").strip()
    if pid:
        proj = await projects.find_one({"id": pid}, {"_id": 0, "name": 1})
        if proj:
            try:
                out["project_root"] = str(project_export_path(proj.get("name", "lama")))
                out["project_name"] = proj.get("name", "")
            except Exception:  # noqa: BLE001
                pass
    return out


# -----------------------------------------------------------
# G. push to GitHub (job)
# -----------------------------------------------------------
async def _run_github_push_job(jid: str, project_id: str):
    try:
        # Source of truth = the project document (written by /api/github/config
        # from the Settings tab). Fall back to the legacy `github_configs`
        # collection for older projects so we don't break existing data.
        from db import projects as _projects
        proj = await _projects.find_one({"id": project_id}, {"_id": 0}) or {}
        repo_url = proj.get("github_repo", "")
        token = proj.get("github_token", "")
        username = proj.get("github_username", "")
        password = proj.get("github_password", "")
        branch = proj.get("github_branch", "main")

        if not repo_url or not (token or (username and password)):
            try:
                from db import github_configs
                legacy = await github_configs.find_one({"project_id": project_id}, {"_id": 0}) or {}
            except Exception:
                legacy = {}
            repo_url = repo_url or legacy.get("repo_url", "")
            token = token or legacy.get("token", "")
            username = username or legacy.get("username", "")
            password = password or legacy.get("password", "")
            if not branch or branch == "main":
                branch = legacy.get("branch", branch or "main")

        if not repo_url or not (token or (username and password)):
            _job_finish(
                jid,
                "error",
                error=(
                    "GitHub not configured for this project. Open "
                    "Settings & GitHub → save a repo URL and either a "
                    "Personal Access Token or a username + password."
                ),
            )
            return

        # Reuse the SSL-verify policy from routes/github.py so corporate
        # proxies (LAMA_DISABLE_SSL_VERIFY / LAMA_CA_BUNDLE) work the same
        # way as the SRS push button on the Settings tab.
        from routes.github import _ssl_verify
        verify = _ssl_verify()

        from github import Github
        from urllib.parse import urlparse

        parsed = urlparse(repo_url)
        repo_full = parsed.path.strip("/").replace(".git", "")
        if token:
            gh = Github(token, verify=verify)
            auth_method = "token"
        else:
            gh = Github(username, password, verify=verify)
            auth_method = "basic"
        repo = gh.get_repo(repo_full)

        files = await codegen_files.find({"project_id": project_id}, {"_id": 0}).to_list(5000)
        if not files:
            _job_finish(jid, "error", error="No files to push.")
            return

        last_sha = ""
        for i, f in enumerate(files):
            base_pct = 2 + int((i / len(files)) * 95)
            _job_update(jid, step=f"[{i+1}/{len(files)}] {f['file_path']}", pct=base_pct)
            try:
                existing = repo.get_contents(f["file_path"], ref=branch)
                r = repo.update_file(f["file_path"], f"LAMA codegen: {f['file_path']}",
                                     f["content"], existing.sha, branch=branch)
            except Exception:
                r = repo.create_file(f["file_path"], f"LAMA codegen: {f['file_path']}",
                                     f["content"], branch=branch)
            try:
                last_sha = r["commit"].sha if r and r.get("commit") else last_sha
            except Exception:
                last_sha = last_sha
        await codegen_runs.update_one({"id": jid}, {"$set": {"github_commit": last_sha}})
        _job_finish(
            jid,
            "complete",
            step="Done",
            pct=100,
            result={
                "commit_sha": last_sha,
                "repo_url": repo_url,
                "files": len(files),
                "auth_method": auth_method,
            },
        )
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/github-push")
async def start_github_push(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    jid = _new_job(project_id, "github_push")
    asyncio.create_task(_run_github_push_job(jid, project_id))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# H. chat
# -----------------------------------------------------------
@router.post("/chat")
async def codegen_chat(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    file_id = payload.get("file_id")
    service_name = payload.get("service_name")
    conv_id = payload.get("conversation_id")
    session_id = (payload.get("session_id") or "").strip()  # iter-13.100

    await require_stage_context(project_id, "Architecture", "CodeGen")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})

    file_doc = None
    related_context = ""
    file_path = ""
    if file_id:
        file_doc = await codegen_files.find_one({"project_id": project_id, "id": file_id}, {"_id": 0})
        if file_doc:
            file_path = file_doc["file_path"]
    if service_name:
        peers = await codegen_files.find({"project_id": project_id, "service_name": service_name},
                                         {"_id": 0}).to_list(20)
        related_context = "\n\n".join(f"--- {p['file_path']} ---\n{p['content'][:1500]}" for p in peers[:5])

    try:
        rag = await qdrant_search(project_id, message, top_k=5)
    except Exception:
        rag = []
    rag_context = "\n\n".join(rag)[:3000]

    template = await _get_prompt(project_id, "codegen.chat")
    sp = _safe_format(
        template,
        project_name=proj.get("name", ""),
        file_path=file_path or "(no file selected)",
        current_content=(file_doc.get("content", "") if file_doc else "")[:6000],
        related_context=related_context[:5000],
        rag_context=rag_context,
        message=message,
    )

    if not conv_id:
        conv_id = __import__("uuid").uuid4().hex
        await conversations.insert_one({"id": conv_id, "project_id": project_id, "stage": "CodeGen",
                                        "created_at": datetime.now(timezone.utc).isoformat()})
    await messages_col.insert_one({
        "id": __import__("uuid").uuid4().hex, "conversation_id": conv_id, "project_id": project_id,
        "role": "user", "content": message, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        # iter-13.100 — route through rolling-memory session when supplied.
        if session_id:
            r = await fabric_call_with_session(
                messages=[{"role": "system", "content": sp}, {"role": "user", "content": message}],
                session_id=session_id,
                agent_key="codegen.chat",
                project_id=project_id,
                model=model, temperature=0.15, max_tokens=6000, timeout=180.0,
            )
        else:
            r = await chat_completion(
                messages=[{"role": "system", "content": sp}, {"role": "user", "content": message}],
                model=model, temperature=0.15, max_tokens=6000, timeout=180.0,
            )
    except Exception as e:
        raise HTTPException(502, f"LLM failed: {e}")
    content = r.get("content", "") or ""

    file_changes = []
    for m in re.finditer(r"\[FILE_CHANGE:([^\]]+)\]\s*(.*?)\s*\[/FILE_CHANGE\]", content, re.DOTALL):
        path = m.group(1).strip()
        new_content = m.group(2).strip()
        target = await codegen_files.find_one({"project_id": project_id, "file_path": path}, {"_id": 0})
        file_changes.append({
            "file_id": (target or {}).get("id"),
            "file_path": path,
            "old_content": (target or {}).get("content", ""),
            "new_content": new_content,
        })

    msg_id = __import__("uuid").uuid4().hex
    await messages_col.insert_one({
        "id": msg_id, "conversation_id": conv_id, "project_id": project_id,
        "role": "assistant", "content": content, "model": model,
        "tokens": r.get("usage", {}).get("total_tokens", 0),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"conversation_id": conv_id, "message_id": msg_id, "content": content, "file_changes": file_changes}


@router.post("/{project_id}/apply-file-change")
async def apply_file_change(project_id: str, payload: dict):
    file_id = payload.get("file_id")
    new_content = payload.get("new_content", "")
    if not file_id:
        raise HTTPException(400, "file_id required")
    d = await codegen_files.find_one({"project_id": project_id, "id": file_id}, {"_id": 0})
    if not d:
        raise HTTPException(404, "File not found")
    now = datetime.now(timezone.utc).isoformat()
    await codegen_files.update_one(
        {"project_id": project_id, "id": file_id},
        {"$set": {"content": new_content, "version": d.get("version", 1) + 1,
                  "edited": True, "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "codegen.chat.apply",
        "project_id": project_id, "at": now,
        "details": {"file_path": d["file_path"], "msg_id": payload.get("conversation_message_id")},
    })
    return {"ok": True}


# Reset
@router.post("/{project_id}/reset")
async def reset_codegen(project_id: str):
    deleted = {
        "codegen_files": (await codegen_files.delete_many({"project_id": project_id})).deleted_count,
        "codegen_runs": (await codegen_runs.delete_many({"project_id": project_id})).deleted_count,
    }
    now = datetime.now(timezone.utc).isoformat()
    await stage_context_col.delete_one({"project_id": project_id, "stage": "CodeGen"})
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.CodeGen": "available", "stage_status.Living": "locked", "updated_at": now}},
    )
    await audit_log.insert_one({"action": "codegen.reset", "project_id": project_id, "at": now, "details": deleted})
    return {"ok": True, "deleted": deleted}


# Freeze entire CodeGen → unlock Living
@router.post("/{project_id}/freeze")
async def freeze_codegen(project_id: str):
    files_count = await codegen_files.count_documents({"project_id": project_id})
    if files_count == 0:
        raise HTTPException(400, "Generate code first.")

    # iter-13.30 — Thumb rule: 100% legacy BRs must appear in generated
    # source (typically as `// BR-NN` comment annotations the codegen
    # prompts are seeded to emit). Concatenates every generated file's
    # body and checks token presence. Hard-blocks when LAMA_BR_ENFORCE=1.
    from kb.br_tracker import assert_coverage_or_warn, BRCoverageError
    file_texts: list[str] = []
    cur = codegen_files.find(
        {"project_id": project_id},
        {"_id": 0, "content": 1},
    )
    async for f in cur:
        file_texts.append(f.get("content", "") or "")
    try:
        br_coverage = await assert_coverage_or_warn(project_id, "codegen", file_texts)
    except BRCoverageError as bce:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "BR_COVERAGE_BELOW_THRESHOLD",
                "stage": "codegen",
                "message": str(bce),
                "coverage": bce.coverage,
            },
        )

    services = await arch_services.find({"project_id": project_id}, {"_id": 0}).to_list(100)
    backend_langs = sorted({s.get("backend_lang", "nodejs") for s in services if not s.get("frontend")})
    outputs = {
        "total_files": files_count,
        "services_generated": len(services),
        "frontend_framework": "react",
        "backend_langs": backend_langs,
        "zip_available": True,
        "br_coverage": br_coverage,
    }
    sources = {"prompts_used": ["codegen.service", "codegen.frontend", "codegen.docs"]}
    await save_stage_context(project_id, "CodeGen", outputs, sources, frozen_by="user")
    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.CodeGen": "frozen", "stage_status.Living": "available", "updated_at": now}},
    )
    return {"ok": True, "br_coverage": br_coverage}


# ════════════════════════════════════════════════════════════════════
# iter-13.119 — AUTOMATED VALIDATION + IMPROVEMENT LOOP
# ────────────────────────────────────────────────────────────────────
# "Continue generating/refining until confidence ≥ 95%."
#
# Pipeline (per iteration):
#   1. Score every generated source file via `parity_loop.score_run()`
#      across 6 axes — structural / parity / coverage / schema / evidence
#      / requirement. Roll up to per-service and per-run confidence %.
#   2. If run_score >= threshold (default 95) → DONE.
#   3. Otherwise, pick the worst-scoring recoverable files via
#      `parity_loop.select_recovery_targets()` and run the existing
#      `_gap_recover_one_file()` on each (concurrent, bounded).
#   4. Re-score → next iteration. Cap at `max_iterations` (default 5)
#      so a stubbornly-low-evidence project can't burn unlimited tokens.
#
# Persisted artifacts:
#   • `parity_runs` collection — one doc per auto-validate run with the
#     per-iteration trajectory + final report.
#   • `_lama/reports/auto-validate-<ts>.md` — codegen_files entry the
#     user can ZIP / GitHub-push along with the generated code.
#   • `audit_log` row keyed by `codegen.auto_validate`.
#
# Job control: reuses the same `_new_job` / `_await_job_control` /
# pause/resume/stop infrastructure as Generate All and Gap Recovery, so
# the UI's existing job-bar machinery just works.
# ════════════════════════════════════════════════════════════════════
async def _run_auto_validate_job(
    jid: str,
    project_id: str,
    threshold: float,
    max_iterations: int,
    max_files_per_iter: int,
    only_service: Optional[str],
    model: str,
):
    """Iterative validate→regenerate loop until confidence ≥ threshold."""
    # iter-13.119 — Immediately flip the job out of "queued" so the UI
    # progress bar doesn't appear stuck at 0% while we're still setting up
    # (parity_runs upsert, Mongo round-trips, etc.). Any exception here is
    # caught below and surfaced as job.status="error" with the message.
    _job_update(
        jid,
        status="running",
        step=f"Initialising auto-validate (threshold={threshold:.1f}%)…",
        pct=1,
    )
    _job_log(jid, "━━ Auto-Validate started ━━")
    _job_log(jid, f"project={project_id} threshold={threshold:.1f}% "
                  f"max_iter={max_iterations} max_files/iter={max_files_per_iter} "
                  f"only_service={only_service or '*'} model={model or '(console-routed)'}")

    try:
        _job_log(jid, "importing parity_loop…")
        from codegen.parity_loop import score_run, select_recovery_targets
        _job_log(jid, "✓ parity_loop ready")
    except Exception as _ie:
        _job_log(jid, f"✗ import failed: {_ie}")
        _job_finish(jid, "error", error=f"import codegen.parity_loop failed: {_ie}")
        return

    completed = {"applied": 0, "failed": 0}
    iterations_log: List[Dict[str, Any]] = []
    run_started = datetime.now(timezone.utc).isoformat()

    # Persist the run row up front so the UI / Audit can see it in
    # flight (status="running"). We update progress on every iteration.
    run_doc = {
        "id": jid,
        "project_id": project_id,
        "kind": "auto_validate",
        "status": "running",
        "threshold": threshold,
        "max_iterations": max_iterations,
        "max_files_per_iter": max_files_per_iter,
        "only_service": only_service,
        "model_used": model or "",
        "started_at": run_started,
        "ended_at": None,
        "iterations": [],
        "final_score": 0.0,
        "converged": False,
    }
    try:
        _job_log(jid, "writing parity_runs row…")
        await asyncio.wait_for(
            parity_runs.update_one({"id": jid}, {"$set": run_doc}, upsert=True),
            timeout=10.0,
        )
        _job_log(jid, "✓ parity_runs row written")
    except asyncio.TimeoutError:
        _job_log(jid, "⚠ parity_runs upsert timed out after 10s — continuing without persistence")
    except Exception as _e:  # noqa: BLE001
        _job_log(jid, f"⚠ parity_runs upsert failed: {_e!r} — continuing")
        logger.warning("parity_runs upsert failed at start: %s", _e)

    try:
        _job_update(jid, step="Checking Architecture stage…", pct=2)
        _job_log(jid, "checking Architecture frozen…")
        await asyncio.wait_for(
            require_stage_context(project_id, "Architecture", "CodeGen"),
            timeout=15.0,
        )
        _job_log(jid, "✓ Architecture frozen")

        # ── Resolve services for gap_recovery context ──────────────
        _job_update(jid, step="Loading arch_services…", pct=3)
        _job_log(jid, "loading arch_services…")
        svc_by_name: Dict[str, dict] = {}
        try:
            svc_list = await asyncio.wait_for(
                arch_services.find({"project_id": project_id}, {"_id": 0}).to_list(500),
                timeout=15.0,
            )
            for s in svc_list:
                svc_by_name[s["name"]] = s
            _job_log(jid, f"✓ loaded {len(svc_by_name)} arch_services")
        except asyncio.TimeoutError:
            _job_log(jid, "⚠ arch_services load timed out — proceeding with empty service map")

        # ── Iteration loop ─────────────────────────────────────────
        for it in range(1, max_iterations + 1):
            await _await_job_control(jid)
            _job_update(jid, step=f"Iter {it}/{max_iterations} — scoring files…",
                        pct=2 + int(((it - 1) / max_iterations) * 95))
            _job_log(jid, f"━━ Iter {it}/{max_iterations} ━━")
            _job_log(jid, f"scoring all files (threshold={threshold:.1f}%, only_service={only_service or '*'})…")

            # Score — deterministic, can take ~5-30s on a large project
            # (no LLM, just file scanning + regex/AST). Wrap in wait_for
            # so a runaway scorer doesn't freeze the whole pipeline.
            _t0 = datetime.now(timezone.utc)
            try:
                report = await asyncio.wait_for(
                    score_run(
                        project_id, iteration=it,
                        threshold=threshold,
                        only_service=only_service,
                    ),
                    timeout=180.0,
                )
            except asyncio.TimeoutError:
                _job_log(jid, "✗ score_run timed out after 180s — bailing this iteration")
                raise HTTPException(500, "score_run timed out after 180s")
            _ms = int((datetime.now(timezone.utc) - _t0).total_seconds() * 1000)
            _job_log(jid, f"✓ scored in {_ms}ms — overall={report.overall_score:.2f}% "
                          f"services={len(report.services)} below={len(report.files_below_threshold)}")
            # iter-13.119 — Persist per-service AND per-file detail so the
            # Report dashboard can render a live, drill-down view (FE
            # services, BE services, per-file axis breakdown, per-file
            # issue list). We also tag each service as frontend / backend
            # using arch_services.frontend so the UI can split the columns.
            services_detail: List[Dict[str, Any]] = []
            for s in report.services:
                arch_svc = svc_by_name.get(s.service) or {}
                is_fe = bool(arch_svc.get("frontend", False))
                files_detail = []
                for f in s.files:
                    files_detail.append({
                        "file_path": f.file_path,
                        "file_type": f.file_type,
                        "score": round(f.score, 2),
                        "recoverable": f.recoverable,
                        "issues": f.issues,
                        "components": [
                            {"name": c.name, "score": round(c.score, 2),
                             "weight": c.weight, "detail": c.detail}
                            for c in f.components
                        ],
                    })
                services_detail.append({
                    "service": s.service,
                    "frontend": is_fe,
                    "score": round(s.score, 2),
                    "file_count": s.file_count,
                    "endpoint_coverage_pct": round(s.endpoint_coverage_pct, 2),
                    "table_coverage_pct": round(s.table_coverage_pct, 2),
                    "column_coverage_pct": round(s.column_coverage_pct, 2),
                    "files": files_detail,
                })

            iter_row = {
                "iteration": it,
                "overall_score": round(report.overall_score, 2),
                "files_below_threshold": len(report.files_below_threshold),
                "below_files": report.files_below_threshold[:50],  # for the modal
                "services": services_detail,
                "summary": report.summary,
            }
            iterations_log.append(iter_row)
            _job_log(jid, f"iter={it} score={report.overall_score:.2f}% below={len(report.files_below_threshold)}")
            # Per-service score breakdown — one line per service so the
            # Live Log shows which services pulled the average down.
            for sd in sorted(services_detail, key=lambda x: x["score"])[:8]:
                tag = "FE" if sd["frontend"] else "BE"
                _job_log(jid, f"   [{tag}] {sd['service']}: {sd['score']:.1f}% "
                              f"({sd['file_count']} files, ep={sd['endpoint_coverage_pct']:.0f}%, "
                              f"tbl={sd['table_coverage_pct']:.0f}%, col={sd['column_coverage_pct']:.0f}%)")

            # Persist progress
            await parity_runs.update_one(
                {"id": jid},
                {"$set": {
                    "iterations": iterations_log,
                    "final_score": round(report.overall_score, 2),
                }},
            )

            # Converged?
            if report.overall_score >= threshold:
                run_doc["converged"] = True
                _job_log(jid, f"✓ converged at iter={it} ({report.overall_score:.2f}% ≥ {threshold:.1f}%)")
                break

            # Last iteration with still-below score? Bail out without
            # running another (pointless) gap-recovery pass.
            if it == max_iterations:
                _job_log(jid, f"⚠ max_iterations reached at score={report.overall_score:.2f}%")
                break

            # Pick worst files and run gap-recovery on them
            targets = select_recovery_targets(report, max_files_per_iter=max_files_per_iter)
            if not targets:
                _job_log(jid, "no recoverable files below threshold — bailing")
                break
            _job_log(jid, f"picked {len(targets)} worst file(s) for gap-recovery (cap={max_files_per_iter})")
            for t in targets[:10]:
                _job_log(jid, f"   queued → {t['file_path']} ({t['score']:.0f}%)")
            if len(targets) > 10:
                _job_log(jid, f"   … +{len(targets) - 10} more")

            # Resolve full file_docs for each target
            target_paths = [t["file_path"] for t in targets]
            file_docs = await codegen_files.find(
                {"project_id": project_id, "file_path": {"$in": target_paths}},
                {"_id": 0},
            ).to_list(len(target_paths))
            path_to_doc = {d["file_path"]: d for d in file_docs}

            _job_update(jid,
                        step=f"Iter {it}/{max_iterations} — regenerating {len(targets)} worst files "
                             f"(score={report.overall_score:.1f}% < {threshold:.1f}%)…",
                        pct=2 + int(((it - 0.5) / max_iterations) * 95))

            sem = asyncio.Semaphore(2)

            async def _recover(tgt):
                async with sem:
                    await _await_job_control(jid)
                    fdoc = path_to_doc.get(tgt["file_path"])
                    if not fdoc:
                        completed["failed"] += 1
                        _job_log(jid, f"✗ iter{it} {tgt['file_path']} — file not found")
                        return
                    svc_name = fdoc.get("service_name") or ""
                    svc = svc_by_name.get(svc_name) or {
                        "name": svc_name, "backend_lang": "any",
                        "tables": [], "api_endpoints": [],
                        "dependencies": [],
                        "frontend": fdoc["file_path"].startswith("frontend/"),
                        "id": "",
                    }
                    _job_log(jid, f"→ iter{it} recovering {fdoc['file_path']} (score={tgt['score']:.0f}%)…")
                    res = await _gap_recover_one_file_bounded(project_id, svc, fdoc, model, jid)
                    if res.get("applied"):
                        completed["applied"] += 1
                        _job_log(jid, f"✓ iter{it} {res['file_path']} — applied")
                    else:
                        completed["failed"] += 1
                        _job_log(jid, f"✗ iter{it} {res['file_path']} {res.get('error','')[:80]}")

            await asyncio.gather(*[_recover(t) for t in targets])
            iter_row["recovery"] = {
                "targets": len(targets),
                "applied": completed["applied"],
                "failed": completed["failed"],
            }

        # Final score (re-score one more time if last iter just ran recovery)
        # Avoid double-scoring on the converged path.
        if not run_doc["converged"]:
            final_report = await score_run(
                project_id, iteration=len(iterations_log) + 1,
                threshold=threshold, only_service=only_service,
            )
            # Same enriched shape as in-loop iterations so the dashboard
            # can render the final state with full drill-down.
            final_services_detail: List[Dict[str, Any]] = []
            for s in final_report.services:
                arch_svc = svc_by_name.get(s.service) or {}
                final_services_detail.append({
                    "service": s.service,
                    "frontend": bool(arch_svc.get("frontend", False)),
                    "score": round(s.score, 2),
                    "file_count": s.file_count,
                    "endpoint_coverage_pct": round(s.endpoint_coverage_pct, 2),
                    "table_coverage_pct": round(s.table_coverage_pct, 2),
                    "column_coverage_pct": round(s.column_coverage_pct, 2),
                    "files": [
                        {
                            "file_path": f.file_path,
                            "file_type": f.file_type,
                            "score": round(f.score, 2),
                            "recoverable": f.recoverable,
                            "issues": f.issues,
                            "components": [
                                {"name": c.name, "score": round(c.score, 2),
                                 "weight": c.weight, "detail": c.detail}
                                for c in f.components
                            ],
                        } for f in s.files
                    ],
                })
            iterations_log.append({
                "iteration": len(iterations_log) + 1,
                "overall_score": round(final_report.overall_score, 2),
                "files_below_threshold": len(final_report.files_below_threshold),
                "below_files": final_report.files_below_threshold[:50],
                "services": final_services_detail,
                "summary": final_report.summary + " (final re-score)",
            })
            run_doc["converged"] = final_report.overall_score >= threshold
        else:
            final_report = None

        final_score = iterations_log[-1]["overall_score"]
        run_doc["final_score"] = final_score
        run_doc["iterations"] = iterations_log
        run_doc["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_doc["status"] = "complete"

        # ── Persist markdown report ────────────────────────────────
        import time as _t
        ts = int(_t.time())
        report_path = f"_lama/reports/auto-validate-{ts}.md"
        verdict = "PASS ✓" if run_doc["converged"] else "BELOW THRESHOLD ⚠"
        md_lines: List[str] = [
            f"# Auto-Validate & Improve Report — {verdict}",
            "",
            f"- Project: `{project_id}`",
            f"- Threshold: **{threshold:.1f}%**",
            f"- Final confidence: **{final_score:.2f}%**",
            f"- Iterations run: **{len(iterations_log)}** (cap {max_iterations})",
            f"- Files repaired: **{completed['applied']}** (failed: {completed['failed']})",
            f"- Started: {run_started}",
            f"- Ended:   {run_doc['ended_at']}",
            "",
            "## Scoring axes (each file scored 0–100, weighted average)",
            "",
            "| Axis | Weight | What it checks |",
            "|------|-------:|----------------|",
            "| structural  | 10% | Framework markers present (@RestController, @Entity, APIRouter…) |",
            "| parity      | 20% | No TODO carpets / Unsupported throws / scaffold-fallback headers |",
            "| coverage    | 25% | Every legacy endpoint→Controller method, every table→@Entity |",
            "| schema      | 15% | Every OLTP DDL column appears as @Column in Entity |",
            "| evidence    | 15% | File cites legacy class/method names (proves KB consulted) |",
            "| requirement | 15% | File references SRS BR-* / UC-* / NFR-* identifiers |",
            "",
            "## Iteration trajectory",
            "",
            "| # | Score | Files < threshold | Notes |",
            "|--:|------:|------------------:|-------|",
        ]
        for row in iterations_log:
            md_lines.append(
                f"| {row['iteration']} | {row['overall_score']:.2f}% | "
                f"{row.get('files_below_threshold', 0)} | "
                f"{row.get('summary','')[:80]} |"
            )
        md_lines.append("")
        if iterations_log and iterations_log[-1].get("services"):
            md_lines.extend([
                "## Final per-service breakdown",
                "",
                "| Service | Score | Files | Endpoint cov | Table cov | Column cov |",
                "|---------|------:|------:|-------------:|----------:|-----------:|",
            ])
            for s in iterations_log[-1]["services"]:
                md_lines.append(
                    f"| `{s['service']}` | {s['score']:.2f}% | {s['file_count']} | "
                    f"{s['endpoint_coverage_pct']:.0f}% | {s['table_coverage_pct']:.0f}% | "
                    f"{s['column_coverage_pct']:.0f}% |"
                )
            md_lines.append("")
        if not run_doc["converged"]:
            md_lines.extend([
                "## Why didn't we reach threshold?",
                "",
                "Confidence below the gate after exhausting the iteration cap usually means one of:",
                "",
                "- **Sparse SRS** — BR-* / UC-* / NFR-* identifiers are missing or thin "
                "(see the `requirement` axis). Regenerate the relevant SRS sections.",
                "- **Sparse legacy KB** — the `evidence` axis is low because no kb_entities "
                "matched the file's seed tokens. Re-run **Build KB** with deeper folder coverage.",
                "- **Sparse DDL** — the `schema` axis is low because OLTP DDL columns are "
                "missing. Re-freeze Stage 2 with `CREATE TABLE` statements for every entity.",
                "- **Missing endpoints** — Stage 3 API contracts didn't include the legacy paths. "
                "Re-run Stage 3 LLD with the full route inventory.",
                "",
                "Re-running this loop on its own won't fix root-cause evidence gaps — the upstream "
                "stages must be re-frozen first.",
            ])
        else:
            md_lines.append("## Production-ready ✓")
            md_lines.append("")
            md_lines.append(
                f"All scored source files cleared the {threshold:.1f}% gate. The generated "
                f"codebase is ready for ZIP / GitHub-push / Freeze."
            )
        report_md = "\n".join(md_lines)

        now = datetime.now(timezone.utc).isoformat()
        await codegen_files.update_one(
            {"project_id": project_id, "file_path": report_path},
            {"$set": {
                "id": __import__("uuid").uuid4().hex,
                "project_id": project_id,
                "service_id": "",
                "service_name": "_lama",
                "file_path": report_path,
                "content": report_md,
                "language": "markdown",
                "file_type": "report",
                "version": 1,
                "edited": False,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
        run_doc["report_path"] = report_path
        run_doc["report_md"] = report_md
        await parity_runs.update_one({"id": jid}, {"$set": run_doc}, upsert=True)
        await audit_log.insert_one({
            "action": "codegen.auto_validate",
            "project_id": project_id,
            "at": now,
            "details": {
                "run_id": jid,
                "threshold": threshold,
                "final_score": final_score,
                "converged": run_doc["converged"],
                "iterations": len(iterations_log),
                "applied": completed["applied"],
                "failed": completed["failed"],
                "report_path": report_path,
                "only_service": only_service,
            },
        })
        # iter-14.44 — Refresh the LLM multi-model CodeGen confidence in the
        # background so the outer pill's section-breakdown popover (backend_code/
        # frontend_code/tests) reflects the newly-recovered files. The pill's
        # top-line number is already mirrored from parity_loop via
        # pipeline.get_stage_confidence, so this refresh only rehydrates the
        # popover's per-section explainer — never awaited, failures logged.
        # iter-14.46 — Also invalidate the live-parity cache so the pill
        # picks up the new score immediately on the next poll.
        try:
            from routes.pipeline import (
                compute_stage_confidence as _refresh_cg_conf,
                invalidate_codegen_parity_cache as _inval_cg_cache,
            )
            try:
                _inval_cg_cache(project_id)
                _job_log(jid, "invalidated CodeGen parity cache")
            except Exception:  # noqa: BLE001
                pass

            async def _bg_refresh_codegen_confidence(pid: str):
                try:
                    await _refresh_cg_conf(pid, "CodeGen")
                    logger.info("iter-14.44 CodeGen LLM confidence refreshed for %s", pid)
                except Exception as _re:  # noqa: BLE001
                    logger.warning(
                        "iter-14.44 CodeGen confidence refresh failed for %s: %s",
                        pid, _re,
                    )

            asyncio.create_task(_bg_refresh_codegen_confidence(project_id))
            _job_log(jid, "queued LLM confidence refresh (background) for popover")
        except Exception as _e:  # noqa: BLE001
            logger.warning("iter-14.44 could not schedule CodeGen conf refresh: %s", _e)
        _job_finish(
            jid, "complete",
            step=(f"✓ Converged at {final_score:.2f}% in {len(iterations_log)} iter"
                  if run_doc["converged"]
                  else f"⚠ Stopped at {final_score:.2f}% (< {threshold:.1f}%) after {len(iterations_log)} iter"),
            pct=100,
            result={
                "converged": run_doc["converged"],
                "final_score": final_score,
                "threshold": threshold,
                "iterations": len(iterations_log),
                "applied": completed["applied"],
                "failed": completed["failed"],
                "report_path": report_path,
            },
        )
    except JobStopped:
        run_doc["status"] = "stopped"
        run_doc["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_doc["iterations"] = iterations_log
        await parity_runs.update_one({"id": jid}, {"$set": run_doc}, upsert=True)
        _job_finish(
            jid, "stopped",
            step=f"Stopped by user (applied {completed['applied']}, failed {completed['failed']})",
            pct=_JOBS.get(jid, {}).get("pct", 0),
            result={"applied": completed["applied"], "failed": completed["failed"]},
        )
    except HTTPException as he:
        run_doc["status"] = "error"
        run_doc["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_doc["error"] = str(he.detail)
        await parity_runs.update_one({"id": jid}, {"$set": run_doc}, upsert=True)
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:  # noqa: BLE001
        logger.exception("auto-validate job failed")
        run_doc["status"] = "error"
        run_doc["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_doc["error"] = str(e)
        await parity_runs.update_one({"id": jid}, {"$set": run_doc}, upsert=True)
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/auto-validate")
async def start_auto_validate(payload: dict):
    """Kick off the iterative validate→regenerate loop.

    Body: {
      project_id:        str  (required)
      threshold:         float  (default 95.0; clamped to [50, 100])
      max_iterations:    int    (default 3; clamped to [1, 10];
                                 overridable via LAMA_CODEGEN_MAX_ITERATIONS)
      max_files_per_iter:int    (default 20; clamped to [1, 50])
      service_name:      str?   (limit scoring + recovery to one service)
      model:             str?   (gap-recovery model override; falls
                                 through to LAMA_GAP_ANALYSIS_MODEL /
                                 Console cross-tier routing)
    }
    """
    project_id = (payload or {}).get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Architecture", "CodeGen")
    # CodeGen output must exist before we can score it.
    existing = await codegen_files.count_documents({"project_id": project_id})
    if existing == 0:
        raise HTTPException(
            400,
            "No generated code to validate. Run Generate All first.",
        )
    threshold = float((payload or {}).get("threshold") or 95.0)
    threshold = max(50.0, min(100.0, threshold))
    # iter-14.21 — Default max iterations = 3 (was 5) per user's explicit
    # ask: "if <=95 then again do the gap analysis with regeneration...
    # max 3 iterations". Configurable via LAMA_CODEGEN_MAX_ITERATIONS
    # (still clamped to [1,10]).
    _default_iter = 3
    try:
        _default_iter = int(os.environ.get("LAMA_CODEGEN_MAX_ITERATIONS", "3") or 3)
    except ValueError:
        _default_iter = 3
    max_iter = int((payload or {}).get("max_iterations") or _default_iter)
    max_iter = max(1, min(10, max_iter))
    max_files = int((payload or {}).get("max_files_per_iter") or 20)
    max_files = max(1, min(50, max_files))
    only_service = (payload or {}).get("service_name") or None
    model = await _resolve_gap_analysis_model((payload or {}).get("model"))

    set_current_agent_key("codegen.gap_recovery")
    jid = _new_job(project_id, "auto_validate")
    asyncio.create_task(_run_auto_validate_job(
        jid, project_id, threshold, max_iter, max_files, only_service, model,
    ))
    return {
        "job_id": jid, "status": "queued",
        "threshold": threshold,
        "max_iterations": max_iter,
        "max_files_per_iter": max_files,
        "model": model or "(console-routed)",
    }


@router.get("/{project_id}/parity-report")
async def get_parity_report(project_id: str, run_id: str = ""):
    """Fetch the most recent auto-validate report (or a specific one)."""
    q: Dict[str, Any] = {"project_id": project_id}
    if run_id:
        q["id"] = run_id
    doc = await parity_runs.find_one(
        q, {"_id": 0}, sort=[("started_at", -1)],
    )
    if not doc:
        raise HTTPException(404, "No auto-validate runs found for this project.")
    return doc


@router.get("/{project_id}/parity-runs")
async def list_parity_runs(project_id: str, limit: int = 20):
    """List recent auto-validate runs for the Activity / History tab."""
    docs = await parity_runs.find(
        {"project_id": project_id},
        {"_id": 0, "report_md": 0},
    ).sort("started_at", -1).to_list(max(1, min(100, limit)))
    return {"runs": docs, "count": len(docs)}


@router.post("/{project_id}/parity-score")
async def score_parity_once(project_id: str, payload: dict = None):
    """One-shot scoring (no recovery loop) so the UI can show the current
    confidence number without burning Gap-Recovery tokens. Optional body:
    ``{ service_name: str, threshold: float }``.
    """
    from codegen.parity_loop import score_run
    payload = payload or {}
    threshold = float(payload.get("threshold") or 95.0)
    threshold = max(50.0, min(100.0, threshold))
    only_service = payload.get("service_name") or None
    existing = await codegen_files.count_documents({"project_id": project_id})
    if existing == 0:
        raise HTTPException(400, "No generated code to score. Run Generate All first.")
    report = await score_run(
        project_id, iteration=0, threshold=threshold, only_service=only_service,
    )
    return report.to_dict()


# ---------------------------------------------------------------------------
# iter-14.41 — 1:1 JSP → React page LIVE generation job
# ---------------------------------------------------------------------------
#
# iter-14.40 shipped a stub-first expander that inserted `pending gap-recovery`
# placeholders — that was wrong: user expects a MIGRATION, not a TODO list.
#
# iter-14.41 replaces the stub expander with a proper long-running job:
#   1. Plan every JSP → target React path (in memory only, no writes).
#   2. For every planned page, call the LLM with the FULL legacy JSP body as
#      the primary source of truth PLUS the API contract slice + service
#      patterns, produce a complete production-grade React component, and
#      write it to `codegen_files` inline.
#   3. When all pages are done, regenerate `frontend/src/routes.tsx` with the
#      full lazy-loaded route table.
#
# The job is wired into `_JOBS` so the existing gap-recovery progress bar
# renders it. Uses `LAMA_GAP_RECOVERY_CONCURRENCY` semaphore.
# ---------------------------------------------------------------------------

_JSP_SKIP_FOLDERS = {"saml2", "fedletAttrQuery.jsp", "fedletAttrResp.jsp",
                     "fedletXACMLQuery.jsp", "myapp.jsp"}


def _jsp_to_component_name(jsp_stem: str) -> str:
    """`ceoWorkList` → `CeoWorkList`; `pay_grades-data` → `PayGradesData`."""
    parts = re.split(r"[_\-.\s]+", jsp_stem)
    out = []
    for p in parts:
        if not p:
            continue
        p2 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", p)
        for w in p2.split():
            if w:
                out.append(w[0].upper() + w[1:])
    name = "".join(out) or "Page"
    if not name[0].isalpha():
        name = "P" + name
    return name


def _jsp_to_route_path(folder: str, comp: str) -> str:
    def kebab(s: str) -> str:
        return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s).lower()
    return f"/{kebab(folder)}/{kebab(comp)}"


async def _plan_frontend_pages_from_jsps(project_id: str) -> List[Dict[str, str]]:
    """Walk `kb_files` for every `.jsp`/`.jspf`, compute the target React
    path per JSP and return the plan. Writes nothing.
    """
    jsp_cursor = kb_files.find(
        {"project_id": project_id,
         "filename": {"$regex": r"\.jspf?$", "$options": "i"}},
        {"filename": 1, "id": 1},
    )
    planned: List[Dict[str, str]] = []
    seen_paths: set[str] = set()
    async for f in jsp_cursor:
        fname = f.get("filename") or ""
        parts = fname.split("/")
        if len(parts) < 2:
            folder = "misc"
            stem = os.path.splitext(parts[-1])[0]
        else:
            if parts[0].lower() in {"webcontent", "web-inf", "webapp", "src"} and len(parts) > 2:
                folder = parts[1]
                stem = os.path.splitext(parts[-1])[0]
            else:
                folder = parts[0]
                stem = os.path.splitext(parts[-1])[0]
        if folder in _JSP_SKIP_FOLDERS:
            continue
        comp = _jsp_to_component_name(stem)
        folder_slug = re.sub(r"[^A-Za-z0-9]+", "", folder) or "misc"
        page_path = f"frontend/src/pages/{folder_slug}/{comp}.tsx"
        if page_path in seen_paths:
            page_path = f"frontend/src/pages/{folder_slug}/{comp}_{f.get('id','')[:6]}.tsx"
        seen_paths.add(page_path)
        planned.append({
            "page_path": page_path,
            "component": comp,
            "folder": folder_slug,
            "legacy_source_file": fname,
            "kb_file_id": f.get("id", ""),
            "route_path": _jsp_to_route_path(folder_slug, comp),
        })
    return planned


async def _load_jsp_body(project_id: str, kb_file_id: str,
                        legacy_source_file: str, max_chars: int = 24000) -> str:
    """Load the FULL legacy JSP body from `kb_chunks`, ordered by chunk_index."""
    bodies: List[str] = []
    query: Dict[str, Any] = {"project_id": project_id}
    if kb_file_id:
        query["file_id"] = kb_file_id
    else:
        f = await kb_files.find_one(
            {"project_id": project_id, "filename": legacy_source_file}, {"id": 1},
        )
        if f:
            query["file_id"] = f.get("id")
    if "file_id" not in query:
        return ""
    async for c in kb_chunks.find(query, {"content": 1, "chunk_index": 1}).sort("chunk_index", 1):
        body = (c.get("content") or "").strip()
        if body:
            bodies.append(body)
        if sum(len(b) for b in bodies) > max_chars:
            break
    return "\n\n".join(bodies)[:max_chars]


def _build_routes_tsx(planned: List[Dict[str, str]]) -> str:
    """Regenerate `frontend/src/routes.tsx` from the planned page manifest."""
    lazy_imports: List[str] = []
    route_entries: List[str] = []
    seen_comp: set[str] = set()
    for p in planned:
        comp = p["component"]
        comp2 = (p["folder"] + comp) if comp in seen_comp else comp
        seen_comp.add(comp2)
        import_from = f"./pages/{p['folder']}/{p['component']}"
        lazy_imports.append(
            f"const {comp2} = lazy(() => import('{import_from}'));"
        )
        route_entries.append(
            f"  {{ path: '{p['route_path']}', element: <Suspense fallback={{<div>Loading…</div>}}><{comp2} /></Suspense> }},"
        )
    return (
        "// AUTO-GENERATED by LAMA iter-14.41 — 1:1 legacy JSP → React page manifest.\n"
        "import React, { lazy, Suspense } from 'react';\n"
        "import { RouteObject } from 'react-router-dom';\n\n"
        + "\n".join(lazy_imports)
        + "\n\nexport const legacyRoutes: RouteObject[] = [\n"
        + "\n".join(route_entries)
        + "\n];\n\nexport default legacyRoutes;\n"
    )


_JSP_TO_REACT_SYSTEM_PROMPT = """You are a Senior Front-End Engineer migrating a legacy JSP/Struts webapp to a modern React 19 + TypeScript + shadcn/ui + Tailwind stack.

You will receive ONE legacy JSP file body and its supporting API-contract slice. Your job: emit ONE production-grade React functional component (`.tsx`) that reproduces the JSP screen faithfully in the modern stack. This is a REAL MIGRATION — the output must run in the target app.

HARD RULES — output MUST comply with all of these:

STRUCTURE
- Emit ONLY the raw `.tsx` file body. NO markdown fences. NO explanations. NO surrounding prose.
- Single default export: `export default <ComponentName>;`
- `import React, { useState, useEffect } from 'react';` at top; import `axios` if the JSP calls backend endpoints.
- Component is a `React.FC` (or `React.FC<Props>` if the JSP takes URL params).

FIDELITY TO THE LEGACY JSP (primary source of truth)
- Every `<form>`, `<input>`, `<select>`, `<textarea>`, `<button>`, `<table>`, `<a href>` and role check in the JSP MUST appear in the React component with equivalent behaviour.
- Preserve every visible label, placeholder text, table column header, dropdown option, tab title, and validation error message VERBATIM.
- Every `onclick=`, `onsubmit=`, `onchange=`, `struts action=`, or `<c:if test='${role==...}'>` construct becomes a real React handler / conditional render — never an empty `onClick={() => {}}`.
- Every backend URL / Struts action referenced in the JSP becomes an `axios.get`/`axios.post` call against the corresponding operationId in the API CONTRACT slice. If no exact match, use the closest RESTful path and add a single `// PARITY-RISK: <one-line reason>` comment.

STATE & DATA FLOW
- Use `useState` for every form field; `useEffect` to load initial data.
- Include loading, empty, and error states.
- Wire real submit handlers: build the request body from state, call the API, then re-fetch or update local state on success. Show a toast/alert on failure.
- Use JS Date / built-in formatters; do not import moment.js.

STYLING
- Use Tailwind utility classes (`className="flex flex-col gap-4 p-6"` style). Do not emit inline `style={{...}}` unless mirroring a legacy inline style is critical.
- Wrap the page in a `<div className="p-6 max-w-6xl mx-auto">` container.

FORBIDDEN
- NO `// TODO`, `// FIXME`, `// XXX`, `// Placeholder`, `// Your code here`, `// Implementation here`, `// Business logic goes here`, `// Add implementation`, `// Insert logic`, `// Fill in`, `// stub`, `// pending gap-recovery`.
- NO empty function bodies `() => {}` on any real handler. Empty handlers are only permissible on genuinely no-op controls (rare — flag with `// PARITY-RISK`).
- NO apologies, refusals, or "please provide" messages. If the JSP body is short, still emit the best possible component from what IS there.
- NO commented-out JSX blocks.
- NO placeholder data arrays like `const rows = [/* fill me */]`.

QUALITY BAR
- At least 60 lines of real JSX/TS code for any non-trivial JSP (skeleton pages < 30 legacy lines may be shorter).
- Type props / API response shapes with proper interfaces when structure is obvious from the JSP.
- Handle at least one edge case (empty result, missing role, form validation).

Output ONLY the .tsx file body, starting with the `import` line and ending with `export default`."""


async def _generate_react_from_jsp(
    project_id: str, plan_item: Dict[str, str],
    jsp_body: str, api_slice: str, model: str,
) -> Dict[str, Any]:
    """Call the LLM once (with retries) to translate a legacy JSP into a
    production-grade React `.tsx` file. Returns
    ``{ok, content, error}``.
    """
    comp = plan_item["component"]
    src = plan_item["legacy_source_file"]
    if not jsp_body or len(jsp_body) < 30:
        return {"ok": False, "content": "", "error": "empty JSP body"}
    user_msg = (
        f"Migrate the following legacy JSP into a production-ready React "
        f"component named `{comp}` (file path: `{plan_item['page_path']}`).\n\n"
        f"# === LEGACY JSP SOURCE (primary source of truth) ===\n"
        f"# Filename: {src}\n"
        f"{jsp_body}\n"
        f"# === END LEGACY JSP SOURCE ===\n\n"
        f"# === API CONTRACT SLICE (backend endpoints available) ===\n"
        f"{api_slice[:6000] if api_slice else '(no explicit contract — use RESTful paths derived from JSP action attributes)'}\n"
        f"# === END API CONTRACT SLICE ===\n\n"
        f"Emit ONLY the raw .tsx file body per the system prompt rules. "
        f"No markdown, no prose, no `// TODO`."
    )
    last_err = ""
    for attempt, temp in enumerate([0.05, 0.15, 0.30], start=1):
        try:
            r = await chat_completion(
                messages=[
                    {"role": "system", "content": _JSP_TO_REACT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                model=model, temperature=temp,
                max_tokens=6000, timeout=240.0,
                agent_key="codegen.gap_recovery",
                project_id=project_id,
            )
            raw = (r.get("content") or "").strip()
            # Strip accidental markdown fences
            raw = re.sub(r"^```(?:tsx?|typescript|jsx?|javascript)?\s*\n?", "", raw)
            raw = re.sub(r"\n?```\s*$", "", raw)
            raw = raw.strip()
            # Reject apologies / stubs / TODOs (same guards as iter-14.38)
            low = raw.lower()
            refusal_markers = (
                "i'm sorry", "i am sorry", "i cannot complete",
                "please provide the necessary", "happy to assist you further",
                "as an ai language model", "pending gap-recovery",
                "// todo:", "// fixme:", "// placeholder",
                "// your code here", "// implementation here",
                "// business logic goes here",
            )
            hits = [m for m in refusal_markers if m in low]
            if hits:
                last_err = f"attempt {attempt}: model returned lazy-stub/apology markers: {hits[:3]}"
                continue
            if len(raw) < 200:
                last_err = f"attempt {attempt}: content too short ({len(raw)} chars)"
                continue
            if "export default" not in raw or "import" not in raw or "<" not in raw:
                last_err = f"attempt {attempt}: not a valid React component (missing import/export/JSX)"
                continue
            return {"ok": True, "content": raw, "error": ""}
        except Exception as e:
            last_err = f"attempt {attempt}: {e}"
    return {"ok": False, "content": "", "error": last_err or "all attempts failed"}


async def _run_frontend_1to1_job(
    jid: str, project_id: str, model: str,
    overwrite_existing: bool = False,
) -> None:
    """Long-running job: for every legacy JSP, generate a real React
    component via LLM and write to `codegen_files`. Progress reported
    via `_JOBS[jid]`.
    """
    try:
        _job_update(jid, status="running", step="Planning JSP → React map…", pct=1)
        planned = await _plan_frontend_pages_from_jsps(project_id)
        if not planned:
            _job_finish(jid, "complete", pct=100,
                        result={"created": 0, "updated": 0, "failed": 0,
                                "total": 0, "note": "no JSPs found in KB"})
            return
        # API contract for the FE service
        arch_ctx = await stage_context_col.find_one(
            {"project_id": project_id, "stage": "Architecture"}, {"_id": 0},
        )
        api_full = ((arch_ctx or {}).get("outputs", {}) or {}).get("api_contracts", "") or ""
        if not api_full:
            api_doc = await arch_documents.find_one(
                {"project_id": project_id, "type": "api_contracts"}, {"_id": 0},
            )
            api_full = (api_doc or {}).get("content", "") or ""
        api_slice = _slice_api_contract_for_service(api_full, "frontend") or api_full[:6000]

        # Concurrency (Ollama default 4)
        try:
            conc = int(os.environ.get("LAMA_GAP_RECOVERY_CONCURRENCY", "4") or 4)
        except ValueError:
            conc = 4
        conc = max(1, min(16, conc))
        sem = asyncio.Semaphore(conc)
        total = len(planned)
        _JOBS[jid]["log"].append(
            f"1:1 JSP → React starting: {total} pages, concurrency={conc}, model={model}"
        )
        created = 0
        updated = 0
        failed = 0
        done = 0
        errors: List[Dict[str, str]] = []
        lock = asyncio.Lock()
        now_iso = datetime.now(timezone.utc).isoformat()

        async def _one(p: Dict[str, str]):
            nonlocal created, updated, failed, done
            async with sem:
                await _await_job_control(jid)
                _job_update(
                    jid,
                    step=f"[{done + 1}/{total}] {p['page_path']}",
                )
                # Skip / overwrite policy
                existing = await codegen_files.find_one(
                    {"project_id": project_id, "file_path": p["page_path"]},
                    {"_id": 1, "version": 1, "edited": 1, "content": 1},
                )
                if existing and existing.get("edited") and not overwrite_existing:
                    async with lock:
                        done += 1
                        pct = 2 + int((done / total) * 95)
                        _job_update(jid, pct=pct)
                    return
                # Load full JSP body
                jsp_body = await _load_jsp_body(
                    project_id, p["kb_file_id"], p["legacy_source_file"],
                )
                res = await _generate_react_from_jsp(
                    project_id, p, jsp_body, api_slice, model,
                )
                if not res["ok"]:
                    async with lock:
                        failed += 1
                        done += 1
                        pct = 2 + int((done / total) * 95)
                        _job_update(jid, pct=pct)
                        errors.append({"path": p["page_path"], "error": res["error"][:200]})
                        if len(_JOBS[jid]["log"]) < 400:
                            _JOBS[jid]["log"].append(
                                f"FAIL [{done}/{total}] {p['page_path']}: {res['error'][:150]}"
                            )
                    return
                content = res["content"]
                if existing:
                    await codegen_files.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {
                            "content": content,
                            "version": (existing.get("version", 1) or 1) + 1,
                            "edited": False,
                            "updated_at": now_iso,
                            "legacy_source_file": p["legacy_source_file"],
                            "kb_file_id": p["kb_file_id"],
                            "route_path": p["route_path"],
                            "file_type": "page",
                            "service_name": "frontend",
                            "language": "typescript",
                        }},
                    )
                    async with lock:
                        updated += 1
                else:
                    await codegen_files.insert_one({
                        "id": f"cgf_{project_id[:8]}_{p['component'].lower()}_{p['kb_file_id'][:6]}",
                        "project_id": project_id,
                        "file_path": p["page_path"],
                        "content": content,
                        "language": "typescript",
                        "file_type": "page",
                        "service_name": "frontend",
                        "service_id": None,
                        "version": 1,
                        "edited": False,
                        "legacy_source_file": p["legacy_source_file"],
                        "kb_file_id": p["kb_file_id"],
                        "route_path": p["route_path"],
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    })
                    async with lock:
                        created += 1
                async with lock:
                    done += 1
                    pct = 2 + int((done / total) * 95)
                    _job_update(jid, pct=pct)
                    if done % 10 == 0 or done == total:
                        _JOBS[jid]["log"].append(
                            f"OK [{done}/{total}] created={created} updated={updated} failed={failed}"
                        )

        await asyncio.gather(*[_one(p) for p in planned], return_exceptions=False)

        # Regenerate routes.tsx from the plan
        _job_update(jid, step="Writing routes.tsx…", pct=98)
        routes_body = _build_routes_tsx(planned)
        existing_routes = await codegen_files.find_one(
            {"project_id": project_id, "file_path": "frontend/src/routes.tsx"},
            {"_id": 1, "version": 1},
        )
        if existing_routes:
            await codegen_files.update_one(
                {"_id": existing_routes["_id"]},
                {"$set": {
                    "content": routes_body,
                    "version": (existing_routes.get("version", 1) or 1) + 1,
                    "updated_at": now_iso,
                    "edited": False,
                }},
            )
        else:
            await codegen_files.insert_one({
                "id": f"cgf_{project_id[:8]}_routes_tsx",
                "project_id": project_id,
                "file_path": "frontend/src/routes.tsx",
                "content": routes_body,
                "language": "typescript",
                "file_type": "route",
                "service_name": "frontend",
                "version": 1,
                "edited": False,
                "created_at": now_iso,
                "updated_at": now_iso,
            })
        try:
            await audit_log.insert_one({
                "project_id": project_id,
                "action": "codegen.frontend.jsp_migrate_job",
                "details": {
                    "created": created, "updated": updated, "failed": failed,
                    "total": total, "iter": "14.41", "model": model,
                },
                "at": now_iso,
            })
        except Exception:
            pass
        _job_finish(
            jid, "complete", pct=100,
            result={
                "created": created, "updated": updated, "failed": failed,
                "total": total, "errors": errors[:50],
            },
        )
    except Exception as e:
        logger.exception("frontend 1:1 job failed: %s", e)
        _job_finish(jid, "error", error=str(e))


@router.post("/{project_id}/frontend/migrate-jsps")
async def start_migrate_jsps(project_id: str, payload: dict = None):
    """Kick off the 1:1 JSP → React migration job. Returns
    ``{job_id, model, total_planned}``. Poll `/api/codegen/jobs/{job_id}`
    for progress (same shape as gap-recovery). Optional body:
    ``{overwrite_existing: bool}`` (default False — leaves user-edited pages alone).
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "project not found")
    planned = await _plan_frontend_pages_from_jsps(project_id)
    if not planned:
        raise HTTPException(400, "No legacy JSPs found in KB. Rebuild Discovery KB first.")
    payload = payload or {}
    overwrite = bool(payload.get("overwrite_existing", False))
    model = await _resolve_gap_analysis_model(payload.get("model"))
    jid = _new_job(project_id, "frontend_migrate_jsps")
    asyncio.create_task(_run_frontend_1to1_job(jid, project_id, model, overwrite))
    return {"job_id": jid, "model": model, "total_planned": len(planned)}


@router.post("/{project_id}/frontend/plan-jsps")
async def plan_frontend_jsps(project_id: str):
    """Preview the JSP → React mapping WITHOUT writing anything. Returns
    ``{total, sample: [...first 30...]}`` so the UI can show a confirm dialog.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "project not found")
    planned = await _plan_frontend_pages_from_jsps(project_id)
    return {"total": len(planned), "sample": planned[:30]}




# ════════════════════════════════════════════════════════════════════════════
# Multi-Agent CodeGen Pipeline (iter-17) — mirrors iter-16 Transformer flow
# ────────────────────────────────────────────────────────────────────────────
# Legacy_migration Stage-4 pipeline. Two coder specialists (`coder_be` /
# `coder_fe`) fan out per wave. The pipeline is state-machine driven via
# the singleton `codegen_pipeline_state` doc per project.
#
# HARD RULE (kept load-bearing): the pre-existing single-shot CodeGen
# `generate` flow above is UNTOUCHED. This is an additive parallel path.
# The two flows share the `codegen_files` collection at finalize time.
#
# See `models.CodeGenEnvelope / CodeGenTask / CodeGenAgentRun /
# CodeGenPipelineState`, `db.codegen_*` accessors, and the seeded prompts
# `codegen.*` in `seed.py`.
# ════════════════════════════════════════════════════════════════════════════

from fastapi import BackgroundTasks  # noqa: E402  (iter-17)
from db import (  # noqa: E402  (iter-17)
    codegen_envelopes,
    codegen_tasks,
    codegen_agent_runs,
    codegen_pipeline_state,
)


# ── State machine constants ────────────────────────────────────────────────
_CODEGEN_STATUS_IDLE = "idle"
_CODEGEN_STATUS_ENV_PENDING = "envelopes_pending"
_CODEGEN_STATUS_ENV_CONFIRMED = "envelopes_confirmed"
_CODEGEN_STATUS_TASKS_PENDING = "tasks_pending"
_CODEGEN_STATUS_TASKS_CONFIRMED = "tasks_confirmed"
_CODEGEN_STATUS_EXECUTING = "executing"
_CODEGEN_STATUS_TRACEABILITY = "traceability_gate"
_CODEGEN_STATUS_COMPLETED = "completed"
_CODEGEN_STATUS_FAILED = "failed"

# Only these three statuses allow (re)starting the pipeline. Everything
# else is either in flight (409) or already in a gate the operator must
# clear.
_CODEGEN_STARTABLE_STATUSES = {
    _CODEGEN_STATUS_IDLE,
    _CODEGEN_STATUS_FAILED,
    _CODEGEN_STATUS_COMPLETED,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Deterministic BE/FE routing ────────────────────────────────────────────
_BE_PATH_PREFIXES = (
    "backend/", "api/", "services/", "db/", "migrations/", "app/",
    "src/main/",
)
_BE_EXTENSIONS = (
    ".py", ".java", ".kt", ".go", ".rs", ".sql", ".yml", ".yaml", ".toml",
)
_BE_BASENAMES = frozenset({
    "Dockerfile", "pom.xml", "build.gradle", "requirements.txt",
    "pyproject.toml",
})

_FE_PATH_PREFIXES = (
    "frontend/", "web/", "ui/", "client/", "src/pages/", "src/components/",
)
_FE_EXTENSIONS = (
    ".jsx", ".tsx", ".ts", ".js", ".css", ".scss", ".html", ".vue",
    ".svelte",
)


def _basename(p: str) -> str:
    return p.rsplit("/", 1)[-1] if p else ""


def _looks_like_backend_path(p: str) -> bool:
    p = (p or "").strip()
    if not p:
        return False
    for pref in _BE_PATH_PREFIXES:
        if p.startswith(pref):
            return True
    if any(p.endswith(ext) for ext in _BE_EXTENSIONS):
        return True
    if _basename(p) in _BE_BASENAMES:
        return True
    return False


def _looks_like_frontend_path(p: str) -> bool:
    p = (p or "").strip()
    if not p:
        return False
    for pref in _FE_PATH_PREFIXES:
        if p.startswith(pref):
            return True
    if any(p.endswith(ext) for ext in _FE_EXTENSIONS):
        return True
    base = _basename(p)
    # Catch pattern names like package.json, vite.config.ts, tailwind.config.js
    if base == "package.json":
        return True
    for stem in ("vite.config", "tailwind.config", "craco.config"):
        if base.startswith(stem + "."):
            return True
    return False


def _route_task_to_coder(task: Dict[str, Any]) -> str:
    """Deterministic BE/FE routing applied AFTER the Planner runs.

    Rules (in order):
      1. If the path is unambiguously BE (matches BE rules AND not FE),
         return "coder_be".
      2. If unambiguously FE (matches FE rules AND not BE), return
         "coder_fe".
      3. Ambiguous or empty — if the Planner already suggested
         "coder_be" or "coder_fe", keep it. Otherwise default to
         "coder_be" (and callers may log a warning agent_run).

    Never returns anything other than "coder_be" or "coder_fe".
    """
    path = (task.get("target_path") or "").strip()
    planner = (task.get("assigned_to") or "").strip()
    be = _looks_like_backend_path(path)
    fe = _looks_like_frontend_path(path)
    if be and not fe:
        return "coder_be"
    if fe and not be:
        return "coder_fe"
    # Ambiguous — trust planner if it picked a valid coder.
    if planner in ("coder_be", "coder_fe"):
        return planner
    return "coder_be"


# ── Target-stack resolution (iter-17.12) ───────────────────────────────────
# The multi-agent deterministic planner used to emit hard-coded Python /
# FastAPI paths (`backend/app/entities/*.py`) regardless of the project's
# actual target stack. Result: Java + Spring Boot targets landed as
# `.py` files with Pydantic BaseModel — Coder followed the extension in
# `target_path` and dutifully produced Python. These helpers translate the
# Architecture stage's `target_backend` / `target_frontend` output into the
# correct filesystem layout AND surface an explicit "target stack" section
# in the Coder user prompt so the LLM has no excuse to drift.
_JAVA_FRAMEWORK_TOKENS = {"spring", "spring-boot", "springboot", "quarkus", "micronaut"}
_KOTLIN_FRAMEWORK_TOKENS = {"ktor", "spring-boot-kotlin"}
_NODE_LANG_TOKENS = {"node", "nodejs", "typescript", "ts", "javascript", "js"}
_GO_LANG_TOKENS = {"go", "golang"}
_DOTNET_LANG_TOKENS = {"csharp", "dotnet", "c#", "aspnetcore"}


def _parse_target_tech_string(text: str) -> Dict[str, Dict[str, str]]:
    """Parse the human-readable ``project.target_tech`` label
    (e.g. "Spring Boot 3.3 / Java 21 / Oracle 23ai / React 19 / Modular Monolith")
    into ``{"backend": {"lang", "framework"}, "frontend": {...}}``.

    iter-17.13 — projects created via the "Custom / Others" picker only
    store the label on ``project.target_tech`` and the parsed evidence on
    ``project.target_stack_selection.selected_label``; they do NOT
    populate ``arch_outputs.target_backend``. Without this fallback the
    Multi-Agent planner always defaulted to Python + FastAPI.
    """
    out: Dict[str, Dict[str, str]] = {"backend": {}, "frontend": {}}
    if not text:
        return out
    t = text.lower()

    # Backend lang
    if "java" in t or "spring boot" in t or "spring-boot" in t or "springboot" in t:
        out["backend"]["lang"] = "java"
    elif "kotlin" in t:
        out["backend"]["lang"] = "kotlin"
    elif ".net" in t or "dotnet" in t or "c#" in t or "csharp" in t or "asp.net" in t:
        out["backend"]["lang"] = "csharp"
    elif "golang" in t or " go " in f" {t} " or "gin" in t or "echo" in t:
        out["backend"]["lang"] = "go"
    elif "node" in t or "express" in t or "nest" in t or "typescript" in t and "react" not in t:
        out["backend"]["lang"] = "typescript"
    elif "python" in t or "fastapi" in t or "django" in t or "flask" in t:
        out["backend"]["lang"] = "python"

    # Backend framework
    if "spring boot" in t or "spring-boot" in t or "springboot" in t:
        out["backend"]["framework"] = "spring-boot"
    elif "quarkus" in t:
        out["backend"]["framework"] = "quarkus"
    elif "micronaut" in t:
        out["backend"]["framework"] = "micronaut"
    elif "ktor" in t:
        out["backend"]["framework"] = "ktor"
    elif "fastapi" in t:
        out["backend"]["framework"] = "fastapi"
    elif "django" in t:
        out["backend"]["framework"] = "django"
    elif "flask" in t:
        out["backend"]["framework"] = "flask"
    elif "nestjs" in t or "nest.js" in t or "nest " in t:
        out["backend"]["framework"] = "nest"
    elif "express" in t:
        out["backend"]["framework"] = "express"
    elif "gin" in t:
        out["backend"]["framework"] = "gin"
    elif "asp.net" in t or "aspnetcore" in t or "asp net" in t:
        out["backend"]["framework"] = "aspnetcore"

    # Frontend lang + framework
    if "react" in t or "next.js" in t or "nextjs" in t:
        out["frontend"]["lang"] = "typescript"
        out["frontend"]["framework"] = "next" if ("next.js" in t or "nextjs" in t) else "react"
    elif "vue" in t or "nuxt" in t:
        out["frontend"]["lang"] = "typescript"
        out["frontend"]["framework"] = "nuxt" if "nuxt" in t else "vue"
    elif "angular" in t:
        out["frontend"]["lang"] = "typescript"
        out["frontend"]["framework"] = "angular"
    elif "svelte" in t:
        out["frontend"]["lang"] = "typescript"
        out["frontend"]["framework"] = "svelte"
    return out


def _resolve_target_backend(
    arch_outputs: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Extract a normalized ``{lang, framework}`` pair for the backend
    target. Priority: (1) ``arch_outputs.target_backend`` structured
    field, (2) ``project.target_stack_selection.parsed_backend`` if
    present, (3) parsed ``project.target_tech`` label, (4) Python +
    FastAPI fallback so older projects that predate `target_backend`
    don't crash the planner.
    """
    tb = (arch_outputs.get("target_backend")
          or arch_outputs.get("backend")
          or {}) or {}
    lang = str(tb.get("lang") or tb.get("language") or "").strip().lower()
    framework = str(tb.get("framework") or tb.get("stack") or "").strip().lower()

    # iter-17.13 — fall through to the project's own target_tech.
    if (not lang or not framework) and project:
        sel = (project.get("target_stack_selection") or {}) or {}
        parsed = _parse_target_tech_string(
            sel.get("selected_label")
            or sel.get("submitted_label")
            or project.get("target_tech")
            or ""
        )
        pb = parsed.get("backend") or {}
        if not lang:
            lang = pb.get("lang", "")
        if not framework:
            framework = pb.get("framework", "")

    if not lang and framework:
        if any(t in framework for t in _JAVA_FRAMEWORK_TOKENS):
            lang = "java"
        elif any(t in framework for t in _KOTLIN_FRAMEWORK_TOKENS):
            lang = "kotlin"
        elif "fastapi" in framework or "django" in framework or "flask" in framework:
            lang = "python"
        elif "express" in framework or "nest" in framework or "koa" in framework:
            lang = "typescript"
        elif "gin" in framework or "echo" in framework or "fiber" in framework:
            lang = "go"
    if not lang:
        lang = "python"
    if not framework:
        framework = {
            "python":     "fastapi",
            "java":       "spring-boot",
            "kotlin":     "spring-boot",
            "typescript": "express",
            "javascript": "express",
            "go":         "gin",
            "csharp":     "aspnetcore",
            "dotnet":     "aspnetcore",
        }.get(lang, "fastapi")
    return {"lang": lang, "framework": framework}


def _resolve_target_frontend(
    arch_outputs: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    tf = (arch_outputs.get("target_frontend")
          or arch_outputs.get("frontend")
          or {}) or {}
    lang = str(tf.get("lang") or tf.get("language") or "").strip().lower()
    framework = str(tf.get("framework") or tf.get("stack") or "").strip().lower()

    if (not lang or not framework) and project:
        sel = (project.get("target_stack_selection") or {}) or {}
        parsed = _parse_target_tech_string(
            sel.get("selected_label")
            or sel.get("submitted_label")
            or project.get("target_tech")
            or ""
        )
        pf = parsed.get("frontend") or {}
        if not lang:
            lang = pf.get("lang", "")
        if not framework:
            framework = pf.get("framework", "")

    if not lang:
        lang = "typescript"
    if not framework:
        framework = "react"
    return {"lang": lang, "framework": framework}


def _pascal_case(slug: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", slug or "") if p]
    return "".join(p[:1].upper() + p[1:].lower() for p in parts) or "Root"


def _java_package_dir(base: str, artifact: str = "") -> str:
    """Return the source-root dir for a Java target using LAMA's
    conventional `com.lama.<service>` package.
    """
    art = re.sub(r"[^a-z0-9]+", "", (artifact or "app").lower()) or "app"
    return f"src/main/java/com/lama/{art}/{base}"


def _svc_prefix(service: str) -> str:
    """iter-17.14 — Return the per-service repo prefix. BE and FE MUST
    be emitted as separate service projects under ``services/<svc>/`` so
    each one has its own build manifest + isolated dependency tree.
    Empty prefix collapses back to repo-root (unit-test compat)."""
    s = re.sub(r"[^a-z0-9_-]+", "-", (service or "").strip().lower()).strip("-")
    return f"services/{s}" if s else ""


def _be_task_layout(
    lang: str,
    framework: str,
    slug: str,
    method: str,
    path: str,
    service: str = "",
) -> Dict[str, Dict[str, str]]:
    """Return per-layer file paths and description flavour for the
    deterministic planner. Every entry has ``path`` (target_path) and
    ``desc`` (task description) tuned to the target stack so Coder emits
    code in the correct language + framework.

    iter-17.14 — every path is now prefixed with ``services/<service>/``
    so BE and FE end up as completely separate build-projects instead of
    being interleaved in a single repo root.
    """
    Slug = _pascal_case(slug)
    method_u = (method or "ANY").upper()
    prefix = _svc_prefix(service)
    art = re.sub(r"[^a-z0-9]+", "", (service or "app").lower()) or "app"

    def _p(sub: str) -> str:
        return f"{prefix}/{sub}" if prefix else sub

    if lang in {"java", "kotlin"}:
        ext = "java" if lang == "java" else "kt"
        entity_dir     = _java_package_dir("entity",     art)
        repo_dir       = _java_package_dir("repository", art)
        service_dir    = _java_package_dir("service",    art)
        controller_dir = _java_package_dir("controller", art)
        dao_dir        = _java_package_dir("dao",        art)
        dto_dir        = _java_package_dir("dto",        art)
        mapper_dir     = _java_package_dir("mapper",     art)
        exception_dir  = _java_package_dir("exception",  art)
        config_dir     = _java_package_dir("config",     art)
        util_dir       = _java_package_dir("util",       art)
        test_dir       = _java_package_dir("controller", art).replace("src/main", "src/test")
        return {
            "entity":     {"path": _p(f"{entity_dir}/{Slug}.{ext}"),
                           "desc": (f"Production-grade JPA `@Entity` for envelope "
                                    f"({method_u} {path}). Package MUST be "
                                    f"`com.lama.{art}.entity`. REQUIREMENTS: map "
                                    f"EVERY column from the DataModel OLTP DDL as a "
                                    f"typed field with the correct `@Column(name=...)` "
                                    f"annotation; `@Id` + `@GeneratedValue` on the PK; "
                                    f"Lombok `@Data @NoArgsConstructor "
                                    f"@AllArgsConstructor` OR handwritten getters / "
                                    f"setters / equals / hashCode / toString; NO empty "
                                    f"class bodies.")},
            "repository": {"path": _p(f"{repo_dir}/{Slug}Repository.{ext}"),
                           "desc": (f"Production-grade Spring Data JPA repository "
                                    f"extending `JpaRepository<{Slug}, Long>`. Package "
                                    f"MUST be `com.lama.{art}.repository`. "
                                    f"REQUIREMENTS: at LEAST one custom finder for "
                                    f"every WHERE / JOIN clause cited in the "
                                    f"envelope's business_logic_summary "
                                    f"(`findByX`, `findByXAndY`, `existsByX`, "
                                    f"`countByX`, `@Query(...)` when JPA method-name "
                                    f"derivation cannot express it). Return "
                                    f"`Optional<{Slug}>` for single-row lookups, "
                                    f"`List<{Slug}>` for multi-row, `Page<{Slug}>` "
                                    f"when the legacy path paginates. An empty "
                                    f"interface body is REJECTED by the write-time "
                                    f"placeholder guard.")},
            "dao":        {"path": _p(f"{dao_dir}/{Slug}Dao.{ext}"),
                           "desc": (f"Hand-written DAO for native SQL / stored-proc "
                                    f"calls the JPA repository cannot express. "
                                    f"Package MUST be `com.lama.{art}.dao`. Use "
                                    f"`NamedParameterJdbcTemplate` with named "
                                    f"parameters (NEVER string concat), map rows via "
                                    f"typed `RowMapper<{Slug}>`, carry `@Repository`. "
                                    f"If the envelope has no native-SQL / stored-proc "
                                    f"reference, return the refusal envelope — DO "
                                    f"NOT emit an empty shell.")},
            "dto":        {"path": _p(f"{dto_dir}/{Slug}Dtos.{ext}"),
                           "desc": (f"Request / response DTOs for `{method_u} {path}`. "
                                    f"Package MUST be `com.lama.{art}.dto`. Prefer "
                                    f"Java records: `public record CreateRequest(...) "
                                    f"{{}}`, `public record UpdateRequest(...) {{}}`, "
                                    f"`public record {Slug}Response(...) {{}}`. Add "
                                    f"`@NotNull` / `@Size` / `@Pattern` validation "
                                    f"annotations for every request field the legacy "
                                    f"handler validates.")},
            "mapper":     {"path": _p(f"{mapper_dir}/{Slug}Mapper.{ext}"),
                           "desc": (f"Entity ↔ DTO mapper for `{Slug}`. Package MUST "
                                    f"be `com.lama.{art}.mapper`. Use MapStruct "
                                    f"(`@Mapper(componentModel=\"spring\")`) OR a "
                                    f"plain `@Component` with explicit `toDto(entity)`, "
                                    f"`toEntity(request)`, `updateEntity(entity, "
                                    f"request)` methods. Every DTO field MUST be "
                                    f"mapped — never leave silent nulls.")},
            "exception":  {"path": _p(f"{exception_dir}/{Slug}Exception.{ext}"),
                           "desc": (f"Domain exceptions for `{Slug}`. Package MUST "
                                    f"be `com.lama.{art}.exception`. Include "
                                    f"`{Slug}NotFoundException`, `{Slug}ConflictException`, "
                                    f"`{Slug}ValidationException`, each extending "
                                    f"`RuntimeException` and carrying a `code` field "
                                    f"matching the legacy error code. Emit a shared "
                                    f"`GlobalExceptionHandler` (`@RestControllerAdvice`) "
                                    f"that maps every subclass to the correct HTTP "
                                    f"status + `ErrorResponse` DTO.")},
            "service":    {"path": _p(f"{service_dir}/{Slug}Service.{ext}"),
                           "desc": (f"Production-grade Spring `@Service` for the "
                                    f"migrated legacy handler. Package MUST be "
                                    f"`com.lama.{art}.service`. Constructor-inject "
                                    f"the repository (and DAO / mapper when needed). "
                                    f"REQUIREMENTS: fully-implemented method bodies "
                                    f"(never `throw new UnsupportedOperationException`); "
                                    f"`@Transactional` on writes, "
                                    f"`@Transactional(readOnly = true)` on read-only "
                                    f"queries; map legacy status codes to the domain "
                                    f"exception subclasses from `com.lama.{art}.exception`.")},
            "controller": {"path": _p(f"{controller_dir}/{Slug}Controller.{ext}"),
                           "desc": (f"Production-grade Spring `@RestController` "
                                    f"exposing `{method_u} {path}`. Package MUST be "
                                    f"`com.lama.{art}.controller`. Delegate ALL "
                                    f"business logic to `{Slug}Service` — controllers "
                                    f"NEVER contain rules. REQUIREMENTS: every legacy "
                                    f"path variable / query param / request body "
                                    f"appears as `@PathVariable` / `@RequestParam` / "
                                    f"`@Valid @RequestBody <Dto>`; response is a "
                                    f"concrete DTO from `com.lama.{art}.dto` or "
                                    f"`ResponseEntity<Dto>` (never `Object` or "
                                    f"`Map<String, Object>`); correct HTTP status "
                                    f"(200 / 201 / 204 / 400 / 404 / 409).")},
            "config":     {"path": _p(f"{config_dir}/SecurityConfig.{ext}"),
                           "desc": (f"`@Configuration` classes for the service. "
                                    f"Package MUST be `com.lama.{art}.config`. Emit "
                                    f"`SecurityConfig` (`@EnableWebSecurity` + "
                                    f"`SecurityFilterChain` bean) + `OpenApiConfig` "
                                    f"only when the envelope demands custom config. "
                                    f"App entrypoint (`@SpringBootApplication`) MUST "
                                    f"live in `com.lama.{art}` — never here.")},
            "util":       {"path": _p(f"{util_dir}/{Slug}Util.{ext}"),
                           "desc": (f"Stateless helper for `{Slug}`. Package MUST "
                                    f"be `com.lama.{art}.util`. No business rules, "
                                    f"no persistence, no HTTP concerns. Skip if "
                                    f"envelope has no helper need.")},
            "test":       {"path": _p(f"{test_dir}/{Slug}ControllerTest.{ext}"),
                           "desc": (f"Production-grade Spring Boot integration test "
                                    f"using `@SpringBootTest` + `MockMvc` under "
                                    f"`src/test/java/com/lama/{art}/controller/`. "
                                    f"REQUIREMENTS: happy path + at least ONE error "
                                    f"path (bad-request / not-found / conflict) with "
                                    f"the exact response body asserted. NEVER "
                                    f"`assertTrue(true)`.")},
        }
    if lang in {"typescript", "javascript", "nodejs", "node"}:
        ext = "ts" if lang in {"typescript", "nodejs", "node"} else "js"
        fw_hint = "NestJS" if "nest" in framework else "Express"
        return {
            "entity":     {"path": _p(f"src/entities/{slug}.{ext}"),
                           "desc": f"TypeORM/Prisma entity for envelope ({method_u} {path})."},
            "repository": {"path": _p(f"src/repositories/{slug}.repository.{ext}"),
                           "desc": f"Data-access repository for {slug}."},
            "service":    {"path": _p(f"src/services/{slug}.service.{ext}"),
                           "desc": f"{fw_hint} service class encapsulating the business logic."},
            "controller": {"path": _p(f"src/routes/{slug}.controller.{ext}"),
                           "desc": f"{fw_hint} route handler exposing `{method_u} {path}`."},
            "test":       {"path": _p(f"test/{slug}.spec.{ext}"),
                           "desc": f"Jest test covering happy path + one error path "
                                   f"for `{method_u} {path}`."},
        }
    if lang in {"go", "golang"}:
        return {
            "entity":     {"path": _p(f"internal/entity/{slug}.go"),
                           "desc": f"Go struct for envelope ({method_u} {path})."},
            "repository": {"path": _p(f"internal/repository/{slug}_repo.go"),
                           "desc": f"Repository interface + impl for {slug}."},
            "service":    {"path": _p(f"internal/service/{slug}_service.go"),
                           "desc": f"Business logic for {slug}."},
            "controller": {"path": _p(f"internal/handler/{slug}_handler.go"),
                           "desc": f"{framework or 'gin'} HTTP handler for `{method_u} {path}`."},
            "test":       {"path": _p(f"internal/handler/{slug}_handler_test.go"),
                           "desc": f"Go test for `{method_u} {path}`."},
        }
    if lang in {"csharp", "dotnet"}:
        return {
            "entity":     {"path": _p(f"src/Entities/{Slug}.cs"),
                           "desc": f"EF Core entity class for envelope ({method_u} {path})."},
            "repository": {"path": _p(f"src/Repositories/{Slug}Repository.cs"),
                           "desc": f"Repository for {Slug} with EF Core `DbContext`."},
            "service":    {"path": _p(f"src/Services/{Slug}Service.cs"),
                           "desc": f"Business-logic service for {Slug}."},
            "controller": {"path": _p(f"src/Controllers/{Slug}Controller.cs"),
                           "desc": f"ASP.NET Core `[ApiController]` exposing `{method_u} {path}`."},
            "test":       {"path": _p(f"tests/{Slug}ControllerTests.cs"),
                           "desc": f"xUnit test for `{method_u} {path}`."},
        }
    # Default → Python / FastAPI (LAMA reference pilot).
    fw_hint = "FastAPI"
    if "django" in framework:
        fw_hint = "Django"
    elif "flask" in framework:
        fw_hint = "Flask"
    return {
        "entity":     {"path": _p(f"app/models/{slug}.py"),
                       "desc": f"SQLAlchemy `Base` model for envelope ({method_u} {path}). "
                               f"Module MUST be `app.models.{slug}` and NEVER carry HTTP "
                               f"or business logic."},
        "repository": {"path": _p(f"app/repositories/{slug}.py"),
                       "desc": f"Repository methods for tables cited by the envelope. "
                               f"Module MUST be `app.repositories.{slug}`."},
        "dao":        {"path": _p(f"app/dao/{slug}.py"),
                       "desc": f"Raw-SQL / stored-proc DAO. Only emit when the envelope "
                               f"references native SQL that SQLAlchemy ORM cannot express. "
                               f"Module MUST be `app.dao.{slug}`."},
        "dto":        {"path": _p(f"app/schemas/{slug}.py"),
                       "desc": f"Pydantic request/response schemas for `{method_u} {path}`. "
                               f"Module MUST be `app.schemas.{slug}`."},
        "mapper":     {"path": _p(f"app/mappers/{slug}.py"),
                       "desc": f"Entity ↔ schema mapper for {slug}. Module MUST be "
                               f"`app.mappers.{slug}`."},
        "exception":  {"path": _p(f"app/exceptions/{slug}.py"),
                       "desc": f"Domain exceptions for {slug}. Module MUST be "
                               f"`app.exceptions.{slug}`. Include NotFound / Conflict / "
                               f"Validation subclasses each carrying a `code` attribute."},
        "service":    {"path": _p(f"app/services/{slug}.py"),
                       "desc": f"Business logic for {slug}. Module MUST be "
                               f"`app.services.{slug}`. Constructor-inject the repository."},
        "controller": {"path": _p(f"app/routes/{slug}.py"),
                       "desc": f"{fw_hint} route handler for `{method_u} {path}` calling "
                               f"the service. Module MUST be `app.routes.{slug}`. NO "
                               f"business logic — only wiring."},
        "config":     {"path": _p(f"app/config/{slug}.py"),
                       "desc": f"Configuration module for {slug}. Module MUST be "
                               f"`app.config.{slug}`. Only emit when the envelope needs "
                               f"custom config."},
        "util":       {"path": _p(f"app/util/{slug}.py"),
                       "desc": f"Stateless helper for {slug}. Module MUST be "
                               f"`app.util.{slug}`. Skip if envelope has no helper need."},
        "test":       {"path": _p(f"tests/test_{slug}.py"),
                       "desc": f"Integration test covering happy path + one error path "
                               f"for `{method_u} {path}`."},
    }


def _fe_task_layout(
    lang: str,
    framework: str,
    slug: str,
    method: str,
    path: str,
    service: str = "",
) -> Dict[str, Dict[str, str]]:
    """Frontend layout — every path prefixed with ``services/<svc>/``
    (iter-17.14) so the FE service is a self-contained project separate
    from the BE service tree."""
    ext_client = "ts" if lang in {"typescript", "ts"} else "js"
    ext_page   = "tsx" if lang in {"typescript", "ts"} else "jsx"
    method_u = (method or "ANY").upper()
    prefix = _svc_prefix(service)

    def _p(sub: str) -> str:
        return f"{prefix}/{sub}" if prefix else sub

    if "vue" in framework:
        return {
            "client": {"path": _p(f"src/api/{slug}.{ext_client}"),
                       "desc": f"Axios/fetch API client for `{method_u} {path}`."},
            "page":   {"path": _p(f"src/views/{slug}.vue"),
                       "desc": f"Vue page that consumes the {slug} client and renders the response."},
        }
    if "angular" in framework:
        return {
            "client": {"path": _p(f"src/app/services/{slug}.service.ts"),
                       "desc": f"Angular `HttpClient` service for `{method_u} {path}`."},
            "page":   {"path": _p(f"src/app/pages/{slug}/{slug}.component.ts"),
                       "desc": f"Angular component that consumes the {slug} service."},
        }
    # React (default)
    return {
        "client": {"path": _p(f"src/api/{slug}.{ext_client}"),
                   "desc": f"Typed axios/fetch client hitting `{method_u} {path}`."},
        "page":   {"path": _p(f"src/pages/{slug}.{ext_page}"),
                   "desc": f"React page that consumes the {slug} client and renders the response."},
    }


# iter-17.14 — Pinned dependency baselines per stack. The build-manifest
# task description passes these to the Coder so pom.xml / build.gradle /
# package.json come out with consistent versions across services and
# don't drift wave-to-wave. Update these baselines here in ONE place
# instead of in the coder prompt.
_STACK_BASELINE = {
    ("java", "spring-boot"): {
        "build": "maven",
        "java_version": "21",
        "framework_version": "3.3.4",
        "extras": [
            "spring-boot-starter-web:3.3.4",
            "spring-boot-starter-data-jpa:3.3.4",
            "spring-boot-starter-validation:3.3.4",
            "spring-boot-starter-actuator:3.3.4",
            "com.oracle.database.jdbc:ojdbc11:23.5.0.24.07",
            "org.projectlombok:lombok:1.18.34",
            "org.springframework.boot:spring-boot-starter-test:3.3.4",
        ],
    },
    ("java", "quarkus"): {
        "build": "maven",
        "java_version": "21",
        "framework_version": "3.15.1",
        "extras": [
            "io.quarkus:quarkus-resteasy-reactive:3.15.1",
            "io.quarkus:quarkus-hibernate-orm-panache:3.15.1",
            "io.quarkus:quarkus-jdbc-oracle:3.15.1",
        ],
    },
    ("kotlin", "spring-boot"): {
        "build": "gradle",
        "kotlin_version": "2.0.20",
        "framework_version": "3.3.4",
        "extras": [
            "org.springframework.boot:spring-boot-starter-web:3.3.4",
            "org.jetbrains.kotlin:kotlin-stdlib:2.0.20",
        ],
    },
    ("python", "fastapi"): {
        "build": "poetry",
        "python_version": "3.12",
        "framework_version": "0.115.0",
        "extras": [
            "fastapi==0.115.0",
            "uvicorn[standard]==0.30.6",
            "pydantic==2.9.2",
            "sqlalchemy==2.0.35",
            "asyncpg==0.29.0",
            "pytest==8.3.3",
        ],
    },
    ("typescript", "nest"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "10.4.4",
        "extras": [
            "@nestjs/core@10.4.4", "@nestjs/common@10.4.4",
            "typescript@5.6.2", "typeorm@0.3.20",
        ],
    },
    ("typescript", "express"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "4.21.0",
        "extras": ["express@4.21.0", "typescript@5.6.2", "typeorm@0.3.20"],
    },
    ("typescript", "react"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "19.0.0",
        "extras": [
            "react@19.0.0", "react-dom@19.0.0",
            "axios@1.7.7", "typescript@5.6.2",
            "vite@5.4.8",
        ],
    },
    ("typescript", "next"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "14.2.13",
        "extras": ["next@14.2.13", "react@19.0.0", "react-dom@19.0.0"],
    },
    ("typescript", "vue"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "3.5.10",
        "extras": ["vue@3.5.10", "vite@5.4.8", "typescript@5.6.2"],
    },
    ("typescript", "angular"): {
        "build": "npm",
        "node_version": "20",
        "framework_version": "18.2.7",
        "extras": ["@angular/core@18.2.7", "typescript@5.5.4"],
    },
}


def _stack_baseline(lang: str, framework: str) -> Dict[str, Any]:
    return _STACK_BASELINE.get(
        (lang, framework),
        {"build": "unknown", "framework_version": "latest", "extras": []},
    )


def _be_manifest_task(lang: str, framework: str, service: str) -> Dict[str, str]:
    """Return the {path, desc, language} for the BE build-manifest task
    (iter-17.14). One manifest per BE service — placed at the root of the
    services/<svc>/ project so `mvn` / `gradle` / `poetry` can consume it
    without a prefix walk. Description enumerates PINNED dependency
    versions so downstream regenerations don't drift."""
    prefix = _svc_prefix(service) or "backend"
    baseline = _stack_baseline(lang, framework)
    deps_pin = ", ".join(baseline.get("extras", []))
    if lang == "java" and "spring" in framework:
        return {
            "path": f"{prefix}/pom.xml",
            "language": "xml",
            "desc": (f"Maven `pom.xml` for Spring Boot {baseline['framework_version']} "
                     f"on Java {baseline.get('java_version', '21')}. "
                     f"PINNED dependencies (exact versions, NO ranges, NO ${{spring.version}} "
                     f"variables): {deps_pin}. Group = `com.lama`, artifact = "
                     f"`{re.sub(r'[^a-z0-9]+', '-', service.lower()).strip('-') or 'app'}`. "
                     f"Include `spring-boot-maven-plugin` with the same version."),
        }
    if lang == "java" and "quarkus" in framework:
        return {
            "path": f"{prefix}/pom.xml", "language": "xml",
            "desc": (f"Maven `pom.xml` for Quarkus {baseline['framework_version']}. "
                     f"PINNED deps: {deps_pin}."),
        }
    if lang == "kotlin":
        return {
            "path": f"{prefix}/build.gradle.kts", "language": "kotlin",
            "desc": (f"Gradle KTS build script for Kotlin "
                     f"{baseline.get('kotlin_version', '2.0.20')} + "
                     f"Spring Boot {baseline['framework_version']}. "
                     f"PINNED deps: {deps_pin}."),
        }
    if lang == "python":
        return {
            "path": f"{prefix}/pyproject.toml", "language": "toml",
            "desc": (f"Poetry `pyproject.toml` for {framework or 'FastAPI'} on "
                     f"Python {baseline.get('python_version', '3.12')}. "
                     f"PINNED deps (== not ^): {deps_pin}."),
        }
    if lang in {"typescript", "javascript", "node", "nodejs"}:
        return {
            "path": f"{prefix}/package.json", "language": "json",
            "desc": (f"npm `package.json` for {framework or 'Express'}. "
                     f"PINNED deps (exact, no ^ / ~): {deps_pin}. "
                     f"Node engine `>=20 <21`."),
        }
    if lang == "go":
        return {
            "path": f"{prefix}/go.mod", "language": "go",
            "desc": (f"Go module for {framework or 'gin'} handler. "
                     f"Use module path `github.com/lama/{service.lower() or 'app'}`."),
        }
    if lang in {"csharp", "dotnet"}:
        return {
            "path": f"{prefix}/{_pascal_case(service or 'App')}.csproj", "language": "xml",
            "desc": ("ASP.NET Core `.csproj` targeting `net8.0`. "
                     "PINNED PackageReference versions for Microsoft.AspNetCore.OpenApi, "
                     "Swashbuckle.AspNetCore, Microsoft.EntityFrameworkCore."),
        }
    return {
        "path": f"{prefix}/pyproject.toml", "language": "toml",
        "desc": f"Poetry manifest for {lang}/{framework}. PINNED deps: {deps_pin}.",
    }


def _fe_manifest_task(lang: str, framework: str, service: str) -> Dict[str, str]:
    prefix = _svc_prefix(service) or "frontend"
    baseline = _stack_baseline(lang, framework)
    deps_pin = ", ".join(baseline.get("extras", []))
    return {
        "path": f"{prefix}/package.json", "language": "json",
        "desc": (f"npm `package.json` for {framework or 'React'} "
                 f"{baseline.get('framework_version', 'latest')} on Node "
                 f"{baseline.get('node_version', '20')}. "
                 f"PINNED deps (exact versions, no ^ / ~, no `latest`): "
                 f"{deps_pin}. Include `scripts.build`, `scripts.dev`, "
                 f"`scripts.test`."),
    }


def _target_stack_hint(be: Dict[str, str], fe: Dict[str, str]) -> str:
    """Compact multi-line hint injected into the Coder user prompt so
    the LLM emits code for the correct stack even if the target_path
    extension is somehow ambiguous.

    iter-17.14 — Also carries: (a) hard "no markdown fences" directive
    the small models (qwen2.5-coder:7b) routinely violate, (b) pinned
    dependency baselines so pom.xml / package.json / pyproject.toml
    come out with consistent versions across services + waves, and (c)
    an explicit "no attempt=N / retry / apology preamble" ban that
    plugs the "(attempt=4)" leak seen in ceots iter-17.13.
    """
    be_base = _stack_baseline(be["lang"], be["framework"])
    fe_base = _stack_baseline(fe["lang"], fe["framework"])
    return (
        "===== TARGET STACK =====\n"
        f"backend  lang={be['lang']}  framework={be['framework']}  "
        f"build={be_base.get('build')}  framework_version={be_base.get('framework_version')}\n"
        f"frontend lang={fe['lang']}  framework={fe['framework']}  "
        f"build={fe_base.get('build')}  framework_version={fe_base.get('framework_version')}\n"
        "\n"
        "OUTPUT RULES (violations are auto-stripped and logged):\n"
        "  1. Return the raw source file ONLY. First character MUST be a\n"
        "     valid source token for the target language.\n"
        "  2. DO NOT wrap the file in ```-fences (no ```java, ```python,\n"
        "     ```json, ```xml, ``` on its own line). The write-time\n"
        "     sanitiser will strip them but every stripped fence is a\n"
        "     verifier warning against your run.\n"
        "  3. DO NOT emit `(attempt=N)`, `retry`, `sorry`, `apolog`,\n"
        "     `here is`, or any conversational preamble anywhere in the\n"
        "     file. These leak past the sanitiser as-is.\n"
        "  4. Build manifests (pom.xml / build.gradle / package.json /\n"
        "     pyproject.toml) MUST use the PINNED versions supplied in\n"
        "     the task description — no `latest`, no `^`, no `~`, no\n"
        "     `${property}` placeholders unless they are declared in the\n"
        "     same file.\n"
        "  5. PRODUCTION-GRADE ONLY (iter-17.15). NO placeholder / stub /\n"
        "     empty-shell files. An empty `interface FooRepository extends\n"
        "     JpaRepository<Foo,Long> { /* BR: ... */ }` is REJECTED —\n"
        "     the write-time guard marks the task BLOCKED and the\n"
        "     operator sees the reject in the timeline. Emit every\n"
        "     custom finder / mapped method / mapped field / render tree\n"
        "     the envelope + KB imply. Forbidden anywhere in the file:\n"
        "     `TODO`, `FIXME`, `NotImplementedError`,\n"
        "     `UnsupportedOperationException`, empty class/interface\n"
        "     body, `<div>TODO</div>`, `return <></>`, `pass  # stub`.\n"
        "  6. If context is genuinely missing, embed a\n"
        "     `// TRACEABILITY-GAP:` (or `# TRACEABILITY-GAP:`) comment\n"
        "     WITH a fully working best-effort implementation. Never\n"
        "     ship an empty shell.\n"
    )


# ── Agent-run logging ──────────────────────────────────────────────────────
async def _log_codegen_agent_run(
    project_id: str,
    agent: str,
    phase: str,
    status: str = "running",
    task_id: str = "",
    envelope_id: str = "",
    input_summary: str = "",
    output_summary: str = "",
    score: Optional[float] = None,
    details: Optional[dict] = None,
    error: str = "",
    duration_ms: int = 0,
) -> str:
    """Persist one CodeGen agent execution step for the timeline."""
    from uuid import uuid4
    run_id = uuid4().hex
    await codegen_agent_runs.insert_one({
        "_id": run_id,
        "id": run_id,
        "project_id": project_id,
        "stage": "CodeGen",
        "agent": agent,
        "phase": phase,
        "task_id": task_id,
        "envelope_id": envelope_id,
        "status": status,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "score": score,
        "details": details or {},
        "error": error,
        "duration_ms": duration_ms,
        "created_at": _now_iso(),
    })
    return run_id


async def _update_codegen_agent_run(run_id: str, **fields) -> None:
    if not run_id:
        return
    await codegen_agent_runs.update_one(
        {"_id": run_id},
        {"$set": fields},
    )


# ── State helpers ──────────────────────────────────────────────────────────
async def _get_codegen_state(project_id: str) -> Dict[str, Any]:
    """Fetch (or lazily create) the singleton pipeline state doc for
    this project. New docs land in `idle` so `start` accepts them."""
    doc = await codegen_pipeline_state.find_one(
        {"project_id": project_id}, {"_id": 0},
    )
    if doc:
        return doc
    fresh = {
        "project_id": project_id,
        "status": _CODEGEN_STATUS_IDLE,
        "current_wave": 0,
        "build_system_be": "",
        "build_system_fe": "",
        "envelope_confirmed_at": None,
        "tasks_confirmed_at": None,
        "last_error": "",
        "started_at": None,
        "completed_at": None,
        "br_coverage_pct": 0.0,
        "br_missing": [],
        "envelope_count": 0,
        "task_count": 0,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await codegen_pipeline_state.update_one(
        {"project_id": project_id},
        {"$set": fresh},
        upsert=True,
    )
    return fresh


async def _update_codegen_state(project_id: str, **fields) -> None:
    fields = {**fields, "updated_at": _now_iso()}
    await codegen_pipeline_state.update_one(
        {"project_id": project_id},
        {"$set": fields},
        upsert=True,
    )


async def _audit_multi_agent(project_id: str, action: str, details: Optional[dict] = None) -> None:
    """Every state-mutating multi-agent endpoint calls this so admins can
    reconstruct the pipeline from the audit log alone."""
    try:
        await audit_log.insert_one({
            "entity": "codegen_multi_agent",
            "entity_id": project_id,
            "action": action,
            "actor": "system",
            "at": _now_iso(),
            "details": details or {},
        })
    except Exception:
        logger.exception("codegen multi-agent audit-log write failed for %s / %s", project_id, action)


# ── LLM helper — resolve prompt template ──────────────────────────────────
async def _get_codegen_prompt(agent_key: str) -> str:
    """Load the seeded prompt template for one of the codegen.* agents.
    Returns "" if the row is missing — callers keep going with an empty
    system message so a missing seed can't kill the pipeline."""
    try:
        doc = await prompts_col.find_one({"key": agent_key}, {"_id": 0, "template": 1})
        if doc and doc.get("template"):
            return str(doc["template"])
    except Exception:
        logger.exception("failed to load prompt for %s", agent_key)
    return ""


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON object extraction from an LLM response. The
    single-shot CodeGen flow has its own helpers; we deliberately don't
    reuse them because they carry a lot of gap-recovery baggage. Small
    self-contained helper is easier to reason about here.

    Returns None rather than raising — every caller relies on that.

    The previous fence handling did ``s.split("```", 2)[-1]``, which on a
    CORRECTLY fenced block yields the empty string following the CLOSING
    fence rather than the content between the fences:

        '```json\\n{...}\\n```'.split('```', 2)
            -> ['', 'json\\n{...}\\n', '']      and [-1] is ''

    so the brace scan below had nothing left to scan. That is the single
    most common shape an LLM emits — a local qwen2.5-coder:7b produced it
    six times out of six when asked to score a file — and it silently cost
    the verifier every one of those verdicts, marking good files
    VERIFY_FAILED at confidence 0.0.

    The brace scan alone already handles fenced, unfenced and
    prose-wrapped replies, so the fence preamble is gone rather than
    repaired: it was both wrong and unnecessary.
    """
    if not text:
        return None
    s = text.strip()
    if not s:
        return None

    # Fast path: the whole reply is the object (what JSON mode produces).
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    # Otherwise scan for the outermost braces. This covers ```json fences,
    # bare fences, prose preambles and trailing chatter in one step, and is
    # unaffected by a reply truncated mid-fence at max_tokens.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(s[start:end + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


# ══════════════════════════════════════════════════════════════════════════
# Core coroutines — invoked as FastAPI background tasks
# ══════════════════════════════════════════════════════════════════════════

async def _deterministic_codegen_envelopes(project_id: str) -> List[Dict[str, Any]]:
    """iter-17.2 — Build envelopes directly from `arch_services` (which
    already carries the frozen Legacy → New API mapping produced during
    Architecture recommend + api_contracts jobs) and the KB owl extractor
    output (for db_tables per controller class).

    Each new endpoint on each service becomes one envelope. This is the
    same information the CodeGen page's `/api-mapping` route surfaces,
    so envelopes are guaranteed to match what the operator already sees.

    Returns a list of envelope-shaped dicts ready for insertion into
    `codegen_envelopes`. Empty list means Architecture is not populated
    enough to derive envelopes deterministically — caller should fall
    back to the LLM path.
    """
    # 1. Load arch_services + self-heal from stage_context if empty
    #    (mirrors the /api-mapping backfill).
    svcs = await arch_services.find(
        {"project_id": project_id},
        {"_id": 0, "name": 1, "display_name": 1, "frontend": 1, "kind": 1,
         "backend_lang": 1, "routes_detail": 1, "api_endpoints": 1},
    ).to_list(200)
    if not svcs:
        try:
            if await _backfill_arch_services_from_stage_context(project_id) > 0:
                svcs = await arch_services.find(
                    {"project_id": project_id},
                    {"_id": 0, "name": 1, "display_name": 1, "frontend": 1,
                     "kind": 1, "backend_lang": 1, "routes_detail": 1,
                     "api_endpoints": 1},
                ).to_list(200)
        except Exception:
            pass
    if not svcs:
        return []

    # 2. Best-effort per-route db_tables from the deterministic KB
    #    traceability. Not required — envelopes without table info are
    #    still useful (Planner will figure it out from the DDL).
    tables_by_path: Dict[str, List[str]] = {}
    try:
        from tools_kb_builder import build_traceability_map  # type: ignore
        trace = await build_traceability_map(project_id) or {}
        for row in (trace.get("api_to_db") or []):
            p = (row.get("api") or "").strip()
            if p:
                tables_by_path.setdefault(p, sorted(row.get("tables") or []))
    except Exception:
        # tools_kb_builder is optional; deterministic envelopes still work.
        pass

    envelopes: List[Dict[str, Any]] = []
    seq = 0
    for svc in svcs:
        svc_name = svc.get("name", "") or ""
        is_frontend = bool(svc.get("frontend"))
        backend_lang = (svc.get("backend_lang") or "").lower()

        # Map legacy routes_detail by path so we can lift controller_class
        # / module onto the matching new endpoint envelope.
        legacy_by_path: Dict[str, Dict[str, Any]] = {}
        for rd in (svc.get("routes_detail") or []):
            if isinstance(rd, dict) and rd.get("path"):
                legacy_by_path.setdefault(rd["path"], rd)

        for ep in (svc.get("api_endpoints") or []):
            if not ep:
                continue
            parts = str(ep).strip().split(None, 1)
            if len(parts) == 2:
                verb, path = parts[0].upper(), parts[1]
            else:
                verb, path = "ANY", parts[0]

            legacy = legacy_by_path.get(path) or {}
            side = "frontend" if is_frontend else "backend"
            # Simple layer guess — Planner will refine, but we pick a
            # sensible default so the envelope table isn't blank.
            layer = "page" if is_frontend else "controller"

            seq += 1
            envelopes.append({
                "envelope_id": f"ENV-{seq:04d}",
                "endpoint_method": verb,
                "endpoint_path": path,
                "controller_class": legacy.get("class") or "",
                "controller_file": legacy.get("module") or "",
                "service_class": "",
                "service_file": "",
                "service_method": "",
                "business_logic_summary": (
                    f"Migrated from legacy route {legacy.get('class') or 'unknown'}.{path} "
                    f"(module {legacy.get('module') or 'unknown'}) → new {side} "
                    f"endpoint {verb} {path} in service '{svc_name}'."
                ) if legacy else (
                    f"New {side} endpoint {verb} {path} in service '{svc_name}' "
                    "(no direct legacy mapping — greenfield route on the target)."
                ),
                "repository_class": "",
                "repository_file": "",
                "db_tables": tables_by_path.get(path, []),
                "db_operations": [],
                "external_calls": [],
                "files_affected": [],
                "action": "NEW",
                "risk_level": "medium" if legacy else "low",
                "layer": layer,
                "side": side,
                "acceptance_criteria": [
                    f"Endpoint {verb} {path} responds with the target contract shape.",
                    f"Behaviour is functionally equivalent to legacy {legacy.get('class') or path}.",
                ],
                "br_ids": [],
                "_service_name": svc_name,
                "_backend_lang": backend_lang,
            })

    return envelopes


async def _run_multi_agent_codegen(project_id: str, model: Optional[str] = None) -> None:
    """Phase 1 of the multi-agent CodeGen pipeline.

    1. Require frozen Architecture StageContext.
    2. Build envelopes deterministically from the frozen Legacy → New
       API mapping (iter-17.2 — this replaces a ~40K-char LLM call that
       was timing out on Ollama at 600s for realistic projects).
    3. Only fall back to the LLM Context Manager if the deterministic
       path yields fewer than 3 envelopes.
    4. Persist envelopes into codegen_envelopes.
    5. State → envelopes_pending. audit_log.
    """
    try:
        # Guard: architecture must be frozen. `require_stage_context`
        # raises HTTPException(400) if it's not; catch that and record a
        # failed state so the UI can surface it.
        try:
            arch_ctx = await require_stage_context(project_id, "Architecture", "CodeGen")
        except HTTPException as he:
            await _update_codegen_state(
                project_id,
                status=_CODEGEN_STATUS_FAILED,
                last_error=str(he.detail),
                completed_at=_now_iso(),
            )
            await _log_codegen_agent_run(
                project_id, "super_agent", "init",
                status="failed",
                error=str(he.detail),
                input_summary="Architecture StageContext not frozen",
            )
            return

        # Discovery KB (YAML) — best-effort, empty string if absent.
        try:
            disc_ctx = await stage_context_col.find_one(
                {"project_id": project_id, "stage": "Discovery"}, {"_id": 0},
            )
        except Exception:
            disc_ctx = None
        disc_toon = ((disc_ctx or {}).get("toon_summary") or "")[:20000]

        arch_outputs = (arch_ctx or {}).get("outputs") or {}
        arch_digest = json.dumps({
            "services": (arch_outputs.get("services") or [])[:40],
            "api_contracts": (arch_outputs.get("api_contracts") or [])[:60],
        }, default=str)[:20000]

        await _log_codegen_agent_run(
            project_id, "super_agent", "init",
            status="completed",
            input_summary="Architecture frozen; delegating to Context Manager",
        )

        # iter-17.2 — Deterministic-first path. Try to derive envelopes
        # from the frozen Architecture Legacy → New API mapping. On real
        # projects this returns dozens of envelopes in milliseconds and
        # completely avoids the 40K-char LLM prompt that was timing out
        # against Ollama at the 600s ceiling.
        det_run_id = await _log_codegen_agent_run(
            project_id, "context_manager", "discover",
            input_summary="Deterministic pass over arch_services.api_endpoints + routes_detail",
        )
        try:
            det_envelopes = await _deterministic_codegen_envelopes(project_id)
        except Exception as _det_exc:  # never let the deterministic path kill the pipeline
            logger.exception("deterministic envelope build failed: %s", _det_exc)
            det_envelopes = []

        if len(det_envelopes) >= 3:
            envelopes = det_envelopes
            await _update_codegen_agent_run(
                det_run_id, status="completed",
                output_summary=(
                    f"Discovered {len(envelopes)} envelope(s) deterministically "
                    "from Architecture mapping — no LLM call needed."
                ),
                details={"source": "deterministic", "envelope_count": len(envelopes)},
            )
        else:
            # Fallback: small-prompt LLM call. We only get here when the
            # deterministic path is starved (Architecture mapping empty
            # or nearly empty). Keep the prompt short so Ollama can finish.
            await _update_codegen_agent_run(
                det_run_id, status="completed",
                output_summary=(
                    f"Deterministic pass yielded only {len(det_envelopes)} "
                    "envelope(s); falling back to LLM Context Manager."
                ),
                details={"source": "deterministic_partial",
                         "envelope_count": len(det_envelopes)},
            )

            prompt_template = await _get_codegen_prompt("codegen.context_manager")
            # Aggressively bound both context slices — for the fallback
            # path we don't need the full KB, just enough for the LLM to
            # invent a handful of envelopes.
            user_prompt = (
                "The deterministic Legacy → New API mapping in Architecture is "
                "empty or too small to drive CodeGen. Propose up to 10 API-to-DB "
                "envelopes based on the summaries below. Do NOT invent endpoints "
                "that are not implied by the Architecture services list.\n\n"
                "===== ARCHITECTURE (frozen StageContext, trimmed) =====\n"
                f"{arch_digest[:6000]}\n\n"
                "===== DISCOVERY KB (TOON, trimmed) =====\n"
                f"{disc_toon[:4000]}\n\n"
                "Return ONLY the JSON object described in your system prompt."
            )

            llm_run_id = await _log_codegen_agent_run(
                project_id, "context_manager", "discover_llm_fallback",
                input_summary=(
                    f"Arch digest {min(len(arch_digest), 6000)} chars, "
                    f"Discovery {min(len(disc_toon), 4000)} chars — fallback LLM path"
                ),
            )
            try:
                response = await chat_completion(
                    messages=[
                        {"role": "system", "content": prompt_template or "You are the CodeGen Context Manager."},
                        {"role": "user", "content": user_prompt},
                    ],
                    agent_key="codegen.context_manager",
                    project_id=project_id,
                    model=model,
                    temperature=0.1,
                    max_tokens=4000,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                await _update_codegen_agent_run(
                    llm_run_id, status="failed", error=str(exc),
                )
                await _update_codegen_state(
                    project_id, status=_CODEGEN_STATUS_FAILED,
                    last_error=f"Context Manager failed: {exc}",
                    completed_at=_now_iso(),
                )
                return

            response_text = response.get("content", "") if isinstance(response, dict) else str(response)
            parsed = _extract_json_object(response_text) or {}
            envelopes = [e for e in (parsed.get("envelopes") or []) if isinstance(e, dict)]
            await _update_codegen_agent_run(
                llm_run_id, status="completed",
                output_summary=f"LLM fallback proposed {len(envelopes)} envelope(s)",
                details={"source": "llm_fallback"},
            )

        # Persist envelopes.
        await codegen_envelopes.delete_many({"project_id": project_id})
        for idx, env in enumerate(envelopes):
            envelope_id = env.get("envelope_id") or f"ENV-{idx + 1:04d}"
            doc = {
                "id": f"cgenv_{project_id[:8]}_{idx + 1:04d}",
                "project_id": project_id,
                "stage": "CodeGen",
                "envelope_id": envelope_id,
                "status": "draft",
                "endpoint_method": (env.get("endpoint_method") or "").upper(),
                "endpoint_path": env.get("endpoint_path") or "",
                "controller_class": env.get("controller_class") or "",
                "controller_file": env.get("controller_file") or "",
                "service_class": env.get("service_class") or "",
                "service_file": env.get("service_file") or "",
                "service_method": env.get("service_method") or "",
                "business_logic_summary": env.get("business_logic_summary") or "",
                "repository_class": env.get("repository_class") or "",
                "repository_file": env.get("repository_file") or "",
                "db_tables": list(env.get("db_tables") or []),
                "db_operations": list(env.get("db_operations") or []),
                "external_calls": list(env.get("external_calls") or []),
                "files_affected": list(env.get("files_affected") or []),
                "action": env.get("action") or "NEW",
                "risk_level": env.get("risk_level") or "low",
                "layer": env.get("layer") or "",
                "side": env.get("side") or "backend",
                "acceptance_criteria": list(env.get("acceptance_criteria") or []),
                "br_ids": list(env.get("br_ids") or []),
                "created_at": _now_iso(),
            }
            await codegen_envelopes.insert_one(doc)

        await _update_codegen_state(
            project_id,
            status=_CODEGEN_STATUS_ENV_PENDING,
            envelope_count=len(envelopes),
            started_at=_now_iso(),
            last_error="",
        )
        await _audit_multi_agent(project_id, "context_manager_completed",
                                 {"envelope_count": len(envelopes)})

    except Exception as exc:
        logger.exception("multi-agent codegen phase-1 failed: %s", exc)
        await _update_codegen_state(
            project_id, status=_CODEGEN_STATUS_FAILED,
            last_error=str(exc), completed_at=_now_iso(),
        )


async def _continue_multi_agent_codegen_after_envelope_confirm(
    project_id: str,
    model: Optional[str] = None,
) -> None:
    """Phase 2 of the multi-agent CodeGen pipeline.

    1. Load approved envelopes.
    2. Run codegen.build_tool_selector → persist be/fe on pipeline_state.
    3. Run codegen.planner → tasks.
    4. Apply `_route_task_to_coder` to each task; persist.
    5. State → tasks_pending. audit_log.
    """
    try:
        # 1. Load approved envelopes.
        env_docs = await codegen_envelopes.find(
            {"project_id": project_id},
        ).to_list(1000)
        approved = [e for e in env_docs if e.get("status") in ("approved", "draft")]

        # 2. Build-tool selector.
        try:
            arch_ctx = await require_stage_context(project_id, "Architecture", "CodeGen")
        except HTTPException as he:
            await _update_codegen_state(
                project_id, status=_CODEGEN_STATUS_FAILED,
                last_error=str(he.detail), completed_at=_now_iso(),
            )
            return
        arch_outputs = (arch_ctx or {}).get("outputs") or {}
        # iter-17.13 — Load the project so we can fall through to
        # `project.target_tech` / `project.target_stack_selection` when
        # Architecture never populated `target_backend`. Without this
        # fallback the planner defaulted to Python + FastAPI even when
        # the operator picked "Spring Boot / Java" via the Others picker.
        proj_doc = await projects.find_one(
            {"id": project_id},
            {"_id": 0, "target_tech": 1, "target_stack_selection": 1},
        ) or {}
        # iter-17.12 — Resolve the target BE + FE stack ONCE up front so
        # both the deterministic planner (below) and the Coder user
        # prompt (via pipeline state) emit code for the correct language.
        be_target = _resolve_target_backend(arch_outputs, proj_doc)
        fe_target = _resolve_target_frontend(arch_outputs, proj_doc)
        target_stack = json.dumps({
            "backend": (arch_outputs.get("target_backend")
                        or arch_outputs.get("backend")
                        or be_target),
            "frontend": (arch_outputs.get("target_frontend")
                         or arch_outputs.get("frontend")
                         or fe_target),
        }, default=str)[:5000]

        bt_prompt = await _get_codegen_prompt("codegen.build_tool_selector")
        bt_run = await _log_codegen_agent_run(
            project_id, "build_tool_selector", "select",
            input_summary=target_stack[:400],
        )
        be_bs = ""
        fe_bs = ""
        try:
            bt_resp = await chat_completion(
                messages=[
                    {"role": "system", "content": bt_prompt or "You pick BE and FE build systems."},
                    {"role": "user", "content":
                        f"Target stack:\n{target_stack}\n\nReturn JSON with be, fe, rationale."},
                ],
                agent_key="codegen.build_tool_selector",
                project_id=project_id,
                model=model,
                temperature=0.0,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )
            bt_text = bt_resp.get("content", "") if isinstance(bt_resp, dict) else str(bt_resp)
            bt_parsed = _extract_json_object(bt_text) or {}
            be_bs = str(bt_parsed.get("be") or "")
            fe_bs = str(bt_parsed.get("fe") or "")
            await _update_codegen_agent_run(
                bt_run, status="completed",
                output_summary=f"be={be_bs}, fe={fe_bs}",
                details={"rationale": bt_parsed.get("rationale", "")},
            )
        except Exception as exc:
            await _update_codegen_agent_run(bt_run, status="failed", error=str(exc))

        await _update_codegen_state(
            project_id,
            build_system_be=be_bs,
            build_system_fe=fe_bs,
            envelope_confirmed_at=_now_iso(),
            # iter-17.12 — Persist the resolved target BE/FE stack so
            # `_run_coder_for_task` can inject an explicit "target stack"
            # hint into the Coder user prompt at execution time.
            target_backend_lang=be_target["lang"],
            target_backend_framework=be_target["framework"],
            target_frontend_lang=fe_target["lang"],
            target_frontend_framework=fe_target["framework"],
        )
        await _audit_multi_agent(project_id, "build_tool_selected",
                                 {"be": be_bs, "fe": fe_bs})

        # 3. Planner — deterministic-first (iter-17.3, same rationale as
        # iter-17.2 Context Manager fix). For real projects the envelope
        # → task expansion is mechanical: each BE envelope becomes a
        # small entity/repository/service/controller/test task set; each
        # FE envelope becomes an api_client + page task set. Deriving
        # this in Python avoids feeding 30K chars of envelope digest to
        # Ollama with max_tokens=12000 (which was timing out at 600s).
        planner_run = await _log_codegen_agent_run(
            project_id, "planner", "plan",
            input_summary=f"{len(approved)} envelope(s), be={be_bs}, fe={fe_bs}",
        )

        def _slugify_path(p: str) -> str:
            import re as _re
            s = _re.sub(r"[^A-Za-z0-9]+", "_", (p or "root").strip("/")).strip("_")
            return (s or "root").lower()

        det_tasks: List[Dict[str, Any]] = []
        seq = 0

        # iter-17.14 — Resolve concrete BE / FE service names from
        # arch_services so the deterministic planner can emit each
        # generated file under `services/<svc>/…`. Falls back to sensible
        # defaults when Architecture didn't name the services OR when
        # the arch_services collection is unreachable (fake-DB tests).
        try:
            _arch_svcs = await arch_services.find(
                {"project_id": project_id},
                {"_id": 0, "name": 1, "side": 1, "kind": 1},
            ).to_list(50)
        except Exception:
            _arch_svcs = []
        be_service_name = ""
        fe_service_name = ""
        for _s in _arch_svcs:
            _sd = (_s.get("side") or "").lower()
            _nm = _s.get("name") or ""
            if _sd == "frontend" or "front" in (_s.get("kind") or "").lower() or _nm.lower() in {"frontend", "web", "ui", "client"}:
                fe_service_name = fe_service_name or _nm
            elif _sd == "backend" or _nm:
                be_service_name = be_service_name or _nm
        # If arch_services didn't tag a frontend explicitly, keep the
        # BE service name for BE and default FE to "web".
        if not be_service_name:
            be_service_name = "api"
        if not fe_service_name:
            fe_service_name = "web"

        # iter-17.14 — Emit ONE build-manifest task per service at Wave 1
        # so pom.xml / build.gradle / package.json / pyproject.toml are
        # produced BEFORE the source files (compilable snapshot after
        # Wave 1) and every regeneration reuses the pinned versions from
        # `_STACK_BASELINE` — no dep-drift wave-to-wave.
        be_manifest = _be_manifest_task(be_target["lang"], be_target["framework"], be_service_name)
        fe_manifest = _fe_manifest_task(fe_target["lang"], fe_target["framework"], fe_service_name)

        def _mk(layer: str, phase: str, wave: int, wave_name: str,
                coder: str, target: str, title: str, desc: str,
                env: Dict[str, Any], br_ids: List[str]) -> Dict[str, Any]:
            nonlocal seq
            seq += 1
            return {
                "task_id": f"TASK-{seq:04d}",
                "envelope_id": env.get("envelope_id") or "",
                "title": title,
                "description": desc,
                "phase": phase,
                "layer": layer,
                "action": env.get("action") or "NEW",
                "wave": wave,
                "wave_name": wave_name,
                "target_path": target,
                "assigned_to": coder,
                "depends_on": [],
                "br_ids": br_ids,
            }

        _synthetic_env = {"envelope_id": "MANIFEST", "action": "NEW"}
        det_tasks.append(_mk(
            "manifest", "scaffold", 1, "Wave 1 — Scaffolding",
            "coder_be", be_manifest["path"],
            f"Build manifest for {be_service_name}",
            be_manifest["desc"], _synthetic_env, [],
        ))
        det_tasks.append(_mk(
            "manifest", "scaffold", 1, "Wave 1 — Scaffolding",
            "coder_fe", fe_manifest["path"],
            f"Build manifest for {fe_service_name}",
            fe_manifest["desc"], _synthetic_env, [],
        ))

        for env in approved:
            env_id = env.get("envelope_id") or ""
            side = (env.get("side") or "backend").lower()
            path = env.get("endpoint_path") or ""
            method = (env.get("endpoint_method") or "ANY").upper()
            slug = _slugify_path(path) or env_id.lower().replace("-", "_")
            br_ids = list(env.get("br_ids") or [])

            if side == "frontend":
                # iter-17.14 — Frontend files live under services/<fe_svc>/
                # (was services/<svc>/frontend). Vue/Angular targets get
                # their idiomatic layout via `_fe_task_layout`.
                fe_layout = _fe_task_layout(
                    fe_target["lang"], fe_target["framework"],
                    slug, method, path, service=fe_service_name,
                )
                det_tasks.append(_mk(
                    "api_client", "scaffold", 1, "Wave 1 — Scaffolding",
                    "coder_fe", fe_layout["client"]["path"],
                    f"API client for {method} {path}",
                    f"{fe_layout['client']['desc']} Envelope {env_id}.",
                    env, br_ids,
                ))
                det_tasks.append(_mk(
                    "page", "logic", 2, "Wave 2 — Business Logic",
                    "coder_fe", fe_layout["page"]["path"],
                    f"Page for {method} {path}",
                    f"{fe_layout['page']['desc']}",
                    env, br_ids,
                ))
            else:
                # iter-17.14 — Backend files live under services/<be_svc>/
                # (was repo-root `backend/...`). Java/Kotlin/TS/Node/Go/
                # .NET/Python targets each get framework-idiomatic paths
                # via `_be_task_layout`.
                be_layout = _be_task_layout(
                    be_target["lang"], be_target["framework"],
                    slug, method, path, service=be_service_name,
                )
                biz_summary = env.get("business_logic_summary") or "migrate legacy behaviour."
                det_tasks.append(_mk(
                    "entity", "scaffold", 1, "Wave 1 — Scaffolding",
                    "coder_be", be_layout["entity"]["path"],
                    f"Entity for {env_id}",
                    f"{be_layout['entity']['desc']}",
                    env, br_ids,
                ))
                det_tasks.append(_mk(
                    "repository", "scaffold", 1, "Wave 1 — Scaffolding",
                    "coder_be", be_layout["repository"]["path"],
                    f"Repository for {env_id}",
                    f"{be_layout['repository']['desc']} "
                    f"Tables: {env.get('db_tables') or []}.",
                    env, br_ids,
                ))
                # iter-17.17 — DTO + Mapper + Exception are ALWAYS emitted
                # so controller/service can rely on typed request/response
                # payloads and domain exceptions instead of raw entities.
                if "dto" in be_layout:
                    det_tasks.append(_mk(
                        "dto", "scaffold", 1, "Wave 1 — Scaffolding",
                        "coder_be", be_layout["dto"]["path"],
                        f"DTOs for {env_id}",
                        f"{be_layout['dto']['desc']}",
                        env, br_ids,
                    ))
                if "exception" in be_layout:
                    det_tasks.append(_mk(
                        "exception", "scaffold", 1, "Wave 1 — Scaffolding",
                        "coder_be", be_layout["exception"]["path"],
                        f"Domain exceptions for {env_id}",
                        f"{be_layout['exception']['desc']}",
                        env, br_ids,
                    ))
                if "mapper" in be_layout:
                    det_tasks.append(_mk(
                        "mapper", "logic", 2, "Wave 2 — Business Logic",
                        "coder_be", be_layout["mapper"]["path"],
                        f"Mapper for {env_id}",
                        f"{be_layout['mapper']['desc']}",
                        env, br_ids,
                    ))
                det_tasks.append(_mk(
                    "service", "logic", 2, "Wave 2 — Business Logic",
                    "coder_be", be_layout["service"]["path"],
                    f"Service for {env_id}",
                    f"{be_layout['service']['desc']} Behaviour: {biz_summary}",
                    env, br_ids,
                ))
                det_tasks.append(_mk(
                    "controller", "logic", 2, "Wave 2 — Business Logic",
                    "coder_be", be_layout["controller"]["path"],
                    f"Route handler {method} {path}",
                    f"{be_layout['controller']['desc']}",
                    env, br_ids,
                ))
                det_tasks.append(_mk(
                    "test", "harden", 3, "Wave 3 — Hardening",
                    "coder_be", be_layout["test"]["path"],
                    f"Tests for {env_id}",
                    f"{be_layout['test']['desc']}",
                    env, br_ids,
                ))

        tasks: List[Dict[str, Any]] = []
        if det_tasks:
            tasks = det_tasks
            await _update_codegen_agent_run(
                planner_run, status="completed",
                output_summary=(
                    f"{len(tasks)} task(s) planned deterministically from "
                    f"{len(approved)} envelope(s) — no LLM call needed."
                ),
                details={"source": "deterministic", "task_count": len(tasks)},
            )
        else:
            # LLM fallback for degenerate case (no envelopes to expand).
            planner_prompt = await _get_codegen_prompt("codegen.planner")
            try:
                planner_resp = await chat_completion(
                    messages=[
                        {"role": "system", "content": planner_prompt or "You are the CodeGen Planner."},
                        {"role": "user", "content":
                            "No envelopes were approved. Propose up to 5 skeleton "
                            "tasks so the multi-agent pipeline can proceed. Every "
                            "task must set assigned_to='coder_be' or 'coder_fe'.\n\n"
                            f"===== BUILD SYSTEMS =====\nbe={be_bs}\nfe={fe_bs}\n\n"
                            "Return ONLY the JSON object described in your system prompt."},
                    ],
                    agent_key="codegen.planner",
                    project_id=project_id,
                    model=model,
                    temperature=0.1,
                    max_tokens=3000,
                    response_format={"type": "json_object"},
                )
                planner_text = planner_resp.get("content", "") if isinstance(planner_resp, dict) else str(planner_resp)
                planner_parsed = _extract_json_object(planner_text) or {}
                tasks = [t for t in (planner_parsed.get("tasks") or []) if isinstance(t, dict)]
                await _update_codegen_agent_run(
                    planner_run, status="completed",
                    output_summary=f"LLM fallback planned {len(tasks)} skeleton task(s)",
                    details={"source": "llm_fallback"},
                )
            except Exception as exc:
                await _update_codegen_agent_run(planner_run, status="failed", error=str(exc))
                await _update_codegen_state(
                    project_id, status=_CODEGEN_STATUS_FAILED,
                    last_error=f"Planner failed: {exc}", completed_at=_now_iso(),
                )
                return

        # 4. Apply deterministic BE/FE routing, persist tasks.
        await codegen_tasks.delete_many({"project_id": project_id})
        for idx, task in enumerate(tasks):
            planner_choice = (task.get("assigned_to") or "").strip()
            routed = _route_task_to_coder(task)
            if planner_choice and planner_choice != routed:
                # Warning agent_run so the audit trail shows a routing
                # override happened.
                await _log_codegen_agent_run(
                    project_id, "planner", "route_override",
                    status="completed",
                    task_id=task.get("task_id") or f"TASK-{idx + 1:04d}",
                    output_summary=f"router overrode {planner_choice} → {routed}",
                    details={
                        "target_path": task.get("target_path") or "",
                        "planner_choice": planner_choice,
                        "router_choice": routed,
                    },
                )
            doc = {
                "id": f"cgtask_{project_id[:8]}_{idx + 1:04d}",
                "project_id": project_id,
                "stage": "CodeGen",
                "task_id": task.get("task_id") or f"TASK-{idx + 1:04d}",
                "envelope_id": task.get("envelope_id") or "",
                "title": task.get("title") or "",
                "description": task.get("description") or "",
                "phase": task.get("phase") or "scaffold",
                "layer": task.get("layer") or "",
                "action": task.get("action") or "NEW",
                "wave": int(task.get("wave") or 1),
                "wave_name": task.get("wave_name") or "",
                "source_path": task.get("source_path") or "",
                "target_path": task.get("target_path") or "",
                "status": "PENDING",
                "assigned_to": routed,
                "depends_on": list(task.get("depends_on") or []),
                "confidence": 0.0,
                "verifier_score": 0.0,
                "verifier_checks": {},
                "rejection_count": 0,
                "notes": task.get("notes") or "",
                "error": "",
                "br_ids": list(task.get("br_ids") or []),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            await codegen_tasks.insert_one(doc)

        # planner_run was already updated to 'completed' in the
        # deterministic / fallback branches above; do NOT overwrite it
        # here — that would clobber the source=deterministic details.
        await _update_codegen_state(
            project_id,
            status=_CODEGEN_STATUS_TASKS_PENDING,
            task_count=len(tasks),
        )
        await _audit_multi_agent(
            project_id, "planner_completed",
            {"task_count": len(tasks)},
        )

    except Exception as exc:
        logger.exception("multi-agent codegen phase-2 failed: %s", exc)
        await _update_codegen_state(
            project_id, status=_CODEGEN_STATUS_FAILED,
            last_error=str(exc), completed_at=_now_iso(),
        )


async def _run_coder_for_task(
    project_id: str,
    task: Dict[str, Any],
    model: Optional[str],
) -> None:
    """Run the assigned coder for a single task. Handles the refusal
    envelope (`{"refusal": true, ...}`) by marking the task BLOCKED.
    Never raises — task-level errors are caught and persisted as BLOCKED
    so a single failing task can never poison its wave."""
    task_id = task.get("task_id") or ""
    assigned = task.get("assigned_to") or "coder_be"
    agent_key = f"codegen.{assigned}"

    run_id = await _log_codegen_agent_run(
        project_id, assigned, "code",
        task_id=task_id,
        envelope_id=task.get("envelope_id") or "",
        input_summary=f"target={task.get('target_path', '')} layer={task.get('layer', '')}",
    )
    await codegen_tasks.update_one(
        {"project_id": project_id, "task_id": task_id},
        {"$set": {"status": "IN_PROGRESS", "updated_at": _now_iso()}},
    )

    prompt_template = await _get_codegen_prompt(agent_key)

    # iter-17.12 — Load the persisted target-stack pair from pipeline_state
    # so the Coder's user prompt carries an explicit language + framework
    # hint. Without this the LLM inferred the target from the file
    # extension only — which was correct AFTER the deterministic planner
    # started emitting `.java` paths, but leaves no defence against a
    # future LLM-generated task with a mismatched extension.
    stack_hint = ""
    try:
        ps = await codegen_pipeline_state.find_one(
            {"project_id": project_id},
            {"_id": 0, "target_backend_lang": 1, "target_backend_framework": 1,
             "target_frontend_lang": 1, "target_frontend_framework": 1},
        ) or {}
        be_lang = ps.get("target_backend_lang")
        be_fw   = ps.get("target_backend_framework")
        fe_lang = ps.get("target_frontend_lang")
        fe_fw   = ps.get("target_frontend_framework")
        # iter-17.13 — pipeline_state may be missing these fields when the
        # run was planned before iter-17.12 landed. Fall through to the
        # same resolver the planner uses (arch_outputs → project.target_tech).
        if not (be_lang and be_fw and fe_lang and fe_fw):
            try:
                arch_ctx = await require_stage_context(project_id, "Architecture", "CodeGen")
                arch_outputs = (arch_ctx or {}).get("outputs") or {}
            except Exception:
                arch_outputs = {}
            proj_doc = await projects.find_one(
                {"id": project_id},
                {"_id": 0, "target_tech": 1, "target_stack_selection": 1},
            ) or {}
            be_res = _resolve_target_backend(arch_outputs, proj_doc)
            fe_res = _resolve_target_frontend(arch_outputs, proj_doc)
            be_lang = be_lang or be_res["lang"]
            be_fw   = be_fw   or be_res["framework"]
            fe_lang = fe_lang or fe_res["lang"]
            fe_fw   = fe_fw   or fe_res["framework"]
        be_pair = {"lang": be_lang, "framework": be_fw}
        fe_pair = {"lang": fe_lang, "framework": fe_fw}
        stack_hint = _target_stack_hint(be_pair, fe_pair)
    except Exception:
        stack_hint = ""

    user_prompt = (
        f"{stack_hint}\n"
        f"Task: {task.get('title', '')}\n"
        f"Description: {task.get('description', '')}\n"
        f"Phase: {task.get('phase', 'scaffold')}\n"
        f"Action: {task.get('action', 'NEW')}\n"
        f"Layer: {task.get('layer', '')}\n"
        f"Target path: {task.get('target_path', '')}\n"
        f"Source path: {task.get('source_path', '')}\n"
        f"Notes: {task.get('notes', '')}\n"
        f"BR IDs: {', '.join(task.get('br_ids') or [])}\n\n"
        f"Emit the file per your system prompt. If the target_path does not "
        f"belong to your side, return the refusal envelope literally."
    )

    try:
        response = await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or f"You are the CodeGen {assigned}."},
                {"role": "user", "content": user_prompt},
            ],
            agent_key=agent_key,
            project_id=project_id,
            model=model,
            temperature=0.1,
            max_tokens=12000,
        )
    except Exception as exc:
        await _update_codegen_agent_run(run_id, status="failed", error=str(exc))
        await codegen_tasks.update_one(
            {"project_id": project_id, "task_id": task_id},
            {"$set": {"status": "BLOCKED", "error": str(exc), "updated_at": _now_iso()}},
        )
        return

    content = response.get("content", "") if isinstance(response, dict) else str(response)
    content = (content or "").strip()
    # iter-17.14 — Sanitize LLM output before persistence. Strips
    # markdown code-fences (```java / ```python / ```) that leak in
    # from small local models (qwen2.5-coder:7b), strips stray
    # `(attempt=N)` / `retry` / apology preambles the LLM sometimes
    # copies from its own retry heuristics, and normalises trailing
    # whitespace. Refusal envelopes are exempt (must be raw JSON).
    if not (content.startswith("{") and '"refusal"' in content):
        content = _sanitize_llm_file(content, _language_for_path(task.get("target_path") or ""))

    # Refusal envelope?
    refusal_parsed: Optional[Dict[str, Any]] = None
    if content.startswith("{") and '"refusal"' in content:
        refusal_parsed = _extract_json_object(content)
    if refusal_parsed and refusal_parsed.get("refusal") is True:
        reason = str(refusal_parsed.get("reason") or "REFUSED")
        await _update_codegen_agent_run(
            run_id, status="completed",
            output_summary=f"refusal reason={reason}",
        )
        await codegen_tasks.update_one(
            {"project_id": project_id, "task_id": task_id},
            {"$set": {
                "status": "BLOCKED",
                "error": f"{assigned} refused: {reason}",
                "updated_at": _now_iso(),
            }},
        )
        return

    # Persist file into codegen_files (the shared single-shot flow uses
    # the same collection).
    target_path = task.get("target_path") or ""
    # iter-17.15 — Production-grade guard. Reject empty-shell files
    # (JPA repo interface with no finders, React page returning
    # `<div>TODO</div>`) BEFORE persistence so downstream verifier +
    # traceability gates don't count them as "coded". Task is marked
    # BLOCKED with a reason so the operator sees the reject in the
    # timeline instead of silently getting a placeholder committed.
    if target_path and content:
        is_stub, stub_reason = _looks_like_placeholder(
            content,
            _language_for_path(target_path),
            task.get("layer") or "",
        )
        if is_stub:
            await _update_codegen_agent_run(
                run_id, status="completed",
                output_summary=f"REJECTED placeholder — {stub_reason}",
            )
            await codegen_tasks.update_one(
                {"project_id": project_id, "task_id": task_id},
                {"$set": {
                    "status": "BLOCKED",
                    "error": f"placeholder rejected: {stub_reason}",
                    "updated_at": _now_iso(),
                }},
            )
            return
    if target_path and content:
        existing = await codegen_files.find_one(
            {"project_id": project_id, "file_path": target_path},
            {"_id": 1, "version": 1},
        )
        now_iso = _now_iso()
        if existing:
            await codegen_files.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "content": content,
                    "version": (existing.get("version", 1) or 1) + 1,
                    "updated_at": now_iso,
                    "edited": False,
                }},
            )
        else:
            await codegen_files.insert_one({
                "id": f"cgf_{project_id[:8]}_{abs(hash(target_path)) % 10**8:08d}",
                "project_id": project_id,
                "file_path": target_path,
                "content": content,
                "language": _language_for_path(target_path),
                "file_type": task.get("layer") or "source",
                "service_name": assigned,
                "service_id": None,
                "version": 1,
                "edited": False,
                "legacy_source_file": task.get("source_path") or "",
                "kb_file_id": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            })

    await _update_codegen_agent_run(
        run_id, status="completed",
        output_summary=f"wrote {len(content)} chars to {target_path}",
    )
    await codegen_tasks.update_one(
        {"project_id": project_id, "task_id": task_id},
        {"$set": {"status": "CODED", "updated_at": _now_iso()}},
    )


def _sanitize_llm_file(content: str, language: str) -> str:
    """iter-17.14 — Post-process raw LLM output before writing to
    ``codegen_files``. Handles three recurring failure modes seen with
    small local models (qwen2.5-coder:7b, deepseek-coder:6.7b):

      * Markdown code-fences (```java, ```python, ```json, ```xml, or
        a bare ```) wrapping the file — Java/Maven/Gradle/Python
        interpreters do NOT tolerate these lines.
      * Stray "(attempt=N)" / "retry" / "sorry, here is …" preambles
        the LLM sometimes echoes from its own retry heuristics.
      * A trailing "```" on the last line without a leading fence.

    Refusal JSON envelopes are handled by the caller — do NOT pass
    them through this function.
    """
    if not content:
        return content
    text = content.strip()

    # 1. Fence stripping — handles both single-line and split fences.
    fence_open = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$", re.M)
    lines = text.splitlines()
    # Drop a leading fence line ( ```java, ```python, ```json, ``` )
    if lines and fence_open.match(lines[0]):
        lines = lines[1:]
    # Drop a trailing fence line ( ``` )
    if lines and fence_open.match(lines[-1]):
        lines = lines[:-1]
    text = "\n".join(lines)
    # 1b. Any REMAINING fence-only lines mid-file are stripped too —
    # these appear when the LLM emits multiple fenced blocks (e.g.
    # `snippet 1 ... ``` ... snippet 2`).
    text = fence_open.sub("", text)

    # 2. Strip conversational preambles + attempt leaks (case-insensitive,
    # only when they appear at the VERY start of the file so we don't
    # mangle legitimate comments deep in the source).
    preamble_re = re.compile(
        r"^\s*(?:\(attempt=\d+\)|here'?s|here is|sorry|apolog|"
        r"sure[,!\s]|let me|i'?ll|of course)[^\n]*\n",
        re.I,
    )
    while preamble_re.match(text):
        text = preamble_re.sub("", text, count=1)

    # 3. Strip any bare "(attempt=N)" that made it into the body (as a
    # comment or as raw text). Matches whole lines only.
    text = re.sub(r"^\s*\(attempt=\d+\)\s*$\n?", "", text, flags=re.M)

    # 4. Language-specific first-char defence — if we accidentally left
    # a stray backtick or paragraph text at the top, walk forward to
    # the first plausible source token.
    lang = (language or "").lower()
    if lang == "java":
        m = re.search(r"^(package|import|/\*|//|public|class|@)", text, re.M)
        if m and m.start() > 0:
            text = text[m.start():]
    elif lang in {"python", "toml"}:
        m = re.search(r"^(from|import|def|class|#|\[)", text, re.M)
        if m and m.start() > 0:
            text = text[m.start():]
    elif lang in {"json"}:
        m = re.search(r"^\s*[{\[]", text)
        if m and m.start() > 0:
            text = text[m.start():]
    elif lang in {"xml", "html"}:
        m = re.search(r"^<", text, re.M)
        if m and m.start() > 0:
            text = text[m.start():]
    elif lang in {"typescript", "javascript"}:
        m = re.search(r"^(import|export|const|let|var|function|class|/\*|//)", text, re.M)
        if m and m.start() > 0:
            text = text[m.start():]
    elif lang == "go":
        m = re.search(r"^(package|import|func|//)", text, re.M)
        if m and m.start() > 0:
            text = text[m.start():]

    return text.rstrip() + "\n"


def _looks_like_placeholder(content: str, language: str, layer: str) -> Tuple[bool, str]:
    """iter-17.15 — Post-sanitiser production-grade guard. Rejects the
    empty-shell files the small Coder models keep emitting (e.g. a JPA
    repository interface with no finder methods, a React page that
    returns `<div>TODO</div>`).

    Returns ``(is_placeholder, reason)``. When True, the caller MUST
    NOT persist the file — it should mark the task BLOCKED with the
    reason so the operator sees the reject in the timeline.

    Applied AFTER the fence/preamble sanitiser so we're measuring real
    code substance, not markdown noise.
    """
    if not content:
        return True, "empty content"
    text = content.strip()
    body = "\n".join(l for l in text.splitlines() if l.strip() and not l.strip().startswith(("//", "#", "/*", "*")))
    body_lines = [l for l in body.splitlines() if l.strip()]
    n_lines = len(body_lines)

    # Universal forbidden markers (case-sensitive on purpose — legit
    # docstrings shouldn't contain "TODO" in production code).
    forbidden = [
        "TODO", "FIXME", "XXX: ", "HACK: ",
        "NotImplementedError", "UnsupportedOperationException",
        'throw new Error("not implemented")',
        "raise NotImplementedError",
        "<div>TODO</div>", "<div>Placeholder</div>",
    ]
    for marker in forbidden:
        if marker in text:
            return True, f"forbidden placeholder marker: {marker!r}"

    lang = (language or "").lower()
    lyr = (layer or "").lower()

    if lang == "java":
        # Empty interface body → repo/service shell with just a BR comment.
        if re.search(r"interface\s+\w+[^{]*\{\s*(//[^\n]*\n\s*)*\}", text):
            return True, "empty interface body (repository shell with no methods)"
        # Empty class body.
        if re.search(r"class\s+\w+[^{]*\{\s*(//[^\n]*\n\s*)*\}", text):
            return True, "empty class body"
        # Layer-specific substance floor.
        if lyr == "repository" and n_lines < 5:
            return True, f"repository too thin ({n_lines} lines)"
        if lyr == "entity" and n_lines < 15:
            return True, f"entity too thin ({n_lines} lines, likely missing mapped fields)"
        if lyr == "controller" and n_lines < 15:
            return True, f"controller too thin ({n_lines} lines, likely no @Mapping methods)"
        if lyr == "service" and n_lines < 15:
            return True, f"service too thin ({n_lines} lines, likely no method bodies)"
        # iter-17.17 — new package-segregated layers.
        if lyr == "dao" and n_lines < 10:
            return True, f"dao too thin ({n_lines} lines, likely no native-SQL calls)"
        if lyr == "dto" and n_lines < 6:
            return True, f"dto too thin ({n_lines} lines, likely no record fields)"
        if lyr == "mapper" and n_lines < 8:
            return True, f"mapper too thin ({n_lines} lines, likely no toDto/toEntity)"
        if lyr == "exception" and n_lines < 6:
            return True, f"exception too thin ({n_lines} lines)"
        # config + util may legitimately be short; skip the floor check.

    elif lang == "python":
        if re.search(r"^\s*(pass|\.\.\.)\s*$", text, re.M) and n_lines < 10:
            return True, "python stub — bare `pass` / `...` with no real body"
        if lyr in {"repository", "service", "controller"} and n_lines < 10:
            return True, f"{lyr} too thin ({n_lines} lines)"

    elif lang in {"typescript", "javascript"}:
        if re.search(r"return\s*<>\s*</>\s*;?", text):
            return True, "React component returns empty fragment"
        if re.search(r"return\s+null\s*;?\s*}?\s*$", text) and n_lines < 15:
            return True, "React component returns only null"
        if lyr in {"page", "component"} and n_lines < 15:
            return True, f"{lyr} too thin ({n_lines} lines)"

    # JSON / XML / manifest files pass regardless of line count — the
    # build-manifest task description already enforces version pins,
    # and empty manifests would fail the build itself.
    return False, ""


def _language_for_path(p: str) -> str:
    """Rough language tag for codegen_files persistence — kept minimal
    on purpose; the single-shot flow does the same coarse mapping."""
    ext = ("." + p.rsplit(".", 1)[-1]) if "." in p else ""
    return {
        ".py": "python", ".java": "java", ".kt": "kotlin", ".go": "go",
        ".rs": "rust", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".sql": "sql", ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml", ".css": "css", ".scss": "scss",
        ".html": "html",
    }.get(ext, "text")


async def _continue_multi_agent_codegen_after_task_confirm(
    project_id: str,
    model: Optional[str] = None,
) -> None:
    """Phase 3 of the multi-agent CodeGen pipeline.

    Wave loop: for each wave, fan out BE + FE tasks in parallel, run
    Verifier per task, Reviewer per wave, Tester per wave. Then
    Traceability Gate; halt if enforced and coverage below threshold,
    otherwise finalize.
    """
    try:
        await _update_codegen_state(
            project_id,
            status=_CODEGEN_STATUS_EXECUTING,
            tasks_confirmed_at=_now_iso(),
        )

        task_docs = await codegen_tasks.find(
            {"project_id": project_id},
        ).to_list(5000)
        if not task_docs:
            await _update_codegen_state(
                project_id, status=_CODEGEN_STATUS_FAILED,
                last_error="No tasks to execute", completed_at=_now_iso(),
            )
            return

        waves = sorted({int(t.get("wave") or 1) for t in task_docs})
        for wave in waves:
            wave_tasks = [t for t in task_docs if int(t.get("wave") or 1) == wave]
            be_tasks = [t for t in wave_tasks if t.get("assigned_to") == "coder_be"]
            fe_tasks = [t for t in wave_tasks if t.get("assigned_to") == "coder_fe"]
            other_tasks = [t for t in wave_tasks
                           if t.get("assigned_to") not in ("coder_be", "coder_fe")]

            await _update_codegen_state(project_id, current_wave=wave)

            # iter-17.16 — Bounded parallelism. Fan out all coder tasks
            # (BE + FE + other) under a semaphore so we don't hammer the
            # LLM provider and trip rate limits, but still get real
            # concurrency inside a wave. Tune via `LAMA_CODEGEN_PARALLELISM`
            # (default 6).
            try:
                _parallelism = int(
                    os.environ.get("LAMA_CODEGEN_PARALLELISM") or "6"
                )
            except Exception:
                _parallelism = 6
            _parallelism = max(1, min(_parallelism, 32))
            _sem = asyncio.Semaphore(_parallelism)

            async def _bounded_coder(t: Dict[str, Any]) -> None:
                async with _sem:
                    await _run_coder_for_task(project_id, t, model)

            coros = [
                _bounded_coder(t)
                for t in be_tasks + fe_tasks + other_tasks
            ]
            if coros:
                await asyncio.gather(*coros, return_exceptions=False)

            # iter-17.16 — Verifier per task, ALSO parallelized under the
            # same semaphore. Previously this was a sequential `for` loop
            # which was the biggest wall-clock offender on large waves
            # (verifier runs ~= coder runs and is LLM-bound).
            coded = await codegen_tasks.find(
                {"project_id": project_id, "wave": wave, "status": "CODED"},
            ).to_list(1000)

            async def _bounded_verifier(ct: Dict[str, Any]) -> None:
                async with _sem:
                    await _run_verifier_for_codegen_task(project_id, ct, model)

            if coded:
                await asyncio.gather(
                    *[_bounded_verifier(ct) for ct in coded],
                    return_exceptions=False,
                )

            # Reviewer per wave.
            await _run_reviewer_for_codegen_wave(project_id, wave, model)

            # Tester per wave.
            await _run_tester_for_codegen_wave(project_id, wave, model)

        # Traceability gate.
        gate = await _run_codegen_traceability_gate(project_id, model)
        coverage_pct = float(gate.get("coverage_pct", 0.0))
        missing = list(gate.get("missing_brs") or [])

        await _update_codegen_state(
            project_id,
            br_coverage_pct=coverage_pct,
            br_missing=missing,
        )
        await _audit_multi_agent(project_id, "traceability_gate_completed",
                                 {"coverage_pct": coverage_pct,
                                  "missing_count": len(missing)})

        enforce = (os.environ.get("LAMA_BR_ENFORCE") or "").strip() == "1"
        try:
            min_cov = float(os.environ.get("LAMA_BR_MIN_COVERAGE") or "100")
        except ValueError:
            min_cov = 100.0

        if enforce and coverage_pct < min_cov:
            await _update_codegen_state(
                project_id,
                status=_CODEGEN_STATUS_TRACEABILITY,
                last_error=(f"BR coverage {coverage_pct:.1f}% < required "
                            f"{min_cov:.1f}%"),
            )
            await _log_codegen_agent_run(
                project_id, "traceability_gate", "gate",
                status="completed",
                score=coverage_pct,
                output_summary=(f"Blocked: coverage {coverage_pct:.1f}% "
                                f"< {min_cov:.1f}% (missing "
                                f"{len(missing)} BR)"),
                details={"missing_brs": missing},
            )
            return

        # Finalize.
        await _finalize_codegen(project_id, model)

    except Exception as exc:
        logger.exception("multi-agent codegen phase-3 failed: %s", exc)
        await _update_codegen_state(
            project_id, status=_CODEGEN_STATUS_FAILED,
            last_error=str(exc), completed_at=_now_iso(),
        )


_VERIFIER_SCORE_FLOOR = 95.0

# Anything that is not an unqualified acceptance fails the gate. Listed
# explicitly rather than inferred so a new verdict string a model invents
# cannot accidentally land in the passing branch.
_VERIFIER_PASSING_VERDICTS = frozenset({"ACCEPT"})


def _verifier_outcome(parsed: Dict[str, Any]) -> Tuple[str, float, str]:
    """Decide a task's fate from the Verifier's parsed reply.

    Returns ``(status, score, fail_reason)`` where status is ``VERIFIED``
    or ``VERIFY_FAILED`` and ``fail_reason`` is empty on success.

    Extracted from the call site so the decision is testable on its own —
    it is a quality gate, and a gate whose logic can only be exercised by
    standing up Mongo and an LLM is a gate nobody checks.

    Two defects this corrects:

    * The verdict used to be parsed into a local and then never consulted;
      only the score was. A model replying ``{"verdict": "REJECT",
      "confidence": 97}`` — "this file is wrong and I am confident" — was
      marked VERIFIED. Confidence measures certainty, not approval, so a
      confident rejection is the strongest possible reason to fail.
    * The seeded prompt advertised an ``ACCEPT_WITH_NOTES`` band at 85-94
      as passing, while the code failed everything under 95. The floor is
      the documented contract, so the band was removed from the prompt.

    Fails closed throughout. ``parsed`` is ``{}`` when the reply could not
    be parsed at all, and a verifier that could not answer must never
    silently approve a file.
    """
    raw_verdict = parsed.get("verdict")
    verdict = str(raw_verdict or "").strip().upper()

    try:
        score = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(100.0, score))

    if not verdict:
        return "VERIFY_FAILED", score, "no verdict returned"
    if verdict not in _VERIFIER_PASSING_VERDICTS:
        # Covers REJECT, UNVERIFIABLE, ACCEPT_WITH_NOTES and anything a
        # model invents. UNVERIFIABLE is the honest answer when the
        # evidence was not supplied; it fails rather than forcing the
        # model to invent an ACCEPT or a REJECT.
        return "VERIFY_FAILED", score, f"verdict {verdict}"
    if score < _VERIFIER_SCORE_FLOOR:
        return (
            "VERIFY_FAILED", score,
            f"confidence {score:.0f} below the {_VERIFIER_SCORE_FLOOR:.0f} floor",
        )
    return "VERIFIED", score, ""


async def _run_verifier_for_codegen_task(
    project_id: str,
    task: Dict[str, Any],
    model: Optional[str],
) -> None:
    task_id = task.get("task_id") or ""
    target_path = task.get("target_path") or ""
    file_doc = await codegen_files.find_one(
        {"project_id": project_id, "file_path": target_path},
        {"_id": 0, "content": 1},
    )
    content = (file_doc or {}).get("content", "")

    run_id = await _log_codegen_agent_run(
        project_id, "verifier", "verify",
        task_id=task_id, envelope_id=task.get("envelope_id") or "",
        input_summary=f"verifying {target_path} ({len(content)} chars)",
    )
    prompt_template = await _get_codegen_prompt("codegen.verifier")
    try:
        resp = await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or "You are the CodeGen Verifier."},
                {"role": "user", "content":
                    f"Verify this generated file for task {task_id}.\n\n"
                    f"===== FILE ({target_path}) =====\n{content[:20000]}\n\n"
                    "Return ONLY the JSON described in your system prompt."},
            ],
            agent_key="codegen.verifier",
            project_id=project_id,
            model=model,
            temperature=0.0,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        await _update_codegen_agent_run(run_id, status="failed", error=str(exc))
        return
    text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
    parsed = _extract_json_object(text) or {}
    final_status, score, fail_reason = _verifier_outcome(parsed)
    verdict = str(parsed.get("verdict") or "REJECT")
    await codegen_tasks.update_one(
        {"project_id": project_id, "task_id": task_id},
        {"$set": {
            "status": final_status,
            "verifier_score": score,
            "verifier_checks": parsed,
            "verifier_reason": fail_reason,
            "confidence": score / 100.0,
            "updated_at": _now_iso(),
        }},
    )
    await _update_codegen_agent_run(
        run_id, status="completed", score=score,
        output_summary=(f"{verdict} {score:.0f}%"
                        + (f" — {fail_reason}" if fail_reason else "")),
    )


async def _run_reviewer_for_codegen_wave(
    project_id: str, wave: int, model: Optional[str],
) -> None:
    wave_tasks = await codegen_tasks.find(
        {"project_id": project_id, "wave": wave},
    ).to_list(1000)
    digest = json.dumps([{
        "task_id": t.get("task_id"),
        "target_path": t.get("target_path"),
        "layer": t.get("layer"),
        "assigned_to": t.get("assigned_to"),
        "status": t.get("status"),
    } for t in wave_tasks], default=str)[:15000]

    run_id = await _log_codegen_agent_run(
        project_id, "reviewer", "review",
        input_summary=f"wave={wave} tasks={len(wave_tasks)}",
        details={"wave": wave},
    )
    prompt_template = await _get_codegen_prompt("codegen.reviewer")
    try:
        resp = await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or "You are the CodeGen Reviewer."},
                {"role": "user", "content":
                    f"Wave {wave} tasks:\n{digest}\n\n"
                    "Return ONLY the JSON described in your system prompt."},
            ],
            agent_key="codegen.reviewer",
            project_id=project_id,
            model=model,
            temperature=0.1,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        await _update_codegen_agent_run(run_id, status="failed", error=str(exc))
        return
    text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
    parsed = _extract_json_object(text) or {}
    await _update_codegen_agent_run(
        run_id, status="completed",
        output_summary=str(parsed.get("verdict", ""))[:120],
        details={"issues": parsed.get("issues", [])},
    )


async def _run_tester_for_codegen_wave(
    project_id: str, wave: int, model: Optional[str],
) -> None:
    wave_tasks = await codegen_tasks.find(
        {"project_id": project_id, "wave": wave},
    ).to_list(1000)
    digest = json.dumps([{
        "task_id": t.get("task_id"),
        "target_path": t.get("target_path"),
        "layer": t.get("layer"),
        "status": t.get("status"),
    } for t in wave_tasks], default=str)[:15000]

    run_id = await _log_codegen_agent_run(
        project_id, "tester", "test",
        input_summary=f"wave={wave} tasks={len(wave_tasks)}",
        details={"wave": wave},
    )
    prompt_template = await _get_codegen_prompt("codegen.tester")
    try:
        resp = await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or "You are the CodeGen Tester."},
                {"role": "user", "content":
                    f"Wave {wave} tasks:\n{digest}\n\n"
                    "Return ONLY the JSON described in your system prompt."},
            ],
            agent_key="codegen.tester",
            project_id=project_id,
            model=model,
            temperature=0.0,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        await _update_codegen_agent_run(run_id, status="failed", error=str(exc))
        return
    text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
    parsed = _extract_json_object(text) or {}
    await _update_codegen_agent_run(
        run_id, status="completed",
        score=float(parsed.get("overall_score", 0.0) or 0.0),
        output_summary=("compilation_ready="
                        + str(parsed.get("compilation_ready", False))),
    )


async def _run_codegen_traceability_gate(
    project_id: str, model: Optional[str],
) -> Dict[str, Any]:
    """Traceability gate — computes BR coverage across envelopes/tasks.

    Deterministic fallback: if the LLM path fails or returns nothing,
    compute coverage from envelope.br_ids ∩ task.br_ids so the gate
    always produces a numeric answer (matches the existing tools-side
    behaviour where the gate is *authoritative* about coverage math).
    """
    envelopes = await codegen_envelopes.find(
        {"project_id": project_id},
    ).to_list(2000)
    tasks = await codegen_tasks.find(
        {"project_id": project_id},
    ).to_list(5000)

    expected: set = set()
    covered_by_task: set = set()
    for e in envelopes:
        for br in e.get("br_ids") or []:
            expected.add(str(br))
    for t in tasks:
        for br in t.get("br_ids") or []:
            covered_by_task.add(str(br))
    covered = expected & covered_by_task
    missing = sorted(expected - covered)
    coverage_pct = 100.0 if not expected else 100.0 * len(covered) / len(expected)

    run_id = await _log_codegen_agent_run(
        project_id, "traceability_gate", "gate",
        input_summary=f"envelopes={len(envelopes)} tasks={len(tasks)} brs={len(expected)}",
    )
    result: Dict[str, Any] = {
        "coverage_pct": coverage_pct,
        "total_brs": len(expected),
        "covered_brs": len(covered),
        "missing_brs": missing,
        "per_envelope": [
            {
                "envelope_id": e.get("envelope_id"),
                "expected": list(e.get("br_ids") or []),
                "covered": [br for br in (e.get("br_ids") or [])
                            if str(br) in covered_by_task],
                "missing": [br for br in (e.get("br_ids") or [])
                            if str(br) not in covered_by_task],
            }
            for e in envelopes
        ],
        "summary": (f"{len(covered)}/{len(expected)} BR(s) covered "
                    f"({coverage_pct:.1f}%)"),
    }

    # Best-effort LLM enrichment — never overrides the numeric answer.
    prompt_template = await _get_codegen_prompt("codegen.traceability_gate")
    try:
        resp = await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or "You are the CodeGen Traceability Gate."},
                {"role": "user", "content":
                    "Traceability rollup (deterministic):\n"
                    + json.dumps(result, default=str)[:20000]
                    + "\n\nReturn ONLY the JSON described in your system prompt."},
            ],
            agent_key="codegen.traceability_gate",
            project_id=project_id,
            model=model,
            temperature=0.0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        parsed = _extract_json_object(text) or {}
        summary = parsed.get("summary")
        if summary:
            result["summary"] = str(summary)
    except Exception:
        pass  # deterministic result stays

    await _update_codegen_agent_run(
        run_id, status="completed", score=coverage_pct,
        output_summary=result["summary"],
        details={"missing_brs": missing[:50]},
    )
    return result


async def _finalize_codegen(project_id: str, model: Optional[str] = None) -> None:
    """Mark the project's CodeGen stage as complete and unlock Living."""
    prompt_template = await _get_codegen_prompt("codegen.finalizer")
    run_id = await _log_codegen_agent_run(
        project_id, "finalizer", "finalize",
        input_summary="Wrapping up multi-agent CodeGen",
    )
    try:
        await chat_completion(
            messages=[
                {"role": "system", "content": prompt_template or "You are the CodeGen Finalizer."},
                {"role": "user", "content": "Emit the completion summary JSON."},
            ],
            agent_key="codegen.finalizer",
            project_id=project_id,
            model=model,
            temperature=0.0,
            max_tokens=1500,
            response_format={"type": "json_object"},
        )
    except Exception:
        # Finalizer is decorative — swallow errors and still complete.
        pass

    # Promote the stage status so Living unlocks.
    try:
        await projects.update_one(
            {"id": project_id},
            {"$set": {"stage_status.Living": "available",
                      "updated_at": _now_iso()}},
        )
    except Exception:
        logger.exception("could not promote project stage_status → Living")

    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_COMPLETED,
        completed_at=_now_iso(),
    )
    await _update_codegen_agent_run(
        run_id, status="completed",
        output_summary="CodeGen multi-agent pipeline complete",
    )
    await _audit_multi_agent(project_id, "codegen_finalized", {})


# ══════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════
@router.post("/{project_id}/multi-agent/start", status_code=202)
async def multi_agent_start(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """Kick off the multi-agent CodeGen pipeline."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "project not found")

    # Architecture must be frozen — call require_stage_context here so
    # the operator gets a synchronous 400 instead of an async failed
    # state buried in the timeline.
    await require_stage_context(project_id, "Architecture", "CodeGen")

    state = await _get_codegen_state(project_id)
    if state.get("status") not in _CODEGEN_STARTABLE_STATUSES:
        raise HTTPException(
            409,
            f"CodeGen multi-agent pipeline is {state.get('status')} — "
            f"cannot start. Cancel or rerun to reset.",
        )

    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_ENV_PENDING,  # optimistic; Context Manager will update
        last_error="",
        started_at=_now_iso(),
        completed_at=None,
        current_wave=0,
        envelope_count=0,
        task_count=0,
        br_coverage_pct=0.0,
        br_missing=[],
    )
    await _audit_multi_agent(project_id, "multi_agent_start", {"model": model})
    background_tasks.add_task(_run_multi_agent_codegen, project_id, model)
    return {"status": "started", "project_id": project_id}


@router.get("/{project_id}/multi-agent/state")
async def multi_agent_get_state(project_id: str):
    doc = await codegen_pipeline_state.find_one(
        {"project_id": project_id}, {"_id": 0},
    )
    if not doc:
        return {
            "project_id": project_id,
            "status": _CODEGEN_STATUS_IDLE,
            "current_wave": 0,
            "build_system_be": "",
            "build_system_fe": "",
            "envelope_count": 0,
            "task_count": 0,
            "br_coverage_pct": 0.0,
            "br_missing": [],
            "last_error": "",
        }
    return doc


@router.get("/{project_id}/multi-agent/envelopes")
async def multi_agent_list_envelopes(project_id: str):
    docs = await codegen_envelopes.find(
        {"project_id": project_id}, {"_id": 0},
    ).to_list(2000)
    return {"total": len(docs), "envelopes": docs}


@router.patch("/{project_id}/multi-agent/envelopes/{envelope_id}")
async def multi_agent_patch_envelope(
    project_id: str, envelope_id: str, payload: Dict[str, Any] = None,
):
    payload = payload or {}
    # Only allow a safe subset of fields to be patched.
    allowed = {
        "endpoint_method", "endpoint_path", "controller_class",
        "controller_file", "service_class", "service_file",
        "service_method", "business_logic_summary",
        "repository_class", "repository_file", "db_tables",
        "db_operations", "external_calls", "files_affected", "action",
        "risk_level", "layer", "side", "acceptance_criteria", "br_ids",
        "status",
    }
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(400, "No allowed fields in payload")
    res = await codegen_envelopes.update_one(
        {"project_id": project_id, "envelope_id": envelope_id},
        {"$set": patch},
    )
    matched = getattr(res, "matched_count", None)
    if matched == 0:
        raise HTTPException(404, "envelope not found")
    await _audit_multi_agent(
        project_id, "envelope_patched",
        {"envelope_id": envelope_id, "fields": sorted(patch.keys())},
    )
    return {"status": "ok", "envelope_id": envelope_id}


@router.post("/{project_id}/multi-agent/envelopes/confirm", status_code=202)
async def multi_agent_confirm_envelopes(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    state = await _get_codegen_state(project_id)
    if state.get("status") != _CODEGEN_STATUS_ENV_PENDING:
        raise HTTPException(
            400,
            f"envelopes cannot be confirmed — current status is "
            f"'{state.get('status')}'",
        )
    await codegen_envelopes.update_many(
        {"project_id": project_id},
        {"$set": {"status": "approved"}},
    )
    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_ENV_CONFIRMED,
        envelope_confirmed_at=_now_iso(),
    )
    await _audit_multi_agent(project_id, "envelopes_confirmed", {})
    background_tasks.add_task(
        _continue_multi_agent_codegen_after_envelope_confirm,
        project_id, model,
    )
    return {"status": "confirmed", "project_id": project_id}


# iter-17.4 — Recovery endpoint. When Planner returned zero tasks (silent
# LLM timeout, or the pipeline ran under pre-iter-17.3 code that lacked
# the deterministic Planner), let the user re-invoke JUST the Planner
# step without wiping their approved envelopes. Rerun-From-Scratch would
# throw away the (correct) envelopes; that's the wrong tool for this job.
@router.post("/{project_id}/multi-agent/retry-planner", status_code=202)
async def multi_agent_retry_planner(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    state = await _get_codegen_state(project_id)
    cur = state.get("status")
    if cur not in (_CODEGEN_STATUS_TASKS_PENDING,
                   _CODEGEN_STATUS_ENV_CONFIRMED,
                   _CODEGEN_STATUS_FAILED):
        raise HTTPException(
            400,
            f"planner cannot be retried — current status is '{cur}'",
        )
    # Wipe any half-baked tasks + earlier planner runs so the audit
    # timeline is clean and idempotent.
    await codegen_tasks.delete_many({"project_id": project_id})
    await codegen_agent_runs.delete_many(
        {"project_id": project_id, "agent": {"$in": ["planner", "build_tool_selector"]}},
    )
    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_ENV_CONFIRMED,
        task_count=0,
        last_error="",
    )
    await _audit_multi_agent(project_id, "planner_retry_requested", {})
    background_tasks.add_task(
        _continue_multi_agent_codegen_after_envelope_confirm,
        project_id, model,
    )
    return {"status": "retrying_planner", "project_id": project_id}


@router.post("/{project_id}/multi-agent/build-system")
async def multi_agent_override_build_system(
    project_id: str, payload: Dict[str, Any] = None,
):
    payload = payload or {}
    be = str(payload.get("be") or "").strip()
    fe = str(payload.get("fe") or "").strip()
    if not be and not fe:
        raise HTTPException(400, "at least one of {be, fe} required")
    patch: Dict[str, Any] = {}
    if be:
        patch["build_system_be"] = be
    if fe:
        patch["build_system_fe"] = fe
    await _update_codegen_state(project_id, **patch)
    await _audit_multi_agent(project_id, "build_system_override", patch)
    state = await codegen_pipeline_state.find_one(
        {"project_id": project_id}, {"_id": 0, "build_system_be": 1, "build_system_fe": 1},
    )
    return {"status": "ok", **(state or {})}


@router.get("/{project_id}/multi-agent/tasks")
async def multi_agent_list_tasks(project_id: str, wave: Optional[int] = None):
    q: Dict[str, Any] = {"project_id": project_id}
    if wave is not None:
        q["wave"] = int(wave)
    docs = await codegen_tasks.find(q, {"_id": 0}).to_list(5000)
    return {"total": len(docs), "tasks": docs}


@router.patch("/{project_id}/multi-agent/tasks/{task_id}")
async def multi_agent_patch_task(
    project_id: str, task_id: str, payload: Dict[str, Any] = None,
):
    payload = payload or {}
    allowed = {
        "title", "description", "phase", "layer", "action", "wave",
        "wave_name", "source_path", "target_path", "assigned_to",
        "depends_on", "notes", "br_ids", "status",
    }
    patch = {k: v for k, v in payload.items() if k in allowed}
    if not patch:
        raise HTTPException(400, "No allowed fields in payload")

    # Re-apply the deterministic router after an edit so target_path
    # changes flow through to `assigned_to`.
    current = await codegen_tasks.find_one(
        {"project_id": project_id, "task_id": task_id}, {"_id": 0},
    )
    if not current:
        raise HTTPException(404, "task not found")
    merged = {**current, **patch}
    patch["assigned_to"] = _route_task_to_coder(merged)
    patch["updated_at"] = _now_iso()

    await codegen_tasks.update_one(
        {"project_id": project_id, "task_id": task_id},
        {"$set": patch},
    )
    await _audit_multi_agent(
        project_id, "task_patched",
        {"task_id": task_id, "fields": sorted(patch.keys())},
    )
    return {"status": "ok", "task_id": task_id, "assigned_to": patch["assigned_to"]}


@router.post("/{project_id}/multi-agent/tasks/confirm", status_code=202)
async def multi_agent_confirm_tasks(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    state = await _get_codegen_state(project_id)
    if state.get("status") != _CODEGEN_STATUS_TASKS_PENDING:
        raise HTTPException(
            400,
            f"tasks cannot be confirmed — current status is "
            f"'{state.get('status')}'",
        )
    await codegen_tasks.update_many(
        {"project_id": project_id},
        {"$set": {"status": "APPROVED"}},
    )
    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_TASKS_CONFIRMED,
        tasks_confirmed_at=_now_iso(),
    )
    await _audit_multi_agent(project_id, "tasks_confirmed", {})
    background_tasks.add_task(
        _continue_multi_agent_codegen_after_task_confirm,
        project_id, model,
    )
    return {"status": "confirmed", "project_id": project_id}


@router.get("/{project_id}/multi-agent/agent-runs")
async def multi_agent_list_agent_runs(
    project_id: str,
    agent: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
):
    q: Dict[str, Any] = {"project_id": project_id}
    if agent:
        q["agent"] = agent
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 100
    try:
        skip = max(0, int(skip))
    except Exception:
        skip = 0
    cursor = codegen_agent_runs.find(q, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(limit)
    return {"total": len(docs), "runs": docs, "limit": limit, "skip": skip}


@router.post("/{project_id}/multi-agent/rerun", status_code=202)
async def multi_agent_rerun(
    project_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
    wipe_files: bool = True,
):
    """Clear envelopes/tasks/agent_runs for this project and restart the
    pipeline from Context Manager.

    iter-17.14 — `wipe_files=True` (the new default) ALSO deletes
    `codegen_files` for this project so a rerun starts from a truly
    clean slate. This fixes the "Rerun From Scratch also leaves stale
    .py files behind" report — previously stale artifacts from a prior
    (Python-defaulted) run coexisted with the newly-generated Java
    files, producing a mixed / uncompilable tree.

    Pass `?wipe_files=false` to preserve existing files (matches
    pre-17.14 behaviour, useful when reruns are patching a small
    envelope subset).
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "project not found")

    await codegen_envelopes.delete_many({"project_id": project_id})
    await codegen_tasks.delete_many({"project_id": project_id})
    await codegen_agent_runs.delete_many({"project_id": project_id})
    files_wiped = 0
    if wipe_files:
        # iter-17.14 — nuke stale artifacts so the new run isn't polluted
        # by earlier (potentially wrong-language / wrong-layout) files.
        res = await codegen_files.delete_many({"project_id": project_id})
        files_wiped = res.deleted_count or 0
    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_ENV_PENDING,
        current_wave=0,
        envelope_count=0,
        task_count=0,
        br_coverage_pct=0.0,
        br_missing=[],
        last_error="",
        started_at=_now_iso(),
        completed_at=None,
        envelope_confirmed_at=None,
        tasks_confirmed_at=None,
        # iter-17.14 — clear the persisted target-stack pair so the next
        # envelope-confirm pass re-resolves against a fresh
        # project.target_tech / arch_outputs snapshot. Without this a
        # project whose target stack was edited between runs would keep
        # emitting under the OLD stack.
        target_backend_lang=None,
        target_backend_framework=None,
        target_frontend_lang=None,
        target_frontend_framework=None,
    )
    await _audit_multi_agent(
        project_id, "multi_agent_rerun",
        {"wipe_files": wipe_files, "files_wiped": files_wiped},
    )
    background_tasks.add_task(_run_multi_agent_codegen, project_id, model)
    return {
        "status": "rerun_started",
        "project_id": project_id,
        "wipe_files": wipe_files,
        "files_wiped": files_wiped,
    }


@router.post("/{project_id}/multi-agent/cancel")
async def multi_agent_cancel(project_id: str):
    await _update_codegen_state(
        project_id,
        status=_CODEGEN_STATUS_FAILED,
        last_error="cancelled by user",
        completed_at=_now_iso(),
    )
    await _audit_multi_agent(project_id, "multi_agent_cancel", {})
    return {"status": "cancelled", "project_id": project_id}


@router.get("/{project_id}/multi-agent/traceability")
async def multi_agent_traceability(project_id: str):
    """Read the last-computed traceability rollup off pipeline state +
    per-envelope BR ids. Never runs the LLM path from this endpoint —
    only reads. To force a fresh computation, rerun the pipeline."""
    state = await codegen_pipeline_state.find_one(
        {"project_id": project_id}, {"_id": 0},
    ) or {}
    envelopes = await codegen_envelopes.find(
        {"project_id": project_id}, {"_id": 0, "envelope_id": 1, "br_ids": 1},
    ).to_list(2000)
    tasks = await codegen_tasks.find(
        {"project_id": project_id}, {"_id": 0, "br_ids": 1},
    ).to_list(5000)
    covered_by_task = set()
    for t in tasks:
        for br in t.get("br_ids") or []:
            covered_by_task.add(str(br))
    per_envelope = []
    for e in envelopes:
        exp = [str(b) for b in (e.get("br_ids") or [])]
        per_envelope.append({
            "envelope_id": e.get("envelope_id"),
            "expected": exp,
            "covered": [b for b in exp if b in covered_by_task],
            "missing": [b for b in exp if b not in covered_by_task],
        })
    return {
        "coverage_pct": float(state.get("br_coverage_pct") or 0.0),
        "missing_brs": list(state.get("br_missing") or []),
        "per_envelope": per_envelope,
    }
