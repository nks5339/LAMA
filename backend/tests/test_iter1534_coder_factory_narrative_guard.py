"""iter-15.34 — Code Transformer's Coder agent tries Factory Droid FIRST
(like every other agent), and only falls back to standard fabric routing
for a single retry if Droid's response looks like an agentic narrative
instead of literal transformed source code. Also verifies the Coder now
receives real ARCHITECTURE CONTEXT (API contract + service→repository→DB
trace) from the linked Context Manager envelope, not just the generic
project-wide KB summary.

Forensic context (do not delete):

    iter-15.27: User report: "droid is connected but code is not
    generated" — Factory Droid's narrative completion ("I've updated
    UserService.java...") was silently accepted as "generated code",
    producing 0 real transformed files. Fix at the time: permanently
    exempt agent_key "tools.transformer.coder" from Factory routing.

    iter-15.32: That exemption (broadened by mistake to the whole
    "tools.transformer." prefix, then narrowed back to just the Coder)
    was still blocking Factory for the Coder specifically.

    iter-15.34 correction: User explicitly asked "this multi agent
    orchestration is taking factory.ai as a llm, not ollama, if factory ai
    is up, running" — i.e. the Coder should ALSO use Factory when it's
    healthy, matching Planner/Context Manager/Verifier/Tester. The
    permanent Coder exemption is removed. Instead:
      1. `llm.fabric_call` gains a `skip_factory` kwarg (per-call opt-out,
         used only for the narrative-retry below — not a standing
         exemption for any agent_key).
      2. `routes/tools.py::_run_coder` detects (via `_looks_like_code`)
         whether a Factory-routed response ("model" field prefixed
         "factory/...") reads like real source code vs. a narrative
         summary, and if not, retries ONCE with `skip_factory=True` so a
         narrative is never persisted as "generated code" while Factory
         is still preferred whenever it behaves correctly.
      3. `_run_coder` also now receives the Context Manager envelope
         linked to the task's file (via `task_doc["envelope_id"]`) and
         includes its full API-to-DB architecture trace in the prompt.

These are source-level contract tests (matching the existing style in
test_iter13991_cross_stage_hook.py / the former
test_iter1527_transformer_factory_exemption.py) plus focused unit tests
for the pure `_looks_like_code` heuristic, which needs no mocking.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from routes.tools import _looks_like_code  # noqa: E402


def _read_source(relpath: str) -> str:
    with open(os.path.join(BACKEND, relpath), "r", encoding="utf-8") as f:
        return f.read()


# ── llm.py: no more standing Coder exemption; skip_factory is per-call ──

def test_permanent_coder_factory_exemption_is_gone():
    src = _read_source("llm.py")
    assert '_ak_lower == "tools.transformer.coder"' not in src, (
        "The old permanent Coder-only Factory exemption is back — this "
        "blocks the user's explicit request to let the Coder use Factory "
        "Droid when it's up and running."
    )
    assert 'startswith("tools.transformer.")' not in src, (
        "The old blanket 'tools.transformer.' prefix exemption is back."
    )


def test_llm_py_supports_skip_factory_kwarg():
    src = _read_source("llm.py")
    assert 'kwargs.pop("skip_factory"' in src, (
        "llm.fabric_call no longer supports a per-call skip_factory "
        "opt-out — _run_coder's narrative-retry has no way to force "
        "standard fabric routing for a single call."
    )


# ── routes/tools.py: Coder tries Factory, retries on narrative response ──

def test_run_coder_detects_factory_and_retries_on_narrative():
    src = _read_source("routes/tools.py")
    assert 'str(response.get("model", "")).startswith("factory")' in src
    assert "skip_factory=True" in src
    assert "_looks_like_code(transformed)" in src


def test_run_coder_accepts_envelope_param_for_architecture_context():
    src = _read_source("routes/tools.py")
    assert "envelope: Optional[dict] = None" in src
    assert "ARCHITECTURE CONTEXT" in src
    assert "table_trace" in src


def test_call_site_loads_and_passes_envelope_to_coder():
    src = _read_source("routes/tools.py")
    assert "envelopes_by_id = {e.get(\"envelope_id\"): e for e in envelope_docs" in src
    assert "envelope=envelope_for_task" in src


# ── _looks_like_code: pure heuristic unit tests ──

def test_looks_like_code_accepts_real_java_source():
    code = """package com.shpp.controller;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/user/api/v1")
public class UserController {
    @PostMapping("/create")
    public ResponseEntity<UserDto> create(@RequestBody UserDto dto) {
        return ResponseEntity.ok(userService.create(dto));
    }
}
"""
    assert _looks_like_code(code) is True


def test_looks_like_code_rejects_narrative_opener():
    narrative = (
        "I've updated UserService.java to use Spring's @Service annotation "
        "and migrated the repository calls to Spring Data JPA. The business "
        "logic for user creation remains unchanged, and I verified the "
        "endpoint still returns the same response shape as before."
    )
    assert _looks_like_code(narrative) is False


def test_looks_like_code_rejects_summary_style_response():
    narrative = "Summary: Updated the controller, service, and repository layers to Spring Boot 3 conventions."
    assert _looks_like_code(narrative) is False


def test_looks_like_code_rejects_empty_string():
    assert _looks_like_code("") is False
    assert _looks_like_code("   ") is False


def test_looks_like_code_accepts_yaml_config():
    yaml_src = """spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/mydb
    username: postgres
server:
  port: 8080
"""
    # YAML has no braces/parens but is short and has ":" — should still
    # pass since code_punct includes ":" and word count stays low per line.
    assert _looks_like_code(yaml_src) is True
