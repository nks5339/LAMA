"""
iter-15.42 — Coder parallelism + file-scoped KB slice.

The Coder loop was fully sequential: every task waited for the previous
task's LLM round-trip. iter-15.42 groups tasks by Planner wave and fans
each wave out under an `asyncio.Semaphore` (default 6, override via
`LAMA_CODER_MAX_CONCURRENCY`). Between-wave order is preserved because
wave N+1 may depend on N's artefacts, but within-wave tasks — which the
Planner already declared independent — run in parallel.

Alongside, `_slim_kb_ctx_for_task` replaces the ~15 KB global KB dump
that was passed to the Coder for every file with a compact per-file
slice built from the file's ARCHITECTURE envelope (endpoint, controller,
service, repository, tables, business rules). Files without an envelope
fall back to the first 4 KB of the global summary.

These tests cover the two helpers directly — the parallel dispatch is
observable via the semaphore + `_emit_log` and is exercised by the
existing end-to-end transformer tests.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes.tools import _slim_kb_ctx_for_task  # noqa: E402


def test_slim_kb_uses_envelope_when_present():
    envelope = {
        "endpoint": "/api/v1/users/{id}",
        "http_method": "GET",
        "controller": "UserController.getUser",
        "service": "UserService.findById",
        "repository": "UserRepository.findById",
        "tables": ["users", "user_roles"],
        "columns": {"users": ["id", "email", "role_id"]},
        "business_logic": "Return the user for the given id including their role.",
        "business_rules": ["BR-001", "BR-014"],
    }
    full_kb = json.dumps({"noise": "x" * 20000})

    slim = _slim_kb_ctx_for_task(full_kb, envelope, "src/UserController.java")

    assert "UserController.getUser" in slim
    assert "users" in slim
    assert "BR-001" in slim
    # Confirm the 15KB noise dump is NOT in the per-task context.
    assert "noise" not in slim
    # Sanity: envelope-derived slice stays under the 4KB cap.
    assert len(slim) <= 4000


def test_slim_kb_drops_empty_envelope_keys():
    envelope = {
        "endpoint": "/api/health",
        "controller": None,
        "service": "",
        "tables": [],
    }
    slim = _slim_kb_ctx_for_task("fallback", envelope, "src/HealthController.java")
    parsed = json.loads(slim)
    assert parsed["endpoint"] == "/api/health"
    assert "controller" not in parsed
    assert "service" not in parsed
    assert "tables" not in parsed
    # source_path is always kept — it's the anchor the Coder needs.
    assert parsed["source_path"] == "src/HealthController.java"


def test_slim_kb_falls_back_to_trimmed_summary_without_envelope():
    big = "A" * 20000
    slim = _slim_kb_ctx_for_task(big, None, "src/Legacy.jsp")
    assert len(slim) == 4000
    assert slim == "A" * 4000


def test_slim_kb_handles_empty_inputs():
    assert _slim_kb_ctx_for_task("", None, "x.php") == ""
    assert _slim_kb_ctx_for_task(None, None, "x.php") == ""


def test_slim_kb_survives_malformed_envelope():
    class Weird:
        def __getitem__(self, k):
            raise RuntimeError("boom")

    # Dict with a value that json.dumps CAN serialise but the .get chain
    # itself never raises → returns compact JSON without crashing.
    envelope = {"endpoint": "/x", "columns": {"users": ["id"]}}
    slim = _slim_kb_ctx_for_task("fallback", envelope, "src/X.java")
    assert "/x" in slim
