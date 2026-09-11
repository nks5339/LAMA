"""Business-domain ontology builder.

Two-pass strategy:
 1. Deterministic clustering — group tables by name-prefix and classes by namespace
    into candidate business entities, infer FK relationships between clusters.
 2. LLM enrichment — feed the clusters to the LLM and ask it to rename / merge /
    split nodes into business terms, propose verb-based relationships, identify
    lifecycle states, business owners, etc.

The result is cached in the `business_ontologies` collection, keyed by a content
hash of the KB so we only re-run the LLM when entities actually change.
"""
from __future__ import annotations
import hashlib
import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List

logger = logging.getLogger("lama.kb.business_ontology")


# ---------------------------------------------------------------------------
# 1. Deterministic clustering
# ---------------------------------------------------------------------------
def _table_prefix(name: str) -> str:
    """Pick a sensible domain prefix from a table name."""
    if not name:
        return "core"
    parts = re.split(r"[_\-]", name.lower())
    if len(parts) == 1:
        return parts[0] or "core"
    # Common 2-3 letter prefixes often indicate domain (e.g. pms_, hrm_, acl_)
    head = parts[0]
    if len(head) <= 4 and len(parts) >= 2:
        return head
    return head


def _class_domain(cls: dict) -> str:
    ns = (cls.get("namespace") or "").strip()
    name = (cls.get("name") or "").strip()
    if ns:
        ns_parts = re.split(r"[\\./]", ns)
        # Strip leading "App" / "Application" etc.
        ns_parts = [p for p in ns_parts if p and p.lower() not in {"app", "application", "src", "main", "java"}]
        if ns_parts:
            return ns_parts[-1].lower()
    # Fallback: word break the class name (camelCase) and pick the first non-suffix token.
    tokens = re.findall(r"[A-Z][a-z0-9]*", name)
    tokens = [t for t in tokens if t.lower() not in {"controller", "service", "repository", "dao", "model", "entity", "manager", "helper", "util", "utils"}]
    if tokens:
        return tokens[0].lower()
    return "core"


def cluster_entities(kb_entities_rows: List[dict]) -> Dict[str, Any]:
    """Group raw KB entities into deterministic domain clusters."""
    tables = [e for e in kb_entities_rows if e.get("type") == "TABLE"]
    classes = [e for e in kb_entities_rows if e.get("type") == "CLASS"]
    roles = [e for e in kb_entities_rows if e.get("type") == "ROLE"]
    routes = [e for e in kb_entities_rows if e.get("type") == "ROUTE"]

    table_by_name = {t["name"]: t for t in tables}
    table_to_domain: Dict[str, str] = {}
    class_to_domain: Dict[str, str] = {}

    clusters: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "tables": [],
        "classes": [],
        "roles": set(),
        "routes": [],
        "column_count": 0,
        "fk_count": 0,
        "method_count": 0,
    })

    # Group tables by prefix
    for t in tables:
        dom = _table_prefix(t["name"])
        table_to_domain[t["name"]] = dom
        clusters[dom]["tables"].append(t["name"])
        clusters[dom]["column_count"] += len(t.get("columns") or [])
        clusters[dom]["fk_count"] += len(t.get("fks") or [])

    # Group classes by namespace tail / camel-case head
    for c in classes:
        dom = _class_domain(c)
        # If a class "looks like" it manages a known table prefix, prefer that.
        for table_dom in table_to_domain.values():
            if table_dom and table_dom in c.get("name", "").lower():
                dom = table_dom
                break
        class_to_domain[c["name"]] = dom
        clusters[dom]["classes"].append(c["name"])
        clusters[dom]["method_count"] += len(c.get("methods") or [])
        # Tables touched by this class's methods
        for m in (c.get("methods") or []):
            for tname in (m.get("tables") or []):
                if tname in table_by_name and table_to_domain.get(tname) and table_to_domain[tname] != dom:
                    # Cross-domain method — surfaced as a relationship later
                    pass

    # Roles + routes attach to a guessed domain
    for r in roles:
        clusters[_table_prefix(r.get("name", "core"))]["roles"].add(r.get("name", ""))
    for rt in routes:
        clusters[_table_prefix(rt.get("name", "core").split("/")[1] if "/" in rt.get("name", "") else "core")]["routes"].append(rt.get("name", ""))

    # FK relationships between clusters
    fk_edges: List[Dict[str, str]] = []
    seen_edges = set()
    for t in tables:
        src_dom = table_to_domain.get(t["name"])
        if not src_dom:
            continue
        for fk in (t.get("fks") or []):
            ref = fk.get("ref_table") or ""
            dst_dom = table_to_domain.get(ref)
            if not dst_dom or src_dom == dst_dom:
                continue
            key = (src_dom, dst_dom, fk.get("column", ""))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            fk_edges.append({
                "source": src_dom,
                "target": dst_dom,
                "via_table": t["name"],
                "via_column": fk.get("column", ""),
                "ref_table": ref,
                "kind": "fk",
            })

    # Tidy each cluster
    clusters_out = []
    for dom, c in clusters.items():
        if not c["tables"] and not c["classes"]:
            continue
        clusters_out.append({
            "id": dom,
            "name": dom,
            "tables": sorted(set(c["tables"]))[:60],
            "classes": sorted(set(c["classes"]))[:60],
            "roles": sorted(c["roles"])[:20],
            "routes": c["routes"][:30],
            "column_count": c["column_count"],
            "fk_count": c["fk_count"],
            "method_count": c["method_count"],
        })
    clusters_out.sort(key=lambda x: x["column_count"] + len(x["classes"]) * 5, reverse=True)
    return {
        "clusters": clusters_out,
        "fk_edges": fk_edges,
        "stats": {
            "total_clusters": len(clusters_out),
            "total_tables": len(tables),
            "total_classes": len(classes),
        },
    }


# ---------------------------------------------------------------------------
# 2. Cache key
# ---------------------------------------------------------------------------
def compute_kb_hash(kb_entities_rows: List[dict]) -> str:
    """Stable hash of KB content so we can invalidate cache when KB changes."""
    sig_parts = []
    for e in sorted(kb_entities_rows, key=lambda x: (x.get("type", ""), x.get("name", ""))):
        typ = e.get("type", "")
        nm = e.get("name", "")
        if typ == "TABLE":
            cols = ",".join(c.get("name", "") for c in (e.get("columns") or []))
            fks = ",".join(fk.get("ref_table", "") for fk in (e.get("fks") or []))
            sig_parts.append(f"T:{nm}|{cols}|{fks}")
        elif typ == "CLASS":
            ms = ",".join(m.get("name", "") for m in (e.get("methods") or []))
            sig_parts.append(f"C:{nm}|{ms}")
        else:
            sig_parts.append(f"{typ}:{nm}")
    blob = "\n".join(sig_parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 3. LLM enrichment
# ---------------------------------------------------------------------------
ENRICHMENT_SYSTEM_PROMPT = """You are a senior business analyst converting a technical
codebase ontology (PHP/Java/.NET classes + SQL tables) into a BUSINESS DOMAIN model
suitable for non-technical stakeholders.

For the given clusters (deterministic groupings of tables + classes by prefix /
namespace) and foreign-key edges, return a clean JSON document describing the
underlying BUSINESS entities and relationships. The reader should NOT need to
know what a table or class is — they should see real-world concepts like
"Citizen", "Loan Application", "Sanction Letter", "Doctor", "Invoice".

RULES:
- Merge clusters that obviously represent the same business concept.
- Split a cluster only if it clearly contains two distinct business concepts.
- Use proper, capitalised business nouns. Never use table prefixes or class
  suffixes (no "_tbl", no "Controller", no "DAO").
- For every entity, infer:
    * description           (1–2 sentences in plain business English)
    * domain                (a higher-level domain bucket, e.g. "Identity & Access",
                              "Finance", "Operations", "Compliance", "Reporting")
    * backed_by_tables      (subset of the SQL tables in the source cluster)
    * implemented_in_classes (subset of the classes in the source cluster)
    * business_owner        (the role most likely to own this entity, inferred
                             from roles / route names / class names — NEVER invent
                             generic placeholders. Use "Unknown" if truly unclear.)
    * lifecycle_states      (list of status / state values you can infer from the
                             code — e.g. ["draft", "submitted", "approved", "rejected"].
                             Empty list if none visible.)
- Propose BUSINESS RELATIONSHIPS using verbs (not "fk"):
    e.g. {"source": "User", "target": "Project", "verb": "owns", "description": "..."}
  Cover BOTH:
    a) every FK edge supplied (translate it to a verb)
    b) any obvious business relationships implied by class names / routes / roles,
       even when no FK exists (e.g. "Doctor *prescribes* Medication" inferred from
       a `PrescriptionController` that touches both tables).
- Keep the total to roughly 15–40 entities — merge aggressively if needed.

Return ONLY valid JSON in this exact shape (no preamble, no markdown fences):
{
  "entities": [
    {
      "id": "user",
      "name": "User",
      "domain": "Identity & Access",
      "description": "...",
      "backed_by_tables": ["users", "user_roles"],
      "implemented_in_classes": ["UserController", "AuthService"],
      "business_owner": "System Administrator",
      "lifecycle_states": ["active", "suspended"]
    }
  ],
  "relationships": [
    {
      "source": "user",
      "target": "project",
      "verb": "owns",
      "description": "A user can own multiple projects.",
      "kind": "business"
    }
  ]
}
The `id` field MUST be a slug (lowercase, ascii, dashes / underscores) and MUST be
unique. Relationship `source` and `target` MUST reference existing entity ids."""


def _build_enrichment_payload(clusters_pack: Dict[str, Any], project: dict) -> str:
    """Stringify the deterministic clusters for the LLM."""
    lines = [
        f"PROJECT: {project.get('name', '')}",
        f"SOURCE STACK: {project.get('source_tech', '')}",
        f"TARGET STACK: {project.get('target_tech', '')}",
        "",
        f"## DETERMINISTIC CLUSTERS ({clusters_pack['stats']['total_clusters']} clusters from "
        f"{clusters_pack['stats']['total_tables']} tables and "
        f"{clusters_pack['stats']['total_classes']} classes)",
        "",
    ]
    for c in clusters_pack["clusters"][:50]:
        lines.append(f"### cluster:{c['id']}")
        lines.append(f"- tables ({len(c['tables'])}): {', '.join(c['tables'][:25])}")
        if c["classes"]:
            lines.append(f"- classes ({len(c['classes'])}): {', '.join(c['classes'][:25])}")
        if c["roles"]:
            lines.append(f"- roles: {', '.join(c['roles'])}")
        if c["routes"]:
            lines.append(f"- sample routes: {', '.join(c['routes'][:8])}")
        lines.append("")

    if clusters_pack["fk_edges"]:
        lines.append("## CROSS-CLUSTER FOREIGN KEYS")
        for e in clusters_pack["fk_edges"][:120]:
            lines.append(
                f"- {e['source']} → {e['target']}  (via {e['via_table']}.{e['via_column']} → {e['ref_table']})"
            )
    return "\n".join(lines)


def _safe_json_parse(text: str) -> Dict[str, Any] | None:
    """Tolerate ```json … ``` fences and stray preambles."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.lstrip("`").lstrip()
        if t.lower().startswith("json"):
            t = t[4:].lstrip()
        if t.endswith("```"):
            t = t[:-3].rstrip()
    # Find first { … last }
    first = t.find("{")
    last = t.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        return json.loads(t[first:last + 1])
    except Exception as e:
        logger.warning("business ontology JSON parse failed: %s", e)
        return None


async def enrich_with_llm(
    clusters_pack: Dict[str, Any],
    project: dict,
    project_id: str,
) -> Dict[str, Any] | None:
    """Run the LLM enrichment pass. Returns None on failure (caller will fall back
    to the deterministic clusters)."""
    from llm import fabric_call

    payload = _build_enrichment_payload(clusters_pack, project)
    messages = [
        {"role": "system", "content": ENRICHMENT_SYSTEM_PROMPT},
        {"role": "user", "content": payload + "\n\nReturn the JSON now."},
    ]
    try:
        r = await fabric_call(
            messages=messages,
            agent_key="srs.generate",
            project_id=project_id,
            model="",  # iter-13.30: empty → Console.routing[high] picks for srs.generate
            max_tokens=12000,
            temperature=0.2,
            timeout=180.0,
        )
    except Exception as e:
        logger.warning("business ontology LLM failed: %s", e)
        return None

    parsed = _safe_json_parse(r.get("content", ""))
    if not parsed or not isinstance(parsed.get("entities"), list):
        return None
    return parsed


# ---------------------------------------------------------------------------
# 4. Compose the final shape (graph-ready + detail-ready)
# ---------------------------------------------------------------------------
def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "entity"


def compose_business_ontology(
    clusters_pack: Dict[str, Any],
    llm_result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Build the final graph payload the frontend consumes.

    If `llm_result` is None we still return a usable graph derived purely from
    the deterministic clusters so the UI never lands on a blank page.
    """
    fk_edges = clusters_pack.get("fk_edges", [])

    # ---- LLM path ----
    if llm_result and llm_result.get("entities"):
        ents = []
        used_ids: set = set()
        for e in llm_result["entities"]:
            eid = _slugify(e.get("id") or e.get("name") or "")
            if not eid or eid in used_ids:
                eid = f"{eid}_{len(used_ids)}"
            used_ids.add(eid)
            ents.append({
                "id": eid,
                "name": e.get("name", "Unnamed entity"),
                "domain": e.get("domain", "General"),
                "description": e.get("description", ""),
                "backed_by_tables": list(e.get("backed_by_tables") or [])[:40],
                "implemented_in_classes": list(e.get("implemented_in_classes") or [])[:40],
                "business_owner": e.get("business_owner", "Unknown"),
                "lifecycle_states": list(e.get("lifecycle_states") or [])[:20],
                "synthetic": False,
            })

        edges = []
        ent_ids = {e["id"] for e in ents}
        for rel in (llm_result.get("relationships") or []):
            s = _slugify(rel.get("source") or "")
            t = _slugify(rel.get("target") or "")
            if s in ent_ids and t in ent_ids:
                edges.append({
                    "source": s,
                    "target": t,
                    "verb": rel.get("verb", "relates to"),
                    "description": rel.get("description", ""),
                    "kind": rel.get("kind", "business"),
                })

        # Also surface any FK edges between *tables* that aren't already covered
        # by the LLM's business relationships — translate them to entity-level.
        table_to_entity: Dict[str, str] = {}
        for e in ents:
            for tbl in e["backed_by_tables"]:
                table_to_entity.setdefault(tbl, e["id"])

        existing_pairs = {(e["source"], e["target"]) for e in edges} | {(e["target"], e["source"]) for e in edges}
        for fke in fk_edges:
            via_tbl = fke.get("via_table")
            ref_tbl = fke.get("ref_table")
            s_ent = table_to_entity.get(via_tbl)
            t_ent = table_to_entity.get(ref_tbl)
            if not (s_ent and t_ent) or s_ent == t_ent:
                continue
            if (s_ent, t_ent) in existing_pairs:
                continue
            existing_pairs.add((s_ent, t_ent))
            edges.append({
                "source": s_ent,
                "target": t_ent,
                "verb": "references",
                "description": f"FK via {via_tbl}.{fke.get('via_column','')} → {ref_tbl}",
                "kind": "fk",
            })

        domains = sorted({e["domain"] for e in ents if e.get("domain")})
        return {
            "entities": ents,
            "relationships": edges,
            "domains": domains,
            "source": "llm",
            "stats": {
                "total_entities": len(ents),
                "total_relationships": len(edges),
                "total_domains": len(domains),
            },
        }

    # ---- Deterministic fallback ----
    ents = []
    for c in clusters_pack.get("clusters", []):
        ents.append({
            "id": c["id"],
            "name": c["id"].replace("_", " ").title(),
            "domain": "General",
            "description": (
                f"Auto-clustered from {len(c['tables'])} table(s) and "
                f"{len(c['classes'])} class(es) sharing prefix '{c['id']}'."
            ),
            "backed_by_tables": c["tables"],
            "implemented_in_classes": c["classes"],
            "business_owner": next(iter(c["roles"]), "Unknown"),
            "lifecycle_states": [],
            "synthetic": True,
        })
    cluster_ids = {c["id"] for c in clusters_pack.get("clusters", [])}
    edges = []
    for fke in fk_edges:
        if fke["source"] in cluster_ids and fke["target"] in cluster_ids:
            edges.append({
                "source": fke["source"],
                "target": fke["target"],
                "verb": "references",
                "description": f"FK via {fke['via_table']}.{fke['via_column']} → {fke['ref_table']}",
                "kind": "fk",
            })
    return {
        "entities": ents,
        "relationships": edges,
        "domains": ["General"],
        "source": "deterministic",
        "stats": {
            "total_entities": len(ents),
            "total_relationships": len(edges),
            "total_domains": 1,
        },
    }
