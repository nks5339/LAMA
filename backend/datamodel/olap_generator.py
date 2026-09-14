"""Deterministic OLAP star-schema DDL generator (iter-13.44).

Input  : parsed bus_matrix JSON (`{facts, dimensions, matrix}`) +
         OLTP DDL string (for measure-type inference) OR the same `kb_entities`
         list the OLTP generator received.
Output : PostgreSQL star-schema DDL — dim_*, fact_* tables, dim_date,
         partitions, indexes, materialised views, refresh procedure.

Star is a MECHANICAL transformation of the bus matrix — there's no judgement
left to make once the matrix is in. Hence no LLM needed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _sanitise(name: str, prefix: str = "") -> str:
    n = (name or "").strip().lower()
    n = re.sub(r"[^a-z0-9_]", "_", n)
    n = re.sub(r"_+", "_", n).strip("_")
    if prefix and not n.startswith(prefix):
        n = prefix + n
    return n[:63] or f"{prefix}_unnamed"


def _measure_pg_type(raw: str) -> str:
    """Map bus-matrix measure types → Postgres types with sensible precision."""
    r = (raw or "").strip().upper()
    if r in ("INT", "INTEGER", "COUNT"):
        return "INTEGER"
    if r in ("BIGINT", "LONG"):
        return "BIGINT"
    if r in ("FLOAT", "DOUBLE"):
        return "DOUBLE PRECISION"
    if r in ("BOOLEAN", "BOOL"):
        return "BOOLEAN"
    # default — money / quantity
    return "NUMERIC(18,4)"


def _attr_pg_type(raw: str) -> str:
    r = (raw or "").strip().upper()
    if r in ("INT", "INTEGER"):
        return "INTEGER"
    if r in ("BIGINT",):
        return "BIGINT"
    if r in ("DATE",):
        return "DATE"
    if r in ("TIMESTAMP", "DATETIME", "TIMESTAMPTZ"):
        return "TIMESTAMPTZ"
    if r in ("BOOLEAN", "BOOL"):
        return "BOOLEAN"
    if r in ("DECIMAL", "NUMERIC", "MONEY"):
        return "NUMERIC(18,4)"
    if r.startswith("VARCHAR"):
        return raw  # preserve length
    if r in ("TEXT", "CHAR", "JSON", "JSONB", "UUID"):
        return r if r != "JSON" else "JSONB"
    return "TEXT"


# ----------------------------------------------------------------------------
# dim_date — fully fixed template
# ----------------------------------------------------------------------------
_DIM_DATE_DDL = '''-- ===== DIMENSIONS =====
CREATE TABLE IF NOT EXISTS "dim_date" (
    "date_key"      INTEGER NOT NULL PRIMARY KEY,
    "full_date"     DATE NOT NULL UNIQUE,
    "day_of_week"   VARCHAR(10) NOT NULL,
    "day_of_month"  SMALLINT NOT NULL,
    "week_number"   SMALLINT NOT NULL,
    "month_number"  SMALLINT NOT NULL,
    "month_name"    VARCHAR(10) NOT NULL,
    "quarter"       SMALLINT NOT NULL,
    "year"          INTEGER NOT NULL,
    "is_weekend"    BOOLEAN NOT NULL DEFAULT FALSE,
    "is_holiday"    BOOLEAN NOT NULL DEFAULT FALSE,
    "fiscal_year"   INTEGER NOT NULL,
    "fiscal_quarter" SMALLINT NOT NULL
);
COMMENT ON TABLE "dim_date" IS 'Mandatory date dimension. Grain: one row per calendar day. Populate via load_dim_date() on initial deploy.';
CREATE INDEX IF NOT EXISTS "ix_dim_date_year_month" ON "dim_date"("year","month_number");
'''


def _emit_dimension(dim: dict) -> str:
    """Emit a single SCD-2 dimension table."""
    name = _sanitise(dim.get("name") or "", prefix="dim_")
    attrs: List[dict] = dim.get("attributes") or []
    lines: List[str] = []
    lines.append(f'CREATE TABLE IF NOT EXISTS "{name}" (')
    lines.append(f'    "{name}_key" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,')
    lines.append(f'    "natural_key" VARCHAR(128) NOT NULL,')
    for a in attrs:
        an = _sanitise(a.get("name") or "")
        if not an or an in ("natural_key",):
            continue
        at = _attr_pg_type(a.get("type") or "")
        lines.append(f'    "{an}" {at},')
    # SCD-2 trailer
    lines.append('    "valid_from" DATE NOT NULL DEFAULT CURRENT_DATE,')
    lines.append('    "valid_to"   DATE NULL,')
    lines.append('    "is_current" BOOLEAN NOT NULL DEFAULT TRUE,')
    lines.append('    "loaded_at"  TIMESTAMPTZ NOT NULL DEFAULT NOW()')
    lines.append(');')
    src = ", ".join(dim.get("source_tables") or []) or "(unspecified)"
    lines.append(
        f'COMMENT ON TABLE "{name}" IS '
        f"'SCD Type-2 dimension. Grain: one row per natural key per change-period. "
        f"Sourced from: {src}.';"
    )
    lines.append(
        f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{name}_natural_current" '
        f'ON "{name}"("natural_key") WHERE is_current = TRUE;'
    )
    return "\n".join(lines)


def _emit_fact(fact: dict, dim_keys_for_fact: List[str]) -> Tuple[str, List[str]]:
    """Emit fact table partitioned by RANGE(date_key) with 3 yearly partitions.

    Returns (parent_ddl, [partition_ddls + index_ddls])."""
    name = _sanitise(fact.get("name") or "", prefix="fact_")
    measures: List[dict] = fact.get("measures") or []
    lines: List[str] = []
    lines.append(f'CREATE TABLE IF NOT EXISTS "{name}" (')
    lines.append(f'    "{name}_id"  BIGINT GENERATED ALWAYS AS IDENTITY,')
    lines.append('    "date_key"  INTEGER NOT NULL REFERENCES "dim_date"("date_key"),')
    for dk in dim_keys_for_fact:
        if dk == "dim_date":
            continue
        dn = _sanitise(dk, prefix="dim_")
        lines.append(f'    "{dn}_key" INTEGER NOT NULL REFERENCES "{dn}"("{dn}_key"),')
    for m in measures:
        mn = _sanitise(m.get("name") or "")
        if not mn:
            continue
        mt = _measure_pg_type(m.get("type") or "")
        lines.append(f'    "{mn}" {mt} NOT NULL DEFAULT 0,')
    lines.append('    "loaded_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),')
    lines.append(f'    PRIMARY KEY ("{name}_id", "date_key")')
    lines.append(') PARTITION BY RANGE ("date_key");')
    grain = fact.get("grain") or "(unspecified)"
    src = ", ".join(fact.get("source_tables") or []) or "(unspecified)"
    parent_comment = (
        f'COMMENT ON TABLE "{name}" IS '
        f"'Fact table. Grain: {grain}. Sourced from: {src}.';"
    )

    # Three yearly partitions: last / current / next year, anchored on
    # the current year stamp at generation time (deterministic per-day).
    from datetime import date
    cur_year = date.today().year
    partitions: List[str] = []
    for y in (cur_year - 1, cur_year, cur_year + 1):
        partitions.append(
            f'CREATE TABLE IF NOT EXISTS "{name}_y{y}" PARTITION OF "{name}" '
            f'FOR VALUES FROM ({y * 10000 + 101}) TO ({(y + 1) * 10000 + 101});'
        )

    # Indexes — composites on (date_key, <dim>_key) + BRIN on loaded_at
    idx: List[str] = []
    for dk in dim_keys_for_fact:
        if dk == "dim_date":
            continue
        dn = _sanitise(dk, prefix="dim_")
        idx.append(
            f'CREATE INDEX IF NOT EXISTS "ix_{name}_{dn}_date" '
            f'ON "{name}"("{dn}_key","date_key");'
        )
    idx.append(
        f'CREATE INDEX IF NOT EXISTS "brin_{name}_loaded_at" '
        f'ON "{name}" USING BRIN("loaded_at");'
    )

    return "\n".join(lines) + "\n" + parent_comment, partitions + idx


def _emit_materialised_views(facts: List[dict]) -> List[str]:
    """Generate up to 5 templated covering MVs — one per fact's primary measure."""
    out: List[str] = []
    for fact in facts[:5]:
        fname = _sanitise(fact.get("name") or "", prefix="fact_")
        if not fact.get("measures"):
            continue
        measure = _sanitise((fact["measures"][0] or {}).get("name") or "amount")
        mv = _sanitise(f"mv_{fname}_by_month_{measure}")
        out.append(
            f'CREATE MATERIALIZED VIEW IF NOT EXISTS "{mv}" AS\n'
            f'SELECT d."year" AS "year", d."month_number" AS "month_number",\n'
            f'       SUM(f."{measure}") AS "total_{measure}",\n'
            f'       COUNT(*) AS "row_count"\n'
            f'FROM "{fname}" f\n'
            f'JOIN "dim_date" d ON d."date_key" = f."date_key"\n'
            f'GROUP BY d."year", d."month_number"\n'
            f'WITH NO DATA;\n'
            f'COMMENT ON MATERIALIZED VIEW "{mv}" IS '
            f"'BI: Monthly {measure} aggregated from {fname}.';\n"
            f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{mv}" '
            f'ON "{mv}"("year","month_number");'
        )
    return out


_REFRESH_PROCEDURE_TEMPLATE = '''-- ===== REFRESH PROCEDURE =====
CREATE OR REPLACE PROCEDURE "refresh_olap_all"() AS $$
DECLARE
    mv_name TEXT;
BEGIN
    FOR mv_name IN
        SELECT matviewname FROM pg_matviews WHERE schemaname = current_schema()
    LOOP
        EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I', mv_name);
    END LOOP;
END;
$$ LANGUAGE plpgsql;
'''


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def generate_olap_ddl(
    bus_matrix: Dict[str, Any],
    project_name: str = "",
) -> str:
    """Build OLAP star-schema DDL from a parsed bus matrix dict.

    `bus_matrix` shape (from the LLM bus-matrix step):
        {"facts": [...], "dimensions": [...], "matrix": {fact_name: {dim_name: bool}}}
    """
    facts: List[dict]      = bus_matrix.get("facts") or []
    dims:  List[dict]      = bus_matrix.get("dimensions") or []
    matrix: Dict[str, Any] = bus_matrix.get("matrix") or {}

    out: List[str] = []
    out.append("-- LAMA Generated OLAP Star Schema (deterministic generator, iter-13.44)")
    out.append(f"-- Project: {project_name or '(unknown)'}")
    out.append(f"-- Facts: {len(facts)} · Dimensions: {len(dims)}")
    out.append("")

    # Dimensions (dim_date first, fixed)
    out.append(_DIM_DATE_DDL)
    for d in sorted(dims, key=lambda x: (x.get("name") or "").lower()):
        if _sanitise(d.get("name") or "", prefix="dim_") == "dim_date":
            continue
        out.append(_emit_dimension(d))
        out.append("")

    # Facts + partitions + indexes
    out.append("-- ===== FACTS =====")
    deferred: List[str] = []
    for f in sorted(facts, key=lambda x: (x.get("name") or "").lower()):
        fname = f.get("name") or ""
        # Resolve list of dim_keys this fact uses (from matrix row)
        dim_keys: List[str] = []
        row = matrix.get(fname) or {}
        for dk, used in row.items():
            if used:
                dim_keys.append(dk)
        if not dim_keys:
            # Fall back: assume all dims if matrix row missing.
            dim_keys = [d.get("name") or "" for d in dims]
        parent, post = _emit_fact(f, dim_keys)
        out.append(parent)
        deferred.extend(post)
        out.append("")
    if deferred:
        out.append("-- ===== PARTITIONS & INDEXES =====")
        out.extend(deferred)
        out.append("")

    # Materialised views
    mvs = _emit_materialised_views(facts)
    if mvs:
        out.append("-- ===== MATERIALIZED VIEWS =====")
        for mv in mvs:
            out.append(mv)
            out.append("")

    # Refresh procedure (always)
    out.append(_REFRESH_PROCEDURE_TEMPLATE)
    return "\n".join(out) + "\n"


def stats(ddl: str) -> Dict[str, int]:
    upper = ddl.upper()
    return {
        "dims":       upper.count("CREATE TABLE IF NOT EXISTS \"DIM_")
                   + upper.count("CREATE TABLE \"DIM_"),
        "facts":      upper.count("CREATE TABLE IF NOT EXISTS \"FACT_")
                   + upper.count("CREATE TABLE \"FACT_"),
        "partitions": upper.count("PARTITION OF"),
        "mvs":        upper.count("CREATE MATERIALIZED VIEW"),
    }

