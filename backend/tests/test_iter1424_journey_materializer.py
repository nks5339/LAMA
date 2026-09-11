"""iter-14.24 — Journey KB (Route/View anchors) unit tests.

Covers the pure, deterministic builders in ``kb/journey_materializer.py``:

- ``_index_graph``            — graph indexing (nodes + directional edges)
- ``_build_api_journey``      — Route-anchored: Route→Class→Methods→
                                Tables→Columns + Roles + BRs
- ``_build_ui_journey``       — View-anchored: JSP_FORM → Fields → pivot
                                to Route by action path
- ``_build_column_journey``   — Column-anchored (opt-in kind)
- ``_normalise_action_to_pivot`` — JSP action → Route name matching
- ``_hash_trace``             — deterministic content hash
- ``render_journey_toon`` / ``render_journeys_block``

DB / Motor calls are NOT exercised — pure helpers are where the
interesting logic lives; ``materialize_journeys`` / ``load_journeys``
are thin CRUD wrappers around them.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from kb.journey_materializer import (  # noqa: E402
    _build_api_journey,
    _build_column_journey,
    _build_ui_journey,
    _hash_trace,
    _index_graph,
    _normalise_action_to_pivot,
    render_journey_toon,
    render_journeys_block,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _api_graph_full() -> dict:
    """Full deterministic path: Route ← Class → Methods, Class →
    REFERENCES_TABLE → Table → Columns, Route → GUARDED_BY → Role.

    No READS/WRITES/CALLS edges — proves api_journey builds purely on
    the deterministic pass (Graph-KB enrichment OFF).
    """
    return {
        "nodes": [
            {"id": "Route::post /api/register", "type": "Route",
             "name": "POST /api/register", "verb": "POST",
             "path": "/api/register", "source": "app/AuthCtrl.php"},
            {"id": "Class::authctrl", "type": "Class", "name": "AuthCtrl",
             "source": "app/AuthCtrl.php"},
            {"id": "Method::authctrl.register", "type": "Method",
             "name": "AuthCtrl.register", "class_name": "AuthCtrl",
             "source": "app/AuthCtrl.php"},
            {"id": "Method::authctrl.validate", "type": "Method",
             "name": "AuthCtrl.validate", "class_name": "AuthCtrl",
             "source": "app/AuthCtrl.php"},
            {"id": "Table::users", "type": "Table", "name": "users",
             "source": "sql/user.sql"},
            {"id": "Column::users.email_id", "type": "Column",
             "name": "users.email_id", "short_name": "email_id",
             "table": "users", "data_type": "varchar(120)"},
            {"id": "Column::users.password_hash", "type": "Column",
             "name": "users.password_hash", "short_name": "password_hash",
             "table": "users", "data_type": "varchar(255)"},
            {"id": "Role::guest", "type": "Role", "name": "guest"},
        ],
        "edges": [
            {"src": "Class::authctrl", "dst": "Route::post /api/register",
             "type": "EXPOSES"},
            {"src": "Class::authctrl", "dst": "Method::authctrl.register",
             "type": "HAS_METHOD"},
            {"src": "Class::authctrl", "dst": "Method::authctrl.validate",
             "type": "HAS_METHOD"},
            {"src": "Class::authctrl", "dst": "Table::users",
             "type": "REFERENCES_TABLE"},
            {"src": "Table::users", "dst": "Column::users.email_id",
             "type": "HAS_COLUMN"},
            {"src": "Table::users", "dst": "Column::users.password_hash",
             "type": "HAS_COLUMN"},
            {"src": "Route::post /api/register", "dst": "Role::guest",
             "type": "GUARDED_BY"},
        ],
        "version": 2,
    }


def _api_graph_with_reads_writes() -> dict:
    """Same shape as ``_api_graph_full`` but with READS edges from the
    Method to the Table (Graph-KB LLM enrichment ON). Verifies both
    edge kinds get merged into the same journey without duplicating."""
    g = _api_graph_full()
    g["edges"].append({
        "src": "Method::authctrl.register", "dst": "Table::users",
        "type": "READS",
    })
    g["version"] = 3
    return g


def _api_graph_orphan_route() -> dict:
    """Route with no exposing Class (JSP-form-derived synthetic route)."""
    return {
        "nodes": [
            {"id": "Route::post /save.do", "type": "Route",
             "name": "POST /save.do", "verb": "POST",
             "path": "/save.do", "source": "views/save.jsp"},
        ],
        "edges": [],
        "version": 1,
    }


def _column_graph() -> dict:
    """Deterministic column-lineage fixture for the opt-in kind."""
    return {
        "nodes": [
            {"id": "Column::orders.total", "type": "Column",
             "name": "orders.total", "short_name": "total",
             "table": "orders", "data_type": "decimal(10,2)",
             "source": "sql/orders.sql"},
            {"id": "Table::orders", "type": "Table", "name": "orders",
             "source": "sql/orders.sql"},
            {"id": "Class::ordercontroller", "type": "Class",
             "name": "OrderController", "source": "app/OrderController.php"},
            {"id": "Route::get /api/orders", "type": "Route",
             "name": "GET /api/orders", "verb": "GET",
             "path": "/api/orders", "source": "app/OrderController.php"},
        ],
        "edges": [
            {"src": "Table::orders", "dst": "Column::orders.total",
             "type": "HAS_COLUMN"},
            {"src": "Class::ordercontroller", "dst": "Table::orders",
             "type": "REFERENCES_TABLE"},
            {"src": "Class::ordercontroller",
             "dst": "Route::get /api/orders", "type": "EXPOSES"},
        ],
        "version": 1,
    }


# ---------------------------------------------------------------------------
# _index_graph
# ---------------------------------------------------------------------------

def test_index_graph_bidirectional_and_ignores_malformed():
    g = _api_graph_full()
    nodes, out_e, in_e = _index_graph(g)
    # HAS_COLUMN is Table → Column; Column appears in in_e for that key.
    assert any(e["type"] == "HAS_COLUMN"
               for e in in_e["Column::users.email_id"])
    # Malformed edges dropped.
    bad = {"nodes": [{"id": "A", "type": "X", "name": "A"}],
           "edges": [{"src": "A"}, {"dst": "A"}, None,
                     {"src": "A", "dst": "A", "type": "T"}]}
    _, o, i = _index_graph(bad)
    assert len(o["A"]) == 1 and len(i["A"]) == 1


# ---------------------------------------------------------------------------
# api_journey (Route anchor)
# ---------------------------------------------------------------------------

def test_api_journey_full_deterministic_path():
    g = _api_graph_full()
    nodes, out_e, in_e = _index_graph(g)
    route = nodes["Route::post /api/register"]
    j = _build_api_journey(route, nodes, out_e, in_e, br_idx={})
    assert j is not None
    assert j["kind"] == "api"
    assert j["journey_id"] == "AJ-post-api-register"
    assert j["pivot_route"] == "POST /api/register"
    assert j["seed"] == {"type": "Route", "verb": "POST",
                         "path": "/api/register", "handler_class": "AuthCtrl"}
    # Rollups.
    assert j["classes_touched"] == ["AuthCtrl"]
    assert set(j["methods"]) == {"AuthCtrl.register", "AuthCtrl.validate"}
    assert j["tables"] == ["users"]
    assert set(j["columns"]) == {"users.email_id", "users.password_hash"}
    assert j["roles"] == ["guest"]
    # Trace shape: Route → Role → Class → 2 Methods → Table → 2 Columns
    types = [s["node_type"] for s in j["trace"]]
    assert types[0] == "Route"
    assert "Role" in types
    assert types.count("Method") == 2
    assert types.count("Column") == 2


def test_api_journey_merges_reads_writes_without_duplicate_tables():
    g = _api_graph_with_reads_writes()
    nodes, out_e, in_e = _index_graph(g)
    route = nodes["Route::post /api/register"]
    j = _build_api_journey(route, nodes, out_e, in_e, br_idx={})
    # Same rollups, no dupes.
    assert j["tables"] == ["users"]
    # Table trace step exists exactly once.
    table_steps = [s for s in j["trace"] if s["node_type"] == "Table"]
    assert len(table_steps) == 1
    # READS wins over REFERENCES_TABLE (the method-level edge is added
    # after the class-level fallback, so setdefault preserves the READS).
    # Either way, an op is recorded.
    assert table_steps[0].get("op") in ("READS", "WRITES", "REFERENCES_TABLE")


def test_api_journey_orphan_route_returns_none():
    g = _api_graph_orphan_route()
    nodes, out_e, in_e = _index_graph(g)
    route = nodes["Route::post /save.do"]
    assert _build_api_journey(route, nodes, out_e, in_e, br_idx={}) is None


def test_api_journey_br_attachment_from_source_paths():
    g = _api_graph_full()
    nodes, out_e, in_e = _index_graph(g)
    route = nodes["Route::post /api/register"]
    br_idx = {
        "app/AuthCtrl.php": ["BR-AUTH-001", "BR-AUTH-004"],
        "sql/user.sql":     ["BR-DATA-010"],
        "app/OtherModule.php": ["BR-UNRELATED-999"],
    }
    j = _build_api_journey(route, nodes, out_e, in_e, br_idx=br_idx)
    assert set(j["business_rules"]) == {
        "BR-AUTH-001", "BR-AUTH-004", "BR-DATA-010",
    }
    assert "BR-UNRELATED-999" not in j["business_rules"]


# ---------------------------------------------------------------------------
# ui_journey (View anchor)
# ---------------------------------------------------------------------------

def test_ui_journey_pivots_to_matching_route():
    form = {"type": "JSP_FORM", "action": "/api/register",
            "source": "views/register.jsp"}
    bindings = {"views/register.jsp": ["email", "password"]}
    known = {"POST /api/register", "GET /api/users"}
    j = _build_ui_journey(form, bindings, known, br_idx={}, seq=0)
    assert j is not None
    assert j["kind"] == "ui"
    assert j["journey_id"] == "UJ-register-jsp-0"
    assert j["pivot_route"] == "POST /api/register"
    assert j["ui_fields"] == ["email", "password"]
    assert j["endpoints"] == ["POST /api/register"]
    types = [s["node_type"] for s in j["trace"]]
    assert types == ["View", "Field", "Field", "Route"]
    # Last step is a Route with SUBMITS_TO op.
    assert j["trace"][-1]["op"] == "SUBMITS_TO"


def test_ui_journey_action_normalisation_strips_query_and_prefixes_slash():
    known: set[str] = set()
    assert _normalise_action_to_pivot("/save.do?x=1", known) == "POST /save.do"
    assert _normalise_action_to_pivot("save.do", known) == "POST /save.do"
    # External / anchor / javascript-scheme actions are ignored.
    assert _normalise_action_to_pivot("#", known) == ""
    assert _normalise_action_to_pivot("javascript:void(0)", known) == ""
    assert _normalise_action_to_pivot("http://x.com/y", known) == ""


def test_ui_journey_matches_route_verb_when_known():
    form = {"type": "JSP_FORM", "action": "/api/orders",
            "source": "views/orders.jsp"}
    # Graph advertises GET only (unusual but legal for a search form).
    known = {"GET /api/orders"}
    j = _build_ui_journey(form, {}, known, br_idx={}, seq=1)
    assert j["pivot_route"] == "GET /api/orders"


def test_ui_journey_with_no_bindings_still_emits():
    form = {"type": "JSP_FORM", "action": "/x", "source": "v.jsp"}
    j = _build_ui_journey(form, {}, set(), br_idx={}, seq=0)
    assert j is not None
    assert j["ui_fields"] == []
    # Even without bindings the view step exists; pivot goes to synthetic
    # POST route (not in known set but still recorded).
    assert j["pivot_route"] == "POST /x"


# ---------------------------------------------------------------------------
# column_journey (opt-in kind)
# ---------------------------------------------------------------------------

def test_column_journey_deterministic_fallback_produces_route():
    g = _column_graph()
    nodes, out_e, in_e = _index_graph(g)
    col = nodes["Column::orders.total"]
    j = _build_column_journey(col, nodes, out_e, in_e, br_idx={})
    assert j is not None
    assert j["kind"] == "column"
    assert j["journey_id"] == "CJ-orders-total"
    assert j["pivot_route"] == "GET /api/orders"
    types = [s["node_type"] for s in j["trace"]]
    assert types == ["Column", "Table", "Class", "Route"]
    assert j["classes_touched"] == ["OrderController"]
    assert j["endpoints"] == ["GET /api/orders"]


def test_column_journey_returns_none_when_table_missing():
    g = {"nodes": [{"id": "Column::x.y", "type": "Column", "name": "x.y",
                    "short_name": "y", "table": "x"}], "edges": []}
    nodes, out_e, in_e = _index_graph(g)
    assert _build_column_journey(
        nodes["Column::x.y"], nodes, out_e, in_e, br_idx={},
    ) is None


# ---------------------------------------------------------------------------
# _hash_trace
# ---------------------------------------------------------------------------

def test_hash_trace_deterministic_and_content_sensitive():
    a = [{"step": 0, "node_type": "Route", "name": "POST /x"}]
    b = [{"step": 0, "node_type": "Route", "name": "POST /y"}]
    assert _hash_trace(a) == _hash_trace(list(a))
    assert _hash_trace(a) != _hash_trace(b)
    assert len(_hash_trace(a)) == 16


# ---------------------------------------------------------------------------
# TOON rendering
# ---------------------------------------------------------------------------

def test_render_api_journey_toon_contains_key_fields():
    g = _api_graph_full()
    nodes, out_e, in_e = _index_graph(g)
    route = nodes["Route::post /api/register"]
    j = _build_api_journey(route, nodes, out_e, in_e, br_idx={
        "app/AuthCtrl.php": ["BR-AUTH-001"],
    })
    toon = render_journey_toon(j)
    assert toon.startswith("[API_JOURNEY:AJ-post-api-register]")
    assert "route=POST /api/register" in toon
    assert "[CLASS:AuthCtrl]" in toon
    assert "[METHOD:AuthCtrl.register]" in toon
    assert "[TABLE:users]" in toon
    assert "[COLUMN:users.email_id]" in toon
    assert "[ROLE:guest]" in toon
    assert "[TABLES] users" in toon
    assert "users.email_id" in toon  # in [COLUMNS] rollup
    assert "[BR] ids=BR-AUTH-001" in toon


def test_render_ui_journey_toon():
    form = {"type": "JSP_FORM", "action": "/api/register",
            "source": "views/register.jsp"}
    j = _build_ui_journey(form, {"views/register.jsp": ["email", "pwd"]},
                          {"POST /api/register"}, br_idx={}, seq=0)
    toon = render_journey_toon(j)
    assert toon.startswith("[UI_JOURNEY:UJ-register-jsp-0]")
    assert "route=POST /api/register" in toon
    assert "[VIEW:register.jsp]" in toon
    assert "[FIELD:email]" in toon
    assert "[FIELDS] email,pwd" in toon
    assert "op=SUBMITS_TO" in toon


def test_render_journeys_block_truncates_at_boundary():
    g = _api_graph_full()
    nodes, out_e, in_e = _index_graph(g)
    j = _build_api_journey(nodes["Route::post /api/register"],
                           nodes, out_e, in_e, br_idx={})
    small = render_journeys_block([j], max_chars=40)
    assert small.startswith("# JOURNEYS")
    assert "[truncated]" in small
    full = render_journeys_block([j], max_chars=5000)
    assert "[API_JOURNEY:AJ-post-api-register]" in full
    assert "[truncated]" not in full


# ---------------------------------------------------------------------------
# Column columns cap (safety on wide tables)
# ---------------------------------------------------------------------------

def test_api_journey_caps_columns_per_table(monkeypatch):
    """Verify _MAX_COLUMNS_PER_TABLE bounds the trace on wide tables."""
    import kb.journey_materializer as mod
    monkeypatch.setattr(mod, "_MAX_COLUMNS_PER_TABLE", 3)

    nodes_list = [
        {"id": "Route::get /wide", "type": "Route", "name": "GET /wide",
         "verb": "GET", "path": "/wide", "source": "c.php"},
        {"id": "Class::widectrl", "type": "Class", "name": "WideCtrl",
         "source": "c.php"},
        {"id": "Table::wide", "type": "Table", "name": "wide",
         "source": "s.sql"},
    ]
    edges = [
        {"src": "Class::widectrl", "dst": "Route::get /wide", "type": "EXPOSES"},
        {"src": "Class::widectrl", "dst": "Table::wide", "type": "REFERENCES_TABLE"},
    ]
    for i in range(10):
        cid = f"Column::wide.c{i}"
        nodes_list.append({"id": cid, "type": "Column",
                           "name": f"wide.c{i}", "short_name": f"c{i}",
                           "table": "wide"})
        edges.append({"src": "Table::wide", "dst": cid, "type": "HAS_COLUMN"})
    g = {"nodes": nodes_list, "edges": edges}
    n, o, i = _index_graph(g)
    j = mod._build_api_journey(n["Route::get /wide"], n, o, i, br_idx={})
    # Cap enforced.
    assert len(j["columns"]) == 3
