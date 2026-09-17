"""DCTE Build Agent — iter-18.11.

Runs `mvn compile` (or `gradle build`) on the AI-transformed output and, if
the build fails, feeds each failing file's compile errors back to the LLM
with a "fix these errors" prompt. Loops up to ``max_attempts`` times.

Design notes
------------
* Runs INSIDE the LAMA container (Maven + Temurin **25** are pre-installed;
  iter-22 raised the image from JDK 17 because `stacks.py` pins
  `spring-boot-4` to Java 25 and the image could not compile the output of
  its own recommended target). The release level is no longer forced on the
  command line: `_release_for` takes it from the target stack and forces
  nothing when the installed JDK is older, because compiling Java 25 source
  at level 17 turns every modern construct into a syntax error that the fix
  loop then "repairs".
* Non-blocking: if no build manifest is detected (no ``pom.xml`` /
  ``build.gradle`` / ``package.json`` / ``*.csproj``) or the tool binary is
  not on ``PATH``, the agent emits a diagnostic and returns
  ``{"skipped": True, ...}`` so the overall job does NOT fail.
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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ai_refactor import _safe_apply, _strip_json_fence, _extract_json_array
from .prompt_builder import build_build_fixer_brief, stack_sections
from .prompt_store import compose, get_dcte_prompt
from .stacks import get_stack, residue_markers

logger = logging.getLogger("lama.dcte.build_agent")

# ── Config ──────────────────────────────────────────────────────────
_DEFAULT_MAX_ATTEMPTS = 5
_BUILD_TIMEOUT_S = 480                # single mvn/gradle invocation
_MAX_FIXES_PER_ATTEMPT = 12           # cap fabric calls per attempt
_MAX_FILE_SIZE_FOR_LLM = 40000
# iter-22 — matched to the eligibility ceiling for the same reason as
# `ai_refactor._MAX_CHARS_PER_FILE`: at 16 000 every file in the 16–40 KB
# band was sent truncated with nothing saying so, and the reply was then
# size-checked against the full body. A file is now either sent whole or
# not sent at all.
_MAX_CHARS_PER_FILE = _MAX_FILE_SIZE_FOR_LLM
# iter-22 — the extension is a group rather than a literal `\.java`, so the
# same two patterns pick up tsc/dotnet diagnostics. Without this, an npm or
# dotnet build could fail and `_parse_errors` would attribute nothing, which
# `build_and_fix` reads as "no fixable errors" and gives up after one attempt.
_SRC_EXT = r"(?:java|kt|ts|tsx|js|jsx|cs|vb)"
_JAVAC_ERR_RE = re.compile(
    rf"(?P<path>[/A-Za-z0-9_\-.\\ ]+\.{_SRC_EXT}):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.+)",
)
# javac -Xdiags plain output, and tsc's `file.ts(12,5): error TS2345: …`
_JAVAC_ERR_RE_ALT = re.compile(
    rf"(?P<path>[/A-Za-z0-9_\-.\\ ]+\.{_SRC_EXT}):(?P<line>\d+):\s+error[^:]*:\s+(?P<msg>.+)",
)
_TSC_ERR_RE = re.compile(
    rf"(?P<path>[/A-Za-z0-9_\-.\\ ]+\.{_SRC_EXT})\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<msg>.+)",
)

# Language level the target stack asks for, read off `Stack.manifest` /
# `Stack.language` rather than hardcoded. Only Java has a compiler flag we
# would want to force; everything else takes its level from its own manifest.
_JAVA_RELEASE_RE = re.compile(r"(?:release|Java)\s*[=\s]\s*(\d{2})", re.IGNORECASE)


# iter-22 — the hardcoded Spring Boot 3.x / Java 17-21 essay that used to
# live here is gone; the brief is built per (source, target) in
# `prompt_builder.build_build_fixer_brief`. See that function for why.
# Kept as the floor for a caller with no pair in hand.
_SYSTEM_FALLBACK = """You are a Senior Backend Developer triaging BUILD ERRORS.

You are given ONE source file and the exact compiler errors it produced.
Return the FULL corrected source so those errors go away.

  1. Do NOT alter business logic — variable names, branch semantics, HTTP
     verbs, URL paths, DB column names and DTO field names are preserved.
  2. Do NOT truncate. Return the file from its first line through its last.
  3. Fix only what the compiler complained about, plus the imports or types
     that fix requires. Do not restructure unrelated code.
  4. A wall of "cannot find symbol" under ONE "package does not exist" is
     ONE fault, not fifty. Fix the cause, not each symptom.

RESPONSE FORMAT — STRICT JSON, one object, no prose, no code fences:
  {"file": "<absolute path echoed back exactly>",
   "action": "rewrite" | "leave",
   "content": "<full corrected file source when action==rewrite>",
   "changes": ["short bullet", "short bullet"],
   "risk": "low" | "medium" | "high"}

If you genuinely cannot fix it without more context, return
`"action":"leave"` with empty `content` and one line in `changes` saying
what you would need."""


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

# iter-22 — manifest → tool. Was root-only Maven/Gradle, so every .NET,
# React and Angular target the iter-21 catalogue added reported
# "no pom.xml or build.gradle found — build validation skipped" and the job
# carried on as though there were nothing to build. The LAMA image already
# ships Node and the dotnet SDK (see Dockerfile), so these really can run.
#
# Order matters: a polyglot tree with both a pom and a package.json is a
# backend with a bundled UI, and the backend is what the compile gate is
# for. `_MANIFEST_TOOLS` is checked in sequence.
_MANIFEST_TOOLS: tuple[tuple[str, str], ...] = (
    ("pom.xml", "maven"),
    ("build.gradle", "gradle"),
    ("build.gradle.kts", "gradle"),
    ("package.json", "npm"),
)

# How deep to look for a manifest below the destination root. A plugin that
# lands the service under `converted-source/<svc>/` puts the pom at depth 0,
# but a multi-module source tree carried across keeps its own nesting and
# the root-only check missed every one of those.
_MANIFEST_SEARCH_DEPTH = 3


def _detect_build_tool(dest_root: Path) -> tuple[str, Path] | None:
    """Return (tool, directory-holding-the-manifest) or None."""
    for depth_dir in _candidate_dirs(dest_root):
        for name, tool in _MANIFEST_TOOLS:
            if (depth_dir / name).is_file():
                return tool, depth_dir
        if any(depth_dir.glob("*.csproj")) or any(depth_dir.glob("*.sln")):
            return "dotnet", depth_dir
    return None


def _candidate_dirs(root: Path) -> list[Path]:
    """`root` first, then subdirectories up to `_MANIFEST_SEARCH_DEPTH`."""
    out = [root]
    if not root.is_dir():
        return out
    for d in sorted(root.rglob("*")):
        if not d.is_dir() or d.name in {"node_modules", "target", "build", ".git"}:
            continue
        try:
            if len(d.relative_to(root).parts) <= _MANIFEST_SEARCH_DEPTH:
                out.append(d)
        except ValueError:  # pragma: no cover — defensive
            continue
    return out


def _maven_cmd(_dest_root: Path, release: str = "") -> list[str] | None:
    exe = shutil.which("mvn")
    if not exe:
        return None
    # iter-22 — the release level used to be hardcoded to 17 regardless of
    # the target. With `spring-boot-4` (Java 25) in the catalogue that meant
    # compiling Java 25 source against a Java 17 language level: every
    # record pattern and sealed type became a syntax error, the fix loop
    # dutifully "repaired" correct code, and the migration got worse each
    # round. Pass nothing when we do not know — the pom's own setting is a
    # better answer than a guess.
    #
    # `-U` per the iter-21 contract: Maven caches a FAILED resolution as a
    # negative entry for 24h, so after a pom repair the very next build can
    # still report the artifact missing.
    cmd = [exe, "-B", "-q", "-U", "-DskipTests=true", "-Dmaven.test.skip=true"]
    if release:
        cmd += [f"-Dmaven.compiler.source={release}",
                f"-Dmaven.compiler.target={release}",
                f"-Dmaven.compiler.release={release}"]
    cmd.append("compile")
    return cmd


def _gradle_cmd(dest_root: Path, _release: str = "") -> list[str] | None:
    wrap = dest_root / "gradlew"
    if wrap.is_file():
        return [str(wrap), "--no-daemon", "-x", "test", "compileJava"]
    exe = shutil.which("gradle")
    if not exe:
        return None
    return [exe, "--no-daemon", "-x", "test", "compileJava"]


def _npm_cmd(dest_root: Path, _release: str = "") -> list[str] | None:
    """`npm run build` when the project declares one, else a type-check.

    A React/Angular migration that emits no build script is not buildable,
    and saying "skipped" there would hide exactly the gap the operator needs
    to see — so we fall back to `tsc --noEmit`, which still catches the
    import and type errors a half-finished migration leaves behind.
    """
    exe = shutil.which("npm")
    if not exe:
        return None
    try:
        pkg = json.loads((dest_root / "package.json").read_text(encoding="utf-8"))
        scripts = pkg.get("scripts") or {}
    except Exception:  # noqa: BLE001
        scripts = {}
    if "build" in scripts:
        return [exe, "run", "--silent", "build"]
    if (dest_root / "tsconfig.json").is_file():
        npx = shutil.which("npx")
        if npx:
            return [npx, "--yes", "tsc", "--noEmit"]
    return None


def _dotnet_cmd(_dest_root: Path, _release: str = "") -> list[str] | None:
    exe = shutil.which("dotnet")
    if not exe:
        return None
    return [exe, "build", "--nologo", "-v", "quiet"]


_TOOL_CMD_BUILDERS = {
    "maven": _maven_cmd,
    "gradle": _gradle_cmd,
    "npm": _npm_cmd,
    "dotnet": _dotnet_cmd,
}


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
    for rx in (_JAVAC_ERR_RE, _JAVAC_ERR_RE_ALT, _TSC_ERR_RE):
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


def _release_for(target_stack: str) -> tuple[str, str]:
    """(release-level-to-force, note-for-the-operator-and-the-model).

    Returns ("", "") when we should not force anything — which is the right
    answer whenever the target is not Java, or when the installed JDK
    already satisfies what the target pins.

    The note exists because the LAMA image ships JDK 17 while the catalogue
    pins Java 25 for `spring-boot-4`. Compiling Java 25 source at release 17
    turns every modern language feature into a syntax error, and the fix
    loop then "repairs" correct code — each round making the migration
    worse. Telling both the operator and the triage model that the
    validating compiler is older than the target is the honest alternative
    to silently forcing a level.
    """
    tgt = get_stack(target_stack)
    if tgt is None or "java" not in (tgt.language or "").lower():
        return "", ""
    wanted = ""
    m = _JAVA_RELEASE_RE.search(tgt.language or "")
    if m:
        wanted = m.group(1)
    if not wanted:
        for entry in tgt.manifest:
            m = _JAVA_RELEASE_RE.search(entry)
            if m:
                wanted = m.group(1)
                break
    if not wanted:
        return "", ""
    installed = _installed_jdk_major()
    if installed and int(wanted) > installed:
        return "", (
            f"target pins Java {wanted} but the JDK on PATH is {installed}; "
            "compiling at the pom's own level and reporting the mismatch "
            "rather than forcing a lower release — a feature this JDK cannot "
            "parse is a TOOLCHAIN gap, not a defect in the migrated source"
        )
    return wanted, ""


def _installed_jdk_major() -> int | None:
    """Major version of the `javac` on PATH, or None."""
    exe = shutil.which("javac")
    if not exe:
        return None
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [exe, "-version"], capture_output=True, text=True, timeout=20,
        )
        blob = f"{out.stdout} {out.stderr}"
        m = re.search(r"javac\s+(\d+)", blob)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


async def _build_fixer_prompt(source_stack: str, target_stack: str,
                              release_note: str = "") -> str:
    """Per-pair triage brief.

    iter-22 (DT-2) — prefers the operator-editable `dcte.build_fixer` row
    from Prompt Library, splicing the pair-specific sections into it, and
    falls back to the module-built brief and then to `_SYSTEM_FALLBACK`.
    """
    if not source_stack and not target_stack:
        return _SYSTEM_FALLBACK
    try:
        built = build_build_fixer_brief(source_stack, target_stack, release_note)
    except Exception:  # noqa: BLE001 — a prompt bug must not kill the build
        logger.warning("build-fixer brief could not be built; using the fallback")
        return _SYSTEM_FALLBACK
    template = await get_dcte_prompt("dcte.build_fixer")
    prompt = compose(template, stack_sections(source_stack, target_stack), built)
    # The toolchain note is appended AFTER any operator edit: it is a fact
    # about this run, not guidance, and must not be editable away.
    return f"{prompt}\n\nTOOLCHAIN\n  {release_note}" if release_note else prompt


# ── LLM fix loop ────────────────────────────────────────────────────

async def _fix_one_file(
    fabric_call, path: Path, errors: list[dict[str, Any]],
    agent_key: str,
    system_prompt: str = "",
    markers: "tuple[str, ...] | None" = None,
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
    # iter-22 — same silent-truncation defect the AI sweep had: the file was
    # cut to 16 000 chars with nothing in the prompt saying so, and the reply
    # was then size-checked against the FULL body. `_MAX_CHARS_PER_FILE` now
    # matches the eligibility ceiling so this should not fire, and if it ever
    # does the model is told, and `_safe_apply` refuses the reply rather than
    # writing back a half file.
    trunc_note = ""
    if len(trimmed) < len(body):
        trunc_note = (
            f"\n---!!! TRUNCATED: you are seeing the first {len(trimmed)} of "
            f"{len(body)} characters. This is NOT the whole file. Return "
            '"action":"leave" — a partial rewrite cannot be merged. ---'
        )
    lang_tag = path.suffix.lstrip(".") or ""
    user = (
        f"---FILE: {path}---{trunc_note}\n"
        f"---COMPILE ERRORS ({len(errors)} total, showing up to 30)---\n"
        f"{err_lines}\n\n"
        f"---CURRENT SOURCE---\n```{lang_tag}\n{trimmed}\n```"
    )
    try:
        resp = await fabric_call(
            messages=[
                {"role": "system", "content": system_prompt or _SYSTEM_FALLBACK},
                {"role": "user", "content": user},
            ],
            agent_key=agent_key,
            temperature=0.1,
            max_tokens=8000,
            response_format={"type": "json_object"},
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
    ok, reason = _safe_apply(path, body, new_content, markers, len(trimmed))
    return ok, reason, changes


async def build_and_fix(
    dest_root: Path,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    agent_key: str = "dcte.build_fixer",
    progress_cb: Callable[[int, int, str, str], None] | None = None,
    source_stack: str = "",
    target_stack: str = "",
) -> BuildResult:
    """Compile ``dest_root``; on failure, ask the LLM to fix each failing
    file and retry. Returns a :class:`BuildResult` describing the outcome.

    iter-22 — takes the selected stack pair. The triage prompt, the
    compiler release level and the residue reject list are all built from
    it; before this they were hardcoded to Spring Boot 3 / Java 17 / Helidon
    regardless of what the operator actually chose.
    """
    res = BuildResult()
    detected = _detect_build_tool(dest_root)
    if detected is None:
        res.skipped = True
        res.notes.append(
            "no build manifest (pom.xml / build.gradle / package.json / *.csproj) "
            f"found under {dest_root.name} — build validation skipped"
        )
        return res
    tool, manifest_dir = detected
    res.tool = tool
    release, release_note = _release_for(target_stack)
    builder = _TOOL_CMD_BUILDERS.get(tool)
    cmd = builder(manifest_dir, release) if builder else None
    if not cmd:
        res.skipped = True
        res.notes.append(
            f"{tool} has no runnable build command here "
            "(binary missing from PATH, or no build script declared) "
            "— build validation skipped"
        )
        return res
    # Everything below compiles in the directory that holds the manifest,
    # which is not necessarily `dest_root` on a multi-module tree.
    dest_root = manifest_dir
    if release_note:
        res.notes.append(release_note)

    system_prompt = await _build_fixer_prompt(source_stack, target_stack, release_note)
    markers = residue_markers(source_stack, target_stack) or None

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
                system_prompt=system_prompt, markers=markers,
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

    # Final compile check after the last fix pass.
    #
    # iter-22 — this comment described a check that was never written: the
    # loop applied fixes on its final attempt and then returned
    # `success=False` without ever recompiling. A build that the last round
    # actually repaired was reported red, the engine emitted "Build did NOT
    # go green … manual intervention required", and the operator was sent to
    # debug a green build.
    rc, output = await _run_build(cmd, dest_root)
    res.final_output_tail = output[-4000:]
    if rc == 0:
        res.success = True
        res.notes.append(
            f"build succeeded on the final check after {max_attempts} fix round(s)"
        )
        if progress_cb:
            try:
                progress_cb(max_attempts, max_attempts, "", "build succeeded")
            except Exception:
                pass
        return res
    res.errors = _parse_errors(output, dest_root)
    res.notes.append(f"max_attempts={max_attempts} reached without a green build")
    return res
