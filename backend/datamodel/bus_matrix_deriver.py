"""Heuristic bus-matrix derivation (iter-13.45).

Used when deterministic OLAP / migration is requested but no LLM-generated
Bus Matrix exists for the project. Derives a sensible facts × dimensions
plan straight from the kb_entities table list:

    • Dimensions = tables that are FK-referenced from other tables AND look
      like lookup / master tables (have name/code/title/label columns).
    • Facts      = tables with one or more numeric "measure-like" columns
      (amount, qty, price, total, count, value, fee, cost, …) AND a date
      column (created_at, txn_date, *_at, *_date, *_on).
    • Matrix     = each fact uses dim_date plus every dim referenced via
      its own FK columns.

The derived matrix is conservative — wrong-grain dimensions are excluded
rather than guessed. It's marked `derived_from="kb_entities"` in the
returned dict so the caller can audit it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_MEASURE_COL_RE = re.compile(
    r"(amount|amt|qty|quantity|price|cost|fee|total|sub_?total|count|"
    r"value|balance|debit|credit|tax|discount|score|weight)",
    re.IGNORECASE,
)
_DATE_COL_RE   = re.compile(r"(_at|_on|_date|_time|_dt|date|datetime|timestamp)$",
                            re.IGNORECASE)
_LOOKUP_COL_RE = re.compile(
    r"^(name|title|label|code|description|short_name|long_name|display_name)$",
    re.IGNORECASE,
)
_NUMERIC_TYPE_RE = re.compile(
    r"(INT|BIGINT|SMALLINT|TINYINT|DECIMAL|NUMERIC|MONEY|FLOAT|DOUBLE|REAL)",
    re.IGNORECASE,
)


def _is_measure_column(col: dict) -> bool:
    name = (col.get("name") or "").lower()
    ctype = (col.get("type") or "").upper()
    if name in ("id",) or name.endswith("_id"):
        return False  # FK / PK, not a measure
    if not _NUMERIC_TYPE_RE.search(ctype):
        return False
    return bool(_MEASURE_COL_RE.search(name))


def _is_date_column(col: dict) -> bool:
    name = (col.get("name") or "").lower()
    return bool(_DATE_COL_RE.search(name))


def _is_lookup_column(col: dict) -> bool:
    return bool(_LOOKUP_COL_RE.match((col.get("name") or "").lower()))


def _table_index(entities: List[dict]) -> Dict[str, dict]:
    return {(e.get("name") or "").lower(): e
            for e in entities if (e.get("type") or "").upper() == "TABLE"}


def _fk_in_degree(entities: List[dict]) -> Dict[str, int]:
    """Count how many distinct tables FK-reference each table."""
    in_degree: Dict[str, set] = {}
    for ent in entities:
        if (ent.get("type") or "").upper() != "TABLE":
            continue
        src = (ent.get("name") or "").lower()
        for fk in (ent.get("fks") or []):
            ref = (fk.get("ref_table") or "").lower()
            if ref:
                in_degree.setdefault(ref, set()).add(src)
    return {k: len(v) for k, v in in_degree.items()}


def _classify_dimension(ent: dict, in_degree: int) -> bool:
    """A table is a dimension candidate if it's FK-referenced and has
    lookup-style columns (name/code/etc.) AND lacks measure columns."""
    if in_degree < 1:
        return False
    cols = ent.get("columns") or []
    has_lookup  = any(_is_lookup_column(c) for c in cols)
    has_measure = any(_is_measure_column(c) for c in cols)
    return has_lookup and not has_measure


def _classify_fact(ent: dict) -> bool:
    cols = ent.get("columns") or []
    has_measure = any(_is_measure_column(c) for c in cols)
    has_date    = any(_is_date_column(c) for c in cols)
    return has_measure and has_date


def _fact_dim_keys(ent: dict, dim_table_names: set) -> List[str]:
    """For a fact entity, return the list of dim_<x> keys it should join to
    (based on FK columns that reference a known dimension table)."""
    out: List[str] = []
    for fk in (ent.get("fks") or []):
        ref = (fk.get("ref_table") or "").lower()
        if ref in dim_table_names:
            out.append(f"dim_{ref}")
    # Also detect implicit *_id FKs that name a dim_ table even without
    # explicit fks entry — common in legacy MyISAM dumps.
    for col in (ent.get("columns") or []):
        cn = (col.get("name") or "").lower()
        if cn.endswith("_id") and cn != "id":
            root = cn[:-3]
            if root in dim_table_names and f"dim_{root}" not in out:
                out.append(f"dim_{root}")
    return out


def _fact_date_column(ent: dict) -> str:
    """Pick the most likely fact date column. Priority:
       transaction_date / order_date / *_date > created_at > any _at / _on."""
    cols = ent.get("columns") or []
    by_name = {(c.get("name") or "").lower(): c.get("name") for c in cols}
    for preferred in ("transaction_date", "order_date", "invoice_date",
                      "payment_date", "txn_date", "event_date"):
        if preferred in by_name:
            return by_name[preferred]
    # any *_date
    for cn, raw in by_name.items():
        if cn.endswith("_date"):
            return raw
    if "created_at" in by_name:
        return by_name["created_at"]
    # any _at / _on / _time
    for cn, raw in by_name.items():
        if _DATE_COL_RE.search(cn):
            return raw
    return "created_at"


def derive_bus_matrix(entities: List[dict]) -> Dict[str, Any]:
    """Return a bus_matrix dict in the same shape the LLM step produces.

    Always emits dim_date as the first dimension. Dimensions and facts are
    sorted alphabetically for reproducibility.
    """
    tables = _table_index(entities)
    in_degree = _fk_in_degree(entities)

    # ----- Dimensions -----
    dim_entries: List[dict] = []
    dim_table_names: set = set()
    for tname in sorted(tables.keys()):
        ent = tables[tname]
        if _classify_dimension(ent, in_degree.get(tname, 0)):
            attrs = [
                {"name": (c.get("name") or "").lower(),
                 "source_column": c.get("name"),
                 "type": c.get("type") or "VARCHAR(255)"}
                for c in (ent.get("columns") or [])
                if (c.get("name") or "").lower() not in ("id",)
                and not (c.get("name") or "").lower().endswith("_id")
                and (c.get("name") or "").lower() not in
                    ("created_at", "updated_at", "deleted_at",
                     "created_by", "updated_by")
            ][:12]  # cap attribute count
            # Pick a natural key — prefer code/slug/email, fall back to id.
            natural_key = "id"
            for cand in ("code", "slug", "email", "name"):
                for c in (ent.get("columns") or []):
                    if (c.get("name") or "").lower() == cand:
                        natural_key = c.get("name")
                        break
                if natural_key != "id":
                    break
            dim_entries.append({
                "name": f"dim_{tname}",
                "source_tables": [ent.get("name")],
                "natural_key": natural_key,
                "attributes": attrs,
            })
            dim_table_names.add(tname)

    # Always include dim_date (no source — populated by load_dim_date()).
    if not any(d["name"] == "dim_date" for d in dim_entries):
        dim_entries.insert(0, {
            "name": "dim_date",
            "source_tables": [],
            "natural_key": "full_date",
            "attributes": [],
        })

    # ----- Facts -----
    fact_entries: List[dict] = []
    matrix: Dict[str, Dict[str, bool]] = {}
    for tname in sorted(tables.keys()):
        ent = tables[tname]
        if not _classify_fact(ent) or tname in dim_table_names:
            continue
        measures = [
            {"name": (c.get("name") or "").lower(),
             "source_column": c.get("name"),
             "type": c.get("type") or "NUMERIC(18,4)",
             "agg": "SUM"}
            for c in (ent.get("columns") or []) if _is_measure_column(c)
        ][:8]
        date_col = _fact_date_column(ent)
        dim_keys = _fact_dim_keys(ent, dim_table_names)
        fact_name = f"fact_{tname}"
        fact_entries.append({
            "name": fact_name,
            "grain": f"one row per {tname} record",
            "source_tables": [ent.get("name")],
            "date_column": date_col,
            "measures": measures,
        })
        row = {"dim_date": True}
        for dk in dim_keys:
            row[dk] = True
        matrix[fact_name] = row

    # If we found dimensions but ZERO facts, promote any non-dim table
    # that has at least one date column to a count-fact so OLAP DDL still
    # has something to generate. Iterates ALL tables (not just FK-referenced
    # ones) — bug-fix from the iter-13.45 smoke test.
    if dim_entries and not fact_entries:
        # Sort by descending in-degree, then alphabetical, but include
        # tables with zero in-degree too.
        all_sorted = sorted(
            tables.keys(),
            key=lambda t: (-in_degree.get(t, 0), t),
        )
        for tname in all_sorted:
            if tname in dim_table_names:
                continue
            ent = tables[tname]
            if not any(_is_date_column(c) for c in (ent.get("columns") or [])):
                continue
            date_col = _fact_date_column(ent)
            fact_name = f"fact_{tname}"
            fact_entries.append({
                "name": fact_name,
                "grain": f"one row per {tname} record",
                "source_tables": [ent.get("name")],
                "date_column": date_col,
                "measures": [
                    {"name": "row_count", "source_column": "id",
                     "type": "INTEGER", "agg": "COUNT"}
                ],
            })
            row = {"dim_date": True}
            # Add any dim joins this table has via FKs
            for dk in _fact_dim_keys(ent, dim_table_names):
                row[dk] = True
            matrix[fact_name] = row
            break

    return {
        "facts": fact_entries,
        "dimensions": dim_entries,
        "matrix": matrix,
        "derived_from": "kb_entities",
        "derivation_note": (
            "Auto-derived heuristic bus matrix — no LLM-generated Bus Matrix "
            "was present. Run /api/data-model/generate/bus-matrix for a "
            "judgement-driven version."
        ),
        "stats": {
            "facts": len(fact_entries),
            "dimensions": len(dim_entries),
            "tables_considered": len(tables),
        },
    }

