"""iter-14.12 — context/token efficiency guardrails.

Locks in three optimisations shipped in this iteration:

  1. `kb.toon.prune` collapses non-hot sections to "names only", saving
     ~50% chars on the TOON payload when the stage doesn't need the
     other slices.
  2. `context_bundler.render_for_prompt` emits COMPACT JSON (no indent)
     for structured sub-payloads. Indented JSON costs ~25-30% more
     tokens on the same data — pure waste for LLM consumption.
  3. `llm.estimate_tokens` prefers `tiktoken` when available for a
     materially more accurate count vs `len(text)//4`. Falls back to
     the heuristic when tiktoken isn't installed.

Every LLM/DB side-effect is stubbed. No real IO.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test_iter1412")

if "motor" not in sys.modules:
    _motor = types.ModuleType("motor")
    _motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class _FakeMongoClient:
        def __init__(self, *a, **kw): pass
        def __getitem__(self, _n): return _FakeDB()

    class _FakeDB:
        def __getattr__(self, _n): return _FakeCollProxy()

    class _FakeCollProxy:
        def __getattr__(self, _n): return _FakeCollProxy()
        def __call__(self, *a, **kw): return self

    _motor_asyncio.AsyncIOMotorClient = _FakeMongoClient
    sys.modules["motor"] = _motor
    sys.modules["motor.motor_asyncio"] = _motor_asyncio


# ---------------------------------------------------------------------------
# 1. kb.toon.prune — stage-aware "names only" demotion.
# ---------------------------------------------------------------------------

def _sample_toon(class_count: int = 40, table_count: int = 40) -> str:
    """Build a synthetic TOON blob with header + detail rows for both
    CLASSES and TABLES so we can prove the non-hot slice collapses."""
    lines = ["# CLASSES"]
    for i in range(class_count):
        lines.append(f"[CLASS:C{i}] pkg=App.Controllers")
        lines.append(f"  [METHOD:m{i}] db=t{i % 5}")
        lines.append(f"  [METHOD:m{i}_extra] auth=session(admin_detail)")
    lines.append("# TABLES")
    for i in range(table_count):
        lines.append(f"[TABLE:t{i}] pk=id")
        for c in range(6):
            lines.append(f"  [COL:col{c}] type=varchar")
    return "\n".join(lines)


def test_prune_discovery_demotes_tables_to_names_only():
    from kb.toon import prune

    # Small CLASSES (hot) + large TABLES (should demote).
    full = _sample_toon(class_count=5, table_count=40)
    # Budget: enough for CLASSES + demoted TABLES, but < full size.
    pruned = prune(full, max_chars=int(len(full) * 0.6), stage="discovery")

    # The hot slice (CLASSES) MUST still carry its indented [METHOD:...] lines.
    assert "[METHOD:m0]" in pruned
    # The non-hot slice header MUST be marked "(names only)".
    assert "# TABLES (names only)" in pruned
    # …AND every [COL:…] detail row MUST have been dropped for TABLES.
    assert "[COL:col0]" not in pruned
    # …but the TABLE header rows themselves are still there.
    assert "[TABLE:t0]" in pruned


def test_prune_datamodel_demotes_classes_to_names_only():
    from kb.toon import prune

    # Small TABLES (hot for datamodel) + large CLASSES (should demote).
    full = _sample_toon(class_count=40, table_count=5)
    pruned = prune(full, max_chars=int(len(full) * 0.6), stage="datamodel")

    # TABLES is the hot slice and MUST keep its column detail.
    assert "[COL:col0]" in pruned
    # CLASSES header MUST be marked "(names only)".
    assert "# CLASSES (names only)" in pruned
    # …and per-class method detail rows are dropped.
    assert "[METHOD:m0]" not in pruned
    # …but class header rows themselves survive.
    assert "[CLASS:C0]" in pruned


def test_prune_passthrough_when_under_budget():
    from kb.toon import prune

    full = _sample_toon(class_count=2, table_count=2)
    # Ample budget → identity.
    assert prune(full, max_chars=1_000_000, stage="discovery") == full


def test_prune_empty_input_is_safe():
    from kb.toon import prune

    assert prune("", 100, "discovery") == ""
    assert prune("", 100, "anything") == ""


# ---------------------------------------------------------------------------
# 2. routes.chat.prune_toon is a thin re-export of kb.toon.prune.
# ---------------------------------------------------------------------------

def test_routes_chat_prune_toon_delegates_to_kb_toon():
    # Only import when needed — routes.chat pulls db.
    from routes.chat import prune_toon as chat_prune
    from kb.toon import prune as canonical_prune

    full = _sample_toon(class_count=10, table_count=10)
    budget = len(full) // 4
    assert chat_prune(full, budget, "discovery") == canonical_prune(full, budget, "discovery")


# ---------------------------------------------------------------------------
# 3. context_bundler.render_for_prompt uses COMPACT JSON (no indent).
# ---------------------------------------------------------------------------

def test_render_for_prompt_uses_compact_json():
    import context_bundler as cb

    bundle = {
        "project": {"id": "p1", "name": "PMIS", "tenant_id": "t1"},
        "target_conventions": "FastAPI conventions here",
        "kb": {"files": 12, "entities": 87},
        "srs": {"actors_use_case_inventory": "Users can log in.",
                "functional_requirements": "FR-001, FR-002"},
        "datamodel": {"oltp_tables": ["users", "orders", "items"]},
        "integrations": [{"integration_id": "pan", "name": "PAN Lookup"}],
    }

    out = cb.render_for_prompt(bundle)

    # Compact JSON has no 2-space-indented `\n  "key":` sequence anywhere.
    assert '\n  "' not in out, (
        "render_for_prompt is emitting indented JSON — bloats every prompt "
        "by ~25-30% tokens vs compact serialisation"
    )
    # Compact JSON uses `key":"value"` with no space after the colon.
    assert '"files":12' in out
    assert '"integration_id":"pan"' in out
    # And every label header is still present (accuracy contract).
    for label in ("KB SUMMARY", "SRS SECTIONS", "DATA MODEL", "INTEGRATIONS"):
        assert label in out, label


def test_render_for_prompt_saves_meaningful_tokens_vs_indented():
    """Sanity-check: compact JSON is materially shorter than indent=2."""
    import context_bundler as cb
    import json

    payload = {
        "sections": {f"s{i}": {"body": "x" * 40, "score": i} for i in range(30)},
    }
    bundle = {
        "project": {"id": "p1", "name": "PMIS", "tenant_id": "t1"},
        "srs": payload,
    }
    compact_out = cb.render_for_prompt(bundle)

    indented = json.dumps(payload, ensure_ascii=False, indent=2)
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # We must be shorter than a version that used indented JSON for the
    # sub-payload (the actual byte-count of the bundle is what LLMs pay for).
    naive_size = compact_out.replace(compact, indented)
    assert len(compact_out) < len(naive_size)
    # And the delta should be at least 10% — indent=2 on a 30-item dict
    # inflates by roughly 30-40%. 10% is a conservative floor.
    assert (len(naive_size) - len(compact_out)) / len(naive_size) > 0.10


# ---------------------------------------------------------------------------
# 4. llm.estimate_tokens tokeniser fallback is well-behaved.
# ---------------------------------------------------------------------------

def test_estimate_tokens_returns_positive_int_for_all_inputs():
    from llm import estimate_tokens

    assert estimate_tokens("") == 1              # floor
    assert estimate_tokens("hello world") >= 1
    # Reasonable ballpark: 500-char English text tokenises to ~90-130 tokens
    # with cl100k_base. Anything absurdly low/high signals a broken tokeniser.
    n = estimate_tokens("hello world " * 50)
    assert 50 < n < 500


def test_estimate_tokens_returns_positive_int_for_all_inputs():
    from llm import estimate_tokens

    assert estimate_tokens("") == 1              # floor
    assert estimate_tokens("hello world") >= 1
    # Reasonable ballpark: 500-char English text tokenises to ~90-130 tokens
    # with cl100k_base. Anything absurdly low/high signals a broken tokeniser.
    n = estimate_tokens("hello world " * 50)
    assert 50 < n < 500


def test_estimate_tokens_falls_back_to_heuristic_when_tiktoken_missing(monkeypatch):
    """Force the encoder to be unavailable and confirm we still return
    the legacy `len//4` estimate. This guards operators running LAMA in
    stripped-down environments (no tiktoken wheel available) — the
    tokeniser must NEVER be a hard dependency of the prompt path.
    """
    import llm

    # Reset the cache and neuter tiktoken.
    monkeypatch.setattr(llm, "_TIKTOKEN_ENCODER", None)
    monkeypatch.setattr(llm, "_TIKTOKEN_ENCODER_LOADED", True)  # already tried

    text = "a" * 400
    assert llm.estimate_tokens(text) == 100  # 400 // 4


# ---------------------------------------------------------------------------
# 5. iter-14.13 — extract_toon_section is memoised across sections.
# ---------------------------------------------------------------------------

def test_extract_toon_section_is_cached_across_repeated_calls(monkeypatch):
    """`_gen_one_section` calls `extract_toon_section` up to 4× per section
    × 12 sections = 48 splits of the same TOON blob per SRS run. The
    memoisation must make identical (toon, section, budget) tuples O(1)
    after the first hit."""
    # routes.srs pulls db + qdrant on import; stub the ambient env just enough
    # to allow the import.
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")

    from routes.srs import extract_toon_section

    # Clear cache — some previous test could have populated it.
    extract_toon_section.cache_clear()

    toon = _sample_toon(class_count=20, table_count=20)

    # First call — miss.
    out1 = extract_toon_section(toon, "CLASSES", 8000)
    info1 = extract_toon_section.cache_info()
    assert info1.misses == 1
    assert info1.hits == 0

    # Same tuple 5 more times — every one MUST hit the cache.
    for _ in range(5):
        assert extract_toon_section(toon, "CLASSES", 8000) == out1
    info2 = extract_toon_section.cache_info()
    assert info2.hits == 5
    assert info2.misses == 1

    # Different section — miss.
    _ = extract_toon_section(toon, "TABLES", 8000)
    info3 = extract_toon_section.cache_info()
    assert info3.misses == 2


# ---------------------------------------------------------------------------
# 6. iter-14.13 — _datamodel_summary caps OLTP DDL at env-configurable size.
# ---------------------------------------------------------------------------

def test_datamodel_summary_truncates_large_ddl_at_stmt_boundary(monkeypatch):
    """Very large OLTP DDLs must be truncated in the bundle. Truncation
    happens at a CREATE-statement boundary so we don't leave a half-open
    parenthesis in the LLM prompt."""
    import asyncio
    import context_bundler as cb

    # Build a 200 KB DDL of 100 CREATE TABLE stmts. Each stmt is ~2 KB.
    stmt = (
        "CREATE TABLE {name} (\n"
        "    id INT PRIMARY KEY,\n"
        + ",\n".join(f"    col{c} VARCHAR(255)" for c in range(30))
        + "\n);\n\n"
    )
    ddl = "".join(stmt.format(name=f"t{i}") for i in range(100))
    assert len(ddl) > 60_000

    # Stub Mongo lookups.
    async def _fake_oltp_findone(_filt, _proj):
        return {"ddl": ddl, "tables": ["t0", "t1"], "version": 3}

    async def _fake_olap_findone(_filt, _proj):
        return {}

    async def _fake_bus_findone(_filt, _proj):
        return {"matrix": []}

    class _F:
        find_one = staticmethod(_fake_oltp_findone)
    class _F2:
        find_one = staticmethod(_fake_olap_findone)
    class _F3:
        find_one = staticmethod(_fake_bus_findone)

    monkeypatch.setattr(cb, "data_models", _F)
    monkeypatch.setattr(cb, "olap_models", _F2)
    monkeypatch.setattr(cb, "bus_matrix", _F3)
    monkeypatch.setenv("LAMA_OLTP_DDL_MAX_CHARS", "60000")

    out = asyncio.run(
        cb._datamodel_summary("p1")
    )

    truncated = out["oltp_ddl"]
    # Bounded by the cap (+ some slack for the truncation footer).
    assert 40_000 < len(truncated) < 62_000
    # Ends with the truncation marker.
    assert "truncated" in truncated
    # Ends at a statement boundary — no half-open parenthesis.
    assert truncated.count("(") == truncated.count(")"), (
        "OLTP DDL truncation left the prompt with unbalanced parentheses — "
        "the LLM will hallucinate the missing schema"
    )


def test_datamodel_summary_passthrough_when_under_cap(monkeypatch):
    """Small DDLs must be returned verbatim — no truncation footer."""
    import asyncio
    import context_bundler as cb

    small_ddl = "CREATE TABLE users (id INT PRIMARY KEY);\n"

    async def _fake_oltp_findone(_filt, _proj):
        return {"ddl": small_ddl, "tables": ["users"], "version": 1}

    async def _fake_olap_findone(_filt, _proj):
        return {}

    async def _fake_bus_findone(_filt, _proj):
        return {"matrix": []}

    class _F:
        find_one = staticmethod(_fake_oltp_findone)
    class _F2:
        find_one = staticmethod(_fake_olap_findone)
    class _F3:
        find_one = staticmethod(_fake_bus_findone)

    monkeypatch.setattr(cb, "data_models", _F)
    monkeypatch.setattr(cb, "olap_models", _F2)
    monkeypatch.setattr(cb, "bus_matrix", _F3)

    out = asyncio.run(
        cb._datamodel_summary("p1")
    )

    assert out["oltp_ddl"] == small_ddl
    assert "truncated" not in out["oltp_ddl"]

