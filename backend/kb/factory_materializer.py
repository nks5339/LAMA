"""iter-13.81.9 — Materialise legacy source files into a real folder on the
Factory.ai Droid Computer's filesystem.

THE PROBLEM
-----------
Factory.ai's Droid is an autonomous coding agent. It very strongly prefers
to discover code by running `ls` / `cat` / `find` on its own filesystem
rather than by reading inlined KB blocks in the prompt — no matter how
loudly the prompt asks it not to.

LAMA's per-project source tree lives in MongoDB (`kb_files` for metadata,
`kb_chunks` for text content), scoped by ``(tenant_id, project_id)``.
The Droid never had access to it, so it reported "coverage = 0",
"no source artifacts available", etc. The inlined KB worked for fabric
routes (which use prompt context directly) but not for Factory.

THIS MODULE
-----------
Builds a real source tree on the Droid under
``<workspace_root>/legacy-source/`` and persists the path back into
LAMA Mongo so it survives restarts and is visible to subsequent runs.

Mechanism (no Factory file-upload API exists today):
  1. Pull every ``kb_files`` row + joined ``kb_chunks`` content for the
     project (tenant-scoped query — never crosses tenants).
  2. Build a single ``tar.gz`` in memory of the whole tree.
  3. Base64-encode it.
  4. Open a transient Factory bootstrap session and stream the base64
     in ~30 KB chunks via the session's shell, appending each chunk to
     ``<workspace>/.lama-bootstrap.b64`` on the Droid.
  5. Final shell command: ``base64 -d < .lama-bootstrap.b64 | tar -xz -C
     legacy-source/`` then ``rm .lama-bootstrap.b64``.
  6. Persist ``{path, file_count, bytes, tarball_sha, materialized_at}``
     into the ``factory_workspaces`` collection so every future
     route_via_factory_orchestrator call can surface the path in the
     prompt's INLINE-KB GROUNDING block.

Idempotent: if the persisted ``tarball_sha`` matches the current KB
hash, the upload is skipped (returns ``{"ok": True, "skipped": True}``).

Size envelope: tested up to ~5 MB tar.gz (≈7 MB base64 → ~230 messages
of 30 KB each, ≈3-5 minutes end-to-end). Beyond that the Droid's shell
session timeout becomes the bottleneck — the caller should chunk
multiple ``materialize_project_workspace`` calls or raise the per-message
poll budget.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import tarfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger("lama.factory.materializer")


# ── tunables ────────────────────────────────────────────────────────────
B64_CHUNK_CHARS = int(os.environ.get("LAMA_FACTORY_MATERIALIZE_CHUNK", "28000") or "28000")
PER_CHUNK_TIMEOUT = float(os.environ.get("LAMA_FACTORY_MATERIALIZE_TIMEOUT", "60") or "60")
MAX_TARBALL_BYTES = int(os.environ.get("LAMA_FACTORY_MATERIALIZE_MAX_BYTES", str(20 * 1024 * 1024)) or str(20 * 1024 * 1024))


# ── filename sanitisation ───────────────────────────────────────────────
_UNSAFE_PATH_RE = re.compile(r"\.\.+|^/+|^[a-zA-Z]:[\\/]+|\x00")


def _safe_rel_path(name: str) -> str:
    """Strip drive letters, absolute-path leaders and ``..`` traversal so a
    malicious ``kb_files.filename`` can never escape ``legacy-source/``."""
    n = (name or "").replace("\\", "/").lstrip("/").strip()
    n = _UNSAFE_PATH_RE.sub("", n)
    parts = [p for p in n.split("/") if p and p not in (".", "..")]
    return "/".join(parts) or f"unnamed-{int(time.time())}.txt"


async def _load_project_source(project_id: str) -> Tuple[List[Tuple[str, bytes]], str]:
    """Return ``[(rel_path, bytes), ...]`` for every file in the project's
    KB (tenant-scoped) plus a SHA-256 hash of the joined manifest so
    callers can detect "nothing changed since last materialise".
    """
    from db import kb_files as _kbf, kb_chunks as _kbc, projects as _proj_col

    proj = await _proj_col.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1})
    if not proj:
        raise RuntimeError(f"project '{project_id}' not found")
    tenant_id = (proj.get("tenant_id") or "").strip() or "tenant_default"

    cursor = _kbf.find(
        {"project_id": project_id, "tenant_id": tenant_id},
        {"_id": 0, "id": 1, "filename": 1, "size": 1, "filetype": 1},
    )
    files: List[Dict[str, Any]] = await cursor.to_list(length=None)
    out: List[Tuple[str, bytes]] = []
    h = hashlib.sha256()
    for f in files:
        fid = f.get("id")
        if not fid:
            continue
        # Load chunks in order, join their `content`.
        chunks = await _kbc.find(
            {"project_id": project_id, "file_id": fid},
            {"_id": 0, "chunk_index": 1, "content": 1},
        ).sort("chunk_index", 1).to_list(length=None)
        text = "".join((c.get("content") or "") for c in chunks)
        if not text:
            # `kb_files` row with no chunks — skip rather than create empty file.
            continue
        rel = _safe_rel_path(f.get("filename") or fid)
        data = text.encode("utf-8", errors="replace")
        out.append((rel, data))
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(data)
        h.update(b"\x00")
    return out, h.hexdigest()


def _build_tarball(files: List[Tuple[str, bytes]]) -> bytes:
    """Pack ``[(rel, bytes), ...]`` into a gzipped tar payload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files:
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── Factory session helpers (thin wrappers over factory_orchestrator) ───
async def _bootstrap_session(client: httpx.AsyncClient, headers: Dict[str, str],
                             computer_id: str) -> str:
    resp = await client.post(
        f"{_factory_api_base()}/sessions",
        headers=headers,
        json={"computerId": computer_id},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Factory bootstrap session creation failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    sid = (resp.json() or {}).get("sessionId", "") or ""
    if not sid:
        raise RuntimeError("Factory bootstrap session: sessionId missing")
    return sid


async def _post_and_wait(client: httpx.AsyncClient, headers: Dict[str, str],
                         session_id: str, computer_id: str, shell_cmd: str,
                         ack_marker: str, poll_seconds: float = PER_CHUNK_TIMEOUT) -> str:
    """POST a shell-only message and wait until the Droid's reply contains
    ``ack_marker``. Returns the full assistant reply text."""
    msg = (
        "LAMA materialisation — run EXACTLY this single shell command and "
        "reply with only its raw output. Do not chat, do not run anything "
        "else:\n\n  " + shell_cmd + "\n"
    )
    resp = await client.post(
        f"{_factory_api_base()}/sessions/{session_id}/messages",
        headers=headers,
        json={"text": msg, "computerId": computer_id},
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Factory message POST failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    deadline = time.time() + max(5.0, poll_seconds)
    last_id = ""
    while time.time() < deadline:
        try:
            from factory_orchestrator import _latest_assistant_message
            last = await _latest_assistant_message(client, headers, session_id)
        except Exception:
            last = None
        if last and last.get("id") != last_id:
            text = _extract_text(last) or ""
            if ack_marker in text:
                return text
            last_id = last.get("id", "")
        await asyncio.sleep(1.0)
    raise RuntimeError(
        f"Factory shell ack timeout waiting for marker '{ack_marker}' "
        f"after {poll_seconds:.0f}s"
    )


def _extract_text(msg: Dict[str, Any]) -> str:
    content = (msg or {}).get("content") or []
    if isinstance(content, str):
        return content.strip()
    parts: List[str] = []
    for c in content:
        if isinstance(c, dict):
            for k in ("text", "content", "value"):
                v = c.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
    return "\n".join(parts).strip()


def _factory_api_base() -> str:
    from factory_orchestrator import FACTORY_API_BASE
    return FACTORY_API_BASE


# ── public entry point ─────────────────────────────────────────────────
async def materialize_project_workspace(
    project_id: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Materialise the project's legacy source files under
    ``<workspace>/legacy-source/`` on the Droid Computer.

    Args:
        project_id: LAMA project id.
        force:      If True, re-upload even when the persisted SHA matches.

    Returns:
        ``{"ok": bool, "path": str, "file_count": int, "bytes": int,
           "sha": str, "skipped": bool, "reason": str}``
    """
    from db import projects as _proj_col
    from factory_orchestrator import (
        get_project_factory_orchestrator_config,
        _resolve_computer_id,
        _resolve_cwd,
        _ensure_workspace_exists,
        _http_verify,
    )

    proj = await _proj_col.find_one({"id": project_id}, {"_id": 0, "id": 1, "tenant_id": 1, "name": 1})
    if not proj:
        return {"ok": False, "reason": "project_not_found"}
    tenant_id = (proj.get("tenant_id") or "").strip() or "tenant_default"

    cfg = await get_project_factory_orchestrator_config(project_id, include_secret=True)
    if not cfg.get("enabled"):
        return {"ok": False, "reason": "factory_disabled"}
    app_key = (cfg.get("app_key") or "").strip()
    computer_ref = (cfg.get("computer_id") or "").strip()
    if not app_key:
        return {"ok": False, "reason": "missing_app_key"}
    if not computer_ref:
        return {"ok": False, "reason": "missing_computer_id"}

    # 1. Snapshot the project's KB → tarball + sha.
    files, sha = await _load_project_source(project_id)
    if not files:
        return {"ok": False, "reason": "kb_empty", "file_count": 0}
    tar_bytes = _build_tarball(files)
    if len(tar_bytes) > MAX_TARBALL_BYTES:
        return {
            "ok": False,
            "reason": "tarball_too_large",
            "bytes": len(tar_bytes),
            "max_bytes": MAX_TARBALL_BYTES,
            "detail": (
                "Tarball exceeded LAMA_FACTORY_MATERIALIZE_MAX_BYTES. Raise "
                "the env var or trim kb_files with module-selection."
            ),
        }
    b64 = base64.b64encode(tar_bytes).decode("ascii")

    # 2. Idempotency probe.
    state = await _get_state(project_id)
    if not force and state and state.get("sha") == sha and state.get("path"):
        return {
            "ok": True,
            "skipped": True,
            "reason": "unchanged",
            "path": state["path"],
            "file_count": state.get("file_count", len(files)),
            "bytes": state.get("bytes", len(tar_bytes)),
            "sha": sha,
        }

    # 3. Open a Factory bootstrap session + ensure workspace exists.
    headers = {"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120.0, verify=_http_verify()) as client:
        computer_id = await _resolve_computer_id(client, headers, computer_ref)
        resolved_cwd = _resolve_cwd(cfg, tenant_id=tenant_id, project_id=project_id)
        if not resolved_cwd:
            return {"ok": False, "reason": "no_cwd"}
        ws = await _ensure_workspace_exists(client, headers, computer_id, resolved_cwd)
        if not ws.get("ok"):
            return {
                "ok": False, "reason": "workspace_bootstrap_failed",
                "detail": ws.get("detail", ""), "ws_reason": ws.get("reason", ""),
            }

        sid = await _bootstrap_session(client, headers, computer_id)
        try:
            # 4. Reset target dir + staging file.
            safe_cwd = resolved_cwd.replace("'", "'\"'\"'")
            await _post_and_wait(
                client, headers, sid, computer_id,
                shell_cmd=(
                    f"cd '{safe_cwd}' && rm -rf legacy-source .lama-bootstrap.b64 && "
                    f"mkdir -p legacy-source && echo LAMA_MAT_INIT_OK:$?"
                ),
                ack_marker="LAMA_MAT_INIT_OK:0",
            )

            # 5. Stream base64 in chunks (append-only) for resumability.
            total_chunks = (len(b64) + B64_CHUNK_CHARS - 1) // B64_CHUNK_CHARS
            for i in range(total_chunks):
                chunk = b64[i * B64_CHUNK_CHARS: (i + 1) * B64_CHUNK_CHARS]
                # printf %s preserves the b64 verbatim and avoids shell expansion.
                # We use a here-doc with no expansion (<<'EOF') for safety.
                ack = f"LAMA_MAT_CHUNK_{i+1}_OK"
                shell_cmd = (
                    f"cd '{safe_cwd}' && cat >> .lama-bootstrap.b64 <<'LAMA_EOF_{i}'\n"
                    f"{chunk}\n"
                    f"LAMA_EOF_{i}\n"
                    f"echo {ack}:$?"
                )
                await _post_and_wait(
                    client, headers, sid, computer_id,
                    shell_cmd=shell_cmd,
                    ack_marker=f"{ack}:0",
                )
                if (i + 1) % 10 == 0:
                    logger.info(
                        "materialize[%s]: streamed %d/%d chunks",
                        project_id, i + 1, total_chunks,
                    )

            # 6. Decode + extract + clean up.
            await _post_and_wait(
                client, headers, sid, computer_id,
                shell_cmd=(
                    f"cd '{safe_cwd}' && base64 -d < .lama-bootstrap.b64 "
                    f"| tar -xz -C legacy-source && rm -f .lama-bootstrap.b64 && "
                    f"file_count=$(find legacy-source -type f | wc -l) && "
                    f"echo LAMA_MAT_EXTRACT_OK:$? files=$file_count"
                ),
                ack_marker="LAMA_MAT_EXTRACT_OK:0",
                poll_seconds=max(PER_CHUNK_TIMEOUT, 120.0),
            )
        finally:
            # Best-effort: abandon the bootstrap session.
            try:
                await client.delete(
                    f"{_factory_api_base()}/sessions/{sid}",
                    headers=headers,
                )
            except Exception:
                pass

    materialized_path = f"{resolved_cwd.rstrip('/')}/legacy-source"
    await _save_state(project_id, {
        "project_id": project_id,
        "tenant_id": tenant_id,
        "computer_id": computer_id,
        "cwd": resolved_cwd,
        "path": materialized_path,
        "file_count": len(files),
        "bytes": len(tar_bytes),
        "sha": sha,
        "materialized_at": _now_iso(),
    })

    return {
        "ok": True,
        "skipped": False,
        "path": materialized_path,
        "file_count": len(files),
        "bytes": len(tar_bytes),
        "sha": sha,
    }


# ── state collection helpers ───────────────────────────────────────────
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _get_state(project_id: str) -> Optional[Dict[str, Any]]:
    from db import db
    try:
        return await db.factory_workspaces.find_one(
            {"project_id": project_id}, {"_id": 0},
        )
    except Exception:
        return None


async def _save_state(project_id: str, payload: Dict[str, Any]) -> None:
    from db import db
    try:
        await db.factory_workspaces.update_one(
            {"project_id": project_id},
            {"$set": payload},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("materialize_state save failed for pid=%s: %s", project_id, exc)


async def get_materialized_state(project_id: str) -> Optional[Dict[str, Any]]:
    """Public read accessor used by the prompt-injection layer to surface
    the materialised path (when present) inside the INLINE-KB GROUNDING
    directive.
    """
    if not project_id:
        return None
    return await _get_state(project_id)

