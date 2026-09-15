"""Factory Sessions API bridge for LAMA orchestrator calls.

When enabled per project, all `llm.fabric_call()` requests route to
Factory (`api.factory.ai`) instead of local provider/model routing.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from db import projects, token_usage_log as log_col, agent_configs as ac_col
from models import TokenUsageLog


FACTORY_API_BASE = (os.environ.get("FACTORY_API_BASE_URL", "https://api.factory.ai/api/v0") or "").rstrip("/")
FACTORY_ORCH_MAX_INPUT_CHARS = int((os.environ.get("FACTORY_ORCH_MAX_INPUT_CHARS") or "32000").strip() or 32000)
FACTORY_PROMPT_MAX_CHARS = int(os.environ.get("LAMA_FACTORY_PROMPT_MAX_CHARS", "320000") or "320000")
FACTORY_PROMPT_RETRY_MAX_CHARS = int(os.environ.get("LAMA_FACTORY_PROMPT_RETRY_MAX_CHARS", "160000") or "160000")
FACTORY_SLOW_AGENT_TIMEOUT_SEC = float(os.environ.get("LAMA_FACTORY_SLOW_AGENT_TIMEOUT_SEC", "600") or "600")
# iter-13.39 — how often we poll for the assistant reply once the user
# message has been delivered. Lower = catch fast Droid replies sooner;
# higher = fewer GET requests over the wire. 1.0 s is a good trade-off.
FACTORY_POLL_INTERVAL_SEC = float(os.environ.get("LAMA_FACTORY_POLL_INTERVAL_SEC", "1.0") or "1.0")
# iter-13.39 — on timeout, retry the whole exchange ONCE with a smaller
# prompt cap. Empirically the Droid often blocks on very large prompts
# (full SRS system prompt = ~45 KB → 20-min think-time spikes); shrinking
# to ~16 KB usually completes in <90 s. 0 = disable retry-on-timeout.
FACTORY_TIMEOUT_RETRY_MAX_CHARS = int(
    os.environ.get("LAMA_FACTORY_TIMEOUT_RETRY_MAX_CHARS", "16000") or "16000"
)
_SLOW_FACTORY_AGENTS = {
    "srs.generate",
    "kb.graphify",
    "datamodel.oltp",
    "datamodel.olap",
    "datamodel.chat",
    "arch.recommend",
    "arch.hld",
    "arch.lld",
    "arch.sequence",
    "arch.api_contracts",
    "codegen.service",
    "codegen.gap_recovery",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────
# iter-13.91 — Per-(tenant, project) Factory context isolation.
#
# Three bugs caused "all projects get merged into the same Factory
# context window":
#
#   1. `agent_key` values contain dots ("srs.generate"). Writing them via
#      a Mongo dot-path (`$set: {".sessions_by_agent.srs.generate": id}`)
#      makes Mongo interpret the dot as a *nested document path*. So the
#      stored shape becomes `{srs: {generate: id}}` while reads of
#      `sessions_map.get("srs.generate")` look for the *flat* key and
#      return `None` — the cache is effectively broken.
#
#   2. `session_key = agent_key or "default"` has no tenant_id or
#      project_id namespace, so any future refactor that shares the
#      `sessions_by_agent` doc across projects/tenants (e.g. global
#      Factory defaults) would silently cross-stitch contexts.
#
#   3. The Droid Computer's `cwd` is the *filesystem* context. Every
#      project in the dump uses `computer_id=arindamdroid` with
#      `cwd=""` → all projects of all tenants share the Droid's home
#      directory. The LLM, told to "read files in cwd", sees whatever
#      the previous project left behind → cross-project context merge.
#
# Fixes:
#   • `_slug_key`: dot- and dollar-safe key sanitiser. NEVER use a raw
#     agent_key in a Mongo dot-path again.
#   • `_session_key(tenant_id, project_id, agent_key)`: namespaced key
#     of the form `t::<tenant>__p::<project>__a::<agent_slug>`. Stored
#     as a flat key inside `sessions_by_agent`.
#   • `_resolve_cwd(cfg, tenant_id, project_id)`: when the user has not
#     pinned an explicit `cwd`, default to
#     `<LAMA_FACTORY_WORKSPACE_BASE or ~/lama-workspaces>/<tenant>/<project>`
#     so each project of each tenant gets its own scratch directory on
#     the Droid Computer.
# ──────────────────────────────────────────────────────────────────────
def _slug_key(raw: str) -> str:
    """Make `raw` safe to use as a single Mongo field name.

    Mongo treats `.` and (in some contexts) `$` as path separators when
    used inside `$set` operators. Replacing them with `__` keeps the
    key flat and human-readable in the stored doc.
    """
    s = (raw or "").strip() or "default"
    return s.replace(".", "__").replace("$", "_").replace("/", "_")


def _session_key(tenant_id: str, project_id: str, agent_key: str) -> str:
    """Project + tenant + agent namespaced session-cache key.

    The triple uniquely identifies a Factory conversation context.
    Anything sharing a tenant or a project but differing in agent gets
    its own session; tenant boundary is the strongest guarantee.
    """
    t = _slug_key(tenant_id or "default")
    p = _slug_key(project_id or "default")
    a = _slug_key(agent_key or "default")
    return f"t::{t}__p::{p}__a::{a}"


# iter-13.91.6 — Default base for per-(tenant, project) Droid workspaces.
#
# Iteration history:
#   • `/workspace/lama-workspaces` → fails on most Droid images (mount is
#     read-only) with `Read-only file system`.
#   • `/tmp/lama-workspaces` → universally writable BUT lives outside the
#     Droid's HOME, so Factory's left-sidebar auto-listing (which shows
#     the HOME directory's top-level folders as "projects") never picks
#     it up. The workspace exists, but the user can't see it in the UI.
#   • `/root/lama-workspaces` (current default) → root's HOME on the
#     stock Factory Droid image. Writable AND visible in the left rail.
#     For Droids using a non-root user, set `LAMA_FACTORY_WORKSPACE_BASE`
#     to the correct HOME (e.g. `/home/factory/lama-workspaces`).
_DEFAULT_WORKSPACE_BASE = "/root"


# ──────────────────────────────────────────────────────────────────────
# iter-13.91.4 — Eager workspace + Droid-wake caches (process-local).
#
# Background: the lazy "bootstrap-on-400" path inside `_create_session`
# worked, but it meant every brand-new (tenant, project, agent) triplet
# paid a 400 → bootstrap → retry tax on its first call. Worse, several
# code paths bypassed `_create_session` entirely (chat_completion calls
# that re-used a cached session id) and never saw the mkdir at all —
# so when the Droid filesystem was wiped or the Computer was rotated,
# the cwd silently went stale.
#
# Fix: make workspace creation EAGER — every call into
# `route_via_factory_orchestrator` runs `_ensure_workspace_exists` BEFORE
# touching POST /sessions, and we explicitly poke the Droid awake via
# GET /computers/{id} on the same code path.
#
# To keep this cheap, both operations are memoised PER PROCESS (the
# Droid filesystem is sticky; we only need to mkdir once per cwd per
# uvicorn worker; the wake-poke is throttled to once every 30s per
# computer). Restarting uvicorn or rotating workers re-arms the cache,
# which is the desired conservative behaviour.
# ──────────────────────────────────────────────────────────────────────
_WORKSPACE_READY: set[tuple[str, str]] = set()       # (computer_id, cwd)
_WORKSPACE_LOCKS: Dict[tuple[str, str], asyncio.Lock] = {}
_LAST_WAKE_AT: Dict[str, float] = {}                  # computer_id → epoch_seconds
_WAKE_LOCKS: Dict[str, asyncio.Lock] = {}
# iter-13.91.7 — per-process (ref → computer_id) cache so the MiniConsole's
# 30s health-check polling doesn't re-resolve the same computer ref over
# and over via `GET /computers/name/<ref>`.
_COMPUTER_ID_CACHE: Dict[str, str] = {}
# iter-13.91.9 — per-process (computer_id → $HOME on the Droid) cache.
# Populated as a side-effect of every successful bootstrap (the shell
# message echoes LAMA_HOME=$HOME alongside the mkdir result). Used to
# auto-fallback to `<HOME>/lama_<tenant>_<project>` when the user-
# supplied or default cwd lands on a read-only mount (e.g. /root or
# /workspace on locked-down Droid images).
_DROID_HOME_CACHE: Dict[str, str] = {}
# iter-13.91.11 — per-process (computer_id → shell flavour) cache.
# Populated as a side-effect of the bootstrap shell probe. Used to pick
# the right mkdir command (POSIX `mkdir -p` vs PowerShell `New-Item
# -ItemType Directory -Force` vs cmd.exe `mkdir`) on subsequent calls
# without re-running the detection round-trip.
# Recognised values: "posix" (Linux / macOS / BSD / Alpine / busybox),
# "powershell" (Windows PowerShell 5.x / pwsh 7.x), "cmd" (legacy
# Windows cmd.exe). The cache is process-local — restarting uvicorn
# rearms detection, which is the desired conservative behaviour.
_DROID_SHELL_CACHE: Dict[str, str] = {}
_WAKE_MIN_INTERVAL_SEC = float(
    os.environ.get("LAMA_FACTORY_WAKE_MIN_INTERVAL_SEC", "30") or "30"
)


# iter-13.91.11 — signatures we look for in the bootstrap response to
# classify the droid's shell. POSIX shells (bash / sh / zsh / dash / ash)
# all accept the same `mkdir -p`, `$HOME`, `$?` syntax so they're
# handled by the default branch when no Windows marker fires.
_POWERSHELL_MARKERS = (
    "at line:",            # "At line:1 char:24"
    "categoryinfo",        # PowerShell error metadata
    "fullyqualifiederrorid",
    "new-item :",
    "is not recognized as the name of a cmdlet",
)
_WINDOWS_IO_MARKERS = (
    "access to the path",  # .NET IO error wording (both PowerShell and cmd surface this)
    "is denied.",
)
_CMD_MARKERS = (
    "the syntax of the command is incorrect",
    "the system cannot find the path specified",
    "is not recognized as an internal or external command",
    "a subdirectory or file",          # "A subdirectory or file ... already exists."
)


def _classify_shell(text: str) -> str:
    """Return one of: "posix" (default / unknown), "powershell", "cmd".

    The classification is best-effort and intentionally cautious — if
    the response carries unambiguous Windows markers we switch flavours,
    otherwise we keep the POSIX default. The `$HOME` echo is the most
    reliable signal: POSIX shells return an absolute path starting with
    "/", PowerShell returns "C:\\Users\\<user>", and cmd.exe leaves
    "$HOME" *literal* (no expansion) which is itself a giveaway.
    """
    if not text:
        return "posix"
    low = text.lower()
    # Strong PowerShell signal — these only appear in PS error output.
    if any(m in low for m in _POWERSHELL_MARKERS):
        return "powershell"
    # cmd.exe leaves $HOME literal and uses its own error wording.
    if "lama_home=$home" in low or any(m in low for m in _CMD_MARKERS):
        return "cmd"
    # Generic Windows IO error — could be either PS or cmd. PowerShell
    # is the modern default on Factory Windows droids so we bias there.
    if any(m in low for m in _WINDOWS_IO_MARKERS):
        return "powershell"
    return "posix"


def _safe_basename(cwd: str) -> str:
    """Extract the final segment of a POSIX or Windows cwd as a safe
    folder name we can drop under `$HOME` / `%USERPROFILE%` on any OS.
    """
    if not cwd:
        return "lama_workspace"
    last = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # Strip anything any shell would treat as a path separator / wildcard
    # / metacharacter. Keep it conservative — alnum + _-. only.
    safe = "".join(c for c in last if c.isalnum() or c in ("_", "-", "."))
    return safe or "lama_workspace"


# Back-compat alias — older call sites used `_windows_basename`.
_windows_basename = _safe_basename


def _workspace_lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _WORKSPACE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKSPACE_LOCKS[key] = lock
    return lock


def _wake_lock(computer_id: str) -> asyncio.Lock:
    lock = _WAKE_LOCKS.get(computer_id)
    if lock is None:
        lock = asyncio.Lock()
        _WAKE_LOCKS[computer_id] = lock
    return lock


async def _wake_computer(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
    *,
    force: bool = False,
) -> bool:
    """Proactively nudge a Factory Droid Computer awake.

    Factory managed computers auto-pause when idle. A cheap GET on
    `/computers/{id}` is sufficient to trigger Factory's auto-resume —
    we don't need (and Factory doesn't expose) a dedicated wake API.

    iter-13.91.14 — when the GET response body indicates the computer
    is in a non-active state (`paused`, `stopped`, `hibernating`,
    `suspended`, `terminated`), we additionally fire-and-forget a small
    set of plausible "start" endpoints. Factory doesn't publicly
    document a start API, so each candidate is best-effort: 4xx/5xx
    responses are ignored. If any of them does the job, the next
    POST /sessions will succeed without needing a separate operator
    click in the Factory UI.

    Throttled to once every `_WAKE_MIN_INTERVAL_SEC` seconds per process
    per computer so back-to-back agent calls don't spam the endpoint.
    `force=True` bypasses the throttle (useful for the explicit Console
    "Wake droid" button).
    """
    if not computer_id:
        return False
    now = time.time()
    last = _LAST_WAKE_AT.get(computer_id, 0.0)
    if not force and (now - last) < _WAKE_MIN_INTERVAL_SEC:
        return True  # recently woken; assume still warm
    async with _wake_lock(computer_id):
        # Re-check under the lock — another coroutine may have just woken it.
        last = _LAST_WAKE_AT.get(computer_id, 0.0)
        if not force and (time.time() - last) < _WAKE_MIN_INTERVAL_SEC:
            return True
        ok = False
        body: Dict[str, Any] = {}
        try:
            resp = await client.get(
                f"{FACTORY_API_BASE}/computers/{quote(computer_id, safe='')}",
                headers=headers,
            )
            _LAST_WAKE_AT[computer_id] = time.time()
            ok = resp.status_code in (200, 202, 204)
            if resp.status_code == 200:
                try:
                    body = resp.json() or {}
                except Exception:
                    body = {}
        except Exception:
            # Failure here is non-fatal — the caller will retry POST /sessions
            # and surface any persistent connectivity issue from there.
            _LAST_WAKE_AT[computer_id] = time.time()
            return False
        # iter-13.91.14 — proactive start attempt for paused / stopped
        # computers. Factory's session-create endpoint returns 424 when
        # the underlying VM isn't running, even though GET /computers
        # happily returns metadata. The GET alone is not always enough
        # to trigger a resume on managed images.
        status = str(body.get("status") or body.get("state") or "").lower()
        _SLEEPING_STATES = {
            "paused", "stopped", "hibernating", "suspended",
            "terminated", "off", "inactive", "asleep",
        }
        if force and status in _SLEEPING_STATES:
            await _try_start_computer(client, headers, computer_id)
        return ok


async def _try_start_computer(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
) -> bool:
    """iter-13.91.14 — best-effort "start this computer" probe.

    Factory does not publicly document a start API, so we send to a
    short list of plausible endpoints. Any 2xx response is treated as
    success and we stop probing. 4xx / 5xx / network errors are
    silently ignored so an operator running an older Factory build
    doesn't see noise in logs. Returns True if any probe succeeded.
    """
    candidates = [
        ("POST", f"/computers/{quote(computer_id, safe='')}/start", None),
        ("POST", f"/computers/{quote(computer_id, safe='')}/resume", None),
        ("POST", f"/computers/{quote(computer_id, safe='')}/wake", None),
        ("POST", f"/computers/{quote(computer_id, safe='')}/actions/start", None),
        ("PATCH", f"/computers/{quote(computer_id, safe='')}", {"status": "active"}),
    ]
    # iter-13.91.14 — module-scoped logger lookup so a NameError can't
    # silently mask a successful start probe (caught earlier by the
    # broad `except Exception`).
    import logging as _logging
    _lg = _logging.getLogger("lama.factory")
    for method, path, payload in candidates:
        try:
            if method == "POST":
                r = await client.post(
                    f"{FACTORY_API_BASE}{path}",
                    headers=headers,
                    json=(payload or {}),
                )
            else:
                r = await client.patch(
                    f"{FACTORY_API_BASE}{path}",
                    headers=headers,
                    json=(payload or {}),
                )
            if r.status_code in (200, 201, 202, 204):
                _lg.info(
                    "Factory start probe %s %s → %d (computer=%s)",
                    method, path, r.status_code, computer_id,
                )
                return True
        except Exception:
            continue
    return False


async def _get_computer_diagnostic(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
) -> Dict[str, Any]:
    """iter-13.91.14 — fetch human-readable diagnostic info about a
    Droid Computer for surfacing in error messages.

    Returns a dict with: `status`, `state`, `last_seen`, `image`,
    `region`, `error` — best-effort. Missing fields are omitted. Used
    when POST /sessions returns 424 so the operator sees WHY Factory
    thinks the computer is unreachable instead of a bare 424.
    """
    if not computer_id:
        return {}
    try:
        r = await client.get(
            f"{FACTORY_API_BASE}/computers/{quote(computer_id, safe='')}",
            headers=headers,
        )
        if r.status_code != 200:
            return {"http_status": r.status_code, "raw": (r.text or "")[:200]}
        body = r.json() or {}
    except Exception as e:
        return {"error": str(e)[:200]}
    keep = ("status", "state", "last_seen", "lastSeen", "image",
            "region", "type", "size", "created_at", "createdAt",
            "is_active", "isActive")
    out: Dict[str, Any] = {}
    for k in keep:
        if k in body and body[k] not in (None, ""):
            out[k] = body[k]
    return out


def _parse_problem_detail(raw_text: str) -> Dict[str, Any]:
    """iter-13.91.15 — extract RFC 7807 Problem Details fields from a
    Factory error response body.

    Factory uses RFC 7807 for non-2xx responses:
        {"detail": "...", "status": 424, "title": "...", "requestId": "..."}

    Returns a dict with the parsed fields (empty if the body isn't valid
    JSON). The `detail` field is the human-readable error and is what
    we use to classify sub-types of 424 (e.g. "Computer disconnected
    during request" vs "Computer is paused" vs "Computer not found").
    """
    if not raw_text:
        return {}
    try:
        body = json.loads(raw_text)
        if isinstance(body, dict):
            return {
                "detail": body.get("detail") or "",
                "title": body.get("title") or "",
                "status": body.get("status") or 0,
                "request_id": body.get("requestId") or body.get("request_id") or "",
            }
    except Exception:
        return {}
    return {}


def _classify_424_detail(detail: str) -> str:
    """iter-13.91.15 — classify Factory's 424 sub-types from the `detail`
    field. Returns a short reason code suitable for the Console UI:

      "droid_agent_unreachable" — VM is alive but agent process isn't
          responding (typical when the droid was never opened in the
          Factory web UI, the agent process crashed, or the browser tab
          hosting it was closed).
      "droid_paused"            — VM itself is paused / stopped.
      "droid_not_found"         — Computer ID is unknown to Factory.
      "droid_disconnected"      — fallback for anything else.
    """
    d = (detail or "").lower()
    if "disconnect" in d:
        return "droid_agent_unreachable"
    if "paus" in d or "stop" in d or "hibernat" in d or "suspend" in d:
        return "droid_paused"
    if "not found" in d or "no such" in d:
        return "droid_not_found"
    return "droid_disconnected"


async def _cleanup_stale_sessions(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
    max_delete: int = 5,
) -> int:
    """iter-13.91.15 — best-effort: list existing Factory sessions for
    this computer and DELETE up to `max_delete` of them.

    Some droid images become "session-locked": Factory thinks a previous
    session is still attached, so it refuses to create a new one and
    returns 424 "Computer disconnected during request" even though the
    VM is fine. Deleting stale sessions can recover this without
    rebooting the droid.

    Best-effort: any 4xx/5xx is silently ignored (different Factory
    builds expose different listing endpoints). Returns the count of
    sessions actually deleted.
    """
    if not computer_id:
        return 0
    deleted = 0
    # Try a few plausible list endpoints — Factory's docs aren't public.
    list_paths = (
        f"/sessions?computerId={quote(computer_id, safe='')}",
        f"/sessions?computer_id={quote(computer_id, safe='')}",
        f"/computers/{quote(computer_id, safe='')}/sessions",
    )
    session_ids: List[str] = []
    for path in list_paths:
        try:
            r = await client.get(f"{FACTORY_API_BASE}{path}", headers=headers)
            if r.status_code != 200:
                continue
            body = r.json() or {}
            # Factory may return either {"sessions":[...]} or a bare list.
            rows = body if isinstance(body, list) else (
                body.get("sessions") or body.get("items") or []
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sid = row.get("sessionId") or row.get("id") or ""
                if sid and sid not in session_ids:
                    session_ids.append(sid)
            if session_ids:
                break  # got results from this endpoint, stop probing
        except Exception:
            continue
    import logging as _logging
    _lg = _logging.getLogger("lama.factory")
    for sid in session_ids[:max_delete]:
        try:
            r = await client.delete(
                f"{FACTORY_API_BASE}/sessions/{quote(sid, safe='')}",
                headers=headers,
            )
            if r.status_code in (200, 202, 204):
                deleted += 1
                _lg.info(
                    "Cleaned up stale Factory session %s (computer=%s)",
                    sid, computer_id,
                )
        except Exception:
            continue
    return deleted


async def _ensure_workspace_ready_cached(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
    cwd: str,
    *,
    force: bool = False,
    poll_seconds: float = 45.0,
) -> Dict[str, Any]:
    """Memoised wrapper around `_ensure_workspace_exists`.

    First call for a (computer_id, cwd) pair actually runs the shell
    `mkdir -p` on the Droid; subsequent calls in the same process return
    a cached success. `force=True` skips the cache.

    iter-13.91.5 — returns the structured dict from `_ensure_workspace_exists`
    so the Console can surface a real failure reason (e.g. "droid_disconnected")
    instead of a bare "WS ✗" with no detail.
    """
    if not (computer_id and cwd):
        return {"ok": False, "reason": "no_cwd", "detail": ""}
    key = (computer_id, cwd)
    if not force and key in _WORKSPACE_READY:
        return {"ok": True, "reason": "cached", "detail": cwd}
    async with _workspace_lock(key):
        if not force and key in _WORKSPACE_READY:
            return {"ok": True, "reason": "cached", "detail": cwd}
        result = await _ensure_workspace_exists(
            client, headers, computer_id, cwd, poll_seconds=poll_seconds
        )
        if result.get("ok"):
            _WORKSPACE_READY.add(key)
        return result


def _invalidate_workspace_cache(computer_id: str | None = None, cwd: str | None = None) -> None:
    """Drop cached `(computer_id, cwd)` entries.

    Used when the operator deletes / rotates the Factory config — the
    next agent call will re-bootstrap the workspace from scratch.
    """
    if computer_id is None and cwd is None:
        _WORKSPACE_READY.clear()
        return
    drop = {k for k in _WORKSPACE_READY if (computer_id is None or k[0] == computer_id) and (cwd is None or k[1] == cwd)}
    _WORKSPACE_READY.difference_update(drop)


def _resolve_cwd(cfg: Dict[str, Any], tenant_id: str, project_id: str) -> str:
    """Return the cwd to send on POST /sessions for this (tenant, project).

    iter-13.91.8 — single-level workspace path so the folder is visible
    in Factory's left sidebar.
    iter-13.91.12 — when `cfg["host_anchored"]` is True, return "" so the
    caller OMITS the cwd field on POST /sessions. Factory then defaults
    cwd to the droid's HOME, which always exists on every OS and never
    needs a bootstrap mkdir. All artifacts are kept on the LAMA host and
    pushed inline.

    Factory's UI auto-lists folders **one level deep** under a computer's
    HOME (or whatever root Factory enumerates). A nested path like
    `<base>/<tenant>/<project>` only surfaces `<base>` in the sidebar —
    the actual project folder is invisible.

    Solution: collapse the per-(tenant, project) identifier into a
    single directory name `lama_<tenant>_<project>` placed DIRECTLY
    under the base. Now Factory's sidebar shows the project folder by
    name and the operator can click it to browse the workspace.

    Precedence:
      0. `cfg["host_anchored"]` True → "" (let Factory default to HOME).
      1. Explicit `cfg["cwd"]` (`~` expanded) — operator override.
      2. `LAMA_FACTORY_WORKSPACE_BASE` env-var (absolute path) —
         operator-pinned base. Joined with single-level `lama_<t>_<p>`.
      3. Built-in default `/root/lama-workspaces`.
    """
    # iter-13.91.12 — host-anchored mode short-circuit. The orchestrator
    # caller will skip the cwd field on POST /sessions when this returns
    # empty AND will skip the workspace bootstrap. Outputs are persisted
    # on the LAMA host (Mongo + filesystem).
    if cfg.get("host_anchored"):
        return ""
    explicit = (cfg.get("cwd") or "").strip()
    if explicit:
        if explicit.startswith("~"):
            explicit = os.path.expanduser(explicit)
        return explicit
    if not project_id:
        return ""
    base = (os.environ.get("LAMA_FACTORY_WORKSPACE_BASE") or "").strip()
    if base:
        if base.startswith("~"):
            base = os.path.expanduser(base)
        if not base.startswith("/"):
            base = _DEFAULT_WORKSPACE_BASE
    else:
        base = _DEFAULT_WORKSPACE_BASE
    t = _slug_key(tenant_id or "default")
    p = _slug_key(project_id)
    # Single-level directory name — visible in Factory's left rail.
    folder = f"lama_{t}_{p}"
    return f"{base.rstrip('/')}/{folder}"


async def _register_session_for_cwd(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
    cwd: str,
) -> bool:
    """iter-13.91.10 — Make a freshly-created folder appear in Factory's UI.

    Factory's left sidebar is NOT a filesystem browser; it's a list of
    folders that Factory sessions have opened. After we `mkdir -p` a
    new workspace via the bootstrap shell, the folder exists on disk
    but is invisible in Factory's UI until a session opens it.

    This helper opens (and immediately abandons) a session targeted at
    the new cwd. The session creation itself is what causes Factory to
    register the folder under the computer in its left rail. We don't
    send any messages on the session — it's a "registration ping" only.

    Best-effort: returns True on success, False on any failure. The
    workspace is fully usable for real agent calls regardless of whether
    the UI sees it (those calls open their own sessions at the same
    cwd via `_create_session`).
    """
    if not (computer_id and cwd):
        return False
    try:
        resp = await client.post(
            f"{FACTORY_API_BASE}/sessions",
            headers=headers,
            json={"computerId": computer_id, "cwd": cwd},
        )
        if resp.status_code in (200, 201):
            sid = (resp.json() or {}).get("sessionId", "") or ""
            if sid:
                import logging as _log
                _log.getLogger("lama.factory").info(
                    "Registered cwd=%s with Factory UI via session %s", cwd, sid,
                )
                return True
        # 424 (Droid disconnected) or 400 (invalid cwd) is fine — the
        # caller already has a successful mkdir; we're just trying to
        # poke the UI. Log but don't error.
        import logging as _log
        _log.getLogger("lama.factory").info(
            "Could not register cwd=%s with Factory UI (HTTP %d %s)",
            cwd, resp.status_code, (resp.text or "")[:120],
        )
    except Exception as e:
        import logging as _log
        _log.getLogger("lama.factory").info(
            "Could not register cwd=%s with Factory UI: %s", cwd, str(e)[:120],
        )
    return False


async def _ensure_workspace_exists(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    computer_id: str,
    cwd: str,
    *,
    poll_seconds: float = 45.0,
) -> Dict[str, Any]:
    """Create `cwd` on the Droid via a transient bootstrap session.

    Factory rejects POST /sessions with an `Invalid cwd` 400 when the
    path doesn't exist on the Droid filesystem. There's no separate
    "filesystem prep" API, so we use the only tool we have — a Factory
    session whose default cwd is the Droid's home — to run `mkdir -p`
    via the Droid's shell. The bootstrap session is short-lived and
    NEVER cached: each call creates a fresh one so we don't pollute
    any agent's conversation context.

    iter-13.91.5 — returns a structured dict so callers (and ultimately
    the Console UI) can show *why* the bootstrap failed instead of a
    bare "WS ✗" with no detail:
      {"ok": bool, "reason": str, "detail": str}
    Common reasons: "no_cwd", "droid_disconnected" (persistent 424),
    "bootstrap_session_failed", "shell_exit_nonzero", "ack_timeout".
    """
    if not cwd:
        return {"ok": False, "reason": "no_cwd", "detail": ""}
    # iter-13.91.5 — match the main `_create_session` retry budget so a
    # cold-starting Droid (60-120 s) doesn't surface as a misleading
    # "workspace failed" message when the real issue is "computer
    # disconnected, still warming up". Total wait now ≤ 95 s.
    sid = ""
    last_status = 0
    last_text = ""
    # iter-13.91.13 — bumped backoff schedule from [5,15,30,45] (~95s) to
    # [5,15,30,45,45,30] (~170s) because Factory managed-computer cold
    # starts can take 90–150 s in practice, especially right after a
    # weekend / overnight idle. The previous 95 s window was juuust
    # short enough to surface a spurious `droid_disconnected` for
    # otherwise-healthy computers that were merely warming up.
    BOOTSTRAP_BACKOFF = [5, 15, 30, 45, 45, 30]
    for attempt in range(len(BOOTSTRAP_BACKOFF)):
        try:
            resp = await client.post(
                f"{FACTORY_API_BASE}/sessions",
                headers=headers,
                json={"computerId": computer_id},
            )
        except Exception as e:
            return {
                "ok": False,
                "reason": "bootstrap_session_failed",
                "detail": f"network error: {str(e)[:200]}",
            }
        last_status = resp.status_code
        last_text = (resp.text or "")[:300]
        if resp.status_code in (200, 201):
            sid = (resp.json() or {}).get("sessionId", "") or ""
            if sid:
                break
            return {
                "ok": False,
                "reason": "bootstrap_session_failed",
                "detail": "sessionId missing from Factory response",
            }
        if resp.status_code == 424 and attempt < len(BOOTSTRAP_BACKOFF) - 1:
            wait = BOOTSTRAP_BACKOFF[attempt]
            # iter-13.91.14 — on persistent 424, the bare GET probe is
            # often insufficient — Factory's metadata endpoint replies
            # 200 even when the underlying VM is paused. Try the start
            # endpoint(s) too. Best-effort: any HTTP error is ignored,
            # the next backoff sleep gives the VM time to come up.
            try:
                await client.get(
                    f"{FACTORY_API_BASE}/computers/{quote(computer_id, safe='')}",
                    headers=headers,
                )
            except Exception:
                pass
            # iter-13.91.15 — sub-classify the 424 from the RFC 7807
            # `detail` field so we can take TARGETED action. The most
            # common case in production is "Computer disconnected during
            # request" → the VM is alive but the droid agent process is
            # unreachable. For that case, hammering start endpoints does
            # nothing useful, but cleaning up stale sessions sometimes
            # does. For "paused / stopped" sub-types, the start probes
            # are the right remediation.
            problem = _parse_problem_detail(last_text)
            sub_reason = _classify_424_detail(problem.get("detail") or "")
            if sub_reason == "droid_agent_unreachable":
                try:
                    await _cleanup_stale_sessions(
                        client, headers, computer_id,
                    )
                except Exception:
                    pass
            else:
                try:
                    await _try_start_computer(client, headers, computer_id)
                except Exception:
                    pass
            await asyncio.sleep(wait)
            continue
        # Non-retriable or retries exhausted
        break
    if not sid:
        if last_status == 424:
            # iter-13.91.14 — fetch the computer's reported status so the
            # operator sees Factory's own view of why this is failing.
            diag: Dict[str, Any] = {}
            try:
                diag = await _get_computer_diagnostic(
                    client, headers, computer_id,
                )
            except Exception:
                diag = {}
            diag_line = ""
            if diag:
                # Build a single readable line from the diagnostic dict
                parts = [f"{k}={v}" for k, v in diag.items()]
                diag_line = "Factory says: " + ", ".join(parts) + ".\n\n"
            # iter-13.91.15 — pick a SPECIFIC error message based on the
            # 424 sub-type so the operator gets actionable steps for
            # the actual failure (not a one-size-fits-all "VM paused"
            # message that's wrong for agent-unreachable cases).
            problem = _parse_problem_detail(last_text)
            sub_reason = _classify_424_detail(problem.get("detail") or "")
            request_id = problem.get("request_id") or ""
            raw_detail = problem.get("detail") or ""
            if sub_reason == "droid_agent_unreachable":
                fix_lines = (
                    "This is NOT a paused-VM error. Factory's session "
                    "router got the request but couldn't deliver it to "
                    "the droid agent process inside the VM. Common causes:\n"
                    "  • The droid was never opened in the Factory web UI "
                    "after creation — open https://app.factory.ai → your "
                    "computer → click it to attach the agent.\n"
                    "  • The browser tab hosting a browser-based droid was "
                    "closed. Re-open it.\n"
                    "  • The agent process crashed inside the VM. Reboot "
                    "the droid from Factory → Settings → Droid Computers.\n"
                    "  • A previous session is locking the droid. LAMA "
                    "already tried to clean up stale sessions; if you have "
                    "active Factory browser tabs for this droid, close them.\n"
                )
            elif sub_reason == "droid_paused":
                fix_lines = (
                    "Factory's session API says the droid VM is paused / "
                    "stopped / hibernating.\n"
                    "  1) Open Factory → Settings → Droid Computers, click "
                    "the computer, and Start / Resume it.\n"
                    "  2) Wait 60–180 s for cold start.\n"
                    "  3) Click 'Wake droid' in the LAMA Console, then "
                    "re-run 'Create / refresh workspace'.\n"
                )
            elif sub_reason == "droid_not_found":
                fix_lines = (
                    "Factory does not recognise this Computer ID. Either "
                    "it was deleted or you're hitting the wrong account.\n"
                    "  1) Open Factory → Settings → Droid Computers.\n"
                    "  2) Copy the current Computer ID and update the "
                    "Console → Factory Orchestrator → Computer ID field.\n"
                )
            else:
                fix_lines = (
                    "Generic Factory 424 — the droid VM is not reachable.\n"
                    "  1) Verify the computer state in Factory → Settings "
                    "→ Droid Computers.\n"
                    "  2) Wait 60–180 s, click 'Wake droid', then retry.\n"
                    "  3) If still failing, contact Factory support with "
                    "the requestId above.\n"
                )
            return {
                "ok": False,
                "reason": sub_reason,
                "diagnostic": diag,
                "factory_detail": raw_detail,
                "factory_request_id": request_id,
                "detail": (
                    f"Factory returned HTTP 424 '{problem.get('title') or 'Failed Dependency'}' "
                    f"after ~170 s of retries.\n"
                    f"Factory's verbatim message: \"{raw_detail or '(empty)'}\".\n"
                    + (f"Factory requestId: {request_id}\n\n" if request_id else "\n")
                    + diag_line
                    + fix_lines
                    + "\nNote: 'Host-anchored workspace' will NOT fix any "
                    "of these — every agent call still needs a working "
                    "Factory session.\n\n"
                    f"(raw last response: {last_text})"
                ),
            }
        return {
            "ok": False,
            "reason": "bootstrap_session_failed",
            "detail": f"HTTP {last_status} {last_text}",
        }

    # Quoted single-quote-safe path. `printf %q` would be cleaner but
    # we'd need to escape it for the JSON body too. The cwd we generate
    # is restricted to slug-safe segments (see _slug_key), so a plain
    # single-quote wrap is sufficient.
    safe_cwd = cwd.replace("'", "'\"'\"'")
    # iter-13.91.11 — pick the bootstrap command flavour based on what we
    # already know about this droid. First call sends the POSIX probe
    # (works for Linux/macOS/BSD/Alpine/busybox); if a previous call
    # classified the droid as Windows PowerShell or cmd.exe, we skip
    # straight to a native command rooted at the user profile folder
    # (the original cwd is almost certainly a POSIX absolute path that
    # Windows can't write to).
    flavour = _DROID_SHELL_CACHE.get(computer_id, "posix")
    win_basename = _safe_basename(cwd)

    def _posix_msg() -> str:
        # iter-13.91.9 — chain a `$HOME` probe into the bootstrap shell so we
        # learn the Droid's writable home directory in the SAME round trip
        # as the mkdir attempt. The caller (`ensure_project_workspace`) uses
        # it to auto-fallback when the requested cwd lands on a read-only
        # mount (e.g. /root or /workspace on locked-down Droid images).
        return (
            "LAMA workspace bootstrap — run EXACTLY this single shell command "
            "and reply with only its raw output. Do not chat, do not run "
            "anything else:\n\n"
            f"  echo LAMA_HOME=$HOME ; mkdir -p '{safe_cwd}' 2>&1 ; echo LAMA_WORKSPACE_READY:$?\n"
        )

    def _powershell_msg() -> str:
        # iter-13.91.11 — Windows / PowerShell bootstrap. We can't write to
        # the operator's POSIX cwd on a Windows droid (paths like
        # `/Users/Arindam.Bose1/...` resolve to a non-existent / read-only
        # location on C:\), so we collapse the cwd to its basename and
        # create it directly under `$HOME` (which on Windows expands to
        # `C:\Users\<user>`). The echo sentinels mirror the POSIX path so
        # the response parser below stays unified.
        return (
            "LAMA workspace bootstrap — run EXACTLY this single PowerShell "
            "command and reply with only its raw output. Do not chat, do not "
            "run anything else:\n\n"
            f"  Write-Host \"LAMA_HOME=$HOME\"; "
            f"New-Item -ItemType Directory -Force -Path (Join-Path $HOME '{win_basename}') "
            f"| Out-Null; Write-Host \"LAMA_WORKSPACE_READY:$LASTEXITCODE\"\n"
        )

    def _cmd_msg() -> str:
        # iter-13.91.11 — Windows cmd.exe bootstrap. cmd doesn't expand
        # `$HOME` (uses `%USERPROFILE%`), doesn't accept `-p`, treats
        # `;` as a literal char, and uses `%ERRORLEVEL%` instead of `$?`.
        # `mkdir` on cmd is recursive by default and exits 1 if the
        # folder already exists — we tolerate that by OR-ing with `ver`
        # (a no-op that always returns 0) so a re-bootstrap of an
        # existing workspace doesn't look like a failure.
        return (
            "LAMA workspace bootstrap — run EXACTLY this single cmd.exe "
            "command line and reply with only its raw output. Do not chat, "
            "do not run anything else:\n\n"
            f"  echo LAMA_HOME=%USERPROFILE%& "
            f"(if not exist \"%USERPROFILE%\\{win_basename}\" "
            f"mkdir \"%USERPROFILE%\\{win_basename}\")& "
            f"echo LAMA_WORKSPACE_READY:%ERRORLEVEL%\n"
        )

    _msg_for = {
        "posix": _posix_msg,
        "powershell": _powershell_msg,
        "cmd": _cmd_msg,
    }
    msg = _msg_for.get(flavour, _posix_msg)()
    try:
        post = await client.post(
            f"{FACTORY_API_BASE}/sessions/{sid}/messages",
            headers=headers,
            json={"text": msg, "computerId": computer_id},
        )
        if post.status_code != 200:
            return {
                "ok": False,
                "reason": "bootstrap_post_failed",
                "detail": f"HTTP {post.status_code} {(post.text or '')[:200]}",
            }
    except Exception as e:
        return {
            "ok": False,
            "reason": "bootstrap_post_failed",
            "detail": f"network error: {str(e)[:200]}",
        }

    deadline = time.time() + max(5.0, poll_seconds)
    last_id = ""
    while time.time() < deadline:
        try:
            last = await _latest_assistant_message(client, headers, sid)
        except Exception:
            last = None
        if last and last.get("id") != last_id:
            text = _extract_assistant_text(last) or ""
            # iter-13.91.9 — capture HOME path so the caller can build a
            # writable-by-default fallback if the requested cwd is on a
            # read-only mount. Side-effect, harmless on parse failure.
            try:
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("LAMA_HOME=") and len(line) > 10:
                        home = line[len("LAMA_HOME="):].strip().rstrip("/").rstrip("\\")
                        # iter-13.91.11 — accept POSIX (`/root`) AND Windows
                        # (`C:\Users\foo`) home paths. Anything that's not
                        # obviously a path (empty / single token) is dropped.
                        if home and (home.startswith("/") or (len(home) > 2 and home[1] == ":")):
                            _DROID_HOME_CACHE[computer_id] = home
                        break
            except Exception:
                pass
            # iter-13.91.11 — Shell-flavour classification + retry. If the
            # response carries Windows shell signatures (PowerShell error
            # metadata, cmd.exe wording, or a literal `$HOME` echo from
            # cmd's lack of variable expansion), switch this droid to the
            # detected flavour and retry the bootstrap ONCE with a native
            # command. Detection is bounded: we retry at most twice per
            # call (posix → powershell, then powershell → cmd) so a truly
            # exotic shell can't loop us forever.
            detected = _classify_shell(text)
            already_tried = (text.count("LAMA_WORKSPACE_READY") == 0
                             and detected != flavour
                             and detected in ("powershell", "cmd"))
            if already_tried and "LAMA_WORKSPACE_READY:0" not in text:
                _DROID_SHELL_CACHE[computer_id] = detected
                retry_msg = _msg_for[detected]()
                try:
                    retry = await client.post(
                        f"{FACTORY_API_BASE}/sessions/{sid}/messages",
                        headers=headers,
                        json={"text": retry_msg, "computerId": computer_id},
                    )
                    if retry.status_code != 200:
                        return {
                            "ok": False,
                            "reason": f"{detected}_bootstrap_post_failed",
                            "detail": (
                                f"{detected.title()} droid detected; retry "
                                f"HTTP {retry.status_code} {(retry.text or '')[:200]}"
                            ),
                        }
                except Exception as e:
                    return {
                        "ok": False,
                        "reason": f"{detected}_bootstrap_post_failed",
                        "detail": f"{detected.title()} droid detected; retry network error: {str(e)[:200]}",
                    }
                # Reset state so the outer poll loop waits for the NEW
                # assistant message produced by the retried command.
                flavour = detected
                last_id = last.get("id") or last_id
                deadline = time.time() + max(5.0, poll_seconds)
                await asyncio.sleep(0.5)
                continue
            if "LAMA_WORKSPACE_READY:0" in text:
                # iter-13.91.10 — Register the new folder with Factory's
                # UI by opening (and immediately abandoning) a session
                # whose cwd is the freshly-created path. Factory's left
                # sidebar enumerates folders that sessions have opened,
                # not arbitrary filesystem entries — without this poke
                # the user wouldn't see the folder appear after mkdir.
                # Failures here are non-fatal (the workspace is still
                # usable for real agent calls).
                # iter-13.91.11 — when we routed via PowerShell or cmd.exe,
                # the cwd that actually got created is `<HOME>\<basename>`,
                # NOT the original POSIX cwd. Synthesize that path so
                # callers (and the Console) see the real on-disk location.
                # POSIX runs continue to use `cwd` unchanged.
                effective_cwd = cwd
                if flavour in ("powershell", "cmd"):
                    home = _DROID_HOME_CACHE.get(computer_id, "")
                    if home:
                        # Heuristic: if home looks like a Windows drive path
                        # (`C:\...`), use backslash; otherwise POSIX slash.
                        is_win_home = len(home) > 2 and home[1] == ":"
                        sep = "\\" if is_win_home else "/"
                        effective_cwd = f"{home.rstrip(sep).rstrip('/').rstrip(chr(92))}{sep}{win_basename}"
                    else:
                        effective_cwd = win_basename
                await _register_session_for_cwd(client, headers, computer_id, effective_cwd)
                return {"ok": True, "reason": "", "detail": effective_cwd}
            if "LAMA_WORKSPACE_READY" in text:
                # Non-zero exit (e.g. permission denied) — bail so caller
                # can fall back to default cwd instead of looping.
                low = text.lower()
                # iter-13.91.6 — classify the most common failure modes
                # so the Console can show an actionable reason instead of
                # a generic "shell_exit_nonzero". The user just needs to
                # know "your base path isn't writable" — not the raw
                # shell output.
                if "read-only file system" in low or "read only file system" in low:
                    return {
                        "ok": False,
                        "reason": "readonly_fs",
                        "detail": (
                            f"Droid filesystem rejected `mkdir -p {cwd}` with "
                            "'Read-only file system'. The base path isn't writable "
                            "on this Droid image. Set LAMA_FACTORY_WORKSPACE_BASE "
                            "to a writable path (e.g. /tmp/lama-workspaces or "
                            "/root/lama-workspaces) and retry."
                        ),
                    }
                if "access to the path" in low and "is denied" in low:
                    # iter-13.91.11 — Windows .NET IO error wording. The
                    # PowerShell retry already ran and STILL came back
                    # with access-denied — almost always because the
                    # operator's Windows user can't write to `$HOME`
                    # (corporate-locked profile) or the basename collides
                    # with an existing protected folder.
                    return {
                        "ok": False,
                        "reason": "windows_path_unwritable",
                        "detail": (
                            f"Windows droid rejected `New-Item ... {win_basename}` "
                            "under $HOME with 'Access denied'. Either pick a "
                            "writable base path via LAMA_FACTORY_WORKSPACE_BASE "
                            "(e.g. C:\\Temp\\lama-workspaces) and override the "
                            "orchestrator `cwd` to a Windows-friendly value, or "
                            "run the droid agent as a user with write access "
                            "to its profile folder. Raw: " + text[:160]
                        ),
                    }
                if "permission denied" in low:
                    return {
                        "ok": False,
                        "reason": "permission_denied",
                        "detail": (
                            f"Droid filesystem rejected `mkdir -p {cwd}` with "
                            "'Permission denied'. Pick a base path the Droid "
                            "user can write to (e.g. /tmp/lama-workspaces or "
                            "$HOME/lama-workspaces) via LAMA_FACTORY_WORKSPACE_BASE."
                        ),
                    }
                if "no space left" in low:
                    return {
                        "ok": False,
                        "reason": "no_space_left",
                        "detail": f"Droid filesystem is full: {text[:200]}",
                    }
                return {
                    "ok": False,
                    "reason": "shell_exit_nonzero",
                    "detail": f"droid shell rejected mkdir: {text[:200]}",
                }
        await asyncio.sleep(1.0)
    # No explicit ack but no error either — assume the mkdir landed
    # and let the caller retry. If POST /sessions still fails the outer
    # retry loop will surface the real error.
    return {
        "ok": True,
        "reason": "ack_timeout",
        "detail": "no LAMA_WORKSPACE_READY ack received within poll window; assuming success",
    }


def _mask_key(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) < 10:
        return "***"
    return f"{k[:6]}...{k[-4:]}"


def _http_verify():
    if os.environ.get("LAMA_DISABLE_SSL_VERIFY", "").lower() in {"1", "true", "yes", "on"}:
        try:
            import warnings as _w
            _w.filterwarnings("ignore", message="Unverified HTTPS request")
        except Exception:
            pass
        return False
    bundle = os.environ.get("LAMA_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if bundle and os.path.exists(bundle):
        return bundle
    return True


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


# ─────────────────────────────────────────────────────────────────────────
# iter-13.81 — Per-pipeline-node model selection (Discovery & SRS,
# DataModel, Architecture, CodeGen, Living) with two buckets each:
#   • generate   → first-time / fresh run
#   • regenerate → re-runs, gap-recovery, drift/diff, revalidation
#
# Stored under settings.factory_orchestrator.models as a flat dict keyed
# `"{stage}.{mode}"` → model id (or "" / "auto" to inherit the project-
# level `model` fallback, which in turn falls back to Factory auto-routing).
# ─────────────────────────────────────────────────────────────────────────

FACTORY_STAGE_BUCKETS: tuple[str, ...] = (
    "discovery",      # SRS, KB, chat
    "datamodel",      # OLTP/OLAP/bus-matrix/migration
    "architecture",   # service-map, HLD, LLD, sequence, API contracts
    "codegen",        # service generation + gap recovery
    "living",         # drift detector, SRS diff
    # iter-14.94 — tool project types (Gap Analyzer, Technology/Code
    # Transformer) run their own agent pipelines, not the 5-stage legacy
    # migration pipeline. They get their own buckets so their Console model
    # picks actually take effect instead of silently falling through to
    # ("default", "generate") — see _bucket_for_agent below.
    "transformer",    # tools.transformer.* — pattern/context/planner/coder/verifier/tester
    "gap_analyzer",   # gap_analyzer — UI/API/DB traceability + coverage gap detection
)
FACTORY_MODE_BUCKETS: tuple[str, ...] = ("generate", "regenerate")


def _default_models_map() -> Dict[str, str]:
    return {f"{stage}.{mode}": "auto" for stage in FACTORY_STAGE_BUCKETS for mode in FACTORY_MODE_BUCKETS}


def _bucket_for_agent(agent_key: str) -> tuple[str, str]:
    """Map an agent_key → (stage, mode) bucket.

    Stages keyed by prefix; mode determined by regen markers in the key.
    Anything unrecognised falls into ("default", "generate") so the lookup
    cleanly falls through to the project-level `model` fallback.
    """
    k = (agent_key or "").lower().strip()
    if not k:
        return ("default", "generate")
    regen_markers = (".regenerate", ".gap_recovery", ".revalidation")
    regen_prefixes = ("drift.", "diff.")
    mode = "regenerate" if (
        any(m in k for m in regen_markers) or any(k.startswith(p) for p in regen_prefixes)
    ) else "generate"
    if k.startswith("srs.") or k.startswith("kb.") or k.startswith("chat.discovery") or k.startswith("onto."):
        stage = "discovery"
    elif k.startswith("datamodel."):
        stage = "datamodel"
    elif k.startswith("arch."):
        stage = "architecture"
    elif k.startswith("codegen."):
        stage = "codegen"
    elif k.startswith("drift.") or k.startswith("diff.") or k.startswith("living."):
        stage = "living"
    elif k.startswith("tools.transformer."):
        stage = "transformer"
    elif k == "gap_analyzer" or k.startswith("gap_analyzer."):
        stage = "gap_analyzer"
    else:
        stage = "default"
    return (stage, mode)


def _canonicalise_factory_model_id(raw: str) -> str:
    """iter-13.129 — map legacy / short Console model IDs to what
    the droid CLI actually accepts.

    The Console UI used to offer bare aliases like `claude-opus-4`,
    `claude-sonnet-4`, `claude-haiku-4`. Droid rejects them with
    `Invalid model` and the fabric fallback then surfaces the
    misleading "OPENROUTER_API_KEY not configured" abort. We
    normalise here so old configs persisted in Mongo keep working
    without operators having to manually re-pick every dropdown.

    Empty / "auto" pass through untouched. Unknown IDs also pass
    through unmodified so future droid releases (with new model
    IDs) don't require a code change here.
    """
    v = (raw or "").strip()
    if not v or v.lower() == "auto":
        return v
    # Legacy short-alias → current canonical droid ID.
    _aliases = {
        "claude-opus-4":     "claude-opus-4-8",
        "claude-sonnet-4":   "claude-sonnet-4-6",
        "claude-haiku-4":    "claude-haiku-4-5-20251001",
        "claude-haiku-4-5":  "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
        # Old anthropic/ prefixed IDs used by the OpenRouter preset.
        "anthropic/claude-opus-4.7":   "claude-opus-4-7",
        "anthropic/claude-opus-4.6":   "claude-opus-4-6",
        "anthropic/claude-sonnet-4.6": "claude-sonnet-4-6",
        "anthropic/claude-sonnet-4":   "claude-sonnet-4-6",
        "anthropic/claude-haiku-4.5":  "claude-haiku-4-5-20251001",
        # Old OpenRouter-style gpt IDs → droid IDs.
        "gpt-5":       "gpt-5.5",
        "gpt-5-mini":  "gpt-5.4-mini",
        # gemini
        "gemini-2.5-pro": "gemini-3.1-pro-preview",
    }
    return _aliases.get(v, v)


def _resolve_model_for_agent(cfg: Dict[str, Any], agent_key: str) -> str:
    """Return the model id to send on POST /sessions for this agent.

    Resolution order:
      1. models[`{stage}.{mode}`] (operator-picked, per-bucket)
      2. models[stage]            (operator-picked, stage-level default)
      3. cfg["model"]             (operator-picked, project-level default)
      4. "auto"                   (Factory's own default routing)
    """
    models_map: Dict[str, Any] = cfg.get("models") or {}
    stage, mode = _bucket_for_agent(agent_key)

    def _pick(key: str) -> str:
        val = str(models_map.get(key) or "").strip()
        if not val or val.lower() == "auto":
            return ""
        return val

    chosen = _pick(f"{stage}.{mode}") or _pick(stage)
    if chosen:
        return _canonicalise_factory_model_id(chosen)
    fallback = str(cfg.get("model") or "").strip()
    if fallback and fallback.lower() != "auto":
        return _canonicalise_factory_model_id(fallback)
    return "auto"


def _normalise_models_payload(raw: Any) -> Dict[str, str]:
    """Sanitise an incoming models map from Console: keep known keys,
    coerce values to trimmed strings, drop unknown / non-string entries.
    """
    if not isinstance(raw, dict):
        return {}
    allowed_keys = {f"{s}.{m}" for s in FACTORY_STAGE_BUCKETS for m in FACTORY_MODE_BUCKETS}
    allowed_keys.update(FACTORY_STAGE_BUCKETS)  # tolerate stage-level defaults
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or k not in allowed_keys:
            continue
        if v is None:
            continue
        val = str(v).strip() or "auto"
        out[k] = val
    return out


def _settings_doc_to_config(doc: Dict[str, Any] | None, include_secret: bool = False) -> Dict[str, Any]:
    settings = ((doc or {}).get("settings") or {}).get("factory_orchestrator") or {}
    stored_models = _normalise_models_payload(settings.get("models"))
    # iter-13.81 — always surface a fully-populated map so the UI can render
    # the grid without conditional logic, even on legacy projects that pre-
    # date per-bucket selection.
    merged_models = _default_models_map()
    merged_models.update(stored_models)
    cfg = {
        "enabled": bool(settings.get("enabled", False)),
        "computer_id": (settings.get("computer_id") or "").strip(),
        "cwd": (settings.get("cwd") or "").strip(),
        "model": (settings.get("model") or "auto").strip() or "auto",  # project-level fallback default
        "models": merged_models,                                       # iter-13.81 — per-stage/per-mode
        "has_app_key": bool((settings.get("app_key") or "").strip()),
        "app_key_masked": _mask_key(settings.get("app_key", "")),
        "updated_at": settings.get("updated_at", ""),
        # iter-13.36 — per-project opt-out of strict mode. Default False so
        # Factory failures surface honestly instead of being silently
        # rewritten to claude-sonnet by the fabric fallback.
        "allow_fallback": bool(settings.get("allow_fallback", False)),
        # iter-13.91.12 — host-anchored mode. When True, the orchestrator
        # SKIPS the on-droid workspace bootstrap (`_ensure_workspace_exists`)
        # and pins `cwd` to the droid's HOME (which always exists on every
        # OS — `/root` on Linux, `/home/factory` on managed images,
        # `C:\Users\<user>` on Windows). All input artifacts are pushed
        # inline in the message body and outputs are persisted on the
        # LAMA host (MongoDB + filesystem). This is the correct mode for
        # reasoning-only agents (SRS critique, gap analysis, ontology
        # enrichment, etc.) and side-steps the entire cross-OS mkdir
        # tar-pit. Leave False for agents that actually need to run shell
        # commands / compilers on the droid (build, test, run).
        # iter-13.91.16 — host_anchored is now the DEFAULT (was False in
        # iter-13.91.12). Rationale: LAMA's system-of-record for every
        # input (KB files, OWL/TOON, SRS, DataModel, Architecture,
        # CodeGen files, stage_context) is MongoDB on the LAMA host. The
        # droid is pure compute — it never needs persistent on-disk
        # state. All four stages already inline their context into the
        # LLM prompt body. So bootstrap-a-workspace-on-the-droid was
        # legacy machinery serving no real purpose and was causing
        # cross-OS / paused-VM / "computer disconnected" failures
        # (iter-13.91.11 / .13 / .14 / .15). Operators who genuinely
        # need on-droid file storage (e.g. for build / test execution
        # later) can still tick the toggle OFF.
        "host_anchored": bool(settings.get("host_anchored", True)),
        # iter-13.125 — Per-project transport mode. "api" (default) routes
        # through Factory's HTTP API + Computer sandbox; "cli" shells out
        # to the `droid` CLI on the LAMA host. The env var
        # LAMA_FACTORY_MODE still acts as a global default when the
        # per-project field is empty so existing single-image deployments
        # don't break. See backend/fabric/factory_cli.py for trade-offs.
        "mode": (settings.get("mode") or "").strip().lower() or None,
        "cli_auto": (settings.get("cli_auto") or "").strip().lower() or None,
        "cli_bin": (settings.get("cli_bin") or "").strip() or None,
    }
    # iter-13.81.4 — explicit diagnostic so the UI can tell the user
    # EXACTLY why "Factory is disabled" — the MiniConsole + Console pill
    # both require ALL THREE of (enabled, has_app_key, computer_id) to
    # turn green, and silent absence of any one was hard to spot.
    _enabled = cfg["enabled"]
    _has_key = cfg["has_app_key"]
    _has_cpu = bool(cfg["computer_id"])
    # iter-13.125 — In CLI mode the readiness contract is different:
    # no app_key / computer_id needed, only `droid` on PATH. We mark
    # routing_active here optimistically and let the Test button hit
    # `/test-cli` for the actual binary probe.
    _is_cli = (cfg.get("mode") or "").lower() == "cli"
    if _is_cli:
        cfg["routing_active"] = _enabled
        cfg["routing_reason"] = "active (cli mode)" if _enabled else "inactive: enabled flag is OFF"
    else:
        cfg["routing_active"] = _enabled and _has_key and _has_cpu
        if cfg["routing_active"]:
            cfg["routing_reason"] = "active"
        else:
            _missing: list[str] = []
            if not _enabled:
                _missing.append("enabled flag is OFF")
            if not _has_key:
                _missing.append("app_key is not set")
            if not _has_cpu:
                _missing.append("computer_id is empty")
            cfg["routing_reason"] = "inactive: " + " · ".join(_missing)
    if include_secret:
        cfg["app_key"] = (settings.get("app_key") or "").strip()
        cfg["sessions_by_agent"] = settings.get("sessions_by_agent") or {}
    return cfg


async def get_project_factory_orchestrator_config(project_id: str, include_secret: bool = False) -> Dict[str, Any]:
    doc = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1, "settings": 1})
    if doc is None:
        raise RuntimeError("Project not found")
    return _settings_doc_to_config(doc, include_secret=include_secret)


async def upsert_project_factory_orchestrator_config(project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    doc = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1, "settings": 1})
    if doc is None:
        raise RuntimeError("Project not found")
    existing = ((doc.get("settings") or {}).get("factory_orchestrator") or {})
    next_cfg = dict(existing)
    # iter-13.36 — detect first-time Factory AI activation so the Console
    # PUT can auto-trigger modernization onboarding (KB build + top-3 stack
    # recommendation) without requiring a separate UI action.
    was_enabled_before = bool(existing.get("enabled"))
    already_onboarded = bool(existing.get("onboarded_at"))

    sessions_need_reset = False
    if "enabled" in payload:
        next_cfg["enabled"] = bool(payload.get("enabled"))
    if "app_key" in payload:
        incoming_key = (payload.get("app_key") or "").strip()
        # iter-13.81.4 — DO NOT wipe a saved app_key when the Console PUTs
        # an empty string. The Console intentionally omits app_key from
        # the payload when the field is blank (so saved keys survive
        # re-saves), but legacy callers / direct REST clients may still
        # send `app_key: ""`. Treat empty as "no change" to avoid the
        # silent "Factory disabled because the key got blanked on a
        # subsequent save" footgun.
        if incoming_key:
            if incoming_key != (existing.get("app_key") or ""):
                sessions_need_reset = True
            next_cfg["app_key"] = incoming_key
    if "computer_id" in payload:
        incoming = (payload.get("computer_id") or "").strip()
        if incoming != (existing.get("computer_id") or ""):
            sessions_need_reset = True
        next_cfg["computer_id"] = incoming
    if "cwd" in payload:
        incoming = (payload.get("cwd") or "").strip()
        # iter-13.91.14 — cross-project cwd-collision guard.
        # Two different projects MUST NOT share the same Droid workspace
        # cwd — that's the leak path we just spent half a day debugging.
        # Empty / blank cwd is fine (each project resolves to its own
        # unique auto-default `<HOME>/lama_<tenant>_<project>`).
        if incoming:
            clash = await projects.find_one(
                {
                    "id": {"$ne": project_id},
                    "settings.factory_orchestrator.cwd": incoming,
                    "settings.factory_orchestrator.enabled": True,
                },
                {"_id": 0, "id": 1, "name": 1, "tenant_id": 1},
            )
            if clash:
                raise RuntimeError(
                    f"Workspace cwd '{incoming}' is already in use by "
                    f"project '{clash.get('name') or clash.get('id')}' "
                    f"(id={clash.get('id')}, tenant={clash.get('tenant_id')}). "
                    "Two projects sharing the same Droid filesystem cwd "
                    "causes context bleed. Pick a unique path or leave "
                    "the field blank to use the auto-default "
                    "<HOME>/lama_<tenant>_<project>."
                )
        if incoming != (existing.get("cwd") or ""):
            sessions_need_reset = True
        next_cfg["cwd"] = incoming
    # iter-13.80 — accept an explicit model from the Console. Empty string
    # or "auto" → Factory keeps default routing. Changing it invalidates
    # cached sessions so the next call creates a fresh one with the new
    # model selection (otherwise Factory pins the session's first model).
    if "model" in payload:
        incoming_model = (payload.get("model") or "").strip() or "auto"
        if incoming_model != (existing.get("model") or "auto"):
            sessions_need_reset = True
        next_cfg["model"] = incoming_model
    else:
        next_cfg["model"] = existing.get("model") or "auto"

    # iter-13.81 — per-stage / per-mode model map. Same invalidation rule
    # as the project-level `model` field: any change wipes cached sessions
    # so the next call creates a fresh session with the right model.
    existing_models = _normalise_models_payload(existing.get("models"))
    if "models" in payload:
        incoming_models = _normalise_models_payload(payload.get("models"))
        # Merge against existing so partial PUTs don't blow away other buckets.
        merged_models = {**existing_models, **incoming_models}
        if merged_models != existing_models:
            sessions_need_reset = True
        next_cfg["models"] = merged_models
    else:
        next_cfg["models"] = existing_models

    # iter-13.36 — per-project opt-in to legacy "best-effort" fallback. When
    # True, a Factory failure silently re-routes through fabric (the old
    # behaviour that surfaced as the "why is claude-sonnet being fetched"
    # bug). Default False — Factory is strict.
    if "allow_fallback" in payload:
        next_cfg["allow_fallback"] = bool(payload.get("allow_fallback"))
    # iter-13.91.12 — host-anchored mode toggle. When ON, the orchestrator
    # bypasses the on-droid workspace bootstrap and pins cwd to the
    # droid's HOME (or, if the operator left `cwd` blank, omits it
    # entirely and lets Factory default to HOME). Inputs flow inline via
    # the message body; outputs are persisted on the LAMA host. Toggling
    # this invalidates cached sessions because the cwd will change.
    if "host_anchored" in payload:
        new_val = bool(payload.get("host_anchored"))
        if new_val != bool(existing.get("host_anchored", False)):
            sessions_need_reset = True
        next_cfg["host_anchored"] = new_val

    # iter-13.125 — Per-project transport mode + CLI tuning. Whitelisted
    # to avoid arbitrary settings injection. Mode change invalidates
    # sessions because the API path and CLI path use different session
    # bookkeeping.
    if "mode" in payload:
        raw_mode = (payload.get("mode") or "").strip().lower()
        new_mode = raw_mode if raw_mode in ("api", "cli") else ""
        if (new_mode or None) != (existing.get("mode") or None):
            sessions_need_reset = True
        next_cfg["mode"] = new_mode  # "" means "fall back to env default"
    if "cli_auto" in payload:
        raw = (payload.get("cli_auto") or "").strip().lower()
        next_cfg["cli_auto"] = raw if raw in ("low", "medium", "high") else ""
    if "cli_bin" in payload:
        next_cfg["cli_bin"] = (payload.get("cli_bin") or "").strip()

    if not next_cfg.get("enabled"):
        sessions_need_reset = True
    if sessions_need_reset:
        next_cfg["sessions_by_agent"] = {}

    # iter-13.81.4 — AUTO-ENABLE when full credentials are present but the
    # operator forgot to tick the "Enabled" checkbox before saving. This
    # mirrors the Console UI's own `targetEnabled` logic on the frontend
    # and prevents the "Factory configured but always shows Disabled"
    # footgun: if you bothered to save an app_key AND a computer_id, you
    # almost certainly meant to enable Factory routing for this project.
    # Still skippable: pass `enabled: false` explicitly in the payload to
    # override (the explicit value wins because it was set earlier in the
    # function before this auto-enable runs).
    _has_full_creds = bool((next_cfg.get("app_key") or "").strip()) and \
                      bool((next_cfg.get("computer_id") or "").strip())
    if _has_full_creds and not next_cfg.get("enabled") and "enabled" not in payload:
        next_cfg["enabled"] = True
        # Re-arming sessions because the route was effectively flipped on.
        next_cfg["sessions_by_agent"] = {}

    now = _now_iso()
    next_cfg["updated_at"] = now
    await projects.update_one(
        {"id": project_id},
        {"$set": {"settings.factory_orchestrator": next_cfg, "updated_at": now}},
    )
    saved = await projects.find_one({"id": project_id}, {"_id": 0, "settings": 1})

    # iter-13.36 — first-time activation auto-onboarding.
    # Fires when:
    #   • Factory is now enabled (next_cfg.enabled == True)
    #   • AND (was previously disabled OR never enabled before)
    #   • AND app_key + computer_id are present (otherwise the call would fail)
    #   • AND no onboarded_at timestamp yet (idempotent)
    try:
        now_enabled = bool(next_cfg.get("enabled"))
        has_credentials = bool((next_cfg.get("app_key") or "").strip()) and \
                          bool((next_cfg.get("computer_id") or "").strip())
        if now_enabled and not was_enabled_before and has_credentials and not already_onboarded:
            from onboarding import schedule_factory_first_time_onboarding
            schedule_factory_first_time_onboarding(project_id)
    except Exception:
        # Onboarding is best-effort — never block the Console save on it.
        pass

    return _settings_doc_to_config(saved, include_secret=False)


def _model_routing_directive(model_hint: str, agent_key: str) -> str:
    """iter-13.81.2 — natural-language pin Factory Droid can act on.

    Returned blob is empty when no operator pin exists (auto-routing).
    Prepended to every Factory prompt build so it survives input-too-long
    and timeout-retry shrinks.
    """
    h = (model_hint or "").strip()
    if not h or h.lower() == "auto":
        return ""
    stage_b, mode_b = _bucket_for_agent(agent_key)
    return (
        "MODEL ROUTING DIRECTIVE (operator-pinned in LAMA Console):\n"
        f"  • Pipeline node : {stage_b}.{mode_b} (agent_key={agent_key})\n"
        f"  • Required model: {h}\n"
        "  • This pin is non-negotiable for this turn. If you cannot use\n"
        f"    `{h}` directly, pick the closest match in your available\n"
        "    model menu (same vendor, same tier) and state which model\n"
        "    you actually ran in a single trailing line\n"
        "    `[model_used: <id>]`. Do NOT silently fall back to your\n"
        "    default model — the operator picked a specific tier for a\n"
        "    reason (cost / quality / latency).\n\n"
    )


def _build_prompt_text(
    messages: List[Dict[str, Any]],
    max_chars: int = FACTORY_PROMPT_MAX_CHARS,
    prefix: str = "",
) -> str:
    header = (
        "You are handling a routed orchestration request from LAMA.\n\n"
        "Follow the SYSTEM instruction and return only the assistant response.\n\n"
        # iter-13.81.8 — KB-IS-INLINE GROUNDING (replaces the iter-13.35
        # 'cat the cwd' directive that was actively misleading Factory
        # Droid). LAMA does NOT mirror the legacy source tree onto the
        # Droid Computer's filesystem. The `/lama-workspaces/{tid}__{pid}/`
        # path you may see in the SYSTEM block is a VIRTUAL CITATION ROOT
        # — it does NOT exist on disk. The real source lives in the
        # tenant's MongoDB (collections: kb_files, kb_entities, kb_toon,
        # kb_graph) and is INLINED into the SYSTEM block below as
        # KNOWLEDGE BASE / TOON / LEGACY WORKSPACE sections.
        "INLINE-KB GROUNDING (Factory Droid runtime — read carefully):\n"
        "  • The legacy source tree is NOT mirrored on the Droid filesystem\n"
        "    by default. LAMA's KB engine extracts every file from the\n"
        "    tenant's MongoDB (collections `kb_files`, `kb_entities`,\n"
        "    `kb_toon`, `kb_graph`, scoped by tenant_id + project_id) and\n"
        "    embeds the contents directly into the SYSTEM block below.\n"
        "  • HOWEVER — if a `KB-READINESS SNAPSHOT` block above declares\n"
        "    a `REAL DROID FILESYSTEM PATH` (iter-13.81.9 materialiser),\n"
        "    that folder DOES exist on disk and is the project's source\n"
        "    tree. You CAN `cd` there and use `ls`, `cat`, `grep`, `find`,\n"
        "    `rg` to read it directly. Use it for any cross-file analysis\n"
        "    the inlined blocks don't cover.\n"
        "  • If NO `REAL DROID FILESYSTEM PATH` is shown, do NOT shell out\n"
        "    (`cat`, `ls`, `grep`, `find`, `tree`, `head`, `sed`, `awk`,\n"
        "    `wc`, `rg`, `stat`) — the cwd is intentionally near-empty and\n"
        "    every such probe will return 'no such file or directory'.\n"
        "    Quote the inlined KNOWLEDGE BASE / TOON / LEGACY WORKSPACE\n"
        "    blocks instead.\n"
        "  • Cite files as `<virtual-root>/relative/path.ext:LINE` where\n"
        "    `<virtual-root>` is the citation root named in the SYSTEM\n"
        "    block's PROJECT-ISOLATED WORKSPACE contract. Only cite paths\n"
        "    that appear inside the inlined KB sections OR inside the\n"
        "    materialised folder — do NOT invent.\n"
        "  • If both the inlined KB sections AND any materialised path\n"
        "    look empty, the KB ingest produced zero artifacts. Report\n"
        "    THAT honestly to the user; do not speculate or pad.\n"
    )
    parts = [header]
    if prefix:
        parts.insert(0, prefix)
    remaining = max(1000, max_chars - sum(len(p) for p in parts) - 2)

    system_msg = ""
    convo: List[Dict[str, str]] = []
    for m in messages or []:
        role = ((m or {}).get("role") or "user").strip().lower()
        content = ((m or {}).get("content") or "").strip()
        if not content:
            continue
        if role == "system" and not system_msg:
            system_msg = content
        else:
            convo.append({"role": role, "content": content})

    if system_msg and remaining > 400:
        # iter-13.81.12 — SEND THE SYSTEM BLOCK IN FULL.
        # The previous `sys_cap = max(4000, max_chars * 0.65)` was the
        # root cause of every "the inlined KNOWLEDGE BASE / TOON /
        # LEGACY WORKSPACE blocks are not actually present" SRS
        # complaint. On a real legacy KB those blocks are 100–300 KB;
        # the 0.65 multiplier silently dropped 70%+ of the evidence
        # before the LLM ever saw it.
        #
        # New policy: the SYSTEM message IS the data. It must arrive
        # in full unless `remaining` would be exceeded, in which case
        # we truncate the conversation history first (it's almost
        # always low-value chit-chat compared to the KB blob).
        sys_block = f"SYSTEM:\n{system_msg}"
        if len(sys_block) > remaining:
            # Genuinely cannot fit — drop convo entirely and hard-cap
            # the system block. Log the gap so operators can see it.
            try:
                import logging as _log
                _log.getLogger("lama.factory").warning(
                    "Factory prompt over-cap: system_msg=%d chars, cap remaining=%d — "
                    "TRUNCATING SYSTEM BLOCK by %d chars. Raise "
                    "LAMA_FACTORY_PROMPT_MAX_CHARS (current=%d) if your KB exceeds it.",
                    len(system_msg), remaining, len(sys_block) - remaining, max_chars,
                )
            except Exception:
                pass
            sys_block = sys_block[:remaining]
            parts.append(sys_block)
            remaining = 0
        else:
            parts.append(sys_block)
            remaining -= len(sys_block) + 2

    convo_blocks: List[str] = []
    for m in reversed(convo):
        if remaining <= 250:
            break
        role = (m.get("role") or "user").upper()
        content = m.get("content") or ""
        per_msg_cap = min(len(content), max(1500, min(12000, remaining - 32)))
        block = f"{role}:\n{content[:per_msg_cap]}"
        if len(block) > remaining:
            block = block[:remaining]
        convo_blocks.append(block)
        remaining -= len(block) + 2

    parts.extend(reversed(convo_blocks))
    text = "\n\n".join(p for p in parts if p)
    return text[:max_chars]


def _is_input_too_long_error(txt: str) -> bool:
    t = (txt or "").lower()
    return (
        "input is too long" in t
        or "context length" in t
        or "maximum context" in t
        or "prompt is too long" in t
    )


def _extract_assistant_text(msg: Dict[str, Any]) -> str:
    content = (msg or {}).get("content") or []
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        btype = (block or {}).get("type")
        if btype == "text":
            txt = (block or {}).get("text") or ""
            if txt:
                parts.append(txt)
        elif btype == "tool_result":
            tr = (block or {}).get("content")
            if isinstance(tr, str) and tr.strip():
                parts.append(tr.strip())
    return "\n".join(parts).strip()


async def _persist_session_id(project_id: str, agent_key: str, session_id: str) -> None:
    # iter-13.91 — `agent_key` may already be a namespaced session-key
    # produced by `_session_key(...)`. Slug it again so dots/dollars
    # never bleed into the Mongo dot-path. Without this fix Mongo would
    # interpret `srs.generate` as a nested path and silently disable the
    # session cache (see the iter-13.91 docstring at the top of this file).
    key = _slug_key(agent_key)
    path = f"settings.factory_orchestrator.sessions_by_agent.{key}"
    await projects.update_one(
        {"id": project_id},
        {"$set": {path: session_id, "settings.factory_orchestrator.updated_at": _now_iso()}},
    )


async def _clear_session_id(project_id: str, agent_key: str) -> None:
    key = _slug_key(agent_key)  # iter-13.91 — same dot-path safety as _persist_session_id
    path = f"settings.factory_orchestrator.sessions_by_agent.{key}"
    await projects.update_one(
        {"id": project_id},
        {"$unset": {path: ""}, "$set": {"settings.factory_orchestrator.updated_at": _now_iso()}},
    )


async def _create_session(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    cfg: Dict[str, Any],
    computer_id: str,
    agent_key: str = "",
    *,
    tenant_id: str = "",
    project_id: str = "",
) -> str:
    body: Dict[str, Any] = {"computerId": computer_id}
    # iter-13.91 — auto-scope the cwd per (tenant, project) so each
    # project gets its own Droid workspace and the cross-project
    # filesystem-merge symptom disappears. Explicit cfg.cwd still wins.
    resolved_cwd = _resolve_cwd(cfg, tenant_id=tenant_id, project_id=project_id)
    if resolved_cwd:
        body["cwd"] = resolved_cwd

    # iter-13.82 — IMPORTANT: Factory's POST /sessions REJECTS a `model`
    # field with HTTP 400 "unrecognized_keys: [model]". Model selection is
    # a property of the Droid Computer (set in Factory → Settings → Droid
    # Computers → <computer> → Default Model), NOT a per-session override.
    # We keep the per-(stage, mode) model picks in Console for:
    #   1. Token-usage logging (so reports show which node ran which model)
    #   2. Forward-compat if Factory ever exposes a per-session override
    #   3. Forwarding into the message body as a hint (best-effort; Factory
    #      may silently ignore it but does not reject the message POST).
    # The chosen model is forwarded on the user message instead — see the
    # `model_hint` injection in route_via_factory_orchestrator below.
    _ = agent_key  # retained for signature stability + future use

    # HTTP 424 = "Computer disconnected during request". Factory managed computers
    # auto-pause when idle and auto-resume when targeted. Retry with backoff to
    # give the computer time to wake up (cold starts can take 60–120 s).
    # Between retries we also issue a cheap GET against the computer endpoint —
    # that touch is what Factory uses as a "wake" signal.
    # iter-13.91.3 — bumped retry budget so cold-starting computers don't
    # surface 424 to the user. Total wait now ≤ 5+15+30+45 = 95s before we
    # give up, comfortably inside the 120s Droid cold-start window.
    MAX_RETRIES = 4
    BACKOFF_SECS = [5, 15, 30, 45]
    last_err = ""
    workspace_bootstrap_attempted = False
    for attempt in range(MAX_RETRIES):
        resp = await client.post(f"{FACTORY_API_BASE}/sessions", headers=headers, json=body)
        if resp.status_code in (200, 201):
            sid = (resp.json() or {}).get("sessionId", "")
            if not sid:
                raise RuntimeError("Factory create session failed: sessionId missing")
            return sid
        last_err = f"HTTP {resp.status_code} {resp.text[:300]}"
        # iter-13.91.3 — dynamic workspace mkdir. Factory rejects unknown
        # cwd with HTTP 400 "Invalid cwd". One-shot: spin up a bootstrap
        # session and `mkdir -p` the path via the Droid's own shell, then
        # retry. If that fails (or we've already tried), drop the cwd and
        # fall back to the Droid's home directory so the call still
        # succeeds — conversation isolation via session_key is preserved.
        if (
            resp.status_code == 400
            and body.get("cwd")
            and "invalid cwd" in resp.text.lower()
            and not workspace_bootstrap_attempted
        ):
            workspace_bootstrap_attempted = True
            target_cwd = body.get("cwd", "")
            import logging as _log
            _log.getLogger("lama.factory").info(
                "Factory rejected cwd=%s (HTTP 400); bootstrapping via shell mkdir",
                target_cwd,
            )
            ok = await _ensure_workspace_exists(client, headers, computer_id, target_cwd)
            if ok.get("ok"):
                _WORKSPACE_READY.add((computer_id, target_cwd))  # iter-13.91.4 — keep eager cache honest
                # Retry POST /sessions with the same cwd — should succeed now.
                continue
            _log.getLogger("lama.factory").warning(
                "Workspace bootstrap failed for cwd=%s reason=%s detail=%s; falling back to Droid home",
                target_cwd, ok.get("reason"), (ok.get("detail") or "")[:200],
            )
            body.pop("cwd", None)
            continue
        if resp.status_code == 424:
            wait = BACKOFF_SECS[min(attempt, len(BACKOFF_SECS) - 1)]
            import logging as _log
            _log.getLogger("lama.factory").info(
                "Factory computer disconnected (424); waiting %ds before retry %d/%d",
                wait, attempt + 1, MAX_RETRIES,
            )
            # Nudge the computer awake — a GET on /computers/{id} is enough to
            # trigger Factory's auto-resume. Failures here are non-fatal.
            try:
                await client.get(f"{FACTORY_API_BASE}/computers/{quote(computer_id, safe='')}", headers=headers)
            except Exception:
                pass
            await asyncio.sleep(wait)
            continue
        # Non-retriable error
        break
    raise RuntimeError(
        f"Factory create session failed: {last_err}. "
        "Make sure your Droid Computer is Active in Factory Settings → Droid Computers."
    )


async def _resolve_computer_id(client: httpx.AsyncClient, headers: Dict[str, str], computer_ref: str) -> str:
    ref = (computer_ref or "").strip()
    if not ref:
        raise RuntimeError("Factory orchestrator is enabled, but computer_id/computer_name is missing in Console.")

    # iter-13.91.7 — Per-process cache. The MiniConsole polls the test
    # endpoint every 30s, which previously hit `GET /computers/name/...`
    # every single time even though the (ref → id) mapping never changes
    # for the life of a Droid. Cache eliminates that traffic entirely.
    cached = _COMPUTER_ID_CACHE.get(ref)
    if cached:
        return cached

    # 1) Try exact-name lookup first (supports users entering computer name).
    try:
        by_name = await client.get(f"{FACTORY_API_BASE}/computers/name/{quote(ref, safe='')}", headers=headers)
        if by_name.status_code == 200:
            cid = (by_name.json() or {}).get("id", "")
            if cid:
                _COMPUTER_ID_CACHE[ref] = cid
                return cid
    except Exception:
        pass

    # 2) Fallback to list + match by id/name/hostname.
    resp = await client.get(f"{FACTORY_API_BASE}/computers", headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Factory list computers failed: HTTP {resp.status_code} {resp.text[:300]}")
    rows = (resp.json() or {}).get("computers") or []
    ref_l = ref.lower()
    for c in rows:
        if (c.get("id") or "") == ref:
            cid = c.get("id") or ""
            if cid:
                _COMPUTER_ID_CACHE[ref] = cid
            return cid
    for c in rows:
        if ((c.get("name") or "").lower() == ref_l) or ((c.get("hostname") or "").lower() == ref_l):
            cid = c.get("id") or ""
            if cid:
                _COMPUTER_ID_CACHE[ref] = cid
            return cid
    raise RuntimeError(
        "Factory computer not found. Use a valid Computer ID or name from Settings → Droid Computers."
    )


async def _fetch_messages(client: httpx.AsyncClient, headers: Dict[str, str], session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    resp = await client.get(
        f"{FACTORY_API_BASE}/sessions/{session_id}/messages",
        headers=headers,
        params={"limit": str(limit)},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Factory get messages failed: HTTP {resp.status_code} {resp.text[:300]}")
    return (resp.json() or {}).get("messages") or []


async def _latest_assistant_message(client: httpx.AsyncClient, headers: Dict[str, str], session_id: str) -> Optional[Dict[str, Any]]:
    msgs = await _fetch_messages(client, headers, session_id=session_id, limit=100)
    assistants = [m for m in msgs if (m or {}).get("role") == "assistant"]
    if not assistants:
        return None
    assistants.sort(key=lambda x: x.get("createdAt", 0))
    return assistants[-1]


async def _log_factory_usage(
    *,
    project_id: str,
    agent_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
) -> None:
    total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    if not total_tokens:
        return
    now = _now_iso()
    stage = ""
    used_all_time = 0
    try:
        agent = await ac_col.find_one({"key": agent_key}, {"_id": 0}) if agent_key else None
        stage = (agent or {}).get("stage", "")
        used_all_time = (agent or {}).get("tokens_used_all_time", 0)
    except Exception:
        agent = None
    log = TokenUsageLog(
        project_id=project_id,
        agent_key=agent_key or "unknown",
        stage=stage,
        model=model,
        provider_type="factory",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=0.0,
        duration_ms=duration_ms,
        status="success",
        error="",
    )
    try:
        await log_col.insert_one(log.model_dump())
    except Exception:
        pass
    if agent_key:
        try:
            await ac_col.update_one(
                {"key": agent_key},
                {"$set": {
                    "tokens_used_last_run": total_tokens,
                    "tokens_used_all_time": used_all_time + total_tokens,
                    "last_run_at": now,
                    "last_run_model": model,
                    "last_run_input_tokens": prompt_tokens,
                    "last_run_output_tokens": completion_tokens,
                    "last_run_cost_usd": 0.0,
                    "updated_at": now,
                }},
            )
        except Exception:
            pass


async def route_via_factory_orchestrator(
    *,
    messages: List[Dict[str, Any]],
    project_id: str,
    agent_key: str,
    timeout: float = 120.0,
) -> Optional[Dict[str, Any]]:
    # iter-13.124 — Optional CLI mode. When the project config sets
    # `mode: "cli"` (Console toggle) OR the env var
    # `LAMA_FACTORY_MODE=cli` is set as a global default, skip the
    # HTTP-API Computer/sandbox path entirely and shell out to the
    # `droid exec` binary on the LAMA host. Per-project mode wins over
    # the env var so operators can A/B without restarting LAMA.
    # See backend/fabric/factory_cli.py for the full rationale + trade-offs.
    try:
        from fabric import factory_cli as _factory_cli  # local import; cheap
        cfg_for_mode = await get_project_factory_orchestrator_config(project_id, include_secret=False)
        project_mode = (cfg_for_mode.get("mode") or "").strip().lower()
        use_cli = (project_mode == "cli") or (
            project_mode != "api" and _factory_cli.is_cli_mode_enabled()
        )
        if use_cli:
            cwd = (cfg_for_mode.get("cwd") or "").strip() or None
            model_hint = _resolve_model_for_agent(cfg_for_mode, agent_key) if cfg_for_mode else None
            return await _factory_cli.route_via_factory_cli(
                messages=messages,
                project_id=project_id,
                agent_key=agent_key,
                timeout=timeout,
                model_hint=model_hint,
                cwd=cwd,
                cli_bin=cfg_for_mode.get("cli_bin") or None,
                auto_level=cfg_for_mode.get("cli_auto") or None,
            )
    except Exception as _cli_exc:
        import logging as _log_cli
        _log_cli.getLogger("lama.factory").warning(
            "Factory CLI mode preflight failed (will use API path) pid=%s err=%s",
            project_id, _cli_exc,
        )

    # iter-13.81.22 — FAILED-call telemetry wrapper. The inner function
    # carries all the existing logic; this wrapper just ensures EVERY
    # failure path emits a uniform `Factory routing FAILED ...` log
    # before re-raising. Without this, an exception inside _create_session,
    # _wake_computer, or the post-loop would surface only via the upstream
    # `fabric_call` warning — operators had no per-attempt timestamp /
    # error-class breakdown.
    import logging as _log_fail
    import time as _time_fail
    _t_start = _time_fail.time()
    try:
        return await _route_via_factory_orchestrator_impl(
            messages=messages, project_id=project_id,
            agent_key=agent_key, timeout=timeout,
        )
    except Exception as _fail_exc:
        _elapsed = int((_time_fail.time() - _t_start) * 1000)
        _log_fail.getLogger("lama.factory").warning(
            "Factory routing FAILED agent=%s pid=%s elapsed=%dms "
            "err_type=%s err=%s",
            agent_key, project_id, _elapsed,
            type(_fail_exc).__name__, str(_fail_exc)[:300],
        )
        raise


async def _route_via_factory_orchestrator_impl(
    *,
    messages: List[Dict[str, Any]],
    project_id: str,
    agent_key: str,
    timeout: float = 120.0,
) -> Optional[Dict[str, Any]]:
    if not project_id:
        return None
    cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    if not cfg.get("enabled"):
        return None
    app_key = (cfg.get("app_key") or "").strip()
    computer_ref = (cfg.get("computer_id") or "").strip()
    if not app_key:
        raise RuntimeError("Factory orchestrator is enabled, but app key is not configured in Console.")
    if not computer_ref:
        raise RuntimeError("Factory orchestrator is enabled, but computer_id/computer_name is missing in Console.")

    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
    effective_timeout = max(timeout, FACTORY_SLOW_AGENT_TIMEOUT_SEC) if agent_key in _SLOW_FACTORY_AGENTS else timeout
    prompt_caps = [
        FACTORY_PROMPT_MAX_CHARS,
        FACTORY_PROMPT_RETRY_MAX_CHARS,
        24000,
        16000,
        6000,
        3000,
    ]
    cap_idx = 0
    # iter-13.81.2 — resolve the per-bucket model pin ONCE here, then
    # forward it as a sticky prefix on every prompt build (initial, input-
    # too-long shrink, timeout-retry shrink). Previously the pin was only
    # used for post-response usage logging, so Factory Droid silently ran
    # its Computer default — the Console grid was cosmetic.
    _model_hint = _resolve_model_for_agent(cfg, agent_key)
    _routing_prefix = _model_routing_directive(_model_hint, agent_key)
    prompt_text = _build_prompt_text(messages, max_chars=prompt_caps[cap_idx], prefix=_routing_prefix)
    prompt_tokens = _estimate_tokens(prompt_text)
    t0 = time.time()

    # iter-13.39 — track how many times we've retried-on-timeout (separate
    # counter from the input-too-long cap_idx walk). We cap this at 1 so a
    # genuinely dead Droid doesn't burn 2× the timeout before surfacing
    # the failure.
    timeout_retries = 0
    TIMEOUT_RETRY_LIMIT = 1

    # iter-13.81.22 — START-OF-CALL telemetry. The single biggest "silently
    # lost" complaint was operators having no visible signal that a Factory
    # call had even been DISPATCHED. We log a short marker here so
    # `docker logs lama | grep "lama.factory"` shows every routing attempt.
    import logging as _log_top
    _log_top.getLogger("lama.factory").info(
        "Factory routing STARTED agent=%s pid=%s computer_ref=%s prompt_chars=%d model_hint=%s",
        agent_key, project_id, computer_ref, len(prompt_text), _model_hint or "auto",
    )

    async with httpx.AsyncClient(timeout=effective_timeout, verify=_http_verify()) as client:
        computer_id = await _resolve_computer_id(client, headers, computer_ref)
        # ── iter-13.91.4 — explicit Droid-wake + eager workspace ──────
        # Two pre-flight steps that USED to be implicit / lazy:
        #   1. Poke the Computer awake via GET /computers/{id} so a cold
        #      Droid has a head start on resuming before we hit
        #      POST /sessions. Throttled per-process so back-to-back
        #      agent calls don't spam Factory.
        #   2. Run the `mkdir -p` bootstrap for this (tenant, project)
        #      cwd before the first POST /sessions, instead of waiting
        #      for Factory to reject with HTTP 400 "Invalid cwd" and
        #      then retrying. Memoised per process so the cost is paid
        #      at most once per (computer_id, cwd) per uvicorn worker.
        # Both are best-effort — failures fall through to the original
        # lazy paths inside `_create_session`.
        await _wake_computer(client, headers, computer_id)
        # Need tenant_id to compute the per-project cwd; defer the actual
        # `find_one` lookup that the post+poll loop already does until we
        # have it, then reuse for both the workspace bootstrap and the
        # session-key namespacing below.
        _proj_doc = await projects.find_one(
            {"id": project_id}, {"_id": 0, "tenant_id": 1}
        ) or {}
        tenant_id = (_proj_doc.get("tenant_id") or "").strip() or "tenant_default"
        target_cwd = _resolve_cwd(cfg, tenant_id=tenant_id, project_id=project_id)
        if target_cwd:
            try:
                # Best-effort — return value is intentionally ignored here;
                # `_create_session`'s lazy bootstrap-on-400 path is the
                # safety net that surfaces real failures to the agent caller.
                await _ensure_workspace_ready_cached(
                    client, headers, computer_id, target_cwd
                )
            except Exception:
                # Best-effort — _create_session's own bootstrap-on-400
                # path is still there as a fallback safety net.
                pass
        # ── iter-13.91 — per-(tenant, project, agent) session cache ───
        # (tenant_id already loaded above; do NOT re-query Mongo.)
        sessions_map = cfg.get("sessions_by_agent") or {}
        session_key = _session_key(tenant_id, project_id, agent_key)
        session_id = sessions_map.get(session_key, "")
        # Back-compat: legacy installs may have a flat or nested entry
        # under the raw agent_key (pre-iter-13.91). Honour it ONCE then
        # migrate to the namespaced key on next persist.
        if not session_id:
            legacy = sessions_map.get(_slug_key(agent_key)) or sessions_map.get(agent_key)
            if isinstance(legacy, str) and legacy:
                session_id = legacy

        if not session_id:
            session_id = await _create_session(
                client, headers, cfg, computer_id,
                agent_key=agent_key, tenant_id=tenant_id, project_id=project_id,
            )
            await _persist_session_id(project_id, session_key, session_id)

        # The outer `while True` is the timeout-retry loop. The inner block
        # is the original post-message + poll-for-reply loop. On timeout we
        # shrink the prompt and re-enter the outer loop with a fresh user
        # message in the same session.
        import logging as _log
        _logger = _log.getLogger("lama.factory")

        while True:
            create_new_session_once = False
            while True:
                try:
                    prev = await _latest_assistant_message(client, headers, session_id)
                except Exception as e:
                    if ("HTTP 404" in str(e)) and not create_new_session_once:
                        create_new_session_once = True
                        await _clear_session_id(project_id, session_key)
                        session_id = await _create_session(
                            client, headers, cfg, computer_id,
                            agent_key=agent_key, tenant_id=tenant_id, project_id=project_id,
                        )
                        await _persist_session_id(project_id, session_key, session_id)
                        continue
                    raise
                prev_id = (prev or {}).get("id", "")

                # iter-13.81.19 — Transport + 5xx retry. Wrap the POST so
                # `httpx.RemoteProtocolError` / `ReadError` / `WriteError`
                # / `ConnectError` from Factory's edge dropping the
                # connection mid-flight are treated as if they were a
                # transient 5xx and retried.
                async def _post_once():
                    return await client.post(
                        f"{FACTORY_API_BASE}/sessions/{session_id}/messages",
                        headers=headers,
                        json={"text": prompt_text, "computerId": computer_id},
                    )

                _transient_excs = (
                    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
                    httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout,
                )
                msg_resp = None  # type: ignore[assignment]
                try:
                    msg_resp = await _post_once()
                except _transient_excs as _te0:
                    msg_resp = None
                    _initial_te = _te0
                else:
                    _initial_te = None

                _5xx_retries = int(os.environ.get("LAMA_FACTORY_5XX_RETRIES", "4") or "4")
                _5xx_attempt = 0
                # iter-13.81.20 — when half the retries have failed,
                # ditch the (probably bad) session and create a fresh
                # one. A session that returned 5xx twice is often stuck
                # at Factory's end (Droid state corruption, agent route
                # cache miss, etc.) — a brand-new session usually
                # bypasses the problem.
                _recreate_at = max(1, _5xx_retries // 2)
                _session_recreated_in_loop = False
                while _5xx_attempt < _5xx_retries:
                    _retry_reason = ""
                    if msg_resp is None:
                        _retry_reason = f"transport-{type(_initial_te).__name__ if _initial_te else 'unknown'}"
                    elif msg_resp.status_code in (500, 502, 503, 504, 524):
                        _retry_reason = f"http-{msg_resp.status_code}"
                    else:
                        break
                    _5xx_attempt += 1
                    _backoff = 0.8 * (2 ** (_5xx_attempt - 1))
                    try:
                        _body_preview = (msg_resp.text[:160] if msg_resp is not None else
                                         (str(_initial_te)[:160] if _initial_te else ""))
                        _logger.warning(
                            "Factory /messages transient failure (reason=%s attempt=%d/%d) — "
                            "session=%s agent=%s body=%s; backing off %.1fs",
                            _retry_reason, _5xx_attempt, _5xx_retries,
                            session_id[:8] if session_id else "?", agent_key,
                            _body_preview, _backoff,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(_backoff)
                    # Mid-retry session recreation
                    if _5xx_attempt == _recreate_at and not _session_recreated_in_loop:
                        try:
                            _logger.warning(
                                "Factory /messages: recreating session after %d transient "
                                "failures (old_session=%s agent=%s) — fresh session usually "
                                "bypasses sticky-broken state at Factory's end.",
                                _5xx_attempt, session_id[:8] if session_id else "?", agent_key,
                            )
                            await _clear_session_id(project_id, session_key)
                            session_id = await _create_session(
                                client, headers, cfg, computer_id,
                                agent_key=agent_key, tenant_id=tenant_id, project_id=project_id,
                            )
                            await _persist_session_id(project_id, session_key, session_id)
                            _session_recreated_in_loop = True
                            # Re-derive prev_id from the brand-new session so the
                            # poll loop below sees the new assistant reply as "new".
                            try:
                                prev = await _latest_assistant_message(client, headers, session_id)
                                prev_id = (prev or {}).get("id", "")
                            except Exception:
                                prev_id = ""
                        except Exception as _crex:
                            _logger.warning(
                                "Factory session recreation failed: %s — continuing with retries "
                                "on the original session.", str(_crex)[:200],
                            )
                    try:
                        msg_resp = await _post_once()
                        _initial_te = None
                    except _transient_excs as _te:
                        msg_resp = None
                        _initial_te = _te

                if msg_resp is None:
                    # All retries exhausted with transport errors.
                    raise RuntimeError(
                        f"Factory post message failed after {_5xx_retries} transient-error retries: "
                        f"{type(_initial_te).__name__}: {str(_initial_te)[:200]}"
                    ) from _initial_te

                if msg_resp.status_code == 400 and _is_input_too_long_error(msg_resp.text):
                    if cap_idx < len(prompt_caps) - 1:
                        cap_idx += 1
                        prompt_text = _build_prompt_text(messages, max_chars=prompt_caps[cap_idx], prefix=_routing_prefix)
                        prompt_tokens = _estimate_tokens(prompt_text)
                        continue
                    raise RuntimeError(
                        "Factory post message failed: input too long even after aggressive prompt shrinking. "
                        "Factory auto-routing still rejected the request after shrinking."
                    )
                if msg_resp.status_code == 404 and not create_new_session_once:
                    create_new_session_once = True
                    await _clear_session_id(project_id, session_key)
                    session_id = await _create_session(
                        client, headers, cfg, computer_id,
                        agent_key=agent_key, tenant_id=tenant_id, project_id=project_id,
                    )
                    await _persist_session_id(project_id, session_key, session_id)
                    continue
                if msg_resp.status_code == 424:
                    # Computer disconnected mid-session — wake it, then
                    # re-create the session and retry. Cold-start window
                    # is up to 120s; we wait 30s here and rely on
                    # _create_session's own 95s budget for further retries.
                    await _clear_session_id(project_id, session_key)
                    try:
                        await client.get(
                            f"{FACTORY_API_BASE}/computers/{quote(computer_id, safe='')}",
                            headers=headers,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(30)
                    session_id = await _create_session(
                        client, headers, cfg, computer_id,
                        agent_key=agent_key, tenant_id=tenant_id, project_id=project_id,
                    )
                    await _persist_session_id(project_id, session_key, session_id)
                    continue
                if msg_resp.status_code != 200:
                    raise RuntimeError(f"Factory post message failed: HTTP {msg_resp.status_code} {msg_resp.text[:300]}")
                break

            # Per-attempt deadline — re-derived each retry so we don't keep
            # shrinking against a single shared clock that's already half-spent.
            attempt_deadline = time.time() + effective_timeout
            poll_interval = max(0.25, FACTORY_POLL_INTERVAL_SEC)
            # iter-13.81.22 — polling-loop resilience + observability.
            # The previous unguarded `_latest_assistant_message` was the
            # #1 silent-loss cause: any transient `httpx.ReadError` /
            # `RemoteProtocolError` / 5xx during the poll killed the
            # ENTIRE request — every codegen file that hit this branch
            # turned into an emergency scaffold and the operator had
            # zero log trail explaining why. We now:
            #   • catch transport + 5xx during polling → treat as "no
            #     new message yet, keep polling" (transient blip)
            #   • emit a heartbeat every 15s so logs prove the call is
            #     alive but the Droid hasn't replied yet (vs lost)
            #   • cap consecutive poll-error count so a wedged session
            #     escalates to outer timeout-retry instead of spinning
            _poll_t0 = time.time()
            _poll_last_heartbeat = _poll_t0
            _poll_err_streak = 0
            _MAX_POLL_ERR_STREAK = 8  # ~2s of constant errors → escalate
            while time.time() < attempt_deadline:
                try:
                    last = await _latest_assistant_message(client, headers, session_id)
                    _poll_err_streak = 0  # reset on any successful fetch
                except (httpx.RemoteProtocolError, httpx.ReadError,
                        httpx.WriteError, httpx.ConnectError, httpx.ReadTimeout,
                        httpx.PoolTimeout) as _poll_te:
                    _poll_err_streak += 1
                    if _poll_err_streak <= 2 or _poll_err_streak % 3 == 0:
                        _logger.warning(
                            "Factory poll transport blip (streak=%d) — agent=%s "
                            "session=%s err=%s — will keep polling.",
                            _poll_err_streak, agent_key,
                            session_id[:8] if session_id else "?",
                            type(_poll_te).__name__,
                        )
                    if _poll_err_streak >= _MAX_POLL_ERR_STREAK:
                        _logger.warning(
                            "Factory poll wedged after %d consecutive errors — "
                            "breaking out to outer timeout-retry. agent=%s",
                            _poll_err_streak, agent_key,
                        )
                        break  # escalate to outer timeout-retry / shrink
                    await asyncio.sleep(poll_interval)
                    continue
                except RuntimeError as _poll_re:
                    # _fetch_messages raises RuntimeError on non-200.
                    # 5xx → transient, keep polling. 4xx → likely the
                    # session went stale → break out so outer loop
                    # decides (e.g. 404 → recreate session path is
                    # handled by next /messages POST).
                    _emsg = str(_poll_re)
                    if any(c in _emsg for c in ("HTTP 500", "HTTP 502", "HTTP 503",
                                                "HTTP 504", "HTTP 524")):
                        _poll_err_streak += 1
                        if _poll_err_streak <= 2 or _poll_err_streak % 3 == 0:
                            _logger.warning(
                                "Factory poll 5xx (streak=%d) agent=%s session=%s "
                                "err=%s — keep polling.",
                                _poll_err_streak, agent_key,
                                session_id[:8] if session_id else "?",
                                _emsg[:120],
                            )
                        if _poll_err_streak >= _MAX_POLL_ERR_STREAK:
                            _logger.warning(
                                "Factory poll wedged after %d consecutive 5xx — "
                                "escalating to outer timeout-retry. agent=%s",
                                _poll_err_streak, agent_key,
                            )
                            break
                        await asyncio.sleep(poll_interval)
                        continue
                    # Non-transient — re-raise so caller sees real error.
                    _logger.warning(
                        "Factory poll non-transient error agent=%s err=%s",
                        agent_key, _emsg[:200],
                    )
                    raise
                # iter-13.81.22 — heartbeat every 15s of silence.
                _now = time.time()
                if _now - _poll_last_heartbeat >= 15.0:
                    _logger.info(
                        "Factory poll alive — agent=%s pid=%s waited=%ds "
                        "(no new assistant message yet). Droid still thinking.",
                        agent_key, project_id, int(_now - _poll_t0),
                    )
                    _poll_last_heartbeat = _now
                if last and (last.get("id") != prev_id):
                    text = _extract_assistant_text(last)
                    if text:
                        completion_tokens = _estimate_tokens(text)
                        duration_ms = int((time.time() - t0) * 1000)
                        # iter-13.80 + iter-13.81 — log the per-bucket resolved
                        # model with a `factory/` prefix so usage reports clearly
                        # separate routed-through-Factory calls from direct-provider
                        # ones AND show which Discovery / DataModel / etc. node
                        # consumed which model.
                        chosen = _resolve_model_for_agent(cfg, agent_key)
                        if chosen and chosen.lower() != "auto":
                            model = f"factory/{chosen}"
                        else:
                            model = "factory/auto"
                        # iter-13.81.22 — COMPLETED telemetry counterpart to
                        # the STARTED log at the top of route_via_factory_orchestrator.
                        _logger.info(
                            "Factory routing COMPLETED agent=%s pid=%s "
                            "elapsed=%dms prompt_tokens=%d completion_tokens=%d "
                            "model=%s",
                            agent_key, project_id, duration_ms,
                            prompt_tokens, completion_tokens, model,
                        )
                        await _log_factory_usage(
                            project_id=project_id,
                            agent_key=agent_key,
                            model=model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            duration_ms=duration_ms,
                        )
                        return {
                            "content": text,
                            "model": model,
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens,
                                "total_tokens": prompt_tokens + completion_tokens,
                            },
                        }
                await asyncio.sleep(poll_interval)

            # ── iter-13.39 — TIMEOUT RETRY with shrunk prompt ─────────────
            # The Droid blew the deadline. Two common causes:
            #   1. The prompt is huge (50 KB SRS system block) and the Droid
            #      is still thinking. Shrinking to ~16 KB usually returns in
            #      under 90 s.
            #   2. The Computer is asleep / overloaded. A retry on the same
            #      session re-pokes the API and often succeeds.
            # We cap retries at TIMEOUT_RETRY_LIMIT so a dead Droid surfaces
            # honestly instead of doubling the wait.
            elapsed_ms = int((time.time() - t0) * 1000)
            if timeout_retries < TIMEOUT_RETRY_LIMIT and FACTORY_TIMEOUT_RETRY_MAX_CHARS > 0:
                timeout_retries += 1
                shrunk_cap = min(prompt_caps[cap_idx], FACTORY_TIMEOUT_RETRY_MAX_CHARS)
                # Force forward progress: if the shrunk cap == current cap,
                # walk to the next entry in prompt_caps so we genuinely shrink.
                if shrunk_cap >= prompt_caps[cap_idx]:
                    cap_idx = min(cap_idx + 1, len(prompt_caps) - 1)
                    shrunk_cap = prompt_caps[cap_idx]
                _logger.warning(
                    "Factory timeout after %dms for agent=%s pid=%s — retrying with cap=%d (attempt %d/%d)",
                    elapsed_ms, agent_key, project_id, shrunk_cap,
                    timeout_retries + 1, TIMEOUT_RETRY_LIMIT + 1,
                )
                prompt_text = _build_prompt_text(messages, max_chars=shrunk_cap, prefix=_routing_prefix)
                prompt_tokens = _estimate_tokens(prompt_text)
                continue  # re-enter the outer post+poll loop

            # All retries exhausted — surface an actionable error.
            raise RuntimeError(
                f"Factory assistant response timed out after "
                f"{int(time.time() - t0)}s for agent='{agent_key}' "
                f"(prompt_chars={len(prompt_text)}, attempts={timeout_retries + 1}). "
                "Likely causes: (a) the Factory Droid Computer is asleep / "
                "overloaded — open Factory → Settings → Droid Computers and "
                "confirm it is Active; (b) the prompt is genuinely too long "
                "for the Droid to handle — set "
                "LAMA_FACTORY_TIMEOUT_RETRY_MAX_CHARS=8000 for more aggressive "
                "shrinking; (c) the per-agent timeout is too tight — bump "
                "LAMA_FACTORY_SLOW_AGENT_TIMEOUT_SEC (current="
                f"{int(FACTORY_SLOW_AGENT_TIMEOUT_SEC)}s)."
            )


async def test_project_factory_orchestrator_connection(
    project_id: str,
    timeout: float = 30.0,
    *,
    with_workspace: bool = False,
) -> Dict[str, Any]:
    """Health-check the Factory orchestrator for a project.

    iter-13.91.7 — `with_workspace` is **False by default** so periodic
    UI polling (MiniConsole's 30s health-check loop) doesn't spin up a
    fresh Factory bootstrap session every cycle. The workspace bootstrap
    is a heavy operation (POST /sessions + shell mkdir + ~30s polling),
    and was the source of the "too many requests to Factory" bug.

    Pass `with_workspace=True` only when the caller actually wants to
    verify (or force) Droid filesystem state — the dedicated
    `/console/factory-orchestrator/workspace` endpoint already does this.
    """
    cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    app_key = (cfg.get("app_key") or "").strip()
    computer_ref = (cfg.get("computer_id") or "").strip()
    if not app_key:
        raise RuntimeError("App key is not configured.")
    if not computer_ref:
        raise RuntimeError("Computer ID or name is not configured.")
    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=max(timeout, 90.0), verify=_http_verify()) as client:
        cid = await _resolve_computer_id(client, headers, computer_ref)
        proj = await projects.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1}) or {}
        tenant_id = (proj.get("tenant_id") or "").strip() or "tenant_default"
        target_cwd = _resolve_cwd(cfg, tenant_id=tenant_id, project_id=project_id)
        workspace_ready = False
        workspace_error = ""
        workspace_reason = ""
        # iter-13.91.7 — skip the heavy bootstrap unless the caller
        # explicitly opts in. The cached `_WORKSPACE_READY` set still
        # gets honoured below so previously-bootstrapped workspaces are
        # reported as ready for free.
        if target_cwd and (cid, target_cwd) in _WORKSPACE_READY:
            workspace_ready = True
            workspace_reason = "cached"
        elif with_workspace and target_cwd:
            try:
                _ws = await _ensure_workspace_exists(
                    client, headers, cid, target_cwd, poll_seconds=60.0,
                )
                workspace_ready = bool(_ws.get("ok"))
                workspace_reason = _ws.get("reason") or ""
                if not workspace_ready:
                    workspace_error = (_ws.get("detail") or "")[:300]
                elif workspace_ready:
                    _WORKSPACE_READY.add((cid, target_cwd))
            except Exception as e:
                workspace_error = str(e)[:300]
        return {
            "ok": True,
            "computer_ref": computer_ref,
            "computer_id": cid,
            "enabled": bool(cfg.get("enabled")),
            "workspace_cwd": target_cwd,
            "workspace_ready": workspace_ready,
            "workspace_reason": workspace_reason,
            "workspace_error": workspace_error,
            "workspace_checked": bool(with_workspace),
        }


async def ensure_project_workspace(project_id: str, timeout: float = 90.0) -> Dict[str, Any]:
    """Proactively create the per-(tenant, project) workspace dir on the
    Droid filesystem. Called during onboarding and exposed via the
    Console "Test connection" endpoint so users can verify (and force)
    workspace creation without waiting for the first agent call.

    iter-13.91.9 — auto-fallback: if the requested cwd lands on a
    read-only mount (a common Droid hardening — `/root` and `/workspace`
    are both read-only on some images), the bootstrap silently retries
    with `<DROID_HOME>/lama_<tenant>_<project>` using the `$HOME` value
    learned from the first attempt. The actually-used path is returned
    in the response and persisted back to the project config so future
    calls skip the fallback dance.
    """
    cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    if not cfg.get("enabled"):
        return {"ok": False, "reason": "factory_not_enabled"}
    app_key = (cfg.get("app_key") or "").strip()
    computer_ref = (cfg.get("computer_id") or "").strip()
    if not app_key or not computer_ref:
        return {"ok": False, "reason": "factory_incomplete_config"}
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1}) or {}
    tenant_id = (proj.get("tenant_id") or "").strip() or "tenant_default"
    target_cwd = _resolve_cwd(cfg, tenant_id=tenant_id, project_id=project_id)
    if not target_cwd:
        # iter-13.91.12 — host-anchored mode is the only legitimate way
        # for `_resolve_cwd` to return "". When the operator has opted
        # in, there's nothing to bootstrap on the droid — Factory's
        # POST /sessions will default cwd to the droid's HOME (which
        # always exists on every OS). We STILL verify Factory is
        # reachable and the droid Computer resolves + wakes, so the
        # operator gets honest connectivity feedback (otherwise an
        # offline droid would silently look "green" here and only fail
        # on the first agent call).
        if cfg.get("host_anchored"):
            headers_h = {
                "Authorization": f"Bearer {app_key}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(
                timeout=max(timeout, 90.0), verify=_http_verify()
            ) as client_h:
                try:
                    cid_h = await _resolve_computer_id(
                        client_h, headers_h, computer_ref
                    )
                except Exception as e:
                    return {
                        "ok": False,
                        "reason": "resolve_computer_failed",
                        "error": str(e)[:300],
                        "host_anchored": True,
                        "detail": (
                            "Host-anchored mode is ON but Factory rejected the "
                            "computer reference. Verify the Computer ID / name "
                            f"in Factory → Settings → Droid Computers. ({str(e)[:120]})"
                        ),
                    }
                # Force-wake so a paused droid resumes BEFORE the first
                # real agent call (instead of failing it with 424).
                woken = await _wake_computer(
                    client_h, headers_h, cid_h, force=True
                )
                # iter-13.91.14 — host-anchored "Create workspace" must
                # ACTUALLY verify the session API works, not just that
                # GET /computers returns 200. We've seen droids where
                # GET succeeds (computer metadata exists) but POST
                # /sessions returns 424 (underlying VM is paused). The
                # only way to know for sure is to try a real session.
                # We make ONE attempt; if it's 424 we surface the same
                # `droid_disconnected` reason as the regular bootstrap.
                session_probe: Dict[str, Any] = {"http": 0, "raw": ""}
                try:
                    _probe = await client_h.post(
                        f"{FACTORY_API_BASE}/sessions",
                        headers=headers_h,
                        json={"computerId": cid_h},
                    )
                    session_probe = {
                        "http": _probe.status_code,
                        "raw": (_probe.text or "")[:200],
                    }
                except Exception as _pe:
                    session_probe = {"http": 0, "raw": f"network: {str(_pe)[:150]}"}
                if session_probe["http"] not in (200, 201):
                    diag = await _get_computer_diagnostic(
                        client_h, headers_h, cid_h,
                    )
                    diag_line = ""
                    if diag:
                        diag_line = "Factory says: " + ", ".join(
                            f"{k}={v}" for k, v in diag.items()
                        ) + ".\n\n"
                    # iter-13.91.15 — try one round of stale-session
                    # cleanup before giving up. If the underlying issue
                    # is "Computer disconnected during request" caused
                    # by a session lock, this often clears it; the next
                    # operator click on 'Create workspace' will then
                    # succeed without any Factory-side action.
                    problem_h = _parse_problem_detail(session_probe["raw"])
                    sub_reason_h = _classify_424_detail(problem_h.get("detail") or "")
                    cleaned = 0
                    if (
                        session_probe["http"] == 424
                        and sub_reason_h == "droid_agent_unreachable"
                    ):
                        try:
                            cleaned = await _cleanup_stale_sessions(
                                client_h, headers_h, cid_h,
                            )
                        except Exception:
                            cleaned = 0
                    return {
                        "ok": False,
                        "reason": (sub_reason_h
                                   if session_probe["http"] == 424
                                   else "session_probe_failed"),
                        "host_anchored": True,
                        "computer_id": cid_h,
                        "diagnostic": diag,
                        "session_probe_http": session_probe["http"],
                        "factory_detail": problem_h.get("detail") or "",
                        "factory_request_id": problem_h.get("request_id") or "",
                        "stale_sessions_cleaned": cleaned,
                        "detail": (
                            f"Host-anchored mode is ON and Factory's metadata "
                            f"endpoint accepted the computer ({cid_h}), but a "
                            f"test POST /sessions returned HTTP "
                            f"{session_probe['http']}.\n"
                            f"Factory's verbatim message: "
                            f"\"{problem_h.get('detail') or '(empty)'}\".\n"
                            + (f"Factory requestId: {problem_h.get('request_id')}\n\n"
                               if problem_h.get('request_id') else "\n")
                            + diag_line
                            + (f"LAMA cleaned up {cleaned} stale session(s) — "
                               "click 'Create / refresh workspace' again to retry.\n"
                               if cleaned else "")
                            + (
                                "This means the VM is alive but the droid "
                                "agent process is unreachable. Open the droid "
                                "in https://app.factory.ai (your computer → "
                                "click to attach), or reboot the droid from "
                                "Factory → Settings → Droid Computers.\n"
                                if sub_reason_h == "droid_agent_unreachable"
                                else
                                "Start / Resume the droid in Factory → "
                                "Settings → Droid Computers, wait 60–180 s, "
                                "then retry.\n"
                            )
                            + f"\n(raw: {session_probe['raw']})"
                        ),
                    }
                # Probe succeeded — clean up the throwaway session if
                # Factory gave us one (best-effort, fire-and-forget).
                try:
                    _probe_sid = (_probe.json() or {}).get("sessionId", "")
                    if _probe_sid:
                        await client_h.delete(
                            f"{FACTORY_API_BASE}/sessions/{_probe_sid}",
                            headers=headers_h,
                        )
                except Exception:
                    pass
                return {
                    "ok": True,
                    "reason": "host_anchored",
                    "detail": (
                        "Host-anchored mode is ON — workspace stays on the "
                        "LAMA host. The droid's HOME folder will be used as "
                        "the default cwd for sessions. No on-droid mkdir is "
                        f"performed; all inputs are pushed inline. "
                        f"Factory reachable={'yes' if woken else 'partial'}; "
                        f"session_probe=ok; computer_id={cid_h}."
                    ),
                    "computer_id": cid_h,
                    "cwd": "",
                    "host_anchored": True,
                    "droid_woken": bool(woken),
                    "session_probe_http": session_probe["http"],
                }
        return {"ok": False, "reason": "no_target_cwd"}
    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, verify=_http_verify()) as client:
        try:
            cid = await _resolve_computer_id(client, headers, computer_ref)
        except Exception as e:
            return {"ok": False, "reason": "resolve_computer_failed", "error": str(e)[:300], "cwd": target_cwd}
        # iter-13.91.4 — explicit operator-triggered workspace creation
        # always forces a fresh mkdir (skips the per-process cache) so
        # the user sees real Droid filesystem state, not a stale cache hit.
        await _wake_computer(client, headers, cid, force=True)
        try:
            result = await _ensure_workspace_ready_cached(
                client, headers, cid, target_cwd, force=True, poll_seconds=60.0,
            )
        except Exception as e:
            return {
                "ok": False,
                "reason": "bootstrap_exception",
                "error": str(e)[:300],
                "cwd": target_cwd,
                "computer_id": cid,
            }
        # iter-13.91.9 — read-only filesystem auto-fallback.
        # If the requested cwd is on a read-only mount AND we learned
        # the Droid's $HOME during the attempt, retry once at
        # `<HOME>/lama_<tenant>_<project>` (a writable, HOME-rooted,
        # single-level dir → also visible in Factory's left sidebar).
        # Persist the new cwd to the project config so future agent
        # calls go straight to the writable path.
        relocated = False
        relocated_from = ""
        if (
            not result.get("ok")
            and result.get("reason") == "readonly_fs"
            and _DROID_HOME_CACHE.get(cid)
        ):
            droid_home = _DROID_HOME_CACHE[cid].rstrip("/")
            t = _slug_key(tenant_id)
            p = _slug_key(project_id)
            fallback_cwd = f"{droid_home}/lama_{t}_{p}"
            # Only retry if fallback path is actually different.
            if fallback_cwd != target_cwd:
                try:
                    retry = await _ensure_workspace_ready_cached(
                        client, headers, cid, fallback_cwd, force=True, poll_seconds=60.0,
                    )
                except Exception as e:
                    retry = {"ok": False, "reason": "bootstrap_exception", "detail": str(e)[:200]}
                if retry.get("ok"):
                    relocated = True
                    relocated_from = target_cwd
                    target_cwd = fallback_cwd
                    result = retry
                    # Persist so subsequent agent calls + the Console
                    # display the actually-writable path.
                    try:
                        await projects.update_one(
                            {"id": project_id},
                            {"$set": {
                                "settings.factory_orchestrator.cwd": fallback_cwd,
                                "settings.factory_orchestrator.updated_at": _now_iso(),
                            }},
                        )
                    except Exception:
                        pass
    # iter-13.91.5 — surface structured reason / detail.
    out = {
        "ok": bool(result.get("ok")),
        "reason": result.get("reason") or "",
        "error": "" if result.get("ok") else (result.get("detail") or ""),
        "detail": result.get("detail") or "",
        "cwd": target_cwd,
        "computer_id": cid,
        "droid_home": _DROID_HOME_CACHE.get(cid, ""),
    }
    if relocated:
        out["relocated"] = True
        out["relocated_from"] = relocated_from
    return out


async def wake_project_droid(project_id: str, timeout: float = 30.0) -> Dict[str, Any]:
    """iter-13.91.4 — operator-triggered "wake my Droid now" helper.

    Resolves the project's Computer ID and issues an explicit GET on
    `/computers/{id}` (Factory's auto-resume trigger) with the throttle
    bypassed. Returns the wake outcome plus the Computer ID so the
    Console can display it as confirmation.
    """
    cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    if not cfg.get("enabled"):
        return {"ok": False, "reason": "factory_not_enabled"}
    app_key = (cfg.get("app_key") or "").strip()
    computer_ref = (cfg.get("computer_id") or "").strip()
    if not app_key or not computer_ref:
        return {"ok": False, "reason": "factory_incomplete_config"}
    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, verify=_http_verify()) as client:
        try:
            cid = await _resolve_computer_id(client, headers, computer_ref)
        except Exception as e:
            return {"ok": False, "reason": "resolve_computer_failed", "error": str(e)[:300]}

        # iter-13.91.x — do the wake GET inline so we can surface the real
        # HTTP status / body / exception to the Console. Previously this
        # delegated to `_wake_computer()` which returns a plain bool, so
        # any non-2xx surfaced to the UI as a useless "Wake failed ·
        # Unknown error". We still invoke the proactive start probe for
        # sleeping computers, identical to `_wake_computer(..., force=True)`.
        url = f"{FACTORY_API_BASE}/computers/{quote(cid, safe='')}"
        body: Dict[str, Any] = {}
        status_code: Optional[int] = None
        ok = False
        try:
            resp = await client.get(url, headers=headers)
            status_code = resp.status_code
            _LAST_WAKE_AT[cid] = time.time()
            ok = status_code in (200, 202, 204)
            if status_code == 200:
                try:
                    body = resp.json() or {}
                except Exception:
                    body = {}
            if not ok:
                # Surface HTTP error to the operator.
                snippet = ""
                try:
                    snippet = (resp.text or "")[:300]
                except Exception:
                    snippet = ""
                return {
                    "ok": False,
                    "reason": f"http_{status_code}",
                    "error": snippet or f"GET {url} returned HTTP {status_code}",
                    "computer_id": cid,
                    "computer_ref": computer_ref,
                    "http_status": status_code,
                }
        except Exception as e:
            _LAST_WAKE_AT[cid] = time.time()
            return {
                "ok": False,
                "reason": "wake_request_failed",
                "error": f"{type(e).__name__}: {str(e)[:280]}",
                "computer_id": cid,
                "computer_ref": computer_ref,
            }

        status = str(body.get("status") or body.get("state") or "").lower()
        _SLEEPING_STATES = {
            "paused", "stopped", "hibernating", "suspended",
            "terminated", "off", "inactive", "asleep",
        }
        if status in _SLEEPING_STATES:
            await _try_start_computer(client, headers, cid)

    return {
        "ok": bool(ok),
        "computer_id": cid,
        "computer_ref": computer_ref,
        "computer_status": status if body else "",
        "http_status": status_code,
    }



async def delete_project_factory_orchestrator_config(project_id: str) -> Dict[str, Any]:
    """Fully wipe the Factory orchestrator config for a project (iter-13.35).

    Removes the entire `settings.factory_orchestrator` document — app key,
    computer id, cwd, all cached session ids, and the enabled flag. After
    this call `route_via_factory_orchestrator()` returns None for any LLM
    call on this project and the system falls back to standard fabric
    routing immediately (no need to also toggle "enabled" off).
    """
    doc = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if doc is None:
        raise RuntimeError("Project not found")
    now = _now_iso()
    await projects.update_one(
        {"id": project_id},
        {
            "$unset": {"settings.factory_orchestrator": ""},
            "$set": {"updated_at": now},
        },
    )
    # iter-13.91.4 — drop any cached workspace-ready entries for this
    # project's computer. We can't filter by project here (the doc is
    # already gone), so we wipe everything; the next agent call rebuilds
    # the cache lazily. Safe — the cache is process-local and cheap.
    _invalidate_workspace_cache()
    _COMPUTER_ID_CACHE.clear()  # iter-13.91.7 — same rationale
    return {"ok": True, "deleted": True, "updated_at": now}


