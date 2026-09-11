"""Journey materialiser — Route/View/Column anchors (iter-14.24).

Emits **vertical migration-unit chunks** by walking ``kb_graph`` +
``kb_entities``. Three kinds, each keyed to the natural unit its
downstream consumer regenerates:

- ``api``    — one chunk per **Route**. Content: controller class + all
               its methods + tables referenced + columns of those tables
               + roles guarding the route + BR ids. Downstream consumer:
               backend CodeGen (one controller/service/repository/entity
               file group per api_journey).
- ``ui``     — one chunk per **View/Form** (JSP_FORM entity). Content:
               view file + model bindings + submit verb + pivot_route
               into the matching api_journey. Downstream consumer:
               frontend CodeGen (one form + validators + API client per
               ui_journey).
- ``column`` — opt-in only. One chunk per Column, walking bottom-up
               (retains the original Phase-1 shape). Downstream consumer:
               DataModel stage (per-column lineage, NOT NULL / uniqueness
               provenance).

Design principles
=================
- **Deterministic**: no LLM calls. All traversals are over
  ``kb_graph`` edges (deterministic pass ± optional graphify
  enrichment) or ``kb_entities`` rows (raw extractor output).
- **Route is the pivot**. Every kind carries ``pivot_route`` (verb+path
  string) so retrieval by endpoint is a single indexed lookup and
  ui↔api journeys can be joined trivially.
- **Additive + idempotent**: content-hash upsert into ``kb_journeys``
  keyed by ``(project_id, journey_id, content_hash)``. Stale journeys
  from a previous graph version are pruned at the end of each run.
- **Additive to callers**: no prompt path reads ``kb_journeys`` in
  Phase 1. Rollback = disable the toggle; Build KB behaviour reverts.

Journey doc shape (persisted)
=============================

::

    {
      "project_id":     "<pid>",
      "kind":           "api" | "ui" | "column",
      "journey_id":     "AJ-post-api-register"   # AJ / UJ / CJ prefix
      "pivot_route":    "POST /api/register",
      "seed":           { ... kind-specific ... },
      "trace":          [ { step, node_type, name, source, op, ... }, ... ],
      "endpoints":      [ "POST /api/register" ],
      "roles":          [ "guest" ],
      "classes_touched":[ "AuthCtrl", "UserService", "UserRepo" ],
      "tables":         [ "users" ],
      "columns":        [ "users.email_id", "users.password_hash", ... ],
      "ui_fields":      [ "email", "password" ],       # ui-only
      "business_rules": [ "BR-AUTH-001", ... ],
      "content_hash":   "<sha256[:16]>",
      "graph_version":  3,
      "materialized_at":"2026-08-21T…Z"
    }

Public API
==========
- ``materialize_journeys(project_id) -> dict`` — main entry point.
  Called from ``routes/kb.py::_build_kb_impl`` after ``build_kb_graph``
  when the toggle is on.
- ``load_journeys(project_id, *, kind=None, pivot_route=None,
  service=None, endpoint=None, table=None, limit=200) -> list[dict]``
  — read helper for the API + Phase-2 prompt wiring.
- ``journeys_summary(project_id) -> dict`` — per-kind counts for UI.
- ``render_journey_toon(j) / render_journeys_block(js, ...)`` —
  compact TOON, ready for Phase-2 prompt injection.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger("lama.kb.journey_materializer")

# Bounded traversal so pathological graphs (10k+ methods) don't blow up.
_MAX_METHODS_PER_ROUTE = int(os.environ.get("LAMA_JOURNEY_MAX_METHODS_PER_ROUTE", "20"))
_MAX_TABLES_PER_ROUTE = int(os.environ.get("LAMA_JOURNEY_MAX_TABLES_PER_ROUTE", "10"))
_MAX_COLUMNS_PER_TABLE = int(os.environ.get("LAMA_JOURNEY_MAX_COLS_PER_TABLE", "40"))
_MAX_JOURNEYS_TOTAL = int(os.environ.get("LAMA_JOURNEY_MAX_TOTAL", "10000"))


# ---------------------------------------------------------------------------
# Graph indexing (shared helpers)
# ---------------------------------------------------------------------------

def _index_graph(g: dict) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, list[dict]]]:
    """Return ``(node_by_id, out_edges_by_src, in_edges_by_dst)``."""
    nodes = {n["id"]: n for n in (g.get("nodes") or []) if isinstance(n, dict)}
    out_edges: dict[str, list[dict]] = defaultdict(list)
    in_edges: dict[str, list[dict]] = defaultdict(list)
    for e in (g.get("edges") or []):
        if not isinstance(e, dict):
            continue
        src, dst = e.get("src"), e.get("dst")
        if not src or not dst:
            continue
        out_edges[src].append(e)
        in_edges[dst].append(e)
    return nodes, out_edges, in_edges


def _edges(edges: list[dict], etype: str) -> list[dict]:
    return [e for e in edges if e.get("type") == etype]


def _norm_id_frag(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip().lower()).strip("-") or "unknown"


def _hash_trace(trace: list[dict]) -> str:
    h = hashlib.sha256()
    for step in trace:
        h.update(
            f"{step.get('step')}|{step.get('node_type')}|{step.get('name')}"
            f"|{step.get('op','')}\n".encode()
        )
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# BR cross-index
# ---------------------------------------------------------------------------

_BR_ID_RE = re.compile(r"\bBR-[A-Z0-9]+(?:-[A-Z0-9]+){0,3}\b", re.IGNORECASE)


async def _br_index_by_source(project_id: str) -> dict[str, list[str]]:
    """Map ``source-file-path → [BR-id, …]`` from ``legacy_analysis``."""
    try:
        from db import legacy_analysis
        doc = await legacy_analysis.find_one(
            {"project_id": project_id},
            {"_id": 0, "result": 1},
            sort=[("version", -1)],
        )
    except Exception:  # noqa: BLE001
        return {}
    if not doc:
        return {}
    rules = ((doc.get("result") or {}).get("business_rules") or [])
    idx: dict[str, list[str]] = defaultdict(list)
    for r in rules:
        if not isinstance(r, dict):
            continue
        rid = (r.get("id") or "").strip().upper()
        if not rid or not _BR_ID_RE.match(rid):
            continue
        src = (r.get("source") or "").strip()
        if src:
            idx[src].append(rid)
    return dict(idx)


def _brs_for_sources(sources: Iterable[str], br_idx: dict[str, list[str]]) -> list[str]:
    """Union of BR ids attached to any of the given source paths."""
    if not br_idx:
        return []
    hits: set[str] = set()
    for src in sources:
        if not src:
            continue
        src_lc = src.lower()
        for k, ids in br_idx.items():
            k_lc = (k or "").lower()
            if not k_lc:
                continue
            if k_lc == src_lc or k_lc in src_lc or src_lc in k_lc:
                hits.update(ids)
    return sorted(hits)


# ---------------------------------------------------------------------------
# API-journey builder (Route-anchored, deterministic)
# ---------------------------------------------------------------------------
# Traversal:
#   Route  ← EXPOSES  ← Class
#   Class  → HAS_METHOD → Method*
#   Class  → REFERENCES_TABLE → Table   (deterministic fallback)
#   Method → READS / WRITES → Table     (added by graphify LLM enrichment)
#   Table  → HAS_COLUMN → Column
#   Route  → GUARDED_BY → Role

def _classes_exposing_route(route_id: str, in_edges: dict[str, list[dict]]) -> list[str]:
    return [e["src"] for e in in_edges.get(route_id, []) if e.get("type") == "EXPOSES"]


def _methods_of_class(class_id: str, out_edges: dict[str, list[dict]]) -> list[str]:
    return [e["dst"] for e in out_edges.get(class_id, []) if e.get("type") == "HAS_METHOD"]


def _tables_of_class(class_id: str, out_edges: dict[str, list[dict]]) -> list[str]:
    return [e["dst"] for e in out_edges.get(class_id, []) if e.get("type") == "REFERENCES_TABLE"]


def _tables_of_method(method_id: str, out_edges: dict[str, list[dict]]) -> list[tuple[str, str]]:
    """Returns ``[(table_id, op), …]`` — op ∈ {"READS","WRITES"}."""
    hits: list[tuple[str, str]] = []
    for e in out_edges.get(method_id, []):
        if e.get("type") in ("READS", "WRITES"):
            hits.append((e["dst"], e["type"]))
    return hits


def _columns_of_table(table_id: str, out_edges: dict[str, list[dict]]) -> list[str]:
    return [e["dst"] for e in out_edges.get(table_id, []) if e.get("type") == "HAS_COLUMN"]


def _roles_of_route(route_id: str, out_edges: dict[str, list[dict]]) -> list[str]:
    return [e["dst"] for e in out_edges.get(route_id, []) if e.get("type") == "GUARDED_BY"]


def _build_api_journey(
    route_node: dict,
    node_by_id: dict[str, dict],
    out_edges: dict[str, list[dict]],
    in_edges: dict[str, list[dict]],
    br_idx: dict[str, list[str]],
) -> dict | None:
    """Emit one api_journey for a Route node.

    Returns ``None`` when the route has no exposing Class in the graph
    (e.g. a synthetic Route emitted from a JSP form action whose target
    controller wasn't extracted) — the ui_journey builder will still
    reference it via ``pivot_route``.
    """
    route_id = route_node["id"]
    verb = (route_node.get("verb") or "ANY").upper()
    path = route_node.get("path") or route_node.get("name") or ""
    pivot_route = route_node.get("name") or f"{verb} {path}".strip()
    route_source = route_node.get("source") or ""

    # Step 0 — Route
    trace: list[dict] = [{
        "step": 0, "node_type": "Route", "name": pivot_route,
        "verb": verb, "path": path, "source": route_source,
    }]

    # Step 1 — Roles that guard the route (if any).
    role_ids = _roles_of_route(route_id, out_edges)
    for rid in role_ids:
        rn = node_by_id.get(rid) or {}
        trace.append({
            "step": len(trace), "node_type": "Role",
            "name": rn.get("name"), "op": "GUARDED_BY",
        })

    # Step 2 — Exposing Class(es). Usually 1; multi-class handled by
    # merging their methods and tables into the same journey (one
    # journey per Route is the invariant callers depend on).
    class_ids = _classes_exposing_route(route_id, in_edges)
    if not class_ids:
        # Orphan route: keep the shell so ui_journey can still pivot,
        # but skip persistence — no controller-side content to codegen.
        return None

    seen_tables: dict[str, str] = {}  # table_id → last op ("READS"/"WRITES"/"REFERENCES_TABLE")
    seen_classes: list[str] = []
    seen_methods: list[str] = []
    sources: set[str] = {route_source}

    for cls_id in class_ids:
        cls_node = node_by_id.get(cls_id) or {}
        cls_name = cls_node.get("name") or ""
        if cls_name and cls_name not in seen_classes:
            seen_classes.append(cls_name)
        cls_source = cls_node.get("source") or ""
        if cls_source:
            sources.add(cls_source)
        trace.append({
            "step": len(trace), "node_type": "Class",
            "name": cls_name, "source": cls_source,
        })

        # Methods of the class (bounded).
        for m_id in _methods_of_class(cls_id, out_edges)[:_MAX_METHODS_PER_ROUTE]:
            m_node = node_by_id.get(m_id) or {}
            m_name = m_node.get("name") or ""
            if m_name and m_name not in seen_methods:
                seen_methods.append(m_name)
            m_source = m_node.get("source") or cls_source
            if m_source:
                sources.add(m_source)
            trace.append({
                "step": len(trace), "node_type": "Method",
                "name": m_name, "class": cls_name, "source": m_source,
            })
            # Method-level READS/WRITES (Graph-KB enrichment).
            for t_id, op in _tables_of_method(m_id, out_edges):
                seen_tables[t_id] = op

        # Class-level REFERENCES_TABLE (deterministic fallback so this
        # works even without Graph-KB enrichment).
        for t_id in _tables_of_class(cls_id, out_edges):
            seen_tables.setdefault(t_id, "REFERENCES_TABLE")

    # Cap table + column fan-out.
    tables_out: list[str] = []
    columns_out: list[str] = []
    for t_id, op in list(seen_tables.items())[:_MAX_TABLES_PER_ROUTE]:
        t_node = node_by_id.get(t_id) or {}
        t_name = t_node.get("name") or ""
        t_source = t_node.get("source") or ""
        if t_source:
            sources.add(t_source)
        if t_name:
            tables_out.append(t_name)
        trace.append({
            "step": len(trace), "node_type": "Table",
            "name": t_name, "source": t_source, "op": op,
        })
        for c_id in _columns_of_table(t_id, out_edges)[:_MAX_COLUMNS_PER_TABLE]:
            c_node = node_by_id.get(c_id) or {}
            c_name = c_node.get("name") or ""
            if c_name:
                columns_out.append(c_name)
            trace.append({
                "step": len(trace), "node_type": "Column",
                "name": c_name,
                "short_name": c_node.get("short_name"),
                "data_type": c_node.get("data_type"),
                "table": t_name,
            })

    journey = {
        "kind": "api",
        "journey_id": f"AJ-{_norm_id_frag(verb)}-{_norm_id_frag(path)}",
        "pivot_route": pivot_route,
        "seed": {
            "type": "Route", "verb": verb, "path": path,
            "handler_class": seen_classes[0] if seen_classes else "",
        },
        "trace": trace,
        "endpoints": [pivot_route],
        "roles": sorted({(node_by_id.get(rid) or {}).get("name")
                         for rid in role_ids
                         if (node_by_id.get(rid) or {}).get("name")}),
        "classes_touched": seen_classes,
        "methods": seen_methods,
        "tables": sorted(set(tables_out)),
        "columns": sorted(set(columns_out)),
        "ui_fields": [],
        "business_rules": _brs_for_sources(sources, br_idx),
        "content_hash": _hash_trace(trace),
    }
    return journey


# ---------------------------------------------------------------------------
# UI-journey builder (View-anchored, from kb_entities)
# ---------------------------------------------------------------------------
# JSP_FORM entities: {type: "JSP_FORM", action: "/some/path", source: "…jsp"}.
# JSP_MODEL entities: {type: "JSP_MODEL", name: "…jsp", bindings: [...],
#                      source: "…jsp"}.
# We anchor per (source_file, action) so a view with N forms produces N
# ui_journeys. Model bindings for the same source file attach to every
# form in that file (the extractor doesn't correlate binding→form).

def _normalise_action_to_pivot(action: str, known_routes: set[str]) -> str:
    """Best-effort mapping from a JSP form action to a Route name.

    JSP form actions are emitted by ``owl_extractor.extract_jsp`` as a
    synthetic ``ROUTE`` with verb=POST + normalised path. We reproduce
    the same normalisation here so ``pivot_route`` matches the Route
    node id/name that ``build_api_journey`` operates on.
    """
    norm = (action or "").strip()
    low = norm.lower()
    if (not norm or norm.startswith("#") or
            low.startswith(("javascript:", "mailto:", "http://", "https://"))):
        return ""
    path = norm if norm.startswith("/") else "/" + norm
    path = path.split("?", 1)[0]
    candidate = f"POST {path}"
    if candidate in known_routes:
        return candidate
    # Fall back to any verb the graph knows for this path.
    for r in known_routes:
        try:
            _, rpath = r.split(" ", 1)
        except ValueError:
            continue
        if rpath == path:
            return r
    return candidate  # unresolved — still useful for later joining


async def _load_view_entities(project_id: str) -> tuple[list[dict], dict[str, list[str]]]:
    """Return ``(forms, bindings_by_source)`` from ``kb_entities``."""
    from db import kb_entities

    forms: list[dict] = []
    bindings_by_source: dict[str, list[str]] = defaultdict(list)
    cur = kb_entities.find(
        {"project_id": project_id,
         "type": {"$in": ["JSP_FORM", "JSP_MODEL"]}},
        {"_id": 0},
    )
    async for ent in cur:
        et = ent.get("type")
        if et == "JSP_FORM":
            forms.append(ent)
        elif et == "JSP_MODEL":
            src = (ent.get("source") or "").strip()
            for b in (ent.get("bindings") or []):
                if b and b not in bindings_by_source[src]:
                    bindings_by_source[src].append(b)
    return forms, dict(bindings_by_source)


def _build_ui_journey(
    form_ent: dict,
    bindings_by_source: dict[str, list[str]],
    known_routes: set[str],
    br_idx: dict[str, list[str]],
    seq: int,
) -> dict | None:
    source = (form_ent.get("source") or "").strip()
    action = (form_ent.get("action") or "").strip()
    pivot_route = _normalise_action_to_pivot(action, known_routes)
    ui_fields = list(bindings_by_source.get(source, []))
    trace: list[dict] = [{
        "step": 0, "node_type": "View",
        "name": source.rsplit("/", 1)[-1] or source,
        "source": source, "action": action,
    }]
    for i, f in enumerate(ui_fields, start=1):
        trace.append({
            "step": len(trace), "node_type": "Field",
            "name": f, "source": source,
        })
    if pivot_route:
        # Not a real graph node here — but keep the pivot in the trace
        # so a reader can walk view → route → api_journey without a
        # separate join.
        try:
            verb, path = pivot_route.split(" ", 1)
        except ValueError:
            verb, path = "POST", pivot_route
        trace.append({
            "step": len(trace), "node_type": "Route",
            "name": pivot_route, "verb": verb, "path": path,
            "op": "SUBMITS_TO", "source": source,
        })
    view_name = source.rsplit("/", 1)[-1] or f"view-{seq}"
    return {
        "kind": "ui",
        "journey_id": f"UJ-{_norm_id_frag(view_name)}-{seq}",
        "pivot_route": pivot_route,
        "seed": {
            "type": "View", "source": source,
            "action": action, "framework": "jsp-form",
        },
        "trace": trace,
        "endpoints": [pivot_route] if pivot_route else [],
        "roles": [],
        "classes_touched": [],
        "methods": [],
        "tables": [],
        "columns": [],
        "ui_fields": ui_fields,
        "business_rules": _brs_for_sources([source], br_idx),
        "content_hash": _hash_trace(trace),
    }


# ---------------------------------------------------------------------------
# Column-journey builder (opt-in — DataModel-focused lineage)
# ---------------------------------------------------------------------------
# Bottom-up walk from a Column through its Table to any Class that
# REFERENCES it (or Methods that READ/WRITE if graphify ran) and finally
# to the Routes those classes EXPOSE. Kept intentionally simple —
# api_journeys are the primary artefact; column_journeys are for
# DataModel per-column lineage.

def _build_column_journey(
    col_node: dict,
    node_by_id: dict[str, dict],
    out_edges: dict[str, list[dict]],
    in_edges: dict[str, list[dict]],
    br_idx: dict[str, list[str]],
) -> dict | None:
    col_name = col_node.get("name") or ""
    table_name = col_node.get("table") or col_name.split(".", 1)[0]
    col_short = col_node.get("short_name") or col_name.split(".", 1)[-1]
    table_id = f"Table::{(table_name or '').lower()}"
    if table_id not in node_by_id:
        return None
    table_node = node_by_id[table_id]
    trace: list[dict] = [
        {"step": 0, "node_type": "Column", "name": col_name,
         "short_name": col_short, "table": table_name,
         "data_type": col_node.get("data_type"),
         "source": col_node.get("source") or table_node.get("source", "")},
        {"step": 1, "node_type": "Table", "name": table_name,
         "source": table_node.get("source", "")},
    ]
    # Methods that READ/WRITE this table (graphify-only).
    method_ids: list[tuple[str, str]] = []
    for e in in_edges.get(table_id, []):
        if e.get("type") in ("READS", "WRITES"):
            method_ids.append((e["src"], e["type"]))
    classes: list[str] = []
    routes_hit: set[str] = set()
    sources: set[str] = {col_node.get("source") or "", table_node.get("source", "")}
    for m_id, op in method_ids[:_MAX_METHODS_PER_ROUTE]:
        m_node = node_by_id.get(m_id) or {}
        cls_name = (m_node.get("class_name") or "").strip()
        if cls_name and cls_name not in classes:
            classes.append(cls_name)
        if m_node.get("source"):
            sources.add(m_node["source"])
        trace.append({
            "step": len(trace), "node_type": "Method",
            "name": m_node.get("name"), "class": cls_name,
            "source": m_node.get("source"), "op": op,
        })
        if cls_name:
            cls_id = f"Class::{cls_name.lower()}"
            for r in _edges(out_edges.get(cls_id, []), "EXPOSES"):
                routes_hit.add(r["dst"])
    # Deterministic fallback via REFERENCES_TABLE.
    if not method_ids:
        for e in in_edges.get(table_id, []):
            if e.get("type") == "REFERENCES_TABLE":
                cls_node = node_by_id.get(e["src"]) or {}
                cls_name = cls_node.get("name") or ""
                if cls_name and cls_name not in classes:
                    classes.append(cls_name)
                if cls_node.get("source"):
                    sources.add(cls_node["source"])
                trace.append({
                    "step": len(trace), "node_type": "Class",
                    "name": cls_name, "source": cls_node.get("source"),
                    "op": "REFERENCES_TABLE",
                })
                for r in _edges(out_edges.get(e["src"], []), "EXPOSES"):
                    routes_hit.add(r["dst"])
    # Add up to 3 Routes.
    pivot_route = ""
    for r_id in sorted(routes_hit)[:3]:
        rn = node_by_id.get(r_id) or {}
        rname = rn.get("name") or ""
        if not pivot_route and rname:
            pivot_route = rname
        trace.append({
            "step": len(trace), "node_type": "Route",
            "name": rname, "verb": rn.get("verb"), "path": rn.get("path"),
            "source": rn.get("source"),
        })
    endpoints = sorted({
        s.get("name") for s in trace
        if s.get("node_type") == "Route" and s.get("name")
    })
    return {
        "kind": "column",
        "journey_id": f"CJ-{_norm_id_frag(table_name)}-{_norm_id_frag(col_short)}",
        "pivot_route": pivot_route,
        "seed": {
            "type": "Column", "table": table_name,
            "column": col_short, "data_type": col_node.get("data_type"),
        },
        "trace": trace,
        "endpoints": endpoints,
        "roles": [],
        "classes_touched": sorted(set(classes)),
        "methods": [],
        "tables": [table_name] if table_name else [],
        "columns": [col_name] if col_name else [],
        "ui_fields": [],
        "business_rules": _brs_for_sources(sources, br_idx),
        "content_hash": _hash_trace(trace),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def materialize_journeys(project_id: str) -> dict[str, Any]:
    """Rebuild ``kb_journeys`` for the project. Idempotent + additive.

    Returns per-kind stats:

    ::

        {
          "kinds":  ["api", "ui"],
          "api":    {"written": 218, "endpoints_covered": 218, ...},
          "ui":     {"written": 143, "endpoints_pivoted": 128, ...},
          "column": {"skipped": true, "reason": "kind_disabled"},
          "totals": {"journeys": 361, "endpoints": 218, ...},
          "graph_version": 3,
        }
    """
    from db import kb_journeys, audit_log
    from kb.kb_graph import load_kb_graph
    from kb.journey_config import journey_kb_kinds

    now_iso = datetime.now(timezone.utc).isoformat()
    g = await load_kb_graph(project_id)
    if not g or not g.get("nodes"):
        stats = {
            "kinds": [], "totals": {"journeys": 0, "endpoints": 0},
            "graph_version": None, "skipped": True,
            "reason": "kb_graph_empty",
        }
        await _audit(audit_log, project_id, stats, now_iso)
        return stats

    node_by_id, out_edges, in_edges = _index_graph(g)
    graph_version = g.get("version")
    kinds_enabled = await journey_kb_kinds(project_id)
    br_idx = await _br_index_by_source(project_id)

    all_journeys: list[dict] = []
    per_kind_stats: dict[str, dict] = {}

    # ---- api_journeys -----------------------------------------------------
    known_route_names: set[str] = {
        n.get("name") for n in node_by_id.values()
        if n.get("type") == "Route" and n.get("name")
    }
    if "api" in kinds_enabled:
        api_out: list[dict] = []
        route_nodes = [n for n in node_by_id.values() if n.get("type") == "Route"]
        for rn in route_nodes:
            if len(all_journeys) + len(api_out) >= _MAX_JOURNEYS_TOTAL:
                break
            try:
                j = _build_api_journey(rn, node_by_id, out_edges, in_edges, br_idx)
            except Exception as e:  # noqa: BLE001
                logger.warning("api_journey build failed for %s: %s", rn.get("name"), e)
                j = None
            if j:
                api_out.append(j)
        all_journeys.extend(api_out)
        per_kind_stats["api"] = {
            "written": len(api_out),
            "routes_in_graph": len(route_nodes),
            "endpoints_covered": len({j["pivot_route"] for j in api_out}),
            "avg_trace_len": round(
                sum(len(j["trace"]) for j in api_out) / len(api_out), 2
            ) if api_out else 0.0,
        }
    else:
        per_kind_stats["api"] = {"skipped": True, "reason": "kind_disabled"}

    # ---- ui_journeys ------------------------------------------------------
    if "ui" in kinds_enabled:
        try:
            forms, bindings_by_source = await _load_view_entities(project_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("ui_journey view-entity load failed: %s", e)
            forms, bindings_by_source = [], {}
        ui_out: list[dict] = []
        for i, form in enumerate(forms):
            if len(all_journeys) + len(ui_out) >= _MAX_JOURNEYS_TOTAL:
                break
            try:
                j = _build_ui_journey(
                    form, bindings_by_source, known_route_names, br_idx, i,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("ui_journey build failed for %s: %s",
                               form.get("source"), e)
                j = None
            if j:
                ui_out.append(j)
        all_journeys.extend(ui_out)
        pivoted = sum(1 for j in ui_out if j.get("pivot_route"))
        per_kind_stats["ui"] = {
            "written": len(ui_out),
            "forms_seen": len(forms),
            "endpoints_pivoted": pivoted,
            "unresolved_pivots": len(ui_out) - pivoted,
        }
    else:
        per_kind_stats["ui"] = {"skipped": True, "reason": "kind_disabled"}

    # ---- column_journeys (opt-in) ----------------------------------------
    if "column" in kinds_enabled:
        col_nodes = [n for n in node_by_id.values() if n.get("type") == "Column"]
        col_out: list[dict] = []
        for cn in col_nodes:
            if len(all_journeys) + len(col_out) >= _MAX_JOURNEYS_TOTAL:
                break
            try:
                j = _build_column_journey(cn, node_by_id, out_edges, in_edges, br_idx)
            except Exception as e:  # noqa: BLE001
                logger.warning("column_journey build failed for %s: %s",
                               cn.get("name"), e)
                j = None
            if j:
                col_out.append(j)
        all_journeys.extend(col_out)
        per_kind_stats["column"] = {
            "written": len(col_out),
            "columns_in_graph": len(col_nodes),
        }
    else:
        per_kind_stats["column"] = {"skipped": True, "reason": "kind_disabled"}

    # ---- Persist + prune stale -------------------------------------------
    live_keys: set[str] = set()
    for j in all_journeys:
        doc = dict(j)
        doc.update({
            "project_id": project_id,
            "graph_version": graph_version,
            "materialized_at": now_iso,
        })
        await kb_journeys.update_one(
            {"project_id": project_id,
             "journey_id": j["journey_id"],
             "content_hash": j["content_hash"]},
            {"$set": doc},
            upsert=True,
        )
        live_keys.add(f"{j['journey_id']}:{j['content_hash']}")

    try:
        deleted = 0
        cur = kb_journeys.find(
            {"project_id": project_id},
            {"_id": 1, "journey_id": 1, "content_hash": 1},
        )
        async for old in cur:
            key = f"{old.get('journey_id')}:{old.get('content_hash')}"
            if key not in live_keys:
                await kb_journeys.delete_one({"_id": old["_id"]})
                deleted += 1
        if deleted:
            logger.info("pruned %d stale journeys for %s", deleted, project_id)
    except Exception:  # noqa: BLE001
        pass

    endpoints_seen = sorted({
        j.get("pivot_route") for j in all_journeys if j.get("pivot_route")
    })
    stats = {
        "kinds": kinds_enabled,
        **per_kind_stats,
        "totals": {
            "journeys": len(all_journeys),
            "endpoints": len(endpoints_seen),
        },
        "graph_version": graph_version,
        "skipped": False,
        "reason": None,
    }
    await _audit(audit_log, project_id, stats, now_iso)
    logger.info(
        "journeys materialised for %s: kinds=%s totals=%s (graph_version=%s)",
        project_id, kinds_enabled, stats["totals"], graph_version,
    )
    return stats


async def _audit(audit_log, project_id: str, stats: dict, at_iso: str) -> None:
    try:
        await audit_log.insert_one({
            "action": "kb.journeys.materialize",
            "project_id": project_id,
            "at": at_iso,
            "details": stats,
        })
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

async def load_journeys(
    project_id: str,
    *,
    kind: str | None = None,
    pivot_route: str | None = None,
    service: str | None = None,
    endpoint: str | None = None,
    table: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """List materialised journeys with optional filters.

    - ``kind``        → exact match on discriminator (``api``/``ui``/``column``)
    - ``pivot_route`` → exact match (fast indexed lookup)
    - ``endpoint``    → substring match on ``pivot_route`` (case-insensitive)
    - ``service``     → substring match on any class in ``classes_touched``
    - ``table``       → exact match on any string in ``tables``
    """
    from db import kb_journeys

    query: dict[str, Any] = {"project_id": project_id}
    if kind:
        query["kind"] = kind
    if pivot_route:
        query["pivot_route"] = pivot_route
    if table:
        query["tables"] = table
    cur = kb_journeys.find(query, {"_id": 0}).limit(max(1, int(limit)))
    ep_lc = (endpoint or "").lower()
    svc_lc = (service or "").lower()
    results: list[dict] = []
    async for doc in cur:
        if ep_lc and ep_lc not in (doc.get("pivot_route") or "").lower():
            continue
        if svc_lc:
            classes = [str(x).lower() for x in (doc.get("classes_touched") or [])]
            if not any(svc_lc in c for c in classes):
                continue
        results.append(doc)
    return results


async def journeys_summary(project_id: str) -> dict:
    """Per-kind counts + endpoint coverage for UI / API health-check."""
    from db import kb_journeys
    from kb.journey_config import all_journey_kinds

    per_kind: dict[str, int] = {}
    for k in all_journey_kinds():
        try:
            per_kind[k] = await kb_journeys.count_documents(
                {"project_id": project_id, "kind": k},
            )
        except Exception:  # noqa: BLE001
            per_kind[k] = 0
    try:
        total = await kb_journeys.count_documents({"project_id": project_id})
    except Exception:  # noqa: BLE001
        total = sum(per_kind.values())
    endpoints: set[str] = set()
    latest_ts: str | None = None
    if total:
        cur = kb_journeys.find(
            {"project_id": project_id},
            {"_id": 0, "pivot_route": 1, "materialized_at": 1},
        ).limit(2000)
        async for d in cur:
            pr = d.get("pivot_route") or ""
            if pr:
                endpoints.add(pr)
            ts = d.get("materialized_at")
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    return {
        "exists": total > 0,
        "count": total,
        "by_kind": per_kind,
        "endpoints_covered": len(endpoints),
        "latest_materialized_at": latest_ts,
    }


# ---------------------------------------------------------------------------
# TOON rendering (Phase 2 will consume)
# ---------------------------------------------------------------------------

def render_journey_toon(journey: dict) -> str:
    """Compact TOON serialisation of a single journey.

    Deliberately minimal — Phase 2 will prepend a chunk of these to
    stage prompts, so every non-essential byte is a waste.
    """
    kind = journey.get("kind", "api").upper()
    header = (
        f"[{kind}_JOURNEY:{journey.get('journey_id')}] "
        f"route={journey.get('pivot_route','') or '-'}"
    )
    lines: list[str] = [header]
    for step in journey.get("trace") or []:
        parts = [f"  [{(step.get('node_type') or '?').upper()}:{step.get('name','')}]"]
        for k in ("op", "class", "verb", "path", "source"):
            v = step.get(k)
            if v:
                parts.append(f"{k}={v}")
        lines.append(" ".join(parts))
    if journey.get("ui_fields"):
        lines.append(f"  [FIELDS] {','.join(journey['ui_fields'])}")
    if journey.get("tables"):
        lines.append(f"  [TABLES] {','.join(journey['tables'])}")
    if journey.get("columns"):
        # Columns can be long; keep the header line size predictable.
        cols = journey["columns"]
        if len(cols) > 12:
            cols = cols[:12] + [f"...+{len(journey['columns']) - 12} more"]
        lines.append(f"  [COLUMNS] {','.join(cols)}")
    if journey.get("business_rules"):
        lines.append(f"  [BR] ids={','.join(journey['business_rules'])}")
    return "\n".join(lines)


def render_journeys_block(journeys: Iterable[dict], max_chars: int = 4000) -> str:
    """Concatenate journey TOON blocks, truncating at the journey boundary."""
    out: list[str] = ["# JOURNEYS"]
    total = len(out[0]) + 1
    for j in journeys:
        block = render_journey_toon(j)
        if total + len(block) + 1 > max_chars:
            out.append("...[truncated]")
            break
        out.append(block)
        total += len(block) + 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Phase 2 — prompt-time context helpers
# ---------------------------------------------------------------------------

def _score_journey_for_seeds(
    journey: dict,
    seeds_lc: list[str],
    endpoints_lc: list[str],
) -> int:
    """Cheap relevance score. Higher = better match."""
    if not seeds_lc and not endpoints_lc:
        return 1
    score = 0
    pivot = (journey.get("pivot_route") or "").lower()
    if pivot:
        for ep in endpoints_lc:
            if ep and ep in pivot:
                score += 8
        for s in seeds_lc:
            if s and s in pivot:
                score += 2
    for cls in journey.get("classes_touched") or []:
        cls_lc = str(cls).lower()
        for s in seeds_lc:
            if s and s in cls_lc:
                score += 2
                break
    for tbl in journey.get("tables") or []:
        tbl_lc = str(tbl).lower()
        for s in seeds_lc:
            if s and s in tbl_lc:
                score += 2
                break
    for col in (journey.get("columns") or [])[:20]:
        col_lc = str(col).lower()
        for s in seeds_lc:
            if s and s in col_lc:
                score += 1
                break
    for fld in journey.get("ui_fields") or []:
        fld_lc = str(fld).lower()
        for s in seeds_lc:
            if s and s in fld_lc:
                score += 1
                break
    return score


async def journey_context_block(
    project_id: str,
    stage: str,
    seeds: Iterable[str] | None = None,
    endpoint_hints: Iterable[str] | None = None,
    max_chars: int = 4000,
    max_journeys: int = 12,
) -> str:
    """Return a ready-to-inject JOURNEYS TOON block for ``stage``.

    Gated by :func:`kb.journey_config.is_stage_enabled` — returns ``""`` when
    the toggle is off, or when ``kb_journeys`` is empty. Selects the top
    N journeys by seed / endpoint overlap so callers can prepend a compact,
    focused slice to their stage prompt.

    Callers should treat this as **additive** context: the legacy retrieval
    and RAG paths must still run so behaviour is identical when the toggle
    flips off.
    """
    from kb.journey_config import is_stage_enabled

    if not await is_stage_enabled(project_id, stage):
        return ""

    seeds_lc = sorted({(s or "").strip().lower() for s in (seeds or [])
                       if s and len(str(s)) >= 3})
    endpoints_lc = sorted({(e or "").strip().lower() for e in (endpoint_hints or [])
                           if e and len(str(e)) >= 2})

    try:
        # Pull a bounded superset (api first, then ui); scoring narrows it.
        docs = await load_journeys(project_id, kind="api", limit=400)
        docs += await load_journeys(project_id, kind="ui", limit=200)
    except Exception as e:  # noqa: BLE001
        logger.debug("journey_context_block load failed: %s", e)
        return ""

    if not docs:
        return ""

    ranked = [
        (_score_journey_for_seeds(d, seeds_lc, endpoints_lc), d) for d in docs
    ]
    # Keep positives when we have seeds/endpoints; otherwise fall back to all.
    if seeds_lc or endpoints_lc:
        ranked = [rd for rd in ranked if rd[0] > 0]
        if not ranked:
            return ""
    ranked.sort(key=lambda rd: rd[0], reverse=True)
    picked = [d for _, d in ranked[: max(1, int(max_journeys))]]

    header = (
        "# JOURNEY-KB SLICE — deterministic legacy → target migration units.\n"
        "# Each [API_JOURNEY] is one endpoint's controller→service→repo→"
        "tables→columns trace; each [UI_JOURNEY] is a form/view that pivots\n"
        "# into an api_journey via `route=`. Reproduce the SAME behaviour on\n"
        "# the target stack — do NOT invent tables, columns, or endpoints\n"
        "# not present here."
    )
    body = render_journeys_block(picked, max_chars=max(500, max_chars - len(header) - 2))
    return header + "\n" + body


# ---------------------------------------------------------------------------
# Per-UC grounding pack — the "storytelling depth" fix for SRS §5
# ---------------------------------------------------------------------------
#
# Problem this addresses (iter-14.25.1): the generic ``journey_context_block``
# above pulls the top-N journeys for the whole section using its
# SECTION_QUERIES seeds ("workflow / approval / submission / ..."). Those
# seeds are too broad to bind ONE journey to ONE use case, so the LLM
# sees a bag of tangentially-related journeys and writes bird's-eye
# summaries instead of concrete stories.
#
# The fix is a **per-UC pack** driven by each UC's §3 one-liner. For
# every ``UC-XX`` we know the model must emit, we pick exactly one
# api_journey + one ui_journey by scoring against the one-liner text,
# then emit a labelled pack the model can anchor its Narrative + Field
# Spec Table + Business Workflow on.

import re as _re

_UC_LINE_RE = _re.compile(
    # Matches:
    #   "- UC-04 — *The applicant submits …*"
    #   "* UC-04 - The applicant submits …"
    #   "UC-04: The applicant submits …"
    #   "| UC-04 | The applicant submits … |"    (table row)
    r"(?:^|\|)\s*[-*+]?\s*(UC-[A-Za-z0-9_-]{1,40})\s*"
    r"(?:[:\-–—]|\|)\s*[*_`\s]*([^\n|]{6,240})",
    _re.MULTILINE,
)


def parse_uc_oneliners(section_text: str, cap: int = 60) -> dict[str, str]:
    """Extract ``{UC-XX: one-liner}`` pairs from a §3 markdown block.

    Handles the three formats §3.3 tends to emit: bullet + em-dash,
    ``UC-04: text`` and pipe-table rows. Returns dedup'd, insertion-ordered
    map capped at ``cap`` entries so a pathological KB can't blow the
    prompt budget.
    """
    if not section_text:
        return {}
    out: dict[str, str] = {}
    for m in _UC_LINE_RE.finditer(section_text):
        uid = m.group(1)
        if uid in out:
            continue
        line = m.group(2).strip().strip("*_`").strip()
        # Kill trailing markdown artefacts and citation parens.
        line = _re.sub(r"\s{2,}", " ", line)
        line = line.rstrip(".;:").strip()
        if line and len(line) >= 6:
            out[uid] = line
        if len(out) >= cap:
            break
    return out


def _seeds_from_uc_line(uc_line: str) -> tuple[list[str], list[str]]:
    """Turn a UC one-liner into ``(seeds, endpoint_hints)`` for scoring.

    Seeds are content nouns/verbs longer than 3 chars minus stop words;
    endpoint_hints are the ``/path`` fragments if the line happens to
    contain any (rare — most one-liners are pure business prose).
    """
    if not uc_line:
        return [], []
    text_lc = uc_line.lower()
    endpoints = _re.findall(r"/[a-z0-9/_\-{}]+", text_lc)
    tokens = _re.findall(r"[a-z][a-z0-9_]{3,}", text_lc)
    stop = {
        "the", "and", "for", "with", "from", "into", "this", "that", "they",
        "them", "their", "then", "than", "when", "where", "will", "shall",
        "must", "have", "having", "does", "some", "user", "users", "system",
        "systems", "action", "actions", "process", "processes", "using",
        "there", "these", "those", "make", "makes", "made",
    }
    seeds = [t for t in tokens if t not in stop]
    # Preserve order + dedupe.
    seen: dict[str, None] = {}
    for s in seeds:
        seen.setdefault(s, None)
    return list(seen.keys())[:20], endpoints[:3]


def _render_uc_pack(uc_id: str, uc_line: str,
                    api_j: dict | None, ui_j: dict | None) -> str:
    """Compact per-UC grounding pack — anchors Narrative + Metadata."""
    lines: list[str] = [
        f"── {uc_id} GROUNDING PACK ──",
        f"  seed one-liner: {uc_line}",
    ]
    if api_j:
        route = api_j.get("pivot_route") or "-"
        cls = ", ".join((api_j.get("classes_touched") or [])[:4]) or "-"
        tables = ", ".join((api_j.get("tables") or [])[:6]) or "-"
        cols = api_j.get("columns") or []
        if len(cols) > 10:
            cols_s = ", ".join(cols[:10]) + f", …+{len(cols)-10}"
        else:
            cols_s = ", ".join(cols) or "-"
        roles = ", ".join((api_j.get("roles") or [])[:4]) or "-"
        brs = ", ".join((api_j.get("business_rules") or [])[:6]) or "-"
        lines += [
            f"  API_JOURNEY   route={route}",
            f"    classes:     {cls}",
            f"    tables:      {tables}",
            f"    columns:     {cols_s}",
            f"    roles:       {roles}",
            f"    BRs:         {brs}",
        ]
    else:
        lines.append("  API_JOURNEY   (no api_journey matched — emit "
                     "`⚠ EVIDENCE GAP: no api_journey for this UC` inline)")
    if ui_j:
        route = ui_j.get("pivot_route") or "-"
        fields = ui_j.get("ui_fields") or []
        if len(fields) > 12:
            f_s = ", ".join(fields[:12]) + f", …+{len(fields)-12}"
        else:
            f_s = ", ".join(fields) or "-"
        src = (ui_j.get("seed") or {}).get("source") or "-"
        lines += [
            f"  UI_JOURNEY    route={route}",
            f"    view file:   {src}",
            f"    form fields: {f_s}",
        ]
    else:
        lines.append("  UI_JOURNEY    (no ui_journey matched — screen "
                     "labels + field spec come from api_journey columns)")
    return "\n".join(lines)


async def journey_grounding_for_use_cases(
    project_id: str,
    uc_oneliners: dict[str, str],
    *,
    stage: str = "srs",
    max_chars: int = 8000,
    max_ucs: int = 30,
) -> str:
    """Per-UC grounding block for SRS §5 detailed_use_cases.

    Gated by ``is_stage_enabled(pid, stage)``. For each UC ID whose
    one-liner scores against at least one api_journey or ui_journey we
    emit a labelled pack. UCs with no matching journey get an explicit
    `⚠ EVIDENCE GAP` marker so the LLM can't silently drop them.

    Returns "" when the gate is off, when ``uc_oneliners`` is empty, or
    when no journeys exist at all — keeping the caller's behaviour
    identical to pre-fix output in every negative branch.
    """
    from kb.journey_config import is_stage_enabled

    if not uc_oneliners:
        return ""
    if not await is_stage_enabled(project_id, stage):
        return ""

    try:
        api_docs = await load_journeys(project_id, kind="api", limit=1000)
        ui_docs = await load_journeys(project_id, kind="ui", limit=500)
    except Exception as e:  # noqa: BLE001
        logger.debug("journey_grounding_for_use_cases load failed: %s", e)
        return ""
    if not api_docs and not ui_docs:
        return ""

    header = (
        "══════════════════════════════════════════════════════════════════════\n"
        "PER-UC JOURNEY GROUNDING PACKS  (HARD RULE — anchor every §5 UC on ITS pack)\n"
        "══════════════════════════════════════════════════════════════════════\n"
        "For every `UC-XX` below, the pack lists the ONE api_journey and (when\n"
        "available) the ONE ui_journey that own this use case's endpoint,\n"
        "tables, columns, roles, business-rule IDs and form fields.\n"
        "\n"
        "Anchoring contract:\n"
        "  • The `#### Narrative (User Story)` for UC-XX MUST name the ACTOR\n"
        "    (from api_journey.roles or ui_journey), the SCREEN or endpoint\n"
        "    (`route=` in the pack), and walk them through the CONCRETE\n"
        "    tables/columns/fields in this pack — no generic verbs, no\n"
        "    bird's-eye summaries.\n"
        "  • The `#### Field Specification Table` for UC-XX MUST use the\n"
        "    ui_journey `form fields` and api_journey `columns` VERBATIM.\n"
        "  • The `#### Business Workflow` for UC-XX MUST cite the pack's\n"
        "    classes / tables in the source column.\n"
        "  • The `#### Business Rules` MUST reuse the pack's `BRs:` IDs\n"
        "    verbatim — do NOT renumber and do NOT invent new BRs when the\n"
        "    pack already lists some for this route.\n"
        "  • NEVER swap packs across UCs. UC-01's Narrative binds ONLY to\n"
        "    UC-01's pack. If a UC's pack shows `⚠ EVIDENCE GAP`, emit that\n"
        "    marker inline in the UC narrative instead of borrowing another\n"
        "    UC's route/tables.\n"
    )

    packs: list[str] = []
    used_chars = len(header)
    for i, (uid, line) in enumerate(uc_oneliners.items()):
        if i >= max_ucs:
            packs.append(f"...[+{len(uc_oneliners) - i} more UCs — pack "
                         "truncated at max_ucs; re-run with a wider budget]")
            break
        seeds, ep_hints = _seeds_from_uc_line(line)
        # Score api candidates.
        best_api = None
        best_api_score = 0
        for j in api_docs:
            s = _score_journey_for_seeds(j, seeds, ep_hints)
            if s > best_api_score:
                best_api_score = s
                best_api = j
        # Score ui candidates — prefer ones pivoting to the picked api route.
        best_ui = None
        best_ui_score = 0
        api_route = (best_api or {}).get("pivot_route", "").lower()
        for j in ui_docs:
            s = _score_journey_for_seeds(j, seeds, ep_hints)
            if api_route and (j.get("pivot_route") or "").lower() == api_route:
                s += 5  # strong bonus: ui pivots into the same route
            if s > best_ui_score:
                best_ui_score = s
                best_ui = j
        # Emit even when both are None so §5 can honestly emit an
        # EVIDENCE GAP for that UC (rather than confabulating).
        if best_api_score == 0:
            best_api = None
        if best_ui_score == 0:
            best_ui = None
        pack_text = _render_uc_pack(uid, line, best_api, best_ui)
        if used_chars + len(pack_text) + 2 > max_chars:
            packs.append(f"...[truncated at UC {uid} — budget exhausted]")
            break
        packs.append(pack_text)
        used_chars += len(pack_text) + 2
    if not packs:
        return ""
    return header + "\n" + "\n\n".join(packs) + "\n"
