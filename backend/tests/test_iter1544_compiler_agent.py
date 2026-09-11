"""
iter-15.44 — Real subprocess-based compiler agent.

Prior to iter-15.44 the "compilation analysis" phase only asked an LLM
whether the transformed code *looked* like it would compile. This iter
replaces that with `_run_compiler`, which materialises the transformed
tree into a scratch workspace and runs the actual build tool the
operator confirmed in step 1 (`transformation.build_tools`). The LLM
narrative is kept as a supplementary `static_analysis` field, no longer
the sole compile signal.

These tests exercise the pure helpers directly:

  * `_write_transformed_workspace`  — filesystem materialisation
  * `_find_manifest_dirs`           — build-root discovery
  * `_run_native_build`             — subprocess-based execution and
                                      the "toolchain missing" hard-fail
                                      code path (iter-15.6x: no longer a
                                      silent skip-as-pass)
  * `_run_compiler`                 — end-to-end aggregation with mixed
                                      passed / failed / skipped rows

We monkeypatch `_binary_on_path` so the toolchain-detection is
deterministic across CI/host environments, and force `NATIVE_BUILD_COMMANDS`
for a couple of tools to point at short shell echo/exit commands so we
never depend on maven/npm actually being installed on the test runner.
"""
import asyncio
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


# ─────────────── helpers ───────────────
async def _noop_log_run(*a, **kw): return "run-id"
async def _noop_update_run(*a, **kw): return None


@pytest.fixture(autouse=True)
def _stub_agent_run(monkeypatch):
    """The _run_compiler entry uses _log_agent_run / _update_agent_run
    which hit Mongo. Stub both — the test cares about the return
    value, not the audit trail."""
    monkeypatch.setattr(tools_mod, "_log_agent_run", _noop_log_run)
    monkeypatch.setattr(tools_mod, "_update_agent_run", _noop_update_run)


# ─────────────── _write_transformed_workspace ───────────────
def test_write_transformed_workspace_materialises_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "tempfile", __import__("tempfile"))
    files = [
        {"path": "app/pom.xml", "content": "<project/>"},
        {"path": "app/src/Main.java", "content": "class Main {}"},
        {"path": "../evil.txt", "content": "should be sanitised"},
        {"path": "", "content": "ignored"},
    ]
    root = tools_mod._write_transformed_workspace(files)
    assert os.path.isdir(root)
    assert os.path.isfile(os.path.join(root, "app/pom.xml"))
    assert os.path.isfile(os.path.join(root, "app/src/Main.java"))
    # The `..` prefix is stripped, not silently allowed to escape.
    listing = []
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            listing.append(os.path.relpath(os.path.join(dp, fn), root))
    assert not any(".." in p for p in listing)


# ─────────────── _find_manifest_dirs ───────────────
def test_find_manifest_dirs_walks_and_skips_common_dirs(tmp_path):
    # Two manifests, one under a skipped dir → the skipped one is
    # never returned.
    (tmp_path / "svc-a").mkdir()
    (tmp_path / "svc-a" / "pom.xml").write_text("<x/>")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pom.xml").write_text("<x/>")
    (tmp_path / "svc-b").mkdir()
    (tmp_path / "svc-b" / "pom.xml").write_text("<x/>")

    dirs = tools_mod._find_manifest_dirs(str(tmp_path), "pom.xml")
    rel = sorted(os.path.relpath(d, str(tmp_path)) for d in dirs)
    assert rel == ["svc-a", "svc-b"]


def test_find_manifest_dirs_glob(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "Foo.csproj").write_text("<Project/>")
    dirs = tools_mod._find_manifest_dirs(str(tmp_path), "*.csproj")
    assert len(dirs) == 1
    assert os.path.basename(dirs[0]) == "svc"


# ─────────────── _run_native_build ───────────────
def test_run_native_build_fails_when_binary_missing(monkeypatch, tmp_path):
    # iter-15.6x — every tool in BUILD_TOOL_NATIVE_SUPPORT now ships a real
    # toolchain in the runtime image, so a missing binary is treated as a
    # hard FAILURE (an infra bug) rather than a silent "skip" that used to
    # be counted as a pass by `compilation_ready`.
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda _n: None)
    out = asyncio.run(tools_mod._run_native_build("maven", str(tmp_path), 30))
    assert out["status"] == "failed"
    assert "toolchain missing" in out["reason"].lower()


def test_run_native_build_reports_passed_on_zero_exit(monkeypatch, tmp_path):
    import shutil as _sh
    true_bin = _sh.which("true") or "/usr/bin/true"
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda _n: true_bin)
    monkeypatch.setitem(tools_mod.NATIVE_BUILD_COMMANDS, "maven", {
        "manifest": "pom.xml", "binary": "true",
        "argv": [true_bin], "label": "true(0)",
    })
    out = asyncio.run(tools_mod._run_native_build("maven", str(tmp_path), 30))
    assert out["status"] == "passed"
    assert out["exit_code"] == 0
    assert out["duration_ms"] >= 0


def test_run_native_build_reports_failed_on_nonzero_exit(monkeypatch, tmp_path):
    import shutil as _sh
    false_bin = _sh.which("false") or "/usr/bin/false"
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda _n: false_bin)
    monkeypatch.setitem(tools_mod.NATIVE_BUILD_COMMANDS, "maven", {
        "manifest": "pom.xml", "binary": "false",
        "argv": [false_bin], "label": "false(1)",
    })
    out = asyncio.run(tools_mod._run_native_build("maven", str(tmp_path), 30))
    assert out["status"] == "failed"
    assert out["exit_code"] != 0


def test_run_native_build_reports_unknown_tool_as_skipped(tmp_path):
    out = asyncio.run(tools_mod._run_native_build("erlang-rebar3", str(tmp_path), 30))
    assert out["status"] == "skipped"
    assert "no native command mapping" in out["reason"]


# ─────────────── _run_compiler end-to-end ───────────────
def test_run_compiler_aggregates_mixed_component_results(monkeypatch, tmp_path):
    import shutil as _sh
    true_bin = _sh.which("true") or "/usr/bin/true"
    false_bin = _sh.which("false") or "/usr/bin/false"

    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "pom.xml").write_text("<x/>")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")

    # Maven passes, npm fails, "erlang" is unknown → skipped.
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda n: {
        "true": true_bin, "false": false_bin,
    }.get(n))
    monkeypatch.setitem(tools_mod.NATIVE_BUILD_COMMANDS, "maven", {
        "manifest": "pom.xml", "binary": "true",
        "argv": [true_bin], "label": "true(0)",
    })
    monkeypatch.setitem(tools_mod.NATIVE_BUILD_COMMANDS, "npm", {
        "manifest": "package.json", "binary": "false",
        "argv": [false_bin], "label": "false(1)",
    })

    result = asyncio.run(tools_mod._run_compiler(
        "tx-1", str(tmp_path),
        {"backend": "maven", "frontend": "npm", "runtime": "erlang-rebar3"},
    ))

    assert result["mode"] == "native"
    comps = {c["component"]: c for c in result["components"]}
    assert comps["backend"]["status"] == "passed"
    assert comps["frontend"]["status"] == "failed"
    assert comps["runtime"]["status"] == "skipped"
    # 1 passed / 1 failed / 1 skipped → 50 % of meaningful comps passed.
    assert result["overall_score"] == 50
    assert result["compilation_ready"] is False
    assert "1 passed" in result["summary"]
    assert "1 failed" in result["summary"]
    assert "1 skipped" in result["summary"]


def test_run_compiler_all_passed_marks_ready(monkeypatch, tmp_path):
    import shutil as _sh
    true_bin = _sh.which("true") or "/usr/bin/true"
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "pom.xml").write_text("<x/>")
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda _n: true_bin)
    monkeypatch.setitem(tools_mod.NATIVE_BUILD_COMMANDS, "maven", {
        "manifest": "pom.xml", "binary": "true",
        "argv": [true_bin], "label": "true(0)",
    })
    result = asyncio.run(tools_mod._run_compiler(
        "tx-2", str(tmp_path), {"backend": "maven"},
    ))
    assert result["compilation_ready"] is True
    assert result["overall_score"] == 100
    assert result["components"][0]["invocations"][0]["cwd"] == "svc"


def test_run_compiler_no_manifest_marks_component_skipped(monkeypatch, tmp_path):
    import shutil as _sh
    true_bin = _sh.which("true") or "/usr/bin/true"
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda _n: true_bin)
    result = asyncio.run(tools_mod._run_compiler(
        "tx-3", str(tmp_path), {"backend": "maven"},
    ))
    row = result["components"][0]
    assert row["status"] == "skipped"
    assert "no `pom.xml`" in row["reason"].lower()


def test_run_compiler_empty_build_tools_returns_zero(tmp_path):
    result = asyncio.run(tools_mod._run_compiler("tx-4", str(tmp_path), {}))
    assert result["components"] == []
    assert result["compilation_ready"] is False
    assert result["overall_score"] == 0
    assert "No build components configured" in result["summary"]
