"""Graph retriever (iter-13.19).

Pulls a relevant subgraph around a set of seed terms and serializes it as
compact YAML for LLM prompts. Used by SRS / Architecture / CodeGen when
``LAMA_USE_GRAPH_KB=1``.

Token cost is roughly **3-5x lower** than the equivalent RAG chunk block at
the same recall, because the YAML carries only entities + relations, not
raw source code lines.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

from kb.kb_graph import load_kb_graph

logger = logging.getLogger("lama.graph_retriever")


# --------------------------------------------------------------------------
# Seed-term → node match
# --------------------------------------------------------------------------

def _match_nodes(nodes: list[dict], seed_terms: list[str]) -> list[str]:
    """Return node IDs whose name (case-insensitive) contains any seed term."""
    if not seed_terms:
        return []
    seeds_lc = [t.lower() for t in seed_terms if t and len(t) >= 3]
    if not seeds_lc:
        return []
    matched: list[str] = []
    for n in nodes:
        name_lc = (n.get("name") or "").lower()
        if not name_lc:
            continue
        for s in seeds_lc:
            if s in name_lc:
                matched.append(n["id"])
                break
    return matched


def _build_adjacency(edges: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """node_id → [(neighbor_id, edge_type), …] — both directions."""
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        s, d, t = e.get("src"), e.get("dst"), e.get("type")
        if not s or not d:
            continue
        adj[s].append((d, t))
        adj[d].append((s, f"~{t}"))  # reverse direction marker
    return adj


# --------------------------------------------------------------------------
# Subgraph
# --------------------------------------------------------------------------

async def subgraph_for(
    project_id: str,
    seed_terms: list[str],
    hops: int = 2,
    max_nodes: int = 120,
) -> dict[str, Any]:
    """BFS subgraph around seed-matched nodes.

    Returns ``{nodes: [...], edges: [...], seed_ids: [...], truncated: bool}``.
    Empty dict if no graph or no matches.
    """
    graph = await load_kb_graph(project_id)
    if not graph:
        return {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not nodes:
        return {}

    nodes_by_id = {n["id"]: n for n in nodes}
    seed_ids = _match_nodes(nodes, seed_terms)
    if not seed_ids:
        # Fall back to "give me the most-connected N nodes" so the LLM still
        # gets a useful skeleton on a vague query.
        adj_count: dict[str, int] = defaultdict(int)
        for e in edges:
            adj_count[e["src"]] += 1
            adj_count[e["dst"]] += 1
        seed_ids = [nid for nid, _ in sorted(adj_count.items(),
                                             key=lambda kv: -kv[1])[:10]]
        if not seed_ids:
            return {}

    adj = _build_adjacency(edges)
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seed_ids)
    truncated = False

    while queue and len(visited) < max_nodes:
        nid, depth = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        if depth >= hops:
            continue
        for neighbor_id, _etype in adj.get(nid, []):
            if neighbor_id not in visited and len(visited) < max_nodes:
                queue.append((neighbor_id, depth + 1))

    if len(visited) >= max_nodes and queue:
        truncated = True

    sub_nodes = [nodes_by_id[i] for i in visited if i in nodes_by_id]
    sub_edges = [
        e for e in edges
        if e["src"] in visited and e["dst"] in visited
    ]
    return {
        "nodes": sub_nodes,
        "edges": sub_edges,
        "seed_ids": seed_ids,
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# Serialize
# --------------------------------------------------------------------------

def serialize_yaml(subgraph: dict, max_chars: int = 6000) -> str:
    """Compact YAML — grouped by node type, with relations inlined per node.

    Format::

        # GRAPH SUBGRAPH (v=N, nodes=A, edges=B)
        Tables:
          - users:
              columns: [id, email, role]
              referenced_by: [LoginController, UserService]
          - orders:
              ...
        Classes:
          - LoginController:
              module: src/controllers/LoginController.java
              exposes: [POST /login, GET /logout]
              calls: [UserService.findByEmail]
              reads: [users]
        ...
    """
    if not subgraph or not subgraph.get("nodes"):
        return ""
    nodes = subgraph["nodes"]
    edges = subgraph.get("edges", [])
    nodes_by_id = {n["id"]: n for n in nodes}

    # Group edges by source node
    out_edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        out_edges[e["src"]].append((e["dst"], e["type"]))

    # Group nodes by type
    by_type: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_type[n["type"]].append(n)

    # Deterministic type ordering — most important first.
    type_order = ["BusinessEntity", "Module", "Class", "Method", "Table",
                  "Column", "Route", "Role"]
    ordered_types = [t for t in type_order if t in by_type] + [
        t for t in by_type if t not in type_order
    ]

    lines: list[str] = [
        f"# GRAPH SUBGRAPH (nodes={len(nodes)}, edges={len(edges)}"
        + (", truncated=true" if subgraph.get("truncated") else "")
        + ")"
    ]
    total = len(lines[0])
    for t in ordered_types:
        items = sorted(by_type[t], key=lambda n: n["name"])
        if not items:
            continue
        header = f"{t}s:"
        lines.append(header)
        total += len(header) + 1
        for n in items:
            entry = [f"  - {n['name']}:"]
            # Inline a few high-signal scalar props.
            for prop in ("path", "verb", "source", "namespace",
                         "data_type", "description"):
                if n.get(prop):
                    val = str(n[prop])[:120]
                    entry.append(f"      {prop}: {val}")
            # Outgoing relations
            rels: dict[str, list[str]] = defaultdict(list)
            for dst_id, etype in out_edges.get(n["id"], []):
                dst_name = nodes_by_id.get(dst_id, {}).get("name", "?")
                rels[etype.lower()].append(dst_name)
            for etype, targets in rels.items():
                if not targets:
                    continue
                trimmed = sorted(set(targets))[:12]
                entry.append(f"      {etype}: [{', '.join(trimmed)}]")
            for line in entry:
                lines.append(line)
                total += len(line) + 1
                if total >= max_chars:
                    lines.append("  # ...[truncated for context budget]")
                    return "\n".join(lines)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Convenience — section-aware retrieval used by SRS / Arch / CodeGen
# --------------------------------------------------------------------------

# Section → seed-term tokens (mirrors SECTION_QUERIES in routes/srs.py).
SECTION_SEEDS: dict[str, list[str]] = {
    "introduction": ["controller", "module", "service"],
    "overall_description": ["controller", "model", "service", "framework"],
    "actors_use_case_inventory": ["role", "user", "controller", "permission"],
    "specific_requirements": ["controller", "validate", "approve", "submit"],
    "detailed_use_cases": ["controller", "form", "submit", "approve", "upload"],
    "external_interfaces": ["route", "api", "controller", "request"],
    "non_functional_requirements": ["audit", "log", "security", "session"],
    "integration_requirements": ["payment", "sms", "email", "service", "client"],
    "validation_verification": ["validate", "error", "exception"],
    "traceability_matrix": ["controller", "module", "table"],
    "appendices": ["table", "column"],
    "entity_model": ["table", "column"],
}


async def subgraph_yaml_for_section(
    project_id: str,
    section_key: str,
    extra_seeds: list[str] | None = None,
    max_chars: int = 6000,
    hops: int = 2,
    max_nodes: int = 120,
) -> str:
    """One-shot helper: section key → compact YAML subgraph string."""
    seeds = list(SECTION_SEEDS.get(section_key, []))
    if extra_seeds:
        seeds.extend(extra_seeds)
    sg = await subgraph_for(project_id, seeds, hops=hops, max_nodes=max_nodes)
    if not sg:
        return ""
    return serialize_yaml(sg, max_chars=max_chars)


async def subgraph_yaml_for_terms(
    project_id: str,
    seed_terms: list[str],
    max_chars: int = 6000,
    hops: int = 2,
    max_nodes: int = 120,
) -> str:
    """Free-form helper used by Architecture / CodeGen (service / file scoped)."""
    sg = await subgraph_for(project_id, seed_terms, hops=hops, max_nodes=max_nodes)
    if not sg:
        return ""
    return serialize_yaml(sg, max_chars=max_chars)

