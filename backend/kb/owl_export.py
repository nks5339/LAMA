"""KB context export — YAML view of the legacy knowledge base.

The legacy JSON-LD/OWL bundle was renamed/repurposed in iter-13: downstream
LLM prompts and the Stage-2/3 service-map use this payload as **context only**
— they never reason over OWL semantics. YAML is a friendlier, less-noisy
serialisation (no `@type` / `@id` / `lama:` prefixes for the LLM to ignore)
and exactly what the user asked for: *"do not expect owl script. take it as
yaml script. and use yml as extractor."*

This module now exposes:
  * `export_kb_context(project, entities, srs_sections)` → flat dict (clean keys).
  * `export_kb_yaml(...)`                                 → YAML string for download.
  * `export_owl(...)`  (kept for backwards-compat: returns the same flat dict
                       so existing callers in `routes/srs.py` keep working).

Heuristic only — deterministic given the same input entities.
"""
from typing import List, Dict, Any
from datetime import datetime, timezone
from collections import defaultdict

try:
    import yaml  # PyYAML — already in requirements.txt
except ImportError:  # pragma: no cover — install-time safety net
    yaml = None  # type: ignore


def export_kb_context(
    project: Dict,
    entities: List[Dict],
    srs_sections: Dict[str, str] = None,
) -> Dict[str, Any]:
    """Flat, LLM-friendly KB context (no JSON-LD prefixes).

    The returned dict is the **single source of truth** for both the YAML
    download AND any in-process consumer that needs a deterministic
    structured view of the KB (e.g. `routes/srs.py` builds StageContext from
    `data_model_hints` + `microservice_hints`).
    """
    classes  = [e for e in entities if e.get("type") == "CLASS"]
    tables   = [e for e in entities if e.get("type") == "TABLE"]
    routes   = [e for e in entities if e.get("type") == "ROUTE"]
    indivs   = [e for e in entities if e.get("type") == "INDIVIDUAL"]

    # FK in-degree for risk scoring
    fk_in_degree: Dict[str, int] = {}
    for t in tables:
        for fk in (t.get("fks") or []):
            ref = fk.get("ref_table", "")
            fk_in_degree[ref] = fk_in_degree.get(ref, 0) + 1

    # Table classification
    audit_tables    = [t["name"] for t in tables
                       if t["name"].startswith("logs_")
                       or "audit" in t["name"].lower()]
    lookup_tables   = [t["name"] for t in tables
                       if len(t.get("columns") or []) <= 4
                       and len(t.get("fks") or []) == 0]
    junction_tables = [t["name"] for t in tables
                       if len(t.get("fks") or []) >= 2
                       and len(t.get("columns") or []) <= 5]
    high_risk_tables = sorted(
        [(t["name"],
          fk_in_degree.get(t["name"], 0) * 3
          + len(t.get("columns") or []) // 10
          + len(t.get("fks") or []) * 2)
         for t in tables],
        key=lambda x: x[1], reverse=True
    )[:20]

    # Domain grouping
    domain_map: Dict[str, Dict] = defaultdict(
        lambda: {"tables": [], "classes": []})
    for t in tables:
        parts  = t["name"].split("_")
        domain = parts[0] if len(parts) > 1 else "core"
        domain_map[domain]["tables"].append(t["name"])
    for c in classes:
        touched = set()
        for m in (c.get("methods") or []):
            for tbl in (m.get("tables") or []):
                parts = tbl.split("_")
                if len(parts) > 1:
                    touched.add(parts[0])
        for domain in (touched or {"core"}):
            domain_map[domain]["classes"].append(c["name"])

    # Suggested microservice boundaries
    suggested_boundaries = []
    for domain, content in domain_map.items():
        if domain in ("logs", "") or len(content["tables"]) < 2:
            continue
        suggested_boundaries.append({
            "service_name": f"{domain}-service",
            "tables":  content["tables"][:20],
            "classes": list(set(content["classes"]))[:10],
            "reason": (
                f"Domain prefix '{domain}' groups "
                f"{len(content['tables'])} tables and "
                f"{len(content['classes'])} controller classes "
                "with shared data access patterns."
            )
        })

    payload: Dict[str, Any] = {
        "kind": "lama.kb.context",
        "version": 1,
        "format": "yaml",
        "migration_context": {
            "project":      project.get("name", ""),
            "source_tech":  project.get("source_tech", ""),
            "target_tech":  project.get("target_tech", ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "classes":    len(classes),
                "tables":     len(tables),
                "routes":     len(routes),
                "roles":      len(indivs),
                "total_fks":  sum(len(t.get("fks") or []) for t in tables),
            },
            "srs_summary": (
                (srs_sections or {}).get("introduction", "")
                or (srs_sections or {}).get("purpose", "")
            )[:500],
        },
        "graph": {
            "classes": [
                {
                    "name":           c["name"],
                    "namespace":      c.get("namespace", ""),
                    "extends":        c.get("extends", ""),
                    "methods": [
                        {
                            "name":            m.get("name", ""),
                            "tables_accessed": m.get("tables", []),
                            "session_vars":    m.get("sessions", []),
                        }
                        for m in (c.get("methods") or [])
                    ],
                    "session_fields": c.get("session_fields", []),
                    "source_file":    c.get("source", ""),
                }
                for c in classes
            ],
            "tables": [
                {
                    "name":        t["name"],
                    "primary_key": t.get("pk", ""),
                    "columns": [
                        {
                            "name":   c.get("name", ""),
                            "type":   c.get("type", ""),
                            "is_pk":  c.get("name") == t.get("pk", ""),
                            "is_fk":  any(fk.get("column") == c.get("name")
                                          for fk in (t.get("fks") or [])),
                        }
                        for c in (t.get("columns") or [])
                    ],
                    "foreign_keys": [
                        {
                            "column":     fk.get("column", ""),
                            "references": fk.get("ref_table", ""),
                        }
                        for fk in (t.get("fks") or [])
                    ],
                    "risk_score": (
                        fk_in_degree.get(t["name"], 0) * 3
                        + len(t.get("columns") or []) // 10
                        + len(t.get("fks") or []) * 2
                    ),
                    "is_audit":    t["name"] in audit_tables,
                    "is_lookup":   t["name"] in lookup_tables,
                    "is_junction": t["name"] in junction_tables,
                }
                for t in tables
            ],
            "routes": [
                {
                    "id":      f"route_{i}",
                    "verb":    r.get("verb", ""),
                    "path":    r.get("name", ""),
                    "handler": r.get("handler", ""),
                    "source":  r.get("source", ""),
                }
                for i, r in enumerate(routes)
            ],
            "roles": [
                {
                    "name":     ind.get("name", ""),
                    "category": ind.get("category", ""),
                }
                for ind in indivs
            ],
        },
        "data_model_hints": {
            "high_risk_tables":  [{"table": n, "score": s}
                                  for n, s in high_risk_tables],
            "audit_tables":      audit_tables,
            "lookup_tables":     lookup_tables[:30],
            "junction_tables":   junction_tables,
            "domains":           dict(domain_map),
        },
        "microservice_hints": {
            "suggested_boundaries":     suggested_boundaries,
            "total_suggested_services": len(suggested_boundaries),
            "rationale": (
                "Boundaries derived from table prefix grouping "
                "and class-to-table access patterns in the KB."
            ),
        },
    }
    return payload


def export_kb_yaml(
    project: Dict,
    entities: List[Dict],
    srs_sections: Dict[str, str] = None,
) -> str:
    """Render `export_kb_context(...)` as a YAML document.

    Falls back to a minimal YAML-ish dump if PyYAML is somehow unavailable
    at runtime — never raises (the LLM consumers just need *some* text).
    """
    payload = export_kb_context(project, entities, srs_sections)
    header = (
        "# LAMA — Knowledge Base context export (YAML)\n"
        "# Generated deterministically from the project KB entities.\n"
        "# Treat this file as the EVIDENCE-bound view of the legacy stack.\n"
        f"# project: {payload['migration_context'].get('project', '')}\n"
        f"# generated_at: {payload['migration_context'].get('generated_at', '')}\n"
        "---\n"
    )
    if yaml is None:  # pragma: no cover
        # Minimal best-effort fallback: pretty-print via repr; YAML-loadable
        # for top-level scalars/lists/dicts of primitives.
        import json as _json
        return header + _json.dumps(payload, indent=2, default=str)
    body = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
    )
    return header + body


# ── Backwards-compat shim ─────────────────────────────────────────────────
# `routes/srs.py` still imports `export_owl(...)` to assemble its
# `StageContext` payload (it consumes the `data_model_hints` /
# `microservice_hints` keys, not the @context/@graph JSON-LD scaffolding).
# Keep it as a thin alias so we don't have to update every call-site.
def export_owl(
    project: Dict,
    entities: List[Dict],
    srs_sections: Dict[str, str] = None,
) -> Dict[str, Any]:
    """Deprecated name — kept for in-process callers. Returns the same flat
    dict as `export_kb_context()`. New downstream code should call
    `export_kb_context` or `export_kb_yaml` directly.
    """
    return export_kb_context(project, entities, srs_sections)
