"""Deterministic OLTP DDL generator (iter-13.44).

Input  : list of `kb_entities` table docs (type=TABLE), optional `domain_map`,
         optional `data_model_hints`, optional FR↔table map.
Output : PostgreSQL DDL string matching the contract previously enforced by
         the `datamodel.oltp` LLM prompt.

Determinism guarantees:
    • Same input → byte-identical output (stable sort, no `dict` iteration order).
    • No I/O. No LLM. No external network.
    • Returns ONLY valid PostgreSQL — no markdown fences, no prose preamble.

What the LLM was doing that this replaces:
    1. Mapping legacy SQL types → Postgres types  → done via TYPE_MAP below.
    2. Adding audit columns                        → done via _AUDIT_COLS template.
    3. Emitting FK constraints + indexes           → done via kb_entities.fks.
    4. Grouping tables by module                   → done via domain_map.
    5. Emitting INDEX on every FK + status/date    → done via _COLUMN_NAME_RULES.
    6. Soft-delete partial UNIQUE                  → done if natural-key columns
                                                     detected on the entity.

What this generator deliberately does NOT do (handled by optional polish calls):
    • Inventing ENUM TYPEs from free-text status columns (LLM polish).
    • Writing `COMMENT ON COLUMN` prose for cryptic column names (LLM polish).
    • Inferring CHECK constraints from business rules (out of scope; LLM polish
      may be added later).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ----------------------------------------------------------------------------
# Legacy → PostgreSQL type mapping.
# Order matters: more-specific patterns first.
# Each entry: (regex on UPPERCASED legacy type, replacement, comment_suffix).
# ----------------------------------------------------------------------------
_TYPE_MAP: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^TINYINT\s*\(\s*1\s*\)$"),                  "BOOLEAN"),
    (re.compile(r"^BIT\s*\(\s*1\s*\)$"),                      "BOOLEAN"),
    (re.compile(r"^BIT$"),                                    "BOOLEAN"),
    (re.compile(r"^BOOL(EAN)?$"),                             "BOOLEAN"),
    (re.compile(r"^TINYINT.*$"),                              "SMALLINT"),
    (re.compile(r"^SMALLINT.*$"),                             "SMALLINT"),
    (re.compile(r"^MEDIUMINT.*$"),                            "INTEGER"),
    (re.compile(r"^INT(EGER)?(\s*\(.*\))?$"),                 "INTEGER"),
    (re.compile(r"^BIGINT.*$"),                               "BIGINT"),
    (re.compile(r"^SERIAL$"),                                 "INTEGER"),
    (re.compile(r"^BIGSERIAL$"),                              "BIGINT"),
    (re.compile(r"^FLOAT.*$"),                                "REAL"),
    (re.compile(r"^DOUBLE.*$"),                               "DOUBLE PRECISION"),
    (re.compile(r"^REAL$"),                                   "REAL"),
    # money/decimal — preserve precision when given
    (re.compile(r"^(DECIMAL|NUMERIC|MONEY|DEC)\s*\((\d+)\s*,\s*(\d+)\s*\)$"),
                                                              r"NUMERIC(\2,\3)"),
    (re.compile(r"^(DECIMAL|NUMERIC|DEC)\s*\((\d+)\s*\)$"),   r"NUMERIC(\2,0)"),
    (re.compile(r"^(DECIMAL|NUMERIC|DEC|MONEY)$"),            "NUMERIC(18,4)"),
    # text family
    (re.compile(r"^CHAR\s*\(\s*(\d+)\s*\)$"),                 r"CHAR(\1)"),
    (re.compile(r"^N?VARCHAR\s*\(\s*(\d+)\s*\)$"),            r"VARCHAR(\1)"),
    (re.compile(r"^N?VARCHAR.*$"),                            "TEXT"),
    (re.compile(r"^(LONG)?TEXT$"),                            "TEXT"),
    (re.compile(r"^MEDIUMTEXT$"),                             "TEXT"),
    (re.compile(r"^TINYTEXT$"),                               "TEXT"),
    (re.compile(r"^N?CLOB.*$"),                               "TEXT"),
    # date / time
    (re.compile(r"^DATETIME.*$"),                             "TIMESTAMPTZ"),
    (re.compile(r"^TIMESTAMP.*$"),                            "TIMESTAMPTZ"),
    (re.compile(r"^DATE$"),                                   "DATE"),
    (re.compile(r"^TIME$"),                                   "TIME"),
    (re.compile(r"^YEAR.*$"),                                 "SMALLINT"),
    # binary
    (re.compile(r"^(VAR)?BINARY.*$"),                         "BYTEA"),
    (re.compile(r"^BLOB.*$"),                                 "BYTEA"),
    # json
    (re.compile(r"^JSON$"),                                   "JSONB"),
    (re.compile(r"^JSONB$"),                                  "JSONB"),
    # uuid
    (re.compile(r"^UUID$"),                                   "UUID"),
    # enum / set → TEXT (real ENUM TYPE creation is part of optional polish)
    (re.compile(r"^(ENUM|SET).*$"),                           "TEXT"),
]


def map_legacy_type(legacy_type: str) -> str:
    """Map a single legacy SQL type literal to its PostgreSQL equivalent."""
    if not legacy_type:
        return "TEXT"
    norm = re.sub(r"\s+UNSIGNED$", "", legacy_type.strip(), flags=re.IGNORECASE)
    norm = re.sub(r"\s+", "", norm).upper()
    for rx, repl in _TYPE_MAP:
        m = rx.match(norm)
        if m:
            return rx.sub(repl, norm)
    # Fallback: keep the original verbatim (already valid Postgres?) else TEXT.
    return legacy_type.strip() if re.match(r"^[A-Z][A-Z0-9_()\s,]+$", norm) else "TEXT"


# Standard audit columns appended to every generated table.
_AUDIT_COLS: List[Tuple[str, str]] = [
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("created_by", "UUID NULL"),
    ("updated_by", "UUID NULL"),
    ("deleted_at", "TIMESTAMPTZ NULL"),
]


# Column-name heuristics that drive index emission.
_FK_COL_RE       = re.compile(r"^(.+)_id$|^fk_.+$", re.IGNORECASE)
_STATUS_COL_RE   = re.compile(r"^(status|state|stage|type|kind|category|workflow|phase)$",
                              re.IGNORECASE)
_DATE_COL_RE     = re.compile(r"(_at|_on|_date|_time|_dt)$", re.IGNORECASE)
_AUDIT_COL_NAMES = {c for c, _ in _AUDIT_COLS}


def _sanitise_ident(name: str) -> str:
    """Lower-case, snake-case, strip backticks/quotes — Postgres-safe identifier."""
    n = name.strip().strip('"').strip("`").strip("'")
    n = re.sub(r"[^A-Za-z0-9_]", "_", n)
    return n.lower()[:63] or "_unnamed"


def _domain_for_table(table_name: str,
                      domain_map: Optional[Dict[str, Any]]) -> str:
    """Pick the module/domain a table belongs to (for grouping comments)."""
    if not domain_map:
        # Fallback: first underscore-prefix
        parts = table_name.split("_")
        return parts[0] if len(parts) > 1 else "core"
    for domain, info in domain_map.items():
        tbls = [t.lower() for t in ((info or {}).get("tables") or [])]
        if table_name.lower() in tbls:
            return domain
    parts = table_name.split("_")
    return parts[0] if len(parts) > 1 else "core"


def _natural_key_columns(columns: List[dict]) -> List[str]:
    """Heuristically identify columns that look like natural unique keys
    (e.g. `email`, `slug`, `code`, `username`, `phone_no`, `national_id`).
    Used to emit partial UNIQUE indexes that respect soft-delete."""
    natural: List[str] = []
    candidates = {"email", "username", "user_name", "login", "slug", "code",
                  "national_id", "pan", "gst", "phone", "phone_no",
                  "mobile", "passport_no", "uin", "aadhaar"}
    for c in columns:
        n = (c.get("name") or "").lower()
        if n in candidates:
            natural.append(n)
    return natural


def _column_line(col: dict, table_name: str, fks: List[dict]) -> str:
    """Render one column definition line, including inline REFERENCES if FK."""
    name = _sanitise_ident(col.get("name") or "")
    if not name:
        return ""
    pg_type = map_legacy_type(col.get("type") or "")

    # FK match? Inline the constraint.
    fk = next((f for f in fks if (f.get("column") or "").lower() == name), None)
    parts = [f'    "{name}"', pg_type]
    if fk:
        ref_table = _sanitise_ident(fk.get("ref_table") or "")
        ref_col   = _sanitise_ident(fk.get("ref_column") or "id")
        parts.append(f'REFERENCES "{ref_table}"("{ref_col}") ON DELETE RESTRICT')
    # NULL default — legacy nullability is rarely captured, leave generous.
    return " ".join(parts)


def _emit_table(entity: dict,
                fr_coverage: Dict[str, List[str]]) -> Tuple[str, List[str]]:
    """Emit `CREATE TABLE` block + return (ddl, list_of_index_statements)."""
    table = _sanitise_ident(entity.get("name") or "_unnamed")
    raw_columns: List[dict] = entity.get("columns") or []
    fks: List[dict]         = entity.get("fks") or []

    # Build column lines, dedupe by lower-name, drop audit-name collisions.
    seen: set = set()
    col_lines: List[str] = []
    have_id = False
    for col in raw_columns:
        n = _sanitise_ident(col.get("name") or "")
        if not n or n in seen:
            continue
        if n in _AUDIT_COL_NAMES:
            continue  # we'll append the standard audit block
        if n == "id":
            # Replace any legacy id with our UUID PK
            col_lines.append('    "id" UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY')
            have_id = True
        else:
            line = _column_line(col, table, fks)
            if line:
                col_lines.append(line)
        seen.add(n)
    if not have_id:
        # Prepend UUID PK
        col_lines.insert(0, '    "id" UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY')

    # Audit columns
    for cname, ctype in _AUDIT_COLS:
        col_lines.append(f'    "{cname}" {ctype}')

    ddl = (
        f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
        + ",\n".join(col_lines)
        + "\n);"
    )

    # COMMENT ON TABLE + COVERS list
    covers = fr_coverage.get(table.lower()) or []
    covers_str = ", ".join(covers[:8]) if covers else "—"
    comment = (
        f'COMMENT ON TABLE "{table}" IS '
        f"'OLTP entity for {table}. Covers SRS FRs: {covers_str}.';"
    )

    # Indexes
    indexes: List[str] = []
    # FK indexes
    fk_cols = {_sanitise_ident(f.get("column") or "") for f in fks}
    # Plus name-pattern detected FKs (e.g. `customer_id` even without explicit fk)
    for col in raw_columns:
        cn = _sanitise_ident(col.get("name") or "")
        if cn and cn != "id" and _FK_COL_RE.match(cn):
            fk_cols.add(cn)
    for col_name in sorted(fk_cols):
        if not col_name or col_name in _AUDIT_COL_NAMES:
            continue
        indexes.append(
            f'CREATE INDEX IF NOT EXISTS "ix_{table}_{col_name}" '
            f'ON "{table}"("{col_name}");'
        )
    # Status / date indexes
    for col in raw_columns:
        cn = _sanitise_ident(col.get("name") or "")
        if not cn or cn in _AUDIT_COL_NAMES or cn in fk_cols:
            continue
        if _STATUS_COL_RE.match(cn) or _DATE_COL_RE.search(cn):
            indexes.append(
                f'CREATE INDEX IF NOT EXISTS "ix_{table}_{cn}" '
                f'ON "{table}"("{cn}");'
            )
    # Natural-key partial UNIQUE (soft-delete friendly)
    for nk in _natural_key_columns(raw_columns):
        indexes.append(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{table}_{nk}_active" '
            f'ON "{table}"("{nk}") WHERE deleted_at IS NULL;'
        )

    return f"{ddl}\n{comment}", indexes


def _build_fr_coverage_map(fr_links: Optional[List[dict]]) -> Dict[str, List[str]]:
    """Convert a list of `{fr_id, table}` records into `{table: [fr_ids]}`.

    Accepts either the shape produced by `kb/br_tracker.py` (BR↔table) or any
    arbitrary list whose dicts carry both keys. Robust to missing fields.
    """
    out: Dict[str, List[str]] = {}
    for rec in (fr_links or []):
        fr = (rec.get("fr_id") or rec.get("br_id") or rec.get("requirement_id") or "").strip()
        tbl = (rec.get("table") or rec.get("entity") or "").strip().lower()
        if fr and tbl:
            out.setdefault(tbl, []).append(fr)
    return out


def generate_oltp_ddl(
    entities: List[dict],
    domain_map: Optional[Dict[str, Any]] = None,
    data_model_hints: Optional[Dict[str, Any]] = None,
    fr_links: Optional[List[dict]] = None,
    project_name: str = "",
    source_tech: str = "",
) -> str:
    """Build a complete OLTP DDL string from KB entities."""
    fr_coverage = _build_fr_coverage_map(fr_links)

    # Stable ordering: by domain alphabetical, then table alphabetical.
    decorated: List[Tuple[str, str, dict]] = []
    for ent in entities:
        if (ent.get("type") or "").upper() != "TABLE":
            continue
        name = _sanitise_ident(ent.get("name") or "")
        if not name:
            continue
        decorated.append((_domain_for_table(name, domain_map), name, ent))
    decorated.sort(key=lambda x: (x[0], x[1]))

    out: List[str] = []
    out.append("-- LAMA Generated OLTP Schema (deterministic generator, iter-13.44)")
    out.append(f"-- Project: {project_name or '(unknown)'}")
    out.append(f"-- Source : {source_tech or '(unknown)'} → Target: PostgreSQL")
    out.append(f"-- Tables : {len(decorated)}")
    out.append("")
    out.append("CREATE EXTENSION IF NOT EXISTS \"pgcrypto\";")
    out.append("CREATE EXTENSION IF NOT EXISTS \"citext\";")
    out.append("")

    # Emit tables grouped by domain
    indexes_all: List[str] = []
    current_domain = ""
    for domain, _, ent in decorated:
        if domain != current_domain:
            out.append("")
            out.append(f"-- ===== MODULE: {domain} =====")
            current_domain = domain
        ddl, idx = _emit_table(ent, fr_coverage)
        out.append("")
        out.append(ddl)
        indexes_all.extend(idx)

    if indexes_all:
        out.append("")
        out.append("-- ===== INDEXES =====")
        out.extend(indexes_all)

    out.append("")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Quick stats helper — used by the OLTP job runner for the audit log + result.
# ----------------------------------------------------------------------------
def stats(ddl: str) -> Dict[str, int]:
    upper = ddl.upper()
    return {
        "tables":  upper.count("CREATE TABLE"),
        "fks":     upper.count("REFERENCES "),
        "indexes": upper.count("CREATE INDEX") + upper.count("CREATE UNIQUE INDEX"),
    }

