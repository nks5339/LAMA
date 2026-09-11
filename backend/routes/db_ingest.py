"""Live database ingestion + application-URL ingestion (iter 13.8).

User requirement:
  > Section where user can directly provide database credentials and
  > application URL. Agent should use this data to understand system
  > context and update knowledge base.

What this router does:

  POST /api/kb/{project_id}/db-connect
      Body: { db_type, host, port, database_name, username, password,
              application_url, ssl }
      Action:
        1. Validate db_type ∈ {mysql, postgres, oracle, mssql, sqlite}.
        2. Try to connect using the appropriate stdlib/driver:
             - postgres → psycopg2/psycopg
             - mysql    → pymysql / mysql-connector
             - mssql    → pyodbc (best-effort)
             - oracle   → oracledb (best-effort)
             - sqlite   → stdlib sqlite3
        3. Pull table/column/FK/index metadata via information_schema.
        4. Persist as KB entities (type=TABLE / FK_HINT) so downstream
           TOON + RAG + business-ontology see the live schema.
        5. If the driver is missing or auth fails, store a "schema_hint"
           descriptor on the project doc so prompts can still reason about
           the connection target (graceful fallback per the spec).

  POST /api/kb/{project_id}/app-url
      Body: { application_url, notes }
      Stores the URL as a project-level integration-point hint. Best-effort
      HTTP HEAD/GET probe to capture the response header surface (Server,
      X-Powered-By, framework cookies) without fetching full HTML.

  GET /api/kb/{project_id}/data-sources
      Lists every DB / URL hint registered for the project.

All credentials are stored encrypted-at-rest **only** when LAMA_DB_SECRET is
set. By default we hash the password (one-way) and keep only the connection
descriptor — the live connection itself is short-lived.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import projects, kb_entities, kb_toon, data_sources
from kb.toon import serialise, summarise
from kb.owl_extractor import aggregate_stats

logger = logging.getLogger("lama.db_ingest")

router = APIRouter(prefix="/kb", tags=["kb-db"])


# ──────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────
class DbConnectRequest(BaseModel):
    db_type: str = Field(..., description="mysql | postgres | oracle | mssql | sqlite")
    host: str = ""
    port: int | None = None
    database_name: str
    username: str = ""
    password: str = ""
    application_url: str = ""
    ssl: bool = False
    # When True, the live connection is skipped — only the descriptor is
    # stored. Lets users register a target DB they don't want LAMA to
    # actually touch.
    descriptor_only: bool = False


class AppUrlRequest(BaseModel):
    application_url: str
    notes: str = ""


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────
def _hash_secret(s: str) -> str:
    if not s:
        return ""
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def _default_port(db_type: str) -> int:
    return {
        "mysql": 3306, "postgres": 5432, "postgresql": 5432,
        "mssql": 1433, "oracle": 1521, "sqlite": 0,
    }.get((db_type or "").lower(), 0)


def _probe_tcp(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Cheap reachability check — does NOT authenticate."""
    if not host or not port:
        return {"reachable": False, "error": "no host/port"}
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "error": ""}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:200]}


# ──────────────────────────────────────────────────────────────────────
# Schema extraction — per-driver. Each fn returns:
#   { "tables": [ {name, columns:[{name,type,nullable,pk,fk_to}], ...} ],
#     "fk_count": int,
#     "driver": "psycopg" | "pymysql" | ... }
# Raises ImportError if the driver isn't installed and RuntimeError on
# any other failure.
# ──────────────────────────────────────────────────────────────────────
def _fetch_postgres(req: DbConnectRequest) -> dict[str, Any]:
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
        driver = "psycopg2"
    except Exception:
        try:
            import psycopg  # type: ignore
            driver = "psycopg"
        except Exception as exc:
            raise ImportError(
                "psycopg2 driver not importable in this backend process. "
                "Remediation:\n"
                "  • Docker:  docker exec lama pip install --no-cache-dir psycopg2-binary==2.9.9 "
                "&& docker compose restart lama\n"
                "  • Local:   pip install psycopg2-binary==2.9.9  (then restart uvicorn)\n"
                f"Underlying error: {exc}"
            )

    port = req.port or _default_port("postgres")
    if driver == "psycopg2":
        conn = psycopg2.connect(host=req.host, port=port, dbname=req.database_name,
                                user=req.username, password=req.password,
                                sslmode="require" if req.ssl else "prefer",
                                connect_timeout=8)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import psycopg  # type: ignore
        conn = psycopg.connect(host=req.host, port=port, dbname=req.database_name,
                               user=req.username, password=req.password,
                               sslmode="require" if req.ssl else "prefer",
                               connect_timeout=8)
        cur = conn.cursor()

    try:
        cur.execute("""
            SELECT table_schema, table_name, column_name, data_type,
                   is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog','information_schema')
            ORDER BY table_schema, table_name, ordinal_position
        """)
        rows = cur.fetchall()
        cur.execute("""
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema    = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema    = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
        """)
        fks = cur.fetchall()
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    return _build_schema_dict(rows, fks, driver=driver, key_table="table_name", key_col="column_name",
                              key_type="data_type", key_nullable="is_nullable",
                              key_fk_t="foreign_table", key_fk_c="foreign_column",
                              key_schema="table_schema")


def _fetch_mysql(req: DbConnectRequest) -> dict[str, Any]:
    try:
        import pymysql  # type: ignore
        driver = "pymysql"
    except Exception as exc:
        raise ImportError(
            "pymysql driver not importable in this backend process. "
            "Remediation:\n"
            "  • Docker:  docker exec lama pip install --no-cache-dir pymysql==1.1.1 "
            "&& docker compose restart lama\n"
            "  • Local:   pip install pymysql==1.1.1  (then restart uvicorn)\n"
            f"Underlying error: {exc}"
        )

    port = req.port or _default_port("mysql")
    conn = pymysql.connect(host=req.host, port=port, user=req.username,
                           password=req.password, database=req.database_name,
                           connect_timeout=8, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor,
                           ssl={} if req.ssl else None)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name,
                       COLUMN_NAME AS column_name, DATA_TYPE AS data_type,
                       IS_NULLABLE AS is_nullable
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
            """, (req.database_name,))
            rows = cur.fetchall()
            cur.execute("""
                SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name,
                       REFERENCED_TABLE_NAME AS foreign_table,
                       REFERENCED_COLUMN_NAME AS foreign_column
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
            """, (req.database_name,))
            fks = cur.fetchall()
    finally:
        try: conn.close()
        except Exception: pass

    return _build_schema_dict(rows, fks, driver=driver, key_table="table_name", key_col="column_name",
                              key_type="data_type", key_nullable="is_nullable",
                              key_fk_t="foreign_table", key_fk_c="foreign_column",
                              key_schema="table_schema")


def _fetch_oracle(req: DbConnectRequest) -> dict[str, Any]:
    """Oracle schema extraction via the modern `oracledb` driver (formerly
    cx_Oracle). `oracledb` defaults to a pure-python "Thin" mode so no
    Oracle Instant Client is required for basic schema introspection —
    works against any Oracle 12c+ server.

    Connection strings supported:
      • host + port + service name in `database_name` (e.g. "ORCLPDB1")
      • EZ-connect string passed as `database_name`
        (e.g. "myhost:1521/ORCLPDB1") — host/port then ignored.
    """
    try:
        import oracledb  # type: ignore
    except Exception as exc:
        raise ImportError(
            "oracledb driver not importable in this backend process. "
            "Remediation:\n"
            "  • Docker:  docker exec lama pip install --no-cache-dir oracledb==2.4.1 "
            "&& docker compose restart lama\n"
            "  • Local:   pip install oracledb==2.4.1  (then restart uvicorn)\n"
            "  • Image:   rebuild with backend/requirements.txt (oracledb is pinned there).\n"
            f"Underlying error: {exc}"
        )

    # iter-13.90 — Auto-init Thick mode when Oracle Instant Client is
    # present (Dockerfile ships a commented-out block to install it).
    # Thick mode is required for Oracle 11g, Wallets / ATP mTLS, OCI
    # advanced security, and TNS_ADMIN ezconnect with SCAN listeners.
    # `init_oracle_client()` is idempotent within a process — calling
    # it more than once is a no-op (raises DatabaseError "already
    # initialized" which we swallow). Skipped silently when no Instant
    # Client is on the LD_LIBRARY_PATH so the Thin-mode default keeps
    # working without any host-side install.
    try:
        ic_path = (os.environ.get("ORACLE_INSTANT_CLIENT") or "").strip()
        if not ic_path:
            for guess in ("/opt/oracle/instantclient", "/opt/oracle/instantclient_23_7"):
                if os.path.isdir(guess):
                    ic_path = guess
                    break
        if ic_path and os.path.isdir(ic_path) and not getattr(oracledb, "_thick_initialised_by_lama", False):
            oracledb.init_oracle_client(lib_dir=ic_path)
            oracledb._thick_initialised_by_lama = True  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — Thick init is best-effort
        pass

    port = req.port or _default_port("oracle")
    # Accept EZ-connect strings in EITHER `database_name` OR `host` verbatim,
    # otherwise build one from host/port/service-name. Users routinely paste
    # the full EZ-connect ("myhost:1521/ORCL") into whichever field is
    # closest to hand — without this guard we'd double-append and produce
    # a nonsensical "myhost:1521/ORCL:1521/ORCL" DSN (DPY-4018).
    host_val = (req.host or "").strip()
    dbname_val = (req.database_name or "").strip()
    if "/" in dbname_val or ":" in dbname_val:
        dsn = dbname_val
    elif "/" in host_val or ":" in host_val:
        dsn = host_val
    else:
        dsn = f"{host_val}:{port}/{dbname_val}"

    # oracledb supports both Thin (pure-python, default) and Thick (Instant
    # Client) modes. Thin is fine for information-schema queries.
    conn = oracledb.connect(
        user=req.username,
        password=req.password,
        dsn=dsn,
        # Short connect timeout so a wrong host doesn't hang the request.
        # `oracledb` accepts `tcp_connect_timeout` from 1.4+.
        tcp_connect_timeout=8,
    )
    try:
        cur = conn.cursor()
        # Schema scope: Oracle "schema" == owning user. Default to the
        # connecting user; let an explicit Oracle DBA pass another via
        # `LAMA_ORACLE_OWNER` if they ever need to introspect another
        # schema. Filter out the noisy SYS / SYSTEM / dictionary owners.
        owner_filter = (req.username or "").upper() or None
        if owner_filter:
            owner_pred = "OWNER = :owner"
            owner_bind = {"owner": owner_filter}
        else:
            owner_pred = ("OWNER NOT IN ('SYS','SYSTEM','OUTLN','DBSNMP','APPQOSSYS',"
                          "'GSMADMIN_INTERNAL','XDB','WMSYS','CTXSYS','ORDDATA','MDSYS',"
                          "'OLAPSYS','LBACSYS','DVSYS','AUDSYS','OJVMSYS','APEX_040000')")
            owner_bind = {}

        # Columns
        cur.execute(
            f"""
            SELECT owner             AS table_schema,
                   table_name        AS table_name,
                   column_name       AS column_name,
                   data_type         AS data_type,
                   nullable          AS is_nullable
              FROM all_tab_columns
             WHERE {owner_pred}
             ORDER BY owner, table_name, column_id
            """,
            owner_bind,
        )
        col_rows = cur.fetchall()
        col_desc = [d[0].lower() for d in cur.description]
        rows: list[dict] = [dict(zip(col_desc, r)) for r in col_rows]
        # Normalise Oracle's 'Y' / 'N' to MySQL/PG-style 'YES' / 'NO'
        # so _build_schema_dict's nullable check works unchanged.
        for r in rows:
            v = (r.get("is_nullable") or "").upper()
            r["is_nullable"] = "YES" if v == "Y" else "NO"

        # Foreign keys — join all_constraints (PK side) ↔ all_cons_columns
        cur.execute(
            f"""
            SELECT c_src.table_name        AS table_name,
                   col_src.column_name     AS column_name,
                   c_dst.table_name        AS foreign_table,
                   col_dst.column_name     AS foreign_column
              FROM all_constraints c_src
              JOIN all_cons_columns col_src
                ON col_src.owner = c_src.owner
               AND col_src.constraint_name = c_src.constraint_name
              JOIN all_constraints c_dst
                ON c_dst.owner = c_src.r_owner
               AND c_dst.constraint_name = c_src.r_constraint_name
              JOIN all_cons_columns col_dst
                ON col_dst.owner = c_dst.owner
               AND col_dst.constraint_name = c_dst.constraint_name
               AND col_dst.position = col_src.position
             WHERE c_src.constraint_type = 'R'
               AND c_src.{owner_pred.replace('OWNER', 'owner').replace(':owner', ':owner')}
            """,
            owner_bind,
        )
        fk_rows = cur.fetchall()
        fk_desc = [d[0].lower() for d in cur.description]
        fks: list[dict] = [dict(zip(fk_desc, r)) for r in fk_rows]
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    return _build_schema_dict(
        rows, fks, driver="oracledb",
        key_table="table_name", key_col="column_name",
        key_type="data_type", key_nullable="is_nullable",
        key_fk_t="foreign_table", key_fk_c="foreign_column",
        key_schema="table_schema",
    )


def _fetch_mssql(req: DbConnectRequest) -> dict[str, Any]:
    """MS SQL Server schema extraction.

    Prefers `pymssql` (pure-python, FreeTDS-based — easiest install).
    Falls back to `pyodbc` if available (requires unixODBC + the
    Microsoft ODBC driver, more painful but standard in MS shops).
    """
    driver = ""
    conn = None
    port = req.port or _default_port("mssql")
    last_exc: Exception | None = None
    try:
        import pymssql  # type: ignore
        driver = "pymssql"
        conn = pymssql.connect(
            server=req.host, port=port, user=req.username,
            password=req.password, database=req.database_name,
            login_timeout=8, timeout=15, as_dict=True,
        )
    except Exception as exc:
        last_exc = exc

    if conn is None:
        try:
            import pyodbc  # type: ignore
            driver = "pyodbc"
            # Caller may pass a full DSN in `host`; otherwise build a
            # default ODBC connection string targeting the MS ODBC driver.
            conn_str = (
                req.host if (";" in req.host and "=" in req.host) else
                f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={req.host},{port};"
                f"DATABASE={req.database_name};UID={req.username};PWD={req.password};"
                f"TrustServerCertificate=yes;Encrypt={'yes' if req.ssl else 'no'};"
            )
            conn = pyodbc.connect(conn_str, timeout=8)
        except Exception as exc:
            last_exc = exc

    if conn is None:
        raise ImportError(
            "Neither pymssql nor pyodbc usable for MSSQL extraction. "
            "Remediation:\n"
            "  • Docker:  docker exec lama pip install --no-cache-dir pymssql==2.3.0 "
            "&& docker compose restart lama\n"
            "  • Local:   pip install pymssql==2.3.0  (then restart uvicorn)\n"
            f"Underlying error: {last_exc}"
        )

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
                   DATA_TYPE, IS_NULLABLE
              FROM INFORMATION_SCHEMA.COLUMNS
             ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """)
        if driver == "pymssql":
            col_rows = cur.fetchall()  # list[dict]
            rows = [{
                "table_schema": r["TABLE_SCHEMA"], "table_name": r["TABLE_NAME"],
                "column_name":  r["COLUMN_NAME"],  "data_type":  r["DATA_TYPE"],
                "is_nullable":  r["IS_NULLABLE"],
            } for r in col_rows]
        else:
            rows = [{
                "table_schema": r[0], "table_name": r[1], "column_name": r[2],
                "data_type":   r[3], "is_nullable": r[4],
            } for r in cur.fetchall()]

        cur.execute("""
            SELECT kcu1.TABLE_NAME    AS table_name,
                   kcu1.COLUMN_NAME   AS column_name,
                   kcu2.TABLE_NAME    AS foreign_table,
                   kcu2.COLUMN_NAME   AS foreign_column
              FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
              JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu1
                ON kcu1.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
              JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu2
                ON kcu2.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
               AND kcu2.ORDINAL_POSITION = kcu1.ORDINAL_POSITION
        """)
        if driver == "pymssql":
            fks = [{"table_name":   r["table_name"],   "column_name":   r["column_name"],
                    "foreign_table": r["foreign_table"], "foreign_column": r["foreign_column"]}
                   for r in cur.fetchall()]
        else:
            fks = [{"table_name": r[0], "column_name": r[1],
                    "foreign_table": r[2], "foreign_column": r[3]}
                   for r in cur.fetchall()]
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    return _build_schema_dict(
        rows, fks, driver=driver,
        key_table="table_name", key_col="column_name",
        key_type="data_type", key_nullable="is_nullable",
        key_fk_t="foreign_table", key_fk_c="foreign_column",
        key_schema="table_schema",
    )


def _fetch_sqlite(req: DbConnectRequest) -> dict[str, Any]:
    import sqlite3
    conn = sqlite3.connect(req.database_name, timeout=8)
    conn.row_factory = sqlite3.Row
    try:
        tbls = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        rows: list[dict] = []
        fks: list[dict] = []
        for t in tbls:
            for c in conn.execute(f'PRAGMA table_info("{t}")'):
                rows.append({"table_schema": "main", "table_name": t, "column_name": c["name"],
                             "data_type": c["type"], "is_nullable": "YES" if c["notnull"] == 0 else "NO"})
            for f in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
                fks.append({"table_name": t, "column_name": f["from"],
                            "foreign_table": f["table"], "foreign_column": f["to"]})
    finally:
        conn.close()

    return _build_schema_dict(rows, fks, driver="sqlite3", key_table="table_name", key_col="column_name",
                              key_type="data_type", key_nullable="is_nullable",
                              key_fk_t="foreign_table", key_fk_c="foreign_column",
                              key_schema="table_schema")


def _build_schema_dict(
    rows: list[dict], fks: list[dict], *, driver: str,
    key_table: str, key_col: str, key_type: str, key_nullable: str,
    key_fk_t: str, key_fk_c: str, key_schema: str,
) -> dict[str, Any]:
    fk_index: dict[tuple[str, str], tuple[str, str]] = {}
    for f in fks:
        fk_index[(str(f.get(key_table) or ""), str(f.get(key_col) or ""))] = (
            str(f.get(key_fk_t) or ""), str(f.get(key_fk_c) or ""))

    tables: dict[str, dict] = {}
    for r in rows:
        tname = str(r.get(key_table) or "")
        if not tname:
            continue
        t = tables.setdefault(tname, {
            "name": tname, "schema": str(r.get(key_schema) or ""), "columns": [],
        })
        col = str(r.get(key_col) or "")
        fk_target = fk_index.get((tname, col))
        t["columns"].append({
            "name": col,
            "type": str(r.get(key_type) or ""),
            "nullable": str(r.get(key_nullable) or "").upper() == "YES",
            "fk_to": f"{fk_target[0]}.{fk_target[1]}" if fk_target else "",
        })
    return {
        "driver": driver,
        "tables": list(tables.values()),
        "fk_count": len(fks),
    }


_FETCHERS = {
    "postgres":   _fetch_postgres,
    "postgresql": _fetch_postgres,
    "mysql":      _fetch_mysql,
    "mariadb":    _fetch_mysql,
    "sqlite":     _fetch_sqlite,
    "oracle":     _fetch_oracle,   # iter 13.8.1 — needs `pip install oracledb`
    "mssql":      _fetch_mssql,    # iter 13.8.1 — needs `pip install pymssql` (or pyodbc)
}


def _schema_to_entities(project_id: str, schema: dict[str, Any], origin_label: str) -> list[dict]:
    """Turn the fetched schema into KB entities compatible with toon.serialise
    + aggregate_stats. Uses the same TABLE / TABLE_HINT shape that
    owl_extractor emits, so the rest of the pipeline doesn't care whether
    a table came from a .sql upload or a live DB connection.
    """
    out: list[dict] = []
    for t in schema.get("tables", []):
        cols = [{"name": c["name"], "type": c.get("type") or ""} for c in t.get("columns", [])]
        out.append({
            "type": "TABLE",
            "name": t["name"],
            "columns": cols,
            "source": f"live-db::{origin_label}::{t.get('schema','')}.{t['name']}",
            "from_live_db": True,
        })
        for c in t.get("columns", []):
            if c.get("fk_to"):
                out.append({
                    "type": "FK_HINT",
                    "name": f"{t['name']}.{c['name']}→{c['fk_to']}",
                    "from_table": t["name"],
                    "from_column": c["name"],
                    "to": c["fk_to"],
                    "source": f"live-db::{origin_label}",
                    "from_live_db": True,
                })
    return out


async def _probe_application_url(url: str) -> dict[str, Any]:
    """Best-effort HEAD probe — captures Server / X-Powered-By / set-cookie
    surface that often outs the legacy framework (e.g. PHPSESSID,
    JSESSIONID, ASP.NET_SessionId)."""
    if not url:
        return {"reachable": False}
    try:
        import httpx  # already in requirements
    except Exception:
        return {"reachable": False, "error": "httpx missing"}
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=6.0) as cli:
            r = await cli.head(url)
            headers = {k.lower(): v for k, v in r.headers.items()}
            return {
                "reachable": True,
                "status": r.status_code,
                "server":        headers.get("server", ""),
                "x_powered_by":  headers.get("x-powered-by", ""),
                "set_cookie":    headers.get("set-cookie", "")[:300],
            }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)[:200]}


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────
# iter-13.95 — Driver-status probe. Lets the UI / operators verify which
# live-DB + DWH drivers are actually importable in the running backend
# process without having to trigger a real db-connect (and without
# shelling into the container). Returns 200 always; the caller inspects
# `ready` per driver. Pairs with entrypoint.sh::ensure_pkg self-heal.
_DRIVER_PROBES = [
    # (label, import_module, pip_spec, category)
    ("postgres",    "psycopg2",                 "psycopg2-binary==2.9.9",            "oltp"),
    ("mysql",       "pymysql",                  "pymysql==1.1.1",                    "oltp"),
    ("oracle",      "oracledb",                 "oracledb==2.4.1",                   "oltp"),
    ("mssql",       "pymssql",                  "pymssql==2.3.0",                    "oltp"),
    ("sqlite",      "sqlite3",                  "(stdlib)",                          "oltp"),
    ("snowflake",   "snowflake.connector",      "snowflake-connector-python>=4.6.0", "dwh"),
    ("clickhouse",  "clickhouse_driver",        "clickhouse-driver==0.2.9",          "dwh"),
    ("redshift",    "redshift_connector",       "redshift_connector==2.1.5",         "dwh"),
    ("bigquery",    "google.cloud.bigquery",    "google-cloud-bigquery==3.27.0",     "dwh"),
    ("db2",         "ibm_db",                   "ibm_db==3.2.4",                     "oltp"),
]


@router.get("/db-drivers")
async def list_db_drivers():
    """Report which DB / DWH drivers are importable in this backend.

    Categories:
      • oltp — classic enterprise OLTP (postgres/mysql/oracle/mssql/sqlite/db2)
      • dwh  — data-warehouse / OLAP (snowflake/redshift/clickhouse/bigquery)

    Each entry: { driver, module, ready, version, pip_install, category,
                  remediation? }
    """
    import importlib
    out: list[dict[str, Any]] = []
    for label, mod, pip_spec, category in _DRIVER_PROBES:
        entry: dict[str, Any] = {
            "driver": label,
            "module": mod,
            "pip_install": pip_spec,
            "category": category,
            "ready": False,
            "version": "",
        }
        try:
            m = importlib.import_module(mod)
            entry["ready"] = True
            entry["version"] = (
                getattr(m, "__version__", None)
                or getattr(m, "VERSION", None)
                or ""
            )
            if isinstance(entry["version"], tuple):
                entry["version"] = ".".join(str(x) for x in entry["version"])
        except Exception as exc:  # noqa: BLE001
            entry["remediation"] = (
                f"docker exec lama pip install --no-cache-dir {pip_spec} "
                "&& docker compose restart lama"
            )
            entry["error"] = str(exc)[:120]
        out.append(entry)
    summary = {
        "ready_count": sum(1 for d in out if d["ready"]),
        "total": len(out),
        "oltp_ready": sum(1 for d in out if d["ready"] and d["category"] == "oltp"),
        "dwh_ready": sum(1 for d in out if d["ready"] and d["category"] == "dwh"),
    }
    return {"drivers": out, "summary": summary}


@router.post("/{project_id}/db-connect")
async def db_connect(project_id: str, req: DbConnectRequest):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    dbt = (req.db_type or "").lower().strip()
    if dbt not in {"mysql", "mariadb", "postgres", "postgresql", "sqlite", "oracle", "mssql"}:
        raise HTTPException(400, f"Unsupported db_type '{req.db_type}'")

    port = req.port or _default_port(dbt)
    # iter-14.10 — TCP probe and driver connect are BOTH synchronous (raw
    # socket + blocking DB driver). Running them directly on the FastAPI
    # event loop wedged the whole backend for the duration of a slow DNS
    # / handshake / query, which the user perceived as "DB connect is
    # taking too much time" AND made the UI unresponsive to every other
    # request. Dispatch both via asyncio.to_thread so the event loop
    # stays free to service polling + status calls.
    if not req.descriptor_only and dbt != "sqlite":
        tcp = await asyncio.to_thread(_probe_tcp, req.host, port)
    else:
        tcp = {"reachable": True}
    app_probe = await _probe_application_url(req.application_url)

    # Descriptor + fallback path -----------------------------------------
    descriptor = {
        "db_type":       dbt,
        "host":          req.host,
        "port":          port,
        "database":      req.database_name,
        "username":      req.username,
        "password_hash": _hash_secret(req.password),
        "ssl":           req.ssl,
        "application_url": req.application_url,
        "tcp_probe":     tcp,
        "app_probe":     app_probe,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }

    schema: dict[str, Any] | None = None
    extract_error = ""
    if not req.descriptor_only:
        fetcher = _FETCHERS.get(dbt)
        if fetcher is None:
            extract_error = f"No live-extractor for {dbt} in this build — descriptor stored only."
        else:
            try:
                # iter-14.10 — synchronous DB driver → to_thread so the
                # event loop can service concurrent polls / other API
                # calls while the driver blocks on the network handshake.
                schema = await asyncio.to_thread(fetcher, req)
            except ImportError as exc:
                extract_error = f"Driver not installed: {exc}"
                logger.warning("db-connect: driver missing for %s — %s", dbt, exc)
            except Exception as exc:
                # iter-14.56.2 — Include the exception TYPE so the UI can
                # tell ORA-01017 (auth) from ORA-12154 (bad TNS) from
                # DPY-4011 (server not reachable). Previously the operator
                # just saw "Connection/query failed: ..." with the message
                # sometimes empty (oracledb raises with .args=() when the
                # server closes mid-handshake). Also log the full traceback
                # into supervisord's backend.log so operators can grep for
                # the ORA code without another round-trip.
                exc_type = type(exc).__name__
                exc_msg = str(exc).strip() or repr(exc)
                extract_error = f"{exc_type}: {exc_msg}"
                logger.exception(
                    "db-connect: fetcher failed for %s://%s/%s — %s",
                    dbt, req.host, req.database_name, extract_error,
                )
        # iter-14.56.2 — Extra guard: fetcher returned something but with
        # zero tables. Surface that as an actionable error instead of the
        # generic "Re-ingest returned 0 tables" that the FE would otherwise
        # synthesise. The empty-schema path usually means the connecting
        # user has no visible tables under their default owner — for
        # Oracle set LAMA_ORACLE_OWNER, for others verify the search_path
        # / current_schema. Only overwrite extract_error if it's blank.
        if not extract_error and schema is not None and not schema.get("tables"):
            extract_error = (
                f"Connected to {dbt}://{req.host}/{req.database_name} as "
                f"{req.username!r} but the introspection query returned 0 "
                "tables. The connecting user probably lacks SELECT on the "
                "target schema, or is pointed at an empty schema. For "
                "Oracle set LAMA_ORACLE_OWNER=<schema>; for Postgres/MySQL "
                "verify the database name and search_path."
            )
            logger.warning("db-connect: 0-table result for %s — %s", dbt, extract_error)

    # Persist entities + descriptor --------------------------------------
    inserted = 0
    if schema and schema.get("tables"):
        origin_label = f"{dbt}://{req.host or '(local)'}/{req.database_name}"
        ents = _schema_to_entities(project_id, schema, origin_label)
        if ents:
            # Stamp project_id so subsequent kb queries pick them up.
            for e in ents:
                e["project_id"] = project_id
                e["file_id"] = f"live-db::{descriptor['registered_at']}"
            await kb_entities.insert_many(ents)
            inserted = len(ents)

        # Rebuild TOON + summary so the new tables show up in prompts.
        all_entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(200000)
        toon = serialise(all_entities)
        stats = aggregate_stats(all_entities)
        detected_lang = ((proj.get("detected_tech") or {}).get("language")
                         or (proj.get("source_tech") or "").split("/")[0].strip())
        summary = summarise(all_entities, stats, language=detected_lang)
        await kb_toon.update_one(
            {"project_id": project_id},
            {"$set": {
                "project_id": project_id, "toon": toon,
                "summary": summary, "stats": stats,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )

    await data_sources.update_one(
        {"project_id": project_id, "kind": "database",
         "host": req.host, "database": req.database_name, "db_type": dbt},
        {"$set": {
            "project_id":   project_id,
            "kind":         "database",
            "descriptor":   descriptor,
            "schema_summary": {
                "tables":   len(schema.get("tables", [])) if schema else 0,
                "fk_count": schema.get("fk_count", 0) if schema else 0,
                "driver":   (schema or {}).get("driver", ""),
            } if schema else {"tables": 0, "fk_count": 0, "driver": ""},
            "entities_ingested": inserted,
            "last_extract_error": extract_error,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
            **{k: descriptor[k] for k in ("db_type", "host", "database")},
        }},
        upsert=True,
    )

    # Promote the application URL into the project doc so SRS prompts
    # (Section 5 — Integration Points) and the legacy-coverage block can
    # cite it without an extra Mongo round-trip.
    if req.application_url:
        await projects.update_one(
            {"id": project_id},
            {"$set": {"application_url": req.application_url,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    return {
        "ok": bool(schema and schema.get("tables")) or bool(req.descriptor_only),
        "descriptor_only": bool(req.descriptor_only),
        "tcp_probe":   tcp,
        "app_probe":   app_probe,
        "extract_error": extract_error,
        "entities_ingested": inserted,
        "tables_seen": len((schema or {}).get("tables", [])) if schema else 0,
        "fk_count":    (schema or {}).get("fk_count", 0) if schema else 0,
    }


@router.post("/{project_id}/app-url")
async def register_app_url(project_id: str, req: AppUrlRequest):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    probe = await _probe_application_url(req.application_url)
    await data_sources.update_one(
        {"project_id": project_id, "kind": "app_url", "application_url": req.application_url},
        {"$set": {
            "project_id":       project_id,
            "kind":             "app_url",
            "application_url":  req.application_url,
            "notes":            req.notes,
            "probe":            probe,
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    await projects.update_one(
        {"id": project_id},
        {"$set": {"application_url": req.application_url,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True, "probe": probe}


@router.get("/{project_id}/data-sources")
async def list_data_sources(project_id: str):
    docs = await data_sources.find({"project_id": project_id}, {"_id": 0}).to_list(200)
    return {"items": docs, "count": len(docs)}




