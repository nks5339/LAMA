"""iter-13.110 — Export the generated CodeGen tree to a real folder on
disk OUTSIDE the LAMA repository.

Folder layout (iter-13.111 — per-project, NO fixed wrapper):

    <LAMA_EXPORT_ROOT>/                      ← default: parent of `lama-main`
    └── <Project-Slug>/                      ← derived from project.name
        ├── frontend/                        ← every file whose Mongo path
        │   ├── package.json                   started with ``frontend/``
        │   └── src/...
        └── backend/                         ← everything else (service code,
            ├── app/                           docker, CI, README, …)
            ├── Dockerfile
            └── README.md

Examples — project named "Aarogyasri TJHS App" → folder
``Aarogyasri-TJHS-App``; project named "PMIS eSCT app" → ``PMIS-eSCT-app``.

The destination root is resolved in this order:
  1. ``LAMA_EXPORT_ROOT`` env-var (absolute path) — takes precedence.
  2. ``Path(this_file).resolve().parents[3]`` — i.e. the directory that
     CONTAINS ``lama-main``. The project folder then sits as a sibling
     of the LAMA repo, exactly as the operator asked for.

Safety:
  • path-traversal guard — any ``file_path`` containing ``..`` or an
    absolute prefix is REFUSED (no silent overwrite of /etc/hosts).
  • per-project subfolder is wiped + recreated on every export so stale
    files from a previous run don't survive (matches the GitHub-push
    semantics where the repo is the source of truth, not the disk).
  • the resolver REFUSES to land inside the lama-main repo itself, so a
    misconfigured ``LAMA_EXPORT_ROOT`` can never overwrite LAMA's own
    source tree.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


# Path-to-this-file → ancestor map (computed once).
_THIS_FILE = Path(__file__).resolve()
# parents[0]=codegen, [1]=backend, [2]=lama-main, [3]=parent dir
_LAMA_REPO_ROOT = _THIS_FILE.parents[2]
_DEFAULT_EXPORT_PARENT = _THIS_FILE.parents[3]


def _project_slug(name: str) -> str:
    """Slug a project name for safe use as a folder name.

    iter-13.111 — CASE-PRESERVING (was lowercasing). The operator wants
    folders like ``Aarogyasri-TJHS-App`` / ``PMIS-eSCT-app``, so we keep
    whatever casing the project name carries and only normalise the
    separators + drop filesystem-hostile characters.
    """
    s = (name or "lama-project").strip()
    out: List[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
            prev_dash = (ch in ("-", "_"))
        elif ch.isspace() or ch in (".", "/", "\\", ":", ";", ",", "(", ")", "[", "]", "{", "}"):
            if not prev_dash:
                out.append("-")
                prev_dash = True
        # everything else → drop
    slug = "".join(out).strip("-_") or "lama-project"
    return slug[:80]  # filesystem-friendly cap


def resolve_export_root() -> Path:
    """Return the absolute PARENT directory under which every project's
    on-disk export lives. Always created if missing.

    Order:
      1. ``LAMA_EXPORT_ROOT`` env-var (must be absolute when set).
      2. ``<parent-of-lama-main>`` — exports become siblings of the repo.

    Refuses to resolve to anywhere inside ``lama-main`` itself.
    """
    env = (os.environ.get("LAMA_EXPORT_ROOT") or "").strip()
    if env:
        root = Path(env).expanduser().resolve()
    else:
        root = _DEFAULT_EXPORT_PARENT.resolve()
    # Hard safety: never inside lama-main.
    try:
        root.relative_to(_LAMA_REPO_ROOT)
        raise RuntimeError(
            f"LAMA_EXPORT_ROOT ({root}) points INTO the LAMA repo "
            f"({_LAMA_REPO_ROOT}). Pick a path outside the repo so "
            f"exports cannot overwrite LAMA's own source."
        )
    except ValueError:
        pass  # not a subpath → good
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_export_path(project_name: str) -> Path:
    """Where the next export for ``project_name`` will land. Used by the
    UI's pre-export confirmation dialog."""
    return (resolve_export_root() / _project_slug(project_name)).resolve()
# ...existing code...


def _safe_join(base: Path, rel: str) -> Path:
    """Join ``base / rel`` and ensure the result stays inside ``base``.
    Raises ValueError on any traversal attempt or absolute path."""
    if not rel:
        raise ValueError("empty file_path")
    rel_norm = rel.replace("\\", "/").lstrip("/")
    if ".." in rel_norm.split("/"):
        raise ValueError(f"path traversal refused: {rel!r}")
    target = (base / rel_norm).resolve()
    base_resolved = base.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(
            f"refused path escaping export root: {rel!r}"
        ) from exc
    return target


def _split_files(files: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Partition into (frontend, backend) by file_path prefix.
    Files whose Mongo ``file_path`` starts with ``frontend/`` go to the
    frontend tree (with the prefix stripped). Everything else goes to
    the backend tree with its path preserved."""
    fe: List[Dict[str, Any]] = []
    be: List[Dict[str, Any]] = []
    for f in files or []:
        p = (f.get("file_path") or "").replace("\\", "/").lstrip("/")
        if not p:
            continue
        if p.startswith("frontend/"):
            new = dict(f)
            new["file_path"] = p[len("frontend/"):] or "README.md"
            fe.append(new)
        else:
            be.append(f)
    return fe, be


def export_project_to_disk(
    project_name: str,
    files: List[Dict[str, Any]],
    *,
    export_root: Path | None = None,
    wipe_existing: bool = True,
) -> Dict[str, Any]:
    """Write every generated file to ``<export_root>/<project-slug>/``.

    Returns a manifest dict (path, file counts, timestamps) that the
    HTTP layer turns into the JSON response.
    """
    root = (export_root or resolve_export_root()).resolve()
    slug = _project_slug(project_name)
    project_root = (root / slug).resolve()
    fe_root = project_root / "frontend"
    be_root = project_root / "backend"

    if wipe_existing and project_root.exists():
        shutil.rmtree(project_root)

    fe_root.mkdir(parents=True, exist_ok=True)
    be_root.mkdir(parents=True, exist_ok=True)

    fe_files, be_files = _split_files(files)

    written: List[str] = []
    skipped: List[Dict[str, str]] = []

    def _write_one(base: Path, file_doc: Dict[str, Any]) -> None:
        try:
            target = _safe_join(base, file_doc.get("file_path") or "")
        except ValueError as exc:
            skipped.append({"file_path": file_doc.get("file_path", ""), "reason": str(exc)})
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        content = file_doc.get("content") or ""
        # text mode with utf-8 keeps line endings native to the OS so the
        # exported project actually runs on the host
        try:
            target.write_text(content, encoding="utf-8")
            written.append(str(target))
        except Exception as exc:  # noqa: BLE001 — disk full / perms
            skipped.append({"file_path": file_doc.get("file_path", ""), "reason": f"{type(exc).__name__}: {exc}"})

    for f in fe_files:
        _write_one(fe_root, f)
    for f in be_files:
        _write_one(be_root, f)

    # tiny marker so the operator can see where this came from
    marker = project_root / ".lama-export.json"
    try:
        import json as _json
        marker.write_text(_json.dumps({
            "project_name": project_name,
            "project_slug": slug,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "frontend_files": len(fe_files),
            "backend_files":  len(be_files),
            "written":        len(written),
            "skipped":        len(skipped),
        }, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return {
        "export_root":    str(root),
        "project_root":   str(project_root),
        "frontend_root":  str(fe_root),
        "backend_root":   str(be_root),
        "frontend_files": len(fe_files),
        "backend_files":  len(be_files),
        "written":        len(written),
        "skipped":        skipped,
    }

