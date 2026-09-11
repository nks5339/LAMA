"""iter-13.91.16 — Tests for the unified host-anchored context bundler.

These tests verify pure-function behaviour that doesn't need Mongo:
  • Target-stack convention lookup (exact + fuzzy + fallback)
  • Local-FS cache key invariance
  • render_for_prompt formatting
  • Cache read/write round-trip
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import context_bundler as cb  # noqa: E402


def test_conventions_lookup_exact_match():
    out = cb._conventions_for("FastAPI/Python/PostgreSQL")
    assert "FastAPI" in out
    assert "Pydantic" in out
    assert "alembic" in out.lower()


def test_conventions_lookup_fuzzy_match():
    # Operator writes a longer label than the canonical key — fuzzy
    # contains match should still resolve to the FastAPI preset.
    out = cb._conventions_for("FastAPI / Python 3.12 / Postgres 16 with asyncpg")
    assert "FastAPI" in out
    assert "Pydantic" in out


def test_conventions_lookup_unknown_returns_default():
    out = cb._conventions_for("MyCustomFramework/SomeLang/WeirdDB")
    assert "No target-stack-specific conventions" in out


def test_conventions_lookup_empty_returns_default():
    assert cb._conventions_for("") == cb._DEFAULT_CONVENTIONS
    assert cb._conventions_for(None) == cb._DEFAULT_CONVENTIONS  # type: ignore[arg-type]


def test_conventions_covers_major_stacks():
    """Every entry in _STACK_CONVENTIONS resolves to itself when looked up."""
    for key in cb._STACK_CONVENTIONS:
        out = cb._conventions_for(key)
        assert out == cb._STACK_CONVENTIONS[key], key


def test_cache_key_is_stable_for_same_inputs():
    k1 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="fastapi")
    k2 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="fastapi")
    assert k1 == k2


def test_cache_key_changes_when_kb_version_changes():
    k1 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="fastapi")
    k2 = cb._cache_key("p1", "datamodel", kb_version="43", srs_version="3", target_tech="fastapi")
    assert k1 != k2


def test_cache_key_changes_when_target_tech_changes():
    k1 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="fastapi")
    k2 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="spring boot")
    assert k1 != k2


def test_cache_key_changes_per_stage():
    k1 = cb._cache_key("p1", "datamodel", kb_version="42", srs_version="3", target_tech="x")
    k2 = cb._cache_key("p1", "architecture", kb_version="42", srs_version="3", target_tech="x")
    assert k1 != k2


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "LAMA_DATA_DIR", str(tmp_path))
    cache_path = cb._cache_path("proj-A", "datamodel", "abc123")
    assert cache_path  # cache dir was creatable
    payload = {"foo": "bar", "_meta": {"char_count": 9}}
    cb._write_cache(cache_path, payload)
    got = cb._read_cache(cache_path)
    assert got == payload


def test_cache_returns_none_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "LAMA_DATA_DIR", str(tmp_path))
    cache_path = cb._cache_path("proj-X", "codegen", "deadbeef")
    assert cb._read_cache(cache_path) is None


def test_cache_returns_none_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "LAMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cb, "CONTEXT_CACHE_TTL_SEC", 1)
    cache_path = cb._cache_path("proj-A", "datamodel", "abc999")
    cb._write_cache(cache_path, {"x": 1})
    # Touch mtime backwards beyond the TTL window.
    old = os.path.getmtime(cache_path) - 10
    os.utime(cache_path, (old, old))
    assert cb._read_cache(cache_path) is None


def test_render_for_prompt_empty_bundle():
    assert cb.render_for_prompt({}) == ""
    assert cb.render_for_prompt(None) == ""  # type: ignore[arg-type]


def test_render_for_prompt_includes_all_sections():
    bundle = {
        "project": {"id": "p1", "name": "PMIS", "tenant_id": "t1",
                    "target_tech": "FastAPI/Python/PostgreSQL", "legacy_tech": "PHP/CI4"},
        "target_conventions": "FastAPI is great",
        "kb": {"files": 12, "entities": 87},
        "toon": "CLASSES:\nUser id:int\nROUTES:\nGET /users",
        "srs": {"functional_requirements": "User can log in."},
        "datamodel": {"oltp_ddl": "CREATE TABLE users (...);"},
        "architecture": {"services": [{"name": "auth"}]},
        "integrations": [{"integration_id": "pan", "name": "PAN Lookup"}],
        "previous_stages": {"datamodel": {"version": 1}},
    }
    out = cb.render_for_prompt(bundle)
    # Header / footer markers.
    assert "LAMA STAGE CONTEXT" in out
    assert "END LAMA STAGE CONTEXT" in out
    # Each section block label is present.
    for label in (
        "TARGET-STACK CONVENTIONS",
        "KB SUMMARY",
        "KB TOON",
        "SRS SECTIONS",
        "DATA MODEL",
        "ARCHITECTURE",
        "INTEGRATIONS",
        "FROZEN UPSTREAM STAGES",
    ):
        assert label in out, label
    # Legacy + target stack inline.
    assert "PHP/CI4" in out
    assert "FastAPI/Python/PostgreSQL" in out


def test_render_for_prompt_skips_missing_sections():
    bundle = {
        "project": {"id": "p1", "name": "x", "tenant_id": "t1"},
        "kb": {"files": 0, "entities": 0},
        "target_conventions": "default",
    }
    out = cb.render_for_prompt(bundle)
    assert "KB SUMMARY" in out
    # Stages that aren't in bundle must NOT appear as section headers.
    for label in ("SRS SECTIONS", "DATA MODEL", "ARCHITECTURE", "INTEGRATIONS"):
        assert label not in out, label


def test_stage_keys_constant_unchanged():
    """Stage names are part of the public contract — they're used by
    the /api/context/{pid}/{stage} route and the freeze gates.
    Changing one breaks every downstream caller."""
    assert cb.STAGE_KEYS == (
        "discovery", "datamodel", "architecture", "codegen", "living",
    )

