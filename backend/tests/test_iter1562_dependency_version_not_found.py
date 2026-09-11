"""
iter-15.62.5 — Two fixes triggered directly by a user-reported failure:

  [ERROR] Failed to execute goal on project helidon-quickstart: Could
  not resolve dependencies for project
  com.example:helidon-quickstart:jar:1.0-SNAPSHOT:
  org.eclipse.parsson:parsson:jar:1.0.10 was not found in
  https://repo.maven.apache.org/maven2 during a previous attempt. This
  failure was cached in the local repository ...

1. Root cause: a PREVIOUS fix round (iter-15.62.3's missing-dependency
   fast-path) told the Coder to add "org.eclipse.parsson:parsson" but
   could not tell it which version to use (regexes can't know Maven
   Central), so the Coder guessed a plausible-looking but nonexistent
   version (1.0.10 — the real releases are 1.0.0-1.0.5, 1.1.0-1.1.9).
   The SAME missing-dependency catch-all regex
   (`could not resolve dependenc(?:y|ies)`) also matched THIS failure,
   so the loop kept mis-diagnosing "wrong pinned version" as "missing
   dependency" and told the Coder to add a duplicate — it just guessed
   a different wrong version each time.

   Fix: `_detect_dependency_version_not_found_error_groups` — a new,
   HIGHER-PRIORITY deterministic fast-path that recognises this exact
   failure shape, extracts the bad groupId:artifactId:version, performs
   a real (non-LLM) Maven Central `maven-metadata.xml` lookup to find
   out which versions genuinely exist, and tells the Coder the EXACT
   verified-correct version to use instead of guessing again.

2. The raw console for this failure contained literal ANSI escape
   garbage (`[0m[0m`) because Maven emits color codes even in some
   non-interactive setups. Fix: `-Dstyle.color=never` added to every
   mvn invocation + a universal `_strip_ansi()` applied to every
   streamed console line, so the live Compile Console is always clean,
   readable plain text.
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

PARSSON_ERROR_TEXT = (
    "[ERROR] Failed to execute goal on project helidon-quickstart: "
    "Could not resolve dependencies for project "
    "com.example:helidon-quickstart:jar:1.0-SNAPSHOT: "
    "org.eclipse.parsson:parsson:jar:1.0.10 was not found in "
    "https://repo.maven.apache.org/maven2 during a previous attempt. "
    "This failure was cached in the local repository and resolution "
    "is not reattempted until the update interval of central has "
    "elapsed or updates are forced -> [Help 1]"
)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


# ─────────────── _strip_ansi ───────────────
def test_strip_ansi_removes_color_and_reset_codes():
    dirty = "\x1b[1;31m[ERROR]\x1b[0m boom\x1b[0m"
    assert tools_mod._strip_ansi(dirty) == "[ERROR] boom"


def test_strip_ansi_noop_on_plain_text():
    assert tools_mod._strip_ansi("BUILD SUCCESS") == "BUILD SUCCESS"


def test_strip_ansi_handles_empty_and_none():
    assert tools_mod._strip_ansi("") == ""
    assert tools_mod._strip_ansi(None) is None


def test_maven_argv_disables_color():
    assert "-Dstyle.color=never" in tools_mod.NATIVE_BUILD_COMMANDS["maven"]["argv"]


# ─────────────── _best_replacement_version ───────────────
def test_best_replacement_version_prefers_same_major_minor_family():
    available = ["1.0.0", "1.0.1", "1.0.5", "1.1.0", "1.1.9"]
    assert tools_mod._best_replacement_version("1.0.10", available) == "1.0.5"


def test_best_replacement_version_falls_back_to_overall_latest():
    available = ["1.1.0", "1.1.9", "2.0.0"]
    assert tools_mod._best_replacement_version("1.0.10", available) == "2.0.0"


def test_best_replacement_version_empty_when_nothing_available():
    assert tools_mod._best_replacement_version("1.0.10", []) == ""


# ─────────────── _detect_dependency_version_not_found_error_groups ───────────────
def test_detect_dependency_version_not_found_matches_parsson_report(monkeypatch):
    async def _fake_fetch(group_id, artifact_id, repo):
        assert group_id == "org.eclipse.parsson"
        assert artifact_id == "parsson"
        return ["1.0.0", "1.0.1", "1.0.5", "1.1.0", "1.1.9"]

    monkeypatch.setattr(tools_mod, "_fetch_maven_versions", _fake_fetch)

    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": PARSSON_ERROR_TEXT,
    }]
    groups = asyncio.run(
        tools_mod._detect_dependency_version_not_found_error_groups(fails, ["backend/pom.xml"])
    )
    assert len(groups) == 1
    assert groups[0]["path"] == "backend/pom.xml"
    assert groups[0]["kind"] == "build_config"
    instruction = groups[0]["lines"][0][1]
    assert "1.0.10" in instruction
    assert "1.0.5" in instruction  # the verified real replacement, not a guess
    assert "org.eclipse.parsson:parsson" in instruction


def test_detect_dependency_version_not_found_falls_back_gracefully_when_lookup_fails(monkeypatch):
    async def _fake_fetch(group_id, artifact_id, repo):
        return []  # offline / blocked

    monkeypatch.setattr(tools_mod, "_fetch_maven_versions", _fake_fetch)

    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": PARSSON_ERROR_TEXT,
    }]
    groups = asyncio.run(
        tools_mod._detect_dependency_version_not_found_error_groups(fails, ["backend/pom.xml"])
    )
    assert len(groups) == 1
    instruction = groups[0]["lines"][0][1]
    assert "do NOT guess" in instruction or "do not guess" in instruction.lower()


def test_detect_dependency_version_not_found_returns_empty_for_unrelated_failure(monkeypatch):
    fails = [{
        "component": "backend", "tool": "maven", "cwd": "backend",
        "stdout_tail": "", "stderr_tail": "BUILD SUCCESS",
    }]
    groups = asyncio.run(
        tools_mod._detect_dependency_version_not_found_error_groups(fails, ["backend/pom.xml"])
    )
    assert groups == []


def test_llm_diagnose_generic_failure_routes_version_not_found_before_missing_dependency(monkeypatch):
    """This is the exact regression this iteration fixes: the failure
    text ALSO matches the missing-dependency catch-all regex
    (`could not resolve dependenc(?:y|ies)`), so ordering matters — the
    version-specific fast-path must win and `fabric_call` must never be
    invoked."""
    async def _fake_fetch(group_id, artifact_id, repo):
        return ["1.0.0", "1.0.5", "1.1.9"]

    monkeypatch.setattr(tools_mod, "_fetch_maven_versions", _fake_fetch)
    monkeypatch.setattr(
        tools_mod.transform_files, "find",
        lambda *a, **kw: _FakeCursor([{"path": "backend/pom.xml"}]),
    )
    monkeypatch.setattr(tools_mod, "_binary_on_path", lambda name: None)

    def _fail_if_called(**kwargs):
        raise AssertionError("fabric_call should not be invoked for the deterministic fast-path")

    monkeypatch.setattr(tools_mod, "fabric_call", _fail_if_called)

    result = {"components": [{"component": "backend", "tool": "maven", "invocations": [
        {"status": "failed", "reason": "", "cwd": "backend", "stdout_tail": "", "stderr_tail": PARSSON_ERROR_TEXT},
    ]}]}
    diag = asyncio.run(tools_mod._llm_diagnose_generic_failure("tx-1", result, 1, None))
    assert diag["blocked"] is False
    assert len(diag["error_groups"]) == 1
    instruction = diag["error_groups"][0]["lines"][0][1]
    assert "1.0.5" in instruction
    assert "do NOT add a duplicate" in instruction
