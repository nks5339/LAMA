"""iter-13.91.14 — unit tests for the new wake / start / diagnostic
helpers added to backend/factory_orchestrator.py.

These tests use httpx.MockTransport so they exercise the real code
paths without touching Factory's API. They verify:

  1. `_try_start_computer` stops probing on the first 2xx response.
  2. `_try_start_computer` returns False when every candidate is 4xx.
  3. `_get_computer_diagnostic` extracts only known status fields and
     handles JSON-decode failures gracefully.
  4. `_wake_computer(force=True)` fires `_try_start_computer` ONLY when
     the GET response body indicates a sleeping state.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from factory_orchestrator import (  # noqa: E402
    _get_computer_diagnostic,
    _try_start_computer,
    _wake_computer,
    _parse_problem_detail,
    _classify_424_detail,
    _cleanup_stale_sessions,
    _LAST_WAKE_AT,
)


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="https://api.factory.test")


HEADERS = {"Authorization": "Bearer test", "Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_try_start_computer_returns_true_on_first_2xx():
    calls: List[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        # First two endpoints 404, third (`/wake`) succeeds.
        if req.url.path.endswith("/wake"):
            return httpx.Response(202, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            ok = await _try_start_computer(client, HEADERS, "cid-123")
    assert ok is True
    # Should have probed at most 3 endpoints before getting the 202.
    assert any(c.endswith("/wake") for c in calls), calls
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_try_start_computer_returns_false_when_all_4xx():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            ok = await _try_start_computer(client, HEADERS, "cid-456")
    assert ok is False


@pytest.mark.asyncio
async def test_get_computer_diagnostic_extracts_known_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "cid-789",
            "status": "paused",
            "state": "idle",
            "last_seen": "2026-06-20T10:00:00Z",
            "region": "us-east-1",
            "irrelevant_field": "should be dropped",
        })

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            diag = await _get_computer_diagnostic(client, HEADERS, "cid-789")
    assert diag.get("status") == "paused"
    assert diag.get("state") == "idle"
    assert diag.get("region") == "us-east-1"
    assert "irrelevant_field" not in diag


@pytest.mark.asyncio
async def test_get_computer_diagnostic_handles_non_200():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not found")

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            diag = await _get_computer_diagnostic(client, HEADERS, "cid-x")
    assert diag.get("http_status") == 404
    assert "Not found" in diag.get("raw", "")


@pytest.mark.asyncio
async def test_wake_force_triggers_start_when_paused():
    """When GET reports `status=paused`, _wake_computer(force=True)
    must additionally call _try_start_computer (visible as POST hits)."""
    _LAST_WAKE_AT.clear()
    method_path: List[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        method_path.append(f"{req.method} {req.url.path}")
        # GET /computers/<id> → 200 with paused status
        if req.method == "GET":
            return httpx.Response(200, json={"id": "cid-p", "status": "paused"})
        # POST start endpoints → first one succeeds
        return httpx.Response(204)

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            ok = await _wake_computer(client, HEADERS, "cid-p", force=True)
    assert ok is True
    # Should have ONE GET and at least ONE POST start probe.
    gets = [c for c in method_path if c.startswith("GET")]
    posts = [c for c in method_path if c.startswith("POST")]
    assert len(gets) == 1
    assert len(posts) >= 1


@pytest.mark.asyncio
async def test_wake_force_skips_start_when_active():
    """When GET reports an active state, no start probes should fire."""
    _LAST_WAKE_AT.clear()
    method_path: List[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        method_path.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={"id": "cid-a", "status": "active"})

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            ok = await _wake_computer(client, HEADERS, "cid-a", force=True)
    assert ok is True
    posts = [c for c in method_path if c.startswith("POST")]
    assert posts == [], f"Expected no POST probes for active computer, got: {posts}"


# ─── iter-13.91.15 — new helpers ─────────────────────────────────────


def test_parse_problem_detail_extracts_rfc7807_fields():
    raw = json.dumps({
        "detail": "Computer disconnected during request",
        "status": 424,
        "title": "Failed Dependency",
        "requestId": "bom1::pmdcw-1782155712474-95f79298",
    })
    p = _parse_problem_detail(raw)
    assert p["detail"] == "Computer disconnected during request"
    assert p["status"] == 424
    assert p["title"] == "Failed Dependency"
    assert p["request_id"].startswith("bom1::pmdcw-")


def test_parse_problem_detail_handles_garbage():
    # Empty, non-JSON, and JSON-but-not-an-object all return {}.
    assert _parse_problem_detail("") == {}
    assert _parse_problem_detail("not json") == {}
    assert _parse_problem_detail("[1,2,3]") == {}


def test_classify_424_detail_distinguishes_subtypes():
    # The real production error → "droid_agent_unreachable", NOT "paused".
    assert _classify_424_detail("Computer disconnected during request") == "droid_agent_unreachable"
    # Generic disconnect wording also routes here.
    assert _classify_424_detail("computer was disconnected") == "droid_agent_unreachable"
    # Paused / stopped sub-types.
    assert _classify_424_detail("Computer is paused") == "droid_paused"
    assert _classify_424_detail("VM stopped") == "droid_paused"
    assert _classify_424_detail("hibernating") == "droid_paused"
    # Not found.
    assert _classify_424_detail("Computer not found") == "droid_not_found"
    # Fallback.
    assert _classify_424_detail("something weird") == "droid_disconnected"
    assert _classify_424_detail("") == "droid_disconnected"


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_lists_then_deletes():
    """When Factory's list endpoint returns sessions, _cleanup_stale_sessions
    should DELETE them and return the count of successful deletions."""
    method_path: List[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        method_path.append(f"{req.method} {req.url.path}{('?' + req.url.query.decode()) if req.url.query else ''}")
        if req.method == "GET" and "/sessions" in req.url.path:
            # First listing endpoint variant returns 2 sessions.
            return httpx.Response(200, json={"sessions": [
                {"sessionId": "sess-A"},
                {"sessionId": "sess-B"},
            ]})
        if req.method == "GET" and "/computers/" in req.url.path:
            return httpx.Response(404)
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            n = await _cleanup_stale_sessions(client, HEADERS, "cid-x")
    assert n == 2
    deletes = [c for c in method_path if c.startswith("DELETE")]
    assert any("sess-A" in c for c in deletes)
    assert any("sess-B" in c for c in deletes)


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_returns_zero_when_nothing_listed():
    """When every listing endpoint returns 404, _cleanup_stale_sessions
    must return 0 without raising."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            n = await _cleanup_stale_sessions(client, HEADERS, "cid-y")
    assert n == 0


@pytest.mark.asyncio
async def test_cleanup_stale_sessions_respects_max_delete():
    """Listing 10 sessions but max_delete=3 should delete exactly 3."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/sessions" in req.url.path:
            return httpx.Response(200, json={"sessions": [
                {"sessionId": f"sess-{i}"} for i in range(10)
            ]})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)
    async with _make_client(handler) as client:
        with patch("factory_orchestrator.FACTORY_API_BASE", "https://api.factory.test"):
            n = await _cleanup_stale_sessions(client, HEADERS, "cid-z", max_delete=3)
    assert n == 3


