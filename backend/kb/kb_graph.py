"""Graphified Knowledge Base (iter-13.19).

Builds a property graph from existing OWL entities + business ontology and
persists it to `kb_graph` (one Mongo doc per project). Used by SRS /
Architecture / CodeGen prompts when ``LAMA_USE_GRAPH_KB=1`` to retrieve
compact, role-aware subgraphs in place of (or alongside) raw RAG chunks.

Design choices
==============
- **MongoDB-backed adjacency lists** — zero new infra. Replaceable with
  Neo4j / Graphiti later by swapping `build_kb_graph()` / `load_kb_graph()`
  without touching the retriever API.
- **Idempotent**: re-runs collapse duplicate nodes by ``(type, name)``.
- **Best-effort**: any failure logs a warning and returns ``False`` /
  empty subgraph so the rest of the pipeline degrades gracefully.

Node types: ``Module, Class, Method, Table, Column, Route, Role,
BusinessEntity``
Edge types: ``HAS_METHOD, HAS_COLUMN, CALLS, READS, WRITES, EXPOSES,
GUARDED_BY, BELONGS_TO_MODULE, BELONGS_TO_ENTITY, REFERENCES_TABLE``
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("lama.kb_graph")


_GRAPHIFY_ENABLED_DEFAULT = "1"


async def _detect_available_llm_route(project_id: str) -> str:
    """iter-13.40 — Detect which LLM route is available for graphify, in
    priority order. Returns one of:
       "factory"     — Factory.ai orchestrator enabled with an app key
       "console"     — at least one is_active provider in Console (any vendor:
                       Anthropic, OpenAI, OpenRouter, Groq, Ollama, …)
       "env"         — env-var OPENROUTER_API_KEY present (last-resort)
       "none"        — no route at all → caller must skip LLM enrichment
    Used by build_kb_graph to short-circuit gracefully when graphify is ON
    but the user removed every provider. The deterministic graph is still
    persisted; only the LLM enrichment step is suppressed.
    """
    # 1. Factory.ai
    try:
        from db import projects as _projects_col
        _proj = await _projects_col.find_one(
            {"id": project_id}, {"_id": 0, "settings.factory_orchestrator": 1},
        ) or {}
        _fo = (_proj.get("settings") or {}).get("factory_orchestrator") or {}
        if bool(_fo.get("enabled")) and bool((_fo.get("app_key") or "").strip()):
            return "factory"
    except Exception:
        pass
    # 2. Any Console provider (Anthropic / OpenAI / OpenRouter / Groq / Ollama)
    try:
        from db import model_providers as mp_col
        if await mp_col.count_documents({"is_active": True}) > 0:
            return "console"
    except Exception:
        pass
    # 3. Env-var OpenRouter (legacy fallback)
    if (os.environ.get("OPENROUTER_API_KEY") or "").strip():
        return "env"
    return "none"


async def _graphify_enabled(project_id: str, override: bool | None = None) -> bool:
    if isinstance(override, bool):
        return override
    # Per-project Graph KB toggle has priority. When disabled from the
    # Sidebar, graphify must not run any LLM call.
    try:
        from kb.graph_config import is_graph_kb_enabled
        if not await is_graph_kb_enabled(project_id):
            return False
    except Exception:
        pass
    raw = (os.environ.get("LAMA_GRAPHIFY_ENRICH", _GRAPHIFY_ENABLED_DEFAULT) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

_TABLE_TOKEN_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\b", re.IGNORECASE)


def _norm(s: str) -> str:
    return (s or "").strip()


def _node_key(ntype: str, name: str) -> str:
    return f"{ntype}::{name.lower()}"


def _ensure_node(
    nodes: dict[str, dict],
    ntype: str,
    name: str,
    **props,
) -> str:
    """Idempotently add a node; merge props on collision. Returns node key."""
    name = _norm(name)
    if not name:
        return ""
    key = _node_key(ntype, name)
    if key not in nodes:
        nodes[key] = {
            "id": key,
            "type": ntype,
            "name": name,
            **{k: v for k, v in props.items() if v is not None},
        }
    else:
        for k, v in props.items():
            if v is None:
                continue
            existing = nodes[key].get(k)
            if existing is None:
                nodes[key][k] = v
            elif isinstance(existing, list) and v not in existing:
                existing.append(v)
            elif existing != v and not isinstance(existing, list):
                nodes[key][k] = [existing, v]
    return key


def _add_edge(edges: list[dict], src: str, dst: str, etype: str, **props) -> None:
    if not src or not dst or src == dst:
        return
    edges.append({"src": src, "dst": dst, "type": etype, **props})


async def build_kb_graph(
    project_id: str,
    graphify_enabled: bool | None = None,
    phase_callback=None,
) -> dict[str, Any]:
    """Build (or rebuild) the property graph for a project.

    Reads from ``kb_entities`` (OWL output) + ``business_ontologies`` and
    writes a single doc to ``kb_graph``. Returns the persisted summary
    ``{nodes_count, edges_count, by_type, version, updated_at}``.
    """
    # Imports kept local to avoid a hard import dependency during seed.
    from db import kb_entities, business_ontologies, kb_graph as kb_graph_col

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    # ---- 1) Lift OWL entities into the graph -------------------------------
    # iter-13.86 — emit periodic progress so the UI's "graph_build" phase
    # stops looking frozen on large legacy KBs (10k+ kb_entities). Without
    # this, the user sees a static "Phase: graph_build" for the entire
    # duration of the deterministic O(entities) lift below — which on real
    # projects (e.g. EJHS: 60k+ entities) can run for minutes before the
    # phase even flips to "graphify". We now ping phase_callback every 500
    # entities OR every 2 seconds, whichever comes first.
    try:
        total_entities = await kb_entities.count_documents({"project_id": project_id})
    except Exception:  # noqa: BLE001
        total_entities = 0

    cur = kb_entities.find({"project_id": project_id}, {"_id": 0})
    classes_by_file: dict[str, list[str]] = defaultdict(list)
    table_keys: set[str] = set()

    import time as _time
    processed = 0
    last_progress_ts = _time.time()
    if phase_callback is not None:
        try:
            await phase_callback("graph_build", {
                "step": "lift_entities",
                "entities_total": total_entities,
                "entities_processed": 0,
                "nodes_built": 0,
                "edges_built": 0,
            })
        except Exception:  # pragma: no cover — observability only
            pass

    async for ent in cur:
        processed += 1
        # Heartbeat every 500 entities OR every 2s — whichever fires first.
        if phase_callback is not None and (processed % 500 == 0 or (_time.time() - last_progress_ts) >= 2.0):
            last_progress_ts = _time.time()
            try:
                await phase_callback("graph_build", {
                    "step": "lift_entities",
                    "entities_total": total_entities,
                    "entities_processed": processed,
                    "nodes_built": len(nodes),
                    "edges_built": len(edges),
                })
            except Exception:  # pragma: no cover
                pass
        et = (ent.get("type") or "").upper()
        name = ent.get("name") or ent.get("file") or ""
        source = ent.get("source") or ent.get("file") or ""

        if et == "CLASS" and name:
            ck = _ensure_node(
                nodes, "Class", name,
                namespace=ent.get("namespace"),
                source=source,
            )
            if source:
                mk = _ensure_node(nodes, "Module", source)
                _add_edge(edges, ck, mk, "BELONGS_TO_MODULE")
                classes_by_file[source].append(ck)
            # Methods nested under the class
            for meth in ent.get("methods") or []:
                mname = meth.get("name") if isinstance(meth, dict) else str(meth)
                if not mname:
                    continue
                qualified = f"{name}.{mname}"
                mk = _ensure_node(
                    nodes, "Method", qualified,
                    short_name=mname,
                    class_name=name,
                    source=source,
                )
                _add_edge(edges, ck, mk, "HAS_METHOD")

        elif et == "TABLE" and name:
            tk = _ensure_node(nodes, "Table", name, source=source)
            table_keys.add(tk)
            for col in ent.get("columns") or []:
                cname = col.get("name") if isinstance(col, dict) else str(col)
                if not cname:
                    continue
                colk = _ensure_node(
                    nodes, "Column", f"{name}.{cname}",
                    short_name=cname,
                    table=name,
                    data_type=(col.get("type") if isinstance(col, dict) else None),
                )
                _add_edge(edges, tk, colk, "HAS_COLUMN")

        elif et in ("TABLE_HINT", "JSP_TABLE_REFS") and (name or ent.get("tables")):
            referenced = ent.get("tables") or [name]
            for t in referenced:
                if not t:
                    continue
                tk = _ensure_node(nodes, "Table", t, hinted=True, source=source)
                table_keys.add(tk)
                # If we know the file owns a class, wire a READS edge.
                for cls_key in classes_by_file.get(source, []):
                    _add_edge(edges, cls_key, tk, "REFERENCES_TABLE",
                              evidence=source)

        elif et == "ROUTE":
            route_name = f"{(ent.get('verb') or 'ANY').upper()} {ent.get('name') or name}"
            rk = _ensure_node(
                nodes, "Route", route_name,
                verb=(ent.get("verb") or "ANY"),
                path=ent.get("name") or name,
                source=source,
            )
            for cls_key in classes_by_file.get(source, []):
                _add_edge(edges, cls_key, rk, "EXPOSES")
            # Role guards declared on the same route line (PHP/Java filters).
            for role in ent.get("roles") or []:
                rolek = _ensure_node(nodes, "Role", role)
                _add_edge(edges, rk, rolek, "GUARDED_BY")

        elif et == "ROLE" and name:
            _ensure_node(nodes, "Role", name, source=source)

    # ---- 2) Lift business ontology clusters ---------------------------------
    if phase_callback is not None:
        try:
            await phase_callback("graph_build", {
                "step": "lift_business_ontology",
                "entities_total": total_entities,
                "entities_processed": processed,
                "nodes_built": len(nodes),
                "edges_built": len(edges),
            })
        except Exception:  # pragma: no cover
            pass
    bo = await business_ontologies.find_one({"project_id": project_id}, {"_id": 0})
    if bo:
        for entity in (bo.get("entities") or []):
            ename = entity.get("name") or entity.get("label")
            if not ename:
                continue
            ek = _ensure_node(
                nodes, "BusinessEntity", ename,
                description=entity.get("description"),
                cluster_size=entity.get("size"),
            )
            for member in (entity.get("members") or entity.get("classes") or []):
                # Members can be class names or table names — try both.
                candidate_class = _node_key("Class", member)
                if candidate_class in nodes:
                    _add_edge(edges, nodes[candidate_class]["id"], ek, "BELONGS_TO_ENTITY")
                    continue
                candidate_table = _node_key("Table", member)
                if candidate_table in nodes:
                    _add_edge(edges, nodes[candidate_table]["id"], ek, "BELONGS_TO_ENTITY")

    # ---- 3) LLM enrichment pass (iter-13.32 — "graphify") -------------------
    # Best-effort: routes through llm.fabric_call with agent_key "kb.graphify"
    # (tier=high → Anthropic Opus by default via Console.routing). Adds
    # inferred edges (CALLS / READS / WRITES) and BusinessEntity nodes the
    # deterministic pass could not see. Failures are swallowed so KB build
    # never breaks because of a missing/throttled LLM key.
    #
    # iter-13.36 — REUSE FACTORY ONBOARDING. When Factory.ai orchestrator
    # is enabled AND first-run onboarding already completed for this
    # project (settings.factory_orchestrator.onboarded_at is set), the
    # Factory droid has already built a rich KB+graph view in its CWD as
    # part of its P0 phase (see prompts/factory-modernization-agent.yml).
    # Running ANOTHER LLM enrichment here would:
    #   • burn tokens duplicating work the droid already did
    #   • re-prompt with a smaller subset of the KB → lower accuracy
    # So we skip the LLM pass and tag graphify as "completed_by_factory_onboarding".
    # The deterministic edges from step 1+2 above are still persisted —
    # they're what powers graph-first retrieval at SRS/Codegen time.
    enrichment_stats = {"added_nodes": 0, "added_edges": 0, "skipped": True}
    factory_already_onboarded = False
    try:
        from db import projects as _projects_col
        _proj_doc = await _projects_col.find_one(
            {"id": project_id},
            {"_id": 0, "settings.factory_orchestrator": 1},
        ) or {}
        _fo = ((_proj_doc.get("settings") or {}).get("factory_orchestrator") or {})
        factory_already_onboarded = bool(_fo.get("enabled")) and bool(_fo.get("onboarded_at"))
    except Exception:  # noqa: BLE001
        factory_already_onboarded = False

    if factory_already_onboarded:
        logger.info(
            "graphify SKIPPED for %s — Factory.ai already onboarded at %s. "
            "Trusting the droid's P0 KB build; not re-prompting.",
            project_id,
            _fo.get("onboarded_at"),
        )
        enrichment_stats = {
            "added_nodes": 0, "added_edges": 0,
            "skipped": True,
            "reason": "completed_by_factory_onboarding",
            "onboarded_at": _fo.get("onboarded_at"),
        }
        if phase_callback is not None:
            try:
                await phase_callback("graphify", {
                    "skipped": True,
                    "reason": "completed_by_factory_onboarding",
                    "onboarded_at": _fo.get("onboarded_at"),
                })
            except Exception as cb_e:  # pragma: no cover
                logger.warning("graphify phase_callback (skip) failed: %s", cb_e)
    elif await _graphify_enabled(project_id, graphify_enabled):
        # iter-13.40 — preflight: if NO LLM is reachable (no active Console
        # provider AND no Factory orchestrator AND no env-var OpenRouter key),
        # skip the LLM enrichment cleanly with an informative reason instead
        # of letting fabric_call raise. The deterministic graph still
        # persists and downstream SRS/Arch/CodeGen can run on it.
        _llm_route = await _detect_available_llm_route(project_id)
        if _llm_route == "none":
            logger.info(
                "graphify SKIPPED for %s — Graph KB enabled but no LLM "
                "provider configured (Console + Factory + env-var all empty). "
                "Persisting deterministic graph only; configure a provider "
                "in Console → Models to enable enrichment.", project_id,
            )
            enrichment_stats = {
                "added_nodes": 0, "added_edges": 0,
                "skipped": True,
                "reason": "no_llm_route_available",
                "hint": "Add a provider in Console → Models (Anthropic, OpenAI, OpenRouter, Groq, or Ollama).",
            }
            if phase_callback is not None:
                try:
                    await phase_callback("graphify", {
                        "skipped": True,
                        "reason": "no_llm_route_available",
                    })
                except Exception:
                    pass
        else:
            # iter-13.35 — flip the build phase to "graphify" BEFORE the LLM
            # call so the UI doesn't show "graph_build" for the full 30–180 s
            # the Anthropic / Ollama / Factory call takes. The deterministic
            # build above is fast (seconds); wall-time here is the LLM hop.
            if phase_callback is not None:
                try:
                    await phase_callback("graphify", {
                        "deterministic_nodes": len(nodes),
                        "deterministic_edges": len(edges),
                        "route": _llm_route,
                    })
                except Exception as cb_e:  # pragma: no cover — observability only
                    logger.warning("graphify phase_callback failed: %s", cb_e)
            try:
                enrichment_stats = await _graphify_with_llm(
                    project_id=project_id,
                    nodes=nodes,
                    edges=edges,
                )
            except Exception as e:
                logger.warning("graphify LLM enrichment failed (continuing): %s", e)
                enrichment_stats = {"added_nodes": 0, "added_edges": 0,
                                    "skipped": True, "error": str(e)}

    # ---- 4) Persist (sharded — iter-13.74) ---------------------------------
    # MongoDB hard-caps a single BSON document at 16 MB. On real-world
    # legacy projects (e.g. EJHS: 4456 TABLEs → 60k+ Column nodes + edges)
    # the monolithic single-doc layout blew past that ceiling
    # (DocumentTooLarge: 17.66 MB) and `routes/kb.py` silently swallowed
    # the exception, leaving the project with no kb_graph at all and the
    # SRS panel rightly complaining "Graphify KB ON but no graph built".
    #
    # New on-disk layout in `kb_graph` (same collection, no breaking API):
    #   • meta shard :  {project_id, kind:"meta",  version, updated_at, stats}
    #   • node shards:  {project_id, kind:"nodes", shard_idx:N, nodes:[...]}
    #   • edge shards:  {project_id, kind:"edges", shard_idx:N, edges:[...]}
    # Each shard is bounded by SHARD_BYTE_BUDGET so the persisted BSON
    # for any single doc stays comfortably under the 16 MB ceiling.
    # `load_kb_graph()` reassembles transparently; every existing call
    # site (graph_retriever, routes/architecture, routes/kb's chunk
    # annotator) keeps working with the same {nodes:[...], edges:[...]}
    # shape it always saw.
    node_list = list(nodes.values())
    by_type: dict[str, int] = defaultdict(int)
    for n in node_list:
        by_type[n["type"]] += 1

    # Bump version BEFORE wiping previous shards so concurrent readers
    # still see the old (consistent) version if they raced us.
    existing_meta = await kb_graph_col.find_one(
        {"project_id": project_id, "kind": "meta"},
        {"version": 1},
    )
    # Legacy fallback: an old monolithic doc has no `kind` field.
    if not existing_meta:
        existing_meta = await kb_graph_col.find_one(
            {"project_id": project_id, "kind": {"$exists": False}},
            {"version": 1},
        )
    next_version = int((existing_meta or {}).get("version", 0)) + 1
    now_iso = datetime.now(timezone.utc).isoformat()
    stats = {
        "nodes_count": len(node_list),
        "edges_count": len(edges),
        "by_type": dict(by_type),
        "graphify": enrichment_stats,
    }

    # Wipe ANY prior shards (legacy monolithic OR sharded) before writing.
    if phase_callback is not None:
        try:
            await phase_callback("graph_build", {
                "step": "persist",
                "entities_total": total_entities,
                "entities_processed": processed,
                "nodes_built": len(node_list),
                "edges_built": len(edges),
            })
        except Exception:  # pragma: no cover
            pass
    await kb_graph_col.delete_many({"project_id": project_id})

    # ~8 MB per shard keeps each BSON doc well under 16 MB even after
    # bson encoding overhead, _id, and the surrounding shard envelope.
    SHARD_BYTE_BUDGET = 8 * 1024 * 1024
    import json as _json_for_size

    def _shard_array(items: list[dict], key: str) -> list[list[dict]]:
        shards: list[list[dict]] = []
        current: list[dict] = []
        current_bytes = 0
        for it in items:
            try:
                sz = len(_json_for_size.dumps(it, default=str))
            except Exception:  # noqa: BLE001
                sz = 512  # conservative fallback
            # Force a flush when this single item would exceed the
            # budget OR when the current shard would overflow.
            if current and (current_bytes + sz) > SHARD_BYTE_BUDGET:
                shards.append(current)
                current = []
                current_bytes = 0
            current.append(it)
            current_bytes += sz
        if current:
            shards.append(current)
        if not shards:
            shards.append([])  # always at least one empty shard so reads find it
        return shards

    node_shards = _shard_array(node_list, "nodes")
    edge_shards = _shard_array(edges, "edges")

    docs_to_insert: list[dict] = [{
        "project_id": project_id,
        "kind": "meta",
        "version": next_version,
        "updated_at": now_iso,
        "stats": stats,
        "shard_counts": {
            "nodes": len(node_shards),
            "edges": len(edge_shards),
        },
    }]
    for idx, shard in enumerate(node_shards):
        docs_to_insert.append({
            "project_id": project_id,
            "kind": "nodes",
            "shard_idx": idx,
            "version": next_version,
            "nodes": shard,
        })
    for idx, shard in enumerate(edge_shards):
        docs_to_insert.append({
            "project_id": project_id,
            "kind": "edges",
            "shard_idx": idx,
            "version": next_version,
            "edges": shard,
        })

    await kb_graph_col.insert_many(docs_to_insert)

    logger.info(
        "kb_graph built for %s: %d nodes / %d edges (%s) — persisted "
        "across %d node-shards + %d edge-shards (version %d)",
        project_id, len(node_list), len(edges), dict(by_type),
        len(node_shards), len(edge_shards), next_version,
    )
    return stats | {"version": next_version, "updated_at": now_iso}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

async def load_kb_graph(project_id: str) -> dict | None:
    """Load the persisted graph; returns None if missing.

    iter-13.74 — reassembles the sharded {meta, nodes×N, edges×N}
    layout written by build_kb_graph. Falls back to the legacy
    monolithic single-doc shape so projects built before this change
    keep loading without a rebuild.

    Return shape (stable, callers depend on it):
        {project_id, version, updated_at, nodes:[...], edges:[...], stats}
    """
    from db import kb_graph as kb_graph_col
    # Try sharded layout first.
    meta = await kb_graph_col.find_one(
        {"project_id": project_id, "kind": "meta"}, {"_id": 0}
    )
    if meta:
        nodes: list[dict] = []
        edges: list[dict] = []
        cur = kb_graph_col.find(
            {"project_id": project_id, "kind": "nodes", "version": meta.get("version")},
            {"_id": 0, "shard_idx": 1, "nodes": 1},
        ).sort("shard_idx", 1)
        async for d in cur:
            nodes.extend(d.get("nodes") or [])
        cur = kb_graph_col.find(
            {"project_id": project_id, "kind": "edges", "version": meta.get("version")},
            {"_id": 0, "shard_idx": 1, "edges": 1},
        ).sort("shard_idx", 1)
        async for d in cur:
            edges.extend(d.get("edges") or [])
        return {
            "project_id": project_id,
            "version": meta.get("version"),
            "updated_at": meta.get("updated_at"),
            "nodes": nodes,
            "edges": edges,
            "stats": meta.get("stats") or {},
        }
    # Legacy monolithic fallback (single doc, no `kind` field).
    legacy = await kb_graph_col.find_one(
        {"project_id": project_id, "kind": {"$exists": False}}, {"_id": 0}
    )
    return legacy


# ---------------------------------------------------------------------------
# LLM enrichment ("graphify") — iter-13.32
# ---------------------------------------------------------------------------

_VALID_NODE_TYPES = {
    "Module", "Class", "Method", "Table", "Column", "Route", "Role",
    "BusinessEntity",
}
_VALID_EDGE_TYPES = {
    "HAS_METHOD", "HAS_COLUMN", "CALLS", "READS", "WRITES", "EXPOSES",
    "GUARDED_BY", "BELONGS_TO_MODULE", "BELONGS_TO_ENTITY",
    "REFERENCES_TABLE",
}


def _summarise_for_llm(nodes: dict[str, dict], edges: list[dict],
                       max_per_type: int = 60) -> dict:
    """Produce a compact, name-only summary of the deterministic graph.
    Keeps the prompt small enough for high-tier Anthropic without truncation.
    """
    by_type: dict[str, list[str]] = defaultdict(list)
    for n in nodes.values():
        by_type[n["type"]].append(n["name"])
    summary = {t: sorted(names)[:max_per_type] for t, names in by_type.items()}
    edge_counts: dict[str, int] = defaultdict(int)
    for e in edges:
        edge_counts[e["type"]] += 1
    return {
        "nodes_by_type": summary,
        "deterministic_edge_counts": dict(edge_counts),
    }


_GRAPHIFY_SYS_PROMPT = (
    "You are a senior software archaeologist. You receive a deterministic "
    "property-graph summary extracted from a legacy codebase (classes, "
    "methods, tables, columns, routes, roles, business entities). Your job "
    "is to enrich it with high-confidence INFERRED relationships and "
    "business-domain entities that the static extractor missed.\n\n"
    "Return STRICT JSON ONLY (no prose, no markdown fences) with this shape:\n"
    "{\n"
    "  \"edges\": [ {\"src_type\": \"Class\", \"src_name\": \"UserCtrl\", "
    "\"dst_type\": \"Table\", \"dst_name\": \"users\", \"type\": \"READS\", "
    "\"why\": \"<one-line evidence>\"} ],\n"
    "  \"business_entities\": [ {\"name\": \"Customer\", "
    "\"description\": \"...\", \"members\": [\"UserCtrl\", \"users\"]} ]\n"
    "}\n\n"
    "Rules:\n"
    "- Use ONLY node names that appear in the input summary, or create a "
    "  BusinessEntity (new node type allowed only for business_entities).\n"
    "- Allowed edge types: HAS_METHOD, HAS_COLUMN, CALLS, READS, WRITES, "
    "  EXPOSES, GUARDED_BY, BELONGS_TO_MODULE, BELONGS_TO_ENTITY, "
    "  REFERENCES_TABLE.\n"
    "- Skip anything you are not at least 80% confident about.\n"
    "- Hard cap: at most 80 edges and 12 business_entities.\n"
)


async def _graphify_with_llm(
    project_id: str,
    nodes: dict[str, dict],
    edges: list[dict],
) -> dict:
    """Run the LLM enrichment pass and merge results back into nodes/edges
    in place. Returns ``{added_nodes, added_edges, skipped, model}``.

    iter-13.36 — when Factory.ai is enabled for this project, we PREFER
    the modernization-agent YAML prompt (prompts/factory-modernization-agent.yml)
    as the SYSTEM message. The droid then opens legacy files directly from
    its CWD for higher-fidelity enrichment, instead of reasoning purely
    over the small summary blob. Falls back to the hardcoded
    `_GRAPHIFY_SYS_PROMPT` for non-Factory provider routes (Anthropic/
    OpenRouter), which can't read the filesystem.
    """
    # Skip if graph is essentially empty — no signal for the LLM.
    if len(nodes) < 5:
        return {"added_nodes": 0, "added_edges": 0, "skipped": True,
                "reason": "graph too small"}

    summary = _summarise_for_llm(nodes, edges)

    # iter-13.36 — pick the SYSTEM prompt based on whether the project's
    # active LLM route is Factory.ai. Factory droids can `cat`/`grep` the
    # legacy code; standalone LLMs cannot.
    use_factory_prompt = False
    try:
        from db import projects as _projects_col
        _proj = await _projects_col.find_one(
            {"id": project_id}, {"_id": 0, "settings.factory_orchestrator": 1},
        ) or {}
        _fo = (_proj.get("settings") or {}).get("factory_orchestrator") or {}
        use_factory_prompt = bool(_fo.get("enabled")) and bool((_fo.get("app_key") or "").strip())
    except Exception:  # noqa: BLE001
        use_factory_prompt = False

    sys_prompt = _GRAPHIFY_SYS_PROMPT
    if use_factory_prompt:
        try:
            from pathlib import Path as _Path
            _yml = (_Path(__file__).resolve().parents[2] / "prompts"
                    / "factory-modernization-agent.yml")
            if _yml.exists():
                yml_text = _yml.read_text(encoding="utf-8")
                sys_prompt = (
                    "You are operating under the LAMA modernization-agent contract "
                    "(YAML below). For THIS request you are executing the P0 (KB build) "
                    "+ P3 (business-rule extraction) sub-task — specifically enriching "
                    "the property graph with HIGH-CONFIDENCE inferred edges and "
                    "business entities the deterministic extractor missed.\n\n"
                    "Use your filesystem access to OPEN and READ the legacy files "
                    "named in the summary below (via `cat`/`grep`) when in doubt; "
                    "evidence > guessing. Cite files as `relative/path.ext:LINE`.\n\n"
                    "═══════════════════════════════════════════════════════════════\n"
                    "MODERNIZATION-AGENT CONTRACT (factory-modernization-agent.yml):\n"
                    "═══════════════════════════════════════════════════════════════\n"
                    f"{yml_text}\n"
                    "═══════════════════════════════════════════════════════════════\n\n"
                    "OUTPUT FORMAT — return STRICT JSON ONLY (no prose, no markdown\n"
                    "fences) with this exact shape:\n"
                    "{\n"
                    "  \"edges\": [ {\"src_type\": \"Class\", \"src_name\": \"UserCtrl\", "
                    "\"dst_type\": \"Table\", \"dst_name\": \"users\", \"type\": \"READS\", "
                    "\"why\": \"<file:line citation>\"} ],\n"
                    "  \"business_entities\": [ {\"name\": \"Customer\", "
                    "\"description\": \"...\", \"members\": [\"UserCtrl\", \"users\"]} ]\n"
                    "}\n\n"
                    "Hard constraints:\n"
                    "- ONLY use node names from the input summary, OR create a NEW\n"
                    "  BusinessEntity (only new node type permitted).\n"
                    "- Allowed edge types: HAS_METHOD, HAS_COLUMN, CALLS, READS,\n"
                    "  WRITES, EXPOSES, GUARDED_BY, BELONGS_TO_MODULE,\n"
                    "  BELONGS_TO_ENTITY, REFERENCES_TABLE.\n"
                    "- Skip anything below 80% confidence.\n"
                    "- Hard cap: 80 edges, 12 business_entities.\n"
                )
            else:
                logger.warning(
                    "factory-modernization-agent.yml not found at %s — "
                    "falling back to hardcoded graphify prompt", _yml,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load factory-modernization-agent.yml: %s", e)

    user_payload = (
        "Project ID: " + project_id + "\n\n"
        "DETERMINISTIC GRAPH SUMMARY (JSON):\n"
        + json.dumps(summary, indent=2)
        + "\n\nReturn the enrichment JSON now."
    )

    # Import here to avoid a hard dependency during seed / tests that don't
    # touch the graph.
    from llm import fabric_call

    try:
        result = await fabric_call(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_payload},
            ],
            agent_key="kb.graphify",
            project_id=project_id,
            temperature=0.2,
            max_tokens=4096,
            timeout=180.0,
        )
    except Exception as e:
        logger.warning("graphify fabric_call failed: %s", e)
        return {"added_nodes": 0, "added_edges": 0, "skipped": True,
                "error": str(e)[:200]}

    raw = (result.get("content") or "").strip()
    if not raw:
        return {"added_nodes": 0, "added_edges": 0, "skipped": True,
                "reason": "empty content"}

    # Tolerate ```json fences.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

    try:
        payload = json.loads(raw)
    except Exception:
        # Try to salvage the first {...} block.
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            return {"added_nodes": 0, "added_edges": 0, "skipped": True,
                    "reason": "non-JSON response"}
        try:
            payload = json.loads(m.group(0))
        except Exception as e:
            return {"added_nodes": 0, "added_edges": 0, "skipped": True,
                    "reason": f"JSON parse error: {e}"}

    added_nodes = 0
    added_edges = 0
    model_used = result.get("model") or ""

    # ---- Merge business entities (new nodes only) ----
    for be in (payload.get("business_entities") or [])[:12]:
        name = (be.get("name") or "").strip()
        if not name:
            continue
        ek = _node_key("BusinessEntity", name)
        is_new = ek not in nodes
        nk = _ensure_node(
            nodes, "BusinessEntity", name,
            description=be.get("description"),
            provenance="llm",
        )
        if is_new:
            added_nodes += 1
        # Link members → entity (only if the member already exists in either
        # Class or Table form). Do not invent member nodes.
        for member in (be.get("members") or [])[:25]:
            member = (member or "").strip()
            if not member:
                continue
            for candidate_type in ("Class", "Table"):
                ck = _node_key(candidate_type, member)
                if ck in nodes:
                    _add_edge(edges, nodes[ck]["id"], nk,
                              "BELONGS_TO_ENTITY", provenance="llm")
                    added_edges += 1
                    break

    # ---- Merge inferred edges (only between existing nodes) ----
    for e in (payload.get("edges") or [])[:80]:
        etype = (e.get("type") or "").upper()
        if etype not in _VALID_EDGE_TYPES:
            continue
        src_type = e.get("src_type") or ""
        dst_type = e.get("dst_type") or ""
        if src_type not in _VALID_NODE_TYPES or dst_type not in _VALID_NODE_TYPES:
            continue
        src_name = (e.get("src_name") or "").strip()
        dst_name = (e.get("dst_name") or "").strip()
        if not src_name or not dst_name:
            continue
        sk = _node_key(src_type, src_name)
        dk = _node_key(dst_type, dst_name)
        if sk not in nodes or dk not in nodes:
            # Refuse to hallucinate new endpoint nodes (BusinessEntity already
            # handled above).
            continue
        _add_edge(edges, nodes[sk]["id"], nodes[dk]["id"], etype,
                  provenance="llm", why=(e.get("why") or "")[:200])
        added_edges += 1

    logger.info(
        "graphify enriched project %s: +%d nodes / +%d edges via %s",
        project_id, added_nodes, added_edges, model_used or "(unknown model)",
    )
    return {
        "added_nodes": added_nodes,
        "added_edges": added_edges,
        "skipped": False,
        "model": model_used,
    }


