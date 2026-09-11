"""
iter-15.62 — Compile-fix loop no longer gives up as "unfixable" the
moment `_parse_compile_errors` fails to find a per-file `file:line`
diagnostic.

Before this iteration ANY build failure without regex-parseable
diagnostics (generic Maven/Gradle failures, dependency-resolution
errors, missing/broken plugin config — and, before iter-15.61's runtime
image fix, "toolchain missing on PATH" too) aborted the Planner/Coder/
Verifier fix loop on iteration 1, even though most of those failures
are fixable by editing a source file or a build manifest.

This iteration adds two building blocks:

  * `_is_infra_blocked`             — deterministic check: is EVERY
                                      failed invocation a genuine
                                      "toolchain missing on PATH" infra
                                      error (truly unfixable by editing
                                      anything)?
  * `_llm_diagnose_generic_failure` — LLM (Planner) fallback: given the
                                      raw failing build output, identify
                                      fixable target file(s) — source OR
                                      build manifest — shaped exactly
                                      like `_parse_compile_errors`'
                                      output so it flows unchanged into
                                      `_planner_fix_tasks_from_errors`.

`_planner_fix_tasks_from_errors` also gained a `kind` branch so
build-manifest fixes get manifest-appropriate instructions instead of
source-file phrasing ("imports, types, missing symbols").

These tests exercise the pure/deterministic parts directly and stub
Mongo + fabric_call for the LLM-backed diagnosis path.
"""
import asyncio
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes import tools as tools_mod  # noqa: E402


async def _noop_log_run(*a, **kw):
    return "run-id"


async def _noop_update_run(*a, **kw):
    return None


@pytest.fixture(autouse=True)
def _stub_agent_run(monkeypatch):
    monkeypatch.setattr(tools_mod, "_log_agent_run", _noop_log_run)
    monkeypatch.setattr(tools_mod, "_update_agent_run", _noop_update_run)


def _compile_result(invocations):
    return {"components": [{"component": "backend", "tool": "maven", "invocations": invocations}]}


# ─────────────── _is_infra_blocked ───────────────
def test_is_infra_blocked_true_when_every_failure_is_toolchain_missing():
    result = _compile_result([
        {"status": "failed", "reason": "toolchain missing on PATH: mvn (this is a LAMA runtime image bug — please report)"},
    ])
    reason = tools_mod._is_infra_blocked(result)
    assert reason is not None
    assert "toolchain missing" in reason.lower()


def test_is_infra_blocked_false_when_any_failure_is_real():
    result = _compile_result([
        {"status": "failed", "reason": "toolchain missing on PATH: mvn"},
        {"status": "failed", "reason": "BUILD FAILURE: dependency org.foo:bar:1.0 not found"},
    ])
    assert tools_mod._is_infra_blocked(result) is None


def test_is_infra_blocked_false_when_no_failures():
    assert tools_mod._is_infra_blocked({"components": []}) is None


def test_is_infra_blocked_ignores_passed_invocations():
    result = _compile_result([
        {"status": "passed", "reason": ""},
        {"status": "failed", "reason": "toolchain missing on PATH: gradle"},
    ])
    reason = tools_mod._is_infra_blocked(result)
    assert reason is not None


# ─────────────── _detect_installed_toolchain_versions ───────────────
def test_detect_installed_toolchain_versions_parses_java(monkeypatch):
    def _fake_binary_on_path(name):
        return "/usr/bin/javac" if name == "javac" else None

    class _FakeProc:
        stdout = ""
        stderr = "javac 17.0.20.1\n"

    monkeypatch.setattr(tools_mod, "_binary_on_path", _fake_binary_on_path)
    monkeypatch.setattr(tools_mod.subprocess, "run", lambda *a, **kw: _FakeProc())

    versions = tools_mod._detect_installed_toolchain_versions()
    assert versions == {"java": "17"}


def test_detect_installed_toolchain_versions_empty_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda name: None)
    versions = tools_mod._detect_installed_toolchain_versions()
    assert versions == {}


def test_detect_installed_toolchain_versions_survives_subprocess_error(monkeypatch):
    def _fake_binary_on_path(name):
        return "/usr/bin/javac" if name == "javac" else None

    def _raise(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(tools_mod, "_binary_on_path", _fake_binary_on_path)
    monkeypatch.setattr(tools_mod.subprocess, "run", _raise)

    versions = tools_mod._detect_installed_toolchain_versions()
    assert versions == {}


# ─────────────── _detect_release_mismatch_error_groups (deterministic fast-path) ───────────────
def test_detect_release_mismatch_finds_pom_in_matching_cwd():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "",
        "stderr_tail": "[ERROR] invalid target release: 21",
    }]
    candidate_files = ["backend/pom.xml", "backend/src/Main.java", "frontend/package.json"]
    groups = tools_mod._detect_release_mismatch_error_groups(fails, candidate_files, {"java": "17"})
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"
    assert groups[0]["kind"] == "build_config"
    assert "JDK 17" in groups[0]["lines"][0][1]


def test_detect_release_mismatch_falls_back_to_any_manifest_when_cwd_mismatches():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "modules/backend-service",
        "stdout_tail": "", "stderr_tail": "release version 21 not supported",
    }]
    # candidate paths use a different prefix than the compiler-reported cwd
    candidate_files = ["backend/pom.xml"]
    groups = tools_mod._detect_release_mismatch_error_groups(fails, candidate_files, {"java": "17"})
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"


def test_detect_release_mismatch_returns_empty_when_no_java_installed():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "invalid target release: 21",
    }]
    groups = tools_mod._detect_release_mismatch_error_groups(fails, ["backend/pom.xml"], {})
    assert groups == []


def test_detect_release_mismatch_returns_empty_for_unrelated_failure():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "dependency org.foo:bar:1.0 not found",
    }]
    groups = tools_mod._detect_release_mismatch_error_groups(fails, ["backend/pom.xml"], {"java": "17"})
    assert groups == []


def test_detect_release_mismatch_dedupes_same_manifest_across_modules():
    fails = [
        {"component": "backend", "tool": "maven", "cwd": "backend",
         "stdout_tail": "", "stderr_tail": "invalid target release: 21"},
        {"component": "backend", "tool": "maven", "cwd": "backend",
         "stdout_tail": "", "stderr_tail": "invalid target release: 21"},
    ]
    groups = tools_mod._detect_release_mismatch_error_groups(fails, ["backend/pom.xml"], {"java": "17"})
    assert len(groups) == 1


def test_llm_diagnose_generic_failure_uses_deterministic_fast_path_without_llm_call(monkeypatch):
    """When the release-mismatch pattern matches, no LLM call should even
    be attempted — fabric_call must not be invoked."""
    result = _compile_result([
        {"status": "failed", "reason": "BUILD FAILURE", "cwd": "backend",
         "stdout_tail": "", "stderr_tail": "invalid target release: 21"},
    ])
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeCursor([{"path": "backend/pom.xml"}]),
    )
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda name: "/usr/bin/javac" if name == "javac" else None)

    class _FakeProc:
        stdout = ""
        stderr = "javac 17.0.20.1\n"

    monkeypatch.setattr(tools_mod.subprocess, "run", lambda *a, **kw: _FakeProc())

    def _fail_if_called(**kwargs):
        raise AssertionError("fabric_call should not be invoked for the deterministic fast-path")

    monkeypatch.setattr(tools_mod, "fabric_call", _fail_if_called)

    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["blocked"] is False
    assert len(diag["error_groups"]) == 1
    assert diag["error_groups"][0]["path"] == "backend/pom.xml"
    assert diag["error_groups"][0]["kind"] == "build_config"


# ─────────────── _detect_missing_dependency_error_groups ───────────────
def test_detect_missing_dependency_matches_package_does_not_exist():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "error: package jakarta.json does not exist",
    }]
    groups = tools_mod._detect_missing_dependency_error_groups(fails, ["backend/pom.xml"])
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"
    assert groups[0]["kind"] == "build_config"
    assert "jakarta.json" in groups[0]["lines"][0][1]


def test_detect_missing_dependency_matches_llm_prose_summary():
    """This is literally the phrasing the user hit: a narrative summary
    of a missing-dependency failure, not raw compiler output."""
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "",
        "reason": "The build is failing due to missing Jakarta JSON API dependencies.",
    }]
    groups = tools_mod._detect_missing_dependency_error_groups(fails, ["backend/pom.xml"])
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"


def test_detect_missing_dependency_matches_node_and_python_and_dotnet_and_go():
    cases = [
        "Cannot find module 'lodash'",
        "ModuleNotFoundError: No module named 'requests'",
        "error CS0246: The type or namespace name 'Foo' could not be found",
        "no required module provides package github.com/foo/bar",
    ]
    for text in cases:
        fails = [{"component": "x", "tool": "npm", "cwd": "", "stdout_tail": text, "stderr_tail": ""}]
        groups = tools_mod._detect_missing_dependency_error_groups(fails, ["package.json"])
        assert len(groups) == 1, f"expected a match for: {text}"


def test_detect_missing_dependency_returns_empty_for_unrelated_failure():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "BUILD SUCCESS",
    }]
    groups = tools_mod._detect_missing_dependency_error_groups(fails, ["backend/pom.xml"])
    assert groups == []


def test_detect_missing_dependency_falls_back_to_any_manifest_when_cwd_mismatches():
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "modules/svc",
        "stdout_tail": "", "stderr_tail": "package jakarta.json does not exist",
    }]
    groups = tools_mod._detect_missing_dependency_error_groups(fails, ["backend/pom.xml"])
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"


def test_llm_diagnose_generic_failure_missing_dependency_fast_path_skips_llm(monkeypatch):
    result = _compile_result([
        {"status": "failed", "reason": "", "cwd": "backend",
         "stdout_tail": "", "stderr_tail": "package jakarta.json does not exist"},
    ])
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeCursor([{"path": "backend/pom.xml"}]),
    )
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda name: None)

    def _fail_if_called(**kwargs):
        raise AssertionError("fabric_call should not be invoked for the deterministic fast-path")

    monkeypatch.setattr(tools_mod, "fabric_call", _fail_if_called)

    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["blocked"] is False
    assert len(diag["error_groups"]) == 1
    assert diag["error_groups"][0]["path"] == "backend/pom.xml"


# ─────────────── _llm_diagnose_generic_failure ───────────────
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


def test_llm_diagnose_generic_failure_returns_error_groups(monkeypatch):
    result = _compile_result([
        {"status": "failed", "reason": "BUILD FAILURE", "cwd": "svc",
         "stdout_tail": "...", "stderr_tail": "dependency resolution failed"},
    ])
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeCursor([{"path": "svc/pom.xml"}, {"path": "svc/src/Main.java"}]),
    )
    monkeypatch.setattr(tools_mod, "_get_effective_prompt", lambda *a, **kw: asyncio.sleep(0, result="PLANNER PROMPT"))
    monkeypatch.setattr(tools_mod, "_get_effective_model", lambda *a, **kw: asyncio.sleep(0, result="fake-model"))
    monkeypatch.setattr(tools_mod, "_project_id_for_transform", lambda *a, **kw: asyncio.sleep(0, result="proj-1"))

    async def _fake_fabric_call(**kwargs):
        payload = {
            "root_cause": "Missing dependency declaration in pom.xml",
            "fixable": True,
            "target_files": [{"path": "svc/pom.xml", "instructions": "Add missing dependency X"}],
        }
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric_call)

    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["blocked"] is False
    assert "pom.xml" in diag["root_cause"] or diag["root_cause"]
    assert len(diag["error_groups"]) == 1
    grp = diag["error_groups"][0]
    assert grp["path"] == "svc/pom.xml"
    assert grp["kind"] == "build_config"
    assert grp["lines"][0][1] == "Add missing dependency X"


def test_llm_diagnose_generic_failure_ignores_files_outside_candidate_list(monkeypatch):
    result = _compile_result([
        {"status": "failed", "reason": "BUILD FAILURE", "cwd": "svc", "stdout_tail": "", "stderr_tail": ""},
    ])
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeCursor([{"path": "svc/pom.xml"}]),
    )
    monkeypatch.setattr(tools_mod, "_get_effective_prompt", lambda *a, **kw: asyncio.sleep(0, result="PLANNER PROMPT"))
    monkeypatch.setattr(tools_mod, "_get_effective_model", lambda *a, **kw: asyncio.sleep(0, result="fake-model"))
    monkeypatch.setattr(tools_mod, "_project_id_for_transform", lambda *a, **kw: asyncio.sleep(0, result="proj-1"))

    async def _fake_fabric_call(**kwargs):
        payload = {
            "root_cause": "hallucinated a file that was never in the candidate list",
            "fixable": True,
            "target_files": [{"path": "svc/not-a-real-file.xml", "instructions": "..."}],
        }
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric_call)

    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["error_groups"] == []


def test_llm_diagnose_generic_failure_returns_blocked_when_llm_says_unfixable(monkeypatch):
    result = _compile_result([
        {"status": "failed", "reason": "disk full", "cwd": "svc", "stdout_tail": "", "stderr_tail": ""},
    ])
    monkeypatch.setattr(tools_mod.transform_files, "find", lambda *a, **kw: _FakeCursor([]))
    monkeypatch.setattr(tools_mod, "_get_effective_prompt", lambda *a, **kw: asyncio.sleep(0, result="PLANNER PROMPT"))
    monkeypatch.setattr(tools_mod, "_get_effective_model", lambda *a, **kw: asyncio.sleep(0, result="fake-model"))
    monkeypatch.setattr(tools_mod, "_project_id_for_transform", lambda *a, **kw: asyncio.sleep(0, result="proj-1"))

    async def _fake_fabric_call(**kwargs):
        payload = {"root_cause": "Disk is full on the build agent", "fixable": False, "target_files": []}
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric_call)

    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["blocked"] is True
    assert diag["error_groups"] == []


def test_llm_diagnose_generic_failure_no_failed_invocations_short_circuits(monkeypatch):
    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", {"components": []}, 1, None))
    assert diag == {"blocked": False, "root_cause": "", "error_groups": []}


# ─────────────── _planner_fix_tasks_from_errors: kind-based notes ───────────────
def test_planner_fix_tasks_build_config_notes_differ_from_source_notes(monkeypatch):
    file_doc = {"path": "svc/pom.xml", "original_path": "svc/pom.xml"}
    monkeypatch.setattr(tools_mod, "_resolve_failing_transform_file", lambda *a, **kw: asyncio.sleep(0, result=file_doc))

    inserted = []

    async def _fake_insert_one(doc):
        inserted.append(doc)

    monkeypatch.setattr(tools_mod.transformer_tasks, "insert_one", _fake_insert_one)

    error_groups = [{
        "path": "svc/pom.xml", "component": "backend", "tool": "maven",
        "kind": "build_config", "lines": [(0, "Add missing dependency X")],
    }]
    created = asyncio.run(tools_mod._planner_fix_tasks_from_errors("tx-1", error_groups, 1))
    assert len(created) == 1
    notes = created[0]["notes"]
    assert "Planner diagnosis for build configuration file" in notes
    assert "imports, types" not in notes


def test_planner_fix_tasks_source_notes_use_default_phrasing(monkeypatch):
    file_doc = {"path": "svc/src/Main.java", "original_path": "svc/src/Main.java"}
    monkeypatch.setattr(tools_mod, "_resolve_failing_transform_file", lambda *a, **kw: asyncio.sleep(0, result=file_doc))

    async def _fake_insert_one(doc):
        pass

    monkeypatch.setattr(tools_mod.transformer_tasks, "insert_one", _fake_insert_one)

    error_groups = [{
        "path": "svc/src/Main.java", "component": "backend", "tool": "maven",
        "lines": [(12, "cannot find symbol Foo")],
    }]
    created = asyncio.run(tools_mod._planner_fix_tasks_from_errors("tx-1", error_groups, 1))
    assert len(created) == 1
    notes = created[0]["notes"]
    assert "imports, types" in notes
    assert "build configuration file" not in notes
