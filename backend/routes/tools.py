"""
Tools — Standalone utilities that bypass the 5-stage pipeline.

1. Gap Analyzer: Code vs SRS/FRS/User Manual comparison
2. Transformer: Direct code transformation (Helidon→SpringBoot, Oracle→PostgreSQL)

Both tools use configurable prompts from the Prompt Library and support
GitHub push for generated artifacts.
"""

import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse, quote

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

from db import (
    gap_analyses,
    gap_analysis_files,
    transformations,
    transform_files,
    projects,
    prompts,
    audit_log,
    github_configs,
    tools_kb,
    transformer_envelopes,
    transformer_tasks,
    transformer_agent_runs,
    transformer_agent_configs,
)
from llm import fabric_call, _http_verify, active_default_provider_is_local

# iter-15.51 — In-process cache mapping transform_id → project_id so the
# transformer pipeline can plumb `project_id` into every `fabric_call` and
# thereby engage Factory (Droid) routing when the target project has
# `settings.factory_orchestrator.enabled=true`. Without this, llm.py's
# `if project_id:` gate (~L1202) skips the whole Factory codepath and every
# transformer LLM call falls through to the Console-active provider —
# which, when Ollama is the only active local provider, causes every
# request to hit qwen2.5-coder:7b at http://host.docker.internal:11434
# (see logs: `iter-14.75: calling http://host.docker.internal:11434/v1`).
_TRANSFORM_PROJECT_ID_CACHE: Dict[str, str] = {}


async def _project_id_for_transform(transform_id: str) -> str:
    """Return the `project_id` the transformation was created against, or
    "" if none is persisted. Cached in-process; safe to call from every
    `fabric_call` site."""
    if not transform_id:
        return ""
    cached = _TRANSFORM_PROJECT_ID_CACHE.get(transform_id)
    if cached is not None:
        return cached
    try:
        doc = await transformations.find_one(
            {"_id": transform_id},
            {"project_id": 1},
        )
    except Exception:  # noqa: BLE001
        doc = None
    pid = ""
    if doc and isinstance(doc.get("project_id"), str):
        pid = doc["project_id"].strip()
    # Fallback: env-var default (single-tenant deploys) so older
    # transformations created before iter-15.51 still get Factory routing.
    if not pid:
        pid = (os.getenv("LAMA_DEFAULT_PROJECT_ID") or "").strip()
    # Last resort: pick the first tech_transformer project in DB. LAMA is
    # single-tenant / single-active-project so this is deterministic in
    # practice.
    if not pid:
        try:
            p = await projects.find_one(
                {"project_type": "tech_transformer"},
                {"id": 1},
                sort=[("created_at", -1)],
            )
            if p and isinstance(p.get("id"), str):
                pid = p["id"]
        except Exception:  # noqa: BLE001
            pass
    _TRANSFORM_PROJECT_ID_CACHE[transform_id] = pid
    return pid
from kb.parsers import parse_file
from kb.owl_extractor import extract
from kb.tech_detector import detect_tech_stack
from tools_kb_builder import (
    build_tools_kb,
    render_kb_for_prompt,
    summarize_kb_for_ui,
)

log = logging.getLogger("lama.tools")

# Skip patterns for file parsing (same as used in kb.py scan-folder)
SKIP_PATTERNS = [
    r"node_modules",
    r"\.git",
    r"vendor",
    r"__pycache__",
    r"\.bak$",
    r"\.save$",
    r"_bkp",
    r"_old",
    r"_backup",
    r"\.php_",
    r"\.pyc$",
    r"\.class$",
    r"\.DS_Store",
    r"\.idea",
    r"\.vscode",
    r"target/",
    r"build/",
    r"dist/",
    # iter-15.36 — macOS zip artifacts (`Compress` on macOS injects a
    # sibling `__MACOSX/` dir holding AppleDouble `._*` resource-fork
    # files) were leaking into the Transformer's source-file listing,
    # task planner, and generated-project structure as useless noise.
    r"__MACOSX",
    r"(^|/)\._",
    r"\.zip$",
    # iter-15.37 — `META-INF/` (JAR/WAR manifest + signature metadata,
    # e.g. `META-INF/MANIFEST.MF`, `META-INF/maven/...`) is packaging
    # metadata, never hand-written source — ignore it same as target/build/dist.
    r"(^|/)META-INF",
]

router = APIRouter(prefix="/tools", tags=["tools"])


# ════════════════════════════════════════════════════════════════════════════
# Pydantic Models
# ════════════════════════════════════════════════════════════════════════════

class GapAnalysisRequest(BaseModel):
    """Request body for gap analysis."""
    name: str = Field(..., description="Analysis name")
    description: Optional[str] = None
    model: Optional[str] = None  # Override default model


# iter-15.38 — "Planner review required" (and the Coder / Tester panels)
# now embed a chat assistant so the user can find/remove/reassign items by
# natural language instead of clicking through every row one at a time,
# e.g. "remove all test files from wave 2", "find files touching the Order
# table", "move the DAO classes to wave 3". `panel` scopes which item set
# (tasks / generated files / verification rows) and which action verbs are
# allowed for a given chat turn.
class AgentPlanChatRequest(BaseModel):
    """Request body for the Planner/Coder/Tester chat assistant."""
    panel: str = Field(..., description="planner | coder | tester")
    message: str = Field(..., min_length=1, description="User's natural-language instruction")
    model: Optional[str] = None


class TransformRequest(BaseModel):
    """Request body for code transformation."""
    name: str = Field(..., description="Transformation name")
    target_stack: str = Field(..., description="Target technology stack")
    description: Optional[str] = None
    model: Optional[str] = None


class TransformTargetStack(BaseModel):
    """Available transformation targets."""
    framework: Optional[str] = None  # e.g., "spring-boot-3"
    database: Optional[str] = None   # e.g., "postgresql"
    frontend: Optional[str] = None   # e.g., "react-18"


class AgentConfigUpdate(BaseModel):
    """iter-15.19 — Per-transformation agent prompt/model override payload.
    Empty string in either field means 'no override for this field, use
    the default' (shared Prompt Library template / run-level model)."""
    prompt_template: str = ""
    model: str = ""


# iter-15.26 — Fields a human reviewer can manually correct on one
# envelope. Kept as a plain dict-of-optionals (not deeply nested pydantic
# sub-models) so a partial edit (e.g. just fixing `db_tables`) doesn't
# require resending the whole envelope. `None` means "leave unchanged".
ENVELOPE_EDITABLE_FIELDS = (
    "business_logic_summary", "service_class", "service_file",
    "repository_class", "repository_file", "db_tables", "db_operations",
    "external_calls", "files_affected", "action", "risk_level",
    "acceptance_criteria", "request", "response", "service_layer",
    "data_layer", "nfr", "notes",
)


class EnvelopeEditRequest(BaseModel):
    """iter-15.26 — PATCH payload for one envelope. Every field is
    optional; only the ones the reviewer actually changed need to be
    sent. Persisted both onto the envelope doc AND (via `user_edited`)
    kept across a future 'Regenerate' pass — see
    `regenerate_transformer_envelopes` below."""
    business_logic_summary: Optional[str] = None
    service_class: Optional[str] = None
    service_file: Optional[str] = None
    repository_class: Optional[str] = None
    repository_file: Optional[str] = None
    db_tables: Optional[List[str]] = None
    db_operations: Optional[List[str]] = None
    external_calls: Optional[List[Dict[str, Any]]] = None
    files_affected: Optional[List[str]] = None
    action: Optional[str] = None
    risk_level: Optional[str] = None
    acceptance_criteria: Optional[List[str]] = None
    request: Optional[Dict[str, Any]] = None
    response: Optional[Dict[str, Any]] = None
    service_layer: Optional[Dict[str, Any]] = None
    data_layer: Optional[Dict[str, Any]] = None
    nfr: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


# ════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ════════════════════════════════════════════════════════════════════════════

async def _get_prompt(key: str) -> Optional[str]:
    """Fetch prompt template from library."""
    doc = await prompts.find_one({"key": key})
    return doc.get("template") if doc else None


# iter-15.19 — Per-transformation agent prompt/model overrides. Lets a
# user open a specific agent (SA/CM/Planner/Coder/Verifier/Tester) from
# the "Agent Pipeline" widget on the Code Transformer page, review/edit
# its prompt + model for just THIS transformation, save, and rerun the
# whole pipeline with the override applied. Never touches the shared
# global Prompt Library — those defaults are unaffected.
AGENT_PROMPT_KEYS = {
    "super_agent": "tools.transformer.super_agent",
    "context_manager": "tools.transformer.context_manager",
    "planner": "tools.transformer.planner",
    "coder": "tools.transformer.coder",
    "verifier": "tools.transformer.verifier",
    "tester": "tools.transformer.tester",
    "devops_expert": "tools.transformer.devops_expert",
    # Plan gate between Planner and Coder. Was seeded with a prompt and a
    # Console row since iter-16 but never reachable, because it had no
    # entry here — `_get_effective_prompt` resolved it to "".
    "validator": "tools.transformer.validator",
    # The DevOps agent's second mode: a proactive dependency audit of the
    # generated build manifests, distinct from its escalation persona in
    # the compile-fix loop.
    "devops_audit": "tools.transformer.devops_audit",
    # iter-20 — the compile-fix loop's last escalation rung. Rungs 0-2 all
    # EDIT a file that may be past saving; this one rewrites it from the
    # legacy original against the target playbook. See _ESCALATION_LADDER.
    "regenerator": "tools.transformer.regenerator",
}
# super_agent is pure orchestration today (it logs a completed run but
# never calls an LLM), so an edited prompt/model has no runtime effect
# yet — the config panel still shows/saves it for future-proofing, but
# flags it as not LLM-backed so the UI can set expectations correctly.
AGENT_LLM_BACKED = {
    "super_agent": False,
    "context_manager": True,
    "planner": True,
    "coder": True,
    "verifier": True,
    "tester": True,
    "devops_expert": True,
    "validator": True,
    "devops_audit": True,
    "regenerator": True,
}
AGENT_LABELS = {
    "super_agent": "Super Agent",
    "context_manager": "Context Manager",
    "planner": "Planner",
    "coder": "Coder",
    "verifier": "Verifier",
    "tester": "Tester",
    "devops_expert": "DevOps Expert",
    "validator": "Validator",
    "devops_audit": "DevOps Expert (dependency audit)",
    "regenerator": "Regenerator (full rewrite)",
}


async def _get_agent_override(transform_id: str, agent: str) -> Optional[dict]:
    return await transformer_agent_configs.find_one({"transform_id": transform_id, "agent": agent})


async def _get_effective_prompt(transform_id: str, agent: str) -> str:
    """Resolve the prompt template for one agent on one transformation: a
    saved per-transformation override wins; otherwise fall back to the
    shared Prompt Library default for that agent's base key."""
    base_key = AGENT_PROMPT_KEYS.get(agent, "")
    override = await _get_agent_override(transform_id, agent)
    if override and (override.get("prompt_template") or "").strip():
        return override["prompt_template"]
    return await _get_prompt(base_key) or ""


async def _get_effective_model(transform_id: str, agent: str, fallback_model: Optional[str]) -> Optional[str]:
    """Resolve the model for one agent on one transformation: a saved
    per-transformation override wins; otherwise fall back to the model
    passed into the pipeline run (which may itself be None, letting
    fabric_call's Console-tier routing pick a model)."""
    override = await _get_agent_override(transform_id, agent)
    if override and (override.get("model") or "").strip():
        return override["model"]
    return fallback_model


async def _log_audit(action: str, entity_type: str, entity_id: str, details: dict):
    """Log audit entry."""
    await audit_log.insert_one({
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _extract_entities_from_files(files: List[Dict]) -> List[Dict]:
    """Extract code entities from parsed files."""
    entities = []
    for f in files:
        content = f.get("content", "")
        filename = f.get("path", "")
        ext = os.path.splitext(filename)[1].lower()
        
        # Map extension to filetype
        ext_map = {
            ".java": "java", ".py": "python", ".js": "javascript",
            ".ts": "typescript", ".php": "php", ".sql": "sql",
            ".jsp": "jsp", ".cs": "csharp", ".go": "go",
        }
        filetype = ext_map.get(ext, "unknown")
        
        if filetype != "unknown":
            file_entities = extract(filetype, content, filename)
            for e in file_entities:
                e["source_file"] = filename
            entities.extend(file_entities)
    
    return entities


def detect_stack(file_paths, content_sample: str = "", files_with_content: list = None) -> Dict[str, Any]:
    """
    Wrapper around tech_detector.detect_tech_stack that maps
    detected technologies to our internal category format.

    iter-15.29 fix: the previous implementation built `files_list` entries
    with NO "id" field and stuffed all sampled content under a single
    "sample" key in `chunks_by_file`. Since `detect_tech_stack` looks up
    each file's content via `chunks_by_file.get(f.get("id"))`, every file's
    `id` was `None` and never matched "sample" — so CONTENT-driven markers
    (e.g. `@SpringBootApplication`, `org.springframework`, JDBC driver
    strings) were NEVER detected, only filename-based markers (pom.xml →
    Maven, etc). This is why "Backend" (framework, e.g. Spring Boot) and
    "Database" often came back empty even though "Runtime" (language, by
    file extension) worked fine. Fixed by assigning a real per-file `id`
    and mapping each file's OWN content into `chunks_by_file[id]`.
    """
    # Preferred path: caller has the actual per-file content available.
    if files_with_content:
        files_list = []
        chunks_by_file = {}
        for idx, f in enumerate(files_with_content[:300]):
            path = f.get("path") or f.get("filename") or ""
            fid = f"f{idx}"
            files_list.append({
                "id": fid,
                "filename": path,
                "filetype": path.split(".")[-1] if "." in path else "",
                "size": f.get("size") or len(f.get("content") or ""),
            })
            content = f.get("content") or ""
            if content:
                chunks_by_file[fid] = [content[:20000]]
        raw = detect_tech_stack(files_list, chunks_by_file or None)
    else:
        # Legacy path (paths only + one flattened content sample) — kept for
        # backward compatibility with any caller that hasn't been migrated.
        files_list = [{"id": f"f{i}", "filename": p, "filetype": p.split(".")[-1] if "." in p else ""}
                      for i, p in enumerate(list(file_paths)[:100])]
        chunks_by_file = {f["id"]: [content_sample] for f in files_list} if content_sample else None
        raw = detect_tech_stack(files_list, chunks_by_file)

    # Map to our format (backend, frontend, database, runtime)
    result = {
        "backend": None,
        "frontend": None,
        "database": None,
        "runtime": None,
        "raw_detection": raw,
    }
    
    # Normalize tech names to our IDs
    # tech_detector may return tuples like ('Python', 1) or plain strings
    def extract_names(items):
        names = []
        for item in items:
            if isinstance(item, tuple):
                names.append(str(item[0]))
            elif isinstance(item, str):
                names.append(item)
            else:
                names.append(str(item))
        return names
    
    frameworks = extract_names(raw.get("frameworks", []))
    languages = extract_names(raw.get("languages", []))
    databases = extract_names(raw.get("databases", []))
    
    # Backend framework detection
    backend_map = {
        "helidon": "helidon", "helidon-mp": "helidon", "helidon-se": "helidon",
        "spring-boot": "spring-boot-3", "springboot": "spring-boot-3", "spring boot": "spring-boot-3",
        "quarkus": "quarkus", "micronaut": "micronaut",
        "fastapi": "fastapi", "django": "django", "flask": "fastapi",
        "express": "express", "nestjs": "nestjs", "koa": "express",
        "codeigniter": "codeigniter", "laravel": "laravel", "php": "php",
        "jax-rs": "helidon", "jaxrs": "helidon", "jersey": "helidon",
    }
    for fw in frameworks:
        fw_lower = fw.lower()
        for key, val in backend_map.items():
            if key in fw_lower:
                result["backend"] = val
                break
        if result["backend"]:
            break
    
    # Frontend framework detection
    frontend_map = {
        "angular": "angular-17", "react": "react-18", "vue": "vue-3",
        "next": "nextjs", "svelte": "svelte", "jquery": "jquery",
        "jsp": "jsp", "thymeleaf": "thymeleaf",
    }
    for fw in frameworks:
        fw_lower = fw.lower()
        for key, val in frontend_map.items():
            if key in fw_lower:
                result["frontend"] = val
                break
        if result["frontend"]:
            break
    
    # Database detection
    db_map = {
        "postgresql": "postgresql", "postgres": "postgresql", "pgsql": "postgresql",
        "mysql": "mysql", "mariadb": "mariadb",
        "oracle": "oracle", "mongodb": "mongodb", "mongo": "mongodb",
        "sqlserver": "sqlserver", "mssql": "sqlserver", "sql server": "sqlserver",
        "sqlite": "sqlite", "h2": "h2",
    }
    for db in databases:
        db_lower = db.lower()
        for key, val in db_map.items():
            if key in db_lower:
                result["database"] = val
                break
        if result["database"]:
            break
    
    # Runtime/language detection
    runtime_map = {
        "java 21": "java-21", "java 17": "java-17", "java 11": "java-11",
        "java 8": "java-8", "java": "java-17",  # Default Java version
        "python 3.12": "python-3.12", "python 3.11": "python-3.11",
        "python 3": "python-3.12", "python": "python-3.12",
        "node 20": "node-20", "node 18": "node-18", "node": "node-20",
        "dotnet": "dotnet-8", ".net": "dotnet-8",
    }
    for lang in languages:
        lang_lower = lang.lower()
        for key, val in runtime_map.items():
            if key in lang_lower:
                result["runtime"] = val
                break
        if result["runtime"]:
            break
    
    # Infer runtime from backend if not detected
    if not result["runtime"]:
        if result["backend"] in ["helidon", "spring-boot-3", "spring-boot-2", "quarkus", "micronaut"]:
            result["runtime"] = "java-17"
        elif result["backend"] in ["fastapi", "django"]:
            result["runtime"] = "python-3.12"
        elif result["backend"] in ["express", "nestjs"]:
            result["runtime"] = "node-20"
        elif result["backend"] in ["codeigniter", "laravel", "php"]:
            result["runtime"] = "php-8"
    
    return result


def _parse_uploaded_files(zip_bytes: bytes) -> List[Dict]:
    """Parse files from uploaded ZIP.

    iter-14.99 — Binary docs (PDF/DOCX) are now routed through `kb.parsers.parse_file`
    so their extracted text is preserved (previously they were UTF-8-decoded to mojibake,
    which yielded 0 requirements from real SRS PDFs).
    """
    files = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
        for name in zf.namelist():
            # Skip directories and filtered patterns
            if name.endswith('/'):
                continue
            skip = False
            for pat in SKIP_PATTERNS:
                if re.search(pat, name, re.IGNORECASE):
                    skip = True
                    break
            if skip:
                continue

            try:
                raw = zf.read(name)
            except Exception:
                continue

            lower = name.lower()
            try:
                if lower.endswith(('.pdf', '.docx', '.doc')):
                    try:
                        _ftype, text = parse_file(name, raw, allow_zip_recurse=False)
                    except Exception as pe:
                        text = f"[Binary parse error for {name}: {pe}]"
                    text = text or ""
                    files.append({"path": name, "content": text, "size": len(text)})
                else:
                    content = raw.decode('utf-8', errors='replace')
                    files.append({"path": name, "content": content, "size": len(content)})
            except Exception:
                continue
    return files


# iter-15.18 — GitHub source support for Gap Analyzer.
# Doc source-type taxonomy that the FE dropdown surfaces + LLM prompt honours.
DOC_TYPE_LABELS = {
    "srs":       "Software Requirements Specification",
    "frs":       "Functional Requirements Specification",
    "brd":       "Business Requirements Document",
    "user_manual": "User Manual / Guide",
    "data_dict": "Data Dictionary",
    "design":    "Design Document / HLD / LLD",
    "api_spec":  "API Specification / OpenAPI",
    "test_plan": "Test Plan",
    "other":     "Other Document",
}


def _inject_token_into_git_url(url: str, token: Optional[str]) -> str:
    """Inject a PAT into an https clone URL (never persisted; used for clone only)."""
    if not token or not url.startswith(("http://", "https://")):
        return url
    try:
        p = urlparse(url)
        netloc = f"{quote(token, safe='')}@{p.hostname}"
        if p.port:
            netloc += f":{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        return url


async def _clone_github_repo(url: str, branch: Optional[str], token: Optional[str], dest: str) -> None:
    """Shallow git-clone a repo into `dest`. Raises HTTPException(400) on failure.

    Uses --depth=1 --single-branch for speed; falls back to `main`/`master` if
    the requested branch is missing. Timeout 240s (env override via
    LAMA_GAP_CLONE_TIMEOUT).
    """
    auth_url = _inject_token_into_git_url(url, token)
    branch = (branch or "").strip() or None

    try:
        timeout_s = float(os.environ.get("LAMA_GAP_CLONE_TIMEOUT", "240"))
    except Exception:
        timeout_s = 240.0

    async def _run(cmd: List[str]) -> tuple[int, str, str]:
        # iter-15.18 — honour LAMA_DISABLE_SSL_VERIFY for git clone the same
        # way httpx calls do elsewhere. Container images without a CA bundle
        # otherwise fail on GitHub's HTTPS cert.
        env = os.environ.copy()
        if (os.environ.get("LAMA_DISABLE_SSL_VERIFY") or "").lower() in ("1", "true", "yes"):
            env["GIT_SSL_NO_VERIFY"] = "true"
        ca_bundle = os.environ.get("LAMA_CA_BUNDLE")
        if ca_bundle:
            env["GIT_SSL_CAINFO"] = ca_bundle
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise HTTPException(
                400,
                f"git clone timed out after {int(timeout_s)}s. Try a smaller repo, "
                "a specific branch, or check network / access.",
            )
        return proc.returncode, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")

    base_cmd = [
        "git", "-c", "gc.auto=0", "-c", "advice.detachedHead=false",
        "clone", "--depth=1", "--single-branch",
    ]
    if branch:
        base_cmd += ["--branch", branch]
    base_cmd += [auth_url, dest]

    rc, out, err = await _run(base_cmd)
    if rc != 0:
        # Fallback: retry without --branch (in case branch doesn't exist)
        if branch and os.path.exists(dest):
            try: shutil.rmtree(dest)
            except Exception: pass
        if branch:
            retry_cmd = [
                "git", "-c", "gc.auto=0", "-c", "advice.detachedHead=false",
                "clone", "--depth=1", "--single-branch", auth_url, dest,
            ]
            rc2, _, err2 = await _run(retry_cmd)
            if rc2 == 0:
                return
            err = err2 or err
        # Redact the token before raising
        redacted = err.replace(token, "***") if token and token in err else err
        raise HTTPException(400, f"git clone failed: {redacted.strip()[:400] or 'unknown error'}")


def _walk_repo_files(root: str, max_files: int = 2000) -> List[Dict]:
    """Walk a cloned repo → List[{path, content, size}] honouring SKIP_PATTERNS.
    Text files only; UTF-8 with replacement. Binaries are skipped.
    """
    files: List[Dict] = []
    root_abs = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root_abs):
        # Prune skipped dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if not any(re.search(p, d, re.IGNORECASE) for p in SKIP_PATTERNS)
        ]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_abs)
            if any(re.search(p, rel, re.IGNORECASE) for p in SKIP_PATTERNS):
                continue
            try:
                if os.path.getsize(full) > 512 * 1024:  # >512KB text: skip
                    continue
                with open(full, "rb") as fh:
                    raw = fh.read()
                # Heuristic: skip likely-binary
                if b"\x00" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", errors="replace")
                files.append({"path": rel, "content": text, "size": len(text)})
                if len(files) >= max_files:
                    return files
            except Exception:
                continue
    return files


# ════════════════════════════════════════════════════════════════════════════
# Gap Analyzer Endpoints
# ════════════════════════════════════════════════════════════════════════════

def _extract_json_object(text: str) -> Optional[dict]:
    """Robust JSON extraction: strip code fences, then bracket-balance scan.

    Handles: markdown ```json fences, extra prose, single-line, deeply nested.
    Returns None on failure.
    """
    if not text:
        return None
    s = text.strip()
    # Strip code fences
    m = re.match(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```\s*$", s)
    if m:
        s = m.group(1).strip()
    # Try direct parse first
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Bracket-balance scan for first complete top-level object
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def _normalize_severity(sev: Any) -> str:
    """Map any LLM severity to critical|major|minor."""
    if not sev:
        return "minor"
    s = str(sev).strip().lower()
    if s in ("critical", "crit", "blocker", "showstopper"):
        return "critical"
    if s in ("high", "major", "high-severity"):
        return "major"
    if s in ("medium", "med", "moderate"):
        return "major"
    if s in ("low", "minor", "trivial", "cosmetic"):
        return "minor"
    return "minor"


def _pick_first_key(d: dict, *candidates) -> Any:
    """Return the first non-null value among candidate keys (case-insensitive)."""
    if not isinstance(d, dict):
        return None
    lc = {k.lower(): v for k, v in d.items()}
    for c in candidates:
        v = lc.get(c.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _flatten_gaps(parsed: dict) -> List[dict]:
    """Extract a list of gaps regardless of LLM structure variation."""
    if not isinstance(parsed, dict):
        return []
    candidates = []
    for key in ("gaps", "gap_list", "gap_analysis", "gapAnalysis", "findings", "issues", "items"):
        v = parsed.get(key)
        if isinstance(v, list):
            candidates.extend(v)
        elif isinstance(v, dict):
            # LLM sometimes wraps a single gap in an object
            candidates.append(v)
    # Normalize each entry
    out = []
    for i, g in enumerate(candidates):
        if not isinstance(g, dict):
            continue
        title = _pick_first_key(g, "title", "name", "summary", "gap", "description") or f"Gap {i + 1}"
        desc = _pick_first_key(g, "description", "details", "detail", "expected", "what") or ""
        if isinstance(desc, dict):
            desc = json.dumps(desc)
        loc = _pick_first_key(g, "location", "code_reference", "codeRef", "code_ref", "file", "component", "path")
        rec = _pick_first_key(g, "recommendation", "fix", "resolution", "action", "remediation") or ""
        doc_ref = _pick_first_key(g, "doc_reference", "docRef", "requirement", "requirement_id", "related")
        gap_type = _pick_first_key(g, "type", "category", "kind") or ""
        expected = _pick_first_key(g, "expected", "spec")
        actual = _pick_first_key(g, "actual", "implemented", "implementation")
        out.append({
            "id": g.get("id") or f"GAP-{i + 1:03d}",
            "type": str(gap_type).upper() if gap_type else "",
            "severity": _normalize_severity(g.get("severity")),
            "title": str(title)[:250],
            "description": str(desc)[:2000],
            "location": str(loc) if loc else "",
            "doc_reference": str(doc_ref) if doc_ref else "",
            "expected": str(expected) if expected else "",
            "actual": str(actual) if actual else "",
            "recommendation": str(rec)[:1500],
        })
    return out


def _extract_coverage_matrix(parsed: dict, requirements: Optional[List[Dict]] = None) -> List[dict]:
    """Normalize coverage matrix if present.

    iter-14.96 — When a `requirements` list is provided, join back onto each
    matrix row (by requirement_id) to surface `doc_type` / `doc_type_label` /
    `category` in the report matrix. Missing joins fall back to empty.
    Additionally, if the LLM returned FEWER rows than we have requirements,
    synthesize "missing" rows for the unmatched ones so the UI always shows
    every requirement — critical for the "show all test cases even if all
    failed" contract.
    """
    if not isinstance(parsed, dict):
        matrix_raw = []
    else:
        matrix_raw = parsed.get("coverage_matrix") or parsed.get("coverageMatrix") or parsed.get("matrix") or []
    if not isinstance(matrix_raw, list):
        matrix_raw = []

    req_by_id: Dict[str, Dict] = {}
    if requirements:
        for r in requirements:
            rid = str(r.get("id") or "").strip()
            if rid:
                req_by_id[rid] = r

    out: List[Dict] = []
    seen_ids: set = set()
    for row in matrix_raw:
        if not isinstance(row, dict):
            continue
        req_id = _pick_first_key(row, "requirement_id", "requirementId", "id", "req") or ""
        req_txt = _pick_first_key(row, "requirement_text", "requirement", "text", "description") or ""
        implemented = row.get("implemented")
        if isinstance(implemented, str):
            implemented = implemented.strip().lower() in ("true", "yes", "covered", "1", "implemented")
        status = row.get("status") or ("covered" if implemented else "missing")
        locs = row.get("code_locations") or row.get("code_refs") or row.get("locations") or []
        if isinstance(locs, str):
            locs = [locs]
        rid_str = str(req_id).strip()
        seen_ids.add(rid_str)
        joined = req_by_id.get(rid_str, {})
        out.append({
            "requirement_id": rid_str,
            "requirement_text": str(req_txt or joined.get("text", ""))[:500],
            "implemented": bool(implemented) if implemented is not None else (str(status).lower() == "covered"),
            "status": str(status).lower(),
            "code_locations": [str(x) for x in locs][:10],
            "notes": str(row.get("notes") or "")[:500],
            "doc_type": joined.get("doc_type", ""),
            "doc_type_label": joined.get("doc_type_label", ""),
            "category": joined.get("category", ""),
            "source": joined.get("source", ""),
        })

    # Synthesize missing rows for any requirement the LLM skipped.
    for r in (requirements or []):
        rid = str(r.get("id") or "").strip()
        if not rid or rid in seen_ids:
            continue
        out.append({
            "requirement_id": rid,
            "requirement_text": str(r.get("text", ""))[:500],
            "implemented": False,
            "status": "missing",
            "code_locations": [],
            "notes": "Not evaluated by the verifier — treated as missing.",
            "doc_type": r.get("doc_type", ""),
            "doc_type_label": r.get("doc_type_label", ""),
            "category": r.get("category", ""),
            "source": r.get("source", ""),
            "synthesized": True,
        })

    return out


# iter-14.98 — Test case normalizer + accuracy computation
# iter-15.3 — Senior Test Analyst schema: case_type + requirement_type + scenario + priority.
_TC_STATUS_MAP = {
    "pass": "PASS", "passed": "PASS", "success": "PASS", "ok": "PASS",
    "fail": "FAIL", "failed": "FAIL", "error": "FAIL", "fault": "FAIL",
    "block": "BLOCKED", "blocked": "BLOCKED", "not_covered": "BLOCKED",
    "missing": "BLOCKED", "n/a": "NOT_APPLICABLE",
    "not_applicable": "NOT_APPLICABLE", "na": "NOT_APPLICABLE",
    "skip": "NOT_APPLICABLE", "skipped": "NOT_APPLICABLE",
}

_TC_CASE_TYPE_MAP = {
    "positive": "positive", "pos": "positive", "happy": "positive",
    "happy_path": "positive", "happy-path": "positive", "success": "positive",
    "negative": "negative", "neg": "negative", "sad": "negative",
    "sad_path": "negative", "sad-path": "negative", "failure": "negative",
    "error": "negative", "invalid": "negative", "boundary": "negative",
    "unauthorized": "negative", "forbidden": "negative",
}

_REQ_TYPE_ID_PREFIX = {
    "UC": "USE_CASE", "USECASE": "USE_CASE", "US": "USE_CASE",
    "NFR": "NFR", "PERF": "NFR", "SEC": "NFR", "AVL": "NFR", "SLA": "NFR",
    "A11Y": "NFR", "ACC": "NFR",
    "BR": "BUSINESS_RULE", "RULE": "BUSINESS_RULE",
    "UI": "UI_SPEC", "SCR": "UI_SPEC",
    "DR": "DATA_SPEC", "DATA": "DATA_SPEC", "ER": "DATA_SPEC",
    "FR": "FUNCTIONAL", "REQ": "FUNCTIONAL", "SR": "FUNCTIONAL",
    "SRS": "FUNCTIONAL", "FRS": "FUNCTIONAL", "IMP": "FUNCTIONAL",
}

_NFR_TEXT_TOKENS = (
    "performance", "latency", "throughput", "response time", "concurrent users",
    "availability", "uptime", "sla", "rto", "rpo",
    "security", "encryption", "audit", "compliance", "gdpr", "hipaa",
    "accessibility", "wcag", "screen reader", "aria",
)
_UC_TEXT_TOKENS = ("actor:", "main flow", "primary flow", "use case", "as a ", "user story")
_BR_TEXT_TOKENS = ("business rule", "shall calculate", "shall enforce", "policy:")


def _classify_requirement_type(req: Dict) -> str:
    """Return one of USE_CASE | NFR | FUNCTIONAL | BUSINESS_RULE | UI_SPEC | DATA_SPEC.

    Prefers the ID prefix, falls back to text-token heuristics.
    """
    rid = str(req.get("id") or "").upper()
    m = re.match(r"^([A-Z]+)", rid)
    if m and m.group(1) in _REQ_TYPE_ID_PREFIX:
        return _REQ_TYPE_ID_PREFIX[m.group(1)]
    text = (str(req.get("text") or "") + " " + str(req.get("category") or "")).lower()
    if any(t in text for t in _UC_TEXT_TOKENS):
        return "USE_CASE"
    if any(t in text for t in _NFR_TEXT_TOKENS):
        return "NFR"
    if any(t in text for t in _BR_TEXT_TOKENS):
        return "BUSINESS_RULE"
    cat = (req.get("category") or "").lower()
    if cat == "ui":
        return "UI_SPEC"
    if cat == "data":
        return "DATA_SPEC"
    return "FUNCTIONAL"


def _negative_scenario_for(req_type: str) -> str:
    """Pick a sensible default negative-case scenario when the LLM didn't."""
    if req_type == "NFR":
        return "availability"
    if req_type == "USE_CASE":
        return "invalid_input"
    if req_type == "BUSINESS_RULE":
        return "business_rule_violation"
    if req_type == "UI_SPEC":
        return "invalid_input"
    return "invalid_input"


# iter-15.4 — Uploaded test-case extractor + human-readable summariser
_UPLOADED_TC_ID_PATTERN = re.compile(
    r"\b(TC[-_]?\d{2,5}|TEST[-_]?CASE[-_]?\d{2,5}|TC[-_]?[A-Z]{2,6}[-_]?\d{2,5})\b",
    re.IGNORECASE,
)
_GHERKIN_SCENARIO = re.compile(
    r"(?im)^\s*(?:Scenario(?:\s+Outline)?):\s*(.+?)$"
)
_GHERKIN_STEP = re.compile(
    r"(?im)^\s*(Given|When|Then|And|But)\s+(.+?)$"
)


def _extract_uploaded_test_cases(doc_files: List[Dict]) -> List[Dict]:
    """Detect test cases already authored by the SME in the uploaded docs.

    Handles:
    - Explicit IDs (TC-001, TC_LOGIN_001, TEST-CASE-042)
    - Gherkin `.feature` blocks (Scenario / Given / When / Then / And / But)
    - Markdown/plain sections titled 'Test Case', 'Scenario', 'Given ... When ... Then'

    Returns [{id, title, human_summary, steps, expected_result, source, source_kind}].
    These are fed back into the verifier prompt as EXISTING TEST CASES (must-reuse)
    and merged into the final report so the SME's authoring is honoured.
    """
    out: List[Dict] = []
    seen_ids: set = set()
    tc_seq = 0

    def _next_id() -> str:
        nonlocal tc_seq
        tc_seq += 1
        return f"UTC-{tc_seq:03d}"

    for f in doc_files or []:
        path = str(f.get("path", "") or "")
        content = str(f.get("content", "") or "")
        if not content.strip():
            continue
        lower_path = path.lower()

        # ── Gherkin (.feature) files or embedded Gherkin blocks ──
        if lower_path.endswith(".feature") or ("scenario:" in content.lower() and "given" in content.lower()):
            for m in _GHERKIN_SCENARIO.finditer(content):
                title = m.group(1).strip()[:200]
                # Grab following steps until the next scenario/EOF
                tail_start = m.end()
                next_scen = _GHERKIN_SCENARIO.search(content, tail_start)
                block = content[tail_start:(next_scen.start() if next_scen else len(content))]
                steps: List[str] = []
                given_txt = when_txt = then_txt = ""
                for sm in _GHERKIN_STEP.finditer(block):
                    kw = sm.group(1).capitalize()
                    body = sm.group(2).strip()[:280]
                    steps.append(f"{kw} {body}")
                    if kw == "Given" and not given_txt:
                        given_txt = body
                    elif kw == "When" and not when_txt:
                        when_txt = body
                    elif kw == "Then" and not then_txt:
                        then_txt = body
                if not steps:
                    continue
                tc_id = _next_id()
                if tc_id in seen_ids:
                    continue
                seen_ids.add(tc_id)
                human = _human_summary_from_gwt(given_txt, when_txt, then_txt, title)
                out.append({
                    "id": tc_id,
                    "title": title or "Scenario from feature file",
                    "human_summary": human,
                    "steps": steps[:20],
                    "expected_result": then_txt[:400] or "As described in the Then step.",
                    "source": path,
                    "source_kind": "gherkin",
                })
                if len(out) > 200:
                    return out[:200]
            continue

        # ── Explicit TC-XXX markers with following block ──
        for m in _UPLOADED_TC_ID_PATTERN.finditer(content):
            raw_id = m.group(1).upper().replace("_", "-")
            if raw_id in seen_ids:
                continue
            # Grab up to 800 chars after the ID for context
            start = m.end()
            block = content[start:start + 900]
            # Stop at next TC-XXX marker
            nxt = _UPLOADED_TC_ID_PATTERN.search(block)
            if nxt:
                block = block[:nxt.start()]
            block = block.strip(" :.-\n")
            if len(block) < 20:
                continue
            seen_ids.add(raw_id)
            # First non-empty line is title
            lines = [ln.strip(" -*.\t") for ln in block.split("\n") if ln.strip()]
            title = (lines[0] if lines else raw_id)[:200]
            # Steps: subsequent numbered / bulleted lines
            steps = [ln[:280] for ln in lines[1:11] if len(ln) > 3]
            # Expected: look for "Expected"/"Result" line
            expected = ""
            for ln in lines:
                low = ln.lower()
                if low.startswith(("expected", "result", "outcome")):
                    expected = ln.split(":", 1)[-1].strip()[:400]
                    break
            human = _human_summary_from_block(title, steps, expected)
            out.append({
                "id": raw_id,
                "title": title,
                "human_summary": human,
                "steps": steps,
                "expected_result": expected or "As documented in the source.",
                "source": path,
                "source_kind": "explicit_id",
            })
            if len(out) > 200:
                return out[:200]

    return out[:200]


def _human_summary_from_gwt(given: str, when: str, then: str, title: str) -> str:
    """Format a plain-English 'Given/When/Then' one-liner for business users."""
    parts = []
    if given:
        parts.append(f"Given {given.rstrip('.')}")
    if when:
        parts.append(f"when {when.rstrip('.')}")
    if then:
        parts.append(f"then {then.rstrip('.')}")
    if parts:
        return ", ".join(parts) + "."
    return (title or "Verify documented behaviour.").strip().rstrip(".") + "."


def _human_summary_from_block(title: str, steps: List[str], expected: str) -> str:
    """Fallback plain-English summary when only steps/expected are known."""
    first_step = (steps[0] if steps else title).strip().rstrip(".")
    outcome = (expected or "the documented outcome occurs").strip().rstrip(".")
    return f"When the tester {first_step[0].lower() + first_step[1:] if first_step else 'follows the steps'}, then {outcome}."


def _derive_human_summary(tc: Dict) -> str:
    """Build a human-readable Given/When/Then summary for a test case.

    Prefers the LLM's own human_summary. Falls back to composing one from
    (preconditions | test_data | steps | expected_result). Kept plain-English
    so business users can follow the report without engineering context.
    """
    llm_hs = str(tc.get("human_summary") or tc.get("plain_summary") or "").strip()
    if llm_hs:
        return llm_hs[:400]

    given = str(tc.get("preconditions") or "").strip().rstrip(".")
    steps = tc.get("steps") or []
    when = ""
    if isinstance(steps, list) and steps:
        # Strip leading "1." / "Step 1:" numbering
        raw = re.sub(r"^\s*(?:\d+[.)]|step\s*\d+:)\s*", "", str(steps[0]), flags=re.IGNORECASE).strip()
        when = raw.rstrip(".")
    if not when:
        when = str(tc.get("title") or "").rstrip(".")
    # iter-15.8 — guard: if `when` is a short garbage token (< 15 chars or
    # looks like an unhelpful noun phrase from a bad extractor), fall
    # back to the title so we don't produce sentences like "when the
    # tester investment Master, then …".
    if len(when.strip()) < 15 or re.match(r"^[A-Z][a-z]+\s+[A-Z][a-z]+$", when.strip()):
        title = str(tc.get("title") or "").rstrip(".")
        if title:
            when = title
    then = str(tc.get("expected_result") or "the documented outcome occurs").rstrip(".")

    case_type = tc.get("case_type") or "positive"
    parts = []
    if given:
        parts.append(f"Given {given}")
    if when:
        connector = "when" if given else "When"
        parts.append(f"{connector} {when[0].lower() + when[1:] if when else 'the flow runs'}")
    parts.append(f"then {then[0].lower() + then[1:] if then else 'the expected outcome occurs'}")

    text = ", ".join(parts).strip() + "."
    if case_type == "negative":
        text += " (The system must reject the attempt and preserve state.)"
    return text[:400]


def _extract_test_cases(
    parsed: dict,
    requirements: Optional[List[Dict]] = None,
    uploaded_test_cases: Optional[List[Dict]] = None,
) -> List[dict]:
    """iter-15.4 — Senior-Test-Analyst normalizer with human summaries + SME merge.

    - Preserves + normalizes case_type, requirement_type, scenario, priority.
    - Attaches a plain-English `human_summary` (Given/When/Then) to every row.
    - Merges SME-uploaded test cases (from `uploaded_test_cases`) at the top of
      the list, marked `source="uploaded"`, so the SME's authoring is never
      dropped.
    - Synthesizes BOTH positive + negative BLOCKED placeholders for every
      requirement the LLM skipped (UC/FR/BR); NFRs get a single verification.
    """
    if not isinstance(parsed, dict):
        rows_raw = []
    else:
        rows_raw = (
            parsed.get("test_cases")
            or parsed.get("testCases")
            or parsed.get("tests")
            or []
        )
    if not isinstance(rows_raw, list):
        rows_raw = []

    req_by_id: Dict[str, Dict] = {}
    if requirements:
        for r in requirements:
            rid = str(r.get("id") or "").strip()
            if rid:
                req_by_id[rid] = r

    out: List[Dict] = []
    # Track seen (requirement_id, case_type) pairs so synthesis can top up
    # missing halves (e.g. LLM authored positive but skipped negative).
    seen_pairs: set = set()
    tc_counter = 0

    def _next_id() -> str:
        nonlocal tc_counter
        tc_counter += 1
        return f"TC-{tc_counter:03d}"

    def _norm_case_type(raw: str, title: str) -> str:
        r = (raw or "").strip().lower()
        if r in _TC_CASE_TYPE_MAP:
            return _TC_CASE_TYPE_MAP[r]
        # Infer from title tokens
        t = (title or "").lower()
        if any(k in t for k in ("reject", "invalid", "unauthorized", "forbidden",
                                "missing", "duplicate", "not found", "boundary",
                                "malformed", "fails", "denies", "blocks", "prevents")):
            return "negative"
        return "positive"

    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        tc_id = str(row.get("id") or _next_id()).strip() or _next_id()
        req_id = str(_pick_first_key(row, "requirement_id", "requirementId", "requirement") or "").strip()
        joined = req_by_id.get(req_id, {})

        title = str(row.get("title") or row.get("name") or "")[:200]
        case_type = _norm_case_type(str(row.get("case_type") or row.get("caseType") or ""), title)
        seen_pairs.add((req_id, case_type))

        # Requirement typing — prefer LLM's answer, fall back to KB classification.
        req_type_raw = str(row.get("requirement_type") or row.get("requirementType") or "").upper().strip()
        allowed_types = {"USE_CASE", "NFR", "FUNCTIONAL", "BUSINESS_RULE", "UI_SPEC", "DATA_SPEC"}
        if req_type_raw not in allowed_types:
            req_type_raw = _classify_requirement_type(joined) if joined else "FUNCTIONAL"

        scenario = str(row.get("scenario") or "").strip().lower() or (
            "happy_path" if case_type == "positive" else _negative_scenario_for(req_type_raw)
        )

        priority = str(row.get("priority") or "").upper().strip()
        if priority not in {"P0", "P1", "P2", "P3"}:
            priority = "P2"

        status_raw = str(row.get("status") or row.get("result") or "").strip().lower()
        status = _TC_STATUS_MAP.get(status_raw, status_raw.upper() if status_raw else "BLOCKED")
        if status not in {"PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"}:
            status = "BLOCKED"

        steps = row.get("steps") or row.get("procedure") or []
        if isinstance(steps, str):
            steps = [s.strip() for s in re.split(r"[\n;]\s*", steps) if s.strip()]
        elif not isinstance(steps, list):
            steps = []

        evidence = row.get("evidence") or row.get("code_locations") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        elif not isinstance(evidence, list):
            evidence = []

        sev = str(row.get("severity") or "").strip().upper()
        if sev not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
            sev = "MEDIUM"

        cat = str(row.get("category") or "").strip().lower() or (joined.get("category") or "functional")

        # iter-15.7 — Guard: some LLMs emit the bare requirement ID as the
        # title (e.g. "FR-INVEST-004"). That's meaningless in the report.
        # Replace with a verb-first scenario title derived from the
        # requirement text so the row is still human-readable.
        _bare_id_re = re.compile(r"^[A-Z]{2,6}[-_][A-Z0-9-]{2,}$", re.IGNORECASE)
        if title and _bare_id_re.match(title.strip()):
            title = ""
        if not title:
            req_text = str(joined.get("text") or "").strip()
            # Pluck a verb-phrase from the requirement — first "shall <verb>"
            # or the head clause.
            hint = ""
            if req_text:
                m_shall = re.search(r"shall\s+([a-z][^.,;]{5,80})", req_text, re.I)
                if m_shall:
                    hint = m_shall.group(1).strip()
                else:
                    hint = req_text.split(".")[0].strip()[:80]
            verb = "Reject" if case_type == "negative" else "Verify"
            if hint:
                title = f"{verb} {hint}"[:200]
            else:
                title = (
                    f"{verb} {req_id or 'requirement'} — "
                    f"{scenario.replace('_', ' ')}"
                )[:200]

        row_out = {
            "id": tc_id,
            "requirement_id": req_id,
            "requirement_type": req_type_raw,
            "case_type": case_type,
            "scenario": scenario,
            "priority": priority,
            "doc_type": joined.get("doc_type", ""),
            "doc_type_label": joined.get("doc_type_label", ""),
            "title": title,
            "category": cat,
            "preconditions": str(row.get("preconditions") or "")[:500],
            "steps": [str(s)[:300] for s in steps][:20],
            "test_data": str(row.get("test_data") or row.get("testData") or "")[:400],
            "expected_result": str(row.get("expected_result") or row.get("expected") or "")[:500],
            "actual_result": str(row.get("actual_result") or row.get("actual") or "")[:500],
            "status": status,
            "severity": sev,
            "evidence": [str(e)[:200] for e in evidence][:10],
            "notes": str(row.get("notes") or "")[:400],
            "source": str(row.get("source") or "llm"),
            "human_summary": str(row.get("human_summary") or row.get("plain_summary") or "")[:400],
        }
        # Guarantee a plain-English one-liner for every row
        row_out["human_summary"] = _derive_human_summary(row_out)
        out.append(row_out)

    # ── Synthesis: guarantee positive + negative coverage per requirement ──
    def _synth(req: Dict, case_type: str, req_type: str) -> Dict:
        rid = str(req.get("id") or "").strip()
        req_text = str(req.get("text") or "")[:500]
        is_neg = case_type == "negative"
        scenario = "happy_path" if not is_neg else _negative_scenario_for(req_type)

        # iter-15.8 — Pull the actual "shall <verb-phrase>" from the requirement
        # so synthesized rows read like real test cases, not generic filler.
        shall_phrase = ""
        m_shall = re.search(r"shall\s+([a-z][^.,;]{5,120})", req_text, re.I)
        if m_shall:
            shall_phrase = m_shall.group(1).strip().rstrip(".")
        elif req_text:
            shall_phrase = req_text.split(".")[0].strip()[:120]
        subject = shall_phrase or "the documented behaviour"

        if req_type == "NFR":
            title = f"Verify NFR — {subject}"[:200]
            steps = [
                f"1. Identify the numeric threshold declared for {rid} in the SRS (latency / availability / security control).",
                "2. Instrument the target flow with the appropriate measurement (APM probe, chaos test, static scan, penetration test, etc.).",
                "3. Exercise the flow under production-representative load / conditions.",
                "4. Compare the observed metric against the documented threshold and record pass/fail with the raw measurement.",
            ]
            expected = req_text or f"System meets the NFR threshold declared in {rid}."
        elif is_neg:
            if req_type == "USE_CASE":
                title = f"Reject unauthorized or invalid attempt to {subject}"[:200]
                steps = [
                    f"1. Set up the environment described in the pre-conditions of {rid}, then remove ONE required condition (missing role / missing field / stale token).",
                    "2. Invoke the documented interface for the use case with the tampered request.",
                    "3. Assert the system responds with the documented error code (401/403/400/409) and no persisted change on the primary aggregate.",
                    "4. Verify the audit log records the rejected attempt with actor + timestamp + reason.",
                ]
                expected = f"System rejects the attempt to {subject} with the documented error and preserves state."
            elif req_type == "BUSINESS_RULE":
                title = f"Reject violation of business rule — {subject}"[:200]
                steps = [
                    f"1. Compose an input that violates the business rule declared in {rid} (e.g. duplicate key, out-of-range value, forbidden transition).",
                    "2. Submit through the documented interface.",
                    "3. Assert the rule fires: request rejected, documented error surfaced, no state change on the primary entity, no downstream events emitted.",
                ]
                expected = f"Business rule {rid} is enforced: the violating attempt to {subject} is blocked."
            else:
                title = f"Reject invalid attempt to {subject}"[:200]
                steps = [
                    f"1. Prepare a payload that violates the documented contract for {rid} (malformed / out-of-range / missing-required).",
                    "2. Submit through the documented interface (API / UI / job).",
                    "3. Assert a 4xx response with the documented validation error, and confirm no persisted change.",
                ]
                expected = f"Invalid inputs targeting {rid} are rejected with a documented error."
        else:
            if req_type == "USE_CASE":
                title = f"Verify main flow — {subject}"[:200]
                steps = [
                    f"1. Authenticate as the actor documented in {rid} and establish the pre-conditions listed in the use case.",
                    f"2. Perform the main flow of {rid} step-by-step with valid representative data.",
                    "3. Assert the documented outcome + persisted state on the primary aggregate + response code + audit entry.",
                    f"4. Verify every post-condition declared in {rid} holds after the flow completes.",
                ]
                expected = req_text or f"Main flow of {rid} completes and produces the documented outcome."
            elif req_type == "BUSINESS_RULE":
                title = f"Verify enforcement — {subject}"[:200]
                steps = [
                    f"1. Prepare a valid payload that exercises the business rule declared in {rid}.",
                    "2. Invoke the documented interface and observe the rule enforcement site in the code.",
                    "3. Assert the rule is applied consistently across UI, API, and DB layers (defence in depth).",
                ]
                expected = req_text or f"Business rule {rid} is enforced end-to-end."
            else:
                title = f"Verify — {subject}"[:200]
                steps = [
                    f"1. Prepare valid inputs that match the documented data contract for {rid}.",
                    "2. Invoke the documented interface (API / UI / job).",
                    "3. Assert the documented outcome + state change + response code.",
                ]
                expected = req_text or f"{rid} behaves as documented."

        preconditions = ""
        if req_type == "USE_CASE" and req_text:
            preconditions = (
                f"Actor is authenticated with the role documented in {rid}; "
                f"any master-data / seed-state required by the pre-conditions of {rid} is in place."
            )

        synth_row = {
            "id": _next_id(),
            "requirement_id": rid,
            "requirement_type": req_type,
            "case_type": case_type,
            "scenario": scenario,
            "priority": "P1" if req_type in ("USE_CASE", "NFR", "BUSINESS_RULE") else "P2",
            "doc_type": req.get("doc_type", ""),
            "doc_type_label": req.get("doc_type_label", ""),
            "title": title,
            "category": req.get("category") or ("security" if req_type == "NFR" else "functional"),
            "preconditions": preconditions,
            "steps": steps,
            "test_data": "" if not is_neg else "Malformed / missing-required / unauthorized fixtures.",
            "expected_result": expected,
            "actual_result": "Not evaluated — no matching code evidence was found in the uploaded source.",
            "status": "BLOCKED",
            "severity": "HIGH" if req_type in ("USE_CASE", "NFR") else "MEDIUM",
            "evidence": [],
            "notes": (
                f"Auto-generated placeholder — verifier did not author this {case_type} case for {rid}. "
                f"Requirement text: {req_text[:200]}"
            ),
            "synthesized": True,
            "source": "synthesized",
        }
        synth_row["human_summary"] = _derive_human_summary(synth_row)
        return synth_row

    for r in (requirements or []):
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        req_type = _classify_requirement_type(r)
        # NFRs need at least one verification case; UI/DATA specs get positive only.
        needed = ["positive"]
        if req_type in ("USE_CASE", "FUNCTIONAL", "BUSINESS_RULE"):
            needed.append("negative")
        # If the LLM authored nothing for this requirement, both halves synthesize.
        # If it authored one half, we top up the missing half.
        for ct in needed:
            if (rid, ct) not in seen_pairs:
                out.append(_synth(r, ct, req_type))
                seen_pairs.add((rid, ct))

    # ── Merge SME-uploaded test cases (iter-15.4) ──
    # Uploaded TCs go to the TOP of the list so users see their own work first.
    # We skip any whose ID already appears (LLM reused it).
    if uploaded_test_cases:
        existing_ids = {str(t.get("id") or "").upper() for t in out}
        merged_uploaded: List[Dict] = []
        for utc in uploaded_test_cases:
            uid = str(utc.get("id") or "").upper()
            if not uid or uid in existing_ids:
                continue
            existing_ids.add(uid)
            steps_u = utc.get("steps") or []
            if not isinstance(steps_u, list):
                steps_u = [str(steps_u)]
            merged_row = {
                "id": uid,
                "requirement_id": str(utc.get("requirement_id") or ""),
                "requirement_type": str(utc.get("requirement_type") or "FUNCTIONAL"),
                "case_type": str(utc.get("case_type") or "positive"),
                "scenario": str(utc.get("scenario") or "documented_scenario"),
                "priority": str(utc.get("priority") or "P1"),
                "doc_type": "",
                "doc_type_label": "SME-authored",
                "title": str(utc.get("title") or uid)[:200],
                "category": str(utc.get("category") or "functional"),
                "preconditions": "",
                "steps": [str(s)[:300] for s in steps_u][:20],
                "test_data": "",
                "expected_result": str(utc.get("expected_result") or "")[:500],
                "actual_result": "Sourced from uploaded SME test case — coverage evaluated by code inspection.",
                "status": "BLOCKED",
                "severity": "MEDIUM",
                "evidence": [],
                "notes": f"Uploaded from {utc.get('source', 'documentation')} ({utc.get('source_kind', 'sme')}).",
                "source": "uploaded",
                "human_summary": str(utc.get("human_summary") or "")[:400],
            }
            merged_row["human_summary"] = _derive_human_summary(merged_row)
            merged_uploaded.append(merged_row)
        if merged_uploaded:
            out = merged_uploaded + out

    return out


def _compute_test_accuracy(test_cases: List[dict]) -> Dict[str, Any]:
    """iter-14.98 — Accuracy = PASS / (PASS + FAIL + BLOCKED). NOT_APPLICABLE
    cases are excluded from the denominator, matching Living System semantics.
    iter-15.3 — Also emits positive / negative counts.
    """
    total = len(test_cases)
    passed = sum(1 for t in test_cases if t.get("status") == "PASS")
    failed = sum(1 for t in test_cases if t.get("status") == "FAIL")
    blocked = sum(1 for t in test_cases if t.get("status") == "BLOCKED")
    na = sum(1 for t in test_cases if t.get("status") == "NOT_APPLICABLE")
    positive = sum(1 for t in test_cases if t.get("case_type") == "positive")
    negative = sum(1 for t in test_cases if t.get("case_type") == "negative")
    denom = passed + failed + blocked
    accuracy = round(100.0 * passed / denom, 1) if denom else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "not_applicable": na,
        "positive": positive,
        "negative": negative,
        "accuracy_pct": accuracy,
    }


async def _run_gap_analysis_background(analysis_id: str, code_files: List[Dict], doc_files: List[Dict], model: str = None):
    """Two-phase gap analysis: build KB → verify against docs.

    Phase 1 (deterministic): extract code entities + doc requirements + build
    UI→API→DB traceability map. Persist to `tools_kb` for FE inspection.

    Phase 2 (LLM): feed the KB summary into the verifier prompt
    (`tools.gap_verifier`, falls back to `tools.gap_analyzer`) and normalize
    the response into severity-tagged gaps + coverage matrix.
    """
    # iter-15.6 — progress model: each phase writes a coarse pct + a human
    # label so the FE can render a progress bar instead of an opaque
    # "Running…" state. Sub-phases within analyzing carry the LLM through
    # 55 → 90% since that's the longest wait.
    _PHASE_META = {
        "queued":       (5,  "Queued for analysis"),
        "building_kb":  (15, "Building knowledge base from uploads"),
        "extracting":   (35, "Extracting requirements & code entities"),
        "analyzing":    (55, "Preparing LLM verification prompt"),
        "llm_call":     (65, "LLM analysing code vs. requirements"),
        "parsing":      (88, "Parsing LLM response & normalising test cases"),
        "finalizing":   (95, "Finalising coverage matrix & gaps"),
        "completed":    (100, "Analysis complete"),
        "failed":       (100, "Analysis failed"),
    }

    async def _phase(name: str, extra: Optional[Dict] = None):
        pct, label = _PHASE_META.get(name, (None, name))
        upd = {
            "phase": name,
            "phase_label": label,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if pct is not None:
            upd["progress_pct"] = pct
        if extra:
            upd.update(extra)
        await gap_analyses.update_one({"_id": analysis_id}, {"$set": upd})

    try:
        # ── Phase 1a: Build KB ─────────────────────────────────────────
        await _phase("building_kb", {"status": "analyzing"})
        kb = build_tools_kb(code_files, doc_files)
        kb_summary = summarize_kb_for_ui(kb)

        # Persist KB (both full-ish and summary variants)
        await tools_kb.update_one(
            {"_id": analysis_id},
            {"$set": {
                "_id": analysis_id,
                "owner": "gap_analyzer",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "summary": kb_summary,
                "stats": kb["stats"],
            }},
            upsert=True,
        )

        await _phase("extracting", {
            "code_entity_count": kb["stats"]["entities"],
            "requirement_count": kb["stats"]["requirements"],
            "traceability_stats": kb["traceability"]["stats"],
        })

        # ── Phase 1b: If no requirements, degrade gracefully ──────────
        if kb["stats"]["requirements"] == 0:
            # LLM still runs, but flag lack of explicit reqs
            print(f"[gap-analyzer:{analysis_id}] no explicit requirements extracted — LLM will infer from raw doc")

        # ── Phase 2: LLM verification ─────────────────────────────────
        await _phase("analyzing")

        # Preferred key: tools.gap_verifier; fallback: tools.gap_analyzer
        prompt_instructions = (
            await _get_prompt("tools.gap_verifier")
            or await _get_prompt("tools.gap_analyzer")
            or "You are a senior QA engineer. Compare code entities vs documentation requirements and return a JSON gap report."
        )

        # Raw doc excerpts as fallback for LLM when explicit reqs are sparse
        doc_excerpts_parts = []
        for f in doc_files[:15]:
            content = (f.get("content", "") or "")[:6000]
            doc_excerpts_parts.append(f"=== {f['path']} ===\n{content}")
        doc_excerpts = "\n\n".join(doc_excerpts_parts)[:45000]

        kb_ctx = render_kb_for_prompt(kb, max_chars=45000)

        # iter-15.4 — SME-uploaded test cases (must-reuse)
        uploaded_tcs = _extract_uploaded_test_cases(doc_files)
        if uploaded_tcs:
            _lines = []
            for utc in uploaded_tcs[:60]:
                _lines.append(
                    f"- {utc['id']}: {utc['title']}\n"
                    f"  Summary: {utc.get('human_summary','')}\n"
                    f"  Source: {utc.get('source','')} ({utc.get('source_kind','')})"
                )
            uploaded_tcs_block = (
                "===== EXISTING TEST CASES (uploaded by SME — REUSE these IDs; do NOT re-number them) =====\n"
                + "\n".join(_lines)
                + "\n\nFor each existing test case above: reuse the ID verbatim, keep the intent, "
                "attach case_type/requirement_type/priority, evaluate coverage against the code KB, "
                "and set status=PASS/FAIL/BLOCKED with evidence. Author additional test cases only "
                "for requirements NOT already covered by the existing set."
            )
            print(f"[gap-analyzer:{analysis_id}] extracted {len(uploaded_tcs)} SME test cases from uploads")
        else:
            uploaded_tcs_block = ""

        final_prompt = f"""{prompt_instructions}

===== KNOWLEDGE BASE (extracted from uploaded code + docs) =====
{kb_ctx}

===== RAW DOCUMENTATION EXCERPTS (for LLM to catch anything the regex missed) =====
{doc_excerpts}

{uploaded_tcs_block}

Return ONLY a valid JSON object matching the output_schema. Be exhaustive:
cover every documented requirement, mark each as implemented or missing,
flag any code entities that are not documented, populate coverage_matrix fully.

TEST-CASE AUTHORING — ACT AS A SENIOR TEST ANALYST:
• The OBJECTIVE is to detect GAPS between the SRS/FRS requirements and
  the implementation. Every documented use case, workflow step, business
  rule, validation, and NFR must have at least one test case that proves
  whether the code satisfies it. If code cannot be located, mark the row
  BLOCKED — that IS the gap.
• Classify each requirement (requirement_type) as USE_CASE, NFR,
  FUNCTIONAL, BUSINESS_RULE, UI_SPEC, or DATA_SPEC.

• USE_CASE requirements (IDs starting with UC-*) are the PRIMARY source
  of test cases. For each UC-* requirement, generate a full "test suite"
  organised around its documented structure:
    (a) ONE positive test case that walks the MAIN FLOW end-to-end
        with realistic test data (case_type="positive",
        scenario="happy_path").
    (b) ONE negative test case per documented ALTERNATE / EXCEPTION FLOW
        (case_type="negative", scenario ∈ invalid_input |
        missing_required | unauthorized | authorized_wrong_role |
        duplicate | not_found | boundary | concurrency |
        business_rule_violation).
    (c) ONE test case per PRE-CONDITION that must hold before the flow
        starts (verify the guard rejects the request when the
        pre-condition is missing).
    (d) ONE test case per POST-CONDITION that must hold after the flow
        completes (verify the observable state change).
    (e) ONE test case per BUSINESS RULE (BR-*) referenced by the UC
        (verify enforcement in the code path).
    (f) ONE test case per VALIDATION rule / field constraint (verify
        rejection of the invalid input).
  Preserve the UC's workflow ordering inside `steps` — numbered "1. …",
  "2. …" — so a reviewer can follow the flow.

• FUNCTIONAL / BUSINESS_RULE requirements: at least one positive AND
  one negative test case, following the same negative-scenario menu.

• NFR requirements: iterate the ENTIRE NFR section item-by-item
  (performance, availability, security, usability, compatibility,
  maintainability, scalability, portability, reliability,
  observability). Each item gets ≥ 1 verification test case with a
  CONCRETE numeric threshold (e.g. p95 latency < 500 ms, RTO ≤ 4 h,
  99.9% uptime) and an observation method (load test, chaos test,
  static scan, code review, penetration test, etc.). Do NOT skip an
  NFR item.

• FIELDS — mandatory on EVERY test case row:
    - id                : short unique TC ID (TC-###)
    - requirement_id    : the source requirement (UC-*, FR-*, NFR-*, BR-*)
    - requirement_type  : one of the classifications above
    - case_type         : "positive" or "negative"
    - scenario          : one of the enumerated scenarios above
    - priority          : P0 (blocker), P1 (critical), P2 (major), P3 (minor)
    - title             : VERB-FIRST English sentence describing WHAT is
                          being verified — NEVER the bare requirement ID.
                          Example: "Create pay-grade source with valid
                          payload succeeds" or "Reject login when
                          password missing". Titles like "FR-INVEST-004"
                          are FORBIDDEN.
    - human_summary     : plain-English Given / when / then one-liner
                          understandable by a business user (no jargon,
                          no code identifiers). This is MANDATORY.
    - preconditions     : Given clause — the state that must hold before
                          the test starts (roles, data, config).
    - steps             : numbered workflow (["1. ...", "2. ...", ...]).
    - test_data         : the concrete input payload (JSON-ish or key=value).
    - expected_result   : Then clause — the observable outcome
                          + state change.
    - notes             : the business rule / validation being enforced
                          OR the NFR metric+threshold+observation method.
    - status            : PASS (with cited evidence), FAIL (code diverges
                          from spec), BLOCKED (no code path found — the
                          gap), NOT_APPLICABLE.
    - evidence          : ["path/to/file:Symbol", ...] — REQUIRED when
                          status == PASS. Empty when status == BLOCKED.
    - severity          : CRITICAL / HIGH / MEDIUM / LOW — how bad the
                          gap is if the test fails.

• Aim for AT LEAST 3–5 test cases per USE_CASE requirement and
  AT LEAST 2 per FUNCTIONAL / BUSINESS_RULE requirement. Small
  requirement sets should produce dense, high-quality coverage; do not
  return an empty test_cases array unless there are literally zero
  requirements.
"""

        # ── Sub-phase: LLM call (longest step; update pct so FE bar moves)
        await _phase("llm_call")

        llm_response = await fabric_call(
            messages=[{"role": "user", "content": final_prompt}],
            model=model,
            agent_key="gap_analyzer",
            temperature=0.1,
            # iter-15.8 — bump from 16k to give the LLM room to author dense,
            # UC-organised test cases with steps + test_data + notes per row.
            max_tokens=24000,
            response_format={"type": "json_object"},
        )

        response_text = ""
        if isinstance(llm_response, dict):
            response_text = llm_response.get("content", "") or ""
        elif isinstance(llm_response, str):
            response_text = llm_response
        else:
            response_text = str(llm_response) if llm_response else ""

        print(f"[gap-analyzer:{analysis_id}] LLM response length: {len(response_text)}")

        # ── Sub-phase: parse LLM output ────────────────────────────────
        await _phase("parsing")

        # ── Parse + normalize LLM output ──────────────────────────────
        parsed = _extract_json_object(response_text) or {}
        gaps = _flatten_gaps(parsed)
        # iter-14.96 — join requirements (with doc_type/category) into the
        # matrix, and synthesize missing rows for anything the LLM skipped.
        matrix = _extract_coverage_matrix(parsed, requirements=kb.get("requirements") or [])

        # iter-15.8 — Reconcile gaps vs matrix. Previously the LLM could
        # return an empty gaps[] AND a matrix showing 98.8% missing rows,
        # producing the absurd "0 gaps / 1.2% coverage" result. Every
        # matrix row with implemented=False must be reflected as a gap.
        existing_gap_reqs = {
            str(g.get("doc_reference") or "").strip().upper()
            for g in gaps if g.get("doc_reference")
        }
        _severity_for = lambda req_type: (
            "critical" if req_type in ("NFR", "USE_CASE") else "major"
        )
        for row in matrix:
            if row.get("implemented"):
                continue
            rid = str(row.get("requirement_id") or "").strip()
            if not rid or rid.upper() in existing_gap_reqs:
                continue
            req_txt = row.get("requirement_text") or ""
            cat = (row.get("category") or "").lower()
            # Classify the synthesized gap type.
            if cat == "security" or "security" in req_txt.lower():
                sev = "critical"
            elif cat == "performance" or "performance" in req_txt.lower():
                sev = "major"
            else:
                sev = "major"
            gaps.append({
                "id": f"GAP-M-{len(gaps) + 1:03d}",
                "type": "MISSING_IMPLEMENTATION",
                "severity": sev,
                "title": f"{rid} — not evidenced in the uploaded code",
                "description": (
                    f"Requirement {rid} is documented in the SRS/FRS but the "
                    f"verifier could not find any code path that implements it. "
                    f"Requirement text: {req_txt[:400]}"
                ),
                "location": ", ".join(row.get("code_locations") or []) or "NOT EVIDENCED",
                "doc_reference": rid,
                "expected": req_txt[:500],
                "actual": row.get("notes") or "No matching code path found in the uploaded source.",
                "recommendation": (
                    "Confirm whether this requirement is genuinely unimplemented "
                    "or the source tree is incomplete. If unimplemented, plan and "
                    "estimate the effort against the target stack; otherwise "
                    "re-upload with the missing modules and re-run the analysis."
                ),
            })
            existing_gap_reqs.add(rid.upper())

        # iter-14.98 — extract & normalize test cases, synthesize BLOCKED
        # placeholders for any requirement the LLM didn't cover.
        test_cases = _extract_test_cases(
            parsed,
            requirements=kb.get("requirements") or [],
            uploaded_test_cases=uploaded_tcs,
        )
        tc_stats = _compute_test_accuracy(test_cases)

        critical = sum(1 for g in gaps if g["severity"] == "critical")
        major = sum(1 for g in gaps if g["severity"] == "major")
        minor = sum(1 for g in gaps if g["severity"] == "minor")

        # iter-14.94 — Honest coverage computation.
        # Priority: (1) coverage_matrix ratio (deterministic) → (2) LLM summary
        # ONLY when we have supporting evidence → (3) fall back to 0 with warning.
        # Never fabricate coverage from "no gaps found" — that hides bugs when
        # LLM produces empty output.
        warnings: List[str] = []
        coverage_pct = 0.0
        summary_block = parsed.get("summary")

        if matrix:
            covered = sum(1 for r in matrix if r.get("implemented"))
            coverage_pct = round((covered / len(matrix)) * 100, 1)
        elif isinstance(summary_block, dict):
            cp = summary_block.get("coverage_percentage") or summary_block.get("coverage_pct") or summary_block.get("coverage")
            try:
                coverage_pct = float(cp) if cp is not None else 0.0
            except Exception:
                coverage_pct = 0.0
            if coverage_pct and not gaps:
                # LLM claimed coverage but returned no evidence — untrusted
                warnings.append(
                    "LLM reported coverage but returned no coverage matrix and no gap details; "
                    "coverage may be unreliable."
                )
        else:
            coverage_pct = 0.0

        # KB-level diagnostics that gate the honesty of any coverage number.
        if kb["stats"].get("is_ui_only"):
            warnings.append(
                "Uploaded source appears to be frontend/UI only — no backend routes or "
                "database tables were extracted. Gap analysis against SRS/FRS cannot verify "
                "server-side behaviour. Please upload backend code as well."
            )
            # Force coverage to 0: we CANNOT claim coverage of server-side reqs
            # when no server-side code was uploaded.
            coverage_pct = 0.0
        elif not kb["stats"].get("has_backend_code"):
            warnings.append(
                "No verifiable backend signal (routes/tables/resolved chains) was extracted "
                "from the uploaded code. Coverage is likely under-reported. Re-upload with "
                "backend source to get a full report."
            )
        if kb["stats"].get("requirements", 0) == 0:
            warnings.append(
                "No explicit requirements (FR-/NFR-/UC-/US-/BR- IDs or shall/must sentences) "
                "were extracted from the uploaded documents. LLM inference used as fallback."
            )
        if not gaps and not matrix:
            warnings.append(
                "LLM returned neither gaps nor a coverage matrix — analysis inconclusive. "
                "This typically means the model could not correlate the requirements with the "
                "provided code. Coverage set to 0 to avoid a false positive."
            )
            coverage_pct = 0.0

        summary_text = ""
        if isinstance(summary_block, str):
            summary_text = summary_block
        else:
            summary_text = (
                f"Analyzed {kb['stats']['requirements']} requirements against "
                f"{kb['stats']['entities']} code entities. "
                f"Found {len(gaps)} gaps ({critical} critical, {major} major, {minor} minor). "
                f"Coverage: {coverage_pct}%. "
                f"Resolved UI→API→DB chains: {kb['stats'].get('resolved_chains', 0)} of "
                f"{kb['stats'].get('resolved_chains', 0) + kb['stats'].get('unresolved_chains', 0)}."
            )

        result = {
            "summary": summary_text,
            "summary_stats": summary_block if isinstance(summary_block, dict) else None,
            "coverage_pct": coverage_pct,
            "critical_gaps": critical,
            "major_gaps": major,
            "minor_gaps": minor,
            "total_gaps": len(gaps),
            "gaps": gaps,
            "coverage_matrix": matrix,
            # iter-14.98 — Test cases + accuracy (mirrors Living System)
            "test_cases": test_cases,
            "test_stats": tc_stats,
            "accuracy_pct": tc_stats["accuracy_pct"],
            "requirement_count": kb["stats"]["requirements"],
            "code_entity_count": kb["stats"]["entities"],
            "traceability_stats": kb["traceability"]["stats"],
            "warnings": warnings,
            "kb_diagnostics": {
                "has_backend_code": kb["stats"].get("has_backend_code", False),
                "is_ui_only": kb["stats"].get("is_ui_only", False),
                "backend_entities": kb["stats"].get("backend_entities", 0),
                "resolved_chains": kb["stats"].get("resolved_chains", 0),
                "db_tables": kb["stats"].get("db_tables", 0),
                "api_routes": kb["stats"].get("api_routes", 0),
            },
            "raw_llm_response": response_text[:5000] if not gaps else None,
        }

        # ── Sub-phase: finalising ─────────────────────────────────────
        await _phase("finalizing")

        await gap_analyses.update_one(
            {"_id": analysis_id},
            {"$set": {
                "status": "completed",
                "phase": "completed",
                "phase_label": "Analysis complete",
                "progress_pct": 100,
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }}
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await gap_analyses.update_one(
            {"_id": analysis_id},
            {"$set": {
                "status": "failed",
                "phase": "failed",
                "phase_label": f"Analysis failed: {str(e)[:120]}",
                "progress_pct": 100,
                "error": str(e),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }}
        )


@router.post("/gap-analyzer/create")
async def create_gap_analysis(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    description: str = Form(None),
    code_zip: List[UploadFile] = File(None),
    docs_zip: List[UploadFile] = File(None),
    # iter-15.18 — GitHub source alternative to code_zip. When github_url is
    # provided, the server clones (shallow, single-branch) into a temp dir and
    # walks the tree for text files. Either code_zip OR github_url is required.
    github_url: str = Form(None),
    github_branch: str = Form(None),
    github_token: str = Form(None),
    # iter-15.18 — Per-file doc type tagging. `docs_types` is a JSON array
    # parallel to `docs_zip`; each entry is one of DOC_TYPE_LABELS keys.
    # Falls back to "srs" for any file without an explicit type.
    docs_types: str = Form(None),
    model: str = Form(None),
):
    """
    Create a new gap analysis comparing code against documentation.

    Code source (exactly one required):
      - code_zip: One or more ZIPs / single source files, OR
      - github_url: HTTPS clone URL (optionally + github_branch + github_token)

    Documents:
      - docs_zip: One or more files (ZIP, PDF, DOCX, MD, TXT) containing docs
      - docs_types: JSON array of doc-type keys (srs/frs/brd/user_manual/
        data_dict/design/api_spec/test_plan/other) parallel to docs_zip. If
        omitted, all files are treated as "srs".
    """
    import traceback
    try:
        code_zip = code_zip or []
        docs_zip = docs_zip or []
        print(
            f"[gap-analyzer] Starting create with name={name}, "
            f"code_zip count={len(code_zip)}, docs_zip count={len(docs_zip)}, "
            f"github_url={'yes' if github_url else 'no'}"
        )
        now = datetime.now(timezone.utc).isoformat()

        # ── Code source: either GitHub or ZIP upload ─────────────────
        code_files: List[Dict] = []
        source_kind = "upload"
        source_meta: Dict[str, Any] = {}

        github_url_clean = (github_url or "").strip()
        if github_url_clean:
            source_kind = "github"
            source_meta = {
                "url": github_url_clean,
                "branch": (github_branch or "").strip() or None,
            }
            tmp_root = tempfile.mkdtemp(prefix="gap_clone_")
            try:
                await _clone_github_repo(
                    github_url_clean,
                    (github_branch or "").strip() or None,
                    (github_token or "").strip() or None,
                    tmp_root,
                )
                code_files = _walk_repo_files(tmp_root)
                print(f"[gap-analyzer] Cloned {github_url_clean} → {len(code_files)} files")
            finally:
                try: shutil.rmtree(tmp_root)
                except Exception: pass
        else:
            # Parse all code files from uploaded ZIPs / raw files
            for uploaded in code_zip:
                content = await uploaded.read()
                filename = uploaded.filename or "unknown"
                if filename.endswith(".zip") or uploaded.content_type == "application/zip":
                    try:
                        parsed = _parse_uploaded_files(content)
                        code_files.extend(parsed)
                    except Exception as e:
                        print(f"[gap-analyzer] Error parsing ZIP {filename}: {e}")
                else:
                    try:
                        text = content.decode("utf-8", errors="replace")
                        code_files.append({"path": filename, "content": text, "size": len(text)})
                    except Exception:
                        pass

        # ── Doc source: multi-file with per-file type tagging ────────
        # Parse docs_types JSON array (parallel to docs_zip).
        doc_type_by_index: Dict[int, str] = {}
        if docs_types:
            try:
                arr = json.loads(docs_types)
                if isinstance(arr, list):
                    for i, t in enumerate(arr):
                        key = str(t or "").lower().strip()
                        if key in DOC_TYPE_LABELS:
                            doc_type_by_index[i] = key
            except Exception:
                pass

        doc_files: List[Dict] = []
        for idx, uploaded in enumerate(docs_zip):
            content = await uploaded.read()
            filename = uploaded.filename or "unknown"
            dtype = doc_type_by_index.get(idx, "srs")
            dtype_label = DOC_TYPE_LABELS.get(dtype, DOC_TYPE_LABELS["srs"])
            if filename.endswith(".zip") or uploaded.content_type == "application/zip":
                try:
                    parsed = _parse_uploaded_files(content)
                    for f in parsed:
                        f["doc_type"] = dtype
                        f["doc_type_label"] = dtype_label
                    doc_files.extend(parsed)
                except Exception:
                    pass
            else:
                try:
                    lower = filename.lower()
                    # iter-14.99 — actually parse PDF / DOCX via kb.parsers so
                    # requirement extraction sees real text, not "[Binary file: …]".
                    if lower.endswith(('.pdf', '.doc', '.docx')):
                        try:
                            _ftype, text = parse_file(filename, content, allow_zip_recurse=False)
                        except Exception as pe:
                            text = f"[Binary parse error for {filename}: {pe}]"
                        text = text or ""
                        doc_files.append({
                            "path": filename, "content": text, "size": len(text),
                            "doc_type": dtype, "doc_type_label": dtype_label,
                        })
                    elif lower.endswith(('.md', '.txt', '.markdown')):
                        text = content.decode("utf-8", errors="replace")
                        doc_files.append({
                            "path": filename, "content": text, "size": len(text),
                            "doc_type": dtype, "doc_type_label": dtype_label,
                        })
                    else:
                        text = content.decode("utf-8", errors="replace")
                        doc_files.append({
                            "path": filename, "content": text, "size": len(text),
                            "doc_type": dtype, "doc_type_label": dtype_label,
                        })
                except Exception:
                    pass

        if not code_files:
            raise HTTPException(
                400,
                "No valid code files. Provide either a source ZIP (code_zip) or a GitHub URL (github_url).",
            )
        if not doc_files:
            raise HTTPException(400, "No valid documentation files found")

        # Detect tech stack
        detected_stack = detect_stack(None, files_with_content=code_files)
        
        # Extract entities from code
        code_entities = _extract_entities_from_files(code_files)
        
        # Create analysis record
        analysis_id = str(ObjectId())
        doc = {
            "_id": analysis_id,
            "name": name,
            "description": description,
            "status": "analyzing",
            "phase": "queued",
            "phase_label": "Queued for analysis",
            "progress_pct": 5,
            "created_at": now,
            "updated_at": now,
            "detected_stack": detected_stack,
            "code_file_count": len(code_files),
            "doc_file_count": len(doc_files),
            "code_entity_count": len(code_entities),
            "model": model,
            "source_kind": source_kind,
            "source_meta": source_meta,
            "doc_types_summary": {
                k: sum(1 for f in doc_files if f.get("doc_type") == k)
                for k in DOC_TYPE_LABELS
                if any(f.get("doc_type") == k for f in doc_files)
            },
            "result": None,
        }
        await gap_analyses.insert_one(doc)
        
        # Store files for background processing
        await gap_analysis_files.insert_one({
            "_id": analysis_id,
            "code_files": code_files,
            "doc_files": doc_files,
        })
        
        # Start background analysis
        background_tasks.add_task(_run_gap_analysis_background, analysis_id, code_files, doc_files, model)
        
        await _log_audit("create", "gap_analysis", analysis_id, {
            "name": name,
            "code_files": len(code_files),
            "doc_files": len(doc_files),
        })
        
        return {
            "_id": analysis_id,
            "id": analysis_id,
            "name": name,
            "status": "analyzing",
            "detected_stack": detected_stack,
            "code_files": len(code_files),
            "doc_files": len(doc_files),
            "code_entities": len(code_entities),
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Gap analysis creation failed: {str(e)}")


@router.get("/gap-analyzer/meta")
async def gap_analyzer_meta():
    """iter-15.18 — Frontend metadata: doc-type taxonomy for the upload UI.
    Consumed by GapAnalyzer.jsx to render the per-file type dropdown.
    """
    return {
        "doc_types": [
            {"key": k, "label": v}
            for k, v in DOC_TYPE_LABELS.items()
        ]
    }


@router.get("/gap-analyzer/{analysis_id}/kb")
async def get_gap_analysis_kb(analysis_id: str):
    """Return the knowledge base (entities + requirements + UI→API→DB map)
    extracted for this analysis. Populated at end of phase 1 so the FE can
    show it before phase-2 verification results are ready."""
    doc = await tools_kb.find_one({"_id": analysis_id})
    if not doc:
        raise HTTPException(404, "KB not built yet for this analysis")
    doc.pop("_id", None)
    return doc


@router.get("/gap-analyzer/{analysis_id}/status")
async def get_gap_analysis_status(analysis_id: str):
    """Poll for analysis status + phase — lightweight endpoint for FE polling."""
    analysis = await gap_analyses.find_one(
        {"_id": analysis_id},
        {
            "status": 1, "phase": 1, "phase_label": 1, "progress_pct": 1,
            "error": 1,
            "code_file_count": 1, "doc_file_count": 1,
            "code_entity_count": 1, "requirement_count": 1,
            "detected_stack": 1,
        }
    )
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    return {
        "id": analysis_id,
        "status": analysis.get("status", "pending"),
        "phase": analysis.get("phase"),
        "phase_label": analysis.get("phase_label"),
        "progress_pct": analysis.get("progress_pct", 0),
        "error": analysis.get("error"),
        "code_file_count": analysis.get("code_file_count", 0),
        "doc_file_count": analysis.get("doc_file_count", 0),
        "code_entity_count": analysis.get("code_entity_count", 0),
        "requirement_count": analysis.get("requirement_count", 0),
        "detected_stack": analysis.get("detected_stack"),
    }


@router.post("/gap-analyzer/{analysis_id}/run")
async def run_gap_analysis(analysis_id: str, background_tasks: BackgroundTasks, model: str = None):
    """Re-run gap analysis on the same uploaded artefacts.

    iter-15.1 — When invoked from the "Re-analyze" button in the FE, the
    caller may pass a `model` query param to route the LLM to a different
    provider (Console routing tier still applies). We reset phase/result so
    the UI shows "analyzing" instead of the stale completed report while the
    new run streams in.
    """
    analysis = await gap_analyses.find_one({"_id": analysis_id})
    if not analysis:
        raise HTTPException(404, "Analysis not found")

    if analysis.get("frozen"):
        raise HTTPException(
            400,
            "This analysis is frozen and cannot be re-analyzed. Unfreeze it first to run a new analysis.",
        )

    # Get stored files
    files_doc = await gap_analysis_files.find_one({"_id": analysis_id})
    if not files_doc:
        raise HTTPException(400, "Analysis files not found - please re-upload")

    resolved_model = (model or analysis.get("model") or "").strip() or None

    now = datetime.now(timezone.utc).isoformat()
    await gap_analyses.update_one(
        {"_id": analysis_id},
        {"$set": {
            "status": "analyzing",
            "phase": "queued",
            "phase_label": "Queued for re-analysis",
            "progress_pct": 5,
            "updated_at": now,
            "result": None,
            "model": resolved_model,
            "reanalyzed_at": now,
            "reanalyze_count": (analysis.get("reanalyze_count") or 0) + 1,
        }},
    )

    background_tasks.add_task(
        _run_gap_analysis_background,
        analysis_id,
        files_doc.get("code_files", []),
        files_doc.get("doc_files", []),
        resolved_model,
    )

    await _log_audit("reanalyze", "gap_analysis", analysis_id, {
        "model": resolved_model,
        "reanalyze_count": (analysis.get("reanalyze_count") or 0) + 1,
    })

    return {
        "id": analysis_id,
        "status": "analyzing",
        "phase": "queued",
        "model": resolved_model,
        "message": "Re-analysis started",
    }


class FreezeBody(BaseModel):
    confirm: str = Field(..., description="Typed confirmation token (must equal 'FREEZE')")


@router.post("/gap-analyzer/{analysis_id}/freeze")
async def freeze_gap_analysis(analysis_id: str, body: FreezeBody):
    """iter-15.4 — Freeze a completed gap analysis.

    Follows LAMA's typed-confirmation contract (same pattern as stage freezes):
    caller must POST `{ "confirm": "FREEZE" }`. Once frozen, the analysis is
    read-only — Re-analyze is blocked until an explicit unfreeze.
    """
    if (body.confirm or "").strip().upper() != "FREEZE":
        raise HTTPException(400, "Typed confirmation must equal 'FREEZE'")
    analysis = await gap_analyses.find_one({"_id": analysis_id})
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    if analysis.get("status") != "completed":
        raise HTTPException(
            400,
            f"Only completed analyses can be frozen (current status: {analysis.get('status')})",
        )
    if analysis.get("frozen"):
        return {"id": analysis_id, "frozen": True, "frozen_at": analysis.get("frozen_at")}

    now = datetime.now(timezone.utc).isoformat()
    await gap_analyses.update_one(
        {"_id": analysis_id},
        {"$set": {
            "frozen": True,
            "frozen_at": now,
            "updated_at": now,
        }},
    )
    await _log_audit("freeze", "gap_analysis", analysis_id, {"frozen_at": now})
    return {"id": analysis_id, "frozen": True, "frozen_at": now}


class UnfreezeBody(BaseModel):
    confirm: str = Field(..., description="Typed confirmation token (must equal 'UNFREEZE')")


@router.post("/gap-analyzer/{analysis_id}/unfreeze")
async def unfreeze_gap_analysis(analysis_id: str, body: UnfreezeBody):
    """Reverse a freeze so the SME can re-analyze again."""
    if (body.confirm or "").strip().upper() != "UNFREEZE":
        raise HTTPException(400, "Typed confirmation must equal 'UNFREEZE'")
    analysis = await gap_analyses.find_one({"_id": analysis_id})
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    if not analysis.get("frozen"):
        return {"id": analysis_id, "frozen": False}
    now = datetime.now(timezone.utc).isoformat()
    await gap_analyses.update_one(
        {"_id": analysis_id},
        {"$set": {
            "frozen": False,
            "unfrozen_at": now,
            "updated_at": now,
        }},
    )
    await _log_audit("unfreeze", "gap_analysis", analysis_id, {"unfrozen_at": now})
    return {"id": analysis_id, "frozen": False, "unfrozen_at": now}


@router.get("/gap-analyzer/{analysis_id}/export/{fmt}")
async def export_gap_analysis(analysis_id: str, fmt: str):
    """
    Export gap analysis results.
    
    Formats: csv, json, html (for PDF, use browser print)
    """
    analysis = await gap_analyses.find_one({"_id": analysis_id})
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    
    if analysis.get("status") != "completed":
        raise HTTPException(400, f"Analysis not complete (status: {analysis.get('status')})")
    
    result = analysis.get("result", {})
    gaps = result.get("gaps", [])
    name = analysis.get("name", "gap-analysis")
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        # Header (iter-15.4 — add plain-English + design-metadata columns)
        writer.writerow([
            "Section", "ID", "Type", "Severity", "Title",
            "Human Summary (plain English)",
            "Case Type", "Req Type", "Priority", "Scenario",
            "Description", "Doc Reference", "Code Location",
            "Expected", "Actual", "Recommendation", "Source",
        ])
        # Gaps
        for gap in gaps:
            writer.writerow([
                "GAP",
                gap.get("id", ""),
                gap.get("type", ""),
                gap.get("severity", ""),
                gap.get("title", ""),
                "",  # human_summary
                "", "", "", "",
                gap.get("description", ""),
                gap.get("doc_reference", ""),
                gap.get("location", ""),
                gap.get("expected", ""),
                gap.get("actual", ""),
                gap.get("recommendation", ""),
                "",
            ])
        # Coverage matrix
        matrix = result.get("coverage_matrix", []) or []
        for row in matrix:
            writer.writerow([
                "COVERAGE",
                row.get("requirement_id", ""),
                "",
                "",
                row.get("requirement_text", ""),
                "", "", "", "", "",
                row.get("notes", ""),
                row.get("requirement_id", ""),
                ", ".join(row.get("code_locations", []) or []),
                "",
                "IMPLEMENTED" if row.get("implemented") else row.get("status", "MISSING").upper(),
                "",
                "",
            ])
        # iter-14.98 / iter-15.4 — Test cases + accuracy + human summaries
        test_cases = result.get("test_cases", []) or []
        for tc in test_cases:
            writer.writerow([
                "TEST_CASE",
                tc.get("id", ""),
                tc.get("category", ""),
                tc.get("severity", ""),
                tc.get("title", ""),
                tc.get("human_summary", ""),
                tc.get("case_type", ""),
                tc.get("requirement_type", ""),
                tc.get("priority", ""),
                tc.get("scenario", ""),
                (tc.get("preconditions", "") + " || " +
                 " | ".join(tc.get("steps", []) or []) + " || " +
                 (tc.get("test_data", "") or ""))[:1000],
                tc.get("requirement_id", ""),
                ", ".join(tc.get("evidence", []) or []),
                tc.get("expected_result", ""),
                tc.get("actual_result", ""),
                tc.get("status", ""),
                tc.get("source", ""),
            ])
        # Summary rows
        writer.writerow([])
        writer.writerow(["SUMMARY", "coverage_pct", "", "", f"{result.get('coverage_pct', 0)}%",
                         "", "", "", "", "",
                         f"critical={result.get('critical_gaps',0)}, major={result.get('major_gaps',0)}, minor={result.get('minor_gaps',0)}",
                         "", "", "", "", "", ""])
        ts = result.get("test_stats") or {}
        writer.writerow(["SUMMARY", "accuracy_pct", "", "", f"{result.get('accuracy_pct', 0)}%",
                         "", "", "", "", "",
                         f"total={ts.get('total',0)}, passed={ts.get('passed',0)}, failed={ts.get('failed',0)}, blocked={ts.get('blocked',0)}, n/a={ts.get('not_applicable',0)}, positive={ts.get('positive',0)}, negative={ts.get('negative',0)}",
                         "", "", "", "", "", ""])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}_gaps.csv"'},
        )
    
    elif fmt == "json":
        return Response(
            content=json.dumps(result, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}_report.json"'},
        )
    
    elif fmt == "html":
        # Generate HTML report (printable to PDF)
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Gap Analysis Report - {name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; }}
        h1 {{ color: #2E2E38; border-bottom: 3px solid #FFE600; padding-bottom: 10px; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }}
        .stat {{ background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; }}
        .stat-value {{ font-size: 2em; font-weight: bold; color: #2E2E38; }}
        .stat-label {{ color: #666; font-size: 0.9em; }}
        .critical {{ color: #dc2626; }}
        .major {{ color: #ea580c; }}
        .minor {{ color: #ca8a04; }}
        .gap {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 4px solid #dc2626; }}
        .gap.major {{ border-left-color: #ea580c; }}
        .gap.minor {{ border-left-color: #ca8a04; }}
        .gap-title {{ font-weight: 600; margin-bottom: 5px; }}
        .gap-meta {{ font-size: 0.85em; color: #666; margin-bottom: 10px; }}
        .gap-desc {{ margin: 10px 0; }}
        .gap-rec {{ background: #f0fdf4; padding: 10px; border-radius: 4px; font-size: 0.9em; }}
        @media print {{ body {{ margin: 0; }} .no-print {{ display: none; }} }}
    </style>
</head>
<body>
    <h1>Gap Analysis Report</h1>
    <p><strong>Analysis:</strong> {name}</p>
    <p><strong>Generated:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    
    <div class="summary">
        <div class="stat">
            <div class="stat-value">{result.get('coverage_pct', 0)}%</div>
            <div class="stat-label">Coverage</div>
        </div>
        <div class="stat">
            <div class="stat-value critical">{result.get('critical_gaps', 0)}</div>
            <div class="stat-label">Critical Gaps</div>
        </div>
        <div class="stat">
            <div class="stat-value major">{result.get('major_gaps', 0)}</div>
            <div class="stat-label">Major Gaps</div>
        </div>
        <div class="stat">
            <div class="stat-value minor">{result.get('minor_gaps', 0)}</div>
            <div class="stat-label">Minor Gaps</div>
        </div>
    </div>
    
    <h2>Summary</h2>
    <p>{result.get('summary', 'No summary available.')}</p>
    
    <h2>Gap Details ({len(gaps)} items)</h2>
"""
        for gap in gaps:
            severity = gap.get("severity", "minor")
            html += f"""
    <div class="gap {severity}">
        <div class="gap-title">{gap.get('title', 'Untitled')}</div>
        <div class="gap-meta">Severity: <strong>{severity.upper()}</strong> | Location: {gap.get('location', 'N/A')}</div>
        <div class="gap-desc">{gap.get('description', '')}</div>
        {f'<div class="gap-rec"><strong>Recommendation:</strong> {gap.get("recommendation", "")}</div>' if gap.get('recommendation') else ''}
    </div>
"""
        html += """
    <p class="no-print" style="margin-top: 40px; color: #666; font-size: 0.9em;">
        💡 To save as PDF: Press Ctrl/Cmd+P and select "Save as PDF"
    </p>
</body>
</html>"""
        return Response(
            content=html,
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}_report.html"'},
        )
    
    else:
        raise HTTPException(400, f"Unsupported format: {fmt}. Use csv, json, or html.")


@router.get("/gap-analyzer")
async def list_gap_analyses():
    """List all gap analyses."""
    cursor = gap_analyses.find({}, {"result": 0}).sort("created_at", -1)
    items = await cursor.to_list(100)
    for item in items:
        item["id"] = str(item.pop("_id"))
    return {"items": items}


@router.get("/gap-analyzer/{analysis_id}")
async def get_gap_analysis(analysis_id: str):
    """Get a specific gap analysis with results."""
    analysis = await gap_analyses.find_one({"_id": analysis_id})
    if not analysis:
        raise HTTPException(404, "Analysis not found")
    analysis["id"] = str(analysis.pop("_id"))
    return analysis


@router.delete("/gap-analyzer/{analysis_id}")
async def delete_gap_analysis(analysis_id: str):
    """Delete a gap analysis + its stored files + KB snapshot."""
    result = await gap_analyses.delete_one({"_id": analysis_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Analysis not found")
    # iter-15.2 — cascade-clean uploaded artefacts and KB snapshot so
    # History doesn't accumulate orphan rows in gap_analysis_files / tools_kb.
    try:
        await gap_analysis_files.delete_one({"_id": analysis_id})
    except Exception:
        pass
    try:
        await tools_kb.delete_one({"_id": analysis_id})
    except Exception:
        pass
    await _log_audit("delete", "gap_analysis", analysis_id, {})
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════════
# Transformer Endpoints
# ════════════════════════════════════════════════════════════════════════════

SUPPORTED_TRANSFORMATIONS = {
    "helidon-to-springboot": {
        "source": ["helidon", "helidon-mp", "helidon-se", "jaxrs"],
        "target": "spring-boot-3",
        "label": "Helidon → Spring Boot 3",
    },
    "oracle-to-postgresql": {
        "source": ["oracle", "plsql"],
        "target": "postgresql",
        "label": "Oracle → PostgreSQL",
    },
    "struts-to-springmvc": {
        "source": ["struts", "struts-1"],
        "target": "spring-mvc",
        "label": "Struts → Spring MVC",
    },
    "jsp-to-react": {
        "source": ["jsp"],
        "target": "react-18",
        "label": "JSP → React 18",
    },
    "ejb-to-spring": {
        "source": ["ejb", "ejb-3"],
        "target": "spring-beans",
        "label": "EJB → Spring Beans",
    },
    "jquery-to-react": {
        "source": ["jquery"],
        "target": "react-18",
        "label": "jQuery → React 18",
    },
}


# iter-15.40 — BUILD SYSTEM SUGGESTIONS
# Maps a target-stack component id (backend / frontend / runtime) → the
# candidate build tools that make sense for it. Order matters: the first
# entry is the auto-suggested default rendered in the UI dropdown.
#
# This is intentionally NOT hardcoded per-source-target-pair (as required
# by the general-agent rule) — it keys off the *target* component only, so
# any current or future target that resolves to `spring-boot-*` gets
# maven / gradle offered regardless of what the source was.
#
# Consumed by:
#   - GET /transformer/build-tools (UI suggestion endpoint)
#   - _run_compiler (iter-15.44) — picks the tool actually invoked on the
#     transformed workspace at final compile
#   - _run_tests_v2 (iter-15.45) — picks the test-runner / coverage tool
BUILD_TOOL_SUGGESTIONS: Dict[str, List[str]] = {
    # Java backends
    "spring-boot-3":     ["maven", "gradle"],
    "spring-boot-2":     ["maven", "gradle"],
    "spring-mvc":        ["maven", "gradle"],
    "spring-webflux":    ["maven", "gradle"],
    "spring-beans":      ["maven", "gradle"],
    "quarkus":           ["maven", "gradle"],
    "micronaut":         ["gradle", "maven"],
    "helidon":           ["maven", "gradle"],
    # Python backends
    "fastapi":           ["pip", "poetry", "uv"],
    "django":            ["pip", "poetry", "uv"],
    # Node backends
    "express":           ["npm", "yarn", "pnpm"],
    "nestjs":            ["npm", "yarn", "pnpm"],
    # .NET / Go
    "dotnet":            ["dotnet"],
    "go":                ["go"],
    # Frontends
    "react-18":          ["npm", "yarn", "pnpm"],
    "angular-17":        ["npm", "yarn", "pnpm"],
    "vue-3":             ["npm", "yarn", "pnpm"],
    # Databases have no build-tool of their own — DDL is applied via the
    # backend's migration runner (flyway/liquibase/alembic/knex). These
    # entries just keep the endpoint from returning an empty list when a
    # database-only transform is selected.
    "postgresql":        ["flyway", "liquibase"],
    "mysql":             ["flyway", "liquibase"],
}

# The canonical set of tools we know how to actually INVOKE in
# `_run_compiler` (iter-15.44). Anything in BUILD_TOOL_SUGGESTIONS that
# isn't here is offered as a UI choice but falls back to Factory Droid's
# Computer for the actual compile (option (c) from the plan). Kept as a
# module-level constant so tests can assert what's covered.
BUILD_TOOL_NATIVE_SUPPORT: Set[str] = {
    "maven", "gradle", "npm", "yarn", "pnpm", "pip", "poetry",
    "dotnet", "go",
}


def _suggest_build_tools(transforms: Dict[str, str]) -> Dict[str, List[str]]:
    """iter-15.40 — Return a `{component: [tools...]}` map with the first
    entry per list being the recommended default. `transforms` is the same
    per-component target map used by `create_transformation_v2` (e.g.
    `{"backend": "spring-boot-3", "frontend": "react-18"}`)."""
    out: Dict[str, List[str]] = {}
    for component, target in (transforms or {}).items():
        if not target:
            continue
        opts = BUILD_TOOL_SUGGESTIONS.get(str(target))
        if opts:
            out[component] = list(opts)
    return out


@router.get("/transformer/targets")
async def list_transformation_targets():
    """List available transformation targets."""
    return {
        "targets": SUPPORTED_TRANSFORMATIONS,
        "frameworks": ["spring-boot-3", "spring-mvc", "spring-webflux", "react-18"],
        "databases": ["postgresql", "mysql"],
    }


@router.get("/transformer/build-tools")
async def suggest_build_tools_endpoint(
    backend: Optional[str] = None,
    frontend: Optional[str] = None,
    runtime: Optional[str] = None,
    database: Optional[str] = None,
):
    """iter-15.40 — Given the components the operator picked in the target
    stack, return the candidate build tools per component (first entry is
    the recommended default). Powers the "Build System" select the UI
    now shows alongside target-stack selection.

    Deliberately stateless — no transformation record yet exists at this
    point in the wizard, so no `transform_id` param."""
    transforms = {
        k: v for k, v in {
            "backend": backend, "frontend": frontend,
            "runtime": runtime, "database": database,
        }.items() if v
    }
    suggestions = _suggest_build_tools(transforms)
    return {
        "suggestions": suggestions,
        "native_support": sorted(BUILD_TOOL_NATIVE_SUPPORT),
        "note": (
            "Tools in native_support run inside LAMA's own container. "
            "Anything else is delegated to Factory Droid's Computer when "
            "Droid is enabled for the project, else skipped at final "
            "compile with a clear 'toolchain missing' report."
        ),
    }


@router.post("/transformer/analyze-stack")
async def analyze_source_stack(
    files: List[UploadFile] = File(...),
):
    """
    Analyze uploaded source files to detect the technology stack.
    
    Returns detected backend, frontend, database, and runtime components
    with confidence-based suggestions for transformation targets.
    """
    # Read and parse files (support both ZIP and individual files)
    all_files = []
    
    for uploaded in files:
        content = await uploaded.read()
        filename = uploaded.filename or "unknown"
        
        # Check if it's a ZIP
        if filename.endswith(".zip") or uploaded.content_type == "application/zip":
            try:
                parsed = _parse_uploaded_files(content)
                all_files.extend(parsed)
            except Exception:
                pass
        else:
            # Individual file
            try:
                text = content.decode("utf-8", errors="replace")
                all_files.append({
                    "path": filename,
                    "content": text,
                    "size": len(text),
                })
            except Exception:
                pass
    
    if not all_files:
        raise HTTPException(400, "No valid files found")
    
    # Detect stack using our helper — pass real per-file content so
    # content-driven framework/database markers (Spring Boot, JPA, JDBC
    # driver strings, etc.) are actually scanned (iter-15.29 fix).
    detected = detect_stack(None, files_with_content=all_files)
    
    return {
        "detected_stack": detected,
        "file_count": len(all_files),
        "files": [f["path"] for f in all_files[:50]],  # Return first 50 paths
    }


@router.post("/transformer/create")
async def create_transformation(
    name: str = Form(...),
    target_stack: str = Form(...),
    description: str = Form(None),
    code_zip: UploadFile = File(...),
    model: str = Form(None),
):
    """
    Create a new code transformation job.
    
    Uploads:
      - code_zip: ZIP containing source code to transform
    """
    now = datetime.now(timezone.utc).isoformat()
    
    # Validate target stack
    if target_stack not in SUPPORTED_TRANSFORMATIONS:
        valid = list(SUPPORTED_TRANSFORMATIONS.keys())
        raise HTTPException(400, f"Invalid target_stack. Valid options: {valid}")
    
    # Read and parse files
    code_bytes = await code_zip.read()
    code_files = _parse_uploaded_files(code_bytes)
    
    if not code_files:
        raise HTTPException(400, "No valid code files found in ZIP")
    
    # Detect source stack
    detected_stack = detect_stack(None, files_with_content=code_files)
    
    # Create transformation record
    transform_id = str(ObjectId())
    doc = {
        "_id": transform_id,
        "name": name,
        "description": description,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "source_stack": detected_stack,
        "target_stack": target_stack,
        "target_info": SUPPORTED_TRANSFORMATIONS[target_stack],
        "file_count": len(code_files),
        "model": model,
        "result": None,
        "files_transformed": 0,
        "manual_review_count": 0,
    }
    await transformations.insert_one(doc)
    
    # Store source files
    for f in code_files:
        await transform_files.insert_one({
            "transform_id": transform_id,
            "type": "source",
            "path": f["path"],
            "content": f["content"],
            "created_at": now,
        })
    
    await _log_audit("create", "transformation", transform_id, {
        "name": name,
        "target_stack": target_stack,
        "files": len(code_files),
    })
    
    return {
        "id": transform_id,
        "name": name,
        "status": "pending",
        "source_stack": detected_stack,
        "target_stack": target_stack,
        "file_count": len(code_files),
    }


@router.post("/transformer/create/v2")
async def create_transformation_v2(
    name: str = Form(...),
    detected_stack: str = Form(None),
    transforms: str = Form(...),
    description: str = Form(None),
    source_files: List[UploadFile] = File(...),
    model: str = Form(None),
    # iter-15.40 — Build system the operator confirmed at target-stack
    # selection. `{"backend": "maven", "frontend": "npm"}` etc. Optional
    # (older clients that don't send it will default at first compile
    # via `_suggest_build_tools`), but the UI now always sends it after
    # the mandatory "confirm build system" step.
    build_tool: str = Form(None),
    # iter-15.51 — LAMA project the transformer runs under. Threaded from
    # `ProjectContext.active.id` in the FE. Persisted on the transformation
    # so every downstream `fabric_call` (context_manager / planner / coder
    # / verifier / tester / regen / gap-recovery) can pass it and engage
    # Factory (Droid) routing when the project has factory_orchestrator
    # enabled in Console. Optional — falls back to `_project_id_for_transform`
    # resolution (LAMA_DEFAULT_PROJECT_ID → first tech_transformer project).
    project_id: str = Form(None),
):
    """
    Create a transformation with component-level control.
    
    Args:
        name: Transformation name
        detected_stack: JSON of detected stack from analyze-stack
        transforms: JSON mapping categories to target techs (e.g., {"backend": "spring-boot-3"})
        source_files: Individual files or ZIPs to transform
    """
    now = datetime.now(timezone.utc).isoformat()
    
    # Parse JSON inputs
    try:
        stack = json.loads(detected_stack) if detected_stack else {}
    except (json.JSONDecodeError, TypeError):
        stack = {}
    
    try:
        transform_map = json.loads(transforms)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "Invalid transforms JSON")
    
    # Filter out null/empty transforms
    active_transforms = {k: v for k, v in transform_map.items() if v}
    if not active_transforms:
        raise HTTPException(400, "At least one transformation must be selected")

    # iter-15.40 — Parse + validate the build-tool selection. The UI is
    # supposed to send one build tool per active component (backend /
    # frontend / runtime), auto-suggested from `_suggest_build_tools`.
    # We fill in defaults for any component the client omitted so
    # downstream (`_run_compiler`) never has to guess.
    try:
        build_tool_map = json.loads(build_tool) if build_tool else {}
        if not isinstance(build_tool_map, dict):
            build_tool_map = {}
    except (json.JSONDecodeError, TypeError):
        build_tool_map = {}

    suggested = _suggest_build_tools(active_transforms)
    resolved_build_tools: Dict[str, str] = {}
    for component in active_transforms:
        picked = str(build_tool_map.get(component) or "").strip().lower()
        candidates = suggested.get(component, [])
        if picked and candidates and picked not in candidates:
            raise HTTPException(
                400,
                f"Invalid build_tool for {component}: {picked!r}. "
                f"Valid options: {candidates}",
            )
        if picked:
            resolved_build_tools[component] = picked
        elif candidates:
            resolved_build_tools[component] = candidates[0]
    
    # Parse uploaded files
    all_files = []
    for uploaded in source_files:
        content = await uploaded.read()
        filename = uploaded.filename or "unknown"
        
        if filename.endswith(".zip") or uploaded.content_type == "application/zip":
            try:
                parsed = _parse_uploaded_files(content)
                all_files.extend(parsed)
            except Exception:
                pass
        else:
            try:
                text = content.decode("utf-8", errors="replace")
                all_files.append({
                    "path": filename,
                    "content": text,
                    "size": len(text),
                })
            except Exception:
                pass
    
    if not all_files:
        raise HTTPException(400, "No valid source files found")
    
    # If no detected stack provided, detect now
    if not stack:
        stack = detect_stack(None, files_with_content=all_files)
    
    # Create transformation record
    transform_id = str(ObjectId())
    doc = {
        "_id": transform_id,
        "name": name,
        "description": description,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "source_stack": stack,
        "transforms": active_transforms,  # New field: per-component transforms
        "target_stack": active_transforms.get("runtime")
        or active_transforms.get("backend")
        or active_transforms.get("frontend")
        or active_transforms.get("database"),
        "source_files": [f["path"] for f in all_files],
        "file_count": len(all_files),
        "model": model,
        # iter-15.40 — Per-component build tool the operator picked (or
        # auto-defaulted from the first suggestion). `_run_compiler` reads
        # this at final compile to decide which subprocess to invoke.
        "build_tools": resolved_build_tools,
        # iter-15.51 — Anchors the transformation to a LAMA project so the
        # per-project Factory (Droid) config is picked up by fabric_call
        # via llm.py's route_via_factory_orchestrator.
        "project_id": (project_id or "").strip() or None,
        "result": None,
        "files_transformed": 0,
        "manual_review_count": 0,
    }
    await transformations.insert_one(doc)
    
    # Store source files
    for f in all_files:
        await transform_files.insert_one({
            "transform_id": transform_id,
            "type": "source",
            "path": f["path"],
            "content": f["content"],
            "created_at": now,
        })
    
    await _log_audit("create", "transformation", transform_id, {
        "name": name,
        "transforms": active_transforms,
        "build_tools": resolved_build_tools,
        "files": len(all_files),
    })
    
    return {
        "_id": transform_id,
        "id": transform_id,
        "name": name,
        "status": "pending",
        "source_stack": stack,
        "transforms": active_transforms,
        "target_stack": doc["target_stack"],
        "build_tools": resolved_build_tools,
        "file_count": len(all_files),
    }


def _compute_confidence(source: str, transformed: str, target_stack: str) -> float:
    """Heuristic confidence score [0,1] for a single transformed file.

    Signals:
      - Length ratio (too short = truncated; way too long = hallucinated)
      - Non-empty output
      - Presence of expected target-stack markers
      - Absence of common LLM-refusal / apology phrases
    """
    if not transformed or not transformed.strip():
        return 0.0
    src_len = max(len(source or ""), 1)
    tgt_len = len(transformed)
    ratio = tgt_len / src_len

    if ratio < 0.15:
        score = 0.35
    elif ratio < 0.4:
        score = 0.55
    elif ratio <= 1.6:
        score = 0.85
    elif ratio <= 3.0:
        score = 0.7
    else:
        score = 0.5

    markers = {
        "spring-boot-3": ["@RestController", "@Service", "@Autowired", "org.springframework"],
        "spring-boot-2": ["@RestController", "@Service", "org.springframework"],
        "fastapi": ["from fastapi", "APIRouter", "@app.", "@router."],
        "react-18": ["import React", "export default", "useState", "jsx", "tsx"],
        "postgresql": ["CREATE TABLE", "SERIAL", "BIGSERIAL", "::", "RETURNING"],
        "mysql": ["CREATE TABLE", "AUTO_INCREMENT", "ENGINE="],
    }
    hits = markers.get(target_stack, [])
    if hits and any(h.lower() in transformed.lower() for h in hits):
        score = min(1.0, score + 0.1)

    refusals = ["i cannot", "i'm sorry", "as an ai", "unable to transform", "please provide"]
    if any(r in transformed.lower()[:400] for r in refusals):
        score = min(score, 0.3)

    return round(max(0.0, min(1.0, score)), 2)


def _resolve_target_stack(transform: Dict[str, Any], path: str = "", content: str = "") -> str:
    """Pick the user-selected target stack for the current file.

    Prefer explicit runtime/backend/frontend/database selections over the
    old hard-coded backend>frontend>database fallback. This keeps the worker
    aligned with the user's chosen target language.
    """
    transforms_map = transform.get("transforms") or {}
    explicit = transform.get("target_stack")
    if explicit:
        return explicit
    path_l = (path or "").lower()
    content_l = (content or "").lower()

    is_frontend = any(k in path_l for k in (".jsx", ".tsx", ".js", ".ts", ".vue", ".html", ".css", ".jsp", "/web-inf/", "/frontend/")) or any(
        kw in content_l for kw in ("import react", "useeffect(", "export default", "<template", "tsx", "jsx")
    )
    if is_frontend:
        return transforms_map.get("frontend") or transforms_map.get("runtime") or transforms_map.get("backend") or transforms_map.get("database") or "react-18"

    is_database = any(k in path_l for k in (".sql", "/sql/", "/schema/", "/ddl/", "/migration", "/migrations/")) or any(
        kw in content_l for kw in ("create table", "alter table", "insert into", "select ", "foreign key")
    )
    if is_database:
        return transforms_map.get("database") or transforms_map.get("runtime") or transforms_map.get("backend") or transforms_map.get("frontend") or "postgresql"

    return transforms_map.get("runtime") or transforms_map.get("backend") or transforms_map.get("frontend") or transforms_map.get("database") or "spring-boot-3"


# iter-15.15 — Worker lease TTL. If a worker's `worker_heartbeat` is
# older than this many seconds, the lease is considered abandoned and
# another worker may claim it. Kept below the startup-sweeper's 60s
# staleness threshold (sweeper marks paused_orphan first; user clicks
# Resume which relaunches) but with headroom so a briefly-paused GC
# hiccup does not trigger duplicate workers.
_WORKER_LEASE_STALE_SECS = 90


# ─────────────────────────────────────────────────────────────────────
# iter-15.16 — Per-transformation live-telemetry ring buffer.
#
# The transformer worker runs as an in-process asyncio background task;
# the Transformer page can poll `/logs?since=...` (default) or subscribe
# via SSE `/logs/stream` (future). Buffer is in-memory only — a container
# restart wipes it, and the orphan sweeper logs a fresh "Recovered"
# line on next boot (contract: no Mongo write amplification per file).
#
# TTL: 10 min after last write, evicted lazily on next access.
# ─────────────────────────────────────────────────────────────────────
from collections import deque  # noqa: E402  (pinned near usage for readability)

_TRANSFORM_LOG_MAX = 500
_TRANSFORM_LOG_TTL_SECS = 600  # 10 min

# id -> {"buf": deque(maxlen=500), "last_write": datetime}
_TRANSFORM_LOG_BUFFERS: Dict[str, Dict[str, Any]] = {}


def _evict_stale_log_buffers() -> None:
    """Lazily drop buffers untouched for > _TRANSFORM_LOG_TTL_SECS."""
    now = datetime.now(timezone.utc)
    stale = [
        tid for tid, entry in _TRANSFORM_LOG_BUFFERS.items()
        if (now - entry["last_write"]).total_seconds() > _TRANSFORM_LOG_TTL_SECS
    ]
    for tid in stale:
        _TRANSFORM_LOG_BUFFERS.pop(tid, None)


def _emit_log(transform_id: str, level: str, message: str, **fields: Any) -> None:
    """Append a structured log entry to the transformation's ring buffer.

    Also prints to stdout so `docker compose logs lama` still shows it.
    `level` ∈ {"info", "warn", "error", "llm"}.
    """
    if not transform_id:
        return
    now = datetime.now(timezone.utc)
    entry = _TRANSFORM_LOG_BUFFERS.get(transform_id)
    if entry is None:
        entry = {"buf": deque(maxlen=_TRANSFORM_LOG_MAX), "last_write": now}
        _TRANSFORM_LOG_BUFFERS[transform_id] = entry
    lvl = (level or "info").lower()
    if lvl not in ("info", "warn", "error", "llm"):
        lvl = "info"
    row = {"ts": now.isoformat(), "level": lvl, "msg": str(message)}
    for k, v in fields.items():
        # Avoid clobbering ts/level/msg via **fields
        if k not in row:
            try:
                json.dumps(v)  # cheap serialisability probe
                row[k] = v
            except Exception:
                row[k] = repr(v)
    entry["buf"].append(row)
    entry["last_write"] = now
    try:
        print(f"[transformer:{transform_id}] {lvl.upper()} {message}")
    except Exception:
        pass


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def _update_progress(transform_id: str, **fields):
    # iter-15.15 — every progress write also refreshes worker_heartbeat so
    # the startup sweeper can distinguish live workers from orphans.
    now_iso = datetime.now(timezone.utc).isoformat()
    fields["updated_at"] = now_iso
    fields.setdefault("worker_heartbeat", now_iso)
    await transformations.update_one({"_id": transform_id}, {"$set": fields})


async def _acquire_worker_lease(transform_id: str) -> bool:
    """iter-15.15 — CAS-style lease so at most one worker per transform.

    Returns True if this process now holds the lease, False if another
    process's heartbeat is still fresh. Overwrites stale leases.
    """
    pid = os.getpid()
    now = datetime.now(timezone.utc)
    doc = await transformations.find_one(
        {"_id": transform_id},
        {"worker_pid": 1, "worker_heartbeat": 1},
    )
    if doc:
        other_pid = doc.get("worker_pid")
        hb = _parse_iso_utc(doc.get("worker_heartbeat"))
        if other_pid and other_pid != pid and hb is not None:
            if (now - hb).total_seconds() < _WORKER_LEASE_STALE_SECS:
                return False
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "worker_pid": pid,
            "worker_started_at": now.isoformat(),
            "worker_heartbeat": now.isoformat(),
        }},
    )
    return True


async def _run_transformation_background(
    transform_id: str,
    model: Optional[str] = None,
    resume: bool = False,
):
    """Background worker for /transformer/{id}/run.

    iter-15.10 — was previously a synchronous route that timed out at the
    600s ingress limit for large projects. Now runs as BackgroundTasks and
    persists progress on each source file so the FE can poll status.
    iter-15.15 — Added `resume=True` mode for orphan recovery. Preserves
    already-transformed / error rows, reuses cached KB, and skips files
    whose `original_path` already has a row. Fresh-run (resume=False) is
    kept bit-exact.
    """
    if not await _acquire_worker_lease(transform_id):
        print(f"[transformer:{transform_id}] lease held by live worker; not starting")
        _emit_log(transform_id, "warn", "Worker start refused — lease held by another worker")
        return
    try:
        _emit_log(transform_id, "info", "Worker started", resume=resume)
        transform = await transformations.find_one({"_id": transform_id})
        if not transform:
            return

        prompt_template = await _get_prompt("tools.transformer")
        if not prompt_template:
            await _update_progress(
                transform_id,
                status="failed",
                phase="failed",
                error="Transformer prompt not configured",
            )
            return

        cursor = transform_files.find({"transform_id": transform_id, "type": "source"})
        source_files_docs = await cursor.to_list(2000)
        total = len(source_files_docs)

        # iter-15.15 — collect already-transformed / errored source paths on resume
        done_paths: set = set()
        if resume:
            async for row in transform_files.find(
                {"transform_id": transform_id, "type": {"$in": ["transformed", "error"]}},
                {"original_path": 1, "path": 1},
            ):
                dp = row.get("original_path") or row.get("path")
                if dp:
                    done_paths.add(dp)

        if resume:
            await _update_progress(
                transform_id,
                status="running",
                phase="transforming",
                phase_label=f"Resuming ({len(done_paths)}/{total} done)",
                files_done=len(done_paths),
                files_total=total,
                current_file=None,
                paused=False,
                stopped=False,
                error=None,
            )
            _emit_log(
                transform_id, "info",
                f"Resuming — skipping {len(done_paths)} already-transformed files of {total}",
            )
        else:
            await _update_progress(
                transform_id,
                status="running",
                phase="building_kb",
                phase_label="Building knowledge base",
                progress_pct=5,
                files_done=0,
                files_total=total,
                current_file=None,
                error=None,
            )
            _emit_log(transform_id, "info", f"Building knowledge base for {total} source file(s)")

        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in source_files_docs]

        kb_ctx = ""
        kb_stats = None

        # iter-15.15 — on resume, reuse cached tools_kb summary instead of
        # rebuilding + hitting the LLM again. Fall through to a rebuild
        # only if no cached doc exists (e.g. worker died before caching).
        reused_kb = False
        if resume:
            existing_kb = await tools_kb.find_one({"_id": transform_id})
            if existing_kb:
                try:
                    kb_ctx = json.dumps(existing_kb.get("summary") or {})[:15000]
                except Exception:
                    kb_ctx = ""
                kb_stats = existing_kb.get("stats")
                reused_kb = True
                _emit_log(transform_id, "info", "Reusing cached KB from previous run", stats=kb_stats)

        if not reused_kb:
            try:
                kb = build_tools_kb(src_files, [])
                kb_summary = summarize_kb_for_ui(kb)
                kb_stats = kb.get("stats")
                await tools_kb.update_one(
                    {"_id": transform_id},
                    {"$set": {
                        "_id": transform_id,
                        "owner": "transformer",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "summary": kb_summary,
                        "stats": kb_stats,
                    }},
                    upsert=True,
                )
                kb_ctx = render_kb_for_prompt(kb, max_chars=15000)
                _emit_log(
                    transform_id, "info",
                    f"KB built — {(kb_stats or {}).get('entities', '?')} entities",
                    stats=kb_stats,
                )
            except Exception as e:
                print(f"[transformer:{transform_id}] KB build failed: {e}")
                _emit_log(transform_id, "error", f"KB build failed: {e}")

        if not resume:
            # Wipe previous transformed/error rows (idempotent on regenerate)
            # NOTE: skipped on resume so orphan recovery preserves progress.
            await transform_files.delete_many({
                "transform_id": transform_id,
                "type": {"$in": ["transformed", "error"]},
            })

            await _update_progress(
                transform_id,
                phase="transforming",
                phase_label="Transforming files",
                progress_pct=15,
            )

        transformed_count = 0
        manual_review_count = 0
        confidence_sum = 0.0
        source_runtime = (transform.get("source_stack") or {}).get("runtime", "unknown")

        # iter-15.15 — on resume, seed counters from existing rows so
        # progress_pct + result summary reflect the FULL run, not just
        # the post-resume tail.
        if resume:
            transformed_count = await transform_files.count_documents(
                {"transform_id": transform_id, "type": "transformed"}
            )
            manual_review_count = await transform_files.count_documents(
                {"transform_id": transform_id, "type": "error"}
            )
            async for _r in transform_files.find(
                {"transform_id": transform_id, "type": "transformed"},
                {"confidence": 1},
            ):
                try:
                    confidence_sum += float(_r.get("confidence") or 0)
                except Exception:
                    pass

        # iter-15.14 — clear any stale pause/stop flags from a previous run
        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {"paused": False, "stopped": False}},
        )

        for idx, sf in enumerate(source_files_docs):
            # iter-15.15 — resume-safe: skip files already transformed/errored
            _sf_path = sf.get("path", f"file_{idx}")
            if resume and _sf_path in done_paths:
                continue
            # iter-15.14 — cooperative pause/stop check between files
            fresh = await transformations.find_one(
                {"_id": transform_id},
                {"paused": 1, "stopped": 1},
            )
            if fresh and fresh.get("stopped"):
                await _update_progress(
                    transform_id,
                    status="stopped",
                    phase="stopped",
                    phase_label=f"Stopped at {idx}/{total}",
                    files_done=idx,
                    current_file=None,
                    paused=False,
                )
                _emit_log(transform_id, "warn", f"Worker stopped at {idx}/{total}")
                await _log_audit("stop", "transformation", transform_id, {"files_done": idx, "total": total})
                return
            # Sleep-loop while paused; wake on resume or stop
            _paused_logged = False
            while fresh and fresh.get("paused") and not fresh.get("stopped"):
                await _update_progress(
                    transform_id,
                    phase="paused",
                    phase_label=f"Paused at {idx}/{total}",
                    files_done=idx,
                    current_file=None,
                )
                if not _paused_logged:
                    _emit_log(transform_id, "warn", f"Worker paused at {idx}/{total}")
                    _paused_logged = True
                await asyncio.sleep(2)
                fresh = await transformations.find_one(
                    {"_id": transform_id},
                    {"paused": 1, "stopped": 1},
                )
            if _paused_logged and not (fresh and fresh.get("stopped")):
                _emit_log(transform_id, "info", f"Worker resumed at {idx}/{total}")
            if fresh and fresh.get("stopped"):
                await _update_progress(
                    transform_id,
                    status="stopped",
                    phase="stopped",
                    phase_label=f"Stopped at {idx}/{total}",
                    files_done=idx,
                    current_file=None,
                    paused=False,
                )
                _emit_log(transform_id, "warn", f"Worker stopped at {idx}/{total} (during pause)")
                await _log_audit("stop", "transformation", transform_id, {"files_done": idx, "total": total})
                return

            path = sf.get("path", f"file_{idx}")
            content = sf.get("content", "")
            target_stack = _resolve_target_stack(transform, path, content)
            target_info = SUPPORTED_TRANSFORMATIONS.get(target_stack, {})
            target_label = target_info.get("target", target_stack)
            now_iso = datetime.now(timezone.utc).isoformat()
            pct = 15 + int(80 * (idx / max(total, 1)))
            await _update_progress(
                transform_id,
                phase="transforming",
                phase_label=f"Transforming {idx + 1}/{total}",
                progress_pct=pct,
                files_done=idx,
                current_file=path,
                paused=False,
            )

            user_prompt = f"""Transform this file from {source_runtime} to {target_label}.

===== PROJECT KNOWLEDGE BASE (for cross-file references) =====
{kb_ctx}

===== SOURCE FILE: {path} =====
```
{content[:8000]}
```

Apply the transformation rules. Preserve route paths, table names, and column
mappings from the KB above. Return ONLY the transformed code (no prose)."""

            try:
                _t0 = datetime.now(timezone.utc)
                _emit_log(
                    transform_id, "info",
                    f"[{idx + 1}/{total}] {path}",
                )
                response = await fabric_call(
                    messages=[
                        {"role": "system", "content": prompt_template},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model or transform.get("model"),
                    agent_key="tools.transformer.pattern",
                    project_id=await _project_id_for_transform(transform_id),
                )
                transformed_content = (
                    response.get("content", content) if isinstance(response, dict) else str(response)
                )
                # iter-16.x — Defensive fence scrub before persist. This
                # is the legacy single-file pattern transformer path (agent
                # key `tools.transformer.pattern`) — not to be confused
                # with `_run_coder` which already scrubs via
                # `_extract_code`. Without this, any LLM that emits a
                # leading ```java (nested example / instruction leakage /
                # Factory Droid narrative wrapper) ships a non-compilable
                # file straight into `transform_files` and later into the
                # compile scratch dir. Idempotent no-op on clean content.
                transformed_content = _scrub_code_fences(transformed_content)
                confidence = _compute_confidence(content, transformed_content, target_stack)
                confidence_sum += confidence

                new_path = _transform_path(path, target_stack)
                await transform_files.insert_one({
                    "transform_id": transform_id,
                    "type": "transformed",
                    "path": new_path,
                    "original_path": path,
                    "content": transformed_content,
                    "confidence": confidence,
                    "created_at": now_iso,
                })
                transformed_count += 1

                _ms = int((datetime.now(timezone.utc) - _t0).total_seconds() * 1000)
                _usage = (response or {}).get("usage") if isinstance(response, dict) else None
                _tin = int((_usage or {}).get("prompt_tokens") or 0)
                _tout = int((_usage or {}).get("completion_tokens") or 0)
                _model_used = (response or {}).get("model") if isinstance(response, dict) else None
                _emit_log(
                    transform_id,
                    "warn" if confidence < 0.5 else "llm",
                    f"→ {new_path} · confidence {confidence:.2f} · {_tin}+{_tout} tok · {_ms}ms",
                    model=_model_used,
                    tokens_in=_tin,
                    tokens_out=_tout,
                    ms=_ms,
                    confidence=confidence,
                )
            except Exception as e:
                await transform_files.insert_one({
                    "transform_id": transform_id,
                    "type": "error",
                    "path": path,
                    "original_path": path,
                    "error": str(e),
                    "confidence": 0.0,
                    "created_at": now_iso,
                })
                manual_review_count += 1
                _emit_log(transform_id, "error", f"[{idx + 1}/{total}] {path} failed: {e}")

        avg_confidence = round(confidence_sum / max(transformed_count, 1), 2) if transformed_count else 0.0
        result = {
            "files_processed": total,
            "files_transformed": transformed_count,
            "manual_review_required": manual_review_count,
            "avg_confidence": avg_confidence,
            "kb_stats": kb_stats,
            "status": "completed",
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {
                "status": "completed",
                "phase": "completed",
                "phase_label": "Completed",
                "progress_pct": 100,
                "files_done": total,
                "files_total": total,
                "current_file": None,
                "result": result,
                "files_transformed": transformed_count,
                "manual_review_count": manual_review_count,
                "avg_confidence": avg_confidence,
                "updated_at": now_iso,
                "error": None,
            }},
        )
        _emit_log(
            transform_id, "info",
            f"Completed · {transformed_count} transformed · {manual_review_count} manual review · "
            f"avg confidence {avg_confidence}",
            result=result,
        )
    except Exception as e:
        import traceback
        print(f"[transformer:{transform_id}] background task failed: {e}\n{traceback.format_exc()}")
        _emit_log(transform_id, "error", f"Background task failed: {e}")
        await _update_progress(
            transform_id,
            status="failed",
            phase="failed",
            phase_label="Failed",
            error=str(e),
        )


@router.post("/transformer/{transform_id}/run", status_code=202)
async def run_transformation(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """Kick off the code transformation as a background task.

    iter-15.10 — was previously synchronous and hit the 600s ingress timeout
    on large projects. Now returns 202 immediately; poll GET /transformer/{id}
    or /transformer/{id}/status for progress.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    if transform.get("status") == "running":
        # already in-flight — return current progress
        return {
            "id": transform_id,
            "status": "running",
            "progress_pct": transform.get("progress_pct", 0),
            "phase": transform.get("phase"),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "status": "running",
            "phase": "queued",
            "phase_label": "Queued",
            "progress_pct": 0,
            "files_done": 0,
            "current_file": None,
            "result": None,
            "error": None,
            "model": model or transform.get("model"),
            "updated_at": now_iso,
        }},
    )
    background_tasks.add_task(_run_transformation_background, transform_id, model)
    return {"id": transform_id, "status": "running", "progress_pct": 0}


# ─────────────────────────────────────────────────────────────────────
# iter-15.16 — Live telemetry log endpoints.
# Polling GET is the FE default (aligned with the 2s status poller).
# SSE stream is an optional upgrade path; capped to <55s per connection
# so it never trips the K8s ingress 60s idle timeout — EventSource
# reconnects transparently on close.
# ─────────────────────────────────────────────────────────────────────
@router.get("/transformer/{transform_id}/logs")
async def get_transformation_logs(
    transform_id: str,
    since: Optional[str] = None,
    limit: int = 200,
):
    """Return buffered worker log rows for a transformation.

    Query params:
      - since: ISO-8601 timestamp; only rows with `ts > since` are returned.
      - limit: max rows returned (default 200, capped at 500).
    """
    _evict_stale_log_buffers()
    entry = _TRANSFORM_LOG_BUFFERS.get(transform_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    if entry is None:
        return {"logs": [], "server_time": now_iso}
    since_dt = _parse_iso_utc(since) if since else None
    rows: List[Dict[str, Any]] = []
    # Snapshot the deque; iterate oldest→newest
    for row in list(entry["buf"]):
        if since_dt is not None:
            row_dt = _parse_iso_utc(row.get("ts"))
            if row_dt is None or row_dt <= since_dt:
                continue
        rows.append(row)
    if limit and limit > 0:
        rows = rows[-min(limit, _TRANSFORM_LOG_MAX):]
    return {"logs": rows, "server_time": now_iso}


@router.get("/transformer/{transform_id}/logs/stream")
async def stream_transformation_logs(
    transform_id: str,
    since: Optional[str] = None,
):
    """SSE stream of worker log rows.

    - Yields any backlog (since-filtered) immediately on connect.
    - Sends a heartbeat comment every 15s so proxies don't idle-timeout.
    - Closes cleanly after ~55s so K8s ingress 60s timeout is never hit
      and the browser EventSource reconnects transparently.
    """
    _evict_stale_log_buffers()

    async def _gen():
        started = datetime.now(timezone.utc)
        cursor_dt = _parse_iso_utc(since)
        last_heartbeat = started
        # Flush backlog first
        entry = _TRANSFORM_LOG_BUFFERS.get(transform_id)
        if entry:
            for row in list(entry["buf"]):
                row_dt = _parse_iso_utc(row.get("ts"))
                if cursor_dt is None or (row_dt and row_dt > cursor_dt):
                    yield f"data: {json.dumps(row)}\n\n"
                    if row_dt:
                        cursor_dt = row_dt
        while True:
            now = datetime.now(timezone.utc)
            # Bounded connection lifetime — well under the 60s ingress limit.
            if (now - started).total_seconds() > 55:
                yield "event: close\ndata: {\"reason\":\"rotate\"}\n\n"
                return
            entry = _TRANSFORM_LOG_BUFFERS.get(transform_id)
            emitted = False
            if entry:
                for row in list(entry["buf"]):
                    row_dt = _parse_iso_utc(row.get("ts"))
                    if cursor_dt is None or (row_dt and row_dt > cursor_dt):
                        yield f"data: {json.dumps(row)}\n\n"
                        if row_dt:
                            cursor_dt = row_dt
                        emitted = True
            # Heartbeat every 15s so the connection is never idle for long.
            if (now - last_heartbeat).total_seconds() >= 15:
                yield ": ping\n\n"
                last_heartbeat = now
            if not emitted:
                await asyncio.sleep(1.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# iter-15.62.8 — Compile-fix-loop terminal phases (mirrors the FE's own
# terminal-phase list in Transformer.jsx) + staleness threshold used by
# `get_transformation_status` to detect an orphaned compile-fix run.
#
# iter-15.62.9 — Bumped 120s → 300s. With bounded Coder-wave concurrency
# (`LAMA_COMPILE_FIX_CONCURRENCY`, default 3) and real LLM latency for a
# Coder call FOLLOWED BY a Verifier call on the same file, a single file
# can legitimately take several minutes even though the loop is actively
# working — 120s was tripping false positives on slow-but-alive runs
# (see PRD iter-15.62.9). Paired with the new intra-file heartbeat in
# `_run_compile_fix_loop._run_one` (touches `updated_at` between the
# Coder and Verifier calls, not just once per file), 300s now has to
# elapse with ZERO heartbeats at all — i.e. a genuinely dead loop —
# before the FE is told to stop polling and prompt a rerun.
_COMPILE_FIX_TERMINAL_PHASES = frozenset({"passed", "exhausted", "unfixable", "infra_blocked", "stagnant"})
_COMPILE_STALL_THRESHOLD_SEC = 300


@router.get("/transformer/{transform_id}/status")
async def get_transformation_status(transform_id: str):
    """Lightweight status poll — returns just the progress fields.

    iter-15.10 — FE polls this every 2s while a transformation is running so
    the progress bar reflects real per-file progress instead of hanging on a
    long-running POST.
    iter-15.14 — Also returns `paused` and `stopped` flags for the
    pause/resume/stop UI in the Transformer header.
    """
    doc = await transformations.find_one(
        {"_id": transform_id},
        {
            "status": 1, "phase": 1, "phase_label": 1, "progress_pct": 1,
            "files_done": 1, "files_total": 1, "current_file": 1,
            "avg_confidence": 1, "result": 1, "error": 1, "updated_at": 1,
            "paused": 1, "stopped": 1, "worker_heartbeat": 1,
            # iter-15.60 — Expose created_at so the header "Elapsed" timer
            # can seed from the transformation's true start time instead
            # of the session's mount time (fixes reset on resume).
            "created_at": 1,
            # iter-15.55 — Live Tester progress for the Activity Rail.
            "tester_progress": 1,
            # iter-15.58 — Live compile→fix loop progress.
            "compile_fix_progress": 1,
            "compilation_result": 1,
            # iter-15.6x — Live streaming compile console (real subprocess
            # stdout/stderr, appended as the build runs).
            "compile_console": 1,
            "compile_console_updated_at": 1,
            # iter-19 — DevOps gate verdict + its remediation trail.
            "dependency_audit": 1,
            "production_ready": 1,
        },
    )
    if not doc:
        raise HTTPException(404, "Transformation not found")

    # iter-15.62.8 — Compile-fix-loop orphan/staleness detection.
    # `_run_compile_fix_loop` runs as a `BackgroundTasks` job with NO
    # heartbeat/lease of its own (unlike the main transformation worker,
    # which already has `worker_heartbeat` + `paused_orphan` recovery). If
    # the backend process restarts (deploy, crash, `docker compose
    # restart`) mid-compile, `compile_fix_progress` freezes on whatever
    # non-terminal phase it last reached and NOTHING ever updates it
    # again — the FE has no choice but to poll silently for its full
    # 20-minute client-side timeout, which reads to the user as "rerun
    # compile is stuck". Surface that state explicitly instead: if the
    # phase isn't one of the terminal outcomes and neither
    # `compile_fix_progress` nor the live `compile_console` has been
    # touched in over `_COMPILE_STALL_THRESHOLD_SEC`, flag it so the FE
    # can stop spinning immediately and prompt a retry.
    cfp = doc.get("compile_fix_progress") or None
    compile_fix_stalled = False
    if cfp and cfp.get("phase") not in _COMPILE_FIX_TERMINAL_PHASES:
        last_touch = _parse_iso_utc(cfp.get("updated_at"))
        console_touch = _parse_iso_utc(doc.get("compile_console_updated_at"))
        if console_touch and (not last_touch or console_touch > last_touch):
            last_touch = console_touch
        if last_touch:
            age_sec = (datetime.now(timezone.utc) - last_touch).total_seconds()
            compile_fix_stalled = age_sec > _COMPILE_STALL_THRESHOLD_SEC

    return {
        "id": transform_id,
        "status": doc.get("status"),
        "phase": doc.get("phase"),
        "phase_label": doc.get("phase_label"),
        "progress_pct": doc.get("progress_pct", 0),
        "files_done": doc.get("files_done", 0),
        "files_total": doc.get("files_total", 0),
        "current_file": doc.get("current_file"),
        "avg_confidence": doc.get("avg_confidence"),
        "result": doc.get("result"),
        "error": doc.get("error"),
        "updated_at": doc.get("updated_at"),
        "paused": bool(doc.get("paused")),
        "stopped": bool(doc.get("stopped")),
        # iter-15.15 — expose lease metadata so FE can render "Interrupted
        # by restart — click Resume" banner when the worker is gone.
        "worker_heartbeat": doc.get("worker_heartbeat"),
        # iter-15.55 — Tester live progress: {done,total,per_tier,in_flight,recent}.
        "tester_progress": doc.get("tester_progress") or None,
        # iter-15.60 — Absolute run start so the header timer survives resume.
        "created_at": doc.get("created_at"),
        # iter-15.58 — Compile-fix loop live progress + latest compile
        # result so the FE "Rerun compile" panel can show iteration state.
        "compile_fix_progress": doc.get("compile_fix_progress") or None,
        "compilation_result": doc.get("compilation_result") or None,
        # iter-15.6x — Live compile console lines (real, streaming
        # subprocess output) for the right-hand "Console" panel.
        "compile_console": doc.get("compile_console") or [],
        "compile_console_updated_at": doc.get("compile_console_updated_at"),
        # iter-15.62.8 — see comment above; True when the compile-fix
        # loop's background job has gone silent mid-run (most likely a
        # server restart killed it) so the FE can stop polling/spinning
        # immediately instead of waiting out its full client-side timeout.
        "compile_fix_stalled": compile_fix_stalled,
        # iter-19 — the DevOps gate. `production_ready` is load-bearing now:
        # a green compile with critical manifest findings ends the run as
        # `completed_with_errors`, so the FE must be able to say WHY.
        "dependency_audit": doc.get("dependency_audit") or None,
        "production_ready": doc.get("production_ready"),
        # iter-20 — `compile_green` was persisted but never projected, so
        # the FE had to infer the build state from `status` strings. It now
        # gates the download button, which needs the real value.
        "compile_green": doc.get("compile_green"),
        # Single source of truth for whether the output may leave the
        # system, computed by the same helper the download endpoint
        # enforces — so the button and the 409 can never disagree.
        "download_blocked_reason": _build_readiness_gate(doc) or None,
    }


# iter-15.15 — Alias for the FE — `phase=paused_orphan` doubles as a
# machine-readable flag AND a human phase label for the header.


@router.post("/transformer/{transform_id}/resume-orphan")
async def resume_orphan_transformation(transform_id: str):
    """iter-15.15 — belt-and-braces manual resume. Refuses if a live
    worker still holds the lease (heartbeat fresh within
    `_WORKER_LEASE_STALE_SECS`).
    """
    doc = await transformations.find_one({"_id": transform_id})
    if not doc:
        raise HTTPException(404, "Transformation not found")
    now = datetime.now(timezone.utc)
    hb = _parse_iso_utc(doc.get("worker_heartbeat"))
    if hb is not None and (now - hb).total_seconds() < _WORKER_LEASE_STALE_SECS:
        return {"resumed": False, "reason": "worker_active"}
    files_done = int(doc.get("files_done") or 0)
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "paused": False,
            "stopped": False,
            "phase": "transforming",
            "phase_label": "Resuming (manual)",
            "updated_at": now.isoformat(),
            "worker_pid": None,
            "worker_heartbeat": None,
            "error": None,
        }},
    )
    asyncio.create_task(_run_transformation_background(transform_id, resume=True))
    await _log_audit("resume_orphan", "transformation", transform_id, {"start_idx": files_done})
    return {"resumed": True, "start_idx": files_done}


# iter-15.14 — Pause / Resume / Stop endpoints. The background worker
# (`_run_transformation_background`) checks these flags between file
# iterations so the response is cooperative (not instantaneous) but
# survives container restarts because state lives in Mongo.
@router.post("/transformer/{transform_id}/pause")
async def pause_transformation(transform_id: str):
    doc = await transformations.find_one({"_id": transform_id})
    if not doc:
        raise HTTPException(404, "Transformation not found")
    if doc.get("status") != "running":
        raise HTTPException(400, "Transformation is not running")
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {"paused": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    await _log_audit("pause", "transformation", transform_id, {})
    return {"paused": True}


@router.post("/transformer/{transform_id}/resume")
async def resume_transformation(transform_id: str):
    doc = await transformations.find_one({"_id": transform_id})
    if not doc:
        raise HTTPException(404, "Transformation not found")
    now = datetime.now(timezone.utc)
    # iter-15.15 — detect an orphaned run (no live worker) and relaunch
    # the background worker with resume=True. Without this, flipping
    # paused=False alone is a no-op because there is no worker to
    # observe the flag after a container restart.
    hb = _parse_iso_utc(doc.get("worker_heartbeat"))
    worker_alive = hb is not None and (now - hb).total_seconds() < _WORKER_LEASE_STALE_SECS
    needs_relaunch = (not worker_alive) or (doc.get("phase") == "paused_orphan")
    set_fields = {"paused": False, "updated_at": now.isoformat()}
    if needs_relaunch:
        set_fields["phase"] = "transforming"
        set_fields["phase_label"] = "Resuming"
        set_fields["error"] = None
        set_fields["worker_pid"] = None
        set_fields["worker_heartbeat"] = None
    await transformations.update_one({"_id": transform_id}, {"$set": set_fields})
    if needs_relaunch:
        asyncio.create_task(_run_transformation_background(transform_id, resume=True))
    await _log_audit("resume", "transformation", transform_id, {"relaunched": needs_relaunch})
    return {"paused": False, "relaunched": needs_relaunch}


@router.post("/transformer/{transform_id}/stop")
async def stop_transformation(transform_id: str):
    """Cooperative stop — the worker sees `stopped=True` before its next
    file and finalises with status=stopped, keeping files done so far.
    The user can then delete the transformation or start a new one.
    """
    doc = await transformations.find_one({"_id": transform_id})
    if not doc:
        raise HTTPException(404, "Transformation not found")
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "stopped": True,
            "paused": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    await _log_audit("stop_request", "transformation", transform_id, {})
    return {"stopped": True}


@router.post("/transformer/{transform_id}/files/{file_id}/regenerate")
async def regenerate_transformation_file(
    transform_id: str,
    file_id: str,
    model: Optional[str] = None,
):
    """Re-run transformation for a single file with an optional model override.

    iter-15.10 — supports the FE "Regenerate" button next to each generated
    file. Preserves the file_id so the UI can reload the same row.
    """
    return await _regenerate_single_file_impl(transform_id, file_id, model)


async def _regenerate_single_file_impl(
    transform_id: str,
    file_id: str,
    model: Optional[str] = None,
):
    """iter-15.38 — extracted from `regenerate_transformation_file` so the
    new Planner/Coder/Tester chat assistant (`agent_chat`) can trigger a
    regeneration as one of its structured actions without duplicating this
    logic."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    try:
        oid = ObjectId(file_id)
    except Exception:
        raise HTTPException(400, "Invalid file_id")

    existing = await transform_files.find_one({"_id": oid, "transform_id": transform_id})
    if not existing:
        raise HTTPException(404, "File not found")

    original_path = existing.get("original_path") or existing.get("path")
    source = await transform_files.find_one({
        "transform_id": transform_id,
        "type": "source",
        "path": original_path,
    })
    if not source:
        raise HTTPException(400, f"Source file not found for path: {original_path}")

    prompt_template = await _get_prompt("tools.transformer")
    if not prompt_template:
        raise HTTPException(500, "Transformer prompt not configured")

    # Prefer previously-built KB context if available
    kb_doc = await tools_kb.find_one({"_id": transform_id})
    kb_ctx = ""
    if kb_doc and kb_doc.get("summary"):
        try:
            kb_ctx = json.dumps(kb_doc["summary"])[:15000]
        except Exception:
            kb_ctx = ""

    content = source.get("content", "")
    target_stack = _resolve_target_stack(transform, original_path, content)
    target_info = SUPPORTED_TRANSFORMATIONS.get(target_stack, {})
    target_label = target_info.get("target", target_stack)
    source_runtime = (transform.get("source_stack") or {}).get("runtime", "unknown")

    user_prompt = f"""Transform this file from {source_runtime} to {target_label}.

===== PROJECT KNOWLEDGE BASE (for cross-file references) =====
{kb_ctx}

===== SOURCE FILE: {original_path} =====
```
{content[:8000]}
```

Apply the transformation rules. Preserve route paths, table names, and column
mappings from the KB above. Return ONLY the transformed code (no prose)."""

    try:
        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": user_prompt},
            ],
            model=model or transform.get("model"),
            agent_key="tools.transformer.pattern",
            project_id=await _project_id_for_transform(transform_id),
        )
        transformed_content = (
            response.get("content", content) if isinstance(response, dict) else str(response)
        )
        # iter-16.x — Defensive fence scrub before persist (regenerate).
        transformed_content = _scrub_code_fences(transformed_content)
    except Exception as e:
        raise HTTPException(500, f"Regeneration failed: {e}")

    confidence = _compute_confidence(content, transformed_content, target_stack)
    new_path = _transform_path(original_path, target_stack)
    now_iso = datetime.now(timezone.utc).isoformat()

    await transform_files.update_one(
        {"_id": oid, "transform_id": transform_id},
        {"$set": {
            "type": "transformed",
            "path": new_path,
            "original_path": original_path,
            "content": transformed_content,
            "confidence": confidence,
            "regenerated_at": now_iso,
            "regenerated_with_model": model,
        }, "$unset": {"error": ""}},
    )

    await _log_audit("regenerate_file", "transformation", transform_id, {
        "file_id": file_id,
        "path": original_path,
        "model": model,
        "confidence": confidence,
    })

    return {
        "id": file_id,
        "path": new_path,
        "original_path": original_path,
        "confidence": confidence,
        "content_preview": transformed_content[:500],
    }


@router.get("/transformer/{transform_id}/kb")
async def get_transformer_kb(transform_id: str):
    """Return the source-side KB built for this transformation
    (entities + UI→API→DB map). Populated at start of `/run`."""
    doc = await tools_kb.find_one({"_id": transform_id})
    if not doc:
        raise HTTPException(404, "KB not built yet for this transformation")
    doc.pop("_id", None)
    return doc


def _slim_kb_ctx_for_task(
    full_kb_ctx: str,
    envelope: Optional[Dict[str, Any]],
    source_path: str,
) -> str:
    """iter-15.42 — Return a file-scoped KB context slice for the Coder.

    The `full_kb_ctx` passed to the Coder used to be a ~15KB dump of the
    whole project KB summary on EVERY task — most of it irrelevant to
    the file being transformed, and paying a large prompt-token cost per
    call. When an ARCHITECTURE envelope is available (built by the
    Planner in `_build_deterministic_tasks`), the envelope already
    contains the endpoint, controller, service, repository, tables and
    business rules for this specific file — that IS the KB slice.

    Behaviour:
      - Envelope present → return a compact JSON summary of the envelope
        (endpoint + controller + service + repository + tables + BR ids).
        Typical size: 800-2500 chars.
      - No envelope (unmapped file) → return the first 4000 chars of the
        global KB summary as a fallback (still safer than 15KB).
    """
    if envelope and isinstance(envelope, dict):
        try:
            trimmed = {
                "source_path": source_path,
                "endpoint": envelope.get("endpoint"),
                "http_method": envelope.get("http_method"),
                "controller": envelope.get("controller"),
                "service": envelope.get("service"),
                "repository": envelope.get("repository"),
                "tables": envelope.get("tables") or envelope.get("related_tables"),
                "columns": envelope.get("columns"),
                "business_logic": envelope.get("business_logic"),
                "business_rules": envelope.get("business_rules"),
                "dependencies": envelope.get("dependencies"),
            }
            trimmed = {k: v for k, v in trimmed.items() if v}
            return json.dumps(trimmed, ensure_ascii=False)[:4000]
        except Exception:
            pass
    return (full_kb_ctx or "")[:4000]


def _transform_path(path: str, target_stack) -> str:
    """Transform file path based on target stack conventions.

    iter-15.31 fix: `target_stack` in the multi-agent pipeline is a DICT
    of category → id (e.g. {"backend": "spring-boot-3", "database":
    "postgresql", "frontend": "react-18", ...}), not a bare string. The
    previous implementation assumed a string, so `target_stack.startswith(...)`
    never matched anything real (every call site either passed the dict —
    a runtime TypeError waiting to happen — or passed `""` to dodge it) and
    `target_path` silently stayed identical to `source_path` for every task.
    Both shapes are accepted here for backward compatibility.
    """
    if isinstance(target_stack, dict):
        backend = str(target_stack.get("backend") or "")
        database = str(target_stack.get("database") or "")
        frontend = str(target_stack.get("frontend") or "")
    else:
        backend = database = frontend = str(target_stack or "")

    if backend.startswith("spring"):
        # Helidon/JAX-RS resource → Spring controller
        path = path.replace("Resource.java", "Controller.java")
        path = path.replace("/resource/", "/controller/")

    if database == "postgresql":
        path = path.replace(".sql", ".pgsql")

    if frontend.startswith("react"):
        if path.endswith(".jsp"):
            path = path.replace(".jsp", ".tsx")
        path = path.replace("/WEB-INF/views/", "/src/pages/")

    return path


# ── iter-15.31 — Deterministic Planner algorithm ────────────────────────
# Mirrors doc/agents/planner.md's WAVE structure: every uploaded source
# file is classified into an architectural layer (by path/filename
# heuristics that are NOT tied to any one source or target stack) and
# grouped into dependency-ordered waves. This guarantees full-project
# coverage and a non-empty task list regardless of project size, unlike
# the previous "ask an LLM to invent + enumerate every file as one JSON
# blob" approach which silently produced 0 tasks on real (600+ file)
# projects.
_LAYER_WAVE_META = {
    "config":      (1, "Wave 1 — Build & Config Scaffold"),
    "model":       (2, "Wave 2 — Models, Entities & DTOs"),
    "repository":  (3, "Wave 3 — Persistence / Repository Layer"),
    "service":     (4, "Wave 4 — Service / Business Logic Layer"),
    "controller":  (5, "Wave 5 — Controller / API Layer"),
    "integration": (6, "Wave 6 — Integration Layer (REST / external clients)"),
    "filter":      (7, "Wave 7 — Security & Filter Layer"),
    "exception":   (8, "Wave 8 — Exception Handling"),
    "test":        (9, "Wave 9 — Tests & Support Files"),
    "other":       (9, "Wave 9 — Tests & Support Files"),
}

# iter-15.38 — reverse lookup (wave number → canonical wave name) so the
# Planner-chat "reassign_wave" action can set a proper `wave_name` instead
# of a bare "Wave N" placeholder when the LLM only returns a wave number.
_WAVE_NAME_BY_NUM: Dict[int, str] = {}
for _layer_key, (_wave_num, _wave_label) in _LAYER_WAVE_META.items():
    _WAVE_NAME_BY_NUM.setdefault(_wave_num, _wave_label)

_CODE_EXTENSIONS = {
    ".java", ".kt", ".scala", ".groovy", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".cs", ".vb", ".go", ".rb", ".php", ".rs", ".swift", ".ex", ".exs",
}

_CONFIG_MANIFEST_BASENAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "web.xml", "application.yml", "application.yaml", "application.properties",
    "microprofile-config.properties", "requirements.txt", "package.json",
    "dockerfile", "docker-compose.yml", "beans.xml", "persistence.xml",
    "web.config", "packages.config", "composer.json",
}


def _classify_file_layer(path: str) -> str:
    """Best-effort, stack-agnostic layer classification from a file path.
    Uses common directory/filename conventions shared across mainstream
    backend frameworks (Java/Helidon/Spring, Python, Node, .NET, etc.) —
    never a specific vendor name, so it works regardless of which
    source/target stack was selected."""
    p = path.lower()
    base = p.rsplit("/", 1)[-1]

    if base in _CONFIG_MANIFEST_BASENAMES:
        return "config"
    if "/restclient/" in p or "/client/" in p or base.endswith("client.java") or "feignclient" in p:
        return "integration"
    if "/filter/" in p or base.endswith("filter.java") or "securityconfig" in base or "corsconfig" in base or "/security/" in p:
        return "filter"
    if "/exception/" in p or "exceptionmapper" in base or "exceptionhandler" in base or "globalexceptionhandler" in base:
        return "exception"
    if "/controller/" in p or base.endswith("controller.java") or base.endswith("resource.java") or "/routes/" in p or "/views/" in p:
        return "controller"
    if "/service/" in p or "/usecase" in p or base.endswith("service.java") or base.endswith("serviceimpl.java") or base.endswith("usecase.java"):
        return "service"
    if "/repository/" in p or "/dao/" in p or base.endswith("repository.java") or base.endswith("dao.java") or base.endswith("mapper.java"):
        return "repository"
    if "/entity/" in p or "/model/" in p or "/dto/" in p or "/enums/" in p or base.endswith("entity.java") or base.endswith("dto.java"):
        return "model"
    if "/test/" in p or base.startswith("test_") or base.endswith("test.java") or base.endswith(".test.js") or base.endswith(".spec.ts"):
        return "test"
    return "other"


def _lang_family(runtime: str) -> str:
    """Reduce a runtime id (e.g. 'java-21', 'python-3.12') to its language
    family ('java', 'python') so cross-language migrations can be detected
    without hardcoding any specific version pair."""
    runtime = (runtime or "").lower()
    for fam in ("java", "python", "node", "dotnet", "go", "rust", "kotlin", "ruby", "php", "elixir", "deno", "bun"):
        if runtime.startswith(fam):
            return fam
    return runtime.split("-")[0] if runtime else ""


# Build manifests recognised on the SOURCE side. Broader than
# `_MANIFEST_BASENAMES` (which describes what we GENERATE) because a legacy
# project may build with something we never emit — its dependency list is
# still the input we need.
_SOURCE_MANIFEST_BASENAMES = frozenset({
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "go.mod", "gemfile", "composer.json", "build.sbt", "ivy.xml",
})

# How much of the source manifest to show. Manifests are small; a
# multi-module aggregator pom is the only realistic outlier.
_SOURCE_MANIFEST_MAX_CHARS = 12000


def _source_manifest_for(
    manifests: Dict[str, Dict[str, str]], module_root: str,
) -> Optional[Dict[str, str]]:
    """The source build manifest most relevant to `module_root`.

    Exact module match first, then the repo-root manifest (a parent pom
    governs its children's versions, so it is the right second choice),
    then any manifest at all — a single-module upload whose manifest sits
    in a directory we did not classify as a module root still beats
    generating blind.
    """
    if not manifests:
        return None
    key = (module_root or "").strip("/")
    if key in manifests:
        return manifests[key]
    if "" in manifests:
        return manifests[""]
    return next(iter(manifests.values()), None)


# ═══════════════════════════════════════════════════════════════════
# iter-20 — Per-target migration playbooks
# ═══════════════════════════════════════════════════════════════════
#
# The operator's own workaround for a failed migration was to hand the
# generated folder to a chat model with a language-specific brief:
#
#     role: senior BE developer and legacy-modernisation expert
#     task: convert Helidon -> Spring Boot, Oracle -> Postgres;
#           implement Swagger; ensure the build is error-free
#     strict: do not alter business logic; do not conclude with broken
#           code; token efficiency with 100% accuracy
#
# That brief works because it is SPECIFIC to the target stack. The Coder
# prompt is deliberately stack-agnostic ("uses detected source/target
# mappings"), which keeps it reusable but leaves the model to infer what
# "idiomatic Spring Boot" means — including things no generic instruction
# covers, like "a REST controller is @RestController, not @Path" or "the
# build must expose an OpenAPI document".
#
# A playbook is that brief, per target, as data. Keyed on the target stack
# id already used by `SUPPORTED_TRANSFORMATIONS` / `BUILD_TOOL_SUGGESTIONS`
# so adding a language is one dict entry and no code — the same convention
# `owl_extractor` follows for parsers.
#
# Deliberately short. These are injected into every Coder call, so every
# line costs tokens on every file; anything the model reliably knows already
# (Java syntax, what a POJO is) is omitted. Only the decisions that
# actually drift are stated.
MIGRATION_PLAYBOOKS: Dict[str, Dict[str, Any]] = {
    "spring-boot-3": {
        "language": "Java 17+",
        "idioms": [
            "@RestController + @RequestMapping/@GetMapping (never JAX-RS @Path/@GET)",
            "constructor injection (never field @Inject/@Autowired)",
            "@ConfigurationProperties or @Value (never MicroProfile @ConfigProperty)",
            "ResponseEntity<T> (never jakarta.ws.rs.core.Response)",
            "Spring Data JPA repositories for persistence",
            "@Transactional from org.springframework.transaction.annotation",
            "jakarta.* imports throughout (Spring Boot 3 is Jakarta EE 9+)",
        ],
        "manifest": [
            "spring-boot-starter-parent as <parent>, so versions are inherited",
            "spring-boot-starter-web for REST, -data-jpa for persistence, "
            "-validation for bean validation, -test for tests",
            "springdoc-openapi-starter-webmvc-ui for the OpenAPI/Swagger UI",
            "spring-boot-maven-plugin so the jar is executable",
        ],
        "api_docs": (
            "Expose OpenAPI/Swagger: add springdoc-openapi-starter-webmvc-ui "
            "and annotate controllers with @Tag/@Operation. /swagger-ui.html "
            "must resolve on a running app."
        ),
    },
    "spring-mvc": {
        "language": "Java",
        "idioms": [
            "@Controller/@RestController + @RequestMapping",
            "constructor injection",
            "ModelAndView only where a server-rendered view is genuinely returned",
        ],
        "manifest": ["spring-webmvc", "a servlet container dependency"],
        "api_docs": "Expose OpenAPI via springdoc where controllers are REST.",
    },
    "quarkus": {
        "language": "Java 17+",
        "idioms": [
            "jakarta.ws.rs JAX-RS resources (@Path/@GET) — this IS the Quarkus way",
            "@ApplicationScoped beans, constructor injection",
            "@ConfigProperty for configuration",
            "Panache or Hibernate ORM for persistence",
        ],
        "manifest": ["quarkus-bom", "quarkus-resteasy-reactive", "quarkus-maven-plugin"],
        "api_docs": "Add quarkus-smallrye-openapi; /q/swagger-ui must resolve.",
    },
    "fastapi": {
        "language": "Python 3.11+",
        "idioms": [
            "APIRouter per resource, included on the app",
            "Pydantic v2 models for request/response bodies",
            "dependency injection via Depends()",
            "async def handlers where I/O is awaited; plain def otherwise",
            "SQLAlchemy 2.x style for persistence",
        ],
        "manifest": [
            "fastapi, uvicorn[standard], pydantic>=2, sqlalchemy>=2",
            "psycopg[binary] for PostgreSQL",
            "every dependency pinned — an unpinned requirements.txt is not reproducible",
        ],
        "api_docs": (
            "FastAPI serves OpenAPI at /docs automatically. Give every route "
            "response_model= and a summary so the document is useful."
        ),
    },
    "django": {
        "language": "Python 3.11+",
        "idioms": [
            "apps with models.py / views.py / urls.py",
            "Django ORM models (never raw SQL where the ORM suffices)",
            "DRF serializers + ViewSets for REST",
        ],
        "manifest": ["django, djangorestframework, psycopg[binary], pinned"],
        "api_docs": "Add drf-spectacular and expose /api/schema/swagger-ui/.",
    },
    "express": {
        "language": "Node 20+",
        "idioms": [
            "express.Router() per resource",
            "async handlers with a central error middleware",
            "no business logic in the route handler — delegate to a service module",
        ],
        "manifest": ["express, and a pinned version for every dependency",
                     "scripts.build and scripts.start present"],
        "api_docs": "Add swagger-ui-express + an OpenAPI document; serve it at /api-docs.",
    },
    "nestjs": {
        "language": "Node 20+ / TypeScript",
        "idioms": ["@Controller/@Injectable with constructor injection",
                   "DTO classes with class-validator decorators"],
        "manifest": ["@nestjs/core, @nestjs/common, reflect-metadata, rxjs"],
        "api_docs": "Use @nestjs/swagger and SwaggerModule.setup().",
    },
    "dotnet": {
        "language": "C# / .NET 8",
        "idioms": [
            "minimal APIs or [ApiController] controllers",
            "constructor injection via the built-in DI container",
            "EF Core for persistence",
        ],
        "manifest": ["<TargetFramework>net8.0</TargetFramework>",
                     "Microsoft.EntityFrameworkCore, Npgsql for PostgreSQL"],
        "api_docs": "Add Swashbuckle.AspNetCore and call AddSwaggerGen/UseSwaggerUI.",
    },
    "go": {
        "language": "Go 1.22+",
        "idioms": ["net/http or chi handlers", "explicit error returns, no panics for control flow",
                   "database/sql or sqlx with pgx for PostgreSQL"],
        "manifest": ["go.mod with an explicit go directive and pinned requires"],
        "api_docs": "Generate OpenAPI with swaggo/swag annotations.",
    },
    "react-18": {
        "language": "TypeScript/JavaScript, React 18",
        "idioms": [
            "function components with hooks (never class components)",
            "no direct DOM manipulation — no jQuery, no document.getElementById",
            "state via useState/useReducer; server data via fetch in useEffect or a query library",
        ],
        "manifest": ["react@18, react-dom@18, and a build script"],
        "api_docs": "",
    },
    "postgresql": {
        "language": "PostgreSQL SQL / PL/pgSQL",
        "idioms": [
            "SERIAL/BIGSERIAL or GENERATED AS IDENTITY (never Oracle sequences + triggers)",
            "VARCHAR/TEXT (never VARCHAR2), NUMERIC (never NUMBER)",
            "COALESCE (never NVL), CURRENT_DATE/now() (never SYSDATE)",
            "LIMIT/OFFSET (never ROWNUM), recursive CTEs (never CONNECT BY)",
            "no FROM DUAL — PostgreSQL allows a bare SELECT",
        ],
        "manifest": [],
        "api_docs": "",
    },
}

# Frameworks whose *target* is really the same family, so a lookup miss on
# a versioned id still finds the right playbook.
_PLAYBOOK_ALIASES: Dict[str, str] = {
    "spring-boot-2": "spring-boot-3",
    "spring-beans": "spring-boot-3",
    "spring-webflux": "spring-boot-3",
    "micronaut": "quarkus",       # both are JAX-RS-shaped, DI-first Java
    "angular-17": "react-18",
    "vue-3": "react-18",
    "mysql": "postgresql",
}


def _playbook_for(target_stack) -> str:
    """Render the migration playbook(s) for the selected target(s).

    Returns "" when nothing matches, so a target we have no playbook for
    degrades to the existing stack-agnostic behaviour rather than
    receiving invented guidance.
    """
    ids: List[str] = []
    if isinstance(target_stack, dict):
        ids = [str(v).lower() for v in target_stack.values() if v]
    elif target_stack:
        ids = [str(target_stack).lower()]

    seen: Set[str] = set()
    blocks: List[str] = []
    for tid in ids:
        key = tid if tid in MIGRATION_PLAYBOOKS else _PLAYBOOK_ALIASES.get(tid, "")
        if not key or key in seen:
            continue
        seen.add(key)
        pb = MIGRATION_PLAYBOOKS.get(key)
        if not pb:
            continue
        lines = [f"TARGET `{key}` ({pb.get('language', '')}):"]
        for idiom in pb.get("idioms") or []:
            lines.append(f"  - {idiom}")
        if pb.get("manifest"):
            lines.append("  Build manifest:")
            for m in pb["manifest"]:
                lines.append(f"    - {m}")
        if pb.get("api_docs"):
            lines.append(f"  API docs: {pb['api_docs']}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return (
        "===== TARGET STACK PLAYBOOK (authoritative for idiom choices) =====\n"
        + "\n\n".join(blocks)
        + "\n\nThese are the conventions of the stack you are migrating TO. "
          "Where they conflict with how the source did it, the playbook wins "
          "— that difference IS the migration. Never leave a source-framework "
          "annotation, import or dependency in place because it still "
          "compiles."
    )


# iter-20 — how much source and KB context the Coder is shown.
#
# The historic pair (10 000 / 12 000 chars) predates every model now in the
# ladder. Kept as the LOCAL default because a local engine sizes `num_ctx`
# from the prompt and a 60 KB prompt to a 32 K model is a guaranteed
# timeout, not a better migration.
_CODER_SOURCE_CHARS_LOCAL = 10000
_CODER_KB_CHARS_LOCAL = 12000
_CODER_SOURCE_CHARS_CLOUD = 60000
_CODER_KB_CHARS_CLOUD = 24000


async def _coder_context_budget() -> Tuple[int, int]:
    """(source_chars, kb_chars) for the Coder prompt.

    Overridable with LAMA_CODER_SOURCE_CHARS / LAMA_CODER_KB_CHARS, which
    apply to both provider kinds — an operator who sets them has said what
    they want.
    """
    try:
        is_local = await active_default_provider_is_local()
    except Exception:  # noqa: BLE001 — budget must never break a migration
        is_local = False
    src = _CODER_SOURCE_CHARS_LOCAL if is_local else _CODER_SOURCE_CHARS_CLOUD
    kb = _CODER_KB_CHARS_LOCAL if is_local else _CODER_KB_CHARS_CLOUD

    def _override(env: str, current: int) -> int:
        raw = (os.environ.get(env) or "").strip()
        if not raw:
            return current
        try:
            return max(1000, int(raw))
        except ValueError:
            return current

    return (
        _override("LAMA_CODER_SOURCE_CHARS", src),
        _override("LAMA_CODER_KB_CHARS", kb),
    )


def _manifest_migration_contract(detected_stack: dict, target_stack) -> str:
    """What a build manifest must keep, drop and never contain.

    iter-20 — a dependency list is not transformed the way source code is:
    roughly half of it must survive verbatim and the other half must
    DISAPPEAR, and nothing in the generic per-layer guidance said so. The
    operator's Helidon -> Spring Boot run produced a pom that still
    declared io.helidon because "transform this file" is, for a manifest,
    an instruction to translate what is there rather than to remove it.

    Shared by both routes a manifest can reach the Coder by: the synthetic
    GENERATE task, and the ordinary per-file transform used when the
    source project ships a manifest at the same target path.
    """
    # Only the COORDINATE-shaped residue tokens belong in a manifest
    # warning. The full residue set is mostly code and SQL (`@Produces(`,
    # `NVL(`, `FROM DUAL`) which cannot appear in a dependency list, and
    # listing them here would spend tokens telling the model not to do
    # something it was never going to do — and bury the two entries that
    # matter (`io.helidon`, `oracle.jdbc`) among ten that do not.
    banned = [
        t for t in _residue_tokens_for(detected_stack or {}, target_stack)
        if ("." in t or "-" in t)
        and not any(c in t for c in "@() %:$")
        and t.lower() == t   # groupIds are lowercase; SQL keywords are not
    ]
    banned_line = ""
    if banned:
        shown = ", ".join(banned[:12])
        banned_line = (
            f"\n  - FORBIDDEN: the result must not contain any of these "
            f"tokens in a groupId, artifactId, property, plugin or "
            f"repository entry: {shown}. They belong to the stack being "
            f"migrated away from."
        )
    return (
        "\n\nBUILD MANIFEST CONTRACT — a dependency list is not translated "
        "line by line; it is re-derived:"
        "\n  - KEEP every third-party dependency that is NOT part of the "
        "source framework (drivers, JSON/XML libraries, logging, "
        "validation, testing, client SDKs, internal artifacts). Dropping "
        "one is a build failure that will not surface until compile time."
        "\n  - REMOVE every dependency, plugin, BOM and property belonging "
        "to the source framework, and add the target stack's equivalent "
        "instead. Do not carry them over and do not comment them out."
        "\n  - PRESERVE groupId / artifactId / version and the module "
        "structure, so sibling modules still resolve each other."
        f"{banned_line}"
    )


def _source_manifest_notes(
    src_manifest: Optional[Dict[str, str]], detected_stack: dict, target_stack,
) -> str:
    """The manifest contract PLUS the source manifest itself.

    iter-20 — a synthetic manifest task carried `source_path: ""`, so
    `_run_coder` was invoked with `source_content=""`: the target pom was
    authored from a prose spec alone, having NEVER SEEN the source pom. It
    could not carry the project's real third-party dependencies across,
    and could not deliberately drop the source framework's.

    Showing it the input is most of the fix; `_manifest_migration_contract`
    is the rest.
    """
    if not src_manifest or not (src_manifest.get("content") or "").strip():
        return ""
    body = (src_manifest.get("content") or "")[:_SOURCE_MANIFEST_MAX_CHARS]
    return (
        _manifest_migration_contract(detected_stack, target_stack)
        + f"\n\nSOURCE MANIFEST — `{src_manifest.get('path', '')}`. This is "
          f"the build file of the project being migrated. It is the INPUT, "
          f"not a template to copy.\n"
          f"--- BEGIN SOURCE MANIFEST ---\n{body}\n--- END SOURCE MANIFEST ---"
    )


def _build_deterministic_tasks(
    envelopes: list, src_files: list, detected_stack: dict, target_stack: dict,
    build_tools: Optional[Dict[str, str]] = None,
) -> Tuple[list, list]:
    """Build the full, wave-grouped task list for EVERY uploaded source
    file — not just the ones tied to a discovered REST endpoint — using
    the Context Manager's already-resolved API→Service→Repository→DB
    envelopes wherever a file matches one, and a generic layer heuristic
    otherwise. Returns (tasks, waves).

    iter-15.49 — When `build_tools` is provided (the `{component: tool}`
    map the operator confirmed in step 1: maven/gradle/npm/…), the
    planner ALSO emits a synthetic wave-1 "GENERATE {manifest}" task
    per component/tool pair when the target manifest isn't already
    produced by an existing source task. Guarantees the Coder sees an
    explicit instruction to write `pom.xml` / `build.gradle` /
    `package.json` / `pyproject.toml` / `go.mod` / `*.csproj` so the
    downstream compiler agent has a manifest to invoke."""
    # Map each file mentioned by an envelope to that envelope (inbound
    # envelopes take priority over outbound REST-CLIENT envelopes if a
    # file is somehow referenced by both).
    file_to_envelope: Dict[str, dict] = {}
    for env in envelopes:
        is_out = bool(env.get("is_outbound_client"))
        for f in env.get("files_affected") or []:
            existing = file_to_envelope.get(f)
            if existing is None or (existing.get("is_outbound_client") and not is_out):
                file_to_envelope[f] = env

    same_lang = bool(detected_stack.get("runtime")) and bool(target_stack.get("runtime")) and (
        _lang_family(detected_stack.get("runtime")) == _lang_family(target_stack.get("runtime"))
    )
    target_backend_name = target_stack.get("backend") or target_stack.get("runtime") or "the target stack"

    tasks = []
    seq = 0
    for f in src_files:
        path = f.get("path", "")
        if not path:
            continue
        seq += 1

        env = file_to_envelope.get(path)
        # Path-based classification takes priority — an envelope's single
        # "layer" describes the ENDPOINT's layer, not every file in its
        # files_affected list (a controller envelope's files_affected also
        # includes the service/repository files it traces through, which
        # must NOT all be lumped into the controller wave). Only fall back
        # to the envelope's layer when the path heuristic is inconclusive.
        classified = _classify_file_layer(path)
        layer = classified if classified != "other" else (
            env.get("layer") if env and env.get("layer") in _LAYER_WAVE_META else "other"
        )
        if layer not in _LAYER_WAVE_META:
            layer = "other"
        wave, wave_name = _LAYER_WAVE_META[layer]

        if env and env.get("action"):
            action = env["action"]
        elif layer == "model" and same_lang:
            action = "NO_CHANGE"
        elif layer == "test":
            action = "NO_CHANGE"
        elif layer == "integration":
            action = "REWRITE"
        elif layer == "other":
            ext = path[path.rfind("."):].lower() if "." in path else ""
            action = "TRANSFORM" if ext in _CODE_EXTENSIONS else "NO_CHANGE"
        else:
            action = "TRANSFORM"

        target_path = _transform_path(path, target_stack)
        envelope_id = env.get("envelope_id", "") if env else ""
        endpoint_bits = ""
        if env and env.get("endpoint_path"):
            endpoint_bits = f" ({env.get('endpoint_method', '')} {env.get('endpoint_path', '')})"

        title = f"{action.title()} {path.rsplit('/', 1)[-1]}{endpoint_bits}"
        description = (
            env.get("business_logic_summary")
            if env and env.get("business_logic_summary")
            else f"{layer.title()}-layer file — migrate to {json.dumps(target_stack)}."
        )
        if action == "NO_CHANGE":
            notes = "NO_CHANGE — this file is unaffected by the stack migration; carry it over unmodified."
        else:
            notes = (
                f"Apply idiomatic {target_backend_name} conventions for the {layer} layer "
                f"(routing / dependency-injection / serialization as appropriate) while "
                f"preserving the existing business logic exactly. Cross-check the linked "
                f"envelope's service/repository trace (if any) for consistency with callers."
            )
            # iter-20 — when the SOURCE project ships its own build manifest,
            # the synthetic GENERATE task for that path is skipped
            # (`existing_target_paths`) and the manifest is migrated through
            # this ordinary per-file path instead. That is the path the
            # operator actually hit: the Coder was handed the Helidon pom
            # and told to "transform" it, with nothing telling it that
            # framework dependencies are to be REMOVED rather than
            # translated. Generic per-layer advice is not enough for a
            # dependency list, so it gets the same explicit contract the
            # synthetic task carries.
            _base = path.rsplit("/", 1)[-1].lower()
            if _base in _SOURCE_MANIFEST_BASENAMES or _base.endswith(".csproj"):
                notes += _manifest_migration_contract(detected_stack, target_stack)

        tasks.append({
            "task_id": f"TASK-{seq:04d}",
            "envelope_id": envelope_id,
            "title": title,
            "description": description,
            "phase": "scaffold" if layer == "config" else "logic",
            "layer": layer,
            "action": action,
            "wave": wave,
            "wave_name": wave_name,
            "source_path": path,
            "target_path": target_path if target_path != path else "",
            "depends_on": [],
            "notes": notes,
        })

    # iter-15.49 — Synthesize a wave-1 "GENERATE {manifest}" task per
    # (component, build_tool) so the plan explicitly carries pom.xml /
    # build.gradle / package.json / pyproject.toml / go.mod / *.csproj
    # for the Coder to write. Skips components whose expected target
    # manifest is already covered by an existing source-file task
    # (e.g. same-tool same-language migrations).
    #
    # iter-15.53 — Root the manifest at the SOURCE PROJECT's top-level
    # directory (e.g. `hiring-service/pom.xml`), NOT at the abstract
    # component label (`backend/pom.xml`). We derive top-level dirs from
    # the source paths so the emitted plan matches the actual module
    # layout the user uploaded. If the source has multiple top-level
    # modules (multi-module repos), we emit one manifest per module.
    if build_tools:
        existing_target_paths = {t.get("target_path") or t.get("source_path") for t in tasks}
        # iter-20 — index the SOURCE build manifests by the module dir they
        # sit in, so a synthetic manifest task can be handed the file it is
        # migrating FROM (see `_source_manifest_for`).
        source_manifests: Dict[str, Dict[str, str]] = {}
        for f in src_files or []:
            p = str(f.get("path") or f.get("original_path") or "").strip().lstrip("./")
            base = p.rsplit("/", 1)[-1].lower()
            if base in _SOURCE_MANIFEST_BASENAMES or base.endswith(".csproj"):
                mod = p.rsplit("/", 1)[0] if "/" in p else ""
                source_manifests.setdefault(mod, {"path": p, "content": f.get("content") or ""})
        # Collect unique top-level dirs from source paths. These are the
        # module roots (e.g. "hiring-service", "user-service") we want
        # the manifest to sit at.
        #
        # iter-15.68 — GUARD against reserved/generic legacy folder names.
        # Some legacy uploads have their own top-level dir literally named
        # `src` (or `source`/`app`/`lib`) — that is an artifact of how the
        # LEGACY code was organised, not a real module boundary. If we
        # naively root the manifest there we get `src/pom.xml`, while the
        # Coder always emits generated Java/etc. at the STANDARD
        # build-tool-relative layout (`src/main/java/...`) measured from
        # the TRUE repo root — NOT nested under the legacy `src/` dir.
        # Maven then resolves `basedir` from the POM's own location
        # (`<root>/src`) and looks for `<root>/src/src/main/java`, which
        # doesn't exist → zero classes compiled → empty JAR / "cannot
        # find main class", no matter how correct the generated code is.
        # Filter these generic names out before treating anything as a
        # "module root"; only genuine multi-module dir names survive.
        _RESERVED_ROOT_NAMES = {
            "src", "source", "sources", "app", "lib", "libs",
            "main", "test", "tests", "public", "dist", "build",
        }
        source_module_roots: List[str] = []
        _seen: Set[str] = set()
        for f in src_files or []:
            p = str(f.get("path") or f.get("original_path") or "").strip().lstrip("./")
            if not p or "/" not in p:
                continue
            head = p.split("/", 1)[0]
            if head and head not in _seen and head.lower() not in _RESERVED_ROOT_NAMES:
                _seen.add(head)
                source_module_roots.append(head)
        # No dirs (flat upload, or every top-level dir was a reserved
        # generic name like `src`) → emit at repo root.
        if not source_module_roots:
            source_module_roots = [""]

        for component, tool in (build_tools or {}).items():
            tool_meta = (NATIVE_BUILD_COMMANDS or {}).get((tool or "").strip().lower())
            if not tool_meta:
                continue
            manifest = tool_meta.get("manifest") or ""
            if not manifest:
                continue

            # iter-15.53 — Attribute this (component, tool) to actual
            # source top-level dir(s):
            #   1. If a source dir literally matches the component name,
            #      use it (multi-module repos where components === dirs).
            #   2. Else if the source has exactly ONE top-level dir,
            #      use it (monolithic case — `hiring-service/pom.xml`).
            #   3. Else emit at every top-level dir (multi-module
            #      fallback when component labels are abstract).
            #   4. If no dirs at all → emit at repo root.
            comp_key = (component or "").strip("/ ").lower()
            # Component key "root" is a documented convention meaning
            # "no wrapping module — put manifest at repo root".
            if comp_key == "root":
                target_dirs = [""]
            else:
                matching_dirs = [d for d in source_module_roots if d and d.lower() == comp_key]
                if matching_dirs:
                    target_dirs = matching_dirs
                elif source_module_roots and source_module_roots[0]:
                    # Monolithic OR multi-module: use every real top-level
                    # dir (single-dir case is the common `hiring-service`
                    # scenario the user reported).
                    target_dirs = source_module_roots
                else:
                    target_dirs = [""]

            for module_root in target_dirs:
                # `*.csproj` is a glob; materialise it as `{module}.csproj`.
                if "*" in manifest:
                    stem = (module_root or component or "app").rsplit("/", 1)[-1] or "app"
                    manifest_file = manifest.replace("*", stem)
                else:
                    manifest_file = manifest
                target_path = f"{module_root}/{manifest_file}" if module_root else manifest_file
                if target_path in existing_target_paths:
                    continue
                existing_target_paths.add(target_path)
                seq += 1
                wave, wave_name = _LAYER_WAVE_META.get("config", (1, "Wave 1 — Build & Config"))
                src_manifest = _source_manifest_for(source_manifests, module_root)
                tasks.append({
                    "task_id": f"TASK-{seq:04d}",
                    "envelope_id": "",
                    "title": f"Generate {target_path} ({tool})",
                    "description": (
                        f"Author the {tool.upper()} build manifest for the "
                        f"`{module_root or 'root'}` module of the target project. "
                        f"Include the dependencies required by the target stack "
                        f"({json.dumps(target_stack)}), the standard plugins for "
                        f"`{tool_meta.get('label') or tool}`, and matching module / "
                        f"artifact identifiers derived from the source project."
                    ),
                    "phase": "scaffold",
                    "layer": "config",
                    "action": "GENERATE",
                    "wave": wave,
                    "wave_name": wave_name,
                    "source_path": "",
                    "target_path": target_path,
                    "depends_on": [],
                    "notes": (
                        f"BUILD MANIFEST — required by the user's step-1 choice "
                        f"of `{tool}` for the `{component}` component of "
                        f"`{module_root or 'root'}`. Produce a syntactically "
                        f"valid {manifest_file}. For maven: full <project> with "
                        f"groupId/artifactId/version and Spring Boot / target-stack "
                        f"dependencies. For gradle: plugins block, dependencies "
                        f"block, and JVM toolchain. For npm/yarn/pnpm: `name`, "
                        f"`version`, `scripts.build`, and target-stack "
                        f"`dependencies` / `devDependencies`. For "
                        f"pyproject.toml/requirements.txt: pinned versions of "
                        f"the target-stack packages. The downstream Compiler agent "
                        f"will invoke `{tool_meta.get('label') or tool}` against "
                        f"this file, so it must be usable as-is."
                        + _source_manifest_notes(src_manifest, detected_stack, target_stack)
                    ),
                    "synthetic": True,
                    "build_tool": tool,
                    "component": component,
                    "module_root": module_root or "root",
                    # iter-20 — the manifest the Coder is migrating FROM.
                    # `_process_single_task` passes this as `source_content`
                    # for a synthetic GENERATE task, so the model can see
                    # which third-party dependencies must carry across.
                    "source_manifest_path": (src_manifest or {}).get("path", ""),
                    "source_manifest_content": (src_manifest or {}).get("content", ""),
                })

    tasks.sort(key=lambda t: (t["wave"], t["task_id"]))
    waves_meta = sorted({(t["wave"], t["wave_name"]) for t in tasks})
    waves = [{"wave": w, "name": n} for w, n in waves_meta]
    return tasks, waves


@router.get("/transformer")
async def list_transformations(project_id: Optional[str] = None):
    """List transformations.

    iter-16.x — Optional `project_id` filter so the Transformer page can
    cheaply hydrate "the latest run for this project" on mount when the
    per-project localStorage `lastId` is unavailable (e.g. InPrivate /
    incognito window, cleared browser data, cross-device access). Without
    this, the UI silently rendered the empty "Upload source" state even
    though a completed run with envelopes/tasks/files existed in Mongo.
    """
    query: Dict[str, Any] = {}
    if project_id:
        query["project_id"] = project_id
    cursor = transformations.find(query, {"result": 0}).sort("created_at", -1)
    items = await cursor.to_list(100)
    for item in items:
        item["id"] = str(item.pop("_id"))
    return {"items": items}


@router.get("/transformer/{transform_id}")
async def get_transformation(transform_id: str):
    """Get a specific transformation with results."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    transform["id"] = str(transform.pop("_id"))
    return transform


@router.get("/transformer/{transform_id}/files")
async def get_transformation_files(transform_id: str, file_type: str = "transformed"):
    """Get files from a transformation."""
    cursor = transform_files.find(
        {"transform_id": transform_id, "type": file_type},
        {"content": 0}  # Exclude content for listing
    )
    files = await cursor.to_list(1000)
    for f in files:
        f["id"] = str(f.pop("_id"))
    return {"files": files}


@router.get("/transformer/{transform_id}/files/{file_id}")
async def get_transformation_file(transform_id: str, file_id: str):
    """Get a specific file content."""
    f = await transform_files.find_one({"_id": ObjectId(file_id), "transform_id": transform_id})
    if not f:
        raise HTTPException(404, "File not found")
    f["id"] = str(f.pop("_id"))
    return f


def _build_readiness_gate(transform: Dict[str, Any]) -> str:
    """Empty string when this transformation's output may leave the system.

    Otherwise a message explaining what is still wrong, for a 409.

    iter-20 — the operator's instruction was explicit: the download button
    does not appear until the code builds properly. Export was previously
    ungated, so a red build downloaded exactly like a green one and a
    "Spring Boot" tree that Maven could not resolve reached their disk.

    Two conditions, matching the ones the pipeline already computes for
    `final_status`:
      * the native build compiles (`compile_green`)
      * the DevOps audit passes (`production_ready`)
    A job with no build tool configured has nothing to compile, so it is
    not blocked — we cannot assert a build is broken when no build was
    ever asked for.

    LAMA_ALLOW_UNVERIFIED_DOWNLOAD=1 lifts the gate. It is off by default
    and deliberately NOT surfaced in the UI: it exists so an
    environmental build failure (no JDK in the container, a blocked
    repository) cannot permanently strand a user's own code, not as a
    routine way around the gate.
    """
    if (os.environ.get("LAMA_ALLOW_UNVERIFIED_DOWNLOAD") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return ""

    status = (transform or {}).get("status") or ""
    if status in ("running", "pending", "awaiting_confirmation", "awaiting_task_confirmation"):
        return (
            f"This transformation is still {status.replace('_', ' ')}. The download "
            f"becomes available once the pipeline finishes and the build is green."
        )

    # Nothing to compile → nothing to gate on.
    if not ((transform or {}).get("build_tools") or {}):
        return ""

    if not (transform or {}).get("compile_green"):
        summary = (
            ((transform or {}).get("compilation_result") or {}).get("summary")
            or "the build did not compile"
        )
        return (
            f"The transformed code does not build yet, so it is not available "
            f"for download: {summary}. Run 'Rerun compile' — the fix loop "
            f"escalates through the Coder, the DevOps Expert and a full "
            f"regeneration before giving up."
        )

    audit = (transform or {}).get("dependency_audit") or {}
    if audit and not audit.get("production_ready"):
        criticals = [
            f.get("issue", "")
            for f in (audit.get("findings") or [])
            if str(f.get("severity", "")).lower() == "critical"
        ][:3]
        detail = ("; ".join(c for c in criticals if c)) or (
            audit.get("summary") or "unresolved dependency findings"
        )
        return (
            f"The build compiles but its dependencies are not production-ready, "
            f"so the code is not available for download: {detail}"
        )
    return ""


@router.get("/transformer/{transform_id}/download")
async def download_transformed_code(transform_id: str, scope: str = "code"):
    """Download transformed files as ZIP.

    iter-15.45 — Added a `scope` query param:
      * `code`  (default) → just the transformed source files
      * `tests`            → just the auto-generated business/api/
                             integration test suites
      * `all`              → transformed code + tests bundled together

    Filenames encode the scope so operators can tell downloads apart
    when they save several from the same run.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    scope = (scope or "code").strip().lower()
    if scope not in ("code", "tests", "all"):
        raise HTTPException(400, "scope must be one of: code, tests, all")

    # iter-20 — do not hand over code that does not build.
    #
    # Until now this endpoint checked only that the transformation existed
    # and the scope was valid: a RED build downloaded as cleanly as a green
    # one, which is how a broken Helidon->Spring Boot tree reached the
    # operator's disk in the first place. Per their instruction the
    # download does not exist until the build is ready.
    _gate = _build_readiness_gate(transform)
    if _gate:
        raise HTTPException(409, _gate)

    if scope == "code":
        types = ["transformed"]
    elif scope == "tests":
        types = ["test"]
    else:
        types = ["transformed", "test"]

    cursor = transform_files.find({"transform_id": transform_id, "type": {"$in": types}})
    files = await cursor.to_list(4000)

    if not files:
        raise HTTPException(
            404,
            f"No {'transformed' if scope == 'code' else scope} files found for this transformation",
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            # iter-15.62.7 — sanitize before export; catches legacy
            # records (pre iter-15.56/.57) or any other edge case that
            # slipped an LLM chat wrapper into the stored content.
            content = _sanitize_exported_file_content(f["path"], f.get("content") or "")
            zf.writestr(f["path"], content)

    zip_buffer.seek(0)
    suffix = {"code": "transformed", "tests": "tests", "all": "bundle"}[scope]
    filename = f"{transform['name']}_{suffix}.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.delete("/transformer/{transform_id}")
async def delete_transformation(transform_id: str):
    """Delete a transformation and its files."""
    result = await transformations.delete_one({"_id": transform_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Transformation not found")
    
    await transform_files.delete_many({"transform_id": transform_id})
    await _log_audit("delete", "transformation", transform_id, {})
    
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════════
# GitHub Integration for Tools
# ════════════════════════════════════════════════════════════════════════════

@router.post("/transformer/{transform_id}/push-github")
async def push_transformation_to_github(
    transform_id: str,
    repo: str = Form(...),
    branch: str = Form("main"),
    commit_message: str = Form("Transformed code from LAMA"),
    path_prefix: str = Form(""),
):
    """Push transformed files to GitHub repository."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    # iter-20 — same gate as the ZIP download. Pushing a red build to a
    # real repository is the more consequential of the two exports, so
    # gating the download and leaving this open would be the wrong half.
    _gate = _build_readiness_gate(transform)
    if _gate:
        raise HTTPException(409, _gate)

    # Get GitHub config
    gh_config = await github_configs.find_one({})
    if not gh_config or not gh_config.get("token"):
        raise HTTPException(400, "GitHub not configured. Set up in Console → GitHub Settings.")
    
    cursor = transform_files.find({"transform_id": transform_id, "type": "transformed"})
    files = await cursor.to_list(1000)
    
    if not files:
        raise HTTPException(404, "No transformed files to push")
    
    try:
        from github import Github, GithubException
        
        g = Github(gh_config["token"])
        repository = g.get_repo(repo)
        
        pushed_files = []
        for f in files:
            file_path = os.path.join(path_prefix, f["path"]) if path_prefix else f["path"]
            # iter-15.62.7 — same export-time sanitization as the ZIP
            # download path (see `_sanitize_exported_file_content`).
            content = _sanitize_exported_file_content(f["path"], f.get("content") or "")
            
            try:
                # Try to get existing file
                existing = repository.get_contents(file_path, ref=branch)
                repository.update_file(
                    file_path,
                    commit_message,
                    content,
                    existing.sha,
                    branch=branch,
                )
            except GithubException as e:
                if e.status == 404:
                    # File doesn't exist, create it
                    repository.create_file(
                        file_path,
                        commit_message,
                        content,
                        branch=branch,
                    )
                else:
                    raise
            
            pushed_files.append(file_path)
        
        await _log_audit("github_push", "transformation", transform_id, {
            "repo": repo,
            "branch": branch,
            "files": len(pushed_files),
        })
        
        return {
            "success": True,
            "repo": repo,
            "branch": branch,
            "files_pushed": len(pushed_files),
            "paths": pushed_files,
        }
        
    except Exception as e:
        raise HTTPException(500, f"GitHub push failed: {str(e)}")


# ════════════════════════════════════════════════════════════════════════════
# Multi-Agent Transformer Pipeline (iter-16)
# ════════════════════════════════════════════════════════════════════════════


async def _log_agent_run(
    transform_id: str, agent: str, phase: str,
    status: str = "running", task_id: str = "",
    input_summary: str = "", output_summary: str = "",
    score: float = None, details: dict = None,
    error: str = "", duration_ms: int = 0,
) -> str:
    """Persist one agent execution step for the timeline."""
    run_id = str(ObjectId())
    await transformer_agent_runs.insert_one({
        "_id": run_id,
        "transform_id": transform_id,
        "agent": agent,
        "phase": phase,
        "task_id": task_id,
        "status": status,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "score": score,
        "details": details or {},
        "error": error,
        "duration_ms": duration_ms,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return run_id


async def _update_agent_run(run_id: str, **fields):
    """Update an existing agent run record."""
    await transformer_agent_runs.update_one(
        {"_id": run_id},
        {"$set": fields},
    )


def _deterministic_envelopes_from_kb(kb: Dict[str, Any]) -> tuple:
    """Build a safety-net envelope list straight from the deterministic KB
    (kb/owl_extractor.py, via tools_kb_builder.build_tools_kb) — the same
    regex/AST-based route+table extraction the main 5-stage LAMA pipeline
    already relies on. Used to (a) ground the Context Manager LLM prompt in
    real facts instead of asking it to invent controller→service→DB
    structure from raw source, and (b) as a fallback when the LLM output is
    empty or degenerate — see iter-15.18 in _run_context_manager below.

    iter-15.20 — Two additional fixes on top of the original safety net:
      1. Rows the KB flagged as `is_outbound_client` (a REST-CLIENT
         interface this service calls OUT to, e.g. MicroProfile Rest
         Client / OpenFeign — see owl_extractor.JAXRS_CLIENT_MARKER_RE)
         are no longer emitted as ordinary "TRANSFORM" API envelopes.
         They get their own `action="INTEGRATE"` / `layer="integration"`
         envelope so the reviewer still sees them, but they never crowd
         out (or get mistaken for) the service's own real, inbound API
         surface — which is exactly what was happening before this fix.
      2. Best-effort `service_class`/`repository_class` inference: any
         CDI/DI-injected field in the SAME source file whose type name
         ends in Service/UseCase/Facade/Manager is treated as the
         envelope's service layer; one ending in Repository/Dao/Mapper
         is treated as the repository layer. This is a heuristic (no
         call-graph analysis), but it beats leaving the column blank for
         every single deterministic-only envelope.

    iter-15.26 — `tools_kb_builder.build_traceability_map` now resolves the
    FULL Controller→Service(-impl)→Repository(-impl)→DB trace itself
    (cross-file, via CDI-injection + implements-interface indexes), and
    resolves the request DTO for JAX-RS handlers too (previously only
    Spring's @RequestBody was recognized). This function's job is now just
    to reshape that already-enriched `api_to_db` row into the full
    API-to-DB envelope schema the reviewer needs — nested `request`/
    `response`/`service_layer`/`data_layer` sections, not a flat table —
    so "Discovered Architecture" actually shows payload + full trace
    instead of a flat, mostly-dashed row.
    """
    trace = kb.get("traceability") or {}
    api_to_db = trace.get("api_to_db") or []

    envelopes = []
    all_tables = set()
    inbound_seq = 0
    outbound_seq = 0
    for row in api_to_db:
        path = (row.get("api") or "").strip()
        if not path:
            continue
        tables = sorted(row.get("tables") or [])
        source_file = row.get("source_file") or ""
        is_outbound = bool(row.get("is_outbound_client"))
        method = (row.get("method") or ("ANY" if is_outbound else "GET")).upper()
        controller_class = row.get("class") or ""
        service_class = row.get("service_class") or ""
        service_interface = row.get("service_interface") or ""
        service_file = row.get("service_file") or ""
        repository_class = row.get("repository_class") or ""
        repository_file = row.get("repository_file") or ""
        table_trace = row.get("table_trace") or []
        request_body_class = row.get("request_body_class") or ""
        response_body_class = row.get("response_body_class") or ""
        framework = row.get("framework") or ""

        files_affected = sorted({f for f in (source_file, service_file, repository_file) if f})

        if is_outbound:
            outbound_seq += 1
            envelopes.append({
                "envelope_id": f"ENV-EXT-{outbound_seq:03d}",
                "endpoint_method": method,
                "endpoint_path": path,
                "controller_class": controller_class,
                "controller_file": source_file,
                "service_class": "",
                "service_file": "",
                "service_method": "",
                "business_logic_summary": (
                    "Outbound REST-CLIENT interface — this service CALLS this "
                    "endpoint on another service; it is not part of this "
                    "service's own exposed API surface."
                ),
                "repository_class": "",
                "repository_file": "",
                "db_tables": [],
                "db_operations": [],
                "external_calls": [{"type": "rest", "target": path}],
                "files_affected": [source_file] if source_file else [],
                "action": "INTEGRATE",
                "risk_level": "low",
                "layer": "integration",
                "is_outbound_client": True,
                "acceptance_criteria": [
                    f"This is an OUTBOUND call to {path} — verify the target service "
                    "endpoint still exists/matches contract after migration.",
                ],
                "request": {
                    "dto_class": request_body_class,
                    "framework": framework,
                },
                "response": {
                    "result_type": response_body_class or "unresolved",
                },
                "service_layer": {},
                "data_layer": {"table_trace": [], "db_tables": []},
                "nfr": {},
            })
            continue

        inbound_seq += 1
        all_tables.update(tables)
        db_operations = sorted({
            op for hop in table_trace for op in (["SELECT/UPDATE (see table_trace)"] if hop.get("tables") else [])
        })
        envelopes.append({
            "envelope_id": f"ENV-DET-{inbound_seq:03d}",
            "endpoint_method": method,
            "endpoint_path": path,
            "controller_class": controller_class,
            "controller_file": source_file,
            "service_class": service_class,
            "service_file": service_file,
            "service_method": "",
            "business_logic_summary": "Auto-detected by deterministic route scan (owl_extractor).",
            "repository_class": repository_class,
            "repository_file": repository_file,
            "db_tables": tables,
            "db_operations": db_operations,
            "external_calls": [],
            "files_affected": files_affected,
            "action": "TRANSFORM",
            "risk_level": "medium" if tables else "low",
            "layer": "controller",
            "is_outbound_client": False,
            "acceptance_criteria": _acceptance_criteria_for_envelope(
                method, path, controller_class, service_class, repository_class, tables,
            ),
            # ─── iter-15.26 — Full API-to-DB envelope schema (nested) ───
            # Mirrors doc/agents/context-manager.md's "API-TO-DB ENVELOPE
            # MODEL" shape as closely as static analysis can resolve it —
            # request/response payload + the full service→repository→DB
            # trace, not just a flat controller/service/repo/table row.
            "request": {
                "dto_class": request_body_class,
                "framework": framework,
                "note": "" if request_body_class else "Request DTO not statically resolved — inspect the handler method signature.",
            },
            "response": {
                "result_type": response_body_class or "",
                "wrapper": "javax.ws.rs.core.Response" if framework == "jaxrs" else ("ResponseEntity" if framework == "spring-mvc" else ""),
                "note": "" if response_body_class else "Response DTO not statically resolved (payload is wrapped inside a generic Response/ResponseEntity — inspect the method body to find the exact type).",
            },
            "service_layer": {
                "use_case_interface": service_interface,
                "service_impl": service_class,
                "service_file": service_file,
            },
            "data_layer": {
                "repository_class": repository_class,
                "repository_file": repository_file,
                "table_trace": table_trace,
                "db_tables": tables,
            },
            "nfr": {},
        })
    stats = {
        "total_endpoints": inbound_seq,
        "total_tables": len(all_tables),
        "total_outbound_clients": outbound_seq,
    }
    return envelopes, stats


def _acceptance_criteria_for_envelope(
    method: str, path: str, controller: str, service: str, repository: str, tables: List[str],
) -> List[str]:
    """iter-15.26 — Template-based, deterministic acceptance criteria (no
    LLM call) generated straight from the resolved trace, so every
    envelope has at least a baseline reviewer checklist even before any
    LLM enrichment runs / if the LLM step is skipped or fails."""
    criteria = [f"{method} {path} must be exposed by the migrated equivalent of {controller or 'this controller'}."]
    if service:
        criteria.append(f"Business logic currently in {service} must be preserved (or explicitly called out if intentionally changed).")
    if repository and tables:
        criteria.append(
            f"Data access via {repository} must continue to read/write: {', '.join(tables)}."
        )
    elif tables:
        criteria.append(f"Data changes must be persisted to: {', '.join(tables)}.")
    criteria.append("No undocumented side effects (triggers, async jobs, stored procedures) should be dropped silently during migration.")
    return criteria


async def _run_context_manager(
    transform_id: str, source_files: list, detected_stack: dict, model: str = None,
    preserved_edits: Optional[Dict[str, Dict[str, Any]]] = None,
) -> list:
    """Phase 1: Context Manager discovers API endpoints, services, tables.

    iter-15.26 — `preserved_edits` (keyed by `endpoint_path`) lets a
    "Regenerate" pass reuse everything freshly re-discovered while still
    honoring any manual corrections a human reviewer already saved via
    `PATCH /envelopes/{envelope_id}` for that same endpoint — see
    `regenerate_transformer_envelopes` below. This is what makes
    "give input and regenerate to recover the gap" actually persistent
    instead of getting wiped on every regenerate.
    """
    run_id = await _log_agent_run(
        transform_id, "context_manager", "discovery",
        input_summary=f"Analyzing {len(source_files)} source files",
    )
    t0 = datetime.now(timezone.utc)

    try:
        # iter-15.18 — Run the SAME deterministic owl_extractor-based KB
        # builder the main LAMA pipeline uses, BEFORE calling the LLM. This
        # grounds the Context Manager prompt in real routes/tables instead
        # of asking a (possibly small/local, e.g. Ollama llama3.1:8b) model
        # to invent controller→service→DB structure zero-shot from raw
        # source dumps — which is exactly what was producing generic
        # "INFRA" envelopes with 0 endpoints / 0 tables even for a real
        # codebase. Also used below as a safety-net fallback if the LLM
        # output is empty or degenerate.
        kb = build_tools_kb(source_files, [])
        det_envelopes, det_stats = _deterministic_envelopes_from_kb(kb)
        kb_digest = render_kb_for_prompt(kb, max_chars=20000)

        prompt_template = await _get_effective_prompt(transform_id, "context_manager")
        if not prompt_template:
            prompt_template = "You are a code analysis agent. Analyze the source code and produce API-to-DB envelopes as JSON."
        model = await _get_effective_model(transform_id, "context_manager", model)

        source_stack_label = json.dumps(detected_stack)
        file_listing = "\n".join(f"- {f.get('path', '')}" for f in source_files[:200])
        content_samples = []
        for f in source_files[:30]:
            content_samples.append(f"===== {f.get('path', '')} =====\n{f.get('content', '')[:1500]}")
        content_block = "\n\n".join(content_samples)[:30000]

        user_prompt = f"""Analyze this source codebase and produce API-to-DB envelopes.

===== DETECTED SOURCE STACK =====
{source_stack_label}

===== DETERMINISTIC EXTRACTION (ground truth — routes/tables found by static analysis) =====
{kb_digest or "(no routes/tables statically detected — rely on the source samples below)"}

===== FILE INVENTORY ({len(source_files)} files) =====
{file_listing}

===== SOURCE CODE SAMPLES =====
{content_block}

The DETERMINISTIC EXTRACTION section above lists REAL endpoints/tables found
by static analysis — treat it as ground truth and do not omit any of them.
Produce ONE envelope per INBOUND endpoint listed there, enriched with
business_logic_summary, risk_level and action. The OUTBOUND REST-CLIENT
CALLS section (if present) is NOT this service's own API — do not create
endpoint envelopes for them; instead reference them inside the
`external_calls` array of whichever inbound endpoint invokes them, or set
`action: "INTEGRATE"` / `layer: "integration"` on a dedicated envelope if
you want to surface one explicitly. Also add envelopes for infrastructure
components (build config, app config, filters, health checks, entry point)
found in the file inventory. Return ONLY valid JSON matching the output
schema."""

        llm_envelopes: List[Dict[str, Any]] = []
        stats: Dict[str, Any] = {}
        try:
            response = await fabric_call(
                messages=[
                    {"role": "system", "content": prompt_template},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                agent_key="tools.transformer.context_manager",
                project_id=await _project_id_for_transform(transform_id),
                temperature=0.1,
                max_tokens=16000,
                response_format={"type": "json_object"},
            )
            response_text = response.get("content", "") if isinstance(response, dict) else str(response)
            parsed = _extract_json_object(response_text) or {}
            llm_envelopes = [e for e in (parsed.get("envelopes") or []) if isinstance(e, dict)]
            stats = parsed.get("stats", {}) or {}
        except Exception as llm_exc:
            # iter-15.18 — Don't fail the whole phase just because the LLM
            # call errored (bad JSON, provider outage, timeout). We already
            # have a deterministic result; degrade to that instead of
            # aborting the pipeline.
            log.warning("transformer context_manager LLM call failed, falling back to deterministic KB: %s", llm_exc)

        # Safety net: if the LLM returned nothing usable, or every entry is
        # a generic INFRA placeholder with no real endpoint/table detail,
        # fall back to the deterministic envelopes so the UI never shows
        # "0 API endpoints / 0 DB tables" when the codebase demonstrably
        # has them. If the LLM DID find real endpoints, keep its (enriched)
        # output and only append deterministic routes it missed.
        llm_found_real = any(
            (e.get("endpoint_path") or "").strip()
            and (e.get("endpoint_method") or "").upper() != "INFRA"
            for e in llm_envelopes
        )
        if llm_found_real:
            llm_paths = {
                (e.get("endpoint_path") or "").strip()
                for e in llm_envelopes if (e.get("endpoint_path") or "").strip()
            }
            envelopes = llm_envelopes + [de for de in det_envelopes if de["endpoint_path"] not in llm_paths]
        else:
            envelopes = det_envelopes + [
                e for e in llm_envelopes if not (e.get("endpoint_path") or "").strip()
            ]

        if not stats.get("total_endpoints"):
            stats["total_endpoints"] = det_stats["total_endpoints"]
        if not stats.get("total_tables"):
            stats["total_tables"] = det_stats["total_tables"]
        stats.setdefault("total_outbound_clients", det_stats.get("total_outbound_clients", 0))
        stats.setdefault("total_files", len(source_files))

        # Persist envelopes
        now = datetime.now(timezone.utc).isoformat()
        preserved_edits = preserved_edits or {}
        for env in envelopes:
            if not isinstance(env, dict):
                continue
            endpoint_path = env.get("endpoint_path", "")
            edit = preserved_edits.get(endpoint_path)
            doc = {
                "_id": str(ObjectId()),
                "transform_id": transform_id,
                "envelope_id": env.get("envelope_id", f"ENV-{str(ObjectId())[:6]}"),
                "status": "draft",
                "endpoint_method": env.get("endpoint_method", ""),
                "endpoint_path": endpoint_path,
                "controller_class": env.get("controller_class", ""),
                "controller_file": env.get("controller_file", ""),
                "service_class": env.get("service_class", ""),
                "service_file": env.get("service_file", ""),
                "service_method": env.get("service_method", ""),
                "business_logic_summary": env.get("business_logic_summary", ""),
                "repository_class": env.get("repository_class", ""),
                "repository_file": env.get("repository_file", ""),
                "db_tables": env.get("db_tables", []),
                "db_operations": env.get("db_operations", []),
                "external_calls": env.get("external_calls", []),
                "files_affected": env.get("files_affected", []),
                "action": env.get("action", "TRANSFORM"),
                "risk_level": env.get("risk_level", "low"),
                "layer": env.get("layer", ""),
                "is_outbound_client": bool(env.get("is_outbound_client")),
                "acceptance_criteria": env.get("acceptance_criteria", []),
                # iter-15.26 — Nested API-to-DB envelope sections (request
                # payload, response wrapper, full service/repository trace)
                # per doc/agents/context-manager.md's envelope schema.
                "request": env.get("request", {}),
                "response": env.get("response", {}),
                "service_layer": env.get("service_layer", {}),
                "data_layer": env.get("data_layer", {}),
                "nfr": env.get("nfr", {}),
                "user_edited": False,
                "created_at": now,
                "updated_at": now,
            }
            if edit:
                # iter-15.26 — Reapply a human reviewer's saved manual
                # corrections for this SAME endpoint on top of the freshly
                # (re)discovered envelope, so "Regenerate" recovers gaps
                # for everything else without silently discarding input
                # the user already gave for this one.
                doc.update({k: v for k, v in edit.items() if k in ENVELOPE_EDITABLE_FIELDS})
                doc["user_edited"] = True
                doc["updated_at"] = now
            await transformer_envelopes.insert_one(doc)

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Discovered {len(envelopes)} envelopes, {stats.get('total_endpoints', '?')} endpoints, {stats.get('total_tables', '?')} tables",
            score=None,
            details={"stats": stats, "envelope_count": len(envelopes)},
            duration_ms=ms,
        )
        return envelopes

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        raise


async def _run_planner(transform_id: str, envelopes: list, detected_stack: dict, target_stack: dict, model: str = None,
                        src_files: Optional[list] = None,
                        build_tools: Optional[Dict[str, str]] = None) -> list:
    """Phase 2: Planner creates an ordered, wave-grouped task list.

    iter-15.31 — Rewritten to be a DETERMINISTIC, rule-based algorithm
    (mirrors doc/agents/planner.md's WAVE-ordering exactly: Build/Config →
    Models/Entities/DTOs → Repository → Service → Controller → Integration
    (REST clients) → Security/Filters → Exceptions → Tests/Support), not
    an LLM asked to invent the entire task list as one giant JSON blob.

    Why: for real projects (600+ files) the previous LLM-JSON approach
    routinely produced 0 tasks — either the model refused to enumerate
    every file, or the JSON was truncated/unparsable at that size. A
    generic project-agnostic planner has to guarantee SOME output no
    matter the source/target stack or file count; a fixed wave algorithm
    driven by the already-enriched Context Manager envelopes (which carry
    the real API→service→repository→DB trace) does that reliably, and
    covers every uploaded source file — not just the ones tied to a REST
    endpoint — exactly as requested: "based on the api to db or
    ui-api-db, it should divide and generate task for the coder agent."

    The Planner's prompt/model config (editable via the Agent Pipeline
    config UI) is still used for a best-effort, NON-BLOCKING note
    enrichment pass over the envelope-linked tasks only (small subset) —
    if that LLM call fails or is misconfigured, the deterministic task
    list is returned unchanged, so the pipeline never regresses to 0
    tasks again.
    """
    run_id = await _log_agent_run(
        transform_id, "planner", "planning",
        input_summary=f"Planning tasks for {len(envelopes)} envelopes / {len(src_files or [])} source files",
    )
    t0 = datetime.now(timezone.utc)

    try:
        tasks, waves = _build_deterministic_tasks(envelopes, src_files or [], detected_stack, target_stack, build_tools or {})

        # ── Best-effort LLM note enrichment (non-blocking) ──────────────
        # Only touches `notes`/`title` for envelope-linked tasks, and only
        # if it succeeds — any failure (bad JSON, provider error, timeout)
        # is swallowed so the deterministic plan above is always what
        # ships if enrichment doesn't pan out.
        try:
            prompt_template = await _get_effective_prompt(transform_id, "planner")
            model = await _get_effective_model(transform_id, "planner", model)
            linked = [t for t in tasks if t.get("envelope_id")][:40]
            if prompt_template and linked:
                digest = json.dumps([
                    {"task_id": t["task_id"], "layer": t["layer"], "action": t["action"],
                     "source_path": t["source_path"], "description": t["description"]}
                    for t in linked
                ], indent=2)[:20000]
                user_prompt = f"""Refine the migration notes for these already-planned tasks
(transforming from {json.dumps(detected_stack)} to {json.dumps(target_stack)}).
Return ONLY valid JSON: {{"task_notes": {{"<task_id>": "<one specific, actionable note for the Coder agent>"}}}}

===== TASKS =====
{digest}"""
                response = await fabric_call(
                    messages=[
                        {"role": "system", "content": prompt_template},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=model,
                    agent_key="tools.transformer.planner",
                    project_id=await _project_id_for_transform(transform_id),
                    temperature=0.1,
                    max_tokens=4000,
                    response_format={"type": "json_object"},
                )
                response_text = response.get("content", "") if isinstance(response, dict) else str(response)
                parsed = _extract_json_object(response_text) or {}
                task_notes = parsed.get("task_notes") or {}
                if isinstance(task_notes, dict):
                    by_id = {t["task_id"]: t for t in tasks}
                    for tid, note in task_notes.items():
                        if tid in by_id and isinstance(note, str) and note.strip():
                            by_id[tid]["notes"] = note.strip()
        except Exception as enrich_err:
            print(f"[transformer:{transform_id}] Planner note-enrichment skipped: {enrich_err}")

        # Persist tasks
        now = datetime.now(timezone.utc).isoformat()
        for task in tasks:
            await transformer_tasks.insert_one({
                "_id": str(ObjectId()),
                "transform_id": transform_id,
                "task_id": task.get("task_id", f"TASK-{str(ObjectId())[:6]}"),
                "envelope_id": task.get("envelope_id", ""),
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "phase": task.get("phase", "scaffold"),
                "layer": task.get("layer", ""),
                "action": task.get("action", "TRANSFORM"),
                "wave": task.get("wave", 1),
                "wave_name": task.get("wave_name", ""),
                "source_path": task.get("source_path", ""),
                "target_path": task.get("target_path", ""),
                "status": "PENDING",
                "assigned_to": "coder",
                "depends_on": task.get("depends_on", []),
                "confidence": 0.0,
                "verifier_score": 0.0,
                "verifier_checks": {},
                "rejection_count": 0,
                "notes": task.get("notes", ""),
                "error": "",
                "created_at": now,
                "updated_at": now,
            })

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Created {len(tasks)} tasks in {len(waves)} waves",
            details={"task_count": len(tasks), "waves": waves},
            duration_ms=ms,
        )
        return tasks

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        raise


_NARRATIVE_MARKERS_RE = re.compile(
    r"^(i've|i have|i'll|i will|sure[,!]|here's|here is|let me|based on|"
    r"the following|as requested|i've successfully|i have successfully|"
    r"i've updated|i have updated|i've created|i have created|"
    r"i've made|i have made|i've completed|i have completed|"
    r"summary:|## summary|to (transform|migrate|convert) this file)",
    re.IGNORECASE,
)


def _looks_like_code(text: str) -> bool:
    """iter-15.34 — Heuristic guard against Factory Droid returning an
    agentic narrative summary ("I've updated UserService.java to use
    Spring's @Service annotation...") instead of the literal transformed
    source file. Droid is an autonomous CLI agent — it edits files on
    disk in its own workspace and, unless the prompt happens to land just
    right, its final chat message often describes what it did rather than
    reproducing the file content. Persisting that narrative as "generated
    code" is strictly worse than falling back to a standard completion
    model, so `_run_coder` uses this to decide whether a Factory-routed
    response needs a one-time fabric-only retry."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    first_line = stripped.splitlines()[0].strip()
    if _NARRATIVE_MARKERS_RE.match(first_line):
        return False
    sample = stripped[:400]
    code_punct = sum(sample.count(ch) for ch in "{};()=<>[]:")
    if code_punct == 0 and len(sample.split()) > 15:
        return False
    return True


# iter-15.62.7 — shared LLM-output sanitizer, used both at generation time
# (`_run_coder`'s `_extract_code`) AND at export time (ZIP download /
# GitHub push). A user reported a downloaded pom.xml that would not load
# in IntelliJ; the root cause was a STALE `transform_files` record (from
# before iter-15.56/15.57 shipped) whose `content` still had the raw LLM
# chat wrapper baked in, e.g. "Here is the transformed code from Java 17
# to Java 21:\n\n```xml\n<?xml ...". `_run_coder` already strips this for
# newly-generated files, but nothing re-checked it at export time, so any
# legacy-contaminated record (or a future edge case the generation-time
# strip misses) would still ship an invalid file straight into the ZIP/
# GitHub push. This is deliberately idempotent — running it on already
# clean content is a no-op.
_LLM_PREAMBLE_RE = re.compile(
    r"^\s*(?:based on|here'?s|here is|below is|the following|"
    r"sure[,!.]?|certainly[,!.]?|okay[,!.]?|ok[,!.]?|"
    r"i(?:'ve| have| will|'ll| am)|as (?:requested|per)|"
    r"to (?:transform|migrate|convert)|this is|note that|"
    r"following (?:is|are))[^\n]*\n+",
    re.IGNORECASE,
)


def _strip_llm_code_wrapper(raw: str) -> str:
    """Strip a fenced code block and/or leading chat-preamble prose from
    a raw LLM completion, returning just the literal source content."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    fences = re.findall(r"```[\w+\-]*\s*\n?(.*?)```", raw, re.DOTALL)
    if fences:
        raw = max(fences, key=len).strip()
    else:
        for _ in range(6):
            m = _LLM_PREAMBLE_RE.match(raw)
            if not m:
                break
            raw = raw[m.end():].lstrip()
    raw = re.sub(r"^```[\w+\-]*\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    return raw.strip()


# iter-15.69 — Some LLM providers (and copy/paste round-trips through
# rich-text editors) substitute "smart"/typographic Unicode punctuation
# for the plain-ASCII characters Java's grammar actually requires:
# curly quotes (U+2018/2019/201C/201D/...) instead of `'`/`"`, an en/em
# dash instead of `-`, a stray BOM/zero-width space/non-breaking space.
# Inside a char/string literal these are not "a fancier character" —
# they're a DIFFERENT TOKEN javac's lexer doesn't recognize as the
# literal delimiter at all, producing exactly "illegal character" /
# "unclosed character literal" cascades. Normalising them is a safe,
# almost pure win: it fixes the (common) broken case and is at worst
# cosmetic in a comment/javadoc.
_SMART_PUNCT_MAP: Dict[str, str] = {
    "\u2018": "'", "\u2019": "'", "\u201A": "'", "\u2032": "'", "\u02BC": "'",
    "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "-",
    "\u00A0": " ", "\u200B": "", "\uFEFF": "",
}
_CODE_FILE_EXTS = {
    "java", "kt", "scala", "groovy", "cs", "cpp", "cc", "c", "h", "hpp",
    "go", "rs", "php", "rb", "swift", "py", "js", "jsx", "ts", "tsx",
}
_STRAY_FENCE_LINE_RE = re.compile(r"(?m)^[ \t]*```[\w+\-]*[ \t]*$\n?")

# iter-16.x — Aggressive belt-and-braces fence scrubber. Runs on Coder
# output BEFORE it is persisted to `transform_files` so a fence marker
# cannot survive into Mongo, the compile scratch dir, the ZIP export,
# or a GitHub push — even if the LLM emits fences in a shape the
# `_strip_llm_code_wrapper` heuristic misses (nested javadoc examples
# using ``` fences, a truncated response with only an opening fence,
# double-fenced dual-file responses whose longest-block pick is still
# a `...```\n```lang\n` tail, etc.). Purely additive on top of the
# existing sanitizer — safe to apply repeatedly.
_LEADING_FENCE_RE = re.compile(r"^\s*```[\w+\-]*[ \t]*\n?")
_TRAILING_FENCE_RE = re.compile(r"\n?```[ \t]*\s*$")


def _scrub_code_fences(content: str) -> str:
    """Remove leading + trailing + stray full-line ``` fences from a
    code file. Idempotent; safe to call on already-clean content.
    Used by `_run_coder` and `_run_one` (compile-fix loop) as the final
    step before `transform_files.content` is written to Mongo."""
    if not content or "```" not in content:
        return content
    content = _LEADING_FENCE_RE.sub("", content, count=1)
    content = _TRAILING_FENCE_RE.sub("", content, count=1)
    content = _STRAY_FENCE_LINE_RE.sub("", content)
    return content


def _normalize_smart_punctuation(content: str) -> str:
    """Replace typographic Unicode punctuation with the plain-ASCII
    equivalent javac's lexer actually accepts. No-op if none present."""
    if not content:
        return content
    if not any(bad in content for bad in _SMART_PUNCT_MAP):
        return content
    for bad, good in _SMART_PUNCT_MAP.items():
        if bad in content:
            content = content.replace(bad, good)
    return content


def _sanitize_exported_file_content(path: str, content: str) -> str:
    """Defense-in-depth pass applied right before a transformed file
    leaves LAMA (ZIP download / GitHub push) AND before it's written to
    the compile scratch dir. Skips markdown/doc files (where a literal
    fenced block or "Here is ..." lead-in may be intentional prose) and
    is a no-op on content that's already clean — only touches files
    that actually look contaminated.

    iter-15.69 — Previously only content STARTING with a fence/preamble
    was touched, missing an unpaired fence marker left mid-file or at
    the very end (a truncated/partial LLM response), and never
    normalised the smart-quote/dash corruption that produces "illegal
    character"/"unclosed character literal" javac errors even in
    otherwise fence-clean files. Both are now handled for recognised
    code-file extensions."""
    if not content:
        return content
    lower_path = (path or "").lower()
    if lower_path.endswith((".md", ".markdown", ".txt", ".rst")):
        return content
    ext = lower_path.rsplit(".", 1)[-1] if "." in lower_path else ""
    is_code = ext in _CODE_FILE_EXTS
    stripped = content.lstrip()
    # iter-16.x — Only run the *destructive* `_strip_llm_code_wrapper`
    # extraction when the content actually LOOKS like a chat-wrapped
    # response (starts with a fence or a preamble). The previous
    # "is_code and ``` in content" branch also entered this path for
    # files whose ``` was just a nested Javadoc/docstring example
    # example — `_strip_llm_code_wrapper`'s non-greedy fence regex
    # then truncated the file to whatever sat between the inner fence
    # pair, wiping the real code. For that "code file with a stray
    # mid-file fence" case, just scrub the stray fence lines instead
    # (handled unconditionally further down).
    needs_extract = (
        stripped.startswith("```")
        or bool(_LLM_PREAMBLE_RE.match(stripped))
    )
    if needs_extract:
        cleaned = _strip_llm_code_wrapper(content) or content
        if is_code:
            # An unpaired trailing/mid-file fence marker survives
            # `_strip_llm_code_wrapper` (it only fully handles a matched
            # ```...``` pair or a LEADING preamble) — strip any
            # remaining stray fence line outright rather than let
            # literal backticks reach the compiler.
            cleaned = _STRAY_FENCE_LINE_RE.sub("", cleaned)
        content = cleaned
    # iter-16.x — Even for code files that did NOT trip the
    # `needs_extract` branch (e.g. the fence sits mid-file inside a
    # javadoc example, or the file only picked up an isolated trailing
    # ```\n from a truncated LLM response), a stray full-line fence
    # marker is NEVER valid Java/Kotlin/etc. syntax. Kill any that
    # remain unconditionally — the regex is scoped to full-line matches
    # so it is safe on legitimately-clean content (no-op).
    if is_code:
        content = _STRAY_FENCE_LINE_RE.sub("", content)
        content = _normalize_smart_punctuation(content)
    return content


async def _run_coder(
    transform_id: str, task_doc: dict, source_content: str, kb_ctx: str,
    detected_stack: dict, target_stack: dict, model: str = None,
    envelope: Optional[dict] = None, agent_name: str = "coder",
) -> str:
    """Phase 3: Coder transforms one file using the 3-pass approach.

    iter-15.62.6 — `agent_name` lets the compile-fix loop swap in the
    "devops_expert" persona/prompt for THIS call when the default Coder
    has already tried and failed to fix a recurring build failure (see
    `_run_compile_fix_loop`'s escalation logic). Defaults to "coder" so
    every other call site is unaffected."""
    task_id = task_doc.get("task_id", "")
    run_id = await _log_agent_run(
        transform_id, agent_name, "coding", task_id=task_id,
        input_summary=f"Transforming {task_doc.get('source_path', '')}",
    )
    t0 = datetime.now(timezone.utc)

    try:
        prompt_template = await _get_effective_prompt(transform_id, agent_name)
        if not prompt_template:
            prompt_template = "You are a code transformation engine. Transform code preserving business logic."
        model = await _get_effective_model(transform_id, agent_name, model)

        source_label = json.dumps(detected_stack)
        target_label = json.dumps(target_stack)
        task_notes = task_doc.get("notes", "")
        task_description = task_doc.get("description", "")

        # iter-15.34 — ARCHITECTURE CONTEXT. Previously the Coder only saw
        # the generic project-wide KB summary (entity/route/table counts),
        # never the specific Context Manager envelope for THIS file (API
        # contract request/response shape, controller→service→repository
        # →table trace). Without it, the Coder had no grounding for how
        # this file's layer fits into the wider call chain it must
        # preserve. Mirrors the same envelope schema shown in the
        # Transformer UI's envelope detail modal (iter-15.30).
        architecture_block = "No architecture envelope is linked to this file (generic file-layer migration)."
        if envelope:
            req = envelope.get("request") or {}
            resp = envelope.get("response") or {}
            svc = envelope.get("service_layer") or {}
            data = envelope.get("data_layer") or {}
            table_trace = data.get("table_trace") or []
            trace_lines = "\n".join(
                f"  - {hop.get('from', '')} → {hop.get('to', '')} ({hop.get('via', hop.get('operation', ''))})"
                if isinstance(hop, dict) else f"  - {hop}"
                for hop in table_trace[:15]
            ) or "  (no hop-by-hop trace resolved)"
            architecture_block = f"""Endpoint: {envelope.get('endpoint_method', '')} {envelope.get('endpoint_path', '')}
Controller: {envelope.get('controller_class', '-')}
Service: {envelope.get('service_class', svc.get('service_impl', '-'))}
Repository: {envelope.get('repository_class', data.get('repository_class', '-'))}
DB Tables: {', '.join(envelope.get('db_tables') or data.get('db_tables') or []) or '-'}
Business logic: {envelope.get('business_logic_summary', '-')}

Request contract: {req.get('dto_class') or '(not statically resolved)'} (framework: {req.get('framework', '-')})
Response contract: {resp.get('result_type') or '(not statically resolved)'} (wrapper: {resp.get('wrapper', '-')})
API → DB trace:
{trace_lines}"""

        # iter-20 — the source file used to be cut at a flat 10 000 chars,
        # SILENTLY, under a prompt that says "Preserve ALL business logic".
        # A 40 KB service class lost three quarters of its body and the
        # model had no way to know: it saw a complete-looking file ending
        # mid-method and produced a complete-looking migration of it.
        #
        # The cap was sized for 8k-16k context models. gpt-5.1 has 400 K and
        # gpt-4.1 1 M, so on a cloud provider it is pure loss. Local engines
        # still need a small one — `_is_local_call` in the fabric forces
        # num_ctx from the prompt size, and a 60 KB prompt to a 32 K model
        # is a guaranteed timeout.
        _src_budget, _kb_budget = await _coder_context_budget()
        # iter-20 — the target stack's own conventions, stated rather than
        # inferred. Empty for a target we have no playbook for, which
        # degrades to the previous stack-agnostic behaviour.
        _pb = _playbook_for(target_stack)
        _playbook_block = f"\n{_pb}\n" if _pb else ""
        _source_shown = source_content[:_src_budget]
        _truncation_warning = ""
        if len(source_content) > _src_budget:
            _truncation_warning = (
                f"\n!!! TRUNCATED — this file is {len(source_content)} characters and "
                f"you are seeing only the first {_src_budget}. You are NOT looking at "
                f"the whole file. Migrate faithfully what IS shown, do not invent the "
                f"remainder, and do not emit a closing brace or trailing structure that "
                f"implies the file ended where the excerpt does. Add a "
                f"`// FLAG: truncated source — remainder not seen` comment so the "
                f"Verifier can route this file for a second pass."
            )

        user_prompt = f"""Transform this file from {source_label} to {target_label}.

===== TASK =====
ID: {task_id}
Action: {task_doc.get('action', 'TRANSFORM')}
Layer: {task_doc.get('layer', '')}
Phase: {task_doc.get('phase', 'scaffold')}
Description: {task_description}
Notes: {task_notes}

===== ARCHITECTURE CONTEXT (this file's role in the API → Service → Repository → DB chain) =====
{architecture_block}

{_playbook_block}
===== PROJECT KNOWLEDGE BASE =====
{kb_ctx[:_kb_budget]}

===== SOURCE FILE: {task_doc.get('source_path', '')} =====
```
{_source_shown}
```
{_truncation_warning}

Apply the 3-pass transformation (Scaffold → Logic → Harden) in a single output.
Preserve ALL business logic, API paths, DB table/column names. Honour the
ARCHITECTURE CONTEXT above — keep this file consistent with the callers/
callees it traces to.
Return ONLY the raw transformed source code. Do NOT wrap it in ```-fences.
Do NOT open with prose (no "Based on ...", "Here is ...", "Sure, ...",
"I've ...", etc.). The very first character MUST be a valid source token
for the target language (e.g. `package`, `import`, `#`, `using`, `<?xml`)."""

        messages = [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": user_prompt},
        ]
        response = await fabric_call(
            messages=messages,
            model=model,
            agent_key=f"tools.transformer.{agent_name}",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=12000,
        )

        def _extract_code(resp) -> str:
            """iter-15.56 — Strip narrative preambles and markdown fences.

            Observed failure: models frequently open with "Based on the source
            file and migration requirements, here's the transformed Spring
            Boot 3 code:\n```java\n<code>\n```" and the old regex only
            trimmed the very first token, leaving the prose line persisted
            at the top of the generated Java file. This unwraps a fenced
            block wherever it appears, then strips any residual leading
            preamble lines when no fence is present.

            iter-15.62.7 — delegates the actual stripping to the shared
            `_strip_llm_code_wrapper` so the exact same logic protects
            files at export time too (see `_sanitize_exported_file_content`).

            iter-16.x — After the primary strip, run `_scrub_code_fences`
            unconditionally. Real-world Coder outputs on Java files were
            still shipping a literal ```java opening marker into the
            persisted file when the LLM emitted a nested javadoc code
            example (`` * Note: use switch like:\n *  ```\n *  ... ``) —
            the non-greedy `_strip_llm_code_wrapper` regex truncates at
            the first inner ``` pair and the outer opener/closer end up
            back in the "cleaned" content. `_scrub_code_fences` is a
            no-op on clean content and idempotent, so this second pass
            is safe and closes that leak."""
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            stripped = _strip_llm_code_wrapper(raw)
            return _scrub_code_fences(stripped)

        transformed = _extract_code(response)

        # iter-15.34 — if Factory Droid handled this call (model id is
        # prefixed "factory/...") but the response reads like an agentic
        # narrative rather than literal code, retry ONCE via the standard
        # fabric chain so we never persist prose as "generated code".
        used_factory = isinstance(response, dict) and str(response.get("model", "")).startswith("factory")
        retried_via_fabric = False
        if used_factory and not _looks_like_code(transformed):
            print(f"[transformer:{transform_id}] Factory Droid returned a non-code-looking response for "
                  f"{task_doc.get('source_path', '')} — retrying via standard fabric providers.")
            response = await fabric_call(
                messages=messages,
                model=model,
                agent_key=f"tools.transformer.{agent_name}",
                project_id=await _project_id_for_transform(transform_id),
                temperature=0.1,
                max_tokens=12000,
                skip_factory=True,
            )
            transformed = _extract_code(response)
            retried_via_fabric = True

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=(
                f"Transformed {task_doc.get('source_path', '')} ({len(transformed)} chars)"
                + (" [Factory narrative detected — retried via fabric]" if retried_via_fabric else "")
            ),
            duration_ms=ms,
        )
        return transformed

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        raise


# ═══════════════════════════════════════════════════════════════════
# iter-15.57 — Deterministic structural gate for the Verifier
# ═══════════════════════════════════════════════════════════════════
#
# The LLM verifier alone was passing files that could not possibly
# compile (prose preamble bleeding into the source, unbalanced braces,
# lingering source-stack imports/annotations, fence residue). This gate
# runs BEFORE the LLM call and HARD-OVERRIDES the verdict to REJECT
# when any critical structural check fails. It is stack-agnostic — the
# only lookups it does are the source stack's own "signature tokens"
# from `source_stack` to detect stale remnants.

# Signature tokens for common source stacks — anything on this list
# appearing in the target file after transformation is a strong signal
# the Coder left something behind. Stack-name → list of substrings.
#
# iter-20 — the Java/Jakarta half of this table was missing entirely:
# helidon, jaxrs, ejb, oracle and jquery are the source side of FIVE of
# the six transformations `SUPPORTED_TRANSFORMATIONS` advertises, and
# none of them had an entry. A Helidon → Spring Boot migration therefore
# had NOTHING to check against, which is how a "Spring Boot" service
# shipped with `io.helidon` imports and Helidon coordinates in its pom.
#
# Tokens are PACKAGE-level wherever possible (`io.helidon`,
# `oracle.jdbc`) rather than bare product names, so a comment or a
# javadoc mentioning the word "helidon" does not trip the gate — only
# real code and real dependency coordinates do.
_SOURCE_STACK_SIGNATURES: Dict[str, List[str]] = {
    "codeigniter": ["CI_Controller", "CI_Model", "$this->load->", "$this->db->", "CodeIgniter\\"],
    "codeigniter4": ["\\CodeIgniter\\", "BaseController", "$this->db->"],
    "struts":      ["extends ActionSupport", "org.apache.struts", "struts-config.xml"],
    "struts2":     ["org.apache.struts2", "extends ActionSupport"],
    # iter-20 — `struts-to-springmvc` declares its sources as
    # ["struts", "struts-1"], and only "struts" had an entry. Struts 1's
    # vocabulary is entirely different from Struts 2's (Action/ActionForm
    # rather than ActionSupport), so it needs its own row.
    "struts-1":    ["org.apache.struts.action", "extends Action", "ActionForm",
                    "ActionMapping", "ActionForward", "struts-config.xml"],
    "spring-mvc-legacy": ["org.springframework.web.servlet.ModelAndView"],
    "jsp":         ["<%@", "<jsp:", "<%=", "<%!"],
    "classic-asp": ["<%@", "Response.Write", "Server.CreateObject"],
    "django":      ["from django", "django.http", "django.db"],
    "flask":       ["from flask import", "Flask(__name__)"],
    "php":         ["<?php", "->query(", "mysqli_"],
    "dotnet-framework": ["System.Web.Mvc", "HttpContext.Current"],
    # ── iter-20: Java / Jakarta EE source stacks ──────────────────────
    "helidon":     ["io.helidon", "helidon-microprofile", "helidon-config",
                    "helidon-webserver", "org.eclipse.microprofile",
                    "io.helidon.config", "@ConfigProperty"],
    "helidon-mp":  ["io.helidon", "helidon-microprofile", "org.eclipse.microprofile"],
    "helidon-se":  ["io.helidon", "helidon-webserver", "io.helidon.webserver"],
    "jaxrs":       ["javax.ws.rs", "jakarta.ws.rs", "@ApplicationPath",
                    "@Produces(", "@Consumes(", "Response.ok(",
                    "javax.json", "jakarta.json"],
    "ejb":         ["javax.ejb", "jakarta.ejb", "@Stateless", "@Stateful",
                    "@MessageDriven", "@LocalBean", "@EJB"],
    "ejb-3":       ["javax.ejb", "jakarta.ejb", "@Stateless", "@Stateful", "@EJB"],
    "jquery":      ["$.ajax", "jQuery(", "$(document)", "$(this)", ".appendTo("],
    # ── iter-20: databases ────────────────────────────────────────────
    # Oracle-isms that are invalid or non-portable on PostgreSQL. These
    # fire on DDL/DML files and on JDBC wiring alike.
    # Oracle-only constructs. Deliberately NOT included, because
    # PostgreSQL supports them too and flagging them would reject
    # correct output: `to_date()`, `%TYPE`, `%ROWTYPE` and `END LOOP;`
    # are all valid PL/pgSQL.
    "oracle":      ["oracle.jdbc", "OracleDriver", "jdbc:oracle:", "VARCHAR2",
                    "NVARCHAR2", "NVL(", "SYSDATE", "ROWNUM", "CONNECT BY",
                    "FROM DUAL", "NUMBER(", "DBMS_"],
    "plsql":       ["DBMS_OUTPUT", "EXECUTE IMMEDIATE", "PRAGMA ",
                    "VARCHAR2", "SYSDATE"],
}

# iter-20 — Target-side tokens, subtracted from the residue set before
# anything is flagged.
#
# Necessary because the signature lists overlap across stacks: a
# `jakarta.ws.rs` import is RESIDUE when migrating Helidon → Spring Boot
# and entirely CORRECT when the target is Quarkus or Helidon itself.
# Without this subtraction, arming the residue gate (which had never
# actually run — see `_structural_check`) would have produced a wave of
# false REJECTs on legitimate output.
_TARGET_STACK_SIGNATURES: Dict[str, List[str]] = {
    "quarkus":       ["jakarta.ws.rs", "javax.ws.rs", "@Produces(", "@Consumes(",
                      "@ApplicationPath", "jakarta.json", "@ConfigProperty",
                      "org.eclipse.microprofile"],
    "helidon":       ["io.helidon", "helidon-microprofile", "helidon-config",
                      "helidon-webserver", "org.eclipse.microprofile",
                      "jakarta.ws.rs", "javax.ws.rs", "@ConfigProperty"],
    "micronaut":     ["jakarta.ws.rs", "@Produces(", "@Consumes("],
    "jakarta-ee":    ["jakarta.ws.rs", "jakarta.ejb", "@Stateless",
                      "@Produces(", "@Consumes(", "jakarta.json"],
    "oracle":        ["oracle.jdbc", "VARCHAR2", "NVL(", "SYSDATE", "NUMBER(",
                      "FROM DUAL", "ROWNUM", "TO_DATE("],
    "react-18":      ["$(document)", "$.ajax"],  # a jQuery interop shim may legitimately remain
}


# Comment syntaxes per file kind, used to blank comments out before the
# residue scan. See `_strip_comments_for_scan`.
_LINE_COMMENT_MARKERS: Dict[str, Tuple[str, ...]] = {
    "c_style": ("//",),
    "hash":    ("#",),
    "sql":     ("--",),
}
_BLOCK_COMMENT_SPANS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "c_style": (("/*", "*/"),),
    "sql":     (("/*", "*/"),),
    "xml":     (("<!--", "-->"),),
    "hash":    (),
}

_EXT_COMMENT_STYLE: Dict[str, str] = {
    "java": "c_style", "js": "c_style", "jsx": "c_style", "ts": "c_style",
    "tsx": "c_style", "cs": "c_style", "go": "c_style", "kt": "c_style",
    "scala": "c_style", "groovy": "c_style", "gradle": "c_style",
    "c": "c_style", "cpp": "c_style", "h": "c_style", "hpp": "c_style",
    "swift": "c_style", "rs": "c_style", "php": "c_style", "json": "c_style",
    "sql": "sql", "ddl": "sql",
    "py": "hash", "yml": "hash", "yaml": "hash", "sh": "hash",
    "properties": "hash", "toml": "hash", "cfg": "hash", "ini": "hash",
    "xml": "xml", "html": "xml", "htm": "xml", "pom": "xml", "csproj": "xml",
}


def _strip_comments_for_scan(text: str, source_path: str) -> str:
    """Blank out comments so the residue scan reads CODE, not prose.

    iter-20 — necessary, not cosmetic. The Coder prompt explicitly asks
    for `// MIGRATION:` notes "where a non-obvious change was made", so a
    correctly-migrated file routinely contains a comment naming the
    construct it replaced ("// was: EXECUTE IMMEDIATE, now JPA"). Scanning
    raw text would reject exactly the files that documented themselves
    best, and would also flag a commented-out dependency in a pom — which
    is not a build problem at all.

    Comments are replaced with spaces rather than deleted so that offsets,
    and therefore line/column arithmetic anywhere downstream, are
    unchanged. Quote-aware, so `"http://x"` and `'--'` are not mistaken
    for comment openers.

    Best-effort by design: this feeds a heuristic gate, and an unparseable
    file simply gets scanned as-is rather than raising.
    """
    ext = (source_path.rsplit(".", 1)[-1] or "").lower() if "." in source_path else ""
    base = source_path.rsplit("/", 1)[-1].lower()
    if base in ("pom.xml", "build.gradle", "build.gradle.kts"):
        style = "xml" if base == "pom.xml" else "c_style"
    else:
        style = _EXT_COMMENT_STYLE.get(ext, "")
    if not style:
        return text

    line_markers = _LINE_COMMENT_MARKERS.get(style, ())
    block_spans = _BLOCK_COMMENT_SPANS.get(style, ())
    # XML has no line comments and its strings do not use shell quoting,
    # so the quote tracking below would misfire on an apostrophe in prose.
    quote_chars = "" if style == "xml" else "\"'`"

    out: List[str] = []
    i, n = 0, len(text)
    quote: str = ""
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:          # escaped char inside a literal
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in quote_chars:
            quote = ch
            out.append(ch)
            i += 1
            continue
        matched = False
        for open_tok, close_tok in block_spans:
            if text.startswith(open_tok, i):
                end = text.find(close_tok, i + len(open_tok))
                end = n if end == -1 else end + len(close_tok)
                # Preserve newlines so line numbering survives.
                out.append("".join(
                    c if c == "\n" else " " for c in text[i:end]
                ))
                i = end
                matched = True
                break
        if matched:
            continue
        for marker in line_markers:
            if text.startswith(marker, i):
                end = text.find("\n", i)
                end = n if end == -1 else end
                out.append(" " * (end - i))
                i = end
                matched = True
                break
        if matched:
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _residue_tokens_for(source_stack: dict, target_stack) -> List[str]:
    """Source-stack tokens that must NOT appear in the transformed file.

    iter-20 — resolves the residue set from two independent signals and
    unions them, because either one alone misses real cases:

    1. The DETECTED framework/language on the job document. This is what
       the original code used, via a `key in src_name` substring test —
       fine when `tech_detector` returns a clean "helidon", useless when
       it returns "helidon-mp 4.0" or "Java/JAX-RS".
    2. The DECLARED transformation pair. `SUPPORTED_TRANSFORMATIONS`
       already carries an explicit `source` list per target
       (helidon-to-springboot → ["helidon", "helidon-mp", "helidon-se",
       "jaxrs"]). The operator chose that pair, so it is a statement of
       intent and more reliable than sniffing.

    Whatever the target stack legitimately uses is then SUBTRACTED, so a
    token that is residue for one migration and correct for another (the
    `jakarta.ws.rs` case) only fires where it is genuinely wrong.
    """
    src_name = str(
        (source_stack or {}).get("framework")
        or (source_stack or {}).get("language")
        or ""
    ).lower()

    # Normalise the target into a set of lowercase stack ids. It arrives
    # as a plain string on v1 jobs and as a {component: target} dict on
    # v2 jobs — `_transform_path` defends against the same split.
    target_ids: Set[str] = set()
    if isinstance(target_stack, dict):
        for v in target_stack.values():
            if v:
                target_ids.add(str(v).lower())
    elif target_stack:
        target_ids.add(str(target_stack).lower())

    tokens: Set[str] = set()

    # (1) detected framework/language substring match — as before.
    for key, sigs in _SOURCE_STACK_SIGNATURES.items():
        if key in src_name:
            tokens.update(sigs)

    # (2) declared transformation pair.
    for pair_id, spec in SUPPORTED_TRANSFORMATIONS.items():
        pair_target = str(spec.get("target") or "").lower()
        # The pair is in play when the operator selected it by id, or
        # when any selected target matches this pair's target stack.
        if pair_id.lower() in target_ids or (pair_target and pair_target in target_ids):
            for src_key in spec.get("source") or []:
                tokens.update(_SOURCE_STACK_SIGNATURES.get(str(src_key).lower(), []))

    if not tokens:
        return []

    # Subtract anything the chosen target legitimately uses.
    allowed: Set[str] = set()
    for tid in target_ids:
        for key, sigs in _TARGET_STACK_SIGNATURES.items():
            if key in tid:
                allowed.update(sigs)

    return sorted(tokens - allowed)

# Fenced/preamble residue markers that the coder sanitizer *should*
# have removed. Presence in the persisted file = sanitizer bug OR the
# coder returned malformed output.
_PROSE_RESIDUE_MARKERS_RE = re.compile(
    r"^\s*(?:```|based on\b|here'?s\b|here is\b|sure[,!.]|certainly[,!.]|"
    r"i(?:'ve| have|'ll| will| am)\b|as (?:requested|per)\b|"
    r"the following\b|note that\b)",
    re.IGNORECASE,
)


def _structural_check(
    transformed: str, source_path: str, detected_stack: dict, target_stack: dict,
) -> Dict[str, Any]:
    """Cheap deterministic gate. Returns {ok, severity, issues[]}."""
    issues: List[str] = []
    text = transformed or ""
    stripped = text.strip()

    if not stripped:
        return {"ok": False, "severity": "critical",
                "issues": ["Transformed file is empty."]}

    # 1. Prose / preamble bleed-in (iter-15.56 sanitizer failure).
    first_nonblank = next((ln for ln in stripped.splitlines() if ln.strip()), "")
    if _PROSE_RESIDUE_MARKERS_RE.match(first_nonblank):
        issues.append(
            f"First non-blank line looks like prose/markdown, not code: "
            f"{first_nonblank[:80]!r}"
        )
    if "```" in stripped:
        issues.append("Markdown fence (```) present in file body.")

    # 2. Balanced brackets — skip for languages that don't use them
    # (yaml, python, ruby, xml, sql, ini, md, txt).
    ext = (source_path.rsplit(".", 1)[-1] or "").lower()
    bracket_langs = {"java", "js", "ts", "tsx", "jsx", "cs", "cpp", "cc", "c",
                     "h", "hpp", "kt", "go", "rs", "php", "swift", "scala", "groovy"}
    if ext in bracket_langs:
        for open_ch, close_ch, label in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
            opens = stripped.count(open_ch)
            closes = stripped.count(close_ch)
            if opens != closes:
                issues.append(f"Unbalanced {label}: {opens} '{open_ch}' vs {closes} '{close_ch}'.")

    # 2b. iter-15.69 — Typographic/"smart" Unicode quotes or dashes
    # anywhere in a code file are a strong signal the content went
    # through a rich-text/LLM auto-correct pass. Inside a char/string
    # literal these are a DIFFERENT token than the ASCII delimiter the
    # grammar requires — this is exactly the "illegal character" /
    # "unclosed character literal" class of compile failure reported
    # live, and it's cheaper to reject it here (before it ever reaches
    # the compiler) than to diagnose it after a failed build.
    if ext in bracket_langs and any(ch in stripped for ch in _SMART_PUNCT_MAP):
        issues.append(
            "Typographic/smart-quote or dash character present in code body "
            "(e.g. \u2018 \u2019 \u201c \u201d \u2013 \u2014) — javac/the "
            "target compiler requires plain ASCII quotes/apostrophes/hyphens."
        )

    # 3. Stub-only content (LLM returned a placeholder).
    stub_markers = ["TODO: implement", "// TODO", "// Not implemented", "raise NotImplementedError"]
    if len(stripped) < 60 and any(m in stripped for m in stub_markers):
        issues.append("File appears to be a stub placeholder.")

    # 4. Source-stack signature residue.
    #
    # iter-20 — this rule had never actually executed. Its caller looked
    # the job up by `{"transform_id": ...}` while the collection is keyed
    # on `_id`, so `detected_stack` arrived as `{}` on every call and the
    # loop below iterated over nothing. Fixed at the call site; the
    # residue set now also resolves from the declared transformation pair
    # and subtracts legitimate target tokens (`_residue_tokens_for`).
    #
    # Reported ALL offending tokens rather than the first: a pom carrying
    # four Helidon coordinates should tell the fix loop about four, not
    # send it round the loop once per dependency.
    #
    # Scanned with comments blanked out — the Coder prompt asks for
    # `// MIGRATION:` notes naming what was replaced, and those must not
    # read as residue.
    scannable = _strip_comments_for_scan(stripped, source_path)
    found = [sig for sig in _residue_tokens_for(detected_stack, target_stack) if sig in scannable]
    if found:
        shown = ", ".join(repr(s) for s in found[:6])
        more = f" (+{len(found) - 6} more)" if len(found) > 6 else ""
        issues.append(
            f"Source-stack remnant found ({shown}{more}) — the transformed file "
            f"still references the framework it was migrated away from."
        )

    # 5. Java-specific: `package` / `import` / class presence when ext=java.
    if ext == "java":
        if "class " not in stripped and "interface " not in stripped and "enum " not in stripped and "record " not in stripped:
            issues.append("Java file has no class/interface/enum/record declaration.")
        # No trailing prose after the closing brace.
        after_last_brace = stripped.rsplit("}", 1)[-1].strip()
        if after_last_brace and not after_last_brace.startswith("//"):
            issues.append(f"Trailing prose after final closing brace: {after_last_brace[:60]!r}")

    if not issues:
        return {"ok": True, "severity": "ok", "issues": []}

    # Critical: prose bleed / fence / empty / unbalanced / source remnants /
    # typographic-punctuation corruption.
    critical_markers = ("Unbalanced ", "empty", "prose", "fence", "remnant", "stub", "Trailing prose", "Typographic/smart-quote")
    severity = "critical" if any(any(m in i for m in critical_markers) for i in issues) else "warn"
    return {"ok": False, "severity": severity, "issues": issues}


# iter-16.x — Operator-requested acceptance gate: a transformed file is
# only ever treated as VERIFIED/compilable when the Verifier's numeric
# `confidence` score is >= 95%. Prior to this the code additionally
# accepted anything the LLM verdict-labelled "ACCEPT_WITH_NOTES"
# (defined in the verifier's own rubric as 85-94%), but per explicit
# request the bar is now a hard 95% regardless of what verdict string
# the LLM attaches — the confidence NUMBER is the single source of
# truth for pass/fail, the verdict/summary/issues remain purely
# narrative context for the operator.
VERIFIER_ACCEPT_THRESHOLD = 95.0


async def _run_verifier(transform_id: str, task_doc: dict, original: str, transformed: str, model: str = None) -> dict:
    """Phase 4: Verifier checks transformed code quality.

    iter-15.57 — Runs a deterministic structural gate BEFORE the LLM. If
    the file fails any critical structural check (empty, prose bleed-in,
    unbalanced braces, fence residue, source-stack imports), the verdict
    is HARD-FORCED to REJECT regardless of what the LLM says. This closes
    the loop where the LLM verifier was passing files that could not
    possibly compile.
    """
    task_id = task_doc.get("task_id", "")
    run_id = await _log_agent_run(
        transform_id, "verifier", "verification", task_id=task_id,
        input_summary=f"Verifying {task_doc.get('source_path', '')}",
    )
    t0 = datetime.now(timezone.utc)

    try:
        prompt_template = await _get_effective_prompt(transform_id, "verifier")
        if not prompt_template:
            prompt_template = "You are a code verification engine. Check transformed code for correctness."
        model = await _get_effective_model(transform_id, "verifier", model)

        user_prompt = f"""Verify this code transformation for correctness.

===== ORIGINAL ({task_doc.get('source_path', '')}) =====
```
{original[:6000]}
```

===== TRANSFORMED ({task_doc.get('target_path', task_doc.get('source_path', ''))}) =====
```
{transformed[:6000]}
```

Run all 9 verification checks and return the results as JSON."""

        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            agent_key="tools.transformer.verifier",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=8000,
            response_format={"type": "json_object"},
        )

        response_text = response.get("content", "") if isinstance(response, dict) else str(response)
        result = _extract_json_object(response_text) or {
            "confidence": 50, "verdict": "REJECT", "checks": [], "issues": [], "summary": "Parse error"
        }

        # iter-16.x — Same schema-drift guard as `_run_tester`: the LLM
        # occasionally nests an object (e.g. its own self-invented
        # {total_checks, passed, failed, overall_status} tally) into the
        # `summary` field instead of a plain string. The frontend's
        # Verification Console renders `summary` directly as JSX text, so
        # an unstringified object here crashes the whole page (React
        # error #31). Coerce it before it can reach the UI.
        _v_summary = result.get("summary")
        if _v_summary is not None and not isinstance(_v_summary, str):
            result["summary"] = json.dumps(_v_summary) if isinstance(_v_summary, (dict, list)) else str(_v_summary)
        if isinstance(result.get("issues"), list):
            for _issue in result["issues"]:
                if not isinstance(_issue, dict):
                    continue
                for _field in ("description", "fix", "check", "severity"):
                    _val = _issue.get(_field)
                    if _val is not None and not isinstance(_val, str):
                        _issue[_field] = json.dumps(_val) if isinstance(_val, (dict, list)) else str(_val)

        confidence = float(result.get("confidence", 0))
        verdict = result.get("verdict", "REJECT")

        # iter-15.57 — Deterministic structural gate. The LLM verifier
        # alone kept letting non-compilable files through (prose bleed-in,
        # unbalanced braces, source-stack remnants). Run our own cheap
        # check and HARD-OVERRIDE to REJECT when the file is obviously
        # broken, so `_run_verifier` can never say "ACCEPT" on garbage.
        try:
            # iter-20 — this lookup was querying `{"transform_id": ...}`
            # while `transformations` is keyed on `_id` (every other one
            # of the 20+ find_one calls in this file uses `_id`, and
            # `transform_id` is not a field on the document at all). It
            # therefore matched nothing and returned None on EVERY call,
            # so `_structural_check` received {} for both stacks and its
            # source-residue and target rules were dead for the entire
            # life of the gate. That is how Helidon imports reached a
            # "Spring Boot" output tree with an ACCEPT verdict attached.
            #
            # The projected field was wrong too: the job stores the
            # detected stack under `source_stack`, not `detected_stack`.
            tx_for_check = await transformations.find_one(
                {"_id": transform_id},
                {"source_stack": 1, "target_stack": 1, "transforms": 1},
            ) or {}
            structural = _structural_check(
                transformed,
                task_doc.get("source_path", "") or task_doc.get("target_path", ""),
                tx_for_check.get("source_stack") or {},
                # v2 jobs carry the per-component dict in `transforms`;
                # v1 jobs carry a single collapsed string in
                # `target_stack`. Prefer the richer one.
                tx_for_check.get("transforms") or tx_for_check.get("target_stack") or {},
            )
        except Exception as _sc_err:
            structural = {"ok": True, "severity": "ok", "issues": [], "check_error": str(_sc_err)}

        result["structural_check"] = structural
        if not structural["ok"]:
            existing_issues = list(result.get("issues") or [])
            for s_issue in structural["issues"]:
                existing_issues.append({
                    "check": "structural",
                    "severity": structural["severity"],
                    "description": s_issue,
                    "fix": "Rewrite the file so the sanity check passes.",
                })
            result["issues"] = existing_issues
            if structural["severity"] == "critical":
                verdict = "REJECT"
                confidence = min(confidence, 30.0)
                result["verdict"] = "REJECT"
                result["confidence"] = confidence
                result["summary"] = (
                    "STRUCTURAL FAIL — " + "; ".join(structural["issues"][:3])
                )

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Verdict: {verdict} ({confidence}%)" + (
                f" [structural:{structural['severity']}]" if not structural["ok"] else ""
            ),
            score=confidence,
            details=result,
            duration_ms=ms,
        )
        return result

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        return {"confidence": 0, "verdict": "REJECT", "checks": [], "issues": [{"description": str(e)}], "summary": f"Verification failed: {e}"}


# ═══════════════════════════════════════════════════════════════════
# iter-15.44 — Real subprocess-based compiler agent
# ═══════════════════════════════════════════════════════════════════
#
# The prior "Tester" agent (`_run_tester`) only asked an LLM whether the
# generated code *looked* like it would compile. That gave an
# `overall_score` and a static-analysis narrative but never actually
# proved anything — the operator's next question was invariably
# "does it *build*?". iter-15.44 answers that question by materialising
# the transformed tree into a scratch workspace and invoking the build
# tool the operator picked in step 1 (iter-15.40, `transformation.build_tools`).
#
# Design:
#   1. Native-first.  When the picked tool is in
#      `BUILD_TOOL_NATIVE_SUPPORT` AND the binary is on PATH inside the
#      LAMA runtime image, we run it directly as an async subprocess
#      (bounded by `LAMA_COMPILE_TIMEOUT_SEC`, default 300). This is the
#      common path for Maven / Gradle / npm / pip because the LAMA
#      Docker image ships those toolchains.
#   2. Manifest-aware.  For each `(component, tool)`, we scan the
#      workspace for the tool's manifest file (`pom.xml`, `package.json`,
#      etc.) and run the build in every directory that contains one.
#      Codegen output that ships a single monolithic build root sees a
#      single invocation; multi-module output sees one invocation per
#      module.
#   3. Toolchain-missing = clean skip.  When the binary isn't on PATH or
#      no manifest is found, the component is marked "skipped" with a
#      concrete reason rather than crashing the compile phase.
#   4. LLM static analysis still runs alongside as a NARRATIVE
#      supplement (`static_analysis` key on the result). It is no longer
#      the sole compilation signal.
#
# The compile is intentionally separate from `_run_tester` so operators
# can re-run just the compile without redoing the whole pipeline via
# `POST /transformer/{id}/compile`.


NATIVE_BUILD_COMMANDS: Dict[str, Dict[str, Any]] = {
    # Each entry:
    #   manifest  → filename that anchors a build root
    #   binary    → the executable checked against PATH
    #   argv      → argv to run (relative to the manifest dir)
    #   label     → user-visible step name
    # iter-15.6x — Maven now runs the REAL `mvn clean install` the operator
    # would run locally (no `-q`, no `-DskipTests`) so a green Tester result
    # actually means "this compiles and its tests pass", matching what the
    # user sees when they download the ZIP and build it themselves. Gradle
    # mirrors this with `clean build` (compile + test) instead of `assemble`
    # (compile-only, which was silently hiding test failures).
    "maven":  {"manifest": "pom.xml",       "binary": "mvn",    "argv": ["mvn", "-B", "-Dstyle.color=never", "clean", "install"],                        "label": "mvn clean install"},
    "gradle": {"manifest": "build.gradle",  "binary": "gradle", "argv": ["gradle", "--no-daemon", "clean", "build"],              "label": "gradle clean build"},
    "npm":    {"manifest": "package.json",  "binary": "npm",    "argv": ["npm", "run", "build", "--if-present"],                 "label": "npm run build"},
    "yarn":   {"manifest": "package.json",  "binary": "yarn",   "argv": ["yarn", "--silent", "build"],                            "label": "yarn build"},
    "pnpm":   {"manifest": "package.json",  "binary": "pnpm",   "argv": ["pnpm", "-s", "build"],                                  "label": "pnpm build"},
    "pip":    {"manifest": "requirements.txt", "binary": "python3", "argv": ["python3", "-m", "py_compile"], "label": "py_compile"},
    "poetry": {"manifest": "pyproject.toml","binary": "poetry", "argv": ["poetry", "build", "--no-interaction"],                  "label": "poetry build"},
    "dotnet": {"manifest": "*.csproj",      "binary": "dotnet", "argv": ["dotnet", "build", "--nologo", "-v", "q"],               "label": "dotnet build"},
    "go":     {"manifest": "go.mod",        "binary": "go",     "argv": ["go", "build", "./..."],                                 "label": "go build"},
}


def _binary_on_path(name: str) -> Optional[str]:
    """Return the resolved path to `name` on PATH, or None if missing.
    Kept in a helper so tests can monkeypatch it without wrestling
    with the real `shutil.which`."""
    import shutil
    return shutil.which(name)


def _write_transformed_workspace(transformed_files: List[Dict[str, Any]]) -> str:
    """Materialise `[{path, content}, ...]` into a fresh temp directory
    and return the workspace root. Any `..` segments are stripped for
    safety — the entire tree lives inside the returned root.
    """
    import tempfile
    root = tempfile.mkdtemp(prefix="lama-compile-")
    for f in transformed_files:
        rel = (f.get("path") or "").lstrip("/").replace("..", "")
        if not rel:
            continue
        target = os.path.join(root, rel)
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        # iter-15.68 — some Coder responses still leak a literal
        # ```lang ... ``` fence into the persisted `transform_files.content`
        # (observed on ~75% of files in one real transform despite
        # `_run_coder`'s own `_strip_llm_code_wrapper` pass — likely a
        # provider-specific formatting quirk not caught upstream). A
        # fenced `.java` file is not valid source and silently produces
        # a `class, interface, enum, or record expected` compile error
        # (or, worse, a totally empty compiled JAR once every file in a
        # dir fails). Apply the same defense-in-depth sanitizer used for
        # ZIP/GitHub export here too, so the REAL build never sees
        # markdown residue regardless of how it got into Mongo.
        content = _sanitize_exported_file_content(rel, f.get("content") or "")
        try:
            with open(target, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(content)
        except Exception:
            # Best-effort — a single unwritable file must not crash
            # the whole compile phase.
            continue
    return root


def _find_manifest_dirs(root: str, manifest_glob: str, limit: int = 8) -> List[str]:
    """Walk `root` and return directories whose *immediate* contents
    include a file matching `manifest_glob`. Capped at `limit` so a
    pathological codegen output can't spawn hundreds of subprocesses.
    Skips `node_modules`, `target`, `build`, `.git`, `dist`."""
    from fnmatch import fnmatch
    skip = {"node_modules", "target", "build", ".git", "dist", "__pycache__", "vendor"}
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if any(fnmatch(fn, manifest_glob) for fn in filenames):
            hits.append(dirpath)
            if len(hits) >= limit:
                break
    return hits


# iter-15.6x — Live console sink for the Tester's "Compile Console".
#
# The compile phase can legitimately run for 1-5 minutes (a real
# `mvn clean install` downloads dependencies + runs tests). Previously
# the FE only ever saw the FULL stdout/stderr tail once the whole
# subprocess had exited — during that window the operator had no idea
# whether the build was progressing or hung. `_ConsoleSink` streams
# real subprocess output line-by-line into `transformation.compile_console`
# (throttled to avoid hammering Mongo), which the FE polls via
# `/transformer/{id}/status` and renders as an actual scrolling terminal.
_COMPILE_CONSOLE_MAX_LINES = 400
_COMPILE_CONSOLE_FLUSH_SEC = 0.75
# iter-15.62.5 — Some Maven/Gradle/npm installs still emit ANSI color/
# cursor-control escape codes even in batch/non-interactive mode (seen
# as literal garbage like `[0m[0m` once rendered in a plain-text
# console), making the log harder to read. Strip them defensively for
# EVERY tool so the console + stdout/stderr tails saved for diagnosis
# are always plain, readable text.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(line: str) -> str:
    if not line:
        return line
    return _ANSI_ESCAPE_RE.sub("", line)


class _ConsoleSink:
    """Accumulates console lines for one compile run and flushes them
    to Mongo on a throttle so a chatty build doesn't spam writes."""

    def __init__(self, transform_id: Optional[str]):
        self.transform_id = transform_id
        self.lines: List[str] = []
        self._last_flush = 0.0
        self._lock = asyncio.Lock()

    async def write(self, text: str, force: bool = False):
        if not text:
            return
        self.lines.append(text)
        if len(self.lines) > _COMPILE_CONSOLE_MAX_LINES:
            self.lines = self.lines[-_COMPILE_CONSOLE_MAX_LINES:]
        now = asyncio.get_event_loop().time()
        if not force and (now - self._last_flush) < _COMPILE_CONSOLE_FLUSH_SEC:
            return
        self._last_flush = now
        if not self.transform_id:
            return
        async with self._lock:
            try:
                await transformations.update_one(
                    {"_id": self.transform_id},
                    {"$set": {
                        "compile_console": list(self.lines),
                        "compile_console_updated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
            except Exception:
                pass

    async def reset(self):
        self.lines = []
        if not self.transform_id:
            return
        try:
            await transformations.update_one(
                {"_id": self.transform_id},
                {"$set": {"compile_console": [], "compile_console_updated_at": None}},
            )
        except Exception:
            pass


async def _run_native_build(
    tool: str,
    cwd: str,
    timeout_sec: int,
    sink: Optional[_ConsoleSink] = None,
    component: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a single native build. Returns a component-result dict.
    Never raises — surface every failure mode as `status="failed"` +
    a reason so the caller can aggregate cleanly.

    When `sink` is provided, stdout/stderr are streamed into it
    line-by-line AS THE BUILD RUNS (see `_ConsoleSink`) so the FE can
    show a realistic, live-updating console instead of only the final
    tail once the process has already exited.
    """
    spec = NATIVE_BUILD_COMMANDS.get(tool)
    label_prefix = f"[{component}]" if component else ""
    if not spec:
        msg = f"{label_prefix} no native command mapping for tool={tool}".strip()
        if sink:
            await sink.write(f"$ {msg}", force=True)
        return {
            "tool": tool, "cwd": cwd, "status": "skipped",
            "reason": f"no native command mapping for tool={tool}",
        }
    if not _binary_on_path(spec["binary"]):
        # iter-15.6x — Every tool in `BUILD_TOOL_NATIVE_SUPPORT` now ships
        # a real toolchain in the runtime image (see Dockerfile). A missing
        # binary at this point is a LAMA infra bug, not a legitimate
        # "nothing to build here" skip — treat it as a hard FAILURE so it
        # can never masquerade as `compilation_ready: true` the way the
        # old skip-counts-as-pass logic allowed.
        reason = f"toolchain missing on PATH: {spec['binary']} (this is a LAMA runtime image bug — please report)"
        if sink:
            await sink.write(f"$ {spec['label']}  ({cwd})", force=True)
            await sink.write(f"ERROR: {reason}", force=True)
        return {
            "tool": tool, "cwd": cwd, "status": "failed",
            "reason": reason, "label": spec["label"],
        }

    if sink:
        await sink.write(f"$ {' '.join(spec['argv'])}   (cwd: {os.path.relpath(cwd) if os.path.isabs(cwd) else cwd})", force=True)

    t0 = datetime.now(timezone.utc)
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec["argv"],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buf: List[str] = []
        stderr_buf: List[str] = []

        async def _drain(stream, buf, tag):
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = _strip_ansi(raw.decode("utf-8", errors="replace").rstrip("\n"))
                buf.append(line)
                if sink:
                    await sink.write(line if tag == "stdout" else f"[stderr] {line}")

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _drain(proc.stdout, stdout_buf, "stdout"),
                    _drain(proc.stderr, stderr_buf, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            elapsed = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            if sink:
                await sink.write(f"TIMEOUT: build exceeded {timeout_sec}s cap — killed", force=True)
            return {
                "tool": tool, "cwd": cwd, "status": "timeout",
                "duration_ms": elapsed,
                "reason": f"Build exceeded {timeout_sec}s cap",
                "label": spec["label"],
                "stdout_tail": "\n".join(stdout_buf)[-8000:],
                "stderr_tail": "\n".join(stderr_buf)[-8000:],
                # iter-15.69 — see the non-timeout branch below for why
                # `_full` also needs to be captured here.
                "stdout_full": "\n".join(stdout_buf)[-200000:],
                "stderr_full": "\n".join(stderr_buf)[-200000:],
            }
        elapsed = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        stdout = "\n".join(stdout_buf)
        stderr = "\n".join(stderr_buf)
        exit_code = proc.returncode
        if sink:
            await sink.write(
                f"$ exit code {exit_code} — {'BUILD SUCCESS' if exit_code == 0 else 'BUILD FAILURE'} "
                f"({elapsed/1000:.1f}s)", force=True,
            )
        return {
            "tool": tool,
            "cwd": cwd,
            "label": spec["label"],
            "status": "passed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "duration_ms": elapsed,
            # Cap captured output — a chatty Maven run can easily emit
            # 10+ MB. Keep the tail because the interesting error is
            # usually at the end.
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-8000:],
            # iter-15.69 — A single genuinely corrupted source file (e.g.
            # illegal characters / unclosed literals baked into the
            # persisted content) makes javac cascade into dozens of
            # per-token diagnostics for THAT ONE file. When Maven also
            # touches other modules in the same invocation, those
            # cascading lines — including the actual `File.java:[line,col]`
            # diagnostics `_parse_compile_errors` keys off — can get
            # evicted entirely from the last-8000-char `_tail` window,
            # leaving nothing for the regex to match and forcing a
            # premature "unfixable" verdict. Keep a much larger window
            # (200KB — plenty even for a heavily cascading failure)
            # purely for diagnostic PARSING; `_tail` stays small for
            # storage/display/LLM-prompt token budget.
            "stdout_full": stdout[-200000:],
            "stderr_full": stderr[-200000:],
        }
    except Exception as e:
        if sink:
            await sink.write(f"ERROR: subprocess launch error: {e}", force=True)
        return {
            "tool": tool, "cwd": cwd, "status": "failed",
            "reason": f"subprocess launch error: {e}",
            "label": spec["label"],
        }


async def _run_compiler(
    transform_id: str,
    workspace_root: str,
    build_tools: Dict[str, str],
) -> Dict[str, Any]:
    """iter-15.44 — Real compile phase.

    `build_tools` is the persisted `{component: tool}` map from step 1.
    For each entry we resolve the tool spec, hunt for its manifest
    directories inside `workspace_root`, run the build in each, and
    aggregate the per-component results.

    Returns a dict shaped like:
      {
        "compilation_ready": bool,   # ALL components either passed or skipped
        "overall_score": int,        # % of components that actually passed
        "summary": str,
        "components": [ {component, tool, invocations: [ {…run result…} ] } ],
        "mode": "native",
      }
    """
    run_id = await _log_agent_run(
        transform_id, "tester", "compiling",
        input_summary=f"Native compile for {len(build_tools or {})} component(s)",
    )
    t0 = datetime.now(timezone.utc)

    # iter-15.6x — Reset + wire the live Compile Console for this run so
    # the FE's "Tester running" left panel / "Console" right panel can
    # show real-time output instead of only the tail once everything
    # has finished.
    sink = _ConsoleSink(transform_id)
    await sink.reset()
    await sink.write(
        f"Tester: compiling {len(build_tools or {})} component(s) — "
        f"{', '.join(f'{c}={t}' for c, t in (build_tools or {}).items()) or 'none configured'}",
        force=True,
    )

    try:
        try:
            timeout_sec = int(os.environ.get("LAMA_COMPILE_TIMEOUT_SEC", "300") or "300")
        except ValueError:
            timeout_sec = 300
        timeout_sec = max(30, min(timeout_sec, 3600))

        components: List[Dict[str, Any]] = []
        passed = failed = skipped = 0

        for component, tool in (build_tools or {}).items():
            spec = NATIVE_BUILD_COMMANDS.get(str(tool or "").lower())
            comp_row: Dict[str, Any] = {
                "component": component,
                "tool": tool,
                "invocations": [],
            }

            if spec is None:
                comp_row["status"] = "skipped"
                comp_row["reason"] = (
                    f"Tool '{tool}' is not in LAMA's native-support set — "
                    "when Factory Droid is enabled for the project the "
                    "compile is delegated to Droid Computer (see "
                    "`/transformer/{id}/compile-log` for Droid transcript)."
                )
                skipped += 1
                components.append(comp_row)
                await sink.write(f"[{component}] skipped — {comp_row['reason']}", force=True)
                continue

            dirs = _find_manifest_dirs(workspace_root, spec["manifest"])
            if not dirs:
                comp_row["status"] = "skipped"
                comp_row["reason"] = (
                    f"No `{spec['manifest']}` found under transformed tree "
                    "— either the Coder did not emit a build manifest or "
                    "the target stack does not require one."
                )
                skipped += 1
                components.append(comp_row)
                await sink.write(f"[{component}] skipped — {comp_row['reason']}", force=True)
                continue

            # Run each manifest dir in sequence (per component). Different
            # components DO run in parallel below via asyncio.gather.
            comp_passed = comp_failed = 0
            for d in dirs:
                inv = await _run_native_build(str(tool).lower(), d, timeout_sec, sink=sink, component=component)
                # Redact absolute workspace prefix so the operator sees
                # a repo-relative path in the UI.
                inv["cwd"] = os.path.relpath(inv.get("cwd", d), workspace_root) or "."
                comp_row["invocations"].append(inv)
                if inv.get("status") == "passed":
                    comp_passed += 1
                elif inv.get("status") in ("failed", "timeout"):
                    comp_failed += 1

            if comp_failed == 0 and comp_passed > 0:
                comp_row["status"] = "passed"
                passed += 1
            elif comp_passed == 0 and comp_failed == 0:
                comp_row["status"] = "skipped"
                skipped += 1
            else:
                comp_row["status"] = "failed"
                failed += 1

            components.append(comp_row)

        total_meaningful = passed + failed
        overall_score = int(round(100 * passed / max(total_meaningful, 1))) if total_meaningful else 0
        compilation_ready = failed == 0 and (passed > 0 or skipped > 0)

        summary_bits = []
        if passed:  summary_bits.append(f"{passed} passed")
        if failed:  summary_bits.append(f"{failed} failed")
        if skipped: summary_bits.append(f"{skipped} skipped")
        summary = " · ".join(summary_bits) or "No build components configured"

        result = {
            "compilation_ready": compilation_ready,
            "overall_score": overall_score,
            "summary": summary,
            "components": components,
            "mode": "native",
            "timeout_sec": timeout_sec,
            "workspace_root": workspace_root,
        }

        await sink.write(
            f"══ Tester result: {'COMPILATION READY ✔' if compilation_ready else 'COMPILATION FAILED ✘'} "
            f"— {summary} ({overall_score}%) ══",
            force=True,
        )

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Compile {summary} ({overall_score}%)",
            score=float(overall_score),
            details={k: v for k, v in result.items() if k != "workspace_root"},
            duration_ms=ms,
        )
        return result

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        with contextlib.suppress(Exception):
            await sink.write(f"ERROR: compile phase crashed: {e}", force=True)
        return {
            "compilation_ready": False,
            "overall_score": 0,
            "summary": f"Compile phase crashed: {e}",
            "components": [],
            "mode": "native",
        }


# ═══════════════════════════════════════════════════════════════════
# iter-15.58 — Compile → Planner → Coder auto-fix loop
# ═══════════════════════════════════════════════════════════════════
#
# When the operator hits "Rerun compile", if the native build fails,
# LAMA now:
#   1. Parses `stderr_tail` / `stdout_tail` from every failed
#      invocation and groups errors by source file.
#   2. Hands the failing-file list to the Planner, which creates
#      one FIX task per file (wave 99, `action="FIX"`, `notes`
#      containing the concrete compiler diagnostic).
#   3. For each fix task, calls Coder → Verifier → persists the
#      updated `transform_files.content`.
#   4. Re-runs `_run_compiler`. Loops until the build is green (or hits
#      an infra block / stagnation guard — see iter-15.62.4). No
#      hardcoded iteration cap by default; `LAMA_COMPILE_FIX_MAX_ITER`
#      is an opt-in cap for operators who want one.
#
# Progress is written to `transformation.compile_fix_progress` and
# streamed to the operator UI via `/transformer/{id}/status`.

# Common compiler diagnostic patterns.  The regex is intentionally
# permissive — every native tool logs "somefile.ext:LINE" somewhere
# in the failure tail. We capture the file path, line number, and
# ~200 chars of surrounding context.
#
# iter-15.68 — Maven/javac's actual diagnostic format is
# `File.java:[182,30] message` — a COLON *then* an OPEN BRACKET before
# the line number (not colon-OR-bracket as a single separator). The
# previous `[:\[(](\d+)` only consumed one separator char, so it never
# matched this — the single most common real-world compiler output
# format — meaning `_parse_compile_errors` silently returned zero
# diagnostics for essentially every real Maven build failure. Every
# such failure then fell through to the generic whole-log LLM
# diagnosis path, which (for large multi-file dumps) frequently gave
# up with "unfixable" after iteration 1 even though the underlying
# errors were ordinary, mechanically-fixable syntax problems. `+`
# allows one-or-more separator characters (`:`, `:[`, `(`) so both the
# old single-separator formats (`File.java:45`, `File.java(45,3)`) and
# Maven's `File.java:[182,30]` are captured.
_COMPILE_ERR_LINE_RE = re.compile(
    r"([A-Za-z0-9_./\\\-]+?\.(?:java|kt|scala|groovy|py|ts|tsx|js|jsx|cs|cpp|cc|c|h|hpp|go|rs|php|rb|swift|xml|json|yaml|yml|properties))"
    r"[:\[(]+(\d+)",
)

# iter-15.69 — A file whose PERSISTED content is corrupted (stray
# markdown-fence residue, LLM-truncated output, or non-ASCII "smart"
# quotes/dashes used inside a string/char literal) makes javac emit
# one of these unmistakable messages. Unlike an ordinary "cannot find
# symbol"/type error, a Coder asked to "fix ONLY the reported issue"
# on a file like this has nothing sane to patch — the fix is always
# "regenerate this file cleanly from scratch". Diagnostic lines
# matching this signature get `kind="corrupted_source"` tagged onto
# their error group so `_planner_fix_tasks_from_errors` routes them to
# the stronger full-rewrite instructions instead of the generic
# "fix ONLY the reported issues" patch framing.
_CORRUPTED_SOURCE_SIGNATURE_RE = re.compile(
    r"illegal character|unclosed (?:character|string) literal|"
    r"class,\s*interface,\s*enum,?\s*(?:or\s+record\s+)?expected|"
    r"reached end of file while parsing",
    re.IGNORECASE,
)


def _parse_compile_errors(
    compile_result: Dict[str, Any],
    workspace_root: str,
) -> List[Dict[str, Any]]:
    """Extract per-file diagnostics from a failing `_run_compiler` result.

    Returns a list of {path, lines: [(lineno, snippet)], tool, component}.
    Deduped by path; up to 8 diagnostic lines kept per file so the Coder
    prompt stays compact.
    """
    ws = os.path.abspath(workspace_root) if workspace_root else ""
    by_path: Dict[str, Dict[str, Any]] = {}
    for comp in compile_result.get("components") or []:
        component = comp.get("component") or ""
        tool = comp.get("tool") or ""
        for inv in comp.get("invocations") or []:
            if inv.get("status") not in ("failed", "timeout"):
                continue
            # iter-15.69 — parse against the FULL captured buffer (not the
            # last-8000-char `_tail`) so a cascading multi-error failure in
            # one file can't evict the very diagnostics we're looking for.
            # `_full` may be absent on older stored results — fall back to
            # `_tail` for back-compat.
            blob = (
                (inv.get("stderr_full") or inv.get("stderr_tail") or "") + "\n" +
                (inv.get("stdout_full") or inv.get("stdout_tail") or "")
            )
            if not blob.strip():
                continue
            inv_cwd_rel = inv.get("cwd") or ""
            for m in _COMPILE_ERR_LINE_RE.finditer(blob):
                raw_path = m.group(1)
                lineno = int(m.group(2))
                # Normalise to workspace-relative.
                candidate = raw_path.replace("\\", "/")
                if ws and candidate.startswith(ws):
                    candidate = os.path.relpath(candidate, ws)
                elif inv_cwd_rel and not candidate.startswith(inv_cwd_rel):
                    joined = os.path.join(inv_cwd_rel, candidate) if inv_cwd_rel != "." else candidate
                    candidate = joined.replace("\\", "/").lstrip("./")
                candidate = candidate.strip().lstrip("/").replace("//", "/")
                # Grab the diagnostic line + up to 2 following lines of context.
                start = max(0, m.start() - 40)
                end = min(len(blob), m.end() + 240)
                snippet = blob[start:end].strip().replace("\n", " ⏎ ")[:300]
                slot = by_path.setdefault(candidate, {
                    "path": candidate,
                    "component": component,
                    "tool": tool,
                    "lines": [],
                })
                if len(slot["lines"]) < 8 and (lineno, snippet) not in slot["lines"]:
                    slot["lines"].append((lineno, snippet))
                    if _CORRUPTED_SOURCE_SIGNATURE_RE.search(snippet):
                        slot["kind"] = "corrupted_source"
    return list(by_path.values())


# iter-15.62 — Some real build failures never surface a `file:line` the
# regex above can key off (Maven dependency-resolution failures, Gradle
# build-script errors, missing/broken plugin config, npm peer-dep
# conflicts, ...). Before this iteration those failures aborted the fix
# loop immediately as "unfixable" — even though they're often fixable by
# editing the build manifest itself. `_is_infra_blocked` distinguishes
# the ONE class of failure that genuinely can't be fixed by editing
# anything in the transformed tree — the toolchain itself missing from
# the LAMA runtime image (a bug in the image, not the generated code) —
# from every other failure, which now gets a shot at LLM-based Planner
# diagnosis (`_llm_diagnose_generic_failure`) instead of being given up
# on.
_INFRA_TOOLCHAIN_MISSING_RE = re.compile(r"toolchain missing on path", re.IGNORECASE)


def _is_infra_blocked(compile_result: Dict[str, Any]) -> Optional[str]:
    """Return a human-readable reason when EVERY failed invocation is a
    `toolchain missing on PATH` infra error, or None when at least one
    failure looks like a real, potentially-fixable code/config problem."""
    reasons: List[str] = []
    for comp in compile_result.get("components") or []:
        for inv in comp.get("invocations") or []:
            if inv.get("status") not in ("failed", "timeout"):
                continue
            reason = inv.get("reason") or ""
            if not _INFRA_TOOLCHAIN_MISSING_RE.search(reason):
                return None
            reasons.append(f"{comp.get('component')}: {reason}")
    return "; ".join(reasons) if reasons else None


def _compile_failure_signature(compile_result: Dict[str, Any]) -> Tuple:
    """iter-15.62.4 — Cheap, deterministic fingerprint of the CURRENT
    failure state (which components/tools failed and why), used by
    `_run_compile_fix_loop`'s stagnation guard: if this signature is
    identical two iterations in a row, the fix round in between made
    zero difference and looping further would just burn iterations."""
    sig = []
    for comp in compile_result.get("components") or []:
        for inv in comp.get("invocations") or []:
            if inv.get("status") not in ("failed", "timeout"):
                continue
            sig.append((comp.get("component"), comp.get("tool"), (inv.get("reason") or "")[:500]))
    return tuple(sorted(sig))


def _detect_installed_toolchain_versions() -> Dict[str, str]:
    """iter-15.62 — Best-effort detection of the ACTUAL runtime versions
    baked into this LAMA image (Java/Node/Python/.NET/Go), so the
    Planner's compile-fix diagnosis has a concrete number to target
    instead of guessing. This is what turns "release 21 not supported"
    from a dead-end into an actionable fix: downgrade the manifest's
    `maven.compiler.release` (or equivalent) to what's actually
    installed, rather than assuming the environment must change."""
    versions: Dict[str, str] = {}
    probes = [
        ("java", ["javac", "-version"], r"(\d+)(?:\.\d+)*"),
        ("node", ["node", "-v"], r"v?(\d+)"),
        ("python", ["python3", "--version"], r"(\d+\.\d+)"),
        ("dotnet", ["dotnet", "--version"], r"(\d+\.\d+)"),
        ("go", ["go", "version"], r"go(\d+\.\d+)"),
    ]
    for name, argv, pattern in probes:
        binary = _binary_on_path(argv[0])
        if not binary:
            continue
        try:
            proc = subprocess.run(
                [binary] + argv[1:], capture_output=True, text=True, timeout=5,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            m = re.search(pattern, out)
            if m:
                versions[name] = m.group(1)
        except Exception:
            continue
    return versions


# iter-15.62.2 — The single most common "generic" (non-file:line) compile
# failure across ALL target stacks is a Java/.NET/Node release-version
# mismatch: the generated build manifest targets a newer runtime than
# what's actually installed in the LAMA image. Relying on the Planner
# LLM to reliably name the exact manifest path every time proved
# fragile (path-matching drift, the model sometimes leaving
# `target_files` empty even after correctly diagnosing the cause) — so
# this is caught DETERMINISTICALLY, with zero LLM round-trip, making
# this exact failure class unfixable-proof regardless of which
# application/stack triggered it.
_RELEASE_MISMATCH_RES = [
    re.compile(r"invalid target release:?\s*(\d+)", re.IGNORECASE),
    re.compile(r"invalid source release:?\s*(\d+)", re.IGNORECASE),
    re.compile(r"release version (\d+) not supported", re.IGNORECASE),
    re.compile(r"source release (\d+) requires target release", re.IGNORECASE),
    re.compile(r"unsupported class file major version \d+", re.IGNORECASE),
    re.compile(r"engine[\" ]*node[\"']?[:>=~^\s]*v?(\d+)", re.IGNORECASE),
]
_MANIFEST_BASENAMES = ("pom.xml", "build.gradle", "build.gradle.kts", ".csproj", "package.json")


def _detect_release_mismatch_error_groups(
    fails: List[Dict[str, Any]],
    candidate_files: List[str],
    installed_versions: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Deterministic catch for the "target/source release newer than
    installed toolchain" failure class. Returns build_config error
    groups (same shape `_parse_compile_errors` produces) or an empty
    list if this doesn't look like that failure."""
    java_installed = installed_versions.get("java")
    groups: List[Dict[str, Any]] = []
    for f in fails:
        text = f"{f.get('stdout_tail','')}\n{f.get('stderr_tail','')}"
        matched = any(pat.search(text) for pat in _RELEASE_MISMATCH_RES)
        if not matched or not java_installed:
            continue
        cwd = (f.get("cwd") or "").strip("/")
        manifests = [
            p for p in candidate_files
            if p.rstrip("/").endswith(_MANIFEST_BASENAMES)
            and (not cwd or p.replace("\\", "/").startswith(cwd))
        ]
        if not manifests:
            # cwd-prefix match found nothing (path-formatting drift between
            # the compiler's cwd and how paths are stored) — fall back to
            # ANY manifest candidate rather than giving up. Most pilots have
            # only 1-2 build manifests, so this is still a safe, targeted fix.
            manifests = [p for p in candidate_files if p.rstrip("/").endswith(_MANIFEST_BASENAMES)]
        for path in manifests:
            groups.append({
                "path": path,
                "component": f.get("component"),
                "tool": f.get("tool"),
                "kind": "build_config",
                "lines": [(0, (
                    f"Build failed: the configured Java release/target/source "
                    f"version is newer than the JDK actually installed in this "
                    f"build environment (JDK {java_installed}). Lower "
                    f"<maven.compiler.release> (or <maven.compiler.source>/"
                    f"<maven.compiler.target>, or Gradle's sourceCompatibility/"
                    f"targetCompatibility/toolchain languageVersion) to "
                    f"{java_installed} in this file. Do not change anything else."
                ))],
            })
    # De-dup by path — multiple failing modules can point at the same
    # parent/aggregator manifest, and we only need one FIX task for it.
    seen_paths = set()
    deduped: List[Dict[str, Any]] = []
    for g in groups:
        if g["path"] in seen_paths:
            continue
        seen_paths.add(g["path"])
        deduped.append(g)
    return deduped


# iter-15.62.3 — The SECOND most common "generic" compile failure across
# stacks is a missing dependency: a source file references a class/
# package/module that isn't declared in the build manifest at all
# (`package jakarta.json does not exist`, `Cannot find module 'x'`,
# `ModuleNotFoundError: No module named 'x'`, C#'s CS0246, Go's
# "cannot find package", or a Maven/Gradle dependency-resolution
# failure). Unlike the release-mismatch case, the CORRECT fix (which
# exact groupId:artifactId:version / npm package / NuGet package to
# add) requires library knowledge an LLM has but a regex doesn't — so
# we can't fully deterministically fix this the way we did for release
# mismatches. What we CAN guarantee deterministically is that this
# failure class is ALWAYS treated as fixable (never "no fixable cause
# identified") and ALWAYS routed to the build manifest with the exact
# missing symbol/package extracted from the compiler output, so the
# Coder (which does have the library knowledge) gets a concrete,
# well-scoped task instead of the Planner LLM being free to bail out.
_MISSING_DEPENDENCY_RES = [
    re.compile(r"package ([\w.]+) does not exist", re.IGNORECASE),
    re.compile(r"cannot find symbol[\s\S]{0,200}?symbol:\s*class\s+([\w.]+)", re.IGNORECASE),
    re.compile(r"class file for ([\w.]+) not found", re.IGNORECASE),
    re.compile(r"could not resolve dependenc(?:y|ies)[^\n]*", re.IGNORECASE),
    re.compile(r"could not find artifact ([\w.\-:]+)", re.IGNORECASE),
    re.compile(r"could not resolve ([\w.\-:]+)", re.IGNORECASE),
    re.compile(r"unresolved dependency[^\n]*", re.IGNORECASE),
    re.compile(r"cannot find module ['\"]([\w./@-]+)['\"]", re.IGNORECASE),
    re.compile(r"module not found: error[^\n]*", re.IGNORECASE),
    re.compile(r"modulenotfounderror: no module named ['\"]([\w.]+)['\"]", re.IGNORECASE),
    re.compile(r"no matching distribution found for ([\w.\-]+)", re.IGNORECASE),
    re.compile(r"error cs0246:[^\n]*", re.IGNORECASE),
    re.compile(r"the type or namespace name ['\"]([\w.]+)['\"] could not be found", re.IGNORECASE),
    re.compile(r"no required module provides package ([\w./-]+)", re.IGNORECASE),
    re.compile(r"cannot find package ['\"]([\w./-]+)['\"]", re.IGNORECASE),
    # Catch-all for the LLM's own prose summary of this failure class
    # (this is literally the phrasing that prompted this iteration —
    # "missing Jakarta JSON API dependencies" — so a Planner/Coder
    # narrative describing a missing dependency should ALSO be treated
    # as fixable here, not just raw compiler tool output).
    re.compile(r"missing[^\n]{0,80}\bdependenc(?:y|ies)\b", re.IGNORECASE),
]


def _detect_missing_dependency_error_groups(
    fails: List[Dict[str, Any]],
    candidate_files: List[str],
) -> List[Dict[str, Any]]:
    """Deterministic catch for "missing dependency" style failures.
    Unlike `_detect_release_mismatch_error_groups`, this does NOT know
    the correct fix itself — it guarantees the failure is routed to the
    build manifest with the extracted missing symbol/package so the
    Coder can add the right dependency, instead of the loop giving up."""
    groups: List[Dict[str, Any]] = []
    for f in fails:
        text = f"{f.get('stdout_tail','')}\n{f.get('stderr_tail','')}\n{f.get('reason','')}"
        missing_tokens: List[str] = []
        matched = False
        for pat in _MISSING_DEPENDENCY_RES:
            for m in pat.finditer(text):
                matched = True
                if m.groups() and m.group(1):
                    missing_tokens.append(m.group(1))
                else:
                    missing_tokens.append(m.group(0).strip()[:160])
        if not matched:
            continue
        cwd = (f.get("cwd") or "").strip("/")
        manifests = [
            p for p in candidate_files
            if p.rstrip("/").endswith(_MANIFEST_BASENAMES)
            and (not cwd or p.replace("\\", "/").startswith(cwd))
        ]
        if not manifests:
            manifests = [p for p in candidate_files if p.rstrip("/").endswith(_MANIFEST_BASENAMES)]
        tokens_desc = ", ".join(dict.fromkeys(missing_tokens))[:400] or "(see raw build output)"
        for path in manifests:
            groups.append({
                "path": path,
                "component": f.get("component"),
                "tool": f.get("tool"),
                "kind": "build_config",
                "lines": [(0, (
                    f"Build failed because of a MISSING DEPENDENCY — the build "
                    f"references: {tokens_desc}. Add the correct dependency "
                    f"(and its runtime implementation artifact if it's an API-"
                    f"only library, e.g. Jakarta JSON API needs both the API "
                    f"and an implementation like org.eclipse.parsson:parsson) "
                    f"to this build manifest, using well-known, stable "
                    f"coordinates and a version consistent with the other "
                    f"dependencies already declared here. Do NOT remove or "
                    f"alter unrelated dependencies."
                ))],
            })
    seen_paths = set()
    deduped: List[Dict[str, Any]] = []
    for g in groups:
        if g["path"] in seen_paths:
            continue
        seen_paths.add(g["path"])
        deduped.append(g)
    return deduped


# iter-15.69 — A FOURTH common "generic" compile failure: the diagnostics
# never made it through the `File.java:[line,col]` regex at all (e.g. an
# unusual path-formatting quirk, or the offending line got evicted from
# the captured tail by a flood of cascading errors on older stored
# results without `_full` buffers), but the raw failure text still
# unmistakably describes a CORRUPTED file — "illegal character",
# "unclosed character/string literal", "class, interface, enum, or
# record expected". This is a deterministic, zero-LLM-round-trip catch
# so this failure class is NEVER dismissed as "no fixable cause could be
# identified" — the fix (regenerate the file cleanly) doesn't require
# any judgement call the way "which dependency" does.
def _detect_corrupted_source_error_groups(
    fails: List[Dict[str, Any]],
    candidate_files: List[str],
) -> List[Dict[str, Any]]:
    """Deterministic catch for "corrupted source file" style failures
    (illegal character / unclosed literal / class-interface-enum-expected).
    Always returns a fixable error group — full-file regeneration is a
    context-free fix, unlike missing-dependency resolution."""
    groups: List[Dict[str, Any]] = []
    candidate_basenames = {os.path.basename(p): p for p in candidate_files}
    for f in fails:
        text = f"{f.get('stdout_tail','')}\n{f.get('stderr_tail','')}\n{f.get('reason','')}"
        if not _CORRUPTED_SOURCE_SIGNATURE_RE.search(text):
            continue
        # Try to pin down WHICH file is corrupted from any `Name.ext:...`
        # mention near the corruption signature, even without a full
        # `[line,col]` match (e.g. path formatting the main regex missed).
        paths_found: List[str] = []
        for m in re.finditer(r"([A-Za-z0-9_./\\\-]+?\.(?:java|kt|scala|groovy|cs|cpp|c|go|rs|php|swift))\b", text):
            raw = m.group(1).replace("\\", "/").strip().lstrip("/")
            base = os.path.basename(raw)
            resolved = raw if raw in candidate_files else candidate_basenames.get(base)
            if resolved and resolved not in paths_found:
                paths_found.append(resolved)
        if not paths_found:
            # Can't pin down which file — still surface the failure as
            # fixable via the Planner LLM pass rather than silently
            # dropping it (this function only returns empty when there's
            # truly nothing plausible to hand the Coder).
            continue
        for path in paths_found[:5]:
            groups.append({
                "path": path,
                "component": f.get("component"),
                "tool": f.get("tool"),
                "kind": "corrupted_source",
                "lines": [(0, (
                    "Build failed because this file's stored content is "
                    "CORRUPTED — the compiler reports illegal characters, "
                    "unclosed character/string literals, or a missing "
                    "class/interface/enum/record declaration. This is "
                    "typically leftover markdown code-fence residue, a "
                    "truncated LLM response, or non-ASCII 'smart' quotes/"
                    "dashes inside a literal — not an ordinary logic bug."
                ))],
            })
    seen_paths = set()
    deduped: List[Dict[str, Any]] = []
    for g in groups:
        if g["path"] in seen_paths:
            continue
        seen_paths.add(g["path"])
        deduped.append(g)
    return deduped



# from BOTH of the above: a dependency IS already declared in the build
# manifest, but the pinned coordinate (groupId:artifactId:VERSION)
# simply does not exist in the repository ("... was not found in
# https://repo.maven.apache.org/maven2 during a previous attempt. This
# failure was cached ..."). This happens in practice when the Coder's
# own PREVIOUS fix round (for a missing-dependency failure, see above)
# guessed a plausible-looking but nonexistent version number, e.g.
# `org.eclipse.parsson:parsson:1.0.10` when only `1.0.0-1.0.5` and
# `1.1.0-1.1.9` actually exist on Maven Central.
#
# The earlier missing-dependency catch-all regex
# (`could not resolve dependenc(?:y|ies)`) ALSO matches this failure's
# wording, which meant it was previously mis-routed to the "add the
# missing dependency" instruction — telling the Coder to add something
# that was already there, so it just guessed a different, equally
# unverified version and the loop stagnated on repeat guesses.
#
# This detector must run BEFORE `_detect_missing_dependency_error_groups`
# and does something the other two can't: it performs a real,
# deterministic (non-LLM) network lookup against the actual Maven
# repository's `maven-metadata.xml` to find out which versions genuinely
# exist, then tells the Coder the EXACT correct version to use — removing
# the guesswork entirely instead of asking an LLM to recall exact patch
# numbers from memory.
_DEP_VERSION_NOT_FOUND_RES = [
    re.compile(
        r"([\w.\-]+):([\w.\-]+):(?:jar|pom|war|maven-plugin):([\w.\-]+)\s+was not found in (\S+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"could not find artifact ([\w.\-]+):([\w.\-]+):(?:jar|pom|war|maven-plugin):([\w.\-]+)",
        re.IGNORECASE,
    ),
]


async def _fetch_maven_versions(group_id: str, artifact_id: str, repo_base: str) -> List[str]:
    """Deterministic (non-LLM) lookup of every version that actually
    exists for `group_id:artifact_id` in a Maven repository, via its
    standard `maven-metadata.xml`. Returns `[]` on any failure (offline,
    blocked, malformed) so the caller can fall back gracefully — this
    is a best-effort accuracy improvement, not a hard dependency."""
    try:
        import httpx
        import xml.etree.ElementTree as ET
        base = (repo_base or "https://repo.maven.apache.org/maven2").rstrip("/")
        url = f"{base}/{group_id.replace('.', '/')}/{artifact_id}/maven-metadata.xml"
        async with httpx.AsyncClient(timeout=6.0, verify=_http_verify()) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.text)
            return [v.text for v in root.iter("version") if v.text]
    except Exception:
        return []


def _best_replacement_version(bad_version: str, available: List[str]) -> str:
    """Pick the best real replacement for a nonexistent pinned version:
    prefer the highest version within the SAME major.minor family (the
    least disruptive change, most likely to be API-compatible with what
    the Coder originally intended), else fall back to the overall
    highest available version."""
    def _key(v: str):
        parts = re.split(r"[.\-]", v)
        out = []
        for p in parts:
            try:
                out.append((0, int(p)))
            except ValueError:
                out.append((1, p))
        return tuple(out)

    bad_parts = re.split(r"[.\-]", bad_version)
    family = ".".join(bad_parts[:2]) if len(bad_parts) >= 2 else bad_version
    same_family = [v for v in available if v.startswith(family + ".") or v == family]
    pool = same_family or available
    return sorted(pool, key=_key)[-1] if pool else ""


async def _detect_dependency_version_not_found_error_groups(
    fails: List[Dict[str, Any]],
    candidate_files: List[str],
) -> List[Dict[str, Any]]:
    """Deterministic catch for "a pinned dependency version does not
    exist in the repository" failures. Unlike
    `_detect_missing_dependency_error_groups`, the dependency is already
    declared — only the version number is wrong — so the fix is to
    CORRECT the version in place, not add a new dependency. Performs a
    real Maven Central lookup so the replacement version is verified to
    actually exist, instead of asking the Coder to guess again."""
    groups: List[Dict[str, Any]] = []
    for f in fails:
        text = f"{f.get('stdout_tail','')}\n{f.get('stderr_tail','')}\n{f.get('reason','')}"
        bad_coords: List[Tuple[str, str, str, str]] = []
        for pat in _DEP_VERSION_NOT_FOUND_RES:
            for m in pat.finditer(text):
                groups_matched = m.groups()
                if len(groups_matched) >= 3:
                    group_id, artifact_id, version = groups_matched[0], groups_matched[1], groups_matched[2]
                    repo = groups_matched[3] if len(groups_matched) >= 4 else ""
                    bad_coords.append((group_id, artifact_id, version, repo or ""))
        if not bad_coords:
            continue
        cwd = (f.get("cwd") or "").strip("/")
        manifests = [
            p for p in candidate_files
            if p.rstrip("/").endswith(_MANIFEST_BASENAMES)
            and (not cwd or p.replace("\\", "/").startswith(cwd))
        ]
        if not manifests:
            manifests = [p for p in candidate_files if p.rstrip("/").endswith(_MANIFEST_BASENAMES)]
        for group_id, artifact_id, bad_version, repo in dict.fromkeys(bad_coords):
            available = await _fetch_maven_versions(group_id, artifact_id, repo)
            replacement = _best_replacement_version(bad_version, available) if available else ""
            if replacement:
                instruction = (
                    f"Build failed because the pinned version `{bad_version}` of "
                    f"`{group_id}:{artifact_id}` DOES NOT EXIST in the Maven "
                    f"repository — it was cached as a permanent negative "
                    f"resolution failure. This dependency is already declared; "
                    f"do NOT add a duplicate. Instead, CORRECT the existing "
                    f"<version> (or dependency-management entry / property) for "
                    f"`{group_id}:{artifact_id}` from `{bad_version}` to "
                    f"`{replacement}` — this version was just confirmed to "
                    f"actually exist on the remote repository. Do not invent a "
                    f"different version; do not change anything else in this "
                    f"file."
                )
            else:
                instruction = (
                    f"Build failed because the pinned version `{bad_version}` of "
                    f"`{group_id}:{artifact_id}` does not exist in the Maven "
                    f"repository (cached as a permanent negative resolution "
                    f"failure). This dependency is already declared; do NOT add "
                    f"a duplicate. Live version lookup was unavailable, so do "
                    f"NOT guess another exact patch/minor version blindly. "
                    f"Prefer removing the explicit <version> for "
                    f"`{group_id}:{artifact_id}` so it falls back to a parent "
                    f"BOM/dependencyManagement default if one exists, or pin it "
                    f"to a well-known, widely-used stable release you are "
                    f"confident actually exists (e.g. the version already used "
                    f"by a sibling dependency in the same manifest, if any)."
                )
            for path in manifests:
                groups.append({
                    "path": path,
                    "component": f.get("component"),
                    "tool": f.get("tool"),
                    "kind": "build_config",
                    "lines": [(0, instruction)],
                })
    seen_paths = set()
    deduped: List[Dict[str, Any]] = []
    for g in groups:
        if g["path"] in seen_paths:
            continue
        seen_paths.add(g["path"])
        deduped.append(g)
    return deduped


async def _llm_diagnose_generic_failure(
    transform_id: str,
    compile_result: Dict[str, Any],
    iteration: int,
    model: Optional[str],
) -> Dict[str, Any]:
    """iter-15.62 — When `_parse_compile_errors` can't extract per-file
    diagnostics, hand the RAW failure output to the Planner (LLM) and
    ask it to identify which file(s) — source OR build manifest
    (pom.xml/build.gradle/package.json/requirements.txt/*.csproj) — most
    likely need editing to fix the build. Returns
    `{"blocked": bool, "root_cause": str, "error_groups": [...]}` where
    `error_groups` is shaped exactly like `_parse_compile_errors`'
    output (`{path, component, tool, lines: [(lineno, snippet)]}`, with
    `kind="build_config"` added) so it feeds straight into
    `_planner_fix_tasks_from_errors` unchanged."""
    fails: List[Dict[str, Any]] = []
    for comp in compile_result.get("components") or []:
        for inv in comp.get("invocations") or []:
            if inv.get("status") not in ("failed", "timeout"):
                continue
            fails.append({
                "component": comp.get("component"),
                "tool": comp.get("tool"),
                "cwd": inv.get("cwd"),
                "reason": inv.get("reason"),
                "stdout_tail": (inv.get("stdout_tail") or "")[-3000:],
                "stderr_tail": (inv.get("stderr_tail") or "")[-3000:],
            })
    if not fails:
        return {"blocked": False, "root_cause": "", "error_groups": []}

    candidate_files: List[str] = []
    cursor = transform_files.find(
        {"transform_id": transform_id, "type": "transformed"}, {"path": 1},
    )
    async for d in cursor:
        p = d.get("path") or ""
        if p:
            candidate_files.append(p)
    candidate_files = candidate_files[:200]
    installed_versions = _detect_installed_toolchain_versions()

    # ── Deterministic fast-path (no LLM round-trip, no path-naming risk) ──
    release_groups = _detect_release_mismatch_error_groups(fails, candidate_files, installed_versions)
    if release_groups:
        java_installed = installed_versions.get("java", "?")
        return {
            "blocked": False,
            "root_cause": (
                f"Build manifest targets a Java release newer than the JDK "
                f"actually installed in this environment (JDK {java_installed})."
            ),
            "error_groups": release_groups,
        }

    dependency_version_groups = await _detect_dependency_version_not_found_error_groups(fails, candidate_files)
    if dependency_version_groups:
        return {
            "blocked": False,
            "root_cause": (
                "Build failed because a pinned dependency version does not "
                "exist in the repository (already declared, just the wrong "
                "version — corrected against a live repository lookup)."
            ),
            "error_groups": dependency_version_groups,
        }

    dependency_groups = _detect_missing_dependency_error_groups(fails, candidate_files)
    if dependency_groups:
        return {
            "blocked": False,
            "root_cause": (
                "Build failed because of a missing dependency — the required "
                "library/module isn't declared in the build manifest yet."
            ),
            "error_groups": dependency_groups,
        }

    corrupted_groups = _detect_corrupted_source_error_groups(fails, candidate_files)
    if corrupted_groups:
        return {
            "blocked": False,
            "root_cause": (
                "Build failed because one or more source files' PERSISTED "
                "content is corrupted (illegal characters, unclosed literals, "
                "or a missing class/interface/enum/record declaration) — "
                "typically markdown-fence residue or a truncated LLM "
                "response, not a normal logic bug."
            ),
            "error_groups": corrupted_groups,
        }

    try:
        # iter-19 — the PROMPT is still the Planner's (the task is unchanged:
        # read the failure, name the files to edit), but the MODEL is no
        # longer the Planner's. Inheriting a per-transformation Planner
        # override here would pin diagnosis to a generative model and
        # defeat the `reasoning` tier this call now routes through.
        prompt_template = await _get_effective_prompt(transform_id, "planner")
        eff_model = model
        if not prompt_template:
            return {"blocked": False, "root_cause": "no Planner prompt configured", "error_groups": []}

        user_prompt = f"""COMPILE-FIX DIAGNOSIS — iteration {iteration}.

The native build failed and no per-file `file:line` diagnostics could be
extracted automatically. Read the RAW failure output below and decide
which file(s) in the transformed tree most likely need editing to fix
the build — this can be a SOURCE file (referenced by class/symbol name
even without a line number) or a BUILD MANIFEST (pom.xml / build.gradle
/ package.json / requirements.txt / *.csproj) when the failure is a
dependency, plugin, or build-configuration problem.

===== TOOLCHAIN VERSIONS ACTUALLY INSTALLED IN THIS BUILD ENVIRONMENT =====
{json.dumps(installed_versions, indent=2)}
If the failure is a Java/Node/Python/.NET/Go RELEASE, SOURCE, or TARGET
version mismatch (e.g. "release version 21 not supported", "invalid
target release", an engines/toolchain version requirement in
package.json, a <TargetFramework> too new for the installed SDK), this
IS fixable: lower the build manifest's configured version to match what
is ACTUALLY installed above (e.g. `<maven.compiler.release>`, Gradle's
`sourceCompatibility`/`targetCompatibility`/`toolchain`, `engines.node`,
`<TargetFramework>`). Do NOT mark this "fixable: false" just because it
looks like an environment/infra mismatch — adjusting the manifest to
match the installed toolchain is the correct, standard fix unless the
generated code demonstrably uses language features unavailable at the
lower version.

===== FAILING BUILD(S) =====
{json.dumps(fails, indent=2)[:12000]}

===== CANDIDATE FILES IN THE TRANSFORMED TREE (pick ONLY from this list) =====
{json.dumps(candidate_files, indent=2)[:6000]}

Return ONLY valid JSON:
{{
  "root_cause": "one or two sentence diagnosis",
  "fixable": true/false,
  "target_files": [
    {{"path": "<one of the candidate files above, verbatim, OR a brand-new file path — see below>", "instructions": "specific, actionable fix instructions for the Coder", "is_new_file": true/false}}
  ]
}}
Set "fixable" to false ONLY when the failure is an infrastructure/
environment problem (missing toolchain, network, disk, licensing) that
no source-code or build-manifest edit could possibly resolve.

iter-15.69 — If the failure says a file has "illegal character(s)",
an "unclosed character/string literal", or is missing a "class,
interface, enum, or record declaration", that file's STORED content is
corrupted (markdown-fence residue, a truncated LLM response, or
non-ASCII smart quotes/dashes inside a literal) — this is ALWAYS
fixable by regenerating that one file cleanly from scratch. Never set
"fixable": false for this failure class; set instructions to
"REWRITE THE ENTIRE FILE from scratch as clean, complete, valid,
compilable source, using only straight ASCII quotes/apostrophes in any
string/char literal, with no markdown fences or commentary."

iter-16.x — Some failures (e.g. Spring Boot's "Unable to find main
class", a missing `__init__.py`/entrypoint module, a missing config
file the build expects) are NOT fixable by editing any file that
already exists — the fix is to CREATE a file that is currently
MISSING altogether. When that is the root cause, set
`"is_new_file": true` and give `path` a brand-new file path that
follows the same package/directory conventions visible in the
candidate list (e.g. a `@SpringBootApplication` main class living
next to the other classes under the same base package, named after
the project). Do NOT set "fixable": false just because the needed
file doesn't exist yet — creating it is a normal, valid fix."""

        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": user_prompt},
            ],
            model=eff_model,
            # iter-19 — this call is DIAGNOSIS, not planning: it reads a
            # wall of raw build output and works out what actually broke,
            # which is what the o-series is good at and what the
            # `reasoning` tier exists to route to. It kept the Planner's
            # key only because there was no better one; it uses the
            # Planner's prompt either way (see `eff_model` above), so the
            # split changes which model reads the output, not what it is
            # asked to do.
            agent_key="tools.transformer.diagnostician",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )
        response_text = response.get("content", "") if isinstance(response, dict) else str(response)
        parsed = _extract_json_object(response_text) or {}
        root_cause = str(parsed.get("root_cause") or "").strip()
        fixable = bool(parsed.get("fixable", True))
        candidate_set = set(candidate_files)
        groups: List[Dict[str, Any]] = []
        if fixable:
            for t in (parsed.get("target_files") or []):
                if not isinstance(t, dict):
                    continue
                path = str(t.get("path") or "").strip()
                instructions = str(t.get("instructions") or "").strip()
                # iter-16.x — a proposed file is either (a) an existing
                # candidate to EDIT, or (b) a brand-new file to CREATE
                # (`is_new_file: true`) when the diagnosed root cause is
                # something missing altogether (e.g. no Spring Boot main
                # class). Previously any path not already in the
                # transformed tree was silently dropped here, which is
                # exactly why "Unable to find main class" failures could
                # never be fixed — the Planner correctly identified the
                # missing file but had nowhere to route it.
                is_new_file = bool(t.get("is_new_file")) and path not in candidate_set
                if not path or not instructions:
                    continue
                if path not in candidate_set and not is_new_file:
                    continue
                # iter-15.69 — only label this a "build_config" fix when
                # the path is actually a build manifest. Previously EVERY
                # LLM-diagnosed existing file (including plain source
                # files, e.g. a corrupted .java file) got mislabeled
                # "build_config", so `_planner_fix_tasks_from_errors`
                # told the Coder to "fix ONLY the build/dependency/plugin
                # configuration" on a file that isn't a manifest at all.
                if is_new_file:
                    kind = "new_file"
                elif path.rstrip("/").endswith(_MANIFEST_BASENAMES):
                    kind = "build_config"
                else:
                    kind = "generic_source"
                groups.append({
                    "path": path,
                    "component": fails[0].get("component") if fails else "",
                    "tool": fails[0].get("tool") if fails else "",
                    "kind": kind,
                    "lines": [(0, instructions[:600])],
                })
        return {
            "blocked": not fixable,
            "root_cause": root_cause or "Planner could not identify a fixable cause.",
            "error_groups": groups,
        }
    except Exception as e:
        return {"blocked": False, "root_cause": f"Planner diagnosis failed: {e}", "error_groups": []}


async def _resolve_failing_transform_file(
    transform_id: str, candidate_path: str,
) -> Optional[Dict[str, Any]]:
    """Find the persisted `transform_files` row whose path matches (or ends
    with) the diagnostic file. Compilers often log a path relative to the
    build cwd, not the transform-workspace root, so we do a suffix match
    as a fallback."""
    doc = await transform_files.find_one(
        {"transform_id": transform_id, "type": "transformed", "path": candidate_path}
    )
    if doc:
        return doc
    cursor = transform_files.find(
        {"transform_id": transform_id, "type": "transformed"},
        {"path": 1, "content": 1, "original_path": 1},
    )
    async for d in cursor:
        p = (d.get("path") or "").replace("\\", "/")
        if p.endswith(candidate_path) or candidate_path.endswith(p):
            return d
    return None


async def _planner_fix_tasks_from_errors(
    transform_id: str,
    error_groups: List[Dict[str, Any]],
    iteration: int,
) -> List[Dict[str, Any]]:
    """Planner-owned: turn compiler diagnostics into concrete FIX tasks
    the Coder can pick up. Persists to `transformer_tasks` with wave=99
    (post-build) and returns the task docs."""
    run_id = await _log_agent_run(
        transform_id, "planner", "fix_planning",
        input_summary=f"Compile-fix iteration {iteration}: {len(error_groups)} failing file(s)",
    )
    t0 = datetime.now(timezone.utc)
    created: List[Dict[str, Any]] = []
    try:
        for grp in error_groups:
            path = grp["path"]
            is_new_file = grp.get("kind") == "new_file"
            file_doc = None if is_new_file else await _resolve_failing_transform_file(transform_id, path)
            if not file_doc and not is_new_file:
                continue
            real_path = (file_doc or {}).get("path") or path
            original_path = (file_doc or {}).get("original_path") or real_path
            lines_desc = "\n".join(
                f"  - line {ln}: {snip}" for ln, snip in grp["lines"][:6]
            ) or "  (no line-level diagnostic captured)"
            # iter-15.62 — Planner-diagnosed build-config fixes (pom.xml,
            # build.gradle, package.json, ...) get phrasing appropriate to
            # editing a manifest instead of source (no "imports/types"
            # framing, and an explicit guard against unrelated dependency
            # churn since the Planner picked this file without a compiler
            # file:line to anchor on).
            if is_new_file:
                notes = (
                    f"COMPILE FIX (iter {iteration}) — this file does NOT exist "
                    f"yet in the transformed tree. CREATE `{real_path}` from "
                    f"scratch to resolve the build failure:\n{lines_desc}\n"
                    "Generate a complete, compilable file at this exact path. "
                    "Do not reference or modify any other file."
                )
            elif grp.get("kind") == "build_config":
                notes = (
                    f"COMPILE FIX (iter {iteration}) — Planner diagnosis for build "
                    f"configuration file `{real_path}`:\n{lines_desc}\n"
                    "Fix ONLY the build/dependency/plugin configuration needed to "
                    "resolve the reported failure. Do NOT remove functionality, "
                    "reorder unrelated dependencies, or bump versions beyond what "
                    "the diagnosis above requires."
                )
            elif grp.get("kind") == "corrupted_source":
                # iter-15.69 — "illegal character" / "unclosed literal" /
                # "class, interface, enum, or record expected" mean the
                # PERSISTED file content itself is broken (markdown-fence
                # residue, truncated output, or stray non-ASCII smart
                # quotes/dashes inside a literal) — there is no sane
                # incremental patch to apply. Ask for a full, clean
                # regeneration instead of "fix only the reported issue".
                notes = (
                    f"COMPILE FIX (iter {iteration}) — {grp.get('tool','')} reports "
                    f"`{real_path}` is CORRUPTED, not merely buggy:\n{lines_desc}\n"
                    "This is caused by broken/garbled file content (leftover markdown "
                    "code-fence markers, an LLM response that was cut off mid-file, "
                    "or non-ASCII 'smart' quotes/apostrophes/dashes used inside a "
                    "string or char literal) — NOT a normal logic bug. Do NOT try to "
                    "patch around the reported line. REWRITE THE ENTIRE FILE from "
                    "scratch as clean, complete, compilable source: re-derive it from "
                    "the ORIGINAL file's business logic, use only straight ASCII "
                    "quotes/apostrophes (' and \", never curly “ ” ‘ ’) in every "
                    "string/char literal, include exactly one top-level class/"
                    "interface/enum/record declaration, and do not wrap the output in "
                    "markdown code fences or add any commentary before/after the code."
                )
            else:
                notes = (
                    f"COMPILE FIX (iter {iteration}) — {grp.get('tool','')} "
                    f"reported errors in `{real_path}`:\n{lines_desc}\n"
                    "Fix ONLY the reported issues (imports, types, missing "
                    "symbols, syntax) while preserving all business logic. "
                    "Do NOT rename public API paths, DB tables, or method "
                    "signatures unless the compiler explicitly demands it."
                )
            task_id = f"FIX-{iteration}-{str(ObjectId())[:6]}"
            task_row = {
                "_id": str(ObjectId()),
                "transform_id": transform_id,
                "task_id": task_id,
                "envelope_id": "",
                "title": f"{'Create missing file' if is_new_file else 'Fix compile errors in'} {os.path.basename(real_path)}",
                "description": f"{'Create' if is_new_file else 'Repair compile diagnostics in'} {real_path}",
                "phase": "fix",
                "layer": "fix",
                "action": "NEW" if is_new_file else "FIX",
                "is_new_file": is_new_file,
                "wave": 99,
                "wave_name": f"Compile-fix iteration {iteration}",
                "source_path": original_path,
                "target_path": real_path,
                "status": "PENDING",
                "assigned_to": "coder",
                "depends_on": [],
                "confidence": 0.0,
                "verifier_score": 0.0,
                "verifier_checks": {},
                "rejection_count": 0,
                "notes": notes,
                "compile_errors": grp["lines"],
                "fix_iteration": iteration,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await transformer_tasks.insert_one(task_row)
            created.append(task_row)

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Created {len(created)} FIX task(s) for iteration {iteration}",
            duration_ms=ms,
        )
        return created
    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        return created


# How much raw build output to put in front of the escalated agent.
# `_run_compiler` already retains 200 KB for parsing; a Maven reactor dump
# is mostly download progress, and the diagnosis lives in the tail.
_RAW_BUILD_LOG_CHARS = 6000


def _raw_build_log_excerpt(compile_result: Dict[str, Any]) -> str:
    """The tail of what the compiler actually printed, for the fix prompt.

    iter-20 — until now no agent in the compile-fix loop ever saw raw
    build output. `_planner_fix_tasks_from_errors` renders the Planner's
    prose diagnosis into `notes`, and that is all the Coder received. Any
    detail the Planner elided — the failing plugin goal, a dependency
    conflict tree, the second error hiding under the first — was simply
    unavailable to every repair attempt, which is a large part of why a
    third attempt reproduced the second.

    Returns "" when there is nothing useful, so the rung degrades to a
    plain devops_expert retry rather than emitting an empty section that
    reads as "the build printed nothing".
    """
    chunks: List[str] = []
    for comp in (compile_result or {}).get("components") or []:
        for inv in (comp or {}).get("invocations") or []:
            if (inv or {}).get("status") in (None, "passed", "skipped"):
                continue
            body = "\n".join(
                s for s in (inv.get("stderr_tail") or "", inv.get("stdout_tail") or "")
                if s.strip()
            ).strip()
            if not body:
                continue
            chunks.append(
                f"--- {comp.get('component', '?')} / {inv.get('tool', comp.get('tool', '?'))} "
                f"in {inv.get('cwd', '.')} (status={inv.get('status')}) ---\n"
                + body[-_RAW_BUILD_LOG_CHARS:]
            )
    if not chunks:
        return ""
    return (
        "\n\nRAW BUILD OUTPUT — this is verbatim what the build tool printed, "
        "not a summary. Read it before deciding what to change; the actual "
        "root cause is often a line ABOVE the first reported error, or a "
        "second failure underneath it.\n"
        + "\n\n".join(chunks)[: _RAW_BUILD_LOG_CHARS * 2]
    )


def _regeneration_brief(
    target_path: str,
    failed_content: str,
    compile_result: Dict[str, Any],
    detected_stack: dict,
    target_stack,
    regenerating_from_original: bool,
) -> str:
    """The last-resort brief: rewrite this file, do not patch it further.

    iter-20 — this is the operator's own successful fallback, made part of
    the loop. When four compile rounds have failed they would take the
    generated folder to a chat model with a senior-developer brief —
    convert it properly, implement Swagger, do not alter business logic,
    do not finish with broken code — and it worked. It worked because it
    stops trying to repair a bad transformation one error at a time and
    redoes it, which is a move rungs 0-2 structurally cannot make.
    """
    playbook = _playbook_for(target_stack)
    raw = _raw_build_log_excerpt(compile_result)
    banned = _residue_tokens_for(detected_stack or {}, target_stack)
    banned_line = ""
    if banned:
        banned_line = (
            f"\n  - The result must contain NONE of these source-stack "
            f"tokens: {', '.join(banned[:15])}."
        )

    if regenerating_from_original:
        source_note = (
            "You are being given the ORIGINAL LEGACY FILE, not the failed "
            "transformation. Migrate it again from scratch. Do not try to "
            "reconstruct or salvage the previous attempt."
        )
    else:
        source_note = (
            "The original legacy file is not available for this path, so you "
            "are given the current failed version. Rewrite it in full against "
            "the target stack's conventions rather than patching the reported "
            "errors one by one."
        )

    failed_excerpt = ""
    if failed_content.strip():
        failed_excerpt = (
            f"\n\nTHE ATTEMPT THAT FAILED (for reference only — do NOT copy its "
            f"structure; it is the thing that does not build):\n"
            f"{failed_content[:4000]}"
        )

    return (
        f"\n\n=== FULL REGENERATION — escalation of last resort for {target_path} ===\n"
        f"Four earlier repair rounds produced the identical build failure. "
        f"Patching has been abandoned for this file.\n\n"
        f"ROLE: you are a senior backend developer and legacy-modernisation "
        f"expert.\n\n"
        f"TASK: produce a complete, correct, build-ready version of this file "
        f"for the target stack.\n\n"
        f"{source_note}\n\n"
        f"STRICT RULES:\n"
        f"  - Do NOT alter business logic. The behaviour must be identical: "
        f"same API paths, same HTTP methods, same request/response shapes, "
        f"same table and column names, same message topics and payloads.\n"
        f"  - Do NOT finish with broken code. Every import must resolve, every "
        f"brace must close, every symbol you reference must exist or be "
        f"declared. A file that does not compile is a failed answer, however "
        f"well-written.\n"
        f"  - Use the target stack's own idioms throughout, not the source "
        f"stack's with the names changed.{banned_line}\n"
        f"  - Return the COMPLETE file. Not a diff, not a fragment, not an "
        f"excerpt with elisions.\n"
        f"{('' if not playbook else chr(10) + chr(10) + playbook)}"
        f"{raw}"
        f"{failed_excerpt}"
    )


async def _coder_apply_fix(
    transform_id: str,
    task_row: Dict[str, Any],
    detected_stack: dict,
    target_stack: dict,
    model: Optional[str],
    agent_name: str = "coder",
    on_stage: Optional[Callable[[str], Awaitable[None]]] = None,
) -> bool:
    """Load the current transformed content, invoke Coder + Verifier
    with the compile diagnostics attached, and overwrite the file.
    Returns True when the fix passed the (structural) verifier.

    iter-15.62.9 — `on_stage` is an optional async callback invoked
    right after the Coder returns and again once the Verifier returns,
    BEFORE the (potentially slow) next LLM round-trip starts. Each
    Coder/Verifier call can legitimately take 60-300s+ under real
    provider latency; with bounded concurrency the compile-fix loop
    used to touch `compile_fix_progress.updated_at` only once per file
    (at the very start of the fix), so a single slow file could easily
    exceed the FE/BE staleness threshold and be misreported as
    "stalled" / "interrupted by a server restart" even though the loop
    was still actively working. `on_stage` lets the caller re-touch the
    progress heartbeat between the two LLM calls instead of only at the
    file boundary.

    iter-15.62.6 — `agent_name` lets the compile-fix loop escalate to
    the "devops_expert" persona (see `_run_compile_fix_loop`) once the
    default Coder has demonstrably failed to make progress on a
    recurring build/infra-class failure.

    iter-16.x — When `task_row["is_new_file"]` is set, there is (by
    definition) no existing `transform_files` doc to load/update — the
    diagnosed root cause is a MISSING file (e.g. no Spring Boot main
    class), and the fix is to CREATE it. That path skips the
    edit-existing-file lookups entirely and inserts a brand-new
    `transform_files` doc once the Coder generates content."""
    target_path = task_row.get("target_path") or task_row.get("source_path")
    is_new_file = bool(task_row.get("is_new_file"))
    file_doc = None
    if not is_new_file:
        file_doc = await transform_files.find_one(
            {"transform_id": transform_id, "type": "transformed", "path": target_path}
        )
        if not file_doc:
            file_doc = await _resolve_failing_transform_file(transform_id, target_path)
        if not file_doc:
            return False
    current_content = (file_doc or {}).get("content") or ""
    original_content = ""
    if file_doc:
        try:
            orig_doc = await transform_files.find_one(
                {"transform_id": transform_id, "type": "source",
                 "path": file_doc.get("original_path") or task_row.get("source_path")},
                {"content": 1},
            )
            original_content = (orig_doc or {}).get("content", "") or current_content
        except Exception:
            original_content = current_content

    coder_task = dict(task_row)
    # `_run_coder` reads `notes` and uses `source_path` for the label.
    # Feed the CURRENT transformed file as the "source" — we want the
    # Coder to edit the already-transformed file, not restart from the
    # legacy source. For a brand-new file there is no "current" content
    # to feed — `notes` alone (set by `_planner_fix_tasks_from_errors`)
    # carries the generation instructions.
    coder_task["source_path"] = target_path

    # iter-20 — the upper escalation rungs. Both are the SAME machinery
    # with a different view of the problem, which is the whole point: a
    # third attempt that sees exactly what the second saw reproduces it.
    fix_input = current_content
    effective_agent = agent_name

    if agent_name == "devops_expert_raw":
        # Rung 2: the build systems specialist, now shown what the
        # compiler actually printed. Until iter-20 NOTHING in this loop
        # ever put raw build output in front of an agent — the Coder saw
        # only the Planner's prose summary of it, so a detail the Planner
        # elided (the offending plugin goal, a transitive conflict tree,
        # the second error under the first) was simply unavailable to
        # every fix attempt.
        effective_agent = "devops_expert"
        raw = _raw_build_log_excerpt(task_row.get("_compile_result") or {})
        if raw:
            coder_task["notes"] = (coder_task.get("notes") or "") + raw

    elif agent_name == "regenerator":
        # Rung 3: stop patching. Rungs 0-2 all edit a file that may be
        # beyond repair — a botched first transformation cannot always be
        # walked back one compiler error at a time. Regenerate from the
        # LEGACY ORIGINAL against the target playbook, which is the
        # programmatic form of the operator's own successful fallback.
        effective_agent = "regenerator"
        if original_content and original_content != current_content:
            fix_input = original_content
            coder_task["source_path"] = (
                (file_doc or {}).get("original_path")
                or task_row.get("source_path")
                or target_path
            )
        coder_task["notes"] = (
            (coder_task.get("notes") or "")
            + _regeneration_brief(
                target_path=target_path,
                failed_content=current_content,
                compile_result=task_row.get("_compile_result") or {},
                detected_stack=detected_stack,
                target_stack=target_stack,
                regenerating_from_original=bool(fix_input is original_content),
            )
        )

    updated = await _run_coder(
        transform_id, coder_task, fix_input,
        kb_ctx=(
            "(compile-fix pass — this file does not exist yet; CREATE it "
            "per the instructions in `notes`)" if is_new_file else
            "(compile-fix pass — use the compile diagnostics in `notes` to guide the edit)"
        ),
        detected_stack=detected_stack, target_stack=target_stack,
        model=model, envelope=None, agent_name=effective_agent,
    )
    if not updated or not updated.strip():
        await transformer_tasks.update_one(
            {"_id": task_row["_id"]},
            {"$set": {"status": "FAILED", "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        return False

    # iter-16.x — Final defence-in-depth scrub of Coder / DevOps output
    # before it lands in Mongo. `_run_coder`'s `_extract_code` already
    # strips fences on the primary path, but a nested javadoc example
    # using ``` fences or a truncated response can still leak a stray
    # ```java opener into `updated`. A fence marker on-disk in the
    # compile scratch dir is a guaranteed javac error ("illegal
    # character: '`'"), and once persisted it recurs on every subsequent
    # compile-fix iteration because the loop feeds the already-persisted
    # file back to the Coder as "current content". Scrub once here and
    # the loop can actually converge.
    updated = _scrub_code_fences(updated)

    if on_stage:
        try:
            await on_stage("verifying")
        except Exception:
            pass

    verify_result = await _run_verifier(
        transform_id, coder_task, original_content, updated, model,
    )
    verdict = verify_result.get("verdict", "REJECT")
    confidence = float(verify_result.get("confidence", 0)) / 100.0
    structural = verify_result.get("structural_check") or {}
    # iter-16.x — same 95%-confidence acceptance gate as the main
    # coder/verifier loop: pass/fail is the numeric score vs
    # VERIFIER_ACCEPT_THRESHOLD, not the LLM's verdict label.
    #
    # iter-16.x follow-up — EXCEPT for a brand-new synthetic scaffold
    # file (`is_new_file`, e.g. a missing Spring Boot `@SpringBootApplication`
    # main class). The Verifier's rubric is built around "does this
    # preserve the ORIGINAL file's business logic" — a criterion that is
    # meaningless here since there IS no original to compare against.
    # In practice the LLM verifier penalizes a correct, minimal bootstrap
    # class for "lacking business logic/entities" and caps it well below
    # 95%, which made the compile-fix loop stagnate forever on an
    # otherwise-correct fix. For `is_new_file` tasks, accept on the
    # deterministic structural check alone (syntactically valid, no
    # prose bleed, balanced braces, target-stack-appropriate) — the
    # thing that actually determines whether Maven/npm/etc. can compile
    # it — rather than the business-logic-preservation score.
    if is_new_file:
        _passing = structural.get("ok", True)
    else:
        _passing = float(verify_result.get("confidence", 0)) >= VERIFIER_ACCEPT_THRESHOLD
    compilable_flag = _passing and structural.get("ok", True)

    if is_new_file:
        await transform_files.update_one(
            {"transform_id": transform_id, "type": "transformed", "path": target_path},
            {"$set": {
                "transform_id": transform_id,
                "type": "transformed",
                "path": target_path,
                "original_path": target_path,
                "content": updated,
                "confidence": confidence,
                "verifier_result": verify_result,
                "compilable": compilable_flag,
                "final_verdict": verdict,
                "last_fix_iteration": task_row.get("fix_iteration"),
                "created_by": "compile_fix_new_file",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
    else:
        await transform_files.update_one(
            {"_id": file_doc["_id"]},
            {"$set": {
                "content": updated,
                "confidence": confidence,
                "verifier_result": verify_result,
                "compilable": compilable_flag,
                "final_verdict": verdict,
                "last_fix_iteration": task_row.get("fix_iteration"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
    await transformer_tasks.update_one(
        {"_id": task_row["_id"]},
        {"$set": {
            "status": "VERIFIED" if _passing else "VERIFY_FAILED",
            "confidence": confidence,
            "verifier_score": float(verify_result.get("confidence", 0)),
            "verifier_checks": verify_result,
            "compilable": compilable_flag,
            "final_verdict": verdict,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return compilable_flag


# iter-20 — Escalation ladder for the compile-fix loop.
#
# The loop used to have exactly two rungs: the Coder, then ONE switch to
# the DevOps Expert, then stop. That is the "it is not fixed in 2
# iterations" the operator reported, and the reason their own fallback --
# handing the folder to a chat model with a senior-developer brief --
# succeeded where the loop did not.
#
# Adding rungs only helps if each one can do something the previous could
# not. Swapping personas over an identical view of the problem is why the
# second attempt so often reproduced the first. So the rungs differ in
# what the agent SEES and what it is allowed to CHANGE:
#
#   0 coder        — the failing file plus the Planner's prose diagnosis.
#   1 devops_expert— same view, build-systems specialist prompt. Right for
#                    a manifest/toolchain problem, which is most of them.
#   2 devops_expert— PLUS the raw compiler output. Until now nothing in
#     +raw log       this loop ever showed an agent what the build actually
#                    printed; it only ever saw the Planner's summary of it.
#   3 regenerator  — the ORIGINAL source file, the target playbook and
#                    every accumulated error, rewriting from scratch.
#                    Rungs 0-2 all edit a file that may be unsalvageable;
#                    this is the one move that can abandon it.
#
# (agent_name, operator-facing label, why this rung is different)
_ESCALATION_LADDER: List[Tuple[str, str, str]] = [
    ("coder", "the Coder", "the default per-file transformation agent"),
    ("devops_expert", "the DevOps Expert",
     "a build-systems specialist, for toolchain, dependency and plugin problems"),
    ("devops_expert_raw", "the DevOps Expert with the raw build log",
     "the same specialist, now shown exactly what the compiler printed rather "
     "than a summary of it"),
    ("regenerator", "a full regeneration from the original source",
     "the file is rewritten from the legacy original against the target "
     "stack's conventions, rather than patched further"),
]

# Default ceiling for the compile-fix loop. iter-15.62.4 made this
# unbounded-until-stagnant, which sounds generous but in practice ended
# runs at two: the stagnation guard fires the moment a fix changes
# nothing. With a four-rung ladder the loop needs room to actually climb
# it, and the operator asked for five attempts before regeneration takes
# over. Still overridable by LAMA_COMPILE_FIX_MAX_ITER.
_COMPILE_FIX_DEFAULT_MAX_ITER = 5


async def _run_compile_fix_loop(
    transform_id: str,
    build_tools_map: Dict[str, str],
    model: Optional[str],
    max_iterations: Optional[int] = None,
) -> Dict[str, Any]:
    """Compile → Planner → Coder loop. Returns the FINAL compile result
    plus an `attempts` list describing each iteration.

    iter-15.62.4 — No hardcoded iteration cap by default: the loop now
    runs until the build actually passes, hits a genuine infra block
    (`_is_infra_blocked`), or — the one automatic stop condition that
    ISN'T an arbitrary count — makes zero forward progress (the exact
    same set of build failures recurs after a fix round, meaning that
    round of Coder edits had no effect). An explicit numeric cap is
    still honoured if the caller passes `max_iterations` or sets
    `LAMA_COMPILE_FIX_MAX_ITER` — but that is now opt-in, not a baked-in
    default of 3."""
    if max_iterations is None:
        env_cap = os.environ.get("LAMA_COMPILE_FIX_MAX_ITER", "").strip()
        if env_cap:
            try:
                max_iterations = max(1, int(env_cap))
            except ValueError:
                max_iterations = _COMPILE_FIX_DEFAULT_MAX_ITER
        else:
            # iter-20 — a real default. "Unbounded until stagnant" ended
            # runs at two in practice, and gave the four-rung escalation
            # ladder no room to climb.
            max_iterations = _COMPILE_FIX_DEFAULT_MAX_ITER
    elif max_iterations <= 0:
        max_iterations = None

    tx = await transformations.find_one({"_id": transform_id}) or {}
    # iter-20 — `detected_stack` is not a field on this document; the
    # detected stack is stored as `source_stack` (see create_transformation
    # / create_transformation_v2). This read has always produced {}, so
    # every fix-loop Coder call described its own source stack as "{}"
    # and the structural residue gate had nothing to match against.
    source_stack = tx.get("source_stack") or {}
    # Prefer the per-component dict over the collapsed string. On a v2 job
    # `target_stack` is whichever of runtime/backend/frontend/database was
    # set first — typically "java-21", which tells the Coder the language
    # but not the framework it is migrating TO. `transforms` carries the
    # whole picture ({"backend": "spring-boot-3", "database": "postgresql"}),
    # which is what the main pipeline already passes.
    target_stack = tx.get("transforms") or tx.get("target_stack") or {}

    attempts: List[Dict[str, Any]] = []
    final_compile: Dict[str, Any] = {}
    prev_failure_sig: Optional[Tuple] = None
    # iter-15.62.6 — Escalation state: which agent persona is currently
    # executing FIX tasks. Starts as the default Coder; if a full fix
    # round makes ZERO difference (stagnation guard fires), the loop
    # escalates ONCE to a "devops_expert" persona (build/infra-focused
    # prompt) rather than giving up immediately — this is exactly the
    # class of failure (release mismatches, dependency/version/plugin
    # config problems) a real DevOps engineer would be pulled in for.
    # Only if the SAME failure recurs a second time AFTER escalation do
    # we finally stop as genuinely stuck.
    active_agent_name = "coder"
    devops_escalated = False
    escalation_rung = 0

    async def _emit_progress(state: Dict[str, Any]):
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        await transformations.update_one(
            {"_id": transform_id}, {"$set": {"compile_fix_progress": state}}
        )
        try:
            _emit_log(transform_id, "info", state.get("message", ""),
                      agent="compiler",
                      phase=state.get("phase", "compile_fix"),
                      **{k: v for k, v in state.items() if k not in ("message", "phase")})
        except Exception:
            pass

    iteration = 0
    while True:
        iteration += 1
        if max_iterations and iteration > max_iterations:
            await _emit_progress({
                "phase": "exhausted", "iteration": max_iterations,
                "max_iterations": max_iterations,
                "message": f"Reached the configured fix-loop cap of {max_iterations} iterations without a green build.",
            })
            break

        # ── Build the workspace from the latest transformed files ─────
        cursor = transform_files.find(
            {"transform_id": transform_id, "type": "transformed"},
            {"path": 1, "content": 1},
        )
        all_files = [{"path": d.get("path", ""), "content": d.get("content", "")}
                     async for d in cursor]
        if not all_files:
            final_compile = {
                "compilation_ready": False, "overall_score": 0,
                "summary": "No transformed files to compile.",
                "components": [], "mode": "native",
            }
            break

        await _emit_progress({
            "phase": "compiling", "iteration": iteration,
            "max_iterations": max_iterations,
            "message": f"Iteration {iteration}"
                       + (f"/{max_iterations}" if max_iterations else "")
                       + f": compiling {len(all_files)} file(s)",
        })

        workspace_root = _write_transformed_workspace(all_files)
        try:
            compile_result = await _run_compiler(transform_id, workspace_root, build_tools_map)
        except Exception as _exc:
            compile_result = {
                "compilation_ready": False, "overall_score": 0,
                "summary": f"Compile phase crashed: {_exc}",
                "components": [], "mode": "native",
            }
        final_compile = compile_result

        if compile_result.get("compilation_ready"):
            attempts.append({
                "iteration": iteration, "status": "passed",
                "summary": compile_result.get("summary", ""),
                "fixed_files": [],
                "acting_agent": active_agent_name,
            })
            await _emit_progress({
                "phase": "passed", "iteration": iteration,
                "max_iterations": max_iterations,
                "message": f"Build passed on iteration {iteration}"
                           + (" (fixed by the DevOps Expert agent)" if devops_escalated else ""),
            })
            shutil.rmtree(workspace_root, ignore_errors=True)
            break

        # ── Stagnation guard — NOT a hardcoded iteration cap, a progress
        # check: if the exact same set of components/tools/reasons failed
        # again after a full fix round, that round's edits had zero effect.
        # iter-15.62.6 — instead of stopping immediately, escalate ONCE to
        # a "devops_expert" persona (a real DevOps/build engineer would be
        # the natural next escalation for a build/infra-class failure the
        # default Coder couldn't resolve) and give it a shot before truly
        # giving up.
        failure_sig = _compile_failure_signature(compile_result)
        if failure_sig and failure_sig == prev_failure_sig:
            # iter-20 — climb one rung of `_ESCALATION_LADDER` rather than
            # stopping after the single devops_expert attempt. The operator
            # reported the loop "not fixed in 2 iterations", and two is
            # exactly what the old binary flag allowed: coder, then
            # devops_expert, then stop. The rungs differ in what they SEE
            # and what they may CHANGE, not merely in which prompt is used
            # — repeating the same view with a different persona is what
            # made the third attempt pointless.
            if escalation_rung + 1 < len(_ESCALATION_LADDER):
                escalation_rung += 1
                active_agent_name, _rung_label, _rung_why = _ESCALATION_LADDER[escalation_rung]
                devops_escalated = True
                # Not appended to `attempts` — it's not a compile iteration
                # by itself, just a mid-iteration persona switch. The
                # subsequent "fixes_applied" entry for THIS iteration is
                # tagged with the rung so the switch is still fully
                # auditable without inflating `iterations_used`.
                await _emit_progress({
                    "phase": "escalated_devops", "iteration": iteration,
                    "max_iterations": max_iterations,
                    "escalation_rung": escalation_rung,
                    "escalation_label": _rung_label,
                    "message": (
                        f"The same build failure recurred after the previous fix. "
                        f"Escalating to {_rung_label} — {_rung_why}"
                    ),
                })
                # Fall through — do NOT break. Re-diagnose and re-fix THIS
                # SAME iteration's failure at the new rung.
            else:
                attempts.append({
                    "iteration": iteration, "status": "stagnant",
                    "summary": compile_result.get("summary", ""),
                    "fixed_files": [],
                    "escalation_rung": escalation_rung,
                })
                await _emit_progress({
                    "phase": "stagnant", "iteration": iteration,
                    "max_iterations": max_iterations,
                    "escalation_rung": escalation_rung,
                    "message": (
                        "Every escalation rung — Coder, DevOps Expert, DevOps Expert "
                        "with the raw build log, and a full regeneration from the "
                        "original source — produced the identical build failure. "
                        "Stopping; the remaining errors are reported against the "
                        "generated files."
                    ),
                })
                shutil.rmtree(workspace_root, ignore_errors=True)
                break
        prev_failure_sig = failure_sig

        # ── Parse diagnostics + generate FIX tasks via Planner ─────────
        errors = _parse_compile_errors(compile_result, workspace_root)
        if not errors:
            # iter-15.62 — Previously ANY failure without a regex-parseable
            # `file:line` diagnostic aborted the whole fix loop as
            # "unfixable" on iteration 1 — this is exactly what silently
            # blocked Maven dependency/plugin failures and (before the
            # runtime-image fix) "toolchain missing" errors from ever
            # reaching Planner/Coder/Verifier. Now: infra-only failures
            # (genuinely un-fixable by editing anything) terminate
            # immediately with a clear message; everything else gets a
            # real Planner (LLM) diagnosis pass before giving up.
            infra_reason = _is_infra_blocked(compile_result)
            if infra_reason:
                attempts.append({
                    "iteration": iteration, "status": "infra_blocked",
                    "summary": compile_result.get("summary", ""),
                    "root_cause": infra_reason,
                    "fixed_files": [],
                })
                await _emit_progress({
                    "phase": "infra_blocked", "iteration": iteration,
                    "max_iterations": max_iterations,
                    "message": f"Build cannot be fixed by editing code — infrastructure issue: {infra_reason}",
                })
                shutil.rmtree(workspace_root, ignore_errors=True)
                break

            await _emit_progress({
                "phase": "diagnosing", "iteration": iteration,
                "max_iterations": max_iterations,
                "message": "No per-file diagnostics parsed — Planner is diagnosing the raw build failure",
            })
            diag = await _llm_diagnose_generic_failure(transform_id, compile_result, iteration, model)
            errors = diag.get("error_groups") or []
            root_cause = diag.get("root_cause") or ""
            if diag.get("blocked") or not errors:
                attempts.append({
                    "iteration": iteration, "status": "unfixable",
                    "summary": compile_result.get("summary", ""),
                    "root_cause": root_cause,
                    "fixed_files": [],
                })
                await _emit_progress({
                    "phase": "unfixable", "iteration": iteration,
                    "max_iterations": max_iterations,
                    "message": f"Build failed and no fixable cause could be identified: {root_cause or 'no diagnostics parsed'}",
                })
                shutil.rmtree(workspace_root, ignore_errors=True)
                break

        await _emit_progress({
            "phase": "planning_fixes", "iteration": iteration,
            "max_iterations": max_iterations,
            "message": f"{len(errors)} failing file(s) → planning fix tasks",
            "failing_files": [e["path"] for e in errors][:20],
        })

        fix_tasks = await _planner_fix_tasks_from_errors(transform_id, errors, iteration)
        if not fix_tasks:
            attempts.append({
                "iteration": iteration, "status": "no_fix_tasks",
                "summary": compile_result.get("summary", ""),
                "fixed_files": [],
            })
            shutil.rmtree(workspace_root, ignore_errors=True)
            break

        # iter-20 — carry this round's compile result on each task so the
        # upper escalation rungs can read the RAW build output. Attached
        # under a leading underscore and never persisted: the task rows in
        # `transformer_tasks` are written by the Planner, and a 200 KB log
        # does not belong in one.
        for _t in fix_tasks:
            _t["_compile_result"] = compile_result

        # ── Coder wave (bounded concurrency) ──────────────────────────
        try:
            fix_concurrency = int(os.environ.get("LAMA_COMPILE_FIX_CONCURRENCY", "3") or "3")
        except ValueError:
            fix_concurrency = 3
        sem = asyncio.Semaphore(max(1, min(fix_concurrency, 8)))
        results: List[Dict[str, Any]] = []

        async def _run_one(t: Dict[str, Any]):
            async with sem:
                agent_label = AGENT_LABELS.get(active_agent_name, active_agent_name)
                target_path = t.get("target_path")

                async def _touch(stage: str):
                    # iter-15.62.9 — re-touch the heartbeat between the
                    # Coder and Verifier LLM calls so a legitimately slow
                    # (but still-alive) fix doesn't trip the staleness
                    # detector. See `_coder_apply_fix.on_stage` docstring.
                    label = "Verifying" if stage == "verifying" else "Fixing"
                    await _emit_progress({
                        "phase": "fixing", "iteration": iteration,
                        "max_iterations": max_iterations,
                        "message": f"[{agent_label}] {label} {target_path}",
                        "current_file": target_path,
                        "acting_agent": active_agent_name,
                    })

                await _touch("fixing")
                try:
                    ok = await _coder_apply_fix(
                        transform_id, t, source_stack, target_stack, model,
                        agent_name=active_agent_name, on_stage=_touch,
                    )
                except Exception as fix_err:
                    print(f"[transformer:{transform_id}] fix_error {t.get('target_path')}: {fix_err}")
                    ok = False
                results.append({"path": t.get("target_path"), "fixed": bool(ok)})

        await asyncio.gather(*[_run_one(t) for t in fix_tasks])
        attempts.append({
            "iteration": iteration,
            "status": "fixes_applied",
            "summary": compile_result.get("summary", ""),
            "fixed_files": results,
            "failing_files": [e["path"] for e in errors],
            "acting_agent": active_agent_name,
            "escalated_to_devops": devops_escalated,
        })
        shutil.rmtree(workspace_root, ignore_errors=True)
        # loop → next iteration re-materialises the workspace with the
        # freshly-updated `transform_files` content.

    final_compile["attempts"] = attempts
    final_compile["iterations_used"] = len(attempts)
    return final_compile


# ═══════════════════════════════════════════════════════════════════
# iter-19 — DevOps → Planner remediation loop
# ═══════════════════════════════════════════════════════════════════
#
# The DevOps audit used to be a dead end. It ran at 97%, AFTER the run's
# status and result had already been decided, wrote its findings to a warn
# line, and returned. `production_ready: false` changed nothing. So a
# transformation could report `completed` while shipping a pom Maven
# cannot resolve — which is exactly the "the build remains incomplete"
# symptom.
#
# The remediation loop closes it: a finding becomes an error group, the
# Planner turns that into a concrete FIX task, and the DevOps Expert
# persona applies it — reusing, unchanged, the same
# `_planner_fix_tasks_from_errors` → `_coder_apply_fix` machinery the
# compile-fix loop already runs. Nothing new is invented for the repair
# path; only the trigger is new.
#
# Bounded, because a loop that cannot make progress must stop rather than
# burn tokens: a hard round cap AND a stagnation guard on the finding set
# (the same idea as `_compile_failure_signature`, applied to audit
# findings instead of compiler output).

_DEVOPS_REMEDIABLE_SEVERITIES = ("critical", "major")


def _devops_findings_signature(findings: List[Dict[str, Any]]) -> Tuple:
    """Stable identity for a set of audit findings.

    Two rounds producing the same signature means the repair changed
    nothing measurable — the same stop condition the compile-fix loop uses,
    and a better one than a bare round counter because it stops on the
    first wasted round rather than the Nth.
    """
    return tuple(sorted(
        (str(f.get("manifest", "")), str(f.get("severity", "")), str(f.get("issue", ""))[:200])
        for f in (findings or []) if isinstance(f, dict)
    ))


def _devops_findings_to_error_groups(
    findings: List[Dict[str, Any]],
    build_tools_map: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Convert audit findings into the error-group shape the Planner
    consumes, one group per manifest.

    Groups per FILE rather than per finding so the Coder sees every
    problem with a manifest in one task and can fix them together — a
    version-less dependency and an undefined property in the same pom are
    one edit, and splitting them into two tasks would have the second
    overwrite the first.
    """
    by_manifest: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        if (f.get("severity") or "").lower() not in _DEVOPS_REMEDIABLE_SEVERITIES:
            continue
        path = (f.get("manifest") or "").strip()
        if not path:
            # A finding with no manifest (e.g. "no build manifest found")
            # names no file to edit, so there is nothing to route.
            continue
        by_manifest.setdefault(path, []).append(f)

    groups: List[Dict[str, Any]] = []
    for path, items in by_manifest.items():
        component = ""
        tool = ""
        for comp, t in (build_tools_map or {}).items():
            if path.replace("\\", "/").startswith(f"{comp}/") or path == f"{comp}/pom.xml":
                component, tool = comp, t
                break
        lines: List[Tuple[int, str]] = []
        for f in items[:8]:
            fix = (f.get("fix") or "").strip()
            lines.append((0, (
                f"[{(f.get('severity') or '').upper()}] {f.get('issue', '')}"
                + (f" SUGGESTED FIX: {fix}" if fix else "")
            )))
        groups.append({
            "path": path,
            "component": component or None,
            "tool": tool or None,
            # Reuses the compile-fix loop's build-manifest phrasing, which
            # already tells the Coder to edit a manifest rather than source
            # and not to churn unrelated dependencies.
            "kind": "build_config",
            "lines": lines,
        })
    return groups


async def _run_devops_remediation_loop(
    transform_id: str,
    compile_result: Dict[str, Any],
    build_tools_map: Dict[str, str],
    model: Optional[str],
    max_rounds: Optional[int] = None,
) -> Dict[str, Any]:
    """Audit → Planner → DevOps Expert → recompile, until the manifests are
    production-ready, nothing changes, or the round cap is reached.

    Returns the FINAL audit plus a `rounds` trail, and the (possibly
    re-run) compile result so the caller's `compile_green` reflects any
    repair made here.
    """
    if max_rounds is None:
        try:
            max_rounds = max(1, int(os.environ.get("LAMA_DEVOPS_REPLAN_MAX_ROUNDS", "2") or 2))
        except ValueError:
            max_rounds = 2

    tx = await transformations.find_one({"_id": transform_id}) or {}
    # iter-20 — `detected_stack` is not a field on this document; the
    # detected stack is stored as `source_stack` (see create_transformation
    # / create_transformation_v2). This read has always produced {}, so
    # every fix-loop Coder call described its own source stack as "{}"
    # and the structural residue gate had nothing to match against.
    source_stack = tx.get("source_stack") or {}
    # Prefer the per-component dict over the collapsed string. On a v2 job
    # `target_stack` is whichever of runtime/backend/frontend/database was
    # set first — typically "java-21", which tells the Coder the language
    # but not the framework it is migrating TO. `transforms` carries the
    # whole picture ({"backend": "spring-boot-3", "database": "postgresql"}),
    # which is what the main pipeline already passes.
    target_stack = tx.get("transforms") or tx.get("target_stack") or {}

    rounds: List[Dict[str, Any]] = []
    audit = await _run_devops_dependency_check(transform_id, compile_result, model)
    prev_sig: Optional[Tuple] = None

    for rnd in range(1, max_rounds + 1):
        if audit.get("production_ready"):
            break

        findings = audit.get("findings") or []
        groups = _devops_findings_to_error_groups(findings, build_tools_map)
        if not groups:
            # Nothing actionable — e.g. every finding is advisory, or the
            # only finding names no file. Reporting that honestly beats
            # spending a round on tasks that cannot be written.
            rounds.append({
                "round": rnd, "status": "no_actionable_findings",
                "findings": len(findings),
                "summary": audit.get("summary", ""),
            })
            break

        sig = _devops_findings_signature(findings)
        if prev_sig is not None and sig == prev_sig:
            rounds.append({
                "round": rnd, "status": "stagnant",
                "findings": len(findings),
                "summary": "The previous remediation round left the audit findings unchanged.",
            })
            _emit_log(
                transform_id, "warn",
                "DevOps remediation made no difference — the identical findings recurred. "
                "Stopping rather than spending another round.",
                agent="devops_expert", phase="devops-remediation",
            )
            break
        prev_sig = sig

        await _update_progress(
            transform_id,
            status="running",
            phase="devops_remediation",
            phase_label=f"DevOps: Planning manifest repairs (round {rnd}/{max_rounds})",
            progress_pct=95,
        )
        _emit_log(
            transform_id, "info",
            f"DevOps found {len(findings)} issue(s) across {len(groups)} manifest(s) — "
            f"handing them to the Planner for repair (round {rnd}/{max_rounds})",
            agent="devops_expert", phase="devops-remediation",
        )

        # The Planner decides HOW to fix each one; iteration 900+ keeps
        # these tasks distinguishable from the compile-fix loop's in
        # `transformer_tasks`.
        fix_tasks = await _planner_fix_tasks_from_errors(transform_id, groups, 900 + rnd)
        if not fix_tasks:
            rounds.append({
                "round": rnd, "status": "no_fix_tasks",
                "findings": len(findings),
                "summary": "The Planner produced no fix tasks for these findings.",
            })
            break

        await _update_progress(
            transform_id,
            status="running",
            phase="devops_remediation",
            phase_label=f"DevOps Expert: Repairing {len(fix_tasks)} manifest(s)",
            progress_pct=96,
        )

        try:
            _conc = max(1, min(int(os.environ.get("LAMA_COMPILE_FIX_CONCURRENCY", "3") or "3"), 8))
        except ValueError:
            _conc = 3
        sem = asyncio.Semaphore(_conc)
        applied: List[Dict[str, Any]] = []

        async def _apply(task: Dict[str, Any]):
            async with sem:
                try:
                    ok = await _coder_apply_fix(
                        transform_id, task, source_stack, target_stack, model,
                        # The DevOps Expert persona, not the default Coder —
                        # a manifest is its domain, and this is the same
                        # escalation the compile-fix loop performs.
                        agent_name="devops_expert",
                    )
                except Exception as exc:  # noqa: BLE001 — one bad manifest must not kill the round
                    log.warning("DevOps remediation failed for %s: %s", task.get("target_path"), exc)
                    ok = False
                applied.append({"path": task.get("target_path"), "fixed": bool(ok)})

        await asyncio.gather(*[_apply(t) for t in fix_tasks])

        # A manifest edit is only real if the build still stands, so
        # recompile before re-auditing. Without this the second audit would
        # grade a pom that may no longer compile.
        if build_tools_map:
            await _update_progress(
                transform_id,
                status="running",
                phase="devops_remediation",
                phase_label="DevOps: Recompiling after manifest repairs",
                progress_pct=96,
            )
            try:
                _cur = transform_files.find(
                    {"transform_id": transform_id, "type": "transformed"},
                    {"path": 1, "content": 1},
                )
                _files = [{"path": d.get("path", ""), "content": d.get("content", "")} async for d in _cur]
                if _files:
                    _ws = _write_transformed_workspace(_files)
                    try:
                        compile_result = await _run_compiler(transform_id, _ws, build_tools_map)
                    finally:
                        shutil.rmtree(_ws, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("DevOps remediation recompile failed for %s: %s", transform_id, exc)

        audit = await _run_devops_dependency_check(transform_id, compile_result, model)
        rounds.append({
            "round": rnd,
            "status": "remediated" if audit.get("production_ready") else "still_failing",
            "findings_before": len(findings),
            "findings_after": len(audit.get("findings") or []),
            "manifests_touched": applied,
            "compile_green": bool(compile_result.get("compilation_ready")),
        })

    audit["remediation_rounds"] = rounds
    audit["remediation_rounds_used"] = len(rounds)
    return {"audit": audit, "compile_result": compile_result}


# ═══════════════════════════════════════════════════════════════════
# iter-15.45 — Three-tier test generator + coverage runner
# ═══════════════════════════════════════════════════════════════════
#
# The pipeline used to hand the operator freshly-transformed code
# with no tests. Anyone who wanted regression safety had to hand-write
# their own suite AFTER the fact. iter-15.45 adds three tiers of
# auto-generated tests written INTO the transformed workspace so the
# `_run_compiler` phase can then optionally run them and report
# coverage:
#
#   * business     — Gherkin/BDD scenarios that describe the user-
#                    visible behaviour of each envelope in plain
#                    business language (one file per envelope group).
#   * api          — Framework-native tests for every envelope's HTTP
#                    endpoint (MockMvc/WebTestClient for Spring, pytest
#                    + httpx for FastAPI, jest + supertest for Express).
#   * integration  — Cross-envelope happy-path suites that exercise
#                    the traceability chain end-to-end.
#
# Tests are stored in the `transform_files` collection with `type="test"`
# and `test_tier` set — this lets the scope-aware download filter
# them cleanly and lets the coverage runner know what to invoke.
#
# The coverage runner uses the same tool the operator picked at
# step 1 but with the tool-specific coverage argv (`mvn verify`,
# `npm test -- --coverage`, `pytest --cov`). If the coverage tool
# isn't on PATH we cleanly skip and report reason.


TEST_TIER_ORDER: Tuple[str, ...] = ("business", "api", "integration")


COVERAGE_COMMANDS: Dict[str, Dict[str, Any]] = {
    # Coverage argv per tool. `report_paths` is a list of glob-style
    # paths (relative to the manifest dir) that the runner reads to
    # extract a coverage percentage.
    "maven":  {"argv": ["mvn", "-B", "-Dstyle.color=never", "-q", "verify"],                    "report_paths": ["target/site/jacoco/jacoco.csv"], "label": "mvn verify + jacoco"},
    "gradle": {"argv": ["gradle", "--no-daemon", "-q", "test", "jacocoTestReport"], "report_paths": ["build/reports/jacoco/test/jacocoTestReport.csv"], "label": "gradle test + jacoco"},
    "npm":    {"argv": ["npm", "test", "--", "--coverage", "--watchAll=false"],   "report_paths": ["coverage/coverage-summary.json"], "label": "npm test --coverage"},
    "yarn":   {"argv": ["yarn", "--silent", "test", "--coverage", "--watchAll=false"], "report_paths": ["coverage/coverage-summary.json"], "label": "yarn test --coverage"},
    "pip":    {"argv": ["python3", "-m", "pytest", "--cov", "--cov-report=json"], "report_paths": ["coverage.json"], "label": "pytest --cov"},
    "poetry": {"argv": ["poetry", "run", "pytest", "--cov", "--cov-report=json"], "report_paths": ["coverage.json"], "label": "poetry run pytest --cov"},
    "dotnet": {"argv": ["dotnet", "test", "--nologo", "--collect:XPlat Code Coverage"], "report_paths": ["TestResults/**/coverage.cobertura.xml"], "label": "dotnet test + coverlet"},
    "go":     {"argv": ["go", "test", "-cover", "./..."],                "report_paths": [], "label": "go test -cover"},
}


def _target_framework_for_tests(target_stack: Dict[str, Any], tier: str) -> str:
    """Pick a test-framework hint based on the target stack + tier.
    Free-form string only used to seed the LLM prompt — the Coder-
    style call figures out the actual boilerplate."""
    backend = str((target_stack or {}).get("backend") or "").lower()
    frontend = str((target_stack or {}).get("frontend") or "").lower()
    if tier == "business":
        return "Gherkin (.feature files) — business language, no framework-specific imports"
    if "spring" in backend:
        if tier == "api":
            return "JUnit 5 + Spring Boot Test + MockMvc (or WebTestClient for reactive)"
        return "JUnit 5 + Spring Boot Test + Testcontainers"
    if "fastapi" in backend or "python" in backend:
        if tier == "api":
            return "pytest + httpx.AsyncClient + FastAPI TestClient"
        return "pytest + httpx + docker-compose fixture"
    if "express" in backend or "node" in backend:
        return "jest + supertest"
    if "dotnet" in backend or "asp.net" in backend:
        return "xUnit + WebApplicationFactory"
    if "react" in frontend and tier == "api":
        return "vitest + msw"
    return "the canonical unit-test framework for the target stack"


def _test_path_for_envelope(
    tier: str,
    target_stack: Dict[str, Any],
    envelope: Dict[str, Any],
    idx: int,
) -> str:
    """Derive a repo-relative test file path for one envelope.
    Chosen to line up with the manifest layout the compiler expects
    so `_run_compiler` picks the tests up automatically."""
    backend = str((target_stack or {}).get("backend") or "").lower()
    slug = re.sub(r"[^a-zA-Z0-9]+", "_",
                  str(envelope.get("endpoint") or f"case{idx}")).strip("_") or f"case{idx}"
    if tier == "business":
        return f"tests/business/{slug}.feature"
    if "spring" in backend:
        stem = "".join(w.capitalize() for w in slug.split("_")) or f"Case{idx}"
        subdir = "api" if tier == "api" else "integration"
        return f"src/test/java/tests/{subdir}/{stem}Test.java"
    if "fastapi" in backend or "python" in backend:
        return f"tests/{tier}/test_{slug}.py"
    if "express" in backend or "node" in backend:
        return f"tests/{tier}/{slug}.test.js"
    if "dotnet" in backend:
        return f"tests/{tier.capitalize()}/{slug}.cs"
    return f"tests/{tier}/{slug}.txt"


async def _generate_one_test_file(
    transform_id: str,
    tier: str,
    target_stack: Dict[str, Any],
    envelope: Dict[str, Any],
    model: str,
) -> Optional[Dict[str, str]]:
    """Ask fabric_call for a single test file's contents. Returns
    {path, content, envelope_id, tier} on success, None on
    empty/blank LLM output. Never raises — errors are swallowed to
    the log so one bad envelope doesn't kill the whole test-gen
    phase."""
    try:
        framework_hint = _target_framework_for_tests(target_stack, tier)
        system_prompt = (
            f"You are a senior test engineer. Emit ONE COMPLETE, "
            f"COMPILABLE test file only — no prose, no fences, no "
            f"placeholder TODOs. Use {framework_hint}. Cover both the "
            f"happy path AND at least one negative/edge case. "
            f"Output ONLY the file contents."
        )
        user_prompt = (
            f"Generate a {tier.upper()} test for this endpoint.\n\n"
            f"===== TARGET STACK =====\n{json.dumps(target_stack)}\n\n"
            f"===== ENDPOINT ENVELOPE =====\n"
            f"{json.dumps({k: envelope.get(k) for k in ('endpoint', 'http_method', 'controller', 'service', 'repository', 'tables', 'business_logic', 'business_rules') if envelope.get(k)}, ensure_ascii=False)}"
        )
        response = await fabric_call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            agent_key="tools.transformer.tester",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.2,
            max_tokens=4000,
        )
        content = response.get("content", "") if isinstance(response, dict) else str(response)
        # iter-16.x — Replaced the inline `^```lang\n` / `\n```$` regex
        # pair with the shared `_scrub_code_fences` helper. The old pair
        # only trimmed a leading and trailing fence and missed stray
        # full-line ``` markers left mid-file when the LLM emitted a
        # nested javadoc example — the same failure mode we saw on
        # Coder output. Use the shared scrubber so tester-generated
        # test files stay compilable too.
        content = _scrub_code_fences(content.strip()).strip()
        if not content:
            return None
        return {
            "envelope_id": envelope.get("envelope_id"),
            "tier": tier,
            "content": content,
        }
    except Exception as e:
        _emit_log(transform_id, "warn",
                  f"[iter-15.45] Test-gen ({tier}) skipped one envelope: {e}")
        return None


async def _run_test_generator(
    transform_id: str,
    envelopes: List[Dict[str, Any]],
    target_stack: Dict[str, Any],
    model: Optional[str] = None,
    tiers: Optional[List[str]] = None,
    max_envelopes_per_tier: int = 40,
) -> Dict[str, Any]:
    """iter-15.45 — Generate business + api + integration tests for the
    transformed project, one file per envelope per tier, capped so a
    huge project doesn't drain the LLM budget in a single call.

    Persists each generated file into `transform_files` with
    `type="test"`, `test_tier=<tier>`, `envelope_id=<eid>` so the
    scope-aware download endpoint can filter them cleanly.
    """
    run_id = await _log_agent_run(
        transform_id, "tester", "test_gen",
        input_summary=f"Generating tests for {len(envelopes)} envelope(s)",
    )
    t0 = datetime.now(timezone.utc)
    tiers = list(tiers or TEST_TIER_ORDER)
    model = await _get_effective_model(transform_id, "tester", model)

    # Purge any previous auto-generated tests so re-runs don't leave
    # stale files in the ZIP.
    await transform_files.delete_many({"transform_id": transform_id, "type": "test"})

    # iter-15.55 — Live progress state so the FE Activity Rail can show
    # what the (slow) Tester agent is actually doing. Persisted on the
    # transformation doc and surfaced via /status. `in_flight` is the
    # set of tier+envelope currently being generated; `recent` is the
    # last N completed test paths (rolling tail).
    total_planned = sum(min(len(envelopes), max_envelopes_per_tier) for _ in tiers)
    tester_progress_lock = asyncio.Lock()
    tester_progress: Dict[str, Any] = {
        "done": 0,
        "total": total_planned,
        "per_tier": {t: {"done": 0, "total": min(len(envelopes), max_envelopes_per_tier)} for t in tiers},
        "in_flight": [],
        "recent": [],
        "started_at": t0.isoformat(),
        "updated_at": t0.isoformat(),
        "model": model,
        "envelopes_considered": len(envelopes),
    }

    async def _persist_progress() -> None:
        tester_progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await transformations.update_one(
                {"_id": transform_id},
                {"$set": {"tester_progress": dict(tester_progress)}},
            )
        except Exception:  # noqa: BLE001 — never break the phase
            pass

    await _persist_progress()
    _emit_log(
        transform_id, "info",
        f"Tester — generating {total_planned} test file(s) "
        f"across {len(tiers)} tier(s) ({', '.join(tiers)}) using model {model}",
        agent="tester", phase="start",
    )

    per_tier: Dict[str, int] = {t: 0 for t in tiers}
    # Concurrency: 4 tests in-flight is enough to keep a cloud endpoint busy
    # without stampeding its rate-limit. A local Ollama serves far fewer
    # concurrent generations and QUEUES the rest, and a queued request burns
    # its own timeout while it waits — so over-fanning a local engine turns
    # throughput into 600s timeouts. Mirrors the same treatment the Coder
    # wave already gets. An explicit env value always wins.
    _tg_env = os.environ.get("LAMA_TESTGEN_CONCURRENCY", "")
    if _tg_env.strip():
        try:
            _tg_conc = max(1, int(_tg_env))
        except ValueError:
            _tg_conc = 4
    else:
        try:
            from llm import active_default_provider_is_local
            _tg_conc = 2 if await active_default_provider_is_local() else 4
        except Exception:  # noqa: BLE001 — pacing must never break the phase
            _tg_conc = 4
    sem = asyncio.Semaphore(_tg_conc)

    async def _emit(tier: str, env: Dict[str, Any], idx: int) -> None:
        async with sem:
            slot_key = f"{tier}:{env.get('envelope_id') or env.get('endpoint') or idx}"
            slot = {
                "key": slot_key,
                "tier": tier,
                "envelope": env.get("endpoint") or env.get("envelope_id") or f"case{idx}",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            async with tester_progress_lock:
                tester_progress["in_flight"] = [
                    s for s in tester_progress["in_flight"] if s.get("key") != slot_key
                ] + [slot]
                await _persist_progress()
            _emit_log(
                transform_id, "info",
                f"Tester — {tier.upper()} test for {slot['envelope']}",
                agent="tester", phase="in_flight", tier=tier,
            )
            try:
                row = await _generate_one_test_file(
                    transform_id, tier, target_stack, env, model,
                )
            finally:
                async with tester_progress_lock:
                    tester_progress["in_flight"] = [
                        s for s in tester_progress["in_flight"] if s.get("key") != slot_key
                    ]
            if not row:
                async with tester_progress_lock:
                    await _persist_progress()
                return
            path = _test_path_for_envelope(tier, target_stack, env, idx)
            await transform_files.insert_one({
                "transform_id": transform_id,
                "type": "test",
                "test_tier": tier,
                "envelope_id": row["envelope_id"],
                "path": path,
                "content": row["content"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            per_tier[tier] = per_tier.get(tier, 0) + 1
            async with tester_progress_lock:
                tester_progress["done"] = tester_progress.get("done", 0) + 1
                tp = tester_progress["per_tier"].get(tier)
                if tp:
                    tp["done"] = tp.get("done", 0) + 1
                tail = tester_progress.get("recent") or []
                tail.append({
                    "path": path,
                    "tier": tier,
                    "envelope": slot["envelope"],
                    "at": datetime.now(timezone.utc).isoformat(),
                })
                tester_progress["recent"] = tail[-20:]
                await _persist_progress()
            _emit_log(
                transform_id, "info",
                f"Tester — [{tester_progress['done']}/{tester_progress['total']}] "
                f"{tier}: {path}",
                agent="tester", phase="file_done", tier=tier, path=path,
            )

    coros = []
    for tier in tiers:
        capped = envelopes[:max_envelopes_per_tier]
        for i, env in enumerate(capped):
            coros.append(_emit(tier, env, i))
    if coros:
        await asyncio.gather(*coros, return_exceptions=False)

    total_generated = sum(per_tier.values())
    summary = ", ".join(f"{tier}: {per_tier.get(tier, 0)}" for tier in tiers)
    result = {
        "generated": True,
        "total": total_generated,
        "by_tier": per_tier,
        "envelopes_considered": len(envelopes),
        "summary": summary,
    }

    ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    async with tester_progress_lock:
        tester_progress["completed_at"] = datetime.now(timezone.utc).isoformat()
        tester_progress["duration_ms"] = ms
        tester_progress["in_flight"] = []
        await _persist_progress()
    _emit_log(
        transform_id, "info",
        f"Tester — done in {ms/1000:.1f}s: {summary}",
        agent="tester", phase="complete",
    )
    await _update_agent_run(run_id,
        status="completed" if total_generated else "failed",
        output_summary=f"Generated {total_generated} test file(s) — {summary}",
        details=result,
        duration_ms=ms,
    )
    return result


def _parse_coverage_report(report_path: str) -> Optional[float]:
    """Extract a coverage percentage (0-100) from a report file.
    Supports JaCoCo CSV, npm coverage-summary.json and pytest-cov
    coverage.json. Returns None on parse failure — coverage % is
    a nice-to-have, not blocking."""
    try:
        if report_path.endswith(".csv"):
            with open(report_path, "r", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                covered = missed = 0
                for row in reader:
                    covered += int(row.get("INSTRUCTION_COVERED", 0) or 0)
                    missed  += int(row.get("INSTRUCTION_MISSED", 0) or 0)
                total = covered + missed
                return round(100 * covered / total, 1) if total else None
        if report_path.endswith("coverage-summary.json"):
            with open(report_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            total = ((data.get("total") or {}).get("lines") or {}).get("pct")
            return float(total) if total is not None else None
        if report_path.endswith("coverage.json"):
            with open(report_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            total = (data.get("totals") or {}).get("percent_covered")
            return float(total) if total is not None else None
    except Exception:
        return None
    return None


# ═══════════════════════════════════════════════════════════════════
# iter-15.59 — JUnit / pytest XML report parser
# ═══════════════════════════════════════════════════════════════════
#
# `_run_coverage` invokes the target-stack test tool (`mvn verify`,
# `npm test --coverage`, `pytest --cov`, `dotnet test`, `go test`).
# Every one of these tools drops JUnit-shaped XML somewhere in the
# manifest tree. We scan for it, extract per-testcase pass / fail /
# skipped counts, and bucket by tier using the file-path convention
# `_test_path_for_envelope` established (paths under `tests/business/`
# → business tier, `tests/api/` → api, etc.). The result is what the
# FE "Test Report" panel graphs.

_JUNIT_REPORT_GLOBS = [
    "**/surefire-reports/*.xml",       # Maven
    "**/failsafe-reports/*.xml",       # Maven IT
    "**/build/test-results/**/*.xml",  # Gradle
    "**/target/surefire-reports/*.xml",
    "**/test-results/**/*.xml",        # dotnet / generic
    "**/junit*.xml",                   # pytest --junitxml
    "**/reports/junit/*.xml",          # jest-junit
]


def _classify_test_tier(path: str, name: str, classname: str) -> str:
    """Derive tier from path/name/classname. Matches the layout that
    `_test_path_for_envelope` uses for generated tests."""
    signal = f"{path} {name} {classname}".lower()
    if "/business/" in signal or ".feature" in signal or "gherkin" in signal or "bdd" in signal:
        return "business"
    if "/integration/" in signal or "integrationtest" in signal or "_it." in signal:
        return "integration"
    if "/api/" in signal or "controllertest" in signal or "resttest" in signal:
        return "api"
    return "api"


def _parse_junit_xml_file(xml_path: str) -> List[Dict[str, Any]]:
    """Parse a single JUnit XML file. Returns list of testcase dicts."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return []
    # Root can be <testsuites> or a single <testsuite>.
    suites = list(root.iter("testsuite"))
    if not suites and root.tag == "testsuite":
        suites = [root]
    out: List[Dict[str, Any]] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            name = case.attrib.get("name") or ""
            classname = case.attrib.get("classname") or suite.attrib.get("name") or ""
            duration_ms = 0
            try:
                duration_ms = int(float(case.attrib.get("time") or 0) * 1000)
            except (TypeError, ValueError):
                duration_ms = 0
            status = "passed"
            message = ""
            failure_el = case.find("failure")
            error_el = case.find("error")
            skipped_el = case.find("skipped")
            if failure_el is not None:
                status = "failed"
                message = (failure_el.attrib.get("message") or (failure_el.text or ""))[:400]
            elif error_el is not None:
                status = "failed"
                message = (error_el.attrib.get("message") or (error_el.text or ""))[:400]
            elif skipped_el is not None:
                status = "skipped"
                message = (skipped_el.attrib.get("message") or (skipped_el.text or ""))[:400]
            out.append({
                "name": name,
                "classname": classname,
                "status": status,
                "duration_ms": duration_ms,
                "message": message,
                "tier": _classify_test_tier(xml_path, name, classname),
                "report_path": xml_path,
            })
    return out


def _collect_test_reports(workspace_root: str) -> Dict[str, Any]:
    """Walk the workspace for JUnit-shaped XML, aggregate per-tier
    counters + full case list. Returns:
      {
        totals: {total, passed, failed, skipped, duration_ms},
        per_tier: {business|api|integration: {total, passed, failed, skipped}},
        cases: [ {name, classname, status, tier, duration_ms, message, report_path} ],
        report_files: [str],
      }
    """
    from glob import glob as _glob
    seen: set = set()
    cases: List[Dict[str, Any]] = []
    report_files: List[str] = []
    for pattern in _JUNIT_REPORT_GLOBS:
        for xml_path in _glob(os.path.join(workspace_root, pattern), recursive=True):
            if xml_path in seen:
                continue
            seen.add(xml_path)
            parsed = _parse_junit_xml_file(xml_path)
            if not parsed:
                continue
            report_files.append(os.path.relpath(xml_path, workspace_root))
            cases.extend(parsed)

    per_tier: Dict[str, Dict[str, int]] = {
        t: {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        for t in ("business", "api", "integration")
    }
    total = passed = failed = skipped = 0
    dur = 0
    for c in cases:
        tier = c.get("tier") or "api"
        bucket = per_tier.setdefault(tier, {"total": 0, "passed": 0, "failed": 0, "skipped": 0})
        bucket["total"] += 1
        bucket[c.get("status", "passed")] = bucket.get(c.get("status", "passed"), 0) + 1
        total += 1
        if c["status"] == "passed":   passed += 1
        elif c["status"] == "failed": failed += 1
        elif c["status"] == "skipped":skipped += 1
        dur += int(c.get("duration_ms") or 0)

    # Cap cases list — the FE graphs from counters, only the failures
    # need full detail.  Keep all failures + first 100 non-failures.
    fails = [c for c in cases if c["status"] == "failed"]
    non_fails = [c for c in cases if c["status"] != "failed"][:100]
    cases_capped = fails + non_fails

    return {
        "totals": {
            "total": total, "passed": passed, "failed": failed,
            "skipped": skipped, "duration_ms": dur,
        },
        "per_tier": per_tier,
        "cases": cases_capped,
        "report_files": report_files,
        "pass_rate": round(100 * passed / total, 1) if total else None,
    }


async def _run_coverage(
    transform_id: str,
    workspace_root: str,
    build_tools: Dict[str, str],
) -> Dict[str, Any]:
    """iter-15.45 — Best-effort coverage measurement. For each component
    where the picked tool has a coverage argv, we run it inside every
    manifest directory and try to parse a coverage % out of the
    tool-native report. Failures degrade to `"skipped"` cleanly."""
    run_id = await _log_agent_run(
        transform_id, "tester", "coverage",
        input_summary=f"Coverage run for {len(build_tools or {})} component(s)",
    )
    t0 = datetime.now(timezone.utc)

    try:
        try:
            timeout_sec = int(os.environ.get("LAMA_COVERAGE_TIMEOUT_SEC", "600") or "600")
        except ValueError:
            timeout_sec = 600
        timeout_sec = max(60, min(timeout_sec, 3600))

        components: List[Dict[str, Any]] = []
        weighted_sum = 0.0
        weighted_n = 0

        for component, tool in (build_tools or {}).items():
            spec = COVERAGE_COMMANDS.get(str(tool or "").lower())
            native = NATIVE_BUILD_COMMANDS.get(str(tool or "").lower())
            row: Dict[str, Any] = {"component": component, "tool": tool, "invocations": []}

            if spec is None or native is None:
                row["status"] = "skipped"
                row["reason"] = f"No coverage command mapping for tool={tool}"
                components.append(row)
                continue
            if not _binary_on_path(native["binary"]):
                row["status"] = "skipped"
                row["reason"] = f"toolchain missing on PATH: {native['binary']}"
                components.append(row)
                continue

            dirs = _find_manifest_dirs(workspace_root, native["manifest"])
            if not dirs:
                row["status"] = "skipped"
                row["reason"] = f"No `{native['manifest']}` found under workspace"
                components.append(row)
                continue

            for d in dirs:
                t_start = datetime.now(timezone.utc)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *spec["argv"], cwd=d,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout_b, stderr_b = await asyncio.wait_for(
                            proc.communicate(), timeout=timeout_sec)
                        exit_code = proc.returncode
                    except asyncio.TimeoutError:
                        with contextlib.suppress(Exception):
                            proc.kill()
                        elapsed = int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000)
                        row["invocations"].append({
                            "cwd": os.path.relpath(d, workspace_root) or ".",
                            "status": "timeout", "duration_ms": elapsed,
                            "reason": f"Coverage exceeded {timeout_sec}s cap",
                            "label": spec["label"],
                        })
                        continue
                except Exception as e:
                    row["invocations"].append({
                        "cwd": os.path.relpath(d, workspace_root) or ".",
                        "status": "failed",
                        "reason": f"subprocess launch error: {e}",
                        "label": spec["label"],
                    })
                    continue

                elapsed = int((datetime.now(timezone.utc) - t_start).total_seconds() * 1000)
                stdout = (stdout_b or b"").decode("utf-8", errors="replace")
                stderr = (stderr_b or b"").decode("utf-8", errors="replace")
                coverage_pct = None
                for rp in spec.get("report_paths") or []:
                    from glob import glob as _glob
                    for candidate in _glob(os.path.join(d, rp)):
                        parsed = _parse_coverage_report(candidate)
                        if parsed is not None:
                            coverage_pct = parsed
                            break
                    if coverage_pct is not None:
                        break

                inv_status = "passed" if exit_code == 0 else "failed"
                row["invocations"].append({
                    "cwd": os.path.relpath(d, workspace_root) or ".",
                    "status": inv_status,
                    "exit_code": exit_code,
                    "duration_ms": elapsed,
                    "coverage_pct": coverage_pct,
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": stderr[-4000:],
                    "label": spec["label"],
                })
                if coverage_pct is not None:
                    weighted_sum += coverage_pct
                    weighted_n += 1

            passed_invs = [i for i in row["invocations"] if i.get("status") == "passed"]
            failed_invs = [i for i in row["invocations"] if i.get("status") in ("failed", "timeout")]
            if passed_invs and not failed_invs:
                row["status"] = "passed"
            elif failed_invs:
                row["status"] = "failed"
            else:
                row["status"] = "skipped"

            covered_pcts = [i.get("coverage_pct") for i in row["invocations"] if i.get("coverage_pct") is not None]
            if covered_pcts:
                row["coverage_pct"] = round(sum(covered_pcts) / len(covered_pcts), 1)

            components.append(row)

        overall_coverage = round(weighted_sum / weighted_n, 1) if weighted_n else None

        # iter-15.59 — Aggregate JUnit / pytest / jest XML reports from
        # anywhere in the workspace and attach a per-tier pass/fail
        # summary for the FE test-report chart.
        try:
            test_report = _collect_test_reports(workspace_root)
        except Exception as _tr_err:
            test_report = {
                "totals": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "duration_ms": 0},
                "per_tier": {}, "cases": [], "report_files": [],
                "pass_rate": None,
                "parse_error": str(_tr_err),
            }

        result = {
            "components": components,
            "overall_coverage_pct": overall_coverage,
            "mode": "native",
            "timeout_sec": timeout_sec,
            "test_report": test_report,
        }

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=f"Coverage: {overall_coverage if overall_coverage is not None else 'n/a'}%",
            score=float(overall_coverage) if overall_coverage is not None else 0.0,
            details=result,
            duration_ms=ms,
        )
        return result

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        return {
            "components": [],
            "overall_coverage_pct": None,
            "mode": "native",
            "summary": f"Coverage phase crashed: {e}",
        }


async def _run_tester(transform_id: str, all_transformed: list, target_stack: dict, model: str = None) -> dict:
    """Phase 5 (static): LLM-based compilation-readiness narrative.

    iter-15.44 — This is no longer the sole compile signal; the real
    compile happens in `_run_compiler` via native subprocess. This
    LLM pass now runs as a supplementary `static_analysis` narrative
    describing likely issues (missing imports, cross-file consistency)
    that a build tool cannot always surface cleanly.
    """
    run_id = await _log_agent_run(
        transform_id, "tester", "testing",
        input_summary=f"Compilation analysis for {len(all_transformed)} files",
    )
    t0 = datetime.now(timezone.utc)

    try:
        prompt_template = await _get_effective_prompt(transform_id, "tester")
        if not prompt_template:
            prompt_template = "You are a compilation analysis engine. Check if the transformed code will compile."
        model = await _get_effective_model(transform_id, "tester", model)

        file_listing = []
        for f in all_transformed[:80]:
            content = f.get("content", "")[:2000]
            file_listing.append(f"===== {f.get('path', '')} =====\n{content}")
        files_block = "\n\n".join(file_listing)[:50000]

        user_prompt = f"""Analyze these transformed files for compilation readiness.

===== TARGET STACK =====
{json.dumps(target_stack)}

===== TRANSFORMED FILES ({len(all_transformed)} total) =====
{files_block}

Check for: missing imports, unresolved references, type mismatches,
missing dependencies, configuration gaps, and cross-file consistency issues.
Return ONLY valid JSON matching the output schema."""

        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            agent_key="tools.transformer.tester",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=8000,
            response_format={"type": "json_object"},
        )

        response_text = response.get("content", "") if isinstance(response, dict) else str(response)
        result = _extract_json_object(response_text) or {
            "compilation_ready": False,
            "overall_score": 0,
            "checks": [],
            "summary": "Analysis could not be parsed",
        }

        # iter-16.x — Schema-drift guard. The prompt tells the LLM
        # `checks[].details` / `checks[].fix_suggestion` must be plain
        # strings, but LLMs occasionally nest a self-invented summary
        # object instead (e.g. {"total_checks": N, "passed": N, "failed":
        # N, "overall_status": "..."}). The frontend renders these fields
        # directly as JSX text, so an unstringified object there crashes
        # the whole page (React error #31 — "objects are not valid as a
        # React child"). Coerce every checks[] entry's free-text fields to
        # strings here so malformed LLM output can never reach the UI.
        _checks = result.get("checks")
        if isinstance(_checks, list):
            for _c in _checks:
                if not isinstance(_c, dict):
                    continue
                for _field in ("details", "fix_suggestion", "status", "category", "file"):
                    _val = _c.get(_field)
                    if _val is not None and not isinstance(_val, str):
                        _c[_field] = json.dumps(_val) if isinstance(_val, (dict, list)) else str(_val)
        # iter-16.x follow-up — the guard above only covered checks[]
        # entries; the exact object shape called out in the comment
        # above ({total_checks, passed, failed, overall_status}) is what
        # LLMs actually nest into the TOP-LEVEL `summary` field, which
        # was never coerced. Close that gap too.
        _summary_val = result.get("summary")
        if _summary_val is not None and not isinstance(_summary_val, str):
            result["summary"] = json.dumps(_summary_val) if isinstance(_summary_val, (dict, list)) else str(_summary_val)

        # iter-15.57 — Deterministic structural failure count. The LLM
        # tester was returning `compilation_ready: True` even when the
        # verifier had already flagged files as VERIFY_FAILED. Cross-check
        # against the persisted `transform_files.compilable` flag and
        # HARD-OVERRIDE `compilation_ready` when any file failed the
        # structural gate.
        try:
            failing_docs = await transform_files.find(
                {"transform_id": transform_id, "type": "transformed", "compilable": False},
                {"path": 1, "verifier_result.structural_check.issues": 1},
            ).to_list(length=200)
        except Exception:
            failing_docs = []
        failing_paths = [d.get("path", "") for d in failing_docs]
        result["failing_files"] = failing_paths
        result["failing_files_count"] = len(failing_paths)
        if failing_paths:
            result["compilation_ready"] = False
            score = float(result.get("overall_score", 0) or 0)
            # Cap score by fraction of passing files.
            total = max(len(all_transformed), 1)
            pass_frac = max(0.0, 1.0 - (len(failing_paths) / total))
            result["overall_score"] = min(score, round(pass_frac * 100.0, 1))
            existing = result.get("summary", "") or ""
            result["summary"] = (
                f"STRUCTURAL: {len(failing_paths)}/{total} files failed the "
                f"deterministic verifier gate — compilation is NOT ready. "
                + existing
            )[:1500]

        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id,
            status="completed",
            output_summary=(
                f"Compilation ready: {result.get('compilation_ready', False)}, "
                f"Score: {result.get('overall_score', 0)}%"
                + (f" [{len(failing_paths)} structural fails]" if failing_paths else "")
            ),
            score=float(result.get("overall_score", 0)),
            details=result,
            duration_ms=ms,
        )
        return result

    except Exception as e:
        ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        await _update_agent_run(run_id, status="failed", error=str(e), duration_ms=ms)
        return {"compilation_ready": False, "overall_score": 0, "checks": [], "summary": f"Analysis failed: {e}"}


# ── Plan gate: Validator ──────────────────────────────────────────────
async def _run_validator(
    transform_id: str,
    task_docs: List[Dict[str, Any]],
    envelopes: List[Dict[str, Any]],
    model: str = None,
) -> Dict[str, Any]:
    """Validate the Planner's task list BEFORE any coder token is spent.

    Sits between Planner and Coder. Deterministic checks run first and are
    authoritative for the things that are decidable without a model
    (missing target path, duplicate targets, orphaned envelope refs,
    unreachable wave ordering). The LLM then reviews what is left --
    decomposition quality, missed envelopes, wrong ordering.

    Returns {verdict, confidence, issues[], deterministic[], summary}.
    `verdict` is ACCEPT or REJECT; REJECT never hard-stops the run (the
    operator already confirmed these tasks at the iter-15.28 gate), it is
    surfaced as a warning so the plan can be corrected on a rerun.
    """
    run_id = await _log_agent_run(
        transform_id, "validator", "plan-validation",
        input_summary=f"Validating {len(task_docs)} task(s) against {len(envelopes)} envelope(s)",
    )
    t0 = datetime.now(timezone.utc)

    # ── Deterministic gate ────────────────────────────────────────────
    det: List[Dict[str, str]] = []
    seen_targets: Dict[str, str] = {}
    env_ids = {e.get("envelope_id") for e in envelopes if e.get("envelope_id")}
    covered_envs: Set[str] = set()

    for t in task_docs:
        tid = t.get("task_id", "") or "?"
        target = (t.get("target_path") or "").strip()
        if not target:
            det.append({"severity": "critical", "task_id": tid,
                        "issue": "Task has no target_path — the Coder has nowhere to write."})
        elif target in seen_targets:
            det.append({"severity": "critical", "task_id": tid,
                        "issue": f"Duplicate target_path '{target}' (also produced by {seen_targets[target]}) — the later task silently overwrites the earlier one."})
        else:
            seen_targets[target] = tid

        eid = t.get("envelope_id") or ""
        if eid:
            covered_envs.add(eid)
            if env_ids and eid not in env_ids:
                det.append({"severity": "critical", "task_id": tid,
                            "issue": f"Task references envelope '{eid}' which does not exist."})

    missing = sorted(env_ids - covered_envs) if env_ids else []
    for eid in missing[:25]:
        det.append({"severity": "major", "task_id": "",
                    "issue": f"Envelope '{eid}' has no task — that unit of work would be silently dropped."})

    waves = sorted({int(t.get("wave", 0) or 0) for t in task_docs})
    if waves and waves != list(range(waves[0], waves[0] + len(waves))):
        det.append({"severity": "minor", "task_id": "",
                    "issue": f"Wave numbers are not contiguous ({waves}) — later waves may start before their inputs exist."})

    det_critical = sum(1 for d in det if d["severity"] == "critical")

    # ── LLM review ────────────────────────────────────────────────────
    llm_result: Dict[str, Any] = {}
    try:
        prompt_template = await _get_effective_prompt(transform_id, "validator")
        if not prompt_template:
            prompt_template = (
                "You are a Plan Validation Engine. Review a transformation "
                "task plan for completeness and correct ordering. Return JSON."
            )
        model = await _get_effective_model(transform_id, "validator", model)

        plan_digest = [
            {
                "task_id": t.get("task_id", ""),
                "wave": t.get("wave", 0),
                "source": t.get("source_path", ""),
                "target": t.get("target_path", ""),
                "envelope_id": t.get("envelope_id", ""),
                "kind": t.get("kind", "") or t.get("type", ""),
            }
            for t in task_docs[:200]
        ]
        env_digest = [
            {"envelope_id": e.get("envelope_id", ""),
             "name": e.get("name", "") or e.get("title", ""),
             "kind": e.get("kind", "")}
            for e in envelopes[:200]
        ]

        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content":
                    "Validate this transformation plan.\n\n"
                    f"===== ENVELOPES ({len(envelopes)}) =====\n"
                    f"{json.dumps(env_digest)[:12000]}\n\n"
                    f"===== PLANNED TASKS ({len(task_docs)}) =====\n"
                    f"{json.dumps(plan_digest)[:12000]}\n\n"
                    "Return ONLY the JSON described in your system prompt."},
            ],
            model=model,
            agent_key="tools.transformer.validator",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
        text = response.get("content", "") if isinstance(response, dict) else str(response)
        llm_result = _extract_json_object(text) or {}
    except Exception as exc:  # noqa: BLE001 — the deterministic gate still stands
        llm_result = {"summary": f"LLM plan review unavailable: {str(exc)[:200]}"}

    issues = llm_result.get("issues") if isinstance(llm_result.get("issues"), list) else []
    for _i in issues:
        if isinstance(_i, dict):
            for _f in ("description", "fix", "severity"):
                _v = _i.get(_f)
                if _v is not None and not isinstance(_v, str):
                    _i[_f] = json.dumps(_v) if isinstance(_v, (dict, list)) else str(_v)

    # Deterministic criticals are authoritative — the LLM cannot vote them away.
    verdict = "REJECT" if det_critical else str(llm_result.get("verdict", "ACCEPT")).upper()
    if verdict not in {"ACCEPT", "REJECT"}:
        verdict = "ACCEPT"

    summary = llm_result.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = json.dumps(summary)
    if not summary:
        summary = (f"{det_critical} critical plan defect(s) found deterministically."
                   if det_critical else "Plan accepted.")

    result = {
        "verdict": verdict,
        "confidence": llm_result.get("confidence", 100 if not det else 60),
        "deterministic": det,
        "issues": issues,
        "summary": summary,
        "tasks_checked": len(task_docs),
        "envelopes_without_task": missing,
    }

    await _update_agent_run(
        run_id, status="completed",
        output_summary=f"{verdict} — {len(det)} deterministic, {len(issues)} model-reported",
        duration_ms=int((datetime.now(timezone.utc) - t0).total_seconds() * 1000),
    )
    await transformations.update_one(
        {"_id": transform_id}, {"$set": {"plan_validation": result}},
    )
    return result


# ── Production gate: DevOps dependency audit ──────────────────────────
_MANIFEST_NAMES = (
    "pom.xml", "build.gradle", "build.gradle.kts", "package.json",
    "requirements.txt", "pyproject.toml", "go.mod", "Gemfile", "Cargo.toml",
)


def _collect_manifests(files: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Pick out build manifests from the generated tree."""
    out = []
    for f in files:
        path = f.get("path", "") or ""
        base = path.rsplit("/", 1)[-1]
        if base in _MANIFEST_NAMES or base.endswith(".csproj"):
            out.append({"path": path, "content": f.get("content", "") or ""})
    return out


# iter-19 — Real Maven-resolution checks.
#
# The previous pom audit looked for exactly one thing: a duplicate
# `<artifactId>`. That is a genuine problem but a rare one, and it is not
# what actually breaks a generated pom. The two failures that do — both
# seen on live transformer output — are:
#
#   1. a `<dependency>` with no `<version>` and nothing in the file able to
#      supply one (no `<parent>`, no `<dependencyManagement>`). Maven fails
#      with "'dependencies.dependency.version' is missing" before it ever
#      reaches the network.
#   2. `<version>${some.prop}</version>` where `some.prop` is not declared
#      in `<properties>`. Maven does not interpolate it, and the literal
#      string `${some.prop}` is then looked up as a version and not found.
#
# Both are decidable by reading the file, which is why they belong in the
# deterministic pass rather than being left to the LLM's judgement.
_POM_DEPENDENCY_RE = re.compile(r"<dependency>(.*?)</dependency>", re.S | re.I)
_POM_PROPERTY_REF_RE = re.compile(r"\$\{([^}]+)\}")


def _audit_pom(path: str, content: str) -> List[Dict[str, str]]:
    """Deterministic production-readiness findings for one pom.xml."""
    findings: List[Dict[str, str]] = []

    # Everything `<dependencyManagement>` declares is a version SOURCE, not
    # a dependency that itself needs a version — strip it before looking
    # for version-less dependencies so a correct BOM isn't reported as a
    # fault.
    managed_block = "".join(
        re.findall(r"<dependencyManagement>(.*?)</dependencyManagement>", content, re.S | re.I)
    )
    has_parent = bool(re.search(r"<parent>", content, re.I))
    has_managed = bool(managed_block.strip())
    body = content
    if managed_block:
        body = re.sub(r"<dependencyManagement>.*?</dependencyManagement>", "", content, flags=re.S | re.I)

    declared_props = {
        m.strip()
        for block in re.findall(r"<properties>(.*?)</properties>", content, re.S | re.I)
        for m in re.findall(r"<([A-Za-z0-9_.\-]+)>", block)
    }
    # Maven resolves these itself; they are never declared in <properties>.
    builtin_props = {"project.version", "project.groupId", "project.artifactId", "pom.version"}

    seen_coords: List[str] = []
    for dep_body in _POM_DEPENDENCY_RE.findall(body):
        gid = (re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", dep_body, re.I) or [None, ""])
        gid = gid.group(1) if hasattr(gid, "group") else ""
        aid_m = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", dep_body, re.I)
        aid = aid_m.group(1) if aid_m else ""
        ver_m = re.search(r"<version>\s*([^<]+?)\s*</version>", dep_body, re.I)
        ver = ver_m.group(1) if ver_m else ""
        coord = f"{gid}:{aid}" if gid else aid
        if aid:
            seen_coords.append(coord)

        if not ver and not has_parent and not has_managed:
            findings.append({
                "severity": "critical", "manifest": path,
                "issue": (
                    f"Dependency '{coord or '(unnamed)'}' declares no <version>, and this pom has "
                    f"neither a <parent> nor a <dependencyManagement> section to supply one. "
                    f"Maven fails resolution with "
                    f"\"'dependencies.dependency.version' is missing\"."
                ),
                "fix": (
                    "Either add an explicit <version>, or inherit one — add the "
                    "spring-boot-starter-parent (or the appropriate BOM) as <parent>, "
                    "or import the BOM under <dependencyManagement>."
                ),
            })

        for prop in _POM_PROPERTY_REF_RE.findall(ver):
            prop = prop.strip()
            if prop in builtin_props or prop in declared_props:
                continue
            findings.append({
                "severity": "critical", "manifest": path,
                "issue": (
                    f"Dependency '{coord or '(unnamed)'}' pins version '${{{prop}}}', but "
                    f"'{prop}' is not declared in <properties>. Maven leaves the placeholder "
                    f"uninterpolated and then cannot find that literal version."
                ),
                "fix": f"Add <{prop}>VERSION</{prop}> to <properties>, or replace the reference with a literal version.",
            })

    dupes = sorted({c for c in seen_coords if seen_coords.count(c) > 1})
    for c in dupes:
        findings.append({
            "severity": "major", "manifest": path,
            "issue": f"'{c}' is declared more than once — Maven resolves one and silently drops the other.",
            "fix": "Remove the duplicate <dependency> block, keeping the one with the correct scope and version.",
        })

    # A parent with an unresolved property version is the same bug one
    # level up, and it breaks the build harder (nothing resolves at all).
    parent_block = re.search(r"<parent>(.*?)</parent>", content, re.S | re.I)
    if parent_block:
        pv = re.search(r"<version>\s*([^<]+?)\s*</version>", parent_block.group(1), re.I)
        for prop in _POM_PROPERTY_REF_RE.findall(pv.group(1) if pv else ""):
            if prop.strip() not in declared_props and prop.strip() not in builtin_props:
                findings.append({
                    "severity": "critical", "manifest": path,
                    "issue": (
                        f"The <parent> version references '${{{prop.strip()}}}', which is not declared "
                        f"in <properties>. Nothing in this module can resolve until the parent does."
                    ),
                    "fix": "Give the <parent> a literal version — a parent POM cannot be resolved via a property it defines itself.",
                })
    return findings


def _audit_gradle(path: str, content: str) -> List[Dict[str, str]]:
    """The Gradle equivalent of the two pom checks above: a coordinate
    whose version is an undefined variable."""
    findings: List[Dict[str, str]] = []
    declared = set(re.findall(r"^\s*(?:val|def|ext\.)?\s*([A-Za-z0-9_]+)\s*=", content, re.M))
    declared |= set(re.findall(r"^\s*([A-Za-z0-9_.\-]+)\s*=", content, re.M))
    for coord in re.findall(r"""['"]([\w.\-]+:[\w.\-]+:[^'"]*\$\{?[\w.]+\}?)['"]""", content):
        for var in re.findall(r"\$\{?([\w.]+)\}?", coord):
            root = var.split(".")[0]
            if var in declared or root in declared:
                continue
            findings.append({
                "severity": "critical", "manifest": path,
                "issue": f"'{coord}' pins its version to '${var}', which is not defined in this build script.",
                "fix": f"Declare {var} in the build script (or gradle.properties), or use a literal version.",
            })
    return findings


async def _run_devops_dependency_check(
    transform_id: str,
    compile_result: Dict[str, Any],
    model: str = None,
) -> Dict[str, Any]:
    """Audit the generated build manifests for production readiness.

    This is the DevOps agent's *proactive* mode, distinct from the
    escalation persona the compile-fix loop pulls in on a stagnant build.
    A green compile only proves the code builds on this machine today; it
    says nothing about unpinned versions or duplicate/conflicting
    declarations that make the build irreproducible tomorrow.

    Deterministic first (unpinned + duplicate detection is decidable),
    then an LLM pass for judgement calls.
    """
    run_id = await _log_agent_run(
        transform_id, "devops_expert", "dependency-audit",
        input_summary="Auditing generated build manifests for production readiness",
    )
    t0 = datetime.now(timezone.utc)

    cursor = transform_files.find(
        {"transform_id": transform_id, "type": "transformed"},
        {"path": 1, "content": 1},
    )
    all_files = [{"path": d.get("path", ""), "content": d.get("content", "")}
                 async for d in cursor]
    manifests = _collect_manifests(all_files)

    findings: List[Dict[str, str]] = []
    if not manifests:
        findings.append({
            "severity": "major", "manifest": "",
            "issue": "No build manifest found in the generated tree — the output cannot be built reproducibly.",
        })

    # ── Deterministic checks ──────────────────────────────────────────
    for m in manifests:
        path, content = m["path"], m["content"]
        base = path.rsplit("/", 1)[-1]

        if base == "requirements.txt":
            for line in content.splitlines():
                dep = line.strip()
                if not dep or dep.startswith("#") or dep.startswith("-"):
                    continue
                if not re.search(r"[=<>~!]=|@", dep):
                    findings.append({
                        "severity": "major", "manifest": path,
                        "issue": f"'{dep}' is unpinned — the build is not reproducible.",
                    })
        elif base == "package.json":
            try:
                pkg = json.loads(content) if content.strip() else {}
                for sect in ("dependencies", "devDependencies"):
                    for name, ver in (pkg.get(sect) or {}).items():
                        if isinstance(ver, str) and ver.strip() in {"*", "latest", ""}:
                            findings.append({
                                "severity": "critical", "manifest": path,
                                "issue": f"{sect}.{name} is '{ver}' — a floating version can change the build without a code change.",
                            })
            except Exception:  # noqa: BLE001
                findings.append({
                    "severity": "critical", "manifest": path,
                    "issue": "package.json is not valid JSON — npm/yarn cannot install.",
                })
        elif base == "pom.xml":
            findings.extend(_audit_pom(path, content))
        elif base in ("build.gradle", "build.gradle.kts"):
            findings.extend(_audit_gradle(path, content))

    det_critical = sum(1 for f in findings if f["severity"] == "critical")

    # ── LLM judgement pass ────────────────────────────────────────────
    llm_result: Dict[str, Any] = {}
    try:
        prompt_template = await _get_effective_prompt(transform_id, "devops_audit")
        if not prompt_template:
            prompt_template = (
                "You are a DevOps engineer auditing build manifests for "
                "production readiness. Return JSON."
            )
        model = await _get_effective_model(transform_id, "devops_audit", model)
        digest = "\n\n".join(
            f"===== {m['path']} =====\n{m['content'][:4000]}" for m in manifests[:8]
        )
        response = await fabric_call(
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content":
                    "Audit these generated build manifests for production readiness.\n\n"
                    f"Compile status: {'PASSED' if compile_result.get('compilation_ready') else 'FAILED'}\n"
                    f"Deterministic findings already known: {json.dumps(findings)[:3000]}\n\n"
                    f"{digest[:20000]}\n\n"
                    "Return ONLY the JSON described in your system prompt."},
            ],
            model=model,
            agent_key="tools.transformer.devops_expert",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
        text = response.get("content", "") if isinstance(response, dict) else str(response)
        llm_result = _extract_json_object(text) or {}
    except Exception as exc:  # noqa: BLE001
        llm_result = {"summary": f"LLM dependency audit unavailable: {str(exc)[:200]}"}

    extra = llm_result.get("findings") if isinstance(llm_result.get("findings"), list) else []
    for _f in extra:
        if isinstance(_f, dict):
            for _k in ("issue", "fix", "severity", "manifest"):
                _v = _f.get(_k)
                if _v is not None and not isinstance(_v, str):
                    _f[_k] = json.dumps(_v) if isinstance(_v, (dict, list)) else str(_v)
            # iter-19 — normalise severity case. The prompt asks for
            # "CRITICAL | MAJOR | MINOR" while the deterministic pass emits
            # lowercase, and the gate below compared lowercase only — so an
            # LLM-reported CRITICAL never blocked production_ready. That was
            # invisible while the verdict was advisory; now that it decides
            # the run's final status, it has to be right.
            _sev = _f.get("severity")
            if isinstance(_sev, str):
                _f["severity"] = _sev.strip().lower()

    production_ready = bool(
        compile_result.get("compilation_ready")
        and det_critical == 0
        and not any((f or {}).get("severity") == "critical" for f in extra if isinstance(f, dict))
    )

    summary = llm_result.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = json.dumps(summary)
    if not summary:
        summary = (
            f"{len(manifests)} manifest(s) audited; "
            f"{det_critical} critical, {len(findings) - det_critical} non-critical finding(s)."
        )

    result = {
        "production_ready": production_ready,
        "manifests_audited": [m["path"] for m in manifests],
        # Provenance: which findings were decided by parsing vs by the model.
        "deterministic": findings,
        "model_reported": extra,
        # iter-19 — `findings` is the UNION, deterministic first.
        #
        # It used to be `extra` alone, which meant the two consumers that
        # matter both ignored every deterministic finding: the remediation
        # loop saw nothing actionable and stopped, and the UI panel showed
        # an empty list — for a pom that definitively cannot resolve. With
        # the LLM unavailable the audit was silent about faults it had
        # already proven.
        #
        # Deterministic first because they are decidable: a missing
        # <version> is not a judgement call, and it should head the list the
        # Coder is handed.
        "findings": list(findings) + list(extra),
        "summary": summary,
    }

    await _update_agent_run(
        run_id, status="completed",
        output_summary=(
            f"{'production-ready' if production_ready else 'NOT production-ready'} — "
            f"{len(findings)} deterministic, {len(extra)} model-reported"
        ),
        duration_ms=int((datetime.now(timezone.utc) - t0).total_seconds() * 1000),
    )
    await transformations.update_one(
        {"_id": transform_id}, {"$set": {"dependency_audit": result}},
    )
    return result


async def _run_multi_agent_transformation(
    transform_id: str,
    model: str = None,
    resume: bool = False,
):
    """Multi-agent transformation pipeline (iter-16).

    Flow:
    1. Super Agent initializes → Context Manager discovers envelopes
    2. Update status to "awaiting_confirmation" → user reviews envelopes
    3. After user confirms → Planner creates tasks
    4. Coder transforms files wave-by-wave with Verifier quality gate
    5. Tester performs compilation analysis
    """
    if not await _acquire_worker_lease(transform_id):
        return

    try:
        transform = await transformations.find_one({"_id": transform_id})
        if not transform:
            return

        detected_stack = transform.get("source_stack") or {}
        source_files_docs = await transform_files.find(
            {"transform_id": transform_id, "type": "source"}
        ).to_list(2000)
        total = len(source_files_docs)
        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in source_files_docs]

        # ── Phase 1: Super Agent → Context Manager ──────────────────
        await _log_agent_run(transform_id, "super_agent", "init",
            status="completed",
            input_summary=f"Starting multi-agent pipeline for {total} files",
            output_summary="Delegating to Context Manager",
        )
        await _update_progress(
            transform_id,
            status="running",
            phase="context_manager",
            phase_label="Context Manager: Discovering APIs & tables",
            progress_pct=5,
            files_done=0,
            files_total=total,
        )

        envelopes = await _run_context_manager(transform_id, src_files, detected_stack, model)

        # Build and PERSIST the KB so the UI can render it and later phases
        # can read it back. Nothing is rendered into a prompt here -- the
        # rendered form is produced per task by `_slim_kb_ctx_for_task`,
        # which slices it to the file being transformed instead of carrying
        # the whole thing.
        try:
            kb = build_tools_kb(src_files, [])
            kb_summary = summarize_kb_for_ui(kb)
            await tools_kb.update_one(
                {"_id": transform_id},
                {"$set": {
                    "_id": transform_id,
                    "owner": "transformer",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "summary": kb_summary,
                    "stats": kb.get("stats"),
                }},
                upsert=True,
            )
        except Exception as e:
            print(f"[transformer:{transform_id}] KB build failed: {e}")

        # Update status to awaiting confirmation — user must review envelopes
        await _update_progress(
            transform_id,
            status="awaiting_confirmation",
            phase="awaiting_confirmation",
            phase_label="Review discovered APIs & tables, then confirm to proceed",
            progress_pct=15,
            files_done=0,
            files_total=total,
        )
        _emit_log(transform_id, "info",
            f"Context Manager discovered {len(envelopes)} envelopes. Awaiting user confirmation.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _update_progress(
            transform_id,
            status="failed",
            phase="failed",
            phase_label="Failed",
            error=str(e),
        )


async def _continue_multi_agent_after_confirm(
    transform_id: str,
    model: str = None,
):
    """Continue the multi-agent pipeline after user confirms the envelopes.

    Runs ONLY the Planner phase, then pauses at 'awaiting_task_confirmation'
    so the operator can review the generated task list before any code is
    touched (see `_continue_multi_agent_after_task_confirm`, which resumes
    into Coder → Verifier → Tester).
    """
    if not await _acquire_worker_lease(transform_id):
        return

    try:
        transform = await transformations.find_one({"_id": transform_id})
        if not transform:
            return

        detected_stack = transform.get("source_stack") or {}
        target_stack = transform.get("transforms") or {}
        build_tools = transform.get("build_tools") or {}

        # Reload envelopes
        env_cursor = transformer_envelopes.find({"transform_id": transform_id})
        envelope_docs = await env_cursor.to_list(500)
        envelopes = [{
            "envelope_id": e.get("envelope_id", ""),
            "endpoint_method": e.get("endpoint_method", ""),
            "endpoint_path": e.get("endpoint_path", ""),
            "controller_class": e.get("controller_class", ""),
            "service_class": e.get("service_class", ""),
            "repository_class": e.get("repository_class", ""),
            "db_tables": e.get("db_tables", []),
            "files_affected": e.get("files_affected", []),
            "action": e.get("action", "TRANSFORM"),
            "layer": e.get("layer", ""),
            "business_logic_summary": e.get("business_logic_summary", ""),
            "is_outbound_client": bool(e.get("is_outbound_client")),
        } for e in envelope_docs]

        # Reload source files — the deterministic Planner (iter-15.31)
        # needs the FULL file list to generate one task per file, not just
        # the ones tied to a discovered REST endpoint.
        source_files_docs = await transform_files.find(
            {"transform_id": transform_id, "type": "source"}
        ).to_list(2000)
        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in source_files_docs]

        # ── Phase 2: Planner ────────────────────────────────────────
        await _update_progress(
            transform_id,
            status="running",
            phase="planner",
            phase_label="Planner: Creating task list & waves",
            progress_pct=20,
        )

        tasks = await _run_planner(transform_id, envelopes, detected_stack, target_stack, model, src_files=src_files, build_tools=build_tools)

        # ── iter-15.28 — Human-in-the-loop gate #2 ──────────────────
        # The Planner's task list (which files get TRANSFORM/REWRITE/
        # DELETE/NO_CHANGE, grouped into waves) is now reviewable before
        # a single line of code is touched, mirroring the existing
        # envelope-confirm gate after Context Manager. The user
        # explicitly asked for this: "planner agent should show the
        # task list — need Human intervention to move further." Pause
        # here; `/transformer/{id}/tasks/confirm` resumes into
        # Coder → Verifier → Tester via
        # `_continue_multi_agent_after_task_confirm` below.
        await _update_progress(
            transform_id,
            status="awaiting_task_confirmation",
            phase="planner",
            phase_label=f"Planner: {len(tasks)} task(s) ready for review",
            progress_pct=22,
        )
        await _log_agent_run(
            transform_id, "planner", "review-gate", status="completed",
            output_summary=f"{len(tasks)} task(s) planned across "
                f"{len({t.get('wave', 0) for t in tasks})} wave(s) — awaiting human confirmation",
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _update_progress(
            transform_id,
            status="failed", phase="failed", phase_label="Failed", error=str(e),
        )


async def _continue_multi_agent_after_task_confirm(
    transform_id: str,
    model: str = None,
):
    """iter-15.28 — Continue the multi-agent pipeline after the user
    confirms the Planner's task list (the second human-in-the-loop gate).

    Runs Coder → Verifier → Tester to completion. Split out of what used
    to be the tail of `_continue_multi_agent_after_confirm` so the
    Planner's output can be reviewed/approved before any file is
    transformed — see `/transformer/{transform_id}/tasks/confirm`.
    """
    if not await _acquire_worker_lease(transform_id):
        return

    try:
        transform = await transformations.find_one({"_id": transform_id})
        if not transform:
            return

        detected_stack = transform.get("source_stack") or {}
        target_stack = transform.get("transforms") or {}

        source_files_docs = await transform_files.find(
            {"transform_id": transform_id, "type": "source"}
        ).to_list(2000)
        total = len(source_files_docs)
        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in source_files_docs]

        # Reload KB context
        kb_ctx = ""
        kb_doc = await tools_kb.find_one({"_id": transform_id})
        if kb_doc:
            try:
                kb_ctx = json.dumps(kb_doc.get("summary") or {})[:15000]
            except Exception:
                kb_ctx = ""

        # iter-15.34 — Reload envelopes so the Coder gets real ARCHITECTURE
        # context (API contract request/response, controller→service→
        # repository→table trace) per file, not just the generic
        # project-wide KB summary. Keyed by envelope_id for O(1) lookup
        # against each task's `envelope_id` (set by
        # `_build_deterministic_tasks`).
        envelope_docs = await transformer_envelopes.find({"transform_id": transform_id}).to_list(500)
        envelopes_by_id = {e.get("envelope_id"): e for e in envelope_docs if e.get("envelope_id")}

        # Load task docs from DB (persisted by planner, already reviewed)
        task_cursor = transformer_tasks.find(
            {"transform_id": transform_id}
        ).sort("wave", 1)
        task_docs = await task_cursor.to_list(2000)

        # ── Phase 2.5: Validator — gate the plan before spending coder
        # tokens. Never hard-stops (the operator already confirmed these
        # tasks at the iter-15.28 gate); a REJECT is surfaced as a
        # warning on the transformation so it can be fixed on a rerun.
        await _update_progress(
            transform_id,
            status="running",
            phase="validator",
            phase_label="Validator: Checking the task plan",
            progress_pct=24,
        )
        try:
            _plan_check = await _run_validator(
                transform_id, task_docs, envelope_docs, model,
            )
            if _plan_check.get("verdict") == "REJECT":
                _emit_log(
                    transform_id, "warn",
                    f"Validator rejected the plan: {_plan_check.get('summary', '')}",
                    agent="validator", phase="plan-validation",
                )
        except Exception as _ve:  # noqa: BLE001 — a gate must not kill the run
            log.warning("Validator skipped for %s: %s", transform_id, _ve)

        # ── Phase 3: Coder + Verifier (wave by wave) ────────────────
        await _update_progress(
            transform_id,
            status="running",
            phase="coder",
            phase_label="Coder: Transforming files",
            progress_pct=25,
        )

        # Build source file index for fast lookup
        src_by_path = {f.get("path", ""): f.get("content", "") for f in src_files}

        # Clear any previous transformed/error files
        await transform_files.delete_many({
            "transform_id": transform_id,
            "type": {"$in": ["transformed", "error"]},
        })

        transformed_files = []
        transformed_files_lock = asyncio.Lock()
        total_tasks = len(task_docs)
        done_counter = {"n": 0}
        done_counter_lock = asyncio.Lock()

        # iter-15.42 — Coder concurrency: fan out within each Planner wave
        # instead of running every task sequentially. Waves are ordered
        # (wave N+1 may depend on N's artefacts) so we keep BETWEEN-wave
        # execution sequential but run WITHIN-wave tasks in parallel under
        # a semaphore. Default is 6 concurrent tasks; override with the
        # `LAMA_CODER_MAX_CONCURRENCY` env-var. Empirically this cuts the
        # wall-clock of a 200-file wave by ~80% on Factory Droid and by
        # ~65% on an OpenRouter provider (bottleneck flips from LLM
        # round-trip latency to provider rate-limits).
        # The default of 6 is sized for a cloud endpoint. A local Ollama
        # daemon serves far fewer concurrent generations and QUEUES the
        # rest — and a queued request still burns its own timeout while
        # it waits, so over-fanning a local engine turns throughput into
        # 600s timeouts. Drop to 2 when the default provider is local.
        # An explicit LAMA_CODER_MAX_CONCURRENCY always wins.
        _conc_env = os.environ.get("LAMA_CODER_MAX_CONCURRENCY", "")
        if _conc_env.strip():
            try:
                _coder_concurrency = int(_conc_env)
            except ValueError:
                _coder_concurrency = 6
        else:
            _coder_concurrency = 2 if await active_default_provider_is_local() else 6
        _coder_concurrency = max(1, min(_coder_concurrency, 32))
        coder_semaphore = asyncio.Semaphore(_coder_concurrency)

        # Group tasks by wave so we can process each wave as a batch.
        waves_map: Dict[int, List[Dict[str, Any]]] = {}
        for td in task_docs:
            w = int(td.get("wave", 0) or 0)
            waves_map.setdefault(w, []).append(td)
        wave_numbers = sorted(waves_map.keys())

        async def _check_pause_stop() -> str:
            """Returns "stopped", "paused", or "ok". Blocks while paused."""
            fresh = await transformations.find_one(
                {"_id": transform_id}, {"paused": 1, "stopped": 1}
            )
            if fresh and fresh.get("stopped"):
                return "stopped"
            while fresh and fresh.get("paused") and not fresh.get("stopped"):
                await asyncio.sleep(2)
                fresh = await transformations.find_one(
                    {"_id": transform_id}, {"paused": 1, "stopped": 1}
                )
            if fresh and fresh.get("stopped"):
                return "stopped"
            return "ok"

        async def _process_single_task(task_doc: Dict[str, Any]) -> None:
            """iter-15.42 — Full per-task lifecycle: Coder → Verifier →
            (optional retry) → persist. Extracted from the previous inline
            for-loop so tasks can run in parallel under `coder_semaphore`.
            Never raises: task-level errors are caught and persisted as
            BLOCKED so a single failing task never poisons its wave."""
            async with coder_semaphore:
                source_path = task_doc.get("source_path", "")
                source_content = src_by_path.get(source_path, "")
                action = task_doc.get("action", "TRANSFORM")

                # iter-20 — a synthetic BUILD MANIFEST task has no
                # `source_path` (there is no 1:1 legacy file to transform),
                # so it used to reach the Coder with source_content="" — the
                # target pom was authored having never seen the source pom.
                # Hand it the manifest it is migrating from.
                if not source_content and task_doc.get("source_manifest_content"):
                    source_content = task_doc["source_manifest_content"]
                    source_path = task_doc.get("source_manifest_path") or source_path

                await transformer_tasks.update_one(
                    {"_id": task_doc["_id"]},
                    {"$set": {"status": "IN_PROGRESS", "updated_at": datetime.now(timezone.utc).isoformat()}},
                )

                # Snapshot progress: last-writer-wins is fine for the
                # UI — we're not gating anything on this value.
                async with done_counter_lock:
                    idx_snapshot = done_counter["n"]
                pct = 25 + int(60 * (idx_snapshot / max(total_tasks, 1)))
                await _update_progress(
                    transform_id,
                    phase="coder",
                    phase_label=f"Coder: {idx_snapshot + 1}/{total_tasks} — {source_path}",
                    progress_pct=pct,
                    files_done=idx_snapshot,
                    current_file=source_path,
                )

                if action == "DELETE":
                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {"status": "DONE", "updated_at": datetime.now(timezone.utc).isoformat()}},
                    )
                    async with done_counter_lock:
                        done_counter["n"] += 1
                    return

                if action == "NO_CHANGE" and source_content:
                    new_path = task_doc.get("target_path") or source_path
                    await transform_files.insert_one({
                        "transform_id": transform_id,
                        "type": "transformed",
                        "path": new_path,
                        "original_path": source_path,
                        "content": source_content,
                        "confidence": 1.0,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    async with transformed_files_lock:
                        transformed_files.append({"path": new_path, "content": source_content})
                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {"status": "DONE", "confidence": 1.0, "updated_at": datetime.now(timezone.utc).isoformat()}},
                    )
                    async with done_counter_lock:
                        done_counter["n"] += 1
                    return

                if not source_content and action in ("TRANSFORM", "REWRITE"):
                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {"status": "BLOCKED", "error": "Source file not found in upload", "updated_at": datetime.now(timezone.utc).isoformat()}},
                    )
                    return

                try:
                    envelope_for_task = envelopes_by_id.get(task_doc.get("envelope_id"))
                    # iter-15.42 — File-scoped KB slice: pass only the KB
                    # signal the envelope actually references (its own
                    # endpoint / tables / classes), not the ~15KB global
                    # summary. Falls back to the trimmed global summary
                    # when the file has no linked envelope.
                    kb_slice = _slim_kb_ctx_for_task(kb_ctx, envelope_for_task, source_path)

                    transformed_content = await _run_coder(
                        transform_id, task_doc, source_content, kb_slice,
                        detected_stack, target_stack, model,
                        envelope=envelope_for_task,
                    )

                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {"status": "CODED", "updated_at": datetime.now(timezone.utc).isoformat()}},
                    )

                    verify_result = await _run_verifier(
                        transform_id, task_doc, source_content, transformed_content, model,
                    )
                    confidence = float(verify_result.get("confidence", 0)) / 100.0
                    verdict = verify_result.get("verdict", "REJECT")

                    # iter-16.x — Retry gate now keys off the numeric score
                    # against VERIFIER_ACCEPT_THRESHOLD (95%), not the LLM's
                    # self-reported verdict string. A file the LLM labelled
                    # ACCEPT_WITH_NOTES at, say, 88% now gets the same
                    # feedback-and-retry loop as an explicit REJECT — it's
                    # below the bar the operator asked for, so give the
                    # Coder a chance to close the gap before we give up.
                    if (float(verify_result.get("confidence", 0)) < VERIFIER_ACCEPT_THRESHOLD
                            and task_doc.get("rejection_count", 0) < 2):
                        await transformer_tasks.update_one(
                            {"_id": task_doc["_id"]},
                            {"$set": {"rejection_count": task_doc.get("rejection_count", 0) + 1}},
                        )
                        issues = verify_result.get("issues", [])
                        fix_notes = "; ".join(i.get("fix", i.get("description", "")) for i in issues[:5])
                        task_doc["notes"] = (
                            f"Verifier scored {verify_result.get('confidence', 0)}% "
                            f"(need >= {VERIFIER_ACCEPT_THRESHOLD:.0f}%). Fix: {fix_notes}"
                        )
                        transformed_content = await _run_coder(
                            transform_id, task_doc, source_content, kb_slice,
                            detected_stack, target_stack, model,
                            envelope=envelope_for_task,
                        )
                        verify_result = await _run_verifier(
                            transform_id, task_doc, source_content, transformed_content, model,
                        )
                        confidence = float(verify_result.get("confidence", 0)) / 100.0
                        verdict = verify_result.get("verdict", "REJECT")

                    # iter-15.57 — Honour the final verdict. A file that
                    # is still below the acceptance bar after retries must
                    # NOT be labelled VERIFIED. It is persisted with
                    # `compilable=False` so downstream (Tester / Compiler)
                    # surfaces it, and the task status reflects the real
                    # state.
                    # iter-16.x — Acceptance is now a HARD 95% confidence
                    # threshold (operator-requested), independent of the
                    # LLM's verdict label. This replaces the previous
                    # verdict-string check (which — bugged — required a
                    # literal "ACCEPT", then briefly also allowed
                    # "ACCEPT_WITH_NOTES" at 85-94%). The confidence score
                    # itself is the single source of truth for pass/fail.
                    passing = float(verify_result.get("confidence", 0)) >= VERIFIER_ACCEPT_THRESHOLD
                    final_status = "VERIFIED" if passing else "VERIFY_FAILED"
                    structural = verify_result.get("structural_check") or {}
                    compilable_flag = passing and structural.get("ok", True)

                    new_path = task_doc.get("target_path") or _transform_path(source_path, target_stack)
                    # iter-16.x — Belt-and-braces: `_run_coder` already
                    # scrubs via `_extract_code` → `_scrub_code_fences`,
                    # but real-world runs still occasionally shipped a
                    # ```java opener when the LLM emitted nested fence
                    # constructs that survived the first strip pass. Run
                    # the idempotent scrub one more time at the DB write
                    # so a fence CANNOT survive into `transform_files` no
                    # matter what shape the Coder / Verifier retry hands
                    # us. Same helper is applied by the compile-fix loop
                    # in `_run_one`.
                    transformed_content = _scrub_code_fences(transformed_content)
                    await transform_files.insert_one({
                        "transform_id": transform_id,
                        "type": "transformed",
                        "path": new_path,
                        "original_path": source_path,
                        "content": transformed_content,
                        "confidence": confidence,
                        "verifier_result": verify_result,
                        "compilable": compilable_flag,
                        "final_verdict": verdict,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    async with transformed_files_lock:
                        transformed_files.append({"path": new_path, "content": transformed_content})

                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {
                            "status": final_status,
                            "confidence": confidence,
                            "verifier_score": float(verify_result.get("confidence", 0)),
                            "verifier_checks": verify_result,
                            "compilable": compilable_flag,
                            "final_verdict": verdict,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                    )
                    async with done_counter_lock:
                        done_counter["n"] += 1

                except Exception as e:
                    await transform_files.insert_one({
                        "transform_id": transform_id,
                        "type": "error",
                        "path": source_path,
                        "original_path": source_path,
                        "error": str(e),
                        "confidence": 0.0,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    await transformer_tasks.update_one(
                        {"_id": task_doc["_id"]},
                        {"$set": {"status": "BLOCKED", "error": str(e), "updated_at": datetime.now(timezone.utc).isoformat()}},
                    )
                    _emit_log(transform_id, "error", f"Task {task_doc.get('task_id', '')} failed: {e}")

        # Drive the pipeline wave-by-wave, parallel within each wave.
        stopped_early = False
        for wave_no in wave_numbers:
            state = await _check_pause_stop()
            if state == "stopped":
                await _update_progress(
                    transform_id, status="stopped", phase="stopped",
                    phase_label=f"Stopped at wave {wave_no}/{len(wave_numbers)}",
                )
                stopped_early = True
                break

            wave_tasks = waves_map[wave_no]
            _emit_log(
                transform_id, "info",
                f"[iter-15.42] Wave {wave_no}: dispatching {len(wave_tasks)} "
                f"task(s) with concurrency={_coder_concurrency}",
            )
            await asyncio.gather(
                *(_process_single_task(td) for td in wave_tasks),
                return_exceptions=False,   # per-task errors are already
                                           # caught inside _process_single_task
            )

        if stopped_early:
            return

        done_count = done_counter["n"]

        # ── Phase 4: Compile + Static Analysis ──────────────────────
        # iter-15.44 — Real subprocess compile using the build tools the
        # operator confirmed in step 1 (`transformation.build_tools`),
        # PLUS the legacy LLM static-analysis narrative as a supplement.
        await _update_progress(
            transform_id,
            phase="tester",
            phase_label="Tester: Compiling transformed code",
            progress_pct=90,
            files_done=done_count,
        )

        build_tools_map = (transform or {}).get("build_tools") or {}
        compile_result = None
        coverage_result = None
        test_gen_result = None
        dependency_audit = None
        workspace_root = None
        try:
            if build_tools_map and transformed_files:
                # iter-15.59 — Compile + Planner→Coder fix loop, so the
                # pipeline itself repairs failing files instead of just
                # reporting them. Only after the loop terminates do we
                # materialise the workspace for test-gen + coverage,
                # using the possibly-updated file contents.
                compile_result = await _run_compile_fix_loop(
                    transform_id, build_tools_map, model,
                )

                # iter-19 — DevOps gate, moved here from the tail of the
                # run. It used to execute at 97% AFTER `final_status` was
                # already decided, which made its verdict decorative. Now
                # it runs while there is still time to act on it: findings
                # go back to the Planner, the DevOps Expert repairs the
                # manifests, and the build is recompiled before test-gen
                # and coverage see the tree.
                await _update_progress(
                    transform_id,
                    phase="devops",
                    phase_label="DevOps: Auditing build manifests",
                    progress_pct=91,
                    files_done=done_count,
                )
                _devops = await _run_devops_remediation_loop(
                    transform_id, compile_result, build_tools_map, model,
                )
                dependency_audit = _devops["audit"]
                compile_result = _devops["compile_result"]
                if not dependency_audit.get("production_ready"):
                    _emit_log(
                        transform_id, "warn",
                        f"DevOps audit: not production-ready — {dependency_audit.get('summary', '')}",
                        agent="devops_expert", phase="dependency-audit",
                    )

                # Refresh transformed_files from Mongo — the fix loop may
                # have rewritten some rows in place.
                _cur = transform_files.find(
                    {"transform_id": transform_id, "type": "transformed"},
                    {"path": 1, "content": 1},
                )
                transformed_files = [
                    {"path": d.get("path", ""), "content": d.get("content", "")}
                    async for d in _cur
                ] or transformed_files
                workspace_root = _write_transformed_workspace(transformed_files)

                # iter-15.45 — Three-tier test generation. Runs whether
                # the compile passed or failed — even a partially-failing
                # build ships with a starter suite the operator can use
                # as a regression net.
                await _update_progress(
                    transform_id,
                    phase="tester",
                    phase_label="Tester: Generating business/api/integration tests",
                    progress_pct=93,
                    files_done=done_count,
                )
                test_gen_result = await _run_test_generator(
                    transform_id,
                    list(envelopes_by_id.values()),
                    target_stack,
                    model=model,
                )

                # Materialise generated tests into the workspace so the
                # coverage runner picks them up alongside the transformed
                # code before the temp dir is torn down.
                if test_gen_result.get("total", 0) > 0:
                    test_docs = await transform_files.find(
                        {"transform_id": transform_id, "type": "test"},
                        {"path": 1, "content": 1},
                    ).to_list(2000)
                    _tests_for_workspace = [
                        {"path": t.get("path", ""), "content": t.get("content", "")}
                        for t in test_docs
                    ]
                    # Reuse _write_transformed_workspace's path-sanitising
                    # logic by materialising into the SAME workspace_root.
                    for f in _tests_for_workspace:
                        rel = (f.get("path") or "").lstrip("/").replace("..", "")
                        if not rel:
                            continue
                        target = os.path.join(workspace_root, rel)
                        os.makedirs(os.path.dirname(target) or workspace_root, exist_ok=True)
                        try:
                            with open(target, "w", encoding="utf-8", errors="replace") as fh:
                                fh.write(f.get("content") or "")
                        except Exception:
                            continue

                # iter-15.45 — Coverage measurement. Best-effort — skip
                # cleanly when the coverage tool isn't on PATH.
                await _update_progress(
                    transform_id,
                    phase="tester",
                    phase_label="Tester: Measuring coverage",
                    progress_pct=97,
                    files_done=done_count,
                )
                coverage_result = await _run_coverage(
                    transform_id, workspace_root, build_tools_map,
                )
        except Exception as _compile_exc:
            compile_result = {
                "compilation_ready": False,
                "overall_score": 0,
                "summary": f"Compile phase crashed: {_compile_exc}",
                "components": [],
                "mode": "native",
            }
        finally:
            if workspace_root and os.path.isdir(workspace_root):
                shutil.rmtree(workspace_root, ignore_errors=True)

        static_analysis = await _run_tester(transform_id, transformed_files, target_stack, model)

        if compile_result is None:
            # No build_tools persisted (older transformation created
            # before iter-15.40) — fall back to LLM narrative only.
            compilation_result = {
                **static_analysis,
                "mode": "static_only",
                "static_analysis": static_analysis,
            }
        else:
            compilation_result = {
                **compile_result,
                "static_analysis": static_analysis,
                "test_generation": test_gen_result,
                "coverage": coverage_result,
            }

        # Store compilation result
        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {"compilation_result": compilation_result}},
        )

        # ── Finalize ────────────────────────────────────────────────
        avg_confidence = 0.0
        if done_count > 0:
            conf_sum = 0.0
            async for row in transform_files.find(
                {"transform_id": transform_id, "type": "transformed"},
                {"confidence": 1},
            ):
                conf_sum += float(row.get("confidence", 0))
            total_transformed = await transform_files.count_documents(
                {"transform_id": transform_id, "type": "transformed"}
            )
            avg_confidence = round(conf_sum / max(total_transformed, 1), 2)

        manual_review_count = await transform_files.count_documents(
            {"transform_id": transform_id, "type": "error"}
        )

        # iter-15.59 — Final compile-status gate. When a build_tools map
        # was configured and the fix loop couldn't turn the build green,
        # the transformation ends in `completed_with_errors` (not the
        # green `completed`). The FE header + stepper both key off this
        # so the operator sees the honest state and can click the
        # Compile Console "Rerun compile" button to try again.
        compile_green = True
        if build_tools_map:
            compile_green = bool((compilation_result or {}).get("compilation_ready"))

        # iter-19 — the DevOps verdict is now part of this gate rather than
        # an observation made after it. `production_ready` was previously
        # computed, stored, and ignored: a run could report the green
        # `completed` while shipping a pom Maven cannot resolve. It is only
        # consulted when an audit actually ran (no build_tools map means no
        # manifests to audit, and absence of evidence must not fail a run).
        production_ready = bool((dependency_audit or {}).get("production_ready"))
        devops_blocked = bool(dependency_audit) and not production_ready

        final_status = "completed" if (compile_green and not devops_blocked) else "completed_with_errors"
        if not compile_green:
            final_phase_label = "Pipeline finished — compilation errors remain, click Rerun compile"
        elif devops_blocked:
            final_phase_label = (
                "Pipeline finished — the build compiles but its dependencies are not "
                "production-ready; see the DevOps audit"
            )
        else:
            final_phase_label = "Multi-agent pipeline completed"

        result = {
            "files_processed": total,
            "files_transformed": done_count,
            "manual_review_required": manual_review_count,
            "avg_confidence": avg_confidence,
            "compilation_result": compilation_result,
            "compile_green": compile_green,
            "dependency_audit": dependency_audit,
            "production_ready": production_ready,
            "status": final_status,
        }

        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {
                "status": final_status,
                "phase": final_status,
                "phase_label": final_phase_label,
                "progress_pct": 100,
                "files_done": total,
                "files_total": total,
                "current_file": None,
                "result": result,
                "files_transformed": done_count,
                "manual_review_count": manual_review_count,
                "avg_confidence": avg_confidence,
                "compile_green": compile_green,
                "production_ready": production_ready,
                "dependency_audit": dependency_audit,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }},
        )

        if not compile_green:
            _verdict = " — build is RED, awaiting rerun-compile"
        elif devops_blocked:
            _rounds = (dependency_audit or {}).get("remediation_rounds_used", 0)
            _verdict = (
                f" — build is GREEN but DevOps blocked production-readiness "
                f"after {_rounds} remediation round(s)"
            )
        else:
            _verdict = ""
        await _log_agent_run(transform_id, "super_agent", "finalize",
            status="completed",
            output_summary=(
                f"Pipeline complete: {done_count} transformed, "
                f"{manual_review_count} errors, avg confidence {avg_confidence}"
                + _verdict
            ),
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _update_progress(
            transform_id,
            status="failed", phase="failed", phase_label="Failed", error=str(e),
        )


# ── Multi-Agent Endpoints ──────────────────────────────────────────────

@router.post("/transformer/{transform_id}/run-multi-agent", status_code=202)
async def run_multi_agent_transformation(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """Start the multi-agent transformation pipeline.

    Phase 1 runs CM, then pauses at 'awaiting_confirmation'.
    User reviews envelopes, then calls /confirm-plan to continue.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "pipeline_mode": "multi_agent",
            "status": "running",
            "phase": "super_agent",
            "phase_label": "Super Agent: Initializing pipeline",
            "progress_pct": 2,
            "paused": False, "stopped": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    background_tasks.add_task(_run_multi_agent_transformation, transform_id, model)
    return {"status": "started", "transform_id": transform_id, "pipeline_mode": "multi_agent"}


@router.post("/transformer/{transform_id}/confirm-plan")
async def confirm_transformation_plan(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """User confirms the discovered envelopes — continue the pipeline.

    Called after the CM phase has paused at 'awaiting_confirmation'.
    Marks all envelopes as approved and kicks off the Planner, which then
    pauses AGAIN at 'awaiting_task_confirmation' (see
    `/transformer/{transform_id}/tasks/confirm`) before any file is coded.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    if transform.get("status") != "awaiting_confirmation":
        raise HTTPException(400, f"Cannot confirm — current status is '{transform.get('status')}'")

    # Mark all envelopes as approved
    await transformer_envelopes.update_many(
        {"transform_id": transform_id},
        {"$set": {"status": "approved"}},
    )

    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "status": "running",
            "phase": "planner",
            "phase_label": "Planner: Creating task list",
            "progress_pct": 18,
            # iter-15.41 — legacy `/plan/confirm` also stamps this so the
            # new traceability read endpoint reflects prior confirmations
            # for older transformations resumed after the upgrade.
            "traceability_confirmed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    background_tasks.add_task(_continue_multi_agent_after_confirm, transform_id, model)
    return {"status": "confirmed", "transform_id": transform_id}


@router.post("/transformer/{transform_id}/tasks/confirm")
async def confirm_transformation_tasks(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """iter-15.28 — User confirms the Planner's task list — continue the
    pipeline into Coder → Verifier → Tester.

    Called after the Planner phase has paused at
    'awaiting_task_confirmation' (the second human-in-the-loop gate,
    right after the envelope-confirm gate). This is the review point
    where the operator can see exactly which files will be TRANSFORMed/
    REWRITTEN/DELETEd/left as NO_CHANGE, in what wave order, before any
    code generation actually happens.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    if transform.get("status") != "awaiting_task_confirmation":
        raise HTTPException(400, f"Cannot confirm — current status is '{transform.get('status')}'")

    await transformer_tasks.update_many(
        {"transform_id": transform_id, "status": {"$in": [None, "", "PENDING"]}},
        {"$set": {"status": "APPROVED"}},
    )

    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "status": "running",
            "phase": "coder",
            "phase_label": "Coder: Transforming files",
            "progress_pct": 25,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    background_tasks.add_task(_continue_multi_agent_after_task_confirm, transform_id, model)
    return {"status": "confirmed", "transform_id": transform_id}


@router.get("/transformer/{transform_id}/envelopes")
async def get_transformation_envelopes(transform_id: str):
    """Get API-to-DB envelopes discovered by the Context Manager."""
    cursor = transformer_envelopes.find({"transform_id": transform_id})
    docs = await cursor.to_list(500)
    envelopes = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        envelopes.append(d)
    return {"envelopes": envelopes, "total": len(envelopes)}


def _traceability_mode_for(transforms: Dict[str, Any]) -> str:
    """iter-15.41 — Decide which traceability view the FE should render:
    'backend' → API → DB hop chain table (Controller/Service/Repository
                /Tables — the classic envelope shape).
    'frontend' → screens/components → API call mapping with CSS-only
                 wireframes (used when the transformation is FE-only).
    'fullstack' → BOTH — render two panels stacked.
    """
    has_be = bool((transforms or {}).get("backend") or (transforms or {}).get("runtime"))
    has_fe = bool((transforms or {}).get("frontend"))
    if has_be and has_fe:
        return "fullstack"
    if has_fe and not has_be:
        return "frontend"
    return "backend"


def _extract_frontend_traceability(src_files: List[Dict[str, Any]], envelopes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """iter-15.41 — For FE mode, scan source files and pull out (screen,
    api_call) pairs. Heuristic — matches HTTP client patterns commonly
    used in JSP/jQuery/React/Angular/Vue source:

        fetch("/api/users")
        axios.get("/api/orders")
        $.ajax({url: "/api/foo"})
        this.http.get("/api/bar")     (Angular / RxJS)

    Returns a list of `{screen, elements[], api_calls[]}` records, one
    per FE source file that touches at least one API. Powers the CSS-
    wireframe render on the frontend traceability confirmation gate."""
    if not src_files:
        return []

    endpoint_index: Dict[str, Dict[str, Any]] = {}
    for env in envelopes or []:
        p = str(env.get("endpoint_path") or "").strip()
        if p:
            endpoint_index[p] = env

    fe_ext = {".jsp", ".js", ".jsx", ".ts", ".tsx", ".vue", ".html", ".htm"}
    call_re = re.compile(
        r"""(?:fetch|axios(?:\.(?:get|post|put|delete|patch))?|"""
        r"""\$\.(?:ajax|get|post)|this\.http\.(?:get|post|put|delete|patch)|"""
        r"""http\.(?:get|post|put|delete|patch))"""
        r"""\s*\(\s*(?:\{[^}]*?url\s*:\s*)?['"`]([^'"`]+)['"`]""",
        re.IGNORECASE,
    )
    ui_el_re = re.compile(
        r"""<(button|form|input|select|table|a)\b[^>]*(?:id\s*=\s*['"]([^'"]*)['"]|"""
        r"""name\s*=\s*['"]([^'"]*)['"]|placeholder\s*=\s*['"]([^'"]*)['"])""",
        re.IGNORECASE,
    )

    rows: List[Dict[str, Any]] = []
    for f in src_files:
        path = f.get("path", "")
        _, ext = os.path.splitext(path.lower())
        if ext not in fe_ext:
            continue
        content = f.get("content", "") or ""
        if not content:
            continue

        apis: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for m in call_re.finditer(content):
            url = (m.group(1) or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            env = endpoint_index.get(url)
            apis.append({
                "url": url,
                "resolved_controller": (env or {}).get("controller_class") or None,
                "resolved_service": (env or {}).get("service_class") or None,
                "resolved_tables": list((env or {}).get("db_tables") or []),
            })

        if not apis:
            continue

        elements: List[Dict[str, str]] = []
        el_seen: Set[str] = set()
        for m in ui_el_re.finditer(content):
            kind = (m.group(1) or "").lower()
            label = m.group(2) or m.group(3) or m.group(4) or ""
            label = label.strip()[:40]
            key = f"{kind}:{label}"
            if key in el_seen:
                continue
            el_seen.add(key)
            elements.append({"kind": kind, "label": label})
            if len(elements) >= 8:
                break

        screen = os.path.basename(path)
        rows.append({
            "screen": screen,
            "screen_path": path,
            "elements": elements,
            "api_calls": apis,
        })

    return rows


@router.get("/transformer/{transform_id}/traceability")
async def get_transformation_traceability(transform_id: str):
    """iter-15.41 — UI-ready traceability data for the mandatory
    confirmation gate that sits between Context Manager and Planner.

    - mode="backend" → envelopes formatted as a compact hop-chain table
      (endpoint → controller → service → repository → tables + business
      summary + top request/response contract fields).
    - mode="frontend" → screens/components → API calls, plus a rough
      element list per screen for the CSS-only wireframe render.
    - mode="fullstack" → both blocks populated.

    Also returns `confirmed_at` so the FE can reflect prior confirmation
    on page reload without a separate call."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    transforms_map = transform.get("transforms") or {}
    mode = _traceability_mode_for(transforms_map)

    env_cursor = transformer_envelopes.find({"transform_id": transform_id})
    envelope_docs = await env_cursor.to_list(500)

    backend_rows: List[Dict[str, Any]] = []
    if mode in ("backend", "fullstack"):
        for d in envelope_docs:
            req = d.get("request") or {}
            resp = d.get("response") or {}
            data = d.get("data_layer") or {}
            trace = data.get("table_trace") or []
            backend_rows.append({
                "envelope_id": d.get("envelope_id"),
                "endpoint_method": d.get("endpoint_method"),
                "endpoint_path": d.get("endpoint_path"),
                "controller_class": d.get("controller_class"),
                "service_class": d.get("service_class") or (d.get("service_layer") or {}).get("service_impl"),
                "repository_class": d.get("repository_class") or data.get("repository_class"),
                "db_tables": d.get("db_tables") or data.get("db_tables") or [],
                "business_logic_summary": d.get("business_logic_summary"),
                "request_dto": req.get("dto_class"),
                "response_type": resp.get("result_type"),
                "table_trace": trace[:15],
                "user_edited": bool(d.get("user_edited")),
            })

    frontend_rows: List[Dict[str, Any]] = []
    if mode in ("frontend", "fullstack"):
        src_docs = await transform_files.find(
            {"transform_id": transform_id, "type": "source"}
        ).to_list(2000)
        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in src_docs]
        # Envelopes need lightweight shape for FE cross-ref
        env_lite = [{
            "endpoint_path": d.get("endpoint_path"),
            "controller_class": d.get("controller_class"),
            "service_class": d.get("service_class"),
            "db_tables": d.get("db_tables") or (d.get("data_layer") or {}).get("db_tables") or [],
        } for d in envelope_docs]
        frontend_rows = _extract_frontend_traceability(src_files, env_lite)

    return {
        "mode": mode,
        "backend_rows": backend_rows,
        "frontend_rows": frontend_rows,
        "backend_total": len(backend_rows),
        "frontend_total": len(frontend_rows),
        "confirmed_at": transform.get("traceability_confirmed_at"),
        "current_status": transform.get("status"),
    }


@router.post("/transformer/{transform_id}/confirm-traceability")
async def confirm_transformation_traceability(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
):
    """iter-15.41 — Mandatory human-in-the-loop gate that sits between
    Context Manager and Planner. Same contract as the pre-existing
    `confirm_transformation_plan` (approves all envelopes + kicks off
    Planner), but ALSO stamps `traceability_confirmed_at` on the
    transformation doc so the FE can show "already confirmed" on reload
    and so any future re-run knows this decision was explicitly made.

    Kept as a distinct endpoint (rather than folded into the existing
    `/envelopes/confirm`) because it carries different UX semantics: the
    operator is confirming they have REVIEWED the traceability, not just
    that the envelope shapes look OK. Both endpoints continue to exist —
    older UI paths still work — but the new UI uses this one."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    if transform.get("status") != "awaiting_confirmation":
        raise HTTPException(
            400,
            f"Cannot confirm traceability — current status is "
            f"'{transform.get('status')}'. Traceability confirmation is "
            f"only valid immediately after Context Manager completes.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    await transformer_envelopes.update_many(
        {"transform_id": transform_id},
        {"$set": {"status": "approved"}},
    )
    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "status": "running",
            "phase": "planner",
            "phase_label": "Planner: Creating task list",
            "progress_pct": 18,
            "traceability_confirmed_at": now_iso,
            "updated_at": now_iso,
        }},
    )
    await _log_audit("confirm_traceability", "transformation", transform_id, {
        "envelope_count": await transformer_envelopes.count_documents({"transform_id": transform_id})
        if hasattr(transformer_envelopes, "count_documents") else None,
    })

    background_tasks.add_task(_continue_multi_agent_after_confirm, transform_id, model)
    return {
        "status": "confirmed",
        "transform_id": transform_id,
        "traceability_confirmed_at": now_iso,
    }


@router.patch("/transformer/{transform_id}/envelopes/{envelope_id}")
async def update_transformer_envelope(transform_id: str, envelope_id: str, payload: EnvelopeEditRequest):
    """iter-15.26 — Let a reviewer manually correct one envelope (fix a
    misresolved service/repository class, add DB tables the heuristic
    missed, tighten the business-logic summary, etc.). Marks the
    envelope `user_edited: true` so a later 'Regenerate' pass (see
    `regenerate_transformer_envelopes`) reapplies these exact corrections
    on top of freshly re-discovered data instead of silently overwriting
    them — this is the persistence half of 'give input and regenerate to
    recover the gap'."""
    doc = await transformer_envelopes.find_one({"transform_id": transform_id, "envelope_id": envelope_id})
    if not doc:
        raise HTTPException(404, "Envelope not found")

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    updates["user_edited"] = True
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    await transformer_envelopes.update_one(
        {"transform_id": transform_id, "envelope_id": envelope_id},
        {"$set": updates},
    )
    await _log_audit("update_envelope", "transformation", transform_id, {
        "envelope_id": envelope_id,
        "fields": list(updates.keys()),
    })
    updated = await transformer_envelopes.find_one({"transform_id": transform_id, "envelope_id": envelope_id})
    updated["id"] = str(updated.pop("_id"))
    return updated


async def _regenerate_context_manager(transform_id: str, model: str = None):
    """iter-15.26 — Background task for 'Regenerate' (Discovered
    Architecture header button). Re-runs ONLY the Context Manager phase
    (envelope discovery) — unlike `/rerun`, this does NOT touch tasks,
    generated files, or agent-run history, and it PRESERVES any
    `user_edited` envelope's manual corrections by keying them off
    `endpoint_path` and reapplying them onto the freshly regenerated
    envelope set inside `_run_context_manager`."""
    try:
        transform = await transformations.find_one({"_id": transform_id})
        if not transform:
            return

        existing = await transformer_envelopes.find({
            "transform_id": transform_id, "user_edited": True,
        }).to_list(500)
        preserved_edits: Dict[str, Dict[str, Any]] = {}
        for e in existing:
            path = e.get("endpoint_path")
            if not path:
                continue
            preserved_edits[path] = {k: e.get(k) for k in ENVELOPE_EDITABLE_FIELDS if k in e}

        detected_stack = transform.get("source_stack") or {}
        source_files_docs = await transform_files.find(
            {"transform_id": transform_id, "type": "source"}
        ).to_list(2000)
        src_files = [{"path": s.get("path", ""), "content": s.get("content", "")} for s in source_files_docs]

        await transformer_envelopes.delete_many({"transform_id": transform_id})
        await _update_progress(
            transform_id, status="running", phase="context_manager",
            phase_label="Context Manager: Regenerating APIs & tables",
            progress_pct=5,
        )

        await _run_context_manager(
            transform_id, src_files, detected_stack, model, preserved_edits=preserved_edits,
        )

        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {
                "status": "awaiting_confirmation",
                "phase": "context_manager",
                "phase_label": "Context Manager: Awaiting confirmation",
                "progress_pct": 15,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        await _log_audit("regenerate_envelopes", "transformation", transform_id, {
            "preserved_count": len(preserved_edits),
        })
    finally:
        # iter-15.15-style lease cleanup — there's no separate "release"
        # helper in this file; other background workers just clear
        # worker_pid/worker_heartbeat directly so the next acquire (or a
        # stale-lease timeout) can proceed immediately instead of waiting
        # out `_WORKER_LEASE_STALE_SECS`.
        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {"worker_pid": None, "worker_heartbeat": None}},
        )


@router.post("/transformer/{transform_id}/envelopes/regenerate", status_code=202)
async def regenerate_transformer_envelopes(transform_id: str, background_tasks: BackgroundTasks):
    """iter-15.26 — Re-run just the discovery (Context Manager) phase to
    recover gaps in "Discovered Architecture" — e.g. after saving manual
    corrections on some envelopes, or after a prompt/model override was
    changed for the Context Manager agent — WITHOUT wiping the rest of
    the pipeline (tasks/generated files) the way a full `/rerun` does,
    and WITHOUT discarding any envelope already marked `user_edited`."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    if transform.get("status") == "running":
        raise HTTPException(400, "Pipeline is already running — stop it first")
    if not await _acquire_worker_lease(transform_id):
        raise HTTPException(409, "Another operation is already in progress for this transformation")

    background_tasks.add_task(_regenerate_context_manager, transform_id, transform.get("model"))
    return {"status": "started", "transform_id": transform_id}


@router.post("/transformer/{transform_id}/agent-chat")
async def agent_plan_chat(transform_id: str, payload: AgentPlanChatRequest):
    """iter-15.38 — Chat assistant embedded in the Planner ("Planner review
    required"), Coder ("Code Generation"), and Tester/Verifier panels.

    The user types a natural-language instruction (e.g. "remove all test
    files from wave 2", "find files touching the Order table", "move the
    DAO classes to wave 3", "regenerate the failed controller files") and
    an LLM maps it onto the CURRENT task/file list for THIS transformation
    and returns a small set of structured actions, which are applied
    immediately and persisted to Mongo (`transformer_tasks` / `transform_files`)
    — no separate "Save" step, per the immediate-persistence requirement.

    `panel` scopes which action verbs are legal:
      - planner: find, remove, reassign_wave
      - coder:   find, remove (deletes the generated file, resets the task
                 to PENDING so it can be regenerated), regenerate
      - tester:  find, remove (dismisses the verification result — clears
                 verifier_checks/verifier_score, resets status)
    """
    panel = (payload.panel or "").strip().lower()
    if panel not in ("planner", "coder", "tester"):
        raise HTTPException(400, "panel must be one of: planner, coder, tester")

    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    task_docs = await transformer_tasks.find({"transform_id": transform_id}).sort("wave", 1).to_list(3000)
    if not task_docs:
        return {
            "reply": "There are no Planner tasks yet for this run — nothing to search or edit.",
            "matched_ids": [],
            "actions_applied": [],
            "tasks": await get_transformation_tasks(transform_id),
        }

    file_docs_by_path: Dict[str, dict] = {}
    if panel == "coder":
        file_cursor = transform_files.find({"transform_id": transform_id, "type": "transformed"})
        async for f in file_cursor:
            key = f.get("original_path") or f.get("path")
            if key:
                file_docs_by_path[key] = f

    # Compact digest — keep it small; the LLM only needs enough signal to
    # resolve "the DAO files" / "wave 2" / "files touching Order" to task_ids.
    digest_lines = []
    for t in task_docs:
        checks = t.get("verifier_checks") or {}
        line = (
            f"{t.get('task_id')}\twave={t.get('wave')}\tlayer={t.get('layer', '')}\t"
            f"action={t.get('action', 'TRANSFORM')}\tstatus={t.get('status', 'PENDING')}\t"
            f"src={t.get('source_path', '')}\ttgt={t.get('target_path', '')}"
        )
        if panel == "coder":
            has_file = bool(file_docs_by_path.get(t.get("source_path", "")))
            line += f"\thas_generated_file={has_file}"
        if panel == "tester":
            line += f"\tverdict={checks.get('verdict', '')}\tconfidence={checks.get('confidence', t.get('verifier_score', ''))}"
        digest_lines.append(line)
    digest = "\n".join(digest_lines)[:24000]

    panel_rules = {
        "planner": (
            "This is the PLANNER review screen. Allowed action types: "
            "\"find\" (just highlight matches, no mutation), "
            "\"remove\" (permanently delete the matching task(s) from the plan), "
            "\"reassign_wave\" (move matching task(s) to a different wave — include "
            "an integer \"wave\" 1-9 in the action)."
        ),
        "coder": (
            "This is the CODE GENERATION screen (generated files). Allowed action "
            "types: \"find\" (highlight matches), \"remove\" (delete the generated "
            "file for the matching task(s) and reset that task to re-generatable), "
            "\"regenerate\" (re-run code generation for the matching task(s))."
        ),
        "tester": (
            "This is the VERIFICATION / TESTER screen. Allowed action types: "
            "\"find\" (highlight matches), \"remove\" (dismiss the verification "
            "result for the matching task(s) so it can be re-verified)."
        ),
    }

    system_prompt = f"""You are the review assistant embedded in the LAMA Code Transformer's
{panel.upper()} panel. {panel_rules[panel]}

You will be given a TAB-separated digest of the current items (one per line) and a
user instruction in plain English. Resolve the instruction to the task_id(s) it refers
to using the source/target path, layer, wave, action, status, or (if present) verdict
and confidence columns — e.g. "DAO files" means source/target paths under a
repository/dao-ish path or layer=repository; "wave 2" means wave==2; "failed tests"
means verdict!=PASS.

Return ONLY strict JSON, no prose outside the JSON:
{{"reply": "<one short sentence for the user>", "actions": [{{"type": "find|remove|reassign_wave|regenerate", "task_ids": ["..."], "wave": <int, only for reassign_wave>}}]}}

If nothing matches, return an empty "actions" array and explain why in "reply".
Never invent a task_id that isn't in the digest."""

    user_prompt = f"===== CURRENT ITEMS ({len(task_docs)}) =====\n{digest}\n\n===== USER INSTRUCTION =====\n{payload.message.strip()}"

    try:
        response = await fabric_call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=payload.model or transform.get("model"),
            agent_key="tools.transformer.chat",
            project_id=await _project_id_for_transform(transform_id),
            temperature=0.1,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        response_text = response.get("content", "") if isinstance(response, dict) else str(response)
    except Exception as e:
        raise HTTPException(500, f"Chat assistant failed: {e}")

    parsed = _extract_json_object(response_text) or {}
    reply = parsed.get("reply") or "Done."
    raw_actions = parsed.get("actions") if isinstance(parsed.get("actions"), list) else []

    valid_task_ids = {t.get("task_id") for t in task_docs}
    matched_ids: List[str] = []
    actions_applied: List[dict] = []

    for action in raw_actions:
        if not isinstance(action, dict):
            continue
        a_type = str(action.get("type", "")).strip().lower()
        task_ids = [tid for tid in (action.get("task_ids") or []) if tid in valid_task_ids]
        if not task_ids:
            continue
        matched_ids.extend(task_ids)

        if a_type == "find":
            actions_applied.append({"type": "find", "task_ids": task_ids})

        elif a_type == "remove" and panel == "planner":
            await transformer_tasks.delete_many({"transform_id": transform_id, "task_id": {"$in": task_ids}})
            actions_applied.append({"type": "remove", "task_ids": task_ids})

        elif a_type == "remove" and panel == "coder":
            removed = 0
            for tid in task_ids:
                task = next((t for t in task_docs if t.get("task_id") == tid), None)
                if not task:
                    continue
                src = task.get("source_path", "")
                fdoc = file_docs_by_path.get(src)
                if fdoc:
                    await transform_files.delete_one({"_id": fdoc["_id"]})
                    removed += 1
                await transformer_tasks.update_one(
                    {"transform_id": transform_id, "task_id": tid},
                    {"$set": {"status": "PENDING"}, "$unset": {"verifier_checks": "", "verifier_score": ""}},
                )
            actions_applied.append({"type": "remove", "task_ids": task_ids, "files_deleted": removed})

        elif a_type == "remove" and panel == "tester":
            await transformer_tasks.update_many(
                {"transform_id": transform_id, "task_id": {"$in": task_ids}},
                {"$set": {"status": "DONE"}, "$unset": {"verifier_checks": "", "verifier_score": ""}},
            )
            actions_applied.append({"type": "remove", "task_ids": task_ids})

        elif a_type == "reassign_wave" and panel == "planner":
            try:
                wave_num = int(action.get("wave"))
            except (TypeError, ValueError):
                continue
            wave_name = _WAVE_NAME_BY_NUM.get(wave_num, f"Wave {wave_num}")
            await transformer_tasks.update_many(
                {"transform_id": transform_id, "task_id": {"$in": task_ids}},
                {"$set": {"wave": wave_num, "wave_name": wave_name}},
            )
            actions_applied.append({"type": "reassign_wave", "task_ids": task_ids, "wave": wave_num})

        elif a_type == "regenerate" and panel == "coder":
            regenerated = []
            for tid in task_ids:
                task = next((t for t in task_docs if t.get("task_id") == tid), None)
                if not task:
                    continue
                src = task.get("source_path", "")
                fdoc = file_docs_by_path.get(src)
                if not fdoc:
                    continue
                try:
                    await _regenerate_single_file_impl(transform_id, str(fdoc["_id"]), payload.model or transform.get("model"))
                    regenerated.append(tid)
                except HTTPException:
                    pass
            actions_applied.append({"type": "regenerate", "task_ids": regenerated})

    await _log_audit("agent_chat", "transformation", transform_id, {
        "panel": panel,
        "message": payload.message,
        "actions_applied": actions_applied,
    })

    return {
        "reply": reply,
        "matched_ids": sorted(set(matched_ids)),
        "actions_applied": actions_applied,
        "tasks": await get_transformation_tasks(transform_id),
    }


@router.get("/transformer/{transform_id}/tasks")
async def get_transformation_tasks(transform_id: str):
    """Get the task list created by the Planner agent."""
    cursor = transformer_tasks.find({"transform_id": transform_id}).sort("wave", 1)
    docs = await cursor.to_list(2000)
    tasks = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        tasks.append(d)

    # Group by wave
    waves = {}
    for t in tasks:
        w = t.get("wave", 0)
        if w not in waves:
            waves[w] = {"wave": w, "name": t.get("wave_name", f"Wave {w}"), "tasks": []}
        waves[w]["tasks"].append(t)

    # Stats
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "DONE")
    verified = sum(1 for t in tasks if t.get("status") == "VERIFIED")
    blocked = sum(1 for t in tasks if t.get("status") == "BLOCKED")
    in_progress = sum(1 for t in tasks if t.get("status") == "IN_PROGRESS")

    return {
        "tasks": tasks,
        "waves": sorted(waves.values(), key=lambda w: w["wave"]),
        "stats": {
            "total": total,
            "done": done + verified,
            "verified": verified,
            "blocked": blocked,
            "in_progress": in_progress,
            "pending": total - done - verified - blocked - in_progress,
        },
    }


@router.get("/transformer/{transform_id}/agent-timeline")
async def get_agent_timeline(transform_id: str):
    """Get the agent execution timeline for the pipeline visualization."""
    cursor = transformer_agent_runs.find({"transform_id": transform_id}).sort("created_at", 1)
    docs = await cursor.to_list(500)
    runs = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        runs.append(d)
    return {"timeline": runs, "total": len(runs)}


async def _agent_config_payload(transform: dict, transform_id: str, agent: str) -> dict:
    """Build the effective (override-aware) config payload for one agent,
    shared by the list and single-agent GET endpoints."""
    base_key = AGENT_PROMPT_KEYS[agent]
    base_prompt_doc = await prompts.find_one({"key": base_key}, {"_id": 0})
    override = await _get_agent_override(transform_id, agent)
    base_template = (base_prompt_doc or {}).get("template", "")
    override_template = (override or {}).get("prompt_template", "")
    override_model = (override or {}).get("model", "")
    run_default_model = transform.get("model") or ""
    return {
        "agent": agent,
        "label": AGENT_LABELS.get(agent, agent),
        "prompt_key": base_key,
        "llm_backed": AGENT_LLM_BACKED.get(agent, True),
        "base_template": base_template,
        "base_description": (base_prompt_doc or {}).get("description", ""),
        "override_template": override_template,
        "effective_template": override_template.strip() or base_template,
        "run_default_model": run_default_model,
        "override_model": override_model,
        "effective_model": override_model.strip() or run_default_model,
        "is_overridden": bool(override_template.strip() or override_model.strip()),
        "updated_at": (override or {}).get("updated_at"),
    }


@router.get("/transformer/{transform_id}/agents")
async def list_transformer_agent_configs(transform_id: str):
    """iter-15.19 — List every Agent Pipeline step (SA/CM/Planner/Coder/
    Verifier/Tester) for this transformation with its effective prompt +
    model, so clicking any agent node can show the full config panel."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    agents = [await _agent_config_payload(transform, transform_id, agent) for agent in AGENT_PROMPT_KEYS]
    return {"agents": agents}


@router.get("/transformer/{transform_id}/agents/{agent}")
async def get_transformer_agent_config(transform_id: str, agent: str):
    """iter-15.19 — Single agent's effective (override-aware) config."""
    if agent not in AGENT_PROMPT_KEYS:
        raise HTTPException(404, f"Unknown agent '{agent}'")
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    return await _agent_config_payload(transform, transform_id, agent)


@router.put("/transformer/{transform_id}/agents/{agent}")
async def save_transformer_agent_config(transform_id: str, agent: str, payload: AgentConfigUpdate):
    """iter-15.19 — Save a per-transformation prompt/model override for
    one agent. Scoped to this transform_id only — never touches the
    shared Prompt Library. Submitting both fields empty clears any
    existing override (reverts to the global default)."""
    if agent not in AGENT_PROMPT_KEYS:
        raise HTTPException(404, f"Unknown agent '{agent}'")
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    now = datetime.now(timezone.utc).isoformat()
    if not payload.prompt_template.strip() and not payload.model.strip():
        await transformer_agent_configs.delete_one({"transform_id": transform_id, "agent": agent})
    else:
        await transformer_agent_configs.update_one(
            {"transform_id": transform_id, "agent": agent},
            {"$set": {
                "transform_id": transform_id,
                "agent": agent,
                "prompt_template": payload.prompt_template,
                "model": payload.model,
                "updated_at": now,
            }},
            upsert=True,
        )
    await _log_audit("update_agent_config", "transformation", transform_id, {
        "agent": agent,
        "has_prompt_override": bool(payload.prompt_template.strip()),
        "has_model_override": bool(payload.model.strip()),
    })
    return await _agent_config_payload(transform, transform_id, agent)


@router.post("/transformer/{transform_id}/rerun", status_code=202)
async def rerun_transformer_pipeline(transform_id: str, background_tasks: BackgroundTasks):
    """iter-15.19 — Fully restart the multi-agent pipeline for this
    transformation, clearing all previously discovered envelopes/tasks/
    generated files/agent-run history, and re-running from Super Agent →
    Context Manager onward. Any per-agent prompt/model overrides saved
    via PUT /agents/{agent} are picked up automatically because each
    _run_* agent function resolves its effective prompt/model live."""
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")
    if transform.get("status") == "running":
        raise HTTPException(400, "Pipeline is already running — stop it first")

    await transformer_envelopes.delete_many({"transform_id": transform_id})
    await transformer_tasks.delete_many({"transform_id": transform_id})
    await transformer_agent_runs.delete_many({"transform_id": transform_id})
    await transform_files.delete_many({
        "transform_id": transform_id,
        "type": {"$in": ["transformed", "error"]},
    })
    await tools_kb.delete_one({"_id": transform_id})

    await transformations.update_one(
        {"_id": transform_id},
        {"$set": {
            "pipeline_mode": "multi_agent",
            "status": "running",
            "phase": "super_agent",
            "phase_label": "Super Agent: Initializing pipeline (rerun)",
            "progress_pct": 2,
            "paused": False, "stopped": False,
            "files_transformed": 0,
            "manual_review_count": 0,
            "result": None,
            "compilation_result": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    await _log_audit("rerun", "transformation", transform_id, {})
    background_tasks.add_task(_run_multi_agent_transformation, transform_id, transform.get("model"))
    return {"status": "started", "transform_id": transform_id, "pipeline_mode": "multi_agent"}


@router.get("/transformer/{transform_id}/compilation")
async def get_compilation_result(transform_id: str):
    """Get the compilation analysis result from the Tester agent."""
    transform = await transformations.find_one(
        {"_id": transform_id},
        {"compilation_result": 1},
    )
    if not transform:
        raise HTTPException(404, "Transformation not found")
    return transform.get("compilation_result") or {
        "compilation_ready": None,
        "overall_score": 0,
        "checks": [],
        "summary": "Compilation analysis not yet run",
    }


@router.post("/transformer/{transform_id}/compile")
async def run_compilation_analysis(
    transform_id: str,
    background_tasks: BackgroundTasks,
    model: Optional[str] = None,
    auto_fix: bool = True,
    max_iterations: Optional[int] = None,
):
    """Manually trigger compilation on already-transformed files.

    iter-15.58 — When `auto_fix=True` (default) and a native build tool
    is configured, this now runs the full **compile → Planner → Coder
    fix loop**: parse diagnostics, generate FIX tasks, re-code the
    failing files, re-verify, re-compile — repeating until the build is
    green. iter-15.62.4: no hardcoded iteration cap by default (loops
    until success, a genuine infra block, or the stagnation guard fires
    because a fix round made no difference); pass `max_iterations` or
    set `LAMA_COMPILE_FIX_MAX_ITER` to opt into an explicit cap. Progress
    streams to `transformation.compile_fix_progress` and the FE polls
    it via `/transformer/{id}/status`.

    Passing `auto_fix=False` restores the pre-15.58 behaviour: single
    compile pass + LLM static analysis, no repair.
    """
    transform = await transformations.find_one({"_id": transform_id})
    if not transform:
        raise HTTPException(404, "Transformation not found")

    # iter-15.68 — GUARD against overlapping compile-fix runs. Nothing
    # previously stopped a second `/compile` call (e.g. a UI double-click,
    # or a caller retrying after a container restart mid-run — exactly
    # what happened live this session) from spawning ANOTHER background
    # `_run_compile_fix_loop` for the SAME transform while one was still
    # active. Two loops racing on the same `transformation.compile_fix_progress`
    # field produces exactly the symptom reported: the iteration counter
    # visibly flickers/regresses, agent nodes look stuck, and duplicate
    # LLM calls silently double the token spend. Reuse the SAME
    # `_COMPILE_STALL_THRESHOLD_SEC` staleness window the status endpoint's
    # `compile_fix_stalled` flag already uses, so "is this run still
    # really alive" is answered identically everywhere.
    _existing_progress = (transform or {}).get("compile_fix_progress") or {}
    _existing_phase = _existing_progress.get("phase")
    if _existing_phase and _existing_phase not in _COMPILE_FIX_TERMINAL_PHASES:
        _last_touch = _parse_iso_utc(_existing_progress.get("updated_at"))
        _stale = True
        if _last_touch:
            _age = (datetime.now(timezone.utc) - _last_touch).total_seconds()
            _stale = _age > _COMPILE_STALL_THRESHOLD_SEC
        if not _stale:
            raise HTTPException(
                409,
                f"A compile-fix run is already in progress (phase={_existing_phase}, "
                f"file={_existing_progress.get('current_file', '-')}). Wait for it to "
                f"reach a terminal state before triggering another one.",
            )

    target_stack = transform.get("transforms") or {}

    # Load all transformed files (initial count, for the response body).
    cursor = transform_files.find(
        {"transform_id": transform_id, "type": "transformed"},
        {"path": 1, "content": 1},
    )
    all_files = [{"path": d.get("path", ""), "content": d.get("content", "")}
                 async for d in cursor]

    if not all_files:
        raise HTTPException(400, "No transformed files to analyze")

    async def _bg():
        build_tools_map = (transform or {}).get("build_tools") or {}
        compile_result: Optional[Dict[str, Any]] = None
        workspace_root: Optional[str] = None

        try:
            if build_tools_map and auto_fix:
                # iter-15.58 — full fix-loop path.
                compile_result = await _run_compile_fix_loop(
                    transform_id, build_tools_map, model, max_iterations,
                )
            elif build_tools_map:
                # Legacy single-shot compile.
                workspace_root = _write_transformed_workspace(all_files)
                compile_result = await _run_compiler(
                    transform_id, workspace_root, build_tools_map,
                )
        except Exception as _exc:
            compile_result = {
                "compilation_ready": False,
                "overall_score": 0,
                "summary": f"Compile phase crashed: {_exc}",
                "components": [],
                "mode": "native",
            }
        finally:
            if workspace_root and os.path.isdir(workspace_root):
                shutil.rmtree(workspace_root, ignore_errors=True)

        # Rebuild the transformed-file list AFTER the loop so the LLM
        # tester analyses the freshly-fixed content.
        cursor2 = transform_files.find(
            {"transform_id": transform_id, "type": "transformed"},
            {"path": 1, "content": 1},
        )
        latest_files = [{"path": d.get("path", ""), "content": d.get("content", "")}
                        async for d in cursor2] or all_files
        static_analysis = await _run_tester(transform_id, latest_files, target_stack, model)

        if compile_result is None:
            result = {**static_analysis, "mode": "static_only",
                      "static_analysis": static_analysis}
        else:
            result = {**compile_result, "static_analysis": static_analysis}

        # iter-15.70 — This manual `/compile` re-trigger previously only
        # ever wrote `compilation_result`, never touching the top-level
        # `transformation.status`/`phase`. That field is what the FE
        # header badge, "Completed" checks, and every `AGENT_ORDER` node
        # key off — so a rerun that ended in `unfixable`/`stagnant` with
        # real compile errors still remaining left the UI showing the
        # transformation's ORIGINAL (often stale, often green) status,
        # exactly the "shows complete but compile errors still exist"
        # bug reported live. Apply the SAME final compile-status gate
        # iter-15.59 already applies at the end of the main pipeline run,
        # here too, so every path that can finish a compile-fix loop
        # converges on one honest terminal state.
        build_tools_map_final = (transform or {}).get("build_tools") or {}
        compile_green = True
        if build_tools_map_final:
            compile_green = bool((result or {}).get("compilation_ready"))

        # iter-20 — re-run the DevOps audit rather than ignoring it.
        #
        # This path used to decide `final_status` from `compile_green`
        # ALONE, with no `devops_blocked` term — unlike the main pipeline,
        # which requires both. So a rerun could flip a job that was
        # `completed_with_errors` because its manifests were not
        # production-ready back to a green `completed`, while leaving the
        # stale `production_ready: false` and its findings sitting on the
        # document. The badge said shipped; the audit still said no.
        #
        # The fix-loop has just rewritten manifests, so the previous
        # verdict is out of date either way — re-auditing is both the
        # correct gate and the more accurate answer. If the audit itself
        # fails we keep the PREVIOUS verdict rather than assuming success:
        # this endpoint must not be a way to launder a blocked build.
        dependency_audit_final = (transform or {}).get("dependency_audit") or {}
        try:
            dependency_audit_final = await _run_devops_dependency_check(
                transform_id, result, model,
            )
        except Exception as _audit_err:  # noqa: BLE001
            _emit_log(transform_id, "warn",
                      f"DevOps re-audit failed after rerun-compile: {_audit_err}; "
                      f"keeping the previous production-readiness verdict",
                      agent="devops_expert", phase="devops")

        production_ready_final = bool(dependency_audit_final.get("production_ready"))
        devops_blocked_final = bool(dependency_audit_final) and not production_ready_final

        final_status = (
            "completed" if (compile_green and not devops_blocked_final)
            else "completed_with_errors"
        )
        if compile_green and devops_blocked_final:
            final_phase_label = (
                "Compile-fix run finished — the build compiles but its "
                "dependencies are not production-ready; see the DevOps audit"
            )
        elif compile_green:
            final_phase_label = "Multi-agent pipeline completed"
        else:
            final_phase_label = (
                "Compile-fix run finished — compilation errors remain, click Rerun compile"
            )
        await transformations.update_one(
            {"_id": transform_id},
            {"$set": {
                "compilation_result": result,
                "status": final_status,
                "phase": "completed" if final_status == "completed" else "completed_with_errors",
                "phase_label": final_phase_label,
                "compile_green": compile_green,
                "dependency_audit": dependency_audit_final,
                "production_ready": production_ready_final,
                "current_file": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        await _log_agent_run(transform_id, "super_agent", "rerun-compile-finalize",
            status="completed",
            output_summary=(
                "Rerun-compile finished: build is GREEN" if compile_green else
                "Rerun-compile finished: build is RED — compilation errors remain"
            ),
        )

    background_tasks.add_task(_bg)
    return {
        "status": "started",
        "files": len(all_files),
        "auto_fix": auto_fix and bool(transform.get("build_tools")),
    }
