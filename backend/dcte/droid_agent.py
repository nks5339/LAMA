"""DCTE Autonomous Droid Agent — iter-19.

Runs Factory.ai's ``droid exec`` in *agentic* mode against a service's
destination tree so droid does the whole migration + build-fix loop
itself, the way it would if a human ran ``droid exec --auto medium
--cwd <service>`` at a shell.

Contrast with :mod:`dcte.ai_refactor` (which shackles droid to a
one-file-per-batch JSON-contract chat completion):

- No JSON contract. Droid returns free-form; we don't parse it.
- ``--auto medium`` — droid may read siblings, run ``mvn`` / ``psql``,
  write files, iterate. It cannot rm -rf outside its ``--cwd``.
- One invocation per SERVICE, not per file.
- Long timeout (``LAMA_DCTE_DROID_AGENT_TIMEOUT_SEC``, default 1800s).
- Fallback: on any failure the caller reverts to the legacy chain
  (``ai_refactor`` + ``build_agent`` + ``devops_agent``).

The tree diff (files whose sha256 changed, plus new files) is snapshotted
pre/post so DCTE can still emit :class:`DcteTransformRecord` rows and
the report + rollback flow keeps working.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from .prompt_builder import BRIEF_REV, build_migration_brief

logger = logging.getLogger("lama.dcte.droid_agent")


# ── Agentic brief ─────────────────────────────────────────────────────
# iter-21 — BUILT per job from the selected (source, target) pair; see
# dcte/prompt_builder.py.
#
# This was a hardcoded Helidon -> Spring Boot essay, handed to droid on every
# job whatever the user picked. A JSP -> React run was told to "Convert this
# project from Helidon (MicroProfile / SE) to Spring Boot 3.x" and to purge
# @ApplicationScoped, so droid either followed a brief for a different job or
# ignored it. `BRIEF_REV` lives with the builder and is recorded on every run.


# Files we consider "owned" by DCTE and eligible for the diff snapshot.
# Everything else in --cwd is ignored (mvn output, .git, node_modules).
_TRACKED_SUFFIXES: tuple[str, ...] = (
    ".java", ".sql", ".xml", ".properties", ".yml", ".yaml",
    ".json", ".md", ".dockerfile", ".gradle", ".kts",
)
_IGNORE_DIR_NAMES: frozenset[str] = frozenset({
    "target", "build", ".git", ".idea", ".vscode", "node_modules",
    ".mvn", ".gradle", "out", "bin",
})


def _iter_tracked_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Cheap ancestor filter — skip anything under an ignored dir.
        if any(part in _IGNORE_DIR_NAMES for part in p.relative_to(root).parts[:-1]):
            continue
        if p.suffix.lower() in _TRACKED_SUFFIXES or p.name.lower() == "dockerfile":
            yield p


def _snapshot(root: Path) -> dict[str, str]:
    """Return {relative_path: sha256} for every tracked file under ``root``."""
    out: dict[str, str] = {}
    for p in _iter_tracked_files(root):
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:
            continue
        out[str(p.relative_to(root))] = h
    return out


def _diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    added   = sorted(k for k in after  if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = sorted(k for k in after if k in before and before[k] != after[k])
    return {"added": added, "removed": removed, "changed": changed}


def _resolve_timeout() -> int:
    raw = os.environ.get("LAMA_DCTE_DROID_AGENT_TIMEOUT_SEC", "1800")
    try:
        return max(60, int(raw))
    except ValueError:
        return 1800


def _resolve_auto_level() -> str:
    lvl = (os.environ.get("LAMA_DCTE_DROID_AGENT_AUTO") or "medium").strip().lower()
    return lvl if lvl in {"low", "medium", "high"} else "medium"


async def run_droid_agent(
    dest_root: Path,
    service_id: str,
    model: str | None,
    source_stack: str = "",
    target_stack: str = "",
    *,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    """Invoke droid in agentic mode against ``dest_root``.

    Returns a dict shaped::

        {
          "success":       bool,
          "elapsed_ms":    int,
          "auto_level":    str,
          "timeout_s":     int,
          "brief_rev":     int,
          "added_files":   [rel, ...],
          "changed_files": [rel, ...],
          "removed_files": [rel, ...],
          "log_tail":      str,   # last ~2000 chars of droid stdout+stderr
          "error":         str,   # only when success=False
        }

    Never raises — every failure surfaces as ``success=False`` with an
    ``error`` string, so the caller can cleanly fall back to the legacy
    per-file chain without a stack trace.
    """
    t0 = time.time()
    result: dict[str, Any] = {
        "success":       False,
        "elapsed_ms":    0,
        "auto_level":    _resolve_auto_level(),
        "timeout_s":     _resolve_timeout(),
        "brief_rev":     BRIEF_REV,
        "added_files":   [],
        "changed_files": [],
        "removed_files": [],
        "log_tail":      "",
        "error":         "",
    }

    dest = Path(dest_root)
    if not dest.exists() or not dest.is_dir():
        result["error"] = f"destination path not found: {dest}"
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result

    # Import lazily so a missing factory_cli module doesn't kill DCTE at
    # import time (e.g. during unit tests that don't touch droid).
    try:
        from fabric.factory_cli import _run_droid_exec, cli_binary_available
    except Exception as e:
        result["error"] = f"factory_cli unavailable: {e}"
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result

    if not cli_binary_available():
        result["error"] = "droid CLI binary not resolvable on PATH / LAMA_FACTORY_CLI_BIN"
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result

    before = _snapshot(dest)
    prompt = prompt_override or build_migration_brief(source_stack, target_stack)
    session_tag = f"lama-dcte-agent-{service_id}"

    try:
        parsed = await _run_droid_exec(
            prompt=prompt,
            cwd=str(dest),
            model=(model or None),
            auto_level=result["auto_level"],
            timeout=float(result["timeout_s"]),
            session_tag=session_tag,
        )
    except TimeoutError as e:  # asyncio.TimeoutError is a subclass in 3.11
        result["error"] = f"droid agent timed out after {result['timeout_s']}s"
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        logger.warning("droid_agent timeout for svc=%s: %s", service_id, e)
        _fill_diff(result, before, dest)
        return result
    except Exception as e:  # noqa: BLE001 — we want to swallow to fall back
        result["error"] = f"droid agent failed: {e}"
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        logger.warning("droid_agent error for svc=%s: %s", service_id, e)
        _fill_diff(result, before, dest)
        return result

    # Success path — capture log tail from the response envelope. The
    # exact shape of `parsed` depends on droid version; be defensive.
    tail_parts: list[str] = []
    for k in ("text", "content", "output", "stdout"):
        v = parsed.get(k) if isinstance(parsed, dict) else None
        if isinstance(v, str) and v:
            tail_parts.append(v)
    stderr_txt = parsed.get("_stderr") if isinstance(parsed, dict) else None
    if isinstance(stderr_txt, str) and stderr_txt:
        tail_parts.append("[stderr] " + stderr_txt)
    log_tail = "\n".join(tail_parts).strip()
    result["log_tail"] = log_tail[-2000:] if log_tail else ""

    _fill_diff(result, before, dest)

    # Success heuristics — droid returned cleanly AND either:
    #   * emitted the DROID_AGENT_OK sentinel, or
    #   * made at least one on-disk change (no sentinel required if the
    #     migration was a no-op, which shouldn't happen but shouldn't
    #     fail either).
    agent_ok = "DROID_AGENT_OK" in log_tail
    agent_incomplete = "DROID_AGENT_INCOMPLETE" in log_tail
    made_changes = bool(
        result["added_files"] or result["changed_files"] or result["removed_files"]
    )
    if agent_incomplete:
        result["success"] = False
        # Try to extract the trailing error snippet after the sentinel.
        snippet = log_tail.rsplit("DROID_AGENT_INCOMPLETE", 1)[-1].strip(": \n")
        result["error"] = f"droid agent could not reach green build: {snippet[:400]}"
    elif agent_ok or made_changes:
        result["success"] = True
    else:
        result["success"] = False
        result["error"] = "droid agent produced no changes and did not emit DROID_AGENT_OK"

    result["elapsed_ms"] = int((time.time() - t0) * 1000)
    return result


def _fill_diff(result: dict[str, Any], before: dict[str, str], dest: Path) -> None:
    """Snapshot ``dest`` post-run and populate diff fields on ``result``."""
    try:
        after = _snapshot(dest)
        d = _diff(before, after)
        result["added_files"]   = d["added"]
        result["changed_files"] = d["changed"]
        result["removed_files"] = d["removed"]
    except Exception as e:  # noqa: BLE001
        logger.warning("droid_agent diff snapshot failed: %s", e)


__all__ = ["run_droid_agent"]
