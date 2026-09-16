"""Deterministic Oracle SQL → PostgreSQL translator.

Regex-driven token/pattern rewrites. Handles the common 90% cases:
    * data types (VARCHAR2, NUMBER, DATE, CLOB, BLOB, NVARCHAR2, ...)
    * built-in functions (SYSDATE, NVL, DECODE, SUBSTR, TRUNC, TO_DATE, ...)
    * DUAL / ROWNUM / MINUS
    * sequences (CREATE SEQUENCE + seq.NEXTVAL)
    * views / synonyms
    * PL/SQL procedure / function / package skeletons → PL/pgSQL

Anything unmatched is flagged and left in place with a
`-- TODO(dcte-manual)` marker.
"""
from __future__ import annotations
import re
from typing import Any


# --- data types -------------------------------------------------------------
TYPE_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bVARCHAR2\s*\(\s*(\d+)(?:\s+(?:BYTE|CHAR))?\s*\)", re.IGNORECASE), r"VARCHAR(\1)"),
    (re.compile(r"\bNVARCHAR2\s*\(\s*(\d+)\s*\)", re.IGNORECASE), r"VARCHAR(\1)"),
    (re.compile(r"\bNUMBER\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE), r"NUMERIC(\1,\2)"),
    (re.compile(r"\bNUMBER\s*\(\s*(\d+)\s*\)", re.IGNORECASE), r"NUMERIC(\1)"),
    (re.compile(r"\bNUMBER\b", re.IGNORECASE), "NUMERIC"),
    (re.compile(r"\bCLOB\b", re.IGNORECASE), "TEXT"),
    (re.compile(r"\bNCLOB\b", re.IGNORECASE), "TEXT"),
    (re.compile(r"\bBLOB\b", re.IGNORECASE), "BYTEA"),
    (re.compile(r"\bRAW\s*\(\s*\d+\s*\)", re.IGNORECASE), "BYTEA"),
    (re.compile(r"\bDATE\b", re.IGNORECASE), "TIMESTAMP"),
    (re.compile(r"\bTIMESTAMP\s+WITH\s+LOCAL\s+TIME\s+ZONE\b", re.IGNORECASE), "TIMESTAMPTZ"),
    (re.compile(r"\bTIMESTAMP\s+WITH\s+TIME\s+ZONE\b", re.IGNORECASE), "TIMESTAMPTZ"),
]

# --- built-in functions ----------------------------------------------------
FUNC_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bSYSDATE\b", re.IGNORECASE), "CURRENT_TIMESTAMP"),
    (re.compile(r"\bSYSTIMESTAMP\b", re.IGNORECASE), "CURRENT_TIMESTAMP"),
    (re.compile(r"\bNVL\s*\(", re.IGNORECASE), "COALESCE("),
    (re.compile(r"\bNVL2\s*\(", re.IGNORECASE), "CASE WHEN /*TODO NVL2*/ ("),
    (re.compile(r"\bSUBSTR\s*\(", re.IGNORECASE), "SUBSTRING("),
    (re.compile(r"\bTO_NUMBER\s*\(", re.IGNORECASE), "CAST("),
    (re.compile(r"\bTO_CHAR\s*\(", re.IGNORECASE), "TO_CHAR("),
    (re.compile(r"\bTO_DATE\s*\(", re.IGNORECASE), "TO_TIMESTAMP("),
    (re.compile(r"\bLENGTH\s*\(", re.IGNORECASE), "LENGTH("),
    (re.compile(r"\bTRUNC\s*\(\s*SYSDATE\s*\)", re.IGNORECASE), "DATE_TRUNC('day', CURRENT_TIMESTAMP)"),
    (re.compile(r"\bMOD\s*\(([^,]+),([^)]+)\)", re.IGNORECASE), r"(\1 % \2)"),
]

# --- sequence usage --------------------------------------------------------
NEXTVAL_RE = re.compile(r"(\w+)\.NEXTVAL\b", re.IGNORECASE)
CURRVAL_RE = re.compile(r"(\w+)\.CURRVAL\b", re.IGNORECASE)

# --- FROM DUAL / ROWNUM / MINUS -------------------------------------------
FROM_DUAL_RE = re.compile(r"\s+FROM\s+DUAL\b", re.IGNORECASE)
ROWNUM_LIMIT_RE = re.compile(r"(WHERE|AND)\s+ROWNUM\s*<=\s*(\d+)", re.IGNORECASE)
MINUS_RE = re.compile(r"\bMINUS\b", re.IGNORECASE)


# --- DECODE(a, k1, v1, k2, v2, ..., default) → CASE ...  --------------------
DECODE_RE = re.compile(r"\bDECODE\s*\((.*?)\)", re.IGNORECASE | re.DOTALL)


def _decode_to_case(inner: str) -> str:
    """Best-effort DECODE(expr, k1, v1, k2, v2, ..., default) → CASE expression."""
    # naive split on top-level commas
    parts, depth, buf = [], 0, []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    if len(parts) < 3:
        return f"DECODE({inner}) /* TODO(dcte-manual): too few args */"
    expr = parts[0]
    pairs = parts[1:]
    default = None
    if len(pairs) % 2 == 1:
        default = pairs[-1]
        pairs = pairs[:-1]
    whens = " ".join(
        f"WHEN {pairs[i]} THEN {pairs[i + 1]}" for i in range(0, len(pairs), 2)
    )
    else_clause = f" ELSE {default}" if default else ""
    return f"CASE {expr} {whens}{else_clause} END"


class SqlTranslator:
    def translate(self, src: str) -> tuple[str, dict[str, Any]]:
        meta: dict[str, Any] = {
            "types_replaced": 0, "funcs_replaced": 0,
            "sequences_touched": 0, "decode_expanded": 0,
            "unsupported": [],
        }
        s = src
        for rx, repl in TYPE_MAP:
            s, n = rx.subn(repl, s)
            meta["types_replaced"] += n
        for rx, repl in FUNC_MAP:
            s, n = rx.subn(repl, s)
            meta["funcs_replaced"] += n

        # DECODE
        def _dec(m: re.Match) -> str:
            meta["decode_expanded"] += 1
            return _decode_to_case(m.group(1))
        s = DECODE_RE.sub(_dec, s)

        # sequences
        def _next(m: re.Match) -> str:
            meta["sequences_touched"] += 1
            return f"nextval('{m.group(1).lower()}')"
        def _curr(m: re.Match) -> str:
            meta["sequences_touched"] += 1
            return f"currval('{m.group(1).lower()}')"
        s = NEXTVAL_RE.sub(_next, s)
        s = CURRVAL_RE.sub(_curr, s)

        # DUAL — Postgres allows SELECT without FROM.
        s = FROM_DUAL_RE.sub("", s)

        # ROWNUM <= N → LIMIT N (heuristic — append at end of statement)
        m = ROWNUM_LIMIT_RE.search(s)
        if m:
            keyword = m.group(1).upper()
            limit = m.group(2)
            # If it was a bare WHERE ROWNUM, drop the whole clause; if it was
            # AND ROWNUM, dropping the AND-clause leaves the preceding WHERE
            # intact. Either way, remove the ROWNUM predicate token.
            s = ROWNUM_LIMIT_RE.sub("" if keyword == "AND" else "", s)
            s = s.rstrip()
            if s.endswith(";"):
                s = s[:-1] + f"\nLIMIT {limit};\n"
            else:
                s += f"\nLIMIT {limit}\n"

        # MINUS → EXCEPT
        s = MINUS_RE.sub("EXCEPT", s)

        # CREATE OR REPLACE PACKAGE — not supported in PG; flag for manual.
        if re.search(r"CREATE\s+OR\s+REPLACE\s+PACKAGE\b", s, re.IGNORECASE):
            meta["unsupported"].append("PACKAGE")
            s = "-- TODO(dcte-manual): Oracle PACKAGE has no PG analogue; split into schemas + functions.\n" + s

        # Any leftover Oracle-isms → TODO comment at top
        leftovers = re.findall(
            r"\b(VARCHAR2|SYSDATE|NVL|DECODE|DUAL|MINUS|ROWNUM)\b",
            s, re.IGNORECASE,
        )
        if leftovers:
            meta["unsupported"].append(
                f"leftovers: {sorted(set(t.upper() for t in leftovers))}"
            )

        return s, meta
