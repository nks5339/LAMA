"""Legacy table inventory harvested from ``kb_entities``.

Not every legacy stack persists its schema the same way, so the OLTP table
list lives in three different shapes inside the KB:

* **Java / JPA (Struts, Spring MVC)** — the schema is captured as
  ``TABLE_HINT`` entities (one per ``@Table`` annotation) whose *columns*
  live on the sibling ``CLASS`` entity (``is_jpa_entity: true``,
  ``fields: [{name, java_type, required}]``). Joined by ``file_id``.
* **SQL-dump projects (PHP / MySQL / Oracle DDL)** — real ``TABLE``
  entities with an explicit ``columns`` list.
* **JSP / view-layer references** — ``JSP_TABLE_REFS`` (names only, noisy),
  intentionally *excluded* here to avoid synthesising junk entities.

This module unifies the first two shapes into a single legacy-table spec and
into synthetic ``CREATE TABLE`` DDL. It is the fallback the pipeline relies on
when the DataModel stage is **skipped** (``oltp_ddl`` empty): both the
Architecture surface and — critically — CodeGen entity/repository generation
read from here so every legacy table still yields an ``@Entity`` +
``JpaRepository``.

Design notes:
* Column *names* are the Java field names (camelCase). The real Oracle column
  names were never captured by the extractor, so the JPA field name is the
  highest-fidelity legacy-derived proxy; CodeGen round-trips it back through
  ``_camel``/``_pascal`` cleanly.
* Collection fields (``Set<..>`` / ``List<..>``) are associations, not
  columns → skipped.
* Every table is guaranteed at least one primary key (a synthetic
  ``id BIGINT`` is prepended when the JPA model exposes none) so the generated
  ``@Entity`` always compiles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from db import kb_entities

# ---------------------------------------------------------------------------
# java_type -> SQL type mapping
# ---------------------------------------------------------------------------
_JAVA_TO_SQL: Dict[str, str] = {
    "string": "VARCHAR(255)",
    "char": "CHAR(1)",
    "character": "CHAR(1)",
    "char[]": "VARCHAR(255)",
    "boolean": "BOOLEAN",
    "byte[]": "BLOB",
    "int": "INTEGER",
    "integer": "INTEGER",
    "short": "SMALLINT",
    "long": "BIGINT",
    "biginteger": "BIGINT",
    "double": "DOUBLE PRECISION",
    "float": "REAL",
    "bigdecimal": "DECIMAL(18,2)",
    "date": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
    "time": "TIMESTAMP",
    "calendar": "TIMESTAMP",
    "localdate": "DATE",
    "localdatetime": "TIMESTAMP",
    "localtime": "TIMESTAMP",
    "instant": "TIMESTAMP",
}

# Junk / non-business table-name markers (kept light — the @Table set is
# already high-precision).
_JUNK_MARKERS = ("_bak", "_backup", "_old", "_tmp", "_temp", "$", " ")


def _java_to_sql(java_type: Optional[str]) -> Optional[str]:
    """Map a Java field type to a SQL column type.

    Returns ``None`` for collection types (associations, not columns).
    Unknown object types (composite-id / entity references) fall back to
    ``BIGINT`` so the association is preserved as a foreign-key-style column.
    """
    if not java_type:
        return "VARCHAR(255)"
    jt = java_type.strip()
    low = jt.lower()
    # Collections are associations, never scalar columns.
    if low.startswith(("set<", "list<", "collection<", "map<")):
        return None
    base = _JAVA_TO_SQL.get(low)
    if base:
        return base
    # Unknown object type (e.g. a composite-id class or entity ref) — model
    # it as a BIGINT foreign-key-style column so the entity keeps the field.
    return "BIGINT"


def _is_junk_table(name: str) -> bool:
    if not name:
        return True
    low = name.lower()
    return any(m in low for m in _JUNK_MARKERS)


def _columns_from_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn JPA ``CLASS.fields`` into DDL column specs.

    Guarantees at least one primary key: uses a field literally named ``id``
    if present, otherwise prepends a synthetic ``id BIGINT`` PK.
    """
    cols: List[Dict[str, Any]] = []
    seen: set = set()
    pk_found = False
    for f in fields or []:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        sql_type = _java_to_sql(f.get("java_type"))
        if sql_type is None:  # collection / association — skip
            continue
        is_pk = low == "id"
        if is_pk:
            pk_found = True
        cols.append({
            "name": name,
            "sql_type": sql_type,
            "nullable": not bool(f.get("required")) and not is_pk,
            "pk": is_pk,
        })
        seen.add(low)
    if not pk_found:
        cols.insert(0, {
            "name": "id",
            "sql_type": "BIGINT",
            "nullable": False,
            "pk": True,
        })
    return cols


async def legacy_table_specs(project_id: str) -> List[Dict[str, Any]]:
    """Harvest the full legacy OLTP table inventory from the KB.

    Returns a de-duplicated list of
    ``{name, columns:[{name, sql_type, nullable, pk}], pk, source}`` sorted by
    table name. Prefers specs that carry real columns over name-only stubs.
    """
    # 1. Index JPA CLASS field-lists by file_id (and by source as a fallback).
    cls_by_file: Dict[Any, List[Dict[str, Any]]] = {}
    cls_by_source: Dict[str, List[Dict[str, Any]]] = {}
    async for c in kb_entities.find(
        {"project_id": project_id, "type": "CLASS", "is_jpa_entity": True},
        {"fields": 1, "file_id": 1, "source": 1},
    ):
        flds = c.get("fields") or []
        if not flds:
            continue
        if c.get("file_id") is not None:
            cls_by_file[c["file_id"]] = flds
        if c.get("source"):
            cls_by_source.setdefault(c["source"], flds)

    specs: Dict[str, Dict[str, Any]] = {}

    def _upsert(name: str, columns: List[Dict[str, Any]], source: str) -> None:
        if not name or _is_junk_table(name):
            return
        key = name.lower()
        pk = next((c["name"] for c in columns if c.get("pk")), None)
        cand = {"name": name, "columns": columns, "pk": pk, "source": source}
        prev = specs.get(key)
        # Prefer the entry with the richer column list.
        if prev is None or len(columns) > len(prev.get("columns") or []):
            specs[key] = cand

    # 2. JPA @Table hints — join to their CLASS for columns.
    async for h in kb_entities.find(
        {"project_id": project_id, "type": "TABLE_HINT"},
        {"name": 1, "file_id": 1, "source": 1},
    ):
        name = h.get("name")
        if not name:
            continue
        flds = cls_by_file.get(h.get("file_id"))
        if flds is None and h.get("source"):
            flds = cls_by_source.get(h["source"])
        columns = _columns_from_fields(flds or [])
        _upsert(name, columns, h.get("source") or "JPA @Table")

    # 3. Real SQL-dump TABLE entities (PHP/MySQL/Oracle DDL) — already have
    #    explicit columns; prefer these when richer.
    async for t in kb_entities.find(
        {"project_id": project_id, "type": "TABLE"},
        {"name": 1, "columns": 1, "source": 1, "pk": 1},
    ):
        name = t.get("name")
        if not name:
            continue
        raw_cols = t.get("columns") or []
        columns: List[Dict[str, Any]] = []
        pk_field = (t.get("pk") or "").lower() if isinstance(t.get("pk"), str) else ""
        for col in raw_cols:
            if isinstance(col, str):
                cn, ct = col, "VARCHAR(255)"
            else:
                cn = col.get("name") or col.get("column") or ""
                ct = col.get("sql_type") or col.get("type") or "VARCHAR(255)"
            if not cn:
                continue
            columns.append({
                "name": cn,
                "sql_type": ct,
                "nullable": (col.get("nullable", True) if isinstance(col, dict) else True),
                "pk": bool(col.get("pk")) if isinstance(col, dict) else (cn.lower() == pk_field),
            })
        if columns and not any(c.get("pk") for c in columns):
            columns[0]["pk"] = True
        if not columns:
            columns = [{"name": "id", "sql_type": "BIGINT", "nullable": False, "pk": True}]
        _upsert(name, columns, t.get("source") or "SQL DDL")

    return [specs[k] for k in sorted(specs.keys())]


def _emit_create_table(spec: Dict[str, Any]) -> str:
    name = spec["name"]
    lines: List[str] = [f"-- Source: {spec.get('source', 'legacy KB')}"]
    lines.append(f"CREATE TABLE {name} (")
    col_lines: List[str] = []
    for c in spec.get("columns") or []:
        parts = [f"    {c['name']} {c['sql_type']}"]
        if c.get("pk"):
            parts.append("PRIMARY KEY")
        elif not c.get("nullable", True):
            parts.append("NOT NULL")
        col_lines.append(" ".join(parts))
    lines.append(",\n".join(col_lines))
    lines.append(");")
    return "\n".join(lines)


async def build_oltp_ddl_from_kb(
    project_id: str, max_tables: Optional[int] = None,
) -> Tuple[str, int]:
    """Build synthetic ``CREATE TABLE`` DDL for every legacy table in the KB.

    ``max_tables`` caps the count (Architecture uses this to bound prompt
    size); CodeGen passes ``None`` so *all* legacy tables materialise as
    entities/repositories. Returns ``(ddl_text, table_count)``.
    """
    specs = await legacy_table_specs(project_id)
    if max_tables is not None and max_tables > 0:
        specs = specs[:max_tables]
    if not specs:
        return "", 0
    blocks = [_emit_create_table(s) for s in specs]
    header = (
        "-- Synthetic OLTP DDL reconstructed from legacy KB\n"
        f"-- ({len(specs)} tables harvested from @Table hints + JPA models)\n"
    )
    return header + "\n\n".join(blocks) + "\n", len(specs)
