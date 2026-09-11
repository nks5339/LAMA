"""Knowledge Base endpoints: upload, build, status, scan-folder."""
import os
import re
import time   # iter-13.96 — phase heartbeat timing for chunk_dedup loop
import uuid
import asyncio  # iter-14.5 — background-task pattern for /clone-git
import logging
import json
import hashlib
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Dict, List
from datetime import datetime, timezone

from db import kb_files, kb_chunks, kb_entities, kb_toon, projects, srs_documents, ontology_snapshots, business_ontologies
from db import kb_git_sources, kb_module_selection  # iter-13.63 — git ingest + module-selection
from models import KBFile, KBStatus
from kb.parsers import parse_file, chunk_text
from kb.owl_extractor import extract, aggregate_stats, enrich_routes
from kb.toon import serialise, summarise
from kb.vector_store import index_chunks as qdrant_index, delete_project_vectors
from kb.owl_export import export_owl, export_kb_yaml
from kb.module_inventory_parser import parse_module_inventory, generate_module_text_summary
from kb.tech_detector import detect_tech_stack
from kb.stack_suggester import suggest_target_stacks
from kb.target_stack import validate_target_stack
from kb.graph_config import is_graph_kb_enabled
from kb.file_kinds import (   # iter-13.69 — source-file role taxonomy
    FILE_KINDS,
    VALID_KINDS,
    detect_kind,
    normalize_kind,
    kind_label,
    KIND_FIGMA_EXPORT,
    KIND_DESIGN_MOCKUP,
)
from kb.business_ontology import (
    cluster_entities,
    compute_kb_hash,
    enrich_with_llm,
    compose_business_ontology,
)

logger = logging.getLogger("lama.kb")

router = APIRouter(prefix="/kb", tags=["kb"])

# ---------------------------------------------------------------------------
# Extractor version (iter-14.30)
# Bump this when owl_extractor.py changes in ways that affect entity
# extraction (e.g., new regexes, new entity types, new filetypes).
# Including this in the build fingerprint ensures that caches built with
# older extraction logic are automatically invalidated.
# ---------------------------------------------------------------------------
EXTRACTOR_VERSION = "14.30-struts-fix"


# ---------------------------------------------------------------------------
# Build progress tracking (iter-13.33)
# ---------------------------------------------------------------------------
# The /build endpoint is synchronous and can take 30 s – several minutes on
# a large legacy ZIP (graphify LLM call alone is up to 180 s). We persist a
# tiny `build_state` doc keyed by project_id so the UI / curl can see the
# current phase + last error without scraping logs.

BUILD_PHASES = (
    "queued", "extracting", "aggregating", "tech_detect",
    "business_ontology", "toon_persist", "graph_build", "graphify",
    "deep_analysis",  # iter-14.80 — comprehensive BR/role/field analysis
    "qdrant_indexing", "done", "error",
)


async def _set_build_phase(project_id: str, phase: str, **extra) -> None:
    """Upsert build progress onto kb_toon.build_state (best-effort)."""
    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": phase,
        "updated_at": now,
    }
    if phase == "queued":
        state["started_at"] = now
        state["error"] = None
    if phase in ("done", "error"):
        state["finished_at"] = now
    state.update(extra)
    try:
        await kb_toon.update_one(
            {"project_id": project_id},
            {"$set": {f"build_state.{k}": v for k, v in state.items()},
             "$setOnInsert": {"project_id": project_id}},
            upsert=True,
        )
    except Exception as e:  # pragma: no cover — observability, never fatal
        logger.warning("could not persist build_state for %s: %s", project_id, e)
    logger.info("kb.build phase=%s project=%s %s", phase, project_id,
                {k: v for k, v in extra.items() if k not in ("error",)})

ALLOWED_EXTS = {
    # Core legacy
    ".php", ".sql",
    # Java stack (added)
    ".java", ".jsp", ".jspx", ".jspf", ".tag", ".tld", ".xml", ".properties", ".xhtml",
    # .NET / VB stack
    ".cs", ".vb", ".aspx", ".cshtml", ".vbhtml", ".config",
    # Frontend / scripting
    ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".scss",
    # Python
    ".py",
    # Data / docs
    ".pdf", ".csv", ".docx", ".txt", ".md", ".yaml", ".yml", ".json",
    # Archives
    ".zip",
}
SKIP_DIRS = {"node_modules", ".git", "vendor", "__pycache__", ".idea", ".vscode", "dist", "build", "__MACOSX", "META-INF"}
SKIP_FILE_PATTERNS = [
    re.compile(r"\.bak$", re.IGNORECASE),
    re.compile(r"\.save$", re.IGNORECASE),
    re.compile(r"_(bkp|old|backup)", re.IGNORECASE),
    re.compile(r"\.php_", re.IGNORECASE),  # AirtelApi.php_03Mar2025
    re.compile(r"\.DS_Store$", re.IGNORECASE),
    re.compile(r"(^|/)\._"),  # macOS AppleDouble resource-fork shadow files
]


def _should_skip_file(name: str) -> bool:
    return any(p.search(name) for p in SKIP_FILE_PATTERNS)


def _looks_minified(text: str, filename: str) -> bool:
    """iter-14.7 — Fast heuristic to detect minified / bundled files that
    trigger catastrophic-backtracking regex in the OWL extractor. Used to
    skip entity extraction (chunks are still persisted for RAG).

    Rules (any one hits → treat as minified):
      • Filename ends with `.min.js` / `.min.css` / `.bundle.js` / `-min.js`.
      • Text is ≥ 20KB AND has fewer than 5 newlines (single-line bundle).
      • Text is ≥ 100KB AND average line length > 500 chars.
    """
    fn = filename.lower()
    if fn.endswith((".min.js", ".min.css", ".bundle.js", "-min.js", ".min.mjs")):
        return True
    n = len(text)
    if n < 20_000:
        return False
    newlines = text.count("\n")
    if newlines < 5:
        return True
    if n >= 100_000 and (n / max(1, newlines)) > 500:
        return True
    return False


def _files_fingerprint(files: list[dict]) -> str:
    """Stable hash of KB file inventory + extractor version to enable fast no-op builds.
    
    iter-14.30 — include EXTRACTOR_VERSION in the hash so that caches built
    with older extraction logic are automatically invalidated when the
    extractor is upgraded (e.g., new regexes for Struts/Spring/SQL).
    """
    items = []
    for f in files or []:
        items.append({
            "id": f.get("id", ""),
            "filename": f.get("filename", ""),
            "size": int(f.get("size", 0) or 0),
            "chunk_count": int(f.get("chunk_count", 0) or 0),
            "entity_count": int(f.get("entity_count", 0) or 0),
            "status": f.get("status", ""),
        })
    items.sort(key=lambda x: (x["id"], x["filename"]))
    # Include extractor version so cache invalidates on extractor upgrades
    blob = json.dumps({"v": EXTRACTOR_VERSION, "files": items}, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", errors="ignore")).hexdigest()


@router.post("/scan-folder")
async def scan_folder(payload: dict):
    """Walk a folder on the server filesystem and ingest all supported files."""
    project_id = payload.get("project_id")
    folder = payload.get("folder_path", "").strip()
    # iter-13.69 — optional default kind applied to every file scanned in
    # this batch. Falls back to auto-detect when omitted / "auto".
    default_kind_raw = (payload.get("kind") or payload.get("default_kind") or "").strip().lower()
    default_kind = default_kind_raw if default_kind_raw in VALID_KINDS else ""
    # iter-13.83 — `replace` controls whether the project's prior KB ingest
    # is wiped before this scan. Defaults to TRUE for scan-folder because
    # scanning a folder represents "this folder IS the project's source of
    # truth" — re-scanning a different folder must not accumulate the old
    # folder's files. Pass replace=false explicitly to layer on top of an
    # existing ingest (e.g. scanning a sibling folder you want merged in).
    # AUTO-REPLACE: even when caller omits `replace`, we wipe when the
    # supplied `folder_path` differs from the project's stored legacy_path.
    replace_requested = "replace" in payload
    replace = _truthy(payload.get("replace")) if replace_requested else None
    if not project_id:
        raise HTTPException(400, "project_id required")
    if not folder:
        raise HTTPException(400, "folder_path required")

    if not os.path.isdir(folder):
        raise HTTPException(400, f"Path does not exist or is not a directory: {folder}")

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    # iter-13.83 — auto-replace decision. Honour explicit caller intent
    # first; otherwise default to True (this scan IS the source of truth).
    prior_path = (proj.get("legacy_path") or "").strip()
    if replace is None:
        replace = True
    purge_stats: dict = {}
    if replace:
        # iter-13.87 — also wipe downstream SRS / stage_context / chat
        # history so the new source isn't contaminated by artefacts that
        # cited the OLD legacy code.
        purge_stats = await _purge_project_kb(
            project_id, clear_legacy_path=False, clear_downstream=True,
        )
        logger.info(
            "scan-folder REPLACE for project=%s prior_path=%s new_path=%s purged=%s",
            project_id, prior_path or "(none)", folder, purge_stats,
        )

    # iter-13.35 — persist the scanned legacy folder on the project so the
    # SRS prompt can tell the LLM (and Factory Droid in particular) WHERE
    # the actual source files live. The Droid has filesystem access via
    # its `cwd`, so it can read files directly when an SRS section needs
    # ground-truth evidence beyond TOON / RAG snippets.
    try:
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "legacy_path": folder,
                "legacy_path_scanned_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
    except Exception:  # noqa: BLE001
        pass

    processed: list[str] = []
    skipped: list[str] = []

    for root, dirs, files in os.walk(folder):
        # prune skipped directories in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            full = os.path.join(root, f)
            if _should_skip_file(f):
                skipped.append(f)
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            try:
                with open(full, "rb") as fh:
                    content = fh.read()
            except Exception:
                skipped.append(f)
                continue

            filetype, text = parse_file(f, content)
            chunks = chunk_text(text, filetype=filetype, filename=f)

            rel = os.path.relpath(full, folder)
            kb_file = KBFile(
                project_id=project_id,
                filename=rel,
                filetype=filetype,
                size=len(content),
                chunk_count=len(chunks),
                status="uploaded",
                kind=default_kind or detect_kind(rel, filetype),
            )
            await kb_file_persist(kb_file, project_id, text, chunks)
            processed.append(kb_file.filename)

    return {
        "ok": True,
        "scanned": len(processed),
        "skipped": len(skipped),
        "files": processed,
        "skipped_files": skipped[:50],
        "replaced": bool(replace),                                   # iter-13.83
        "purge_stats": purge_stats if replace else {},               # iter-13.83
    }


@router.post("/upload")
async def upload_files(
    project_id: str = Form(...),
    files: List[UploadFile] = File(...),
    kind: str = Form(""),  # iter-13.69 — default kind for this batch
    replace: str = Form(""),  # iter-13.83 — set to "1"/"true" to wipe prior KB first
):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    default_kind = kind.strip().lower() if (kind or "").strip().lower() in VALID_KINDS else ""

    # iter-13.83 — `replace` defaults to FALSE for upload (the typical use
    # is "drop more files into the existing project"); set to true to wipe
    # the project's prior KB first. Use scan-folder / clone-git for the
    # source-of-truth ingest paths that default to replace=true.
    replace_flag = _truthy(replace)
    purge_stats: dict = {}
    if replace_flag:
        # iter-13.87 — clear downstream artefacts on explicit replace so
        # SRS / stage_context / messages don't leak references to files
        # that no longer exist.
        # iter-13.88 — ALSO clear legacy_path / legacy_git_* markers. Upload
        # has no folder-path semantics (files are stored in MongoDB), so a
        # stale `legacy_path` left over from a prior scan-folder would
        # otherwise get injected into the SRS prompt's "LEGACY CODEBASE
        # LOCATION" block, causing the LLM to cite source paths the user
        # never actually uploaded. This was the smoking gun behind the
        # "SRS still references the old source" regression on
        # replace-upload.
        purge_stats = await _purge_project_kb(
            project_id, clear_legacy_path=True, clear_downstream=True,
        )
        logger.info("upload REPLACE for project=%s purged=%s", project_id, purge_stats)

    uploaded = []
    for f in files:
        content = await f.read()
        filetype, text = parse_file(f.filename, content)
        chunks = chunk_text(text, filetype=filetype, filename=f.filename)

        kb_file = KBFile(
            project_id=project_id,
            filename=f.filename,
            filetype=filetype,
            size=len(content),
            chunk_count=len(chunks),
            status="uploaded",
            kind=default_kind or detect_kind(f.filename, filetype),
        )
        await kb_file_persist(kb_file, project_id, text, chunks)

        uploaded.append({
            "id": kb_file.id,
            "filename": kb_file.filename,
            "filetype": kb_file.filetype,
            "size": kb_file.size,
            "chunks": kb_file.chunk_count,
            "entities": kb_file.entity_count,
            "kind": kb_file.kind,
        })

    return {
        "uploaded": uploaded,
        "replaced": replace_flag,                       # iter-13.83
        "purge_stats": purge_stats if replace_flag else {},  # iter-13.83
    }


async def kb_file_persist(
    kb_file: KBFile,
    project_id: str,
    text: str,
    chunks: list[str],
    tenant_id: str | None = None,
) -> None:
    """Persist file + chunks + run OWL extraction on the in-memory parsed text.

    iter-13.120 — `tenant_id` can be pre-fetched by the caller (e.g. the
    ingest loop that processes many files for the same project) to skip a
    `projects.find_one` round-trip per file. When omitted we still resolve
    it on-the-fly for legacy call sites.

    iter-14.7 —
      • `extract()` is now dispatched via `asyncio.to_thread` so a
        regex-heavy pass on a large file does NOT block the event loop
        (which used to stall progress flushes + parallel workers'
        Mongo I/O, presenting to the user as "ingest stuck at N%").
      • Files above ~500KB of parsed text, or single-line files (heuristic:
        minified JS / concatenated bundles / SQL dumps) skip OWL entity
        extraction entirely. Chunks are still persisted so RAG chat works;
        we just don't pay quadratic-regex tax on generated code. This can
        be tuned via LAMA_OWL_MAX_TEXT_BYTES (default 500000) and
        LAMA_OWL_SKIP_MINIFIED (default "1").
    """
    entities: list = []
    try:
        _owl_max = int(os.environ.get("LAMA_OWL_MAX_TEXT_BYTES", "500000"))
    except (TypeError, ValueError):
        _owl_max = 500000
    _skip_minified = os.environ.get("LAMA_OWL_SKIP_MINIFIED", "1") not in ("0", "false", "False", "")
    _is_minified = _skip_minified and _looks_minified(text, kb_file.filename)
    if len(text) <= _owl_max and not _is_minified:
        try:
            _owl_timeout = float(os.environ.get("LAMA_OWL_TIMEOUT_SEC", "20"))
        except (TypeError, ValueError):
            _owl_timeout = 20.0
        try:
            entities = await asyncio.wait_for(
                asyncio.to_thread(
                    extract, kb_file.filetype, text, kb_file.filename,
                ),
                timeout=_owl_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "OWL extract TIMEOUT for %s after %.1fs — skipping entity extraction "
                "(chunks still persisted). Bump LAMA_OWL_TIMEOUT_SEC to increase.",
                kb_file.filename, _owl_timeout,
            )
            entities = []
        except Exception as e:  # noqa: BLE001
            logger.warning("OWL extract failed for %s: %s", kb_file.filename, e)
            entities = []
    else:
        logger.info(
            "OWL extract SKIPPED for %s (bytes=%d, minified=%s)",
            kb_file.filename, len(text), _is_minified,
        )
    kb_file.entity_count = len(entities)
    kb_file.status = "processed"
    # iter-13.87 — denormalise tenant_id onto every KB doc so the KB is
    # physically scoped to (tenant_id, project_id) and a stray query
    # missing project_id cannot leak across tenants.
    if not tenant_id:
        _proj = await projects.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1})
        tenant_id = ((_proj or {}).get("tenant_id") or "").strip() or "tenant_default"
    file_doc = kb_file.model_dump()
    file_doc["tenant_id"] = tenant_id
    # iter-13.120 — fold the terminal update_one into the initial insert.
    file_doc["raw_text_len"] = len(text)
    await kb_files.insert_one(file_doc)

    # store chunks
    if chunks:
        # batch the inserts so we never hold all 90k chunk dicts at once
        BATCH = 1000
        for start in range(0, len(chunks), BATCH):
            chunk_docs = [
                {
                    "id": f"{kb_file.id}:{i}",
                    "project_id": project_id,
                    "tenant_id": tenant_id,
                    "file_id": kb_file.id,
                    "chunk_index": i,
                    "content": c,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                for i, c in enumerate(chunks[start:start + BATCH], start=start)
            ]
            await kb_chunks.insert_many(chunk_docs)

    # store entities (also batched)
    if entities:
        BATCH = 1000
        for start in range(0, len(entities), BATCH):
            docs = []
            for e in entities[start:start + BATCH]:
                e_doc = dict(e)
                e_doc["project_id"] = project_id
                e_doc["tenant_id"] = tenant_id
                e_doc["file_id"] = kb_file.id
                docs.append(e_doc)
            await kb_entities.insert_many(docs)


@router.get("/{project_id}/files")
async def list_files(project_id: str):
    docs = await kb_files.find({"project_id": project_id}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@router.delete("/files/{file_id}")
async def delete_file(file_id: str):
    f = await kb_files.find_one({"id": file_id}, {"_id": 0})
    if not f:
        raise HTTPException(404, "File not found")
    await kb_chunks.delete_many({"file_id": file_id})
    await kb_entities.delete_many({"file_id": file_id})
    await kb_files.delete_one({"id": file_id})
    return {"ok": True}


@router.delete("/{project_id}/all")
async def delete_all_kb(project_id: str):
    """Wipe all KB data for a project (files, chunks, entities, TOON,
    vectors, graph, ontology, legacy analysis, BR tracking — and the
    cached source-of-truth markers like `legacy_path`/`legacy_git_*`).

    iter-13.83 — extended to cover EVERY KB-scoped collection so a fresh
    ingest after this call is genuinely fresh. Previously kb_graph,
    ontology_snapshots, business_ontologies and legacy_analysis lingered
    across re-scans, which is exactly what the "kb is considering all old
    project [scans] as well" complaint surfaces.
    """
    stats = await _purge_project_kb(
        project_id, clear_legacy_path=True, clear_downstream=True,
    )
    return {"ok": True, **stats}


async def _purge_project_kb(
    project_id: str,
    *,
    clear_legacy_path: bool = False,
    clear_downstream: bool = False,
) -> dict:
    """Wipe every KB-scoped collection for one project. Shared by
    `delete_all_kb` and the auto-replace path on scan-folder/upload/clone.

    iter-13.87 — `clear_downstream` also wipes EVERY artefact derived from
    the KB (SRS docs, stage_context, conversations + messages, data
    models, architecture outputs, codegen tree, living artifacts, stage
    confidence). Without this, swapping the legacy source (e.g. cloning
    a different git URL) left old SRS sections, old chat history that
    cited old classes, and old freeze gates behind — so the next
    "Generate SRS" produced output that "still referred to the old
    source". The clone-git / scan-folder / upload-replace paths now opt
    into this so the project becomes a genuine blank slate aside from
    the project doc and tenant link.

    Returns a small stats dict so callers can log how much was deleted.
    """
    from db import (
        kb_graph as _kb_graph_col,
        legacy_analysis as _legacy_col,
    )
    try:
        from db import br_table_links as _brl
    except Exception:  # noqa: BLE001
        _brl = None  # collection may not exist in older deployments
    out: dict = {}

    # Per-project shards — run all deletes in parallel. Each delete_many is
    # an independent Mongo command, so gather() ~= N/2 latency reduction on
    # top of the index speedup (iter-13.120).
    _shards = [
        ("kb_chunks",            kb_chunks,            {"project_id": project_id}),
        ("kb_entities",          kb_entities,          {"project_id": project_id}),
        ("kb_files",             kb_files,             {"project_id": project_id}),
        ("kb_graph",             _kb_graph_col,        {"project_id": project_id}),
        ("kb_module_selection",  kb_module_selection,  {"project_id": project_id}),
        ("kb_git_sources",       kb_git_sources,       {"project_id": project_id}),
        ("legacy_analysis",      _legacy_col,          {"project_id": project_id}),
        ("ontology_snapshots",   ontology_snapshots,   {"project_id": project_id}),
        ("business_ontologies",  business_ontologies,  {"project_id": project_id}),
    ]

    async def _del(name, col, filt):
        try:
            r = await col.delete_many(filt)
            return name, int(getattr(r, "deleted_count", 0) or 0)
        except Exception as e:  # noqa: BLE001
            return name, f"err: {type(e).__name__}"

    results = await asyncio.gather(*[_del(n, c, f) for n, c, f in _shards])
    for name, val in results:
        out[name] = val
    # Singleton-per-project docs.
    try:
        r = await kb_toon.delete_one({"project_id": project_id})
        out["kb_toon"] = int(getattr(r, "deleted_count", 0) or 0)
    except Exception as e:  # noqa: BLE001
        out["kb_toon"] = f"err: {type(e).__name__}"
    if _brl is not None:
        try:
            r = await _brl.delete_many({"project_id": project_id})
            out["br_table_links"] = int(getattr(r, "deleted_count", 0) or 0)
        except Exception as e:  # noqa: BLE001
            out["br_table_links"] = f"err: {type(e).__name__}"

    # Qdrant vectors. Wrap in a timeout — a wedged Qdrant should not hang
    # the whole delete_project call (iter-13.120).
    try:
        await asyncio.wait_for(delete_project_vectors(project_id), timeout=10.0)
        out["qdrant"] = "purged"
    except asyncio.TimeoutError:
        out["qdrant"] = "timeout"
    except Exception as e:  # noqa: BLE001
        out["qdrant"] = f"err: {type(e).__name__}"

    # iter-13.87 — Optionally wipe EVERY downstream artefact derived from
    # the KB. Called by clone-git / scan-folder / upload when the source
    # has truly changed (replace=True path). Old SRS sections + old chat
    # history + old stage_context were the smoking gun behind users
    # reporting "SRS still refers to the old project" after re-uploading.
    if clear_downstream:
        from db import (
            srs_documents as _srs_col,
            stage_context as _stagectx_col,
            conversations as _conv_col,
            messages as _msg_col,
            data_models as _dm_col,
            bus_matrix as _bm_col,
            olap_models as _olap_col,
            migration_artifacts as _mig_col,
            arch_documents as _arch_col,
            arch_services as _archsvc_col,
            codegen_files as _cgf_col,
            codegen_runs as _cgr_col,
            living_artifacts as _liva_col,
            living_runs as _livr_col,
            stage_confidence as _conf_col,
            freeze_gates as _fg_col,
        )
        _downstream = [
            ("srs_documents",        _srs_col),
            ("stage_context",        _stagectx_col),
            ("conversations",        _conv_col),
            ("messages",             _msg_col),
            ("data_models",          _dm_col),
            ("bus_matrix",           _bm_col),
            ("olap_models",          _olap_col),
            ("migration_artifacts",  _mig_col),
            ("arch_documents",       _arch_col),
            ("arch_services",        _archsvc_col),
            ("codegen_files",        _cgf_col),
            ("codegen_runs",         _cgr_col),
            ("living_artifacts",     _liva_col),
            ("living_runs",          _livr_col),
            ("stage_confidence",     _conf_col),
            ("freeze_gates",         _fg_col),
        ]
        results = await asyncio.gather(*[
            _del(n, c, {"project_id": project_id}) for n, c in _downstream
        ])
        for name, val in results:
            out[name] = val
        # Reset the project's pipeline progress so downstream stages
        # re-lock and the UI shows Discovery as the only available stage.
        try:
            await projects.update_one(
                {"id": project_id},
                {"$set": {
                    "stage_status": {
                        "Discovery":    "available",
                        "DataModel":    "locked",
                        "Architecture": "locked",
                        "CodeGen":      "locked",
                        "Living":       "locked",
                    },
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
        except Exception:  # noqa: BLE001
            pass

    # Optionally clear the project's "source of truth" markers so the next
    # scan/upload starts from a blank slate. Used by delete_all_kb but not
    # by the per-ingest auto-replace path (which immediately re-writes
    # legacy_path with the new folder).
    if clear_legacy_path:
        try:
            await projects.update_one(
                {"id": project_id},
                {"$unset": {
                    "legacy_path": "",
                    "legacy_path_scanned_at": "",
                    "legacy_git_url": "",
                    "legacy_git_branch": "",
                    "legacy_git_commit": "",
                }},
            )
        except Exception:  # noqa: BLE001
            pass
    logger.info("KB purge for project=%s → %s", project_id, out)
    return out


def _truthy(v) -> bool:
    """Tolerant truthiness coercion for query params / form fields."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


# ─────────────────────────────────────────────────────────────────────────
# iter-13.69 — Source-file KIND taxonomy + per-file tagging endpoints.
#
# Every kb_file now carries a `kind` declaring HOW downstream stages
# should use it (legacy code vs. existing SRS vs. Figma export vs.
# design mockup …). The full taxonomy lives in `kb/file_kinds.py`.
#
#   GET   /api/kb/kinds                          → catalogue (UI dropdown)
#   PATCH /api/kb/files/{file_id}/kind           → re-tag a single file
#   GET   /api/kb/{project_id}/source-inventory  → grouped counts + samples
#                                                  (fed into SRS + CodeGen)
# ─────────────────────────────────────────────────────────────────────────

@router.get("/kinds")
async def list_file_kinds():
    """Static catalogue of valid file kinds (label + hint)."""
    return {"kinds": FILE_KINDS}


@router.patch("/files/{file_id}/kind")
async def update_file_kind(file_id: str, payload: dict):
    """Re-tag a single KB file with a new kind.

    Body: ``{"kind": "<one of VALID_KINDS>", "notes": "<optional>"}``
    """
    new_kind = normalize_kind((payload or {}).get("kind"))
    notes = ((payload or {}).get("notes") or "").strip()[:500]
    if new_kind not in VALID_KINDS:
        raise HTTPException(400, f"Invalid kind. Allowed: {sorted(VALID_KINDS)}")
    r = await kb_files.update_one(
        {"id": file_id},
        {"$set": {"kind": new_kind, "kind_notes": notes}},
    )
    if r.matched_count == 0:
        raise HTTPException(404, "File not found")
    doc = await kb_files.find_one({"id": file_id}, {"_id": 0})
    return {"ok": True, "file": doc, "kind_label": kind_label(new_kind)}


async def _build_source_inventory(project_id: str, *, sample_per_kind: int = 5) -> dict:
    """Group every kb_file by `kind` so SRS/CodeGen prompts can see at a
    glance what auxiliary material the user uploaded.
    """
    rows = await kb_files.find(
        {"project_id": project_id},
        {"_id": 0, "id": 1, "filename": 1, "filetype": 1, "kind": 1, "size": 1},
    ).to_list(20000)

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        k = normalize_kind(r.get("kind"))
        buckets.setdefault(k, []).append(r)

    by_kind: list[dict] = []
    for cfg in FILE_KINDS:
        k = cfg["value"]
        items = buckets.get(k, [])
        if not items and k != "legacy_code":
            continue
        items.sort(key=lambda x: int(x.get("size", 0) or 0), reverse=True)
        by_kind.append({
            "kind": k,
            "label": cfg["label"],
            "hint": cfg["hint"],
            "count": len(items),
            "samples": [i.get("filename", "") for i in items[:sample_per_kind]],
        })
    return {"project_id": project_id, "total_files": len(rows), "by_kind": by_kind}


@router.get("/{project_id}/source-inventory")
async def get_source_inventory(project_id: str):
    """Return file counts + sample filenames grouped by `kind` for this project."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    return await _build_source_inventory(project_id)


def _format_source_inventory_block(inventory: dict) -> str:
    """Render the inventory as a prompt-ready markdown block (SRS prompts)."""
    by_kind = inventory.get("by_kind") or []
    interesting = [b for b in by_kind if b["count"] > 0 and b["kind"] != "legacy_code"]
    if not interesting:
        return ""
    lines: list[str] = [
        "SOURCE MATERIAL INVENTORY (iter-13.69 — user-tagged auxiliary inputs):",
        "  • Treat each item below as ADDITIONAL EVIDENCE on top of the legacy code.",
        "  • You MAY cite them in source columns (use the filename verbatim).",
        "  • `existing_srs` and `business_doc` items represent the CURRENT contract — preserve",
        "    every requirement they state unless legacy code visibly overrides it.",
        "  • `figma_export` / `design_mockup` items describe the desired UI THEME for the rebuild",
        "    (forwarded to Stage 4 CodeGen — call them out in UI / NFR sections only).",
        "  • `api_spec` items are authoritative on endpoint shapes (cite operationIds).",
        "",
    ]
    for b in interesting:
        samples = ", ".join(b.get("samples", [])[:5]) or "(no samples)"
        lines.append(f"  - {b['label']} (`{b['kind']}`) × {b['count']}: {samples}")
    return "\n".join(lines)


async def get_source_inventory_block(project_id: str) -> str:
    """Helper for other routes (SRS) — returns a ready-to-inject markdown block."""
    try:
        inv = await _build_source_inventory(project_id)
        return _format_source_inventory_block(inv)
    except Exception:
        return ""


async def get_theme_brief_for_codegen(project_id: str, max_chars: int = 6000) -> str:
    """Concatenate user-uploaded design/figma file content (and reconstruct
    text where possible) into a single THEME BRIEF block injected into the
    `codegen.frontend` prompt.

    Returns "" when no design/figma files are tagged.
    """
    rows = await kb_files.find(
        {"project_id": project_id, "kind": {"$in": [KIND_FIGMA_EXPORT, KIND_DESIGN_MOCKUP]}},
        {"_id": 0, "id": 1, "filename": 1, "kind": 1, "filetype": 1, "size": 1, "kind_notes": 1},
    ).to_list(50)
    if not rows:
        return ""

    rows.sort(key=lambda x: (x.get("kind") != KIND_FIGMA_EXPORT, -int(x.get("size", 0) or 0)))
    chunks_out: list[str] = []
    chunks_out.append("USER-PROVIDED THEME / DESIGN BRIEF (iter-13.69):")
    chunks_out.append(
        "  • The files below were tagged by the user as `figma_export` or `design_mockup`."
    )
    chunks_out.append(
        "  • Treat them as the AUTHORITATIVE source of colour palette, typography,"
    )
    chunks_out.append(
        "    spacing, component shapes, iconography and overall look-and-feel."
    )
    chunks_out.append(
        "  • Where legacy UI evidence and the theme brief disagree on VISUAL style,"
    )
    chunks_out.append(
        "    THE THEME BRIEF WINS for the rebuilt UI. Behavioural rules still come"
    )
    chunks_out.append(
        "    from the legacy code + SRS."
    )
    chunks_out.append("")

    budget = max(1000, int(max_chars) - len("\n".join(chunks_out)))
    per_file = max(400, budget // max(1, len(rows)))
    for f in rows:
        fid = f.get("id")
        fname = f.get("filename", "")
        klabel = kind_label(f.get("kind", ""))
        notes = (f.get("kind_notes") or "").strip()
        header = f"── {klabel}: `{fname}`"
        if notes:
            header += f"  // {notes[:200]}"
        chunks_out.append(header)
        # Reassemble (a slice of) the file's text from chunks for figma JSON
        # / token files. Image mockups have no useful text — list them by name only.
        text_pieces: list[str] = []
        cur = kb_chunks.find({"file_id": fid}, {"_id": 0, "content": 1}).sort("chunk_index", 1).limit(8)
        async for c in cur:
            text_pieces.append(c.get("content", "") or "")
        body = "\n".join(text_pieces).strip()
        if body:
            if len(body) > per_file:
                body = body[:per_file] + "\n… (truncated)"
            chunks_out.append(body)
        else:
            chunks_out.append("(binary asset — see filename above; mirror its visual style)")
        chunks_out.append("")
    out = "\n".join(chunks_out)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n… (truncated)"
    return out





@router.post("/build")
async def build_kb(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(400, "project_id required")
    # When force=true, wipe all previously extracted entities for this project
    # and re-run extraction on every file. Required after upgrading the
    # extractor (e.g. SQL regex fix in Iter-12) because the default code path
    # skips files that already have entities, so stale (broken) extractions
    # would persist forever.
    force = bool(payload.get("force"))

    files = await kb_files.find({"project_id": project_id}, {"_id": 0}).to_list(2000)
    if not files:
        raise HTTPException(400, "No files uploaded")
    build_fingerprint = _files_fingerprint(files)

    if not force:
        cached = await kb_toon.find_one(
            {"project_id": project_id},
            {"_id": 0, "stats": 1, "summary": 1, "toon": 1, "build_fingerprint": 1, "indexed_last": 1, "extractor_version": 1},
        )
        if cached and cached.get("toon") and cached.get("build_fingerprint") == build_fingerprint:
            proj_doc = await projects.find_one({"id": project_id}, {"_id": 0, "detected_tech": 1})
            # iter-13.73 — surface the persisted graph stats on the cached
            # short-circuit too, otherwise the UI's "Done · N graph nodes"
            # line lies (always 0) every time the user re-clicks Build KB
            # on an unchanged fingerprint, even though the graph already
            # has thousands of nodes/edges in kb_graph.
            # iter-13.74 — query the meta shard explicitly; the legacy
            # monolithic doc has no `kind` so fall through to that too.
            from db import kb_graph as _kb_graph_col
            _g = await _kb_graph_col.find_one(
                {"project_id": project_id, "kind": "meta"}, {"_id": 0, "stats": 1}
            ) or await _kb_graph_col.find_one(
                {"project_id": project_id, "kind": {"$exists": False}}, {"_id": 0, "stats": 1}
            ) or {}
            _g_stats = _g.get("stats") or {}
            await _set_build_phase(
                project_id,
                "done",
                cached=True,
                reason="fingerprint_unchanged",
                entity_count=int((cached.get("stats") or {}).get("entities", 0) or 0),
                chunks_indexed=int(cached.get("indexed_last", 0) or 0),
                graph_nodes=int(_g_stats.get("nodes_count", 0) or 0),
                graph_edges=int(_g_stats.get("edges_count", 0) or 0),
                graphify=_g_stats.get("graphify") or {},
            )
            return {
                "ok": True,
                "cached": True,
                "stats": cached.get("stats") or {},
                "summary": cached.get("summary") or "KB already up-to-date.",
                "toon_size": len(cached.get("toon") or ""),
                "indexed": int(cached.get("indexed_last", 0) or 0),
                "detected_tech": (proj_doc or {}).get("detected_tech") or {},
                "graph": _g_stats,
            }
        elif cached and cached.get("build_fingerprint"):
            # iter-14.30 — helpful log when cache is invalidated
            old_v = cached.get("extractor_version", "(unknown)")
            logger.info(
                "KB cache invalidated: old_extractor=%s, new_extractor=%s, "
                "fingerprint_changed=%s",
                old_v, EXTRACTOR_VERSION,
                cached.get("build_fingerprint") != build_fingerprint,
            )

    await _set_build_phase(project_id, "queued", file_count=len(files), force=force)
    try:
        return await _build_kb_impl(project_id, files, force, build_fingerprint=build_fingerprint)
    except HTTPException:
        await _set_build_phase(project_id, "error", error="HTTPException")
        raise
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        logger.exception("KB build failed for %s", project_id)
        await _set_build_phase(project_id, "error", error=f"{type(exc).__name__}: {exc}"[:500])
        raise HTTPException(500, f"KB build failed: {exc}") from exc


async def _build_kb_impl(project_id: str, files: list, force: bool, build_fingerprint: str = ""):
    if force:
        # iter-14.56 — Preserve live-DB ingested entities (TABLE / FK_HINT with
        # `from_live_db: True`) across a force-rebuild. Build KB only re-parses
        # uploaded source files; it does NOT re-connect to a registered Oracle /
        # Postgres / SQL Server / MySQL / SQLite live DB. Wiping them here left
        # the deterministic OLTP / OLAP / Migration paths with zero
        # `type: "TABLE"` entities even though `data_sources.schema_summary`
        # still advertised e.g. 2256 tables — producing the "No TABLE entities
        # found in KB" failure seen on the ceots pilot (Oracle EYDEVDB).
        await kb_entities.delete_many({
            "project_id": project_id,
            "from_live_db": {"$ne": True},
        })

    # Re-extract for any file that has chunks but zero entities (legacy uploads or
    # files where extraction was added after the upload).
    # Note: keep this in sync with kb/owl_extractor.extract() — anything that
    # extractor knows how to handle should be re-attempted here.
    # iter-14.22 — Added "xml" so struts-config.xml / web.xml / applicationContext.xml
    # actually reach extract_xml (previously routed to /dev/null: EXTRACTABLE was
    # {"php","sql","zip","java","jsp"} so every Struts 1 <action> mapping + every
    # web.xml <servlet-mapping> was silently discarded — root cause of the
    # 3-routes-for-1202-Java-files ceots gap). Also "dotnet","js","python" for
    # completeness so mixed-stack projects don't have this same footgun.
    EXTRACTABLE = {"php", "sql", "zip", "java", "jsp", "xml", "dotnet", "js", "python"}
    extracted_files = 0
    for f in files:
        existing = await kb_entities.count_documents({"file_id": f["id"]})
        if existing > 0 and not force:
            continue
        # iter-14.22 — repair stale filetype for .xml files that were
        # persisted as "config" by an earlier upload path (parse_file
        # returns "xml" for *.xml, but older kb_files docs mislabeled
        # them). We treat by-suffix when the filetype isn't extractable.
        ftype = f.get("filetype")
        if ftype not in EXTRACTABLE:
            fname_lower = (f.get("filename") or "").lower()
            if fname_lower.endswith(".xml"):
                ftype = "xml"
            elif fname_lower.endswith((".jspx", ".jspf", ".tag", ".xhtml")):
                ftype = "jsp"
            else:
                continue
        extracted_files += 1
        await _set_build_phase(
            project_id, "extracting",
            current_file=f.get("filename", ""),
            extracted_files=extracted_files,
            total_extractable=sum(1 for x in files if x.get("filetype") in EXTRACTABLE),
        )
        # Rebuild raw text from chunks — no cap, batched read
        cur = kb_chunks.find({"file_id": f["id"]}, {"_id": 0}).sort("chunk_index", 1)
        # Use the chunker overlap of 150 chars; for accuracy we just join — minor
        # duplicate text in overlaps is harmless for regex matching.
        pieces: list[str] = []
        async for c in cur:
            pieces.append(c["content"])
        text = "\n".join(pieces)
        # iter-14.10 — mirror the iter-14.7 ingest guard: skip OWL
        # extraction on oversized / minified files, and run it via
        # asyncio.to_thread so a regex-heavy pass on a large file does
        # NOT freeze the whole build (which the /build-progress poller
        # depends on to render live phase updates).
        try:
            _owl_max = int(os.environ.get("LAMA_OWL_MAX_TEXT_BYTES", "500000"))
        except (TypeError, ValueError):
            _owl_max = 500000
        _skip_minified = os.environ.get("LAMA_OWL_SKIP_MINIFIED", "1") not in ("0", "false", "False", "")
        try:
            if len(text) > _owl_max or (_skip_minified and _looks_minified(text, f.get("filename", ""))):
                entities = []
            else:
                entities = await asyncio.to_thread(
                    extract, ftype, text, f["filename"],
                )
        except Exception:
            entities = []
        if entities:
            BATCH = 1000
            for start in range(0, len(entities), BATCH):
                docs = []
                for e in entities[start:start + BATCH]:
                    e_doc = dict(e)
                    e_doc["project_id"] = project_id
                    e_doc["file_id"] = f["id"]
                    docs.append(e_doc)
                await kb_entities.insert_many(docs)
        # iter-14.30 — Detailed entity extraction logging for debugging
        route_count = sum(1 for e in entities if e.get("type") == "ROUTE")
        table_count = sum(1 for e in entities if e.get("type") in ("TABLE", "TABLE_HINT"))
        class_count = sum(1 for e in entities if e.get("type") == "CLASS")
        if route_count > 0 or entities:
            logger.info(
                "KB extract file=%s type=%s → %d entities (routes=%d, tables=%d, classes=%d)",
                f.get("filename", "?")[:60], ftype, len(entities), route_count, table_count, class_count,
            )
        await kb_files.update_one(
            {"id": f["id"]},
            {"$set": {"entity_count": len(entities), "status": "processed"}},
        )

    # Aggregate
    await _set_build_phase(project_id, "aggregating")
    all_entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)

    # iter-14.23 — Field-level ROUTE enrichment. Join FORM_FIELD / CLASS.fields
    # / FORM_BEAN / SECURITY_CONSTRAINT / JSP_MODEL evidence onto every ROUTE so
    # CodeGen sees request_fields / response_fields / roles / target_view and
    # can populate DTO records instead of emitting `⚠ EVIDENCE GAP` markers.
    # We mutate all_entities in place (so TOON/stats/summary see the enriched
    # shape) then persist the enriched ROUTE docs back to kb_entities.
    try:
        enrich_routes(all_entities)
        enriched_routes = [e for e in all_entities if e.get("type") == "ROUTE"]
        _wired = sum(1 for r in enriched_routes if r.get("request_fields"))
        if enriched_routes:
            # Replace ROUTE docs atomically: delete then re-insert the enriched
            # copies (they still carry project_id + file_id from the initial
            # load, so downstream file-scoped queries stay intact).
            await kb_entities.delete_many({"project_id": project_id, "type": "ROUTE"})
            BATCH = 1000
            for start in range(0, len(enriched_routes), BATCH):
                await kb_entities.insert_many(
                    [dict(r) for r in enriched_routes[start:start + BATCH]]
                )
        logger.info(
            "iter-14.23 route enrichment: %d ROUTEs, %d with request_fields, "
            "%d FORM_FIELD, %d SECURITY_CONSTRAINT",
            len(enriched_routes), _wired,
            sum(1 for e in all_entities if e.get("type") == "FORM_FIELD"),
            sum(1 for e in all_entities if e.get("type") == "SECURITY_CONSTRAINT"),
        )
    except Exception as _enr_exc:  # noqa: BLE001
        logger.warning("route field-evidence enrichment skipped: %s", _enr_exc)

    toon = serialise(all_entities)
    stats = aggregate_stats(all_entities)
    # `summary` is (re)computed AFTER tech detection below so it can carry
    # the actual primary language label (e.g. "Java classes") instead of the
    # old hard-coded "PHP classes" (iter 13.7 toast-message fix).
    summary = summarise(all_entities, stats)

    # ---------------------------------------------------------------------
    # Tech-stack auto-detection. Replaces the seed-supplied source_tech
    # (e.g. "PHP / CodeIgniter / MariaDB") with what the uploaded files
    # ACTUALLY are. This output also gets stored on the project so every
    # downstream LLM call (SRS sections, revalidation, architecture) sees
    # the truth — no more "still says PHP" complaint.
    # ---------------------------------------------------------------------
    # iter-13.96 — single project-scoped query instead of N per-file
    # round-trips. Sort by (file_id, chunk_index) so we can stop reading
    # for each file once the per-file cap is hit. ZIP files keep their
    # higher cap (200) because their inner-file enumeration depends on
    # seeing many chunks; everything else stops at 6.
    chunks_by_file: dict[str, list[str]] = {}
    per_file_caps = {
        (f.get("id") or ""): (200 if (f.get("filetype") or "").lower() == "zip" else 6)
        for f in files if f.get("id")
    }
    if per_file_caps:
        cur = kb_chunks.find(
            {"project_id": project_id, "file_id": {"$in": list(per_file_caps.keys())}},
            {"_id": 0, "file_id": 1, "content": 1, "chunk_index": 1},
        ).sort([("file_id", 1), ("chunk_index", 1)])
        async for c in cur:
            fid = c.get("file_id")
            cap = per_file_caps.get(fid)
            if not cap:
                continue
            bucket = chunks_by_file.setdefault(fid, [])
            if len(bucket) < cap:
                bucket.append(c.get("content", "") or "")

    # iter 13.6 part 2 — feed the detector a synthetic file list expanded
    # from `kb_entities.source` fields. Every extracted CLASS / TABLE /
    # ROUTE entity records its origin as `outer-file::inner-path` (see
    # owl_extractor.extract_zip), giving us the COMPLETE inner-file
    # enumeration of a 13MB zip essentially for free. Without this the
    # 200-chunk cap above would still miss the Java/JSP files that sit at
    # chunk_index > 200 inside a large legacy zip.
    seen_inner: set[str] = set()
    synthetic_files: list[dict] = []
    src_cursor = kb_entities.find(
        {"project_id": project_id, "source": {"$exists": True, "$ne": ""}},
        {"_id": 0, "source": 1},
    ).limit(20000)
    async for ent in src_cursor:
        src = (ent.get("source") or "")
        if "::" in src:
            inner = src.split("::", 1)[1].strip()
        else:
            inner = src.strip()
        if not inner or inner in seen_inner:
            continue
        seen_inner.add(inner)
        synthetic_files.append({"id": f"synthetic::{inner}", "filename": inner, "filetype": "", "size": 0})
    # Merge: real files first (they own the chunks for content scan),
    # synthetic files only contribute filenames for the language tally.
    files_for_detect = files + synthetic_files
    await _set_build_phase(project_id, "tech_detect")
    try:
        tech = detect_tech_stack(files, chunks_by_file)
    except Exception as exc:
        logger.warning(f"tech_detector failed: {exc}")
        tech = {"summary": "", "language": "", "frameworks": [], "database": "", "databases": [], "build": "", "evidence": {}}
    # Persist on the project so the UI sidebar + every prompt template sees
    # the truth. We only overwrite source_tech when detection actually
    # found something — never blank-out a manually-set value.
    if tech.get("summary") and tech["summary"] != "Unknown legacy stack":
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "source_tech": tech["summary"],
                "detected_tech": tech,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
    else:
        # Still persist the (partial) fingerprint so prompts can decide
        # whether to ask the LLM for a stack confirmation.
        await projects.update_one(
            {"id": project_id},
            {"$set": {"detected_tech": tech, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    # Rebuild the toast/system-prompt summary now that the tech detector
    # has identified the primary language — gives us "Java classes" /
    # "Python classes" / "C# classes" instead of the iter-13.6 hard-coded
    # "PHP classes". Fall back to whatever the project doc currently holds
    # (manual override) when detection found nothing.
    detected_lang = (tech or {}).get("language") or ""
    proj_doc = None
    if not detected_lang:
        proj_doc = await projects.find_one({"id": project_id}, {"_id": 0, "detected_tech": 1, "source_tech": 1, "tenant_id": 1})
        if proj_doc:
            detected_lang = ((proj_doc.get("detected_tech") or {}).get("language")
                             or (proj_doc.get("source_tech") or "").split("/")[0].strip())
    summary = summarise(all_entities, stats, language=detected_lang)

    await _set_build_phase(project_id, "toon_persist",
                           entity_count=len(all_entities), toon_size=len(toon))
    # iter-13.87 — denormalise tenant_id onto the KB singleton so multi-
    # tenant queries can filter on (project_id, tenant_id) without an
    # extra join. The project doc is the source of truth for tenant_id;
    # this is purely defence-in-depth so a misrouted query that omits
    # project_id can never leak across tenants.
    if proj_doc is None:
        proj_doc = await projects.find_one({"id": project_id}, {"_id": 0, "tenant_id": 1}) or {}
    _tenant_id = (proj_doc.get("tenant_id") or "").strip() or "tenant_default"
    await kb_toon.update_one(
        {"project_id": project_id},
        {"$set": {
            "project_id": project_id,
            "tenant_id": _tenant_id,
            "toon": toon,
            "summary": summary,
            "stats": stats,
            "build_fingerprint": build_fingerprint,
            "extractor_version": EXTRACTOR_VERSION,  # iter-14.30 — track extractor version
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    # ---- iter-13.29 — BUILD THE GRAPH BEFORE INDEXING -------------------
    # Previously the graph was built AFTER Qdrant indexing, so chunk
    # payloads carried no entity metadata and graph-first retrieval was
    # impossible at codegen / gap-recovery time. Build it now so the
    # indexing pass below can enrich each chunk with the class / method /
    # route / table / role names it contains.
    #
    # iter-13.36 — SHORT-CIRCUIT WHEN GRAPH KB IS OFF.
    # The deterministic graph build is O(entities) + O(chunks × nodes)
    # for the entity_refs annotation that follows. On a real-world legacy
    # KB (10k+ classes/methods + 5k+ chunks) this dominates Build KB
    # wall-time. When the Sidebar Graph-KB toggle is OFF, SRS / Arch /
    # CodeGen never read kb_graph (they also gate on
    # `is_graph_kb_enabled`), so we skip both the graph build AND the
    # per-chunk entity-ref pass entirely. The toggle becomes the user's
    # explicit "I don't need the graph" lever for slow builds.
    graph_stats: dict = {}
    graph_node_names_by_type: dict[str, set[str]] = {}
    graph_toggle_enabled = False
    try:
        graph_toggle_enabled = await is_graph_kb_enabled(project_id)
    except Exception:
        graph_toggle_enabled = False

    if not graph_toggle_enabled:
        logger.info(
            "kb_graph build skipped for %s — Graph KB toggle is OFF "
            "(saves the O(entities)+O(chunks×nodes) wall-time).",
            project_id,
        )
        graph_stats = {"skipped": True, "reason": "graph_kb_disabled",
                       "nodes_count": 0, "edges_count": 0,
                       "graphify": {"skipped": True, "reason": "graph_kb_disabled",
                                    "added_nodes": 0, "added_edges": 0}}
        await _set_build_phase(project_id, "graph_build", skipped=True,
                               reason="graph_kb_disabled")
    else:
        try:
            from kb.kb_graph import build_kb_graph, load_kb_graph
            await _set_build_phase(project_id, "graph_build")

            async def _graph_phase_cb(phase_name: str, extra: dict | None = None):
                await _set_build_phase(project_id, phase_name, **(extra or {}))

            graph_stats = await build_kb_graph(
                project_id,
                graphify_enabled=graph_toggle_enabled,
                phase_callback=_graph_phase_cb,
            )
            # graphify enrichment runs inside build_kb_graph(); the phase was
            # already flipped to "graphify" by _graph_phase_cb before the LLM
            # call started. Now record the result counters.
            if graph_toggle_enabled and graph_stats.get("graphify"):
                await _set_build_phase(project_id, "graphify",
                                       graphify=graph_stats.get("graphify"))
            g = await load_kb_graph(project_id)
            if g and g.get("nodes"):
                for n in g["nodes"]:
                    graph_node_names_by_type.setdefault(n["type"], set()).add(n["name"])
        except Exception as e:
            # iter-13.74 — make this failure VISIBLE. The previous
            # warn-and-continue silently hid the BSON DocumentTooLarge
            # (EJHS hit 17.66 MB > 16 MB) so the UI showed "Done · 0
            # graph nodes" and SRS later complained "Graphify KB ON but
            # no graph built". The graph build is now sharded (so this
            # specific failure won't recur) but if a DIFFERENT failure
            # crops up we want it on the build_phase doc + a record in
            # graph_stats so the UI can show the reason.
            err_text = f"{type(e).__name__}: {e}"[:500]
            logger.warning("kb_graph build failed (continuing without graph): %s", err_text)
            graph_stats = {
                "skipped": True, "reason": "graph_build_failed",
                "error": err_text,
                "nodes_count": 0, "edges_count": 0,
                "graphify": {"skipped": True, "reason": "graph_build_failed",
                             "error": err_text,
                             "added_nodes": 0, "added_edges": 0},
            }
            await _set_build_phase(
                project_id, "graph_build",
                skipped=True, reason="graph_build_failed", error=err_text,
            )

    # ------------------------------------------------------------------
    # iter-14.24 — Journey KB (Phase 1) materialisation.
    # Opt-in via LAMA_USE_JOURNEY_KB=1 or per-project toggle. Runs after
    # the graph is built (needs Column/Table/Method/Route nodes). Best-
    # effort — failures are logged but never fail Build KB.
    # ------------------------------------------------------------------
    try:
        from kb.journey_config import is_journey_kb_enabled
        journey_kb_on = await is_journey_kb_enabled(project_id)
    except Exception as _je:  # noqa: BLE001
        logger.warning("journey_kb toggle lookup failed: %s", _je)
        journey_kb_on = False
    if journey_kb_on:
        try:
            from kb.journey_materializer import materialize_journeys
            j_stats = await materialize_journeys(project_id)
            logger.info("journey materialize done: %s", j_stats)
        except Exception as _je:  # noqa: BLE001
            logger.warning(
                "journey materialize failed (continuing): %s", _je,
            )

    # ------------------------------------------------------------------
    # iter-14.80 — Deep KB Analysis.
    # Run comprehensive LLM-powered analysis to extract:
    # - Business Rules with preconditions/postconditions
    # - Role → Privilege mappings and approval chains
    # - UI → API → DB field traceability with gap detection
    # The output is persisted to kb_deep_analysis and used by SRS,
    # CodeGen, and Test Case generation stages.
    # ------------------------------------------------------------------
    deep_analysis_stats: dict = {}
    try:
        await _set_build_phase(project_id, "deep_analysis")
        from kb.deep_analyzer import run_deep_analysis
        da_result = await run_deep_analysis(project_id, force=force)
        if da_result.get("error"):
            logger.warning(
                "Deep analysis failed (continuing): %s",
                da_result.get("error"),
            )
            deep_analysis_stats = {"skipped": True, "error": da_result.get("error")}
        else:
            analysis = da_result.get("analysis", {})
            deep_analysis_stats = {
                "cached": da_result.get("cached", False),
                "business_rules": len(analysis.get("business_rules", [])),
                "roles": len((analysis.get("role_analysis") or {}).get("roles", [])),
                "fields_traced": len(analysis.get("field_traceability", [])),
                "gaps_detected": len(analysis.get("validation_gaps", [])),
                "use_cases": len(analysis.get("use_cases", [])),
            }
            logger.info(
                "Deep analysis complete for %s: %s",
                project_id, deep_analysis_stats,
            )
    except Exception as _da_exc:  # noqa: BLE001
        logger.warning(
            "Deep analysis skipped (continuing): %s", _da_exc,
        )
        deep_analysis_stats = {"skipped": True, "error": str(_da_exc)[:200]}

    # Push all chunks into Qdrant for semantic RAG (best-effort, non-blocking failure).
    # iter-13.18 — dedup near-identical chunks (legacy code is rife with copy-paste
    # controllers / boilerplate JSP includes). Hash the chunk after stripping the
    # CONTEXT header line so legitimately-duplicated logic across files collapses
    # to one Qdrant point but its filename/symbol metadata is preserved in the
    # surviving chunk's payload.
    import hashlib as _hash
    import re as _annotate_re
    seen_hashes: set[str] = set()
    all_chunk_dicts: list[dict] = []
    dropped_dupes = 0

    # iter-13.36 — PRECOMPUTE name-token sets per node type ONCE so the
    # per-chunk annotation loop is O(chunk_tokens + node_count) instead of
    # O(chunks × nodes × name_len). Skipped entirely when the graph is
    # empty (Graph KB off, or build failed) — `entity_refs` stays empty.
    name_token_index: dict[str, dict[str, set[str]]] = {}
    if graph_node_names_by_type:
        for ntype, names in graph_node_names_by_type.items():
            # For each ntype build: { token_lower → set(original_names) }.
            # Methods like "UserCtrl.save" contribute BOTH the qualified form
            # (matched as substring at the very end) and the short name.
            tok_map: dict[str, set[str]] = {}
            for n in names:
                if not n or len(n) < 3:
                    continue
                # Primary token = whole name lowercased (for class/table/role).
                tok_map.setdefault(n.lower(), set()).add(n)
                # Short component (for qualified method names).
                if "." in n:
                    short = n.rsplit(".", 1)[-1]
                    if short and len(short) >= 3:
                        tok_map.setdefault(short.lower(), set()).add(n)
            if tok_map:
                name_token_index[ntype] = tok_map
    _CHUNK_TOKEN_RE = _annotate_re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

    # iter-13.96 — single-query bulk chunk read.
    # Previously this loop did `kb_chunks.find({"file_id": f["id"]})` per
    # file → N Mongo round-trips for an N-file project (689 round-trips
    # for the TJHS pilot). On large legacy KBs this dominated the
    # post-extraction wall-time. Replaced with ONE project-scoped query
    # whose results we group by file_id in memory.
    file_meta_by_id: dict[str, dict] = {f["id"]: f for f in files if f.get("id")}
    _chunk_phase_t0 = time.time()
    _bulk_cursor = kb_chunks.find(
        {"project_id": project_id},
        {"_id": 0, "id": 1, "file_id": 1, "content": 1},
    )
    _processed_chunks = 0
    async for c in _bulk_cursor:
        f = file_meta_by_id.get(c.get("file_id"))
        if f is None:
            # Orphan chunk (file_id was deleted from kb_files); skip rather
            # than letting it leak into the Qdrant payload with an empty
            # filename.
            continue
        _processed_chunks += 1
        # Lightweight heartbeat every 2000 chunks so the UI's
        # build-progress panel shows forward motion through what is
        # otherwise a silent CPU-bound phase.
        if _processed_chunks % 2000 == 0:
            await _set_build_phase(
                project_id, "aggregating",
                stage="chunk_dedup",
                chunks_processed=_processed_chunks,
                kept=len(all_chunk_dicts),
                dropped=dropped_dupes,
                elapsed_s=round(time.time() - _chunk_phase_t0, 2),
            )
        body = c["content"] or ""
        # Strip CONTEXT header so identical bodies dedup across files.
        body_for_hash = body
        if body_for_hash.startswith("# CONTEXT:"):
            nl = body_for_hash.find("\n")
            if nl > 0:
                body_for_hash = body_for_hash[nl + 1:]
        # Normalize whitespace for hashing.
        body_for_hash = " ".join(body_for_hash.split())
        if len(body_for_hash) < 40:
            # Too small to be useful evidence; skip.
            dropped_dupes += 1
            continue
        h = _hash.md5(body_for_hash.encode("utf-8", errors="ignore")).hexdigest()
        if h in seen_hashes:
            dropped_dupes += 1
            continue
        seen_hashes.add(h)
        # iter-13.29 — annotate chunk payload with the graph entities
        # that appear in it (class / method / route / table / role
        # names matched verbatim). Indexed payload allows graph-first
        # filtered retrieval at codegen/gap-recovery time instead of
        # blind cosine similarity.
        #
        # iter-13.36 — set-intersection fast path. We tokenize the chunk
        # ONCE, then for each ntype intersect with its precomputed
        # name-token set. This replaces the per-chunk O(node_count ×
        # name_len) substring scan with O(chunk_tokens + matched_nodes).
        # Measured ~40× faster on a 10k-class / 5k-chunk legacy KB.
        entity_refs: dict[str, list[str]] = {}
        if name_token_index:
            tokens_in_chunk = {t.lower() for t in _CHUNK_TOKEN_RE.findall(body)}
            if tokens_in_chunk:
                for ntype, tok_map in name_token_index.items():
                    matched: set[str] = set()
                    # Iterate the SMALLER side — token-set is typically
                    # 50–500 tokens; tok_map can be 10k+ keys.
                    for tok in tokens_in_chunk:
                        names = tok_map.get(tok)
                        if names:
                            matched.update(names)
                            if len(matched) >= 25:
                                break
                    if matched:
                        entity_refs[ntype] = sorted(matched)[:25]
        all_chunk_dicts.append({
            "id": c["id"],
            "content": body,
            "filename": f["filename"],
            "filetype": f["filetype"],
            "entity_refs": entity_refs,
            "class_names": entity_refs.get("Class", []),
            "method_names": entity_refs.get("Method", []),
            "table_names": entity_refs.get("Table", []),
            "route_paths": entity_refs.get("Route", []),
            "roles": entity_refs.get("Role", []),
        })
    logger.info(
        "kb.build[%s]: chunk_dedup done — processed=%d kept=%d dropped=%d in %.2fs",
        project_id, _processed_chunks, len(all_chunk_dicts), dropped_dupes,
        time.time() - _chunk_phase_t0,
    )
    indexed = 0
    if all_chunk_dicts:
        logger.info(
            f"Indexing {len(all_chunk_dicts)} chunks into Qdrant "
            f"(dropped {dropped_dupes} duplicates/trivial)"
        )
        await _set_build_phase(project_id, "qdrant_indexing",
                               chunks=len(all_chunk_dicts),
                               dropped=dropped_dupes)
        try:
            indexed = await qdrant_index(project_id, all_chunk_dicts)
        except Exception as e:
            logger.warning(f"Qdrant indexing failed (continuing without RAG): {e}")

    # ---- iter-13.29 — graph already built above (before indexing) -------
    # graph_stats was populated pre-indexing so chunk payloads carry
    # entity_refs / class_names / method_names / table_names / route_paths
    # / roles. Nothing left to do here.

    await _set_build_phase(
        project_id, "done",
        entity_count=len(all_entities),
        chunks_indexed=indexed,
        graph_nodes=(graph_stats or {}).get("nodes_count", 0),
        graph_edges=(graph_stats or {}).get("edges_count", 0),
        # iter-13.34 — surface graphify LLM-enrichment outcome to the UI.
        graphify=(graph_stats or {}).get("graphify", {}),
        # iter-14.80 — surface deep analysis stats
        deep_analysis=deep_analysis_stats,
    )
    await kb_toon.update_one(
        {"project_id": project_id},
        {"$set": {
            "build_fingerprint": build_fingerprint,
            "indexed_last": indexed,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    # iter-14.9 — Materialize a compact context bundle to the per-project
    # droid workspace so subsequent droid calls can `cat` the KB from
    # `.lama/` instead of receiving 300K-char embedded prompts. This is
    # the token-spend fix the operator asked for: "context will be
    # passed to droid at the time of knowledge building". Best-effort —
    # a materialize failure is logged but never blocks Build KB.
    try:
        from kb.droid_workspace import materialize_kb_context
        ws_result = await materialize_kb_context(project_id, toon=toon, stats=stats)
        logger.info(
            "Droid workspace materialize result for %s: %s",
            project_id,
            {k: v for k, v in (ws_result or {}).items() if k != "error"},
        )
    except Exception as ws_exc:  # noqa: BLE001
        logger.warning(
            "Droid workspace materialize skipped for %s: %s",
            project_id, ws_exc,
        )

    # iter-14.23 — audit-log the KB rebuild (field-evidence enrichment run)
    # so the pipeline history records the extractor upgrade + route wiring.
    try:
        from db import audit_log as _audit
        _route_rf = sum(1 for e in all_entities
                        if e.get("type") == "ROUTE" and e.get("request_fields"))
        await _audit.insert_one({
            "action": "kb.rebuild",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "force": bool(force),
                "entities": len(all_entities),
                "routes": stats.get("routes", 0),
                "form_fields": sum(1 for e in all_entities if e.get("type") == "FORM_FIELD"),
                "routes_with_request_fields": _route_rf,
                "security_constraints": sum(1 for e in all_entities
                                            if e.get("type") == "SECURITY_CONSTRAINT"),
                "iter": "14.23",
            },
        })
    except Exception as _ae:  # noqa: BLE001
        logger.warning("kb.rebuild audit_log write skipped: %s", _ae)

    return {
        "ok": True,
        "stats": stats,
        "summary": summary,
        "toon_size": len(toon),
        "indexed": indexed,
        "detected_tech": tech,
        "graph": graph_stats,
    }
async def build_progress(project_id: str):
    """Return the current/last KB-build phase for a project.

    Phases (in order): ``queued → extracting → aggregating → tech_detect →
    business_ontology → toon_persist → graph_build → graphify →
    qdrant_indexing → done`` (or ``error``).

    Use this to tell whether ``POST /api/kb/build`` is still running
    (no ``finished_at`` yet) or failed (``phase == "error"`` with an
    ``error`` message). Poll every 1–2 s while the build is in flight.
    """
    doc = await kb_toon.find_one(
        {"project_id": project_id},
        {"_id": 0, "build_state": 1},
    )
    state = (doc or {}).get("build_state") or {}
    if not state:
        return {
            "project_id": project_id,
            "phase": "idle",
            "running": False,
            "phases": list(BUILD_PHASES),
        }
    phase = state.get("phase", "unknown")
    running = phase not in ("done", "error", "idle")
    return {
        "project_id": project_id,
        "running": running,
        "phases": list(BUILD_PHASES),
        **state,
    }


# ---------------------------------------------------------------------------
# iter-14.80 — Deep Analysis endpoints
# ---------------------------------------------------------------------------

@router.get("/{project_id}/deep-analysis")
async def get_deep_analysis(project_id: str):
    """Return the deep analysis (business rules, roles, field traceability).
    
    This analysis is generated during Build KB and provides structured
    insights used by SRS, CodeGen, and Test Case generation.
    """
    from db import kb_deep_analysis
    doc = await kb_deep_analysis.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        return {
            "ok": False,
            "error": "No deep analysis found — run Build KB first",
            "project_id": project_id,
        }
    return {
        "ok": True,
        "project_id": project_id,
        "analysis": doc.get("analysis", {}),
        "summary_md": doc.get("summary_md", ""),
        "content_hash": doc.get("content_hash", ""),
        "created_at": doc.get("created_at", ""),
    }


@router.post("/{project_id}/deep-analysis/run")
async def run_deep_analysis_endpoint(project_id: str, payload: dict = None):
    """Trigger or re-run the deep analysis for a project.
    
    This is automatically called during Build KB, but can also be
    triggered manually to refresh the analysis.
    
    Body: {"force": true} to re-run even if cached result exists.
    """
    payload = payload or {}
    force = bool(payload.get("force"))
    model = payload.get("model") or ""
    
    from kb.deep_analyzer import run_deep_analysis
    result = await run_deep_analysis(project_id, force=force, model=model)
    
    if result.get("error"):
        return {
            "ok": False,
            "error": result["error"],
            "project_id": project_id,
        }
    
    analysis = result.get("analysis", {})
    return {
        "ok": True,
        "project_id": project_id,
        "cached": result.get("cached", False),
        "stats": {
            "business_rules": len(analysis.get("business_rules", [])),
            "roles": len((analysis.get("role_analysis") or {}).get("roles", [])),
            "fields_traced": len(analysis.get("field_traceability", [])),
            "gaps_detected": len(analysis.get("validation_gaps", [])),
            "use_cases": len(analysis.get("use_cases", [])),
        },
        "summary_md_chars": len(result.get("summary_md", "")),
    }


@router.get("/{project_id}/status", response_model=KBStatus)
async def kb_status(project_id: str):
    """Return live KB health counters.

    Previously this returned the *cached* `stats` written by `build_kb`, so
    Tables/Relations/Roles stayed at 0 until the user clicked "Build
    Knowledge Base" again. We now recompute the entity-type breakdown live
    from `kb_entities` on every call (the Refresh button on KB Health relies
    on this). The classes/methods/tables/columns/roles values match what an
    OWL export would report. Stats other than entity-type breakdown
    (modules, component_maps, relationships) still come from the cached
    `kb_toon` doc because they're produced by the toon builder.
    """
    file_count = await kb_files.count_documents({"project_id": project_id})
    chunk_count = await kb_chunks.count_documents({"project_id": project_id})
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    cached_stats = (toon_doc or {}).get("stats", {})
    toon_size = len((toon_doc or {}).get("toon", "")) if toon_doc else 0

    # Live entity-type breakdown via $group aggregation — cheap even for
    # 10k+ entities and avoids the stale-cache bug. Maps OWL type names to
    # KBStatus field names.
    type_to_field = {
        "CLASS": "classes",
        "METHOD": "methods",
        "TABLE": "tables",
        "COLUMN": "columns",
        "ROLE": "roles",
        "ROUTE": "routes",  # not on KBStatus today but harmless if added
    }
    live_counts: dict[str, int] = {v: 0 for v in type_to_field.values()}
    total_entities = 0
    try:
        cursor = kb_entities.aggregate([
            {"$match": {"project_id": project_id}},
            {"$group": {"_id": "$type", "n": {"$sum": 1}}},
        ])
        async for row in cursor:
            n = int(row.get("n", 0) or 0)
            total_entities += n
            field = type_to_field.get(row.get("_id") or "")
            if field:
                live_counts[field] = n
    except Exception:
        # If aggregation blows up for any reason, fall back to cached stats
        # so the UI still gets *something*.
        total_entities = cached_stats.get("entities", 0)
        for f in live_counts:
            live_counts[f] = cached_stats.get(f, 0)

    return KBStatus(
        project_id=project_id,
        files=file_count,
        chunks=chunk_count,
        # If there are no live entities, NO derived counter is trustworthy
        # any more — the toon cache is from a previous build. We previously
        # gated this on `file_count > 0`, but that left "1581 entities /
        # 525 tables / 208 relations" ghosts after the user deleted the
        # entities (kb_entities collection wiped) while keeping the source
        # files. Use `entities > 0` instead so the KB Health card actually
        # zeroes out when you delete files or wipe the KB.  (iter 13.4)
        entities=total_entities if file_count > 0 else 0,
        classes=live_counts["classes"] if total_entities > 0 else 0,
        methods=live_counts["methods"] if total_entities > 0 else 0,
        tables=live_counts["tables"] if total_entities > 0 else 0,
        columns=live_counts["columns"] if total_entities > 0 else 0,
        roles=live_counts["roles"] if total_entities > 0 else 0,
        routes=live_counts["routes"] if total_entities > 0 else 0,  # iter-14.30
        # Relationships, modules and component_maps come from the cached
        # toon doc — zero them out when there are no entities so the card
        # doesn't show ghost numbers after a wipe.
        relationships=cached_stats.get("relationships", 0) if total_entities > 0 else 0,
        toon_size=toon_size if total_entities > 0 else 0,
        modules=cached_stats.get("modules", 0) if total_entities > 0 else 0,
        component_maps=cached_stats.get("component_maps", 0) if total_entities > 0 else 0,
    )


@router.get("/{project_id}/toon")
async def get_toon(project_id: str):
    doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        return {"toon": "", "summary": "", "stats": {}}
    return doc


# iter-14.30 — Diagnostic endpoint for detailed KB extraction stats
@router.get("/{project_id}/extraction-stats")
async def get_extraction_stats(project_id: str):
    """Detailed KB extraction statistics for debugging.
    Shows routes by framework, tables by source, and files with most entities."""
    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)
    
    # Count by type
    type_counts: dict[str, int] = {}
    routes_by_framework: dict[str, int] = {}
    routes_by_source: dict[str, list] = {}
    tables_by_source: dict[str, list] = {}
    files_with_routes: list[dict] = []
    
    for e in entities:
        et = (e.get("type") or "").upper()
        type_counts[et] = type_counts.get(et, 0) + 1
        src = e.get("source") or "(unknown)"
        
        if et == "ROUTE":
            fw = e.get("framework") or "unknown"
            routes_by_framework[fw] = routes_by_framework.get(fw, 0) + 1
            if src not in routes_by_source:
                routes_by_source[src] = []
            routes_by_source[src].append({
                "verb": e.get("verb"),
                "path": e.get("name"),
                "handler_class": e.get("handler_class"),
            })
        elif et in ("TABLE", "TABLE_HINT"):
            if src not in tables_by_source:
                tables_by_source[src] = []
            tables_by_source[src].append(e.get("name"))
    
    # Top files with most routes
    for src, routes in sorted(routes_by_source.items(), key=lambda x: -len(x[1]))[:20]:
        files_with_routes.append({
            "file": src,
            "route_count": len(routes),
            "routes": routes[:10],  # First 10 routes
        })
    
    # Top files with most tables
    files_with_tables = []
    for src, tables in sorted(tables_by_source.items(), key=lambda x: -len(x[1]))[:20]:
        files_with_tables.append({
            "file": src,
            "table_count": len(tables),
            "tables": tables[:20],
        })
    
    return {
        "project_id": project_id,
        "total_entities": len(entities),
        "by_type": type_counts,
        "routes": {
            "total": type_counts.get("ROUTE", 0),
            "by_framework": routes_by_framework,
            "by_file": files_with_routes,
        },
        "tables": {
            "total": type_counts.get("TABLE", 0) + type_counts.get("TABLE_HINT", 0),
            "by_file": files_with_tables,
        },
        "hint": "If routes are low, ensure struts-config.xml and Spring controllers are in the uploaded files. Re-run 'Build KB' after upload.",
    }


@router.get("/{project_id}/ontology")
async def get_ontology(project_id: str):
    """Return a nodes/edges graph of all extracted ontology elements for the
    Ontology Studio visualiser. Pure JSON — no LLM call."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()
    type_counts: dict[str, int] = {}

    def add_node(nid: str, ntype: str, label: str, **meta):
        if nid in node_ids:
            return
        node_ids.add(nid)
        nodes.append({"id": nid, "type": ntype, "label": label, **meta})
        type_counts[ntype] = type_counts.get(ntype, 0) + 1

    def add_edge(src: str, dst: str, kind: str):
        if src in node_ids and dst in node_ids:
            edges.append({"source": src, "target": dst, "kind": kind})

    for e in entities:
        et = e.get("type", "")
        src = e.get("source", "")

        if et == "CLASS":
            cid = f"class:{e.get('namespace', '')}.{e['name']}"
            add_node(cid, "Class", e["name"],
                     namespace=e.get("namespace", ""),
                     source=src,
                     extends=e.get("extends", ""),
                     implements=e.get("implements", []),
                     is_jpa_entity=e.get("is_jpa_entity", False))
            if e.get("extends"):
                pid = f"class:{e['extends']}"
                add_node(pid, "Class", e["extends"], synthetic=True)
                add_edge(cid, pid, "extends")
            for impl in (e.get("implements") or []):
                iid = f"interface:{impl}"
                add_node(iid, "Interface", impl, synthetic=True)
                add_edge(cid, iid, "implements")
            for m in (e.get("methods") or []):
                mid = f"method:{cid}#{m['name']}"
                add_node(mid, "Method", m["name"],
                         params=m.get("params", ""), tables=m.get("tables", []),
                         sessions=m.get("sessions", []), parent_class=cid)
                add_edge(cid, mid, "has_method")
                for t in (m.get("tables") or []):
                    tid = f"table:{t}"
                    add_node(tid, "Table", t)
                    add_edge(mid, tid, "uses_table")

        elif et == "TABLE":
            tid = f"table:{e['name']}"
            add_node(tid, "Table", e["name"],
                     columns=e.get("columns", []),
                     source=src)
            for c in (e.get("columns") or []):
                cid = f"col:{e['name']}.{c.get('name', '')}"
                add_node(cid, "Column", c.get("name", ""),
                         data_type=c.get("type", ""), is_pk=c.get("is_pk", False),
                         is_fk=c.get("is_fk", False), parent_table=tid)
                add_edge(tid, cid, "has_column")
                if c.get("references"):
                    ref_tid = f"table:{c['references']}"
                    add_node(ref_tid, "Table", c["references"], synthetic=True)
                    add_edge(tid, ref_tid, "references")

        elif et == "TABLE_HINT":
            tid = f"table:{e['name']}"
            add_node(tid, "Table", e["name"], hint_from=src,
                     via=e.get("via", ""))

        elif et == "ROUTE":
            rid = f"route:{e.get('verb', '')}:{e['name']}"
            add_node(rid, "Route", e["name"],
                     verb=e.get("verb", ""), handler=e.get("handler", ""),
                     source=src)

        elif et == "JSP_FORM":
            fid = f"jspform:{e.get('action', '')}:{src}"
            add_node(fid, "JspForm", e.get("action", ""), source=src)
            # Try to link to a matching route
            for n in nodes:
                if n["type"] == "Route" and n["label"].rstrip("/") == e.get("action", "").rstrip("/"):
                    add_edge(fid, n["id"], "posts_to")
                    break

        elif et == "JSP_INCLUDE":
            iid = f"jspinclude:{e.get('file', '')}:{src}"
            add_node(iid, "JspInclude", e.get("file", ""), source=src)

        elif et == "JSP_TABLE_REFS":
            for t in (e.get("tables") or []):
                add_node(f"table:{t}", "Table", t, hint_from=src)

        elif et == "ROLE":
            rid = f"role:{e['name']}"
            add_node(rid, "Role", e["name"], source=src)

        elif et == "RELATIONSHIP":
            # Direct table-to-table FK relationship
            a = f"table:{e.get('from', '')}"
            b = f"table:{e.get('to', '')}"
            add_node(a, "Table", e.get("from", ""), synthetic=True)
            add_node(b, "Table", e.get("to", ""), synthetic=True)
            add_edge(a, b, e.get("kind", "references"))

    return {
        "project_id": project_id,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "by_type": type_counts,
        },
        "nodes": nodes,
        "edges": edges,
    }


@router.post("/{project_id}/ontology/snapshot")
async def create_ontology_snapshot(project_id: str, payload: dict = None):
    """Save the current ontology as a named snapshot (for later diff)."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    name = (payload or {}).get("name", "").strip() or f"snapshot-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    # Re-use get_ontology aggregation
    onto = await get_ontology(project_id)
    snap_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    await ontology_snapshots.insert_one({
        "id": snap_id, "project_id": project_id, "name": name,
        "nodes": onto["nodes"], "edges": onto["edges"], "stats": onto["stats"],
        "created_at": now,
    })
    return {"snapshot_id": snap_id, "name": name, "created_at": now, "stats": onto["stats"]}


@router.get("/{project_id}/ontology/snapshots")
async def list_ontology_snapshots(project_id: str):
    rows = await ontology_snapshots.find(
        {"project_id": project_id}, {"_id": 0, "nodes": 0, "edges": 0}
    ).sort("created_at", -1).to_list(100)
    return {"snapshots": rows, "count": len(rows)}


@router.delete("/{project_id}/ontology/snapshot/{snapshot_id}")
async def delete_ontology_snapshot(project_id: str, snapshot_id: str):
    r = await ontology_snapshots.delete_one({"id": snapshot_id, "project_id": project_id})
    if r.deleted_count == 0:
        raise HTTPException(404, "Snapshot not found")
    return {"ok": True}


@router.get("/{project_id}/ontology/diff")
async def diff_ontology(project_id: str, a: str = "", b: str = "current"):
    """Diff two ontology states.
    - `a` is a snapshot_id (required)
    - `b` is either another snapshot_id or the literal "current" (default)
    Returns added / removed / unchanged nodes and edges.
    """
    if not a:
        raise HTTPException(400, "query param 'a' (snapshot_id) is required")
    snap_a = await ontology_snapshots.find_one({"id": a, "project_id": project_id}, {"_id": 0})
    if not snap_a:
        raise HTTPException(404, "Snapshot A not found")
    if b == "current":
        snap_b = await get_ontology(project_id)
        snap_b_name = "Current"
    else:
        snap_b = await ontology_snapshots.find_one({"id": b, "project_id": project_id}, {"_id": 0})
        if not snap_b:
            raise HTTPException(404, "Snapshot B not found")
        snap_b_name = snap_b.get("name", "")

    a_node_ids = {n["id"] for n in snap_a.get("nodes", [])}
    b_node_ids = {n["id"] for n in snap_b.get("nodes", [])}
    a_edges = {(e["source"], e["target"], e.get("kind", "")) for e in snap_a.get("edges", [])}
    b_edges = {(e["source"], e["target"], e.get("kind", "")) for e in snap_b.get("edges", [])}

    added_nodes = [n for n in snap_b.get("nodes", []) if n["id"] not in a_node_ids]
    removed_nodes = [n for n in snap_a.get("nodes", []) if n["id"] not in b_node_ids]
    unchanged_node_ids = a_node_ids & b_node_ids

    added_edges = [{"source": s, "target": t, "kind": k} for (s, t, k) in (b_edges - a_edges)]
    removed_edges = [{"source": s, "target": t, "kind": k} for (s, t, k) in (a_edges - b_edges)]

    by_type_delta: Dict[str, int] = {}
    for n in added_nodes:
        by_type_delta[n["type"]] = by_type_delta.get(n["type"], 0) + 1
    for n in removed_nodes:
        by_type_delta[n["type"]] = by_type_delta.get(n["type"], 0) - 1

    return {
        "a": {"id": a, "name": snap_a.get("name", ""), "created_at": snap_a.get("created_at", "")},
        "b": {"id": b, "name": snap_b_name, "created_at": snap_b.get("created_at", "")},
        "summary": {
            "added_nodes": len(added_nodes), "removed_nodes": len(removed_nodes),
            "unchanged_nodes": len(unchanged_node_ids),
            "added_edges": len(added_edges), "removed_edges": len(removed_edges),
            "by_type_delta": by_type_delta,
        },
        "added_nodes": added_nodes, "removed_nodes": removed_nodes,
        "added_edges": added_edges, "removed_edges": removed_edges,
    }


# ─────────────────────────────────────────────────────────────────────────
# Business-Domain Ontology (deterministic clustering + LLM enrichment).
# Replaces the raw code-level Class/Table/Column graph for users who want
# to see real-world concepts (Citizen, Loan Application, Invoice, …).
# ─────────────────────────────────────────────────────────────────────────
import asyncio
import time as _time
from typing import Any as _Any

_BIZ_JOBS: Dict[str, Dict[str, _Any]] = {}
_BIZ_JOB_TTL = 60 * 30  # 30 minutes


def _biz_gc():
    now = _time.time()
    for k in [k for k, v in _BIZ_JOBS.items() if v.get("ended_at") and now - v["ended_at"] > _BIZ_JOB_TTL]:
        _BIZ_JOBS.pop(k, None)


async def _build_business_ontology(project_id: str, force: bool = False) -> dict:
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)
    if not entities:
        raise HTTPException(400, "KB is empty — upload files and build the KB first.")

    kb_hash = compute_kb_hash(entities)
    cached = await business_ontologies.find_one({"project_id": project_id}, {"_id": 0})
    if cached and not force and cached.get("kb_hash") == kb_hash and cached.get("payload"):
        return {**cached["payload"], "cached": True, "kb_hash": kb_hash,
                "generated_at": cached.get("generated_at")}

    clusters_pack = cluster_entities(entities)
    llm_result = await enrich_with_llm(clusters_pack, proj, project_id)
    payload = compose_business_ontology(clusters_pack, llm_result)

    now = datetime.now(timezone.utc).isoformat()
    await business_ontologies.update_one(
        {"project_id": project_id},
        {"$set": {
            "project_id": project_id,
            "kb_hash": kb_hash,
            "payload": payload,
            "generated_at": now,
            "source": payload.get("source"),
        }},
        upsert=True,
    )
    return {**payload, "cached": False, "kb_hash": kb_hash, "generated_at": now}


async def _biz_run(project_id: str, job_id: str, force: bool):
    try:
        _BIZ_JOBS[job_id]["status"] = "running"
        result = await _build_business_ontology(project_id, force=force)
        _BIZ_JOBS[job_id].update({
            "status": "done",
            "result": result,
            "ended_at": _time.time(),
            "progress": 1.0,
        })
    except Exception as exc:
        _BIZ_JOBS[job_id].update({
            "status": "error",
            "error": str(exc)[:500],
            "ended_at": _time.time(),
        })


@router.get("/{project_id}/business-ontology")
async def get_business_ontology(project_id: str):
    """Return the cached business-domain ontology for the project.
    Returns 404 if it has never been generated — call jobs/start first."""
    cached = await business_ontologies.find_one({"project_id": project_id}, {"_id": 0})
    if not cached or not cached.get("payload"):
        raise HTTPException(404, "No business ontology cached yet — call jobs/start first.")
    # Compare hash against current KB to surface staleness
    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)
    current_hash = compute_kb_hash(entities) if entities else ""
    stale = bool(current_hash and current_hash != cached.get("kb_hash"))
    return {
        **cached["payload"],
        "cached": True,
        "kb_hash": cached.get("kb_hash"),
        "current_kb_hash": current_hash,
        "stale": stale,
        "generated_at": cached.get("generated_at"),
    }


@router.post("/{project_id}/business-ontology/jobs/start")
async def start_business_ontology_job(project_id: str, payload: dict | None = None):
    """Kick off a background build of the business ontology. Returns a job_id
    the client can poll. If a fresh cached copy exists and `force` is false,
    returns the cached result immediately under `result` instead."""
    force = bool((payload or {}).get("force", False))
    _biz_gc()

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    entities_count = await kb_entities.count_documents({"project_id": project_id})
    if entities_count == 0:
        raise HTTPException(400, "KB is empty — upload files and build the KB first.")

    # If we already have a fresh cache, skip the job entirely.
    if not force:
        entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)
        current_hash = compute_kb_hash(entities)
        cached = await business_ontologies.find_one({"project_id": project_id}, {"_id": 0})
        if cached and cached.get("kb_hash") == current_hash and cached.get("payload"):
            jid = uuid.uuid4().hex
            _BIZ_JOBS[jid] = {
                "id": jid, "project_id": project_id, "status": "done",
                "started_at": _time.time(), "ended_at": _time.time(),
                "progress": 1.0,
                "result": {**cached["payload"], "cached": True,
                           "kb_hash": current_hash, "generated_at": cached.get("generated_at")},
            }
            return {"job_id": jid, "status": "done", "cached": True}

    jid = uuid.uuid4().hex
    _BIZ_JOBS[jid] = {
        "id": jid, "project_id": project_id, "status": "queued",
        "started_at": _time.time(), "progress": 0.05,
    }
    asyncio.create_task(_biz_run(project_id, jid, force=force))
    return {"job_id": jid, "status": "queued", "cached": False}


@router.get("/{project_id}/business-ontology/jobs/{job_id}")
async def get_business_ontology_job(project_id: str, job_id: str):
    j = _BIZ_JOBS.get(job_id)
    if not j or j.get("project_id") != project_id:
        raise HTTPException(404, "Job not found (or expired)")
    return {
        "job_id": job_id,
        "status": j.get("status"),
        "progress": j.get("progress", 0.0),
        "error": j.get("error"),
        "result": j.get("result"),
    }


@router.post("/{project_id}/business-ontology/regenerate")
async def regenerate_business_ontology(project_id: str):
    """Force a fresh deterministic + LLM run (sync). Prefer jobs/start for big KBs."""
    return await _build_business_ontology(project_id, force=True)


@router.get("/{project_id}/glossary")
async def get_glossary(project_id: str):
    """Return key terms for type-ahead suggestions."""
    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(2000)
    terms = set()
    for e in entities:
        if e.get("name"):
            terms.add(e["name"])
        for m in (e.get("methods") or []):
            if m.get("name"):
                terms.add(m["name"])
        for c in (e.get("columns") or []):
            if c.get("name"):
                terms.add(c["name"])
    return {"terms": sorted(terms)[:500]}


@router.get("/{project_id}/owl-export")
async def download_owl(project_id: str):
    """Download the KB context bundle (YAML) for Stage 2 / Stage 3 consumption.

    Path is kept as `/owl-export` for backwards-compat with existing
    `lib/api.js::owlExportUrl` callers + the data-testid in `UploadPanel.jsx`.
    The payload is now plain YAML — not JSON-LD / OWL — per the user
    directive: *"do not expect owl script. take it as yaml script. and use
    yml as extractor."*
    """
    from fastapi.responses import Response

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    entities = await kb_entities.find(
        {"project_id": project_id}, {"_id": 0}
    ).to_list(100000)

    srs_doc      = await srs_documents.find_one(
        {"project_id": project_id}, {"_id": 0}
    )
    srs_sections = (srs_doc or {}).get("sections", {})

    yaml_text = export_kb_yaml(proj, entities, srs_sections)

    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={
            "Content-Disposition":
                f'attachment; filename="kb_context_{project_id}.yml"',
        },
    )



# ============================================================
# Target-stack suggestions  (knowledge-graph driven)
# ============================================================
#
# Surfaces top-N candidate MODERN stacks derived from the detected legacy
# tech + KB entity stats. The user picks one; the chosen `label` is then
# written verbatim into `project.target_tech`, where the existing target-
# stack guardrails / SRS / Architecture / CodeGen pipelines pick it up.
# Pure additive endpoint — does NOT mutate KB or stage_context.
# ------------------------------------------------------------
@router.get("/{project_id}/target-stack/suggestions")
async def get_target_stack_suggestions(project_id: str, top_n: int = 3):
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    detected = proj.get("detected_tech") or {}
    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0, "stats": 1})
    kb_stats = (toon_doc or {}).get("stats") or {}

    suggestions = suggest_target_stacks(detected, kb_stats, top_n=max(3, int(top_n or 3)))

    # Mark the currently-selected stack so the UI can pre-tick it.
    current = (proj.get("target_tech") or "").strip()
    for s in suggestions:
        s["current"] = (s["label"] == current)

    return {
        "project_id": project_id,
        "detected_tech": {
            "language":   detected.get("language", ""),
            "frameworks": detected.get("frameworks", []),
            "database":   detected.get("database", ""),
            "summary":    detected.get("summary", ""),
        },
        "kb_stats": {
            "classes": kb_stats.get("classes", 0),
            "tables":  kb_stats.get("tables", 0),
            "routes":  kb_stats.get("routes", 0),
        },
        "current_target_tech": current,
        "suggestions": suggestions,
    }


@router.post("/{project_id}/target-stack/select")
async def select_target_stack(project_id: str, payload: dict):
    """Apply a chosen target stack to the project.

    Body: { "label": "...", "id": "...", "rationale": "..." }
    Either `label` or `id` (resolved against fresh suggestions) is required.
    The chosen label runs through `validate_target_stack` so we never
    persist a disallowed hybrid (e.g. \"PHP FastAPI\").
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    label = (payload or {}).get("label") or ""
    sid   = (payload or {}).get("id") or ""

    if not label and sid:
        detected = proj.get("detected_tech") or {}
        toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0, "stats": 1})
        kb_stats = (toon_doc or {}).get("stats") or {}
        for s in suggest_target_stacks(detected, kb_stats, top_n=8):
            if s["id"] == sid:
                label = s["label"]
                payload.setdefault("rationale", s.get("rationale", ""))
                payload.setdefault("kind", s.get("kind", ""))
                payload.setdefault("pattern", s.get("pattern", ""))
                break

    if not label:
        raise HTTPException(400, "label (or a resolvable id) is required")

    detected_lang = (proj.get("detected_tech") or {}).get("language", "") or ""
    verdict = validate_target_stack(label, detected_language=detected_lang)
    safe_label = label if verdict["valid"] else verdict["suggested"]

    now = datetime.now(timezone.utc).isoformat()
    selection_meta = {
        "selected_label":  safe_label,
        "submitted_label": label,
        "selected_id":     sid,
        "rationale":       (payload or {}).get("rationale", ""),
        "kind":            (payload or {}).get("kind", ""),
        "pattern":         (payload or {}).get("pattern", ""),
        "valid":           verdict["valid"],
        "validation":      verdict,
        "selected_at":     now,
    }

    await projects.update_one(
        {"id": project_id},
        {"$set": {
            "target_tech":          safe_label,
            "target_stack_selection": selection_meta,
            "updated_at":           now,
        }},
    )

    # Audit trail — every state change should leave a trace per AGENTS.md §8.
    try:
        from db import audit_log as _audit
        await _audit.insert_one({
            "action":     "project.target_stack.select",
            "project_id": project_id,
            "at":         now,
            "details":    selection_meta,
        })
    except Exception:
        pass  # non-blocking

    updated = await projects.find_one({"id": project_id}, {"_id": 0})
    return {
        "ok": True,
        "project": updated,
        "selection": selection_meta,
    }




# ============================================================
# Module Inventory — generic import (Excel / CSV / JSON)
# ============================================================
@router.post("/import-module-inventory")
async def import_module_inventory(
    project_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Import module inventory from .xlsx / .csv / .json / .sql. Idempotent — replaces previous MODULE/COMPONENT_MAP entities."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    fname = file.filename or "inventory"
    ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
    if ext not in ("xlsx", "xls", "csv", "json", "sql"):
        raise HTTPException(400, "Supported formats: .xlsx, .csv, .json, .sql")

    content = await file.read()
    entities = parse_module_inventory(fname, content)
    if not entities:
        raise HTTPException(
            422,
            "Could not parse the file. Ensure it contains module and component/table mapping data. "
            "Supported formats: Excel (.xlsx), CSV (.csv), JSON (.json), SQL (.sql).",
        )

    modules = [e for e in entities if e.get("type") == "MODULE"]
    comp_maps = [e for e in entities if e.get("type") == "COMPONENT_MAP"]
    now = datetime.now(timezone.utc).isoformat()

    # Idempotent replace
    await kb_entities.delete_many({
        "project_id": project_id,
        "type": {"$in": ["MODULE", "COMPONENT_MAP"]},
    })

    BATCH = 500
    for start in range(0, len(entities), BATCH):
        batch = entities[start:start + BATCH]
        docs = [{**e, "project_id": project_id, "file_id": "module_inventory"} for e in batch]
        if docs:
            await kb_entities.insert_many(docs)

    summary_text = generate_module_text_summary(entities)
    chunks = chunk_text(summary_text) if summary_text else []

    # Replace previous module_inventory file + chunks
    await kb_files.delete_many({"project_id": project_id, "filetype": "module_inventory"})
    await kb_chunks.delete_many({"project_id": project_id, "file_id": "module_inventory"})

    kb_file = KBFile(
        project_id=project_id,
        filename=fname,
        filetype="module_inventory",
        size=len(content),
        chunk_count=len(chunks),
        entity_count=len(entities),
        status="processed",
    )
    await kb_files.insert_one(kb_file.model_dump())

    if chunks:
        chunk_docs = [
            {
                "id": f"module_inventory:{i}",
                "project_id": project_id,
                "file_id": "module_inventory",
                "chunk_index": i,
                "content": c,
                "created_at": now,
            }
            for i, c in enumerate(chunks)
        ]
        await kb_chunks.insert_many(chunk_docs)
        try:
            await qdrant_index(project_id, [
                {
                    "id": f"module_inventory:{i}",
                    "content": c,
                    "filename": fname,
                    "filetype": "module_inventory",
                }
                for i, c in enumerate(chunks)
            ])
        except Exception:
            pass  # non-blocking

    # Rebuild TOON to include the # MODULES section
    all_entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(200000)
    # iter-14.23 — keep ROUTE field-evidence applied after re-aggregation
    # (idempotent — persisted ROUTEs are already enriched from the build pass).
    try:
        enrich_routes(all_entities)
    except Exception:  # noqa: BLE001
        pass
    toon = serialise(all_entities)
    stats = aggregate_stats(all_entities)
    # iter 13.7 — pull detected language off the project so the summary
    # toast says e.g. "Java classes" instead of the legacy "PHP classes".
    _proj = await projects.find_one({"id": project_id}, {"_id": 0, "detected_tech": 1, "source_tech": 1}) or {}
    _lang = ((_proj.get("detected_tech") or {}).get("language")
             or (_proj.get("source_tech") or "").split("/")[0].strip())
    summary = summarise(all_entities, stats, language=_lang)
    await kb_toon.update_one(
        {"project_id": project_id},
        {"$set": {
            "project_id": project_id,
            "toon": toon,
            "summary": summary,
            "stats": stats,
            "updated_at": now,
        }},
        upsert=True,
    )

    return {
        "ok": True,
        "modules": len(modules),
        "component_maps": len(comp_maps),
        "chunks": len(chunks),
        "format_detected": ext,
        "message": (
            f"Imported {len(modules)} modules and {len(comp_maps)} component mappings "
            f"from {ext.upper()} file. TOON rebuilt."
        ),
    }


@router.get("/{project_id}/module-traceability")
async def get_module_traceability(project_id: str):
    """Return two views: user-imported modules vs. auto-detected domain groupings."""
    from collections import defaultdict

    entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(200000)

    user_modules = [
        {
            "name": e["name"],
            "component_count": e.get("component_count", 0),
            "table_ref_count": e.get("table_ref_count", 0),
            "source_format": e.get("source_format", ""),
            "tables": (e.get("tables") or [])[:15],
            "description": e.get("description", ""),
        }
        for e in entities if e.get("type") == "MODULE"
    ]
    user_modules.sort(key=lambda x: x["table_ref_count"], reverse=True)

    auto_domains: dict = defaultdict(lambda: {"classes": [], "tables": [], "count": 0})
    for e in entities:
        if e.get("type") == "CLASS":
            touched = set()
            for m in (e.get("methods") or []):
                for t in (m.get("tables") or []):
                    p = t.split("_")
                    if len(p) > 1:
                        touched.add(p[0])
            domain = next(iter(touched), "core") if touched else "core"
            auto_domains[domain]["classes"].append(e["name"])
            auto_domains[domain]["count"] += 1
        elif e.get("type") == "TABLE":
            p = e["name"].split("_")
            domain = p[0] if len(p) > 1 else "other"
            auto_domains[domain]["tables"].append(e["name"])

    auto_list = sorted(
        [
            {
                "domain": k,
                "class_count": len(v["classes"]),
                "table_count": len(v["tables"]),
                "sample_classes": v["classes"][:5],
                "sample_tables": v["tables"][:5],
            }
            for k, v in auto_domains.items()
            if len(v["classes"]) + len(v["tables"]) > 1
        ],
        key=lambda x: x["class_count"] + x["table_count"],
        reverse=True,
    )

    return {
        "user_modules": user_modules,
        "auto_modules": auto_list,
        "has_user_import": len(user_modules) > 0,
    }


# ──────────────────────────────────────────────────────────────────────
# iter-13.17 — Deep legacy-logic analysis routes.
# Runs after Build KB, BEFORE Generate SRS. Output is auto-injected into
# every SRS section prompt AND persisted into StageContext on Discovery
# freeze so Stage-4 CodeGen has the same structured logic to rebuild.
# ──────────────────────────────────────────────────────────────────────
from kb.legacy_analyzer import run_legacy_analysis
from db import legacy_analysis as legacy_analysis_col


@router.post("/{project_id}/analyze-logic")
async def analyze_legacy_logic(project_id: str, payload: dict | None = None):
    """Run the deep-analysis pass over the parsed legacy codebase.

    Body (optional):
      {"model": "<override>", "force": true}

    Returns the persisted analysis doc. Safe to call repeatedly — by default
    re-uses a cached analysis younger than 24 h.
    """
    payload = payload or {}
    model = payload.get("model") or ""  # iter-13.30: Console resolves via AGENT_COMPLEXITY
    force = bool(payload.get("force"))

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    toon = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    if not toon or not (toon.get("toon") or "").strip():
        raise HTTPException(
            400,
            "KB not built — run Build KB first so the analyzer has something to read",
        )

    try:
        doc = await run_legacy_analysis(project_id, model=model, force=force)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Legacy analysis failed for %s", project_id)
        raise HTTPException(500, f"Legacy analysis failed: {exc}")

    a = doc.get("analysis", {}) or {}
    return {
        "ok": True,
        "version": doc.get("version", 1),
        "model_used": doc.get("model_used"),
        "tokens": doc.get("tokens", 0),
        "counts": {
            "workflows": len(a.get("workflows") or []),
            "business_rules": len(a.get("business_rules") or []),
            "integrations": len(a.get("integrations") or []),
            "user_journeys": len(a.get("user_journeys") or []),
            "actors": len(a.get("actors") or []),
            "domain_entities": len(a.get("domain_entities") or []),
        },
        "coverage": doc.get("coverage", {}),
        "updated_at": doc.get("updated_at"),
    }


@router.get("/{project_id}/logic-analysis")
async def get_legacy_logic(project_id: str):
    """Return the persisted analysis doc verbatim (or `exists: False`)."""
    doc = await legacy_analysis_col.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        return {"project_id": project_id, "exists": False}
    return {"project_id": project_id, "exists": True, **doc}


# ----------------------------------------------------------------------------
# iter-13.95 — Cross-project leak diagnostic + cache busters
# ----------------------------------------------------------------------------

@router.get("/{project_id}/leak-scan")
async def scan_for_foreign_paths(project_id: str):
    """Audit every collection that feeds the SRS prompt and report any
    file-path-shaped token whose basename is NOT present in this
    project's scope-checked kb_files index.

    Use this when a user reports "the SRS is citing files that don't
    belong to my project". Output enumerates which upstream collection
    is polluted so the fix is targeted.
    """
    from routes.srs import _PATH_REGEX, _allowed_basenames
    from db import (
        kb_files as _kbf,
        kb_chunks as _kbc,
        kb_toon as _kbt,
        srs_documents as _srs,
        messages as _msg,
        legacy_analysis as _la,
    )

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    tid = (proj.get("tenant_id") or "").strip()
    flt: dict = {"project_id": project_id}
    if tid:
        flt["tenant_id"] = tid

    # Build the allowed-basename set.
    names: list[str] = []
    async for f in _kbf.find(flt, {"_id": 0, "filename": 1}):
        n = (f.get("filename") or "").strip()
        if n:
            names.append(n)
    allowed = _allowed_basenames(names)

    def _scan(text: str) -> list[str]:
        if not text or not allowed:
            return []
        out: set[str] = set()
        for m in _PATH_REGEX.finditer(text):
            tok = m.group(1)
            bn = re.split(r"::|:", tok, maxsplit=1)[0]
            bn = bn.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if bn not in allowed:
                out.add(tok)
        return sorted(out)[:40]

    # 1. legacy_analysis — high-leak vector
    la_doc = await _la.find_one({"project_id": project_id}, {"_id": 0})
    la_foreign = _scan(json.dumps(la_doc.get("analysis") or {})) if la_doc else []

    # 2. kb_toon — should be clean (built locally) but worth checking
    toon_doc = await _kbt.find_one({"project_id": project_id}, {"_id": 0, "toon": 1})
    toon_foreign = _scan((toon_doc or {}).get("toon", "") or "")

    # 3. chat messages — user paste-ins
    convo = []
    async for m in _msg.find({"project_id": project_id}, {"_id": 0, "content": 1}).limit(500):
        c = (m.get("content") or "")
        if c:
            convo.append(c)
    convo_foreign = _scan("\n".join(convo))

    # 4. srs_documents — what the user is actually seeing
    srs_doc = await _srs.find_one({"project_id": project_id}, {"_id": 0, "sections": 1})
    srs_foreign: dict[str, list[str]] = {}
    if srs_doc:
        for k, v in (srs_doc.get("sections") or {}).items():
            f = _scan(v.get("content", "") if isinstance(v, dict) else str(v or ""))
            if f:
                srs_foreign[k] = f

    # 5. kb_chunks — if scan ingested foreign files mis-tagged with this
    #    project_id, the chunk content itself will be foreign. Sample.
    chunk_foreign: list[str] = []
    async for c in _kbc.find(flt, {"_id": 0, "content": 1}).limit(200):
        chunk_foreign.extend(_scan(c.get("content", "") or ""))
        if len(chunk_foreign) > 40:
            break

    return {
        "project_id": project_id,
        "tenant_id": tid,
        "allowed_basename_count": len(allowed),
        "kb_file_count": len(names),
        "foreign_paths_by_source": {
            "legacy_analysis": la_foreign,
            "kb_toon": toon_foreign,
            "chat_messages": _scan("\n".join(convo)),
            "kb_chunks_sample": sorted(set(chunk_foreign))[:40],
            "srs_documents": srs_foreign,
        },
        "remediation": {
            "legacy_analysis": (
                "POST /api/kb/{pid}/invalidate-analysis to drop the "
                "stale cached digest, then regenerate SRS."
                if la_foreign else "clean"
            ),
            "srs_documents": (
                "Regenerate SRS after invalidating the analysis cache."
                if srs_foreign else "clean"
            ),
            "kb_chunks": (
                "Re-scan the legacy folder — foreign files were ingested "
                "into this project's KB at scan time."
                if chunk_foreign else "clean"
            ),
        },
    }


@router.post("/{project_id}/invalidate-analysis")
async def invalidate_legacy_analysis_cache(project_id: str):
    """Drop this project's cached `legacy_analysis` doc so the next SRS
    generation rebuilds it from scratch against the (hardened) prompt.

    Required when the cached digest already carries foreign paths (the
    SRS sanitizer will now redact them, but the underlying Mongo doc
    stays polluted until this is called).
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    res = await legacy_analysis_col.delete_one({"project_id": project_id})
    from db import audit_log as _audit
    await _audit.insert_one({
        "action": "kb.invalidate_analysis",
        "project_id": project_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "details": {"deleted": res.deleted_count},
    })
    return {"ok": True, "deleted": res.deleted_count}


# ----------------------------------------------------------------------------
# iter-13.99 — Project-isolated workspace introspection
# ----------------------------------------------------------------------------

@router.get("/{project_id}/workspace-info")
async def get_workspace_info(project_id: str):
    """Return the project's iter-13.99 isolated workspace metadata.

    The workspace root is a per-(tenant, project) pseudo-path under
    `/lama-workspaces/{tenant_id}__{project_id}/`. Every file citation
    in any LAMA-generated SRS / architecture / codegen output is now
    REQUIRED to be rooted at this prefix; anything else gets redacted
    server-side. Use this endpoint to verify the isolation is configured
    correctly before regenerating outputs.
    """
    from routes.srs import _workspace_root, _workspace_slug
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    tid = (proj.get("tenant_id") or "").strip()
    root = _workspace_root(tid, project_id)
    flt: dict = {"project_id": project_id}
    if tid:
        flt["tenant_id"] = tid
    kb_file_count = await kb_files.count_documents(flt)
    return {
        "project_id": project_id,
        "tenant_id": tid,
        "workspace_root": root,
        "workspace_id": f"{_workspace_slug(tid)}__{_workspace_slug(project_id)}",
        "kb_file_count": kb_file_count,
        "isolation_contract": (
            "Every file citation in this project's LAMA outputs (SRS / "
            "Architecture / CodeGen) MUST be rooted at `workspace_root` "
            "above OR be a bare basename mentioned in prose that matches "
            "one of this project's kb_files. Any other path is rejected "
            "by the iter-13.99 hard-prefix guard."
        ),
    }


# ----------------------------------------------------------------------------
# iter-13.30 — Business-Rule coverage inspection
# ----------------------------------------------------------------------------

@router.get("/{project_id}/br-coverage")
async def get_br_coverage(project_id: str, stage: str = "srs"):
    """Return BR-reflection coverage for a stage WITHOUT freezing.

    Lets the UI surface the gap before the user clicks Freeze. Query
    param ``stage`` ∈ {"srs", "architecture", "codegen"} chooses which
    artifacts to scan.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    from kb.br_tracker import load_business_rules, compute_coverage
    rules = await load_business_rules(project_id)
    texts: list[str] = []
    stage_lc = (stage or "").lower()
    if stage_lc == "srs":
        srs_doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
        texts = [v or "" for v in ((srs_doc or {}).get("sections") or {}).values()]
    elif stage_lc == "architecture":
        from db import arch_documents
        async for d in arch_documents.find({"project_id": project_id}, {"_id": 0, "content": 1}):
            texts.append(d.get("content", "") or "")
    elif stage_lc == "codegen":
        from db import codegen_files
        async for d in codegen_files.find({"project_id": project_id}, {"_id": 0, "content": 1}):
            texts.append(d.get("content", "") or "")
    else:
        raise HTTPException(400, f"Unknown stage '{stage}'. Use srs|architecture|codegen.")
    coverage = compute_coverage(rules, *texts)
    return {
        "project_id": project_id,
        "stage": stage_lc,
        **coverage,
        "thumb_rule": "100% legacy business rules must appear in this stage's artifacts.",
    }



# ─────────────────────────────────────────────────────────────────────────────
# iter-13.63 — Git ingest + module-selection (Discovery → Source Files)
#
# Three small endpoints layered on top of the existing folder-scan pipeline:
#
#   POST /api/kb/{pid}/clone-git
#       Clone a public/private repo via system `git` (depth=1 by default),
#       inject `token` into the URL if provided, then funnel the cloned dir
#       into the existing `_ingest_folder` plumbing (kb_files + chunks +
#       OWL extraction). Token is NEVER persisted — read-once, inject, drop.
#
#   GET  /api/kb/{pid}/modules-tree
#       Build a hierarchical tree of folders/files from `kb_files.filename`,
#       merged with `kb_module_selection.excluded` so the UI can render
#       checkbox state. Each node carries {kind, file_count, included}.
#
#   PUT  /api/kb/{pid}/modules-selection
#       Persist `excluded` prefixes and stamp matching `kb_files` with
#       `excluded=true` so chat-RAG / build / SRS prompts can filter them out.
#
# No new top-level deps. `git` is already present in `python:3.11-slim` base
# image's recommended add-ons; if it isn't installed we surface a clear
# error from the clone endpoint instead of crashing.
# ─────────────────────────────────────────────────────────────────────────────
import asyncio as _asyncio
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile
from urllib.parse import quote as _urlquote, urlparse as _urlparse


def _inject_git_token(url: str, token: str, username: str = "") -> str:
    """Inject a personal-access-token into an HTTPS clone URL.

    Provider quirks:
      • GitHub accepts any non-empty username with the PAT, or `x-access-token`
        for a fine-grained token. We use `x-access-token` as a safe default.
      • GitLab REJECTS arbitrary usernames — it validates them against the
        actual account. For PATs it expects `oauth2` as the username; for
        Project/Group Access Tokens it expects the token's *name* (or `oauth2`
        also works). Passing an unknown username (e.g. `lama`) yields the
        infamous "HTTP Basic: Access denied" error.
      • Bitbucket Cloud accepts `x-token-auth` for repository tokens, and
        the account username for App Passwords.

    iter-14.2 — Default username changed from `lama` (rejected by GitLab)
    to `oauth2`, which is accepted by GitHub, GitLab, and Bitbucket alike
    when paired with a PAT. Callers can still override via the `username`
    parameter (e.g. for GitLab deploy tokens, which need the token *name*).

    SSH URLs are returned unchanged — those rely on the system's SSH keys.
    """
    if not token:
        return url
    try:
        p = _urlparse(url)
    except Exception:
        return url
    if p.scheme not in ("http", "https"):
        return url  # ssh:// or git@... — token is meaningless
    user = _urlquote(username or "oauth2", safe="")
    tok = _urlquote(token, safe="")
    return f"{p.scheme}://{user}:{tok}@{p.netloc}{p.path}{('?' + p.query) if p.query else ''}"


def _redact_url(url: str) -> str:
    """Strip embedded credentials so we never log/store secrets."""
    try:
        p = _urlparse(url)
        if "@" in p.netloc:
            host = p.netloc.split("@", 1)[1]
            return f"{p.scheme}://{host}{p.path}"
    except Exception:
        pass
    return url


async def _git_clone(url: str, branch: str, depth: int, dest: str) -> str:
    """Run `git clone` and return the resolved commit SHA. Raises HTTPException
    on failure so the caller can surface a clean error to the UI.

    iter-13.90 — Falls back to a pure-python `dulwich` clone when the
    system `git` binary is missing (slim runner images, hardened k8s
    nodes, local dev on a server without git). The system git path is
    preferred when available because it's faster, supports shallow
    clones, and has better credential helpers.

    iter-14.4 — Speed knobs for Docker Desktop-on-Mac users who reported
    clone-inside-container being noticeably slower than clone-on-host
    for large / corporate repos. Adds:
      • `--filter=blob:none`  — partial clone; only fetch tree + commits
        eagerly, and lazy-download blobs on demand. Huge win on repos
        with big binaries / long history because we only need HEAD.
        Falls back gracefully on servers that don't advertise the
        `filter` capability (git prints a warning and clones fully).
      • `--no-tags`           — skips fetching every tag ref (large
        monorepos can have thousands).
      • `--jobs=8`            — parallel object fetch for the pack.
      • `-c protocol.version=2` — HTTP/1.1 chattiness reduction; the
        v2 wire protocol batches ref-advertisement.
      • `-c http.postBuffer=524288000` — avoid 501-post-buffer errors
        on TLS-intercepting corporate proxies (Zscaler / Netskope).
      • `-c core.compression=0` — skip zlib recompression on write.
        The pack is already compressed on the wire; recompressing on
        disk costs CPU (which is scarce inside a Mac VM) with no
        wire savings.
      • `-c gc.auto=0`        — skip the auto-gc that a fresh clone
        would otherwise trigger over the newly-written pack.
    All flags are safe to pass to any git ≥ 2.20 (2019) — LAMA's slim
    Debian image ships 2.39 (2023), so we're comfortably compatible.
    """
    fast_flags = [
        "-c", "protocol.version=2",
        "-c", "http.postBuffer=524288000",
        "-c", "core.compression=0",
        "-c", "gc.auto=0",
    ]
    cmd = ["git", *fast_flags, "clone",
           "--quiet",
           "--filter=blob:none",
           "--no-tags",
           "--jobs=8",
           "--depth", str(max(1, int(depth or 1)))]
    if branch:
        cmd += ["--branch", branch, "--single-branch"]
    cmd += [url, dest]

    # Honour LAMA's existing TLS knobs so corporate MITM proxies (Zscaler /
    # Netskope) work without per-call configuration. Mirrors what
    # llm.fabric_call() does for httpx.
    env = os.environ.copy()
    disable = (env.get("LAMA_DISABLE_SSL_VERIFY", "") or "").strip().lower()
    if disable in ("1", "true", "yes", "on"):
        env["GIT_SSL_NO_VERIFY"] = "true"
    ca_bundle = (env.get("LAMA_CA_BUNDLE", "") or "").strip()
    if ca_bundle and os.path.isfile(ca_bundle):
        env["GIT_SSL_CAINFO"] = ca_bundle
    # Suppress credential prompts — fail fast instead of blocking forever.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/echo")

    # iter-14.4 — Honour host proxy env-vars if the operator set them on
    # `docker compose up` (e.g. corporate laptops behind Zscaler). Docker
    # doesn't forward the host's HTTP proxy into the container by default,
    # so a Mac dev whose host shell has `https_proxy=…` set (and where
    # git-on-host is therefore fast) would otherwise route directly and
    # slowly / not at all through the container network. We accept both
    # upper- and lower-case variants because different tools honour
    # different conventions.
    for _var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "no_proxy"):
        _val = os.environ.get(_var)
        if _val and _var not in env:
            env[_var] = _val

    # iter-14.3 — Hard timeout on the git subprocess (default 300s / 5min,
    # override via LAMA_GIT_CLONE_TIMEOUT env). Without this a slow /
    # hanging upstream (corporate GitLab behind a proxy, DNS stalls, TLS
    # renegotiation loops) would block indefinitely, until the frontend's
    # axios timeout eventually gives up — leaving a zombie git process
    # AND a confusing "timeout" toast for the user with no server-side
    # error message. Now we kill the process on timeout and surface a
    # 504 with an actionable hint.
    try:
        clone_timeout = float(os.environ.get("LAMA_GIT_CLONE_TIMEOUT", "300"))
    except (TypeError, ValueError):
        clone_timeout = 300.0

    try:
        proc = await _asyncio.create_subprocess_exec(
            *cmd, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, env=env,
        )
    except FileNotFoundError:
        # System git missing — try the dulwich pure-python fallback.
        logger.warning(
            "system `git` binary not found — falling back to dulwich for "
            "clone of %s (install `git` on the host for faster / more "
            "reliable clones)", _redact_url(url),
        )
        return await _git_clone_dulwich(url, branch, depth, dest)

    try:
        _stdout, stderr = await _asyncio.wait_for(
            proc.communicate(), timeout=clone_timeout,
        )
    except _asyncio.TimeoutError:
        # Kill the hung git process (and its child fetch-pack) so we don't
        # leak subprocesses on every retry.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        logger.warning(
            "git clone TIMED OUT after %.0fs for %s — killing subprocess",
            clone_timeout, _redact_url(url),
        )
        raise HTTPException(
            504,
            f"git clone timed out after {int(clone_timeout)}s. Common causes: "
            f"(1) the git server ({_urlparse(url).netloc}) is unreachable / "
            f"very slow from this container — try `curl -I {_redact_url(url).rstrip('.git')}` "
            f"from inside the container to confirm; "
            f"(2) the repo is very large — try a shallow clone with a smaller depth, "
            f"or set the LAMA_GIT_CLONE_TIMEOUT env var to a larger value (seconds) and restart; "
            f"(3) HTTPS/TLS handshake stalling on a corporate proxy — set LAMA_DISABLE_SSL_VERIFY=1 "
            f"or LAMA_CA_BUNDLE=/path/to/ca.pem.",
        )

    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="ignore").strip()[:500]
        # Mask the token if it leaked into the error message
        msg = msg.replace(url, _redact_url(url))
        raise HTTPException(400, f"git clone failed: {msg or 'unknown error'}")
    # Resolve commit
    try:
        rev = await _asyncio.create_subprocess_exec(
            "git", "-C", dest, "rev-parse", "HEAD",
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
        )
        out, _err = await rev.communicate()
        return (out or b"").decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


async def _git_clone_dulwich(url: str, branch: str, depth: int, dest: str) -> str:
    """Pure-python clone via dulwich. Used as a fallback when the system
    `git` binary is unavailable. Runs the (blocking) dulwich call in a
    thread-pool so the asyncio loop isn't starved.

    Returns the resolved commit SHA (hex) or "" on best-effort.
    Raises HTTPException(500) with an actionable hint if dulwich itself
    is not importable (== the user has neither git binary nor dulwich).
    Raises HTTPException(400) for clone failures so the UI surfaces a
    clean error.
    """
    try:
        from dulwich import porcelain as _dulwich  # type: ignore
        from dulwich.errors import GitProtocolError, NotGitRepository  # type: ignore
    except ImportError as e:
        raise HTTPException(
            500,
            "`git` is not installed on the server AND the pure-python "
            "`dulwich` fallback is also missing. Fix by ONE of:\n"
            "  • Docker:  rebuild the image (Dockerfile installs `git` + "
            "    dulwich as of iter-13.90).\n"
            "  • Local:   `apt-get install git`  OR  "
            "    `pip install dulwich==0.22.1`  then restart uvicorn.\n"
            "  • Cloud:   install git on the runner (most managed Python "
            "    runtimes have it pre-installed; serverless platforms "
            "    often don't).",
        ) from e

    # dulwich clones are blocking — run them off the loop. Pass `depth`
    # for a shallow clone (dulwich supports depth since 0.21).
    import asyncio as _ay
    loop = _ay.get_running_loop()

    def _do_clone():
        try:
            # Suppress dulwich's verbose stderr progress output by handing
            # it a sink BytesIO — the type hint demands a writable binary
            # stream and explicit None can trip newer dulwich versions.
            import io as _io
            repo = _dulwich.clone(
                url,
                dest,
                branch=branch.encode("utf-8") if branch else None,
                depth=max(1, int(depth or 1)),
                checkout=True,
                errstream=_io.BytesIO(),
            )
            try:
                head = repo.head()
                return head.decode("ascii") if isinstance(head, bytes) else str(head)
            finally:
                try:
                    repo.close()
                except Exception:  # noqa: BLE001
                    pass
        except (GitProtocolError, NotGitRepository) as ex:
            # Re-raise as a 400 so the UI shows the actual git error.
            raise HTTPException(
                400, f"dulwich clone failed: {str(ex)[:300]}"
            ) from ex
        except Exception as ex:  # noqa: BLE001
            # Most likely auth / network / SSL — surface to the user.
            raise HTTPException(
                400, f"dulwich clone failed: {type(ex).__name__}: {str(ex)[:300]}"
            ) from ex

    sha = await loop.run_in_executor(None, _do_clone)
    logger.info(
        "dulwich fallback clone succeeded for %s @ %s (commit %s)",
        _redact_url(url), branch or "(default)", (sha or "")[:8],
    )
    return sha or ""


async def _ingest_folder_into_kb(project_id: str, folder: str, subdir: str = "", default_kind: str = "", source_id: str = "") -> tuple[list[str], list[str]]:
    """Re-use of the scan-folder loop body so /clone-git and /scan-folder share
    one ingestion path. Returns (processed_filenames, skipped_filenames).

    iter-13.69 — `default_kind` (when set to a value in VALID_KINDS) is
    applied to every file ingested in this batch; otherwise per-file
    auto-detect from extension/filename wins.

    iter-13.120 — Performance:
      • tenant_id resolved ONCE for the whole ingest (previously every
        file did a `projects.find_one`).
      • Per-file work runs with bounded concurrency via an asyncio
        Semaphore (tunable via LAMA_INGEST_CONCURRENCY, default 12).
        The old code awaited each file sequentially; on a repo with
        1000 files this is where most of the "cloning… forever" time
        was spent (git clone itself is seconds, ingest was minutes).

    iter-14.6 — `source_id` (optional): when provided, live progress is
    flushed to the matching kb_git_sources row by exact id. Previously
    the flush used `update_one(..., sort=[...])`, which Motor rejects
    (sort is only valid on find_one_and_update), so every progress
    write raised and was swallowed by the bare-except → the frontend
    poll saw `file_count=0` and `total_files=0` for the entire ingest
    and it looked like a hang.
    """
    base = folder
    if subdir:
        candidate = os.path.normpath(os.path.join(folder, subdir.lstrip("/\\")))
        if not candidate.startswith(os.path.abspath(folder)):
            raise HTTPException(400, "subdir escapes the cloned repository")
        if os.path.isdir(candidate):
            base = candidate

    dk = default_kind if default_kind in VALID_KINDS else ""

    # Resolve tenant_id once — every file gets the same value.
    _proj = await projects.find_one(
        {"id": project_id}, {"_id": 0, "tenant_id": 1},
    )
    tenant_id = ((_proj or {}).get("tenant_id") or "").strip() or "tenant_default"

    # Collect the file list first so we can dispatch with bounded concurrency.
    file_list: list[str] = []
    skipped: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            full = os.path.join(root, f)
            if _should_skip_file(f):
                skipped.append(f)
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            file_list.append(full)

    try:
        _concurrency = int(os.environ.get("LAMA_INGEST_CONCURRENCY", "12"))
    except (TypeError, ValueError):
        _concurrency = 12
    _concurrency = max(1, min(_concurrency, 32))
    sem = asyncio.Semaphore(_concurrency)
    processed: list[str] = []
    processed_lock = asyncio.Lock()
    skipped_lock = asyncio.Lock()

    # iter-13.120 — live progress. Push the current (processed, skipped,
    # total) counts back onto kb_git_sources every ~1s so the frontend
    # toast + /clone-git/status endpoint can render "Ingesting X of Y…"
    # instead of a static "Ingesting files…" spinner that gives the user
    # no feedback for the minutes-long ingest.
    total = len(file_list)
    progress = {"done": 0, "skipped": 0, "last_flush": time.monotonic()}
    # iter-14.9 — track files currently in flight so the UI can show
    # "Processing: <name>" instead of a static spinner. Bounded to
    # `_concurrency` entries by construction (added on start, removed on
    # finish inside the semaphore).
    in_flight: set[str] = set()
    in_flight_lock = asyncio.Lock()
    # Last file that finished (any status) — useful when in_flight briefly
    # empties between batches so the UI still has *something* to render.
    last_activity = {"file": ""}

    async def _flush_progress(force: bool = False) -> None:
        now_m = time.monotonic()
        if not force and (now_m - progress["last_flush"]) < 1.0:
            return
        progress["last_flush"] = now_m
        try:
            # Snapshot in_flight without holding its lock during Mongo I/O.
            current_files = sorted(in_flight)[:12]
            update = {"$set": {
                "file_count":    progress["done"],
                "skipped_count": progress["skipped"],
                "total_files":   total,
                "progress_pct":  int(100 * (progress["done"] + progress["skipped"]) / max(1, total)),
                "current_files": current_files,
                "current_file":  current_files[0] if current_files else last_activity["file"],
                "last_file":     last_activity["file"],
            }}
            # iter-14.6 — prefer exact source_id match (no `sort=` needed).
            # Motor's update_one does NOT support `sort=`; only
            # find_one_and_update does. The old code silently raised on
            # every flush, which surfaced as "progress stuck at 0" in the UI.
            if source_id:
                await kb_git_sources.update_one({"id": source_id}, update)
            else:
                await kb_git_sources.find_one_and_update(
                    {"project_id": project_id, "status": "ingesting"},
                    update,
                    sort=[("cloned_at", -1)],
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("clone-git progress flush failed: %s", e)

    try:
        _per_file_timeout = int(os.environ.get("LAMA_INGEST_PER_FILE_TIMEOUT", "60"))
    except (TypeError, ValueError):
        _per_file_timeout = 60

    async def _ingest_one_inner(full: str) -> None:
        try:
            content = await asyncio.to_thread(_read_bytes, full)
        except Exception:
            async with skipped_lock:
                skipped.append(os.path.basename(full))
                progress["skipped"] += 1
            await _flush_progress()
            return
        f = os.path.basename(full)
        try:
            filetype, text = await asyncio.to_thread(parse_file, f, content)
            chunks = await asyncio.to_thread(
                chunk_text, text, filetype=filetype, filename=f,
            )
        except Exception:
            async with skipped_lock:
                skipped.append(f)
                progress["skipped"] += 1
            await _flush_progress()
            return
        rel = os.path.relpath(full, base)
        kb_file = KBFile(
            project_id=project_id,
            filename=rel,
            filetype=filetype,
            size=len(content),
            chunk_count=len(chunks),
            status="uploaded",
            kind=dk or detect_kind(rel, filetype),
        )
        try:
            await kb_file_persist(
                kb_file, project_id, text, chunks, tenant_id=tenant_id,
            )
        except Exception:
            async with skipped_lock:
                skipped.append(f)
                progress["skipped"] += 1
            await _flush_progress()
            return
        async with processed_lock:
            processed.append(kb_file.filename)
            progress["done"] += 1
        await _flush_progress()

    async def _ingest_one(full: str) -> None:
        async with sem:
            rel_name = os.path.relpath(full, base)
            async with in_flight_lock:
                in_flight.add(rel_name)
            # Emit a flush the moment a new file starts so the UI updates
            # even if the file itself takes many seconds.
            await _flush_progress(force=True)
            try:
                # iter-14.7 — hard per-file timeout. Regex-heavy passes on
                # generated/minified files can hang a worker forever; without
                # a cap, N such files where N ≥ concurrency wedge the whole
                # ingest at "82%".
                try:
                    await asyncio.wait_for(
                        _ingest_one_inner(full), timeout=_per_file_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "clone-git ingest TIMEOUT (%ds) skipping %s",
                        _per_file_timeout, full,
                    )
                    async with skipped_lock:
                        skipped.append(os.path.basename(full))
                        progress["skipped"] += 1
                    await _flush_progress()
            finally:
                async with in_flight_lock:
                    in_flight.discard(rel_name)
                last_activity["file"] = rel_name
                await _flush_progress()

    if file_list:
        logger.info(
            "clone-git INGEST START project=%s files=%d concurrency=%d",
            project_id, len(file_list), _concurrency,
        )
        # iter-14.6 — flush the total immediately so the UI's "Ingesting X/Y"
        # toast has the denominator before the first file finishes.
        await _flush_progress(force=True)
        await asyncio.gather(*[_ingest_one(f) for f in file_list])
        await _flush_progress(force=True)
    return processed, skipped


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


@router.post("/{project_id}/clone-git")
async def clone_git_repository(project_id: str, payload: dict):
    """Clone a git repo and ingest every supported file into the KB.

    Body: { url, branch?, token?, username?, depth?, subdir?, replace? }
    Token is consumed for the clone only and never persisted.

    iter-13.83 — `replace` defaults to TRUE; cloning a different URL /
    branch / subdir than the project's stored one auto-replaces too.
    Pass `replace=false` explicitly to layer this clone on top of an
    existing KB (rare; usually a sign of a workflow mistake).
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(400, "url required")
    branch  = ((payload or {}).get("branch") or "").strip()
    token   = ((payload or {}).get("token") or "").strip()
    user    = ((payload or {}).get("username") or "").strip()
    depth   = int((payload or {}).get("depth") or 1)
    subdir  = ((payload or {}).get("subdir") or "").strip()
    # iter-13.69 — optional default kind for every cloned file
    dk_raw  = ((payload or {}).get("kind") or (payload or {}).get("default_kind") or "").strip().lower()
    default_kind = dk_raw if dk_raw in VALID_KINDS else ""

    auth_url = _inject_git_token(url, token, user)
    redacted = _redact_url(url)

    # iter-13.83 — replace decision (explicit caller intent → fallback True
    # → auto-True when source URL/branch/subdir differs from stored).
    replace_requested = "replace" in (payload or {})
    replace = _truthy((payload or {}).get("replace")) if replace_requested else True
    prior_url    = (proj.get("legacy_git_url") or "").strip()
    prior_branch = (proj.get("legacy_git_branch") or "").strip()
    if not replace_requested and (
        (prior_url and prior_url != redacted)
        or (prior_branch and branch and prior_branch != branch)
    ):
        replace = True
    # Per-project working dir. Default is the container's OverlayFS `/tmp`
    # (fast on Mac Docker Desktop — VM-local ext4). Operators can override
    # via `LAMA_GIT_CLONE_ROOT` to point at a bind-mounted HOST path if
    # they want the clone to land on the Mac filesystem (visible in
    # Finder, re-usable across container rebuilds). See docker-compose
    # `LAMA_GIT_CACHE_HOST` for the recommended pattern.
    _clone_root = (os.environ.get("LAMA_GIT_CLONE_ROOT", "") or "").strip()
    if _clone_root and os.path.isdir(_clone_root):
        workdir = os.path.join(_clone_root, project_id)
    else:
        workdir = os.path.join(_tempfile.gettempdir(), "lama-git", project_id)
    os.makedirs(workdir, exist_ok=True)
    dest = os.path.join(workdir, uuid.uuid4().hex)

    # iter-14.3 + iter-14.5 — Do the git clone FIRST (fast — seconds), then
    # purge, then kick off ingest as an async background task. Ingest of a
    # large monorepo can take minutes and used to be inlined into the
    # request, blowing past nginx / axios timeouts and surfacing as a
    # confusing "Network Error" in the UI even though the clone had
    # succeeded on disk. See the poll endpoint below.
    try:
        commit = await _git_clone(auth_url, branch, depth, dest)
    except HTTPException:
        # iter-14.5 — the clone itself failed (auth, timeout, unreachable
        # host). No KB damage yet since we haven't purged; re-raise the
        # HTTPException so the frontend sees a proper 4xx/5xx response
        # (not a generic "Network Error").
        _shutil.rmtree(dest, ignore_errors=True)
        raise

    # Clone succeeded — now safe to purge the previous KB.
    purge_stats: dict = {}
    if replace:
        # iter-13.87 — also clear downstream SRS / chat / stage_context.
        # Cloning a new repo invalidates every artefact derived from the
        # previous source; leaving them around manifests as "SRS still
        # references the old project" in the UI.
        purge_stats = await _purge_project_kb(
            project_id, clear_legacy_path=False, clear_downstream=True,
        )
        logger.info(
            "clone-git REPLACE for project=%s prior_url=%s new_url=%s purged=%s",
            project_id, prior_url or "(none)", redacted, purge_stats,
        )

    now = datetime.now(timezone.utc).isoformat()

    # iter-14.5 — Insert the git-source row NOW with status="ingesting" so
    # the frontend has something to poll from immediately, then kick off
    # ingest as a background asyncio task and return 202. Previously the
    # entire ingest ran inside the request → HTTP round-trip which, for
    # repos with 1000+ files, routinely blew past nginx's proxy_read_timeout
    # (or the browser's fetch idle timeout), surfaced to the user as
    # "Network Error", and dropped the source-doc insert on the floor so
    # the UI never knew the clone had actually succeeded.
    source_id = uuid.uuid4().hex
    source_doc = {
        "id":            source_id,
        "project_id":    project_id,
        "url":           redacted,
        "branch":        branch or "(default)",
        "commit":        commit,
        "subdir":        subdir,
        "depth":         depth,
        "local_path":    dest,
        "file_count":    0,
        "skipped_count": 0,
        "cloned_at":     now,
        # iter-14.5 — ingest lifecycle
        "status":        "ingesting",   # ingesting | done | failed
        "error":         "",
        "started_at":    now,
        "finished_at":   "",
    }
    await kb_git_sources.insert_one(source_doc)

    # Stamp the project so SRS / Droid know where the live files live
    # (do this BEFORE the background task so downstream stages that read
    # `project.legacy_path` right after clone-git returns don't race).
    try:
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "legacy_path":            dest,
                "legacy_path_scanned_at": now,
                "legacy_git_url":         redacted,
                "legacy_git_branch":      source_doc["branch"],
                "legacy_git_commit":      commit,
            }},
        )
    except Exception:
        pass

    async def _bg_ingest():
        """iter-14.5 — background ingest task. Runs decoupled from the
        HTTP request/response so a slow / large repo never surfaces as
        a Network Error on the frontend.
        """
        try:
            processed, skipped = await _ingest_folder_into_kb(
                project_id, dest, subdir=subdir, default_kind=default_kind,
                source_id=source_id,
            )
            done_ts = datetime.now(timezone.utc).isoformat()
            await kb_git_sources.update_one(
                {"id": source_id},
                {"$set": {
                    "status":        "done",
                    "file_count":    len(processed),
                    "skipped_count": len(skipped),
                    "finished_at":   done_ts,
                }},
            )
            logger.info(
                "clone-git INGEST DONE project=%s source=%s files=%d skipped=%d",
                project_id, source_id, len(processed), len(skipped),
            )
        except HTTPException as e:
            _shutil.rmtree(dest, ignore_errors=True)
            await kb_git_sources.update_one(
                {"id": source_id},
                {"$set": {
                    "status":       "failed",
                    "error":        f"HTTP {e.status_code}: {str(e.detail)[:400]}",
                    "finished_at":  datetime.now(timezone.utc).isoformat(),
                }},
            )
            logger.warning(
                "clone-git INGEST FAILED project=%s source=%s http=%d detail=%s",
                project_id, source_id, e.status_code, str(e.detail)[:200],
            )
        except Exception as e:  # noqa: BLE001
            _shutil.rmtree(dest, ignore_errors=True)
            await kb_git_sources.update_one(
                {"id": source_id},
                {"$set": {
                    "status":       "failed",
                    "error":        f"{type(e).__name__}: {str(e)[:400]}",
                    "finished_at":  datetime.now(timezone.utc).isoformat(),
                }},
            )
            logger.exception(
                "clone-git INGEST CRASHED project=%s source=%s", project_id, source_id,
            )

    # Kick off the ingest without awaiting.
    asyncio.create_task(_bg_ingest())

    return {
        "ok":           True,
        "status":       "ingesting",          # iter-14.5 — poll /clone-git/status
        "source_id":    source_id,            # iter-14.5 — pass this to the poll endpoint
        "commit":       commit,
        "source_url":   redacted,
        "branch":       source_doc["branch"],
        "local_path":   dest,
        "replaced":     bool(replace),
        "purge_stats":  purge_stats if replace else {},
        # iter-14.5 — final counts are unknown at this point; the poll
        # endpoint will surface them once the background task finishes.
        # We keep these keys present (but 0) so legacy frontend code
        # that reads `r.scanned` doesn't crash.
        "scanned":      0,
        "skipped":      0,
        "files":        [],
    }


# ── iter-14.5 — Poll endpoint for the async clone-git ingest ─────────────
@router.get("/{project_id}/clone-git/status")
async def clone_git_status(project_id: str, source_id: str = ""):
    """Return the ingest status for a clone-git run. If `source_id` is
    omitted, returns the most recent clone for this project.

    Response shape:
      { status: "ingesting" | "done" | "failed",
        file_count, skipped_count,   # 0 while ingesting
        commit, url, branch, subdir,
        started_at, finished_at,     # "" while ingesting
        error }                      # populated iff status == "failed"
    """
    q: dict = {"project_id": project_id}
    if source_id:
        q["id"] = source_id
    doc = await kb_git_sources.find_one(
        q, {"_id": 0}, sort=[("cloned_at", -1)],
    )
    if not doc:
        # iter-14.5 — return 200 with an empty body instead of 404 so the
        # frontend polling loop can distinguish "never cloned" from
        # transient network errors (which axios also renders as 404-ish).
        return {"status": "unknown"}
    # iter-14.6 — Defensive fallback: if the doc is still marked "ingesting"
    # and the periodic flush hasn't populated file_count yet (e.g. old
    # run before the sort= bug fix, or a flush that raced with a mongo
    # blip), fall back to a live count from kb_files so the UI toast at
    # least shows real progress instead of "0/0".
    if doc.get("status") == "ingesting" and not doc.get("file_count"):
        try:
            live_files = await kb_files.count_documents({"project_id": project_id})
            if live_files:
                doc["file_count"] = live_files
                total = doc.get("total_files") or 0
                if total:
                    doc["progress_pct"] = int(100 * live_files / max(1, total))
        except Exception:
            pass
    return doc


@router.get("/{project_id}/git-source")
async def get_latest_git_source(project_id: str):
    """Most recent git clone for this project (UI badge in Source Files)."""
    doc = await kb_git_sources.find_one(
        {"project_id": project_id}, {"_id": 0},
        sort=[("cloned_at", -1)],
    )
    return doc or {}


# ── Module tree + selection ─────────────────────────────────────────────────

def _build_tree(rows: list[dict], excluded_prefixes: list[str]) -> dict:
    """Build a nested {name, path, kind, children:[], file_count, included}
    tree from kb_files rows. Folders aggregate file_count from descendants."""
    root: dict = {"name": "", "path": "", "kind": "dir", "children": {}, "file_count": 0, "included": True}

    def _is_excluded(path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in excluded_prefixes)

    for r in rows:
        fname = (r.get("filename") or "").replace("\\", "/").lstrip("./")
        if not fname:
            continue
        parts = fname.split("/")
        cur = root
        accum = ""
        for i, part in enumerate(parts):
            accum = part if not accum else accum + "/" + part
            is_leaf = i == len(parts) - 1
            kind = "file" if is_leaf else "dir"
            child = cur["children"].get(part)
            if not child:
                child = {
                    "name": part, "path": accum, "kind": kind,
                    "children": {}, "file_count": 0,
                    "included": not _is_excluded(accum),
                    "file_id": r.get("id") if is_leaf else None,
                }
                cur["children"][part] = child
            cur["file_count"] += 1
            cur = child
        cur["file_count"] = 1  # leaf

    def _serialise(node: dict) -> dict:
        kids = [_serialise(c) for c in node["children"].values()]
        kids.sort(key=lambda n: (n["kind"] == "file", n["name"].lower()))
        # A folder is "included" only if it has at least one included descendant
        # — folder-toggle state is implied by the children. For leaves the
        # `included` flag is authoritative.
        if node["kind"] == "dir" and kids:
            node["included"] = any(k["included"] for k in kids)
            node["file_count"] = sum(k.get("file_count", 0) for k in kids if k["kind"] == "file") or len(
                [k for k in kids if k["kind"] == "file"]
            )
        return {
            "name": node["name"],
            "path": node["path"],
            "kind": node["kind"],
            "file_count": node.get("file_count", 0),
            "included": bool(node.get("included", True)),
            "file_id": node.get("file_id"),
            "children": kids,
        }

    return _serialise(root)


@router.get("/{project_id}/modules-tree")
async def get_modules_tree(project_id: str):
    """Return a folder/file tree of every kb_file, merged with the current
    exclusion list so the UI can render checkbox state."""
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    rows = await kb_files.find(
        {"project_id": project_id}, {"_id": 0, "id": 1, "filename": 1, "excluded": 1},
    ).to_list(20000)
    sel = await kb_module_selection.find_one({"project_id": project_id}, {"_id": 0}) or {}
    excluded = sel.get("excluded", []) or []
    tree = _build_tree(rows, excluded)
    return {
        "project_id": project_id,
        "file_count": len(rows),
        "excluded": excluded,
        "tree": tree,
        "updated_at": sel.get("updated_at", ""),
    }


@router.put("/{project_id}/modules-selection")
async def put_modules_selection(project_id: str, payload: dict):
    """Replace the exclusion list. Body: { excluded: [path_prefix, …] }.

    The matching `kb_files` are tagged `excluded: true/false` so chat-RAG,
    build, SRS and Architecture prompts can skip them by adding
    `{"excluded": {"$ne": true}}` to their kb_files queries.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    raw = (payload or {}).get("excluded") or []
    if not isinstance(raw, list):
        raise HTTPException(400, "excluded must be a list of path prefixes")
    # Normalise: trim, drop empties, dedup, kill duplicates that are children
    # of another excluded prefix (path/a + path/a/b ⇒ keep path/a only).
    cleaned = {(p or "").strip().strip("/").replace("\\", "/") for p in raw if p}
    cleaned.discard("")
    minimal: list[str] = []
    for p in sorted(cleaned):
        if not any(p == q or p.startswith(q + "/") for q in minimal):
            minimal.append(p)

    now = datetime.now(timezone.utc).isoformat()
    await kb_module_selection.update_one(
        {"project_id": project_id},
        {"$set": {"project_id": project_id, "excluded": minimal, "updated_at": now}},
        upsert=True,
    )

    # Stamp kb_files
    await kb_files.update_many({"project_id": project_id}, {"$set": {"excluded": False}})
    if minimal:
        cond = [
            {"filename": p} for p in minimal
        ] + [
            {"filename": {"$regex": f"^{re.escape(p)}/"}} for p in minimal
        ]
        await kb_files.update_many(
            {"project_id": project_id, "$or": cond},
            {"$set": {"excluded": True}},
        )

    included = await kb_files.count_documents({"project_id": project_id, "excluded": {"$ne": True}})
    excluded_count = await kb_files.count_documents({"project_id": project_id, "excluded": True})
    return {
        "ok": True,
        "excluded": minimal,
        "included_files": included,
        "excluded_files": excluded_count,
        "updated_at": now,
    }





# ── iter-13.81.9 — Factory.ai filesystem materialisation ──────────────
# Pulls every kb_files row for the project, tar.gzs it, and uploads to
# the Droid Computer under <workspace>/legacy-source/ via a transient
# bootstrap session (Factory has no file-upload API). The materialised
# path is persisted in `factory_workspaces` so the prompt-injection layer
# can surface it to subsequent runs.

@router.post("/{project_id}/factory-materialize")
async def kb_factory_materialize(project_id: str, force: bool = False):
    """Materialise this project's KB onto the Factory Droid's filesystem.

    Query params:
      - force=true  → re-upload even if persisted sha matches.
    """
    proj = await projects.find_one({"id": project_id}, {"_id": 0, "id": 1})
    if not proj:
        raise HTTPException(404, "Project not found")
    try:
        from kb.factory_materializer import materialize_project_workspace
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"materialiser unavailable: {exc}") from exc
    try:
        result = await materialize_project_workspace(project_id, force=bool(force))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"materialise failed: {exc}") from exc
    if not result.get("ok") and not result.get("skipped"):
        # Surface a structured error so the UI can show actionable reason.
        raise HTTPException(
            400 if result.get("reason") in {
                "factory_disabled", "missing_app_key", "missing_computer_id",
                "kb_empty", "project_not_found",
            } else 502,
            (result.get("detail") or result.get("reason") or "materialise failed"),
        )
    try:
        from db import audit_log as _audit
        await _audit.insert_one({
            "project_id": project_id,
            "action": "factory_materialize",
            "detail": {
                "file_count": result.get("file_count", 0),
                "bytes": result.get("bytes", 0),
                "skipped": bool(result.get("skipped")),
                "path": result.get("path", ""),
            },
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:  # noqa: BLE001
        pass
    return result


@router.get("/{project_id}/factory-materialize")
async def kb_factory_materialize_status(project_id: str):
    """Return the persisted materialisation state (or null if not yet run)."""
    try:
        from kb.factory_materializer import get_materialized_state
        state = await get_materialized_state(project_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    return {"project_id": project_id, "state": state or None}


# ---------------------------------------------------------------------------
# iter-14.24 — Journey KB endpoints (Phase 1)
# ---------------------------------------------------------------------------
# All journey endpoints are read/config only in Phase 1. Materialisation
# happens as part of Build KB (see _build_kb_impl). Enabling the toggle
# does NOT retro-materialise; the operator must re-run Build KB after
# flipping ON so the graph → journey pipeline runs cleanly.

@router.get("/{project_id}/journeys/config")
async def kb_journeys_get_config(project_id: str):
    """Return the effective journey-KB config for this project."""
    try:
        from kb.journey_config import (
            is_journey_kb_enabled,
            journey_kb_stages,
            journey_kb_kinds,
            env_default_journey_kb,
            all_journey_kinds,
            kb_journey_config,
        )
        doc = await kb_journey_config.find_one(
            {"project_id": project_id}, {"_id": 0}
        ) or {}
        return {
            "project_id": project_id,
            "enabled": await is_journey_kb_enabled(project_id),
            "stages_enabled": await journey_kb_stages(project_id),
            "kinds_enabled": await journey_kb_kinds(project_id),
            "all_kinds": list(all_journey_kinds()),
            "env_default": env_default_journey_kb(),
            "override": doc or None,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


@router.post("/{project_id}/journeys/toggle")
async def kb_journeys_toggle(project_id: str, payload: dict):
    """Set the per-project journey-KB toggle.

    Body: ``{"enabled": bool, "stages_enabled": ["srs","codegen"],
             "kinds_enabled": ["api","ui","column"]}``.
    All fields optional; only present ones are updated.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "payload must be a JSON object")
    enabled = payload.get("enabled")
    stages = payload.get("stages_enabled")
    kinds = payload.get("kinds_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(400, "`enabled` must be boolean when provided")
    if stages is not None and not isinstance(stages, list):
        raise HTTPException(400, "`stages_enabled` must be a list when provided")
    if kinds is not None and not isinstance(kinds, list):
        raise HTTPException(400, "`kinds_enabled` must be a list when provided")
    try:
        from kb.journey_config import set_journey_kb_config
        doc = await set_journey_kb_config(
            project_id, enabled=enabled,
            stages_enabled=stages, kinds_enabled=kinds,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    try:
        from db import audit_log
        await audit_log.insert_one({
            "action": "kb.journeys.toggle",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {"enabled": doc.get("enabled"),
                        "stages_enabled": doc.get("stages_enabled"),
                        "kinds_enabled": doc.get("kinds_enabled")},
        })
    except Exception:  # noqa: BLE001
        pass
    return {"project_id": project_id, "config": doc}


@router.get("/{project_id}/journeys/summary")
async def kb_journeys_summary(project_id: str):
    """Cheap counts for the UI Journeys tab (Phase 2)."""
    try:
        from kb.journey_materializer import journeys_summary
        return {"project_id": project_id, **(await journeys_summary(project_id))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


@router.get("/{project_id}/journeys")
async def kb_journeys_list(
    project_id: str,
    kind: str | None = None,
    pivot_route: str | None = None,
    service: str | None = None,
    endpoint: str | None = None,
    table: str | None = None,
    limit: int = 200,
):
    """List materialised journeys, optionally filtered.

    Query params:
      - ``kind``        — one of ``api`` / ``ui`` / ``column``
      - ``pivot_route`` — exact match (e.g. ``POST /api/register``)
      - ``endpoint``    — substring match on ``pivot_route``
      - ``service``     — substring match on any class in ``classes_touched``
      - ``table``       — exact match on any string in ``tables``
      - ``limit``       — capped at 1000
    """
    if kind is not None:
        from kb.journey_config import all_journey_kinds
        if kind not in all_journey_kinds():
            raise HTTPException(400, f"invalid kind '{kind}' — expected one of {all_journey_kinds()}")
    try:
        from kb.journey_materializer import load_journeys
        journeys = await load_journeys(
            project_id,
            kind=kind, pivot_route=pivot_route,
            service=service, endpoint=endpoint, table=table,
            limit=min(1000, max(1, int(limit))),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))
    return {"project_id": project_id, "count": len(journeys), "journeys": journeys}


@router.post("/{project_id}/journeys/rebuild")
async def kb_journeys_rebuild(project_id: str):
    """Re-run journey materialisation without re-running the full Build KB.

    Requires ``kb_graph`` to exist. Returns the materialisation stats or
    a skipped-with-reason dict.
    """
    try:
        from kb.journey_config import is_journey_kb_enabled
        from kb.journey_materializer import materialize_journeys
        if not await is_journey_kb_enabled(project_id):
            return {"project_id": project_id, "skipped": True,
                    "reason": "journey_kb_disabled"}
        stats = await materialize_journeys(project_id)
        return {"project_id": project_id, **stats}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


