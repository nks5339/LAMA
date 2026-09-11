"""iter-14.9 — Droid workspace materializer.

Operator directive:

    "context will be passed to droid at the time of knowledge building.
    after that same droid should use.  … token taken too much that is
    also a blocker here"

What this module does
---------------------
At Build KB time we drop a compact set of context files onto the
per-project droid workspace at

    /tmp/lama-factory-cli/{project_id}/.lama/

These are the SAME facts we used to embed in every LLM prompt (TOON,
file index, tech stack, analysis digest). By living on the workspace
filesystem, droid can `cat` them itself and the LAMA-side prompt drops
from ~300K chars to a couple of hundred bytes — that's the token-spend
cut the operator asked for.

Files written
-------------
* `.lama/README.md`         Instructions droid should read first.
* `.lama/kb_summary.md`     Tech stack, file counts, high-level project shape.
* `.lama/file_index.md`     Every source file the KB knows about.
* `.lama/kb_toon.txt`       The raw TOON string (compact class/table/route index).
* `.lama/analysis_digest.md` Deep-legacy analysis digest (when available).

Idempotency
-----------
Every call OVERWRITES existing files. Build KB is the only expected
caller so stale content on re-build is a bug not a feature.

Callers
-------
* `backend/routes/kb.py::_build_kb_impl` — end-of-build hook.
* Any future stage that wants to "refresh" the workspace can call
  `materialize_kb_context(project_id)` directly.

Design notes
------------
* Kept dependency-free (uses only stdlib + our own `db` / `kb.legacy_analyzer`
  imports) so importing during Build KB doesn't pull in heavy modules.
* Failure is BEST-EFFORT and logged, never fatal. If we can't write to
  /tmp for some reason the SRS pipeline still works (it just falls back
  to the fat-prompt path).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("lama.kb.droid_workspace")

# Where factory_cli.py points droid's --cwd. Keep the path derivation
# identical to `route_via_factory_cli` in fabric/factory_cli.py so the
# workspace we populate here is the same one droid actually reads.
_WORKSPACE_ROOT = os.environ.get("LAMA_FACTORY_CLI_CWD", "").strip() or "/tmp/lama-factory-cli"


def workspace_path(project_id: str) -> str:
    """Return the droid `--cwd` directory for a given project."""
    override = os.environ.get("LAMA_FACTORY_CLI_CWD", "").strip()
    base = override or _WORKSPACE_ROOT
    return os.path.join(base, project_id)


def _readme_body() -> str:
    return (
        "# LAMA Context for Factory Droid\n\n"
        "This directory is the LAMA project workspace. Files under `.lama/`\n"
        "are the pre-computed context bundle produced at KB-build time.\n"
        "Read them **before** answering the current prompt:\n\n"
        "1. `.lama/kb_summary.md`     — project tech stack + high-level shape.\n"
        "2. `.lama/file_index.md`     — every source file LAMA knows about;\n"
        "   cite only paths that appear verbatim in this index.\n"
        "3. `.lama/kb_toon.txt`       — compact class / table / route index\n"
        "   in TOON format (LAMA's LLM-friendly serialisation).\n"
        "4. `.lama/analysis_digest.md` — deep legacy behaviour digest\n"
        "   (workflows / calculations / integrations). Present only when the\n"
        "   pre-SRS analyzer produced a successful pass.\n\n"
        "Ground every citation in these files. Do NOT invent paths, class\n"
        "names, tables, or workflows from your training data — the LAMA\n"
        "codebase is single-tenant and any off-index reference is a\n"
        "cross-project contamination bug.\n"
    )


def _summary_body(proj: Dict[str, Any], stats: Dict[str, Any]) -> str:
    name = proj.get("name") or proj.get("id") or "(unnamed project)"
    tech = proj.get("detected_tech") or {}
    lines: List[str] = [
        f"# {name}",
        "",
        f"Project ID: `{proj.get('id', '')}`",
        "",
        "## Detected Stack",
    ]
    if tech:
        for k, v in sorted(tech.items()):
            if not v:
                continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            lines.append(f"- **{k}**: {v}")
    else:
        lines.append("- (not detected)")
    lines.append("")
    lines.append("## KB Statistics")
    for k in ("files", "entities", "chunks", "tables", "classes", "routes"):
        if k in stats:
            lines.append(f"- {k}: {stats.get(k)}")
    lines.append("")
    lines.append("## Target Stack (migration destination)")
    tgt = proj.get("target_stack") or {}
    if tgt:
        for k, v in sorted(tgt.items()):
            lines.append(f"- **{k}**: {v}")
    else:
        lines.append("- (not configured)")
    return "\n".join(lines) + "\n"


def _index_body(files: List[Dict[str, Any]]) -> str:
    lines = ["# Legacy File Index", "",
             "Every source file the KB extracted. Cite ONLY paths from this list.\n",
             "| Filename | Size (bytes) | Type | Entities | Chunks |",
             "|---|---:|---|---:|---:|"]
    # Sort largest-first so top-of-index reflects what the LLM should pay
    # most attention to.
    for f in sorted(files, key=lambda x: int(x.get("size", 0) or 0), reverse=True):
        lines.append(
            f"| `{f.get('filename', '')}` | {f.get('size', 0)} "
            f"| {f.get('filetype', '')} | {f.get('entity_count', 0)} "
            f"| {f.get('chunk_count', 0)} |"
        )
    return "\n".join(lines) + "\n"


async def materialize_kb_context(
    project_id: str,
    toon: str = "",
    stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write the compact KB context bundle to the per-project droid workspace.

    Returns a metadata dict:
        {
          "ok": bool,
          "workspace": "/tmp/lama-factory-cli/<pid>",
          "files_written": ["kb_summary.md", ...],
          "bytes_total": int,
          "error": str (only when ok=False),
        }

    Best-effort: failures are logged and returned as `{"ok": False,
    "error": "..."}` — never raised. Callers should not block Build KB
    on a materialization failure; the SRS pipeline still works with the
    legacy fat-prompt path.
    """
    ws = workspace_path(project_id)
    dot_lama = os.path.join(ws, ".lama")
    files_written: List[str] = []
    total_bytes = 0

    try:
        os.makedirs(dot_lama, exist_ok=True)

        # Deferred imports so this module can be imported at startup
        # without dragging Motor into every process (mirrors the pattern
        # used elsewhere in backend/kb/).
        from db import projects, kb_files, kb_toon, legacy_analysis as _la
        from kb.legacy_analyzer import build_analysis_digest

        proj = await projects.find_one({"id": project_id}, {"_id": 0}) or {}
        files = await kb_files.find(
            {"project_id": project_id},
            {"_id": 0, "id": 1, "filename": 1, "size": 1,
             "filetype": 1, "chunk_count": 1, "entity_count": 1},
        ).to_list(None) or []

        # Fall back to persisted TOON when the caller didn't pass one
        # (e.g. a manual refresh from the Console).
        if not toon:
            toon_doc = await kb_toon.find_one(
                {"project_id": project_id}, {"_id": 0, "toon": 1, "stats": 1},
            ) or {}
            toon = toon_doc.get("toon", "") or ""
            stats = stats or toon_doc.get("stats") or {}
        stats = stats or {}
        stats.setdefault("files", len(files))

        # Deep-legacy digest — best-effort. When it's a failure marker
        # (iter-14.8) we skip the digest file entirely so droid doesn't
        # read a stale error blob as authoritative context.
        analysis_doc = await _la.find_one({"project_id": project_id}, {"_id": 0}) or {}
        analysis_digest = ""
        if analysis_doc and analysis_doc.get("status") != "failed":
            try:
                analysis_digest = build_analysis_digest(analysis_doc) or ""
            except Exception as exc:  # noqa: BLE001
                log.warning("Analysis digest build failed for %s: %s", project_id, exc)

        # ---- Write each file (best-effort, per-file try/except) ----
        def _write(name: str, body: str) -> None:
            nonlocal total_bytes
            path = os.path.join(dot_lama, name)
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                files_written.append(name)
                total_bytes += len(body.encode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Workspace materialize: %s failed: %s", path, exc)

        _write("README.md", _readme_body())
        _write("kb_summary.md", _summary_body(proj, stats))
        _write("file_index.md", _index_body(files))
        if toon:
            _write("kb_toon.txt", toon)
        if analysis_digest:
            _write("analysis_digest.md",
                   "# Deep Legacy Analysis Digest\n\n" + analysis_digest + "\n")

        log.info(
            "Droid workspace materialized for %s at %s (%d file(s), %d bytes)",
            project_id, ws, len(files_written), total_bytes,
        )
        return {
            "ok": True,
            "workspace": ws,
            "files_written": files_written,
            "bytes_total": total_bytes,
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("Droid workspace materialize failed for %s", project_id)
        return {"ok": False, "workspace": ws, "error": str(exc)[:400]}
