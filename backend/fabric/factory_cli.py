"""
iter-13.124 — Factory AI **CLI** adapter (alternative to the HTTP API path).

This module is the local, no-Computer, no-network-roundtrip drop-in for
`factory_orchestrator.route_via_factory_orchestrator`. It shells out to the
`droid` CLI (`droid exec ...`), which Factory ships as the official
non-interactive entry point to its agent.

When to use this instead of the API path
----------------------------------------
* The LAMA host already has `droid` installed and `droid auth login` has
  been completed once (creds live in `~/.factory/`). Saves you from
  managing per-tenant app keys + Computer sandbox sessions.
* You do NOT need the Computer/Sandbox isolation — the CLI runs on the
  LAMA host directly with full filesystem access, so the workspace IS
  just a normal local folder.
* You want a much smaller surface area: no httpx, no Computer wake-up
  loop, no 424 / 502 / stale-session classification.

Wiring
------
Opt-in via env var `LAMA_FACTORY_MODE=cli`. When set, the public
`route_via_factory_orchestrator()` in `factory_orchestrator.py` delegates
here. Default (unset / "api") keeps the existing HTTP-API behaviour.

Output contract
---------------
Returns the same shape that `route_via_factory_orchestrator` returns so
`llm.fabric_call` doesn't care which mode is active:
    {
      "content": str,
      "model":   str,
      "usage":   {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
      "session_id": str | None,
      "via": "factory-cli",
    }

Trade-offs vs the API path
--------------------------
+ No HTTP. No 424s. No "computer asleep" wake loops. No app-key rotation.
+ Works offline-ish (the CLI still needs Factory's backend, but auth is
  local and a single `droid auth login` covers every project).
+ Simpler to debug — `docker exec lama droid exec "ping"` reproduces it.
- droid auth is HOST-WIDE, not per-tenant. In a multi-tenant LAMA install
  every project would share the same Factory account. LAMA is currently
  single-tenant single-project so this is fine, but flag it before you
  scale out.
- No Computer sandbox = the agent has direct shell access on the LAMA
  host. Always run with `--auto low` (default below) unless you trust
  the prompt + agent combination.
- droid is an external binary; it MUST be present on PATH (or set
  `LAMA_FACTORY_CLI_BIN`). The Dockerfile needs an install line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("lama.factory.cli")

# ---------------------------------------------------------------------
# Configuration knobs (env-driven)
# ---------------------------------------------------------------------

# Where the `droid` binary lives. Falls back to PATH lookup.
FACTORY_CLI_BIN = os.environ.get("LAMA_FACTORY_CLI_BIN", "").strip() or "droid"

# Autonomy level passed to `droid exec --auto <level>`. We default to
# "low" which lets the agent create/modify files in the workspace but
# blocks `sudo` / package installs / git push. Operators can raise it
# to "medium" or "high" per-project via the Console (todo: wire field).
FACTORY_CLI_AUTO = (os.environ.get("LAMA_FACTORY_CLI_AUTO", "low") or "low").strip().lower()
if FACTORY_CLI_AUTO not in ("low", "medium", "high"):
    FACTORY_CLI_AUTO = "low"

# Hard cap so a runaway agent can't burn the box for an hour.
# iter-14.8 — default bumped from 300s → 600s. The 300s ceiling was
# calibrated for short prompts, but real LAMA workloads (SRS section
# generation, DataModel DDL synthesis) send 80-150K-token prompts to
# claude-sonnet-4-6 and legitimately need 4-6 minutes of inference.
# The floor bug we fixed in iter-14.7 unmasked this — the schedule
# in routes/srs.py (240/200/150s) was already too tight for real
# content, but got hidden by the floor forcing every call to 300s.
# Operators who want the old behaviour can still set
# LAMA_FACTORY_CLI_TIMEOUT_SEC=300 in .env; those who need more
# headroom (very large KBs, opus-tier models) can go to 900 or 1200.
FACTORY_CLI_TIMEOUT_SEC = float(os.environ.get("LAMA_FACTORY_CLI_TIMEOUT_SEC", "600") or "600")

# Where droid keeps the project workspace. Defaults to LAMA's per-project
# generated-code dir; falls back to the system temp dir if unset.
FACTORY_CLI_DEFAULT_CWD = os.environ.get("LAMA_FACTORY_CLI_CWD", "").strip()

# iter-14.94 — Console "Test connection" auth-probe timeout. Was hardcoded
# to 20s, which is too tight for droid CLI's cold-start path: the FIRST
# `droid exec` invocation in a container (or after the CLI/daemon has been
# idle) pays for an update-check + auth-token refresh round-trip to
# Factory's API before it even reaches the prompt, and that round-trip is
# subject to normal network / corporate-proxy latency. Operators kept
# reporting "droid is running but Console says the probe timed out" even
# though a manual `droid exec` from a shell succeeded a few seconds later
# once the connection warmed up. Made configurable + raised the default so
# a single slow cold-start doesn't look like a hard outage.
#
# iter-15.48 — Bumped default 45 → 180. On macOS Docker Desktop the first
# `droid exec` after container start commonly runs 90-150s (auth-token
# refresh + Factory API round-trip across the VM boundary). 45s was
# consistently short → Console flapped between UP and DOWN, and the FE's
# Prompt & Model dropdown regressed to the Ollama catalogue every time
# the probe returned TimeoutError.
FACTORY_CLI_PROBE_TIMEOUT_SEC = float(
    os.environ.get("LAMA_FACTORY_CLI_PROBE_TIMEOUT_SEC", "180") or "180"
)

# iter-15.48 — Probe-result cache. The Console page polls
# `/api/console/factory-orchestrator/test` every ~5s while open (each
# navigation to Console, plus the Transformer page and a few dashboard
# widgets). Without a cache every poll spawns a fresh `droid exec`
# subprocess, and every one of those pays the same cold-start round-trip
# → the CLI becomes the bottleneck, load average spikes, and the SECOND
# concurrent probe usually times out because droid serialises auth-token
# access. Cache successful probes for 5 min (env override:
# LAMA_FACTORY_CLI_PROBE_SUCCESS_TTL_SEC) and failures for 30s
# (LAMA_FACTORY_CLI_PROBE_FAILURE_TTL_SEC) — short enough that a real
# outage still surfaces quickly, long enough that Console polls hit the
# cache instead of spawning subprocesses.
FACTORY_CLI_PROBE_SUCCESS_TTL_SEC = float(
    os.environ.get("LAMA_FACTORY_CLI_PROBE_SUCCESS_TTL_SEC", "300") or "300"
)
FACTORY_CLI_PROBE_FAILURE_TTL_SEC = float(
    os.environ.get("LAMA_FACTORY_CLI_PROBE_FAILURE_TTL_SEC", "30") or "30"
)
# iter-15.48 — Sticky-success window. When the auth probe times out but
# we had a successful probe within this window, treat the CLI as OK
# instead of flipping the Console UI to DOWN. Rationale: droid is
# actually up (we proved it minutes ago) and the current timeout is a
# cold-start artefact, not a real outage. Set to 0 to disable.
FACTORY_CLI_PROBE_STICKY_SUCCESS_SEC = float(
    os.environ.get("LAMA_FACTORY_CLI_PROBE_STICKY_SUCCESS_SEC", "1800") or "1800"
)

# Module-level cache. Keyed by `effective_bin` so a per-project bin
# override with a different path gets its own cache slot.
#   { bin_path: {"result": <dict>, "expires_at": <epoch>, "last_success_at": <epoch>} }
_PROBE_CACHE: Dict[str, Dict[str, Any]] = {}
_PROBE_CACHE_LOCK = asyncio.Lock()


def is_cli_mode_enabled() -> bool:
    """True when env vars opt this module in for routing."""
    return (os.environ.get("LAMA_FACTORY_MODE", "") or "").strip().lower() == "cli"


def cli_binary_available() -> bool:
    """Lightweight `which` check used by the readiness probe."""
    return _binary_resolves(FACTORY_CLI_BIN)


def _binary_resolves(path_or_name: str) -> bool:
    """Same logic as cli_binary_available() but for an arbitrary binary
    string — used for per-project overrides set via the Console UI."""
    if not path_or_name:
        return False
    if os.path.isabs(path_or_name):
        return os.path.exists(path_or_name) and os.access(path_or_name, os.X_OK)
    return shutil.which(path_or_name) is not None


# iter-13.126 — Smart binary resolver with graceful fallback.
#
# Why: per-project `cli_bin` is persisted in MongoDB. When the operator
# switches host (laptop → container, macOS path → linux path) the saved
# value can become stale, e.g. "/Users/foo/.local/bin/droid" doesn't
# exist inside the linux container even though the bind-mounted
# `/usr/local/bin/droid` does. Previously this caused
# `factory.ai · DOWN · /Users/.../droid not found on PATH` even though
# a perfectly working `droid` binary was available at the env-var
# default. We now try, in order:
#   1. The exact candidate path the caller gave us (preserves explicit
#      operator intent — if they typed an absolute path that exists,
#      use it).
#   2. The env-var default `FACTORY_CLI_BIN` (`/usr/local/bin/droid`
#      in the bundled docker image after iter-13.125).
#   3. `shutil.which("droid")` against the current PATH.
# Returns the first absolute path that resolves, or `""` when nothing
# works.
def _resolve_binary_with_fallback(candidate: str) -> str:
    """Return the first executable binary path among the candidate /
    env-default / PATH-lookup chain. Empty string when nothing resolves.

    Used by the readiness probe AND the actual exec path so a stale
    per-project `cli_bin` value never blocks the call when a working
    `droid` is reachable via the env-var default or `shutil.which`.
    """
    tried: List[str] = []
    candidates = [
        (candidate or "").strip(),
        (FACTORY_CLI_BIN or "").strip(),
        "droid",
    ]
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        tried.append(c)
        if os.path.isabs(c):
            if os.path.exists(c) and os.access(c, os.X_OK):
                return c
        else:
            resolved = shutil.which(c)
            if resolved:
                return resolved
    return ""


def _slim_flag_enabled() -> bool:
    """iter-14.9 — When True, `_flatten_messages_to_prompt` strips the
    embedded KB / TOON / legacy-index / analysis-digest blocks from
    outgoing prompts and appends a short footer that points droid at
    the pre-materialized `.lama/` context bundle in its `--cwd`.

    Rationale: the operator asked to stop shipping 300K-char prompts
    every call (huge token cost, most of it duplicated across sections)
    and instead materialize context ONCE at Build KB time. See
    `backend/kb/droid_workspace.py`.

    Opt-in for now so existing users (API path, OpenRouter-only) are
    not affected: set `LAMA_FACTORY_CLI_SLIM=1` in the compose env to
    activate. The env var is read on every call so operators can
    toggle without restarting the backend."""
    return (
        os.environ.get("LAMA_FACTORY_CLI_SLIM", "0") or "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


# Sentinel markers we detect and STRIP from the prompt in slim mode.
# Anything between a start marker (first line beginning with the
# pattern) and the next "======" divider is removed. These match the
# actual banner strings used in routes/srs.py::_build_section_prompt
# and routes/datamodel.py::_build_ddl_prompt. Keeping the strip
# marker-based (rather than character-count-based) means we never
# accidentally drop the tail of the *instructions*.
_SLIM_STRIP_MARKERS = (
    "LEGACY FILE INDEX",
    "KNOWLEDGE BASE / TOON",
    "KNOWLEDGE BASE (TOON",
    "DEEP LEGACY ANALYSIS DIGEST",
    "BUSINESS ONTOLOGY",
    "TOON SKELETON",
    "PROJECT CONTEXT (TOON)",
)

# iter-14.11 — Real block START anchors from the actual SRS prompt.
#
# These are matched at the BEGINNING of a stripped line (after
# lstripping "# - " decorations) — NOT as substrings — so mentions of
# "LEGACY FILE INDEX" in the middle of a sentence don't falsely
# trigger a strip. Each entry MUST uniquely appear at column 0 of a
# real fat block; we assert this against the recorded prompt dump.
_SLIM_BLOCK_START_PREFIXES = (
    "PROJECT-ISOLATED LEGACY WORKSPACE",         # ~200K chars — legacy workspace files (main offender)
    "PROJECT-ISOLATED WORKSPACE (iter-13.99",    # Early instructions variant (line 2 of prompt)
    "KNOWLEDGE BASE:",                            # ~30K chars — TOON individuals dump
    "STRUCTURAL SKELETON (TOON",                  # Sub-marker inside KB block (safety net)
    "DEEP LEGACY ANALYSIS DIGEST",
    "BUSINESS ONTOLOGY (LLM-ENRICHED",
    "LEGACY FILE INDEX (top files",               # Only the actual banner form, not prose
    "PROJECT CONTEXT (TOON",
    "RAG HITS FOR SECTION",
    "INLINE LEGACY FILES",
    "ATTACHED LEGACY FILES",
    "GRAPH SUBGRAPH FOR SECTION",
)

# iter-14.11 — a stripped block STOPS at the FIRST line matching any
# of these hard-end anchors (again anchored at line start). This lets
# us walk right past the internal `─── path: …` file dividers and
# multiple `══` bars that live inside a workspace block.
_SLIM_BLOCK_END_PREFIXES = (
    "# User",                                     # Message role separator
    "# Assistant",
    "# System",
    "Write Section ",                             # SRS section-produce directive
    "Produce the ",                               # SRS produce directive
    "PRODUCE THE ",
    "LENGTH TARGET:",
    "FORMAT:",
    "HARD CONSTRAINTS:",
    "WRITING GUIDANCE",
    "OUTPUT STRUCTURE",
    "STORYTELLING VOICE",
    "AGENTIC AUTONOMY",
    "SELF-CONFIDENCE",
    "MANDATORY GOVERNANCE BUNDLE",
    "SRS AUTHORING CHARTER",
    "SRS PROMPT",
    "TARGET STACK GUARDRAILS",
    "LEGACY COVERAGE CHECKLIST",
    # NOTE: bare `══` divider is deliberately NOT an END anchor —
    # it would prematurely fire on the banner-bottom divider that
    # immediately follows a banner header (iter-14.10 regression).
    # Real block terminations are covered by the explicit anchors
    # above plus the next block START anchor in the outer loop.
)


_SLIM_FOOTER = (
    "\n\n══════════════════════════════════════════════════════════════════════\n"
    "CONTEXT LOOKUP  (LAMA slim-CLI mode — iter-14.9)\n"
    "══════════════════════════════════════════════════════════════════════\n"
    "The knowledge base for this project has been pre-materialized to your\n"
    "current working directory. **Read these files first**, then produce\n"
    "the requested output:\n"
    "  • `./.lama/README.md`            (start here — how to use this bundle)\n"
    "  • `./.lama/kb_summary.md`        (tech stack, high-level shape)\n"
    "  • `./.lama/file_index.md`        (authoritative list of source files)\n"
    "  • `./.lama/kb_toon.txt`          (compact class/table/route index)\n"
    "  • `./.lama/analysis_digest.md`   (deep behaviour digest; may be absent)\n"
    "\n"
    "GROUNDING RULE: cite ONLY paths that appear verbatim in\n"
    "`file_index.md`. Do NOT invent filenames, tables or classes from your\n"
    "training data — this codebase is single-tenant and any off-index\n"
    "reference is a cross-project contamination bug that will be caught\n"
    "by the LAMA completeness gate.\n"
)


def _slim_strip_context_blocks(text: str) -> str:
    """Strip fat KB / workspace / analysis blocks by START→END anchors.

    iter-14.11 rewrite. The previous version scanned for markers as
    SUBSTRINGS which produced two bugs:

      (a) False-positives — any prose sentence mentioning "LEGACY FILE
          INDEX" (e.g. inside the SRS authoring charter) got treated
          as a block start and swallowed subsequent instructions.
      (b) False-negatives — the actual fat blocks in the SRS prompt
          are headed by plain lines like `PROJECT-ISOLATED LEGACY
          WORKSPACE (iter-13.99 …):` or `KNOWLEDGE BASE:` — neither
          of which matched the previous marker list.

    Result: only 5.6% was stripped from a 300K-char prompt (verified
    from a live prompt dump under /tmp/droid-debug in the container).

    New rules:

      * A block START is detected ONLY when a line, stripped of
        leading `#`/`-`/spaces, BEGINS with one of the anchors in
        `_SLIM_BLOCK_START_PREFIXES`. Anchors are chosen so they
        match the banner form (e.g. `LEGACY FILE INDEX (top files`)
        and NOT prose references (e.g. `… the LEGACY FILE INDEX above
        …`).
      * A block ENDS at the FIRST subsequent line beginning with any
        anchor in `_SLIM_BLOCK_END_PREFIXES` — which includes hard
        role separators (`# User`, `# Assistant`), instruction
        headers (`Write Section`, `LENGTH TARGET:`, `HARD
        CONSTRAINTS:`), the next known governance block header, or a
        bare `══════` divider.
      * Consecutive-block chains (workspace → KB → analysis) are
        supported because each block-end anchor may itself be the
        START of the NEXT block (matched on the next iteration).

    Every stripped block is replaced with a single-line pointer so
    the LLM still knows the concept exists and can read the real
    data from the pre-materialised `./.lama/` workspace.
    """
    if not text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    skip = False
    for line in lines:
        stripped = line.lstrip("#- \t").rstrip()
        if not skip:
            # Detect block START by prefix at column 0 (after decorators).
            hit = next(
                (p for p in _SLIM_BLOCK_START_PREFIXES if stripped.startswith(p)),
                None,
            )
            if hit:
                out.append(line)
                out.append(
                    "  (LAMA slim-mode iter-14.11 — block content redacted. "
                    "Read `./.lama/` on your cwd for the data.)"
                )
                skip = True
                continue
            out.append(line)
            continue
        # In skip mode: look for a block END anchor.
        end_hit = next(
            (p for p in _SLIM_BLOCK_END_PREFIXES if stripped.startswith(p)),
            None,
        )
        if end_hit is not None:
            skip = False
            # The end-anchor line itself may ALSO be a new block START
            # (e.g. `══` divider between two govt bundles, or the
            # `KNOWLEDGE BASE:` header right after a workspace block).
            new_start = next(
                (p for p in _SLIM_BLOCK_START_PREFIXES if stripped.startswith(p)),
                None,
            )
            if new_start:
                out.append(line)
                out.append(
                    "  (LAMA slim-mode iter-14.11 — block content redacted. "
                    "Read `./.lama/` on your cwd for the data.)"
                )
                skip = True
                continue
            out.append(line)
            continue
        # Still inside the block — drop the line.
        continue
    return "\n".join(out)


def _flatten_messages_to_prompt(messages: List[Dict[str, Any]]) -> str:
    """Collapse an OpenAI-style messages list into a single prompt string.

    droid exec takes ONE prompt (via argv or stdin). We mimic the format
    `factory_orchestrator._build_prompt_text` uses so the agent sees a
    near-identical instruction context regardless of transport mode.

    iter-14.9 — when `LAMA_FACTORY_CLI_SLIM=1`, we also strip the big
    embedded KB / TOON / analysis blocks and append the workspace
    footer so droid reads the actual data from its own `--cwd` (which
    Build KB pre-populated via `backend/kb/droid_workspace.py`).
    """
    slim = _slim_flag_enabled()
    lines: List[str] = []
    for m in messages or []:
        role = (m.get("role") or "user").strip().lower()
        content = m.get("content")
        if content is None:
            continue
        if isinstance(content, list):
            # OpenAI multimodal — flatten text parts only.
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            content = "\n".join(t for t in text_parts if t)
        text = str(content).strip()
        if not text:
            continue
        if slim:
            text = _slim_strip_context_blocks(text)
        if role == "system":
            lines.append(f"# System\n{text}")
        elif role == "assistant":
            lines.append(f"# Assistant (prior)\n{text}")
        else:
            lines.append(f"# User\n{text}")
    result = "\n\n".join(lines).strip()
    if slim and result:
        result = result + _SLIM_FOOTER
    return result


def _estimate_tokens(text: str) -> int:
    """Rough char→token estimate; matches the heuristic the API path uses."""
    return max(1, len(text or "") // 4)


async def _run_droid_exec(
    *,
    prompt: str,
    cwd: str,
    model: Optional[str],
    auto_level: str,
    timeout: float,
    session_tag: Optional[str],
    bin_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Subprocess invocation of `droid exec`. Returns parsed JSON output
    augmented with execution metadata.

    Raises asyncio.TimeoutError on hard timeout, RuntimeError on
    non-zero exit (with stderr captured in the message).

    `bin_path` overrides the module-level FACTORY_CLI_BIN (set by the
    Console > Factory Orchestrator UI on a per-project basis).

    iter-13.126 — Uses `_resolve_binary_with_fallback` so a stale
    per-project `bin_path` (e.g. a macOS absolute path saved before the
    operator moved to the dockerised deploy) automatically falls
    through to the env-var default / PATH lookup instead of failing the
    whole stage with "exec format error" / "no such file".
    """
    effective_bin = _resolve_binary_with_fallback(bin_path or "")
    if not effective_bin:
        raise RuntimeError(
            "droid binary not found (tried per-project bin_path, "
            f"LAMA_FACTORY_CLI_BIN={FACTORY_CLI_BIN}, and PATH lookup). "
            "Install droid or set the binary path in Console → Factory Orchestrator."
        )
    args: List[str] = [
        effective_bin, "exec",
        "--output-format", "json",
        "--auto", auto_level,
        "--cwd", cwd,
    ]
    if model:
        args.extend(["-m", model])
    if session_tag:
        # Sessions are surfaced as named tags so subsequent calls within
        # the same LAMA stage can be searched/resumed via `droid search`.
        args.extend(["--tag", session_tag])
    # iter-13.127 — droid 0.102+ removed the `-` argv token that used to
    # mean "read prompt from stdin". Piping into `droid exec` without any
    # trailing argument is the officially supported replacement (see
    # `droid exec --help`). We still write the prompt to the child's
    # stdin below to bypass argv length limits and avoid shell-injection
    # footguns from prompt content with quotes / $ / backticks.

    log.info(
        "factory-cli launch: bin=%s auto=%s cwd=%s model=%s tag=%s prompt_chars=%d timeout_sec=%.1f",
        effective_bin, auto_level, cwd, model or "default",
        session_tag or "-", len(prompt), timeout,
    )
    # iter-14.6 — Debug prompt dump. Enabled via LAMA_FACTORY_CLI_DUMP_PROMPT=1.
    # Writes the exact prompt LAMA sends to droid so operators can replay it
    # with `droid exec` directly and compare wall-clock times.
    try:
        if os.environ.get("LAMA_FACTORY_CLI_DUMP_PROMPT", "").strip() in {"1", "true", "yes"}:
            import hashlib
            _dump_dir = os.environ.get("LAMA_FACTORY_CLI_DUMP_DIR", "/tmp/droid-debug")
            os.makedirs(_dump_dir, exist_ok=True)
            _tag = (session_tag or "unknown").replace("/", "_").replace(":", "_")
            _h = hashlib.md5(prompt.encode("utf-8", "replace")).hexdigest()[:8]
            _path = os.path.join(_dump_dir, f"{_tag}__{_h}.txt")
            with open(_path, "w", encoding="utf-8") as _f:
                _f.write(prompt)
            log.info("factory-cli: prompt dumped to %s (%d chars)", _path, len(prompt))
    except Exception as _dump_exc:  # noqa: BLE001
        log.warning("factory-cli prompt-dump failed: %s", _dump_exc)

    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Don't inherit LAMA's PATH munging; droid finds its own config.
        env={**os.environ},
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Best-effort kill so a stuck droid doesn't leak.
        with _suppress_all():
            proc.kill()
        raise

    elapsed_ms = int((time.time() - t0) * 1000)
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(
            f"droid exec exited {proc.returncode} after {elapsed_ms}ms: "
            f"{(stderr or stdout).strip()[:600]}"
        )

    # `droid exec --output-format json` prints a single JSON document on
    # stdout. We tolerate a leading banner / trailing newline.
    parsed: Dict[str, Any]
    try:
        parsed = _extract_last_json_object(stdout)
    except ValueError:
        # If droid didn't emit JSON (e.g. older version, plain-text fallback),
        # treat the entire stdout as the agent's response. Surface the
        # raw text to the caller rather than failing the call.
        log.warning(
            "factory-cli: stdout was not JSON (chars=%d) — falling back to plain text",
            len(stdout),
        )
        parsed = {"text": stdout.strip()}

    parsed["_elapsed_ms"] = elapsed_ms
    parsed["_stderr"] = stderr.strip()[:2000] if stderr else ""
    return parsed


class _suppress_all:
    """Context manager that swallows every exception. Used for best-effort
    cleanup paths where re-raising would mask the original error."""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True


def _extract_last_json_object(text: str) -> Dict[str, Any]:
    """Find the last balanced `{...}` in `text` and parse it. droid's
    json formatter may emit log lines before the final payload."""
    if not text:
        raise ValueError("empty")
    s = text.strip()
    # Fast path: entire stdout is one JSON doc.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Slow path: scan backwards for the start of the last top-level object.
    depth = 0
    end = -1
    for i in range(len(s) - 1, -1, -1):
        ch = s[i]
        if ch == "}":
            if end == -1:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end != -1:
                candidate = s[i:end + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    end = -1
                    depth = 0
                    continue
    raise ValueError("no JSON object found in droid output")


def _extract_response_text(parsed: Dict[str, Any]) -> str:
    """droid's JSON output isn't stable across versions. Probe the
    commonly-seen field names, then fall back to the raw `text`.

    iter-13.127 — droid 0.102+ emits the assistant reply under the
    `result` key (payload shape: `{"type":"result","subtype":"success",
    "is_error":false,...,"result":"...text..."}`). Older versions used
    `response` / `output` / `text` / `content`. We probe `result` FIRST
    for the modern CLI, then fall back to the older keys so the same
    adapter works across upgrades.
    """
    for key in ("result", "response", "output", "text", "content", "message", "final_message"):
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            inner = v.get("content") or v.get("text") or v.get("result")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    # Last-ditch: stringified parsed.
    return json.dumps(parsed, ensure_ascii=False)[:8000]


# ---------------------------------------------------------------------
# Public API — mirrors factory_orchestrator.route_via_factory_orchestrator
# ---------------------------------------------------------------------

async def route_via_factory_cli(
    *,
    messages: List[Dict[str, Any]],
    project_id: str,
    agent_key: str,
    timeout: float = 120.0,
    model_hint: Optional[str] = None,
    cwd: Optional[str] = None,
    cli_bin: Optional[str] = None,
    auto_level: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Drop-in replacement for the HTTP-API Factory route.

    Returns None (i.e. fall through to env-var OpenRouter) when the CLI
    isn't actually available, so a misconfigured host degrades the same
    way a misconfigured Factory project does today.

    `cli_bin` / `auto_level` are per-project overrides set via the
    Console > Factory Orchestrator tab. They fall back to the env-var
    defaults (LAMA_FACTORY_CLI_BIN / LAMA_FACTORY_CLI_AUTO) when blank.
    """
    if not project_id:
        return None
    effective_bin = (cli_bin or FACTORY_CLI_BIN).strip()
    effective_auto = (auto_level or FACTORY_CLI_AUTO).strip().lower()
    if effective_auto not in ("low", "medium", "high"):
        effective_auto = FACTORY_CLI_AUTO
    # Probe binary availability for the actual resolved bin (per-project
    # override can point at a different path on disk).
    if not _binary_resolves(effective_bin):
        log.warning(
            "factory-cli: `%s` binary not found on PATH — falling back to OpenRouter",
            effective_bin,
        )
        return None

    prompt_text = _flatten_messages_to_prompt(messages)
    if not prompt_text:
        return None
    # iter-14.9 — visibility for the slim-mode payload-shrink. When
    # `LAMA_FACTORY_CLI_SLIM=1`, the flatten helper strips embedded KB
    # blocks; log the original vs slim size so the operator sees the
    # token saving in a single grep-able line.
    if _slim_flag_enabled():
        raw_len = sum(len((m.get("content") or "") if isinstance(m.get("content"), str)
                          else "") for m in (messages or []))
        log.info(
            "factory-cli SLIM prompt shrink: raw=%d chars → slim=%d chars "
            "(saved %d chars, %.1f%%)",
            raw_len, len(prompt_text),
            max(raw_len - len(prompt_text), 0),
            (100.0 * (raw_len - len(prompt_text)) / raw_len) if raw_len else 0.0,
        )

    workspace = (cwd or FACTORY_CLI_DEFAULT_CWD or os.path.join("/tmp", "lama-factory-cli", project_id)).strip()
    os.makedirs(workspace, exist_ok=True)

    # iter-14.7 — Timeout semantics fix.
    #
    # `FACTORY_CLI_TIMEOUT_SEC` is documented (see module top) as a
    # **hard cap** — "so a runaway agent can't burn the box for an hour".
    # The previous `max(timeout, FACTORY_CLI_TIMEOUT_SEC)` turned it into
    # a **floor** instead, forcing every droid invocation to wait at
    # least 300s regardless of what the caller asked for. The user-
    # visible symptom was catastrophic: `routes/srs.py` sends
    # (240s, 200s, 150s) per attempt for a 3-attempt schedule, expecting
    # to fail-fast at ~590s worst case; the floor turned that into
    # 3×300s = 900s of hard timeouts before the OpenRouter fallback
    # kicked in — and if OPENROUTER_API_KEY was unset, the user saw
    # 15 minutes of "loading" followed by a misleading error.
    #
    # Restore the intended semantics:
    #   • Caller's `timeout` is authoritative (it knows its own schedule).
    #   • FACTORY_CLI_TIMEOUT_SEC clamps as an UPPER bound only.
    #   • If the caller passes 0/None/negative, fall back to the env cap.
    if timeout and timeout > 0:
        effective_timeout = min(float(timeout), FACTORY_CLI_TIMEOUT_SEC)
    else:
        effective_timeout = FACTORY_CLI_TIMEOUT_SEC
    session_tag = f"lama:{project_id}:{agent_key}"

    t0 = time.time()
    log.info(
        "Factory CLI routing STARTED agent=%s pid=%s cwd=%s prompt_chars=%d model_hint=%s",
        agent_key, project_id, workspace, len(prompt_text), model_hint or "auto",
    )

    try:
        parsed = await _run_droid_exec(
            prompt=prompt_text,
            cwd=workspace,
            model=model_hint or None,
            auto_level=effective_auto,
            timeout=effective_timeout,
            session_tag=session_tag,
            bin_path=effective_bin,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.time() - t0) * 1000)
        log.warning(
            "Factory CLI routing TIMEOUT agent=%s pid=%s elapsed=%dms",
            agent_key, project_id, elapsed,
        )
        return None
    except Exception as exc:
        elapsed = int((time.time() - t0) * 1000)
        log.warning(
            "Factory CLI routing FAILED agent=%s pid=%s elapsed=%dms err_type=%s err=%s",
            agent_key, project_id, elapsed, type(exc).__name__, str(exc)[:300],
        )
        return None

    response_text = _extract_response_text(parsed)
    # iter-13.127 — droid 0.102+ signals a soft-failure (agent gave up,
    # tool crashed, etc.) via `is_error: true` in the JSON payload while
    # still exiting 0. Treat that as a routing failure so the caller
    # falls back to OpenRouter instead of pretending we got a real answer.
    if parsed.get("is_error") is True:
        elapsed = int((time.time() - t0) * 1000)
        subtype = str(parsed.get("subtype", "") or "")
        result_snippet = (response_text or "")[:200]
        log.warning(
            "Factory CLI routing REPORTED is_error=true agent=%s pid=%s elapsed=%dms subtype=%s snippet=%s",
            agent_key, project_id, elapsed, subtype, result_snippet,
        )
        # iter-13.128 — Auth-failure detection. droid returns
        # {"is_error": true, "subtype": "failure", "result": "Exec failed"}
        # when it's installed but not authenticated. Surfacing this as
        # `None` (the generic fall-through) is why users see the
        # misleading "OPENROUTER_API_KEY not configured" error two hops
        # later. Attach the diagnostic on the exception path so the
        # caller (fabric_call) can log it with actionable text; still
        # return None so the fallback chain runs (strict-mode users get
        # a proper error surfaced upstream via the Factory logs).
        if "exec failed" in result_snippet.lower() or (
            subtype == "failure" and not result_snippet
        ):
            log.error(
                "Factory CLI: droid is INSTALLED but NOT AUTHENTICATED "
                "(subtype=%s, result=%r). Run `droid auth login` on the "
                "machine where the LAMA backend runs. If you are using "
                "the bundled docker image: `docker exec -it lama droid "
                "auth login`.",
                subtype, result_snippet,
            )
        return None
    elapsed = int((time.time() - t0) * 1000)
    log.info(
        "Factory CLI routing COMPLETED agent=%s pid=%s elapsed=%dms response_chars=%d",
        agent_key, project_id, elapsed, len(response_text),
    )

    # iter-13.127 — prefer droid's real usage counters over our char/4
    # estimate when the CLI actually reports them (0.102+ emits an
    # `usage` object with input_tokens / output_tokens / thinking_tokens
    # / cache_* fields).
    real_usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else None
    if real_usage:
        prompt_tokens = int(real_usage.get("input_tokens") or 0) or _estimate_tokens(prompt_text)
        completion_tokens = int(real_usage.get("output_tokens") or 0) or _estimate_tokens(response_text)
    else:
        completion_tokens = _estimate_tokens(response_text)
        prompt_tokens = _estimate_tokens(prompt_text)
    return {
        "content": response_text,
        # droid records the model it actually used in its session metadata.
        # If we got it back in the JSON we forward it; otherwise echo the
        # hint so the Console usage report has something to display.
        "model": parsed.get("model") or model_hint or "droid",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "session_id": parsed.get("session_id") or session_tag,
        "via": "factory-cli",
        "elapsed_ms": elapsed,
    }


async def test_cli_connection(cli_bin: Optional[str] = None) -> Dict[str, Any]:
    """Cache-aware wrapper around the real probe.

    iter-15.48 — Console polls this endpoint every few seconds while the
    page is open. Without caching, every poll spawns a fresh `droid exec`
    → cold-start cost N times → probe flakes. We now serve any recent
    result (success TTL 5 min, failure TTL 30s) straight from memory,
    and only spawn a subprocess when the cache is empty/stale.

    Sticky-success: if the fresh probe times out but we have a successful
    probe within `FACTORY_CLI_PROBE_STICKY_SUCCESS_SEC` (default 30 min),
    return that cached success with a `stale: true` marker rather than
    flipping the UI to DOWN. The CLI is up — we proved it minutes ago —
    the current timeout is a cold-start hiccup, not an outage.
    """
    candidate = (cli_bin or "").strip()
    # Resolve early so we cache-key on the actual binary path (per-project
    # bin overrides get their own cache slot).
    cache_key = _resolve_binary_with_fallback(candidate) or (candidate or FACTORY_CLI_BIN or "droid")
    now = time.time()

    async with _PROBE_CACHE_LOCK:
        entry = _PROBE_CACHE.get(cache_key)
        if entry and entry.get("expires_at", 0) > now:
            cached = dict(entry["result"])
            cached["cached"] = True
            return cached

    result = await _do_test_cli_connection(cli_bin)

    async with _PROBE_CACHE_LOCK:
        prior = _PROBE_CACHE.get(cache_key) or {}
        last_success_at = float(prior.get("last_success_at") or 0.0)

        # Sticky-success: if fresh probe timed out but we had a real
        # success recently, prefer the cached OK. Keeps Console green
        # through cold-start hiccups.
        is_timeout = (
            not result.get("ok")
            and "timed out" in (result.get("error") or "").lower()
        )
        if (
            is_timeout
            and last_success_at
            and FACTORY_CLI_PROBE_STICKY_SUCCESS_SEC > 0
            and (now - last_success_at) < FACTORY_CLI_PROBE_STICKY_SUCCESS_SEC
            and prior.get("last_success_result")
        ):
            log.info(
                "factory-cli probe: sticky-success (last real OK %.0fs ago) — "
                "masking cold-start timeout as OK.",
                now - last_success_at,
            )
            result = dict(prior["last_success_result"])
            result["stale"] = True
            result["stale_age_sec"] = round(now - last_success_at, 1)

        ttl = FACTORY_CLI_PROBE_SUCCESS_TTL_SEC if result.get("ok") else FACTORY_CLI_PROBE_FAILURE_TTL_SEC
        new_entry: Dict[str, Any] = {
            "result": result,
            "expires_at": now + ttl,
            "last_success_at": last_success_at,
            "last_success_result": prior.get("last_success_result"),
        }
        if result.get("ok") and not result.get("stale"):
            new_entry["last_success_at"] = now
            new_entry["last_success_result"] = result
        _PROBE_CACHE[cache_key] = new_entry

    return result


def _invalidate_probe_cache() -> None:
    """Drop the cache — used by Console `save config` handlers when the
    user changes the binary path or auth so the very next Test picks up
    the new state without waiting for TTL."""
    try:
        _PROBE_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass


async def _do_test_cli_connection(cli_bin: Optional[str] = None) -> Dict[str, Any]:
    """Lightweight readiness probe used by the Console "Test" button when
    LAMA_FACTORY_MODE=cli (or per-project mode=cli). Mirrors the shape
    returned by `factory_orchestrator.test_project_factory_orchestrator_connection`.

    `cli_bin` lets the UI probe a candidate path BEFORE the user saves
    the form, so they get instant feedback on whether `/usr/local/bin/droid`
    actually exists in the container.

    iter-13.126 — uses `_resolve_binary_with_fallback` so a stale
    per-project `cli_bin` value (e.g. a macOS absolute path saved before
    the operator moved to the dockerised deploy) automatically falls
    through to the env-var default and the PATH lookup. The response
    includes a `resolved_via` field so the UI can show whether the
    saved value worked or a fallback kicked in.

    iter-13.128 — Beyond `--version`, we now issue a real `droid exec`
    probe with a tiny prompt so we can detect the "installed but not
    authenticated" state. That case previously slipped through: the
    probe reported OK ("version=…"), Console showed green, but every
    real LLM call failed with `{"is_error": true, "result": "Exec
    failed"}` — which factory_cli converts to `None` → fabric_call
    falls through to OpenRouter → the user sees the misleading
    "OPENROUTER_API_KEY not configured" error. The exec probe surfaces
    the auth problem AT THE TEST BUTTON with an actionable
    `hint: run `droid auth login`` message.
    """
    candidate = (cli_bin or "").strip()
    effective_bin = _resolve_binary_with_fallback(candidate)
    if not effective_bin:
        # Build a helpful chain of what we tried so the user sees exactly
        # which paths failed.
        tried: List[str] = []
        for c in (candidate, FACTORY_CLI_BIN, "droid"):
            c = (c or "").strip()
            if c and c not in tried:
                tried.append(c)
        return {
            "ok": False,
            "via": "factory-cli",
            "binary": candidate or FACTORY_CLI_BIN or "droid",
            "tried": tried,
            "error": (
                "`droid` binary not found. Tried (in order): "
                + ", ".join(f"`{t}`" for t in tried)
            ),
            "hint": (
                "Install droid (https://docs.factory.ai/cli), set the CLI "
                "binary path in the Console, or set the LAMA_FACTORY_CLI_BIN "
                "env-var to an absolute path inside the container."
            ),
        }
    # If the caller gave us a candidate that didn't resolve but a
    # fallback did, surface that explicitly.
    resolved_via = "candidate"
    if candidate and candidate != effective_bin and not (
        os.path.isabs(candidate) and os.path.exists(candidate)
    ):
        resolved_via = "fallback (env-default / PATH lookup)"
    version = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            effective_bin, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        version = (stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return {
                "ok": False,
                "via": "factory-cli",
                "binary": effective_bin,
                "requested": candidate or None,
                "error": f"droid --version exited {proc.returncode}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "via": "factory-cli",
            "binary": effective_bin,
            "requested": candidate or None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    # iter-13.128 — Auth probe. `droid exec` with a trivial prompt
    # returns immediately (~2s) with is_error=true / result="Exec
    # failed" when the CLI is unauthenticated. A real call would waste
    # ~5-8s and lots of tokens, so we deliberately keep this cheap.
    auth_ok = True
    auth_error = ""
    auth_hint = ""
    probe_proc = None
    try:
        probe_proc = await asyncio.create_subprocess_exec(
            effective_bin, "exec", "--output-format", "json", "--auto", "low",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        probe_out_b, probe_err_b = await asyncio.wait_for(
            probe_proc.communicate(input=b"ok"),
            timeout=FACTORY_CLI_PROBE_TIMEOUT_SEC,
        )
        probe_out = (probe_out_b or b"").decode("utf-8", errors="replace")
        probe_err = (probe_err_b or b"").decode("utf-8", errors="replace")
        try:
            parsed = _extract_last_json_object(probe_out)
        except ValueError:
            parsed = {}
        if parsed.get("is_error") is True or probe_proc.returncode != 0:
            auth_ok = False
            result_txt = str(parsed.get("result") or probe_err or probe_out).strip()
            auth_error = (result_txt or "droid exec failed")[:300]
            # "Exec failed" with no other detail is Factory's tell for
            # "no auth token" / "auth token expired". Give the operator
            # the exact remediation.
            if "exec failed" in auth_error.lower() or not auth_error:
                auth_hint = (
                    "droid CLI is installed but not authenticated. "
                    "Run `droid auth login` on the SAME machine where the "
                    "LAMA backend runs (inside the docker container if you "
                    "use `docker compose up`, or your host shell if you "
                    "run `uvicorn` directly). The credentials live at "
                    "`~/.factory/auth.v2.file` and must match the process "
                    "the backend runs under."
                )
            else:
                auth_hint = (
                    "droid rejected a smoke prompt. See the error message "
                    "and check Factory dashboard / plan credits."
                )
    except asyncio.TimeoutError:
        auth_ok = False
        auth_error = f"droid exec probe timed out after {FACTORY_CLI_PROBE_TIMEOUT_SEC:.0f}s"
        auth_hint = (
            "droid CLI accepted the request but did not reply in time. This is "
            "usually a slow cold-start (first call after container start/idle "
            "pays for an update-check + auth-token refresh round-trip) or "
            "blocked network egress from the backend to Factory's API "
            "(corporate proxy / firewall). Try the Test button again — a "
            "second attempt is normally fast once the connection is warm. If "
            "it keeps timing out, verify outbound HTTPS to app.factory.ai is "
            "allowed from this host/container, or raise "
            "LAMA_FACTORY_CLI_PROBE_TIMEOUT_SEC (default 45s)."
        )
        # iter-14.94 — a timed-out asyncio.wait_for does NOT kill the
        # underlying subprocess; without this the probe leaked a `droid`
        # process per failed Test click, and those leaked processes could
        # go on to hold auth-token file locks that made the NEXT probe
        # fail too. Best-effort terminate + reap so retries start clean.
        if probe_proc is not None and probe_proc.returncode is None:
            try:
                probe_proc.kill()
                await asyncio.wait_for(probe_proc.wait(), timeout=5.0)
            except Exception:
                pass
    except Exception as exc:
        auth_ok = False
        auth_error = f"{type(exc).__name__}: {exc}"[:300]
    return {
        "ok": auth_ok,
        "via": "factory-cli",
        "version": version or "unknown",
        "binary": shutil.which(effective_bin) or effective_bin,
        "requested": candidate or None,
        "resolved_via": resolved_via,
        "auto_level": FACTORY_CLI_AUTO,
        # iter-13.128 — auth probe results
        "auth_ok": auth_ok,
        **({"error": auth_error} if not auth_ok else {}),
        **({"hint": auth_hint} if auth_hint else {}),
    }

