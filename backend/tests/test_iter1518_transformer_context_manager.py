"""
iter-15.18 — Regression test for the Code Transformer "Discovered
Architecture" showing all-zero / generic-INFRA envelopes.

Root cause: `_run_context_manager()` in routes/tools.py relied entirely on
an LLM to invent endpoint/service/DB structure from raw source dumps, with
no deterministic ground truth. Weak/local models (e.g. Ollama llama3.1:8b)
would echo the prompt's placeholder JSON schema almost verbatim, producing
5 identical "INFRA" rows with 0 endpoints / 0 tables even for a real
codebase.

Fix: run the same deterministic owl_extractor-based KB builder the main
5-stage pipeline uses (`tools_kb_builder.build_tools_kb`) BEFORE the LLM
call, feed its digest into the prompt as ground truth, and fall back to
(or merge with) the deterministic envelopes whenever the LLM's output is
empty or degenerate.

These tests exercise the deterministic pieces directly (no network / LLM
calls, no Mongo) since `_run_context_manager` itself talks to Mongo
collections and `fabric_call`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# db.py reads MONGO_URL/DB_NAME at import time; these tests never touch
# Mongo (they only exercise pure deterministic extraction helpers), so a
# placeholder is enough — matches the pattern used by other test modules.
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from tools_kb_builder import build_tools_kb  # noqa: E402
from routes.tools import _deterministic_envelopes_from_kb  # noqa: E402

JAVA_CONTROLLER = """
package com.shpp.core.restclient;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/workflow/api/v1")
public class WorkflowController {

    @PostMapping("/transaction/initiateProcess")
    public String initiateProcess() {
        return "ok";
    }

    @PostMapping("/transaction/performTask")
    public String performTask() {
        return "ok";
    }
}
"""

JAVA_ENTITY = """
package com.shpp.core.entity;

import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "workflow_transaction")
public class WorkflowTransaction {
    private Long id;
}
"""


def test_build_tools_kb_finds_real_routes_and_tables():
    """Deterministic extraction must find real routes/tables from Java
    source — this is the ground-truth signal that was previously discarded."""
    code_files = [
        {"path": "src/main/java/com/shpp/core/restclient/WorkflowController.java", "content": JAVA_CONTROLLER},
        {"path": "src/main/java/com/shpp/core/entity/WorkflowTransaction.java", "content": JAVA_ENTITY},
    ]
    kb = build_tools_kb(code_files, [])
    assert kb["stats"]["api_routes"] >= 2, kb["stats"]
    assert kb["stats"]["db_tables"] >= 1, kb["stats"]
    assert kb["stats"]["has_backend_code"] is True


def test_deterministic_envelopes_from_kb_produces_real_endpoints():
    """The safety-net envelope builder must never emit generic 'INFRA'
    placeholders when real routes are present in the traceability map."""
    code_files = [
        {"path": "src/main/java/com/shpp/core/restclient/WorkflowController.java", "content": JAVA_CONTROLLER},
        {"path": "src/main/java/com/shpp/core/entity/WorkflowTransaction.java", "content": JAVA_ENTITY},
    ]
    kb = build_tools_kb(code_files, [])
    envelopes, stats = _deterministic_envelopes_from_kb(kb)

    assert stats["total_endpoints"] >= 2
    assert all(e["endpoint_method"] != "INFRA" for e in envelopes)
    assert all(e["endpoint_path"] for e in envelopes)
    paths = {e["endpoint_path"] for e in envelopes}
    assert any("initiateProcess" in p for p in paths)


def test_deterministic_envelopes_from_kb_handles_empty_kb():
    """No routes/tables discovered → empty (not crashing) safety net."""
    kb = build_tools_kb([], [])
    envelopes, stats = _deterministic_envelopes_from_kb(kb)
    assert envelopes == []
    assert stats["total_endpoints"] == 0
    assert stats["total_tables"] == 0
