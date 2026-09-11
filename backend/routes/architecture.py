"""Stage 3 — Architecture.

Pipeline-gated by Stage 2 (DataModel) frozen.
Long-running LLM calls run as background jobs (in-memory _JOBS registry, polled by frontend).
"""
import io
import json
import os
import re
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from db import (
    projects,
    audit_log,
    prompts as prompts_col,
    project_prompts,
    arch_documents,
    arch_services,
    kb_entities,
    messages as messages_col,
    conversations,
    stage_context as stage_context_col,
    data_models,
    srs_documents,
    # iter-14.21 — merge cleanup needs to sweep stale CodeGen artifacts
    # for source services that no longer exist (see
    # _cleanup_merged_service_artifacts).
    codegen_files,
    codegen_runs,
)
from llm import fabric_call as chat_completion
from llm import fabric_call_with_session  # iter-13.100 — rolling-memory sessions
from llm import set_current_project_id, set_current_agent_key  # iter-13.38 / iter-13.81.3 — Factory.ai context propagation
from llm import TransportError  # iter-13.50 — abort jobs cleanly on DNS / connect failure
from fabric.model_fabric import _is_billing_error, _is_auth_error  # iter-13.51 — abort on 402/401 too
from kb.vector_store import search as qdrant_search
from kb.legacy_tables import build_oltp_ddl_from_kb  # iter-14.33 — legacy-table harvest
from pipeline import require_stage_context, save_stage_context, get_module_context
# iter-14.20 — deterministic Python generators for HLD / LLD / API Contracts.
# These replace the LLM-driven jobs at the bottom of this file. The
# `arch.hld`, `arch.lld`, `arch.api_contracts` prompts are left in seed.py
# unused so a future revert is a one-line change.
from arch_deterministic import (
    render_hld as _render_hld_deterministic,
    render_lld as _render_lld_deterministic,
    render_api_contracts as _render_api_contracts_deterministic,
)
# iter-13.59 — cross-cutting utility services (audit logger, etc.)
from codegen.utility_templates import UTILITY_CATALOG, get_utility_meta

logger = logging.getLogger("lama.architecture")
router = APIRouter(prefix="/architecture", tags=["architecture"])


# iter-14.20.2 — Hydrate services with legacy routes fetched fresh from the KB.
#
# WHY: The deterministic HLD / LLD / API-Contract renderers rely on
# ``arch_services.routes_detail`` which is populated during arch.recommend.
# If the user ran recommend BEFORE Build KB finished, or if the LLM
# clustering left modules unassigned, some services end up with an empty
# routes_detail block. Under the LLM design that was OK (LLM would
# invent something); under the deterministic design it silently produces
# empty OpenAPI paths.
#
# This hydrator:
#   1. Loads the current KB surface via `_enumerate_legacy_surface`.
#   2. For every service, if `routes_detail` is empty (or thinner than the
#      total routes owned by its `module_names`), it MERGES in the routes
#      from those modules — never removes existing ones.
#   3. Computes the set of KB routes that ended up unassigned to any
#      service and returns them so the caller can append a
#      "legacy-unsorted" synthetic service — guaranteeing 100 % of the
#      legacy surface lands in the API Contracts artifact.
#
# iter-14.20.3 — Short-lived (60s) per-project cache for the KB surface so
# LLD → API Contracts jobs, which run back-to-back, don't re-fetch tens
# of thousands of graph rows twice.
import time as _time
_SURFACE_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_SURFACE_CACHE_TTL_SEC = 60.0


async def _cached_legacy_surface(project_id: str) -> Dict[str, Any]:
    now = _time.time()
    hit = _SURFACE_CACHE.get(project_id)
    if hit and (now - hit[0]) < _SURFACE_CACHE_TTL_SEC:
        return hit[1]
    surface = await _enumerate_legacy_surface(project_id)
    _SURFACE_CACHE[project_id] = (now, surface)
    return surface


async def _hydrate_services_with_legacy_routes(
    project_id: str,
    services: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(enriched_services, meta)``.

    ``meta`` keys::
        {
          "surface_source":     "kb_graph" | "kb_entities" | "unavailable",
          "kb_total_routes":    int,
          "attached_before":    int,
          "attached_after":     int,
          "unassigned_routes":  [{"verb", "path", "class", "roles", "module"}, ...],
          "unassigned_modules": [str, ...],
          "coverage_pct":       float,
        }

    Every mutation is on a COPY of the service dict — the Mongo rows are
    left untouched. The caller can decide whether to append the synthetic
    "legacy-unsorted" service on top.
    """
    meta: Dict[str, Any] = {
        "surface_source": "unavailable",
        "kb_total_routes": 0,
        "attached_before": 0,
        "attached_after": 0,
        "unassigned_routes": [],
        "unassigned_modules": [],
        "coverage_pct": 0.0,
    }

    def _count(svc: Dict[str, Any]) -> int:
        return len(svc.get("routes_detail") or svc.get("api_endpoints") or [])

    attached_before = sum(_count(s) for s in services)
    meta["attached_before"] = attached_before

    try:
        await _ensure_kb_graph(project_id)
        surface = await _cached_legacy_surface(project_id)
    except Exception as e:
        logger.warning("hydrate: legacy surface fetch failed: %s", e)
        # Nothing to hydrate with — return a shallow copy of the input.
        return [dict(s) for s in services], meta

    kb_modules = surface.get("modules") or []
    meta["surface_source"] = surface.get("source") or ("kb_graph" if kb_modules else "unavailable")
    kb_total_routes = int(surface.get("total_routes") or sum(m.get("route_count", 0) for m in kb_modules))
    meta["kb_total_routes"] = kb_total_routes

    # Build module_name / module_id → routes lookup.
    mod_by_key: Dict[str, Dict[str, Any]] = {}
    for m in kb_modules:
        if m.get("name"):
            mod_by_key[str(m["name"]).strip().lower()] = m
        if m.get("id"):
            mod_by_key[str(m["id"]).strip().lower()] = m

    def _route_key(r: Dict[str, Any]) -> tuple:
        return ((r.get("verb") or "ANY").upper(), (r.get("path") or "").strip())

    all_kb_route_keys: set = set()
    for m in kb_modules:
        for r in m.get("routes") or []:
            all_kb_route_keys.add(_route_key(r))

    enriched: List[Dict[str, Any]] = []
    globally_assigned_keys: set = set()
    for svc in services:
        s = dict(svc)  # shallow copy — we'll mutate list fields
        existing_detail = list(s.get("routes_detail") or [])
        existing_keys: set = {_route_key(r) for r in existing_detail}

        module_refs = [
            str(x).strip().lower()
            for x in (s.get("module_names") or s.get("modules") or [])
            if x
        ]
        merged_new: List[Dict[str, Any]] = []
        added_tables: set = set(s.get("tables") or [])
        added_roles: set = set(s.get("roles") or [])
        touched_any_module = False
        for mref in module_refs:
            m = mod_by_key.get(mref)
            if not m:
                continue
            touched_any_module = True
            for r in m.get("routes") or []:
                k = _route_key(r)
                if k in existing_keys:
                    continue
                existing_keys.add(k)
                merged_new.append({
                    "verb":   (r.get("verb") or "ANY").upper(),
                    "path":   r.get("path") or "",
                    "class":  r.get("class") or "",
                    "roles":  r.get("roles") or [],
                    "module": m.get("name") or mref,
                })
            for t in m.get("tables") or []:
                added_tables.add(t)
            for role in m.get("roles") or []:
                added_roles.add(role)

        if merged_new:
            s["routes_detail"] = existing_detail + merged_new
            # Keep api_endpoints in sync as a compact list of "VERB path" strings.
            existing_flat = list(s.get("api_endpoints") or [])
            flat_seen: set = set(existing_flat)
            for r in merged_new:
                ep = f"{r['verb']} {r['path']}".strip()
                if ep and ep not in flat_seen:
                    flat_seen.add(ep)
                    existing_flat.append(ep)
            s["api_endpoints"] = existing_flat
            s["api_count"] = len(s["routes_detail"])
            s["route_count"] = len(s["routes_detail"])
        # iter-14.20.4 — Always union tables/roles from KB modules even when
        # we didn't merge any new routes. Previously the tables update sat
        # inside the `if merged_new:` guard, so a service whose modules
        # owned tables but no routes got no table attachment at all.
        if touched_any_module:
            s["tables"] = sorted(added_tables)
            s["roles"] = sorted(added_roles)

        globally_assigned_keys.update(_route_key(r) for r in s.get("routes_detail") or [])
        enriched.append(s)

    # Compute unassigned routes (in KB but not attached to any service).
    unassigned: List[Dict[str, Any]] = []
    unassigned_modules: set = set()
    for m in kb_modules:
        for r in m.get("routes") or []:
            k = _route_key(r)
            if k in globally_assigned_keys:
                continue
            unassigned.append({
                "verb":   (r.get("verb") or "ANY").upper(),
                "path":   r.get("path") or "",
                "class":  r.get("class") or "",
                "roles":  r.get("roles") or [],
                "module": m.get("name") or m.get("id") or "?",
            })
            if m.get("name"):
                unassigned_modules.add(m["name"])

    meta["attached_after"] = sum(_count(s) for s in enriched)
    meta["unassigned_routes"] = unassigned
    meta["unassigned_modules"] = sorted(unassigned_modules)
    if kb_total_routes > 0:
        meta["coverage_pct"] = round(
            100.0 * (meta["attached_after"]) / max(1, kb_total_routes), 1,
        )

    return enriched, meta


# iter-14.20.4 — DDL rescue.
#
# When the KB surface returns 0 routes AND services have no owned tables,
# the deterministic renderer used to abort. If the DataModel stage produced
# an OLTP DDL, we can still emit a valid CRUD API from those tables — the
# migration will have SOMETHING to code against. This helper:
#   1. Parses `CREATE TABLE <name>` statements from the DDL.
#   2. Fuzzy-matches each table to a service by comparing the table name to
#      the service's name/display_name/description/module_names.
#   3. Distributes matched tables into service.tables (union, no removal).
#   4. Returns any unassigned tables so the caller can bucket them into a
#      synthetic `legacy-crud` service.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(?:[a-zA-Z0-9_]+\.)?"
    r"([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s*\(",
    re.IGNORECASE,
)


def _parse_ddl_table_names(ddl: str) -> List[str]:
    if not ddl:
        return []
    seen: set = set()
    out: List[str] = []
    for m in _CREATE_TABLE_RE.finditer(ddl):
        n = m.group(1).strip().lower()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _stem(word: str) -> str:
    """Minimal plural-only stemmer so ``documents``/``document`` collide
    and ``policies``/``policy`` collide. Deliberately does NOT touch
    derivational suffixes (``-ment``, ``-tion``) to avoid stripping
    ``document`` → ``docu`` or ``notification`` → ``notifica``.
    """
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and not w.endswith(("ses", "xes", "zes")):
        # `boxes` → `box`, `notifications` handled by the trailing `s` rule
        # (already `notification` after strip). Keep the -es rule for
        # `endorses`/`processes`/`analyses` etc.
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


# iter-14.22 — API PARITY computation (extracted so tests can call it
# without spinning up the whole _run_recommend_job). Reads
# services[].routes_detail (legacy KB truth) and services[].api_endpoints
# ("VERB path" strings emitted by _expand_services_from_modules) and
# annotates each service with a `parity` block. Also stamps parsed with
# an aggregate `_parity` block for the pipeline / UI.
def _annotate_parity(parsed: Dict[str, Any], project_id: str = "",
                     logger_=None) -> Dict[str, Any]:
    """Compute per-service + aggregate API parity metadata IN-PLACE.

    Returns the aggregate ``_parity`` block for convenience.
    """
    totals = {"legacy": 0, "target": 0, "missing": 0}
    for svc in parsed.get("services") or []:
        legacy_routes = svc.get("routes_detail") or []
        legacy_keys = {
            (str(r.get("verb") or "ANY").upper(),
             str(r.get("path") or r.get("name") or ""))
            for r in legacy_routes
            if (r.get("path") or r.get("name"))
        }
        target_keys: set = set()
        for ep in svc.get("api_endpoints") or []:
            parts = str(ep).split(None, 1)
            if len(parts) == 2:
                target_keys.add((parts[0].upper(), parts[1]))
            elif len(parts) == 1:
                target_keys.add(("ANY", parts[0]))
        missing = sorted(f"{v} {p}" for (v, p) in (legacy_keys - target_keys))
        legacy_n = len(legacy_keys)
        covered_n = len(legacy_keys & target_keys)
        coverage_pct = round(100.0 * covered_n / legacy_n, 2) if legacy_n else 100.0
        svc["parity"] = {
            "legacy_endpoint_count": legacy_n,
            "target_endpoint_count": len(target_keys),
            "covered": covered_n,
            "missing": missing[:50],
            "missing_count": len(missing),
            "coverage_pct": coverage_pct,
            "pass": bool(legacy_n) and coverage_pct >= 100.0,
        }
        totals["legacy"] += legacy_n
        totals["target"] += len(target_keys)
        totals["missing"] += len(missing)
    agg_pct = (
        round(100.0 * (totals["legacy"] - totals["missing"]) /
              max(totals["legacy"], 1), 2)
        if totals["legacy"] else 100.0
    )
    parsed["_parity"] = {
        **totals,
        "aggregate_coverage_pct": agg_pct,
        "pass": totals["missing"] == 0 and totals["legacy"] > 0,
    }
    if logger_ is not None:
        logger_.info(
            "arch.recommend parity: project=%s legacy=%d target=%d missing=%d agg=%.2f%%",
            project_id, totals["legacy"], totals["target"], totals["missing"], agg_pct,
        )
    return parsed["_parity"]


def _service_lexicon(svc: Dict[str, Any]) -> set:
    """Bag-of-stemmed-words describing what a service is about."""
    parts: List[str] = []
    for k in ("name", "display_name", "description", "responsibility"):
        v = svc.get(k)
        if v:
            parts.append(str(v))
    for mn in svc.get("module_names") or []:
        parts.append(str(mn))
    for t in svc.get("tables") or []:
        parts.append(str(t))
    joined = " ".join(parts).lower()
    return {_stem(w) for w in re.findall(r"[a-z][a-z0-9]{2,}", joined)}


def _attach_ddl_tables(
    services: List[Dict[str, Any]], oltp_ddl: str,
) -> tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    """Fuzzy-attach OLTP DDL tables to services by shared stemmed tokens.

    Returns ``(services_with_tables, unassigned_tables, per_svc_added)``.
    Each service is mutated in-place; caller passes shallow copies.
    """
    table_names = _parse_ddl_table_names(oltp_ddl)
    if not table_names:
        return services, [], {}

    svc_lex = [(s, _service_lexicon(s)) for s in services]
    unassigned: List[str] = []
    per_svc_added: Dict[str, int] = {}

    for t in table_names:
        # tokens the DDL table itself contributes (split snake_case + stem)
        t_tokens = {_stem(w) for w in re.findall(r"[a-z][a-z0-9]+", t)}
        if not t_tokens:
            unassigned.append(t)
            continue
        best: tuple[int, Dict[str, Any] | None] = (0, None)
        for svc, lex in svc_lex:
            overlap = len(t_tokens & lex)
            if overlap > best[0]:
                best = (overlap, svc)
        if best[0] > 0 and best[1] is not None:
            svc = best[1]
            existing = set([str(x).lower() for x in svc.get("tables") or []])
            if t not in existing:
                svc["tables"] = sorted(set(svc.get("tables") or []) | {t})
                per_svc_added[svc.get("name") or "?"] = (
                    per_svc_added.get(svc.get("name") or "?", 0) + 1
                )
        else:
            unassigned.append(t)

    return services, unassigned, per_svc_added


def _synthetic_legacy_crud_service(tables: List[str]) -> Dict[str, Any] | None:
    """Bucket for OLTP tables that couldn't be matched to any service.

    The deterministic renderer will emit 5-verb CRUD paths per table.
    """
    if not tables:
        return None
    return {
        "name": "legacy-crud",
        "display_name": "Legacy — CRUD Bucket (auto-synthesised)",
        "description": (
            f"Auto-synthesised bucket: {len(tables)} OLTP table(s) had no "
            f"matching service — a canonical 5-verb CRUD surface is emitted "
            f"for each so migration still has an API to code against."
        ),
        "responsibility": "Bucket for unassigned OLTP tables (CRUD).",
        "tables": sorted(tables),
        "roles": [],
        "modules": [],
        "module_names": [],
        "dependencies": [],
        "api_endpoints": [],
        "routes_detail": [],
        "api_count": 0,
        "route_count": 0,
        "backend_lang": "python",
        "kind": "legacy_crud_bucket",
        "frontend": False,
    }


# iter-14.20.5 — Robust OLTP DDL loader.
# The frozen `stage_context.outputs.oltp_ddl` snapshot can end up empty if
# (a) DataModel was frozen before the OLTP artifact had content, (b) the
# snapshot only stored artifact IDs, or (c) the LLM produced comment-only
# output. Fall back to the live `data_models` collection where the OLTP
# artifact lives with its full `.content` field. Also merge in per-service
# `ddl_snippet` fields — some legacy-KB flows write DDL there when the
# global OLTP is empty.
#
# iter-14.20.6 — Add a FOURTH source: the legacy KB itself. At Build KB
# time we parse SQL dumps + PHP model queries and persist every discovered
# table into `kb_entities` with ``type=TABLE`` (see
# ``kb/owl_extractor.py::245``) carrying ``name``, ``pk``, ``columns``,
# ``fks``. Reconstruct a synthetic ``CREATE TABLE ...`` block from those
# rows so the deterministic OpenAPI renderer + DDL parser just work,
# even when DataModel never produced a real OLTP DDL.
_KB_SQL_TYPE_MAP = {
    "int": "INTEGER", "integer": "INTEGER", "bigint": "BIGINT",
    "smallint": "SMALLINT", "tinyint": "SMALLINT",
    "decimal": "NUMERIC", "numeric": "NUMERIC", "float": "REAL", "double": "DOUBLE PRECISION",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
    "date": "DATE", "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP",
    "time": "TIME", "year": "SMALLINT",
    "text": "TEXT", "longtext": "TEXT", "mediumtext": "TEXT", "tinytext": "TEXT",
    "blob": "BYTEA", "longblob": "BYTEA", "mediumblob": "BYTEA",
    "json": "JSONB", "jsonb": "JSONB",
    "uuid": "UUID",
}


def _kb_col_to_sql(col_type: str) -> str:
    if not col_type:
        return "TEXT"
    raw = str(col_type).strip().lower()
    # Strip length spec e.g. varchar(255)
    base = re.split(r"[\(\s]", raw, 1)[0]
    if base in ("varchar", "char", "nvarchar", "nchar"):
        # Preserve the length spec
        m = re.search(r"\((\d+)\)", raw)
        return f"VARCHAR({m.group(1)})" if m else "VARCHAR(255)"
    return _KB_SQL_TYPE_MAP.get(base, "TEXT")


# iter-14.20.7 — Table-scope filter.
#
# A live Oracle KB can enumerate tens of thousands of tables — most of
# them ETL dumps, snapshot / matched / temp / backup tables that were
# never part of the target application surface. Emitting 5 CRUD paths
# per table would blow up the OpenAPI artifact to double-digit MB and
# choke CodeGen. This helper prunes obvious junk and hard-caps the
# result to a workable spec size.
_JUNK_TABLE_PATTERNS = [
    re.compile(r"_bkp\d*$", re.I),
    re.compile(r"_backup\d*$", re.I),
    re.compile(r"_old\d*$", re.I),
    re.compile(r"_tmp\d*$", re.I),
    re.compile(r"_temp\d*$", re.I),
    re.compile(r"_bak\d*$", re.I),
    re.compile(r"_matched\b", re.I),
    re.compile(r"_dump\d*$", re.I),
    re.compile(r"_dumps?\b", re.I),
    re.compile(r"_snapshot\d*$", re.I),
    re.compile(r"_test\d*$", re.I),
    re.compile(r"^tmp_", re.I),
    re.compile(r"^backup_", re.I),
    re.compile(r"^old_", re.I),
    re.compile(r"\d{6,}$"),                    # trailing 6+ digits → date/dump
    re.compile(r"^aargo\d+$", re.I),           # dump codes seen in the CGHS corpus
    re.compile(r"^ehftest_", re.I),            # test-env prefix
    re.compile(r"^aadhar_(matched|nos)", re.I),  # matched/nos ETL variants
]

# Hard cap on the number of tables spec'd — anything above this becomes
# unworkable in the UI + CodeGen. Overridable via env-var so power users
# on smaller schemas can bypass.
_MAX_SPEC_TABLES = int(os.environ.get("LAMA_ARCH_MAX_SPEC_TABLES", "300"))


def _is_junk_table(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    for pat in _JUNK_TABLE_PATTERNS:
        if pat.search(n):
            return True
    return False


async def _kb_tables_as_ddl(project_id: str) -> tuple[str, int]:
    """Reconstruct a synthetic CREATE-TABLE DDL from ``kb_entities`` rows.

    Returns ``(ddl_text, table_count)``. Emits one ``CREATE TABLE`` per
    row with the columns we discovered at Build-KB time. Foreign keys are
    emitted as inline REFERENCES clauses when both sides look sane.

    iter-14.20.7 — filters junk / dump / matched / backup tables and
    caps the result at ``LAMA_ARCH_MAX_SPEC_TABLES`` (default 300).
    """
    try:
        cur = kb_entities.find(
            {"project_id": project_id, "type": "TABLE"},
            {"_id": 0, "name": 1, "pk": 1, "columns": 1, "fks": 1, "source": 1},
        )
    except Exception as e:
        logger.warning("_kb_tables_as_ddl: kb_entities query failed: %s", e)
        return "", 0

    # First pass: collect + dedupe + junk-filter.
    keep: List[Dict[str, Any]] = []
    seen: set = set()
    junk_dropped = 0
    total_rows = 0
    async for row in cur:
        total_rows += 1
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if _is_junk_table(name):
            junk_dropped += 1
            continue
        keep.append(row)

    # Sort: tables with FKs first (schema hub), then tables with more
    # columns (richer entities), then alpha. Deterministic.
    def _rank(r: Dict[str, Any]) -> tuple:
        fks = r.get("fks") or []
        cols = r.get("columns") or []
        return (-len(fks), -len(cols), (r.get("name") or "").lower())

    keep.sort(key=_rank)

    truncated = False
    if len(keep) > _MAX_SPEC_TABLES:
        keep = keep[:_MAX_SPEC_TABLES]
        truncated = True

    logger.info(
        "_kb_tables_as_ddl: total=%d junk_dropped=%d kept=%d truncated=%s (cap=%d)",
        total_rows, junk_dropped, len(keep), truncated, _MAX_SPEC_TABLES,
    )

    parts: List[str] = []
    if truncated:
        parts.append(
            f"-- iter-14.20.7 NOTE: the KB enumerated {total_rows} tables; "
            f"{junk_dropped} matched junk patterns; the top {_MAX_SPEC_TABLES} "
            f"(ranked by FK count, then column count) are spec'd here. "
            f"Raise LAMA_ARCH_MAX_SPEC_TABLES env-var to include more, or "
            f"curate an OLTP DDL in the DataModel stage."
        )
        parts.append("")
    elif junk_dropped:
        parts.append(
            f"-- iter-14.20.7 NOTE: {junk_dropped} junk / dump / matched / "
            f"backup tables were skipped from the KB surface."
        )
        parts.append("")

    count = 0
    for row in keep:
        name = (row.get("name") or "").strip()
        cols = row.get("columns") or []
        pk = (row.get("pk") or "").strip()
        fks = row.get("fks") or []
        fk_map = {}
        for fk in fks:
            if isinstance(fk, dict) and fk.get("column") and fk.get("ref_table"):
                fk_map[fk["column"]] = (fk["ref_table"], fk.get("ref_column") or "id")

        if not cols:
            parts.append(f"-- Source: {row.get('source') or 'kb_entities'}")
            parts.append(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY);")
            parts.append("")
            count += 1
            continue

        col_lines: List[str] = []
        pk_marked = False
        for c in cols:
            cname = (c.get("name") or "").strip()
            ctype = _kb_col_to_sql(c.get("type") or "")
            if not cname:
                continue
            line = f"    {cname} {ctype}"
            if pk and cname == pk and not pk_marked:
                line += " PRIMARY KEY"
                pk_marked = True
            if cname in fk_map:
                ref_tbl, ref_col = fk_map[cname]
                line += f" REFERENCES {ref_tbl}({ref_col})"
            col_lines.append(line)
        if not col_lines:
            col_lines.append("    id INTEGER PRIMARY KEY")
        elif not pk_marked:
            col_lines.insert(0, "    id INTEGER PRIMARY KEY")

        parts.append(f"-- Source: {row.get('source') or 'kb_entities'}")
        parts.append(f"CREATE TABLE {name} (")
        parts.append(",\n".join(col_lines))
        parts.append(");")
        parts.append("")
        count += 1

    # iter-14.33 — JPA / Struts / Spring apps store their schema as @Table
    # hints + JPA model classes, NOT as `type:"TABLE"` rows, so the query
    # above yields 0. Fall back to the legacy-KB harvester which joins
    # TABLE_HINT → CLASS.fields, so Architecture still sees the full legacy
    # table inventory (and DataModel-skipped projects get real entities).
    if count == 0:
        ddl, n = await build_oltp_ddl_from_kb(project_id, max_tables=_MAX_SPEC_TABLES)
        if n:
            logger.info(
                "_kb_tables_as_ddl: no TABLE rows → harvested %d legacy tables "
                "from @Table hints + JPA models", n,
            )
        return ddl, n

    return "\n".join(parts), count


async def _load_oltp_ddl(
    project_id: str, dm_ctx: Dict[str, Any], services: List[Dict[str, Any]],
) -> tuple[str, Dict[str, Any]]:
    """Return ``(ddl_text, meta)``. ``meta`` records which source(s) were
    used and how many bytes each contributed — surface via logs + artifact
    header so the user can debug empty-DDL scenarios."""
    outputs = (dm_ctx or {}).get("outputs", {}) or {}
    stage_ddl = outputs.get("oltp_ddl") or ""
    stage_ddl = str(stage_ddl).strip()

    live_ddl = ""
    try:
        doc = await data_models.find_one(
            {"project_id": project_id, "type": "oltp_ddl"},
            {"_id": 0, "content": 1, "id": 1, "frozen": 1},
        )
        if doc and doc.get("content"):
            live_ddl = str(doc["content"]).strip()
    except Exception as e:
        logger.warning("_load_oltp_ddl: data_models fetch failed: %s", e)

    per_svc_snippets: List[str] = []
    for s in services or []:
        for k in ("ddl_snippet", "ddl", "schema_ddl", "table_ddl"):
            v = s.get(k)
            if v and str(v).strip():
                per_svc_snippets.append(str(v).strip())

    # iter-14.20.6 — fourth source: legacy KB tables (parsed from SQL dumps
    # / PHP models at Build KB time).
    kb_ddl, kb_table_count = await _kb_tables_as_ddl(project_id)

    # Preference order:
    #   1. Live artifact (source of truth, current DataModel content)
    #   2. Frozen stage_context snapshot
    #   3. Legacy KB tables (rebuilt from kb_entities)
    #   4. Per-service DDL snippets
    chosen = live_ddl or stage_ddl or kb_ddl
    merged_parts = [chosen] if chosen else []
    # Always append per-service snippets — they are additive.
    if per_svc_snippets:
        merged_parts += per_svc_snippets
    combined = "\n\n".join(p for p in merged_parts if p)

    if live_ddl and chosen is live_ddl:
        source_used = "live_artifact"
    elif stage_ddl and chosen is stage_ddl:
        source_used = "stage_context"
    elif kb_ddl and chosen is kb_ddl:
        source_used = "kb_entities"
    elif per_svc_snippets:
        source_used = "per_service_snippets"
    else:
        source_used = "none"

    meta = {
        "stage_ctx_ddl_bytes": len(stage_ddl),
        "live_artifact_ddl_bytes": len(live_ddl),
        "kb_entities_ddl_bytes": len(kb_ddl),
        "kb_entities_table_count": kb_table_count,
        "per_service_ddl_bytes": sum(len(x) for x in per_svc_snippets),
        "source_used": source_used,
    }
    return combined, meta


def _synthetic_legacy_unsorted_service(unassigned: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """Build a synthetic service row containing every KB route that was
    not attached to any user-defined service. Return None if there are
    no unassigned routes.
    """
    if not unassigned:
        return None
    roles: set = set()
    modules: set = set()
    api_endpoints: List[str] = []
    for r in unassigned:
        api_endpoints.append(f"{r['verb']} {r['path']}".strip())
        if r.get("module"):
            modules.add(r["module"])
        for role in r.get("roles") or []:
            roles.add(role)
    return {
        "name": "legacy-unsorted",
        "display_name": "Legacy — Unsorted Routes (auto-attached)",
        "description": (
            f"Auto-attached bucket: {len(unassigned)} legacy route(s) from "
            f"{len(modules)} KB module(s) were not clustered into any service "
            f"by arch.recommend. Included here so every legacy API is present "
            f"in the API Contracts artifact."
        ),
        "responsibility": "Bucket for unassigned legacy routes.",
        "tables": [],
        "roles": sorted(roles),
        "modules": [],
        "module_names": sorted(modules),
        "dependencies": [],
        "api_endpoints": api_endpoints,
        "routes_detail": unassigned,
        "api_count": len(unassigned),
        "route_count": len(unassigned),
        "backend_lang": "python",
        "kind": "legacy_bucket",
        "frontend": False,
    }


_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SEC = 60 * 60


def _new_job(project_id: str, kind: str) -> str:
    import uuid, time as _t
    jid = uuid.uuid4().hex
    _JOBS[jid] = {
        "id": jid, "project_id": project_id, "kind": kind,
        "status": "queued", "step": "Queued…", "pct": 0,
        "started_at": _t.time(), "ended_at": None,
        "error": None, "result": {},
    }
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


# iter-13.50 — Transport-failure circuit breaker for arch sub-jobs.
#
# WHY: Sequence/HLD/LLD/API-contracts jobs fan out N parallel LLM calls
# (one per use-case / service / section). Until iter-13.50, each call had
# its own `except Exception as e: content = f"... error: {e}"` which baked
# the raw error string (commonly the glibc `[Errno -2] Name or service
# not known`) into the artifact. The user then saw 10 sequence diagrams
# that contained nothing but the DNS error message.
#
# Now each call uses `_safe_llm_call` which:
#   • Distinguishes TransportError (DNS / connect / SSL) from model errors
#   • Records every failure into a job-scoped list
#   • Returns "" content + the error object so callers can SKIP the
#     section instead of polluting it with the error text
# After fanout finishes, callers check `_should_abort_for_transport` and
# call `_job_finish(jid, "error", error=...)` rather than persisting a
# fake artifact.
async def _safe_llm_call(jid: str, label: str, **kwargs) -> tuple[dict | None, Exception | None]:
    """Wrap a chat_completion call so transport errors are recorded on the
    job and never leak into artifact content. Returns (response, error).
    On any failure: response is None and error is the captured exception.
    """
    try:
        r = await chat_completion(**kwargs)
        return r, None
    except TransportError as te:
        logger.error("arch job=%s section=%s transport error: %s", jid, label, te)
        if jid in _JOBS:
            _JOBS[jid].setdefault("section_errors", []).append(
                {"section": label, "kind": "transport", "error": str(te)}
            )
        return None, te
    except Exception as e:  # noqa: BLE001
        logger.warning("arch job=%s section=%s LLM error: %s", jid, label, e)
        if jid in _JOBS:
            _JOBS[jid].setdefault("section_errors", []).append(
                {"section": label, "kind": "model", "error": str(e)[:300]}
            )
        return None, e


def _should_abort_for_transport(jid: str) -> str:
    """If ANY section failed with TransportError, return a human-readable
    abort message; else "". Used by job runners to bail out cleanly
    rather than persisting a broken artifact.
    """
    j = _JOBS.get(jid) or {}
    errs = j.get("section_errors") or []
    transports = [e for e in errs if e.get("kind") == "transport"]
    if not transports:
        return ""
    # Dedupe identical messages
    unique = sorted({(e.get("error") or "") for e in transports})
    head = unique[0] if unique else "Transport failure"
    n = len(transports)
    return (
        f"{n} section(s) failed with a network/DNS error — aborting before "
        f"writing a broken artifact. First error: {head}"
    )


# iter-13.51 — extend abort logic to billing/auth (402/401) errors too.
# Previously the per-section catch classified an OpenRouter 402 as
# kind="model" and inlined the verbose 402 JSON into the sequence
# diagram body ("model error — OpenRouter error 402: Insufficient
# credits…"). Like transport errors, billing/auth errors are
# JOB-FATAL: every section will fail the same way, so persisting a
# partial artifact is worse than failing the whole job.
def _classify_llm_error_kind(exc: Exception) -> str:
    """Return "transport" / "billing" / "auth" / "model" for `exc`."""
    if isinstance(exc, TransportError):
        return "transport"
    msg = str(exc) or ""
    if _is_billing_error(msg):
        return "billing"
    if _is_auth_error(msg):
        return "auth"
    return "model"


_FATAL_ERROR_KINDS = {"transport", "billing", "auth"}


def _should_abort_job(jid: str) -> str:
    """Return a human-readable abort message if any section failed with a
    JOB-FATAL error (transport / billing / auth) — empty string otherwise.
    Superset of `_should_abort_for_transport` and the preferred caller.
    """
    j = _JOBS.get(jid) or {}
    errs = j.get("section_errors") or []
    fatal = [e for e in errs if e.get("kind") in _FATAL_ERROR_KINDS]
    if not fatal:
        return ""
    # Group by kind so the message is informative even when both types fired
    by_kind: Dict[str, List[str]] = {}
    for e in fatal:
        by_kind.setdefault(e.get("kind") or "model", []).append(e.get("error") or "")
    parts: List[str] = []
    for kind, msgs in by_kind.items():
        unique = sorted(set(msgs))
        head = (unique[0] if unique else "")[:300]
        nice = {
            "transport": "network/DNS",
            "billing":   "credits/billing (e.g. OpenRouter 402)",
            "auth":      "auth/401 (invalid or missing API key)",
        }.get(kind, kind)
        parts.append(f"{len(msgs)} × {nice} — first: {head}")
    return (
        "Aborting before writing a broken artifact. " + " | ".join(parts) +
        " — fix the provider / top-up credits in Console → Models, then retry."
    )


async def _get_prompt(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts_col.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


async def _with_completeness_contract(project_id: str, sp: str) -> str:
    """Prepend gov.completeness_contract so every Architecture generation
    / regeneration (recommend, HLD, LLD, sequence, api_contracts) inherits
    the stage-agnostic ≥ 95 % confidence target + CONFIDENCE_SELF_SCORE
    footer contract. Silent no-op when the prompt is unseeded."""
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


# iter-13.42 — required placeholders per architecture prompt. If a stored
# prompt template is missing one of these, the LLM cannot possibly cite
# target tech / SRS / graph properly. We log a loud warning so operators
# can spot a stale / hand-edited prompt before blaming the LLM.
# Expanded so the freshness asserter matches the v5 prompts in seed.py
# (recommend now has its own graph_subgraph + legacy_evidence + srs_use_cases
# + srs_nfr slots, no longer rolls graph into rag_context).
_REQUIRED_PLACEHOLDERS: Dict[str, set] = {
    "arch.recommend":     {"target_tech", "srs_summary", "srs_use_cases",
                           "srs_nfr", "surface_skeleton", "oltp_table_list",
                           "complexity_signals"},
    # iter-13.44 — HLD / LLD / API contracts now consume pre-attached
    # service surfaces and the global module index; the legacy
    # graph_subgraph placeholder is retained as a back-compat alias only.
    "arch.hld":           {"target_tech", "service_breakdown", "module_index",
                           "legacy_evidence", "srs_functional",
                           "srs_use_cases", "srs_nfr", "services_summary",
                           "oltp_summary"},
    "arch.lld":           {"target_tech", "service_surface", "service_modules",
                           "service_api_count", "service_description",
                           "legacy_evidence", "srs_use_cases", "srs_nfr",
                           "relevant_ddl", "hld_summary"},
    "arch.sequence":      {"target_tech", "graph_subgraph", "legacy_evidence",
                           "use_case_content", "srs_nfr", "relevant_endpoints"},
    "arch.api_contracts": {"target_tech", "service_surface", "service_modules",
                           "service_api_count", "service_description",
                           "legacy_evidence", "relevant_ddl",
                           "srs_use_cases", "srs_nfr"},
}


# iter-13.42 — Graph subgraph retrieval helper.
#
# Replaces the previous "if await is_graph_kb_enabled(project_id)" guards
# scattered across every arch job. That toggle was designed to gate the
# EXPENSIVE Opus enrichment during Build KB, not the cheap retrieval at
# use time. Gating retrieval on the same flag was the root cause of arch
# artifacts feeling un-grounded even on projects where the user had built
# the graph: the runtime silently fed an empty {graph_subgraph} slot to
# the LLM, which then wrote generic prose.
#
# New contract: if the kb_graph collection has a doc for this project,
# ALWAYS use it for retrieval regardless of the toggle. Returns
# (yaml_str, node_count) so callers can:
#   • surface a warning banner on the saved artifact when node_count == 0
#   • record `graph_grounded` + `graph_node_count` in tracability
async def _graph_subgraph_yaml(
    project_id: str,
    seeds: List[str],
    max_chars: int = 4000,
    hops: int = 2,
    max_nodes: int = 140,
) -> tuple[str, int]:
    """Return (yaml_str, node_count). Empty string + 0 if graph not built."""
    try:
        from db import kb_graph as kb_graph_col
        has_graph = await kb_graph_col.find_one(
            {"project_id": project_id}, {"version": 1}
        )
        if not has_graph:
            return "", 0
        from kb.graph_retriever import subgraph_for, serialize_yaml
        sg = await subgraph_for(project_id, seeds, hops=hops, max_nodes=max_nodes)
        if not sg or not sg.get("nodes"):
            return "", 0
        return serialize_yaml(sg, max_chars=max_chars), len(sg.get("nodes") or [])
    except Exception as e:
        logger.warning("graph subgraph retrieval failed (project=%s): %s", project_id, e)
        return "", 0


def _graph_warning_banner(graph_node_count: int) -> str:
    """Single-line markdown warning prepended to artifacts generated without
    a graph. Makes "silent un-grounded generation" visible to the user."""
    if graph_node_count > 0:
        return ""
    return (
        "> ⚠ **Generated WITHOUT GRAPHIFY evidence** — decisions below are "
        "heuristic, not codebase-grounded. Build the KB graph "
        "(Sidebar → Build KB) and regenerate for higher fidelity.\n\n"
    )


# ─────────────────────────────────────────────────────────────────────
# iter-13.43 — Deterministic Legacy Surface Enumerator (arch.recommend)
# ─────────────────────────────────────────────────────────────────────
#
# WHY THIS EXISTS
# ---------------
# arch.recommend used to ask one LLM call to "discover" the whole service
# map, including every endpoint. For a real legacy app with 500+ routes
# this is impossible:
#   • the model only sees the first ~3-5 KB of graph YAML (truncated)
#   • it then hallucinates 10-20 representative services with 2-5
#     endpoints each, missing 90% of the actual surface area
#   • token cost balloons (we throw 30+ KB at it) while signal collapses
#
# THE FIX
# -------
# Enumerate the legacy surface DETERMINISTICALLY from the KB:
#   1. Pull every Route node from kb_graph (or kb_entities fallback)
#   2. Walk EXPOSES edges back to owning Class, then BELONGS_TO_MODULE
#      to its Module — that's the natural service boundary
#   3. Walk READS/WRITES edges from those Classes to Tables — that's
#      table ownership
#   4. Walk GUARDED_BY edges to Roles — that's the auth contract
#
# The LLM only sees a COMPACT module-level summary (name, route_count,
# table_count, role list) and is asked to do the ONE thing it's good at:
# cluster modules into services, name them, pick the pattern. The full
# route + table lists are then attached MECHANICALLY by the backend,
# guaranteeing 100% coverage of the legacy surface.


# ─────────────────────────────────────────────────────────────────────
# iter-14.31 — Business-capability domain bucketing
# ─────────────────────────────────────────────────────────────────────
# ROOT CAUSE of the "Ceo Action / Ceo Dao / Ceo Service / Ceo Vo" bug:
# the KB graph creates one MODULE node PER FILE, named by its full path
# (e.g. src/com/ahct/CEO/service/BudgetService.java). When those file-
# modules are handed to the recommender LLM, the only structure it can
# see is the *technical layer directory* (action / service / dao / vo /
# util), so it clusters HORIZONTALLY by layer instead of VERTICALLY by
# business capability. That produces layer-services, which is an anti-
# pattern (a service that is "all the DAOs" owns no business capability).
#
# Legacy class names DO encode the business capability as a prefix, with
# a well-known technical-layer suffix:
#     BudgetService, BudgetServiceImpl, BudgetAction, BudgetDAO,
#     BudgetDAOImpl, BudgetVO, BudgetForm  → capability "budget"
#     AdminSanctionService, AdminSanctionAction, AdminSanctionVO
#                                           → capability "admin-sanction"
# We strip the layer suffix, kebab-case the remaining business prefix,
# and RE-BUCKET the per-file modules into business-capability modules.
# The recommender then sees clean vertical slices and can only cluster
# by domain. Toggle off with LAMA_ARCH_DOMAIN_BUCKETING=0 if needed.

# Technical-layer suffixes stripped from class names to reveal the
# business-capability prefix. Ordered longest / compound FIRST so
# "ServiceImpl" is stripped before "Service", "DAOImpl" before "DAO".
_LAYER_SUFFIXES = [
    "ServiceImpl", "ServiceBean", "RepositoryImpl", "ControllerImpl",
    "ManagerImpl", "DelegateImpl", "DAOImpl", "DaoImpl", "BOImpl", "BoImpl",
    "Controller", "RestController", "Servlet", "Service", "Delegate",
    "Repository", "Repo", "DAO", "Dao", "Action", "ActionForm",
    "FormBean", "Form", "Bean", "DTO", "Dto", "VO", "Vo", "PO", "Po",
    "Entity", "Mapper", "Model", "Manager", "Handler", "Processor",
    "Factory", "Builder", "Provider", "Validator", "Interceptor",
    "Filter", "Listener", "Scheduler", "Job", "Task", "Worker",
    "Config", "Configuration", "Constants", "Const", "Exception",
    "Helper", "Utils", "Util", "BO", "Bo", "Impl",
]

# Generic prefixes that are NOT business capabilities — when a stripped
# class name collapses to one of these, fall back to the path domain.
_GENERIC_PREFIXES = {
    "", "common", "base", "abstract", "generic", "core", "main",
    "default", "global", "app", "application", "sql", "db", "data",
    "util", "utils", "helper", "constants", "test", "temp", "tmp",
    # Technical-layer / junk-package words — a route path token or class
    # that collapses to one of these is NOT a business capability, so we
    # force a fallback to the owning module's real domain.
    "action", "actions", "service", "services", "dao", "daos",
    "controller", "controllers", "repository", "repositories", "repo",
    "vo", "dto", "form", "forms", "formbean", "bean", "beans", "servlet",
    "servlets", "entity", "entities", "model", "models", "mapper",
    "mappers", "manager", "handler", "handlers", "filter", "listener",
    "config", "configuration", "web-content", "webcontent", "web",
    "webapp", "com", "org", "net", "java", "src", "impl",
}


def _camel_to_kebab(name: str) -> str:
    """`AdminSanctionRemarks` → `admin-sanction-remarks`; keeps acronym
    runs together (`SQLChangeMgmt` → `sql-change-mgmt`)."""
    if not name:
        return ""
    # Insert boundary between an acronym run and a following Word
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    # Insert boundary between lower/digit and Upper
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    s = s.replace("_", "-").replace(" ", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s.lower()


def _strip_layer_suffix(class_name: str) -> str:
    """Strip trailing technical-layer suffixes to reveal the business
    prefix. Applied repeatedly so `BudgetDAOImpl` → `Budget`."""
    if not class_name:
        return ""
    name = class_name.strip()
    # Take the simple name (drop any package qualifier)
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    changed = True
    while changed and name:
        changed = False
        for suf in _LAYER_SUFFIXES:
            if len(name) > len(suf) and name.endswith(suf):
                name = name[: -len(suf)]
                changed = True
                break
    return name


def _business_domain_from_class(class_name: str) -> str:
    """Business-capability kebab key from a class name, or '' if the
    class collapses to a generic/technical stub."""
    prefix = _strip_layer_suffix(class_name)
    kebab = _camel_to_kebab(prefix)
    if kebab in _GENERIC_PREFIXES:
        return ""
    return kebab


def _business_domain_from_path(source: str) -> str:
    """Fallback domain from a source path — use the business sub-package
    (the segment BEFORE the technical-layer directory), never the layer
    directory itself.

    src/com/ahct/CEO/service/BudgetService.java → 'ceo'
    src/com/ahct/login/controller/LoginController.java → 'login'
    """
    if not source:
        return "unsorted"
    _LAYER_DIRS = {
        "action", "actions", "controller", "controllers", "service",
        "services", "dao", "daos", "repository", "repositories", "vo",
        "dto", "form", "forms", "bean", "beans", "model", "models",
        "entity", "entities", "util", "utils", "helper", "helpers",
        "config", "constants", "web", "webapp", "src", "main", "java",
        "com", "org", "net", "impl", "handler", "handlers", "filter",
        "listener", "mapper", "mappers", "manager", "managers",
    }
    # Normalise + drop the filename
    parts = [p for p in re.split(r"[\\/]", source) if p]
    if parts and "." in parts[-1]:
        parts = parts[:-1]
    # Walk from the deepest segment backwards; the first NON-layer,
    # NON-generic-package segment is the business sub-package.
    for seg in reversed(parts):
        low = seg.lower()
        if low in _LAYER_DIRS:
            continue
        return _camel_to_kebab(seg) or "unsorted"
    return "unsorted"


def _route_domain(route: Dict[str, Any], module: Dict[str, Any]) -> str:
    """Domain for a single route — prefer the handler class name, then
    the route path token, then the owning module's path domain."""
    # 1. Handler class carried on the route (Struts <action type=…> or
    #    Spring @Controller class) — strongest signal.
    handler = (route.get("class") or "").strip()
    if handler:
        d = _business_domain_from_class(handler)
        if d:
            return d
    # 2. Path token: /budgetAction.do → 'budget', /claims/list → 'claims'
    path = (route.get("path") or "").strip()
    if path:
        tok = re.split(r"[\\/.?]", path.strip("/"))
        tok = [t for t in tok if t]
        if tok:
            # Strip common action verbs / suffixes from the first token
            cand = tok[0]
            cand = re.sub(r"(Action|Servlet|Controller|do|htm|html|jsp)$", "", cand, flags=re.IGNORECASE)
            d = _business_domain_from_class(cand) or _camel_to_kebab(cand)
            if d and d not in _GENERIC_PREFIXES:
                return d
    # 3. Fall back to the module's path domain
    return _business_domain_from_path(module.get("name") or module.get("id") or "")


def _module_dominant_domain(module: Dict[str, Any]) -> str:
    """Best single domain for a module — majority vote over its class
    names, falling back to the path domain."""
    from collections import Counter
    votes: Counter = Counter()
    for c in module.get("classes") or []:
        d = _business_domain_from_class(c)
        if d:
            votes[d] += 1
    if votes:
        return votes.most_common(1)[0][0]
    return _business_domain_from_path(module.get("name") or module.get("id") or "")


def _rebucket_modules_by_domain(surface: Dict[str, Any]) -> Dict[str, Any]:
    """Re-group per-file modules into business-capability modules.

    Splits multi-domain modules (e.g. struts-config.xml whose routes
    span many handlers) by per-route handler domain, and merges the
    layer-split file-modules (action/service/dao/vo of one capability)
    into a single vertical-slice module. Returns a NEW surface dict with
    the same shape so all downstream code (skeleton formatter, expansion)
    is unchanged.
    """
    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(dk: str) -> Dict[str, Any]:
        if dk not in buckets:
            buckets[dk] = {
                "id": dk,
                "name": dk,
                "classes": set(),
                "routes": [],
                "route_keys": set(),
                "tables": set(),
                "roles": set(),
                "source_files": set(),
            }
        return buckets[dk]

    for m in surface.get("modules") or []:
        # Route-level split by handler/path domain
        for r in m.get("routes") or []:
            dk = _route_domain(r, m) or "unsorted"
            b = _bucket(dk)
            rkey = f"{(r.get('verb') or 'ANY').upper()} {r.get('path') or ''}".strip()
            if rkey and rkey not in b["route_keys"]:
                b["route_keys"].add(rkey)
                b["routes"].append({
                    "verb": (r.get("verb") or "ANY").upper(),
                    "path": r.get("path") or "",
                    "class": r.get("class") or "",
                    "roles": r.get("roles") or [],
                })
            b["roles"].update(r.get("roles") or [])
        # Class-level split by class-name domain
        for c in m.get("classes") or []:
            dk = _business_domain_from_class(c) or _module_dominant_domain(m) or "unsorted"
            b = _bucket(dk)
            b["classes"].add(c)
        # Tables + roles follow the module's dominant class domain
        dom = _module_dominant_domain(m) or "unsorted"
        b = _bucket(dom)
        b["tables"].update(t for t in (m.get("tables") or []) if t)
        b["roles"].update(m.get("roles") or [])
        b["source_files"].add(m.get("name") or m.get("id") or "")

    # Build the full module list first, then decide whether to prune the
    # pure-supporting-type (VO/DTO-only, no routes, no tables) buckets.
    all_built: List[Dict[str, Any]] = []
    for dk, b in buckets.items():
        if not (b["routes"] or b["classes"] or b["tables"]):
            continue
        all_built.append({
            "id": dk,
            "name": dk,
            "class_count": len(b["classes"]),
            "classes": sorted(b["classes"])[:80],
            "route_count": len(b["routes"]),
            "routes": b["routes"],
            "tables": sorted(b["tables"]),
            "roles": sorted(r for r in b["roles"] if r),
        })

    # iter-14.31 — a module is part of the SERVICE SURFACE when it exposes
    # endpoints OR owns tables. Pure-class buckets (e.g. a lone `FooVO`
    # whose prefix matches no routed capability) are supporting data
    # structures, not services — surfacing them as candidate services
    # floods the recommender with dozens of meaningless single-VO
    # "services". Keep only surface modules WHEN any exist; otherwise
    # fall back to the class-only buckets so sparse apps still decompose.
    surface_modules = [m for m in all_built if m["route_count"] > 0 or m["tables"]]
    modules_out = surface_modules if surface_modules else all_built

    all_tables: set = set()
    total_routes = 0
    for m in modules_out:
        all_tables.update(m["tables"])
        total_routes += m["route_count"]

    modules_out.sort(key=lambda mm: (-mm["route_count"], -mm["class_count"], mm["name"]))
    return {
        "total_routes": total_routes,
        "total_tables": len(all_tables),
        "total_modules": len(modules_out),
        "source": (surface.get("source") or "kb") + "+domain-bucketed",
        "modules": modules_out,
    }


async def _enumerate_legacy_surface(project_id: str) -> Dict[str, Any]:
    """Return the full enumerated legacy surface for arch.recommend.

    Shape::

        {
          "total_routes": int,
          "total_tables": int,
          "total_modules": int,
          "source": "kb_graph" | "kb_entities",
          "modules": [
            {
              "id": str, "name": str,
              "class_count": int, "classes": [str],
              "route_count": int,
              "routes": [{"verb": str, "path": str, "class": str, "roles": [str]}],
              "tables": [str],
              "roles": [str],
            }, ...   # sorted by route_count desc
          ],
        }
    """
    # Prefer kb_graph (richer relations). Fall back to kb_entities scan.
    try:
        from kb.kb_graph import load_kb_graph
        graph = await load_kb_graph(project_id)
    except Exception as e:
        logger.warning("load_kb_graph failed: %s", e)
        graph = None

    raw: Dict[str, Any]
    if graph and (graph.get("nodes") or []):
        result = _enumerate_from_graph(graph)
        # iter-14.22 / iter-14.31 — fall through to entities when the graph
        # is STALE. The graph is rebuilt only on a full KB build; if the
        # user re-extracted entities (e.g. after the iter-14.30 Struts fix
        # bumped route count 3→163) but the graph meta still reflects the
        # OLD extraction, `_enumerate_from_graph` would silently return the
        # stale surface. We therefore ALWAYS cross-check the live
        # kb_entities ROUTE count and prefer the entity scan whenever the
        # graph is missing a material fraction of the routes.
        graph_routes = result.get("total_routes", 0)
        ent_route_count = await kb_entities.count_documents({
            "project_id": project_id, "type": "ROUTE",
        })
        # "Materially fewer" = graph has < 90% of the entity routes (with a
        # small absolute cushion so a 1-route rounding diff doesn't churn).
        graph_is_stale = ent_route_count > 0 and graph_routes < max(1, int(ent_route_count * 0.9))
        if graph_is_stale:
            logger.warning(
                "arch.recommend: kb_graph reports %d routes but kb_entities "
                "has %d — graph is stale, falling through to entity scan.",
                graph_routes, ent_route_count,
            )
            raw = await _enumerate_from_entities(project_id)
        else:
            raw = result
    else:
        raw = await _enumerate_from_entities(project_id)

    # iter-14.31 — re-group per-file / per-layer modules into business-
    # capability modules so the recommender clusters VERTICALLY (by
    # domain) instead of HORIZONTALLY (by action/service/dao/vo layer).
    if os.environ.get("LAMA_ARCH_DOMAIN_BUCKETING", "1") not in ("0", "false", "False", ""):
        try:
            bucketed = _rebucket_modules_by_domain(raw)
            # Guard against SILENT route LOSS. The bucketer dedups routes by
            # (verb, path) within a domain, so its count can legitimately be
            # LOWER than raw.total_routes when the KB carries duplicate ROUTE
            # rows (e.g. the same path emitted by both the Struts <action>
            # AND a jsp-form scan). We therefore compare against the raw
            # UNIQUE (verb, path) surface, not the raw row count.
            raw_unique: set = set()
            for m in raw.get("modules") or []:
                for r in m.get("routes") or []:
                    raw_unique.add(
                        f"{(r.get('verb') or 'ANY').upper()} {(r.get('path') or '').strip()}"
                    )
            raw_unique_n = len(raw_unique)
            if bucketed["modules"] and bucketed["total_routes"] >= raw_unique_n:
                logger.info(
                    "arch.recommend: domain-bucketed %d file/layer modules → "
                    "%d business-capability modules (routes raw=%d unique=%d "
                    "bucketed=%d, tables %d→%d)",
                    raw.get("total_modules", 0), bucketed["total_modules"],
                    raw.get("total_routes", 0), raw_unique_n,
                    bucketed["total_routes"],
                    raw.get("total_tables", 0), bucketed["total_tables"],
                )
                return bucketed
            logger.warning(
                "arch.recommend: domain bucketing skipped (unique routes "
                "%d→%d, modules=%d) — using raw surface.",
                raw_unique_n, bucketed.get("total_routes", 0),
                bucketed.get("total_modules", 0),
            )
        except Exception as _reb_exc:  # noqa: BLE001
            logger.warning("arch.recommend: domain bucketing failed: %s", _reb_exc)
    return raw


def _enumerate_from_graph(graph: Dict[str, Any]) -> Dict[str, Any]:
    from collections import defaultdict
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    nodes_by_id: Dict[str, dict] = {n["id"]: n for n in nodes}

    exposes: List[tuple] = []                            # (class, route)
    belongs_to_module: Dict[str, str] = {}               # class_id -> module_id
    class_tables: Dict[str, set] = defaultdict(set)      # class_id -> {table_name}
    route_roles: Dict[str, set] = defaultdict(set)       # route_id -> {role_name}

    for e in edges:
        src_id, dst_id, etype = e.get("src"), e.get("dst"), (e.get("type") or "").upper()
        src = nodes_by_id.get(src_id)
        dst = nodes_by_id.get(dst_id)
        if not src or not dst:
            continue
        if etype == "EXPOSES":
            exposes.append((src, dst))
        elif etype == "BELONGS_TO_MODULE":
            belongs_to_module[src_id] = dst_id
        elif etype in ("READS", "WRITES"):
            if dst.get("type") == "Table":
                class_tables[src_id].add(dst.get("name") or "")
        elif etype == "GUARDED_BY":
            if src.get("type") == "Route":
                route_roles[src_id].add(dst.get("name") or "")

    class_routes: Dict[str, list] = defaultdict(list)
    for cls, route in exposes:
        if cls.get("type") == "Class" and route.get("type") == "Route":
            class_routes[cls["id"]].append(route)

    # Group classes by module (orphan classes bucket under "__orphan__")
    module_classes: Dict[str, list] = defaultdict(list)
    for n in nodes:
        if n.get("type") != "Class":
            continue
        mid = belongs_to_module.get(n["id"], "__orphan__")
        module_classes[mid].append(n)

    modules_out: List[Dict[str, Any]] = []
    for mid, classes in module_classes.items():
        mod_node = nodes_by_id.get(mid) if mid != "__orphan__" else None
        module_name = (mod_node or {}).get("name") or ("(unaffiliated classes)" if mid == "__orphan__" else mid)

        routes: List[Dict[str, Any]] = []
        tables: set = set()
        roles_for_module: set = set()
        for cls in classes:
            for r in class_routes.get(cls["id"], []):
                rr_roles = sorted(route_roles.get(r["id"], set()))
                routes.append({
                    "verb": (r.get("verb") or "ANY").upper(),
                    "path": r.get("path") or r.get("name") or "",
                    "class": cls.get("name") or "",
                    "roles": rr_roles,
                })
                roles_for_module.update(rr_roles)
            tables.update(class_tables.get(cls["id"], set()))

        modules_out.append({
            "id": mid,
            "name": module_name,
            "class_count": len(classes),
            "classes": sorted({c.get("name") or "" for c in classes})[:80],
            "route_count": len(routes),
            "routes": routes,
            "tables": sorted(t for t in tables if t),
            "roles": sorted(r for r in roles_for_module if r),
        })

    modules_out.sort(key=lambda m: (-m["route_count"], -m["class_count"], m["name"]))

    total_routes = sum(m["route_count"] for m in modules_out)
    all_tables: set = set()
    for m in modules_out:
        all_tables.update(m["tables"])

    return {
        "total_routes": total_routes,
        "total_tables": len(all_tables),
        "total_modules": len([m for m in modules_out if m["id"] != "__orphan__"]),
        "source": "kb_graph",
        "modules": modules_out,
    }


async def _enumerate_from_entities(project_id: str) -> Dict[str, Any]:
    """Fallback when kb_graph is absent — walk kb_entities directly.

    Less precise (we don't have Class→Route owning edges) so we bucket by
    source file path. Still enumerates EVERY route — which is the whole
    point: even without a graph, arch.recommend must see the real count.
    """
    from collections import defaultdict
    modules: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"routes": [], "tables": set(), "roles": set(), "classes": set()}
    )
    all_tables: set = set()
    total_routes = 0
    cur = kb_entities.find({"project_id": project_id}, {"_id": 0}).limit(20000)
    async for ent in cur:
        et = (ent.get("type") or "").upper()
        src = (ent.get("source") or ent.get("file") or "(unknown)")
        # Bucket by top folder of the source path (typical module layout)
        mod_key = src.split("/")[1] if src.count("/") >= 1 and src.startswith(("app/", "src/", "application/")) else src.split("/")[0] or "(root)"
        if et == "ROUTE":
            total_routes += 1
            roles = ent.get("roles") or []
            modules[mod_key]["routes"].append({
                "verb": (ent.get("verb") or ent.get("http_method") or "ANY").upper(),
                "path": ent.get("name") or ent.get("path") or "",
                # iter-14.31 — carry the Struts/Spring handler class so
                # domain bucketing can group routes by business capability.
                "class": ent.get("handler_class") or ent.get("handler") or "",
                "roles": roles,
            })
            modules[mod_key]["roles"].update(roles)
        elif et == "TABLE":
            tname = ent.get("name") or ""
            if tname:
                all_tables.add(tname)
                modules[mod_key]["tables"].add(tname)
        elif et == "TABLE_HINT":
            # iter-14.33 — JPA/Struts/Spring apps expose their schema as
            # @Table hints (not `type:"TABLE"`). Count them too so the
            # recommend surface shows the REAL legacy table inventory per
            # module (was 0 → recommendation had no tables to reason about).
            tname = ent.get("name") or ""
            if tname:
                all_tables.add(tname)
                modules[mod_key]["tables"].add(tname)
        elif et == "CLASS":
            cname = ent.get("name") or ""
            if cname:
                modules[mod_key]["classes"].add(cname)

    modules_out: List[Dict[str, Any]] = []
    for mname, info in modules.items():
        modules_out.append({
            "id": mname,
            "name": mname,
            "class_count": len(info["classes"]),
            "classes": sorted(info["classes"])[:80],
            "route_count": len(info["routes"]),
            "routes": info["routes"],
            "tables": sorted(info["tables"]),
            "roles": sorted(info["roles"]),
        })
    modules_out.sort(key=lambda m: (-m["route_count"], -m["class_count"], m["name"]))

    return {
        "total_routes": total_routes,
        "total_tables": len(all_tables),
        "total_modules": len(modules_out),
        "source": "kb_entities",
        "modules": modules_out,
    }


def _format_surface_skeleton(surface: Dict[str, Any], max_modules: int = 80) -> str:
    """Compact YAML-ish summary of the legacy surface for the LLM prompt.

    Critically: we do NOT include the per-route verb+path lines — only
    aggregate counts. The LLM never has to "see" 500 endpoints; it just
    has to decide which modules cluster into which service. The backend
    re-attaches the full route list mechanically after the LLM responds.
    """
    lines: List[str] = [
        f"# LEGACY SURFACE SKELETON (source={surface.get('source')}, "
        f"total_routes={surface['total_routes']}, "
        f"total_tables={surface['total_tables']}, "
        f"total_modules={surface['total_modules']})",
        "modules:",
    ]
    for m in surface["modules"][:max_modules]:
        sample_routes = m["routes"][:3]
        sample_str = "; ".join(f"{r['verb']} {r['path']}" for r in sample_routes if r.get("path"))
        lines.append(f"  - id: {m['id']}")
        lines.append(f"    name: {m['name']}")
        lines.append(f"    route_count: {m['route_count']}")
        lines.append(f"    table_count: {len(m['tables'])}")
        lines.append(f"    class_count: {m['class_count']}")
        if m["tables"]:
            lines.append(f"    tables: [{', '.join(m['tables'][:15])}{', ...' if len(m['tables']) > 15 else ''}]")
        if m["roles"]:
            lines.append(f"    roles: [{', '.join(m['roles'][:8])}]")
        if sample_str:
            lines.append(f"    sample_routes: \"{sample_str}\"")
    if len(surface["modules"]) > max_modules:
        lines.append(
            f"  # … {len(surface['modules']) - max_modules} more module(s) truncated from prompt; "
            f"still enumerated server-side and will be attached to services."
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# iter-13.44 — Pre-attached surface formatters for HLD / LLD / API jobs
# ─────────────────────────────────────────────────────────────────────
#
# All three downstream Architecture jobs (HLD, LLD, API contracts) used
# to make their own redundant retrieval calls:
#   • HLD pulled a project-wide graph_subgraph YAML (~6KB) per section
#     × 17 sections = ~100KB of repeated context
#   • LLD pulled a per-service graph_subgraph (~4KB) per service
#   • API contracts pulled the same per-service subgraph (~4KB) again
# That's wasted tokens AND inconsistent context (each retrieval call
# returns a slightly different neighbourhood).
#
# Now we read the SAME enumerated surface that recommend produced. The
# formatters below convert the persisted data into compact text blocks
# the prompts can drop in. Net effect: cheaper, more consistent, and
# the prompts can be tech-agnostic because they no longer need to talk
# about graph node types or edge labels.


def _format_global_module_index(parsed_service_map: Dict[str, Any], max_modules: int = 100) -> str:
    """Pre-computed global module index for the HLD prompt.

    Reads `_surface.modules` from the parsed service_map JSON (written by
    recommend). Falls back to an empty block if the field is missing
    (e.g. service_map produced by an older recommend run).
    """
    surface = (parsed_service_map or {}).get("_surface") or {}
    modules = surface.get("modules") or []
    if not modules:
        return (
            "# (no surface index available — service_map was generated "
            "before iter-13.44; re-run /jobs/start/recommend to populate)"
        )
    lines: List[str] = [
        f"# LEGACY MODULE INDEX (pre-enumerated by recommend; "
        f"total_routes={surface.get('total_routes')}, "
        f"total_tables={surface.get('total_tables')}, "
        f"total_modules={surface.get('total_modules')})",
        "modules:",
    ]
    for m in modules[:max_modules]:
        lines.append(f"  - {m.get('name')}: routes={m.get('route_count')}, "
                     f"tables={m.get('table_count')}, classes={m.get('class_count')}")
        if m.get("tables"):
            lines.append(f"      tables: [{', '.join(m['tables'][:12])}"
                         f"{', ...' if len(m.get('tables') or []) > 12 else ''}]")
        if m.get("roles"):
            lines.append(f"      roles: [{', '.join(m['roles'][:6])}]")
    if len(modules) > max_modules:
        lines.append(f"  # … {len(modules) - max_modules} more module(s) elided")
    return "\n".join(lines)


def _format_service_breakdown(parsed_service_map: Dict[str, Any], services: List[Dict[str, Any]], max_per_svc: int = 12) -> str:
    """Project-wide service breakdown for HLD: one block per service with
    api_count, description, owned tables, sample routes, roles.

    Used by HLD instead of generic graph_subgraph YAML — same evidence,
    structured for human + LLM readability.
    """
    pattern = (parsed_service_map or {}).get("recommended_pattern", "?")
    lines: List[str] = [f"# SERVICE BREAKDOWN (pattern={pattern}, services={len(services)})"]
    for svc in services:
        name = svc.get("name") or "?"
        api_count = svc.get("api_count") or svc.get("route_count") or len(svc.get("api_endpoints") or [])
        tbl_count = svc.get("table_count") or len(svc.get("tables") or [])
        roles = svc.get("roles") or []
        modules = svc.get("module_names") or svc.get("modules") or []
        desc = svc.get("description") or svc.get("responsibility") or ""
        lines.append("")
        lines.append(f"## {name}")
        lines.append(f"  api_count: {api_count}")
        lines.append(f"  table_count: {tbl_count}")
        lines.append(f"  description: {desc}")
        if modules:
            lines.append(f"  modules: [{', '.join(modules[:8])}{'…' if len(modules) > 8 else ''}]")
        if roles:
            lines.append(f"  roles: [{', '.join(roles[:8])}]")
        tables = svc.get("tables") or []
        if tables:
            lines.append(f"  tables: [{', '.join(tables[:12])}{', ...' if len(tables) > 12 else ''}]")
        sample = (svc.get("api_endpoints") or [])[:max_per_svc]
        if sample:
            lines.append(f"  sample_endpoints: [{', '.join(sample)}"
                         f"{', ...' if len(svc.get('api_endpoints') or []) > max_per_svc else ''}]")
    return "\n".join(lines)


def _format_service_surface(svc: Dict[str, Any], max_routes: int = 60) -> str:
    """Per-service pre-attached surface for LLD / API contracts.

    Replaces the per-service graph_subgraph retrieval call. Contains
    EVERYTHING the LLM needs about this service's legacy footprint:
    api_count, owned tables, full routes list (verb+path+class+roles),
    owning modules, distinct roles.
    """
    name = svc.get("name") or "?"
    api_count = svc.get("api_count") or svc.get("route_count") or len(svc.get("api_endpoints") or [])
    tables = svc.get("tables") or []
    roles = svc.get("roles") or []
    modules = svc.get("module_names") or svc.get("modules") or []
    routes = svc.get("routes_detail") or []

    lines: List[str] = [
        f"# SERVICE SURFACE — {name}",
        f"api_count: {api_count}",
        f"table_count: {len(tables)}",
        f"description: {svc.get('description') or svc.get('responsibility') or ''}",
    ]
    if modules:
        lines.append(f"owning_modules: [{', '.join(modules)}]")
    if roles:
        lines.append(f"roles: [{', '.join(roles)}]")
    if tables:
        lines.append("tables:")
        for t in tables:
            lines.append(f"  - {t}")
    if routes:
        lines.append("routes:")
        for r in routes[:max_routes]:
            verb = r.get("verb") or "ANY"
            path = r.get("path") or ""
            cls = r.get("class") or ""
            rls = r.get("roles") or []
            rline = f"  - {verb} {path}"
            extras: List[str] = []
            if cls:
                extras.append(f"class={cls}")
            if rls:
                extras.append(f"roles=[{', '.join(rls)}]")
            if extras:
                rline += "  # " + ", ".join(extras)
            lines.append(rline)
        if len(routes) > max_routes:
            lines.append(f"  # … {len(routes) - max_routes} more route(s) elided "
                         f"(full set in DB / OpenAPI deliverable)")
    elif svc.get("api_endpoints"):
        # routes_detail missing (older service_map): fall back to flat list
        lines.append("routes:")
        for ep in (svc.get("api_endpoints") or [])[:max_routes]:
            lines.append(f"  - {ep}")
    return "\n".join(lines)


def _is_placeholder_name(name: str) -> bool:
    """iter-13.47 — Detect LLM-emitted placeholder names like 'service-1',
    'svc2', 'service_3', 'unknown-service', etc. so we can rewrite them
    to something meaningful from the description.
    """
    if not name:
        return True
    n = name.strip().lower()
    return bool(re.fullmatch(
        r"(service|svc|microservice|component|module|backend|app|node|ms)"
        r"[\s_\-]?\d*"
        r"(\s*-?\s*service|\s*-?\s*svc)?",
        n,
    ))


# Common stop-words that should never become a service name.
_NAME_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "for", "with", "to",
    "from", "by", "in", "on", "at", "as", "is", "are", "be", "this",
    "that", "these", "those", "service", "services", "module", "system",
    "manages", "handles", "owns", "processes", "provides", "centralizes",
    "centralises", "hosts", "performs", "executes", "tracks", "stores",
    "encapsulates", "implements", "supports", "controls", "covers",
    "wraps", "represents", "responsible", "responsibility",
}


def _name_from_description(desc: str, max_tokens: int = 3) -> str:
    """Mine a kebab-case name from the LLM-supplied description.

    Strategy: take the first 6-10 alphabetic tokens after the leading verb,
    drop stopwords, keep the first ``max_tokens`` content words.
    Examples:
        "Owns users, roles, provider/user master data..."  → "users-roles-provider"
        "Manages case intake, preauth, clinical details..." → "case-intake-preauth"
        "Handles biometric registration, enrollment sync..." → "biometric-registration-enrollment"
        "Provides case/card search, reporting views..."     → "case-card-search"
    Returns "" when nothing usable can be extracted.
    """
    if not desc:
        return ""
    # Lowercase + split on non-alphanumeric to keep slash/dash compounds
    # (provider/user → provider, user).
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{1,}", desc.lower())[:14]
    keep: List[str] = []
    for w in words:
        if w in _NAME_STOPWORDS or len(w) < 3:
            continue
        if w in keep:
            continue
        keep.append(w)
        if len(keep) >= max_tokens:
            break
    if not keep:
        return ""
    return "-".join(keep)


def _normalize_parsed_service_map(
    parsed: Dict[str, Any],
    proj: Dict[str, Any],
    surface: Dict[str, Any],
) -> Dict[str, Any]:
    """iter-13.46 — Enforce server-side invariants on the LLM-returned
    service map BEFORE module expansion + DB write.

    iter-13.47 — Also rewrite LLM-emitted placeholder names
    (`service-1`, `svc2`, …) using meaningful keywords mined from the
    `description` field, so the UI shows real names like
    `users-roles-provider` instead of "Service 1 Service".

    The LLM (especially fallback / smaller models) frequently:
      • Sets `backend_lang` to a literal default (`"nodejs"`) regardless of
        the user-chosen target stack.
      • Returns generic placeholder `name` / `display_name`.
      • Omits or returns empty `recommended_pattern`.
      • Returns blank `responsibility` / `description`.

    Mutates and returns ``parsed``.
    """
    derived_lang = _derive_backend_lang_for_arch(proj) or "nodejs"
    target_tech = (proj or {}).get("target_tech", "") or ""
    route_count = surface.get("total_routes", 0) or 0
    module_count = surface.get("total_modules", 0) or 0

    services = parsed.get("services") or []
    if not isinstance(services, list):
        services = []

    # ── 1. recommended_pattern fallback ────────────────────────────
    pattern = (parsed.get("recommended_pattern") or "").strip().lower()
    if pattern not in ("microservices", "modular_monolith", "monolith"):
        if route_count > 200 or module_count > 12:
            pattern = "microservices"
        elif route_count >= 50 or module_count >= 5:
            pattern = "modular_monolith"
        else:
            pattern = "monolith"
        parsed["recommended_pattern"] = pattern

    # ── 2. per-service normalisation ───────────────────────────────
    used_names: set = set()
    lang_overrides: List[str] = []
    name_fills: List[str] = []
    for idx, svc in enumerate(services):
        if not isinstance(svc, dict):
            continue

        # backend_lang: enforce target language (user choice is authoritative)
        llm_lang = (svc.get("backend_lang") or "").strip().lower()
        if not llm_lang or llm_lang != derived_lang.lower():
            if llm_lang and llm_lang != derived_lang.lower():
                lang_overrides.append(f"{svc.get('name','svc'+str(idx))}: {llm_lang}→{derived_lang}")
            svc["backend_lang"] = derived_lang

        # name + display_name: rewrite placeholder names using description.
        original_name = (svc.get("name") or "").strip()
        original_display = (svc.get("display_name") or "").strip()
        modules = svc.get("modules") or []
        module_names = svc.get("module_names") or []
        desc = (svc.get("description") or svc.get("responsibility") or "").strip()

        # iter-13.47 — Rewrite if the LLM gave us a generic placeholder
        # like "service-1" / "svc2" / "" — same logic for display_name
        # because the LLM often parrots the placeholder there too.
        is_placeholder = _is_placeholder_name(original_name)

        new_name: str = ""
        if is_placeholder:
            # Priority 1: mine the description for meaningful tokens.
            new_name = _name_from_description(desc, max_tokens=3)
            # Priority 2: derive from first owning module name (kept from
            # iter-13.46 — useful when description is missing too).
            if not new_name and (module_names or modules):
                first_mod = str((module_names or modules)[0]).lower()
                seed = re.sub(r"[^a-z0-9]+", "-", first_mod).strip("-")
                seed = re.sub(r"-(module|svc|service|app|ctrl|controller)$", "", seed)
                new_name = seed
            # Priority 3: ultimate fallback.
            if not new_name:
                new_name = f"domain-{idx + 1}"

            # Clean up: do NOT append "-service" if name already ends in
            # service-like suffix. This was the iter-13.46 bug that
            # produced "Service 1 Service".
            new_name = re.sub(r"^[\s\-_]+|[\s\-_]+$", "", new_name)
            if not new_name.endswith(("-service", "-svc", "-api")):
                new_name = new_name + "-service"

            # Uniquify within the service map.
            base = new_name
            n = 2
            while new_name in used_names:
                new_name = f"{base}-{n}"
                n += 1

            name_fills.append(f"#{idx}: ←'{original_name}' → '{new_name}' (mined from description)")
            svc["name"] = new_name
        used_names.add(svc["name"])

        # display_name: regenerate when blank OR when it was a placeholder
        # (so the name rewrite above is reflected in the card title).
        if not original_display or _is_placeholder_name(original_display):
            svc["display_name"] = " ".join(
                w.capitalize() for w in re.split(r"[-_\s]+", svc["name"]) if w
            )
            # Drop the trailing literal "Service" so the display reads
            # "Users Roles Provider" instead of "Users Roles Provider
            # Service" (the kebab-case `-service` suffix is enough on
            # the technical name; humans don't need the duplicate).
            svc["display_name"] = re.sub(
                r"\s+(Service|Svc|Api)$", "", svc["display_name"]
            ).strip() or svc["display_name"]

        # responsibility / description: never leave both blank
        resp = (svc.get("responsibility") or "").strip()
        d = (svc.get("description") or "").strip()
        if not resp and not d:
            mod_str = ", ".join(str(m) for m in (module_names or modules)[:3])
            stub = (
                f"Owns the {mod_str or 'unassigned'} module(s). "
                f"Generated description placeholder — LLM omitted; re-run "
                f"Recommend with a stronger model to enrich."
            )
            svc["responsibility"] = stub
            svc["description"] = stub
        elif not resp:
            svc["responsibility"] = d[:160]
        elif not d:
            svc["description"] = resp

    # ── 3. frontend_service backend_lang sanity ────────────────────
    fe = parsed.get("frontend_service") or {}
    fe_fw = (fe.get("framework") or "").strip()
    if not fe_fw:
        tt_lc = target_tech.lower()
        guess = ""
        for k in ("react", "angular", "vue", "svelte", "blazor", "thymeleaf",
                  "jsf", "next.js", "nuxt", "solid", "qwik"):
            if k in tt_lc:
                guess = k.capitalize() if k != "jsf" else "JSF"
                break
        fe["framework"] = guess
        parsed["frontend_service"] = fe

    # ── 4. surface the overrides for tracability ───────────────────
    parsed.setdefault("_normalizations", {})
    parsed["_normalizations"].update({
        "derived_backend_lang": derived_lang,
        "target_tech": target_tech,
        "backend_lang_overrides": lang_overrides,
        "name_synthesised": name_fills,
        "pattern_filled_from_counts": pattern if not (parsed.get("recommended_pattern") or "").strip() else "",
    })
    return parsed


def _expand_services_from_modules(
    parsed: Dict[str, Any],
    surface: Dict[str, Any],
) -> Dict[str, Any]:
    """Mechanically attach full route + table lists to each service based
    on the LLM-returned `modules` clustering. Guarantees 100% surface
    coverage regardless of LLM token budget.

    Mutates and returns ``parsed``. Also reports coverage stats so we can
    surface "X modules were unassigned" warnings to the user.

    iter-13.44 — also enriches each service with `routes_detail` (list of
    `{verb, path, class, roles}` dicts) and `module_names` (human readable)
    so downstream HLD / LLD / API contract jobs can consume the same
    pre-enumerated surface without re-fetching it. This is the
    "compute once, read everywhere" pattern that drives the token /
    accuracy improvement across the whole Architecture stage.
    """
    modules_by_id_or_name: Dict[str, Dict[str, Any]] = {}
    for m in surface["modules"]:
        modules_by_id_or_name[m["id"]] = m
        modules_by_id_or_name[m["name"]] = m

    services = parsed.get("services") or []
    assigned: set = set()
    total_routes_attached = 0

    for svc in services:
        svc_modules = svc.get("modules") or []
        # Back-compat: if the LLM still returned legacy `tables` /
        # `api_endpoints` arrays, preserve them as the seed; we'll union.
        existing_tables = svc.get("tables") or []
        existing_endpoints = list(svc.get("api_endpoints") or [])

        merged_routes: List[str] = []
        merged_routes_detail: List[Dict[str, Any]] = []
        seen_endpoints: set = set(existing_endpoints)
        merged_tables: set = set(existing_tables)
        roles_set: set = set()
        owning_modules: List[str] = []
        module_names_pretty: List[str] = []

        for mref in svc_modules:
            mod = modules_by_id_or_name.get(mref)
            if not mod:
                continue
            owning_modules.append(mod["id"])
            module_names_pretty.append(mod["name"])
            assigned.add(mod["id"])
            for r in mod["routes"]:
                ep = f"{r['verb']} {r['path']}".strip()
                if ep and ep not in seen_endpoints:
                    seen_endpoints.add(ep)
                    merged_routes.append(ep)
                    merged_routes_detail.append({
                        "verb": r.get("verb") or "ANY",
                        "path": r.get("path") or "",
                        "class": r.get("class") or "",
                        "roles": r.get("roles") or [],
                        "module": mod["name"],
                    })
            merged_tables.update(mod["tables"])
            roles_set.update(mod["roles"])

        # Preserve original order: existing endpoints first, then new ones.
        full_endpoints = existing_endpoints + merged_routes
        svc["api_endpoints"] = full_endpoints
        svc["routes_detail"] = merged_routes_detail
        svc["tables"] = sorted(merged_tables)
        svc["modules"] = owning_modules
        svc["module_names"] = module_names_pretty
        svc["roles"] = sorted(roles_set)
        svc["route_count"] = len(full_endpoints)
        svc["table_count"] = len(svc["tables"])
        # api_count is the canonical authoritative count, set by backend.
        # If the LLM also produced an `api_count` field, ignore it and
        # overwrite — the skeleton is truth.
        svc["api_count"] = len(full_endpoints)
        # If the LLM omitted `description`, fall back to responsibility so
        # downstream UI / prompts always have a non-empty short summary.
        if not svc.get("description"):
            svc["description"] = svc.get("responsibility", "")
        total_routes_attached += len(full_endpoints)

    unassigned_modules = [
        m for m in surface["modules"]
        if m["id"] not in assigned and m["id"] != "__orphan__" and m["route_count"] > 0
    ]
    parsed["_coverage"] = {
        "total_routes_in_kb": surface["total_routes"],
        "total_routes_attached": total_routes_attached,
        "total_modules_in_kb": surface["total_modules"],
        "modules_assigned": len(assigned - {"__orphan__"}),
        "modules_unassigned": len(unassigned_modules),
        "unassigned_module_names": [m["name"] for m in unassigned_modules[:25]],
        "coverage_pct": (
            round(100.0 * total_routes_attached / max(1, surface["total_routes"]), 1)
        ),
    }

    # If the LLM left modules with routes unassigned, auto-bucket them
    # into a generated "legacy-unsorted" service so 100% of the surface
    # area ends up represented. The user can re-cluster in Console.
    if unassigned_modules:
        extra_endpoints: List[str] = []
        extra_routes_detail: List[Dict[str, Any]] = []
        extra_tables: set = set()
        extra_modules: List[str] = []
        extra_module_names: List[str] = []
        extra_roles: set = set()
        for m in unassigned_modules:
            extra_modules.append(m["id"])
            extra_module_names.append(m["name"])
            extra_roles.update(m["roles"])
            for r in m["routes"]:
                ep = f"{r['verb']} {r['path']}".strip()
                if ep:
                    extra_endpoints.append(ep)
                    extra_routes_detail.append({
                        "verb": r.get("verb") or "ANY",
                        "path": r.get("path") or "",
                        "class": r.get("class") or "",
                        "roles": r.get("roles") or [],
                        "module": m["name"],
                    })
            extra_tables.update(m["tables"])
        services.append({
            "name": "legacy-unsorted",
            "display_name": "Legacy — Unsorted Modules (auto-bucketed)",
            "responsibility": (
                f"Auto-bucketed by backend: {len(unassigned_modules)} module(s) "
                f"and {len(extra_endpoints)} endpoint(s) were not assigned to any "
                f"LLM-proposed service. Re-cluster into proper services before freeze."
            ),
            "description": (
                f"Auto-generated container for {len(unassigned_modules)} "
                f"orphan module(s): {', '.join(extra_module_names[:8])}"
                f"{'…' if len(extra_module_names) > 8 else ''}. "
                f"Contains {len(extra_endpoints)} unallocated route(s)."
            ),
            "modules": extra_modules,
            "module_names": extra_module_names,
            "tables": sorted(extra_tables),
            "api_endpoints": extra_endpoints,
            "routes_detail": extra_routes_detail,
            "roles": sorted(extra_roles),
            "dependencies": [],
            "events_published": [],
            "events_consumed": [],
            "backend_lang": (services[0] if services else {}).get("backend_lang") or "nodejs",  # already normalised upstream
            "estimated_loc": len(extra_endpoints) * 50,
            "route_count": len(extra_endpoints),
            "table_count": len(extra_tables),
            "api_count": len(extra_endpoints),
            "_auto_generated": True,
        })
        parsed["services"] = services

    return parsed


def _assert_prompt_fresh(key: str, template: str) -> List[str]:
    """Return a list of missing placeholders. Empty == prompt is fresh."""
    needed = _REQUIRED_PLACEHOLDERS.get(key, set())
    missing = sorted(p for p in needed if "{" + p + "}" not in (template or ""))
    if missing:
        logger.warning(
            "stale prompt detected key=%s missing_placeholders=%s "
            "(run backend restart or DELETE FROM project_prompts WHERE key='%s')",
            key, missing, key,
        )
    return missing


def _log_prompt_context(key: str, **slots) -> None:
    """Log the *lengths* of the major context slots being fed to the LLM so
    we can verify graph / SRS / target tech are actually populated."""
    sizes = {k: len(str(v or "")) for k, v in slots.items()}
    logger.info("arch.prompt key=%s context_sizes=%s", key, sizes)


def _safe_format(template: str, **vars) -> str:
    return template.format(**{k: (str(v) if v is not None else "") for k, v in vars.items()})


def _derive_backend_lang_for_arch(proj: dict) -> str:
    """iter 13.8 — derive backend_lang from the detected legacy stack so
    arch.recommend stops blindly picking Node for every project.
    Falls back to the legacy `nodejs` default only when both
    detected_tech.language and target_tech are missing.
    """
    try:
        from kb.target_stack import derive_backend_lang
        detected_lang = ((proj or {}).get("detected_tech") or {}).get("language") or ""
        return derive_backend_lang(detected_lang, (proj or {}).get("target_tech", ""))
    except Exception:
        return "nodejs"


async def _strip_md_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        for tag in ("json", "yaml", "markdown", "md"):
            if t[: len(tag)].lower() == tag:
                t = t[len(tag):]
                break
        t = t.lstrip("\n")
        if t.endswith("```"):
            t = t[:-3].rstrip()
    return t


# iter-13.49 — Sanitize LLM-emitted Mermaid sequenceDiagram blocks so they
# parse under mermaid >= 11. The model frequently violates the grammar in
# small, mechanical ways (hyphenated participant IDs, missing `end` for
# `alt`/`loop`/`par`, stray Markdown prose inside the fence, double-fenced
# blocks, etc.). We fix these deterministically before storage so the
# frontend never has to render broken syntax.
_MERMAID_KEYWORDS = (
    "sequenceDiagram", "participant", "actor", "activate", "deactivate",
    "Note", "note", "loop", "end", "alt", "else", "opt", "par", "and",
    "rect", "autonumber", "title", "link", "links", "properties", "details",
    "box", "create", "destroy", "break", "critical", "option",
)


def _sanitize_mermaid_sequence(raw: str) -> str:
    """Best-effort cleanup of an LLM-emitted sequenceDiagram.

    Always returns a string wrapped in a ``` ```mermaid ... ``` ``` fence so
    the frontend extractor picks it up. Never raises — falls back to a
    minimal error stub if the input is unrecoverable.
    """
    import re as _re
    import unicodedata as _ud

    if not raw:
        return "```mermaid\nsequenceDiagram\n  Note over System: (empty LLM response)\n```"

    text = raw.strip()

    # iter-13.52 — Pre-pass: kill non-ASCII chars that mermaid 11's parser
    # struggles with: smart quotes, NBSP, zero-width spaces, BOM. Replaced
    # with their ASCII equivalents (or stripped).
    _UC_REPLACE = {
        "\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
        "\u2013": "-", "\u2014": "-",          # en/em dash → hyphen
        "\u00a0": " ",                          # NBSP → space
        "\u200b": "", "\u200c": "", "\u200d": "",  # zero-width chars
        "\ufeff": "",                          # BOM
    }
    for k, v in _UC_REPLACE.items():
        text = text.replace(k, v)
    # Strip any remaining control chars except \n / \t.
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or _ud.category(ch)[0] != "C")

    # 1. Strip ALL outer fences (handles double-fenced + bare ``` fences).
    #    Repeat until no leading/trailing fence remains.
    for _ in range(3):
        m = _re.match(r"^[ \t]*(?:```|~~~)[ \t]*(?:mermaid|mmd)?[^\n]*\n", text)
        if m:
            text = text[m.end():]
        m2 = _re.search(r"\n[ \t]*(?:```|~~~)[ \t]*$", text)
        if m2:
            text = text[: m2.start()]
        text = text.strip()
        if not (text.startswith("```") or text.startswith("~~~")):
            break

    # 2. Drop any leading prose lines until we hit `sequenceDiagram`.
    lines = text.split("\n")
    start_idx = 0
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("sequencediagram"):
            start_idx = i
            break
    else:
        # No header at all — synthesise one and assume the rest is the body.
        lines = ["sequenceDiagram"] + lines
        start_idx = 0
    lines = lines[start_idx:]

    # Normalise the header to canonical casing.
    lines[0] = "sequenceDiagram"

    # 3. Drop trailing prose lines after the last mermaid-looking statement.
    last_good = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith("%%"):
            continue
        if (
            "->>" in s or "-->>" in s or "-x" in s or "--x" in s
            or any(s.split()[0] == kw or s.startswith(kw + " ") or s.startswith(kw + ":")
                   for kw in _MERMAID_KEYWORDS if " " not in kw)
        ):
            last_good = i
    lines = lines[: last_good + 1] if last_good else lines

    # 4. Per-line repairs.
    fixed: list[str] = []
    for ln in lines:
        s = ln.rstrip()

        # 4a. Remove stray inline ``` that the LLM nested mid-diagram.
        if "```" in s:
            s = s.replace("```", "")

        # 4b. Strip HTML-only line breaks inside Notes (some Mermaid 11
        # parsers reject them in sequence notes).
        s = s.replace("<br/>", " — ").replace("<br />", " — ").replace("<br>", " — ")

        # iter-13.52 — Replace literal `\n` / `\r` escape sequences inside
        # message text with a single space. The LLM occasionally emits
        # `A->>B: line one\nline two` which mermaid 11 treats as a syntax
        # error (newline mid-statement).
        s = s.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")

        # iter-13.52 — Normalize legacy `end note` / `end alt` / `end loop`
        # / `end opt` etc. to bare `end` (mermaid 11 only accepts `end`).
        s = _re.sub(r"^(\s*)end\s+(note|alt|loop|opt|par|rect|critical|box)\s*$",
                    r"\1end", s, flags=_re.IGNORECASE)

        # 4c. participant / actor: ID before `as` must be alphanumeric + _.
        #     Rewrite hyphens/dots/slashes in the ID portion. Also handles
        #     quoted idents like: participant "Auth Service" as AS
        m = _re.match(
            # iter-13.52 — match optional quoted ident first, fall through to bare ident
            r'^(\s*)(participant|actor)\s+(?:"([^"]+)"|(\S+))(\s+as\s+(.+))?\s*$',
            s, _re.IGNORECASE,
        )
        if m:
            indent = m.group(1)
            kw = m.group(2).lower()  # iter-13.52 — normalize keyword case (Participant→participant)
            ident = m.group(3) or m.group(4) or ""
            label = m.group(6)
            safe_id = _re.sub(r"[^A-Za-z0-9_]", "_", ident).strip("_") or "P"
            if not safe_id[0].isalpha():
                safe_id = "P_" + safe_id
            if label:
                # Quote the label if it contains a comma/colon/dash to be safe.
                label_clean = label.strip().strip('"').strip("'")
                if any(ch in label_clean for ch in ",:#"):
                    label_clean = f'"{label_clean}"'
                s = f"{indent}{kw} {safe_id} as {label_clean}"
            elif safe_id != ident:
                # Preserve the original (human) label after `as`.
                # If ident had spaces (came from quoted form), wrap it in quotes.
                disp = ident if " " not in ident else f'"{ident}"'
                s = f"{indent}{kw} {safe_id} as {disp}"
            else:
                s = f"{indent}{kw} {safe_id}"

        # 4d. Rewrite hyphenated/dotted IDs on either side of an arrow.
        # iter-13.52 — also handle the activation `+`/`-` prefix on the
        # destination (`A->>+B: foo`, `A-->>-B: foo`) and ensure the
        # message is non-empty (mermaid 11 chokes on bare `:` at EOL).
        def _arrow_rewrite(mm):
            head = mm.group(1)
            src = _re.sub(r"[^A-Za-z0-9_]", "_", mm.group(2))
            arrow = mm.group(3)
            act = mm.group(4) or ""           # +/- prefix
            dst = _re.sub(r"[^A-Za-z0-9_]", "_", mm.group(5))
            msg = (mm.group(6) or "").rstrip()
            if not msg.strip():
                msg = " ."  # mermaid 11 needs at least one non-space char after :
            return f"{head}{src} {arrow} {act}{dst}:{msg}"

        s = _re.sub(
            r"(^|\s)([A-Za-z][A-Za-z0-9_\-\.\/]*?)\s*(-->>|->>|--x|-x|->|-->)\s*([+\-])?\s*"
            r"([A-Za-z][A-Za-z0-9_\-\.\/]*?)\s*:(.*)$",
            _arrow_rewrite,
            s,
        )

        # 4e. Rewrite hyphenated IDs in `Note over X,Y:` / `activate X` / `deactivate X`.
        def _safe_ids_in_csv(match):
            head = match.group(1)
            ids_csv = match.group(2)
            tail = match.group(3)
            ids = [
                _re.sub(r"[^A-Za-z0-9_]", "_", x.strip().strip('"').strip("'")) or "P"
                for x in ids_csv.split(",")
            ]
            return f"{head}{', '.join(ids)}{tail}"

        s = _re.sub(
            r"^(\s*Note\s+(?:over|left of|right of)\s+)([^:\n]+?)(\s*:)",
            _safe_ids_in_csv, s, flags=_re.IGNORECASE,
        )
        s = _re.sub(
            r"^(\s*(?:activate|deactivate)\s+)([^\s]+)(\s*)$",
            lambda mm: f"{mm.group(1)}{_re.sub(r'[^A-Za-z0-9_]', '_', mm.group(2))}{mm.group(3)}",
            s, flags=_re.IGNORECASE,
        )

        fixed.append(s)

    # 5. Auto-close unbalanced alt/loop/par/opt/rect/critical/box blocks.
    opens = 0
    for ln in fixed:
        head = ln.strip().split(" ", 1)[0].lower() if ln.strip() else ""
        if head in ("alt", "loop", "par", "opt", "rect", "critical", "box"):
            opens += 1
        elif head == "end":
            opens -= 1
    while opens > 0:
        fixed.append("end")
        opens -= 1

    body = "\n".join(fixed).rstrip()
    if not body or body.strip().lower() == "sequencediagram":
        body = "sequenceDiagram\n  Note over System: (LLM produced no diagram body)"
    return f"```mermaid\n{body}\n```"


async def _load_srs_sections(project_id: str, dm_ctx: dict | None) -> dict:
    """Return the freshest SRS sections dict, falling back across sources.

    Priority order:
      1. Live ``srs_documents`` doc (always freshest — picks up any post-
         freeze edits the user made via the SRS panel).
      2. ``stage_context(Discovery).outputs.srs_sections`` (frozen baseline).
      3. ``stage_context(DataModel).outputs.srs_sections`` (legacy compat).

    iter-13.25 — Architecture jobs used to read only 2+3 above; users
    who edited SRS sections after freeze were silently fed stale text
    and complained that HLD/LLD ignored the SRS.
    """
    sections: dict = {}
    try:
        live = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
        if live and live.get("sections"):
            sections = live["sections"]
    except Exception as e:
        logger.warning("srs_documents fetch failed for %s: %s", project_id, e)
    if not sections:
        try:
            disc = await stage_context_col.find_one(
                {"project_id": project_id, "stage": "Discovery"}, {"_id": 0}
            )
            sections = ((disc or {}).get("outputs", {}) or {}).get("srs_sections", {}) or {}
        except Exception:
            sections = {}
    if not sections and dm_ctx:
        sections = ((dm_ctx.get("outputs", {}) or {}).get("srs_sections") or {})
    if not sections:
        logger.warning(
            "SRS sections empty for project=%s — HLD/LLD/Sequence/API "
            "contracts will be generated WITHOUT SRS grounding. Freeze "
            "the SRS in Stage 1 before generating Architecture artifacts.",
            project_id,
        )
    return sections


async def _ensure_kb_graph(project_id: str) -> bool:
    """Ensure ``kb_graph`` is built for this project. Returns True if a
    graph exists (built now or previously). False on best-effort failure.

    iter-13.25 — projects created before iter-13.19 never had their graph
    auto-built; Architecture jobs then silently fell back to "no graph"
    and the user saw artifacts that ignored the codebase. Build on demand.
    """
    try:
        from db import kb_graph as kb_graph_col
        existing = await kb_graph_col.find_one({"project_id": project_id}, {"version": 1})
        if existing:
            return True
        # Build it now, best-effort.
        from kb.kb_graph import build_kb_graph
        logger.info("kb_graph missing for %s — auto-building before Architecture run", project_id)
        stats = await build_kb_graph(project_id)
        return bool(stats and stats.get("nodes_count", 0) > 0)
    except Exception as e:
        logger.warning("kb_graph auto-build failed for %s: %s", project_id, e)
        return False


async def _legacy_evidence(project_id: str, seed_terms: List[str], max_chars: int = 4000) -> str:
    """Pull raw legacy code excerpts (CLASS/ROUTE/TABLE entities with snippet
    or source path) from ``kb_entities`` for the given seed terms. Used to
    ground Architecture prompts in concrete legacy artifacts.

    Returns a compact YAML-ish block. Empty string when no entities match.
    """
    if not seed_terms:
        return ""
    seeds_lc = [t.lower() for t in seed_terms if t and len(str(t)) >= 3]
    if not seeds_lc:
        return ""
    cur = kb_entities.find({"project_id": project_id}, {"_id": 0}).limit(2000)
    buckets: Dict[str, List[str]] = {"Classes": [], "Routes": [], "Tables": []}
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
            buckets["Classes"].append(line)
            total += len(line) + 1
        elif et == "ROUTE":
            verb = (ent.get("verb") or "ANY").upper()
            roles = ", ".join(ent.get("roles") or []) or "-"
            line = f"  - {verb} {name} ({src}) roles: [{roles}]"
            buckets["Routes"].append(line)
            total += len(line) + 1
        elif et == "TABLE":
            cols = [
                (c.get("name") if isinstance(c, dict) else str(c))
                for c in (ent.get("columns") or [])
            ][:12]
            line = f"  - {name} ({src}) columns: [{', '.join(c for c in cols if c)}]"
            buckets["Tables"].append(line)
            total += len(line) + 1
    if not any(buckets.values()):
        return ""
    out: List[str] = ["# LEGACY EVIDENCE (raw kb_entities slice)"]
    for label in ("Classes", "Routes", "Tables"):
        if buckets[label]:
            out.append(f"{label}:")
            out.extend(buckets[label][:40])
    return "\n".join(out)[:max_chars]


async def _save_arch_doc(project_id: str, type_: str, content: str, model: str, tracability: dict) -> dict:
    existing = await arch_documents.find_one({"project_id": project_id, "type": type_}, {"_id": 0})
    version = ((existing or {}).get("version", 0)) + 1
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": (existing or {}).get("id") or __import__("uuid").uuid4().hex,
        "project_id": project_id,
        "type": type_,
        "content": content,
        "version": version,
        "frozen": (existing or {}).get("frozen", False),
        "frozen_at": (existing or {}).get("frozen_at"),
        "generated_by": model,
        "tracability": tracability,
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
    }
    await arch_documents.update_one(
        {"project_id": project_id, "type": type_},
        {"$set": doc},
        upsert=True,
    )
    return doc


# -----------------------------------------------------------
# A. recommend (job)
# -----------------------------------------------------------
def _repair_truncated_json(s: str) -> str | None:
    """Best-effort repair of a JSON object that was cut off mid-stream
    (LLM hit `max_tokens`). Drops the incomplete trailing element and
    closes every still-open array / object.

    Strategy: scan the string string-aware, find the last index at which
    the structure sat at an array-element boundary (a `}`/`]`/scalar that
    closed while the parent container was `[`, or the root closed), then
    re-balance the surviving prefix by appending the required closers.
    """
    if not s:
        return None
    in_str = False
    esc = False
    stack: list[str] = []
    last_safe: int | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            # A close that leaves us inside an array (or at root) is a
            # clean place to truncate — it ends a complete element.
            if not stack or stack[-1] == "[":
                last_safe = i + 1
    if last_safe is None:
        return None
    head = s[:last_safe]
    # Recompute the open-container stack for the surviving prefix so we
    # know exactly which closers to append.
    in_str = False
    esc = False
    stack2: list[str] = []
    for ch in head:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack2.append(ch)
        elif ch in "}]":
            if stack2:
                stack2.pop()
    closers = "".join("]" if c == "[" else "}" for c in reversed(stack2))
    return head + closers


def _lenient_json_parse(raw: str) -> dict | None:
    """Parse an LLM JSON payload tolerantly: raw → first{…}last} slice →
    truncation-repair. Returns a dict on success, else None."""
    if not raw or not raw.strip():
        return None
    candidates: list[str] = [raw]
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last > first:
        candidates.append(raw[first:last + 1])
    for cand in candidates:
        try:
            out = json.loads(cand)
            if isinstance(out, dict):
                return out
        except Exception:
            continue
    # Truncated mid-stream (LLM hit max_tokens) — repair + retry.
    repaired = _repair_truncated_json(raw[first:] if first != -1 else raw)
    if repaired:
        try:
            out = json.loads(repaired)
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    return None


async def _run_recommend_job(jid: str, project_id: str, model: str, override_message: str = ""):
    try:
        _job_update(jid, status="running", step="Loading DataModel context…", pct=4)
        dm_ctx = await require_stage_context(project_id, "DataModel", "Architecture")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        await _ensure_kb_graph(project_id)

        # iter-13.43 — DETERMINISTIC enumeration of the legacy surface.
        # This is the core fix: the LLM no longer has to "discover" 500+
        # endpoints from a truncated context window. We enumerate every
        # route + table + module from kb_graph (or kb_entities fallback)
        # before calling the model, hand it a compact module-level
        # skeleton, and mechanically attach the full route/table lists
        # to each service after the model returns. 100% surface coverage,
        # ~10x fewer prompt tokens, no hallucinated endpoints.
        _job_update(jid, step="Enumerating legacy surface (routes / tables / modules)…", pct=10)
        surface = await _enumerate_legacy_surface(project_id)
        logger.info(
            "arch.recommend surface: project=%s source=%s routes=%d tables=%d modules=%d",
            project_id, surface.get("source"),
            surface["total_routes"], surface["total_tables"], surface["total_modules"],
        )
        surface_skeleton = _format_surface_skeleton(surface, max_modules=80)

        # SRS slices (NO truncation tricks — these are what the LLM
        # actually needs to ground service responsibilities in UC-IDs).
        srs_sections = await _load_srs_sections(project_id, dm_ctx)
        srs_summary = (
            (srs_sections.get("introduction", "") or srs_sections.get("scope", ""))
            + "\n"
            + (srs_sections.get("specific_requirements", "") or srs_sections.get("functional_requirements", ""))
        )[:4000]
        srs_use_cases_r = (
            srs_sections.get("detailed_use_cases", "")
            or srs_sections.get("actors_use_case_inventory", "")
            or ""
        )[:6000]
        srs_nfr_r = (srs_sections.get("non_functional_requirements", "") or "")[:2000]

        # OLTP table NAME list (compact — full DDL is wasted tokens here;
        # the skeleton already names the tables each module touches).
        oltp_ddl_full = (dm_ctx.get("outputs", {}) or {}).get("oltp_ddl", "") or ""
        oltp_table_names = sorted(set(re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)[\"`]?",
            oltp_ddl_full, flags=re.IGNORECASE,
        )))
        oltp_table_list = (
            "tables: [" + ", ".join(oltp_table_names) + f"]  # count={len(oltp_table_names)}"
            if oltp_table_names else "(no OLTP tables parsed — Stage 2 may not be frozen properly)"
        )

        # Complexity signals — now driven by the REAL counts, not by the
        # incomplete DataModel boundaries heuristic that was previously
        # 0-9. A system with 500+ routes is unambiguously microservices.
        table_count = surface["total_tables"] or len(oltp_table_names)
        route_count = surface["total_routes"]
        module_count = surface["total_modules"]
        srs_nfr_lc = srs_nfr_r.lower()
        score = 0
        if route_count > 50: score += 1
        if route_count > 150: score += 2
        if route_count > 400: score += 2
        if table_count > 80: score += 1
        if table_count > 200: score += 2
        if module_count > 8: score += 1
        if module_count > 20: score += 1
        if "concurrent" in srs_nfr_lc: score += 1
        if "10000" in srs_nfr_lc or "high load" in srs_nfr_lc or "scalab" in srs_nfr_lc: score += 1
        complexity_signals = (
            f"Routes: {route_count} | Tables: {table_count} | Modules: {module_count} | "
            f"Complexity score: {score}/12"
        )

        _job_update(jid, step="Building prompt (compact module skeleton)…", pct=20)
        template = await _get_prompt(project_id, "arch.recommend")
        if not template:
            _job_finish(jid, "error", error="arch.recommend prompt missing.")
            return
        _assert_prompt_fresh("arch.recommend", template)
        _log_prompt_context(
            "arch.recommend",
            target_tech=proj.get("target_tech", ""),
            surface_skeleton=surface_skeleton,
            oltp_table_list=oltp_table_list,
            srs_summary=srs_summary,
            srs_use_cases=srs_use_cases_r,
            srs_nfr=srs_nfr_r,
            complexity_signals=complexity_signals,
        )

        system_prompt = _safe_format(
            template,
            project_name=proj.get("name", ""),
            source_tech=proj.get("source_tech", ""),
            backend_lang=_derive_backend_lang_for_arch(proj),
            target_tech=proj.get("target_tech", ""),
            srs_summary=srs_summary,
            srs_use_cases=srs_use_cases_r,
            srs_nfr=srs_nfr_r,
            oltp_table_list=oltp_table_list,
            surface_skeleton=surface_skeleton,
            complexity_signals=complexity_signals,
            # Back-compat placeholders for any project_prompts override
            # that still references the v4/v5 slot names. Safe to leave
            # empty — _safe_format handles missing keys gracefully.
            oltp_summary=oltp_table_list,
            module_context=surface_skeleton,
            rag_context="(omitted — skeleton supersedes)",
            graph_subgraph="(omitted — skeleton supersedes)",
            legacy_evidence="(omitted — skeleton supersedes)",
        )
        system_prompt = await _with_completeness_contract(project_id, system_prompt)
        user_msg = override_message or (
            f"Cluster the {module_count} enumerated modules into services. "
            f"Return JSON only. Do NOT enumerate individual endpoints — they "
            f"will be attached by the backend from the skeleton."
        )

        _job_update(jid, step="Calling LLM (60-90s typical)…", pct=30)

        # iter-14.32 — recommend must emit one stub per business module.
        # Large legacy apps (ceots: 74 modules) blew past the old 8k cap,
        # truncating the JSON mid-object → "LLM returned non-JSON" → the UI
        # froze at ~58%. Budget now scales with module count and the parse
        # is truncation-tolerant, with one retry when the fabric returns an
        # empty body (transient Factory-CLI routing failures).
        _rec_max_tokens = max(8000, min(24000, 4000 + module_count * 180))

        async def _call_llm():
            return await chat_completion(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
                model=model, temperature=0.2, max_tokens=_rec_max_tokens, timeout=240.0,
                # iter-13.45 — explicit agent_key + project_id so the fabric
                # routes to the right tier and Factory.ai session.
                agent_key="arch.recommend",
                project_id=project_id,
            )

        parsed = None
        raw = ""
        last_err = ""
        for _attempt in range(2):
            gen_task = asyncio.create_task(_call_llm())
            import time as _t
            started = _t.time()
            while not gen_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(gen_task), timeout=3.0)
                except asyncio.TimeoutError:
                    el = int(_t.time() - started)
                    _job_update(jid, step=f"LLM analysing architecture ({el}s elapsed)…",
                                pct=min(85, 30 + int(55 * (1 - 1 / (1 + el / 25)))))
                except Exception:
                    break
            try:
                r = await gen_task
            except Exception as e:
                last_err = f"LLM failed: {e}"
                logger.warning("arch.recommend LLM call raised (attempt %d): %s", _attempt + 1, e)
                continue

            raw = await _strip_md_fence(r.get("content", "") or "")
            parsed = _lenient_json_parse(raw)
            if parsed is not None:
                if _attempt > 0:
                    logger.info("arch.recommend recovered on retry attempt %d", _attempt + 1)
                break
            last_err = (
                "empty LLM response (fabric routing may have failed)"
                if not raw.strip() else "LLM returned non-JSON / truncated output"
            )
            logger.warning(
                "arch.recommend parse failed (attempt %d): %s (raw_len=%d)",
                _attempt + 1, last_err, len(raw),
            )

        if parsed is None:
            _job_finish(jid, "error", pct=58,
                        error=f"Recommendation failed: {last_err}. Please retry.")
            return

        # iter-13.43 — mechanically expand `services[].modules` → full
        # route + table lists from the enumerated surface. Guarantees
        # 100% surface coverage regardless of LLM token budget.
        _job_update(jid, step="Normalising service map (enforce target language + names + pattern)…", pct=88)
        # iter-13.46 — server-enforce backend_lang / name / pattern before
        # expansion so weak LLM outputs don't propagate "NODEJS" badges +
        # blank names + "Recommended pattern: —" into the UI.
        parsed = _normalize_parsed_service_map(parsed, proj, surface)
        normalizations = parsed.pop("_normalizations", {})
        if normalizations.get("backend_lang_overrides"):
            logger.warning(
                "arch.recommend forced backend_lang override for %d service(s) "
                "(target_tech=%r, derived=%r): %s",
                len(normalizations["backend_lang_overrides"]),
                normalizations.get("target_tech"),
                normalizations.get("derived_backend_lang"),
                normalizations["backend_lang_overrides"][:10],
            )
        if normalizations.get("name_synthesised"):
            logger.warning(
                "arch.recommend synthesised %d service name(s) (LLM left blank): %s",
                len(normalizations["name_synthesised"]),
                normalizations["name_synthesised"][:10],
            )
        _job_update(jid, step="Attaching enumerated routes + tables to services…", pct=90)
        parsed = _expand_services_from_modules(parsed, surface)
        coverage = parsed.pop("_coverage", {})
        # iter-14.22 — per-service API PARITY metadata. The KB drives the
        # legacy endpoint set (`routes_detail`); after expansion, every
        # route becomes a `VERB path` entry in `api_endpoints`. Parity =
        # target_endpoint_count / legacy_endpoint_count (unique path+verb).
        # By construction of _expand_services_from_modules the two sets are
        # identical (attachment is 1:1), so coverage_pct is 100% under the
        # happy path. Any drift will show up here and block CodeGen.
        _annotate_parity(parsed, project_id, logger_=logger)

        # iter-13.44 — persist the canonical surface skeleton inside the
        # service_map JSON so HLD / LLD / API contracts can read it
        # without re-enumerating from kb_graph. This is the "compute
        # once, read everywhere" pattern: every downstream stage in
        # Architecture now operates on the same pre-attached evidence.
        parsed["_surface"] = {
            "source": surface.get("source"),
            "total_routes": surface["total_routes"],
            "total_tables": surface["total_tables"],
            "total_modules": surface["total_modules"],
            # Compact module list (no per-route detail to keep it small —
            # full per-route detail lives on each `services[].routes_detail`)
            "modules": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "route_count": m["route_count"],
                    "table_count": len(m["tables"]),
                    "class_count": m["class_count"],
                    "tables": m["tables"][:30],
                    "roles": m["roles"][:10],
                }
                for m in surface["modules"][:120]
            ],
        }
        logger.info(
            "arch.recommend coverage: project=%s attached=%d/%d routes (%.1f%%) "
            "modules_assigned=%d unassigned=%d",
            project_id, coverage.get("total_routes_attached", 0),
            coverage.get("total_routes_in_kb", 0),
            coverage.get("coverage_pct", 0.0),
            coverage.get("modules_assigned", 0),
            coverage.get("modules_unassigned", 0),
        )

        # iter-13.53 — Detect services the LLM proposed but assigned NO
        # modules / endpoints to. Common when a weaker model invents
        # services like "Annual Checkup Service" without picking owning
        # modules. Surface them in the rationale so the user sees the
        # problem on the Service Map tab and can re-cluster (or rerun
        # with a stronger model) before generating LLD/API contracts.
        zero_route_services = [
            (s.get("display_name") or s.get("name") or "?")
            for s in (parsed.get("services") or [])
            if not (s.get("api_endpoints") or [])
        ]
        if zero_route_services:
            warn = (
                f"WARNING: {len(zero_route_services)} service(s) have 0 endpoints "
                f"attached — the LLM proposed them without owning modules: "
                f"{', '.join(zero_route_services[:6])}"
                f"{'…' if len(zero_route_services) > 6 else ''}. "
                f"Re-cluster on the Service Map tab or re-run Recommend with a "
                f"stronger model. API Contracts will SKIP these services."
            )
            existing = (parsed.get("rationale") or "").strip()
            parsed["rationale"] = (warn + " " + existing).strip()
            logger.warning("arch.recommend zero-route services: %s", zero_route_services[:10])

        _job_update(jid, step="Saving service map…", pct=94)
        tracability = {
            "datamodel_version": dm_ctx.get("version"),
            "oltp_table_count": table_count,
            "route_count_total": route_count,
            "module_count_total": module_count,
            "complexity_score": score,
            "model": model,
            "prompt_key": "arch.recommend",
            # iter-13.43 — coverage telemetry: was the model honest about
            # the legacy surface, or did it drop modules on the floor?
            "surface_source": surface.get("source"),
            "routes_attached": coverage.get("total_routes_attached", 0),
            "coverage_pct": coverage.get("coverage_pct", 0.0),
            "modules_unassigned": coverage.get("modules_unassigned", 0),
            "unassigned_module_names": coverage.get("unassigned_module_names", []),
            "srs_use_cases_chars": len(srs_use_cases_r or ""),
            "srs_nfr_chars": len(srs_nfr_r or ""),
        }
        # Visible banner in the service_map JSON when coverage is bad.
        if isinstance(parsed, dict):
            rat = parsed.get("rationale") or ""
            warns: List[str] = []
            if surface["total_routes"] == 0:
                warns.append("WARNING: KB has 0 routes — Build KB before recommending services.")
            elif coverage.get("coverage_pct", 0) < 95.0:
                warns.append(
                    f"WARNING: only {coverage.get('coverage_pct', 0)}% of "
                    f"{surface['total_routes']} legacy routes were attached to services. "
                    f"{coverage.get('modules_unassigned', 0)} module(s) auto-bucketed into "
                    f"`legacy-unsorted` — re-cluster before freeze."
                )
            if warns:
                parsed["rationale"] = (" ".join(warns) + " " + rat).strip()
        art = await _save_arch_doc(project_id, "service_map", json.dumps(parsed, indent=2), model, tracability)

        # iter-13.46 — derive canonical backend lang once so every default
        # below (legacy-unsorted bucket, svc_docs writer, frontend service)
        # uses the user-chosen target language instead of literal "nodejs".
        canonical_backend_lang = _derive_backend_lang_for_arch(proj) or "nodejs"

        # Replace existing service definitions
        await arch_services.delete_many({"project_id": project_id})
        services = parsed.get("services", [])
        now = datetime.now(timezone.utc).isoformat()
        svc_docs = []
        for s in services:
            svc_docs.append({
                "id": __import__("uuid").uuid4().hex,
                "project_id": project_id,
                "name": s.get("name", "service"),
                "display_name": s.get("display_name", s.get("name", "")),
                "pattern": parsed.get("recommended_pattern", "microservice"),
                # iter-13.46 — fall back to the derived target language, NOT
                # to literal "nodejs". Normalizer already enforces this on
                # `s["backend_lang"]`, so this default fires only on rare
                # paths where normalizer didn't touch the service.
                "backend_lang": s.get("backend_lang") or canonical_backend_lang,
                "frontend": False,
                "tables": s.get("tables", []) or [],
                "api_endpoints": s.get("api_endpoints", []) or [],
                # iter-13.44 — persist the enriched per-service surface so
                # HLD / LLD / API contract prompts can read it directly
                # instead of re-fetching graph subgraphs (cheaper + more
                # accurate because all four arch jobs see identical data).
                "routes_detail": s.get("routes_detail", []) or [],
                "modules": s.get("modules", []) or [],
                "module_names": s.get("module_names", []) or [],
                "roles": s.get("roles", []) or [],
                "route_count": s.get("route_count", len(s.get("api_endpoints", []) or [])),
                "table_count": s.get("table_count", len(s.get("tables", []) or [])),
                "api_count": s.get("api_count", len(s.get("api_endpoints", []) or [])),
                "description": s.get("description", s.get("responsibility", "")),
                "dependencies": s.get("dependencies", []) or [],
                "events_published": s.get("events_published", []) or [],
                "events_consumed": s.get("events_consumed", []) or [],
                "status": "pending",
                "codegen_status": "pending",
                "source_module": "",
                "responsibility": s.get("responsibility", ""),
                "estimated_loc": s.get("estimated_loc", 0),
                "created_at": now,
                "updated_at": now,
            })
        # Add frontend as a service
        fe = parsed.get("frontend_service") or {}
        svc_docs.append({
            "id": __import__("uuid").uuid4().hex,
            "project_id": project_id,
            "name": fe.get("name", "frontend"),
            "display_name": "React Frontend",
            "pattern": "frontend",
            "backend_lang": "react",
            "frontend": True,
            "tables": [],
            "api_endpoints": [],
            "dependencies": fe.get("api_consumers", []) or [],
            "events_published": [], "events_consumed": [],
            "status": "pending", "codegen_status": "pending",
            "source_module": "", "responsibility": "User interface",
            "estimated_loc": 0,
            "created_at": now, "updated_at": now,
        })
        if svc_docs:
            await arch_services.insert_many([{**d} for d in svc_docs])

        await audit_log.insert_one({
            "action": "architecture.recommend",
            "project_id": project_id,
            "at": now,
            "details": {
                "pattern": parsed.get("recommended_pattern"),
                "service_count": len(services),
                # iter-13.43 — actual coverage metrics
                "total_routes_in_kb": surface["total_routes"],
                "routes_attached": coverage.get("total_routes_attached", 0),
                "coverage_pct": coverage.get("coverage_pct", 0.0),
                "modules_unassigned": coverage.get("modules_unassigned", 0),
            },
        })

        _job_finish(jid, "complete", step="Done", pct=100,
                    result={"arch_doc_id": art["id"], "version": art["version"],
                            "pattern": parsed.get("recommended_pattern"),
                            "service_count": len(services),
                            "complexity_score": score,
                            "total_routes": surface["total_routes"],
                            "routes_attached": coverage.get("total_routes_attached", 0),
                            "coverage_pct": coverage.get("coverage_pct", 0.0)})
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/recommend")
async def start_recommend(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "DataModel", "Architecture")
    # iter-13.81.3 — re-run? → architecture.regenerate bucket (Console pin)
    _existing = await arch_services.find_one({"project_id": project_id}, {"_id": 1})
    set_current_agent_key("arch.regenerate" if _existing else "arch.recommend")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    msg = payload.get("message", "")
    jid = _new_job(project_id, "recommend")
    asyncio.create_task(_run_recommend_job(jid, project_id, model, msg))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# B. approve service map
# -----------------------------------------------------------
@router.post("/approve")
async def approve_service_map(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    approved = bool(payload.get("approved", True))
    overrides = payload.get("overrides") or []
    # iter-13.57 — per-service selection (checkboxes in ServiceMapView).
    # When provided, services NOT in this list are removed from
    # arch_services AND filtered out of the service_map artifact's JSON
    # body so every downstream job (HLD / LLD / Sequence / API / CodeGen)
    # operates on only the user-selected subset. When omitted (legacy
    # callers / tests), all services pass through unchanged.
    raw_selected = payload.get("selected_service_names")
    selected: list[str] | None = None
    if isinstance(raw_selected, list):
        selected = [str(s).strip() for s in raw_selected if str(s).strip()]
    # iter-13.59 — per-utility checkboxes (audit-logger etc.). When the
    # frontend sends this list, we upsert matching `arch_services` rows
    # with kind="utility" and prune utilities the user removed this round.
    raw_utils = payload.get("selected_utilities")
    selected_utils: list[dict] | None = None
    if isinstance(raw_utils, list):
        selected_utils = [u for u in raw_utils if isinstance(u, dict) and u.get("key")]
    sm = await arch_documents.find_one({"project_id": project_id, "type": "service_map"}, {"_id": 0})
    if not sm:
        raise HTTPException(404, "Service map not found — run /jobs/start/recommend first.")

    for o in overrides:
        name = o.get("service_name")
        if not name:
            continue
        upd = {}
        if "backend_lang" in o:
            upd["backend_lang"] = o["backend_lang"]
        if "pattern" in o:
            upd["pattern"] = o["pattern"]
        if upd:
            await arch_services.update_one({"project_id": project_id, "name": name}, {"$set": upd})

    pruned_count = 0
    if selected is not None:
        if not selected:
            raise HTTPException(400, "At least one service must be selected before approval.")
        # Drop deselected rows from arch_services so HLD/LLD/Sequence/API/CodeGen
        # never see them. The service_map artifact body is also rewritten so
        # the frozen JSON reflects the user's choice.
        all_names = await arch_services.distinct("name", {"project_id": project_id})
        # iter-13.59 — never prune utility rows via the business-service
        # selection list; utilities are managed separately below.
        util_names = await arch_services.distinct(
            "name", {"project_id": project_id, "kind": "utility"},
        )
        # iter-13.122 — the frontend row (frontend=True) is NEVER present in
        # `selected_service_names` because the caller derives that list from
        # `service_map.services[]` which only carries business services. Prior
        # to this fix the pruner silently deleted the frontend row on every
        # approve → CodeGen then had no FE plan and, when combined with a
        # post-merge state where only one business service was selected,
        # produced the "No architecture services found" toast. Exclude
        # `frontend: True` rows from both the candidate list AND the delete
        # filter (belt-and-braces).
        fe_names = await arch_services.distinct(
            "name", {"project_id": project_id, "frontend": True},
        )
        to_remove = [
            n for n in all_names
            if n not in selected
            and n not in (util_names or [])
            and n not in (fe_names or [])
        ]
        if to_remove:
            res = await arch_services.delete_many(
                {"project_id": project_id, "name": {"$in": to_remove},
                 "kind": {"$ne": "utility"},
                 "frontend": {"$ne": True}}
            )
            pruned_count = res.deleted_count
        # Filter the JSON body too.
        try:
            sm_json = json.loads(sm.get("content", "") or "{}")
            svcs = sm_json.get("services") or []
            sm_json["services"] = [s for s in svcs if (s.get("name") or "") in selected]
            sm_json["deselected_services"] = to_remove
            new_content = json.dumps(sm_json, indent=2)
            await arch_documents.update_one(
                {"project_id": project_id, "type": "service_map"},
                {"$set": {"content": new_content,
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
        except Exception as _e:
            logger.warning("approve: failed to filter service_map JSON body: %s", _e)

    # iter-13.59 — Upsert / prune utility services. Utilities live as
    # arch_services rows with kind="utility" so the CodeGen plan picks
    # them up naturally. Frontend always sends the full set the user
    # currently wants; anything missing here gets removed.
    utilities_persisted = 0
    if selected_utils is not None:
        import uuid as _uuid
        now_iso = datetime.now(timezone.utc).isoformat()
        selected_keys: set[str] = set()
        for u in selected_utils:
            meta = get_utility_meta(u.get("key"))
            if not meta:
                continue
            selected_keys.add(meta["key"])
            cfg = u.get("config") or {}
            existing = await arch_services.find_one(
                {"project_id": project_id, "name": meta["name"]}, {"_id": 0},
            )
            doc = {
                "id":             (existing or {}).get("id") or _uuid.uuid4().hex,
                "project_id":     project_id,
                "name":           meta["name"],
                "display_name":   meta["display_name"],
                "kind":           "utility",
                "utility_type":   meta["key"],
                "utility_config": cfg,
                "backend_lang":   "any",
                "frontend":       False,
                "tables":         [],
                "api_endpoints":  [],
                "dependencies":   [],
                "events_published": [],
                "events_consumed":  [],
                "responsibility": meta["description"],
                "status":         "pending",
                "codegen_status": "pending",
                "source_module":  "",
                "estimated_loc":  0,
                "created_at":     (existing or {}).get("created_at", now_iso),
                "updated_at":     now_iso,
            }
            await arch_services.update_one(
                {"project_id": project_id, "name": meta["name"]},
                {"$set": doc}, upsert=True,
            )
            utilities_persisted += 1
        # Prune utilities the user just deselected.
        await arch_services.delete_many({
            "project_id": project_id, "kind": "utility",
            "utility_type": {"$nin": list(selected_keys)},
        })

    # iter-13.122 — self-healing frontend row. If a previous approve (or a
    # legacy code path) accidentally pruned the frontend service, re-create
    # it from the service_map JSON body's `frontend_service` block. This is
    # cheap, idempotent, and unblocks CodeGen's `_frontend_files_plan()`.
    await _ensure_frontend_service(project_id)

    stage_frozen = False
    if approved:
        now = datetime.now(timezone.utc).isoformat()
        await arch_documents.update_one(
            {"project_id": project_id, "type": "service_map"},
            {"$set": {"frozen": True, "frozen_at": now, "updated_at": now}},
        )
        # iter-13.81.13 — Approving the Service Map IS the Architecture
        # stage freeze. HLD / LLD / Sequence / API Contracts are now
        # OPTIONAL deliverables that the user can generate before or
        # after CodeGen. CodeGen only needs the per-service `arch_services`
        # rows (which Approve has just finalised) — not the HLD/LLD bodies.
        stage_frozen = await _promote_architecture_stage(project_id, frozen_by="user")
        await audit_log.insert_one({
            "action": "architecture.approve",
            "project_id": project_id,
            "at": now,
            "details": {
                "overrides": overrides,
                "selected_service_names": selected,
                "pruned_services": pruned_count,
                "selected_utilities": [u.get("key") for u in (selected_utils or [])],
                "utilities_persisted": utilities_persisted,
                "stage_frozen": stage_frozen,
            },
        })

    return {"ok": True, "approved": approved, "pruned_services": pruned_count,
            "utilities_persisted": utilities_persisted,
            "stage_frozen": stage_frozen}


# iter-13.122 — Self-healing helper. Ensures the frontend `arch_services`
# row exists whenever the service_map JSON body declares one. Called from
# `/approve` so a previously-pruned FE row is transparently restored on
# the next approve cycle; also safe to call from anywhere else.
async def _ensure_frontend_service(project_id: str) -> bool:
    existing = await arch_services.find_one(
        {"project_id": project_id, "frontend": True}, {"_id": 0, "name": 1},
    )
    if existing:
        return False
    sm = await arch_documents.find_one(
        {"project_id": project_id, "type": "service_map"}, {"_id": 0},
    )
    if not sm:
        return False
    try:
        sm_data = json.loads(sm.get("content", "") or "{}")
    except Exception:
        sm_data = {}
    fe = sm_data.get("frontend_service") or {}
    # Only auto-create when the service map itself opted-in to a frontend.
    if not fe and not (sm_data.get("services") or []):
        return False
    import uuid as _uuid
    now_iso = datetime.now(timezone.utc).isoformat()
    await arch_services.insert_one({
        "id":            _uuid.uuid4().hex,
        "project_id":    project_id,
        "name":          fe.get("name", "frontend"),
        "display_name":  fe.get("display_name", "React Frontend"),
        "pattern":       "frontend",
        "backend_lang":  "react",
        "frontend":      True,
        "tables":        [],
        "api_endpoints": [],
        "routes_detail": [],
        "modules":       [],
        "module_names":  [],
        "roles":         [],
        "dependencies":  fe.get("api_consumers", []) or [],
        "events_published": [],
        "events_consumed":  [],
        "status":        "pending",
        "codegen_status": "pending",
        "source_module": "",
        "responsibility": fe.get("responsibility", "User interface"),
        "estimated_loc": 0,
        "created_at":    now_iso,
        "updated_at":    now_iso,
    })
    return True


# iter-13.81.13 — Centralised stage promotion. Used by:
#   • /approve            — Service Map approval (now the canonical freeze)
#   • /artifact/{id}/freeze — back-compat path (freezing any single artifact
#                            still promotes the stage as long as the Service
#                            Map is frozen).
# Returns True when the stage was promoted (or already frozen).
# iter-14.21 — Cleanup helper for merge operations.
# Symptom this fixes: user merges N services in Architecture (via
# `/merge-services` or the batch endpoint). `arch_services` correctly
# collapses to 1 row, but the previously-generated `codegen_files`
# rows keyed by the old `service_name` remain. The CodeGen UI reads
# `codegen_files` directly (see routes/codegen.py::list_files) so it
# STILL shows N separate service source trees — exactly what the user
# reported: "4 BE projects still showing as 4 separate projects after
# I merged them into ceots".
#
# We now cascade-delete every artifact keyed by a source service name
# that isn't the merged target. Safe to run before OR after the merge
# (source names uniquely identify the rows).
async def _cleanup_merged_service_artifacts(
    project_id: str, source_names: List[str], merged_name: str,
) -> Dict[str, int]:
    victims = [n for n in (source_names or []) if n and n != merged_name]
    stats = {"codegen_files_deleted": 0, "codegen_runs_updated": 0}
    if not victims:
        return stats
    try:
        r = await codegen_files.delete_many(
            {"project_id": project_id, "service_name": {"$in": victims}},
        )
        stats["codegen_files_deleted"] = int(getattr(r, "deleted_count", 0) or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning("merge cleanup: codegen_files delete failed: %s", e)
    # Prune per-service confidence blocks from the most recent codegen_runs
    # so the UI's confidence badge doesn't report a stale service name.
    try:
        recent = await codegen_runs.find(
            {"project_id": project_id},
        ).sort("started_at", -1).to_list(5)
        for run in recent:
            per_svc = (run.get("per_service_confidence") or {})
            new_map = {k: v for k, v in per_svc.items() if k not in victims}
            if len(new_map) != len(per_svc):
                await codegen_runs.update_one(
                    {"id": run.get("id")},
                    {"$set": {"per_service_confidence": new_map}},
                )
                stats["codegen_runs_updated"] += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("merge cleanup: codegen_runs sweep failed: %s", e)
    try:
        await audit_log.insert_one({
            "action":     "architecture.merge_services.cleanup",
            "project_id": project_id,
            "at":         datetime.now(timezone.utc).isoformat(),
            "details":    {"source_names": victims, "merged_name": merged_name,
                           **stats},
        })
    except Exception:  # noqa: BLE001
        pass
    return stats


async def _promote_architecture_stage(project_id: str, frozen_by: str = "system") -> bool:
    sm = await arch_documents.find_one(
        {"project_id": project_id, "type": "service_map"}, {"_id": 0},
    )
    if not sm or not sm.get("frozen"):
        return False
    try:
        sm_data = json.loads(sm.get("content", "") or "{}")
    except Exception:
        sm_data = {}
    services = await arch_services.find(
        {"project_id": project_id}, {"_id": 0},
    ).to_list(200)
    biz_services = [s for s in services if s.get("kind") != "utility" and not s.get("frontend")]
    hld = await arch_documents.find_one(
        {"project_id": project_id, "type": "hld"}, {"_id": 0},
    ) or {}
    lld = await arch_documents.find_one(
        {"project_id": project_id, "type": "lld"}, {"_id": 0},
    ) or {}
    seq = await arch_documents.find_one(
        {"project_id": project_id, "type": "sequence_diagrams"}, {"_id": 0},
    ) or {}
    api = await arch_documents.find_one(
        {"project_id": project_id, "type": "api_contracts"}, {"_id": 0},
    ) or {}
    outputs = {
        "pattern": sm_data.get("recommended_pattern"),
        "services": services,
        # iter-13.81.13 — these blocks are OPTIONAL. Empty strings are valid;
        # CodeGen only uses them as supplementary prompt context.
        "hld_content": hld.get("content", ""),
        "lld_content": lld.get("content", ""),
        "api_contracts": api.get("content", ""),
        "sequence_diagrams": seq.get("content", ""),
        "service_count": len(services),
        "frontend_service": sm_data.get("frontend_service", {}),
        "backend_lang": (biz_services[0]["backend_lang"]
                         if biz_services else "nodejs"),
        "event_bus": sm_data.get("event_bus", False),
        # Provenance — which optional deliverables actually exist?
        "optional_deliverables": {
            "hld": bool(hld.get("frozen")),
            "lld": bool(lld.get("frozen")),
            "sequence_diagrams": bool(seq.get("frozen")),
            "api_contracts": bool(api.get("frozen")),
        },
    }
    dm_ctx = await stage_context_col.find_one(
        {"project_id": project_id, "stage": "DataModel"}, {"_id": 0},
    )
    sources = {
        "datamodel_version": (dm_ctx or {}).get("version"),
        "service_map_version": sm.get("version"),
        "prompts_used": ["arch.recommend"]
            + (["arch.hld"] if hld.get("frozen") else [])
            + (["arch.lld"] if lld.get("frozen") else [])
            + (["arch.sequence"] if seq.get("frozen") else [])
            + (["arch.api_contracts"] if api.get("frozen") else []),
    }
    await save_stage_context(
        project_id, "Architecture", outputs, sources, frozen_by=frozen_by,
    )
    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.Architecture": "frozen",
                  "stage_status.CodeGen": "available",
                  "updated_at": now}},
    )
    return True


# -----------------------------------------------------------
# iter-13.81.13 — Merge / unmerge services
# -----------------------------------------------------------
# Lets the user collapse N services from the recommended service map
# into ONE combined service that CodeGen will materialise as a single
# source tree. The merged row carries the UNION of every source's
# api_endpoints / tables / routes_detail / modules / roles / events /
# dependencies, so the existing _backend_files_plan() naturally fans
# out one app folder, one Dockerfile, one Controller-per-resource etc.
@router.post("/{project_id}/merge-services")
async def merge_services(project_id: str, payload: dict):
    """Collapse N services into a single combined service that CodeGen
    will materialise as ONE source tree."""
    names = [str(s).strip() for s in (payload.get("service_names") or []) if str(s).strip()]
    if len(names) < 2:
        raise HTTPException(400, "Provide at least 2 service_names to merge.")
    merged_name = (payload.get("merged_name") or "").strip()
    if not merged_name or not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,60}", merged_name):
        raise HTTPException(
            400,
            "merged_name must be lower-kebab-case (a-z, 0-9, -) and 2-61 chars.",
        )

    # Reject merging when the service map is already frozen — the
    # combined surface needs to land in arch_services BEFORE codegen.
    sm = await arch_documents.find_one(
        {"project_id": project_id, "type": "service_map"}, {"_id": 0},
    )
    if sm and sm.get("frozen"):
        raise HTTPException(
            400,
            "Service map is frozen — reset Architecture stage to merge services.",
        )

    rows = await arch_services.find(
        {"project_id": project_id, "name": {"$in": names},
         "kind": {"$ne": "utility"}, "frontend": False}, {"_id": 0},
    ).to_list(50)
    if len(rows) != len(names):
        missing = sorted(set(names) - {r["name"] for r in rows})
        raise HTTPException(
            404,
            f"Services not found / not mergeable (utility & frontend rows are "
            f"excluded): {missing}",
        )
    if merged_name not in names:
        clash = await arch_services.find_one(
            {"project_id": project_id, "name": merged_name}, {"_id": 0},
        )
        if clash:
            raise HTTPException(409, f"merged_name `{merged_name}` already exists.")

    # Reject mixed backend_lang — codegen plan is language-specific.
    langs = {(r.get("backend_lang") or "").lower() for r in rows if r.get("backend_lang")}
    if len(langs) > 1:
        raise HTTPException(
            400,
            f"Cannot merge services with different backend_lang: {sorted(langs)}. "
            f"Override them all to the same target in the Service Map first.",
        )

    import uuid as _uuid
    now_iso = datetime.now(timezone.utc).isoformat()
    union_endpoints: list[str] = []
    seen_eps: set = set()
    for r in rows:
        for ep in (r.get("api_endpoints") or []):
            if ep and ep not in seen_eps:
                seen_eps.add(ep)
                union_endpoints.append(ep)

    union = {
        "id":            _uuid.uuid4().hex,
        "project_id":    project_id,
        "name":          merged_name,
        "display_name":  (payload.get("merged_display_name") or "").strip()
                          or " & ".join((r.get("display_name") or r["name"]) for r in rows),
        "description":   (payload.get("merged_description") or "").strip()
                          or (f"Merged from: {', '.join(r['name'] for r in rows)}. "
                              + "; ".join((r.get("description") or "")[:120]
                                          for r in rows if r.get("description"))),
        "responsibility": "; ".join((r.get("responsibility") or "")
                                    for r in rows if r.get("responsibility")),
        "pattern":       rows[0].get("pattern") or "merged",
        # iter-13.121 — persist an explicit `kind` (was previously
        # absent). Downstream codegen splits on `kind == "utility"`; a
        # merged business service must be classified as such (empty /
        # non-utility) so it flows into `biz_services`. Preserve
        # rows[0]'s kind when it's present so a merge of two utility
        # rows stays a utility.
        "kind":          rows[0].get("kind", "") or "",
        "backend_lang":  rows[0].get("backend_lang") or "nodejs",
        "frontend":      False,
        "tables":        sorted({t for r in rows for t in (r.get("tables") or [])}),
        "api_endpoints": union_endpoints,
        "routes_detail": [rd for r in rows for rd in (r.get("routes_detail") or [])],
        "modules":       sorted({m for r in rows for m in (r.get("modules") or [])}),
        "module_names":  sorted({m for r in rows for m in (r.get("module_names") or [])}),
        "roles":         sorted({r2 for r in rows for r2 in (r.get("roles") or [])}),
        "dependencies":  sorted({d for r in rows for d in (r.get("dependencies") or [])
                                 if d not in names}),  # drop intra-merge deps
        "events_published": sorted({e for r in rows for e in (r.get("events_published") or [])}),
        "events_consumed":  sorted({e for r in rows for e in (r.get("events_consumed") or [])}),
        "status":        "pending",
        "codegen_status": "pending",
        "source_module": "",
        "estimated_loc": sum(int(r.get("estimated_loc") or 0) for r in rows),
        "merged_from":   names,
        "created_at":    now_iso,
        "updated_at":    now_iso,
    }
    union["route_count"] = len(union["api_endpoints"])
    union["table_count"] = len(union["tables"])
    union["api_count"]   = len(union["api_endpoints"])

    # Atomically: drop sources, insert merged.
    await arch_services.delete_many(
        {"project_id": project_id, "name": {"$in": names}}
    )
    await arch_services.insert_one(union)

    # iter-14.21 — Cascade-delete stale CodeGen artifacts for the
    # now-collapsed source services so the CodeGen page doesn't keep
    # showing N separate source trees after the merge. Runs regardless
    # of whether codegen has been executed yet (no-op when
    # codegen_files is empty for these names).
    _merge_cleanup = await _cleanup_merged_service_artifacts(
        project_id, names, merged_name,
    )

    # iter-14.30 — Eagerly enrich the merged service from the OLTP DDL
    # + frozen API contracts so the UI shows real table_count / api_count
    # immediately (previously the merged row stayed at 0/0 whenever the
    # source services were themselves empty, e.g. Java Struts KBs where
    # route extraction is thin). The same enricher runs again at codegen
    # time; running it here means the operator gets accurate feedback
    # in the Service Map tab BEFORE hitting Generate Code, and the audit
    # log records the enriched counts (not the pre-enrichment zeros).
    try:
        from routes.codegen import _enrich_services_from_kb as _enrich_svcs  # local import to avoid cycle
        await _enrich_svcs(project_id, [union])
        union["route_count"] = len(union.get("api_endpoints") or [])
        union["table_count"] = len(union.get("tables") or [])
        union["api_count"]   = len(union.get("api_endpoints") or [])
        # Persist enriched fields on the merged arch_services row.
        await arch_services.update_one(
            {"project_id": project_id, "name": merged_name},
            {"$set": {
                "tables":        union["tables"],
                "api_endpoints": union["api_endpoints"],
                "route_count":   union["route_count"],
                "table_count":   union["table_count"],
                "api_count":     union["api_count"],
                "updated_at":    now_iso,
            }},
        )
    except Exception as _ee:  # noqa: BLE001
        logger.warning("merge: eager enrichment skipped: %s", _ee)

    # Rewrite the service_map artifact JSON body so the UI reflects it.
    if sm:
        try:
            sm_json = json.loads(sm.get("content", "") or "{}")
            svcs = [s for s in (sm_json.get("services") or [])
                    if s.get("name") not in names]
            svcs.append({
                k: v for k, v in union.items()
                if k not in {"id", "project_id", "created_at", "updated_at",
                             "status", "codegen_status", "source_module"}
            })
            sm_json["services"] = svcs
            sm_json.setdefault("_merges", []).append({
                "from": names, "to": merged_name, "at": now_iso,
            })
            await arch_documents.update_one(
                {"project_id": project_id, "type": "service_map"},
                {"$set": {"content": json.dumps(sm_json, indent=2),
                          "updated_at": now_iso}},
            )
        except Exception as e:
            logger.warning("merge: could not rewrite service_map JSON: %s", e)

    await audit_log.insert_one({
        "action":     "architecture.merge_services",
        "project_id": project_id,
        "at":         now_iso,
        "details":    {"from": names, "to": merged_name,
                       "table_count": union["table_count"],
                       "api_count":   union["api_count"],
                       "codegen_files_deleted": _merge_cleanup.get(
                           "codegen_files_deleted", 0),
                       "codegen_runs_updated": _merge_cleanup.get(
                           "codegen_runs_updated", 0)},
    })
    return {"ok": True, "merged": {k: v for k, v in union.items() if k != "_id"},
            "cleanup": _merge_cleanup}


@router.post("/{project_id}/merge-services/batch")
async def merge_services_batch(project_id: str, payload: dict):
    """iter-14.33 — Apply multiple named merge-groups in one call.

    Payload shape::
        {
          "groups": [
            {"merged_name": "billing-invoicing-svc",
             "service_names": ["billing", "invoicing"],
             "merged_display_name": "Billing & Invoicing",         # optional
             "merged_description": "Combined billing service."},   # optional
            {"merged_name": "identity-svc",
             "service_names": ["user-mgmt", "roles", "sessions"]},
            ...
          ]
        }

    Validation is upfront and all-or-nothing:
      • Every group must have ≥ 2 service_names.
      • Every merged_name must be lower-kebab-case.
      • merged_names must be pairwise unique across the batch.
      • No source service_name may appear in two groups.
      • Every source service_name must exist in arch_services and not
        be utility / frontend.
      • All source rows in a group must share backend_lang.
      • Service map must not be frozen.

    On success, each group is applied via the same code path as the
    single-group `merge_services` endpoint (union + eager enrichment +
    service_map JSON rewrite + audit-log entry).
    """
    groups_in = payload.get("groups") or []
    if not isinstance(groups_in, list) or len(groups_in) < 1:
        raise HTTPException(400, "Provide `groups: [ {merged_name, service_names, …}, … ]`.")

    # Reject when service map is frozen — same as single-group merge.
    sm = await arch_documents.find_one(
        {"project_id": project_id, "type": "service_map"}, {"_id": 0},
    )
    if sm and sm.get("frozen"):
        raise HTTPException(
            400,
            "Service map is frozen — reset Architecture stage to merge services.",
        )

    # ── Batch-wide validation ─────────────────────────────────────
    seen_merged_names: set[str] = set()
    seen_source_names: set[str] = set()
    normalized: list[dict] = []
    for idx, g in enumerate(groups_in):
        if not isinstance(g, dict):
            raise HTTPException(400, f"groups[{idx}] must be an object.")
        merged_name = (g.get("merged_name") or "").strip()
        if not merged_name or not re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,60}", merged_name):
            raise HTTPException(
                400,
                f"groups[{idx}].merged_name must be lower-kebab-case (a-z, 0-9, -) "
                f"and 2-61 chars. Got: {merged_name!r}",
            )
        if merged_name in seen_merged_names:
            raise HTTPException(
                400, f"Duplicate merged_name in batch: {merged_name!r}",
            )
        seen_merged_names.add(merged_name)
        names = [str(s).strip() for s in (g.get("service_names") or []) if str(s).strip()]
        if len(names) < 2:
            raise HTTPException(
                400,
                f"groups[{idx}] ({merged_name}) needs at least 2 service_names.",
            )
        for n in names:
            if n in seen_source_names:
                raise HTTPException(
                    400,
                    f"Service {n!r} appears in multiple groups. "
                    "Each service can only be merged into one group.",
                )
            seen_source_names.add(n)
        normalized.append({
            "merged_name": merged_name,
            "service_names": names,
            "merged_display_name": (g.get("merged_display_name") or "").strip(),
            "merged_description": (g.get("merged_description") or "").strip(),
        })

    # Verify every source row exists & is not utility/frontend, and
    # every merged_name doesn't clash with a NON-source existing row.
    all_sources = list(seen_source_names)
    all_rows = await arch_services.find(
        {"project_id": project_id, "name": {"$in": all_sources}},
        {"_id": 0},
    ).to_list(500)
    rows_by_name = {r["name"]: r for r in all_rows}
    missing = [n for n in all_sources if n not in rows_by_name]
    if missing:
        raise HTTPException(404, f"Services not found: {missing}")
    for n, r in rows_by_name.items():
        if r.get("kind") == "utility" or r.get("frontend"):
            raise HTTPException(
                400,
                f"Cannot merge utility / frontend service {n!r}. "
                "Filter your selection to backend business services only.",
            )

    # merged_name clash check (against non-source rows).
    non_source_names = seen_merged_names - seen_source_names
    if non_source_names:
        clashes = await arch_services.find(
            {"project_id": project_id, "name": {"$in": list(non_source_names)}},
            {"_id": 0, "name": 1},
        ).to_list(50)
        if clashes:
            raise HTTPException(
                409,
                f"merged_name(s) already exist as non-source services: "
                f"{[c['name'] for c in clashes]}",
            )

    # Per-group: check backend_lang consistency.
    for g in normalized:
        langs = {(rows_by_name[n].get("backend_lang") or "").lower()
                 for n in g["service_names"]
                 if rows_by_name[n].get("backend_lang")}
        if len(langs) > 1:
            raise HTTPException(
                400,
                f"Group {g['merged_name']!r}: cannot merge services with "
                f"different backend_lang: {sorted(langs)}. Override them "
                "all to the same target in the Service Map first.",
            )

    # ── Apply each group in sequence (same as single-group merge) ─
    from routes.codegen import _enrich_services_from_kb as _enrich_svcs  # local import
    import uuid as _uuid
    results: list[dict] = []
    for g in normalized:
        names = g["service_names"]
        merged_name = g["merged_name"]
        rows = [rows_by_name[n] for n in names]
        now_iso = datetime.now(timezone.utc).isoformat()

        union_endpoints: list[str] = []
        seen_eps: set = set()
        for r in rows:
            for ep in (r.get("api_endpoints") or []):
                if ep and ep not in seen_eps:
                    seen_eps.add(ep)
                    union_endpoints.append(ep)

        union = {
            "id":            _uuid.uuid4().hex,
            "project_id":    project_id,
            "name":          merged_name,
            "display_name":  g["merged_display_name"] or
                             " & ".join((r.get("display_name") or r["name"]) for r in rows),
            "description":   g["merged_description"] or
                             (f"Merged from: {', '.join(r['name'] for r in rows)}. "
                              + "; ".join((r.get("description") or "")[:120]
                                          for r in rows if r.get("description"))),
            "responsibility": "; ".join((r.get("responsibility") or "")
                                        for r in rows if r.get("responsibility")),
            "pattern":       rows[0].get("pattern") or "merged",
            "kind":          rows[0].get("kind", "") or "",
            "backend_lang":  rows[0].get("backend_lang") or "nodejs",
            "frontend":      False,
            "tables":        sorted({t for r in rows for t in (r.get("tables") or [])}),
            "api_endpoints": union_endpoints,
            "routes_detail": [rd for r in rows for rd in (r.get("routes_detail") or [])],
            "modules":       sorted({m for r in rows for m in (r.get("modules") or [])}),
            "module_names":  sorted({m for r in rows for m in (r.get("module_names") or [])}),
            "roles":         sorted({r2 for r in rows for r2 in (r.get("roles") or [])}),
            "dependencies":  sorted({d for r in rows for d in (r.get("dependencies") or [])
                                     if d not in seen_source_names}),
            "events_published": sorted({e for r in rows for e in (r.get("events_published") or [])}),
            "events_consumed":  sorted({e for r in rows for e in (r.get("events_consumed") or [])}),
            "status":        "pending",
            "codegen_status": "pending",
            "source_module": "",
            "estimated_loc": sum(int(r.get("estimated_loc") or 0) for r in rows),
            "merged_from":   names,
            "created_at":    now_iso,
            "updated_at":    now_iso,
        }
        union["route_count"] = len(union["api_endpoints"])
        union["table_count"] = len(union["tables"])
        union["api_count"]   = len(union["api_endpoints"])

        await arch_services.delete_many(
            {"project_id": project_id, "name": {"$in": names}}
        )
        await arch_services.insert_one(union)

        # iter-14.21 — cascade CodeGen cleanup (same rationale as
        # merge_services above).
        await _cleanup_merged_service_artifacts(project_id, names, merged_name)

        # Eager enrichment (iter-14.30).
        try:
            await _enrich_svcs(project_id, [union])
            union["route_count"] = len(union.get("api_endpoints") or [])
            union["table_count"] = len(union.get("tables") or [])
            union["api_count"]   = len(union.get("api_endpoints") or [])
            await arch_services.update_one(
                {"project_id": project_id, "name": merged_name},
                {"$set": {
                    "tables":        union["tables"],
                    "api_endpoints": union["api_endpoints"],
                    "route_count":   union["route_count"],
                    "table_count":   union["table_count"],
                    "api_count":     union["api_count"],
                    "updated_at":    now_iso,
                }},
            )
        except Exception as _ee:  # noqa: BLE001
            logger.warning("batch merge: eager enrichment skipped for %s: %s",
                           merged_name, _ee)

        await audit_log.insert_one({
            "action":     "architecture.merge_services.batch",
            "project_id": project_id,
            "at":         now_iso,
            "details":    {"from": names, "to": merged_name,
                           "table_count": union["table_count"],
                           "api_count":   union["api_count"]},
        })
        results.append({k: v for k, v in union.items() if k != "_id"})

    # Rewrite the service_map JSON body once at the end.
    if sm:
        try:
            sm_json = json.loads(sm.get("content", "") or "{}")
            svcs = [s for s in (sm_json.get("services") or [])
                    if s.get("name") not in seen_source_names]
            for r in results:
                svcs.append({
                    k: v for k, v in r.items()
                    if k not in {"id", "project_id", "created_at", "updated_at",
                                 "status", "codegen_status", "source_module"}
                })
            sm_json["services"] = svcs
            _now = datetime.now(timezone.utc).isoformat()
            sm_json.setdefault("_merges", []).extend([
                {"from": g["service_names"], "to": g["merged_name"], "at": _now}
                for g in normalized
            ])
            await arch_documents.update_one(
                {"project_id": project_id, "type": "service_map"},
                {"$set": {"content": json.dumps(sm_json, indent=2),
                          "updated_at": _now}},
            )
        except Exception as e:
            logger.warning("batch merge: could not rewrite service_map JSON: %s", e)

    return {"ok": True, "groups": len(results), "merged": results}


@router.post("/{project_id}/unmerge-service")
async def unmerge_service(project_id: str, payload: dict):
    """Best-effort split — deletes the merged row. The next /jobs/start/recommend
    will re-cluster from KB; if the user wants the EXACT original split back,
    they should regenerate the Service Map."""
    name = (payload.get("merged_name") or "").strip()
    row = await arch_services.find_one(
        {"project_id": project_id, "name": name}, {"_id": 0},
    )
    if not row:
        raise HTTPException(404, "Service not found.")
    if not row.get("merged_from"):
        raise HTTPException(400, f"`{name}` is not a merged service.")
    sm = await arch_documents.find_one(
        {"project_id": project_id, "type": "service_map"}, {"_id": 0},
    )
    if sm and sm.get("frozen"):
        raise HTTPException(
            400,
            "Service map is frozen — reset Architecture stage to unmerge.",
        )
    await arch_services.delete_one({"project_id": project_id, "name": name})
    now_iso = datetime.now(timezone.utc).isoformat()
    # Strip the merged entry from the service_map JSON body too.
    if sm:
        try:
            sm_json = json.loads(sm.get("content", "") or "{}")
            sm_json["services"] = [s for s in (sm_json.get("services") or [])
                                   if s.get("name") != name]
            await arch_documents.update_one(
                {"project_id": project_id, "type": "service_map"},
                {"$set": {"content": json.dumps(sm_json, indent=2),
                          "updated_at": now_iso}},
            )
        except Exception as e:
            logger.warning("unmerge: could not rewrite service_map JSON: %s", e)
    await audit_log.insert_one({
        "action":     "architecture.unmerge_service",
        "project_id": project_id,
        "at":         now_iso,
        "details":    {"merged_name": name,
                       "original_sources": row.get("merged_from")},
    })
    return {"ok": True, "deleted": name,
            "note": "Re-run Recommend to repopulate the original split from KB."}


# -----------------------------------------------------------
# B.1 iter-13.59 — utility catalog
# -----------------------------------------------------------
@router.get("/{project_id}/utilities")
async def list_utilities(project_id: str):
    """Return the cross-cutting utility catalog merged with whatever is
    currently materialised in ``arch_services`` (kind='utility'). The
    frontend uses this to render the Utilities panel inside the service
    map: each entry has ``enabled`` (currently persisted?) + ``config``
    (either the persisted config or the schema defaults)."""
    existing = await arch_services.find(
        {"project_id": project_id, "kind": "utility"}, {"_id": 0},
    ).to_list(50)
    by_type = {s.get("utility_type"): s for s in existing}
    out = []
    for u in UTILITY_CATALOG:
        cur = by_type.get(u["key"])
        defaults = {item["key"]: item.get("default") for item in u.get("config_schema", [])}
        out.append({
            **u,
            "enabled": cur is not None,
            "config":  (cur or {}).get("utility_config") or defaults,
        })
    return {"utilities": out}


# -----------------------------------------------------------
# C. HLD generation (job)
# -----------------------------------------------------------
# iter-13.23 — Enterprise HLD section list. Expanded from the original
# 10 sections to 15 to cover the concerns an Enterprise Solution Architect
# would always address: caching, async messaging, observability stack,
# CI/CD, disaster recovery / BCP, capacity planning, and migration
# strategy were all missing from the v1 list.
HLD_SECTIONS = [
    ("executive_summary", "Executive Summary",
     "Concise overview of the proposed architecture, key drivers, business outcomes, and the target stack (verbatim). "
     "Include a one-line legacy→target migration thesis.", 250),
    ("system_context", "System Context Diagram",
     "Mermaid C4Context diagram showing the system, every actor class from the SRS, and every external integration named in the KB / graph. "
     "Pair the diagram with prose naming each external system.", 300),
    ("service_decomposition", "Service Decomposition & Bounded Contexts",
     "One subsection per service: bounded context, responsibility, owned tables, key endpoints, published/consumed events, runtime allocation. "
     "Cite UC-IDs satisfied by each service.", 800),
    ("api_gateway", "API Gateway & Edge Routing",
     "Gateway product chosen for the target stack, routing rules, auth pass-through, rate limiting, request tracing headers, "
     "API versioning strategy (URI vs header), throttling tiers per actor class.", 350),
    ("data_flow", "Critical Data Flow Diagrams",
     "Mermaid sequenceDiagram for the top 3 critical workflows (cite UC-IDs). "
     "Include sync vs async legs, error/compensation paths, and which service owns which write.", 450),
    ("database_arch", "Database Architecture",
     "Database-per-service vs shared-DB justification per bounded context, schema ownership, distributed transaction strategy "
     "(SAGA / outbox / 2PC-avoided), read-replica strategy, PII isolation, backup/restore RPO/RTO from SRS NFRs.", 400),
    ("caching_strategy", "Caching Strategy",
     "Cache layers (CDN / edge / application / DB read-replica), invalidation strategy (TTL / event-driven), "
     "cache product per layer (Redis cluster / CDN vendor / DB-internal), keys/TTL per data class. "
     "Cite the NFR-IDs forcing each layer.", 350),
    ("async_messaging", "Asynchronous Messaging & Event Architecture",
     "Message bus product (canonical for the target stack), event taxonomy, schema-registry approach, "
     "consumer-group strategy, dead-letter handling, exactly-once vs at-least-once decisions per topic, "
     "outbox pattern for transactional publishing.", 400),
    ("auth", "Authentication & Authorization",
     "Identity provider, OAuth2/OIDC flows, JWT issuance/rotation, RBAC vs ABAC, service-to-service auth (mTLS / SPIFFE / token), "
     "session management for the SPA, role-permission matrix grounded in SRS actors.", 300),
    ("security_architecture", "Security Architecture (defence in depth)",
     "Network segmentation (public/private/data subnets), WAF rules, secrets management (vault product), "
     "data-at-rest + in-transit encryption (algorithm + key rotation), audit logging requirements, "
     "threat model summary (STRIDE per service), compliance mapping (cite NFR-COMPLIANCE-* IDs).", 400),
    ("observability", "Observability Stack (Logs / Metrics / Traces)",
     "Structured log schema + sink, metric backend (Prometheus / OTel collector / native), "
     "distributed tracing backend + sampling strategy, SLO/SLI definitions per service (cite NFR-PERF-* / NFR-AVAIL-*), "
     "alert routing (paging vs ticketing thresholds), dashboards inventory.", 400),
    ("nfr", "Non-Functional Requirements Realisation",
     "Map every SRS NFR-ID to the architecture decision that satisfies it. Table format: "
     "NFR-ID | Requirement | Architecture decision | Component | Evidence/measurement.", 400),
    ("deployment", "Deployment Architecture",
     "Mermaid graph LR: containers, ingress, service mesh (if any), DB cluster, cache cluster, messaging cluster, observability stack. "
     "Name target runtime versions verbatim. Environments (dev/stage/prod), promotion gates.", 350),
    ("ci_cd", "CI/CD Pipeline",
     "Pipeline tool (GitHub Actions / Azure DevOps / GitLab — whichever the target ecosystem favours), "
     "stages (build, unit-test, lint, sast, container-build, sign, integration-test, deploy), "
     "branching model, artifact registry, blue-green vs canary rollout per service, automated rollback triggers.", 350),
    ("dr_bcp", "Disaster Recovery & Business Continuity",
     "RPO/RTO targets per service (cite NFR-AVAIL-* / NFR-COMPLIANCE-*), multi-AZ vs multi-region posture, "
     "backup cadence + retention per data class, failover runbook, periodic DR-drill cadence.", 300),
    ("migration_strategy", "Migration Strategy (legacy → target)",
     "Strangler-fig vs big-bang justification, traffic shifting mechanism (gateway-level / DNS / feature flag), "
     "dual-write window strategy + reconciliation, data backfill plan, cutover criteria per service, "
     "rollback plan per cutover step. Reference the legacy stack components being retired.", 400),
    ("tech_decisions", "Technology Decisions & Trade-offs",
     "ADR-style entries: for each major choice (framework / messaging / DB / cache / IdP), give Context → Decision → Consequences → Alternatives considered. "
     "All choices must be consistent with the user-selected TARGET STACK.", 350),
]


async def _run_hld_job(jid: str, project_id: str, model: str):
    """iter-14.20 — HLD is now deterministic Python. No LLM calls.

    Reads the same upstream inputs the LLM version consumed (frozen service
    map, arch_services, SRS sections, OLTP DDL from StageContext) and emits
    the same 17 sections defined in HLD_SECTIONS via arch_deterministic.
    """
    try:
        _job_update(jid, status="running", step="Loading service map…", pct=5)
        dm_ctx = await require_stage_context(project_id, "DataModel", "Architecture")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        sm = await arch_documents.find_one(
            {"project_id": project_id, "type": "service_map"}, {"_id": 0}
        )
        if not sm or not sm.get("frozen"):
            _job_finish(jid, "error", error="Service map must be approved (frozen) first.")
            return
        try:
            sm_data = json.loads(sm["content"])
        except Exception:
            sm_data = {}

        services_full = await arch_services.find(
            {"project_id": project_id, "frontend": False}, {"_id": 0}
        ).to_list(200)
        if not services_full:
            services_full = sm_data.get("services") or []

        _job_update(jid, step="Loading SRS + OLTP DDL…", pct=25)
        srs_sections = await _load_srs_sections(project_id, dm_ctx)
        oltp_ddl, _ddl_meta_hld = await _load_oltp_ddl(project_id, dm_ctx, services_full)
        logger.info("arch.hld DDL load: %s", _ddl_meta_hld)

        _job_update(jid, step="Rendering 17 HLD sections deterministically…", pct=55)
        full = _render_hld_deterministic(
            proj=proj or {},
            sm_data=sm_data,
            services=services_full,
            srs_sections=srs_sections,
            oltp_ddl=oltp_ddl,
        )

        tracability = {
            "datamodel_version": dm_ctx.get("version"),
            "service_map_version": sm.get("version"),
            "section_count": len(HLD_SECTIONS),
            "model": "deterministic",
            "prompt_key": "deterministic",
            "services_count": len(services_full),
            "generator": "arch_deterministic.render_hld",
        }
        _job_update(jid, step="Persisting artifact…", pct=90)
        art = await _save_arch_doc(project_id, "hld", full, "deterministic", tracability)
        await audit_log.insert_one({
            "action": "architecture.generate.hld",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "sections": len(HLD_SECTIONS),
                        "mode": "deterministic"},
        })
        _job_finish(jid, "complete", step="Done", pct=100,
                    result={"arch_doc_id": art["id"], "version": art["version"],
                            "sections": len(HLD_SECTIONS), "mode": "deterministic"})
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/hld")
async def start_hld(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "DataModel", "Architecture")
    _existing = await arch_documents.find_one({"project_id": project_id, "type": "hld"}, {"_id": 1})
    set_current_agent_key("arch.regenerate" if _existing else "arch.hld")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    jid = _new_job(project_id, "hld")
    asyncio.create_task(_run_hld_job(jid, project_id, model))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# D. LLD generation (job) — one section per service
# -----------------------------------------------------------
async def _run_lld_job(jid: str, project_id: str, model: str):
    """iter-14.20 — LLD is now deterministic Python. No LLM calls.

    Emits one `# Service: <display_name> (`<name>`)` block per business
    service (regex-consumed by CodeGen's `_backend_files_plan`). Also
    extracts the embedded OpenAPI YAML blocks into an `api_contracts`
    artifact for back-compat with users who only run the LLD job.
    """
    try:
        _job_update(jid, status="running", step="Loading services…", pct=5)
        dm_ctx = await require_stage_context(project_id, "DataModel", "Architecture")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        services = await arch_services.find(
            {"project_id": project_id, "frontend": False}, {"_id": 0}
        ).to_list(50)
        if not services:
            _job_finish(jid, "error", error="No services. Generate service map first.")
            return

        _job_update(jid, step="Loading SRS + OLTP DDL…", pct=20)
        srs_sections_l = await _load_srs_sections(project_id, dm_ctx)
        oltp_ddl, ddl_meta = await _load_oltp_ddl(project_id, dm_ctx, services)
        logger.info("arch.lld DDL load: %s", ddl_meta)

        # iter-14.20.2 — Same hydration pass as api_contracts so the LLD
        # blocks list every legacy route belonging to each service (plus a
        # `legacy-unsorted` block for anything the LLM clustering missed).
        _job_update(jid, step="Hydrating services with legacy KB routes…", pct=35)
        services, hydrate_meta = await _hydrate_services_with_legacy_routes(
            project_id, services,
        )
        legacy_bucket = _synthetic_legacy_unsorted_service(
            hydrate_meta.get("unassigned_routes") or []
        )
        if legacy_bucket:
            services.append(legacy_bucket)
            logger.info(
                "arch.lld: appended legacy-unsorted bucket with %d route(s)",
                legacy_bucket["api_count"],
            )

        # iter-14.20.4 — Same DDL rescue as api_contracts so LLD blocks
        # still have owned tables (and thus DTOs + CRUD endpoints) when
        # KB routes are missing.
        _job_update(jid, step="Fuzzy-attaching OLTP tables to services…", pct=45)
        services, unassigned_tables, per_svc_ddl_added = _attach_ddl_tables(
            services, oltp_ddl,
        )
        ddl_bucket = _synthetic_legacy_crud_service(unassigned_tables)
        if ddl_bucket:
            services.append(ddl_bucket)
            logger.info(
                "arch.lld: appended legacy-crud bucket with %d table(s)",
                len(ddl_bucket["tables"]),
            )
        if per_svc_ddl_added:
            logger.info(
                "arch.lld DDL rescue: attached %d table(s) across %d service(s): %s",
                sum(per_svc_ddl_added.values()),
                len(per_svc_ddl_added),
                per_svc_ddl_added,
            )

        _job_update(jid, step=f"Rendering LLD for {len(services)} services deterministically…", pct=55)
        full = _render_lld_deterministic(
            proj=proj or {},
            services=services,
            srs_sections=srs_sections_l,
            oltp_ddl=oltp_ddl,
        )

        total_attached_routes = sum(
            len(s.get("routes_detail") or s.get("api_endpoints") or [])
            for s in services
        )
        tracability = {
            "service_count": len(services),
            "model": "deterministic",
            "prompt_key": "deterministic",
            "surface_grounded": total_attached_routes > 0,
            "total_attached_routes": total_attached_routes,
            "kb_surface_source": hydrate_meta.get("surface_source"),
            "kb_total_routes": hydrate_meta.get("kb_total_routes"),
            "kb_coverage_pct": hydrate_meta.get("coverage_pct"),
            "legacy_unsorted_routes": (legacy_bucket or {}).get("api_count", 0),
            "generator": "arch_deterministic.render_lld",
        }
        _job_update(jid, step="Persisting LLD…", pct=85)
        art = await _save_arch_doc(project_id, "lld", full, "deterministic", tracability)

        # Extract OpenAPI YAML blocks into api_contracts (back-compat: users
        # who only run the LLD job still get a matching api_contracts doc).
        yaml_blocks = re.findall(r"```yaml\n(.*?)```", full, re.DOTALL)
        if yaml_blocks:
            await _save_arch_doc(
                project_id, "api_contracts",
                "\n---\n".join(yaml_blocks),
                "deterministic",
                {"merged_from_lld": True, "block_count": len(yaml_blocks),
                 "generator": "arch_deterministic.render_lld"},
            )

        await audit_log.insert_one({
            "action": "architecture.generate.lld",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "services": len(services),
                        "mode": "deterministic"},
        })
        _job_finish(jid, "complete", step="Done", pct=100,
                    result={"arch_doc_id": art["id"], "version": art["version"],
                            "services": len(services),
                            "api_contracts_extracted": len(yaml_blocks),
                            "mode": "deterministic"})
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/lld")
async def start_lld(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "DataModel", "Architecture")
    _existing = await arch_documents.find_one({"project_id": project_id, "type": "lld"}, {"_id": 1})
    set_current_agent_key("arch.regenerate" if _existing else "arch.lld")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    jid = _new_job(project_id, "lld")
    asyncio.create_task(_run_lld_job(jid, project_id, model))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# E. Sequence diagrams (job)
# -----------------------------------------------------------
async def _run_seq_job(jid: str, project_id: str, model: str):
    try:
        _job_update(jid, status="running", step="Loading use cases…", pct=2)
        dm_ctx = await require_stage_context(project_id, "DataModel", "Architecture")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        await _ensure_kb_graph(project_id)
        srs_sections = await _load_srs_sections(project_id, dm_ctx)
        use_cases_text = (srs_sections.get("detailed_use_cases", "") or srs_sections.get("use_cases", "")) or ""
        srs_nfr_seq = (srs_sections.get("non_functional_requirements", "") or "")[:2500]
        # iter-13.24 — up to 10 workflows (was 5) so the artifact covers more
        # of the SRS, not only the top critical paths.
        cases = [c.strip() for c in re.split(r"\n(?=##\s|UC-)", use_cases_text) if c.strip()][:10]
        if not cases:
            cases = ["Top workflow"]
        services = await arch_services.find({"project_id": project_id, "frontend": False}, {"_id": 0}).to_list(50)
        services_list = ", ".join(s["name"] for s in services)
        endpoints = "\n".join(f"{s['name']}: {', '.join((s.get('api_endpoints') or [])[:8])}" for s in services)

        template = await _get_prompt(project_id, "arch.sequence")
        if not template:
            _job_finish(jid, "error", error="arch.sequence prompt missing.")
            return
        _assert_prompt_fresh("arch.sequence", template)

        diagrams: List[Dict[str, str]] = [None] * len(cases)
        total = len(cases)
        graph_nodes_per_uc: List[int] = [0] * total
        completed = {"n": 0}
        sem = asyncio.Semaphore(4)

        async def gen_seq(i, uc):
            async with sem:
                label = uc.split("\n")[0][:80].lstrip("# ").strip() or f"Workflow {i+1}"
                # iter-13.42 — workflow-scoped graph (toggle-independent).
                seeds = [label] + label.split()[:6] + ["controller", "service", "route"]
                seq_graph_yaml, uc_graph_nodes = await _graph_subgraph_yaml(
                    project_id, seeds,
                    max_chars=3500, hops=2, max_nodes=120,
                )
                graph_nodes_per_uc[i] = uc_graph_nodes
                # iter-13.24 — workflow-scoped legacy evidence
                legacy_block_seq = await _legacy_evidence(
                    project_id,
                    [label] + label.split()[:6],
                    max_chars=2500,
                ) or "(no legacy entities matched this workflow)"
                if i == 0:
                    _log_prompt_context(
                        "arch.sequence[first-workflow]",
                        target_tech=proj.get("target_tech", ""),
                        graph_subgraph=seq_graph_yaml,
                        legacy_evidence=legacy_block_seq,
                        use_case_content=uc,
                        srs_nfr=srs_nfr_seq,
                    )
                sp = _safe_format(
                    template,
                    project_name=proj.get("name", ""),
                    source_tech=proj.get("source_tech", ""),
                    target_tech=proj.get("target_tech", ""),
                    use_case_name=label,
                    use_case_content=uc[:2500],
                    services_list=services_list,
                    relevant_endpoints=endpoints[:3000],
                    graph_subgraph=seq_graph_yaml or "(no graph slice for this workflow)",
                    legacy_evidence=legacy_block_seq,
                    srs_nfr=srs_nfr_seq,
                )
                sp = await _with_completeness_contract(project_id, sp)
                try:
                    r = await chat_completion(
                        messages=[{"role": "system", "content": sp},
                                  {"role": "user", "content": "Generate the mermaid sequenceDiagram now."}],
                        model=model, temperature=0.2, max_tokens=4000, timeout=180.0,
                        agent_key="arch.sequence", project_id=project_id,
                    )
                    content = (r.get("content", "") or "").strip()
                except Exception as e:
                    # iter-13.51 — classify; fatal kinds (transport / billing /
                    # auth) skip body inlining so 10 diagrams don't all become
                    # "Note over System: model error — OpenRouter error 402:
                    # Insufficient credits". The runner aborts the whole job
                    # after gather() finishes.
                    kind = _classify_llm_error_kind(e)
                    _JOBS.get(jid, {}).setdefault("section_errors", []).append(
                        {"section": label, "kind": kind, "error": str(e)[:400]}
                    )
                    if kind in _FATAL_ERROR_KINDS:
                        content = ""
                    else:
                        content = f"sequenceDiagram\n  Note over System: model error — {str(e)[:120]}"
                # iter-13.49 — always sanitize; guarantees a parseable
                # ```mermaid``` block regardless of how the LLM wrapped /
                # framed its response.
                if content:
                    content = _sanitize_mermaid_sequence(content)
                completed["n"] += 1
                pct = 5 + int((completed["n"] / total) * 90)
                _job_update(jid, step=f"Diagram {completed['n']}/{total}: {label}", pct=pct)
                diagrams[i] = {"use_case": label, "mermaid": content}

        _job_update(jid, step=f"Generating {total} diagrams in parallel…", pct=5)
        await asyncio.gather(*[gen_seq(i, uc) for i, uc in enumerate(cases)])

        # iter-13.50 — abort cleanly on transport (DNS/connect) failures so
        # we don't persist a sequence_diagrams artifact whose body is just
        # repeated "Name or service not known" notes.
        abort = _should_abort_job(jid)
        if abort:
            _job_finish(jid, "error", error=abort)
            return

        # Drop diagrams with empty content (transport-failure skips). Keep
        # at least a stub so the artifact is never literally empty.
        diagrams = [d for d in diagrams if d and (d.get("mermaid") or "").strip()]
        if not diagrams:
            _job_finish(jid, "error", error="All sequence diagrams failed to generate.")
            return

        body = "\n\n".join(f"## {d['use_case']}\n\n{d['mermaid']}" for d in diagrams)
        total_uc_nodes = sum(graph_nodes_per_uc)
        body = _graph_warning_banner(total_uc_nodes) + body
        tracability = {
            "use_case_count": total,
            "model": model,
            "prompt_key": "arch.sequence",
            "graph_grounded": total_uc_nodes > 0,
            "graph_node_count": total_uc_nodes,
        }
        art = await _save_arch_doc(project_id, "sequence_diagrams", body, model, tracability)
        _job_finish(jid, "complete", step="Done", pct=100,
                    result={"arch_doc_id": art["id"], "version": art["version"], "diagrams": total})
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/sequence")
async def start_seq(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "DataModel", "Architecture")
    _existing = await arch_documents.find_one({"project_id": project_id, "type": "sequence_diagrams"}, {"_id": 1})
    set_current_agent_key("arch.regenerate" if _existing else "arch.sequence")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    jid = _new_job(project_id, "sequence")
    asyncio.create_task(_run_seq_job(jid, project_id, model))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# E2. API Contracts (job) — dedicated OpenAPI 3.1 generator
# iter-13.24 — runs one parallel LLM call per service, then merges
# every per-service spec into a single ``api_contracts`` artifact.
# Replaces the brittle "extract ```yaml from LLD" fallback (still
# left in place for back-compat when this job hasn't been run).
# -----------------------------------------------------------
async def _run_api_contracts_job(jid: str, project_id: str, model: str):
    """iter-14.20 — API Contracts are now deterministic Python. No LLM calls.

    Emits one OpenAPI 3.1 YAML block per business service that has ≥1
    attached route. Services with 0 routes are recorded in tracability +
    header note (same UX as the LLM version had).
    """
    try:
        _job_update(jid, status="running", step="Loading services + DDL…", pct=5)
        dm_ctx = await require_stage_context(project_id, "DataModel", "Architecture")
        proj = await projects.find_one({"id": project_id}, {"_id": 0})
        services_all = await arch_services.find(
            {"project_id": project_id, "frontend": False}, {"_id": 0}
        ).to_list(50)
        if not services_all:
            _job_finish(jid, "error", error="No services. Generate service map first.")
            return

        def _route_count(s: dict) -> int:
            return len(s.get("routes_detail") or s.get("api_endpoints") or [])

        def _table_count(s: dict) -> int:
            return len(s.get("tables") or [])

        oltp_ddl, ddl_meta = await _load_oltp_ddl(project_id, dm_ctx, services_all)
        logger.info("arch.api_contracts DDL load: %s", ddl_meta)

        # iter-14.20.2 — Hydrate services with legacy routes fetched fresh
        # from the KB, then append any KB routes still unassigned as a
        # synthetic `legacy-unsorted` service. This guarantees EVERY
        # legacy API is defined in the API Contracts artifact, even when
        # arch.recommend clustering left gaps or wasn't re-run after
        # Build KB.
        _job_update(jid, step="Hydrating services with legacy KB routes…", pct=25)
        services_all, hydrate_meta = await _hydrate_services_with_legacy_routes(
            project_id, services_all,
        )
        legacy_bucket = _synthetic_legacy_unsorted_service(
            hydrate_meta.get("unassigned_routes") or []
        )
        if legacy_bucket:
            services_all.append(legacy_bucket)
            logger.info(
                "arch.api_contracts: appended legacy-unsorted bucket with %d route(s)",
                legacy_bucket["api_count"],
            )
        logger.info(
            "arch.api_contracts hydration: source=%s kb_routes=%d attached_before=%d attached_after=%d coverage=%.1f%% unassigned=%d",
            hydrate_meta.get("surface_source"),
            hydrate_meta.get("kb_total_routes", 0),
            hydrate_meta.get("attached_before", 0),
            hydrate_meta.get("attached_after", 0),
            hydrate_meta.get("coverage_pct", 0.0),
            len(hydrate_meta.get("unassigned_routes") or []),
        )

        # iter-14.20.4 — DDL rescue.
        # If the KB surface returned 0 routes (Build KB didn't produce ROUTE
        # entities, or graph is stale) and services have no owned tables,
        # we still have OLTP DDL. Distribute those tables to services by
        # fuzzy name match; anything unmatched → synthetic `legacy-crud`
        # bucket. The deterministic renderer then emits 5-verb CRUD paths
        # per table. This guarantees an artifact whenever DataModel froze.
        _job_update(jid, step="Fuzzy-attaching OLTP tables to services…", pct=40)
        services_all, unassigned_tables, per_svc_ddl_added = _attach_ddl_tables(
            services_all, oltp_ddl,
        )
        ddl_bucket = _synthetic_legacy_crud_service(unassigned_tables)
        if ddl_bucket:
            services_all.append(ddl_bucket)
            logger.info(
                "arch.api_contracts: appended legacy-crud bucket with %d table(s)",
                len(ddl_bucket["tables"]),
            )
        if per_svc_ddl_added:
            logger.info(
                "arch.api_contracts DDL rescue: attached %d table(s) across %d service(s): %s",
                sum(per_svc_ddl_added.values()),
                len(per_svc_ddl_added),
                per_svc_ddl_added,
            )
        ddl_all_tables = _parse_ddl_table_names(oltp_ddl)

        # iter-14.20.1 — A service is renderable if it has EITHER explicit
        # routes OR owned tables. Tables alone are enough because the
        # deterministic generator synthesises CRUD routes from them.
        services = [s for s in services_all if _route_count(s) > 0 or _table_count(s) > 0]
        skipped = [s for s in services_all if _route_count(s) == 0 and _table_count(s) == 0]

        # iter-14.20.5 — Final safety net.
        # If we STILL have nothing (KB empty AND DDL empty), fabricate a
        # placeholder resource per service from its own name so migration
        # engineers get a scaffold OpenAPI to iterate on, rather than a
        # hard failure. Every op is tagged x-placeholder=true so reviewers
        # know these need to be filled in.
        if not services:
            logger.warning(
                "arch.api_contracts: no routes, no tables, no DDL — "
                "emitting placeholder resources per service."
            )
            for svc in services_all:
                # Skip pure buckets that already had nothing to render
                if svc.get("kind") in ("legacy_bucket", "legacy_crud_bucket"):
                    continue
                nm = svc.get("name") or "resource"
                # Use the service short-name as the resource stem
                svc["tables"] = [re.sub(r"[^a-z0-9_]+", "_", nm.lower()).strip("_") or "resource"]
                svc["_placeholder"] = True
            services = [s for s in services_all if _route_count(s) > 0 or _table_count(s) > 0]
            skipped = [s for s in services_all if _route_count(s) == 0 and _table_count(s) == 0]

        if not services:
            empty_names = ", ".join(
                (s.get("display_name") or s.get("name") or "?") for s in skipped[:8]
            )
            _job_finish(
                jid, "error",
                error=(
                    f"All {len(services_all)} service(s) have 0 attached routes "
                    f"AND 0 owned tables — nothing to spec.\n"
                    f"KB enumerated {hydrate_meta.get('kb_total_routes', 0)} route(s) "
                    f"(source: {hydrate_meta.get('surface_source', 'unavailable')}).\n"
                    f"OLTP DDL enumerated {len(ddl_all_tables)} table(s) "
                    f"(source: {ddl_meta.get('source_used')}, "
                    f"stage_ctx={ddl_meta.get('stage_ctx_ddl_bytes')}B, "
                    f"live_artifact={ddl_meta.get('live_artifact_ddl_bytes')}B, "
                    f"kb_entities={ddl_meta.get('kb_entities_ddl_bytes')}B "
                    f"({ddl_meta.get('kb_entities_table_count')} tables), "
                    f"per_service={ddl_meta.get('per_service_ddl_bytes')}B).\n"
                    f"Fix by (a) re-running Build KB to populate ROUTE entities, "
                    f"or (b) confirming the DataModel OLTP artifact has real "
                    f"`CREATE TABLE ...` statements — check "
                    f"`GET /api/datamodel/{{project_id}}/artifacts` and the "
                    f"artifact preview.\n"
                    f"Empty services: {empty_names}"
                    f"{'…' if len(skipped) > 8 else ''}"
                ),
            )
            return

        synthesized = [
            s for s in services if _route_count(s) == 0 and _table_count(s) > 0
        ]
        if skipped:
            logger.warning(
                "arch.api_contracts skipping %d service(s) with no routes and no tables: %s",
                len(skipped),
                [s.get("name") for s in skipped[:10]],
            )
        if synthesized:
            logger.info(
                "arch.api_contracts synthesising CRUD for %d service(s) with tables but no routes: %s",
                len(synthesized),
                [s.get("name") for s in synthesized[:10]],
            )

        _job_update(jid, step=f"Rendering OpenAPI for {len(services)} services…", pct=55)
        body, _skipped_names_dup = _render_api_contracts_deterministic(
            proj=proj or {},
            services=services_all,
            oltp_ddl=oltp_ddl,
        )

        header_notes: List[str] = []
        # iter-14.20.2 — surface KB coverage + hydration result at the top.
        kb_total = hydrate_meta.get("kb_total_routes") or 0
        attached_after = hydrate_meta.get("attached_after") or 0
        if kb_total:
            header_notes.append(
                f"# KB legacy surface: {kb_total} route(s) enumerated from "
                f"{hydrate_meta.get('surface_source', 'kb')}.\n"
                f"# Attached to services after hydration: {attached_after} "
                f"({hydrate_meta.get('coverage_pct', 0.0)}% coverage).\n"
            )
        if legacy_bucket:
            header_notes.append(
                f"# Note: {legacy_bucket['api_count']} legacy route(s) from "
                f"{len(legacy_bucket.get('module_names') or [])} KB module(s) were "
                f"not clustered into any user service by arch.recommend.\n"
                f"# They are included below under the synthetic "
                f"`legacy-unsorted` service so 100% of the legacy surface is "
                f"defined in this artifact.\n"
                f"# Re-run arch.recommend if you want them clustered into "
                f"proper services.\n"
            )
        if synthesized:
            synth_names = ", ".join(
                (s.get("display_name") or s.get("name") or "?") for s in synthesized[:10]
            )
            header_notes.append(
                f"# Note: {len(synthesized)} service(s) had 0 attached legacy routes\n"
                f"# from arch.recommend AND no matching KB modules; CRUD paths were\n"
                f"# synthesised from their owned tables: {synth_names}"
                f"{'…' if len(synthesized) > 10 else ''}.\n"
            )
        # iter-14.20.4 — surface DDL rescue outcome
        if per_svc_ddl_added:
            header_notes.append(
                f"# Note: DDL rescue attached {sum(per_svc_ddl_added.values())} "
                f"OLTP table(s) to {len(per_svc_ddl_added)} service(s) via fuzzy "
                f"name match (KB had no routes to hydrate from): "
                f"{dict(list(per_svc_ddl_added.items())[:10])}.\n"
            )
        if ddl_bucket:
            header_notes.append(
                f"# Note: {len(ddl_bucket['tables'])} OLTP table(s) could not be "
                f"matched to any service by name; included below as the synthetic "
                f"`legacy-crud` service with canonical 5-verb CRUD paths per "
                f"table.\n"
            )
        if skipped:
            skipped_names = ", ".join(
                (s.get("display_name") or s.get("name") or "?") for s in skipped[:10]
            )
            header_notes.append(
                f"# Note: {len(skipped)} service(s) were skipped because they had\n"
                f"# 0 routes AND 0 owned tables: {skipped_names}"
                f"{'…' if len(skipped) > 10 else ''}.\n"
            )
        if header_notes:
            body = "\n".join(header_notes) + "\n" + body

        total_attached_routes = sum(_route_count(s) for s in services)
        total_synthesized = sum(
            5 * _table_count(s) for s in synthesized
        )
        tracability = {
            "service_count": len(services),
            "skipped_service_count": len(skipped),
            "skipped_service_names": [s.get("name") for s in skipped[:25]],
            "synthesized_service_count": len(synthesized),
            "synthesized_service_names": [s.get("name") for s in synthesized[:25]],
            "model": "deterministic",
            "prompt_key": "deterministic",
            "openapi_version": "3.1.0",
            "surface_grounded": total_attached_routes > 0,
            "total_attached_routes": total_attached_routes,
            "total_synthesized_routes": total_synthesized,
            # iter-14.20.2 — hydration provenance
            "kb_surface_source": hydrate_meta.get("surface_source"),
            "kb_total_routes": hydrate_meta.get("kb_total_routes"),
            "kb_attached_before_hydrate": hydrate_meta.get("attached_before"),
            "kb_attached_after_hydrate": hydrate_meta.get("attached_after"),
            "kb_coverage_pct": hydrate_meta.get("coverage_pct"),
            "legacy_unsorted_routes": (legacy_bucket or {}).get("api_count", 0),
            # iter-14.20.4 — DDL rescue provenance
            "ddl_total_tables": len(ddl_all_tables),
            "ddl_tables_attached_by_rescue": sum(per_svc_ddl_added.values()),
            "ddl_rescue_service_hits": per_svc_ddl_added,
            "legacy_crud_bucket_tables": len((ddl_bucket or {}).get("tables") or []),
            "generator": "arch_deterministic.render_api_contracts",
        }
        _job_update(jid, step="Persisting artifact…", pct=90)
        art = await _save_arch_doc(project_id, "api_contracts", body, "deterministic", tracability)
        await audit_log.insert_one({
            "action": "architecture.generate.api_contracts",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"version": art["version"], "services": len(services),
                        "skipped_services": len(skipped),
                        "synthesized_services": len(synthesized),
                        "mode": "deterministic"},
        })
        _job_finish(jid, "complete", step="Done", pct=100,
                    result={"arch_doc_id": art["id"], "version": art["version"],
                            "services": len(services), "skipped_services": len(skipped),
                            "mode": "deterministic"})
    except HTTPException as he:
        _job_finish(jid, "error", error=he.detail)
    except Exception as e:
        _job_finish(jid, "error", error=str(e))


@router.post("/jobs/start/api_contracts")
async def start_api_contracts(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    await require_stage_context(project_id, "DataModel", "Architecture")
    _existing = await arch_documents.find_one({"project_id": project_id, "type": "api_contracts"}, {"_id": 1})
    set_current_agent_key("arch.regenerate" if _existing else "arch.api_contracts")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    jid = _new_job(project_id, "api_contracts")
    asyncio.create_task(_run_api_contracts_job(jid, project_id, model))
    return {"job_id": jid, "status": "queued"}


# -----------------------------------------------------------
# Job poll
# -----------------------------------------------------------
@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return {"id": j["id"], "status": j["status"], "step": j.get("step", ""),
            "pct": j.get("pct", 0), "error": j.get("error"),
            "result": j.get("result", {}), "kind": j.get("kind"),
            # iter-13.50 — per-section failures (transport vs model) so the
            # UI can surface a "5/10 sections failed with DNS error" toast.
            "section_errors": j.get("section_errors", [])}


# -----------------------------------------------------------
# F. Chat
# -----------------------------------------------------------
@router.post("/chat")
async def arch_chat(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message required")
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    target = payload.get("target_artifact") or "all"
    conv_id = payload.get("conversation_id")
    session_id = (payload.get("session_id") or "").strip()  # iter-13.100

    await require_stage_context(project_id, "DataModel", "Architecture")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})

    arts = await arch_documents.find({"project_id": project_id}, {"_id": 0}).to_list(20)
    arch_context_parts = []
    for a in arts:
        if target == "all" or target == a["type"]:
            arch_context_parts.append(f"## {a['type']}\n\n{a.get('content','')[:4000]}")
    arch_context = "\n\n".join(arch_context_parts)[:10000]

    try:
        rag = await qdrant_search(project_id, message, top_k=5)
    except Exception:
        rag = []
    rag_context = "\n\n---\n\n".join(rag)[:4000]

    template = await _get_prompt(project_id, "arch.chat")
    sp = _safe_format(template, project_name=proj.get("name", ""),
                      arch_context=arch_context, rag_context=rag_context, message=message)

    if not conv_id:
        conv_id = __import__("uuid").uuid4().hex
        await conversations.insert_one({"id": conv_id, "project_id": project_id, "stage": "Architecture",
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
                agent_key="arch.chat",
                project_id=project_id,
                model=model, temperature=0.2, max_tokens=4000, timeout=120.0,
            )
        else:
            r = await chat_completion(
                messages=[{"role": "system", "content": sp}, {"role": "user", "content": message}],
                model=model, temperature=0.2, max_tokens=4000, timeout=120.0,
                agent_key="arch.chat", project_id=project_id,
            )
    except Exception as e:
        raise HTTPException(502, f"LLM failed: {e}")
    content = r.get("content", "") or ""

    changes = []
    for tag, ctype in (("HLD_CHANGE", "hld_section_update"),
                       ("ARCH_CHANGE", "service_modify"),
                       ("LLD_CHANGE", "lld_service_update")):
        for m in re.finditer(rf"\[{tag}:([^\]]+)\]\s*(.*?)\s*\[/{tag}\]", content, re.DOTALL):
            changes.append({"type": ctype, "target": m.group(1).strip(), "new_content": m.group(2).strip()})
    for m in re.finditer(r"\[SERVICE_ADD\]\s*(.*?)\s*\[/SERVICE_ADD\]", content, re.DOTALL):
        changes.append({"type": "service_add", "target": "", "new_content": m.group(1).strip()})
    for m in re.finditer(r"\[SERVICE_REMOVE:([^\]]+)\]", content):
        changes.append({"type": "service_remove", "target": m.group(1).strip(), "new_content": ""})

    msg_id = __import__("uuid").uuid4().hex
    await messages_col.insert_one({
        "id": msg_id, "conversation_id": conv_id, "project_id": project_id,
        "role": "assistant", "content": content, "model": model,
        "tokens": r.get("usage", {}).get("total_tokens", 0),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"conversation_id": conv_id, "message_id": msg_id, "content": content, "changes": changes}


# -----------------------------------------------------------
# G. Apply changes
# -----------------------------------------------------------
@router.post("/{project_id}/apply-changes")
async def apply_changes(project_id: str, payload: dict):
    changes = payload.get("changes") or []
    updated = []
    now = datetime.now(timezone.utc).isoformat()
    for ch in changes:
        ctype = ch.get("type")
        target = ch.get("target", "")
        body = ch.get("new_content", "")
        if ctype == "hld_section_update":
            doc = await arch_documents.find_one({"project_id": project_id, "type": "hld"}, {"_id": 0})
            if doc and not doc.get("frozen"):
                # naive section replace by ## header line
                content = doc["content"]
                pattern = re.compile(rf"(## {re.escape(target)}.*?)(?=\n## |\Z)", re.DOTALL | re.IGNORECASE)
                if pattern.search(content):
                    new = pattern.sub(f"## {target}\n\n{body}\n", content)
                else:
                    new = content + f"\n\n## {target}\n\n{body}\n"
                await arch_documents.update_one(
                    {"project_id": project_id, "type": "hld"},
                    {"$set": {"content": new, "version": doc.get("version", 1) + 1, "updated_at": now}},
                )
                updated.append({"type": "hld", "section": target})
        elif ctype == "service_modify":
            try:
                svc = json.loads(body)
                await arch_services.update_one(
                    {"project_id": project_id, "name": target},
                    {"$set": {**svc, "updated_at": now}},
                )
                updated.append({"type": "service_modify", "service": target})
            except Exception:
                pass
        elif ctype == "service_add":
            try:
                svc = json.loads(body)
                svc.update({"id": __import__("uuid").uuid4().hex, "project_id": project_id,
                            "status": "pending", "codegen_status": "pending",
                            "created_at": now, "updated_at": now, "frontend": False})
                await arch_services.insert_one(svc)
                updated.append({"type": "service_add", "service": svc.get("name")})
            except Exception:
                pass
        elif ctype == "service_remove":
            await arch_services.delete_one({"project_id": project_id, "name": target})
            updated.append({"type": "service_remove", "service": target})
    await audit_log.insert_one({
        "action": "architecture.apply_changes",
        "project_id": project_id, "at": now,
        "details": {"updated": updated, "request_id": payload.get("conversation_message_id")},
    })
    return {"ok": True, "updated": updated}


# -----------------------------------------------------------
# H/I/J/K — CRUD + freeze + reset
# -----------------------------------------------------------
@router.get("/{project_id}/artifacts")
async def list_artifacts(project_id: str):
    arts = await arch_documents.find({"project_id": project_id}, {"_id": 0}).sort("updated_at", -1).to_list(100)
    services = await arch_services.find({"project_id": project_id}, {"_id": 0}).to_list(100)
    # iter-13.81.13 — Auto-heal: if the Service Map is frozen but the
    # Architecture stage hasn't been promoted yet (e.g. project was
    # frozen under the OLD freeze gate that required HLD+LLD, or a
    # previous /approve ran before this iteration shipped), promote
    # silently so CodeGen unlocks without forcing the user to re-click
    # Approve. Idempotent — _promote_architecture_stage is a no-op
    # when the Service Map isn't frozen.
    try:
        sm = next((a for a in arts if a.get("type") == "service_map"), None)
        if sm and sm.get("frozen"):
            proj = await projects.find_one({"id": project_id}, {"stage_status": 1, "_id": 0})
            if (proj or {}).get("stage_status", {}).get("Architecture") != "frozen":
                await _promote_architecture_stage(project_id, frozen_by="auto-heal")
                logger.info("auto-promoted Architecture stage on artifact fetch (project=%s)", project_id)
    except Exception as e:
        logger.warning("Architecture auto-promote failed for %s: %s", project_id, e)
    return {"artifacts": arts, "services": services}


@router.post("/{project_id}/promote")
async def promote_stage(project_id: str):
    """iter-13.81.13 — Manual stage promotion escape hatch. Useful when
    the Service Map is frozen but ``stage_status.Architecture`` is still
    `available` (legacy project, or a previously-failed /approve call)."""
    ok = await _promote_architecture_stage(project_id, frozen_by="user-promote")
    if not ok:
        raise HTTPException(
            400,
            "Cannot promote — the Service Map is not frozen. "
            "Click Approve & Freeze first.",
        )
    return {"ok": True, "stage_frozen": True}


@router.get("/{project_id}/artifact/{artifact_id}")
async def get_arch_artifact(project_id: str, artifact_id: str):
    a = await arch_documents.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    return a


@router.put("/{project_id}/artifact/{artifact_id}")
async def update_arch_artifact(project_id: str, artifact_id: str, payload: dict):
    doc = await arch_documents.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Artifact not found")
    if doc.get("frozen"):
        raise HTTPException(400, "Artifact is frozen")
    new_content = payload.get("content", "")
    now = datetime.now(timezone.utc).isoformat()
    await arch_documents.update_one(
        {"project_id": project_id, "id": artifact_id},
        {"$set": {"content": new_content, "version": doc.get("version", 1) + 1, "updated_at": now}},
    )
    return {"ok": True}


@router.get("/{project_id}/artifact/{artifact_id}/download")
async def download_arch(project_id: str, artifact_id: str):
    a = await arch_documents.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    ext = "yaml" if a["type"] == "api_contracts" else ("json" if a["type"] == "service_map" else "md")
    fn = f"{a['type']}.{ext}"
    return StreamingResponse(io.BytesIO((a.get("content", "") or "").encode("utf-8")),
                             media_type="text/plain",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# iter-14.25.12 — PDF download for HLD / LLD / Sequence / API Contracts.
# The raw markdown/YAML endpoint above stays as-is for developers who
# want the source. This endpoint renders the same artifact through
# reportlab so business reviewers get a paginated, branded, tabular PDF.
@router.get("/{project_id}/artifact/{artifact_id}/download.pdf")
async def download_arch_pdf(project_id: str, artifact_id: str):
    a = await arch_documents.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    proj = await projects.find_one({"id": project_id}, {"_id": 0}) or {}
    pdf_bytes = _render_arch_pdf(proj, a)
    label_map = {
        "hld": "HLD", "lld": "LLD",
        "sequence_diagrams": "Sequence-Diagrams",
        "api_contracts": "API-Contracts",
        "service_map": "Service-Map",
    }
    stem = label_map.get(a.get("type", ""), a.get("type", "architecture"))
    fn = f"{stem}_{(proj.get('name') or 'project').replace(' ', '_')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


def _render_arch_pdf(project: dict, artifact: dict) -> bytes:
    """Render an Architecture artifact (HLD / LLD / Sequence Diagrams /
    API Contracts / Service Map) to PDF bytes.

    Handles: H1/H2/H3, bold/italic/code inline, fenced code blocks,
    bulleted and numbered lists, pipe tables. YAML (api_contracts) and
    JSON (service_map) bodies are rendered as one big preformatted
    code block. Mermaid ``` mermaid ``` blocks in sequence_diagrams are
    kept as-is inside a code block — reviewers copy them into a
    mermaid viewer if they need the rendered diagram.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        ListFlowable, ListItem, Preformatted,
    )
    from reportlab.lib.units import cm

    kind = artifact.get("type", "artifact")
    label_map = {
        "hld": "High-Level Design (HLD)",
        "lld": "Low-Level Design (LLD)",
        "sequence_diagrams": "Sequence Diagrams",
        "api_contracts": "API Contracts",
        "service_map": "Service Map",
    }
    title_text = label_map.get(kind, kind.replace("_", " ").title())

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"{title_text} - {project.get('name', '')}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=22,
                                 spaceAfter=6, textColor=colors.HexColor("#0A2540"))
    sub_style = ParagraphStyle("subtitle", parent=styles["Normal"], fontSize=10,
                               spaceAfter=20, textColor=colors.HexColor("#747480"))
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16,
                        spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#0A2540"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13,
                        spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#0A2540"))
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=11,
                        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#1f2937"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10,
                          leading=14, spaceAfter=4)
    code_style = ParagraphStyle("code", parent=styles["BodyText"], fontSize=8.5,
                                leading=11, fontName="Courier",
                                textColor=colors.HexColor("#1f2937"),
                                backColor=colors.HexColor("#F6F6FA"),
                                borderPadding=6, spaceAfter=6)

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # iter-14.25.13 — Guard against pathological inputs. Real HLDs
    # produced by the deterministic renderer occasionally emit a single
    # bullet item with 8-10 KB of comma-separated identifiers (e.g.
    # "Owned tables (405): TBL_A, TBL_B, …") or a table cell containing
    # a giant merged bounded-context list. ReportLab's Paragraph
    # word-wrap goes exponential on tokens that big (no whitespace to
    # break on) and the whole render blocks the event loop → OOM →
    # SIGKILL by supervisord (exit 137). Hard-truncate content that
    # exceeds these caps; the raw markdown download preserves the full
    # source for anyone who needs the 405-table dump verbatim.
    _PARA_MAX = 3000    # single paragraph cap
    _ITEM_MAX = 1500    # single list-item cap
    _CELL_MAX = 500     # single table-cell cap
    _CSV_KEEP = 40      # when clipping a comma-separated run, keep first N

    def _clip_csv(s: str, max_len: int) -> str:
        """If `s` looks like `A, B, C, …` and busts `max_len`, keep the
        first _CSV_KEEP tokens and append `… (N more)`. Otherwise
        hard-truncate at `max_len` with `…`. Renders as human-readable
        prose rather than a wall of garbled text.
        """
        if len(s) <= max_len:
            return s
        if s.count(",") >= 20:
            parts = [p.strip() for p in s.split(",")]
            kept = parts[:_CSV_KEEP]
            more = len(parts) - _CSV_KEEP
            tail = f" … (+{more} more; see raw source)" if more > 0 else ""
            return ", ".join(kept) + tail
        return s[:max_len].rstrip() + " …"

    def _inline(text: str) -> str:
        """Convert inline markdown to ReportLab mini-HTML.
        Same escaping strategy as srs.py::_render_pdf::_inline — code
        spans first (opaque placeholder), then bold, then italic, then
        restore code spans. Prevents `<font>` overlap crashes.
        """
        code_slots: list[str] = []

        def _stash(m):
            inner = m.group(1).replace("<", "&lt;").replace(">", "&gt;")
            code_slots.append(f'<font face="Courier">{inner}</font>')
            return f"\x00CODE{len(code_slots) - 1}\x00"

        t = re.sub(r"`([^`\n]+)`", _stash, text)
        # HTML-escape after code stashing (before restore).
        t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # \x00CODE...\x00 markers got HTML-escaped — un-escape just those.
        t = re.sub(r"\x00CODE(\d+)\x00",
                   lambda m: code_slots[int(m.group(1))], t)
        # Bold, then italic.
        t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
        # Scrub any stray solo `*` or `_` so reportlab's mini-XML parser
        # doesn't blow up on unbalanced tags.
        t = re.sub(r"(?<!\w)\*(?!\w)", "", t)
        return t

    def _emit_para(story, txt):
        if not txt.strip():
            return
        safe = _clip_csv(txt, _PARA_MAX)
        try:
            story.append(Paragraph(_inline(safe), body))
        except Exception:
            story.append(Paragraph(_esc(safe), body))

    def _emit_list(story, items, ordered=False):
        if not items:
            return
        safe_items = [_clip_csv(x, _ITEM_MAX) for x in items]
        try:
            flow_items = [ListItem(Paragraph(_inline(x), body), leftIndent=8) for x in safe_items]
        except Exception:
            flow_items = [ListItem(Paragraph(_esc(x), body), leftIndent=8) for x in safe_items]
        story.append(ListFlowable(
            flow_items,
            bulletType="1" if ordered else "bullet",
            leftIndent=14,
            bulletFontSize=9,
        ))
        story.append(Spacer(1, 4))

    def _emit_table(story, rows):
        if not rows:
            return
        maxc = max(len(r) for r in rows)
        rows = [r + [""] * (maxc - len(r)) for r in rows]
        cells = [[Paragraph(_inline(_clip_csv(str(c), _CELL_MAX)), body) for c in r] for r in rows]
        tbl = Table(cells, repeatRows=1, hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A2540")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C8C8D0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F6F6FA")]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 6))

    def _emit_code_block(story, text):
        # Preformatted preserves whitespace but can't handle very long
        # lines gracefully; wrap-safe by soft-splitting at 100 chars.
        safe = text.rstrip()
        # ReportLab's Preformatted eats tab chars — normalise.
        safe = safe.replace("\t", "    ")
        story.append(Preformatted(safe, code_style))
        story.append(Spacer(1, 4))

    def _parse_markdown(md: str):
        """Very small markdown walker sized to what our LLM emits.
        Emits (type, payload) tuples: h1/h2/h3, para, ul, ol, table, code.
        """
        lines = md.replace("\r\n", "\n").split("\n")
        i, n = 0, len(lines)
        events = []
        # iter-14.25.13 — Safety guard: if any branch fails to advance
        # `i`, force-advance and log. Prevents a future markdown quirk
        # (like the unhandled H4 headings that caused the original
        # infinite loop on real HLD content) from hanging uvicorn and
        # taking down the container via SIGKILL.
        _prev_i = -1
        _stall = 0
        while i < n:
            if i == _prev_i:
                _stall += 1
                if _stall >= 3:
                    logger.warning(
                        "arch pdf: parser stalled at line %d: %r — force-advancing",
                        i, lines[i][:120],
                    )
                    i += 1
                    _stall = 0
                    continue
            else:
                _stall = 0
            _prev_i = i

            ln = lines[i]
            stripped = ln.strip()

            # Fenced code block.
            if stripped.startswith("```"):
                buf_c = []
                i += 1
                while i < n and not lines[i].strip().startswith("```"):
                    buf_c.append(lines[i])
                    i += 1
                i += 1  # skip closing fence (or EOF)
                events.append(("code", "\n".join(buf_c)))
                continue

            # Heading (H1-H6). H4-H6 fall through to h3 styling; they're
            # rare and don't warrant their own paragraph style, but we
            # MUST recognise them here or the paragraph accumulator
            # loops on them (it breaks on `#`-prefixed lines but the
            # outer dispatch previously only knew about #{1,3}).
            # We allow leading whitespace — the LLD renderer sometimes
            # emits indented `## 4. Endpoints` inside embedded code
            # sections and the strict `^#` regex would miss them,
            # triggering the stall guard for every one.
            m = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", ln)
            if m:
                lvl = min(3, len(m.group(1)))
                events.append((f"h{lvl}", m.group(2)))
                i += 1
                continue

            # Table — must have 2+ pipe rows AND a separator row.
            if "|" in ln and i + 1 < n and re.match(r"^\s*\|?[\s:\-\|]+\|[\s:\-\|]*$", lines[i + 1]):
                rows = []
                # header
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                rows.append(cells)
                i += 2  # skip separator
                while i < n and "|" in lines[i] and lines[i].strip():
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    rows.append(cells)
                    i += 1
                events.append(("table", rows))
                continue

            # Unordered list.
            if re.match(r"^\s*[-*+]\s+", ln):
                items = []
                while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                    items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]))
                    i += 1
                events.append(("ul", items))
                continue

            # Ordered list.
            if re.match(r"^\s*\d+\.\s+", ln):
                items = []
                while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                    items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                    i += 1
                events.append(("ol", items))
                continue

            # Blank line = paragraph break.
            if not stripped:
                i += 1
                continue

            # Plain paragraph — accumulate contiguous non-empty non-special lines.
            para_lines = []
            while i < n:
                ln2 = lines[i]
                s2 = ln2.strip()
                if not s2:
                    break
                if s2.startswith("#") or s2.startswith("```"):
                    break
                if re.match(r"^\s*[-*+]\s+", ln2) or re.match(r"^\s*\d+\.\s+", ln2):
                    break
                if "|" in ln2 and i + 1 < n and re.match(
                        r"^\s*\|?[\s:\-\|]+\|[\s:\-\|]*$", lines[i + 1]):
                    break
                para_lines.append(ln2)
                i += 1
            events.append(("para", " ".join(para_lines)))
        return events

    # ------ Build story ------
    story = []
    story.append(Paragraph(title_text, title_style))
    subtitle_bits = [project.get("name", "").strip() or "Project"]
    if artifact.get("frozen"):
        subtitle_bits.append("FROZEN")
    subtitle_bits.append(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    story.append(Paragraph(" · ".join(subtitle_bits), sub_style))

    content = (artifact.get("content") or "").strip()
    if not content:
        story.append(Paragraph("<i>(artifact is empty)</i>", body))
        pdf.build(story)
        return buf.getvalue()

    # api_contracts (YAML) and service_map (JSON) render as a single code
    # block — they're not meant to be prose.
    if kind in ("api_contracts", "service_map"):
        _emit_code_block(story, content)
        pdf.build(story)
        return buf.getvalue()

    # Everything else: walk as markdown.
    for kind_ev, payload in _parse_markdown(content):
        if kind_ev == "h1":
            story.append(Paragraph(_inline(payload), h1))
        elif kind_ev == "h2":
            story.append(Paragraph(_inline(payload), h2))
        elif kind_ev == "h3":
            story.append(Paragraph(_inline(payload), h3))
        elif kind_ev == "para":
            _emit_para(story, payload)
        elif kind_ev == "ul":
            _emit_list(story, payload, ordered=False)
        elif kind_ev == "ol":
            _emit_list(story, payload, ordered=True)
        elif kind_ev == "table":
            _emit_table(story, payload)
        elif kind_ev == "code":
            _emit_code_block(story, payload)

    pdf.build(story)
    return buf.getvalue()


@router.post("/{project_id}/artifact/{artifact_id}/freeze")
async def freeze_arch_artifact(project_id: str, artifact_id: str):
    a = await arch_documents.find_one({"project_id": project_id, "id": artifact_id}, {"_id": 0})
    if not a:
        raise HTTPException(404, "Artifact not found")
    now = datetime.now(timezone.utc).isoformat()
    await arch_documents.update_one(
        {"project_id": project_id, "id": artifact_id},
        {"$set": {"frozen": True, "frozen_at": now, "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "architecture.artifact.freeze",
        "project_id": project_id, "at": now,
        "details": {"type": a["type"], "version": a.get("version", 1)},
    })

    # iter-13.81.13 — Service Map is now the canonical Architecture
    # freeze gate. HLD / LLD / Sequence / API Contracts are OPTIONAL
    # deliverables: the user can generate them before OR after CodeGen.
    # If freezing the Service Map (or any artifact once SM is already
    # frozen) — promote the stage so CodeGen unlocks.
    await _promote_architecture_stage(project_id, frozen_by="user")
    return {"ok": True}


@router.post("/{project_id}/reset")
async def reset_arch(project_id: str):
    deleted = {
        "arch_documents": (await arch_documents.delete_many({"project_id": project_id})).deleted_count,
        "arch_services": (await arch_services.delete_many({"project_id": project_id})).deleted_count,
    }
    await stage_context_col.delete_one({"project_id": project_id, "stage": "Architecture"})
    now = datetime.now(timezone.utc).isoformat()
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.Architecture": "available",
                  "stage_status.CodeGen": "locked",
                  "stage_status.Living": "locked",
                  "updated_at": now}},
    )
    await audit_log.insert_one({
        "action": "architecture.reset",
        "project_id": project_id, "at": now, "details": deleted,
    })
    return {"ok": True, "deleted": deleted}


# iter-13.50 — Selective cleanup of artifacts whose body is just baked-in
# transport errors (legacy bug fixed this iteration). Lets the user purge
# the corrupted sequence_diagrams / api_contracts / hld / lld documents
# without nuking the whole stage (and the approved service_map).
#
# An artifact is considered "corrupt" when its content contains either:
#   • the glibc DNS marker `Name or service not known`
#   • a `Note over System: error:` line (sequence-diagram error template)
#   • `# OpenAPI generation failed:` header (api_contracts error template)
#   • `_Generation error:` (HLD/LLD per-section error template)
# AND it is not frozen (frozen artifacts are user-blessed; never auto-delete).
@router.post("/{project_id}/purge-broken")
async def purge_broken_artifacts(project_id: str, payload: dict | None = None):
    markers = (
        "Name or service not known",
        "Note over System: error:",
        "# OpenAPI generation failed:",
        "_Generation error:",
        "model error —",
    )
    types_to_check = (payload or {}).get("types") or [
        "sequence_diagrams", "api_contracts", "hld", "lld",
    ]
    cur = arch_documents.find(
        {"project_id": project_id, "type": {"$in": types_to_check}, "frozen": {"$ne": True}},
        {"_id": 0, "id": 1, "type": 1, "content": 1},
    )
    deleted: List[dict] = []
    async for a in cur:
        content = a.get("content", "") or ""
        if any(m in content for m in markers):
            await arch_documents.delete_one(
                {"project_id": project_id, "id": a["id"]}
            )
            deleted.append({"type": a["type"], "id": a["id"]})
    now = datetime.now(timezone.utc).isoformat()
    await audit_log.insert_one({
        "action": "architecture.purge_broken",
        "project_id": project_id, "at": now,
        "details": {"deleted": deleted, "checked_types": types_to_check},
    })
    return {"ok": True, "deleted": deleted, "count": len(deleted)}

