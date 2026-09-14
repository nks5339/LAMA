"""Stage 2 — Data Model.

All endpoints require Discovery stage to be frozen (StageContext present).
Generates OLTP DDL, OLAP star schema, Kimball Bus Matrix, and 3 migration scripts.
"""
import io
import re
import json
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db import (
    projects,
    audit_log,
    prompts as prompts_col,
    project_prompts,
    data_models,
    bus_matrix as bus_matrix_col,
    olap_models,
    migration_artifacts,
    kb_entities,
    messages as messages_col,
    conversations,
    srs_documents,
    stage_context as stage_context_col,
    kb_files,
    kb_chunks,
    kb_toon,
    # iter-13.34 — collections previously missed by factory_reset, which
    # caused stale "Live Data Source" cards and stale "Currently using
    # deepseek" telemetry to survive a Refresh App.
    arch_documents,
    arch_services,
    codegen_files,
    codegen_runs,
    living_artifacts,
    living_runs,
    ontology_snapshots,
    business_ontologies,
    data_sources,
    legacy_analysis,
    kb_graph as kb_graph_col,
    token_usage_log,
    freeze_gates,
)
from models import DataModelArtifact, ChatMessage
from llm import fabric_call as chat_completion
from llm import fabric_call_with_session  # iter-13.100 — rolling-memory sessions
from llm import set_current_project_id, set_current_agent_key  # iter-13.38 / iter-13.81.2 — Factory.ai context propagation
from kb.vector_store import search as qdrant_search, delete_project_vectors
from pipeline import require_stage_context, save_stage_context

# iter-13.44 — hybrid pipeline: deterministic Python generators replace the
# LLM for OLTP / OLAP / Migration scripts by default. Bus Matrix stays on
# LLM (judgement call). Polish passes (ENUM / COMMENT) are opt-in.
from datamodel import config as dm_config
from datamodel import oltp_generator, olap_generator, migration_generator
from datamodel.bus_matrix_deriver import derive_bus_matrix as _derive_bm

logger = logging.getLogger("lama.datamodel")

router = APIRouter(prefix="/data-model", tags=["data-model"])

# In-memory job registry for long-running tasks that exceed K8s ingress timeout (~60s).
# Replaces SSE for OLAP + migration-scripts; the frontend polls /jobs/{id} every ~2s.
# Restarting the backend clears all jobs — that's intentional, no need for persistence.
_JOBS: dict = {}
_JOB_TTL_SEC = 60 * 60  # keep job records around for an hour after completion


def _new_job(project_id: str, kind: str) -> str:
    import uuid
    import time as _time
    jid = uuid.uuid4().hex
    _JOBS[jid] = {
        "id": jid,
        "project_id": project_id,
        "kind": kind,
        "status": "queued",     # queued | running | complete | error
        "step": "Queued…",
        "pct": 0,
        "started_at": _time.time(),
        "ended_at": None,
        "error": None,
        "result": {},
    }
    # opportunistic cleanup of old jobs
    now = _time.time()
    stale = [k for k, v in _JOBS.items() if v.get("ended_at") and now - v["ended_at"] > _JOB_TTL_SEC]
    for k in stale:
        _JOBS.pop(k, None)
    return jid


def _job_update(jid: str, **kw):
    job = _JOBS.get(jid)
    if not job:
        return
    job.update(kw)


def _job_finish(jid: str, status: str, **kw):
    import time as _time
    job = _JOBS.get(jid)
    if not job:
        return
    job["status"] = status
    job["ended_at"] = _time.time()
    job.update(kw)



# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------
async def _get_prompt(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts_col.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


async def _with_completeness_contract(project_id: str, sp: str) -> str:
    """Prepend gov.completeness_contract so every DataModel generation /
    regeneration (OLTP, OLAP, Bus Matrix) inherits the stage-agnostic
    ≥ 95 % confidence target + CONFIDENCE_SELF_SCORE footer contract.
    Silent no-op when the prompt is unseeded."""
    try:
        contract = await _get_prompt(project_id, "gov.completeness_contract")
    except Exception:
        contract = ""
    if not contract:
        return sp
    return (
        "═══════════════════════════════════════════════════════════════════\n"
        "GOVERNANCE BLOCK · gov.completeness_contract (LOADED FIRST)\n"
        "Target: every regenerate climbs to ≥ 95 % confidence per artifact.\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"{contract}\n\n"
        f"{sp}"
    )


def _safe_format(template: str, **vars) -> str:
    """str.format with neutralised user-content braces."""
    return template.format(**{k: (str(v) if v is not None else "") for k, v in vars.items()})


async def _save_artifact(project_id: str, type_: str, content: str, model: str, tracability: dict) -> dict:
    existing = await data_models.find_one({"project_id": project_id, "type": type_}, {"_id": 0})
    version = ((existing or {}).get("version", 0)) + 1
    art = DataModelArtifact(
        project_id=project_id,
        type=type_,
        content=content,
        version=version,
        generated_by=model,
        tracability=tracability,
    )
    doc = art.model_dump()
    # Replace existing (one artifact per type)
    if existing:
        doc["id"] = existing["id"]
        doc["created_at"] = existing.get("created_at", doc["created_at"])
        doc["frozen"] = existing.get("frozen", False)
        await data_models.update_one({"project_id": project_id, "type": type_}, {"$set": doc})
    else:
        await data_models.insert_one(doc)
    return doc


async def _strip_md_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        for tag in ("sql", "json", "python", "py"):
            if t[: len(tag)].lower() == tag:
                t = t[len(tag) :]
                break
        t = t.lstrip("\n")
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


async def _wait_for_llm_with_progress(gen_task, label: str, start_pct: int = 40, end_pct: int = 88):
    """Async generator that yields SSE progress events while gen_task runs.

    Replaces silent `: ping` keepalives with VISIBLE progress events showing
    elapsed time, so the UI never looks stuck during a 60-120s LLM call.
    Yields events; caller is responsible for awaiting gen_task at end.
    """
    import time as _time
    started = _time.time()
    while not gen_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(gen_task), timeout=4.0)
        except asyncio.TimeoutError:
            elapsed = int(_time.time() - started)
            # Asymptotic curve: rises fast at first, then slows toward end_pct
            pct = min(end_pct, start_pct + int((end_pct - start_pct) * (1 - 1 / (1 + elapsed / 30))))
            yield f"data: {json.dumps({'type': 'progress', 'step': f'{label} ({elapsed}s elapsed)', 'pct': pct})}\n\n"
        except Exception:
            break



# -----------------------------------------------------------
# JOB-based generation (bypass K8s ingress 60s timeout)
# Frontend POSTs to /jobs/start/{kind}, gets a job_id, then polls /jobs/{id}.
# -----------------------------------------------------------
# -----------------------------------------------------------
# iter-13.44 — Optional LLM "polish" passes used by the deterministic OLTP path.
# These are small targeted calls, opt-in via flags. They do NOT touch the
# main DDL body — they prepend ENUM TYPE declarations or append COMMENT ON
# COLUMN statements. Failure is logged and silently skipped (caller decides).
# -----------------------------------------------------------
# -----------------------------------------------------------
# iter-14.56 — "No TABLE entities" diagnostic. When the deterministic
# OLTP / OLAP / Migration path finds zero `type: "TABLE"` rows in
# kb_entities, we now inspect `data_sources` to detect the specific
# "live-DB ingest was wiped by a later Build KB" scenario and return a
# targeted, actionable error instead of the generic
# "Re-run Build KB or fall back to LLM mode" hint (which is wrong —
# Build KB does NOT reach out to Oracle / Postgres, it only re-parses
# uploaded source files, so it would leave the pipeline in the same
# broken state).
# -----------------------------------------------------------
async def _no_tables_error(project_id: str) -> str:
    """Produce a targeted operator-facing error string when the
    deterministic path finds no ``type: "TABLE"`` entities.

    Detects three cases:
      1. A live DB is registered and ingest previously succeeded
         (``schema_summary.tables > 0``): entities were wiped by a later
         Build KB — tell the user to re-register the live DB (credentials
         are only stored as a sha256 hash so we cannot re-connect
         silently).
      2. A live DB is registered but ingest failed (driver missing / auth
         error): surface the stored ``last_extract_error`` and tell them
         to fix that first.
      3. No live DB registered at all: tell them to either register one
         from the Discovery → Data Sources tab, upload a .sql DDL dump,
         or fall back to LLM mode.
    """
    try:
        srcs = await data_sources.find(
            {"project_id": project_id, "kind": "database"}, {"_id": 0}
        ).to_list(20)
    except Exception:
        srcs = []
    if not srcs:
        return (
            "No TABLE entities found in KB. This project has no live database "
            "registered and no SQL DDL uploaded. Fix any ONE of these:\n"
            "  • Discovery → Data Sources → 'Register Live DB' and enter your "
            "Oracle / Postgres / MySQL / SQL Server / SQLite credentials, OR\n"
            "  • Upload a .sql DDL dump via Discovery → KB Upload, then re-run Build KB, OR\n"
            "  • Switch OLTP generation to 'llm' mode (Console → DataModel → Generation Modes)."
        )
    prev_ingested = 0
    prev_fks = 0
    prev_error = ""
    prev_host = ""
    prev_db = ""
    prev_dbtype = ""
    for s in srcs:
        summary = s.get("schema_summary") or {}
        t = int(summary.get("tables") or 0)
        if t > prev_ingested:
            prev_ingested = t
            prev_fks = int(summary.get("fk_count") or 0)
            prev_host = s.get("host") or ""
            prev_db = s.get("database") or ""
            prev_dbtype = s.get("db_type") or ""
        if s.get("last_extract_error"):
            prev_error = s.get("last_extract_error") or ""
    if prev_ingested > 0:
        return (
            f"No TABLE entities found in KB — but Discovery still shows "
            f"{prev_ingested} tables · {prev_fks} FKs from a previous live-DB "
            f"ingest ({prev_dbtype}://{prev_host}/{prev_db}). A later 'Build KB' "
            "wiped them (Build KB re-parses uploaded source only; it does NOT "
            "re-connect to your live DB). Remediation:\n"
            "  • Discovery → Data Sources → 'Register Live DB' again (enter the "
            "password once more — LAMA only stores its sha256 hash), OR\n"
            "  • Upload the schema as a .sql DDL dump, OR\n"
            "  • Switch this generation to 'llm' mode.\n"
            "iter-14.56 patched Build KB to preserve `from_live_db` entities "
            "going forward — this repair only needs to happen once per project."
        )
    if prev_error:
        return (
            "No TABLE entities found in KB. A live database is registered "
            f"({prev_dbtype}://{prev_host}/{prev_db}) but the last ingest failed:\n"
            f"  → {prev_error[:400]}\n"
            "Fix the driver / credentials / network path, then re-register "
            "the live DB from Discovery → Data Sources."
        )
    return (
        "No TABLE entities found in KB. A live database is registered but no "
        "schema has been ingested yet. Open Discovery → Data Sources and "
        "click 'Register Live DB' to trigger the ingest, or upload a .sql "
        "DDL dump."
    )


# -----------------------------------------------------------
# iter-13.45 — Bus-matrix ensure helper. Returns (bus_matrix_dict, source)
# where source ∈ {"existing", "derived"}. When deterministic OLAP / migration
# is requested without a Bus Matrix artifact, we now auto-derive a heuristic
# one from kb_entities and persist it as a normal bus_matrix artifact (so
# the next regen reuses it). The user can still overwrite it by running
# /generate/bus-matrix (LLM-driven) afterwards.
# -----------------------------------------------------------
async def _ensure_bus_matrix(project_id: str, proj: dict) -> tuple[dict | None, str, dict | None]:
    """Return ``(bus_matrix_dict, source, artifact)``.

    source:
      "existing" — found a saved bus_matrix artifact.
      "derived"  — auto-derived from kb_entities + persisted now.
      "missing"  — KB has no TABLE entities to derive from (bus_matrix_dict is None).
    """
    existing = await data_models.find_one(
        {"project_id": project_id, "type": "bus_matrix"}, {"_id": 0}
    )
    if existing and (existing.get("content") or "").strip():
        try:
            return json.loads(existing["content"]), "existing", existing
        except Exception as exc:
            logger.warning("Existing bus_matrix is not valid JSON (%s) — re-deriving", exc)

    ents = await kb_entities.find(
        {"project_id": project_id, "type": "TABLE"}, {"_id": 0}
    ).to_list(2000)
    if not ents:
        return None, "missing", None

    bm = _derive_bm(ents)
    tracability = {
        "source": "heuristic_derivation",
        "derivation_note": bm.get("derivation_note"),
        "entities_used": len(ents),
        "stats": bm.get("stats"),
        "model": "(deterministic)",
        "prompt_key": None,
    }
    art = await _save_artifact(
        project_id, "bus_matrix",
        json.dumps(bm, indent=2),
        "(deterministic-derived)",
        tracability,
    )
    await audit_log.insert_one({
        "action": "datamodel.bus_matrix.derived",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"facts": len(bm.get("facts") or []),
                    "dims": len(bm.get("dimensions") or []),
                    "version": art["version"],
                    "entities_used": len(ents)},
    })
    art["content"] = json.dumps(bm, indent=2)
    return bm, "derived", art


async def _llm_polish_enums(ddl: str, model: str) -> str:
    """Ask the LLM to emit CREATE TYPE ... AS ENUM (...) declarations for any
    free-text status / type columns it can identify in the DDL.

    Returns just the ENUM block (caller prepends to the DDL). Empty string
    on failure or when no enums are inferable.
    """
    if not ddl.strip():
        return ""
    sys_prompt = (
        "You are a PostgreSQL DBA. Below is a deterministically-generated DDL. "
        "Look ONLY at VARCHAR / TEXT columns whose names look like status / "
        "state / type / kind / category enums. For each one you are CONFIDENT "
        "about (column name strongly implies a fixed set of values), emit a "
        "single `CREATE TYPE \"<table>_<col>_enum\" AS ENUM ('VAL1', 'VAL2', …);` "
        "declaration. Use UPPER_SNAKE for the values. Do NOT invent enums for "
        "free-form text columns. Do NOT touch the existing DDL. Return ONLY "
        "the CREATE TYPE statements separated by newlines, or an empty string "
        "if no confident enums exist. No prose, no markdown fences.\n\n"
        "DDL:\n" + ddl[:14000]
    )
    try:
        result = await chat_completion(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "Emit the CREATE TYPE block now."},
            ],
            model=model, temperature=0.1, max_tokens=2000, timeout=120.0,
        )
        text = (result.get("content") or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip()
            if text.lower().startswith("sql"):
                text = text[3:].lstrip()
            if text.endswith("```"):
                text = text[:-3].rstrip()
        return text
    except Exception as exc:
        logger.warning("ENUM polish LLM call failed: %s", exc)
        return ""


async def _llm_polish_comments(ddl: str, model: str) -> str:
    """Ask the LLM to emit `COMMENT ON COLUMN "tbl"."col" IS '…';` statements
    for any column whose name alone is cryptic (abbreviations, codes).
    Returns just the COMMENT block (caller appends). Empty string on failure.
    """
    if not ddl.strip():
        return ""
    sys_prompt = (
        "You are a PostgreSQL DBA. Below is a generated DDL. For each column "
        "whose name is cryptic or abbreviated (e.g. cstm_id, hsn_cd, gstn, "
        "ptr_no) — NOT obvious nouns like email, created_at, amount — emit a "
        "single `COMMENT ON COLUMN \"<table>\".\"<column>\" IS 'plain-english "
        "description';` line. Skip columns whose meaning is obvious. Return "
        "ONLY the COMMENT ON COLUMN statements, one per line, no prose, no "
        "markdown fences.\n\n"
        "DDL:\n" + ddl[:14000]
    )
    try:
        result = await chat_completion(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "Emit the COMMENT ON COLUMN block now."},
            ],
            model=model, temperature=0.2, max_tokens=3000, timeout=150.0,
        )
        text = (result.get("content") or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip()
            if text.lower().startswith("sql"):
                text = text[3:].lstrip()
            if text.endswith("```"):
                text = text[:-3].rstrip()
        return text
    except Exception as exc:
        logger.warning("COMMENT polish LLM call failed: %s", exc)
        return ""


# -----------------------------------------------------------
async def _run_olap_job(job_id: str, project_id: str, model: str,
                        generation_overrides: dict | None = None):
    """Background OLAP generation.

    iter-13.44 — defaults to deterministic generator (no LLM). Bus matrix
    must already exist (still needed even in deterministic mode — that's
    the LLM step we keep). Override via flags as in `_run_oltp_job`.
    """
    import time as _time
    try:
        _job_update(job_id, status="running", step="Loading context…", pct=5)
        discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        oltp_art = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
        if not oltp_art:
            _job_finish(job_id, "error", error="Generate OLTP DDL first.")
            return

        bus_art = await data_models.find_one({"project_id": project_id, "type": "bus_matrix"}, {"_id": 0})
        modes = dm_config.resolve_modes(project=proj, request_overrides=generation_overrides)
        _job_update(job_id, step=f"Resolved generation modes: {dm_config.describe_mode(modes)}", pct=8)

        # ── iter-13.44 — deterministic path ─────────────────────────────────
        if modes["olap"] == "deterministic":
            # iter-13.45 — auto-derive a heuristic Bus Matrix if none exists.
            # The LLM bus-matrix step is still the preferred path (better
            # judgement on facts/dims), but we no longer hard-fail when the
            # user hasn't run it yet.
            bus_matrix, bm_source, bm_art_now = await _ensure_bus_matrix(project_id, proj)
            if bus_matrix is None:
                _job_finish(
                    job_id, "error",
                    error=await _no_tables_error(project_id) +
                          "\n\n(Bus Matrix derivation also requires TABLE entities. "
                          "Alternatively switch OLAP to 'llm' mode.)",
                )
                return
            if bm_source == "derived":
                _job_update(
                    job_id, step="Auto-derived Bus Matrix from kb_entities (heuristic).", pct=25,
                )
                bus_art = bm_art_now
            else:
                bus_art = bus_art or bm_art_now
            _job_update(job_id, step="Rendering star-schema DDL deterministically…", pct=55)
            content = olap_generator.generate_olap_ddl(bus_matrix, project_name=proj.get("name", ""))
            st = olap_generator.stats(content)
            _job_update(job_id, step="Saving artifact…", pct=92)
            tracability = {
                "discovery_version": discovery_ctx.get("version"),
                "oltp_version": oltp_art.get("version"),
                "bus_matrix_version": (bus_art or {}).get("version"),
                "bus_matrix_source": bm_source,  # iter-13.45
                "model": "(deterministic)",
                "generator": "datamodel.olap_generator",
                "generation_modes": modes,
                "prompt_key": None,
            }
            art = await _save_artifact(project_id, "olap_ddl", content, "(deterministic)", tracability)
            await audit_log.insert_one({
                "action": "datamodel.generate.olap",
                "project_id": project_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": {
                    "version": art["version"], "dims": st["dims"], "facts": st["facts"],
                    "mvs": st["mvs"], "mode": "deterministic", "modes": modes, "job_id": job_id,
                },
            })
            _job_finish(
                job_id, "complete", step="Done (deterministic)", pct=100,
                result={
                    "artifact_id": art["id"], "version": art["version"],
                    "dims": st["dims"], "facts": st["facts"], "mvs": st["mvs"],
                    "mode": "deterministic", "modes": modes,
                },
            )
            return

        # ── Legacy LLM path (unchanged) ─────────────────────────────────────
        srs_functional = (discovery_ctx.get("outputs", {}).get("srs_sections", {}) or {}).get("functional_requirements", "")[:6000]

        _job_update(job_id, step="Building prompt…", pct=15)
        template = await _get_prompt(project_id, "datamodel.olap")
        if not template:
            _job_finish(job_id, "error", error="datamodel.olap prompt missing.")
            return

        system_prompt = _safe_format(
            template,
            project_name=proj.get("name", ""),
            oltp_ddl=oltp_art.get("content", "")[:14000],
            srs_functional=srs_functional,
            bus_matrix=(bus_art or {}).get("content", "")[:4000],
        )
        system_prompt = await _with_completeness_contract(project_id, system_prompt)

        _job_update(job_id, step="Calling LLM (60-120s typical)…", pct=25)
        gen_task = asyncio.create_task(
            chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate the complete OLAP star-schema DDL now."},
                ],
                model=model,
                temperature=0.15,
                max_tokens=14000,
                timeout=300.0,
            )
        )
        started = _time.time()
        while not gen_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(gen_task), timeout=3.0)
            except asyncio.TimeoutError:
                elapsed = int(_time.time() - started)
                pct = min(88, 25 + int(63 * (1 - 1 / (1 + elapsed / 25))))
                _job_update(job_id, step=f"LLM generating ({elapsed}s elapsed)…", pct=pct)
            except Exception:
                break

        try:
            result = await gen_task
        except Exception as e:
            _job_finish(job_id, "error", error=f"LLM call failed: {e}")
            return

        content = await _strip_md_fence(result.get("content", ""))
        if not content.strip():
            _job_finish(job_id, "error", error="LLM returned empty DDL.")
            return

        _job_update(job_id, step="Saving artifact…", pct=92)
        dim_n = content.upper().count("CREATE TABLE DIM_") + content.upper().count("CREATE TABLE \"DIM_")
        fact_n = content.upper().count("CREATE TABLE FACT_") + content.upper().count("CREATE TABLE \"FACT_")
        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "oltp_version": oltp_art.get("version"),
            "bus_matrix_version": (bus_art or {}).get("version"),
            "model": model,
            "prompt_key": "datamodel.olap",
        }
        art = await _save_artifact(project_id, "olap_ddl", content, model, tracability)
        await audit_log.insert_one({
            "action": "datamodel.generate.olap",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "dims": dim_n, "facts": fact_n, "model": model, "job_id": job_id},
        })
        _job_finish(
            job_id,
            "complete",
            step="Done",
            pct=100,
            result={"artifact_id": art["id"], "version": art["version"], "dims": dim_n, "facts": fact_n},
        )
    except HTTPException as he:
        _job_finish(job_id, "error", error=he.detail)
    except Exception as e:
        _job_finish(job_id, "error", error=str(e))


async def _run_scripts_job(job_id: str, project_id: str, model: str,
                           generation_overrides: dict | None = None):
    """Background generation of all 3 migration scripts.

    iter-13.44 — defaults to deterministic generator (no LLM). Override
    via flags as in `_run_oltp_job`.
    """
    import time as _time
    try:
        _job_update(job_id, status="running", step="Loading context…", pct=2)
        discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        oltp_art = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
        olap_art = await data_models.find_one({"project_id": project_id, "type": "olap_ddl"}, {"_id": 0})
        if not oltp_art or not olap_art:
            _job_finish(job_id, "error", error="Generate both OLTP and OLAP DDL first.")
            return

        modes = dm_config.resolve_modes(project=proj, request_overrides=generation_overrides)
        _job_update(job_id, step=f"Resolved generation modes: {dm_config.describe_mode(modes)}", pct=6)

        # ── iter-13.44 — deterministic path ─────────────────────────────────
        if modes["migration"] == "deterministic":
            _job_update(job_id, step="Loading entities + bus matrix…", pct=15)
            ents = await kb_entities.find(
                {"project_id": project_id, "type": "TABLE"}, {"_id": 0}
            ).to_list(2000)
            # iter-13.45 — auto-derive heuristic Bus Matrix if missing.
            bus_matrix, bm_source, bus_art = await _ensure_bus_matrix(project_id, proj)
            if bus_matrix is None:
                _job_finish(
                    job_id, "error",
                    error=await _no_tables_error(project_id) +
                          "\n\n(Bus Matrix derivation also requires TABLE entities. "
                          "Alternatively switch migration to 'llm' mode.)",
                )
                return
            if bm_source == "derived":
                _job_update(job_id, step="Auto-derived Bus Matrix (heuristic) for ETL.", pct=22)

            _job_update(job_id, step="Rendering migrate_old_to_oltp.py…", pct=35)
            scripts = migration_generator.generate_all(
                ents, bus_matrix, source_tech=proj.get("source_tech", "MySQL"),
            )
            results: dict = {}
            for idx, (script_type, content) in enumerate(scripts.items()):
                _job_update(job_id, step=f"Saving {script_type}.py…", pct=55 + idx * 12)
                tracability = {
                    "discovery_version": discovery_ctx.get("version"),
                    "oltp_version": oltp_art.get("version"),
                    "olap_version": olap_art.get("version"),
                    "bus_matrix_version": (bus_art or {}).get("version"),
                    "bus_matrix_source": bm_source,  # iter-13.45
                    "model": "(deterministic)",
                    "generator": "datamodel.migration_generator",
                    "generation_modes": modes,
                    "prompt_key": None,
                }
                art = await _save_artifact(project_id, script_type, content, "(deterministic)", tracability)
                results[script_type] = {"artifact_id": art["id"], "version": art["version"]}
            await audit_log.insert_one({
                "action": "datamodel.generate.migration_scripts",
                "project_id": project_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": {"mode": "deterministic", "modes": modes,
                            "scripts": list(results.keys()),
                            "bus_matrix_source": bm_source, "job_id": job_id},
            })
            _job_finish(job_id, "complete",
                        step="All 3 scripts generated (deterministic).", pct=100,
                        result={**results, "mode": "deterministic", "modes": modes})
            return

        # ── Legacy LLM path (unchanged) ─────────────────────────────────────
        tables = await kb_entities.find({"project_id": project_id, "type": "TABLE"}, {"_id": 0}).to_list(500)
        kb_tables = "\n".join(
            f"{t.get('name')}({', '.join((c.get('name') for c in (t.get('columns') or [])[:8]))})"
            for t in tables[:200]
        )[:8000]
        oltp_ddl = oltp_art.get("content", "")[:12000]
        olap_ddl = olap_art.get("content", "")[:12000]

        results = {}
        scripts = ["migrate_old_to_oltp", "migrate_oltp_to_olap", "test_migration"]
        for idx, script_type in enumerate(scripts):
            base_pct = 5 + idx * 31
            _job_update(job_id, step=f"Generating {script_type}.py…", pct=base_pct)
            prompt_template = _SCRIPT_PROMPTS[script_type]
            system_prompt = prompt_template.format(
                source_tech=proj.get("source_tech", ""),
                kb_tables=kb_tables,
                oltp_ddl=oltp_ddl,
                olap_ddl=olap_ddl,
            )
            gen_task = asyncio.create_task(
                chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Write {script_type}.py now."},
                    ],
                    model=model,
                    temperature=0.15,
                    max_tokens=8000,
                    timeout=240.0,
                )
            )
            started = _time.time()
            while not gen_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(gen_task), timeout=3.0)
                except asyncio.TimeoutError:
                    elapsed = int(_time.time() - started)
                    _job_update(
                        job_id,
                        step=f"{script_type}.py — LLM generating ({elapsed}s elapsed)…",
                        pct=min(base_pct + 25, base_pct + int(25 * (1 - 1 / (1 + elapsed / 25)))),
                    )
                except Exception:
                    break
            try:
                r = await gen_task
            except Exception as e:
                _job_finish(job_id, "error", error=f"{script_type} failed: {e}")
                return
            content = await _strip_md_fence(r.get("content", ""))
            if not content.strip():
                _job_finish(job_id, "error", error=f"{script_type}: empty content from LLM.")
                return
            tracability = {
                "discovery_version": discovery_ctx.get("version"),
                "oltp_version": oltp_art.get("version"),
                "olap_version": olap_art.get("version"),
                "model": model,
                "prompt_key": f"_inline.{script_type}",
            }
            art = await _save_artifact(project_id, script_type, content, model, tracability)
            results[script_type] = {"artifact_id": art["id"], "version": art["version"]}
        _job_finish(job_id, "complete", step="All 3 scripts generated.", pct=100, result=results)
    except HTTPException as he:
        _job_finish(job_id, "error", error=he.detail)
    except Exception as e:
        _job_finish(job_id, "error", error=str(e))


async def _run_oltp_job(job_id: str, project_id: str, model: str,
                        generation_overrides: dict | None = None):
    """Background OLTP DDL generation.

    iter-13.44 — defaults to deterministic Python generator (no LLM, ~zero
    tokens). Set `generation_overrides={"oltp":"llm"}`, project
    `settings.datamodel_generation.oltp="llm"`, or env
    `LAMA_OLTP_DETERMINISTIC=0` to fall back to the legacy LLM path.
    """
    import time as _time
    try:
        _job_update(job_id, status="running", step="Loading context…", pct=5)
        discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        if not proj:
            _job_finish(job_id, "error", error="Project not found")
            return

        modes = dm_config.resolve_modes(project=proj, request_overrides=generation_overrides)
        _job_update(job_id, step=f"Resolved generation modes: {dm_config.describe_mode(modes)}", pct=8)

        # ── iter-13.44 — deterministic path ─────────────────────────────────
        if modes["oltp"] == "deterministic":
            _job_update(job_id, step="Generating OLTP DDL deterministically (no LLM)…", pct=20)
            ents = await kb_entities.find(
                {"project_id": project_id, "type": "TABLE"}, {"_id": 0}
            ).to_list(2000)
            if not ents:
                _job_finish(job_id, "error", error=await _no_tables_error(project_id))
                return
            domain_map = (discovery_ctx.get("outputs") or {}).get("domain_map") or {}
            data_hints = (discovery_ctx.get("outputs") or {}).get("data_model_hints") or {}
            # FR↔table links may come from kb.br_tracker or SRS traceability.
            try:
                from db import br_table_links as _brl
                fr_links = await _brl.find({"project_id": project_id}, {"_id": 0}).to_list(2000)
            except Exception:
                fr_links = []
            _job_update(job_id, step=f"Rendering DDL for {len(ents)} tables…", pct=55)
            content = oltp_generator.generate_oltp_ddl(
                ents,
                domain_map=domain_map,
                data_model_hints=data_hints,
                fr_links=fr_links,
                project_name=proj.get("name", ""),
                source_tech=proj.get("source_tech", ""),
            )

            # Optional polish passes (small LLM calls — opt-in via flags).
            if modes.get("enum_polish"):
                _job_update(job_id, step="Polish: ENUM types via LLM…", pct=72)
                try:
                    enum_block = await _llm_polish_enums(content, model)
                    if enum_block.strip():
                        content = enum_block.rstrip() + "\n\n" + content
                except Exception as exc:
                    logger.warning("OLTP enum polish failed (continuing): %s", exc)
            if modes.get("comment_polish"):
                _job_update(job_id, step="Polish: COMMENT ON COLUMN via LLM…", pct=82)
                try:
                    comments_block = await _llm_polish_comments(content, model)
                    if comments_block.strip():
                        content = content.rstrip() + "\n\n" + comments_block.rstrip() + "\n"
                except Exception as exc:
                    logger.warning("OLTP comment polish failed (continuing): %s", exc)

            _job_update(job_id, step="Saving artifact…", pct=92)
            st = oltp_generator.stats(content)
            tracability = {
                "discovery_version": discovery_ctx.get("version"),
                "model": "(deterministic)",
                "generator": "datamodel.oltp_generator",
                "generation_modes": modes,
                "prompt_key": None,
                "entities_used": len(ents),
                "fr_links_used": len(fr_links),
            }
            art = await _save_artifact(project_id, "oltp_ddl", content, "(deterministic)", tracability)
            await audit_log.insert_one({
                "action": "datamodel.generate.oltp",
                "project_id": project_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": {
                    "version": art["version"], "tables": st["tables"], "fks": st["fks"],
                    "indexes": st["indexes"], "mode": "deterministic", "modes": modes,
                    "job_id": job_id,
                },
            })
            _job_finish(
                job_id, "complete", step="Done (deterministic)", pct=100,
                result={
                    "artifact_id": art["id"], "version": art["version"],
                    "tables": st["tables"], "fks": st["fks"], "indexes": st["indexes"],
                    "mode": "deterministic", "modes": modes,
                },
            )
            return

        # ── Legacy LLM path (unchanged) ─────────────────────────────────────
        srs_functional = (discovery_ctx.get("outputs", {}).get("srs_sections", {}) or {}).get("functional_requirements", "")[:8000]
        domain_map = discovery_ctx.get("outputs", {}).get("domain_map", {})
        data_hints = discovery_ctx.get("outputs", {}).get("data_model_hints", {})
        domain_map_str = json.dumps(
            {k: {"tables": (v or {}).get("tables", [])[:15]} for k, v in (domain_map or {}).items()}
        )[:4000]

        _job_update(job_id, step="Retrieving KB chunks…", pct=15)
        try:
            rag_chunks = await qdrant_search(project_id, "database tables relationships foreign keys constraints", top_k=20)
        except Exception:
            rag_chunks = []
        rag_context = "\n\n---\n\n".join(rag_chunks)[:12000] if rag_chunks else (discovery_ctx.get("toon_summary") or "")[:12000]

        n_tables = data_hints.get("domains") and sum(len(d.get("tables", [])) for d in (data_hints.get("domains") or {}).values())
        if not n_tables:
            n_tables = await kb_entities.count_documents({"project_id": project_id, "type": "TABLE"})

        _job_update(job_id, step="Building prompt…", pct=22)
        template = await _get_prompt(project_id, "datamodel.oltp")
        if not template:
            _job_finish(job_id, "error", error="datamodel.oltp prompt missing in seed.")
            return

        system_prompt = _safe_format(
            template,
            project_name=proj.get("name", ""),
            source_tech=proj.get("source_tech", ""),
            target_tech=proj.get("target_tech", ""),
            rag_context=rag_context,
            domain_map=domain_map_str,
            srs_functional=srs_functional,
        )
        system_prompt = await _with_completeness_contract(project_id, system_prompt)

        _job_update(job_id, step=f"Calling LLM on {n_tables} legacy tables (60–120s typical)…", pct=30)
        gen_task = asyncio.create_task(
            chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate the complete OLTP DDL now. Be exhaustive."},
                ],
                model=model,
                temperature=0.15,
                max_tokens=16000,
                timeout=300.0,
            )
        )
        started = _time.time()
        while not gen_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(gen_task), timeout=3.0)
            except asyncio.TimeoutError:
                elapsed = int(_time.time() - started)
                pct = min(88, 30 + int(58 * (1 - 1 / (1 + elapsed / 25))))
                _job_update(job_id, step=f"LLM generating ({elapsed}s elapsed)…", pct=pct)
            except Exception:
                break

        try:
            result = await gen_task
        except Exception as e:
            _job_finish(job_id, "error", error=f"LLM call failed: {e}")
            return

        content = await _strip_md_fence(result.get("content", ""))
        if not content.strip():
            _job_finish(job_id, "error", error="LLM returned empty DDL.")
            return

        _job_update(job_id, step="Saving artifact…", pct=92)
        tables_n = content.upper().count("CREATE TABLE")
        fks_n = content.upper().count("REFERENCES ")

        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "srs_sections_used": ["functional_requirements"],
            "rag_chunks": len(rag_chunks),
            "model": model,
            "prompt_key": "datamodel.oltp",
        }
        art = await _save_artifact(project_id, "oltp_ddl", content, model, tracability)
        await audit_log.insert_one({
            "action": "datamodel.generate.oltp",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "tables": tables_n, "fks": fks_n, "model": model, "job_id": job_id},
        })
        _job_finish(
            job_id,
            "complete",
            step="Done",
            pct=100,
            result={"artifact_id": art["id"], "version": art["version"], "tables": tables_n, "fks": fks_n},
        )
    except HTTPException as he:
        _job_finish(job_id, "error", error=he.detail)
    except Exception as e:
        _job_finish(job_id, "error", error=str(e))


@router.post("/{project_id}/bus-matrix/apply")
async def apply_bus_matrix_change(project_id: str, payload: dict):
    """Replace bus_matrix artifact with the supplied JSON (used by chat Apply button)."""
    raw = (payload or {}).get("matrix") or (payload or {}).get("content") or ""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            try:
                s = raw.index("{")
                e = raw.rindex("}")
                parsed = json.loads(raw[s:e + 1])
            except Exception as ex:
                raise HTTPException(400, f"Invalid JSON: {ex}")
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise HTTPException(400, "matrix payload required")

    existing = await data_models.find_one({"project_id": project_id, "type": "bus_matrix"}, {"_id": 0})
    tracability = {
        "source": "chat_apply",
        "previous_version": (existing or {}).get("version"),
    }
    art = await _save_artifact(project_id, "bus_matrix",
                               json.dumps(parsed, indent=2),
                               (existing or {}).get("generated_by", "user-edit"),
                               tracability)
    await audit_log.insert_one({
        "action": "datamodel.bus_matrix.apply",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"version": art["version"], "facts": len(parsed.get("facts", []) or []),
                    "dims": len(parsed.get("dimensions", []) or [])},
    })
    return {"ok": True, "artifact_id": art["id"], "version": art["version"], "matrix": parsed}


@router.post("/{project_id}/er/apply")
async def apply_er_change(project_id: str, payload: dict):
    """Apply an ER patch (add_edges / remove_edges) directly to kb_entities.fks so the
    deterministic ER builder picks them up immediately."""
    raw = (payload or {}).get("patch") or (payload or {}).get("content") or {}
    if isinstance(raw, str):
        try:
            patch = json.loads(raw)
        except Exception as e:
            raise HTTPException(400, f"Invalid JSON patch: {e}")
    else:
        patch = raw or {}

    add = patch.get("add_edges") or []
    remove = patch.get("remove_edges") or []
    added = 0
    removed = 0
    for edge in add:
        src = edge.get("from_table")
        col = edge.get("from_col")
        ref = edge.get("to_table")
        ref_col = edge.get("to_col") or "id"
        if not (src and col and ref):
            continue
        await kb_entities.update_one(
            {"project_id": project_id, "name": src, "type": "TABLE"},
            {"$addToSet": {"fks": {"column": col, "ref_table": ref, "ref_column": ref_col}}},
        )
        added += 1
    for edge in remove:
        src = edge.get("from_table")
        col = edge.get("from_col")
        ref = edge.get("to_table")
        if not (src and col and ref):
            continue
        await kb_entities.update_one(
            {"project_id": project_id, "name": src, "type": "TABLE"},
            {"$pull": {"fks": {"column": col, "ref_table": ref}}},
        )
        removed += 1

    await audit_log.insert_one({
        "action": "datamodel.er.apply",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"added": added, "removed": removed},
    })
    return {"ok": True, "added": added, "removed": removed}


@router.post("/jobs/start/oltp")
async def start_oltp_job(payload: dict):
    """Start OLTP generation as a background job. Returns {job_id} immediately so the
    frontend can poll without being killed by K8s 60s ingress timeout.

    iter-13.44 — accepts optional `generation` overrides (per-request layer of
    the deterministic/LLM flag system). Example:
        { "project_id": "...", "generation": {"oltp": "llm"} }
    """
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Discovery", "DataModel")
    # iter-13.81.2 — first run → datamodel.generate bucket; re-run (artifact
    # already exists) → datamodel.regenerate. Lets the Console's per-bucket
    # model pin actually take effect for Factory.ai.
    _existing = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 1})
    set_current_agent_key("datamodel.regenerate" if _existing else "datamodel.generate")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    gen_overrides = payload.get("generation") or {}
    jid = _new_job(project_id, "oltp")
    asyncio.create_task(_run_oltp_job(jid, project_id, model, gen_overrides))
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/olap")
async def start_olap_job(payload: dict):
    """Start OLAP generation as a background job. Returns {job_id} immediately.

    iter-13.44 — accepts optional `generation` overrides (see start_oltp_job).
    """
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Discovery", "DataModel")
    _existing = await data_models.find_one({"project_id": project_id, "type": "olap_ddl"}, {"_id": 1})
    set_current_agent_key("datamodel.regenerate" if _existing else "datamodel.generate")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    gen_overrides = payload.get("generation") or {}
    jid = _new_job(project_id, "olap")
    asyncio.create_task(_run_olap_job(jid, project_id, model, gen_overrides))
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/scripts")
async def start_scripts_job(payload: dict):
    """Start migration-scripts generation (deterministic by default; LLM-mode
    runs 3 sequential calls).

    iter-13.44 — accepts optional `generation` overrides (see start_oltp_job).
    """
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Discovery", "DataModel")
    _existing = await data_models.find_one(
        {"project_id": project_id, "type": {"$in": ["migration_oltp_to_oltp", "migration_oltp_to_olap", "migration_full"]}},
        {"_id": 0, "id": 1},
    )
    set_current_agent_key("datamodel.regenerate" if _existing else "datamodel.generate")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    gen_overrides = payload.get("generation") or {}
    jid = _new_job(project_id, "scripts")
    asyncio.create_task(_run_scripts_job(jid, project_id, model, gen_overrides))
    return {"job_id": jid, "status": "queued"}


# ─────────────────────────────────────────────────────────────────────────────
# iter-13.44 — per-project generation settings (deterministic / LLM toggles)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/{project_id}/generation-settings")
async def get_generation_settings(project_id: str):
    """Return the resolved generation modes for this project (env + project
    layers; per-request layer is empty here)."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    modes = dm_config.resolve_modes(project=proj)
    saved = ((proj.get("settings") or {}).get("datamodel_generation") or {})
    return {
        "project_id": project_id,
        "resolved": modes,
        "saved": saved,
        "describe": dm_config.describe_mode(modes),
    }


@router.put("/{project_id}/generation-settings")
async def update_generation_settings(project_id: str, payload: dict):
    """Save per-project flags. Accepts any subset of:
        {"oltp": "deterministic"|"llm",
         "olap": "deterministic"|"llm",
         "migration": "deterministic"|"llm",
         "enum_polish": bool,
         "comment_polish": bool}
    """
    valid_modes = {"deterministic", "llm"}
    cleaned: dict = {}
    for k in ("oltp", "olap", "migration"):
        v = (payload.get(k) or "").strip().lower() if isinstance(payload.get(k), str) else None
        if v in valid_modes:
            cleaned[k] = v
    for k in ("enum_polish", "comment_polish"):
        if k in payload:
            cleaned[k] = bool(payload[k])
    if not cleaned:
        raise HTTPException(400, "no valid generation-settings keys provided")

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    settings = dict(proj.get("settings") or {})
    dm_cur = dict(settings.get("datamodel_generation") or {})
    dm_cur.update(cleaned)
    settings["datamodel_generation"] = dm_cur

    await projects.update_one(
        {"id": project_id},
        {"$set": {"settings": settings,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    await audit_log.insert_one({
        "action": "datamodel.generation_settings.update",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"applied": cleaned},
    })
    proj2 = await projects.find_one({"id": project_id}, {"_id": 0})
    modes = dm_config.resolve_modes(project=proj2)
    return {"ok": True, "resolved": modes, "saved": dm_cur}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Poll a job's status. Frontend polls every ~2s. Returns 404 if unknown."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (may have expired or backend restarted)")
    return {
        "id": job["id"],
        "status": job["status"],
        "step": job.get("step", ""),
        "pct": job.get("pct", 0),
        "error": job.get("error"),
        "result": job.get("result", {}),
        "kind": job.get("kind"),
    }


# -----------------------------------------------------------
# OLTP DDL — SSE
# -----------------------------------------------------------
@router.post("/generate/oltp")
async def generate_oltp(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY

    discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    srs_functional = (discovery_ctx.get("outputs", {}).get("srs_sections", {}) or {}).get("functional_requirements", "")
    domain_map = discovery_ctx.get("outputs", {}).get("domain_map", {})
    data_hints = discovery_ctx.get("outputs", {}).get("data_model_hints", {})

    # Truncate for prompt
    srs_functional = (srs_functional or "")[:8000]
    domain_map_str = json.dumps(
        {k: {"tables": (v or {}).get("tables", [])[:15]} for k, v in (domain_map or {}).items()}
    )[:4000]

    async def event_gen():
        # RAG context
        try:
            rag_chunks = await qdrant_search(project_id, "database tables relationships foreign keys constraints", top_k=20)
        except Exception:
            rag_chunks = []
        rag_context = "\n\n---\n\n".join(rag_chunks)[:12000] if rag_chunks else (discovery_ctx.get("toon_summary") or "")[:12000]

        n_tables = data_hints.get("domains") and sum(len(d.get("tables", [])) for d in (data_hints.get("domains") or {}).values())
        if not n_tables:
            n_tables = await kb_entities.count_documents({"project_id": project_id, "type": "TABLE"})

        yield f"data: {json.dumps({'type': 'start', 'message': f'Analysing {n_tables} legacy tables…'})}\n\n"
        yield f"data: {json.dumps({'type': 'progress', 'step': 'Building prompt', 'pct': 10})}\n\n"

        template = await _get_prompt(project_id, "datamodel.oltp")
        if not template:
            yield f"data: {json.dumps({'type': 'error', 'message': 'datamodel.oltp prompt missing in seed.'})}\n\n"
            return

        system_prompt = _safe_format(
            template,
            project_name=proj.get("name", ""),
            source_tech=proj.get("source_tech", ""),
            target_tech=proj.get("target_tech", ""),
            rag_context=rag_context,
            domain_map=domain_map_str,
            srs_functional=srs_functional,
        )
        system_prompt = await _with_completeness_contract(project_id, system_prompt)

        yield f"data: {json.dumps({'type': 'progress', 'step': 'Calling LLM (this may take 60-120s)…', 'pct': 40})}\n\n"

        gen_task = asyncio.create_task(
            chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate the complete OLTP DDL now. Be exhaustive."},
                ],
                model=model,
                temperature=0.15,
                max_tokens=16000,
                timeout=300.0,
            )
        )
        async for ev in _wait_for_llm_with_progress(gen_task, "Generating OLTP DDL", 40, 88):
            yield ev

        try:
            result = await gen_task
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'LLM call failed: {e}'})}\n\n"
            return

        content = await _strip_md_fence(result.get("content", ""))
        if not content.strip():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Empty DDL returned from LLM.'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'progress', 'step': 'Saving artifact…', 'pct': 90})}\n\n"

        # Count tables / FKs in the DDL roughly
        tables_n = content.upper().count("CREATE TABLE")
        fks_n = content.upper().count("REFERENCES ")

        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "srs_sections_used": ["functional_requirements"],
            "rag_chunks": len(rag_chunks),
            "model": model,
            "prompt_key": "datamodel.oltp",
        }
        art = await _save_artifact(project_id, "oltp_ddl", content, model, tracability)

        await audit_log.insert_one({
            "action": "datamodel.generate.oltp",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "tables": tables_n, "fks": fks_n, "model": model},
        })

        yield f"data: {json.dumps({'type': 'complete', 'artifact_id': art['id'], 'tables': tables_n, 'fks': fks_n, 'version': art['version']})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# -----------------------------------------------------------
# OLAP DDL — SSE
# -----------------------------------------------------------
@router.post("/generate/olap")
async def generate_olap(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY

    discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    oltp_art = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
    if not oltp_art:
        raise HTTPException(400, "Generate OLTP DDL first.")

    bus_art = await data_models.find_one({"project_id": project_id, "type": "bus_matrix"}, {"_id": 0})
    srs_functional = (discovery_ctx.get("outputs", {}).get("srs_sections", {}) or {}).get("functional_requirements", "")[:6000]

    async def event_gen():
        try:
            _ = await qdrant_search(project_id, "reporting analytics aggregation metrics dashboard", top_k=10)
        except Exception:
            pass
        yield f"data: {json.dumps({'type': 'start', 'message': 'Designing star schema…'})}\n\n"
        yield f"data: {json.dumps({'type': 'progress', 'step': 'Calling LLM…', 'pct': 40})}\n\n"

        template = await _get_prompt(project_id, "datamodel.olap")
        if not template:
            yield f"data: {json.dumps({'type': 'error', 'message': 'datamodel.olap prompt missing.'})}\n\n"
            return

        system_prompt = _safe_format(
            template,
            project_name=proj.get("name", ""),
            oltp_ddl=oltp_art.get("content", "")[:14000],
            srs_functional=srs_functional,
            bus_matrix=(bus_art or {}).get("content", "")[:4000],
        )
        system_prompt = await _with_completeness_contract(project_id, system_prompt)

        gen_task = asyncio.create_task(
            chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Generate the complete OLAP star-schema DDL now."},
                ],
                model=model,
                temperature=0.15,
                max_tokens=14000,
                timeout=300.0,
            )
        )
        async for ev in _wait_for_llm_with_progress(gen_task, "Generating OLAP star schema", 40, 88):
            yield ev

        try:
            result = await gen_task
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'LLM failed: {e}'})}\n\n"
            return

        content = await _strip_md_fence(result.get("content", ""))
        if not content.strip():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Empty DDL returned.'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'progress', 'step': 'Saving artifact…', 'pct': 90})}\n\n"

        dim_n = content.upper().count("CREATE TABLE DIM_") + content.upper().count("CREATE TABLE \"DIM_")
        fact_n = content.upper().count("CREATE TABLE FACT_") + content.upper().count("CREATE TABLE \"FACT_")

        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "oltp_version": oltp_art.get("version"),
            "bus_matrix_version": (bus_art or {}).get("version"),
            "model": model,
            "prompt_key": "datamodel.olap",
        }
        art = await _save_artifact(project_id, "olap_ddl", content, model, tracability)
        await audit_log.insert_one({
            "action": "datamodel.generate.olap",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "dims": dim_n, "facts": fact_n, "model": model},
        })

        yield f"data: {json.dumps({'type': 'complete', 'artifact_id': art['id'], 'dims': dim_n, 'facts': fact_n, 'version': art['version']})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# -----------------------------------------------------------
# Bus Matrix — JSON
# -----------------------------------------------------------
@router.post("/generate/bus-matrix")
async def generate_bus_matrix(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    force_llm = bool(payload.get("force_llm"))

    discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    oltp_art = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
    if not oltp_art:
        raise HTTPException(400, "Generate OLTP DDL first.")

    # iter-14.57 — Prefer the deterministic deriver whenever the project has
    # real TABLE entities to derive from. The previous LLM-first flow was
    # burning tokens on projects with 2000+ tables (only the first ~50 fit
    # into the 14 000-char prompt slice) AND hallucinating badly on the
    # phrase "Bus Matrix" — small local models routinely produced
    # `fact_bus_passenger_count` / `dim_bus_route` because "bus" is a
    # far more common training-set token than the Kimball Enterprise Bus
    # Matrix. Deterministic path is safer, cheaper, and grounded in the
    # actual columns / FKs. Operators can still force the LLM path by
    # POSTing `{"force_llm": true}` (used from the Console).
    ents = await kb_entities.find(
        {"project_id": project_id, "type": "TABLE"}, {"_id": 0}
    ).to_list(3000)

    if ents and not force_llm:
        parsed = _derive_bm(ents)
        # Enrich traceability so operators know this came from the
        # deterministic deriver, not the LLM.
        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "oltp_version": oltp_art.get("version"),
            "model": "deterministic",
            "prompt_key": "datamodel.bus_matrix.deriver",
            "source": "kb_entities",
            "table_count": len(ents),
        }
        art = await _save_artifact(project_id, "bus_matrix", json.dumps(parsed, indent=2), "deterministic", tracability)
        await audit_log.insert_one({
            "action": "datamodel.generate.bus_matrix",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "version": art["version"],
                "facts": len(parsed.get("facts", [])),
                "dims": len(parsed.get("dimensions", [])),
                "source": "deterministic",
                "table_count": len(ents),
            },
        })
        return {"ok": True, "artifact_id": art["id"], "version": art["version"], "matrix": parsed, "source": "deterministic"}

    # ── LLM fallback path (only when no TABLE entities OR force_llm=True) ──
    srs_use_cases = (discovery_ctx.get("outputs", {}).get("srs_sections", {}) or {}).get("use_cases", "")[:6000]

    template = await _get_prompt(project_id, "datamodel.bus_matrix")
    if not template:
        raise HTTPException(500, "datamodel.bus_matrix prompt missing.")

    # iter-14.57 — Sample real table names from the OLTP DDL and prepend a
    # hard-grounded whitelist so the LLM has an anchor for `source_tables`.
    # Extract `CREATE TABLE "xyz"` OR `CREATE TABLE xyz` occurrences.
    ddl_text = oltp_art.get("content", "") or ""
    ct_re = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_][A-Za-z0-9_]*)"?', re.IGNORECASE)
    real_tables = [m.group(1) for m in ct_re.finditer(ddl_text)]
    # De-dup preserving order; keep first ~120 for grounding.
    seen = set()
    real_tables_uniq = []
    for t in real_tables:
        if t.lower() not in seen:
            seen.add(t.lower())
            real_tables_uniq.append(t)
        if len(real_tables_uniq) >= 120:
            break
    whitelist_block = (
        "AUTHORITATIVE TABLE WHITELIST (facts.source_tables / dimensions.source_tables "
        "MUST be drawn EXCLUSIVELY from this list — if you invent a name not present "
        "here, the artifact is rejected):\n  " + ", ".join(real_tables_uniq)
        if real_tables_uniq else ""
    )
    grounding_preamble = (
        "IMPORTANT — 'Bus Matrix' here means the KIMBALL ENTERPRISE BUS MATRIX from "
        "dimensional-modelling / data-warehouse theory (facts × conformed dimensions "
        "for an OLAP star schema). It has NOTHING to do with public transport, "
        "vehicles, routes, passengers, or the word 'bus' in the literal sense. If the "
        "OLTP schema below is for a healthcare / education / regulatory / finance / "
        "e-commerce / logistics domain, the facts and dimensions MUST reflect THAT "
        "domain — never invent bus / transportation entities that are absent from the "
        "OLTP schema.\n\n"
    )

    system_prompt = _safe_format(
        template,
        project_name=proj.get("name", ""),
        oltp_ddl=ddl_text[:14000],
        srs_use_cases=srs_use_cases,
    )
    system_prompt = grounding_preamble + system_prompt + "\n\n" + whitelist_block
    system_prompt = await _with_completeness_contract(project_id, system_prompt)

    try:
        result = await chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Return the bus matrix JSON now."},
            ],
            model=model,
            temperature=0.1,
            max_tokens=6000,
            timeout=180.0,
        )
    except Exception as e:
        raise HTTPException(502, f"LLM failed: {e}")

    raw = await _strip_md_fence(result.get("content", ""))
    try:
        parsed = json.loads(raw)
    except Exception:
        # Attempt to extract JSON between first { and last }
        try:
            start = raw.index("{")
            end = raw.rindex("}")
            parsed = json.loads(raw[start : end + 1])
        except Exception as e:
            raise HTTPException(502, f"LLM returned non-JSON bus matrix: {e}")

    # iter-14.57 — Validate source_tables against the OLTP DDL. If the LLM
    # invented fact/dim tables that don't exist (the exact ceots
    # "fact_bus_passenger_count" hallucination), we FALL BACK to the
    # deterministic deriver rather than persist garbage.
    real_lower = {t.lower() for t in real_tables_uniq}
    invented: list[str] = []
    if real_lower:
        for grp in ("facts", "dimensions"):
            for item in (parsed.get(grp) or []):
                for st in (item.get("source_tables") or []):
                    if isinstance(st, str) and st.lower() not in real_lower and st.lower() != "":
                        # dim_date has empty source_tables which is fine.
                        invented.append(f"{grp}.{item.get('name','?')}→{st}")
    if invented and ents:
        logger.warning(
            "bus_matrix LLM output referenced tables missing from OLTP DDL — "
            "falling back to deterministic deriver. invented=%s",
            invented[:8],
        )
        parsed = _derive_bm(ents)
        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "oltp_version": oltp_art.get("version"),
            "model": f"{model}→deterministic-fallback",
            "prompt_key": "datamodel.bus_matrix",
            "source": "deterministic_after_llm_hallucination",
            "invented_tables": invented[:20],
        }
    else:
        tracability = {
            "discovery_version": discovery_ctx.get("version"),
            "oltp_version": oltp_art.get("version"),
            "model": model,
            "prompt_key": "datamodel.bus_matrix",
            "source": "llm",
        }
    art = await _save_artifact(project_id, "bus_matrix", json.dumps(parsed, indent=2), tracability["model"], tracability)
    await audit_log.insert_one({
        "action": "datamodel.generate.bus_matrix",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {
            "version": art["version"],
            "facts": len(parsed.get("facts", [])),
            "dims": len(parsed.get("dimensions", [])),
            "source": tracability.get("source"),
        },
    })
    return {"ok": True, "artifact_id": art["id"], "version": art["version"], "matrix": parsed, "source": tracability.get("source")}


# -----------------------------------------------------------
# Migration scripts — SSE (3 sequential scripts)
# -----------------------------------------------------------
_SCRIPT_PROMPTS = {
    "migrate_old_to_oltp": (
        "You are a senior Python data migration engineer.\n\n"
        "Write a single Python 3 script `migrate_old_to_oltp.py` that migrates data from a legacy "
        "{source_tech} source database to the new PostgreSQL OLTP schema.\n\n"
        "LEGACY TABLES (schema summary):\n{kb_tables}\n\n"
        "TARGET OLTP DDL:\n{oltp_ddl}\n\n"
        "Requirements:\n"
        "- Use psycopg2-binary for PostgreSQL and PyMySQL (or appropriate driver) for the source.\n"
        "- Read source/target DSNs from env: SOURCE_DB_HOST, SOURCE_DB_USER, SOURCE_DB_PASSWORD, SOURCE_DB_NAME, "
        "TARGET_DB_HOST, TARGET_DB_USER, TARGET_DB_PASSWORD, TARGET_DB_NAME, TARGET_DB_PORT (default 5432).\n"
        "- For each legacy table SELECT * in batches of 1000 and INSERT into the new table.\n"
        "- Type mappings: VARCHAR→TEXT, TINYINT(1)→BOOLEAN, DATETIME→TIMESTAMPTZ, INT→INTEGER.\n"
        "- Generate UUIDs for new primary keys; keep a legacy_id column when present to support FK rewiring.\n"
        "- Two passes: pass 1 inserts rows + records id mapping; pass 2 rewires FKs using the id map.\n"
        "- Progress logging (every 1000 rows). Transactional commit per table; rollback on error.\n"
        "- Idempotent: skip rows already present (check by legacy_id).\n\n"
        "Return ONLY the Python source. No markdown."
    ),
    "migrate_oltp_to_olap": (
        "You are a senior Python ETL engineer.\n\n"
        "Write `migrate_oltp_to_olap.py` that extracts from the OLTP PostgreSQL schema and loads the OLAP "
        "(star-schema) PostgreSQL warehouse.\n\n"
        "OLTP DDL:\n{oltp_ddl}\n\n"
        "OLAP DDL:\n{olap_ddl}\n\n"
        "Requirements:\n"
        "- Use psycopg2-binary. Read OLTP_* and OLAP_* DSNs from env.\n"
        "- Populate dim_date for the past 10 years and next 2 years.\n"
        "- Populate every dim_ table first (SCD Type 1: UPSERT by natural key).\n"
        "- Populate every fact_ table after dims, joining to dims to resolve surrogate keys.\n"
        "- Use ON CONFLICT DO UPDATE for idempotency.\n"
        "- Log row counts per dim and per fact at the end.\n\n"
        "Return ONLY the Python source. No markdown."
    ),
    "test_migration": (
        "You are a senior QA engineer.\n\n"
        "Write `test_migration.py` — a pytest file that validates the migration.\n\n"
        "OLTP DDL:\n{oltp_ddl}\n\nOLAP DDL:\n{olap_ddl}\n\n"
        "Tests:\n"
        "- test_row_count_parity: per-table source vs OLTP row counts (allow ≤1% drift).\n"
        "- test_no_unexpected_nulls: NOT NULL columns truly non-null in OLTP.\n"
        "- test_fk_integrity: every FK in OLTP resolves to an existing PK row.\n"
        "- test_sample_data: pick 5 key tables, fetch first 10 rows, assert non-empty and shape.\n"
        "- test_olap_facts_populated: every fact_ table has rows > 0.\n"
        "- test_olap_dims_populated: every dim_ table has rows > 0.\n\n"
        "Use psycopg2 + env DSNs (same as the migration scripts). Return ONLY the Python source. No markdown."
    ),
}


@router.post("/generate/migration-scripts")
async def generate_migration_scripts(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY

    discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    oltp_art = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
    olap_art = await data_models.find_one({"project_id": project_id, "type": "olap_ddl"}, {"_id": 0})

    if not oltp_art or not olap_art:
        raise HTTPException(400, "Generate both OLTP and OLAP DDL first.")

    # KB tables for script 1
    tables = await kb_entities.find({"project_id": project_id, "type": "TABLE"}, {"_id": 0}).to_list(500)
    kb_tables = "\n".join(
        f"{t.get('name')}({', '.join((c.get('name') for c in (t.get('columns') or [])[:8]))})"
        for t in tables[:200]
    )[:8000]

    oltp_ddl = oltp_art.get("content", "")[:12000]
    olap_ddl = olap_art.get("content", "")[:12000]

    async def event_gen():
        yield f"data: {json.dumps({'type': 'start', 'message': 'Generating 3 migration scripts…'})}\n\n"
        for script_type in ("migrate_old_to_oltp", "migrate_oltp_to_olap", "test_migration"):
            yield f"data: {json.dumps({'type': 'script_start', 'script': f'{script_type}.py'})}\n\n"

            prompt_template = _SCRIPT_PROMPTS[script_type]
            system_prompt = prompt_template.format(
                source_tech=proj.get("source_tech", ""),
                kb_tables=kb_tables,
                oltp_ddl=oltp_ddl,
                olap_ddl=olap_ddl,
            )

            gen_task = asyncio.create_task(
                chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Write {script_type}.py now."},
                    ],
                    model=model,
                    temperature=0.15,
                    max_tokens=8000,
                    timeout=240.0,
                )
            )
            async for ev in _wait_for_llm_with_progress(gen_task, f"Writing {script_type}.py", 20, 88):
                yield ev
            try:
                result = await gen_task
            except Exception as e:
                yield f"data: {json.dumps({'type': 'script_error', 'script': script_type, 'message': str(e)})}\n\n"
                continue
            content = await _strip_md_fence(result.get("content", ""))
            tracability = {
                "discovery_version": discovery_ctx.get("version"),
                "oltp_version": oltp_art.get("version"),
                "olap_version": olap_art.get("version"),
                "model": model,
                "prompt_key": f"_inline.{script_type}",
            }
            art = await _save_artifact(project_id, script_type, content, model, tracability)
            yield f"data: {json.dumps({'type': 'script_complete', 'script': f'{script_type}.py', 'artifact_id': art['id'], 'version': art['version']})}\n\n"

        yield f"data: {json.dumps({'type': 'complete', 'message': 'All scripts generated.'})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# -----------------------------------------------------------
# Entity graph — fast (re-uses Discovery er_model if present)
# -----------------------------------------------------------
@router.post("/generate/entity-graph")
async def generate_entity_graph(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "Discovery", "DataModel")
    # Always compute live (the frozen Discovery cache omits full column/type detail).
    from routes.srs import _gen_entity_model
    r = await _gen_entity_model(project_id)
    return json.loads(r["content"])


# -----------------------------------------------------------
# RAG chat
# -----------------------------------------------------------
@router.post("/chat")
async def data_model_chat(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    model_type = (payload.get("model_type") or "oltp").lower()  # oltp | olap
    conversation_id = payload.get("conversation_id")
    session_id = (payload.get("session_id") or "").strip()  # iter-13.100

    discovery_ctx = await require_stage_context(project_id, "Discovery", "DataModel")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})

    art_type = "olap_ddl" if model_type == "olap" else ("bus_matrix" if model_type == "bus" else "oltp_ddl")
    current_art = await data_models.find_one({"project_id": project_id, "type": art_type}, {"_id": 0})
    current_ddl = (current_art or {}).get("content", "")[:10000]

    try:
        rag_chunks = await qdrant_search(project_id, message, top_k=8)
    except Exception:
        rag_chunks = []
    rag_context = "\n\n---\n\n".join(rag_chunks)[:6000] if rag_chunks else (discovery_ctx.get("toon_summary") or "")[:6000]

    template = await _get_prompt(project_id, "datamodel.chat")
    system_prompt = _safe_format(
        template,
        project_name=proj.get("name", ""),
        model_type=model_type.upper(),
        current_ddl=current_ddl,
        rag_context=rag_context,
        message=message,
    )

    # Conversation persistence (shared `messages` collection)
    if not conversation_id:
        conv_doc = {
            "id": __import__("uuid").uuid4().hex,
            "project_id": project_id,
            "stage": "DataModel",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await conversations.insert_one(conv_doc)
        conversation_id = conv_doc["id"]

    user_msg = ChatMessage(conversation_id=conversation_id, project_id=project_id, role="user", content=message)
    await messages_col.insert_one(user_msg.model_dump())

    try:
        # iter-13.100 — route through rolling-memory session when supplied.
        if session_id:
            result = await fabric_call_with_session(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message},
                ],
                session_id=session_id,
                agent_key="datamodel.chat",
                project_id=project_id,
                model=model,
                temperature=0.2, max_tokens=4000, timeout=120.0,
            )
        else:
            result = await chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message},
                ],
                model=model,
                temperature=0.2,
                max_tokens=4000,
                timeout=120.0,
            )
    except Exception as e:
        raise HTTPException(502, f"LLM failed: {e}")

    content = result.get("content", "") or ""
    # Extract suggested changes — supports [DDL_CHANGE]/[BUS_CHANGE]/[ER_CHANGE] markers.
    suggested_ddl = None
    change_kind = None
    for marker in ("DDL_CHANGE", "BUS_CHANGE", "ER_CHANGE"):
        if f"[{marker}]" in content and f"[/{marker}]" in content:
            try:
                suggested_ddl = content.split(f"[{marker}]", 1)[1].split(f"[/{marker}]", 1)[0].strip()
                change_kind = marker.replace("_CHANGE", "").lower()  # 'ddl' | 'bus' | 'er'
                break
            except Exception:
                suggested_ddl = None

    assistant_msg = ChatMessage(
        conversation_id=conversation_id,
        project_id=project_id,
        role="assistant",
        content=content,
        model=model,
        tokens=result.get("usage", {}).get("total_tokens", 0),
    )
    await messages_col.insert_one(assistant_msg.model_dump())

    return {
        "conversation_id": conversation_id,
        "message": assistant_msg.model_dump(),
        "suggested_ddl": suggested_ddl,
        "change_kind": change_kind,  # 'ddl' | 'bus' | 'er' | None
        "artifact_id": (current_art or {}).get("id"),
        "model_type": model_type,
    }


# -----------------------------------------------------------
# Artifact CRUD
# -----------------------------------------------------------
@router.get("/{project_id}/artifacts")
async def list_artifacts(project_id: str):
    docs = await data_models.find(
        {"project_id": project_id},
        {"_id": 0, "content": 0},
    ).sort("updated_at", -1).to_list(200)
    return {"artifacts": docs}


@router.get("/{project_id}/artifact/{artifact_id}")
async def get_artifact(project_id: str, artifact_id: str):
    doc = await data_models.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Artifact not found")
    return doc


@router.put("/{project_id}/artifact/{artifact_id}")
async def update_artifact(project_id: str, artifact_id: str, payload: dict):
    doc = await data_models.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Artifact not found")
    if doc.get("frozen"):
        raise HTTPException(400, "Artifact is frozen")
    new_content = payload.get("content", "")
    now = datetime.now(timezone.utc).isoformat()
    await data_models.update_one(
        {"project_id": project_id, "id": artifact_id},
        {"$set": {
            "content": new_content,
            "version": doc.get("version", 1) + 1,
            "updated_at": now,
        }},
    )
    await audit_log.insert_one({
        "action": "datamodel.artifact.update",
        "project_id": project_id,
        "at": now,
        "details": {"artifact_id": artifact_id, "type": doc.get("type")},
    })
    return {"ok": True, "version": doc.get("version", 1) + 1}


@router.post("/{project_id}/artifact/{artifact_id}/freeze")
async def freeze_artifact(project_id: str, artifact_id: str):
    doc = await data_models.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Artifact not found")
    now = datetime.now(timezone.utc).isoformat()
    await data_models.update_one(
        {"project_id": project_id, "id": artifact_id},
        {"$set": {"frozen": True, "frozen_at": now, "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "datamodel.artifact.freeze",
        "project_id": project_id,
        "at": now,
        "details": {"artifact_id": artifact_id, "type": doc.get("type")},
    })

    # If oltp_ddl + olap_ddl both frozen → freeze the whole DataModel stage
    oltp = await data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
    olap = await data_models.find_one({"project_id": project_id, "type": "olap_ddl"}, {"_id": 0})
    bus = await data_models.find_one({"project_id": project_id, "type": "bus_matrix"}, {"_id": 0})

    if oltp and oltp.get("frozen") and olap and olap.get("frozen"):
        discovery_ctx = await stage_context_col.find_one({"project_id": project_id, "stage": "Discovery"}, {"_id": 0})
        scripts = {}
        for s in ("migrate_old_to_oltp", "migrate_oltp_to_olap", "test_migration"):
            a = await data_models.find_one({"project_id": project_id, "type": s}, {"_id": 0})
            if a:
                scripts[s] = a["id"]
        outputs = {
            "oltp_artifact_id": oltp["id"],
            "olap_artifact_id": olap["id"],
            "bus_matrix_artifact_id": (bus or {}).get("id"),
            "script_artifact_ids": scripts,
            "oltp_ddl": oltp.get("content", ""),
            "olap_ddl": olap.get("content", ""),
            "oltp_table_count": oltp.get("content", "").upper().count("CREATE TABLE"),
            "olap_fact_count": (olap.get("content", "")).upper().count("CREATE TABLE FACT_"),
            "olap_dim_count": (olap.get("content", "")).upper().count("CREATE TABLE DIM_"),
            "domain_map": (discovery_ctx or {}).get("outputs", {}).get("domain_map", {}),
            "service_boundaries": (discovery_ctx or {}).get("outputs", {}).get("suggested_service_boundaries", []),
        }
        sources = {
            "discovery_version": (discovery_ctx or {}).get("version", 1),
            "model_used": doc.get("generated_by", ""),
            "prompts_used": ["datamodel.oltp", "datamodel.olap", "datamodel.bus_matrix", "datamodel.chat"],
        }
        await save_stage_context(
            project_id=project_id,
            stage="DataModel",
            outputs=outputs,
            sources=sources,
            toon_summary=(discovery_ctx or {}).get("toon_summary", "")[:4000],
            frozen_by="system",
        )
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "stage_status.DataModel": "frozen",
                "stage_status.Architecture": "available",
                "updated_at": now,
            }},
        )

    return {"ok": True, "frozen": True}


@router.get("/{project_id}/artifact/{artifact_id}/download")
async def download_artifact(project_id: str, artifact_id: str):
    doc = await data_models.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Artifact not found")
    type_to_filename = {
        "oltp_ddl": ("oltp_schema.sql", "application/sql"),
        "olap_ddl": ("olap_schema.sql", "application/sql"),
        "bus_matrix": ("bus_matrix.json", "application/json"),
        "migrate_old_to_oltp": ("migrate_old_to_oltp.py", "text/x-python"),
        "migrate_oltp_to_olap": ("migrate_oltp_to_olap.py", "text/x-python"),
        "test_migration": ("test_migration.py", "text/x-python"),
    }
    filename, mime = type_to_filename.get(doc.get("type"), (f"{doc.get('type','artifact')}.txt", "text/plain"))
    return StreamingResponse(
        io.BytesIO((doc.get("content", "") or "").encode("utf-8")),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------------------------------------------
# Reset endpoints
# -----------------------------------------------------------
@router.post("/{project_id}/reset")
async def reset_stage2(project_id: str):
    """Wipe Stage 2 data only (artifacts, bus_matrix, olap, scripts). KB / SRS / Discovery context preserved."""
    deleted = {}
    for col, name in (
        (data_models, "data_models"),
        (bus_matrix_col, "bus_matrix"),
        (olap_models, "olap_models"),
        (migration_artifacts, "migration_artifacts"),
    ):
        r = await col.delete_many({"project_id": project_id})
        deleted[name] = r.deleted_count
    # Reset DataModel stage status to available (since SRS still frozen)
    now = datetime.now(timezone.utc).isoformat()
    await stage_context_col.delete_one({"project_id": project_id, "stage": "DataModel"})
    await projects.update_one(
        {"id": project_id},
        {"$set": {
            "stage_status.DataModel": "available",
            "stage_status.Architecture": "locked",
            "stage_status.CodeGen": "locked",
            "stage_status.Living": "locked",
            "updated_at": now,
        }},
    )
    await audit_log.insert_one({
        "action": "datamodel.reset",
        "project_id": project_id,
        "at": now,
        "details": deleted,
    })
    return {"ok": True, "deleted": deleted}


# -----------------------------------------------------------
# Factory reset — exposed via this router for convenience, but pathed under /projects
# -----------------------------------------------------------
factory_router = APIRouter(prefix="/projects", tags=["projects"])


@factory_router.post("/{project_id}/factory-reset")
async def factory_reset(project_id: str):
    """Wipe ALL project data: KB, SRS, chat, data model, stage context, vectors. Project metadata kept; stages re-locked."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    # iter-13.35 — kill ANY in-flight background work for this project BEFORE
    # wiping its collections. Without this, the SRS background task (held in
    # `routes.srs._SRS_JOBS`) keeps writing to Mongo after the reset, and the
    # frontend's poll-on-mount (`/srs/{pid}/generate/status`) re-attaches to
    # the still-running job → the Pause / Start / Cancel control bar comes
    # back even though the user just clicked "Refresh App".
    killed_jobs: list[str] = []
    try:
        from routes.srs import _SRS_JOBS as _SRS_JOBS_MAP
        job = _SRS_JOBS_MAP.get(project_id)
        if job:
            # Set the control flags first so the loop exits cleanly between
            # sections, then cancel the task as a hard floor.
            try:
                job.get("control", {})["cancelled"] = True
                job.get("control", {})["paused"] = False
            except Exception:  # noqa: BLE001
                pass
            try:
                t = job.get("task")
                if t and not t.done():
                    t.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                broadcast = job.get("broadcast")
                if broadcast:
                    # Tell any listening SSE clients we're done so they tear
                    # down their UI rather than spinning forever.
                    await broadcast({"type": "run_aborted", "reason": "factory_reset",
                                     "message": "Project was factory-reset; SRS run was cancelled."})
                    await broadcast({"type": "__end__"})
            except Exception:  # noqa: BLE001
                pass
            _SRS_JOBS_MAP.pop(project_id, None)
            killed_jobs.append("srs")
    except Exception as e:  # noqa: BLE001
        killed_jobs.append(f"srs:err:{e}")

    deleted = {}
    for col, name in (
        (kb_files, "kb_files"),
        (kb_chunks, "kb_chunks"),
        (kb_entities, "kb_entities"),
        (kb_toon, "kb_toon"),
        (conversations, "conversations"),
        (messages_col, "messages"),
        (srs_documents, "srs_documents"),
        (data_models, "data_models"),
        (bus_matrix_col, "bus_matrix"),
        (olap_models, "olap_models"),
        (migration_artifacts, "migration_artifacts"),
        (stage_context_col, "stage_context"),
        # iter-13.34 — additional project-scoped collections that previously
        # leaked across Refresh App, causing the symptoms:
        #   • "Live Data Source" panel kept showing the stale DB/URL hint
        #     (data_sources)
        #   • MiniConsole "Currently using" stuck on the last model from
        #     the previous session (token_usage_log)
        #   • Ghost graph slice fed into SRS/Arch/CodeGen after a wipe
        #     (kb_graph, business_ontologies, legacy_analysis,
        #     ontology_snapshots)
        #   • Stale arch/codegen/living artifacts surviving a "fresh start"
        (data_sources, "data_sources"),
        (token_usage_log, "token_usage_log"),
        (business_ontologies, "business_ontologies"),
        (ontology_snapshots, "ontology_snapshots"),
        (legacy_analysis, "legacy_analysis"),
        (kb_graph_col, "kb_graph"),
        (arch_documents, "arch_documents"),
        (arch_services, "arch_services"),
        (codegen_files, "codegen_files"),
        (codegen_runs, "codegen_runs"),
        (living_artifacts, "living_artifacts"),
        (living_runs, "living_runs"),
        (project_prompts, "project_prompts"),
        (freeze_gates, "freeze_gates"),
    ):
        try:
            r = await col.delete_many({"project_id": project_id})
            deleted[name] = r.deleted_count
        except Exception as e:
            deleted[name] = f"err: {e}"

    # Audit log entries for this project
    try:
        r = await audit_log.delete_many({"project_id": project_id})
        deleted["audit_log"] = r.deleted_count
    except Exception:
        pass

    # Qdrant vectors
    try:
        await delete_project_vectors(project_id)
        deleted["qdrant_vectors"] = "ok"
    except Exception as e:
        deleted["qdrant_vectors"] = f"err: {e}"

    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {
            "stage": "Discovery",
            "stage_status": {
                "Discovery": "active",
                "DataModel": "locked",
                "Architecture": "locked",
                "CodeGen": "locked",
                "Living": "locked",
            },
            "freeze_gates": {},
            "updated_at": now,
        },
         # iter-13.34 — drop any cached tech-detection / stack-suggestion /
         # graphify result on the project doc itself, so the UI doesn't
         # render ghost "detected: Java" / "Live Data Source" hints from
         # the previous build after Refresh App.
         "$unset": {
            "detected_tech": "",
            "target_stack_suggestions": "",
            "target_stack_validation": "",
            "logic_analysis_summary": "",
            "kb_build_state": "",
            # iter-13.35 — also wipe the persisted legacy folder path so the
            # next SRS run won't try to ground itself on a path that no
            # longer matches anything in the KB (which we just deleted).
            "legacy_path": "",
            "legacy_path_scanned_at": "",
        }},
    )
    await audit_log.insert_one({
        "action": "project.factory_reset",
        "project_id": project_id,
        "at": now,
        "details": {**deleted, "killed_jobs": killed_jobs},
    })
    return {"ok": True, "deleted": deleted, "killed_jobs": killed_jobs}
