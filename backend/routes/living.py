"""Stage 5 — Living System.

Generates:
  - Selenium acceptance tests (test.selenium)
  - JMeter performance plans (test.jmeter)
  - Drift reports (drift.detector)
  - SRS diff reports (diff.srs)

All long LLM calls use the in-memory job polling pattern (no SSE),
identical to architecture.py / codegen.py to bypass K8s 60s ingress timeouts.
"""
import asyncio
import io
import json
import logging
import math
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from db import (
    projects,
    srs_documents,
    living_artifacts,
    living_runs,
    arch_documents,
    arch_services,
    project_prompts,
    prompts as prompts_col,
    # iter-13.70 — confidence engine + Accuracy Report storage
    kb_toon,
    kb_entities,
    codegen_files,
    legacy_analysis as legacy_analysis_col,
    data_models as data_models_col,
    living_reports,
)
from llm import fabric_call
from pipeline import require_stage_context
from confidence import (
    score_artifact_multi_model,
    pick_evaluator_models,
    band_of,
)


async def get_prompt_for_project(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p.get("template", "")
    g = await prompts_col.find_one({"key": key}, {"_id": 0})
    return (g or {}).get("template", "")

logger = logging.getLogger("lama.living")

router = APIRouter(prefix="/living", tags=["living"])

# ─── In-memory job registry (same pattern as architecture/codegen) ─────
_JOBS: Dict[str, Dict[str, Any]] = {}


def _new_job(project_id: str, kind: str) -> str:
    jid = uuid.uuid4().hex
    _JOBS[jid] = {
        "id": jid, "kind": kind, "project_id": project_id,
        "status": "queued", "step": "queued", "pct": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None, "error": "", "result": None,
        "cancelled": False,  # iter-14.61 — cancel flag
    }
    return jid


def _job_update(jid: str, **kwargs):
    if jid in _JOBS:
        _JOBS[jid].update(kwargs)


def _job_complete(jid: str, result: Dict[str, Any]):
    _job_update(jid, status="complete", step="Done", pct=100, result=result,
                completed_at=datetime.now(timezone.utc).isoformat())


def _job_error(jid: str, err: str):
    _job_update(jid, status="error", error=err,
                completed_at=datetime.now(timezone.utc).isoformat())


def _job_is_cancelled(jid: str) -> bool:
    """Check if job has been cancelled."""
    job = _JOBS.get(jid)
    return job.get("cancelled", False) if job else True


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Cancel a running job. The job will stop after the current batch completes."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") in ("complete", "error", "cancelled"):
        return {"ok": True, "message": "Job already finished", "status": job.get("status")}
    
    _job_update(job_id, cancelled=True, status="cancelled", step="Cancelled by user")
    logger.info("Job %s cancelled by user", job_id)
    return {"ok": True, "message": "Job cancelled", "status": "cancelled"}


# ─── Helpers to assemble inputs ────────────────────────────────────────
async def _assemble_context(project_id: str) -> Dict[str, str]:
    proj = await projects.find_one({"id": project_id}, {"_id": 0}) or {}
    srs = await srs_documents.find_one({"project_id": project_id}, {"_id": 0}) or {}
    services = await arch_services.find({"project_id": project_id}, {"_id": 0}).to_list(200)
    sections = srs.get("sections", {}) or {}
    routes_text = "\n".join(
        f"{(s.get('name') or '')}: {ep}" for s in services for ep in (s.get("api_endpoints") or [])
    ) or "(no service map yet)"
    use_cases = (sections.get("use_cases") or sections.get("detailed_use_cases") or "").strip() or "(no use cases yet)"
    nfr = (sections.get("non_functional_requirements") or "").strip() or "(no NFR section)"

    # iter-14.48 — Pull legacy KB evidence so Selenium/JMeter prompts
    # can generate one test per real legacy screen / endpoint instead of
    # inventing use-cases. Falls back gracefully when a slice is empty.
    screens_catalogue = "(no legacy screens indexed)"
    legacy_endpoints = "(no legacy endpoints indexed)"
    legacy_actors = "(no roles indexed)"
    legacy_test_evidence = "(no legacy test files indexed)"
    try:
        from db import kb_entities as _kbe, kb_chunks as _kbc

        # Group JSP_FORM entities by file_path → collect FORM_FIELDs per file
        jsp_forms = await _kbe.find(
            {"project_id": project_id, "type": {"$in": ["JSP_FORM", "JSP_MODEL"]}},
            {"_id": 0, "name": 1, "file_path": 1, "attributes": 1},
        ).limit(60).to_list(60)
        form_fields = await _kbe.find(
            {"project_id": project_id, "type": "FORM_FIELD"},
            {"_id": 0, "name": 1, "file_path": 1, "attributes": 1},
        ).limit(400).to_list(400)
        by_file: dict = {}
        for ff in form_fields:
            by_file.setdefault(ff.get("file_path", "?"), []).append(ff)
        screen_lines: list[str] = []
        for jf in jsp_forms[:40]:
            fp = jf.get("file_path", "?")
            name = jf.get("name", fp.rsplit("/", 1)[-1]) or fp
            fields = by_file.get(fp, [])[:12]
            field_txt = ", ".join(
                f"{ff.get('name','?')}"
                + (":req" if str((ff.get('attributes') or {}).get('required','')).lower() in ('1','true','yes') else "")
                for ff in fields
            ) or "(no field metadata)"
            screen_lines.append(f"- {name}  ({fp})\n    fields: {field_txt}")
        if screen_lines:
            screens_catalogue = "\n".join(screen_lines)

        # ROUTE + ACTION entities → endpoint / navigation catalogue
        routes_kb = await _kbe.find(
            {"project_id": project_id, "type": {"$in": ["ROUTE", "ACTION", "FORWARD"]}},
            {"_id": 0, "name": 1, "file_path": 1, "attributes": 1, "type": 1},
        ).limit(160).to_list(160)
        route_lines: list[str] = []
        for r in routes_kb[:120]:
            attr = r.get("attributes") or {}
            verb = attr.get("verb") or attr.get("method") or ""
            path = attr.get("path") or attr.get("url") or attr.get("target") or r.get("name", "")
            route_lines.append(f"- [{r.get('type','')}] {verb} {path}  (from {r.get('file_path','?')})")
        if route_lines:
            legacy_endpoints = "\n".join(route_lines)

        # Actors / roles — helps drive persona-based JMeter thread groups
        # and negative-path Selenium tests.
        role_entities = await _kbe.find(
            {"project_id": project_id, "type": {"$regex": "role", "$options": "i"}},
            {"_id": 0, "name": 1},
        ).limit(20).to_list(20)
        if role_entities:
            legacy_actors = ", ".join(sorted({e.get("name", "") for e in role_entities if e.get("name")}))

        # Any existing @Test / assertEquals / describe(  evidence → so
        # the LLM can preserve legacy assertion semantics.
        try:
            tc = await _kbc.find(
                {"project_id": project_id,
                 "content": {"$regex": r"(@Test\b|assertEquals\(|assertThat\(|describe\(|it\(\s*['\"])", "$options": "i"}},
                {"_id": 0, "file_path": 1, "content": 1},
            ).limit(6).to_list(6)
            if tc:
                blocks = [
                    f"### {c.get('file_path','?')}\n{(c.get('content') or '')[:600]}"
                    for c in tc
                ]
                legacy_test_evidence = "\n\n".join(blocks)[:4000]
        except Exception:  # noqa: BLE001
            pass
    except Exception as _ke:  # noqa: BLE001
        logger.warning("living._assemble_context: KB slice failed %s", _ke)

    return {
        "project_name": proj.get("name", ""),
        "target_tech": proj.get("target_tech", "FastAPI + React + PostgreSQL"),
        "use_cases": use_cases[:8000],
        "routes": routes_text[:6000],
        "endpoints": routes_text[:6000],
        "srs_functional": (sections.get("functional_requirements") or sections.get("specific_requirements") or "")[:10000],
        "nfr_summary": nfr[:4000],
        "base_url": proj.get("deploy_base_url", "http://localhost:8001"),
        # iter-14.48 legacy-KB evidence for Selenium/JMeter generation
        "screens_catalogue": screens_catalogue[:9000],
        "legacy_endpoints": legacy_endpoints[:6000],
        "legacy_actors": legacy_actors[:800],
        "legacy_test_evidence": legacy_test_evidence[:4000],
    }


def _split_files(raw: str) -> List[Dict[str, str]]:
    """Split LLM output into files. Accepts several marker styles the
    models actually emit in the wild (iter-14.54):

      1. ``=== FILE: <path> ===`` (Selenium prompt canonical)
      2. ``### FILE: <path>``  (markdown-heading style JMeter loves)
      3. ``## FILE: <path>`` or ``# FILE: <path>``
      4. ``FILE: <path>`` on its own line

    Any surrounding ```` ``` `` `` fenced code blocks are unwrapped so we
    persist the raw file body, not the Markdown wrapper.
    """
    if not raw:
        return [{"path": "output.txt", "content": ""}]

    # Normalise every accepted marker to `=== FILE: <path> ===` so the
    # single splitter below works uniformly.
    norm = raw
    # order matters — most specific first
    norm = re.sub(r"(?m)^\s*={2,}\s*FILE\s*:\s*(.+?)\s*={2,}\s*$",
                  r"=== FILE: \1 ===", norm)
    norm = re.sub(r"(?m)^\s*#{1,6}\s*FILE\s*:\s*(.+?)\s*$",
                  r"=== FILE: \1 ===", norm)
    norm = re.sub(r"(?m)^\s*FILE\s*:\s*([^\s].+?)\s*$",
                  r"=== FILE: \1 ===", norm)

    out: List[Dict[str, str]] = []
    parts = norm.split("=== FILE:")
    for p in parts[1:]:
        nl = p.find("\n")
        if nl < 0:
            continue
        header = p[:nl].strip()
        body = p[nl + 1:]
        path = header.rstrip("=").strip()
        if not path:
            continue
        # Cut at the next FILE marker
        body = body.split("=== FILE:")[0]
        # Trim a trailing `===` closer if the source used it
        body = body.rstrip()
        if body.endswith("==="):
            body = body[:-3].rstrip()
        # Unwrap a single leading/trailing fenced code block
        m = re.match(r"^\s*```[a-zA-Z0-9_+\-]*\s*\n(.*?)\n\s*```\s*$", body, re.DOTALL)
        if m:
            body = m.group(1)
        out.append({"path": path, "content": body.strip()})
    if not out:
        out.append({"path": "output.txt", "content": raw})
    return out


async def _save_artifact(project_id: str, kind: str, files: List[Dict[str, str]], meta: Dict[str, Any]):
    now = datetime.now(timezone.utc).isoformat()
    existing = await living_artifacts.find_one({"project_id": project_id, "kind": kind}, {"_id": 0})
    if existing:
        await living_artifacts.update_one(
            {"id": existing["id"]},
            {"$set": {
                "files": files, "meta": meta,
                "version": existing.get("version", 1) + 1,
                "updated_at": now, "frozen": False,
            }},
        )
        return existing["id"]
    art_id = uuid.uuid4().hex
    await living_artifacts.insert_one({
        "id": art_id, "project_id": project_id, "kind": kind,
        "files": files, "meta": meta,
        "version": 1, "frozen": False,
        "created_at": now, "updated_at": now,
    })
    return art_id


# ─── iter-14.50 — Detailed test-case matrix helpers ────────────────────
_TC_COLUMNS: list[str] = [
    "TC_ID", "Module", "Screen_or_API", "Priority", "Type",
    "Preconditions", "Steps", "Test_Data", "Expected_Result",
    "SRS_UC_Ref", "Legacy_Ref", "Negative_Path",
]


def _salvage_test_case_rows(text: str) -> list[dict]:
    """Best-effort recovery when the LLM's JSON was truncated by max_tokens.
    Scans the text for balanced `{ … }` blocks that look like test-case
    rows (contain a `TC_ID` key) and json.loads them individually. Any
    trailing partial object is discarded silently."""
    rows: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                blob = text[start:i + 1]
                if '"TC_ID"' in blob or '"tc_id"' in blob:
                    try:
                        rows.append(json.loads(blob))
                    except Exception:  # noqa: BLE001
                        pass
                start = -1
    return rows


def _extract_test_cases_json(raw: str) -> Dict[str, Any]:
    """Parse the LLM output for `test.cases`. Accepts either a bare
    JSON object with a `test_cases` array, a fenced ```json block,
    or a `=== FILE: ... ===` split. Returns a dict with `test_cases`
    (list of row dicts using `_TC_COLUMNS` keys) plus `meta`.

    iter-14.52 — added truncation-tolerant salvage: when the LLM runs up
    against max_tokens and produces JSON cut mid-row, we still recover
    every fully-formed row it managed to emit."""
    text = (raw or "").strip()
    # Strip ```json / ``` fences
    fence = re.search(r"```(?:json|JSON)?\s*(\{[\s\S]*\}|\[[\s\S]*\])\s*```", text)
    if fence:
        text = fence.group(1)
    else:
        # Fence may be UNCLOSED because the LLM was truncated — strip a
        # leading ```json even if there's no closing ``` to match against.
        text = re.sub(r"^\s*```(?:json|JSON)?\s*", "", text)
    # Strip any === FILE: === marker sections
    if "=== FILE:" in text:
        parts = text.split("=== FILE:")
        for p in parts[1:]:
            nl = p.find("\n")
            if nl > 0 and p[:nl].strip().lower().endswith(".json"):
                text = p[nl + 1:].split("=== FILE:")[0].strip()
                break
    # Find first { or [
    first_brace = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0),
        default=-1,
    )
    if first_brace > 0:
        text = text[first_brace:]

    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        # Whole-blob salvage — try to close the JSON by trimming the
        # trailing incomplete row / open bracket / brace.
        for close_hint in ("}]}", "]}", "}", "]"):
            for trim in range(len(text), max(len(text) - 4000, 0), -1):
                candidate = text[:trim].rstrip().rstrip(",")
                # Skip candidates that end mid-string or mid-value
                if not candidate:
                    break
                # Try appending progressive closers
                for suffix in ("", "\"", "}", "]}"):
                    try:
                        parsed = json.loads(candidate + suffix)
                        break
                    except Exception:
                        continue
                if parsed is not None:
                    break
            if parsed is not None:
                break

    rows: list[dict] = []
    meta: dict = {}
    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict):
        rows = parsed.get("test_cases") or parsed.get("cases") or parsed.get("rows") or []
        meta = {k: v for k, v in parsed.items() if k not in {"test_cases", "cases", "rows"}}

    # Even if whole-blob parse succeeded, run the row-level salvage as a
    # top-up — it recovers rows the outer parse silently dropped when the
    # array's tail row was malformed.
    salvaged = _salvage_test_case_rows(text)
    if salvaged and len(salvaged) > len(rows):
        # Prefer salvage-scan when it recovered strictly more rows.
        rows = salvaged

    normalised: list[dict] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        norm: dict = {}
        for col in _TC_COLUMNS:
            v = (
                row.get(col)
                or row.get(col.lower())
                or row.get(col.replace("_", " ").lower())
                or row.get(col.replace("_", ""))
                or ""
            )
            if isinstance(v, list):
                v = "\n".join(f"{idx + 1}. {str(x)}" for idx, x in enumerate(v))
            norm[col] = str(v).strip()
        if not norm["TC_ID"]:
            norm["TC_ID"] = f"TC-{i:04d}"
        normalised.append(norm)
    return {"test_cases": normalised, "meta": meta}


def _render_test_cases_markdown(payload_obj: Dict[str, Any]) -> str:
    rows = payload_obj.get("test_cases") or []
    meta = payload_obj.get("meta") or {}
    if not rows:
        return "# Detailed Test Cases\n\n_No test cases generated._\n"
    
    # iter-14.86: Show incremental mode info in header
    header = f"# Detailed Test Cases ({len(rows)})"
    if meta.get("incremental_mode"):
        existing = meta.get("existing_tc_count", 0)
        new_added = meta.get("new_tcs_added", len(rows) - existing)
        header = f"# Detailed Test Cases ({len(rows)} total · +{new_added} new this run)"
    
    lines = [
        header,
        "",
        "| " + " | ".join(_TC_COLUMNS) + " |",
        "|" + "|".join("---" for _ in _TC_COLUMNS) + "|",
    ]
    for r in rows[:400]:  # cap in-viewer render
        cells = []
        for c in _TC_COLUMNS:
            v = str(r.get(c, "")).replace("|", "/").replace("\n", "<br/>")
            if len(v) > 220:
                v = v[:220] + "…"
            cells.append(v)
        lines.append("| " + " | ".join(cells) + " |")
    if len(rows) > 400:
        lines.append(f"\n_… {len(rows) - 400} more test cases truncated in viewer; full set in test_cases.json / Excel export._")
    return "\n".join(lines) + "\n"


# ─── iter-14.51 — batched test-case generation ─────────────────────────
# Reaching a prod-grade floor of ≥ 2000 test cases in a SINGLE LLM turn is
# not realistic even at 14k max_tokens. The runner below calls the LLM in
# successive batches (each ~150 rows), deduping by TC_ID, until the
# coverage floor derived from the KB is hit OR two consecutive batches
# fail to add anything new. Every request is a fresh formatted prompt so
# the LLM sees the current TC_ID sample + still-starved modules.

_TC_BATCH_SIZE = 40             # iter-14.54.1 — 60 was too heavy on qwen7b (>13min/batch); 40 is sustainable
_TC_MAX_BATCHES = 80            # hard safety cap
_TC_BATCH_TIMEOUT_S = 300.0     # per-batch LLM timeout
_TC_MAX_TOKENS_PER_BATCH = 10000 # 40 rows × ~180 tokens/row + salvage headroom

# iter-14.60 — Dynamic test case target based on KB complexity
# Removed hardcoded 2000 minimum. Target is now calculated from:
#   - screens, endpoints, roles, NFRs (legacy)
#   - use cases, business rules, tables (SRS + DataModel)
# Multipliers tuned for realistic coverage:
#   - Simple app (10 screens, 20 endpoints): ~100-200 TCs
#   - Medium app (50 screens, 100 endpoints): ~500-800 TCs
#   - Large app (200+ screens, 500+ endpoints): 2000+ TCs
_TC_MIN_FLOOR = 50              # absolute minimum (small apps)
_TC_MAX_CEILING = 5000          # absolute maximum (huge apps)


async def _assess_kb_complexity(project_id: str) -> dict:
    """Assess application complexity from KB, SRS, and DataModel artifacts.
    
    Returns a dict with counts and a complexity_score (0-100).
    """
    # Count KB entities
    n_classes = await kb_entities.count_documents({"project_id": project_id, "entity_type": "CLASS"})
    n_tables = await kb_entities.count_documents({"project_id": project_id, "entity_type": "TABLE"})
    n_routes = await kb_entities.count_documents({"project_id": project_id, "entity_type": "ROUTE"})
    n_methods = await kb_entities.count_documents({"project_id": project_id, "entity_type": "METHOD"})
    
    # Count SRS items - handle both dict-of-sections and array-of-sections formats
    srs_doc = await srs_documents.find_one({"project_id": project_id}, {"sections": 1})
    n_use_cases = 0
    n_business_rules = 0
    if srs_doc and srs_doc.get("sections"):
        sections = srs_doc["sections"]
        # iter-14.70: Handle dict format {key: content_str} vs array format [{key, content}]
        if isinstance(sections, dict):
            # Dict format: {"specific_requirements": "...", "use_cases": "..."}
            items = [(k, v) for k, v in sections.items() if isinstance(v, str)]
        elif isinstance(sections, list):
            # Array format: [{"key": "...", "content": "..."}, ...]
            items = []
            for sec in sections:
                if isinstance(sec, dict):
                    items.append((sec.get("key", ""), sec.get("content", "")))
                elif isinstance(sec, str):
                    items.append(("", sec))
        else:
            items = []
        
        for key, content in items:
            key = (key or "").lower()
            content = (content or "").lower()
            # Count use cases (UC-xxx patterns or bullet points in functional req)
            if "use_case" in key or "functional" in key or "uc-" in content:
                n_use_cases += content.count("uc-") + content.count("- ") // 2 + 1
            # Count business rules (BR-xxx patterns)
            if "business" in key or "rule" in key or "br-" in content:
                n_business_rules += content.count("br-") + content.count("- ") // 3 + 1
            # Count NFRs
            if "non_functional" in key or "nfr-" in content:
                n_use_cases += content.count("nfr-") + content.count("- ") // 4
    
    # Count DataModel tables
    dm_doc = await data_models_col.find_one({"project_id": project_id}, {"oltp_ddl": 1})
    n_dm_tables = 0
    if dm_doc and dm_doc.get("oltp_ddl"):
        ddl = dm_doc["oltp_ddl"]
        n_dm_tables = ddl.lower().count("create table")
    
    # Compute complexity score (0-100)
    # Weighted formula based on typical enterprise app metrics
    raw_score = (
        n_classes * 0.5 +
        n_tables * 1.0 +
        n_routes * 2.0 +
        n_methods * 0.1 +
        n_use_cases * 3.0 +
        n_business_rules * 2.0 +
        n_dm_tables * 1.5
    )
    
    # Normalize to 0-100 scale (typical range: 10-500 raw)
    complexity_score = min(100, max(0, int(raw_score / 5)))
    
    return {
        "classes": n_classes,
        "tables": n_tables,
        "routes": n_routes,
        "methods": n_methods,
        "use_cases": n_use_cases,
        "business_rules": n_business_rules,
        "dm_tables": n_dm_tables,
        "complexity_score": complexity_score,
        "complexity_tier": "high" if complexity_score >= 60 else "medium" if complexity_score >= 30 else "low",
    }

def _count_lines_starting(text: str, prefix: str = "-") -> int:
    if not text:
        return 0
    return sum(1 for ln in text.splitlines() if ln.strip().startswith(prefix))

def _extract_screen_names(catalogue_text: str) -> list[str]:
    out: list[str] = []
    for ln in (catalogue_text or "").splitlines():
        s = ln.strip()
        if not s.startswith("-"):
            continue
        body = s[1:].strip()
        # "<name>  (<file_path>)" — split on double space or paren
        name = body.split("  ")[0].split("(")[0].strip()
        if name:
            out.append(name)
    return out

def _module_slug_from_screen(name: str) -> str:
    """Best-effort MODULE slug for TC_ID stats — mirrors the prompt's
    convention (uppercase, strip extension, keep first ~14 chars)."""
    base = (name or "").rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0]
    base = re.sub(r"[^A-Za-z0-9]+", "", base).upper()
    return base[:14] or "GEN"


def _slice_catalogue_by_modules(catalogue_text: str, target_modules: set[str], max_lines: int = 8) -> str:
    """Return only the catalogue lines whose module_slug is in
    `target_modules`, capped at `max_lines`. Falls back to the first
    `max_lines` lines if the intersection is empty. This is the single
    biggest lever for cutting per-batch prompt size."""
    if not catalogue_text:
        return "(none)"
    lines = [ln for ln in catalogue_text.splitlines() if ln.strip().startswith("-")]
    if not lines:
        return catalogue_text[:1500]
    def _mod_of(ln: str) -> str:
        body = ln.strip()[1:].strip()
        name = body.split("  ")[0].split("(")[0].strip()
        return _module_slug_from_screen(name)
    matched = [ln for ln in lines if _mod_of(ln) in target_modules]
    if not matched:
        matched = lines[:max_lines]
    return "\n".join(matched[:max_lines])


def _slice_endpoints_by_modules(endpoints_text: str, target_modules: set[str], max_lines: int = 15) -> str:
    """Return only the endpoint lines whose file_path segment matches a
    target module (uppercase compare against module_slug). Endpoints
    lines look like: `- [ROUTE] GET /foo/bar (from WebContent/asri/foo/…)`."""
    if not endpoints_text:
        return "(none)"
    lines = [ln for ln in endpoints_text.splitlines() if ln.strip().startswith("-")]
    if not lines:
        return endpoints_text[:1500]
    def _match(ln: str) -> bool:
        u = ln.upper()
        return any(m and m in u for m in target_modules)
    matched = [ln for ln in lines if _match(ln)]
    if not matched:
        # Round-robin fallback so different batches see different endpoints
        matched = lines[:max_lines]
    return "\n".join(matched[:max_lines])


async def _run_test_cases_batched(jid: str, project_id: str, agent_key: str, model: str):
    """Loop-driven prod-grade test-case generator (iter-14.51 → 14.86).
    - Per-batch context is SLICED to just the modules under focus, keeping
      each prompt ~5-10 KB instead of 40+ KB (Ollama 7b throughput fix).
    - Persists an incremental artifact after every batch.
    - Per-batch timeout so a stalled LLM is visible in the UI, not silent.
    - iter-14.60: Dynamic target based on KB complexity assessment.
    - iter-14.86: INCREMENTAL mode — loads existing TCs and adds more on top.
    """
    _job_update(jid, status="running", step="Loading existing test cases…", pct=1)
    
    # ═══ iter-14.86: Load existing test cases for incremental generation ═══
    existing_artifact = await living_artifacts.find_one(
        {"project_id": project_id, "kind": "test_cases"},
        {"_id": 0, "files": 1},
        sort=[("created_at", -1)],
    )
    accumulated: list[dict] = []
    seen_ids: set[str] = set()
    module_case_count: dict[str, int] = {}
    existing_count = 0
    
    if existing_artifact:
        tc_file = next(
            (f for f in (existing_artifact.get("files") or []) 
             if str(f.get("path", "")).endswith(".json")),
            None,
        )
        if tc_file:
            try:
                payload = json.loads(tc_file.get("content") or "{}")
                existing_tcs = payload.get("test_cases") or []
                for tc in existing_tcs:
                    tid = (tc.get("TC_ID") or "").strip()
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        accumulated.append(tc)
                        mod = (tc.get("Module") or "").strip().upper() or "GEN"
                        module_case_count[mod] = module_case_count.get(mod, 0) + 1
                existing_count = len(accumulated)
                logger.info("test_cases job=%s loaded %d existing TCs for incremental mode", 
                           jid, existing_count)
            except Exception as e:
                logger.warning("test_cases job=%s failed to load existing: %s", jid, e)
    
    _job_update(jid, status="running", step="Assessing application complexity…", pct=2)
    
    # Assess KB complexity first
    complexity = await _assess_kb_complexity(project_id)
    logger.info("test_cases job=%s complexity=%s", jid, complexity)
    
    _job_update(jid, status="running", step="Loading legacy KB evidence…", pct=3)
    ctx = await _assemble_context(project_id)
    template = await get_prompt_for_project(project_id, agent_key)
    if not template:
        raise RuntimeError(f"Prompt {agent_key} not found")

    n_screens = _count_lines_starting(ctx.get("screens_catalogue", ""))
    n_endpoints = _count_lines_starting(ctx.get("legacy_endpoints", ""))
    actors_raw = (ctx.get("legacy_actors") or "").strip()
    n_roles = 0 if actors_raw.startswith("(") or not actors_raw else len(
        [x for x in actors_raw.split(",") if x.strip()])
    nfr_raw = (ctx.get("nfr_summary") or "").strip()
    n_nfr = max(1, min(30, nfr_raw.count("\n") // 2)) if not nfr_raw.startswith("(") else 6

    # iter-14.60 — Dynamic coverage formula based on KB complexity:
    # Use logarithmic scaling for large counts to prevent runaway targets.
    # A project with 2000 tables doesn't need 1000+ TCs just for tables.
    import math as _m
    
    def _scaled(count: int, base_weight: float = 1.0, cap: int = 100) -> int:
        """Logarithmic scaling: diminishing returns after 20 items."""
        if count <= 20:
            return int(count * base_weight)
        # log2(count) gives ~4.3 for 20, ~7 for 100, ~11 for 2000
        return int(min(cap, 20 * base_weight + _m.log2(count) * base_weight * 3))
    
    base_coverage = (
        _scaled(n_screens, 2.0, 80) +           # 2 TCs per screen, max 80
        _scaled(n_endpoints, 1.5, 60) +          # 1.5 TCs per endpoint, max 60
        _scaled(complexity.get("use_cases", 0), 2.0, 100) +  # use cases are important
        _scaled(complexity.get("business_rules", 0), 1.5, 80) +
        _scaled(n_roles, 3.0, 30) +              # roles need auth/permission TCs
        _scaled(n_nfr, 2.0, 40) +                # NFRs need perf/security TCs
        _scaled(complexity.get("routes", 0), 0.5, 50) +   # KB routes (lower weight)
        _scaled(complexity.get("dm_tables", 0), 0.1, 30)  # tables barely count
    )
    
    # Apply complexity multiplier
    tier = complexity.get("complexity_tier", "medium")
    multiplier = {"low": 0.8, "medium": 1.0, "high": 1.2}.get(tier, 1.0)
    computed_floor = int(base_coverage * multiplier)
    
    # Clamp to min/max bounds - max 1500 for any project
    full_target = max(_TC_MIN_FLOOR, min(1500, computed_floor))
    
    # iter-14.86: Incremental mode — calculate how many MORE we need
    remaining_target = max(0, full_target - existing_count)
    # Always generate at least 25 more TCs per regeneration, up to full target
    increment_min = 25
    target_total = max(increment_min, remaining_target) if existing_count > 0 else full_target
    # Add existing count to get final target
    final_target = existing_count + target_total
    
    target_batches = min(_TC_MAX_BATCHES,
                         max(2, math.ceil(target_total / _TC_BATCH_SIZE)))
    
    logger.info(
        "test_cases job=%s screens=%s endpoints=%s roles=%s nfr=%s "
        "use_cases=%s br=%s routes=%s tables=%s tier=%s → "
        "base=%s multiplier=%.1f existing=%s remaining=%s target=%s batches=%s",
        jid, n_screens, n_endpoints, n_roles, n_nfr,
        complexity.get("use_cases", 0), complexity.get("business_rules", 0),
        complexity.get("routes", 0), complexity.get("dm_tables", 0), tier,
        base_coverage, multiplier, existing_count, remaining_target, final_target, target_batches
    )

    screen_names = _extract_screen_names(ctx.get("screens_catalogue", ""))
    all_modules = sorted({_module_slug_from_screen(s) for s in screen_names}) or ["GEN"]
    # Merge with existing module counts
    for m in all_modules:
        if m not in module_case_count:
            module_case_count[m] = 0

    # Trim slow-moving global slices ONCE so per-batch prompt stays small.
    # We only pass a compact version of use_cases/routes/nfr, not the full 8k.
    # iter-14.76 — Detect local LLM and use MUCH smaller context limits
    # Local LLMs (Ollama on consumer hardware) can't handle >8KB prompts
    _is_local_llm = not os.environ.get("OPENROUTER_API_KEY", "").strip()
    if _is_local_llm:
        # Aggressive limits for local LLMs (~4KB total context)
        _uc_limit, _func_limit, _route_limit = 800, 800, 600
        _nfr_limit, _actor_limit, _test_limit = 500, 200, 500
        logger.info("test_cases job=%s using LOCAL LLM slim mode (4KB context)", jid)
    else:
        # Cloud LLMs can handle more context
        _uc_limit, _func_limit, _route_limit = 2500, 2500, 2000
        _nfr_limit, _actor_limit, _test_limit = 1500, 400, 1500
    
    ctx_slim = {
        **ctx,
        "use_cases": (ctx.get("use_cases") or "")[:_uc_limit],
        "srs_functional": (ctx.get("srs_functional") or "")[:_func_limit],
        "routes": (ctx.get("routes") or "")[:_route_limit],
        "nfr_summary": (ctx.get("nfr_summary") or "")[:_nfr_limit],
        "legacy_actors": (ctx.get("legacy_actors") or "")[:_actor_limit],
        "legacy_test_evidence": (ctx.get("legacy_test_evidence") or "")[:_test_limit],
        # Add complexity info to context for prompt interpolation
        "complexity_tier": tier,
        "complexity_score": str(complexity.get("complexity_score", 50)),
        "target_tc_count": str(final_target),
        # iter-14.86: Include existing count info
        "existing_tc_count": str(existing_count),
    }

    consecutive_empty = 0
    llm_status = "continue"
    last_model = ""
    art_id: str | None = None
    batch_no = 0
    
    # iter-14.76 — Dynamic batch size for local vs cloud LLMs
    if _is_local_llm:
        batch_size = 15  # Much smaller for local LLMs
        batch_timeout = 180.0  # 3 min timeout per batch
        max_tokens = 4000
        logger.info("test_cases job=%s LOCAL mode: batch_size=15, timeout=180s", jid)
    else:
        batch_size = _TC_BATCH_SIZE
        batch_timeout = _TC_BATCH_TIMEOUT_S
        max_tokens = _TC_MAX_TOKENS_PER_BATCH

    for batch_no in range(1, target_batches + 1):
        # iter-14.61 — Check for cancellation at start of each batch
        if _job_is_cancelled(jid):
            logger.info("test_cases job=%s cancelled by user at batch %s", jid, batch_no)
            break
        
        # Rank modules by fewest cases → focus this batch there.
        sorted_modules = sorted(module_case_count.items(), key=lambda kv: kv[1])
        under_covered = [m for m, _ in sorted_modules[:6]]  # 6 modules × ~7 rows = 40
        target_module_set = set(under_covered)

        # Per-batch sliced context — this is the throughput unlock.
        sliced_screens = _slice_catalogue_by_modules(
            ctx.get("screens_catalogue", ""), target_module_set, max_lines=8)
        sliced_endpoints = _slice_endpoints_by_modules(
            ctx.get("legacy_endpoints", ""), target_module_set, max_lines=15)

        # Sample of already-issued TC_IDs so the LLM can dedupe. Cap
        # aggressively — 50 IDs for incremental mode, 30 otherwise.
        sample_ids = sorted(seen_ids)
        # iter-14.86: In incremental mode, share more existing IDs to avoid dupes
        sample_cap = 50 if existing_count > 0 else 30
        if len(sample_ids) > sample_cap:
            step = len(sample_ids) // sample_cap
            sample_ids = sample_ids[::step][:sample_cap]
        sample_txt = ", ".join(sample_ids) or "(none yet — this is batch 1)"
        modules_txt = ", ".join(under_covered) or "(all modules equally covered — pick any)"

        batch_ctx = {
            **ctx_slim,
            "screens_catalogue": sliced_screens,
            "legacy_endpoints": sliced_endpoints,
            "batch_number": str(batch_no),
            "target_batches": str(target_batches),
            "cases_generated_so_far": str(len(accumulated)),
            "target_total": str(final_target),  # iter-14.86: Use final_target (existing + new)
            "target_batch_size": str(batch_size),
            "existing_tc_ids_sample": sample_txt,
            "modules_needing_more": modules_txt,
        }
        try:
            system_prompt = template.format(**batch_ctx)
        except KeyError as e:
            raise RuntimeError(f"Missing template variable {e}; available: {list(batch_ctx.keys())}")

        prompt_kb = len(system_prompt) // 1024
        pct = 3 + int(89 * (batch_no - 1) / max(1, target_batches))
        # iter-14.86: Show existing + new in progress display
        progress_display = f"{len(accumulated)}/{final_target}"
        if existing_count > 0:
            progress_display = f"{len(accumulated)}/{final_target} (+{len(accumulated) - existing_count} new)"
        _job_update(jid, step=f"Batch {batch_no}/{target_batches} · calling LLM (prompt {prompt_kb} KB, ~{batch_size} rows/turn) · {progress_display} so far…",
                    pct=pct)
        logger.info("test_cases job=%s batch=%s prompt=%dKB modules_focus=%s ids_sampled=%d",
                    jid, batch_no, prompt_kb, under_covered, len(sample_ids))
        t0 = datetime.now(timezone.utc)
        try:
            r = await fabric_call(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content":
                     f"Emit batch {batch_no} of test cases now. "
                     f"Return EXACTLY {batch_size} rows. Strict JSON only. "
                     f"Focus modules: {modules_txt}. "
                     f"Do NOT regenerate existing IDs: {sample_txt[:200]}..."},
                ],
                agent_key=agent_key, project_id=project_id,
                model_override=model,
                max_tokens=max_tokens,
                temperature=0.3,
                timeout=batch_timeout,
            )
        except Exception as call_exc:  # noqa: BLE001
            logger.exception("test_cases job=%s batch=%s LLM call failed: %s", jid, batch_no, call_exc)
            consecutive_empty += 1
            if consecutive_empty >= 3:
                logger.warning("test_cases job=%s giving up after 3 LLM failures", jid)
                break
            continue
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        last_model = r.get("model") or last_model
        raw = (r.get("content") or "").strip()
        parsed = _extract_test_cases_json(raw)
        new_rows = parsed.get("test_cases") or []
        llm_status = str((parsed.get("meta") or {}).get("status", "continue")).lower()
        logger.info("test_cases job=%s batch=%s LLM done in %.1fs · raw=%d chars · parsed=%d rows",
                    jid, batch_no, elapsed, len(raw), len(new_rows))
        if not new_rows and raw:
            # Log a preview so an operator can see WHY the parse produced 0.
            logger.warning("test_cases job=%s batch=%s parse yielded 0 rows. raw head: %s",
                           jid, batch_no, raw[:300].replace("\n", " ⏎ "))

        added_this_batch = 0
        for row in new_rows:
            tid = (row.get("TC_ID") or "").strip()
            if not tid or tid in seen_ids:
                # Rename duplicates so we don't lose the content, but
                # keep dedupe intent by suffixing with the batch number.
                tid = f"{tid or 'TC-GEN'}-B{batch_no}-{added_this_batch:03d}"
                row["TC_ID"] = tid
                if tid in seen_ids:
                    continue
            seen_ids.add(tid)
            accumulated.append(row)
            added_this_batch += 1
            mod = (row.get("Module") or "").strip().upper() or _module_slug_from_screen(row.get("Screen_or_API", ""))
            module_case_count[mod] = module_case_count.get(mod, 0) + 1

        logger.info("test_cases job=%s batch=%s: +%s rows (total %s / target %s, llm_status=%s)",
                    jid, batch_no, added_this_batch, len(accumulated), final_target, llm_status)

        # Persist an interim artifact so the UI reflects progress live.
        try:
            interim_payload = {
                "meta": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "batches_completed": batch_no,
                    "batches_target": target_batches,
                    "target_total": final_target,  # iter-14.86: Use final_target
                    "computed_floor": computed_floor,
                    "n_screens": n_screens, "n_endpoints": n_endpoints,
                    "n_roles": n_roles, "n_nfr": n_nfr,
                    # iter-14.87: Add complexity counts that actually drive the target
                    "n_use_cases": complexity.get("use_cases", 0),
                    "n_business_rules": complexity.get("business_rules", 0),
                    "n_routes": complexity.get("routes", 0),
                    "n_dm_tables": complexity.get("dm_tables", 0),
                    "model": last_model,
                    "status": "in_progress" if len(accumulated) < final_target else "complete",
                    # iter-14.86: Include incremental tracking in interim payload
                    "incremental_mode": existing_count > 0,
                    "existing_tc_count": existing_count,
                    "new_tcs_added": len(accumulated) - existing_count,
                    "coverage_summary": {
                        "total": len(accumulated),
                        "by_module": dict(sorted(module_case_count.items(), key=lambda kv: -kv[1])[:20]),
                    },
                },
                "test_cases": accumulated,
            }
            files = [
                {"path": "test_cases.json", "content": json.dumps(interim_payload, indent=2)},
                {"path": "test_cases_summary.md",
                 "content": _render_test_cases_markdown(interim_payload)},
            ]
            art_id = await _save_artifact(project_id, "test_cases", files,
                                          {"model": last_model, "agent_key": agent_key,
                                           "batches_completed": batch_no})
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning("test_cases interim persist failed: %s", persist_exc)

        if added_this_batch == 0:
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        # Stop conditions — use final_target (existing + new)
        if len(accumulated) >= final_target and llm_status == "complete":
            logger.info("test_cases: floor met + LLM signalled complete → stop")
            break
        if len(accumulated) >= final_target and batch_no >= 3:
            logger.info("test_cases: floor met → stop (LLM status still %s)", llm_status)
            break
        if consecutive_empty >= 3:
            logger.warning("test_cases: 3 empty batches in a row → stop early with %s rows", len(accumulated))
            break

    _job_update(jid, step="Finalising test-case matrix…", pct=94)
    # iter-14.86: Track incremental mode in final payload
    new_tcs_added = len(accumulated) - existing_count
    final_payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "batches_completed": min(target_batches, batch_no),
            "batches_target": target_batches,
            "target_total": final_target,
            "computed_floor": computed_floor,
            "n_screens": n_screens, "n_endpoints": n_endpoints,
            "n_roles": n_roles, "n_nfr": n_nfr,
            # iter-14.87: Add complexity counts that actually drive the target
            "n_use_cases": complexity.get("use_cases", 0),
            "n_business_rules": complexity.get("business_rules", 0),
            "n_routes": complexity.get("routes", 0),
            "n_dm_tables": complexity.get("dm_tables", 0),
            "model": last_model,
            "status": "complete" if len(accumulated) >= final_target else "under_floor",
            # iter-14.86: Incremental mode tracking
            "incremental_mode": existing_count > 0,
            "existing_tc_count": existing_count,
            "new_tcs_added": new_tcs_added,
            "coverage_summary": {
                "total": len(accumulated),
                "by_module": dict(sorted(module_case_count.items(), key=lambda kv: -kv[1])[:30]),
            },
        },
        "test_cases": accumulated,
    }
    files = [
        {"path": "test_cases.json", "content": json.dumps(final_payload, indent=2)},
        {"path": "test_cases_summary.md",
         "content": _render_test_cases_markdown(final_payload)},
    ]
    art_id = await _save_artifact(project_id, "test_cases", files,
                                  {"model": last_model, "agent_key": agent_key,
                                   "final_row_count": len(accumulated),
                                   "target_total": final_target,
                                   "existing_tc_count": existing_count,
                                   "new_tcs_added": new_tcs_added})
    _job_complete(jid, {"artifact_id": art_id, "kind": "test_cases",
                        "files_count": len(files), "model": last_model,
                        "row_count": len(accumulated), "target_total": final_target,
                        "existing_tc_count": existing_count, "new_tcs_added": new_tcs_added})


# ─── iter-14.55 — batched JMeter plan generator ────────────────────────
# The `test.jmeter` prompt asks for 4 samplers per endpoint, which for a
# real project (120+ endpoints) means 480+ HTTPSamplerProxy blocks in a
# single .jmx. No local 7b model can emit that in one turn — the previous
# runner produced ~9 samplers before running out of tokens. This runner
# splits the endpoint list into small chunks, calls the LLM per chunk
# for ONLY the sampler XML fragments, and merges them into a single
# valid .jmx envelope built from a deterministic template. Every legacy
# endpoint gets exactly 4 samplers.

_JM_ENDPOINTS_PER_BATCH = 5      # iter-14.55.1 — 5 endpoints × 4 samplers = 20 samplers/turn (~6k tokens, ~3-4 min on qwen 7b)
_JM_MAX_BATCHES = 40             # safety cap
_JM_BATCH_TIMEOUT_S = 240.0
_JM_MAX_TOKENS_PER_BATCH = 8000

_JM_ENDPOINT_LINE_RX = re.compile(
    r"^\s*-\s*(?:\[[A-Z_]+\]\s*)?"          # optional `[ROUTE]` type tag
    r"(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD)\s+"
    r"(?P<path>\S+)",
    re.IGNORECASE,
)


def _parse_endpoint_lines(legacy_endpoints_text: str) -> list[dict]:
    """Extract [(method, path, source_file)] triples from the KB endpoint
    slice so we can chunk them for the JMeter batched runner."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ln in (legacy_endpoints_text or "").splitlines():
        m = _JM_ENDPOINT_LINE_RX.match(ln)
        if not m:
            continue
        method = m.group("method").upper()
        path = m.group("path").strip().rstrip("(),")
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        src = ""
        p_from = ln.find("(from ")
        if p_from > 0:
            src = ln[p_from + 6:].rstrip(")").strip()
        out.append({"method": method, "path": path, "source": src})
    return out


def _build_jmeter_envelope(project_name: str, base_url: str,
                           samplers_xml: str, personas: list[str],
                           n_endpoints: int, n_samplers: int) -> str:
    """iter-14.55 — Deterministic .jmx skeleton. Wraps the LLM-produced
    sampler fragments in a valid, JMeter 5.6.3-openable envelope with
    variables, header manager, cookie/cache managers, per-persona thread
    groups, CSV data set, listeners, and result collectors — none of
    which the LLM can be trusted to structure correctly at scale."""
    persona_groups = []
    thread_var_map = {"Anonymous": "THREADS_ANON", "Authenticated": "THREADS_USER", "Admin": "THREADS_ADMIN"}
    if not personas:
        personas = ["Anonymous", "Authenticated", "Admin"]
    for p in personas:
        tvar = thread_var_map.get(p, "THREADS_USER")
        persona_groups.append(f"""
        <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="TG {p}" enabled="true">
          <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
          <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
            <boolProp name="LoopController.continue_forever">false</boolProp>
            <stringProp name="LoopController.loops">${{LOOPS}}</stringProp>
          </elementProp>
          <stringProp name="ThreadGroup.num_threads">${{{tvar}}}</stringProp>
          <stringProp name="ThreadGroup.ramp_time">${{RAMP_UP}}</stringProp>
          <boolProp name="ThreadGroup.scheduler">true</boolProp>
          <stringProp name="ThreadGroup.duration">${{DURATION_SEC}}</stringProp>
          <stringProp name="ThreadGroup.delay">0</stringProp>
        </ThreadGroup>
        <hashTree>
          {samplers_xml}
        </hashTree>""")
    tg_block = "\n".join(persona_groups)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- requires: jmeter-plugins-manager, jpgc-graphs-basic, jpgc-casutg, jpgc-tst, jpgc-perfmon -->
<!-- iter-14.55 batched plan: {n_endpoints} endpoints × 4 samplers = {n_samplers} HTTPSamplerProxy blocks -->
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="{project_name} — Load + API Contract" enabled="true">
      <stringProp name="TestPlan.comments">Generated by LAMA Living/JMeter — batched samplers merged from {n_endpoints} endpoints.</stringProp>
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.tearDown_on_shutdown">true</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments">
          <elementProp name="BASE_URL" elementType="Argument"><stringProp name="Argument.name">BASE_URL</stringProp><stringProp name="Argument.value">{base_url}</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="RAMP_UP" elementType="Argument"><stringProp name="Argument.name">RAMP_UP</stringProp><stringProp name="Argument.value">60</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="LOOPS" elementType="Argument"><stringProp name="Argument.name">LOOPS</stringProp><stringProp name="Argument.value">1</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="DURATION_SEC" elementType="Argument"><stringProp name="Argument.name">DURATION_SEC</stringProp><stringProp name="Argument.value">300</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="THINK_TIME_MS" elementType="Argument"><stringProp name="Argument.name">THINK_TIME_MS</stringProp><stringProp name="Argument.value">500</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="THREADS_ANON" elementType="Argument"><stringProp name="Argument.name">THREADS_ANON</stringProp><stringProp name="Argument.value">10</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="THREADS_USER" elementType="Argument"><stringProp name="Argument.name">THREADS_USER</stringProp><stringProp name="Argument.value">50</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
          <elementProp name="THREADS_ADMIN" elementType="Argument"><stringProp name="Argument.name">THREADS_ADMIN</stringProp><stringProp name="Argument.value">5</stringProp><stringProp name="Argument.metadata">=</stringProp></elementProp>
        </collectionProp>
      </elementProp>
    </TestPlan>
    <hashTree>
      <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
        <elementProp name="HTTPsampler.Arguments" elementType="Arguments"><collectionProp name="Arguments.arguments"/></elementProp>
        <stringProp name="HTTPSampler.domain"></stringProp>
        <stringProp name="HTTPSampler.port"></stringProp>
        <stringProp name="HTTPSampler.protocol"></stringProp>
      </ConfigTestElement>
      <hashTree/>
      <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="HTTP Header Manager" enabled="true">
        <collectionProp name="HeaderManager.headers">
          <elementProp name="" elementType="Header"><stringProp name="Header.name">Content-Type</stringProp><stringProp name="Header.value">application/json</stringProp></elementProp>
          <elementProp name="" elementType="Header"><stringProp name="Header.name">Accept</stringProp><stringProp name="Header.value">application/json</stringProp></elementProp>
        </collectionProp>
      </HeaderManager>
      <hashTree/>
      <CookieManager guiclass="CookiePanel" testclass="CookieManager" testname="HTTP Cookie Manager" enabled="true"/>
      <hashTree/>
      <CacheManager guiclass="CacheManagerGui" testclass="CacheManager" testname="HTTP Cache Manager" enabled="true"/>
      <hashTree/>
      <CSVDataSet guiclass="TestBeanGUI" testclass="CSVDataSet" testname="CSV — auth data" enabled="true">
        <stringProp name="delimiter">,</stringProp>
        <stringProp name="fileEncoding">UTF-8</stringProp>
        <stringProp name="filename">test-data/authenticated_data.csv</stringProp>
        <boolProp name="ignoreFirstLine">true</boolProp>
        <boolProp name="quotedData">true</boolProp>
        <boolProp name="recycle">true</boolProp>
        <boolProp name="stopThread">false</boolProp>
        <stringProp name="shareMode">shareMode.all</stringProp>
        <stringProp name="variableNames">username,password,role</stringProp>
      </CSVDataSet>
      <hashTree/>
      {tg_block}
      <ResultCollector guiclass="ViewResultsFullVisualizer" testclass="ResultCollector" testname="View Results Tree" enabled="false">
        <boolProp name="ResultCollector.error_logging">false</boolProp>
        <objProp><name>saveConfig</name><value class="SampleSaveConfiguration"><time>true</time><latency>true</latency><timestamp>true</timestamp><success>true</success><label>true</label><code>true</code><message>true</message><threadName>true</threadName><dataType>true</dataType><encoding>false</encoding><assertions>true</assertions><subresults>true</subresults><responseData>false</responseData><samplerData>false</samplerData><xml>false</xml><fieldNames>true</fieldNames><responseHeaders>false</responseHeaders><requestHeaders>false</requestHeaders><responseDataOnError>false</responseDataOnError><saveAssertionResultsFailureMessage>true</saveAssertionResultsFailureMessage><assertionsResultsToSave>0</assertionsResultsToSave><bytes>true</bytes><sentBytes>true</sentBytes><url>true</url><threadCounts>true</threadCounts><idleTime>true</idleTime><connectTime>true</connectTime></value></objProp>
        <stringProp name="filename"></stringProp>
      </ResultCollector>
      <hashTree/>
      <ResultCollector guiclass="SimpleDataWriter" testclass="ResultCollector" testname="Simple Data Writer (.jtl)" enabled="true">
        <boolProp name="ResultCollector.error_logging">false</boolProp>
        <objProp><name>saveConfig</name><value class="SampleSaveConfiguration"><time>true</time><latency>true</latency><timestamp>true</timestamp><success>true</success><label>true</label><code>true</code><message>true</message><threadName>true</threadName><dataType>true</dataType><encoding>false</encoding><assertions>false</assertions><subresults>false</subresults><responseData>false</responseData><samplerData>false</samplerData><xml>false</xml><fieldNames>true</fieldNames><responseHeaders>false</responseHeaders><requestHeaders>false</requestHeaders><responseDataOnError>false</responseDataOnError><saveAssertionResultsFailureMessage>true</saveAssertionResultsFailureMessage><assertionsResultsToSave>0</assertionsResultsToSave><bytes>true</bytes><sentBytes>true</sentBytes><url>true</url></value></objProp>
        <stringProp name="filename">results/results.jtl</stringProp>
      </ResultCollector>
      <hashTree/>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


def _jmeter_csv_content(personas: list[str]) -> dict[str, str]:
    """Generate deterministic sample CSV data files (auth + payload seeds)."""
    header = "username,password,role\n"
    rows = [f"user{i:02d},P@ssw0rd{i:02d},{persona}\n"
            for i, persona in enumerate(personas or ["Authenticated"], start=1)
            for _ in range(5)]
    return {
        "perf/test-data/authenticated_data.csv": header + "".join(rows[:15]),
    }


def _jmeter_runbook(project_name: str, n_endpoints: int) -> str:
    return f"""# {project_name} — JMeter Load + API Contract Plan

Generated by LAMA Living/JMeter. Covers **{n_endpoints} endpoints × 4
sampler types** = **{n_endpoints * 4} HTTPSamplerProxy** blocks in total.

## Run in CI

```bash
jmeter -n -t perf/plan.jmx \\
       -JBASE_URL=$BASE_URL \\
       -JTHREADS_ANON=5 -JTHREADS_USER=25 -JTHREADS_ADMIN=2 \\
       -JLOOPS=1 -JDURATION_SEC=180 \\
       -l results/results.jtl \\
       -e -o results/html-report
```

## Filter one sampler class

```bash
# Run only the CONTRACT samplers (API contract pass in seconds)
jmeter -n -t perf/plan.jmx -Jjmeter.saveservice.output_format=csv \\
       -JincludeRegex='\\[CONTRACT\\].*' -l results/contract.jtl
```

## Sampler naming convention

* `[POS] METHOD /path`      → Positive-Load (2xx + Duration Assertion)
* `[NEG-AUTH] METHOD /path` → Negative-Auth (401/403 expected)
* `[BAD] METHOD /path`      → Boundary / Bad-Input (4xx + $.error)
* `[CONTRACT] METHOD /path` → Contract (schema-shape via JSONPathAssertion)

## Variables

| Var | Default | Purpose |
|---|---|---|
| BASE_URL | http://localhost:8001 | Target host |
| RAMP_UP | 60 | Seconds |
| LOOPS | 1 | -1 for soak |
| DURATION_SEC | 300 | Test wall-clock |
| THREADS_ANON | 10 | Anonymous persona |
| THREADS_USER | 50 | Authenticated user persona |
| THREADS_ADMIN | 5 | Admin persona |
"""


async def _fetch_kb_endpoints(project_id: str) -> list[dict]:
    """iter-14.55 — Query the KB directly for ROUTE / ACTION / FORWARD
    entities and return a canonical [{method, path, source}] list. This
    bypasses the prose `legacy_endpoints` slice (which drops the verb +
    route_path fields we need for JMeter samplers)."""
    from db import kb_entities as _kbe
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    async for e in _kbe.find(
        {"project_id": project_id, "type": "ROUTE"},
        {"_id": 0, "verb": 1, "http_method": 1, "name": 1, "source": 1},
    ):
        method = (e.get("verb") or e.get("http_method") or "POST").upper()
        path = (e.get("name") or "").strip()
        if not path:
            continue
        if not path.startswith("/"):
            path = "/" + path
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": method, "path": path, "source": e.get("source", ""), "kind": "ROUTE"})

    async for e in _kbe.find(
        {"project_id": project_id, "type": "ACTION"},
        {"_id": 0, "route_path": 1, "name": 1, "source": 1},
    ):
        raw = (e.get("route_path") or "").strip()
        if not raw:
            nm = (e.get("name") or "").strip()
            if not nm:
                continue
            raw = "/" + re.sub(r"Action$", "", nm)
        path = raw if raw.startswith("/") else "/" + raw
        if not path.endswith(".do"):
            path = path + ".do"
        key = ("POST", path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": "POST", "path": path, "source": e.get("source", ""), "kind": "ACTION"})

    async for e in _kbe.find(
        {"project_id": project_id, "type": "FORWARD"},
        {"_id": 0, "name": 1, "source": 1},
    ):
        raw = (e.get("name") or "").strip()
        if not raw:
            continue
        path = raw if raw.startswith("/") else "/" + raw
        key = ("GET", path)
        if key in seen:
            continue
        seen.add(key)
        out.append({"method": "GET", "path": path, "source": e.get("source", ""), "kind": "FORWARD"})

    return out


def _synth_payload_json(method: str, path: str, source_hint: str = "") -> str:
    """Best-effort payload synthesis from path shape. Not comprehensive
    — enough to make the sampler runnable. For strict payloads users
    can plug the CSV Data Set."""
    if method in {"GET", "DELETE", "HEAD"}:
        return ""
    # id-like path segments → include an id in body
    body: dict[str, object] = {}
    for seg in path.strip("/").split("/"):
        if seg.endswith("Id") or seg.endswith("ID") or seg == "id":
            body["id"] = 1
    # source hint often names the entity
    ent = source_hint.rsplit("/", 1)[-1].rsplit(".", 1)[0] if source_hint else ""
    if ent:
        body["entity"] = ent
    if not body:
        body["name"] = "sample"
    body["ts"] = "2026-08-20T12:00:00Z"
    return json.dumps(body).replace("\"", "&quot;")


def _synth_bad_payload_json(method: str) -> str:
    if method in {"GET", "DELETE", "HEAD"}:
        return ""
    return "{&quot;id&quot;:-1,&quot;name&quot;:&quot;&quot;,&quot;dropTable&quot;:&quot;\\u0027;DROP TABLE users;--&quot;}"


def _sampler_xml(name: str, method: str, path: str, body_json: str,
                 expected_status: int, duration_ms: int,
                 auth_bearer: str = "", extra_assertions: str = "") -> str:
    """Emit a single <HTTPSamplerProxy> + hashTree with baked-in
    assertions. Deterministic — no LLM. All URLs go through ${BASE_URL}."""
    args_block = ""
    if body_json:
        args_block = f"""
        <elementProp name="" elementType="HTTPArgument">
          <boolProp name="HTTPArgument.always_encode">false</boolProp>
          <stringProp name="Argument.value">{body_json}</stringProp>
          <stringProp name="Argument.metadata">=</stringProp>
        </elementProp>"""
    auth_header = ""
    if auth_bearer:
        auth_header = f"""
      <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="Auth override" enabled="true">
        <collectionProp name="HeaderManager.headers">
          <elementProp name="" elementType="Header"><stringProp name="Header.name">Authorization</stringProp><stringProp name="Header.value">{auth_bearer}</stringProp></elementProp>
        </collectionProp>
      </HeaderManager>
      <hashTree/>"""
    # Response Assertion — string match on the first digit(s) of the status
    status_prefix = str(expected_status)[0]  # "2", "4", "5"
    return f"""
  <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{name}" enabled="true">
    <stringProp name="HTTPSampler.domain"></stringProp>
    <stringProp name="HTTPSampler.port"></stringProp>
    <stringProp name="HTTPSampler.protocol"></stringProp>
    <stringProp name="HTTPSampler.method">{method}</stringProp>
    <stringProp name="HTTPSampler.path">${{BASE_URL}}{path}</stringProp>
    <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
    <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
    <boolProp name="HTTPSampler.postBodyRaw">{"true" if body_json else "false"}</boolProp>
    <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
      <collectionProp name="Arguments.arguments">{args_block}
      </collectionProp>
    </elementProp>
  </HTTPSamplerProxy>
  <hashTree>{auth_header}
    <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Assert {expected_status}" enabled="true">
      <collectionProp name="Asserion.test_strings"><stringProp name="0">{status_prefix}</stringProp></collectionProp>
      <stringProp name="Assertion.custom_message">Expected HTTP {expected_status}</stringProp>
      <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
      <boolProp name="Assertion.assume_success">false</boolProp>
      <intProp name="Assertion.test_type">1</intProp>
    </ResponseAssertion>
    <hashTree/>
    <DurationAssertion guiclass="DurationAssertionGui" testclass="DurationAssertion" testname="≤ {duration_ms}ms" enabled="true">
      <stringProp name="DurationAssertion.duration">{duration_ms}</stringProp>
    </DurationAssertion>
    <hashTree/>
    <UniformRandomTimer guiclass="UniformRandomTimerGui" testclass="UniformRandomTimer" testname="Think time" enabled="true">
      <stringProp name="ConstantTimer.delay">${{THINK_TIME_MS}}</stringProp>
      <stringProp name="RandomTimer.range">200</stringProp>
    </UniformRandomTimer>
    <hashTree/>{extra_assertions}
  </hashTree>"""


def _samplers_for_endpoint(ep: dict) -> str:
    """iter-14.55.2 — Emit all 4 sampler variants for one endpoint
    (Positive-Load, Negative-Auth, Boundary/Bad-Input, Contract) wrapped
    in a GenericController named after the endpoint. Deterministic —
    the LLM is not in the loop; every legacy endpoint gets 4 samplers."""
    method = ep["method"].upper()
    path = ep["path"]
    src = ep.get("source", "")
    good_body = _synth_payload_json(method, path, src)
    bad_body = _synth_bad_payload_json(method)
    duration_ms = 1500 if method == "GET" else 3000
    # JSONPathAssertion for the Contract sampler
    contract_extra = """
    <com.atlantbh.jmeter.plugins.jsonutils.jsonpathassertion.JSONPathAssertion guiclass="com.atlantbh.jmeter.plugins.jsonutils.jsonpathassertion.gui.JSONPathAssertionGui" testclass="com.atlantbh.jmeter.plugins.jsonutils.jsonpathassertion.JSONPathAssertion" testname="Contract: response is JSON object" enabled="true">
      <stringProp name="JSON_PATH">$</stringProp>
      <stringProp name="EXPECTED_VALUE"></stringProp>
      <boolProp name="JSONVALIDATION">true</boolProp>
      <boolProp name="EXPECT_NULL">false</boolProp>
      <boolProp name="INVERT">false</boolProp>
      <boolProp name="ISREGEX">false</boolProp>
    </com.atlantbh.jmeter.plugins.jsonutils.jsonpathassertion.JSONPathAssertion>
    <hashTree/>"""
    pos = _sampler_xml(f"[POS] {method} {path}", method, path, good_body,
                       expected_status=200, duration_ms=duration_ms)
    neg_auth = _sampler_xml(f"[NEG-AUTH] {method} {path}", method, path, good_body,
                            expected_status=401, duration_ms=duration_ms,
                            auth_bearer="Bearer invalid-token-xxx")
    bad = _sampler_xml(f"[BAD] {method} {path}", method, path, bad_body,
                       expected_status=400, duration_ms=duration_ms)
    contract = _sampler_xml(f"[CONTRACT] {method} {path}", method, path, good_body,
                            expected_status=200, duration_ms=duration_ms,
                            extra_assertions=contract_extra)
    ctrl_name = f"{method} {path}".replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""
<GenericController guiclass="LogicControllerGui" testclass="GenericController" testname="{ctrl_name}" enabled="true"/>
<hashTree>{pos}{neg_auth}{bad}{contract}
</hashTree>"""


async def _run_jmeter_batched(jid: str, project_id: str, model: str = ""):
    """iter-14.55.2 — Deterministic JMeter plan builder.

    LLM output at scale (480+ HTTPSamplerProxy blocks with valid XML +
    assertions + timers) is not achievable on any single-turn local
    model. Since the XML structure is standardized JMeter 5.6.3 boilerplate,
    we generate it deterministically from the KB endpoint list, guaranteeing:
      • Every KB endpoint gets exactly 4 samplers (POS/NEG-AUTH/BAD/CONTRACT).
      • Consistent Response + Duration + JSONPath assertions.
      • Runs in seconds, not hours.
      • Reproducible.

    The LLM prompt library still owns the higher-level shape (via
    `test.jmeter` for the previous single-shot mode and
    `test.jmeter.samplers` reserved for a future opt-in enrichment
    pass), but this runner is the fast, guaranteed-coverage path.
    """
    _job_update(jid, status="running", step="Loading legacy KB endpoints…", pct=5)
    ctx = await _assemble_context(project_id)

    endpoints = await _fetch_kb_endpoints(project_id)
    if not endpoints:
        endpoints = _parse_endpoint_lines(ctx.get("legacy_endpoints", ""))
    if not endpoints:
        raise RuntimeError("No legacy endpoints indexed in KB — build KB first.")

    logger.info("jmeter job=%s: deterministic build for %d endpoints × 4 samplers",
                jid, len(endpoints))

    _job_update(jid, step=f"Rendering {len(endpoints) * 4} samplers…", pct=40)
    sampler_blocks: list[str] = []
    for i, ep in enumerate(endpoints):
        sampler_blocks.append(_samplers_for_endpoint(ep))
        if i % 20 == 0:
            pct = 40 + int(45 * i / max(1, len(endpoints)))
            _job_update(jid, step=f"Rendering samplers · {i+1}/{len(endpoints)} endpoints…", pct=pct)
    samplers_xml = "\n".join(sampler_blocks)
    total_samplers = samplers_xml.count("<HTTPSamplerProxy")

    _job_update(jid, step="Assembling plan envelope + persona thread groups…", pct=90)
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "name": 1, "deploy_base_url": 1}) or {}
    project_name = proj.get("name") or "Migration"
    base_url = proj.get("deploy_base_url") or "http://localhost:8001"

    actors_raw = (ctx.get("legacy_actors") or "").strip()
    personas = [p.strip().title() for p in actors_raw.split(",")
                if p.strip() and not p.strip().startswith("(")][:5]
    if not personas:
        personas = ["Anonymous", "Authenticated", "Admin"]

    plan_xml = _build_jmeter_envelope(project_name, base_url, samplers_xml,
                                      personas, len(endpoints), total_samplers)

    files = [{"path": "perf/plan.jmx", "content": plan_xml}]
    for path, content in _jmeter_csv_content(personas).items():
        files.append({"path": path, "content": content})
    files.append({"path": "perf/README-run.md",
                  "content": _jmeter_runbook(project_name, len(endpoints))})

    art_id = await _save_artifact(project_id, "jmeter", files,
                                  {"model": "deterministic-scaffold",
                                   "agent_key": "jmeter.builder",
                                   "n_endpoints": len(endpoints),
                                   "n_samplers": total_samplers,
                                   "coverage_pct": 100})
    _job_complete(jid, {"artifact_id": art_id, "kind": "jmeter",
                        "files_count": len(files),
                        "model": "deterministic-scaffold",
                        "n_endpoints": len(endpoints),
                        "n_samplers": total_samplers})


async def _run_generic(jid: str, project_id: str, agent_key: str, kind: str, model: str = ""):
    # iter-14.51 — Detailed test-case matrix is a MULTI-BATCH job (needs
    # ≥ 2000 rows for a real project). Delegate to the dedicated loop
    # runner which handles progress + interim persistence.
    if kind == "test_cases":
        try:
            await _run_test_cases_batched(jid, project_id, agent_key, model)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"living job test_cases failed: {e}")
            _job_error(jid, str(e)[:500])
        return
    # iter-14.55 — JMeter also runs multi-batch (4 samplers × N endpoints
    # cannot fit in a single 7b Ollama turn). Delegate to the batched
    # runner which loops over endpoint chunks and merges the fragments.
    if kind == "jmeter":
        try:
            await _run_jmeter_batched(jid, project_id, model)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"living job jmeter failed: {e}")
            _job_error(jid, str(e)[:500])
        return
    try:
        _job_update(jid, status="running", step="Loading context…", pct=8)
        ctx = await _assemble_context(project_id)
        _job_update(jid, step="Rendering prompt…", pct=18)
        template = await get_prompt_for_project(project_id, agent_key)
        if not template:
            raise RuntimeError(f"Prompt {agent_key} not found")
        try:
            system_prompt = template.format(**ctx)
        except KeyError as e:
            raise RuntimeError(f"Missing template variable {e}; available: {list(ctx.keys())}")

        _job_update(jid, step=f"Calling {agent_key} LLM…", pct=35)
        r = await fabric_call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Generate {kind} now."},
            ],
            agent_key=agent_key, project_id=project_id,
            model_override=model, max_tokens=8000, temperature=0.2, timeout=200.0,
        )
        raw = (r.get("content") or "").strip()

        _job_update(jid, step="Persisting artifact…", pct=88)
        if kind == "test_cases":
            # iter-14.50 — Detailed test-case matrix. The prompt is
            # instructed to return one JSON object per LLM turn with
            # a `test_cases` array. Persist as a single JSON file so
            # the Excel exporter can stream it out; a plain-text
            # summary rendering is written alongside for the UI
            # viewer that only knows how to display strings.
            payload_obj = _extract_test_cases_json(raw)
            files = [
                {"path": "test_cases.json", "content": json.dumps(payload_obj, indent=2)},
                {"path": "test_cases_summary.md",
                 "content": _render_test_cases_markdown(payload_obj)},
            ]
        elif kind in {"selenium", "jmeter"}:
            files = _split_files(raw)
        else:
            files = [{"path": f"{kind}.md", "content": raw}]
        art_id = await _save_artifact(project_id, kind, files, {"model": r.get("model"), "agent_key": agent_key})

        _job_complete(jid, {"artifact_id": art_id, "kind": kind,
                            "files_count": len(files), "model": r.get("model")})
    except Exception as e:
        logger.exception(f"living job {kind} failed: {e}")
        _job_error(jid, str(e)[:500])


@router.post("/jobs/start/selenium")
async def start_selenium(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "CodeGen", "Living")
    jid = _new_job(project_id, "selenium")
    asyncio.create_task(_run_generic(jid, project_id, "test.selenium", "selenium",
                                     payload.get("model", "")))
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/jmeter")
async def start_jmeter(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "CodeGen", "Living")
    jid = _new_job(project_id, "jmeter")
    asyncio.create_task(_run_generic(jid, project_id, "test.jmeter", "jmeter",
                                     payload.get("model", "")))
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/test-cases")
async def start_test_cases(payload: dict):
    """iter-14.50 — Detailed LLM-driven test-case matrix. Prompt is
    fully controlled from the Prompt Library (`test.cases` key); the
    route only pipes context in and unpacks the JSON output."""
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "CodeGen", "Living")
    jid = _new_job(project_id, "test_cases")
    asyncio.create_task(_run_generic(jid, project_id, "test.cases", "test_cases",
                                     payload.get("model", "")))
    return {"job_id": jid, "status": "queued"}


@router.get("/{project_id}/artifact/{artifact_id}/excel")
async def download_test_cases_excel(project_id: str, artifact_id: str):
    """iter-14.50 — Excel export for any Living artifact that carries a
    `test_cases.json` file. Works for the test.cases artifact today and
    any future JSON-row artifacts we wire up. openpyxl streamed from memory."""
    art = await living_artifacts.find_one(
        {"project_id": project_id, "id": artifact_id}, {"_id": 0},
    )
    if not art:
        raise HTTPException(404, "Artifact not found")
    tc_file = next(
        (f for f in (art.get("files") or []) if str(f.get("path", "")).endswith(".json")),
        None,
    )
    if not tc_file:
        raise HTTPException(400, "Artifact has no JSON payload to export")
    try:
        payload_obj = json.loads(tc_file.get("content") or "{}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Invalid JSON in artifact: {exc}") from exc
    rows = payload_obj.get("test_cases") or []
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"openpyxl unavailable: {exc}") from exc
    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"
    header_fill = PatternFill(start_color="FFE600", end_color="FFE600", fill_type="solid")
    header_font = Font(bold=True, color="2E2E38")
    ws.append(_TC_COLUMNS)
    for col_idx, col in enumerate(_TC_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append([str(row.get(c, "")) for c in _TC_COLUMNS])
        r_num = ws.max_row
        for c_idx in range(1, len(_TC_COLUMNS) + 1):
            ws.cell(row=r_num, column=c_idx).alignment = wrap
    widths = [12, 18, 26, 10, 12, 32, 40, 24, 32, 14, 30, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"test_cases_{project_id[:8]}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/jobs/start/drift")
async def start_drift(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "CodeGen", "Living")
    live_signals = payload.get("live_signals", "") or "(no live signals attached)"
    jid = _new_job(project_id, "drift")

    async def _run():
        try:
            _job_update(jid, status="running", step="Loading SRS…", pct=15)
            ctx = await _assemble_context(project_id)
            ctx["live_signals"] = live_signals[:8000]
            tpl = await get_prompt_for_project(project_id, "drift.detector")
            if not tpl:
                raise RuntimeError("drift.detector prompt missing")
            sys_p = tpl.format(**ctx)
            _job_update(jid, step="Detecting drift…", pct=45)
            r = await fabric_call(
                messages=[{"role": "system", "content": sys_p},
                          {"role": "user", "content": "Produce the drift report."}],
                agent_key="drift.detector", project_id=project_id,
                max_tokens=5000, temperature=0.1, timeout=180.0,
            )
            content = (r.get("content") or "").strip()
            art_id = await _save_artifact(project_id, "drift", [{"path": "drift_report.md", "content": content}],
                                          {"model": r.get("model"), "live_signals_chars": len(live_signals)})
            _job_complete(jid, {"artifact_id": art_id, "kind": "drift"})
        except Exception as e:
            _job_error(jid, str(e)[:500])

    asyncio.create_task(_run())
    return {"job_id": jid, "status": "queued"}


@router.post("/jobs/start/srs-diff")
async def start_srs_diff(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "CodeGen", "Living")
    srs_a = (payload.get("srs_a") or "").strip()
    srs_b = (payload.get("srs_b") or "").strip()
    if not srs_a or not srs_b:
        raise HTTPException(400, "srs_a and srs_b required (raw markdown)")
    jid = _new_job(project_id, "srs-diff")

    async def _run():
        try:
            _job_update(jid, status="running", step="Diffing…", pct=30)
            tpl = await get_prompt_for_project(project_id, "diff.srs")
            sys_p = (tpl or "").format(srs_a=srs_a[:10000], srs_b=srs_b[:10000])
            r = await fabric_call(
                messages=[{"role": "system", "content": sys_p},
                          {"role": "user", "content": "Produce the diff."}],
                agent_key="diff.srs", project_id=project_id,
                max_tokens=4000, temperature=0.1, timeout=120.0,
            )
            content = (r.get("content") or "").strip()
            art_id = await _save_artifact(project_id, "srs_diff", [{"path": "srs_diff.md", "content": content}],
                                          {"model": r.get("model")})
            _job_complete(jid, {"artifact_id": art_id, "kind": "srs_diff"})
        except Exception as e:
            _job_error(jid, str(e)[:500])

    asyncio.create_task(_run())
    return {"job_id": jid, "status": "queued"}


# ─── CRUD for artifacts ──────────────────────────────────────────────
@router.get("/{project_id}/artifacts")
async def list_artifacts(project_id: str):
    rows = await living_artifacts.find({"project_id": project_id}, {"_id": 0, "files": 0}).to_list(100)
    return {"artifacts": rows, "count": len(rows)}


@router.get("/{project_id}/artifact/{artifact_id}")
async def get_artifact(project_id: str, artifact_id: str):
    a = await living_artifacts.find_one({"id": artifact_id, "project_id": project_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    return a


@router.put("/{project_id}/artifact/{artifact_id}")
async def update_artifact(project_id: str, artifact_id: str, payload: dict):
    files = payload.get("files")
    if not isinstance(files, list):
        raise HTTPException(400, "files (list) required")
    now = datetime.now(timezone.utc).isoformat()
    r = await living_artifacts.update_one(
        {"id": artifact_id, "project_id": project_id, "frozen": {"$ne": True}},
        {"$set": {"files": files, "updated_at": now}, "$inc": {"version": 1}},
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Artifact not found or frozen")
    return {"ok": True}


@router.post("/{project_id}/artifact/{artifact_id}/freeze")
async def freeze_artifact(project_id: str, artifact_id: str):
    now = datetime.now(timezone.utc).isoformat()
    r = await living_artifacts.update_one(
        {"id": artifact_id, "project_id": project_id},
        {"$set": {"frozen": True, "frozen_at": now, "updated_at": now}},
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Artifact not found")
    return {"ok": True}


@router.post("/{project_id}/artifact/{artifact_id}/download")
@router.get("/{project_id}/artifact/{artifact_id}/download")
async def download_artifact(project_id: str, artifact_id: str):
    a = await living_artifacts.find_one({"id": artifact_id, "project_id": project_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    files = a.get("files", [])
    if len(files) == 1:
        f = files[0]
        body = (f.get("content") or "").encode("utf-8")
        return StreamingResponse(io.BytesIO(body), media_type="text/plain",
                                 headers={"Content-Disposition": f"attachment; filename={f['path'].split('/')[-1]}"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(f.get("path", "file.txt"), f.get("content", ""))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename={a['kind']}_{project_id[:8]}.zip"})


@router.post("/{project_id}/freeze")
async def freeze_stage(project_id: str):
    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.Living": "frozen", "updated_at": now}},
    )
    return {"ok": True}


@router.post("/{project_id}/reset")
async def reset_stage(project_id: str):
    await living_artifacts.delete_many({"project_id": project_id})
    await living_runs.delete_many({"project_id": project_id})
    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.Living": "available", "updated_at": now}},
    )
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
# iter-13.70 — Accuracy Report (KB vs latest generated code/docs)
#
# Multi-model section-by-section confidence scoring of every stage's
# latest artifacts against the Knowledge Base ground truth. Surfaces
# per-section accuracy + confidence + click-through deep-links so users
# can jump straight to the stage that needs regeneration.
# ═══════════════════════════════════════════════════════════════════════

# Stage routing for the UI "Regenerate this section" deep-link.
_STAGE_TO_FRONTEND_ROUTE = {
    "Discovery":    "/",
    "DataModel":    "/data-model",
    "Architecture": "/architecture",
    "CodeGen":      "/code-gen",
    "Living":       "/living",
}

# Section catalogue — one row per LOGICAL section we score across the
# whole pipeline. Each entry tells the evaluator (a) what to look for,
# (b) which stage owns it (for click-through), and (c) which artifact
# bucket(s) to pull text from.
_REPORT_SECTIONS: list[dict] = [
    # ── Discovery / SRS ──
    # iter-14.23 — Replaced the 6 business-oriented rows (workflow,
    # business_rules, approval_flow, user_manual, actors, nfr) with the
    # 12 IEEE-830 SRS sections defined in `routes/srs.py::SECTION_CONFIGS`
    # so the confidence popover names match the numbered sections a
    # migration architect sees in the SRS panel. Each row's key + label
    # follows SECTION_CONFIGS 1:1, and `source_keys` is a single-element
    # list pointing at the matching SRS bucket — so the auto-improve
    # regenerator (via `_srs_keys_for_report_row`) targets exactly the
    # row the operator clicked.
    {"key": "introduction",              "label": "1. Introduction",
     "stage": "Discovery", "source_keys": ["srs.introduction"],
     "what_to_check": "Purpose, scope, definitions, references and overview are complete, cite the correct legacy stack, and match the KB — nothing invented."},
    {"key": "overall_description",       "label": "2. Overall Description",
     "stage": "Discovery", "source_keys": ["srs.overall_description"],
     "what_to_check": "Product perspective, functions, user classes, operating environment, constraints, assumptions and dependencies are grounded in the KB and cover every user-visible legacy module."},
    {"key": "actors_use_case_inventory", "label": "3. Actors & Use Case Inventory",
     "stage": "Discovery", "source_keys": ["srs.actors_use_case_inventory"],
     "what_to_check": "Every legacy role (kb_entities.ROLE) is enumerated with plain-English description + source evidence, and every discoverable use case appears in the UC inventory with a stable UC-* id."},
    {"key": "specific_requirements",     "label": "4. Specific Requirements",
     "stage": "Discovery", "source_keys": ["srs.specific_requirements"],
     "what_to_check": "Functional requirements + the full business-rules catalogue (branching guards / validators / DB constraints / status transitions / authz / calculations / audit / notifications / config flags / scheduled jobs / idempotency) are enumerated with verbatim semantics and `path::symbol` citations."},
    {"key": "detailed_use_cases",        "label": "5. Detailed Use Cases",
     "stage": "Discovery", "source_keys": ["srs.detailed_use_cases"],
     "what_to_check": "Every UC has Narrative + Preconditions + Postconditions + Business Workflow (≥5 numbered steps for non-trivial UCs) + Business Rules subsections. Pre/Post cite guarding symbols/DB constraints; NOT_EVIDENCED rows carry evidence-gap markers."},
    {"key": "external_interfaces",       "label": "6. External Interfaces",
     "stage": "Discovery", "source_keys": ["srs.external_interfaces"],
     "what_to_check": "UI, hardware, software and communication interfaces are documented — every screen/nav path/validation surface + every downstream API/queue/DB visible in the legacy code."},
    {"key": "non_functional_requirements", "label": "7. Non-Functional Requirements",
     "stage": "Discovery", "source_keys": ["srs.non_functional_requirements"],
     "what_to_check": "Performance, security, availability, accessibility, i18n and audit NFRs cover every concern the KB hints at (login, audit trail, role-based visibility, session management, etc)."},
    {"key": "integration_requirements",  "label": "8. Integration Requirements",
     "stage": "Discovery", "source_keys": ["srs.integration_requirements"],
     "what_to_check": "Every external system the legacy app talks to (SSO / SMTP / payment / storage / third-party APIs) is listed with protocol, data-shape and error contract."},
    {"key": "validation_verification",   "label": "9. Validation & Verification",
     "stage": "Discovery", "source_keys": ["srs.validation_verification"],
     "what_to_check": "Test strategy, acceptance criteria and verification method per requirement class are defined — no requirement lacks a V&V approach."},
    {"key": "traceability_matrix",       "label": "10. Traceability Matrix",
     "stage": "Discovery", "source_keys": ["srs.traceability_matrix"],
     "what_to_check": "Every BR-* / UC-* / FR-* / NFR-* has a row linking it to the source legacy artifact (file::symbol or table.column) AND to the SRS section that specifies it. No orphans."},
    {"key": "appendices",                "label": "11. Appendices",
     "stage": "Discovery", "source_keys": ["srs.appendices"],
     "what_to_check": "Data dictionary, glossary, supporting diagrams and reference documents are populated where the KB has raw material for them."},
    {"key": "entity_model",              "label": "12. Entity Relationship Model",
     "stage": "Discovery", "source_keys": ["srs.entity_model"],
     "what_to_check": "Every business-critical entity (kb_entities.TABLE) appears with attributes, PKs, FKs and cardinality, matching the legacy schema — no invented entities or relationships."},
    # ── DataModel ──
    {"key": "data_model_oltp",   "label": "OLTP data model",
     "stage": "DataModel", "source_keys": ["datamodel.oltp_ddl", "kb.tables"],
     "what_to_check": "Every business-critical table in kb_entities.TABLE appears in OLTP DDL with the right columns, PKs, FKs and constraints. No invented tables."},
    {"key": "data_model_olap",   "label": "OLAP / star schema",
     "stage": "DataModel", "source_keys": ["datamodel.olap_ddl", "datamodel.bus_matrix"],
     "what_to_check": "Bus matrix's facts + dimensions match the OLAP DDL; every fact has at least one dimension and a date dimension. No dangling references."},
    # ── Architecture ──
    # iter-14.28 — HLD / LLD promoted from `service_map.source_keys` to
    # dedicated confidence rows so the operator can see per-artifact
    # scores in the Architecture confidence popover and Regenerate hits
    # exactly the artifact whose score is low. Same LangGraph + HF engine
    # scores all 5 rows (`compute_stage_confidence` routes through
    # `score_artifact_multi_model_langgraph` for every stage that appears
    # in `_STAGE_CONFIDENCE_STAGES`).
    {"key": "service_map",       "label": "Service map",
     "stage": "Architecture", "source_keys": ["arch.service_map"],
     "what_to_check": "Each service owns a clear bounded context, with explicit dependencies + datastore + role boundaries traceable to KB modules. Every kb_entities.CLASS is attributed to exactly one service."},
    {"key": "hld",               "label": "High-Level Design",
     "stage": "Architecture", "source_keys": ["arch.hld", "arch.service_map"],
     "what_to_check": "HLD explains the target topology (services, gateways, datastores, queues, external systems) with a diagram + prose. Every service in the service_map appears in the HLD; every legacy integration point is either preserved or explicitly retired with a rationale."},
    {"key": "lld",               "label": "Low-Level Design",
     "stage": "Architecture", "source_keys": ["arch.lld", "arch.service_map"],
     "what_to_check": "LLD covers per-service package layout, key classes/modules, data-access patterns, error handling, config, observability. Every non-trivial BR-* / UC-* from the SRS maps to a concrete class or module in the LLD."},
    {"key": "api_contract",      "label": "API contract",
     "stage": "Architecture", "source_keys": ["arch.api_contracts"],
     "what_to_check": "Every legacy route in kb_entities.ROUTE is represented as an OpenAPI operation with matching verb, path semantics, request/response shapes and role-guard tags."},
    {"key": "sequence_diagrams", "label": "Sequence diagrams",
     "stage": "Architecture", "source_keys": ["arch.sequence_diagrams"],
     "what_to_check": "Each major use case (UC-*) has a sequence diagram with the right actors, services, calls and error branches."},
    # ── CodeGen ──
    {"key": "backend_code",      "label": "Backend code (parity vs legacy)",
     "stage": "CodeGen", "source_keys": ["codegen.backend_files", "kb.classes"],
     "what_to_check": "Generated backend files cover every business module in kb_entities.CLASS (or the chosen subset), preserve all BR-* rules and role checks visible in legacy, and don't invent endpoints."},
    {"key": "frontend_code",     "label": "Frontend code (UI parity)",
     "stage": "CodeGen", "source_keys": ["codegen.frontend_files", "kb.routes"],
     "what_to_check": "Generated UI components match the screens, fields and validation rules visible in legacy JSP/HTML/PHP/CSHTML + honour the user-supplied theme brief (Figma / mockups)."},
    {"key": "tests",             "label": "Tests",
     "stage": "CodeGen", "source_keys": ["codegen.test_files"],
     "what_to_check": "Each major use case has at least one happy-path test + one unauthorised-role test + one validation-failure test."},
    # iter-14.50 — Detailed test-case matrix (Living stage). Scored
    # against the KB via the standard LangGraph/multi-model evaluator.
    {"key": "test_cases",        "label": "Detailed Test Cases",
     "stage": "Living", "source_keys": ["living.test_cases"],
     "what_to_check": "Every legacy screen and API endpoint in the KB has ≥1 positive, ≥1 negative, and ≥1 validation-failure test case with clear Steps, Test_Data, Expected_Result, SRS_UC_Ref and Legacy_Ref. No fabricated modules."},
]


async def _kb_summary_for_eval(project_id: str, max_chars: int = 5500) -> str:
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0}) or {}
    stats = toon_doc.get("stats") or {}
    summary = toon_doc.get("summary") or ""
    toon = (toon_doc.get("toon") or "")[:max_chars - 800]
    head = (
        "KB STATS: " + json.dumps(stats, separators=(",", ":"))[:600] + "\n"
        + (summary[:600] + "\n" if summary else "")
        + "\nTOON SLICE:\n" + toon
    )
    return head[:max_chars]


async def _ground_truth_for_eval(project_id: str, max_chars: int = 7500) -> str:
    """Pull the most authoritative KB digests: legacy analysis + module
    inventory + source inventory + role table. This is what the evaluator
    judges AGAINST."""
    parts: list[str] = []
    try:
        analysis = await legacy_analysis_col.find_one({"project_id": project_id}, {"_id": 0}) or {}
        a = (analysis or {}).get("analysis") or {}
        if a:
            parts.append("── LEGACY ANALYSIS DIGEST ──")
            for bucket in ("workflows", "business_rules", "user_journeys", "integrations", "actors"):
                items = (a.get(bucket) or [])[:8]
                if not items:
                    continue
                parts.append(f"\n[{bucket}]")
                for it in items:
                    parts.append("  - " + json.dumps(it, ensure_ascii=False)[:240])
    except Exception:  # noqa: BLE001
        pass
    try:
        roles = await kb_entities.find(
            {"project_id": project_id, "type": "ROLE"},
            {"_id": 0, "name": 1},
        ).to_list(40)
        if roles:
            parts.append("\n── ROLES ──")
            parts.append("  " + ", ".join((r.get("name") or "") for r in roles)[:600])
    except Exception:  # noqa: BLE001
        pass
    try:
        from routes.kb import get_source_inventory_block
        inv = await get_source_inventory_block(project_id)
        if inv:
            parts.append("\n" + inv)
    except Exception:  # noqa: BLE001
        pass
    out = "\n".join(parts)
    return out[:max_chars]


async def _collect_artifact_text(project_id: str, source_keys: list[str], max_chars: int = 28000) -> str:
    """Pull the latest text for one of the catalogue's section buckets.

    iter-13.78 fixes:
      • `max_chars` doubled (14k → 28k) — the BR + UC catalog rows pull
        2-3 SRS sections totalling ~30-40k chars; the old 14k clip cut
        the per-UC BR + Pre/Post tables off, forcing the evaluator to
        score the section as "missing".
      • `srs.<name>` lookups now honour `routes.srs.LEGACY_SECTION_ALIAS`
        so projects whose SRS docs still carry pre-iter-13 section keys
        (`functional_requirements`, `business_rules`, `use_cases`,
        `user_interfaces`) still resolve to real artifact text.
      • Per-source slicing is fair-share (each requested key gets up to
        `max_chars / N` chars) so a single huge section can't starve
        sibling sections of context.
    """
    # Build the alias resolver once; we look up canonical → legacy and
    # legacy → canonical so either direction works.
    try:
        from routes.srs import LEGACY_SECTION_ALIAS as _ALIAS  # legacy → new
    except Exception:  # noqa: BLE001 — never block the eval on import error
        _ALIAS = {}
    _REVERSE_ALIAS: dict[str, list[str]] = {}
    for old, new in (_ALIAS or {}).items():
        _REVERSE_ALIAS.setdefault(new, []).append(old)

    def _resolve_srs(name: str, sections: dict) -> str:
        # 1. exact key (post-iter-13 SRS docs)
        txt = (sections.get(name) or "").strip()
        if txt:
            return txt
        # 2. reverse-alias (e.g. specific_requirements → functional_requirements)
        for legacy in _REVERSE_ALIAS.get(name, []):
            txt = (sections.get(legacy) or "").strip()
            if txt:
                return txt
        # 3. forward-alias (rare — legacy key supplied directly)
        forward = _ALIAS.get(name)
        if forward:
            txt = (sections.get(forward) or "").strip()
            if txt:
                return txt
        return ""

    pieces: list[str] = []
    srs_sections: dict = {}
    if any(k.startswith("srs.") for k in source_keys):
        srs = await srs_documents.find_one({"project_id": project_id}, {"_id": 0}) or {}
        srs_sections = (srs.get("sections") or {})

    # Fair-share budget per source so one huge section can't dominate.
    n = max(1, len([k for k in source_keys if not k.startswith("kb.")]))
    per_source_cap = max(4000, max_chars // n)

    for k in source_keys:
        if k.startswith("srs."):
            name = k.split(".", 1)[1]
            txt = _resolve_srs(name, srs_sections)
            if txt:
                if len(txt) > per_source_cap:
                    txt = txt[:per_source_cap] + "\n… (section truncated for evaluation)"
                pieces.append(f"### SRS::{name}\n{txt}")
        elif k.startswith("datamodel."):
            name = k.split(".", 1)[1]
            try:
                if name == "oltp_ddl":
                    art = await data_models_col.find_one(
                        {"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0}
                    ) or {}
                    txt = (art.get("content") or "").strip()
                    if txt:
                        pieces.append(f"### OLTP_DDL\n{txt}")
                elif name == "olap_ddl":
                    art = await data_models_col.find_one(
                        {"project_id": project_id, "type": "olap_ddl"}, {"_id": 0}
                    ) or {}
                    txt = (art.get("content") or "").strip()
                    if txt:
                        pieces.append(f"### OLAP_DDL\n{txt}")
                elif name == "bus_matrix":
                    art = await data_models_col.find_one(
                        {"project_id": project_id, "type": "bus_matrix"}, {"_id": 0}
                    ) or {}
                    txt = (art.get("content") or "").strip()
                    if txt:
                        pieces.append(f"### BUS_MATRIX\n{txt[:3500]}")
            except Exception:  # noqa: BLE001
                pass
        elif k.startswith("arch."):
            name = k.split(".", 1)[1]
            try:
                if name == "service_map":
                    rows = await arch_services.find({"project_id": project_id}, {"_id": 0}).to_list(80)
                    if rows:
                        pieces.append("### SERVICE_MAP\n" + json.dumps(rows, indent=2)[:4000])
                else:
                    doc = await arch_documents.find_one(
                        {"project_id": project_id, "type": name}, {"_id": 0}
                    ) or {}
                    txt = (doc.get("content") or "").strip()
                    if txt:
                        pieces.append(f"### ARCH::{name}\n{txt}")
            except Exception:  # noqa: BLE001
                pass
        elif k.startswith("codegen."):
            name = k.split(".", 1)[1]
            try:
                # iter-14.42 — Field is `file_path` in codegen_files,
                # not `path`. The old queries always matched zero rows,
                # which is why the Living-Diff report showed "Frontend
                # code 0.0% — Artifact not generated yet" even after
                # 215 FE files landed on disk.
                query: dict = {"project_id": project_id}
                if name == "backend_files":
                    query["file_path"] = {"$not": re.compile(r"^frontend/")}
                elif name == "frontend_files":
                    query["file_path"] = re.compile(r"^frontend/")
                elif name == "test_files":
                    query["file_path"] = re.compile(r"(test|spec)", re.IGNORECASE)
                cur = codegen_files.find(
                    query, {"_id": 0, "file_path": 1, "content": 1}
                ).limit(8)
                async for f in cur:
                    body = (f.get("content") or "")[:2500]
                    pieces.append(f"### FILE: {f.get('file_path', '?')}\n{body}")
            except Exception:  # noqa: BLE001
                pass
        elif k.startswith("living."):
            # iter-14.50 — Pull a Living artifact (e.g. detailed test
            # cases) into the evaluator context.
            name = k.split(".", 1)[1]
            try:
                art = await living_artifacts.find_one(
                    {"project_id": project_id, "kind": name}, {"_id": 0},
                ) or {}
                parts_txt: list[str] = []
                for f in (art.get("files") or [])[:4]:
                    fp = f.get("path", "?")
                    body = (f.get("content") or "")[:12000]
                    parts_txt.append(f"### {fp}\n{body}")
                if parts_txt:
                    pieces.append("\n\n".join(parts_txt))
            except Exception:  # noqa: BLE001
                pass
        elif k.startswith("kb."):
            # KB-only sections — the evaluator already has the KB summary
            # block, so this is a no-op marker.
            continue

    out = "\n\n".join(pieces).strip()
    if not out:
        return ""
    if len(out) > max_chars:
        out = out[:max_chars] + "\n… (truncated)"
    return out


async def _run_accuracy_report(jid: str, project_id: str, only_sections: Optional[list[str]] = None):
    try:
        _job_update(jid, status="running", step="Loading KB digests…", pct=8)
        kb_summary = await _kb_summary_for_eval(project_id)
        ground_truth = await _ground_truth_for_eval(project_id)

        _job_update(jid, step="Picking evaluator models…", pct=15)
        models = await pick_evaluator_models()

        _job_update(jid, step="Loading CodeGen parity snapshot…", pct=18)        # iter-14.48 — Pull one deterministic parity snapshot for the
        # three CodeGen rows (backend_code / frontend_code / tests) so
        # they mirror the outer confidence pill and never disagree with
        # it. Fully replaces the LLM-eval path for those rows.
        # iter-14.49 — Use `prefer_fast=True` so a cold cache falls
        # through to the persisted parity_runs snapshot instead of
        # blocking the report for 3-4 min on a fresh score_run (that
        # was pinning the job at 15% "Picking evaluator models…"
        # every time the backend restarted). We ALSO warm the cache
        # in the background so the NEXT re-run gets a live number.
        parity_snap = None
        try:
            from routes.pipeline import (
                get_codegen_parity_snapshot as _snap,
                _CODEGEN_PARITY_CACHE as _cache,
                _CODEGEN_PARITY_TTL_SECS as _ttl,
            )
            import time as _time
            parity_snap = await _snap(project_id, threshold=95.0, prefer_fast=True)
            _cached = _cache.get(project_id)
            _stale = (not _cached) or ((_time.time() - _cached["ts"]) >= _ttl)
            if _stale:
                async def _warm():
                    try:
                        await _snap(project_id, threshold=95.0, prefer_fast=False)
                    except Exception:  # noqa: BLE001
                        pass
                asyncio.create_task(_warm())
        except Exception as _pe:  # noqa: BLE001
            logger.warning("accuracy-report[%s] parity snapshot failed: %s", project_id, _pe)

        sections_to_score = [s for s in _REPORT_SECTIONS
                             if not only_sections or s["key"] in set(only_sections)]
        section_results: list[dict] = []
        total = len(sections_to_score)
        _CODEGEN_PARITY_KEYS = {"backend_code", "frontend_code", "tests"}

        for i, sec in enumerate(sections_to_score, start=1):
            pct = 20 + int(70 * i / max(1, total))
            _job_update(jid, step=f"Scoring · {sec['label']}", pct=pct)

            # ── CodeGen rows: use deterministic parity_loop, not LLM eval ──
            if sec["key"] in _CODEGEN_PARITY_KEYS and parity_snap is not None:
                if sec["key"] == "backend_code":
                    files = parity_snap.get("backend_files") or []
                    score = float(parity_snap.get("backend_score") or 0.0)
                elif sec["key"] == "frontend_code":
                    files = parity_snap.get("frontend_files") or []
                    score = float(parity_snap.get("frontend_score") or 0.0)
                else:  # tests
                    files = parity_snap.get("test_files") or []
                    score = float(parity_snap.get("tests_score") or 0.0)
                if not files:
                    # iter-14.49 — Persisted parity_runs may only carry a
                    # subset of services (e.g. a run that scored only
                    # frontend files). Rather than misleading the user
                    # with 0.0% for the missing category, fall through
                    # to the LLM eval path below so they at least get a
                    # KB-vs-artifact score. Background cache warm will
                    # fix this on the next re-run.
                    logger.info(
                        "accuracy-report[%s] parity_snap has no files "
                        "for %s (source=%s) — falling back to LLM eval",
                        project_id, sec["key"], parity_snap.get("source"),
                    )
                else:
                    threshold = float(parity_snap.get("threshold") or 95.0)
                    below = [f for f in files if float(f.get("score") or 0.0) < threshold]
                    top_gap = ""
                    evidence: list[str] = []
                    if below:
                        worst = min(below, key=lambda f: float(f.get("score") or 0.0))
                        comps = worst.get("components") or []
                        weakest = min(comps, key=lambda c: float(c.get("score") or 0.0)) if comps else {}
                        _p = str(worst.get("file_path") or "")
                        _short = _p.rsplit("/", 1)[-1] if _p else ""
                        top_gap = (
                            f"{_short}: {weakest.get('name','?')} "
                            f"{float(weakest.get('score') or 0.0):.0f}% — "
                            f"{(weakest.get('detail') or '')[:80]}"
                        )
                        evidence = [str(f.get("file_path") or "") for f in below[:5]]
                    if score >= 95:
                        band = "excellent"
                    elif score >= 85:
                        band = "good"
                    elif score >= 70:
                        band = "fair"
                    else:
                        band = "poor"
                    section_results.append({
                        "key": sec["key"], "label": sec["label"],
                        "stage": sec["stage"],
                        "stage_route": _STAGE_TO_FRONTEND_ROUTE.get(sec["stage"], "/"),
                        "score": round(score, 2),
                        "band": band,
                        "rationale": (
                            f"parity_loop deterministic score over {len(files)} file(s); "
                            f"{len(below)} below {threshold:.0f}% threshold "
                            f"(source={parity_snap.get('source')})."
                        ),
                        "gaps": [top_gap] if top_gap else [],
                        "evidence": evidence,
                        "votes": [{"model": "parity_loop", "score": round(score, 2)}],
                        "model_agreement_spread": 0.0,
                        "missing": False,
                        "engine": "parity_loop",
                    })
                    continue

            artifact_text = await _collect_artifact_text(project_id, sec.get("source_keys", []))
            if not artifact_text:
                # Nothing generated yet for this section. Record an explicit
                # "missing" row so the user knows what to address.
                section_results.append({
                    "key": sec["key"], "label": sec["label"],
                    "stage": sec["stage"], "stage_route": _STAGE_TO_FRONTEND_ROUTE.get(sec["stage"], "/"),
                    "score": 0.0, "band": "poor",
                    "rationale": "No artifact generated yet for this section.",
                    "gaps": ["Run the owning stage to produce this section"],
                    "evidence": [],
                    "votes": [],
                    "model_agreement_spread": 0.0,
                    "missing": True,
                })
                continue
            # Treat the single section as a one-row scoring request so the
            # evaluator focuses tightly on this concern.
            # iter-14.15 — LangGraph + HF engine when the resolver picks
            # it (explicit env or iter-14.29 auto-fallback when Factory.ai
            # is not enabled / not connected); transparent fallback to the
            # existing `score_artifact_multi_model` symbol on any failure.
            # Preserves monkeypatch surface for legacy tests.
            try:
                from confidence_langgraph import resolve_confidence_engine as _resolve_ce
                _use_langgraph = _resolve_ce() == "langgraph"
            except Exception:  # noqa: BLE001
                _use_langgraph = (
                    (os.environ.get("LAMA_CONFIDENCE_ENGINE") or "").strip().lower()
                    == "langgraph"
                )
            verdict = None
            if _use_langgraph:
                try:
                    from confidence_langgraph import (  # noqa: E402
                        is_available as _clg_available,
                        score_artifact_multi_model_langgraph as _clg_multi,
                    )
                    if _clg_available():
                        verdict = await _clg_multi(
                            project_id=project_id,
                            stage=sec["stage"],
                            artifact_text=artifact_text,
                            sections=[{"key": sec["key"], "label": sec["label"],
                                       "what_to_check": sec["what_to_check"]}],
                            kb_summary=kb_summary,
                            ground_truth=ground_truth,
                            models=models,
                        )
                        if not (verdict.get("sections") or []):
                            verdict = None
                except Exception as _exc:  # noqa: BLE001
                    logger.warning(
                        "living[%s] · langgraph engine failed (%s) — "
                        "falling back to fabric path",
                        project_id, _exc,
                    )
                    verdict = None
            if verdict is None:
                # iter-14.21 — Strict-HF guard for the living-diff scorer:
                # never fall through to fabric_call → Factory CLI when the
                # engine is HF-only. Record a marked-missing section row
                # (so overall stage score reflects the outage) and skip.
                try:
                    from confidence_langgraph import (
                        strict_hf_only as _strict,
                    )
                except Exception:  # noqa: BLE001
                    _strict = lambda: False  # noqa: E731
                if _strict():
                    logger.warning(
                        "living[%s] · strict-HF mode ON — refusing fabric "
                        "fallback for %s; recording missing row.",
                        project_id, sec.get("key"),
                    )
                    section_results.append({
                        "key": sec["key"], "label": sec["label"],
                        "stage": sec["stage"],
                        "stage_route": _STAGE_TO_FRONTEND_ROUTE.get(sec["stage"], "/"),
                        "score": 0.0, "band": "poor",
                        "rationale": (
                            "Confidence engine unavailable and strict-HF "
                            "mode is on — skipped Factory/OpenRouter fallback."
                        ),
                        "gaps": [], "evidence": [], "votes": [],
                        "model_agreement_spread": 0.0,
                        "missing": True,
                        "engine": "langgraph",
                        "route_taken": "engine_unavailable",
                    })
                    continue
                verdict = await score_artifact_multi_model(
                    project_id=project_id,
                    stage=sec["stage"],
                    artifact_text=artifact_text,
                    sections=[{"key": sec["key"], "label": sec["label"],
                               "what_to_check": sec["what_to_check"]}],
                    kb_summary=kb_summary,
                    ground_truth=ground_truth,
                    models=models,
                )
            row = (verdict.get("sections") or [{}])[0]
            row.update({
                "stage": sec["stage"],
                "stage_route": _STAGE_TO_FRONTEND_ROUTE.get(sec["stage"], "/"),
                "missing": False,
            })
            section_results.append(row)

        # Overall = mean of non-missing rows so missing artifacts pull the
        # number down sharply (intentional — drives the user to fill gaps).
        scores = [r["score"] for r in section_results]
        overall = round(sum(scores) / len(scores), 2) if scores else 0.0
        below = [r for r in section_results if r["score"] < 95.0]

        report = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "overall_score": overall,
            "overall_band": band_of(overall),
            "models_used": models,
            "section_count": len(section_results),
            "sections_below_95": len(below),
            "sections": section_results,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        _job_update(jid, step="Persisting report…", pct=95)
        await living_reports.insert_one(report)
        # Keep only the last 10 reports per project
        try:
            old = await living_reports.find(
                {"project_id": project_id},
                {"_id": 0, "id": 1, "generated_at": 1},
            ).sort("generated_at", -1).to_list(50)
            if len(old) > 10:
                stale_ids = [r["id"] for r in old[10:]]
                await living_reports.delete_many({"project_id": project_id, "id": {"$in": stale_ids}})
        except Exception:  # noqa: BLE001
            pass

        _job_complete(jid, {"report_id": report["id"], "overall_score": overall,
                            "sections_below_95": len(below)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("accuracy-report job failed for %s: %s", project_id, exc)
        _job_error(jid, str(exc)[:500])


@router.post("/{project_id}/jobs/start/accuracy-report")
async def start_accuracy_report(project_id: str, payload: Optional[dict] = None):
    """Kick off a multi-model accuracy + confidence report job.

    Body (optional): ``{"sections": ["workflow", "business_rules", ...]}``
    to score a subset only.
    """
    payload = payload or {}
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    # NOTE: we deliberately do NOT require CodeGen to be frozen — the
    # report is most useful WHILE the user is iterating, so they can see
    # confidence climb before they freeze.
    only = payload.get("sections")
    if only is not None and not isinstance(only, list):
        raise HTTPException(400, "`sections` must be a list of section keys when provided")
    jid = _new_job(project_id, "accuracy-report")
    asyncio.create_task(_run_accuracy_report(jid, project_id, only))
    return {"job_id": jid, "status": "queued"}


@router.get("/{project_id}/accuracy-report/latest")
async def get_latest_accuracy_report(project_id: str):
    """Return the most recent persisted accuracy report (or 404)."""
    doc = await living_reports.find_one(
        {"project_id": project_id},
        {"_id": 0},
        sort=[("generated_at", -1)],
    )
    if not doc:
        raise HTTPException(404, "No accuracy report yet — start one first.")
    return doc


@router.get("/{project_id}/accuracy-report/sections")
async def list_accuracy_sections():
    """Static catalogue (UI bootstrap) — what the report will score."""
    return {
        "sections": [
            {"key": s["key"], "label": s["label"], "stage": s["stage"],
             "stage_route": _STAGE_TO_FRONTEND_ROUTE.get(s["stage"], "/")}
            for s in _REPORT_SECTIONS
        ],
    }


