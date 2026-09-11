"""iter-13.91.16 — Unified, host-anchored context bundler.

Architectural intent
====================
LAMA's system-of-record for every input and every intermediate artifact
is the LAMA host (MongoDB at `/data/db` + optionally a local cache dir
at `${LAMA_DATA_DIR}/projects/{project_id}/context/`). The droid (when
Factory orchestrator is enabled) is pure compute — it never needs
persistent on-disk state. Every stage prompt embeds its full context
INLINE in the message body sent to the LLM.

This module is the canonical "gather everything relevant for stage X"
helper. All four stages (SRS / DataModel / Architecture / CodeGen)
should call `build_stage_context()` instead of hand-rolling their own
context assembly. It is target-stack-aware: when `project.target_tech`
is set, the bundle includes conventions / coding style hints / typical
package layouts for that stack.

Why bundle once, not per-section?
---------------------------------
Stages like SRS generate 12+ sections in parallel. If each section
re-queries Mongo for KB + OWL + previous-stage context, we hit Mongo
~100× per stage run and pay the serialisation cost N times. The
bundler runs ONCE, optionally caches the result on local disk (keyed
by Mongo document versions so cache is auto-invalidated when KB
re-builds), and every section reuses the same dict.

Cache key
---------
`<project_id>:<stage>:<kb_version>:<srs_version>:<target_tech>` — any
change in upstream artifacts invalidates the cache automatically.

Returns
-------
Always returns a JSON-serialisable dict. Empty / missing pieces are
omitted (not present as None). Callers should `json.dumps(bundle,
ensure_ascii=False)` and embed in the prompt body verbatim.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from db import (
    projects,
    kb_files,
    kb_entities,
    kb_toon,
    kb_graph,
    srs_documents,
    data_models,
    bus_matrix,
    olap_models,
    arch_documents,
    arch_services,
    codegen_files,
    legacy_analysis,
    business_ontologies,
    stage_context,
    project_integrations,
)


# ──────────────────────────────────────────────────────────────────────
# Local filesystem cache
# ──────────────────────────────────────────────────────────────────────
LAMA_DATA_DIR = os.environ.get("LAMA_DATA_DIR", "/data/lama").rstrip("/")
CONTEXT_CACHE_TTL_SEC = int(os.environ.get("LAMA_CONTEXT_CACHE_TTL_SEC", "1800") or "1800")
CONTEXT_BUNDLE_MAX_CHARS = int(os.environ.get("LAMA_CONTEXT_BUNDLE_MAX_CHARS", "200000") or "200000")


def _cache_dir(project_id: str) -> str:
    path = os.path.join(LAMA_DATA_DIR, "projects", project_id, "context")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # Filesystem read-only or quota exhausted — degrade to no-cache mode.
        return ""
    return path


def _cache_key(
    project_id: str,
    stage: str,
    *,
    kb_version: str,
    srs_version: str,
    target_tech: str,
) -> str:
    raw = f"{project_id}|{stage}|{kb_version}|{srs_version}|{target_tech}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(project_id: str, stage: str, cache_key: str) -> str:
    d = _cache_dir(project_id)
    if not d:
        return ""
    return os.path.join(d, f"{stage}-{cache_key}.json")


def _read_cache(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        if (time.time() - os.path.getmtime(path)) > CONTEXT_CACHE_TTL_SEC:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_cache(path: str, bundle: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, separators=(",", ":"))
    except OSError:
        # Best-effort — never let cache write failure break the call.
        pass


# ──────────────────────────────────────────────────────────────────────
# Target-stack conventions (target-tech-aware prompt enrichment)
# ──────────────────────────────────────────────────────────────────────
# Every entry is a SHORT (≤ ~600 chars) hint block the LLM uses to
# decide "what does idiomatic code in this stack look like?" — added
# verbatim to the bundle under `target_conventions`. Keep entries small
# so they don't bloat the prompt.
_STACK_CONVENTIONS: Dict[str, str] = {
    # ── Python ─────────────────────────────────────────────────────
    "fastapi/python/postgresql": (
        "Stack: FastAPI 0.110+, Python 3.12, Pydantic v2, SQLAlchemy 2.x async, "
        "asyncpg, Alembic migrations. Layout: app/ with routers/, models/, "
        "schemas/, services/, db/. Use `async def` everywhere; APIRouter per "
        "domain; Pydantic v2 `model_config = ConfigDict(from_attributes=True)`; "
        "dependency injection via `Depends`. Migrations live in alembic/versions/. "
        "Settings via pydantic-settings."
    ),
    "django/python/postgresql": (
        "Stack: Django 5.x, Python 3.12, DRF, psycopg3. Layout: project/ with "
        "settings/, apps/ each holding models.py/serializers.py/views.py/urls.py. "
        "Class-based views; ModelViewSet for CRUD; DRF Routers; settings split "
        "by env (base/dev/prod)."
    ),
    "flask/python/postgresql": (
        "Stack: Flask 3.x, Python 3.12, Flask-SQLAlchemy 3.x, Flask-Migrate. "
        "Layout: app/ with blueprints/, models/, schemas/. Application factory "
        "pattern. Use Marshmallow for serialisation. Migrations via alembic."
    ),
    # ── Java ───────────────────────────────────────────────────────
    "spring boot/java/postgresql": (
        "Stack: Spring Boot 3.x, Java 21, Spring Data JPA, Hibernate 6, "
        "Flyway. Layout: src/main/java/<group>/<app>/ with controller/, "
        "service/, repository/, entity/, dto/. Use @RestController, "
        "@RequestMapping, @Valid; Lombok for boilerplate; JpaRepository for DAOs; "
        "application.yml per profile (dev/qa/prod); Flyway under "
        "src/main/resources/db/migration."
    ),
    "spring boot/kotlin/postgresql": (
        "Stack: Spring Boot 3.x, Kotlin 2.x, Spring Data JPA, Hibernate 6, "
        "Flyway. Layout mirrors the Java preset but with .kt files, data "
        "classes for DTOs, and constructor injection (no @Autowired)."
    ),
    # ── Node ───────────────────────────────────────────────────────
    "express/node/postgresql": (
        "Stack: Express 4.x, Node 20, TypeScript, Knex or Prisma, Zod for "
        "validation. Layout: src/ with routes/, controllers/, services/, "
        "repositories/, models/. Async/await; centralised error middleware; "
        "winston/pino for logging."
    ),
    "nestjs/node/postgresql": (
        "Stack: NestJS 10, TypeScript, TypeORM or Prisma. Layout: src/ with "
        "<feature>/<feature>.module.ts/.controller.ts/.service.ts. DI via "
        "@Injectable; DTO classes with class-validator; e2e tests under test/."
    ),
    # ── .NET ───────────────────────────────────────────────────────
    "asp.net core/c#/sql server": (
        "Stack: ASP.NET Core 8, C# 12, EF Core 8. Layout: <App>/ with "
        "Controllers/, Services/, Models/, Data/. Minimal APIs or controllers; "
        "DI via builder.Services; FluentValidation; xUnit for tests."
    ),
    # ── Go ─────────────────────────────────────────────────────────
    "gin/go/postgresql": (
        "Stack: Go 1.22, gin-gonic, sqlc or GORM, golang-migrate. Layout: "
        "cmd/<app>/main.go + internal/<feature>/handler.go/service.go/repo.go. "
        "Standard project layout (cmd / internal / pkg). Use context.Context "
        "everywhere; zap or zerolog for logging."
    ),
}

_DEFAULT_CONVENTIONS = (
    "No target-stack-specific conventions registered for this combination. "
    "Generate idiomatic, layered code (routes / services / repositories / "
    "models). Follow standard project layout for the chosen language. "
    "Include dependency manifest, env-config, migrations, and tests."
)


def _conventions_for(target_tech: str) -> str:
    """Return a short stack-convention hint block for the LLM. Looks up
    by exact match, then by case-insensitive normalised match, falling
    back to a generic hint."""
    if not target_tech:
        return _DEFAULT_CONVENTIONS
    key = target_tech.strip().lower()
    if key in _STACK_CONVENTIONS:
        return _STACK_CONVENTIONS[key]
    # Try a fuzzy contains match — operator may have entered slight
    # variations like "FastAPI / Python 3.12 / Postgres 16".
    for k, v in _STACK_CONVENTIONS.items():
        if all(part in key for part in k.split("/")[:2]):  # framework + language
            return v
    return _DEFAULT_CONVENTIONS


# ──────────────────────────────────────────────────────────────────────
# Per-stage context fetchers (each returns a small JSON-able piece)
# ──────────────────────────────────────────────────────────────────────
async def _kb_summary(project_id: str) -> Dict[str, Any]:
    """File-count + entity-count summary. Avoids loading huge KB into RAM."""
    file_count = await kb_files.count_documents({"project_id": project_id})
    entity_count = await kb_entities.count_documents({"project_id": project_id})
    return {"files": file_count, "entities": entity_count}


async def _kb_toon_slice(project_id: str, stage: str) -> str:
    """Pull the stage-relevant slice of the TOON KB.

    iter-14.12 — actually stage-prune (previously returned the full TOON).
    Non-hot sections are demoted to "names only" so the LLM still sees
    every entity exists but pays no per-column / per-method cost for
    slices this stage doesn't need. See `kb.toon.prune` for the ordering.
    Budget is env-tunable via LAMA_TOON_STAGE_BUDGET_CHARS (default 30000
    chars ≈ 8-9k tokens) — well inside every provider's context window
    even for the largest legacy KBs.
    """
    doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0, "toon": 1})
    full = (doc or {}).get("toon", "") if doc else ""
    if not full:
        return ""
    budget = int(os.environ.get("LAMA_TOON_STAGE_BUDGET_CHARS", "30000") or "30000")
    from kb.toon import prune as _prune_toon
    return _prune_toon(full, budget, stage)


async def _srs_sections(project_id: str) -> Dict[str, str]:
    """Latest SRS section bodies keyed by section_key."""
    doc = await srs_documents.find_one(
        {"project_id": project_id}, {"_id": 0, "sections": 1, "version": 1},
    )
    if not doc:
        return {}
    sections = doc.get("sections") or []
    return {
        (s.get("section_key") or s.get("key") or ""): (s.get("content") or "")
        for s in sections if isinstance(s, dict)
    }


async def _datamodel_summary(project_id: str) -> Dict[str, Any]:
    """OLTP DDL + Bus Matrix + OLAP summary; full DDL embedded ONLY for
    stages downstream of DataModel (Architecture / CodeGen).

    iter-14.13 — Soft cap on the embedded OLTP DDL. For very large
    schemas (100+ tables → 200-400 KB of CREATE TABLE statements) the
    full DDL used to consume ~50 % of the Architecture / CodeGen
    context bundle even though downstream stages typically only need
    it as reference material. When the DDL exceeds the cap we truncate
    at a statement boundary and note the truncation — Architecture /
    CodeGen jobs can re-query `data_models` directly when they need
    the full text. Cap is env-tunable via LAMA_OLTP_DDL_MAX_CHARS
    (default 60000 chars ≈ 15-18 k tokens).
    """
    oltp = await data_models.find_one(
        {"project_id": project_id, "model_type": "oltp"},
        {"_id": 0, "ddl": 1, "tables": 1, "version": 1},
    ) or {}
    olap = await olap_models.find_one(
        {"project_id": project_id}, {"_id": 0, "ddl": 1, "facts": 1, "dimensions": 1},
    ) or {}
    bus = await bus_matrix.find_one(
        {"project_id": project_id}, {"_id": 0, "matrix": 1},
    ) or {}

    ddl = oltp.get("ddl") or ""
    olap_ddl = olap.get("ddl") or ""
    cap = int(os.environ.get("LAMA_OLTP_DDL_MAX_CHARS", "60000") or "60000")

    def _truncate_at_stmt_boundary(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        # Cut at the last full CREATE / ALTER / -- statement boundary
        # inside the budget so we don't leave a half-open parenthesis
        # in the prompt.
        head = text[:limit]
        for marker in ("\n\nCREATE ", "\nCREATE ", "\n\nALTER ", "\nALTER "):
            idx = head.rfind(marker)
            if idx > limit * 0.7:  # only accept if we keep >=70 % of budget
                head = head[:idx]
                break
        return head.rstrip() + (
            f"\n\n-- [truncated: original DDL was {len(text)} chars, "
            f"kept first {len(head)} at statement boundary. Query "
            f"data_models collection for full DDL.]"
        )

    return {
        "oltp_ddl": _truncate_at_stmt_boundary(ddl, cap),
        "oltp_tables": oltp.get("tables") or [],
        "olap_ddl": _truncate_at_stmt_boundary(olap_ddl, cap),
        "bus_matrix": bus.get("matrix") or [],
    }


async def _architecture_summary(project_id: str) -> Dict[str, Any]:
    """Service map + HLD/LLD summary for CodeGen consumption."""
    services = await arch_services.find(
        {"project_id": project_id}, {"_id": 0},
    ).to_list(200)
    docs = await arch_documents.find(
        {"project_id": project_id}, {"_id": 0, "doc_type": 1, "content": 1},
    ).to_list(50)
    by_type: Dict[str, str] = {}
    for d in docs:
        t = d.get("doc_type") or ""
        if t and t not in by_type:
            by_type[t] = d.get("content") or ""
    return {"services": services, "documents": by_type}


async def _previous_stage_outputs(project_id: str, stage: str) -> Dict[str, Any]:
    """Pull every upstream `stage_context` doc — the canonical inter-stage
    handoff payload (already validated by the freeze gate)."""
    stage_order = ["discovery", "datamodel", "architecture", "codegen"]
    if stage not in stage_order:
        return {}
    idx = stage_order.index(stage)
    upstream = stage_order[:idx]
    out: Dict[str, Any] = {}
    for s in upstream:
        doc = await stage_context.find_one(
            {"project_id": project_id, "stage": s},
            {"_id": 0, "outputs": 1, "version": 1, "frozen_at": 1},
        )
        if doc:
            out[s] = {
                "version": doc.get("version", 0),
                "frozen_at": doc.get("frozen_at", ""),
                "outputs": doc.get("outputs") or {},
            }
    return out


async def _integrations(project_id: str) -> List[Dict[str, Any]]:
    """Selected govt-service integrations (PAN, Aadhaar, UPI, …) so
    CodeGen knows which clients to scaffold."""
    rows = await project_integrations.find(
        {"project_id": project_id, "enabled": True},
        {"_id": 0, "integration_id": 1, "name": 1, "config": 1},
    ).to_list(50)
    return rows


# ──────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────
STAGE_KEYS = ("discovery", "datamodel", "architecture", "codegen", "living")


async def build_stage_context(
    project_id: str,
    stage: str,
    *,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Assemble the canonical input context for a stage.

    Always returns a JSON-serialisable dict with these top-level keys
    (any of them may be omitted if upstream data isn't ready yet):

      project        — basic project metadata + target_tech
      target_conventions — short stack-specific style hint for the LLM
      kb             — {files, entities} summary
      toon           — TOON KB string (stage-pruned where applicable)
      srs            — {section_key: content} from frozen SRS
      datamodel      — OLTP/OLAP DDL + bus matrix (only for stages ≥ Architecture)
      architecture   — service map + HLD/LLD (only for stages ≥ CodeGen)
      previous_stages — frozen stage_context for every upstream stage
      integrations   — enabled govt-service integrations
      legacy_analysis — cross-file business logic analysis (state machines, etc.)
      _meta          — {bundled_at, char_count, cache_key, cache_hit}

    Cached to local FS keyed by upstream version numbers — auto-invalidated
    on KB rebuild / SRS edit / stage freeze.
    """
    if stage not in STAGE_KEYS:
        raise ValueError(f"unknown stage: {stage!r}")

    # Resolve project + version keys for cache invalidation.
    proj = await projects.find_one(
        {"id": project_id},
        {"_id": 0, "id": 1, "name": 1, "target_tech": 1, "tenant_id": 1,
         "stage_status": 1, "legacy_tech": 1},
    ) or {}
    if not proj:
        return {}
    target_tech = (proj.get("target_tech") or "").strip()

    # Quick version probes for cache key (cheap projections).
    _toon = await kb_toon.find_one(
        {"project_id": project_id}, {"_id": 0, "version": 1, "updated_at": 1},
    ) or {}
    _srs = await srs_documents.find_one(
        {"project_id": project_id}, {"_id": 0, "version": 1, "updated_at": 1},
    ) or {}
    kb_version = str(_toon.get("version") or _toon.get("updated_at") or "0")
    srs_version = str(_srs.get("version") or _srs.get("updated_at") or "0")

    cache_key = _cache_key(
        project_id, stage,
        kb_version=kb_version, srs_version=srs_version,
        target_tech=target_tech,
    )
    cache_path = _cache_path(project_id, stage, cache_key) if use_cache else ""
    if cache_path:
        cached = _read_cache(cache_path)
        if cached:
            cached.setdefault("_meta", {})["cache_hit"] = True
            return cached

    bundle: Dict[str, Any] = {
        "project": {
            "id": proj.get("id"),
            "name": proj.get("name"),
            "tenant_id": proj.get("tenant_id"),
            "target_tech": target_tech,
            "legacy_tech": proj.get("legacy_tech") or "",
            "stage_status": proj.get("stage_status") or {},
        },
        "target_conventions": _conventions_for(target_tech),
    }

    # Discovery onwards — every stage needs KB summary + TOON.
    bundle["kb"] = await _kb_summary(project_id)
    bundle["toon"] = await _kb_toon_slice(project_id, stage)

    # Legacy analysis (the cross-file business logic doc).
    la = await legacy_analysis.find_one(
        {"project_id": project_id},
        {"_id": 0, "analysis": 1, "version": 1},
    )
    if la:
        bundle["legacy_analysis"] = la.get("analysis") or ""

    # Business ontology (if available).
    bo = await business_ontologies.find_one(
        {"project_id": project_id},
        {"_id": 0, "entities": 1, "relationships": 1},
    )
    if bo:
        bundle["business_ontology"] = {
            "entities": bo.get("entities") or [],
            "relationships": bo.get("relationships") or [],
        }

    # SRS — needed for DataModel onwards.
    if stage in ("datamodel", "architecture", "codegen", "living"):
        srs = await _srs_sections(project_id)
        if srs:
            bundle["srs"] = srs

    # DataModel — needed for Architecture / CodeGen / Living.
    if stage in ("architecture", "codegen", "living"):
        dm = await _datamodel_summary(project_id)
        if any(dm.values()):
            bundle["datamodel"] = dm

    # Architecture — needed for CodeGen / Living.
    if stage in ("codegen", "living"):
        arch = await _architecture_summary(project_id)
        if arch.get("services") or arch.get("documents"):
            bundle["architecture"] = arch

    # Integrations — primarily for CodeGen scaffolding.
    if stage in ("codegen", "architecture"):
        ints = await _integrations(project_id)
        if ints:
            bundle["integrations"] = ints

    # Frozen previous-stage handoffs (always — single source of truth).
    prev = await _previous_stage_outputs(project_id, stage)
    if prev:
        bundle["previous_stages"] = prev

    # Optional graph snapshot (richer than TOON for relationship queries).
    if os.environ.get("LAMA_USE_GRAPH_KB") in ("1", "true", "yes"):
        gr = await kb_graph.find_one(
            {"project_id": project_id},
            {"_id": 0, "stats": 1, "version": 1},
        )
        if gr:
            bundle["kb_graph"] = {
                "stats": gr.get("stats") or {},
                "version": gr.get("version", 0),
            }

    # Size cap — if the bundle is too large, drop the heaviest sections
    # in priority order: TOON > legacy_analysis > previous_stages.
    raw = json.dumps(bundle, ensure_ascii=False)
    if len(raw) > CONTEXT_BUNDLE_MAX_CHARS:
        for drop_key in ("toon", "legacy_analysis", "previous_stages"):
            if drop_key in bundle:
                bundle[drop_key] = (
                    "[truncated — original was %d chars, kept summary only]"
                    % len(json.dumps(bundle[drop_key], ensure_ascii=False))
                )
                raw = json.dumps(bundle, ensure_ascii=False)
                if len(raw) <= CONTEXT_BUNDLE_MAX_CHARS:
                    break

    bundle["_meta"] = {
        "bundled_at": time.time(),
        "char_count": len(raw),
        "cache_key": cache_key,
        "cache_hit": False,
        "kb_version": kb_version,
        "srs_version": srs_version,
    }

    if cache_path:
        _write_cache(cache_path, bundle)
    return bundle


def render_for_prompt(bundle: Dict[str, Any]) -> str:
    """Serialise a bundle into a single prompt-ready string with clear
    section markers. Use this in stage prompts instead of inlining
    individual fields manually.
    """
    if not bundle:
        return ""
    parts: List[str] = ["===== LAMA STAGE CONTEXT (host-anchored) ====="]
    proj = bundle.get("project") or {}
    if proj:
        parts.append(
            f"Project: {proj.get('name', '?')} (id={proj.get('id', '?')}, "
            f"tenant={proj.get('tenant_id', '?')})"
        )
        if proj.get("legacy_tech"):
            parts.append(f"Legacy stack: {proj['legacy_tech']}")
        if proj.get("target_tech"):
            parts.append(f"Target stack: {proj['target_tech']}")
    if bundle.get("target_conventions"):
        parts.append("\n--- TARGET-STACK CONVENTIONS ---")
        parts.append(bundle["target_conventions"])
    for key, label in (
        ("kb", "KB SUMMARY"),
        ("toon", "KB TOON"),
        ("legacy_analysis", "LEGACY-LOGIC ANALYSIS"),
        ("business_ontology", "BUSINESS ONTOLOGY"),
        ("srs", "SRS SECTIONS"),
        ("datamodel", "DATA MODEL"),
        ("architecture", "ARCHITECTURE"),
        ("integrations", "INTEGRATIONS"),
        ("previous_stages", "FROZEN UPSTREAM STAGES"),
    ):
        if key in bundle and bundle[key]:
            parts.append(f"\n--- {label} ---")
            val = bundle[key]
            if isinstance(val, str):
                parts.append(val)
            else:
                # iter-14.12 — compact JSON (no indent) for LLM consumption.
                # Indented JSON costs ~25-30% more tokens than compact for
                # the same data, and the LLM parses both identically. Keep
                # a single space after separators so numeric-heavy DDL
                # payloads stay readable in trace logs.
                parts.append(json.dumps(val, ensure_ascii=False, separators=(",", ":")))
    parts.append("\n===== END LAMA STAGE CONTEXT =====")
    return "\n".join(parts)

