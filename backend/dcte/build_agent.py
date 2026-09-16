"""DCTE Build Agent — iter-18.11.

Runs `mvn compile` (or `gradle build`) on the AI-transformed output and, if
the build fails, feeds each failing file's compile errors back to the LLM
with a "fix these errors" prompt. Loops up to ``max_attempts`` times.

Design notes
------------
* Runs INSIDE the LAMA container (Maven 3.8 + JDK 17 are pre-installed in
  the runtime image). Java 21 is not present, so we override
  ``maven.compiler.{source,target,release}=17`` on the command line to
  keep the validation compile working while the emitted pom targets 21
  for the final artefact.
* Non-blocking: if no build tool is detected (no ``pom.xml`` /
  ``build.gradle``) or the tool binary is not on ``PATH``, the agent
  emits a diagnostic and returns ``{"skipped": True, ...}`` so the
  overall job does NOT fail.
* Fix loop is bounded (``max_attempts``, default 5) so a truly broken
  service cannot burn tokens forever.
* Every LLM rewrite goes through the same ``_safe_apply`` guardrails as
  ``ai_refactor.py`` — no half-migrated markers, size-ratio bounds, file
  identity check.
* Errors from files LAMA doesn't own (jars, generated sources) are
  filtered out — we only rewrite files under ``dest_root``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ai_refactor import _safe_apply, _strip_json_fence, _extract_json_array

logger = logging.getLogger("lama.dcte.build_agent")


# ── Config ──────────────────────────────────────────────────────────
_DEFAULT_MAX_ATTEMPTS = 5
_BUILD_TIMEOUT_S = 480                # single mvn/gradle invocation
_MAX_FIXES_PER_ATTEMPT = 12           # cap fabric calls per attempt
_MAX_CHARS_PER_FILE = 16000
_MAX_FILE_SIZE_FOR_LLM = 40000
_JAVAC_ERR_RE = re.compile(
    r"(?P<path>[/A-Za-z0-9_\-.\\ ]+\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.+)",
)
# Fallback pattern for javac -Xdiags plain output
_JAVAC_ERR_RE_ALT = re.compile(
    r"(?P<path>[/A-Za-z0-9_\-.\\ ]+\.java):(?P<line>\d+):\s+error:\s+(?P<msg>.+)",
)


_SYSTEM = """You are a Senior Backend Developer and Spring Boot 3.x / Java 17-21 build-error triage expert.

TASK
  You are given ONE Java source file and the exact javac / Maven compile errors it produced.
  Return the FULL corrected source of the file so it compiles cleanly on Spring Boot 3.3+ under Java 17-21 with these starters on the classpath: spring-boot-starter-web, spring-boot-starter-data-jpa, spring-boot-starter-security, spring-boot-starter-actuator, springdoc-openapi-starter-webmvc-ui, postgresql, flyway-core, micrometer-core, lombok.

STRICT RULES
  1. Do NOT alter business logic — variable names, branch semantics, HTTP verbs, URL paths, DB column names, DTO field names must be preserved.
  2. Do NOT truncate. Return the FULL file from the `package` line through the final closing brace. Never end with `...` or "rest of file omitted".
  3. Do NOT reintroduce Helidon / MicroProfile symbols: no `jakarta.ws.rs.*`, no `io.helidon.*`, no `@ApplicationScoped`, no `@ConfigProperty`, no `org.eclipse.microprofile.*`.
  4. Fix only what the compiler complained about, plus any transitive imports/types that fix requires. Do not restructure unrelated code.
  5. If a fix requires a class you cannot see, add a reasonable Spring equivalent (e.g., missing DTO → keep the field names, add getters/setters or `record`).
  6. Response is STRICT JSON. Exactly one object:

     {
       "file": "<absolute path echoed back>",
       "action": "rewrite" | "leave",
       "content": "<full corrected file source if action=='rewrite'>",
       "changes": ["short bullet", "short bullet"],
       "risk": "low" | "medium" | "high"
     }

  7. If you genuinely cannot fix the file without more context, return `"action":"leave"` with an empty `content` and a `changes` array explaining why in one line.
"""


@dataclass
class BuildResult:
    tool: str | None = None
    attempted: bool = False
    skipped: bool = False
    success: bool = False
    attempts: int = 0
    fixes_applied: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    final_output_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "attempted": self.attempted,
            "skipped": self.skipped,
            "success": self.success,
            "attempts": self.attempts,
            "fixes_applied": self.fixes_applied,
            "errors": self.errors[:50],
            "notes": self.notes[-20:],
            "final_output_tail": self.final_output_tail[-4000:],
        }


# ── Build tool detection & invocation ───────────────────────────────

def _detect_build_tool(dest_root: Path) -> str | None:
    if (dest_root / "pom.xml").is_file():
        return "maven"
    if (dest_root / "build.gradle").is_file() or (dest_root / "build.gradle.kts").is_file():
        return "gradle"
    return None


def _maven_cmd(_dest_root: Path) -> list[str] | None:
    exe = shutil.which("mvn")
    if not exe:
        return None
    # Force JDK-17 compile flags so a container without JDK 21 can still
    # validate. This does NOT alter the pom on disk.
    return [
        exe, "-B", "-q",
        "-DskipTests=true",
        "-Dmaven.compiler.source=17",
        "-Dmaven.compiler.target=17",
        "-Dmaven.compiler.release=17",
        "-Dmaven.test.skip=true",
        "compile",
    ]


def _gradle_cmd(dest_root: Path) -> list[str] | None:
    wrap = dest_root / "gradlew"
    if wrap.is_file():
        return [str(wrap), "--no-daemon", "-x", "test", "compileJava"]
    exe = shutil.which("gradle")
    if not exe:
        return None
    return [exe, "--no-daemon", "-x", "test", "compileJava"]


async def _run_build(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run the build command async, capture combined stdout+stderr."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_BUILD_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, f"[build agent] build timed out after {_BUILD_TIMEOUT_S}s"
    output = (stdout_b or b"").decode("utf-8", errors="replace")
    return int(proc.returncode or 0), output


# ── Error parsing ───────────────────────────────────────────────────

def _parse_errors(output: str, dest_root: Path) -> list[dict[str, Any]]:
    errs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for rx in (_JAVAC_ERR_RE, _JAVAC_ERR_RE_ALT):
        for m in rx.finditer(output):
            raw_path = m.group("path").strip()
            try:
                line = int(m.group("line"))
            except Exception:
                continue
            msg = m.group("msg").strip()
            p = Path(raw_path)
            # Only accept files INSIDE dest_root (skip jars, generated stubs)
            try:
                p_resolved = p.resolve()
                p_resolved.relative_to(dest_root.resolve())
            except Exception:
                continue
            key = (str(p_resolved), line, msg)
            if key in seen:
                continue
            seen.add(key)
            errs.append({
                "file": str(p_resolved),
                "line": line,
                "message": msg,
            })
    return errs


def _group_errors_by_file(errs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in errs:
        out.setdefault(e["file"], []).append(e)
    return out


# ── LLM fix loop ────────────────────────────────────────────────────

async def _fix_one_file(
    fabric_call, path: Path, errors: list[dict[str, Any]],
    agent_key: str,
) -> tuple[bool, str, list[str]]:
    """Ask the LLM to rewrite `path` given `errors`. Returns (applied, reason, changes)."""
    try:
        body = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return False, f"read failed: {e}", []
    if len(body) > _MAX_FILE_SIZE_FOR_LLM:
        return False, f"file > {_MAX_FILE_SIZE_FOR_LLM // 1000}KB — needs manual fix", []
    trimmed = body[:_MAX_CHARS_PER_FILE]
    err_lines = "\n".join(f"  line {e['line']}: {e['message']}" for e in errors[:30])
    user = (
        f"---FILE: {path}---\n"
        f"---COMPILE ERRORS ({len(errors)} total, showing up to 30)---\n"
        f"{err_lines}\n\n"
        f"---CURRENT SOURCE---\n```java\n{trimmed}\n```"
    )
    try:
        resp = await fabric_call(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            agent_key=agent_key,
            temperature=0.1,
            max_tokens=8000,
        )
    except Exception as e:
        return False, f"fabric_call failed: {e}", []
    text = (resp or {}).get("content") if isinstance(resp, dict) else str(resp or "")
    if not text:
        return False, "empty model response", []
    try:
        # response can be a lone object OR an array of one — handle both
        payload = _extract_json_array(text) or _strip_json_fence(text)
        data = json.loads(payload)
    except Exception:
        return False, "non-JSON model response", []
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return False, "response not an object", []
    action = str(data.get("action") or "leave").lower()
    if action != "rewrite":
        return False, "model chose to leave file unchanged", []
    new_content = data.get("content") or ""
    changes = [str(c) for c in (data.get("changes") or [])]
    ok, reason = _safe_apply(path, body, new_content)
    return ok, reason, changes


async def build_and_fix(
    dest_root: Path,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    agent_key: str = "dcte.build_fixer",
    progress_cb: Callable[[int, int, str, str], None] | None = None,
) -> BuildResult:
    """Compile ``dest_root``; on failure, ask the LLM to fix each failing
    file and retry. Returns a :class:`BuildResult` describing the outcome.
    """
    res = BuildResult()
    tool = _detect_build_tool(dest_root)
    res.tool = tool
    if tool is None:
        res.skipped = True
        res.notes.append("no pom.xml or build.gradle found — build validation skipped")
        return res
    cmd = _maven_cmd(dest_root) if tool == "maven" else _gradle_cmd(dest_root)
    if not cmd:
        res.skipped = True
        res.notes.append(f"{tool} binary not on PATH — build validation skipped")
        return res

    res.attempted = True

    try:
        from llm import fabric_call
    except Exception as e:
        res.notes.append(f"fabric_call unavailable: {e}")
        # We can still run the build; we just can't fix.
        rc, output = await _run_build(cmd, dest_root)
        res.attempts = 1
        res.final_output_tail = output[-4000:]
        res.success = rc == 0
        if not res.success:
            res.errors = _parse_errors(output, dest_root)
        return res

    for attempt in range(1, max_attempts + 1):
        res.attempts = attempt
        if progress_cb:
            try:
                progress_cb(attempt, max_attempts, "", f"build attempt {attempt}/{max_attempts}")
            except Exception:
                pass
        rc, output = await _run_build(cmd, dest_root)
        res.final_output_tail = output[-4000:]
        if rc == 0:
            res.success = True
            res.notes.append(f"build succeeded on attempt {attempt}")
            if progress_cb:
                try:
                    progress_cb(attempt, max_attempts, "", "build succeeded")
                except Exception:
                    pass
            return res

        errs = _parse_errors(output, dest_root)
        res.errors = errs
        if not errs:
            # Build failed but we can't attribute to any owned file —
            # give up (probably a pom / dependency / classpath problem).
            res.notes.append(f"attempt {attempt} failed with rc={rc} but no fixable javac errors were parsed")
            if progress_cb:
                try:
                    progress_cb(attempt, max_attempts, "",
                                f"failed rc={rc} — no fixable errors")
                except Exception:
                    pass
            return res

        by_file = _group_errors_by_file(errs)
        # Prioritise files with the most errors first — usually the root cause.
        ranked = sorted(by_file.items(), key=lambda kv: -len(kv[1]))
        ranked = ranked[:_MAX_FIXES_PER_ATTEMPT]

        fixed_this_round = 0
        for i, (fpath, ferrs) in enumerate(ranked, start=1):
            if progress_cb:
                try:
                    progress_cb(attempt, max_attempts, Path(fpath).name,
                                f"fixing file {i}/{len(ranked)} ({len(ferrs)} err)")
                except Exception:
                    pass
            applied, reason, changes = await _fix_one_file(
                fabric_call, Path(fpath), ferrs, agent_key=agent_key,
            )
            if applied:
                fixed_this_round += 1
                res.fixes_applied += 1
                res.notes.append(f"attempt {attempt}: fixed {Path(fpath).name} ({', '.join(changes[:2])})")
            else:
                res.notes.append(f"attempt {attempt}: could NOT fix {Path(fpath).name} — {reason}")

        if fixed_this_round == 0:
            res.notes.append(f"attempt {attempt}: no files were fixable — stopping loop")
            if progress_cb:
                try:
                    progress_cb(attempt, max_attempts, "",
                                "no progress — giving up")
                except Exception:
                    pass
            return res

    # Final compile check after last fix pass (max_attempts exhausted)
    res.notes.append(f"max_attempts={max_attempts} reached without a green build")
    return res
