"""SRS generation, retrieval, freeze, section edit."""
import io
import os                # iter-13.42 — batch-mode env flag + LAMA_SRS_BATCH opt-out
import time              # iter-13.42 — used by _run_batch_srs for heartbeat / elapsed
import json
import re as _re   # module-level — used by _gen_one_section, _render_pdf, and the
                   # OLTP FK-augmentation block. Was previously imported only in
                   # local scopes; missing it in _gen_one_section's prior-section
                   # digest path caused `NameError: name '_re' is not defined`
                   # to crash section 2 onwards (iter-13.16 critical fix).
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from datetime import datetime, timezone

from db import srs_documents, kb_toon, messages, projects, audit_log, prompts, project_prompts, kb_entities, stage_context as stage_context_col, legacy_analysis as legacy_analysis_col
from models import SRSDocument, SRSSectionUpdate, StageContext
from llm import fabric_call as chat_completion
from llm import set_current_project_id  # iter-13.38 — Factory.ai context propagation
from llm import set_current_agent_key, get_current_agent_key   # iter-13.81.3 — per-pipeline-step Factory bucket override
from kb.vector_store import search as qdrant_search, search_many as qdrant_search_many
from kb.owl_export import export_owl
from kb.toon import serialise as toon_serialise
from kb.legacy_analyzer import run_legacy_analysis, build_analysis_digest
# iter-14.11 — Score-gated auto-retry loop needs the same judge stack the
# stage-confidence popover uses. `pick_evaluator_models` returns the
# cross-tier model list from Console; `score_artifact_multi_model` calls
# them and aggregates by mean. `band_of` is the shared threshold table.
from confidence import (
    band_of as _band_of,
    pick_evaluator_models as _pick_evaluator_models,
    score_artifact_multi_model as _score_artifact_multi_model,
)


SECTION_QUERIES = {
    # IEEE 29148 — 11 LLM-generated sections + deterministic ER as #12.
    "introduction": "system purpose scope user roles modules controllers domain",
    "overall_description": "architecture monolith framework controllers models sessions environment",
    "actors_use_case_inventory": "roles actors permissions use cases workflows controllers",
    "specific_requirements": "CRUD workflow approval status calculation validation rules",
    "detailed_use_cases": "workflow approval submission upload form field validation flow exception",
    "external_interfaces": "API endpoint REST SOAP HTTP request response header content-type",
    "non_functional_requirements": "performance security audit log uptime concurrent users encryption",
    "integration_requirements": "third party external service payment gateway SMS email integration",
    "validation_verification": "test acceptance criteria error code message validation scenario",
    "traceability_matrix": "module screen requirement FR test case mapping",
    "appendices": "data dictionary glossary supporting diagrams reference document",
}

router = APIRouter(prefix="/srs", tags=["srs"])
logger = logging.getLogger("lama.srs")


# ──────────────────────────────────────────────────────────────────────
# iter-13.90 — Cross-project KB-leak hardening.
#
# Forensic finding: multiple projects across multiple tenants ended up
# with SRS documents citing source paths (e.g. `src/com/ahct/*`) that
# did NOT belong to their own KB. Root cause was the defensive
# `{"project_id": project_id} if project_id else {}` fallback in the
# LEGACY-FILE-INDEX builder — when `project_id` was empty the filter
# collapsed to `{}` and the query returned EVERY file across EVERY
# tenant. Those filenames were then injected into the SRS prompt as
# authoritative evidence to "cite verbatim".
#
# This helper is the single source of truth for KB-scope filters used
# anywhere in the SRS hot path. It REFUSES to build a filter when
# `project_id` is missing, and it pins `tenant_id` whenever the project
# doc carries one — so even a future caller that forgets project_id
# cannot leak across tenant boundaries.
# ──────────────────────────────────────────────────────────────────────


def _assert_kb_filter(project_id: str | None, proj: dict | None = None) -> dict:
    """Return a Mongo filter `{project_id, tenant_id?}` for any KB query.

    Raises ``HTTPException(500)`` when `project_id` is empty — the caller
    has a bug, and silently degrading to "give me everything" is exactly
    how iter-13.89's leak reached production.
    """
    pid = (project_id or "").strip()
    if not pid:
        raise HTTPException(
            500,
            "Internal error: KB query attempted without project_id "
            "(would leak cross-project data — refusing).",
        )
    flt: dict = {"project_id": pid}
    tid = ""
    if proj is not None:
        tid = (proj.get("tenant_id") or "").strip()
    if tid:
        flt["tenant_id"] = tid
    return flt


# ──────────────────────────────────────────────────────────────────────
# iter-13.93 — Inline "Virtual Workspace" block.
#
# Background:
#   iter-13.92 forbade the Factory Droid LLM from reading the Droid's
#   filesystem (because the Droid carries stale source from prior
#   projects). User then asked: "why do I require [to mkdir on the
#   Droid]? you can ask it in srs prompt to do so. and show the path
#   from where it should read. create same like structure in server
#   space and do use it."
#
# Design:
#   • LAMA already stores every source file's content sharded across
#     `kb_chunks` (one doc per chunk, ordered by chunk_index, scoped
#     by project_id + tenant_id).
#   • We rebuild the file contents for the top-N highest-signal files
#     and emit them INLINE in the SRS prompt as a virtual workspace
#     with a stable, per-(tenant, project) pseudo-path:
#         /lama-workspace/<tenant_slug>/<project_slug>/
#     so the LLM cites paths grounded in the actual KB rather than
#     anything it might find on the Droid filesystem.
#   • Hard budget (default 60 KB total) keeps prompt size bounded.
#   • Per-section cache (file -> content) is short-circuited by an
#     in-process dict keyed on (project_id, build_fingerprint) so we
#     don't re-aggregate kb_chunks for every section in a run.
#
# Result is rendered as:
#
#     VIRTUAL LEGACY WORKSPACE (rebuilt from this project's KB —
#     this IS the authoritative source; cite paths VERBATIM from
#     the tree below; do NOT use shell tools):
#       workspace_root: /lama-workspace/<tenant>/<project>/
#       tree:
#         src/com/tjhs/ClaimsAction.java   (4.2 KB)
#         src/com/tjhs/UserDao.java        (1.8 KB)
#         ...
#       files:
#         ─── /lama-workspace/.../src/com/tjhs/ClaimsAction.java ───
#         <reconstructed content>
#         ─── /lama-workspace/.../src/com/tjhs/UserDao.java ───
#         <reconstructed content>
# ──────────────────────────────────────────────────────────────────────
_LEGACY_WORKSPACE_CACHE: dict[tuple, str] = {}
# iter-13.81.12 — bumped from 60 KB / 40 files / 8 KB-per-file to
# 200 KB / 80 files / 12 KB-per-file. The previous defaults were
# tuned for a 48 KB Factory prompt cap that has now been raised to
# 320 KB (see factory_orchestrator.FACTORY_PROMPT_MAX_CHARS) — the
# old budget was producing audit SRSs with "KB content blocks are
# not actually present" because the workspace evidence + TOON +
# RAG combined was still being chopped to ~31 KB by Factory's
# system-cap multiplier (now also removed). With the new cap the
# realistic envelope is: TOON ~20 KB + RAG ~15 KB + workspace
# ~200 KB + section instructions ~30 KB ≈ 265 KB, leaving 55 KB
# headroom inside the 320 KB Factory cap.
_LEGACY_WORKSPACE_DEFAULT_BUDGET = int(
    os.environ.get("LAMA_LEGACY_WORKSPACE_BUDGET", "200000") or "200000"
)
_LEGACY_WORKSPACE_MAX_FILES = int(
    os.environ.get("LAMA_LEGACY_WORKSPACE_MAX_FILES", "80") or "80"
)
_LEGACY_WORKSPACE_PER_FILE_CAP = int(
    os.environ.get("LAMA_LEGACY_WORKSPACE_PER_FILE_CAP", "12000") or "12000"
)


def _workspace_slug(s: str) -> str:
    """Slug a tenant_id / project_id for safe use in a pseudo-path."""
    return (s or "default").strip() or "default"


# ── iter-13.95 — cross-project path leak: server-side sanitizer ─────────
# Prompt language alone could not stop the `src/com/ahct/*` leak the user
# kept seeing in TJHS SRS. The actual ingress paths were UPSTREAM of the
# SRS prompt: the `legacy_analysis` JSON (cached for 24h, "cite IDs
# verbatim") and arbitrary chat history (`convo_text`) can both carry
# file-path tokens hallucinated from training data or copy-pasted from
# previous PMIS sessions. Once those tokens reach the SRS prompt as
# "authoritative pre-pass" they get woven into the output verbatim.
#
# This sanitizer runs BEFORE any free-text source is concatenated into
# the SRS system prompt. It walks every path-like token (anything with
# a `/` and a known source extension, with an optional `:LINE` suffix)
# and redacts the ones whose PATH SUFFIX does NOT appear in the project's
# scope-checked `kb_files` index. Defense-in-depth: it doesn't matter
# if an upstream component leaks a foreign path — the SRS prompt never
# sees it.
#
# ── iter-13.98 — BASENAME COLLISION FIX ─────────────────────────────────
# The iter-13.95 implementation matched on BASENAME ONLY, which is
# permissive enough to leak generic names like `LoginAction.java` /
# `Controller.java` / `service.py` whenever both the legitimate project
# and a stale past-project happen to have a same-named file. The user's
# concrete report: TJHS SRS cited
#     src/com/ahct/login/controller/LoginAction.java
#     src/com/ahct/login/service/LoginServiceImpl.java
# despite TJHS having ZERO ahct files in its KB — because TJHS's OWN
# kb_files contained a `LoginAction.java` (under a different parent dir,
# `com/tjhs/...`), so the basename matched.
#
# Fix: match on PATH SUFFIX. For each kb_files entry like
# `src/com/tjhs/login/LoginAction.java`, register every suffix:
#   - `LoginAction.java`
#   - `login/LoginAction.java`
#   - `tjhs/login/LoginAction.java`
#   - `com/tjhs/login/LoginAction.java`
#   - `src/com/tjhs/login/LoginAction.java`
# For a candidate path, compute the same suffix list and require a
# match with ≥ 2 path components when the candidate itself has ≥ 2
# components. A bare basename (`LoginAction.java` mentioned in prose)
# still passes on basename match — that's not a leak, just a casual
# reference. The leak surface is the FULL-PATH form which forces the
# discriminator.

# Source-file extensions worth defending. Conservative on purpose — we
# don't want to redact arbitrary `.md` / `.txt` mentions.
_PATH_EXTENSIONS = (
    "java", "py", "php", "phtml", "jsp", "jspx", "rb", "go", "kt", "kts",
    "scala", "groovy", "cs", "vb", "fs", "ts", "tsx", "js", "jsx", "mjs",
    "cjs", "sql", "ddl", "pks", "pkb", "plb", "prc", "fnc", "trg", "xml",
    "xsd", "wsdl", "yaml", "yml", "properties", "conf", "ini", "json",
    "html", "htm", "vue", "svelte", "ftl", "vm", "twig", "blade", "erb",
    "cbl", "cob", "for", "f90", "pas", "rs",
)

# Match a path-like token: anything that contains at least one `/`
# and ends with one of `_PATH_EXTENSIONS`, optionally followed by
# `:LINE` or `::function_name`. Word-bounded on both sides so we
# don't tear into prose like `bar/foo` mid-sentence.
_PATH_REGEX = _re.compile(
    r"(?<![\w./-])"                              # left boundary
    r"([A-Za-z0-9_./\\-]+/[A-Za-z0-9_./\\-]+"    # at least one slash
    r"\.(?:" + "|".join(_PATH_EXTENSIONS) + r")"  # ext
    r"(?:::[A-Za-z_][A-Za-z0-9_]*|:\d+(?:-\d+)?)?)"  # optional :LINE / ::fn
    r"(?![\w-])"                                  # right boundary
)

# Cap suffix expansion at 8 components — beyond that we burn memory
# for diminishing accuracy. Real legacy projects rarely have paths
# deeper than 8 packages.
_MAX_SUFFIX_DEPTH = 8


def _normalize_path(p: str) -> list[str]:
    """Return the path's components, lowercased, separators normalized."""
    s = (p or "").replace("\\", "/").strip().strip("/").lower()
    if not s:
        return []
    return [c for c in s.split("/") if c]


def _path_suffixes(components: list[str], max_depth: int = _MAX_SUFFIX_DEPTH) -> list[tuple[str, ...]]:
    """All suffixes of `components`, from 1 component up to min(len, max_depth)."""
    n = len(components)
    if not n:
        return []
    upper = min(n, max_depth)
    out: list[tuple[str, ...]] = []
    for depth in range(1, upper + 1):
        out.append(tuple(components[n - depth:]))
    return out


def _allowed_basenames(filenames: "list[str] | set[str]") -> set[str]:
    """Build a basename-only set from a list of KB filenames.

    Kept for back-compat with the leak-scan endpoint + existing tests.
    The actual sanitizer now uses `_allowed_path_suffixes` for the
    discriminator and falls back to basename only for bare-basename
    candidates (no `/`).
    """
    out: set[str] = set()
    for f in filenames or ():
        if not f:
            continue
        comps = _normalize_path(f)
        if comps:
            out.add(comps[-1])
    return out


def _allowed_path_suffixes(filenames: "list[str] | set[str]") -> set[tuple[str, ...]]:
    """Build the set of all path-suffix tuples for kb_files.

    For `src/com/tjhs/login/LoginAction.java` we register every suffix
    from 1 component (`LoginAction.java`) up to 8 components, so a
    citation `tjhs/login/LoginAction.java` (3 components) hits the
    `('tjhs','login','loginaction.java')` entry and survives.
    """
    out: set[tuple[str, ...]] = set()
    for f in filenames or ():
        if not f:
            continue
        comps = _normalize_path(f)
        for suf in _path_suffixes(comps):
            out.add(suf)
    return out


def _redact_foreign_paths(
    text: str,
    allowed_basenames: set[str],
    *,
    placeholder: str = "[redacted-foreign-path]",
    allowed_path_suffixes: "set[tuple[str, ...]] | None" = None,
) -> tuple[str, int]:
    """Replace every path-like token whose path-suffix isn't in
    `allowed_path_suffixes` (or basename in `allowed_basenames` as a
    1-component fallback) with `placeholder`.

    Returns `(sanitized_text, redaction_count)`. When BOTH allowed sets
    are empty we DO NOT redact — that's the "no KB to compare against"
    case, handled upstream by `_load_srs_context`'s `kb_file_count == 0`
    guard.

    iter-13.98 semantics:
      • Candidate has 1 path component (bare basename in prose) → must
        be in `allowed_basenames`. Same as iter-13.95.
      • Candidate has ≥ 2 path components → must have a suffix-tuple
        match (depth ≥ 2) in `allowed_path_suffixes`. Bare basename
        match is NOT sufficient anymore — that was the leak vector.
    """
    if not text:
        return "", 0
    if not allowed_basenames and not allowed_path_suffixes:
        return text, 0
    redactions = 0
    suf_set = allowed_path_suffixes or set()

    def _maybe_redact(m: _re.Match[str]) -> str:
        nonlocal redactions
        tok = m.group(1)
        # Strip an optional `:LINE` / `::fn` suffix before path check.
        path_only = _re.split(r"::|:", tok, maxsplit=1)[0]
        comps = _normalize_path(path_only)
        if not comps:
            return tok  # unreachable given the regex, but defensive

        if len(comps) == 1:
            # Bare basename mentioned in prose — basename-match is fine.
            if comps[0] in allowed_basenames:
                return tok
            redactions += 1
            return placeholder

        # ≥ 2 components — REQUIRE a depth-≥-2 suffix match. The bare
        # basename is NOT enough: that's exactly how PMIS `LoginAction.java`
        # slipped through into TJHS SRS in iter-13.97.
        cand_suffixes = _path_suffixes(comps)
        # cand_suffixes is depth-1, depth-2, ..., depth-N
        # Skip the depth-1 entry — we want a directory-discriminated match.
        for suf in cand_suffixes[1:]:
            if suf in suf_set:
                return tok
        # No discriminated match. Redact.
        redactions += 1
        return placeholder

    sanitized = _PATH_REGEX.sub(_maybe_redact, text)
    return sanitized, redactions


async def _load_allowed_basenames(
    project_id: str, proj: dict | None = None
) -> set[str]:
    """Back-compat shim: returns basename set only. Prefer
    `_load_allowed_path_index` which returns both basename + suffix
    sets so the sanitizer can do directory-discriminated matching.
    """
    bn, _suf = await _load_allowed_path_index(project_id, proj)
    return bn


async def _load_allowed_path_index(
    project_id: str, proj: dict | None = None
) -> tuple[set[str], set[tuple[str, ...]]]:
    """Load the scope-checked allowed (basenames, path_suffixes) for a project.

    Wraps the standard tenant+project KB filter; returns `(set(), set())`
    on plumbing failure — which disables redaction (the upstream
    `kb_file_count` guard is responsible for the empty-KB case).
    """
    if not project_id:
        return set(), set()
    try:
        from db import kb_files as _kbf
        flt: dict = {"project_id": project_id}
        tid = ((proj or {}).get("tenant_id") or "").strip()
        if tid:
            flt["tenant_id"] = tid
        names: list[str] = []
        async for f in _kbf.find(flt, {"_id": 0, "filename": 1}):
            n = (f.get("filename") or "").strip()
            if n:
                names.append(n)
        return _allowed_basenames(names), _allowed_path_suffixes(names)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "srs.sanitizer[%s]: kb_files load failed (%s) — disabling "
            "foreign-path redaction for this call",
            project_id, exc,
        )
        return set(), set()


def _workspace_root(tenant_id: str, project_id: str) -> str:
    """Stable per-(tenant, project) pseudo-path used in the prompt.

    iter-13.99 — renamed to the `{tenant_id}__{project_id}` format
    requested by the user and root moved under `/lama-workspaces/`
    (plural). The double-underscore separator avoids collisions if
    either id contains a single underscore.

    NOT a real filesystem path on the LAMA host; it's the path the
    LLM is told to cite from. Keeps every (tenant, project) tuple in
    its own namespace so cross-project filenames can NEVER collide —
    the path PREFIX itself is now a discriminator that the post-LLM
    sanitizer can match on exactly.
    """
    t = _workspace_slug(tenant_id)
    p = _workspace_slug(project_id)
    return f"/lama-workspaces/{t}__{p}"


def _is_workspace_path(path: str, workspace_root: str) -> bool:
    """True iff `path` is rooted at `workspace_root` (case-insensitive,
    separator-normalised). Used by the post-LLM hard guard to reject
    anything not coming from the project-isolated workspace.
    """
    if not path or not workspace_root:
        return False
    p = path.replace("\\", "/").strip().lower()
    r = workspace_root.replace("\\", "/").strip().lower().rstrip("/")
    return p == r or p.startswith(r + "/")


def _enforce_workspace_prefix(
    text: str,
    workspace_root: str,
    allowed_basenames: set[str],
    *,
    placeholder: str = "[redacted-foreign-path]",
) -> tuple[str, int]:
    """iter-13.99 — STRICT POST-LLM GUARD.

    When the project has a materialised isolated workspace at
    `workspace_root`, EVERY path-like token in `text` must either:
      (a) be rooted at `workspace_root` (the user-asked-for hard
          isolation guarantee), OR
      (b) be a bare basename mentioned in prose AND match a kb_files
          basename (back-compat for casual mentions).

    Anything else is unconditionally redacted. This is the contract
    the iter-13.99 prompt promises the model — and the guarantee the
    user wanted: cross-project paths are STRUCTURALLY impossible to
    surface in SRS output.
    """
    if not text:
        return "", 0
    if not workspace_root:
        return text, 0
    redactions = 0
    root_lower = workspace_root.replace("\\", "/").strip().lower().rstrip("/")

    def _maybe_redact(m: _re.Match[str]) -> str:
        nonlocal redactions
        tok = m.group(1)
        path_only = _re.split(r"::|:", tok, maxsplit=1)[0]
        comps = _normalize_path(path_only)
        if not comps:
            return tok
        if len(comps) == 1:
            # Bare basename in prose — accept if it's a known kb_files basename.
            if comps[0] in allowed_basenames:
                return tok
            redactions += 1
            return placeholder
        # ≥ 2 components — require workspace-root prefix.
        if _is_workspace_path(path_only, root_lower):
            return tok
        redactions += 1
        return placeholder

    sanitized = _PATH_REGEX.sub(_maybe_redact, text)
    return sanitized, redactions


# ---------------------------------------------------------------------------
# iter-14.25.5 — POST-LLM output-quality sanitizer.
#
# Diagnosed on the CGHS pilot (Aug 21 2026): the July-24 SRS was rich +
# specific; the Aug-21 SRS came back as generic "Application Approval"
# stubs with `[redacted-foreign-path]` literals in Source Evidence
# cells AND with the section's own prompt-instruction template rows
# echoed as content. Three distinct failure modes, one shared fix
# surface — a single post-LLM scrubber run on every section body just
# before it returns from ``_gen_one_section``.
#
# 1. `[redacted-foreign-path]` is an INTERNAL marker from
#    ``_enforce_workspace_prefix`` / ``_redact_foreign_paths``. It's
#    fine as an intermediate signal ("this citation was outside the
#    isolated workspace") but MUST NOT reach the reader — it looks
#    like a bug, and it forces the SRS reader to guess what path was
#    intended. Replace with a business-language marker.
#
# 2. When the analysis digest is thin, the model echoes the prompt-
#    template intro lines as its section body (e.g. "For EVERY role
#    found in the KB (roles tables, auth / visibility checks…),
#    produce a row in this table:"). These sentences come verbatim
#    from ``SECTION_CONFIGS.instructions``. They are INSTRUCTIONS to
#    the model, not content for the reader. Strip them.
#
# 3. The model likes to leave placeholder angle-bracket tokens
#    (``<relative/path>``, ``<role>``, ``<screen name>``, ``<list or
#    'none'>``) or bare ``…`` cells when it has no evidence. Those
#    are prompt-template leftovers, never legitimate SRS text.
#    Replace with ``NOT_EVIDENCED`` so the gap is auditable rather
#    than mysterious.
# ---------------------------------------------------------------------------

# Instruction-echo sentence patterns. Each pattern targets a canonical
# opening clause from one of the SECTION_CONFIGS instruction blocks.
# Matched case-insensitively, only at line-start (so a legit paragraph
# that quotes the phrase mid-sentence survives).
_INSTRUCTION_ECHO_PATTERNS: tuple[str, ...] = (
    r"^\s*For EVERY (?:role|use case|module|actor|domain term|acronym|"
    r"functional module|major business capability) found in the KB.*$",
    r"^\s*A single matrix table\s*[—-]\s*rows\s*=\s*actors.*$",
    r"^\s*Definition list \(term:\s*explanation\).*$",
    r"^\s*Bullet list of EVERY source artifact.*$",
    r"^\s*EXHAUSTIVE per-module (?:FR|BR) table.*$",
    r"^\s*Numbered list of conditions that MUST be true.*$",
    r"^\s*produce a row in this table:\s*$",
    r"^\s*Use\s+`R`\s*\(responsible\),\s*`A`\s*\(approver\).*$",
    r"^\s*Cover every CRUD operation, every approval.*$",
    r"^\s*Cite by \*\*relative file path\*\*.*$",
    r"^\s*Write the Description in BUSINESS LANGUAGE.*$",
)

# Placeholder tokens that should NEVER appear in final SRS content.
# Each maps to its rewrite. Ordering matters — longer patterns first
# so we don't half-replace nested tokens.
_PLACEHOLDER_REWRITES: tuple[tuple[str, str], ...] = (
    ("[redacted-foreign-path]", "NOT_EVIDENCED (path outside workspace)"),
    ("<relative/path>::<symbol>", "NOT_EVIDENCED (source citation)"),
    ("<relative/path>:<line or symbol>", "NOT_EVIDENCED (source citation)"),
    ("<relative/path>", "NOT_EVIDENCED (path)"),
    ("<schema_object>", "NOT_EVIDENCED (schema object)"),
    ("<table_or_entity_a>", "NOT_EVIDENCED"),
    ("<table_or_entity_b>", "NOT_EVIDENCED"),
    ("<file>:<line>", "NOT_EVIDENCED"),
    ("<screen name>", "NOT_EVIDENCED (screen)"),
    ("<Use Case Name>", "NOT_EVIDENCED"),
    ("<role>", "NOT_EVIDENCED (role)"),
    ("<list or \"none\">", "none"),
    ("<list or 'none'>", "none"),
)


def _scrub_section_output(content: str) -> tuple[str, dict]:
    """Post-LLM sanitizer — remove internal markers and template echoes.

    Returns ``(scrubbed_content, stats)`` where ``stats`` is a dict
    counting each remediation applied. Empty-input passthrough. Safe
    to call multiple times (idempotent for the specific tokens listed).
    """
    stats = {
        "placeholder_rewrites": 0,
        "instruction_echoes_removed": 0,
        "empty_ellipsis_cells": 0,
    }
    if not content:
        return content or "", stats

    scrubbed = content

    # 1) Placeholder rewrites.
    for needle, replacement in _PLACEHOLDER_REWRITES:
        if needle in scrubbed:
            stats["placeholder_rewrites"] += scrubbed.count(needle)
            scrubbed = scrubbed.replace(needle, replacement)

    # 2) Instruction-echo line removal (line-anchored, case-insensitive).
    for pat in _INSTRUCTION_ECHO_PATTERNS:
        rx = _re.compile(pat, _re.IGNORECASE | _re.MULTILINE)
        matches = rx.findall(scrubbed)
        if matches:
            stats["instruction_echoes_removed"] += len(matches)
            scrubbed = rx.sub("", scrubbed)

    # 3) Empty ellipsis-only markdown-table cells ("| … |" or "| ... |").
    #    These are template rows the model failed to populate.
    #    Use a lookahead on the trailing pipe so adjacent empty cells
    #    ("| … | ... |") both match (a plain match would consume the
    #    shared pipe and skip the second cell).
    def _cell_scrub(m: _re.Match[str]) -> str:
        stats["empty_ellipsis_cells"] += 1
        return "| NOT_EVIDENCED "
    scrubbed = _re.sub(
        r"\|\s*(?:…|\.\.\.)\s*(?=\|)",
        _cell_scrub,
        scrubbed,
    )

    # 4) Collapse the 3+ blank lines the removals may have introduced.
    scrubbed = _re.sub(r"\n{3,}", "\n\n", scrubbed).strip("\n") + "\n"

    return scrubbed, stats


def _digest_richness(analysis_digest: str) -> dict:
    """Cheap coverage signal for the pre-SRS analyzer output.

    We can't fix a thin digest at prompt-time (the analyzer has already
    run) but we can DETECT it, log a loud warning, and surface an
    "evidence-starved" banner in the section so the operator knows to
    rebuild the KB / rerun the analyzer instead of trusting the SRS.
    """
    if not analysis_digest:
        return {"workflows": 0, "actors": 0, "entities": 0, "rules": 0,
                "chars": 0, "starved": True}
    workflows = len(_re.findall(r"\bWF-\d+\b", analysis_digest))
    actors = len(_re.findall(r"\bA-\d+\b", analysis_digest))
    rules = len(_re.findall(r"\bBR-[A-Za-z0-9_-]{1,20}\b", analysis_digest))
    entities = 0
    ent_m = _re.search(
        r"\*\*Domain Entities:\*\*(.+?)(?=\n\*\*|\Z)",
        analysis_digest,
        _re.DOTALL,
    )
    if ent_m:
        entities = len(_re.findall(r"^\s*[-*]\s*\*\*", ent_m.group(1),
                                   _re.MULTILINE))
    chars = len(analysis_digest)
    # Starvation threshold — empirically the "old CGHS" digest was
    # ~4 KB with 8+ workflows + 6+ actors; anything materially below
    # ~800 chars OR under 3 workflows / 2 actors produces the
    # generic-CRUD collapse observed on the Aug-21 CGHS regression.
    starved = (
        chars < 400
        or workflows < 3
        or actors < 2
    )
    return {
        "workflows": workflows,
        "actors": actors,
        "entities": entities,
        "rules": rules,
        "chars": chars,
        "starved": starved,
    }


def _is_high_signal_file(fname: str, ftype: str) -> int:
    """Higher score → more likely to be useful in SRS reasoning.

    The scoring is intentionally simple/transparent — it nudges
    controllers/actions/services to the front and pushes generated
    or vendored noise to the back. The KB scan-pattern skip-list
    already drops the worst (node_modules, vendor, …) so this only
    needs to discriminate within "real" project files.
    """
    f = (fname or "").lower()
    score = 0
    for k in ("action", "controller", "service", "facade", "manager",
              "workflow", "orchestrat", "engine", "scheduler"):
        if k in f:
            score += 30
    for k in ("dao", "repository", "mapper", "validator", "rule"):
        if k in f:
            score += 20
    for k in ("config", "constant", "util", "helper"):
        if k in f:
            score -= 5
    for k in ("test", "spec", "mock", "fixture"):
        if k in f:
            score -= 40
    for k in ("generated", "build/", "target/", "dist/", ".min."):
        if k in f:
            score -= 60
    # Source code beats markup beats data.
    if any(f.endswith(ext) for ext in (".java", ".py", ".php", ".cs", ".rb",
                                       ".go", ".kt", ".scala", ".ts", ".tsx",
                                       ".js", ".jsx", ".sql")):
        score += 10
    if any(f.endswith(ext) for ext in (".yml", ".yaml", ".json", ".xml",
                                       ".properties", ".conf")):
        score += 3
    return score


async def _build_legacy_workspace_block(
    project_id: str,
    proj: dict,
    *,
    budget_chars: int = _LEGACY_WORKSPACE_DEFAULT_BUDGET,
    max_files: int = _LEGACY_WORKSPACE_MAX_FILES,
    per_file_cap: int = _LEGACY_WORKSPACE_PER_FILE_CAP,
) -> str:
    """Build the inline virtual-workspace block for the SRS prompt.

    Returns an empty string when the KB has no files (callers MUST
    treat empty as "fall back to TOON/RAG only" — the absence of
    legacy code in the KB is already surfaced as a hard error by
    `_load_srs_context`'s `kb_file_count == 0` guard, so this should
    never silently degrade real runs).
    """
    if not project_id:
        # iter-13.93 — defensive symmetry with _assert_kb_filter: an
        # empty project_id has been the root cause of every cross-
        # project leak in iters 89/90; do NOT build a workspace block
        # without a scope.
        return ""
    flt = _assert_kb_filter(project_id, proj)
    fp = ((await kb_toon.find_one({"project_id": project_id},
                                  {"_id": 0, "build_fingerprint": 1,
                                   "files_fingerprint": 1})) or {})
    cache_key = (
        project_id,
        (proj or {}).get("tenant_id", ""),
        fp.get("build_fingerprint") or fp.get("files_fingerprint") or "",
        budget_chars, max_files, per_file_cap,
    )
    cached = _LEGACY_WORKSPACE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        from db import kb_files as _kbf, kb_chunks as _kbc
    except Exception:  # noqa: BLE001
        return ""

    # Pull file metadata (scope-checked).
    files: list[dict] = []
    try:
        async for f in _kbf.find(
            flt,
            {"_id": 0, "id": 1, "filename": 1, "filetype": 1,
             "size": 1, "chunk_count": 1, "raw_text_len": 1,
             "excluded": 1},
        ):
            if f.get("excluded"):
                continue
            fname = (f.get("filename") or "").strip()
            if not fname:
                continue
            files.append(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace[%s]: kb_files fetch failed: %s", project_id, exc)
        return ""
    if not files:
        return ""

    # Rank: signal score (descending), then size (descending — bigger
    # files have more behaviour to anchor on).
    files.sort(
        key=lambda f: (
            _is_high_signal_file(f.get("filename", ""), f.get("filetype", "")),
            int(f.get("size") or 0),
        ),
        reverse=True,
    )
    files = files[:max_files]

    # Aggregate kb_chunks per file_id into ordered content.
    file_ids = [f["id"] for f in files if f.get("id")]
    chunks_by_file: dict[str, list[tuple[int, str]]] = {fid: [] for fid in file_ids}
    if file_ids:
        try:
            chunk_flt = dict(flt)
            chunk_flt["file_id"] = {"$in": file_ids}
            async for c in _kbc.find(
                chunk_flt,
                {"_id": 0, "file_id": 1, "chunk_index": 1, "content": 1},
            ):
                fid = c.get("file_id")
                if fid in chunks_by_file:
                    chunks_by_file[fid].append((
                        int(c.get("chunk_index") or 0),
                        c.get("content") or "",
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace[%s]: kb_chunks fetch failed: %s",
                           project_id, exc)

    root = _workspace_root((proj or {}).get("tenant_id", ""), project_id)
    tree_lines: list[str] = []
    file_blocks: list[str] = []
    used = 0
    truncation_note = ""
    for f in files:
        fname = f.get("filename", "")
        size = int(f.get("size") or 0)
        tree_lines.append(f"  {fname}  ({size} B)")
        fid = f.get("id", "")
        parts = chunks_by_file.get(fid) or []
        parts.sort(key=lambda kv: kv[0])
        body = "".join(p[1] for p in parts).strip()
        if not body:
            continue  # tree-only entry; no chunks materialised
        if len(body) > per_file_cap:
            body = body[:per_file_cap] + "\n…[file truncated to fit prompt budget]"
        block = (
            f"─── path: {root}/{fname}  ({len(body)} chars) ───\n"
            f"{body}\n"
        )
        if used + len(block) > budget_chars:
            truncation_note = (
                f"  (… {len(files) - len(file_blocks)} more files omitted to fit "
                f"the {budget_chars}-char workspace budget — top-N ranking applied.)\n"
            )
            break
        file_blocks.append(block)
        used += len(block)

    if not file_blocks:
        # All chunks empty (rare) — emit the tree alone, still useful.
        rendered = (
            f"PROJECT-ISOLATED LEGACY WORKSPACE (iter-13.99 — this IS the only "
            f"legitimate source for THIS project):\n"
            f"  workspace_root: {root}/\n"
            "  • EVERY file citation in your output MUST be rooted at exactly\n"
            f"    `{root}/`. Any other path is a CROSS-PROJECT LEAK and will\n"
            "    be redacted server-side (so emitting it just wastes a citation).\n"
            "  • The KB has files but their inlined content is unavailable for\n"
            "    this run — cite tree-only entries by name and acknowledge the\n"
            "    behavioural gap rather than inventing detail.\n"
            "  tree:\n"
            + "\n".join(tree_lines) + "\n"
        )
    else:
        rendered = (
            f"PROJECT-ISOLATED LEGACY WORKSPACE (iter-13.99 — this IS the only "
            f"legitimate source for THIS project):\n"
            f"  workspace_root: {root}/\n"
            "  • The files below were materialised from THIS project's KB\n"
            f"    (tenant + project scope-checked) and ROOTED at `{root}/`.\n"
            "  • EVERY file citation in your output MUST be rooted at exactly\n"
            f"    `{root}/` or be a bare basename mentioned in prose. ANY\n"
            "    OTHER PREFIX (e.g. `src/`, `legacy-code/`, `app/`, anything\n"
            "    you remember from training data) is a CROSS-PROJECT LEAK\n"
            "    and will be redacted server-side before reaching the user.\n"
            "  • DO NOT invoke shell tools (`cat`, `ls`, `grep`, `find`,\n"
            "    `tree`, `sed`, `head`, `tail`); the content you need is\n"
            "    INLINED below. The Droid filesystem outside this workspace\n"
            "    belongs to OTHER projects and ANY content found there is\n"
            "    cross-project contamination.\n"
            "  • Examples of VALID citations:\n"
            f"      • `{root}/src/com/yourproj/foo/Bar.java:42`\n"
            f"      • `{root}/properties/app.properties:18`\n"
            "      • `Bar.java::doSubmit`  (bare basename in prose)\n"
            "  • Examples of INVALID citations (will be redacted):\n"
            "      • `src/com/anything/Bar.java:42`  (missing workspace prefix)\n"
            "      • `/lama-workspaces/other__other/Bar.java`  (other workspace)\n"
            "      • `legacy-code/...`  (legacy free-form path — forbidden)\n"
            "  tree:\n"
            + "\n".join(tree_lines) + "\n"
            + (truncation_note or "")
            + "  files:\n"
            + "\n".join(file_blocks)
        )

    _LEGACY_WORKSPACE_CACHE[cache_key] = rendered
    return rendered


# In-process registry of running SRS background jobs, keyed by project_id.
# Each entry: {"task": asyncio.Task, "listeners": list[asyncio.Queue], "history": list[dict]}
# This is single-process state — fine for LAMA's single-tenant single-image
# deployment model. A second connection to /generate/stream while a job is
# running will attach to the existing run instead of launching a second one.
_SRS_JOBS: dict[str, dict] = {}


# ----------------------------------------------------------------------------
# Section configs — one LLM call per section, each with a focused TOON slice.
# Structure matches the IEEE 830 / IEEE 29148 specification embedded in the
# gov.* and srs.spec.ieee29148 prompts. Order matters: the same order is used
# by the PDF exporter (_render_pdf), the SRSPanel frontend, and the
# revalidation pass.
# ----------------------------------------------------------------------------
SECTION_CONFIGS = [
    {
        "key": "introduction",
        "label": "1. Introduction",
        "toon_focus": "CLASSES",
        "min_words": 600,
        "instructions": """Write Section 1 of an IEEE 830 / IEEE 29148 SRS with these FIVE sub-sections (use `## 1.x` headings, do NOT skip any):

## 1.1 Purpose
What this system does (based on actual module / controller / table names visible in the KB), the business problems it solves, and what the migration aims to achieve. Write for a domain reader (an operations head, a programme owner) — not a developer. Avoid framework or language jargon unless it appears VERBATIM in the source.

## 1.2 Scope
Every functional area found in the KB grouped by business domain (procurement / billing / reporting / etc.). What is explicitly OUT of scope for this migration. Integration boundaries with upstream and downstream systems.

## 1.3 Definitions, Acronyms & Abbreviations
Definition list (term: explanation) for every domain term, acronym and role found in table names, module names, role names, and any regulatory terms visible. Use the business meaning, not the implementation meaning.

## 1.4 References
Bullet list of EVERY source artifact this SRS derives from. Cite by **relative file path** (no language assumptions — could be any source / config / schema file). If only the KB summary is available, say so explicitly with `NOT_EVIDENCED`.

## 1.5 Document Overview
Summarise the structure of the remaining 10 sections of this SRS in 1–2 sentences each.""",
    },
    {
        "key": "overall_description",
        "label": "2. Overall Description",
        "toon_focus": "TABLES",
        "min_words": 700,
        "instructions": """Write Section 2 with these SIX sub-sections (use `## 2.x` headings):

## 2.1 Product Perspective
Where this system sits — standalone, part of a larger landscape, dependencies on upstream / downstream systems visible in the code. Speak in business terms; if the source stack ships specific runtime constraints, cite them verbatim from the DETECTED LEGACY STACK block.

## 2.2 Product Functions
Top-level functional grouping (one bullet per major business capability, e.g. "Tender Floating", "Bill Processing", "Beneficiary Onboarding"). Tie each capability to the source-tree directory or module group that implements it — language-agnostic ("the procurement module under `<path>`"), not "the PHP controllers".

## 2.3 User Classes and Characteristics
For EVERY role found in the KB: role name, typical user profile, technical-skill assumption, and the screens / modules they touch. Describe each user class as a human, not a database row.

## 2.4 Operating Environment
Runtime versions, framework, DB engine, OS, browser support — taken from the DETECTED LEGACY STACK block, properties files and config XML. Mark as `NOT_EVIDENCED` if not visible.

## 2.5 Design and Implementation Constraints
Constraints the legacy system imposes on any rewrite: standard libraries, code-style mandates, mandatory interfaces, schema invariants. List the constraint + the file that establishes it.

## 2.6 Assumptions and Dependencies
External libraries, government services, payment gateways, SMS / email providers etc. visible in the code or in the configuration.""",
    },
    {
        "key": "actors_use_case_inventory",
        "label": "3. Actors and Use Case Inventory",
        "toon_focus": "INDIVIDUALS",
        "min_words": 500,
        "instructions": """Write Section 3 with TWO sub-sections (use `## 3.x` headings):

══════════════════════════════════════════════════════════════════════
STORYTELLING VOICE — applies to actor descriptions AND UC one-liners
══════════════════════════════════════════════════════════════════════
This section is the *cast list and table of contents* for the stories
that live in Section 5. Both halves below MUST read like a human
explaining the system to a new joiner — not a class diagram caption.
Use business language, name people in roles ("the District Magistrate",
"the field officer Anita"), never class / interface / service names.

## 3.1 Actor Definitions
For EVERY role found in the KB (roles tables, auth / visibility checks, filter logic in any source file), produce a row in this table:

| Actor ID | Actor Name | Plain-English Description | Source Evidence |
|----------|------------|----------------------------|-----------------|
| A-01     | …          | One sentence — who this person is and what they do  | `<relative/path>:<line or symbol>` or `<table>` row |

Write the Description in BUSINESS LANGUAGE (e.g. "Field officer who verifies on-site survey reports") — never as a class diagram annotation. If a role appears in code but no description is evidenced, mark Description as `NOT_EVIDENCED`.

## 3.2 Actor → Use Case Mapping
A single matrix table — rows = actors, columns = use case IDs (UC-01, UC-02 …). Use `R` (responsible), `A` (approver), `O` (observer/notified) per cell. Include every use case derived from the source tree (controllers, handlers, jobs, scripts — whatever artefact triggers a user-facing or system-facing action).

After the matrix, add a `## 3.3 Use Case One-Liners` subsection: for EVERY UC referenced in the matrix, write ONE plain-English sentence in the present tense that names the actor and the outcome, e.g.:
  - UC-04 — *The applicant submits a new permit request and pays the registration fee online.*
  - UC-07 — *The District Magistrate approves or rejects pending permits from her dashboard, with reason.*
These one-liners are the seed for the full storytelling-style narratives in Section 5; they MUST match in actor name, action, and outcome so a reader can scan the inventory and predict each story. Full UC details live in Section 5.""",
    },
    {
        "key": "specific_requirements",
        "label": "4. Specific Requirements",
        "toon_focus": "CLASSES",
        "min_words": 2000,
        "instructions": """Write Section 4 with THREE sub-sections (use `## 4.x` headings).

══════════════════════════════════════════════════════════════════════
PHRASING CONTRACT (HARD RULE)
══════════════════════════════════════════════════════════════════════
Every Functional Requirement (FR-*) and Business Rule (BR-*) MUST be
phrased so a domain expert can read it WITHOUT looking at the code:
  • Start with the actor / system: "The system shall…", "An approver shall…"
  • Describe the BEHAVIOUR in business words, not the implementation
    ("…reject any bill whose total exceeds the sanctioned budget"
     — NOT "…throw BudgetExceededException from BillService.validate").
  • Source citations live in their own column / line — they augment the
    requirement, they do NOT replace the prose.
  • Avoid framework / language nouns inside the requirement statement
    itself unless the source IDENTIFIER is the requirement (e.g. "PAN
    must match `[A-Z]{5}[0-9]{4}[A-Z]`").

══════════════════════════════════════════════════════════════════════
BUSINESS-RULE EXTRACTION CONTRACT (HARD RULE — read before writing 4.2)
══════════════════════════════════════════════════════════════════════
Mine business rules EXHAUSTIVELY from these CONCRETE source-code
patterns (technology-agnostic — apply the equivalent in whatever stack
the KB shows). Every match becomes a `BR-*` row. Do NOT paraphrase code
into a single vague rule when the code shows multiple distinct rules.

  PATTERN ──────────────────  SOURCE LOCATION
  1. Branching guards         conditional blocks that REFUSE / REJECT /
                              ABORT / RAISE / RETURN ERROR — every
                              `if … then reject/throw/return` is a BR.
  2. Validators               regex patterns, length caps, range
                              checks, format checks, mandatory-field
                              checks (in any layer: UI / API / service).
  3. Database constraints     PK / UNIQUE / FK ON DELETE/UPDATE / CHECK
                              / NOT NULL — read the DDL VERBATIM.
  4. Triggers and procedures  before/after triggers, stored procedures,
                              functions — every conditional INSERT /
                              UPDATE / DELETE inside them is a BR.
  5. Status transitions       state-machine code, switch on status
                              column, `if status == X then …` blocks.
                              Each forbidden transition = one BR.
  6. Authorization guards     role / permission checks, policy filters,
                              `requires(role=…)` annotations, allowlists.
                              Each protected resource = one BR.
  7. Calculations             tax, fee, discount, interest, penalty,
                              proration, rounding — extract the formula
                              VERBATIM as a BR ("the system shall round
                              monetary totals to 2 decimal places…").
  8. Audit / logging mandates `audit_log.insert(...)`, `logger.info(...)`
                              calls that record state changes — each is
                              an "the system shall record X" BR.
  9. Notification triggers    email/SMS/push send sites tied to
                              business events — "the system shall
                              notify <role> when <event>" BRs.
 10. Configuration flags      feature flags, env-driven behaviour
                              switches — each = "when flag X is on,
                              the system shall…" BR.
 11. Scheduled jobs           cron entries, timer-triggered tasks —
                              each = a periodic BR with cadence.
 12. Idempotency / retry      retry counters, dedup keys, idempotency
                              tokens — each is a BR on safe re-execution.

When the DEEP LEGACY-LOGIC ANALYSIS digest is present in your prompt,
REUSE its `BR-*` IDs and rule statements verbatim — do NOT renumber.
When it is absent, mine the patterns above directly from the RAG
snippets and graph subgraph. NEVER claim "no business rules found" —
even a CRUD form has validators (Pattern 2) and auth guards (Pattern 6).

## 4.1 Functional Requirements
EXHAUSTIVE per-module FR table. For EACH functional module found in the KB:

### FR-[MODULE]: [Module Name in business terms]
| ID | Requirement (plain-English, "shall …") | Source File ⇄ Symbol | Data Objects Touched | Enforcement Layer |
|----|----------------------------------------|----------------------|----------------------|-------------------|
| FR-[MOD]-001 | The system shall …                     | `<relative/path>::<symbol>` or `<file>:<line>` | `<table_or_entity_a>, <table_or_entity_b>` | UI / API / SERVICE / DB |

Cover every CRUD operation, every approval / status transition, every calculation, every upload / download, every report, every notification, every access-control rule. Do NOT collapse rows.

## 4.2 Global Business Rules
A numbered list of system-wide rules (`BR-G-001`, `BR-G-002` …). Apply the BUSINESS-RULE EXTRACTION CONTRACT above. The MINIMUM expected count for a non-trivial legacy app is one BR per pattern category that appears in the KB — typically 25–80 rows. Format each as:

`BR-G-001` — *Rule statement in plain English.*
**Pattern:** *<one of the 12 categories above — e.g. "Database constraint", "Status transition">*
**Source:** `<relative/path>::<symbol>` or `<schema_object>` or `(BR-XX from deep analysis)`

Group rules by pattern category (sub-heading per category) so a reviewer can scan completeness at a glance. If a pattern category has NO matches in the KB, write the sub-heading + `_(none observed in source — pattern not used by this system)_` so absence is explicit, not implicit.

## 4.3 Common Validation Standards
Validation rules reused across multiple forms / endpoints — mandatory-field policies, regex patterns for domain identifiers (PAN / GST / email / mobile / etc.), length caps, date ranges, numerical bounds. Cite the validator function, schema constraint, or config entry that defines each. Group by data-type family (identifiers / monetary / dates / strings / enums) for readability.""",
    },
    {
        "key": "detailed_use_cases",
        "label": "5. Detailed Use Cases",
        "toon_focus": "CLASSES",
        "min_words": 2500,
        "instructions": """Write Section 5 — DETAILED use cases for EVERY use case listed in 3.2.

══════════════════════════════════════════════════════════════════════
DUAL-AUDIENCE WRITING — BUSINESS FIRST, TECHNICAL SECOND
══════════════════════════════════════════════════════════════════════
This section MUST serve TWO audiences:
  1. **Business stakeholders** (executives, product owners, domain experts) —
     they need to understand WHAT the system does and WHY it matters, without
     reading code references.
  2. **Technical teams** (developers, QA, architects) — they need precise
     mappings to code, tables, and validation rules.

Structure every UC so that a business reader can STOP after the Narrative
and Business Impact Summary sections and still understand the use case
completely. The technical details (source citations, BR IDs, column names)
are nested INSIDE those sections in parentheses or in the tables below —
they don't interrupt the flow for business readers who skip them.

══════════════════════════════════════════════════════════════════════
STORYTELLING VOICE — READ THIS FIRST (HARD RULE)
══════════════════════════════════════════════════════════════════════
Use cases in this section are written as **human-understandable stories
that a non-technical reader (operations lead / domain SME / new joiner)
can follow without opening the codebase.** Each UC opens with a short
narrative scene; the structured tables below it are *evidence and
acceptance criteria*, NOT the storytelling itself.

This is contractually enforced. A UC that has the structured tables
(Metadata / Preconditions / etc.) but no `#### Narrative (User Story)`
subsection — or a Narrative that is bullet-listed, written in passive
voice, or names class/service identifiers instead of people — is
considered INCOMPLETE and will be flagged by the revalidation pass
(reason: `missing_storytelling_narrative`) and scored < 95 by the
confidence engine.

For EVERY UC, write the `#### Narrative (User Story)` subsection as
3–5 short paragraphs in plain English:
  • Paragraph 1 (THE BUSINESS CONTEXT) — WHY does this use case exist?
    What business problem does it solve? What would happen if this
    functionality didn't exist? Example: "The permit application process
    is the primary revenue channel for the licensing department,
    handling approximately 500 submissions daily. Without this workflow,
    applicants would need to visit physical offices, causing delays and
    reducing department efficiency."
  • Paragraph 2 (THE SCENE) — sets the scene: WHO the actor is in
    business terms (their role, their goal), WHEN/WHY they show up
    (the trigger), and WHAT outcome they want.
  • Paragraph 3 (THE HAPPY PATH) — walks the happy path as a story:
    "Anita the applicant opens the application screen, sees her pending
    applications, presses <Submit>; the system validates her identity,
    debits the fee, and queues the file for the reviewer …". Name real
    screens, buttons and roles — NOT class or function names.
  • Paragraph 4 (THE EXCEPTION) — covers the most important alternate /
    exception branch in the same narrative voice: "If the identity
    number is malformed, Anita stays on the form with an inline error …".
  • Paragraph 5 (THE OUTCOME) — what the downstream actor sees and does
    next, to close the loop. What business value is delivered.

Storytelling rules:
  • Present tense, active voice, simple sentences.
  • Name actors as people in roles ("the District Magistrate", "the
    applicant Anita") — NOT class / interface / service names.
  • Use real screen labels / button text / role names from the KB.
  • The narrative comes BEFORE Metadata, Preconditions, etc. — it
    frames the rest of the UC.
  • Do NOT bullet-list inside the narrative. Prose only.
  • Length target: 150–300 words per UC narrative.
  • Technical references (source files, column names) go in parentheses
    at the END of sentences, so business readers can skip them.

══════════════════════════════════════════════════════════════════════
BIRD'S-EYE FAILURE MODE  (READ CAREFULLY — this is the #1 reject cause)
══════════════════════════════════════════════════════════════════════
A UC written in "eagle-view" — abstract prose that only paraphrases the
one-liner without walking the CONCRETE tables, columns, form fields,
routes and roles that live in the KNOWLEDGE BASE above (TOON classes /
routes / tables / columns, plus the analysis digest and workflow
inventory for this project) — is INCOMPLETE and will be flagged with
reason `bird_eye_narrative`.

WRONG (bird's-eye — REJECT):
  "The applicant submits a permit request. The system validates the
  request and forwards it for approval. Once approved, the applicant
  is notified."
  ^ paraphrases the one-liner. No specific columns, no field names,
    no route, no BR ids, no concrete branch conditions. Any UC could
    have been written this way. Fails.

RIGHT (grounded, storytelling — ACCEPT):
  "It is 09:30 on a Tuesday and Anita, an applicant living in Nashik
  district, opens the **New Permit Application** screen. She types
  her Aadhaar (`permits.applicant_aadhaar`), category
  (`permits.category` from the dropdown of five values in
  `PermitCategoryLookup`), the sanction amount she is requesting
  (`permits.requested_amount`) and uploads a scanned ID
  (`permits.id_proof_url`). She presses **Submit**.
  Behind the scenes the system checks that Aadhaar matches
  `^[0-9]{12}$` (BR-UC01-002), that no draft permit is already open
  for this applicant this quarter (BR-UC01-004 — a `SELECT … WHERE
  applicant_aadhaar = ? AND status = 'DRAFT'`), then persists a row
  into `permits` with status `DRAFT`, writes an audit entry, and
  routes the request to the field officer for verification.
  If Aadhaar is malformed Anita stays on the form and sees an inline
  red error under the field; the record is never persisted. …"
  ^ names the actor, the screen, real column names, real regex, real
    BR IDs, real branch conditions. This is what "grounded story-
    telling" means. THIS IS THE STANDARD.

Every UC narrative you write MUST cite AT LEAST:
  • one column name from the KNOWLEDGE BASE (TOON tables/columns or
    the analysis digest),
  • one form field or route from the KNOWLEDGE BASE (TOON routes,
    controllers, or view-anchored fields),
  • one BR ID from the analysis digest's Business Rules — or an
    explicit `⚠ EVIDENCE GAP: no BR found in KB for this UC`,
  • one concrete branch condition or validation rule.
A narrative that names none of the above is bird's-eye — reject it.

══════════════════════════════════════════════════════════════════════
MANDATORY SUBSECTION ORDER PER UC (do NOT skip any)
══════════════════════════════════════════════════════════════════════
Per the SRS authoring spec, Preconditions, Postconditions, Business
Workflow, Business Rules, and the End-to-End Journey (Screen → API →
DB) are MANDATORY on every UC. Missing any of these five causes the UC
to be REJECTED at revalidation.

### UC-01: <Use Case Name>
#### Narrative (User Story)
2–4 short paragraphs as described above. Storytelling voice. Real screen
+ role names. Cite the source file in parentheses on first mention of a
system interaction (e.g. "submits the form (`<relative/path>::<symbol>`)")
— the citation is the only place tech-stack names are allowed.

#### Metadata
| Field | Value |
|-------|-------|
| UC ID | UC-01 |
| Primary Actor | <role> |
| Secondary Actors | <list or "none"> |
| Screen / Module | <screen name> |
| Priority | HIGH / MEDIUM / LOW |
| Description | One-line business-language summary |

#### Preconditions  *(MANDATORY)*
Numbered list of conditions that MUST be true BEFORE the UC starts. Mine them EXHAUSTIVELY from these patterns (apply the equivalent in the actual stack the KB shows):

  PATTERN ──────────────────  SOURCE LOCATION
  P1. Authentication state    "user must be logged in", "session valid"
                              — read from auth filters / middleware /
                              `requires_login` annotations / route guards.
  P2. Authorization state     role / permission gate at controller or
                              service entry — "the actor's role IS in
                              {…}".
  P3. Prior data state        rows that must already exist (FK targets,
                              parent records, status preconditions e.g.
                              "the parent application is in `SUBMITTED`
                              status before approval can run").
  P4. Configuration / feature flag must be on (read from feature-flag
                              checks at the top of the handler).
  P5. Time / window           "within working hours", "before deadline X"
                              — read from time-of-day or date guards.
  P6. Input validity gates    request payload passed schema / form
                              validation BEFORE the workflow proper
                              starts (regex, mandatory-field, length).
  P7. Lock / mutex held       optimistic / pessimistic locks acquired,
                              row-level locks, distributed locks.

For EACH precondition, cite the source code line or DB constraint that enforces it (`<relative/path>::<symbol>` or `<table>.<column>` or `(BR-XX from deep analysis)`). If a precondition is implied by the workflow but NOT enforced anywhere in code, mark it `NOT_EVIDENCED` — but DO list it; an un-enforced precondition is a known gap the migration must close.

#### Postconditions  *(MANDATORY)*
Numbered list of conditions that MUST be true AFTER the UC ends successfully. Mine EXHAUSTIVELY from these patterns:

  PATTERN ──────────────────  SOURCE LOCATION
  Q1. Row writes              every `INSERT` / `UPDATE` / `DELETE` the
                              workflow performs — the resulting state
                              of EACH affected row.
  Q2. Status transitions      the column that changed and its NEW value
                              ("`application.status` becomes `APPROVED`").
  Q3. Audit-log entries       `audit_log.insert(...)` calls reachable
                              from the workflow — name the action code.
  Q4. Notifications fired     emails / SMS / push messages sent — name
                              the recipient role and the template/event.
  Q5. Side-effect events      messages published to queues / topics,
                              webhooks fired, downstream sync triggered.
  Q6. Cache invalidation      cache keys cleared / refreshed.
  Q7. File / blob writes      uploads stored, reports generated, PDFs
                              persisted — name the storage location.
  Q8. Counters / aggregates   denormalised totals incremented, balances
                              updated, quotas decremented.

Cite source for each. The minimum credible postcondition count for a happy-path UC that mutates state is 3–5 entries (at least one row write + one audit entry + one status / notification effect).

#### Business Workflow  *(MANDATORY)*
Step-by-step numbered actions naming **actor** + **system** for each step. Each step in plain English, with the source symbol that implements it in parentheses. Example:
1. *Applicant* presses **Submit** on the application screen.
2. *System* validates required fields (`<relative/path>::<symbol>`).
3. *System* writes a new application row with status `DRAFT` and emits an audit-log entry.

#### Business Rules  *(MANDATORY)*
Numbered list (`BR-UC01-001`, `BR-UC01-002` …) of rules enforced DURING this UC. Apply the same 12-pattern BUSINESS-RULE EXTRACTION CONTRACT used in §4.2 (branching guards / validators / DB constraints / triggers / status transitions / authorization guards / calculations / audit mandates / notification triggers / config flags / scheduled jobs / idempotency rules) — restricted to rules that fire on THIS use case's path. For each: rule statement in plain English + pattern category + source reference.
Example: `BR-UC01-001` — *The system shall reject any submission whose total exceeds the approved sanction.*  **Pattern:** Branching guard.  **Source:** `<relative/path>::<symbol>` or `<schema_object>`.

#### Main Flow
Numbered happy-path steps (may overlap with Business Workflow but written from the actor's point of view — what they SEE and DO).

#### Alternate Flows
Each alternate-path branch as `A1`, `A2` … with trigger condition + steps.

#### Exception Flows
Each error path as `E1`, `E2` … with trigger condition + handler behaviour.

#### Field Specification Table
| Field Name | Label (English) | Data Type | Length / Format | Mandatory | Validation Rule | Source |
|------------|-----------------|-----------|------------------|-----------|------------------|--------|
| <field>    | <human label>   | <type>    | <length/format>  | YES / NO  | <rule>           | `<view file>` ⇄ `<table.column>` |

#### End-to-End Journey (Screen → API → DB)  *(MANDATORY)*
A single traceability table that captures the FULL journey of every
user interaction in this UC — from the screen the actor sees, through
the API endpoint the screen invokes, down to the specific database
table + column(s) that get read or written. This is the primary
migration artefact: it becomes the input to the DataModel + Architecture
stages that follow, and it MUST be complete.

Emit ONE row per (screen action) × (API call) combination. If the same
screen action triggers multiple API calls, emit multiple rows. If the
same API call touches multiple tables, split into multiple rows so the
`DB Table` / `DB Column(s)` cells stay concrete.

| Step | Screen / UI Element | User Action | API Method + Path | Legacy Handler | Request Fields → Payload | DB Table | DB Column(s) | Operation | Response → UI Effect |
|------|---------------------|-------------|--------------------|----------------|---------------------------|----------|---------------|-----------|-----------------------|
| 1 | `New Permit Application` screen · **Submit** button | Applicant submits form | `POST /api/permits` | `PermitController::store` (`<relative/path>::<symbol>`) | `aadhaar`, `category`, `requested_amount`, `id_proof_url` → JSON body | `permits` | `applicant_aadhaar`, `category`, `requested_amount`, `id_proof_url`, `status`, `created_at` | INSERT | Redirect to `Draft Permits` list; new row shown |
| 2 | (same submit) | System writes audit trail | (internal, no route) | `AuditService::log` | `actor_id`, `action='PERMIT_DRAFT_CREATED'`, `entity_id` | `audit_log` | `actor_id`, `action`, `entity_type`, `entity_id`, `created_at` | INSERT | (none — background) |
| 3 | `Draft Permits` list · **View** link | Applicant opens a draft | `GET /api/permits/{id}` | `PermitController::show` | path param `id` | `permits` | `id`, `applicant_aadhaar`, `category`, `requested_amount`, `status` | SELECT | Renders read-only detail screen |

Hard rules for this table (enforced at revalidation):
  • **Every user-visible action** named in the Narrative / Main Flow / Alternate Flows MUST have a corresponding row (or a `NOT_EVIDENCED` row explaining the gap).
  • **`API Method + Path` MUST come from the KB routes/TOON** — never invent a route. If the legacy stack doesn't expose HTTP (e.g. classic ASP postback, JSP form action), name the actual dispatch endpoint (`.aspx` page + event, `.jsp` action target, servlet mapping) and mark `Operation` accordingly.
  • **`Legacy Handler` cites the exact controller / method / procedure** in `<relative/path>::<symbol>` form so the CodeGen stage can locate it.
  • **`DB Table` + `DB Column(s)` come from the TOON tables/columns slice** or the legacy analysis digest — do NOT paraphrase. If a column is derived (calculated, joined-in), suffix with `(derived)` and note the source columns.
  • **`Operation` is one of** `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `UPSERT`, `CALL <procedure>`, `TX (multi)`, or `NONE (read-through cache)`.
  • **`Response → UI Effect`** describes what the user sees next (screen navigation, inline validation, toast, list refresh) — this closes the round-trip.
  • Minimum credible row count for a UC that mutates state = 3 (submit row + audit row + follow-up read). Read-only UCs (view / list / export) still need ≥ 1 row.

This table replaces the loose "which table does this touch" prose that plagues legacy documentation. Business readers can skim the `Screen / UI Element` and `Response → UI Effect` columns; developers use the `API`, `Legacy Handler`, `DB Table` and `DB Column(s)` columns as the migration checklist.

#### Data Flow Diagram (DFD)
A Mermaid DFD (Data Flow Diagram) showing how data moves through the use case.
Use the following Mermaid syntax:
```mermaid
flowchart LR
    subgraph External Entities
        A[Actor: <role>]
        E1[External System: <name>]
    end
    subgraph Processes
        P1((1. <process name>))
        P2((2. <process name>))
    end
    subgraph Data Stores
        D1[(Database: <table>)]
        D2[(File: <storage>)]
    end
    A -->|"<data item>"| P1
    P1 -->|"<data item>"| D1
    P1 -->|"<data item>"| P2
    P2 -->|"<result>"| A
```
The DFD must show:
  • External Entities: Actors and external systems (rectangles)
  • Processes: Actions that transform data (rounded circles with numbers)
  • Data Stores: Databases, files, caches (cylinder shapes)
  • Data Flows: Arrows labeled with the data items moving between elements
Name concrete data items from the KB (e.g., "application_form", "approval_status", "notification").

#### Process Flow Diagram
A Mermaid `flowchart TD` block showing actor lanes and decision diamonds, including alternate / exception branches.
```mermaid
flowchart TD
    subgraph Actor: <role>
        A1[Start: <trigger>]
        A2[Action: <step>]
    end
    subgraph System
        S1{Validation}
        S2[Process: <action>]
        S3[Write: <table>]
    end
    A1 --> S1
    S1 -->|Valid| S2
    S1 -->|Invalid| E1[Error: <message>]
    S2 --> S3
    S3 --> A2
```

#### Business Impact Summary
A short (50–100 word) paragraph written for EXECUTIVE STAKEHOLDERS explaining:
  • **Why this use case matters** to the organization (revenue, compliance, user satisfaction).
  • **What happens if it fails** — the business consequence (delayed payments, regulatory penalty, customer churn).
  • **Who cares** — the business owner / department responsible.
Example:
  "This use case handles permit fee collection, contributing approximately 15% of monthly revenue.
   If the payment gateway integration fails, applicants cannot complete submissions, leading to
   backlogs and potential compliance violations under the Service Level Agreement with the
   licensing authority. The Finance department monitors daily collection dashboards."

Repeat the above template once per use case. Do NOT skip subsections — if no evidence exists, write `NOT_EVIDENCED` explicitly. The Narrative subsection AND the five MANDATORY subsections (Preconditions, Postconditions, Business Workflow, Business Rules, End-to-End Journey Screen→API→DB) are non-negotiable.""",
    },
    {
        "key": "external_interfaces",
        "label": "6. External Interfaces",
        "toon_focus": "CLASSES",
        "min_words": 500,
        "instructions": """Write Section 6 with FOUR sub-sections (use `## 6.x` headings):

## 6.1 User Interface Specifications
Per screen: layout pattern (form / list / dashboard / wizard), key widgets, responsive behaviour evidenced in the view files. Describe the screen as a user sees it, not as the template engine renders it.

## 6.2 Software Integration Interfaces
For every external system integration found in the code: name, direction (inbound / outbound), protocol, authentication mechanism, and the calling source symbol. Tech-stack-agnostic description of the contract — what data flows, in what shape, and what triggers it.

## 6.3 Communication Protocols
HTTP / HTTPS, SOAP, SFTP, SMTP, message queues etc., with cipher / TLS requirements where visible.

## 6.4 Data Exchange Formats
JSON / XML / CSV / fixed-width schemas — include sample payloads if evidenced in the code (request / response classes, XSD, sample files, fixtures).""",
    },
    {
        "key": "non_functional_requirements",
        "label": "7. Non-Functional Requirements (NFRs)",
        "toon_focus": "TABLES",
        "min_words": 500,
        "instructions": """Write Section 7 with SEVEN sub-sections (use `## 7.x` headings). Phrase every NFR as a measurable, testable statement in business language ("the system shall…"):

## 7.1 Performance
Response-time targets, concurrent-user load, bulk-operation throughput — derive from any timing / cache code or properties visible. If nothing is evidenced, propose a baseline and mark it `NOT_EVIDENCED`.

## 7.2 Security
Authentication, session management, role enforcement, encryption-at-rest, encryption-in-transit, secret handling, OWASP-relevant patterns visible in the code.

## 7.3 Availability
Uptime SLA, scheduled-maintenance window, backup cadence.

## 7.4 Usability
Accessibility (WCAG), language / locale support, browser support.

## 7.5 Maintainability
Logging, monitoring hooks, configuration externalisation visible in the source.

## 7.6 Scalability
Horizontal vs vertical scaling assumptions; expected data growth (reference actual table-row counts where visible).

## 7.7 Compliance
Regulatory mandates visible in the code (data-protection, tax, financial, statutory or sector-specific). One bullet per mandate with source.""",
    },
    {
        "key": "integration_requirements",
        "label": "8. Integration Requirements",
        "toon_focus": "CLASSES",
        "min_words": 400,
        "instructions": """Write Section 8 with THREE sub-sections (use `## 8.x` headings):

## 8.1 Third-Party Systems
Per integration: vendor name, business purpose, calling module / symbol, request / response contract summary, SLA visible in code.

## 8.2 Government / External Services
Per service (identity providers, payment gateways, statutory endpoints, document repositories): name + endpoint base + authentication mechanism + which actor triggers calls. Use neutral language — name what the service DOES in business terms, then cite its exact name and endpoint.

## 8.3 Data Synchronization Rules
Batch vs real-time, full vs delta, retry / backoff / idempotency logic visible in the code (cron jobs, schedulers, queue consumers, file pollers).""",
    },
    {
        "key": "validation_verification",
        "label": "9. Validation and Verification",
        "toon_focus": "CLASSES",
        "min_words": 500,
        "instructions": """Write Section 9 with FOUR sub-sections (use `## 9.x` headings):

## 9.1 Acceptance Criteria
For each of the top-priority use cases in Section 5, a `Given / When / Then` block that the migrated system must pass. Write in business English so a UAT lead can execute it manually.

## 9.2 Error Code and Message Standards
A table of error codes / messages found in the code (status enums, exception types, error properties / locale files): code, severity, user-facing message (human-readable, not stack-trace), source.

## 9.3 Sample Test Scenarios
Per major workflow, 3 sample test scenarios (positive / negative / boundary) referencing real screen names and field values.

## 9.4 Validation Approach
How acceptance will be proven post-migration — UAT plan, parallel-run rules, sign-off authority, data-reconciliation method.""",
    },
    {
        "key": "traceability_matrix",
        "label": "10. Traceability Matrix",
        "toon_focus": "CLASSES",
        "min_words": 300,
        "instructions": """Write Section 10 — a single comprehensive traceability table. EVERY functional requirement from Section 4.1 must appear as a row.

| Requirement ID | Module / Screen | Use Case | Source Symbol | Data Objects | Test Case ID |
|----------------|------------------|----------|----------------|---------------|---------------|
| FR-<MOD>-001   | <Module name>    | UC-<NN>  | `<relative/path>::<symbol>` | `<table_or_entity>` | TC-<MOD>-001 |

If a requirement has no use case yet, write `—` in that cell. The test case IDs should match the scenarios written in 9.3. Do NOT skip any FR-ID — exhaustive coverage is mandatory.""",
    },
    {
        "key": "appendices",
        "label": "11. Appendices",
        "toon_focus": "TABLES",
        "min_words": 400,
        "instructions": """Write Section 11 with THREE sub-sections (use `## 11.x` headings):

## 11.1 Supporting Diagrams
Brief textual descriptions (no images — they live elsewhere) of any sequence / activity / state / deployment diagrams that should accompany the SRS once design is done. Cite which UC each refers to.

## 11.2 Data Dictionary
A summary table of the most important tables / entities from the KB:

| Table / Entity | Purpose (plain English) | Key Columns | Foreign Keys | Domain |
|----------------|--------------------------|-------------|--------------|--------|
| <table>        | <one sentence>           | `id (PK), …` | `dept_id → <table>.id` | <domain> |

Pull as many as you have evidence for from the TOON slice — do NOT invent.

## 11.3 Reference Documents
Bullet list of every source artifact: legacy code repos, DB dump files, properties files, prior SRS versions, regulatory documents cited in the code. Include the in-repo path for each.""",
    },
    {
        "key": "entity_model",
        "label": "12. Entity Relationship Model",
        "toon_focus": "TABLES",
        "min_words": 0,
        "instructions": "",
    },
]


# Backwards-compat alias for stage-context consumers that may still reference
# the legacy key names. Maps OLD key → NEW key.
LEGACY_SECTION_ALIAS = {
    "purpose": "introduction",
    "scope": "introduction",
    "definitions": "introduction",
    "functional_requirements": "specific_requirements",
    "use_cases": "detailed_use_cases",
    "constraints": "integration_requirements",
}


async def _gen_entity_model(project_id: str) -> dict:
    """Compute ER diagram data deterministically from kb_entities (no LLM).
    Also augments edges by parsing FK statements from the generated OLTP DDL
    so the ER works for legacy schemas where the raw SQL dump has no explicit
    REFERENCES (common in MySQL <5.6 MyISAM and most older PHP apps)."""
    import math
    from collections import defaultdict as _defaultdict
    from db import data_models as _data_models

    entities = await kb_entities.find(
        {"project_id": project_id, "type": "TABLE"}, {"_id": 0}
    ).to_list(2000)

    nodes = []
    edges = []
    edge_set = set()

    for e in entities:
        parts  = e["name"].split("_")
        domain = parts[0] if len(parts) > 1 else "other"
        node = {
            "id":       e["name"],
            "name":     e["name"],
            "pk":       e.get("pk", ""),
            "domain":   domain,
            "columns": [
                {
                    "name":     c["name"],
                    "type":     c["type"],
                    "is_pk":    c["name"] == e.get("pk", ""),
                    "is_fk":    any(fk["column"] == c["name"]
                                    for fk in e.get("fks", [])),
                    "nullable": True,
                }
                for c in (e.get("columns") or [])
            ],
            "fk_count":  len(e.get("fks", [])),
            "col_count": len(e.get("columns") or []),
        }
        nodes.append(node)
        for fk in (e.get("fks") or []):
            key = f"{e['name']}.{fk['column']}->{fk['ref_table']}"
            if key not in edge_set:
                edge_set.add(key)
                edges.append({
                    "id":          key,
                    "from_table":  e["name"],
                    "from_col":    fk["column"],
                    "to_table":    fk["ref_table"],
                    "type":        "fk",
                    "cardinality": "many-to-one",
                })

    # --- Augmentation 1: parse FK statements from generated OLTP DDL ---
    oltp_art = await _data_models.find_one({"project_id": project_id, "type": "oltp_ddl"}, {"_id": 0})
    ddl = (oltp_art or {}).get("content", "") or ""
    if ddl:
        table_names = {n["id"].lower(): n["id"] for n in nodes}
        # Inline: REFERENCES "other" ("id")  or REFERENCES other(id)
        # Standalone: FOREIGN KEY ("a") REFERENCES "other" ("id")
        # Iterate per CREATE TABLE block so we know the *source* table.
        block_re = _re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`']?([A-Za-z0-9_\.]+)[\"`']?\s*\((.*?)\)\s*;",
            _re.IGNORECASE | _re.DOTALL,
        )
        fk_re = _re.compile(
            r"FOREIGN\s+KEY\s*\(\s*[\"`']?([A-Za-z0-9_]+)[\"`']?\s*\)\s*"
            r"REFERENCES\s+[\"`']?([A-Za-z0-9_\.]+)[\"`']?\s*\(\s*[\"`']?([A-Za-z0-9_]+)[\"`']?\s*\)",
            _re.IGNORECASE,
        )
        inline_re = _re.compile(
            r"[\"`']?([A-Za-z0-9_]+)[\"`']?\s+[A-Za-z][A-Za-z0-9_()\s,]*?\s+REFERENCES\s+"
            r"[\"`']?([A-Za-z0-9_\.]+)[\"`']?\s*\(\s*[\"`']?([A-Za-z0-9_]+)[\"`']?\s*\)",
            _re.IGNORECASE,
        )
        for m in block_re.finditer(ddl):
            src_table_raw = m.group(1).split(".")[-1]
            body = m.group(2)
            src_table = table_names.get(src_table_raw.lower(), src_table_raw)
            for fm in fk_re.finditer(body):
                from_col, to_table_raw, to_col = fm.group(1), fm.group(2).split(".")[-1], fm.group(3)
                to_table = table_names.get(to_table_raw.lower(), to_table_raw)
                key = f"{src_table}.{from_col}->{to_table}"
                if key in edge_set:
                    continue
                edge_set.add(key)
                edges.append({
                    "id": key, "from_table": src_table, "from_col": from_col,
                    "to_table": to_table, "to_col": to_col, "type": "fk",
                    "cardinality": "many-to-one", "source": "oltp_ddl",
                })
            for fm in inline_re.finditer(body):
                # Skip if the keyword token itself was "FOREIGN" (already matched above)
                if fm.group(1).lower() == "key":
                    continue
                from_col, to_table_raw, to_col = fm.group(1), fm.group(2).split(".")[-1], fm.group(3)
                to_table = table_names.get(to_table_raw.lower(), to_table_raw)
                key = f"{src_table}.{from_col}->{to_table}"
                if key in edge_set:
                    continue
                edge_set.add(key)
                edges.append({
                    "id": key, "from_table": src_table, "from_col": from_col,
                    "to_table": to_table, "to_col": to_col, "type": "fk",
                    "cardinality": "many-to-one", "source": "oltp_ddl",
                })

    # --- Augmentation 2: heuristic inference when neither KB nor OLTP has FKs ---
    # If a table has column "X_id" / "Xid" / "fk_X" and another table named X (or Xs) exists,
    # treat it as a many-to-one relationship. Common in legacy PHP MySQL dumps.
    if not edges:
        name_to_id = {n["id"].lower(): n["id"] for n in nodes}
        # Try both "users" -> "user" and "user" -> "users" matches.
        def _resolve_target(raw):
            cand = raw.lower()
            for c in (cand, cand + "s", cand[:-1] if cand.endswith("s") else cand + "es",
                      "tbl_" + cand, cand.rstrip("s")):
                if c in name_to_id and c != "id":
                    return name_to_id[c]
            return None

        for n in nodes:
            for col in n["columns"]:
                cn = col["name"].lower()
                ref = None
                if cn.endswith("_id") and cn != "id":
                    ref = _resolve_target(cn[:-3])
                elif cn.endswith("id") and len(cn) > 3:
                    ref = _resolve_target(cn[:-2])
                if ref and ref != n["id"]:
                    key = f"{n['id']}.{col['name']}->{ref}"
                    if key in edge_set:
                        continue
                    edge_set.add(key)
                    col["is_fk"] = True
                    n["fk_count"] += 1
                    edges.append({
                        "id": key, "from_table": n["id"], "from_col": col["name"],
                        "to_table": ref, "type": "fk", "cardinality": "many-to-one",
                        "source": "inferred",
                    })

    # Domain-clustered layout
    domain_groups = _defaultdict(list)
    for n in nodes:
        domain_groups[n["domain"]].append(n["id"])

    domain_list = list(domain_groups.keys())
    grid_cols   = max(math.ceil(math.sqrt(len(domain_list))), 1)
    for di, domain in enumerate(domain_list):
        dx      = (di % grid_cols) * 600 + 300
        dy      = (di // grid_cols) * 500 + 300
        members = domain_groups[domain]
        for mi, table_id in enumerate(members):
            angle  = (2 * math.pi * mi) / max(len(members), 1)
            radius = min(40 * len(members), 200)
            for n in nodes:
                if n["id"] == table_id:
                    n["x"]            = dx + radius * math.cos(angle)
                    n["y"]            = dy + radius * math.sin(angle)
                    n["domain_index"] = di
                    break

    er_data = {
        "nodes": nodes,
        "edges": edges,
        "domains": {
            d: {"tables": tbls, "index": i}
            for i, (d, tbls) in enumerate(domain_groups.items())
        },
        "stats": {
            "total_tables":        len(nodes),
            "total_relationships": len(edges),
            "domains":             len(domain_groups),
        },
    }
    return {"content": json.dumps(er_data), "tokens": 0}


import functools


@functools.lru_cache(maxsize=64)
def extract_toon_section(toon: str, section_name: str, max_chars: int) -> str:
    """Extract a named section from the TOON output (e.g. 'CLASSES', 'TABLES').

    iter-14.13 — Memoised with `lru_cache(maxsize=64)`. `_gen_one_section`
    is called 12+ times per SRS run, each time calling this helper up to
    4× on the same `full_toon` string with the same section_name +
    max_chars combination. Before: 48 full O(len(toon)) string splits
    per SRS run. After: ~4-6 unique cache misses per run, everything
    else O(1). No signature change — Python string hashing handles the
    key. `maxsize=64` bounds memory at ~4× the largest TOON blob typical
    projects generate (~100 KB), well within budget.
    """
    if not toon:
        return ""
    lines = toon.split("\n")
    in_section = False
    result: list[str] = []
    total = 0
    for line in lines:
        if line.startswith(f"# {section_name}"):
            in_section = True
            continue
        if in_section and line.startswith("# "):
            break
        if in_section:
            result.append(line)
            total += len(line)
            if total >= max_chars:
                result.append("...[truncated for context]")
                break
    return "\n".join(result)


async def _get_prompt(project_id: str, key: str) -> str:
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


# Order matters — gov.core MUST come first per the YAML header
# ("Loaded FIRST in all sessions. All other modules inherit these.").
# `srs.generate` (iter-13.77) is the technology-agnostic master SRS
# authoring contract: it sits between gov.core and the SRS-specific IEEE
# 29148 overlay so it can declare the input-source registry + precedence
# ladder + depth axes BEFORE the structural overlay applies them. The
# IEEE 29148 spec stays last so it takes precedence on document-structure
# questions.
_GOVERNANCE_BUNDLE_KEYS = (
    "gov.core",
    "gov.role_analysis",
    "gov.field_traceability",
    "gov.business_rule_extraction",
    "gov.completeness_contract",   # iter-13.71 — 100% legacy-parity contract
    "srs.generate",                # iter-13.77 — tech-agnostic master spec
    "srs.spec.ieee29148",
)


async def _load_governance_bundle(project_id: str, project_name: str) -> str:
    """Concatenate all governance prompts into a single compliance preamble.

    Each prompt is fetched via _get_prompt so project-level overrides win
    over the global seed. Missing prompts are skipped silently so the SRS
    flow keeps working even if a key hasn't been seeded yet. Substitutes
    {project_name} where present.

    Additionally appends a DETECTED LEGACY STACK block (built by
    kb.tech_detector during Build KB) so the LLM never has to guess the
    source stack — it sees the EVIDENCE-derived language, frameworks,
    database and build system at the top of every section call.
    """
    parts: list[str] = []
    for key in _GOVERNANCE_BUNDLE_KEYS:
        try:
            tmpl = await _get_prompt(project_id, key)
        except Exception:
            tmpl = ""
        if not tmpl:
            continue
        tmpl = tmpl.replace("{project_name}", project_name or "the system")
        parts.append(
            "═══════════════════════════════════════════════════════════════════\n"
            f"GOVERNANCE BLOCK · {key}\n"
            "═══════════════════════════════════════════════════════════════════\n"
            f"{tmpl}"
        )
    if not parts:
        return ""

    # ---- Pull the live detected stack from the project doc -------------
    try:
        proj_doc = await projects.find_one({"id": project_id}, {"_id": 0}) or {}
    except Exception:
        proj_doc = {}
    detected = proj_doc.get("detected_tech") or {}

    def _fmt_evidence_list(items: list[str]) -> str:
        items = list(items or [])[:5]
        return ", ".join(items) if items else "(none)"

    if detected and (detected.get("language") or detected.get("frameworks") or detected.get("database")):
        langs = detected.get("languages") or []
        lang_line = ", ".join(f"{l} ({n} files)" for l, n in langs[:6]) or "—"
        fw_block = ""
        for fw, evidence in (detected.get("evidence", {}).get("frameworks") or {}).items():
            fw_block += f"  • {fw}  ←  {_fmt_evidence_list(evidence)}\n"
        if not fw_block:
            fw_block = "  (no framework evidence)\n"
        db_block = ""
        for db in (detected.get("databases") or []):
            db_block += f"  • {db}\n"
        if not db_block:
            db_block = "  (no database evidence)\n"
        # ── iter 13.5 fix ────────────────────────────────────────────────
        # User report: SRS kept calling Struts-based projects "SQL/Java" and
        # generalised the framework away. Root cause: this block was being
        # appended at the END of the bundle (after gov.core / gov.*
        # / srs.spec.ieee29148) where the LLM under-weighted it. We now
        # PREPEND it at position 0, before any governance block, and harden
        # the language so omitting the detected framework name is a
        # HARD-CONTRACT violation.
        # ─────────────────────────────────────────────────────────────────
        stack_block = (
            "═══════════════════════════════════════════════════════════════════\n"
            "DETECTED LEGACY STACK — HARD CONTRACT · OVERRIDES gov.core source_of_truth.\n"
            "Derived from the uploaded files at Build-KB time. EVIDENCE-BOUND.\n"
            "Every reference to the legacy platform — in prose, headings,\n"
            "tables, FR-ID descriptions, NFR rationale, glossary, anywhere —\n"
            "MUST use the exact values below VERBATIM. Generalising to\n"
            "\"a Java application\", \"SQL/Java stack\", \"legacy app\", or\n"
            "any phrase that omits the detected framework / database is a\n"
            "VIOLATION and the section will be regenerated.\n"
            "═══════════════════════════════════════════════════════════════════\n"
            f"summary:    {detected.get('summary', '')}\n"
            f"language:   {detected.get('language', '') or '(unknown)'}\n"
            f"languages:  {lang_line}\n"
            f"frameworks (use these names — DO NOT abbreviate, DO NOT drop):\n{fw_block}"
            f"database:   {detected.get('database', '') or '(unknown)'}\n"
            f"databases (use these names — DO NOT abbreviate, DO NOT drop):\n{db_block}"
            f"build:      {detected.get('build', '') or '(unknown)'}\n"
            f"evidence (language sample): {_fmt_evidence_list(detected.get('evidence', {}).get('language'))}\n"
            "─────────────────────────────────────────────────────────────────\n"
            "REQUIRED PHRASING EXAMPLES (substitute the actual values):\n"
            "  ✅  \"migrating the legacy {language} / {frameworks[0]} application\"\n"
            "  ✅  \"the existing {frameworks[0]} action classes\" (when Struts/Spring)\n"
            "  ✅  \"the source {database} schema\"\n"
            "  ❌  \"the legacy SQL/Java stack\"        ← drops framework\n"
            "  ❌  \"the existing Java application\"   ← drops framework + DB\n"
            "  ❌  \"the legacy system\"                ← drops EVERYTHING\n"
            "═══════════════════════════════════════════════════════════════════"
        )
        # PREPEND, not append.
        parts.insert(0, stack_block)

    # ── iter 13.8 ───────────────────────────────────────────────────
    # TARGET STACK GUARDRAILS + LEGACY COVERAGE CHECKLIST.
    # User report: SRS kept suggesting "PHP FastAPI" (PHP language with
    # Python framework) as the modern stack. Inject a hard-contract
    # block that bans these hybrids AND a 5-item legacy coverage
    # checklist (overview / structure / deps / schema / integrations)
    # so recommendations are never superficial. Prepended at position 0
    # so it dominates attention even when the DETECTED LEGACY STACK
    # block above also exists.
    # ────────────────────────────────────────────────────────────────
    try:
        from kb.target_stack import build_target_stack_block
        guardrails = build_target_stack_block(proj_doc)
        if guardrails:
            parts.insert(0, guardrails)
    except Exception:
        # Never let a guardrails-builder bug block SRS generation.
        pass

    header = (
        "═══════════════════════════════════════════════════════════════════\n"
        "MANDATORY GOVERNANCE BUNDLE — read in order, each block inherits\n"
        "from gov.core. Every rule below is a HARD CONTRACT. Violations\n"
        "will be rejected. truth_rule_mode = STRICT_VERIFIED · EVIDENCE_ONLY.\n"
        "═══════════════════════════════════════════════════════════════════\n\n"
    )
    bundle = header + "\n\n".join(parts) + "\n\n═══════════════════════════════════════════════════════════════════\n\n"
    # iter-13.70 — append the Agentic Autonomy + Self-Confidence charter
    # so every artifact ends with a `## CONFIDENCE SUMMARY` table the
    # multi-model confidence engine cross-checks.
    try:
        from agentic_charter import append_agentic_charter
        bundle = append_agentic_charter(bundle)
    except Exception:  # noqa: BLE001 — non-fatal
        pass
    return bundle


# --------------------------------------------------------------------------
# iter-13.18 — Coverage gate. After each section is generated, we compute
# what fraction of the entities visible in the TOON slice were actually
# cited in the section body. Below a threshold, we fire ONE focused
# continuation call asking the model to extend the section by covering
# the missing items. Bounded retry (max 1) keeps total tokens predictable
# while materially lifting SRS / CodeGen coverage on large monoliths
# where a single pass leaves half the modules un-referenced.
# --------------------------------------------------------------------------
_TOON_ENTITY_RE = _re.compile(
    r"^\s*-?\s*(?:name|class|table|route|method)\s*:\s*([A-Za-z_][\w./:-]{2,})",
    _re.MULTILINE,
)
_TOON_BARE_NAME_RE = _re.compile(r"^\s*-\s*([A-Z_][A-Za-z0-9_]{2,})\s*$", _re.MULTILINE)
_GENERIC_TOKENS = {
    "User", "Users", "Login", "Logout", "Admin", "Index", "Home", "Main",
    "Base", "Application", "App", "Config", "Test", "Tests", "Util", "Utils",
    "Helper", "Helpers", "Common", "Default", "Model", "View", "Controller",
    "Service", "Repository", "Dto", "Bean", "Entity",
}


def _extract_toon_entities(toon_slice: str, cap: int = 80) -> list[str]:
    """Pull candidate entity names from a TOON slice (best-effort, regex-only)."""
    if not toon_slice:
        return []
    seen: dict[str, None] = {}
    for m in _TOON_ENTITY_RE.finditer(toon_slice):
        token = m.group(1).strip().rstrip(":,.;")
        if len(token) >= 3 and token not in _GENERIC_TOKENS and token not in seen:
            seen[token] = None
            if len(seen) >= cap:
                break
    if len(seen) < cap:
        for m in _TOON_BARE_NAME_RE.finditer(toon_slice):
            token = m.group(1).strip()
            if len(token) >= 3 and token not in _GENERIC_TOKENS and token not in seen:
                seen[token] = None
                if len(seen) >= cap:
                    break
    return list(seen.keys())


def _compute_coverage(content: str, entities: list[str]) -> tuple[float, list[str]]:
    """Return (coverage_ratio, missing_entities) — substring match, case-sensitive."""
    if not entities:
        return 1.0, []
    body = content or ""
    missing = [e for e in entities if e not in body]
    covered = len(entities) - len(missing)
    return (covered / len(entities)), missing


# --------------------------------------------------------------------------
# iter-13.79 — Use-Case completeness gate (for §5 detailed_use_cases).
#
# Why this exists: the model often emits §5 with a fraction of the UC
# inventory expanded, then truncates. The pre-existing TOON-coverage gate
# counts entity NAMES, not UC IDs, so it can't detect "12 of 25 UCs done".
# This gate enumerates expected UC-* IDs from §3 + the legacy-analysis
# digest, audits §5 against the 5 mandatory subsections per UC, and
# returns a structured report the caller can act on.
# --------------------------------------------------------------------------
_UC_ID_RE = _re.compile(r"\bUC-[A-Za-z0-9_-]{1,40}\b")
_WF_ROW_RE = _re.compile(
    # Matches the digest format emitted by `build_analysis_digest`:
    #   "- **WF-01 Tender Approval** (actor: …, states: …)"
    # Also catches bare "WF-01: Title" and "WF-01 — Title" fallbacks.
    r"\bWF-\d+\b(?:\s*[:\-–—]?\s*)?(?:\*\*)?\s*([^\n*()]{0,80})",
)


def _extract_workflow_ids(analysis_digest: str, cap: int = 40) -> dict[str, str]:
    """Extract ``{WF-NN: title}`` pairs from the analysis_digest markdown.

    Returns an insertion-ordered map so downstream prompts can iterate
    workflows in the same order the digest emitted them. Titles are
    best-effort; empty strings when only the ID appears.
    """
    if not analysis_digest:
        return {}
    out: dict[str, str] = {}
    for m in _re.finditer(r"\bWF-\d+\b[^\n]*", analysis_digest):
        line = m.group(0)
        wf_id_m = _re.match(r"\bWF-\d+\b", line)
        if not wf_id_m:
            continue
        wf_id = wf_id_m.group(0)
        if wf_id in out:
            continue
        rest = line[wf_id_m.end():].strip()
        # Peel leading punctuation + markdown bold markers.
        rest = _re.sub(r"^[\s:*\-–—]+", "", rest)
        # Take everything up to the first "(" or "**" as the title.
        title = _re.split(r"\s*\(|\s*\*\*", rest, maxsplit=1)[0].strip().rstrip(".;:")
        out[wf_id] = title
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# iter-14.25.4 — Deterministic roster helpers (anti-hallucination guardrails).
#
# The CGHS pilot SRS PDF (139 pages, Aug 2026) showed two independent failure
# modes that no amount of prompt-eloquence had cured:
#
#   1) §1.3 Definitions filled with LLM-invented acronyms like
#      "PYY — Project Yearly Year", "QQA — Quality Quality Assurance",
#      "NHS — National Health Service (UK)". Pure padding — none tied
#      to the actual codebase.
#
#   2) §5 Detailed Use Cases collapsed into 4 generic CRUD stubs
#      (Submit / Approve / Reject / View Application) with zero binding
#      to the 12 real workflows (Annual Health Checkup, Chronic OP
#      Registration, Cochlear Implant Follow-Up, …) that the analyzer
#      correctly extracted as WF-01..WF-12.
#
# Both failures share the same root cause: the section prompts left the
# LLM free to invent the *set* of items (terms in §1.3; UCs in §5) rather
# than being handed a deterministic roster. The fixes below produce that
# roster from the analysis_digest — the same source the confidence engine
# scores against, so nothing added here can be marked "not in KB".
# ---------------------------------------------------------------------------

# Actor row shape emitted by ``build_analysis_digest``:
#   "**Actors:** A-01 Applicant, A-02 Field Officer, A-03 District Magistrate"
_ACTOR_ROW_RE = _re.compile(r"\bA-\d+\b\s+([^,\n]+?)(?=,|$)")

# Acronym candidate — 2–6 uppercase letters, optional trailing digit.
# Excludes single letters and CamelCase leading caps (must be all-caps).
_ACRONYM_RE = _re.compile(r"\b[A-Z]{2,6}\d?\b")

# Tech / language acronyms that keep leaking in from framework nouns —
# these are NOT domain glossary entries and must never seed §1.3.
_ACRONYM_BLOCKLIST = {
    "PHP", "JSP", "JSF", "JPA", "JDBC", "JVM", "JWT", "SQL", "XML", "JSON",
    "HTTP", "HTTPS", "HTML", "CSS", "JS", "API", "REST", "SOAP", "RPC",
    "CRUD", "MVC", "ORM", "DTO", "DAO", "POJO", "URI", "URL", "UUID",
    "TCP", "UDP", "IP", "DNS", "TLS", "SSL", "CI", "CD", "IDE", "SDK",
    "OS", "UI", "UX", "DB", "OK", "ID", "PDF", "CSV", "ZIP", "AWS",
    "GCP", "IBM", "GNU", "GPL", "MIT", "ASF", "IEEE", "ISO",
    # SRS structural terms that legitimately appear but are not glossary:
    "SRS", "FR", "NFR", "BR", "UC", "WF", "PR", "PO",
}


def _extract_actors_from_digest(analysis_digest: str) -> dict[str, str]:
    """Extract ``{A-NN: role_name}`` pairs from the ``**Actors:**`` line.

    ``build_analysis_digest`` emits actors as a single comma-separated
    line: ``**Actors:** A-01 Applicant, A-02 Field Officer, …``. We
    parse that inline list; malformed rows (missing ID or blank name)
    are skipped. Insertion-ordered, deduped by ID.
    """
    if not analysis_digest:
        return {}
    # Find the Actors line, then scan every "A-NN Name" tuple on it.
    m = _re.search(r"\*\*Actors:\*\*[^\n]+", analysis_digest)
    if not m:
        return {}
    line = m.group(0)
    out: dict[str, str] = {}
    for pair in _re.finditer(r"\b(A-\d+)\b\s+([^,\n]+?)(?=,|$)", line):
        aid = pair.group(1)
        name = pair.group(2).strip().rstrip(".;:")
        if aid and name and aid not in out:
            out[aid] = name
    return out


def _build_uc_roster_from_workflows(
    analysis_digest: str,
    cap: int = 60,
) -> list[dict]:
    """Deterministic ``UC-NN`` roster derived from the workflow inventory.

    Empirically the model collapses §5 into 4 generic CRUD stubs when it
    is left free to *invent* the UC set. When it's handed a fixed roster
    ("emit exactly these N UCs, in this order, with these titles and
    actors"), the collapse cannot happen — the roster IS the section
    scaffold. This is that roster.

    Returns a list of dicts:
        {"uc_id": "UC-01", "wf_id": "WF-01", "name": "Tender Approval",
         "actor": "District Magistrate"}

    Empty when there are no workflows in the digest (the caller then
    falls back to §3-scanned UC IDs, i.e. the pre-14.25.4 behaviour).
    """
    workflows = _extract_workflow_ids(analysis_digest, cap=cap)
    if not workflows:
        return []
    # Actor per WF is embedded in the digest as
    #   "- **WF-01 Tender Approval** (actor: District Magistrate, states: …)"
    # so scan actor-per-line rather than relying on the global A-* map.
    per_wf_actor: dict[str, str] = {}
    for m in _re.finditer(
        r"\b(WF-\d+)\b[^\n]*?actor:\s*([^,\n)]+)",
        analysis_digest,
    ):
        per_wf_actor.setdefault(m.group(1), m.group(2).strip().rstrip(".;:"))
    roster: list[dict] = []
    for i, (wf_id, wf_title) in enumerate(workflows.items(), start=1):
        roster.append({
            "uc_id": f"UC-{i:02d}",
            "wf_id": wf_id,
            "name": wf_title or f"Workflow {wf_id}",
            "actor": per_wf_actor.get(wf_id, "").strip() or "NOT_EVIDENCED",
        })
    return roster


def _build_glossary_roster(
    analysis_digest: str,
    extra_corpus: str = "",
    cap: int = 40,
) -> dict[str, str]:
    """Return a whitelist of domain acronyms/terms allowed in §1.3.

    Extracts all-caps 2–6-letter tokens from the ``analysis_digest`` +
    optional ``extra_corpus`` (typically the KB summary block). Filters
    tech/framework acronyms via ``_ACRONYM_BLOCKLIST`` so §1.3 stays
    focused on domain vocabulary. Also picks up domain entity NAMES so
    long-form terms like "Chronic Outpatient" get an explanation slot
    even when their acronym doesn't appear.

    The returned dict maps ``term → source_hint`` where ``source_hint``
    is a short breadcrumb like ``"analysis_digest"`` — the prompt shows
    the hint so the model knows the term is evidenced and NOT to
    invent a fresh acronym.

    Empty dict → digest has no evidenced glossary vocabulary; the §1.3
    instructions then tell the model to write
    ``_(no acronyms evidenced in source)_`` rather than fabricate a list.
    """
    if not analysis_digest and not extra_corpus:
        return {}
    corpus = f"{analysis_digest}\n{extra_corpus}"
    counts: dict[str, int] = {}
    for m in _ACRONYM_RE.finditer(corpus):
        tok = m.group(0)
        if tok in _ACRONYM_BLOCKLIST:
            continue
        # Skip pure section-ID tokens (WF01, UC01) — those aren't glossary.
        if _re.match(r"^(WF|UC|FR|BR|NFR|A|R)\d+$", tok):
            continue
        counts[tok] = counts.get(tok, 0) + 1
    out: dict[str, str] = {}
    # Only keep acronyms that appear >= 2 times to filter noise, or that
    # appear alongside a Title-Case word suggesting they're expanded
    # somewhere in the corpus.
    for tok, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        if n >= 2:
            out[tok] = "analysis_digest"
        if len(out) >= cap:
            break
    # Also seed domain entity names (Title-Case multi-word terms) from
    # the **Domain Entities:** block of the digest so long-form glossary
    # candidates make it in.
    for m in _re.finditer(r"\*\*Domain Entities:\*\*(.+?)(?=\n\*\*|\Z)",
                          analysis_digest, _re.DOTALL):
        block = m.group(1)
        for row in _re.finditer(r"^\s*[-*]\s*\*\*([^*]+?)\*\*", block, _re.MULTILINE):
            name = row.group(1).strip().rstrip(".;:")
            if 3 <= len(name) <= 60 and name not in out:
                out[name] = "domain_entities"
            if len(out) >= cap:
                break
    return out
_UC_HEADER_RE = _re.compile(
    # Matches `### UC-01: Some Name`, `#### UC-AUTH-003 — Login Flow`, etc.
    r"^(?:#{2,5})\s+(UC-[A-Za-z0-9_-]{1,40})(?:\s*[:\-–—]\s*(.{0,160}))?\s*$",
    _re.MULTILINE,
)
# The subsections we treat as MANDATORY per the §5 instructions.
# iter-17.6 — added "end-to-end journey" (Screen → API → DB traceability
# table). This is the primary migration artefact that binds a UC to
# concrete routes + DB columns for downstream DataModel + Architecture.
_UC_REQUIRED_SUBSECTIONS = (
    "narrative",
    "preconditions",
    "postconditions",
    "business workflow",
    "business rules",
    "end-to-end journey",
)


def _enumerate_expected_uc_ids(
    prior_sections: dict[str, str] | None,
    analysis_digest: str,
    cap: int = 60,
) -> list[str]:
    """Pull every UC-* ID seen in §3 (prior_sections) + the legacy-analysis
    digest's workflows / user-journeys. De-duplicated, order-stable, capped
    so a runaway KB can't blow the prompt budget."""
    seen: dict[str, None] = {}

    def _scan(text: str):
        if not text:
            return
        for m in _UC_ID_RE.finditer(text):
            uid = m.group(0)
            if uid not in seen:
                seen[uid] = None
                if len(seen) >= cap:
                    return

    # 1. Prior section text — §3 actor-matrix is the canonical UC inventory.
    if prior_sections:
        for k in ("actors_use_case_inventory", "overall_description",
                  "specific_requirements"):
            _scan(prior_sections.get(k) or "")
            if len(seen) >= cap:
                break

    # 2. Deep legacy analysis digest — workflows / user journeys frequently
    #    carry IDs that map 1:1 onto UCs the model should emit.
    _scan(analysis_digest or "")
    return list(seen.keys())


def _audit_uc_completeness(content: str, expected_ids: list[str]) -> dict:
    """Inspect §5 body and report which UCs are missing and, for those that
    are present, which mandatory subsections are absent. Presence check is
    tolerant: `#### Narrative`, `### Narrative`, or `**Narrative**` all
    count — under length pressure models sometimes drop the heading level."""
    if not content:
        return {"expected": expected_ids, "present": [], "missing": list(expected_ids),
                "incomplete": {}}
    headers = list(_UC_HEADER_RE.finditer(content))
    blocks: dict[str, str] = {}
    for i, m in enumerate(headers):
        uid = m.group(1)
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(content)
        body = content[start:end]
        # Duplicate UID → keep the longer block (usually the more complete one)
        if uid in blocks:
            if len(body) > len(blocks[uid]):
                blocks[uid] = body
        else:
            blocks[uid] = body
    present_ids = list(blocks.keys())
    missing = [uid for uid in expected_ids if uid not in blocks]
    incomplete: dict[str, list[str]] = {}
    for uid, body in blocks.items():
        lowered = body.lower()
        absent = [
            sub for sub in _UC_REQUIRED_SUBSECTIONS
            if (f"#### {sub}" not in lowered
                and f"### {sub}" not in lowered
                and f"**{sub}" not in lowered)
        ]
        if absent:
            incomplete[uid] = absent
    return {"expected": expected_ids, "present": present_ids,
            "missing": missing, "incomplete": incomplete}


async def _gen_one_section(
    cfg: dict,
    proj: dict,
    full_toon: str,
    summary: str,
    convo_text: str,
    model: str,
    project_id: str | None = None,
    prior_sections: dict[str, str] | None = None,
    analysis_digest: str = "",
    fast_mode: bool = False,
    agent_key: str = "srs.generate",   # iter-13.76 — regenerate passes "srs.regenerate" → Opus
    remediation_hints: list[str] | None = None,   # iter-14.10 — auto-improve loop
) -> dict:
    """Generate one SRS section. Returns {'content': ..., 'tokens': int}.

    iter-13.81.5 — STICKY agent_key for the whole SRS run.
    `_gen_one_section` is invoked many times per run (one per section, plus
    repair / coverage-gate continuation passes). Most callers omit `agent_key`,
    so the function default `"srs.generate"` was overriding the request-scoped
    contextvar (`srs.regenerate`) that /generate and /generate/stream set at
    the top of every re-run — and Factory.ai kept resolving the FIRST-RUN
    Console pin even on regenerations. Honour the contextvar when the caller
    did NOT explicitly override the default.
    """
    if agent_key == "srs.generate":
        try:
            _ctx_ak = (get_current_agent_key() or "").strip()
            if _ctx_ak:
                agent_key = _ctx_ak
        except Exception:  # noqa: BLE001
            pass

    """Original-docstring continued below (kept for IDE hover).

    `prior_sections` (iter-13.10 quality fix) — dict of `{key: markdown}` for
    sections produced earlier in the same run. Threaded so later sections can
    reference earlier ones (e.g. Section 5 use-cases cite Section 4 FR-IDs;
    Section 10 traceability rows pull FR-IDs from Section 4).  Without this
    the model produced cross-section inconsistencies (FR-IDs cited in §5 that
    never appeared in §4) — one of the top drivers of the 'reads like Copilot
    is better' user complaint.

    `analysis_digest` (iter-13.17) — compact markdown digest of the deep
    legacy-logic analysis (workflows, business rules, state machines,
    integrations, user journeys, calculations) produced by `kb/legacy_analyzer.py` and
    auto-triggered in `_load_srs_context`. Injected as a SHARED PREAMBLE
    into every section's system prompt so sections cite the SAME workflow /
    rule IDs instead of re-deriving them from raw TOON + RAG. Empty string
    when the deep-analysis pass failed or hasn't run yet — sections then
    fall back to the previous TOON+RAG-only behaviour.
    """
    # Section 9 is computed deterministically from KB entities — no LLM.
    if cfg["key"] == "entity_model":
        return await _gen_entity_model(project_id or "")

    # -------- Section-tier classification (drives every budget) ------------
    # Heavy sections (FRs, detailed use-cases) need substantially more
    # context, output budget, and slightly higher creativity than the
    # one-shot housekeeping sections.
    is_heavy = cfg["min_words"] >= 1500
    is_medium = (not is_heavy) and cfg["min_words"] >= 600

    # -------- TOON skeleton --------------------------------------------------
    # Pull a larger slice for heavy sections — they cite many classes/tables.
    toon_budget = 9000 if is_heavy else (6000 if is_medium else 4000)
    toon_slice = extract_toon_section(full_toon, cfg["toon_focus"], toon_budget)
    if not toon_slice.strip():
        toon_slice = "\n".join([
            extract_toon_section(full_toon, "CLASSES", 8000),
            extract_toon_section(full_toon, "TABLES", 8000),
        ]).strip() or full_toon[:10000]

    # -------- Semantic RAG ---------------------------------------------------
    # iter-13.18 — multi-facet retrieval. Each SECTION_QUERIES entry is a
    # space-separated bag of concept tokens. Splitting it into 3 facet
    # queries (terms 0-3, 3-6, 6+) and unioning the dedup'd results
    # materially lifts recall vs a single bag-query, at roughly the same
    # Qdrant cost. Falls back to single-query `qdrant_search` on failure.
    rag_top_k = 30 if is_heavy else (20 if is_medium else 12)
    rag_chunks: list[str] = []
    if project_id:
        query_bag = SECTION_QUERIES.get(cfg["key"], cfg["label"])
        terms = [t for t in query_bag.split() if t]
        # Build 3 facet queries from the term bag — each is a coherent
        # phrase the embedder can match against code/comments cleanly.
        facets: list[str] = []
        if len(terms) >= 6:
            facets = [
                " ".join(terms[:4]),
                " ".join(terms[3:7]),
                " ".join(terms[6:]),
            ]
        elif len(terms) >= 3:
            facets = [" ".join(terms[:3]), " ".join(terms[2:])]
        else:
            facets = [query_bag]
        # Add the section label as an extra facet — captures sections
        # whose code/comment text aligns with the human-readable label.
        if cfg["label"] not in facets:
            facets.append(cfg["label"])
        try:
            per_q = max(5, rag_top_k // max(1, len(facets) - 1))
            rag_chunks = await qdrant_search_many(
                project_id, facets, per_query_k=per_q, final_k=rag_top_k,
            )
            # If multi-facet returned nothing (e.g. helper unavailable), fall back.
            if not rag_chunks:
                rag_chunks = await qdrant_search(project_id, query_bag, top_k=rag_top_k)
        except Exception:
            try:
                rag_chunks = await qdrant_search(project_id, query_bag, top_k=rag_top_k)
            except Exception:
                rag_chunks = []
    rag_context = "\n\n---\n\n".join(rag_chunks) if rag_chunks else ""

    # If no RAG available, swell the TOON slice so the model still has substance.
    if not rag_context:
        toon_slice = (
            extract_toon_section(full_toon, cfg["toon_focus"], 20000) or toon_slice
        )

    kb_block = (
        f"STRUCTURAL SKELETON (TOON — {cfg['toon_focus']}):\n{toon_slice}"
        + (f"\n\nSEMANTICALLY RELEVANT CODE & SCHEMA:\n{rag_context}" if rag_context else "")
    )

    # ---- iter-13.19 — Graph subgraph (env-gated) -------------------------
    # When LAMA_USE_GRAPH_KB=1 we pull a compact YAML subgraph from the
    # property graph built during Build KB. Cheaper than RAG chunks AND
    # more precise (explicit relations: REFERENCES_TABLE, EXPOSES,
    # BELONGS_TO_ENTITY, …). Falls back silently if the graph wasn't built
    # or the helper raises — the section then runs on TOON + RAG only.
    graph_block = ""
    try:
        from kb.graph_config import is_graph_kb_enabled
        if project_id and await is_graph_kb_enabled(project_id):
            from kb.graph_retriever import subgraph_yaml_for_section
            # Extra seeds from the section's RAG query terms — improves
            # match rate against table/class names embedded in the bag.
            extra = SECTION_QUERIES.get(cfg["key"], "").split()
            graph_yaml = await subgraph_yaml_for_section(
                project_id,
                cfg["key"],
                extra_seeds=extra,
                max_chars=4500 if is_heavy else 3000,
                hops=2,
                max_nodes=140 if is_heavy else 90,
            )
            if graph_yaml:
                graph_block = (
                    "\n\nGRAPH SUBGRAPH (authoritative entity/relation skeleton — "
                    "cite these names VERBATIM, follow the declared relations):\n"
                    + graph_yaml
                    + "\n\nGRAPH COVERAGE MANDATE (iter-13.75 — enforced by the "
                    "revalidation + confidence engines):\n"
                    "• Every Table / Route / Method / Role / BusinessEntity "
                    "node above MUST be referenced verbatim in THIS section "
                    "when it falls within scope — use the section catalogue's "
                    "`what_to_check` as the scope filter.\n"
                    "• Follow the declared edges (REFERENCES_TABLE, EXPOSES, "
                    "HAS_METHOD, GUARDED_BY, BELONGS_TO_ENTITY, "
                    "BELONGS_TO_MODULE) when narrating how an entity is used.\n"
                    "• If a node is genuinely out-of-scope for this section, "
                    "DO NOT silently drop it — leave it for the section where "
                    "it belongs. If it IS in scope but evidence is thin, emit "
                    "the row with `confidence=NOT_EVIDENCED` and an inline "
                    "`⚠ EVIDENCE GAP` marker.\n"
                    "• A graph node cited verbatim wins over a paraphrase; "
                    "the confidence evaluator scores higher when section "
                    "rows align 1:1 with graph nodes."
                )
    except Exception:  # noqa: BLE001 — graph is best-effort
        graph_block = ""
    if graph_block:
        kb_block = kb_block + graph_block

    # ---- iter-14.25 — Journey-KB slice (env + per-project gated) ----------
    # Additive: prepends focused api_journey / ui_journey blocks so the
    # SRS section grounds every use case / FR / entity in a concrete
    # endpoint→controller→service→table→columns trace from `kb_journeys`.
    # Silent no-op when the per-project toggle is off — the section then
    # runs on TOON + RAG (+ optional graph) only, identical to legacy.
    journey_block_srs = ""
    try:
        from kb.journey_materializer import journey_context_block
        j_seeds = (SECTION_QUERIES.get(cfg["key"], "") or "").split()
        journey_block_srs = await journey_context_block(
            project_id, stage="srs",
            seeds=j_seeds, endpoint_hints=j_seeds,
            max_chars=4500 if is_heavy else 3000,
            max_journeys=14 if is_heavy else 9,
        )
    except Exception:  # noqa: BLE001 — journeys are best-effort
        journey_block_srs = ""
    if journey_block_srs:
        kb_block = kb_block + "\n\n" + journey_block_srs

    # -------- Prior-section context (iter-13.10) ----------------------------
    # Send the model a SHORT digest of every section already written in this
    # run so it can cite real IDs and stay tonally consistent. We pass the
    # full markdown of small sections and only the headings/IDs of large
    # ones — keeps prompt tokens bounded while still preserving cross-refs.
    #
    # iter-14.10 — REGENERATION DEBT LEDGER
    #
    # Prior symptom: user regenerated 3 times and the confidence pill stayed
    # at ~35%. Root cause: the section-body digest below was filtering out
    # everything except headings + FR/UC-like table rows, which meant the
    # `<!-- CONFIDENCE_SELF_SCORE: 52 ... OPEN_GAPS: ... -->` footer was
    # stripped BEFORE being fed back to the LLM. The seed prompt tells the
    # model "read your prior score + close every OPEN_GAP" — but the model
    # literally never saw the score or the gaps, so every regen was blind
    # and reproduced the same defects.
    #
    # Fix: parse the prior footer + the last stage_confidence report for
    # THIS section, and prepend both as an explicit REGENERATION DEBT
    # LEDGER at the top of the prompt with a hard directive to close each
    # gap. Also strip the raw footer from `prior_block` so the parsed
    # summary doesn't get duplicated by the raw-text pass-through.
    _footer_re = _re.compile(
        r"<!--\s*(?:.*?\n)?\s*CONFIDENCE_SELF_SCORE\s*:\s*(\d+(?:\.\d+)?)\s*"
        r"(?:.*?OPEN_GAPS\s*:\s*(.*?))?-->",
        _re.DOTALL | _re.IGNORECASE,
    )

    def _parse_prior_footer(text: str) -> tuple[float | None, list[str]]:
        if not text:
            return None, []
        m = _footer_re.search(text)
        if not m:
            return None, []
        try:
            score = float(m.group(1))
        except (TypeError, ValueError):
            score = None
        gaps_raw = (m.group(2) or "").strip()
        gaps: list[str] = []
        for ln in gaps_raw.splitlines():
            s = ln.strip().lstrip("-*• ").strip()
            if s:
                gaps.append(s[:220])
        return score, gaps[:20]

    def _strip_footer(text: str) -> str:
        # Removes both well-formed <!-- ... --> footers and truncated ones
        # (opening `<!--` with no matching `-->`) so a malformed footer
        # can't leak as visible body text into the next regenerate prompt.
        text = _footer_re.sub("", text or "")
        # Also drop trailing dangling `<!--` if the LLM forgot the close.
        text = _re.sub(r"<!--\s*CONFIDENCE_SELF_SCORE[\s\S]*$", "", text, flags=_re.IGNORECASE)
        return text.rstrip()

    # Pull prior self-score + open-gaps for THE section we're about to
    # regenerate (the one keyed by cfg["key"] in prior_sections).
    _this_prior = (prior_sections or {}).get(cfg["key"], "") if prior_sections else ""
    _prior_self_score, _prior_open_gaps = _parse_prior_footer(_this_prior)

    # Pull the LAST evaluator verdict for this section from the stage
    # confidence report (persisted by pipeline.compute_stage_confidence).
    # This gives us the INDEPENDENT judge's rationale + gap list, which
    # is the single most useful signal for a regen: it names exactly
    # what the multi-model evaluator flagged as missing/wrong.
    _judge_score: float | None = None
    _judge_gaps: list[str] = []
    _judge_rationale: str = ""
    if project_id:
        try:
            from db import stage_confidence as _sc_col
            _sc_doc = await _sc_col.find_one(
                {"project_id": project_id, "stage": "Discovery"},
                {"_id": 0, "sections": 1, "generated_at": 1},
            ) or {}
            for _srow in (_sc_doc.get("sections") or []):
                # `_REPORT_SECTIONS` catalogue keys (workflow, business_rules
                # etc.) reference the SRS section via `source_keys` — we
                # want any evaluator row whose source_keys include the SRS
                # section currently being regenerated.
                pass
            # The catalogue uses `source_keys` to map judge-section →
            # SRS-section-keys.  Load the catalogue once and pick rows.
            try:
                from routes.living import _REPORT_SECTIONS as _RS
            except Exception:  # noqa: BLE001
                _RS = []
            _target = cfg["key"]
            _related = []
            for _cat in _RS:
                _srcs = _cat.get("source_keys") or []
                if any(sk.endswith("." + _target) or sk == "srs." + _target for sk in _srcs):
                    _related.append(_cat.get("key"))
            if _related:
                for _srow in (_sc_doc.get("sections") or []):
                    if _srow.get("key") in _related:
                        s = _srow.get("score")
                        if isinstance(s, (int, float)):
                            _judge_score = float(s) if _judge_score is None else min(_judge_score, float(s))
                        for g in (_srow.get("gaps") or []):
                            if isinstance(g, str) and g.strip():
                                _judge_gaps.append(g.strip()[:220])
                        _r = _srow.get("rationale")
                        if isinstance(_r, str) and _r.strip() and not _judge_rationale:
                            _judge_rationale = _r.strip()[:400]
                # De-duplicate gaps while preserving order.
                _seen = set()
                _uniq = []
                for g in _judge_gaps:
                    if g not in _seen:
                        _seen.add(g)
                        _uniq.append(g)
                _judge_gaps = _uniq[:12]
        except Exception:  # noqa: BLE001 — never block generation on lookup
            pass

    debt_block = ""
    if (_prior_self_score is not None) or _prior_open_gaps or (_judge_score is not None) or _judge_gaps:
        _ledger_lines: list[str] = [
            "══════════════════════════════════════════════════════════════════════",
            "REGENERATION DEBT LEDGER — YOU MUST BEAT THIS (iter-14.10)",
            "══════════════════════════════════════════════════════════════════════",
            "This is a REGENERATION of a section that has already been produced",
            "and independently scored. The numbers + gap lists below are FACTS,",
            "not suggestions. The new version of THIS section (`" + cfg["key"] + "`)",
            "MUST satisfy every rule below:",
            "",
        ]
        if _prior_self_score is not None:
            _ledger_lines.append(
                f"  • PRIOR SELF-SCORE (from your previous footer): {_prior_self_score:.1f} / 100"
            )
            _ledger_lines.append(
                f"    → New CONFIDENCE_SELF_SCORE MUST be strictly greater than "
                f"{_prior_self_score:.1f} AND MUST be ≥ 95. No exceptions."
            )
        if _judge_score is not None:
            _ledger_lines.append(
                f"  • INDEPENDENT MULTI-MODEL JUDGE SCORE (last run): {_judge_score:.1f} / 100"
            )
            _ledger_lines.append(
                "    → After this regen, an independent multi-model panel will "
                "re-score this section against the KB. Target ≥ 95 there too."
            )
        if _judge_rationale:
            _ledger_lines.append(f"  • JUDGE RATIONALE: {_judge_rationale}")
        if _prior_open_gaps or _judge_gaps:
            _ledger_lines.append("")
            _ledger_lines.append("  OPEN GAPS TO CLOSE IN THIS PASS "
                                 "(each MUST become either a fully-evidenced row")
            _ledger_lines.append("   OR an explicit `NOT_EVIDENCED` row with `⚠ EVIDENCE GAP`")
            _ledger_lines.append("   citing where you looked — silently dropping a gap = FORBIDDEN):")
            _all_gaps: list[str] = []
            _seen: set[str] = set()
            for g in (_judge_gaps + _prior_open_gaps):
                gs = g.strip()
                if gs and gs.lower() not in _seen:
                    _seen.add(gs.lower())
                    _all_gaps.append(gs)
            for i, g in enumerate(_all_gaps[:20], start=1):
                _ledger_lines.append(f"    {i:>2}. {g}")
        _ledger_lines.extend([
            "",
            "HARD RULES FOR THIS REGEN:",
            "  1. NEVER regress. Preserve every evidenced row from the prior",
            "     version — add missing ones; refactor only if it raises coverage.",
            "  2. Emit the CONFIDENCE_SELF_SCORE footer LAST, as a WELL-FORMED",
            "     `<!-- ... -->` HTML comment on separate lines (never single-line).",
            "     The prior footer LEAKED into the visible SRS body because it was",
            "     malformed — do NOT repeat that mistake.",
            "  3. Any KB row you CANNOT cite verbatim MUST be emitted with",
            "     `confidence=NOT_EVIDENCED` + `⚠ EVIDENCE GAP: <where you looked>`.",
            "     `NOT_EVIDENCED` counts toward coverage; silent omission does NOT.",
            "  4. Length monotonically grows or stays equal across regenerates until",
            "     the artifact converges at ≥ 95. Never shrink.",
            "══════════════════════════════════════════════════════════════════════",
            "",
        ])
        debt_block = "\n".join(_ledger_lines)

    prior_block = ""
    if prior_sections:
        digest_parts: list[str] = []
        for pkey, ptext in prior_sections.items():
            if not ptext:
                continue
            # iter-14.10 — strip any prior footer BEFORE truncation so a
            # malformed comment can't leak as visible body prose into the
            # next regenerate prompt.  Parsed values are already surfaced
            # via `debt_block` above; keeping them in body would only
            # confuse the LLM about whether to emit a NEW footer.
            t = _strip_footer(ptext).strip()
            if not t:
                continue
            # Cap individual section digest to keep total under ~12 KB.
            if len(t) > 4000:
                # Pull only headings + lines that look like requirement IDs.
                kept: list[str] = []
                for ln in t.splitlines():
                    s = ln.strip()
                    if (
                        s.startswith("#")
                        or _re.match(r"^\|", s)         # table rows (FR / UC IDs)
                        or _re.match(r"^[-*]\s+\*?\*?(FR|UC|NFR|BR|INT|REQ)-", s)
                    ):
                        kept.append(ln)
                t = "\n".join(kept)[:4000] or t[:4000]
            digest_parts.append(f"### {pkey}\n{t}")
        if digest_parts:
            prior_block = (
                "PRIOR SECTIONS ALREADY WRITTEN (cite their IDs verbatim, "
                "stay tonally consistent, do NOT re-introduce content):\n\n"
                + "\n\n".join(digest_parts)
            )

    # Prepend the debt ledger so the LLM reads it before the KB context.
    if debt_block:
        prior_block = debt_block + ("\n\n" + prior_block if prior_block else "")

    # -------- Output budget --------------------------------------------------
    # Bump heavy / medium sections so Claude doesn't soft-stop mid-table.
    if is_heavy:
        max_tokens = 16000
    elif is_medium:
        max_tokens = 12000
    else:
        max_tokens = 7000

    # iter-13.79 — §5 detailed_use_cases scales the budget to the UC count.
    # Empirical floor: ~800 tokens per UC (narrative + 5 mandatory subsections
    # + Main/Alt/Exception flows + field table + Mermaid). With 25 UCs that's
    # ~20k just for prose; the old 16k hard ceiling truncated half of them.
    # Capped at 32k so we don't blow past provider per-request limits.
    expected_uc_ids: list[str] = []
    if cfg["key"] == "detailed_use_cases":
        expected_uc_ids = _enumerate_expected_uc_ids(prior_sections, analysis_digest)
        if expected_uc_ids:
            per_uc = 800
            ceiling = 32000
            max_tokens = min(ceiling, max(max_tokens, len(expected_uc_ids) * per_uc))

    # ----- Compliance directive bundle (gov.* + IEEE 830/29148) -----
    # Editable globally via Prompt Library; per-project overrides win.
    # Falls back to empty if seed hasn't run yet (very first boot).
    # All KB-aware LLM calls share THIS bundle — single source of truth.
    compliance_preamble = ""
    if project_id:
        try:
            compliance_preamble = await _load_governance_bundle(project_id, proj.get("name", ""))
        except Exception:
            compliance_preamble = ""

    # Surface detected stack alongside the seed-supplied source_tech so the
    # LLM sees both. The DETECTED LEGACY STACK block (assembled inside
    # _load_governance_bundle and PREPENDED at position 0 since iter 13.5)
    # is authoritative — this is a redundant reminder inside the project
    # header so even a truncated/poorly-attended bundle still gets the
    # framework name in front of the model.
    detected = proj.get("detected_tech") or {}
    detected_summary = detected.get("summary") or ""
    detected_frameworks = ", ".join(detected.get("frameworks") or []) or "(none detected)"
    detected_db = detected.get("database") or "(none detected)"

    # Word-count target written as a *range* (lower bound + aspirational
    # upper bound) — empirically yields longer, denser sections than a
    # bare "minimum N words" line, because the model reads it as room to
    # elaborate rather than a ceiling to hit.
    min_w = cfg["min_words"]
    target_w = int(min_w * 1.6) if min_w else 0

    # -------- Deep legacy-logic preamble (iter-13.17) ----------------------
    # The pre-SRS analyzer (kb/legacy_analyzer.py) produces a strict-JSON
    # behavioural model of the codebase (workflows, business rules, state
    # machines, integrations, user journeys, calculations). We embed its
    # compact markdown digest as a SHARED PREAMBLE so every section cites
    # the same WF-XX / BR-XX / UJ-XX identifiers rather than re-deriving
    # logic from raw TOON+RAG (which produced inconsistent IDs across
    # sections and missed cross-file behaviours entirely).
    #
    # iter-13.95 — the digest is cached for 24h and is the #1 ingress
    # path for cross-project filenames (the deep-analyzer LLM
    # hallucinates `src/com/ahct/*` from training data when evidence is
    # thin, then we serve those paths to every SRS section as
    # "authoritative … cite verbatim"). Redact paths whose directory-
    # discriminated suffix is not present in THIS project's scope-checked
    # kb_files index before injection.
    #
    # iter-13.98 — upgraded from basename-only to PATH-SUFFIX matching
    # because basename collisions (TJHS having its own `LoginAction.java`
    # under a different parent dir) let PMIS `src/com/ahct/login/.../
    # LoginAction.java` slip through the iter-13.95 sanitizer.
    allowed_bn, allowed_suf = await _load_allowed_path_index(project_id, proj)
    if analysis_digest and (allowed_bn or allowed_suf):
        sanitized, n_redacted = _redact_foreign_paths(
            analysis_digest, allowed_bn, allowed_path_suffixes=allowed_suf,
        )
        if n_redacted:
            logger.warning(
                "SRS[%s] · section=%s · analysis_digest: redacted %d "
                "foreign path(s) (iter-13.98 path-suffix sanitizer kept "
                "them out of the prompt)",
                project_id, cfg["key"], n_redacted,
            )
        analysis_digest = sanitized
    if convo_text and (allowed_bn or allowed_suf):
        convo_text, n_conv = _redact_foreign_paths(
            convo_text, allowed_bn, allowed_path_suffixes=allowed_suf,
        )
        if n_conv:
            logger.warning(
                "SRS[%s] · section=%s · convo_text: redacted %d foreign path(s)",
                project_id, cfg["key"], n_conv,
            )
    if summary and (allowed_bn or allowed_suf):
        summary, n_sum = _redact_foreign_paths(
            summary, allowed_bn, allowed_path_suffixes=allowed_suf,
        )
        if n_sum:
            logger.warning(
                "SRS[%s] · section=%s · summary: redacted %d foreign path(s)",
                project_id, cfg["key"], n_sum,
            )
    analysis_block = ""
    starvation_banner = ""
    if analysis_digest:
        analysis_block = (
            "DEEP LEGACY-LOGIC ANALYSIS (authoritative pre-pass — cite IDs verbatim, "
            "do NOT re-derive workflows / rules / integrations from scratch):\n\n"
            + analysis_digest
        )
    # iter-14.25.5 — evidence-starvation detector. When the analyzer
    # output is too thin for the model to write concretely, we hit the
    # "generic Application Approval CRUD" failure mode. Log loudly so
    # the operator can rebuild the KB, and inject a visible banner at
    # the top of the section so the SRS reader knows this section is
    # NOT to be trusted until the analyzer is re-run.
    richness = _digest_richness(analysis_digest)
    if richness["starved"]:
        logger.warning(
            "SRS[%s] · section=%s · iter-14.25.5 evidence starvation detected "
            "(chars=%d workflows=%d actors=%d entities=%d rules=%d) — "
            "recommend re-running Build KB + legacy analyzer before trusting "
            "this section.",
            project_id, cfg["key"],
            richness["chars"], richness["workflows"], richness["actors"],
            richness["entities"], richness["rules"],
        )
        starvation_banner = (
            "══════════════════════════════════════════════════════════════════════\n"
            "EVIDENCE-STARVATION BANNER  (iter-14.25.5 — HARD RULE)\n"
            "══════════════════════════════════════════════════════════════════════\n"
            f"The DEEP LEGACY-LOGIC ANALYSIS digest has only "
            f"{richness['chars']} chars, {richness['workflows']} workflow(s), "
            f"{richness['actors']} actor(s) and {richness['entities']} domain\n"
            "entity(ies). This is below the empirical threshold for producing\n"
            "a concrete SRS section. If you cannot cite AT LEAST 2 specific\n"
            "workflow IDs, 2 actor names, and 2 domain entity references drawn\n"
            "verbatim from the digest / KB above, DO NOT invent generic content\n"
            "('Application Approval', 'User Login', 'Data Entry Form'). Instead,\n"
            "OPEN the section body with this single-line notice — verbatim:\n\n"
            "    > ⚠ **EVIDENCE-STARVED SECTION** — the pre-SRS legacy analyzer\n"
            "    > produced a thin digest (see stats above). Please run\n"
            "    > **Build KB → Deep Analysis** on the full source tree and\n"
            "    > regenerate this section. The content below is best-effort\n"
            "    > and NOT to be signed off until the analyzer is re-run.\n\n"
            "…then proceed with WHATEVER concrete evidence IS available — do\n"
            "not fabricate placeholder tables just to fill the section length.\n"
        )

    # ── iter-13.35 — LEGACY FILESYSTEM CONTEXT block ──────────────────
    # Tells the LLM (and Factory.ai Droid in particular) WHERE the actual
    # source files live + lists the top filenames so it can read them
    # DIRECTLY rather than relying on TOON / RAG samples. The Droid has
    # filesystem access via its working directory, so when this block
    # mentions a cwd we instruct the Droid to `cat`/`grep` the files.
    legacy_path = (proj.get("legacy_path") or "").strip()
    factory_cwd = ""
    factory_enabled = False
    try:
        _fset = ((proj.get("settings") or {}).get("factory_orchestrator") or {})
        if _fset.get("enabled"):
            factory_enabled = True
            factory_cwd = (_fset.get("cwd") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    legacy_fs_block = ""
    if legacy_path or factory_cwd:
        # Build a top-N file index (capped at 80 files / 6 KB).
        # ── iter-13.89 forensic fix ─────────────────────────────────────
        # CRITICAL: this index is injected into the SRS prompt as the
        # authoritative "LEGACY FILE INDEX". The previous code used
        # `{"project_id": project_id} if project_id else {}` — when
        # `project_id` was falsy the filter collapsed to `{}` and the
        # query returned EVERY kb_files document across EVERY project
        # AND EVERY tenant. Those foreign filenames then ended up in the
        # SRS as "cite VERBATIM" evidence, which is exactly the
        # cross-project leakage the user reported.
        #
        # Hardening (defence in depth):
        #   1. Refuse to build the block at all when project_id is empty.
        #   2. Always scope by project_id (no `else {}` fallback).
        #   3. Also scope by tenant_id when the project doc carries one,
        #      so a stray project_id reuse across tenants still can't
        #      bleed (project_id is a UUID so collisions are theoretical,
        #      but the cost of the extra filter is zero).
        file_rows: list[str] = []
        if project_id:
            try:
                from db import kb_files as _kbf
                _tid = (proj.get("tenant_id") or "").strip()
                _kbf_filter: dict = {"project_id": project_id}
                if _tid:
                    _kbf_filter["tenant_id"] = _tid
                cursor = _kbf.find(
                    _kbf_filter,
                    {"_id": 0, "filename": 1, "filetype": 1, "size": 1},
                ).sort("size", -1).limit(80)
                async for f in cursor:
                    fname = (f.get("filename") or "").strip()
                    if not fname:
                        continue
                    file_rows.append(
                        f"- {fname}  ({f.get('filetype', '')}, {f.get('size', 0)} B)"
                    )
            except Exception:  # noqa: BLE001
                file_rows = []
        index_md = "\n".join(file_rows[:80])
        if len(index_md) > 6000:
            index_md = index_md[:6000] + "\n- … (truncated)"

        droid_hint = ""
        if factory_enabled and factory_cwd:
            # iter-13.94 — even when a cwd is pinned, the Droid Computer's
            # filesystem is SHARED ACROSS LAMA SESSIONS. Past projects (e.g.
            # PMIS with `legacy-code/src/com/ahct/*`) leave artifacts at,
            # below, or alongside the cwd. If we tell the model "OPEN the
            # relevant file(s)" without binding to the scope-checked index,
            # it `ls`/`find`s around and cites whatever it discovers —
            # producing the EXACT cross-project leak iter-13.90/91/92
            # tried to close (TJHS SRS citing `src/com/ahct/*` despite
            # zero ahct files in its KB). Lock shell access to filenames
            # that appear VERBATIM in the LEGACY FILE INDEX below.
            droid_hint = (
                "FACTORY DROID FILESYSTEM ACCESS (HARD-BOUNDED):\n"
                "  • You are running INSIDE a Factory Droid Computer with read access to the project tree.\n"
                f"  • Working directory: `{factory_cwd}`\n"
                "  • The Droid filesystem is SHARED across LAMA sessions and may contain stale\n"
                "    source from UNRELATED projects (sibling dirs, parent dirs, or even inside cwd).\n"
                "    Reading those leaks foreign filenames into THIS project's SRS.\n"
                "  • ALLOWED reads — you MAY `cat` / `grep -n` / `sed -n 'A,Bp'` a file ONLY IF its\n"
                f"    path (relative to `{factory_cwd}`) appears VERBATIM in the LEGACY FILE INDEX\n"
                "    below. Any other path is FOREIGN — treat it as if it does not exist.\n"
                "  • FORBIDDEN tools — `ls`, `find`, `tree`, `fd`, `rg --files`, glob expansion\n"
                "    (`cat src/**`), or any directory-walk. Discovery via shell is the leak vector;\n"
                "    the LEGACY FILE INDEX is your SOLE discovery surface.\n"
                "  • If a workflow / rule / column you need to cite is NOT present in the index,\n"
                "    say so explicitly in the SRS (gap) — do NOT shell out to look for it.\n"
                f"  • When you cite a file, give its path RELATIVE TO `{factory_cwd}` plus the line\n"
                "    range or function/class name you read it from.\n"
            )
        elif factory_enabled and not factory_cwd:
            # iter-13.92 — CRITICAL: the Droid's home directory frequently
            # contains stale source from OTHER LAMA projects (e.g. PMIS
            # left `legacy-code/src/com/ahct/*` lying around). If we don't
            # explicitly forbid shell access, the LLM will `cat`/`ls`/
            # `grep` and cite WHATEVER it finds — producing cross-project
            # file-path leakage even though the KB is fully scoped.
            # The user-reported symptom: TJHS SRS cited
            # `legacy-code/src/com/ahct/claims/action/ClaimsFlowAction.java:900`
            # even though TJHS has zero ahct files in its KB.
            droid_hint = (
                "FACTORY DROID FILESYSTEM — DO NOT READ:\n"
                "  • You are running INSIDE a Factory Droid Computer, but THIS PROJECT'S\n"
                "    source code is NOT present on the Droid's local filesystem.\n"
                "  • DO NOT use shell tools (`cat`, `ls`, `grep`, `find`, `sed`, `head`,\n"
                "    `tail`, `tree`, `wc`) to inspect the cwd or any other path. The Droid\n"
                "    may contain UNRELATED source from previous sessions — reading it will\n"
                "    produce CROSS-PROJECT CONTAMINATION in the SRS.\n"
                "  • The LEGACY FILE INDEX below AND the KNOWLEDGE BASE / TOON skeleton are\n"
                "    your SOLE authoritative sources. Cite ONLY filenames that appear in\n"
                "    the LEGACY FILE INDEX — any other file path is a hallucination.\n"
                "  • To opt in to direct filesystem reads, the operator must pin an explicit\n"
                "    Factory Droid `cwd` in Console → Factory Orchestrator → Working Directory.\n"
            )

        legacy_fs_block = (
            "LEGACY CODEBASE LOCATION (iter-13.35 — ground your analysis here):\n"
            f"  • Server-side scan path (LAMA host): `{legacy_path or '(not recorded — re-run Scan Folder to persist it)'}`\n"
            + (f"  • Factory Droid cwd: `{factory_cwd}`\n" if factory_cwd else "")
            + "\n"
            + (droid_hint + "\n" if droid_hint else "")
            + "LEGACY FILE INDEX (top files by size — cite these filenames VERBATIM):\n"
            + (index_md or "(no files indexed yet — run /api/kb/scan-folder or upload files first)")
        )

    # iter-13.69 — append the user-tagged source-material inventory so the
    # LLM knows there are auxiliary docs (existing SRS, business notes,
    # Figma exports, API specs …) beyond raw legacy code.
    try:
        from routes.kb import get_source_inventory_block
        inv_block = await get_source_inventory_block(project_id) if project_id else ""
        if inv_block:
            legacy_fs_block = (legacy_fs_block + "\n\n" + inv_block).strip()
    except Exception:  # noqa: BLE001 — non-fatal
        pass

    # iter-13.93 — inline the project's reconstructed source files as a
    # virtual workspace so the LLM never needs Droid filesystem access
    # to cite real code paths. This is the answer to the user's
    # "create same like structure in server space and do use it" ask:
    # we build the structure server-side from kb_chunks and ship it
    # inside the prompt itself, scoped per (tenant, project).
    #
    # iter-13.99 — when the inline workspace is present, it is the SOLE
    # legitimate source. REPLACE (not append to) the LEGACY CODEBASE
    # LOCATION + FACTORY DROID FILESYSTEM blocks instead of stacking
    # both, because mentioning `legacy_path` / `factory_cwd` at all was
    # giving the model a license to wander the Droid FS and cite
    # foreign paths (the `src/com/ahct/medicalAudit/*` leak the user
    # kept reporting). The workspace block now stands alone and the
    # post-LLM `_enforce_workspace_prefix` guard rejects anything not
    # rooted at this workspace.
    workspace_root_for_section = _workspace_root(
        (proj or {}).get("tenant_id", ""), project_id,
    )
    try:
        workspace_block = await _build_legacy_workspace_block(project_id, proj)
        if workspace_block:
            legacy_fs_block = workspace_block.strip()
            logger.info(
                "SRS[%s] · section=%s · iter-13.99 ISOLATED WORKSPACE active "
                "(root=%s) — LEGACY CODEBASE LOCATION + FACTORY hints suppressed.",
                project_id, cfg["key"], workspace_root_for_section,
            )
    except Exception as exc:  # noqa: BLE001 — never block SRS on workspace build
        logger.warning("SRS[%s] workspace build failed (non-fatal): %s",
                       project_id, exc)

    # Always-visible marker so it's obvious in `docker logs` whether the
    # block was injected for this section (iter-13.35).
    try:
        logger.info(
            "SRS[%s] · section=%s · legacy_fs_block: %s (legacy_path=%r factory_cwd=%r files_indexed=%d)",
            project_id, cfg["key"],
            "INJECTED" if legacy_fs_block else "EMPTY",
            legacy_path, factory_cwd, len(file_rows) if 'file_rows' in dir() else 0,
        )
    except Exception:  # noqa: BLE001
        pass

    # iter-13.79 — For §5 detailed_use_cases, inject a HARD UC inventory
    # block listing every UC ID the model is REQUIRED to emit in full.
    # Without this the model picks a subset and silently drops the rest.
    # `expected_uc_ids` was already computed in the budget section above.
    uc_inventory_block = ""
    if cfg["key"] == "detailed_use_cases" and expected_uc_ids:
        uc_inventory_block = (
            "══════════════════════════════════════════════════════════════════════\n"
            "MANDATORY USE-CASE INVENTORY (HARD RULE — non-negotiable)\n"
            "══════════════════════════════════════════════════════════════════════\n"
            f"The following {len(expected_uc_ids)} use cases were enumerated in §3 / the deep "
            "legacy analysis. You MUST emit EVERY one of them in §5 with the COMPLETE "
            "subsection set (Narrative, Metadata, Preconditions, Postconditions, Business "
            "Workflow, Business Rules, Main Flow, Alternate Flows, Exception Flows, Field "
            "Specification Table, End-to-End Journey (Screen → API → DB), Process Flow "
            "Diagram).\n\n"
            "Skipping any UC, or emitting it with fewer than the 6 MANDATORY subsections "
            "(Narrative + Preconditions + Postconditions + Business Workflow + Business "
            "Rules + End-to-End Journey Screen→API→DB), is a HARD VIOLATION that will be "
            "detected by the completeness gate and force a follow-up regeneration.\n\n"
            "REQUIRED UC IDS (emit a `### UC-XX: <Name>` header for each, in this order):\n"
            + "\n".join(f"  □ {uid}" for uid in expected_uc_ids)
            + "\n\n"
            "If you genuinely run out of token budget mid-way, STOP at a clean UC boundary "
            "and emit a final line `<!-- continuation-needed: UC-XX onwards -->` so the "
            "post-processor can drive a follow-up pass — do NOT emit half-finished UCs.\n"
        )

    # iter-14.25.2 — Workflow-ID coverage mandate.
    #
    # Symptom this addresses: revalidation flags §3 with "KB token not
    # covered: WF-01, WF-02" and scores 55 %. Root cause: the
    # confidence-coverage extractor pulls WF-* IDs from analysis_digest
    # as expected KB tokens, but §3's instructions never told the model
    # to cite them. Fix: parse workflow IDs from analysis_digest and
    # emit a MANDATORY WORKFLOW INVENTORY block for §3 and §5 that
    # requires each WF-NN to appear inline (matrix column / one-liner /
    # UC Business Workflow). No-op when there is no analysis_digest.
    workflow_inventory_block = ""
    if cfg["key"] in ("actors_use_case_inventory", "detailed_use_cases"):
        workflow_map = _extract_workflow_ids(analysis_digest, cap=40)
        if workflow_map:
            wf_lines = "\n".join(
                f"  □ {wf_id}" + (f" — {title}" if title else "")
                for wf_id, title in workflow_map.items()
            )
            if cfg["key"] == "actors_use_case_inventory":
                citation_rule = (
                    "  • Cite EVERY `WF-NN` verbatim in §3. Two acceptable placements:\n"
                    "      – as a trailing tag on the UC one-liner in §3.3 that maps\n"
                    "        to that workflow, e.g. `UC-04 — *The applicant submits …* (WF-01)`.\n"
                    "      – or as an extra column `Workflow` in the §3.2 Actor → UC\n"
                    "        matrix, one row per UC citing its WF-NN.\n"
                    "  • A UC that owns a workflow but drops the `WF-NN` tag is a\n"
                    "    HARD COVERAGE VIOLATION (the revalidation scorer flags it as\n"
                    "    `KB token not covered: WF-NN` and forces a regeneration).\n"
                )
            else:
                citation_rule = (
                    "  • Every UC's `#### Business Workflow` MUST cite the `WF-NN`\n"
                    "    it implements verbatim on the first step (e.g. `1. *Applicant*\n"
                    "    presses **Submit** — this executes workflow WF-01 …`).\n"
                    "  • Every UC's `#### Metadata` table MUST include a `Workflow ID`\n"
                    "    row citing `WF-NN` or `NOT_EVIDENCED`.\n"
                    "  • A UC that names no `WF-NN` when the inventory below lists\n"
                    "    workflows is a coverage violation and will be regenerated.\n"
                )
            workflow_inventory_block = (
                "══════════════════════════════════════════════════════════════════════\n"
                "MANDATORY WORKFLOW INVENTORY (HARD RULE — coverage-scored, non-negotiable)\n"
                "══════════════════════════════════════════════════════════════════════\n"
                f"The following {len(workflow_map)} workflow(s) were extracted from the "
                "DEEP LEGACY-LOGIC ANALYSIS digest and are graded by the confidence\n"
                "engine as REQUIRED KB tokens for this section:\n\n"
                f"{wf_lines}\n\n"
                "CITATION CONTRACT:\n"
                f"{citation_rule}"
                "  • Use the WF-NN IDs VERBATIM — do not renumber, do not rename, do\n"
                "    not paraphrase. They are the atoms the coverage scorer looks for.\n"
            )

    # iter-14.25.4 — DETERMINISTIC UC ROSTER (kills the "generic-CRUD collapse").
    #
    # Symptom this fixes: the CGHS pilot SRS PDF collapsed §5 into 4 stubs
    # (Submit / Approve / Reject / View "Application") even though the
    # analyzer had extracted 12 real workflows (Annual Health Checkup,
    # Chronic OP Registration, Cochlear Follow-Up, Drug Inventory, …).
    # Root cause: §3 / §5 were free to *invent* the UC set. Fix: hand the
    # model a fixed 1:1 map from WF-NN to UC-NN (title + primary actor).
    # The section prompts then anchor on this roster rather than
    # improvising UC titles from thin air.
    #
    # Applies to §3 (actor→UC matrix, one-liners) and §5 (detailed UCs).
    # No-op when the digest has no workflows — falls back to the legacy
    # UC-scan behaviour. Also feeds into ``expected_uc_ids`` so the §5
    # completeness gate audits against the deterministic roster.
    uc_roster_block = ""
    uc_roster: list[dict] = []
    if cfg["key"] in ("actors_use_case_inventory", "detailed_use_cases"):
        uc_roster = _build_uc_roster_from_workflows(analysis_digest, cap=60)
        if uc_roster:
            roster_lines = "\n".join(
                f"  □ {row['uc_id']}  ←→  {row['wf_id']}  |  "
                f"{row['name']}  |  actor: {row['actor']}"
                for row in uc_roster
            )
            if cfg["key"] == "actors_use_case_inventory":
                roster_rule = (
                    "  • §3.2 Actor → UC matrix MUST have EXACTLY one column per\n"
                    "    UC-NN below, in the given order. Add NO extra UCs. Drop\n"
                    "    NO listed UCs.\n"
                    "  • §3.3 one-liner MUST use the exact `UC-NN — <sentence>` \n"
                    "    format with the actor named verbatim from the roster and\n"
                    "    a `(WF-NN)` tag at the end of the sentence.\n"
                    "  • The UC *name* in §3.3 MUST be the workflow name (or a\n"
                    "    faithful paraphrase — NOT a generic 'Submit X' / 'Approve\n"
                    "    X' stub). Generic verbs alone are a HARD violation.\n"
                )
            else:
                roster_rule = (
                    "  • Emit EXACTLY these UC subsections, in this order — no\n"
                    "    more, no less. No 'Submit Application' / 'Approve\n"
                    "    Application' stubs. The UC header MUST be:\n"
                    "        `### UC-NN: <exact workflow name from roster>`\n"
                    "  • The Metadata `Primary Actor` MUST be the actor named on\n"
                    "    the roster row (never `User`, never `Admin` unless that\n"
                    "    IS the roster actor).\n"
                    "  • The Metadata table MUST include a `Workflow ID` row\n"
                    "    citing the WF-NN from the roster verbatim.\n"
                    "  • Each Business Workflow step MUST reference the workflow\n"
                    "    name (or a real screen/route/column from the grounding\n"
                    "    pack) — NOT generic 'System validates' fillers.\n"
                )
            uc_roster_block = (
                "══════════════════════════════════════════════════════════════════════\n"
                "MANDATORY USE-CASE ROSTER (HARD RULE — deterministic, non-negotiable)\n"
                "══════════════════════════════════════════════════════════════════════\n"
                f"The following {len(uc_roster)} UC(s) were derived 1:1 from the\n"
                "WORKFLOW INVENTORY above. This is the CANONICAL UC set for this\n"
                "SRS — do NOT invent additional UCs, do NOT collapse two workflows\n"
                "into one 'generic CRUD' UC, do NOT rename a workflow to a\n"
                "vague verb ('Submit X' / 'Approve X' / 'View X' are BANNED as\n"
                "UC names when a specific workflow name is on the roster below):\n\n"
                f"{roster_lines}\n\n"
                "ROSTER CONTRACT:\n"
                f"{roster_rule}"
            )
            # Feed the roster IDs into the §5 completeness gate so the audit
            # compares against the deterministic set, not just what §3 wrote.
            if cfg["key"] == "detailed_use_cases":
                roster_ids = [r["uc_id"] for r in uc_roster]
                merged: list[str] = list(roster_ids)
                for uid in expected_uc_ids:
                    if uid not in merged:
                        merged.append(uid)
                expected_uc_ids = merged[:60]

    # iter-14.25.4 — DETERMINISTIC GLOSSARY ROSTER for §1.3.
    #
    # Symptom this fixes: CGHS pilot §1.3 filled with ~100 hallucinated
    # acronyms (PYY, QQA, NHS, …) — pure LLM padding, nothing tied to
    # the codebase. Root cause: the §1.3 instructions asked for a
    # definition list without pinning the *set* of terms to something
    # source-derived. Fix: extract a whitelist of acronyms + domain
    # entity names from the analysis_digest and hand it to the model as
    # THE ONLY allowed vocabulary for §1.3. If the whitelist is empty,
    # the instruction switches to "write '_(no acronyms evidenced in
    # source)_' and move on" rather than let the model invent.
    glossary_roster_block = ""
    if cfg["key"] == "introduction":
        glossary_map = _build_glossary_roster(analysis_digest, cap=40)
        if glossary_map:
            gloss_lines = "\n".join(
                f"  □ {term}  (source: {src})"
                for term, src in glossary_map.items()
            )
            glossary_roster_block = (
                "══════════════════════════════════════════════════════════════════════\n"
                "MANDATORY GLOSSARY ROSTER for §1.3 (HARD RULE — whitelist, non-negotiable)\n"
                "══════════════════════════════════════════════════════════════════════\n"
                f"The following {len(glossary_map)} term(s) were extracted from the\n"
                "DEEP LEGACY-LOGIC ANALYSIS digest (actors, entities, workflows,\n"
                "business-rule scopes). §1.3 Definitions/Acronyms/Abbreviations\n"
                "MUST contain ONLY terms drawn from this whitelist:\n\n"
                f"{gloss_lines}\n\n"
                "WHITELIST CONTRACT:\n"
                "  • Any acronym or term NOT on this list is a HALLUCINATION and\n"
                "    will be rejected at revalidation. Do NOT invent acronyms\n"
                "    like 'PYY — Project Yearly Year' or 'QQA — Quality Quality\n"
                "    Assurance'.\n"
                "  • If a whitelisted term has no evidenced expansion in the\n"
                "    source, write its row with the explanation `NOT_EVIDENCED\n"
                "    in source — retained because it appears in <source hint>`.\n"
                "  • Drop terms from the whitelist that are actually opaque\n"
                "    codes with no domain meaning (e.g. a table-name prefix\n"
                "    only), and record the drop in a trailing note. Do NOT\n"
                "    silently replace them with invented terms.\n"
                "  • Tech / framework acronyms (JSP, JPA, SQL, HTTP, JSON, MVC,\n"
                "    …) are ALREADY filtered from the whitelist — do NOT add\n"
                "    them back. §1.3 is a *domain* glossary, not a stack list.\n"
            )
        else:
            glossary_roster_block = (
                "══════════════════════════════════════════════════════════════════════\n"
                "MANDATORY GLOSSARY EMPTY-STATE for §1.3 (HARD RULE)\n"
                "══════════════════════════════════════════════════════════════════════\n"
                "The DEEP LEGACY-LOGIC ANALYSIS digest carries NO evidenced\n"
                "domain acronyms or entities for this project. §1.3 MUST be\n"
                "written as a single line:\n"
                "    _(no domain acronyms evidenced in source — see §2 for\n"
                "     detected framework/runtime terms)_\n"
                "Do NOT invent acronyms to fill the section. Do NOT copy a\n"
                "generic healthcare/procurement glossary from training data.\n"
            )

    # iter-14.25.1 — Per-UC JOURNEY GROUNDING PACKS for §5.
    #
    # Root cause of the "eagle-view use cases" complaint: the generic
    # journey block appended to `kb_block` earlier uses SECTION_QUERIES
    # seeds ("workflow / approval / submission / ...") that match every
    # route weakly, so no UC gets a concrete pin. Fix: for each expected
    # UC ID, read its §3 one-liner and pick the top-1 api + top-1 ui
    # journey by scoring against that one-liner. Emit a labelled pack the
    # LLM MUST anchor Narrative + Field Spec Table + Business Workflow +
    # Business Rules on. Silent no-op when the journey toggle is off or
    # when there are no journeys / no UCs.
    uc_grounding_block = ""
    if cfg["key"] == "detailed_use_cases":
        try:
            from kb.journey_materializer import (
                parse_uc_oneliners, journey_grounding_for_use_cases,
            )
            uc3_text = (prior_sections or {}).get("actors_use_case_inventory") or ""
            uc_lines = parse_uc_oneliners(uc3_text, cap=min(60, max(1, len(expected_uc_ids) or 60)))
            # Keep only UCs the completeness gate expects, in declared order.
            if expected_uc_ids:
                ordered: dict[str, str] = {}
                for uid in expected_uc_ids:
                    if uid in uc_lines:
                        ordered[uid] = uc_lines[uid]
                # Include UCs missing from §3 with a stub so the pack still
                # forces the model to reason about them.
                for uid in expected_uc_ids:
                    ordered.setdefault(uid, f"({uid} listed in inventory but no §3 one-liner text found)")
                uc_lines = ordered
            uc_grounding_block = await journey_grounding_for_use_cases(
                project_id, uc_lines,
                stage="srs",
                max_chars=12000 if is_heavy else 8000,
                max_ucs=40,
            )
        except Exception:  # noqa: BLE001 — best-effort
            uc_grounding_block = ""

    # iter-14.10 — REMEDIATION BLOCK for the auto-improve loop.
    # When the confidence engine tags this section below the threshold,
    # its `rationale` + `gaps[]` are passed here so the LLM knows the
    # SPECIFIC weaknesses the scorer just flagged. Injected before the
    # main system prompt so it wins any tone/style conflict. Empty
    # string on the first pass — no behavioural change from prior runs.
    remediation_block = ""
    if remediation_hints:
        hints_text = "\n".join(
            f"  ✗ {h.strip()}" for h in remediation_hints if h and h.strip()
        )
        if hints_text:
            remediation_block = (
                "══════════════════════════════════════════════════════════════════════\n"
                "SCORER FEEDBACK — YOU MUST ADDRESS THESE GAPS THIS RUN\n"
                "══════════════════════════════════════════════════════════════════════\n"
                "The previous version of this section was reviewed by a multi-model\n"
                "confidence evaluator and scored BELOW the ≥95% threshold. The evaluator\n"
                "flagged the following gaps — every single one must be resolved in your\n"
                "next output. Do NOT re-emit the prior content unchanged; do NOT hedge\n"
                "('may', 'might', 'appears to') — either cite verbatim source evidence,\n"
                "or emit an explicit `⚠ EVIDENCE GAP` marker.\n\n"
                f"{hints_text}\n\n"
                "Concrete anchors that keep scores high in this engine:\n"
                "  • Cite every referenced KB entity VERBATIM (class / method /\n"
                "    table / column / route / role name — copy-paste, don't paraphrase).\n"
                "  • For every Business Rule row, include the source file + line\n"
                "    (e.g. `Source: src/.../PayGradeService.java:143`).\n"
                "  • For every Actor row, cite the KB ROLE record + evidence path.\n"
                "  • For NFR / user-manual rows, avoid hedging language entirely.\n"
                "  • If a KB record genuinely does not exist, DO NOT invent one —\n"
                "    emit `⚠ EVIDENCE GAP: <what is missing>` inline instead.\n"
                "══════════════════════════════════════════════════════════════════════\n\n"
            )

    system_prompt = f"""{remediation_block}{starvation_banner}{compliance_preamble}You are a **Senior Software Requirements Analyst + Domain Compliance Specialist** writing a professional IEEE 830 / IEEE 29148 Software Requirements Specification for a legacy-application modernization.

══════════════════════════════════════════════════════════════════════
OUTPUT DISCIPLINE  (HARD RULE — iter-14.25.5)
══════════════════════════════════════════════════════════════════════
The paragraphs below tell you WHAT to write and WHY. They are
INSTRUCTIONS, not content. Your output MUST NOT:

  • Copy the instruction preamble sentences ("For EVERY role found
    in the KB…", "A single matrix table — rows = actors…",
    "produce a row in this table:", "Definition list (term:
    explanation)…", "EXHAUSTIVE per-module FR table…", etc.) into
    your section body. Those sentences are directives to you, the
    author. If you find yourself wanting to echo one, you have not
    yet started writing the section — pivot to real content.

  • Emit angle-bracket placeholder tokens (`<relative/path>`,
    `<role>`, `<screen name>`, `<Use Case Name>`, `<table>`,
    `<list or "none">`). Replace each with real evidence from the
    KB / analysis digest, or with `NOT_EVIDENCED` when nothing is
    available. Never leave the raw placeholder.

  • Emit the internal sanitizer marker `[redacted-foreign-path]`.
    That token means "a citation was scrubbed by workspace
    isolation". You must not fabricate paths in the first place.
    If you need to cite a source but only have a workspace-scoped
    path, cite it verbatim (rooted at the workspace root). If you
    have no citable path, write `NOT_EVIDENCED` in the Source
    Evidence cell.

  • Emit ellipsis-only cells (`| … |` or `| ... |`) in tables.
    These are template scaffolding — every cell must contain real
    content OR the exact string `NOT_EVIDENCED`.

  • Emit generic-CRUD placeholder verbs ("Submit Application",
    "Approve Application", "Reject Application", "View Application",
    "User Login", "Admin Login") as UC names or FR names when a
    specific WORKFLOW INVENTORY or USE-CASE ROSTER has been given
    to you above. Those rosters are the CANONICAL set — use their
    names verbatim.

An SRS section that violates any of the four rules above is a
HARD REJECT at revalidation and will trigger an automatic
regeneration.

══════════════════════════════════════════════════════════════════════
STORYTELLING-FIRST CONTRACT  (HARD RULE — overrides every later instruction
that pulls toward dry/technical phrasing)
══════════════════════════════════════════════════════════════════════
This SRS is read by **business owners, domain SMEs, programme managers
and auditors** — NOT by the developers who wrote the legacy code. Every
section MUST therefore be written in a **human, story-driven voice**:

  • Open EVERY section (and every sub-section) with 2–4 sentences of
    scene-setting prose. Picture the organisation, the people in their
    roles, the moment of the day when this part of the system matters,
    and the outcome they want. Then drill into the structured details.
  • Speak as a domain narrator, not as a code reviewer. Say "the field
    officer reviews her queue and forwards approvals to the District
    Magistrate" — NOT "the `ReviewController.forward()` method calls
    `WorkflowService.advance(...)`".
  • Active voice, present tense, simple sentences. Avoid passive voice
    ("is invoked", "shall be persisted") — prefer active equivalents
    ("the system records", "the reviewer sees").
  • Name people in business roles ("the District Magistrate", "the
    applicant Anita", "the finance clerk"), never class / interface /
    service / method / controller names inside narrative prose.
  • Use real screen labels, button text, menu items, role names —
    quoted verbatim from the KB. They anchor the story in the user's
    actual world.
  • Tables, regex, formulas, IDs, schema columns and source citations
    are EVIDENCE — they come AFTER the prose, never instead of it. A
    section that is only tables + bullet lists fails this contract and
    will be flagged for rewrite.
  • Length floor of prose per section: at LEAST 2 paragraphs of
    narrative before any structured artefact. The narrative is what
    makes the SRS validate-able by a non-technical reviewer.

Wrong (technical, lifeless):
  "Section 2.2 — The system provides the following functions:
   • applyPermit()  • approvePermit()  • generateReport()"

Right (storytelling):
  "Section 2.2 — Every working morning, the district office opens with
   a queue of overnight permit applications submitted by citizens.
   The system carries each application from the moment Anita the
   applicant submits it on the public portal, through verification by
   the field officer, sign-off by the District Magistrate, and finally
   into the monthly report the Programme Director reviews in the
   board meeting. Four capabilities make this end-to-end journey
   possible — described below.
   …"

Whenever a later instruction asks for "a table" or "a bullet list",
keep that table / list — but **introduce it with narrative first**.
Whenever a later instruction asks for "EXHAUSTIVE per-module …", do
that exhaustive enumeration — but **frame each module with a one-
paragraph human story** before the enumeration.

═══════════���══════════════════════════════════════════════════════════
SRS AUTHORING CHARTER (HARD RULES — these override any later guidance)
══════════════════════════════════════════════════════════════════════
truth_rule_mode: SOURCE_CODE_AND_EXISTING_SRS_ONLY

source_of_truth (in this priority order):
  1. The legacy source code visible in KNOWLEDGE BASE + LEGACY FILE INDEX
     (and readable directly from disk if a Factory Droid cwd is provided).
  2. Database artefacts (tables, procedures, functions, triggers).
  3. The DEEP LEGACY-LOGIC ANALYSIS digest (if present below).
  4. The prior SRS sections written earlier in this run.
  Anything NOT covered by 1–4 is OUT OF SCOPE.

logic_rules (apply silently while writing every section):
  • If a requirement is not present in source code or the existing SRS
    → mark the line as `OUT_OF_SCOPE` (do NOT invent behaviour).
  • If documentation and implementation conflict
    → IMPLEMENTATION WINS (the code is the authority).
  • If a DB column, API contract, UI selector, or field detail is not
    explicit in source → mark the line as `NOT_EVIDENCED` (do NOT guess).
  • If a behaviour is implied but not enforced anywhere
    → still mark `NOT_EVIDENCED` and continue.

tech_stack_neutrality (CRITICAL):
  • Write every requirement, business rule, narrative and acceptance
    criterion in **plain business language**. A domain expert who has
    never opened the codebase must be able to read and validate it.
  • Framework / language / runtime names belong ONLY in:
      - the DETECTED LEGACY STACK header,
      - the source-citation column / parenthesis at the end of a line.
    They do NOT belong inside requirement prose.
  • Wrong:  "FR-001: The `BillService.validate()` method shall throw
            `BudgetExceededException` …"
  • Right:  "FR-001: The system shall reject any bill whose total
            exceeds the sanctioned budget.  Source: `<relative/path>::<symbol>`"

human_readability_contract:
  • Functional Requirements (FR-*) — "The system shall …" form, one
    behaviour per line, business language.
  • Business Rules (BR-*) — "shall / shall not / must" form, one rule
    per line, with the source object that enforces it.
  • Use Cases (UC-*) — open with a NARRATIVE (User Story) paragraph in
    storytelling voice (people in roles, real screen labels, no class
    names). Structured fields come AFTER the narrative.
  • Every UC MUST include Preconditions, Postconditions, Business
    Workflow and Business Rules — these four are NON-NEGOTIABLE.

constraints (from srs.yml):
  • No inferred functionality.
  • No optimization or redesign suggestions.
  • Terminology platform-agnostic unless the source itself names a
    platform (then quote it verbatim).
  • Language formal, implementation-ready, and audit-friendly.

PROJECT: {proj.get('name', '')}
SOURCE STACK (declared seed value — HINT ONLY): {proj.get('source_tech', '')}
SOURCE STACK (detected from uploaded code — AUTHORITATIVE, use VERBATIM):
  summary:    {detected_summary or '(no detection — fall back to declared)'}
  frameworks: {detected_frameworks}
  database:   {detected_db}
TARGET STACK: {proj.get('target_tech', '')}
KB SUMMARY: {summary}
CONVERSATION CONTEXT:
{convo_text}

{legacy_fs_block}

KNOWLEDGE BASE:
{kb_block}

{analysis_block}

{prior_block}

{uc_inventory_block}
{uc_roster_block}
{workflow_inventory_block}
{glossary_roster_block}
{uc_grounding_block}
Write ONLY the "{cfg['label']}" section of the SRS.

{cfg['instructions']}

WRITING GUIDANCE (this is what separates a great SRS from a checklist):
- Open every sub-section with one or two SCENE-SETTING sentences that name the
  actual modules / actors / data flow you saw in the KB — then drill into the
  specifics. Do NOT lead with a bullet list.
- When you cite a class, table, route, role, or column, use its real name
  from the KB above (verbatim — case-sensitive). Pair it with the file or
  module it lives in, in parentheses, on first mention. KEEP THIS CITATION
  separate from the requirement prose itself (see tech_stack_neutrality
  in the CHARTER above).
- Where a sub-section asks for a TABLE, render a proper markdown pipe-table.
  Where it asks for narrative, write flowing prose, not bullet skeletons.
- Treat each sub-section as a self-contained mini-essay: introduce the
  concern → walk the evidence → state the requirement / acceptance.
- If the KB is genuinely thin on some sub-topic, mark the gap explicitly
  with `> ⚠ EVIDENCE GAP: <one line>` rather than inventing — but ALWAYS
  produce the surrounding sub-section in full.

LENGTH TARGET:
- Minimum {min_w} words. Aim for {target_w} words of *substantive* content
  (not padding, not repetition). If you find yourself running short, you
  have under-cited the KB — go back and add concrete names + flows.

FORMAT:
- Markdown only. Use `## X.Y` for the sub-sections promised by the
  instructions above; `###` for further depth.
- No JSON, no XML, no preamble, no ```markdown``` fences.
- Tables: standard pipe syntax with a header separator row.

HARD CONSTRAINTS:
- Use actual class / table / method / route names from the KB. No
  generic placeholders like "[Module Name]" or "<TBD>".
- Do NOT refuse, do NOT ask follow-up questions, do NOT output an empty
  response. If the KB seems thin, anchor on the SOURCE STACK and the
  classes/tables that ARE visible, and call out gaps with `OUT_OF_SCOPE`
  or `NOT_EVIDENCED` per the CHARTER logic rules.
- LEGACY STACK FIDELITY (HARD CONTRACT): every reference to the source
  platform in a CITATION must use the DETECTED frameworks + database
  VERBATIM. Phrases like "SQL/Java stack", "the legacy Java application",
  "the existing system" that drop the framework name in CITATIONS are
  VIOLATIONS — name `{detected_frameworks}` and `{detected_db}`
  explicitly when describing the source platform. (Requirement prose
  remains tech-agnostic per the CHARTER.)
- LEGACY-SOURCE GROUNDING (HARD CONTRACT): the LEGACY FILE INDEX above
  is the COMPLETE and AUTHORITATIVE list of source-file paths for THIS
  project. Cite ONLY filenames that appear in that index — paths invented
  from your training data, or discovered by `ls`/`find`/`tree`/glob
  against the Droid Computer's filesystem, are CROSS-PROJECT
  CONTAMINATION and a HARD violation. Format citations as
  `path/to/file.ext:LINE` or `path/to/file.ext::function_name`. Even when
  the LEGACY CODEBASE LOCATION block above lists a Factory Droid `cwd`,
  shell reads (`cat`/`grep -n`/`sed -n`) are PERMITTED ONLY against
  paths that appear VERBATIM in the LEGACY FILE INDEX — the Droid
  filesystem is shared across LAMA sessions and ANY off-index path
  (e.g. `legacy-code/src/com/ahct/*` leaked from prior pilots) MUST NOT
  be opened or cited. If no Factory cwd is listed at all, DO NOT invoke
  any shell tool against the local filesystem (see iter-13.92 / 13.94
  forensic notes).
"""

    llm_msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Produce the **{cfg['label']}** section now. "
            f"Aim for ~{target_w} words of substantive, evidence-grounded prose. "
            "Be specific, name real artifacts, walk the evidence, and write as if a "
            "developer with zero prior knowledge must implement from this section alone."
        )},
    ]

    # Temperature — first pass slightly creative (Claude / GPT produce
    # noticeably more fluid prose at 0.45 vs 0.2); later passes nudge
    # higher and shorten the ceiling to dodge mid-stream truncations.
    #
    # iter-13.13: dialled back from the 4-attempt × 300s schedule that
    # iter-13.12 introduced. That schedule pushed total run time past
    # most proxy / ingress connection caps and triggered cascading
    # "network error" toasts on the client. Three attempts on the
    # primary model with short backoff strike a better balance — the
    # post-generation repair pass still re-tries failed sections so we
    # don't lose the resilience win.
    #
    # Each attempt: (model_used, temperature, max_tokens_factor, timeout_sec)
    # iter-13.37 — `fast_mode` (single-section regen path) uses a tighter
    # schedule so the user gets a response well inside the typical 60–120 s
    # proxy idle window without sacrificing the retry safety net. The full
    # SRS run still uses the longer schedule because it has SSE heartbeats
    # to keep the connection alive.
    #
    # iter-14.8 — widened from (240/200/150 · 110/90) after the iter-14.7
    # timeout-floor fix unmasked the fact that Claude sonnet 4-6 with a
    # real 80K-token SRS prompt legitimately needs 4-6 minutes per call.
    # The old schedule was calibrated against the broken 300s floor and
    # was silently hidden by it. Under fast_mode we still stay under the
    # 300s reverse-proxy idle cap; the full-run schedule pushes to 480s
    # for the first pass because the SSE relay keeps the connection
    # alive with its own 5s heartbeats regardless.
    if fast_mode:
        attempt_plan = [
            (model, 0.45, 0.85, 260.0),
            (model, 0.30, 0.70, 180.0),
        ]
    else:
        attempt_plan = [
            (model, 0.45, 1.00, 480.0),
            (model, 0.60, 0.85, 360.0),
            (model, 0.30, 0.70, 240.0),  # final pass = lower temp, smaller ask, shorter wait
        ]
    last_err: str = ""
    for attempt_idx, (try_model, temperature, tok_factor, per_call_timeout) in enumerate(attempt_plan):
        # Short backoff between attempts (1.5s, 3s) — enough to clear a
        # transient rate-limit blip without bloating total run time.
        if attempt_idx > 0:
            try:
                await asyncio.sleep(1.5 * attempt_idx)
            except Exception:  # noqa: BLE001
                pass
        try:
            result = await chat_completion(
                messages=llm_msgs,
                model=try_model,
                temperature=temperature,
                max_tokens=int(max_tokens * tok_factor),
                timeout=per_call_timeout,
                agent_key=agent_key,       # iter-13.76 — tier-aware routing (Sonnet vs Opus)
                project_id=project_id,
            )
            content = (result["content"] or "").strip()
            # Some models wrap the response in ```markdown … ``` despite the rules.
            if content.startswith("```"):
                content = content[3:]
                if content[:8].lower().startswith("markdown"):
                    content = content[8:]
                content = content.lstrip("\n")
                if content.endswith("```"):
                    content = content[:-3].rstrip()
            # Accept ONLY if the response is non-trivial. A handful of
            # whitespace or a single-line refusal shouldn't pass —
            # otherwise the SRS marks the section "complete" while
            # being effectively empty.
            if content.strip() and len(content.strip()) >= 120:
                # ── iter-13.79 — UC-completeness gate (§5 only) ─────────────
                # Audits the generated §5 body against the expected UC
                # inventory + the 5 mandatory subsections per UC. If any
                # UCs are missing entirely or have incomplete subsections,
                # fires ONE focused continuation call that explicitly asks
                # the model to APPEND the missing UCs and BACKFILL the
                # absent subsections — without rewriting what's there.
                uc_extra_tokens = 0
                if (cfg["key"] == "detailed_use_cases"
                        and expected_uc_ids
                        and not fast_mode):
                    try:
                        audit = _audit_uc_completeness(content, expected_uc_ids)
                        missing_ids = audit["missing"]
                        incomplete = audit["incomplete"]
                        if missing_ids or incomplete:
                            logger.info(
                                "SRS[%s] §5 UC-gate: %d missing, %d incomplete "
                                "(expected=%d, present=%d)",
                                project_id, len(missing_ids), len(incomplete),
                                len(expected_uc_ids), len(audit["present"]),
                            )
                            missing_block = ""
                            if missing_ids:
                                missing_block = (
                                    "USE CASES MISSING FROM YOUR RESPONSE (write each "
                                    "one IN FULL using the §5 UC template — Narrative, "
                                    "Metadata, Preconditions, Postconditions, Business "
                                    "Workflow, Business Rules, Main Flow, Alternate "
                                    "Flows, Exception Flows, Field Specification Table, "
                                    "Process Flow Diagram):\n"
                                    + "\n".join(f"  • {uid}" for uid in missing_ids[:40])
                                    + "\n\n"
                                )
                            incomplete_block = ""
                            if incomplete:
                                incomplete_lines = [
                                    f"  • {uid} — missing: {', '.join(absent_subs)}"
                                    for uid, absent_subs in list(incomplete.items())[:30]
                                ]
                                incomplete_block = (
                                    "USE CASES PRESENT BUT INCOMPLETE (append the listed "
                                    "subsections beneath the existing UC header — do NOT "
                                    "duplicate the header, do NOT rewrite content that "
                                    "is already there):\n"
                                    + "\n".join(incomplete_lines)
                                    + "\n\n"
                                )
                            patch_msgs = [
                                {"role": "system", "content": system_prompt},
                                {"role": "assistant", "content": content},
                                {"role": "user", "content": (
                                    "Use-case completeness audit: your previous response "
                                    "is missing required content. APPEND the items below "
                                    "to your previous output (do NOT rewrite what is "
                                    "already there, do NOT re-emit headers that already "
                                    "exist). Use markdown only. Storytelling Narrative "
                                    "MUST come before the structured tables for any "
                                    "newly-added UC.\n\n"
                                    + missing_block
                                    + incomplete_block
                                    + "Output ONLY the new UC blocks / backfilled "
                                    "subsections. Do NOT include any commentary or summary."
                                )},
                            ]
                            try:
                                shortfall = len(missing_ids) * 800 + len(incomplete) * 400
                                patch_tokens = min(16000, max(4000, shortfall))
                                patch = await chat_completion(
                                    messages=patch_msgs,
                                    model=try_model,
                                    temperature=0.30,
                                    max_tokens=patch_tokens,
                                    timeout=240.0,
                                    agent_key=agent_key,
                                    project_id=project_id,
                                )
                                patch_text = (patch.get("content") or "").strip()
                                if patch_text.startswith("```"):
                                    patch_text = patch_text[3:].lstrip("\n")
                                    if patch_text.endswith("```"):
                                        patch_text = patch_text[:-3].rstrip()
                                if patch_text and len(patch_text) >= 200:
                                    content = (
                                        content.rstrip()
                                        + "\n\n<!-- uc-completeness continuation -->\n\n"
                                        + patch_text
                                    )
                                    uc_extra_tokens = (
                                        patch.get("usage", {}).get("total_tokens", 0)
                                    )
                            except Exception as _ucp:  # noqa: BLE001
                                logger.info(
                                    "UC-completeness continuation skipped for %s: %s",
                                    cfg["key"], str(_ucp)[:160],
                                )
                    except Exception:  # noqa: BLE001 — never let the gate break gen
                        pass

                # ── iter-13.18 — Coverage gate ───────────────────────────────
                # Fire ONE focused continuation pass if the section under-cites
                # the entities in its TOON slice. Bounded retry: at most one
                # extra call per section so total token spend stays predictable.
                # iter-13.37 — Skipped in `fast_mode` (single-section regen)
                # because the extra continuation call adds 30–180 s and is
                # what pushes the request past the 60 s ingress timeout.
                try:
                    if fast_mode:
                        extra_tokens = 0
                    else:
                        expected = _extract_toon_entities(toon_slice, cap=60)
                        if expected:
                            ratio, missing = _compute_coverage(content, expected)
                            threshold = 0.55 if is_heavy else 0.40
                            if ratio < threshold and missing:
                                missing_block = ", ".join(missing[:30])
                                patch_msgs = [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "assistant", "content": content},
                                    {"role": "user", "content": (
                                        f"Coverage check: this section currently references only "
                                        f"{int(ratio*100)}% of the entities visible in the KB. "
                                        "Extend the section so the following names from the KB are "
                                        "each described with their role in the system. Do NOT rewrite "
                                        "what is already there — APPEND new sub-sections / table rows / "
                                        "use-case entries that cover them. Use markdown only.\n\n"
                                        f"MISSING ENTITIES: {missing_block}"
                                    )},
                                ]
                                try:
                                    patch = await chat_completion(
                                        messages=patch_msgs,
                                        model=try_model,
                                        temperature=0.35,
                                        max_tokens=min(int(max_tokens * 0.6), 8000),
                                        timeout=180.0,
                                    )
                                    patch_text = (patch.get("content") or "").strip()
                                    if patch_text.startswith("```"):
                                        patch_text = patch_text[3:].lstrip("\n")
                                        if patch_text.endswith("```"):
                                            patch_text = patch_text[:-3].rstrip()
                                    if patch_text and len(patch_text) >= 120:
                                        content = (
                                            content.rstrip()
                                            + "\n\n<!-- coverage-gate continuation -->\n\n"
                                            + patch_text
                                        )
                                        extra_tokens = patch.get("usage", {}).get("total_tokens", 0)
                                    else:
                                        extra_tokens = 0
                                except Exception as _pe:  # noqa: BLE001
                                    logger.info(
                                        "Coverage-gate continuation skipped for %s: %s",
                                        cfg["key"], str(_pe)[:160],
                                    )
                                    extra_tokens = 0
                            else:
                                extra_tokens = 0
                        else:
                            extra_tokens = 0
                except Exception:  # noqa: BLE001 — never let the gate break generation
                    extra_tokens = 0

                # iter-13.98 / iter-13.99 — OUTPUT SANITIZER + HARD GUARD.
                # iter-13.98 had a path-suffix sanitizer that redacted
                # paths whose ≥2-component suffix wasn't in kb_files. That
                # still allowed any path whose suffix happened to collide
                # with a TJHS file (the recurring `src/com/ahct/*` leak).
                # iter-13.99 adds a HARD GUARD: when the project has a
                # materialised isolated workspace, EVERY multi-component
                # path MUST be rooted at `/lama-workspaces/{tid}__{pid}/`.
                # Anything else — regardless of basename collision — is
                # redacted. This is the structural guarantee the user
                # asked for.
                #
                # ALWAYS log so the operator can verify the new code is
                # actually running (the leak persisted previously partly
                # because old containers were still in use).
                try:
                    workspace_root_for_guard = _workspace_root(
                        (proj or {}).get("tenant_id", ""), project_id or "",
                    )
                    # 1) Hard workspace-prefix guard FIRST. This rejects
                    #    foreign multi-component paths regardless of
                    #    basename collisions.
                    content, n_hard = _enforce_workspace_prefix(
                        content, workspace_root_for_guard, allowed_bn,
                    )
                    # 2) Fall-through path-suffix sanitizer for anything
                    #    the hard guard didn't have an opinion on (bare
                    #    basenames in prose still get the basename check).
                    n_soft = 0
                    if allowed_bn or allowed_suf:
                        content, n_soft = _redact_foreign_paths(
                            content, allowed_bn,
                            allowed_path_suffixes=allowed_suf,
                        )
                    logger.warning(
                        "SRS[%s] · section=%s · iter-13.99 output guard: "
                        "hard_workspace_redactions=%d soft_suffix_redactions=%d "
                        "workspace_root=%s",
                        project_id, cfg["key"], n_hard, n_soft,
                        workspace_root_for_guard,
                    )
                except Exception as _sx:  # noqa: BLE001
                    logger.warning(
                        "SRS[%s] · section=%s · output sanitizer crashed: %s",
                        project_id, cfg["key"], _sx,
                    )

                # iter-14.25.5 — POST-LLM output-quality scrub. Runs AFTER
                # the workspace-prefix / suffix redactors so we can (a)
                # rewrite their intermediate `[redacted-foreign-path]`
                # marker to a business-language equivalent and (b) strip
                # any prompt-instruction sentences the model echoed as
                # content (a classic "thin evidence" failure mode we saw
                # on the CGHS Aug-21 SRS run).
                try:
                    content, scrub_stats = _scrub_section_output(content)
                    if any(scrub_stats.values()):
                        logger.warning(
                            "SRS[%s] · section=%s · iter-14.25.5 output scrub: "
                            "placeholder_rewrites=%d instruction_echoes=%d "
                            "empty_ellipsis_cells=%d",
                            project_id, cfg["key"],
                            scrub_stats["placeholder_rewrites"],
                            scrub_stats["instruction_echoes_removed"],
                            scrub_stats["empty_ellipsis_cells"],
                        )
                except Exception as _scrub_exc:  # noqa: BLE001
                    logger.warning(
                        "SRS[%s] · section=%s · output scrub crashed: %s",
                        project_id, cfg["key"], _scrub_exc,
                    )

                return {
                    "content": content,
                    "tokens": (
                        result["usage"].get("total_tokens", 0)
                        + extra_tokens
                        + uc_extra_tokens   # iter-13.79 — UC-completeness continuation
                    ),
                    # iter-13.34 — `result["model"]` is the model that the
                    # fabric layer actually invoked. After a failover (e.g.
                    # Anthropic 402 → OpenRouter/qwen) this differs from
                    # `try_model` (= what the user requested). The streaming
                    # endpoint forwards both up so the UI can show a
                    # "ran on X instead of Y" badge — making the silent
                    # failover visible to the user.
                    "model_used": result.get("model") or try_model,
                    "model_requested": try_model,
                    "attempt": attempt_idx + 1,
                }
            # Too-short / empty output — retry with a more directive nudge.
            last_err = (
                f"LLM ({try_model}) returned only {len(content.strip())} chars "
                "(under the 120-char minimum acceptance threshold)"
            )
            llm_msgs = [
                llm_msgs[0],
                {"role": "user", "content": (
                    f"Your previous response was too short ({len(content.strip())} chars). "
                    f"Write the FULL {cfg['label']} section now in flowing prose — "
                    f"minimum {min_w} words, aim for higher. Use real class / "
                    "table / route names from the KNOWLEDGE BASE block above. "
                    "Open with a scene-setting paragraph; then walk the evidence; "
                    "do NOT refuse and do NOT respond with bullet skeletons only."
                )},
            ]
        except Exception as e:
            last_err = str(e)[:240]
            logger.warning(
                "SRS section %s attempt %d/%d (model=%s) failed: %s",
                cfg["key"], attempt_idx + 1, len(attempt_plan), try_model, last_err,
            )
            continue
    # iter 13.8.2 — convert raw provider error JSON into an actionable
    # message the user can read. Specifically detect the OpenRouter 402
    # "Insufficient credits" path which used to dump the whole error
    # blob into the section body.
    err_low = (last_err or "").lower()
    if ("402" in err_low or "insufficient credits" in err_low
            or "insufficient_quota" in err_low or "credit balance" in err_low
            or "billing" in err_low or "payment required" in err_low):
        # iter-13.98 — Factory-aware credit-error message. The user
        # frequently has Factory.ai configured but the project's per-
        # project `factory_orchestrator.enabled` flag is OFF — so the
        # call falls through to fabric / env OpenRouter and they see
        # a credit error from a provider they don't think they're
        # using. Probe the Factory config for THIS project and surface
        # the answer in the friendly message so the diagnosis is
        # one-glance instead of one-debugging-session.
        factory_hint = ""
        try:
            from factory_orchestrator import get_project_factory_orchestrator_config
            _fcfg = await get_project_factory_orchestrator_config(
                project_id or "", include_secret=False,
            )
            _f_enabled = bool(_fcfg.get("enabled"))
            _f_app_key = bool((_fcfg.get("app_key") or "").strip())
            _f_computer = bool((_fcfg.get("computer_id") or "").strip())
            if _f_enabled and _f_app_key and _f_computer:
                # Factory is fully configured — the credit error means
                # Factory itself failed (and strict mode let it fall
                # through, or its error was transient 5xx). Surface
                # the original Factory failure.
                factory_hint = (
                    "\n\n**⚠️ Factory.ai is enabled for this project** "
                    "but the request still hit a billing-provider error. "
                    "This usually means Factory.ai returned 5xx and the "
                    "strict-mode fallback chose env-OpenRouter (which "
                    "has no credits). Check `docker logs lama | grep "
                    "factory` for the Factory error, OR set "
                    "`LAMA_FACTORY_STRICT=1` (the default) to surface "
                    "Factory failures instead of silently falling back."
                )
            elif _f_enabled and (not _f_app_key or not _f_computer):
                factory_hint = (
                    "\n\n**⚠️ Factory.ai is enabled for this project but "
                    "incompletely configured** — missing "
                    + ("`app_key`" if not _f_app_key else "")
                    + ("` & `" if (not _f_app_key and not _f_computer) else "")
                    + ("`computer_id`" if not _f_computer else "")
                    + ". Open **Console → Factory Orchestrator** for this "
                    "project and complete the configuration."
                )
            else:
                # Factory is NOT enabled for this project. The user
                # probably enabled it on a DIFFERENT project.
                factory_hint = (
                    "\n\n**ℹ️ Factory.ai is NOT enabled for this project.** "
                    "If you intended to use Factory.ai for THIS project, "
                    "open **Console → Factory Orchestrator**, select this "
                    "project from the dropdown, and toggle Factory ON. "
                    "Otherwise the LLM call goes to your configured "
                    "Console provider (OpenRouter / Anthropic / OpenAI) "
                    "— which needs funded credits."
                )
        except Exception:  # noqa: BLE001 — diagnostic only, never blocks
            pass

        friendly = (
            "> ⚠️ **Section skipped — LLM provider out of credits.**\n\n"
            "Every configured LLM provider refused this request because the\n"
            "API key has run out of credits / quota.\n"
            + factory_hint
            + "\n\n**To recover:**\n"
            "1. Open Console → *Models* and confirm your default provider's key is funded.\n"
            "   - OpenRouter: top up at <https://openrouter.ai/settings/credits>\n"
            "   - Anthropic native: <https://console.anthropic.com/settings/billing>\n"
            "   - OpenAI: <https://platform.openai.com/account/billing>\n"
            "2. (Optional) Add a second provider in Console → *Models* — LAMA\n"
            "   will automatically fail over between them on credit errors.\n"
            "3. Re-run **Generate SRS** for this project; previously-generated\n"
            "   sections are kept, only the failed ones are regenerated.\n\n"
            f"_Provider response: `{last_err[:300]}`_"
        )
        return {"content": friendly, "tokens": 0, "error": "credits"}
    if "401" in err_low or "unauthorized" in err_low or "invalid api key" in err_low:
        friendly = (
            "> ⚠️ **Section skipped — LLM provider rejected the API key.**\n\n"
            "Open **Console → Models** and re-enter / re-validate the default\n"
            "provider's API key, then re-run *Generate SRS*.\n\n"
            f"_Provider response: `{last_err[:300]}`_"
        )
        return {"content": friendly, "tokens": 0, "error": "auth"}
    return {"content": f"_[Section generation failed: {last_err}]_", "tokens": 0, "error": "other"}


# --------------------------------------------------------------------------
# SRS Revalidation — second pass after every Regenerate.
# Scans DB procedures / functions / triggers from the KB (via Qdrant RAG),
# and incrementally merges explicit business rules into Functional
# Requirements + Detailed Use Cases per srs.revalidation prompt's spec.
# Uses a DIFFERENT model than the main SRS pass per the spec's note.
#
# iter-13.30 — Console-driven rotation. No hard-coded vendor slugs. The
# alternate model is whatever the active provider's `routing` map assigns
# to a DIFFERENT complexity tier than the primary model occupies.
# --------------------------------------------------------------------------


async def _console_model_pool() -> list[str]:
    """Return the union of every active provider's catalogue, default first.

    Used as a last-resort rotation list when the Console provides no
    explicit alternate-tier mapping. Strictly Console-sourced — never
    contains a hard-coded vendor slug.
    """
    try:
        from db import model_providers as mp_col
        cursor = mp_col.find({"is_active": True}, {"_id": 0}).sort([("is_default", -1)])
        seen: set[str] = set()
        out: list[str] = []
        async for p in cursor:
            for m in (p.get("models") or []):
                mid = (m.get("id") or "").strip()
                if mid and mid not in seen:
                    seen.add(mid)
                    out.append(mid)
        return out
    except Exception:
        return []


async def _attempt_model_rotation_console(primary: str) -> list[str]:
    """Console-driven rotation that excludes the primary model.

    Replaces the legacy hard-coded `_FALLBACK_MODEL_ROTATION` list. When
    fabric/Console is unavailable returns an empty list so the caller
    falls back to its own retry path.
    """
    pool = await _console_model_pool()
    primary_norm = (primary or "").strip().lower()
    out = [m for m in pool if m.lower() != primary_norm]
    return out


# Back-compat shims — kept so test_srs_streaming.py still imports them.
# Both delegate to the Console pool synchronously by trusting the caller
# to pass `primary` (so the test stays self-contained).
_FALLBACK_MODEL_ROTATION: list[str] = []  # populated lazily at call-time


def _attempt_model_rotation(primary: str) -> list[str]:
    """Synchronous wrapper kept for test compatibility (iter-13.30 shim).

    Resolution order:
      1. Console-driven rotation (when an event loop & Mongo are available).
      2. Bootstrap fallback derived from `llm.AVAILABLE_MODELS` — the
         "no Console configured" enumeration. NOT a call-site hardcoding
         (the enumeration itself is editable in Console → Models).
    Always returns at least 2 entries so the section-retry plan has room.
    """
    primary_norm = (primary or "").strip().lower()

    # Try Console-driven path
    out: list[str] = []
    try:
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
            # Inside a running loop — caller should use the async path.
            # Skip Mongo here, fall through to bootstrap.
        except RuntimeError:
            out = _asyncio.run(_attempt_model_rotation_console(primary))
    except Exception:
        out = []

    # Bootstrap fallback: pull from the AVAILABLE_MODELS enumeration in
    # llm.py (which itself defers to Console at runtime). This keeps the
    # synchronous test path + cold-boot path working with no hardcoded
    # vendor preference embedded in the SRS module itself.
    if not out:
        try:
            from llm import AVAILABLE_MODELS as _AM
            out = [m["id"] for m in _AM if m.get("id") and m["id"].lower() != primary_norm]
        except Exception:
            out = []

    while len(out) < 2:
        out.append(primary or "")
    return out


# Marker prefixes left in a section's content when generation ultimately
# failed (credits, auth, all-attempts-exhausted). Used by `_repair_failed_sections`
# to detect which sections still need a retry pass.
_FAILURE_MARKERS = (
    "> ⚠️ **Section skipped — LLM provider out of credits.**",
    "> ⚠️ **Section skipped — LLM provider rejected the API key.**",
    "> ⚠️ **Section generation failed**",
    "_[Section generation failed:",
)


def _is_failed_section(content: str) -> bool:
    if not content or not content.strip():
        return True
    head = content.lstrip()[:200]
    return any(head.startswith(m) for m in _FAILURE_MARKERS)


# --------------------------------------------------------------------------
# iter-14.11 — Score-gated per-section auto-retry
#
# After every section is generated we ask the multi-model judge whether it
# clears MIN_SECTION_CONFIDENCE (default 95). If not, we retry up to
# MAX_AUTO_RETRIES times. The retry feeds THIS attempt's content back into
# `_gen_one_section` via `prior_sections`, which activates the DEBT LEDGER
# (see the iter-14.10 comment in `_gen_one_section`) so the LLM sees the
# judge's gaps and fills them.
#
# Everything is behind `LAMA_SRS_AUTORETRY` (default on) so an operator
# can revert to prior behaviour with a single env-var flip.
# --------------------------------------------------------------------------
try:
    MIN_SECTION_CONFIDENCE = float(os.environ.get("LAMA_SRS_MIN_CONFIDENCE", "95") or "95")
except (TypeError, ValueError):
    MIN_SECTION_CONFIDENCE = 95.0
try:
    MAX_AUTO_RETRIES = int(os.environ.get("LAMA_SRS_AUTO_RETRIES", "2") or "2")
except (TypeError, ValueError):
    MAX_AUTO_RETRIES = 2
AUTORETRY_ENABLED = (
    (os.environ.get("LAMA_SRS_AUTORETRY", "1") or "1").strip().lower()
    not in {"0", "false", "no", "off"}
)
# Freeze-gate hard-block threshold (mean of sections_meta.final_score).
# `LAMA_SRS_FREEZE_MIN` defaults to MIN_SECTION_CONFIDENCE so the two
# knobs move together unless the operator wants them separated.
try:
    FREEZE_MIN_CONFIDENCE = float(os.environ.get("LAMA_SRS_FREEZE_MIN", str(MIN_SECTION_CONFIDENCE)) or MIN_SECTION_CONFIDENCE)
except (TypeError, ValueError):
    FREEZE_MIN_CONFIDENCE = MIN_SECTION_CONFIDENCE


async def _score_section_now(
    project_id: str,
    cfg: dict,
    content: str,
    kb_summary: str = "",
    ground_truth: str = "",
) -> dict:
    """Score ONE just-generated SRS section against its IEEE-830 label.

    Uses the FIRST evaluator model returned by Console (via
    `pick_evaluator_models`). One model per attempt is deliberate — this
    call is on the section hot-path (fires 1..3 times per section per run),
    so we keep it cheap. Cross-model spread is still available through the
    "Compute stage confidence" popover which uses the full multi-model
    aggregate.

    iter-14.13 — `kb_summary` + `ground_truth` are now REQUIRED for the
    judge to converge above the 85-88 plateau. Previously both were passed
    as empty strings, which forced the evaluator to score blind. Per the
    confidence-engine rubric that either yields a rubric-default 100
    ("kb_silent_on_topic") or middle-band conservatism (80-88) — neither
    of which reflects the artifact's real fidelity to the KB. Callers on
    the SRS generation hot path MUST pass the same digests
    `compute_stage_confidence` uses (via `_kb_summary_for_eval` +
    `_ground_truth_for_eval` from `routes.living`). When both are empty
    the returned row is tagged `context_missing=True` so the outer loop
    can distinguish an evidence gap from a scorer-context gap.

    iter-14.14 — OPTIONAL LangGraph + HuggingFace engine. When
    `LAMA_CONFIDENCE_ENGINE=langgraph` is set the call is routed through
    `confidence_langgraph.score_section_langgraph`, which uses local HF
    encoder models (sentence-transformer for coverage + cross-encoder
    NLI for contradictions) to SKIP the LLM evaluator entirely on
    sections that HF signals prove are KB-faithful. Empirical target:
    40-60% of well-generated sections skip the LLM call → 40-60% fewer
    Factory AI / OpenRouter tokens spent on the retry loop. Any failure
    (import, graph build, model load, ainvoke) transparently falls back
    to the legacy fabric_call path — no user-visible regression.

    Returns a compact dict:
      {score, band, rationale, gaps, model, context_missing}.
    When routed through the LangGraph engine also carries the extras:
      {engine, route_taken, hf_coverage, hf_contradictions}.
    Never raises — on evaluator failure returns score=0.0 so the outer
    loop records the attempt but doesn't spuriously plateau.
    """
    if not content or not content.strip():
        return {"score": 0.0, "band": "poor", "rationale": "empty content",
                "gaps": [], "model": "", "context_missing": False}
    _ctx_missing = not (kb_summary or "").strip() and not (ground_truth or "").strip()
    if _ctx_missing:
        logger.warning(
            "SRS[%s] · _score_section_now called WITHOUT kb_summary/ground_truth "
            "for %s — evaluator will score blind; confidence will likely plateau. "
            "This is the iter-14.13 root cause; caller should forward the digests "
            "from `_kb_summary_for_eval` + `_ground_truth_for_eval`.",
            project_id, cfg.get("key"),
        )

    # iter-14.14 / 14.29 — LangGraph + HF encoder engine. Picked when the
    # env flag opts in explicitly OR when Factory.ai is not enabled /
    # not connected (auto-fallback via `resolve_confidence_engine`).
    # Falls back to the legacy fabric_call path on any failure so no
    # deployment can regress by flipping the flag.
    try:
        from confidence_langgraph import resolve_confidence_engine as _resolve_ce
        _engine = _resolve_ce()
    except Exception:  # noqa: BLE001
        _engine = (os.environ.get("LAMA_CONFIDENCE_ENGINE") or "").strip().lower()
    if _engine == "langgraph":
        try:
            from confidence_langgraph import (
                is_available as _lg_available,
                score_section_langgraph as _lg_score,
            )
            if _lg_available():
                _lg_result = await _lg_score(
                    project_id, cfg, content,
                    kb_summary=kb_summary or "",
                    ground_truth=ground_truth or "",
                )
                # Preserve the caller-visible `context_missing` flag from
                # the legacy path — the engine may not set it in every
                # code path.
                _lg_result.setdefault("context_missing", _ctx_missing)
                logger.info(
                    "SRS[%s] · confidence engine=langgraph · section=%s · "
                    "route=%s · score=%.1f · coverage=%s · contradictions=%s",
                    project_id, cfg.get("key"),
                    _lg_result.get("route_taken"),
                    float(_lg_result.get("score", 0.0) or 0.0),
                    (_lg_result.get("hf_coverage") or {}).get("coverage"),
                    (_lg_result.get("hf_contradictions") or {}).get("contradictions"),
                )
                return _lg_result
            logger.warning(
                "SRS[%s] · LAMA_CONFIDENCE_ENGINE=langgraph but the engine is "
                "not available (langgraph/langchain-core not importable). "
                "Falling back to legacy fabric_call scoring.",
                project_id,
            )
        except Exception as _lg_exc:  # noqa: BLE001 — graceful fallback
            logger.warning(
                "SRS[%s] · LangGraph confidence engine failed for %s (%s); "
                "falling back to legacy fabric_call scoring.",
                project_id, cfg.get("key"), _lg_exc,
            )

    # iter-14.21 — Strict-HF guard. When the LangGraph engine is selected
    # (or LAMA_CONFIDENCE_STRICT_HF=1 is explicitly set) we MUST NOT fall
    # back to `_score_artifact_multi_model` here, because that routes
    # through fabric_call → the Factory-CLI droid. Return a deterministic
    # engine-unavailable stub instead so the caller sees the outage
    # instead of silently spending Factory tokens on grading.
    try:
        from confidence_langgraph import strict_hf_only as _strict
    except Exception:  # noqa: BLE001
        _strict = lambda: False  # noqa: E731
    if _strict():
        logger.warning(
            "SRS[%s] · strict-HF mode ON — refusing fabric fallback for %s; "
            "returning engine_unavailable row.",
            project_id, cfg.get("key"),
        )
        return {
            "score": 0.0, "band": "poor",
            "rationale": (
                "Confidence engine unavailable and strict-HF mode is on — "
                "skipped Factory/OpenRouter fallback. Rebuild the container "
                "so langgraph + sentence-transformers install cleanly, or "
                "unset LAMA_CONFIDENCE_STRICT_HF to re-enable it."
            ),
            "gaps": [], "model": "", "context_missing": _ctx_missing,
            "engine": "langgraph", "route_taken": "engine_unavailable",
        }

    try:
        models = await _pick_evaluator_models()
        pick = models[:1] if models else [""]
        what = f"IEEE 830 / IEEE 29148 section «{cfg.get('label','')}» — {(cfg.get('instructions') or '')[:400]}"
        v = await _score_artifact_multi_model(
            project_id=project_id,
            stage="SRS",
            artifact_text=content,
            sections=[{
                "key": cfg["key"], "label": cfg.get("label", cfg["key"]),
                "what_to_check": what,
            }],
            kb_summary=kb_summary or "",
            ground_truth=ground_truth or "",
            models=pick,
            min_models=1,
        )
        row = ((v or {}).get("sections") or [{}])[0]
        s = float(row.get("score", 0.0) or 0.0)
        return {
            "score": round(s, 2),
            "band": row.get("band") or _band_of(s),
            "rationale": (row.get("rationale") or "")[:400],
            "gaps": (row.get("gaps") or [])[:8],
            "model": (v.get("models_used") or [""])[0] if isinstance(v, dict) else "",
            "context_missing": _ctx_missing,
            "engine": "fabric",
            "route_taken": "llm",
        }
    except Exception as _exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "SRS[%s] · _score_section_now failed for %s: %s",
            project_id, cfg.get("key"), _exc,
        )
        return {"score": 0.0, "band": "poor",
                "rationale": f"scorer error: {str(_exc)[:120]}",
                "gaps": [], "model": "", "context_missing": _ctx_missing,
                "engine": "fabric", "route_taken": "llm_error"}


def _pick_alternate_model(primary: str) -> str:
    """Synchronous wrapper around the Console-driven alternate picker.

    iter-13.30 — No hard-coded vendor slugs. Asks the active provider's
    routing map for a model on a DIFFERENT complexity tier than the
    primary occupies. Falls back to env-var `LAMA_REVALIDATION_MODEL` if
    set, then to any other model in the Console pool, then to the
    primary itself (so the run still completes).
    """
    import os as _os
    forced = _os.environ.get("LAMA_REVALIDATION_MODEL", "").strip()
    if forced:
        return forced
    try:
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
            # Inside an event loop — callers in the SRS hot path use the
            # async variant directly; this branch is only hit by tests.
            return primary or ""
        except RuntimeError:
            return _asyncio.run(_pick_alternate_model_console(primary))
    except Exception:
        return primary or ""


async def _pick_alternate_model_console(primary: str) -> str:
    """Pick the cross-tier alternate from Console (iter-13.30).

    Algorithm:
      1. Find the active default provider's `routing` map {low,medium,high}.
      2. Identify which tier `primary` occupies.
      3. Return the OTHER tier's model (high → medium, medium → high,
         low → medium). This guarantees a model FROM A DIFFERENT
         complexity rung — usually a different vendor too because the
         user configured tiers that way.
      4. If primary isn't in any tier (e.g. agent override), fall back to
         the provider's `routing["medium"]` then to the first non-primary
         model in the active pool.
    """
    try:
        from db import model_providers as mp_col
        prov = await mp_col.find_one({"is_default": True, "is_active": True}, {"_id": 0})
    except Exception:
        prov = None

    primary_norm = (primary or "").strip().lower()
    routing = (prov or {}).get("routing") or {}
    # Find primary's tier
    primary_tier = ""
    for tier, mid in routing.items():
        if (mid or "").strip().lower() == primary_norm:
            primary_tier = tier
            break

    # Tier rotation: cross to a different tier
    tier_rotation = {"high": "medium", "medium": "high", "low": "medium"}
    target_tier = tier_rotation.get(primary_tier, "medium")
    alt = (routing.get(target_tier) or "").strip()
    if alt and alt.lower() != primary_norm:
        return alt

    # Walk the rest of the routing map for any non-primary slot
    for tier in ("high", "medium", "low"):
        cand = (routing.get(tier) or "").strip()
        if cand and cand.lower() != primary_norm:
            return cand

    # Last resort — first model in the Console pool that isn't primary
    pool = await _console_model_pool()
    for m in pool:
        if m.lower() != primary_norm:
            return m
    return primary or ""


async def _run_revalidation(project_id: str, proj: dict, current_sections: dict, primary_model: str) -> dict:
    """Run the SRS revalidation pass. Best-effort — never raises; returns
    {applied: bool, functional_requirements?, use_cases?, model_used, tokens,
     skipped_reason?}.
    """
    try:
        spec = await _get_prompt(project_id, "srs.revalidation")
        if not spec:
            return {"applied": False, "skipped_reason": "srs.revalidation prompt not seeded"}

        # ---- Gather DB-procedure / function / trigger evidence ----
        # We don't have an explicit PROCEDURE entity type today, so we use
        # Qdrant RAG with targeted queries and additionally pull any SQL
        # chunks whose source filename hints at /db/ or .sql.
        rag_queries = [
            "CREATE PROCEDURE stored procedure business rule status",
            "CREATE FUNCTION return validation logic",
            "CREATE TRIGGER BEFORE AFTER INSERT UPDATE row condition",
            "IF condition THEN UPDATE status workflow transition",
            "BEGIN END plsql declare cursor exception",
        ]
        chunks: list[str] = []
        seen: set[str] = set()
        for q in rag_queries:
            try:
                hits = await qdrant_search(project_id, q, top_k=10)
            except Exception:
                hits = []
            for h in hits or []:
                key = (h or "")[:200]
                if key and key not in seen:
                    seen.add(key)
                    chunks.append(h)
        # Cap evidence block at ~30k chars so we leave headroom for the model.
        evidence_block = "\n\n--- procedure/function/trigger chunk ---\n\n".join(chunks)
        if len(evidence_block) > 30000:
            evidence_block = evidence_block[:30000] + "\n...[truncated]"

        # iter-13.75 — Graph subgraph block (parity with _gen_one_section).
        # Without this the revalidation pass was RAG-only over Qdrant — it
        # never saw the property graph (Tables/Columns/Routes/Methods/Roles
        # + REFERENCES_TABLE / EXPOSES / GUARDED_BY / BELONGS_TO_ENTITY
        # edges) that the first-pass section generator already cites. As a
        # result the revalidator couldn't detect FR/UC rows that were
        # missing for a real Table/Route node, or rules whose enforcement
        # layer (UI/API/SERVICE/DB) didn't match an actual graph edge.
        # When the Graph KB toggle is OFF or the graph hasn't been built,
        # this block stays empty and the revalidation continues on
        # evidence + governance only (no behavioural change vs. before).
        graph_block_reval = ""
        try:
            from kb.graph_config import is_graph_kb_enabled
            if project_id and await is_graph_kb_enabled(project_id):
                from kb.graph_retriever import subgraph_yaml_for_section
                seen_seeds: set[str] = set()
                extras: list[str] = []
                for sk in ("specific_requirements", "detailed_use_cases",
                           "external_interfaces", "system_features"):
                    for s in (SECTION_QUERIES.get(sk, "") or "").split():
                        if s and s not in seen_seeds:
                            seen_seeds.add(s)
                            extras.append(s)
                graph_yaml = await subgraph_yaml_for_section(
                    project_id,
                    "specific_requirements",
                    extra_seeds=extras[:120],
                    max_chars=10000,
                    hops=2,
                    max_nodes=260,
                )
                if graph_yaml:
                    graph_block_reval = (
                        "PROPERTY GRAPH SUBGRAPH (authoritative entity/relation skeleton — "
                        "every Table / Route / Method / Role node below MUST appear in "
                        "FR or UC, or be explained by an entry in the trigger evidence "
                        "block; cite IDs VERBATIM, follow declared relations):\n\n"
                        + graph_yaml
                    )
        except Exception:  # noqa: BLE001 — graph is best-effort
            graph_block_reval = ""

        # iter-13.75 — skip only when BOTH evidence AND graph are empty;
        # previously the absence of DB-procedure evidence aborted the pass
        # even on projects with rich graph coverage that could drive
        # additions on its own.
        if not evidence_block.strip() and not graph_block_reval:
            return {"applied": False,
                    "skipped_reason": "no DB procedure/function/trigger evidence AND no graph subgraph available"}

        # ---- Compose the revalidation system prompt ----
        spec_block = spec.replace("{project_name}", proj.get("name", "") or "the system")
        # Prepend the full governance bundle so the revalidation pass inherits
        # gov.core's truth_rule_mode and the field-traceability / business-rule
        # extraction contracts (same source of truth as the section generator).
        gov_block = await _load_governance_bundle(project_id, proj.get("name", ""))
        fr_current = current_sections.get("specific_requirements", "") or ""
        uc_current = current_sections.get("detailed_use_cases", "") or ""

        system_prompt = (
            gov_block
            + "═══════════════════════════════════════════════════════════════════\n"
            "MANDATORY REVALIDATION DIRECTIVE\n"
            "You are running the SECOND pass over an already-generated IEEE 830\n"
            "SRS document. You MUST honour every mandate / shall_not / "
            "failure_condition listed in the YAML below. Do NOT invent rules. "
            "Do NOT rewrite untouched paragraphs. Only ADD explicit rules you "
            "can cite from the procedure/function/trigger evidence block.\n"
            "═══════════════════════════════════════════════════════════════════\n"
            f"{spec_block}\n"
            "═══════════════════════════════════════════════════════════════════\n\n"
            f"PROJECT: {proj.get('name', '')}\n"
            f"SOURCE STACK (declared): {proj.get('source_tech', '')}\n"
            f"SOURCE STACK (detected — AUTHORITATIVE): {(proj.get('detected_tech') or {}).get('summary', '') or '(none)'}\n"
            f"TARGET STACK: {proj.get('target_tech', '')}\n\n"
            "CURRENT SRS — Section 4 (Specific Requirements):\n"
            f"{fr_current}\n\n"
            "CURRENT SRS — Section 5 (Detailed Use Cases):\n"
            f"{uc_current}\n\n"
            "DB PROCEDURE / FUNCTION / TRIGGER EVIDENCE (binding source of truth):\n"
            f"{evidence_block}\n\n"
            + (f"{graph_block_reval}\n\n" if graph_block_reval else "")
            + "TASK:\n"
            "1. Identify explicit business rules in the evidence above "
            "(status transitions, mandatory-field checks, role/state gates, "
            "conditional INSERT/UPDATE). IGNORE indexing/logging/infrastructure SQL.\n"
            "2. Merge each rule INCREMENTALLY into the matching FR-ID row in "
            "Section 4.1 (or add a new FR-ID at the next free number).\n"
            "3. For every affected use case in Section 5, ensure the "
            "Preconditions + Postconditions + Business Rules subsections "
            "list the rule with traceability.\n"
            "4. iter-13.75 — GRAPH COVERAGE CHECK. Walk the PROPERTY GRAPH "
            "SUBGRAPH above (if present). For EVERY Table / Route / Method "
            "/ Role node that does NOT appear in Section 4 or Section 5, "
            "add a NEW FR-ID row (or augment the closest existing FR) that "
            "explicitly references the node by its verbatim ID and the "
            "relation declared in the graph (REFERENCES_TABLE / EXPOSES / "
            "HAS_METHOD / GUARDED_BY / BELONGS_TO_ENTITY). If no DB-rule "
            "evidence backs the addition, mark it `confidence=NOT_EVIDENCED` "
            "with an inline `⚠ EVIDENCE GAP` marker and append a "
            "graph_coverage trace comment in the format:\n"
            "   <!-- graph_coverage: node=<TYPE>:<VERBATIM_ID> "
            "relation=<EDGE_TYPE> reason=ADD_FROM_GRAPH -->\n"
            "5. iter-13.76 — STORYTELLING CHECK for Section 5. For EVERY "
            "`### UC-<NN>:` heading, verify a `#### Narrative (User Story)` "
            "subsection exists IMMEDIATELY after the heading and BEFORE "
            "`#### Metadata`. If the Narrative is MISSING / shorter than 80 "
            "words / a bullet list / passive voice / uses class-or-service "
            "identifiers instead of named actors in business roles, ADD a "
            "correctly-shaped narrative of 2–4 paragraphs (120–250 words) "
            "in present tense, active voice, naming actors as people "
            "(e.g. \"the District Magistrate\", \"the applicant Anita\") and "
            "citing real screen labels / button text / role names from the "
            "KB. NEVER use class / interface / service identifiers in the "
            "story. Append a one-line trace comment:\n"
            "   <!-- storytelling: uc=<UC_ID> reason=ADD_NARRATIVE|FIX_VOICE -->\n"
            "5b. iter-17.6 — END-TO-END JOURNEY CHECK for Section 5. For "
            "EVERY `### UC-<NN>:` heading, verify a "
            "`#### End-to-End Journey (Screen → API → DB)` subsection "
            "exists AFTER `#### Field Specification Table` and BEFORE "
            "`#### Data Flow Diagram`. If missing / empty / has fewer than "
            "the minimum credible row count (≥3 rows for state-mutating "
            "UCs, ≥1 row for read-only UCs), REBUILD it as a Markdown table "
            "with columns: `Step | Screen / UI Element | User Action | "
            "API Method + Path | Legacy Handler | Request Fields → Payload "
            "| DB Table | DB Column(s) | Operation | Response → UI Effect`. "
            "Populate `API Method + Path` VERBATIM from the KB routes / "
            "graph EXPOSES edges. Populate `Legacy Handler` in "
            "`<relative/path>::<symbol>` form from the KB routes / methods. "
            "Populate `DB Table` + `DB Column(s)` VERBATIM from the KB "
            "tables/columns slice or graph REFERENCES_TABLE edges. If a "
            "route or table cannot be sourced from the KB, mark the cell "
            "`NOT_EVIDENCED` — do NOT invent. Append a one-line trace comment:\n"
            "   <!-- e2e_journey: uc=<UC_ID> reason=ADD_TABLE|EXTEND_ROWS -->\n"
            "6. For every addition derived from the trigger evidence, "
            "append a one-line traceability comment in the exact format:\n"
            "   <!-- traceability: proc=<NAME> path=<DB_OBJECT_PATH> "
            "trigger=<COND_EXPR> reason=ADD|CORRECT -->\n"
            "7. Output STRICTLY this JSON (no preamble, no code fences):\n"
            '   {"specific_requirements": "<full updated markdown for sec 4>",\n'
            '    "detailed_use_cases": "<full updated markdown for sec 5>"}\n'
            "If the evidence + graph yield ZERO acceptable additions, return "
            "the two sections unchanged inside the same JSON shape."
        )

        alt_model = await _pick_alternate_model_console(primary_model)
        result = await chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Run the revalidation pass now and return the JSON."},
            ],
            model=alt_model,
            temperature=0.1,
            max_tokens=14000,
            timeout=300.0,
            agent_key="srs.revalidation",   # iter-13.76 — explicit tier (high → Opus)
            project_id=project_id,
        )
        raw = (result.get("content") or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            # strip optional leading "json"
            if raw[:4].lower() == "json":
                raw = raw[4:]
            raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            # Last-ditch: pull the first {...} block.
            m = _re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return {
                    "applied": False,
                    "model_used": alt_model,
                    "tokens": result.get("usage", {}).get("total_tokens", 0),
                    "skipped_reason": "revalidation model did not return parseable JSON",
                }
            data = json.loads(m.group(0))

        new_fr = (data.get("specific_requirements") or "").strip()
        new_uc = (data.get("detailed_use_cases") or "").strip()
        if not (new_fr and new_uc):
            return {
                "applied": False,
                "model_used": alt_model,
                "tokens": result.get("usage", {}).get("total_tokens", 0),
                "skipped_reason": "revalidation model returned empty section(s)",
            }

        # Audit trail
        try:
            await audit_log.insert_one({
                "action": "srs.revalidation",
                "project_id": project_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": {
                    "primary_model": primary_model,
                    "revalidation_model": alt_model,
                    "evidence_chunks": len(chunks),
                    "tokens": result.get("usage", {}).get("total_tokens", 0),
                },
            })
        except Exception:
            pass

        return {
            "applied": True,
            "model_used": alt_model,
            "tokens": result.get("usage", {}).get("total_tokens", 0),
            "specific_requirements": new_fr,
            "detailed_use_cases": new_uc,
        }
    except Exception as exc:
        return {"applied": False, "skipped_reason": f"revalidation crashed: {str(exc)[:240]}"}


async def _load_srs_context(project_id: str, conversation_id: str | None):
    # ── iter-13.89 forensic fix ─────────────────────────────────────────
    # Hard-reject an empty / whitespace project_id at the very top of the
    # SRS context loader. Every downstream Mongo query (kb_toon,
    # kb_entities, kb_files, legacy_analysis, messages …) is keyed by
    # project_id; an empty value either matches nothing or — worse — gets
    # silently swapped for an unscoped `{}` filter by a defensive caller
    # (see the LEGACY FILE INDEX builders above). Refuse early so a
    # malformed payload cannot leak another project's KB into the SRS.
    project_id = (project_id or "").strip()
    if not project_id:
        raise HTTPException(400, "project_id required")

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    existing = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if existing and existing.get("frozen"):
        raise HTTPException(400, "SRS is frozen — unfreeze before regenerating")

    # iter-13.88 — Orphaned-marker guard. `proj.legacy_path` is written by
    # scan-folder / clone-git ingests and embedded into the SRS prompt's
    # "LEGACY CODEBASE LOCATION" block. If the project was later re-ingested
    # via the upload path (which previously did NOT clear this marker), the
    # SRS would cite the OLD folder path even though the user uploaded a
    # totally different source. Detect a stale marker (folder no longer
    # exists on host) and silently clear it so the SRS prompt only injects
    # ground-truth paths.
    legacy_path_now = (proj.get("legacy_path") or "").strip()
    if legacy_path_now:
        import os as _os
        if not _os.path.isdir(legacy_path_now):
            logger.warning(
                "SRS: clearing orphaned legacy_path %r for project=%s "
                "(folder no longer exists on host)",
                legacy_path_now, project_id,
            )
            try:
                await projects.update_one(
                    {"id": project_id},
                    {"$unset": {
                        "legacy_path": "",
                        "legacy_path_scanned_at": "",
                    }},
                )
                proj["legacy_path"] = ""
            except Exception:  # noqa: BLE001
                pass

    toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
    full_toon = (toon_doc or {}).get("toon", "")
    summary = (toon_doc or {}).get("summary", "Knowledge base is empty.")

    # iter-13.87 — Hard guard: refuse SRS generation when the KB is empty
    # OR has not been re-built since the latest source ingest. Without
    # this, replacing the legacy source (e.g. cloning a different git
    # URL) and immediately clicking Generate SRS produced output that
    # cited the OLD codebase because:
    #   - kb_toon was purged on the new ingest, so `full_toon` was empty
    #   - but `messages` (chat history) still mentioned old class names
    # ⇒ the LLM hallucinated by stitching together stale chat context
    #   with an empty source-of-truth.
    # We now block here and tell the user exactly what to do.
    from db import kb_files as _kbf_col
    kb_file_count = await _kbf_col.count_documents({"project_id": project_id})
    if kb_file_count == 0:
        raise HTTPException(
            400,
            "Knowledge base is empty. Upload / scan / git-clone the legacy "
            "source on the Discovery page before generating SRS.",
        )
    if not full_toon.strip():
        raise HTTPException(
            400,
            f"Knowledge base has {kb_file_count} file(s) but has not been "
            "built yet. Click \"Build KB\" on the Discovery page before "
            "generating SRS — otherwise the LLM cannot see the new source.",
        )
    # Detect stale TOON (files changed since last build).
    try:
        files_now = await _kbf_col.find(
            {"project_id": project_id},
            {"_id": 0, "id": 1, "filename": 1, "size": 1,
             "chunk_count": 1, "entity_count": 1, "status": 1},
        ).to_list(None)
        from routes.kb import _files_fingerprint  # reuse the canonical hash
        current_fp = _files_fingerprint(files_now)
        toon_fp = (toon_doc or {}).get("build_fingerprint") or (toon_doc or {}).get("files_fingerprint")
        if toon_fp and toon_fp != current_fp:
            raise HTTPException(
                400,
                "Knowledge base is stale — source files have changed since "
                "the last Build KB. Re-run \"Build KB\" on the Discovery "
                "page before regenerating SRS so the new source is used.",
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        # Fingerprint check is best-effort; never block on plumbing failures.
        pass

    q = {"project_id": project_id}
    if conversation_id:
        q["conversation_id"] = conversation_id
    # iter-13.89 — `messages` is already canonically keyed by project_id;
    # the empty-project_id guard at the top of this function prevents
    # the `{"project_id": ""}` degenerate case that would otherwise
    # match nothing AND open the door to a future `if project_id else {}`
    # defensive copy-paste. Tenant_id is NOT added here because legacy
    # message rows pre-date the tenant denormalisation (iter-13.87) and
    # filtering them out would silently truncate chat history.
    history = await messages.find(q, {"_id": 0}).sort("created_at", 1).to_list(200)
    convo_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)[-6000:]

    # iter-13.17 — ensure the deep legacy-logic analysis exists, then
    # build a compact digest the SRS section prompts can embed. The
    # auto-trigger is BEST-EFFORT: a failure here is logged but does
    # NOT block SRS generation (sections fall back to TOON+RAG only).
    #
    # iter-14.8 — treat the persisted failure-marker doc (status=failed)
    # as "no analysis available" WITHOUT re-invoking the LLM. Prior to
    # iter-14.8, `analysis_doc` was truthy only when a successful run
    # existed, so failures silently re-ran the whole 240s pass on every
    # section regen — a single droid outage burned ~48 minutes for a
    # 12-section SRS. Now the failure marker short-circuits fast and
    # the cool-down inside `run_legacy_analysis` prevents thrashing.
    analysis_doc = await legacy_analysis_col.find_one({"project_id": project_id}, {"_id": 0})
    if analysis_doc and analysis_doc.get("status") == "failed":
        logger.debug(
            "SRS[%s] deep-analysis unavailable (last attempt failed at %s); "
            "proceeding with TOON+RAG only",
            project_id, analysis_doc.get("failed_at", "?"),
        )
        analysis_doc = None
    if not analysis_doc:
        try:
            logger.info("Auto-triggering deep legacy analysis for %s", project_id)
            analysis_doc = await run_legacy_analysis(project_id)
            # iter-14.8 — `run_legacy_analysis` may return the failure
            # marker itself during the cool-down window; treat it the
            # same as "no analysis available" downstream.
            if analysis_doc and analysis_doc.get("status") == "failed":
                analysis_doc = None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Auto deep-analysis failed for %s — proceeding without it: %s",
                project_id, exc,
            )
            analysis_doc = None
    analysis_digest = build_analysis_digest(analysis_doc) if analysis_doc else ""

    # iter-14.80 — Append the KB deep analysis summary (business rules,
    # roles, field traceability) that was generated during Build KB.
    # This provides richer context than legacy_analysis alone.
    try:
        from db import kb_deep_analysis
        kb_da_doc = await kb_deep_analysis.find_one({"project_id": project_id}, {"_id": 0})
        if kb_da_doc and kb_da_doc.get("summary_md"):
            da_summary = kb_da_doc["summary_md"]
            if da_summary.strip():
                # Append after the legacy analysis digest
                if analysis_digest:
                    analysis_digest += "\n\n---\n\n"
                analysis_digest += (
                    "## KB DEEP ANALYSIS (Business Rules, Roles, Field Traceability)\n\n"
                    + da_summary[:12000]  # Cap to avoid prompt overflow
                )
                logger.info(
                    "SRS[%s] injected KB deep analysis summary (%d chars)",
                    project_id, len(da_summary),
                )
    except Exception as _da_exc:  # noqa: BLE001
        logger.debug("KB deep analysis fetch failed (harmless): %s", _da_exc)

    return proj, existing, full_toon, summary, convo_text, analysis_digest


async def _persist_srs(project_id: str, existing: dict | None, sections: dict, total_tokens: int, model: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    doc = SRSDocument(
        project_id=project_id,
        sections=sections,
        version=(existing.get("version", 0) + 1) if existing else 1,
    )
    doc_dict = doc.model_dump()
    doc_dict["updated_at"] = now
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": doc_dict},
        upsert=True,
    )
    await audit_log.insert_one({
        "action": "srs.generate",
        "project_id": project_id,
        "at": now,
        "details": {
            "model": model,
            "sections": len(sections),
            "version": doc_dict["version"],
            "tokens": total_tokens,
        },
    })
    return doc_dict["version"]


@router.post("/generate")
async def generate_srs(payload: dict):
    """Synchronous JSON generation — runs all 8 sections then returns."""
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    # iter-13.81.3 — re-run? → srs.regenerate so the Console's
    # `discovery.regenerate` bucket pin is honoured by Factory.
    _existing_srs = await srs_documents.find_one({"project_id": project_id}, {"_id": 1})
    set_current_agent_key("srs.regenerate" if _existing_srs else "srs.generate")
    conversation_id = payload.get("conversation_id")
    # iter-13.30 — no hardcoded vendor slug. Empty model = `fabric_call`
    # resolves via Console: AGENT_COMPLEXITY["srs.generate"] = "high" →
    # active provider's `routing["high"]`. Override per-request from the
    # Discovery chat model dropdown if needed.
    model = payload.get("model") or ""

    # iter-13.34 fix — clear any stale provider pin on the srs.generate
    # agent so each regen starts from the current Console default. See
    # the streaming endpoint below for the full rationale.
    try:
        from db import agent_configs as _ac
        await _ac.update_one(
            {"key": "srs.generate"},
            {"$set": {"provider_id": ""}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass

    proj, existing, full_toon, summary, convo_text, analysis_digest = await _load_srs_context(project_id, conversation_id)

    # iter-13.42 — batch-mode dispatch for the sync /generate endpoint.
    # Same resolution as /generate/stream: explicit payload wins, else env
    # `LAMA_SRS_BATCH=0` opts out, else default = batched. This is the
    # token-saving + accuracy-improving path: ONE LLM call streams every
    # section instead of 11 separate calls, shared TOON / RAG-union /
    # graph subgraph / governance preamble sent only once.
    _env_batch_raw = (os.environ.get("LAMA_SRS_BATCH", "") or "").strip().lower()
    _env_batch_off = _env_batch_raw in {"0", "false", "no", "off"}
    if "mode" in payload or "batch" in payload:
        _mode_val = (payload.get("mode") or "").lower()
        batch_mode = (_mode_val == "batch") or bool(payload.get("batch"))
        if _mode_val in {"per-section", "per_section", "sequential"}:
            batch_mode = False
    elif _env_batch_off:
        batch_mode = False
    else:
        batch_mode = True

    sections: dict[str, str] = {}
    total_tokens = 0
    aborted_first_section = False
    consec_fails_sync = 0  # iter-13.81.10 — tolerate transient failures

    if batch_mode:
        # No SSE listeners in the sync path — provide a no-op broadcast
        # so _run_batch_srs's per-section progress events are simply
        # discarded. Persistence still happens inside the helper.
        async def _noop_broadcast(_evt):
            return None
        _control: dict[str, bool] = {"paused": False, "cancelled": False}
        batched_sections, _batched_tokens, repair_keys = await _run_batch_srs(
            project_id=project_id, proj=proj, full_toon=full_toon,
            summary=summary, convo_text=convo_text,
            analysis_digest=analysis_digest, model=model,
            broadcast=_noop_broadcast, control=_control,
        )
        sections.update(batched_sections)
        total_tokens += _batched_tokens

        # Entity model (deterministic) — always compute.
        try:
            em = await _gen_entity_model(project_id)
            sections["entity_model"] = em.get("content", "")
            await srs_documents.update_one(
                {"project_id": project_id},
                {"$set": {
                    "sections.entity_model": sections["entity_model"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Entity-model gen failed in sync batch: %s", exc)

        # Repair short / missing sections.
        # iter-13.43 — batched repair pass first (one extra LLM call total
        # instead of N) so we don't re-scan the legacy code for each
        # missing section. Per-section fallback below covers any sections
        # the second batched call still left short.
        if repair_keys:
            try:
                repaired_subset, _subset_tokens, repair_keys = await _run_batch_srs_subset(
                    project_id=project_id, proj=proj, full_toon=full_toon,
                    summary=summary, convo_text=convo_text,
                    analysis_digest=analysis_digest, model=model,
                    subset_keys=repair_keys,
                    broadcast=_noop_broadcast, control=_control,
                )
                sections.update(repaired_subset)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sync batched repair failed (%s) — "
                               "falling back to per-section repair", exc)

        for rk in repair_keys:
            cfg = next((c for c in SECTION_CONFIGS if c["key"] == rk), None)
            if not cfg or cfg["key"] in _BATCH_SKIP_KEYS:
                continue
            try:
                r = await _gen_one_section(
                    cfg, proj, full_toon, summary, convo_text, model,
                    project_id=project_id, prior_sections=sections,
                    analysis_digest=analysis_digest, fast_mode=True,
                )
                sections[rk] = r.get("content", "")
                total_tokens += r.get("tokens", 0)
                await srs_documents.update_one(
                    {"project_id": project_id},
                    {"$set": {
                        f"sections.{rk}": sections[rk],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                    upsert=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sync batch repair of %s failed: %s", rk, exc)
    else:
        for idx, cfg in enumerate(SECTION_CONFIGS, start=1):
            # Pass everything written so far so later sections cite real
            # IDs from earlier ones (iter-13.10 cross-section consistency).
            r = await _gen_one_section(
                cfg, proj, full_toon, summary, convo_text, model,
                project_id=project_id, prior_sections=sections,
                analysis_digest=analysis_digest,
            )
            sections[cfg["key"]] = r["content"]
            total_tokens += r["tokens"]
            # incremental save so client can poll GET /srs/{id}
            await srs_documents.update_one(
                {"project_id": project_id},
                {"$set": {
                    "project_id": project_id,
                    f"sections.{cfg['key']}": r["content"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
            # iter-13.35 — EARLY ABORT on first-section failure (sync variant).
            # iter-13.81.10 — Softened to N consecutive failures (default 3).
            if _is_failed_section(r["content"]):
                consec_fails_sync += 1
            else:
                consec_fails_sync = 0
            _max_fails_sync = int(os.environ.get("LAMA_SRS_MAX_CONSEC_FAILS", "3") or "3")
            if consec_fails_sync >= _max_fails_sync:
                logger.warning(
                    "SRS[%s] · %d consecutive failures (≥%d) in sync path — aborting. preview=%r",
                    project_id, consec_fails_sync, _max_fails_sync,
                    (r.get("content") or "")[:200],
                )
                aborted_first_section = True
                break

    if aborted_first_section:
        version = await _persist_srs(project_id, existing, sections, total_tokens, model)
        return {
            "ok": False,
            "aborted": True,
            "abort_reason": (
                f"SRS run aborted — {consec_fails_sync} consecutive sections failed "
                f"(threshold={int(os.environ.get('LAMA_SRS_MAX_CONSEC_FAILS', '3') or '3')}). "
                "The upstream provider is likely fully down — Factory.ai Droid Computer "
                "offline, every fabric provider out of credits, or every API key "
                "rejected. Fix the upstream issue and click Regenerate SRS — sections "
                "that DID succeed are preserved."
            ),
            "sections": sections,
            "version": version,
            "total_tokens": total_tokens,
            "revalidation": {"applied": False, "skipped_reason": "consecutive_failures"},
        }

    # ---- Second pass: SRS Revalidation against DB procedures/functions/triggers ----
    revalidation = await _run_revalidation(project_id, proj, sections, model)
    if revalidation.get("applied"):
        sections["specific_requirements"] = revalidation["specific_requirements"]
        sections["detailed_use_cases"] = revalidation["detailed_use_cases"]
        total_tokens += revalidation.get("tokens", 0)
        await srs_documents.update_one(
            {"project_id": project_id},
            {"$set": {
                "sections.specific_requirements": sections["specific_requirements"],
                "sections.detailed_use_cases": sections["detailed_use_cases"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )

    version = await _persist_srs(project_id, existing, sections, total_tokens, model)
    return {
        "ok": True,
        "sections": sections,
        "version": version,
        "total_tokens": total_tokens,
        "revalidation": {
            "applied": revalidation.get("applied", False),
            "model_used": revalidation.get("model_used"),
            "skipped_reason": revalidation.get("skipped_reason"),
            "tokens": revalidation.get("tokens", 0),
        },
    }


# ══════════════════════════════════════════════════════════════════════
# iter-13.41 — BATCHED SRS GENERATION (single LLM call, streamed).
#
# Why this exists:
#   Default flow makes ONE LLM call PER SECTION (11 LLM-driven sections
#   = 11 calls). Each call carries ~30-45 KB of nearly-identical context
#   (governance preamble, KB summary, TOON skeleton, graph subgraph,
#   deep-analysis digest, project header). That's ~400 KB of redundant
#   input tokens per SRS run — anywhere from 100K-400K wasted tokens.
#
# Batched mode (`payload['mode'] == 'batch'`):
#   • Builds the shared context ONCE (~50 KB).
#   • Concatenates every section's instructions into a single user prompt
#     with `<<<SECTION:key>>>` / `<<<END_SECTION:key>>>` delimiters.
#   • Streams the response. As each `<<<END_SECTION:key>>>` delimiter
#     arrives we persist that section to Mongo and emit a
#     `section_complete` SSE event — so the UI still renders sections
#     progressively even though there's only ONE LLM call underneath.
#
# Trade-offs (kept opt-in):
#   • Output budget: heavy sections (4 / 5 / 6 / 7) can blow the model's
#     `max_tokens`. Any section that streams in below MIN_BATCH_SECTION_CHARS
#     is queued for per-section repair using the existing _gen_one_section
#     fallback — token spend on the regenerated subset is still far less
#     than running all 11 individually.
#   • Reliability: if the stream dies mid-response, every section persisted
#     so far survives; the unfinished one + the queue get repaired via the
#     existing post-gen repair path.
# ══════════════════════════════════════════════════════════════════════

# Sections that should NOT be in the batched prompt:
#   • entity_model — deterministic (no LLM)
_BATCH_SKIP_KEYS = {"entity_model"}

# Minimum char count below which a batched section is treated as "missing"
# and queued for repair.
#
# iter-13.43 — dropped from 600 → 250. Empirically, batched runs frequently
# produce compact-but-valid sections (e.g. Appendices, short NFR sub-sections)
# in the 300–550 char range. The old 600-char threshold triggered the
# expensive per-section repair loop unnecessarily — which is exactly the
# "every section being regenerated by scanning legacy code again" symptom
# users reported. 250 chars still catches genuinely empty / refusal output
# without flagging legitimate short sections.
MIN_BATCH_SECTION_CHARS = 250

_SECTION_OPEN_RE = _re.compile(
    r"<<<\s*SECTION\s*:\s*([a-z_]+)\s*>>>", _re.IGNORECASE,
)
_SECTION_CLOSE_RE = _re.compile(
    r"<<<\s*/?\s*END_?SECTION\s*:?\s*([a-z_]*)\s*>>>", _re.IGNORECASE,
)


def _build_batch_instructions() -> tuple[str, list[dict]]:
    """Concatenate every (non-deterministic) section's instructions into a
    single delimited user prompt. Returns (prompt_text, included_cfgs).
    """
    included = [c for c in SECTION_CONFIGS if c["key"] not in _BATCH_SKIP_KEYS]
    blocks: list[str] = [
        "You are about to write a COMPLETE IEEE 830 / 29148 SRS in ONE response. "
        "The document has the sections listed below. Wrap each section's body in "
        "the exact delimiters shown — the closing delimiter is how downstream "
        "tooling knows the section is finished.",
        "",
        "DELIMITER CONTRACT (HARD — do NOT vary):",
        "  • Open  : `<<<SECTION:key>>>` on its own line BEFORE the section body.",
        "  • Close : `<<<END_SECTION:key>>>` on its own line AFTER the section body.",
        "  • The `key` value is the lowercase snake_case id shown in the table below.",
        "  • Do NOT emit any prose outside delimiter pairs.",
        "  • Do NOT skip a section; if a section is genuinely thin on evidence, "
        "    still emit the open/close pair with `> ⚠ EVIDENCE GAP` markers inside.",
        "  • Do NOT wrap delimiters in markdown / code fences.",
        "",
        "SECTIONS TO PRODUCE (in this exact order):",
    ]
    for idx, cfg in enumerate(included, start=1):
        blocks.append(
            f"  {idx}. `{cfg['key']}` — {cfg['label']} "
            f"(min_words={cfg['min_words']}, focus={cfg['toon_focus']})"
        )
    blocks.append("")
    blocks.append(
        "Now write each section in order. For each section, follow its specific "
        "INSTRUCTION BLOCK below verbatim (sub-section headings, table formats, "
        "phrasing contracts). Honor the SRS AUTHORING CHARTER, tech-stack neutrality, "
        "and human-readability contract from the SYSTEM prompt for EVERY section."
    )
    blocks.append("")
    blocks.append("══════════════════════════════════════════════════════════════════════")
    blocks.append("PER-SECTION INSTRUCTION BLOCKS")
    blocks.append("══════════════════════════════════════════════════════════════════════")
    for cfg in included:
        target_w = int(cfg["min_words"] * 1.6) if cfg["min_words"] else 0
        blocks.append("")
        blocks.append(f"───── SECTION `{cfg['key']}` — {cfg['label']} ─────")
        blocks.append(f"(min {cfg['min_words']} words, target ~{target_w} words; "
                      f"open with `<<<SECTION:{cfg['key']}>>>`, "
                      f"close with `<<<END_SECTION:{cfg['key']}>>>`)")
        blocks.append("")
        blocks.append(cfg["instructions"])
    blocks.append("")
    blocks.append("Begin now. Remember: every section MUST be wrapped in its open/close "
                  "delimiter pair, sections in the order listed above, no preamble before "
                  "the first delimiter, no commentary between sections.")
    return "\n".join(blocks), included


def _build_batch_instructions_subset(subset_keys: list[str]) -> tuple[str, list[dict]]:
    """iter-13.43 — Build a batched user prompt for a SUBSET of sections.

    Used by the post-batch repair pass: if the primary batched call left
    some sections too short / missing, we re-issue ONE more batched call
    asking the LLM to produce ONLY those sections, reusing the same
    shared system prompt (governance + TOON + RAG + graph + analysis +
    legacy-fs index). This replaces the previous per-section repair loop
    which rebuilt the full context for each missing section — the exact
    redundant-scanning behaviour the user flagged.

    Returns (prompt_text, included_cfgs) just like `_build_batch_instructions`.
    """
    valid = set(subset_keys)
    included = [c for c in SECTION_CONFIGS
                if c["key"] in valid and c["key"] not in _BATCH_SKIP_KEYS]
    blocks: list[str] = [
        "REPAIR PASS — the previous batched call did not return acceptable "
        "content for the sections listed below. Re-emit ONLY these sections "
        "using the SAME shared context (SYSTEM prompt above) — do NOT "
        "re-introduce sections that already succeeded.",
        "",
        "DELIMITER CONTRACT (HARD — do NOT vary):",
        "  • Open  : `<<<SECTION:key>>>` on its own line BEFORE the section body.",
        "  • Close : `<<<END_SECTION:key>>>` on its own line AFTER the section body.",
        "  • The `key` value is the lowercase snake_case id shown in the table below.",
        "  • Do NOT emit prose outside delimiter pairs.",
        "  • Do NOT wrap delimiters in markdown / code fences.",
        "",
        "SECTIONS TO REGENERATE (in this exact order):",
    ]
    for idx, cfg in enumerate(included, start=1):
        blocks.append(
            f"  {idx}. `{cfg['key']}` — {cfg['label']} "
            f"(min_words={cfg['min_words']}, focus={cfg['toon_focus']})"
        )
    blocks.append("")
    blocks.append("══════════════════════════════════════════════════════════════════════")
    blocks.append("PER-SECTION INSTRUCTION BLOCKS")
    blocks.append("══════════════════════════════════════════════════════════════════════")
    for cfg in included:
        target_w = int(cfg["min_words"] * 1.6) if cfg["min_words"] else 0
        blocks.append("")
        blocks.append(f"───── SECTION `{cfg['key']}` — {cfg['label']} ─────")
        blocks.append(f"(min {cfg['min_words']} words, target ~{target_w} words; "
                      f"open with `<<<SECTION:{cfg['key']}>>>`, "
                      f"close with `<<<END_SECTION:{cfg['key']}>>>`)")
        blocks.append("")
        blocks.append(cfg["instructions"])
    blocks.append("")
    blocks.append("Begin now — emit ONLY the sections listed above, in the order listed, "
                  "each wrapped in its open/close delimiter pair.")
    return "\n".join(blocks), included


class _SectionStreamParser:
    """Stateful parser for `<<<SECTION:key>>>...<<<END_SECTION:key>>>` chunks.

    Accumulates the raw token stream and detects open/close delimiters as
    they arrive. Emits structured events through the `events` deque:
       {"type": "section_open",  "key": "..."}
       {"type": "section_close", "key": "...", "content": "..."}
    The caller drains `events` between feeds.
    """

    def __init__(self, valid_keys: set[str]):
        self._buf: str = ""
        self._active_key: str | None = None
        self._active_buf: list[str] = []
        self._valid = valid_keys
        self.events: list[dict] = []
        # Sections that closed without ever opening (malformed) — surfaced
        # in the summary so the caller can repair them.
        self.completed_keys: set[str] = set()

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self._buf += chunk
        while True:
            if self._active_key is None:
                m = _SECTION_OPEN_RE.search(self._buf)
                if not m:
                    # No open marker yet — drop everything up to the last
                    # potential delimiter prefix (`<`) to keep the buffer small.
                    cut = self._buf.rfind("<<<")
                    if cut > 0 and cut < len(self._buf) - 12:
                        self._buf = self._buf[cut:]
                    return
                key = m.group(1).lower().strip()
                self._buf = self._buf[m.end():]
                if key in self._valid:
                    self._active_key = key
                    self._active_buf = []
                    self.events.append({"type": "section_open", "key": key})
                # else: unknown key — skip it and keep scanning
            else:
                m = _SECTION_CLOSE_RE.search(self._buf)
                if not m:
                    # No close marker yet — flush most of the buffer into the
                    # section body, but keep a tail (32 chars) in case the
                    # delimiter is split across feeds.
                    if len(self._buf) > 32:
                        body = self._buf[:-32]
                        self._active_buf.append(body)
                        self._buf = self._buf[-32:]
                    return
                body = self._buf[:m.start()]
                if body:
                    self._active_buf.append(body)
                content = "".join(self._active_buf).strip()
                key = self._active_key
                self._buf = self._buf[m.end():]
                self._active_key = None
                self._active_buf = []
                self.completed_keys.add(key)
                self.events.append({"type": "section_close", "key": key, "content": content})

    def finish(self) -> None:
        """Flush any in-flight section that never received a close delimiter."""
        if self._active_key is not None and self._active_buf:
            content = "".join(self._active_buf).strip()
            if content:
                self.events.append({
                    "type": "section_close",
                    "key": self._active_key,
                    "content": content,
                    "unterminated": True,
                })
                self.completed_keys.add(self._active_key)
            self._active_key = None
            self._active_buf = []


async def _build_shared_srs_system_prompt(
    proj: dict, project_id: str, full_toon: str, summary: str,
    convo_text: str, analysis_digest: str,
) -> str:
    """Iter-13.41 — assemble the SHARED prefix used by the batched SRS call.

    Reuses every context source the per-section generator pulls (governance
    bundle, TOON, graph subgraph, RAG union, deep analysis, legacy fs
    index) but in their *broadest* form so the single call has enough
    context to write every section without re-querying.
    """
    # ── Governance bundle (compliance preamble) ──
    compliance_preamble = ""
    try:
        compliance_preamble = await _load_governance_bundle(project_id, proj.get("name", ""))
    except Exception:  # noqa: BLE001
        compliance_preamble = ""

    # ── TOON: union of every section's focus + a big global slice ──
    focus_slices: list[str] = []
    seen_focus: set[str] = set()
    for cfg in SECTION_CONFIGS:
        focus = cfg.get("toon_focus") or ""
        if focus and focus not in seen_focus:
            seen_focus.add(focus)
            sl = extract_toon_section(full_toon, focus, 6000)
            if sl.strip():
                focus_slices.append(f"## TOON · {focus}\n{sl}")
    toon_combined = "\n\n".join(focus_slices) if focus_slices else full_toon[:20000]

    # ── RAG: union across all sections (deduped, capped) ──
    rag_chunks: list[str] = []
    seen_chunks: set[str] = set()
    if project_id:
        for cfg in SECTION_CONFIGS:
            if cfg["key"] in _BATCH_SKIP_KEYS:
                continue
            try:
                facets = (SECTION_QUERIES.get(cfg["key"], cfg["label"]) or "").split()
                q = " ".join(facets[:6]) or cfg["label"]
                cs = await qdrant_search(project_id, q, top_k=8)
            except Exception:  # noqa: BLE001
                cs = []
            for c in cs:
                key = (c or "")[:120]
                if key in seen_chunks:
                    continue
                seen_chunks.add(key)
                rag_chunks.append(c)
                if len(rag_chunks) >= 60:
                    break
            if len(rag_chunks) >= 60:
                break
    rag_block = (
        "SEMANTICALLY RELEVANT CODE & SCHEMA (deduped union across sections):\n\n"
        + "\n\n---\n\n".join(rag_chunks)
        if rag_chunks else ""
    )

    # ── Graph subgraph: union across all section seeds ──
    graph_block = ""
    try:
        from kb.graph_config import is_graph_kb_enabled
        if project_id and await is_graph_kb_enabled(project_id):
            from kb.graph_retriever import subgraph_yaml_for_section
            seen_seeds: set[str] = set()
            extras: list[str] = []
            for cfg in SECTION_CONFIGS:
                for s in (SECTION_QUERIES.get(cfg["key"], "") or "").split():
                    if s and s not in seen_seeds:
                        seen_seeds.add(s)
                        extras.append(s)
            # Use the heaviest section's profile to fetch a generous subgraph.
            graph_yaml = await subgraph_yaml_for_section(
                project_id,
                "specific_requirements",
                extra_seeds=extras[:80],
                max_chars=9000,
                hops=2,
                max_nodes=220,
            )
            if graph_yaml:
                graph_block = (
                    "\n\nGRAPH SUBGRAPH (authoritative entity/relation skeleton — "
                    "cite these names VERBATIM, follow the declared relations):\n"
                    + graph_yaml
                    + "\n\nGRAPH COVERAGE MANDATE (iter-13.75 — applies to the "
                    "batched union of sections below): every Table / Route / "
                    "Method / Role / BusinessEntity node above must be "
                    "cited verbatim in WHICHEVER section catalogue scope "
                    "covers it. Out-of-scope nodes belong to a different "
                    "section, not silently dropped. Thin-evidence nodes get "
                    "`confidence=NOT_EVIDENCED` with an inline `⚠ EVIDENCE GAP` "
                    "marker so the confidence engine can score the gap."
                )
    except Exception:  # noqa: BLE001
        graph_block = ""

    kb_block = f"STRUCTURAL SKELETON (TOON — union of focuses):\n{toon_combined}"
    if rag_block:
        kb_block += "\n\n" + rag_block
    if graph_block:
        kb_block += graph_block

    detected = proj.get("detected_tech") or {}
    detected_summary = detected.get("summary") or ""
    detected_frameworks = ", ".join(detected.get("frameworks") or []) or "(none detected)"
    detected_db = detected.get("database") or "(none detected)"

    analysis_block = ""
    # iter-13.95 — sanitize digest + convo before injection (batched
    # path mirror of _gen_one_section). See the helper's docstring for
    # the forensic rationale. iter-13.98 upgraded to path-suffix.
    allowed_bn_batched, allowed_suf_batched = await _load_allowed_path_index(project_id, proj)
    if analysis_digest and (allowed_bn_batched or allowed_suf_batched):
        analysis_digest, _nrd = _redact_foreign_paths(
            analysis_digest, allowed_bn_batched,
            allowed_path_suffixes=allowed_suf_batched,
        )
        if _nrd:
            logger.warning(
                "SRS[%s] · batched · analysis_digest: redacted %d foreign path(s)",
                project_id, _nrd,
            )
    if convo_text and (allowed_bn_batched or allowed_suf_batched):
        convo_text, _ncv = _redact_foreign_paths(
            convo_text, allowed_bn_batched,
            allowed_path_suffixes=allowed_suf_batched,
        )
        if _ncv:
            logger.warning(
                "SRS[%s] · batched · convo_text: redacted %d foreign path(s)",
                project_id, _ncv,
            )
    if summary and (allowed_bn_batched or allowed_suf_batched):
        summary, _nsm = _redact_foreign_paths(
            summary, allowed_bn_batched,
            allowed_path_suffixes=allowed_suf_batched,
        )
        if _nsm:
            logger.warning(
                "SRS[%s] · batched · summary: redacted %d foreign path(s)",
                project_id, _nsm,
            )
    if analysis_digest:
        analysis_block = (
            "DEEP LEGACY-LOGIC ANALYSIS (authoritative pre-pass — cite IDs verbatim, "
            "do NOT re-derive workflows / rules / integrations):\n\n"
            + analysis_digest
        )

    # ── Legacy FS index (same as _gen_one_section, abbreviated) ──
    legacy_path = (proj.get("legacy_path") or "").strip()
    factory_cwd = ""
    factory_enabled = False
    try:
        _fset = ((proj.get("settings") or {}).get("factory_orchestrator") or {})
        if _fset.get("enabled"):
            factory_enabled = True
            factory_cwd = (_fset.get("cwd") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    legacy_fs_block = ""
    if legacy_path or factory_cwd:
        # ── iter-13.89 forensic fix (batched path) ─────────────────────
        # Mirror the per-section hardening: refuse to build the block
        # when project_id is empty, never let the filter degrade to `{}`
        # (which used to return EVERY tenant's files), and add a
        # defence-in-depth tenant_id constraint.
        file_rows: list[str] = []
        if project_id:
            try:
                from db import kb_files as _kbf
                _tid = (proj.get("tenant_id") or "").strip()
                _kbf_filter: dict = {"project_id": project_id}
                if _tid:
                    _kbf_filter["tenant_id"] = _tid
                cursor = _kbf.find(
                    _kbf_filter,
                    {"_id": 0, "filename": 1, "filetype": 1, "size": 1},
                ).sort("size", -1).limit(80)
                async for f in cursor:
                    fname = (f.get("filename") or "").strip()
                    if fname:
                        file_rows.append(f"- {fname}  ({f.get('filetype', '')}, {f.get('size', 0)} B)")
            except Exception:  # noqa: BLE001
                file_rows = []
        index_md = "\n".join(file_rows[:80])
        if len(index_md) > 6000:
            index_md = index_md[:6000] + "\n- … (truncated)"
        droid_hint = ""
        if factory_enabled and factory_cwd:
            # iter-13.94 — mirror of _gen_one_section: shell access is
            # ALLOWED only against paths that appear verbatim in the
            # scope-checked LEGACY FILE INDEX. Discovery walkers
            # (`ls`/`find`/`tree`) are FORBIDDEN — they are the recurring
            # leak vector that surfaces `src/com/ahct/*` etc. from
            # unrelated past sessions on the shared Droid filesystem.
            droid_hint = (
                "FACTORY DROID FILESYSTEM ACCESS (HARD-BOUNDED):\n"
                f"  • cwd: `{factory_cwd}` — shared across LAMA sessions; sibling/stale source may live around it.\n"
                "  • You MAY `cat`/`grep -n`/`sed -n` ONLY files whose path (relative to cwd) is\n"
                "    listed VERBATIM in the LEGACY FILE INDEX below. Any other path is FOREIGN.\n"
                "  • FORBIDDEN: `ls`, `find`, `tree`, `fd`, glob expansion, directory walks.\n"
                "  • If the index lacks evidence for a claim, mark it as a gap — do NOT hunt for it.\n"
            )
        elif factory_enabled and not factory_cwd:
            # iter-13.92 — see _gen_one_section for the full forensic
            # context. TL;DR: without this prohibition the Droid `cat`s
            # whatever stale source happens to live in its home and
            # produces cross-project file-path leakage.
            droid_hint = (
                "FACTORY DROID FILESYSTEM — DO NOT READ:\n"
                "  • This project's source is NOT on the Droid's local filesystem.\n"
                "  • DO NOT use `cat`/`ls`/`grep`/`find`/`sed`/`head`/`tail` — the Droid\n"
                "    may contain UNRELATED source from previous sessions. Reading it\n"
                "    will leak foreign filenames into the SRS.\n"
                "  • The LEGACY FILE INDEX and KB below are your SOLE authoritative\n"
                "    sources for filenames. Anything else is a hallucination.\n"
            )
        legacy_fs_block = (
            "LEGACY CODEBASE LOCATION:\n"
            f"  • Server-side scan path: `{legacy_path or '(not recorded)'}`\n"
            + (f"  • Factory Droid cwd: `{factory_cwd}`\n" if factory_cwd else "")
            + "\n"
            + (droid_hint + "\n" if droid_hint else "")
            + "LEGACY FILE INDEX (top files by size):\n"
            + (index_md or "(no files indexed)")
        )

    # iter-13.69 — append user-tagged source-material inventory (batched gen).
    try:
        from routes.kb import get_source_inventory_block
        inv_block = await get_source_inventory_block(project_id) if project_id else ""
        if inv_block:
            legacy_fs_block = (legacy_fs_block + "\n\n" + inv_block).strip()
    except Exception:  # noqa: BLE001
        pass

    # iter-13.93 — inline virtual workspace (batched path mirror of
    # _gen_one_section). See the helper's docstring for the full rationale.
    # iter-13.93 — inline virtual workspace (batched path mirror of
    # _gen_one_section). See the helper's docstring for the full rationale.
    # iter-13.99 — REPLACE (not append to) legacy_fs_block when workspace
    # is present, so the model only sees the isolated workspace prefix.
    try:
        workspace_block = await _build_legacy_workspace_block(project_id, proj)
        if workspace_block:
            legacy_fs_block = workspace_block.strip()
            logger.info(
                "SRS[%s] · batched · iter-13.99 ISOLATED WORKSPACE active — "
                "LEGACY CODEBASE LOCATION + FACTORY hints suppressed.",
                project_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRS[%s] batched workspace build failed (non-fatal): %s",
                       project_id, exc)

    return f"""{compliance_preamble}You are a **Senior Software Requirements Analyst + Domain Compliance Specialist** writing a professional IEEE 830 / IEEE 29148 SRS for a legacy-application modernization. This is a BATCHED RUN — you will produce EVERY section in this single response.

══════════════════════════════════════════════════════════════════════
SRS AUTHORING CHARTER (HARD RULES)
══════════════════════════════════════════════════════════════════════
truth_rule_mode: SOURCE_CODE_AND_EXISTING_SRS_ONLY
source_of_truth (priority order):
  1. Legacy source code visible in KNOWLEDGE BASE + LEGACY FILE INDEX.
  2. Database artefacts (tables, procedures, functions, triggers).
  3. The DEEP LEGACY-LOGIC ANALYSIS digest (if present).
Anything else is OUT OF SCOPE.

logic_rules:
  • Not in source / SRS → mark `OUT_OF_SCOPE` (no invented behaviour).
  • Doc vs implementation conflict → IMPLEMENTATION WINS.
  • DB column / API / UI detail not explicit → `NOT_EVIDENCED`.

tech_stack_neutrality:
  • Requirements + narratives are in plain BUSINESS LANGUAGE.
  • Framework / language / runtime names belong ONLY in the DETECTED
    LEGACY STACK header and source-citation columns.

human_readability_contract:
  • FR-* : "The system shall …" form, one behaviour per line.
  • BR-* : "shall / shall not / must" form, with source object.
  • UC-* : opens with NARRATIVE (user story); structured fields after.
  • Every UC MUST include Preconditions / Postconditions / Business
    Workflow / Business Rules.

PROJECT: {proj.get('name', '')}
SOURCE STACK (declared seed — HINT ONLY): {proj.get('source_tech', '')}
SOURCE STACK (detected — AUTHORITATIVE, use VERBATIM):
  summary:    {detected_summary or '(no detection — fall back to declared)'}
  frameworks: {detected_frameworks}
  database:   {detected_db}
TARGET STACK: {proj.get('target_tech', '')}
KB SUMMARY: {summary}

CONVERSATION CONTEXT:
{convo_text}

{legacy_fs_block}

KNOWLEDGE BASE:
{kb_block}

{analysis_block}

FORMAT (universal across all sections):
- Markdown only. Use `## X.Y` for sub-sections; `###` for further depth.
- Tables: standard pipe syntax with header separator.
- No JSON, no XML, no markdown fences around section bodies.
- Use REAL class / table / route / column names from the KB above (verbatim).
- Cite source as `relative/path.ext::symbol` or `relative/path.ext:LINE`.
- Mark gaps with `> ⚠ EVIDENCE GAP: <one line>` rather than inventing.
"""


async def _run_batch_srs(
    project_id: str,
    proj: dict,
    full_toon: str,
    summary: str,
    convo_text: str,
    analysis_digest: str,
    model: str,
    broadcast,
    control: dict,
):
    """Iter-13.41 — Single LLM call that streams ALL sections inline.

    Returns ``(sections_dict, total_tokens, repair_keys)`` where
    ``repair_keys`` lists sections that came back too short or missing
    (caller queues them through the existing _gen_one_section path).
    """
    from llm import fabric_call_stream
    sys_prompt = await _build_shared_srs_system_prompt(
        proj, project_id, full_toon, summary, convo_text, analysis_digest,
    )
    user_prompt, included = _build_batch_instructions()

    # Announce the batch phase up-front so the UI can show "Single-call
    # batched generation (saves ~85% of input tokens)…".
    await broadcast({
        "type": "batch_start",
        "total": len(included),
        "section_keys": [c["key"] for c in included],
        "model_requested": model or "",
    })

    valid_keys = {c["key"] for c in included}
    parser = _SectionStreamParser(valid_keys)
    sections: dict[str, str] = {}
    # Approximate total output for the heaviest plausible run (Sections 4+5+6
    # alone need >6000 words → ~24K tokens; cap is the floor of "everything").
    max_out = 48000

    started_at = time.time()
    last_progress_ts = started_at

    try:
        async for piece in fabric_call_stream(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            agent_key=(get_current_agent_key() or "srs.generate"),  # iter-13.81.3 — honour Console discovery.regenerate bucket on re-runs
            project_id=project_id,
            model_override=model or "",
            max_tokens=max_out,
            temperature=0.4,
            timeout=900.0,
        ):
            parser.feed(piece)
            # Drain emitted events (open + close) for downstream side-effects.
            while parser.events:
                ev = parser.events.pop(0)
                if ev["type"] == "section_open":
                    cfg = next((c for c in included if c["key"] == ev["key"]), None)
                    if cfg:
                        await broadcast({
                            "type": "section_start",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "index": included.index(cfg) + 1,
                            "total": len(included),
                            "batched": True,
                        })
                elif ev["type"] == "section_close":
                    key = ev["key"]
                    content = ev.get("content", "")
                    cfg = next((c for c in included if c["key"] == key), None)
                    if cfg and content:
                        sections[key] = content
                        # Persist immediately so a mid-stream crash still leaves
                        # this section behind.
                        try:
                            await srs_documents.update_one(
                                {"project_id": project_id},
                                {"$set": {
                                    "project_id": project_id,
                                    f"sections.{key}": content,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }},
                                upsert=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("SRS batch persist of %s failed: %s", key, exc)
                        await broadcast({
                            "type": "section_complete",
                            "section": key,
                            "label": cfg["label"],
                            "index": included.index(cfg) + 1,
                            "total": len(included),
                            "content": content,
                            "tokens": 0,  # accounted at the call level
                            "model_requested": model or "",
                            "model_used": model or "",
                            "batched": True,
                            "unterminated": ev.get("unterminated", False),
                        })

            # Periodic heartbeat (every 4 s) so SSE proxies stay happy
            # even when the LLM streams in long bursts.
            now_ts = time.time()
            if now_ts - last_progress_ts >= 4.0:
                last_progress_ts = now_ts
                await broadcast({
                    "type": "batch_progress",
                    "elapsed_sec": int(now_ts - started_at),
                    "sections_done": len(sections),
                    "total": len(included),
                })

            # Honor user cancel.
            if control.get("cancelled"):
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRS batch stream failed (%s) — partial=%d sections", exc, len(sections))
        await broadcast({
            "type": "batch_error",
            "error": str(exc)[:240],
            "partial": len(sections),
        })

    parser.finish()
    while parser.events:
        ev = parser.events.pop(0)
        if ev["type"] == "section_close":
            key = ev["key"]
            content = ev.get("content", "")
            cfg = next((c for c in included if c["key"] == key), None)
            if cfg and content and key not in sections:
                sections[key] = content
                try:
                    await srs_documents.update_one(
                        {"project_id": project_id},
                        {"$set": {f"sections.{key}": content,
                                  "updated_at": datetime.now(timezone.utc).isoformat()}},
                        upsert=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                await broadcast({
                    "type": "section_complete",
                    "section": key, "label": cfg["label"],
                    "index": included.index(cfg) + 1, "total": len(included),
                    "content": content, "tokens": 0,
                    "model_requested": model or "", "model_used": model or "",
                    "batched": True,
                    "unterminated": True,
                })

    # Identify sections that didn't come through (or came through too short)
    # so the caller can repair them via the per-section path.
    repair_keys: list[str] = []
    for cfg in included:
        body = sections.get(cfg["key"]) or ""
        if len(body.strip()) < MIN_BATCH_SECTION_CHARS:
            repair_keys.append(cfg["key"])

    await broadcast({
        "type": "batch_complete",
        "sections_done": len(sections),
        "sections_repair": repair_keys,
        "elapsed_sec": int(time.time() - started_at),
    })

    return sections, 0, repair_keys


async def _run_batch_srs_subset(
    project_id: str,
    proj: dict,
    full_toon: str,
    summary: str,
    convo_text: str,
    analysis_digest: str,
    model: str,
    subset_keys: list[str],
    broadcast,
    control: dict,
) -> tuple[dict, int, list[str]]:
    """iter-13.43 — Second batched pass that regenerates ONLY the missing
    sections, sharing the same expensive system prompt as the primary call.

    Replaces the previous per-section repair loop (which rebuilt
    governance + TOON + RAG + graph + analysis for EACH missing section).
    Now: one extra LLM call total, regardless of how many sections need
    repair, with the same context the primary batched call used.

    Returns (sections, tokens, still_missing).
    """
    from llm import fabric_call_stream
    subset_keys = [k for k in subset_keys if k not in _BATCH_SKIP_KEYS]
    if not subset_keys:
        return {}, 0, []

    sys_prompt = await _build_shared_srs_system_prompt(
        proj, project_id, full_toon, summary, convo_text, analysis_digest,
    )
    user_prompt, included = _build_batch_instructions_subset(subset_keys)
    if not included:
        return {}, 0, []

    await broadcast({
        "type": "batch_repair_start",
        "subset_keys": [c["key"] for c in included],
        "total": len(included),
        "model_requested": model or "",
    })

    valid_keys = {c["key"] for c in included}
    parser = _SectionStreamParser(valid_keys)
    sections: dict[str, str] = {}
    # Roughly proportional to the subset size — heavy sections need room.
    max_out = max(8000, min(48000, 6000 * len(included)))

    started_at = time.time()
    last_progress_ts = started_at

    try:
        async for piece in fabric_call_stream(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            agent_key=(get_current_agent_key() or "srs.generate"),  # iter-13.81.3 — honour Console discovery.regenerate bucket on re-runs
            project_id=project_id,
            model_override=model or "",
            max_tokens=max_out,
            temperature=0.4,
            timeout=900.0,
        ):
            parser.feed(piece)
            while parser.events:
                ev = parser.events.pop(0)
                if ev["type"] == "section_open":
                    cfg = next((c for c in included if c["key"] == ev["key"]), None)
                    if cfg:
                        await broadcast({
                            "type": "section_start",
                            "section": cfg["key"], "label": cfg["label"],
                            "index": SECTION_CONFIGS.index(cfg) + 1,
                            "total": len(SECTION_CONFIGS),
                            "batched": True, "repair": True,
                        })
                elif ev["type"] == "section_close":
                    key = ev["key"]
                    content = ev.get("content", "")
                    cfg = next((c for c in included if c["key"] == key), None)
                    if cfg and content:
                        sections[key] = content
                        try:
                            await srs_documents.update_one(
                                {"project_id": project_id},
                                {"$set": {
                                    "project_id": project_id,
                                    f"sections.{key}": content,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }},
                                upsert=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("SRS batch-repair persist of %s failed: %s", key, exc)
                        await broadcast({
                            "type": "section_complete",
                            "section": key, "label": cfg["label"],
                            "index": SECTION_CONFIGS.index(cfg) + 1,
                            "total": len(SECTION_CONFIGS),
                            "content": content, "tokens": 0,
                            "model_requested": model or "",
                            "model_used": model or "",
                            "batched": True, "repaired": True,
                            "unterminated": ev.get("unterminated", False),
                        })
            now_ts = time.time()
            if now_ts - last_progress_ts >= 4.0:
                last_progress_ts = now_ts
                await broadcast({
                    "type": "batch_repair_progress",
                    "elapsed_sec": int(now_ts - started_at),
                    "sections_done": len(sections),
                    "total": len(included),
                })
            if control.get("cancelled"):
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRS batch-repair stream failed (%s) — partial=%d sections",
                       exc, len(sections))
        await broadcast({
            "type": "batch_repair_error",
            "error": str(exc)[:240],
            "partial": len(sections),
        })

    parser.finish()
    while parser.events:
        ev = parser.events.pop(0)
        if ev["type"] == "section_close":
            key = ev["key"]
            content = ev.get("content", "")
            cfg = next((c for c in included if c["key"] == key), None)
            if cfg and content and key not in sections:
                sections[key] = content
                try:
                    await srs_documents.update_one(
                        {"project_id": project_id},
                        {"$set": {f"sections.{key}": content,
                                  "updated_at": datetime.now(timezone.utc).isoformat()}},
                        upsert=True,
                    )
                except Exception:  # noqa: BLE001
                    pass

    still_missing = [
        c["key"] for c in included
        if len((sections.get(c["key"]) or "").strip()) < MIN_BATCH_SECTION_CHARS
    ]
    await broadcast({
        "type": "batch_repair_complete",
        "sections_done": len(sections),
        "still_missing": still_missing,
        "elapsed_sec": int(time.time() - started_at),
    })
    return sections, 0, still_missing


@router.post("/generate/stream")
async def generate_srs_stream(payload: dict):
    """SSE streaming generation. Emits section_start / section_complete / complete events.

    IMPORTANT (iter-13.11 fix): the actual section-by-section generation runs as a
    **detached background asyncio task** that is NOT bound to the HTTP request's
    lifetime. The SSE generator only relays progress events from a queue.

    Why: previously the whole pipeline lived inside the request's async generator,
    so any client disconnect (proxy/ingress timeout, mobile network blip, browser
    tab backgrounded, user navigating away) cancelled the generator and stopped
    generation mid-document — exactly the "stops after section 1/2/3" symptom.
    Now the background task keeps writing each section to MongoDB regardless, so
    the user always ends up with a complete SRS even if the live progress stream
    drops. A subsequent GET /srs/{project_id} (which the SRSPanel calls on
    mount + on the `complete` event) will fetch the full document.
    """
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    if not project_id:
        raise HTTPException(400, "project_id required")
    # iter-13.81.3 — re-run detection so Console's `discovery.regenerate`
    # bucket model pin actually reaches Factory.ai on full re-runs.
    _existing_srs = await srs_documents.find_one({"project_id": project_id}, {"_id": 1})
    set_current_agent_key("srs.regenerate" if _existing_srs else "srs.generate")
    conversation_id = payload.get("conversation_id")
    # Same as /generate — Console resolves via AGENT_COMPLEXITY["srs.generate"].
    model = payload.get("model") or ""

    # iter-14.5 — RESUME MODE. When the client sends `resume: true`, the
    # existing SRS document is pre-loaded and only sections that are
    # missing or failed (`_is_failed_section`) are regenerated. All
    # already-good sections are preserved verbatim. Forces per-section
    # mode because batch mode would re-generate the whole doc in one
    # LLM call, defeating the point. The client "Resume" button in
    # SRSPanel.jsx uses this flag.
    resume_mode = bool(payload.get("resume"))
    if resume_mode:
        # Explicit user intent — override batch default.
        payload["mode"] = "per-section"

    # iter-13.41 — batched mode. ONE LLM call streams every section delimited
    # by `<<<SECTION:key>>>`. Saves ~85% of input tokens because the shared
    # KB / TOON / graph subgraph / governance preamble is sent ONCE instead
    # of 11 times. The graph subgraph (when graph-KB is enabled for the
    # project) AND the deduped RAG union AND the deep-legacy-analysis digest
    # are all baked into the shared system prompt — so accuracy goes UP
    # while tokens go down.
    #
    # iter-13.42 — DEFAULT FLIPPED TO ON. Resolution order:
    #   1. Explicit payload `mode` / `batch` field wins.
    #   2. Env var `LAMA_SRS_BATCH` — only "0/false/no/off" disables it.
    #   3. Default = batched.
    # Old behaviour (per-section, 11 LLM calls) is still reachable via
    # `payload={"mode":"per-section"}` or `LAMA_SRS_BATCH=0`.
    _env_batch_raw = (os.environ.get("LAMA_SRS_BATCH", "") or "").strip().lower()
    _env_batch_off = _env_batch_raw in {"0", "false", "no", "off"}
    if "mode" in payload or "batch" in payload:
        _mode_val = (payload.get("mode") or "").lower()
        batch_mode = (
            _mode_val == "batch"
            or bool(payload.get("batch"))
        )
        # explicit per-section opt-out
        if _mode_val in {"per-section", "per_section", "sequential"}:
            batch_mode = False
    elif _env_batch_off:
        batch_mode = False
    else:
        batch_mode = True

    # iter-13.78 — STREAM-CAPABILITY GUARD.
    # Batch mode assumes the LLM call streams sections progressively (the
    # `<<<END_SECTION:key>>>` delimiter fires section_complete events
    # mid-stream). When the active provider doesn't support OpenAI-style
    # SSE (Anthropic native today, Factory.ai always), fabric_call_stream
    # falls back to a BUFFERED single yield — every section then arrives
    # in one burst at the end of a 60-120s wait, defeating the whole
    # "stage by stage" UX. Auto-downgrade to per-section mode in that
    # case so each section becomes its own short LLM call and lands in
    # the UI as soon as it completes (~5-15s each).
    # Honours explicit `mode`/`batch` payload + LAMA_SRS_BATCH env-var:
    # those are user-intent and never get overridden by the probe.
    _user_set_mode = ("mode" in payload) or ("batch" in payload) or _env_batch_off
    if batch_mode and not _user_set_mode:
        try:
            from llm import fabric_stream_supported
            if not await fabric_stream_supported(project_id):
                batch_mode = False
                logger.info(
                    "SRS: streaming unsupported by active provider for pid=%s — "
                    "auto-downgraded to per-section mode for progressive UX.",
                    project_id,
                )
        except Exception:  # noqa: BLE001
            pass

    # iter-13.34 fix — clear any stale provider pin on the srs.generate agent
    # before kicking off a regeneration. `fabric_chat_with_failover` pins the
    # agent to whichever provider last succeeded so the SAME SRS batch reuses
    # it without re-paying the failover tax. But that pin survives across
    # runs — so if the user reconfigured Console (added a funded Anthropic
    # key, retired DeepSeek, etc.) every Regenerate kept hitting the old
    # provider first and surfacing the same 402 / credits error. Wiping the
    # pin here forces a fresh resolve_model() against the current Console
    # default on every regen click.
    try:
        from db import agent_configs as _ac
        await _ac.update_one(
            {"key": "srs.generate"},
            {"$set": {"provider_id": ""}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass

    proj, existing, full_toon, summary, convo_text, analysis_digest = await _load_srs_context(project_id, conversation_id)

    # If a generation is already running for this project, reattach to its
    # event stream instead of launching a second one (which would race on
    # srs_documents writes and waste tokens).
    running = _SRS_JOBS.get(project_id)
    if running and not running["task"].done():
        queue: asyncio.Queue = asyncio.Queue()
        running["listeners"].append(queue)
        # Replay any events that were emitted before we attached so the late
        # listener sees the full picture (start + already-completed sections).
        for evt in running["history"]:
            await queue.put(evt)
    else:
        queue = asyncio.Queue()
        history: list[dict] = []
        listeners: list[asyncio.Queue] = [queue]
        # iter-13.35 — shared control dict (closure + _SRS_JOBS entry).
        # Endpoints flip these flags; the loop checks between sections.
        control: dict[str, bool] = {"paused": False, "cancelled": False}

        async def _broadcast(evt: dict):
            history.append(evt)
            # Cap history so a very long run doesn't balloon memory.
            if len(history) > 200:
                del history[: len(history) - 200]
            dead: list[asyncio.Queue] = []
            for q in listeners:
                try:
                    q.put_nowait(evt)
                except Exception:  # noqa: BLE001
                    dead.append(q)
            for d in dead:
                try:
                    listeners.remove(d)
                except ValueError:
                    pass

        async def _run_job():
            sections: dict[str, str] = {}
            # iter-14.11 — per-section retry telemetry captured by the
            # score-gated auto-retry loop inside `_run_one_section`.
            # Persisted onto srs_documents.sections_meta.<key> after every
            # section so the UI can render color-coded backgrounds even if
            # the run is aborted or the SSE connection drops.
            section_meta: dict[str, dict] = {}
            total_tokens = 0
            # iter-13.90 — Pre-load `existing` so BOTH branches (batch_mode
            # and per-section) can pass it to `_persist_srs`. Without this
            # the batch_mode branch at L~3841 raised UnboundLocalError
            # because `existing` was only assigned inside the per-section
            # path below (L~4050), crashing every batched SRS run.
            existing: dict | None = None
            try:
                existing = await srs_documents.find_one(
                    {"project_id": project_id}, {"_id": 0, "version": 1}
                )
            except Exception:  # noqa: BLE001
                existing = None

            # iter-14.5 — RESUME. Pre-populate `sections` from the stored
            # SRS so the per-section loop below can skip anything already
            # good and only regenerate missing/failed slots.
            resume_skip_keys: set[str] = set()
            if resume_mode:
                try:
                    prior_doc = await srs_documents.find_one(
                        {"project_id": project_id},
                        {"_id": 0, "sections": 1},
                    )
                    prior = (prior_doc or {}).get("sections") or {}
                    for _k, _v in prior.items():
                        if isinstance(_v, str) and not _is_failed_section(_v):
                            sections[_k] = _v
                            resume_skip_keys.add(_k)
                    logger.info(
                        "SRS[%s] · RESUME mode — preserving %d good sections, will regenerate the rest",
                        project_id, len(resume_skip_keys),
                    )
                except Exception as _exc:  # noqa: BLE001
                    logger.warning("SRS resume pre-load failed for %s: %s", project_id, _exc)
                    resume_skip_keys = set()
            try:
                # iter-13.34 — surface Graphify availability up-front so the
                # user can see whether SRS will consult the graph subgraph
                # for each section. Three signals:
                #   • graph_enabled — per-project + env-default toggle
                #   • graph_built   — kb_graph collection has a doc for this pid
                #   • graph_enriched — kb.graphify pass added LLM nodes/edges
                # The frontend turns this into a "Graphify KB: ON · N nodes
                # enriched" badge on the SRS progress strip.
                graph_meta: dict = {
                    "enabled": False, "built": False, "enriched": False,
                    "nodes_count": 0, "edges_count": 0, "graphify_nodes_added": 0,
                }
                try:
                    from kb.graph_config import is_graph_kb_enabled
                    from db import kb_graph as _kb_graph_col
                    graph_meta["enabled"] = await is_graph_kb_enabled(project_id)
                    if graph_meta["enabled"]:
                        # iter-13.74 — kb_graph is now sharded across
                        # {meta, nodes×N, edges×N} docs to dodge MongoDB's
                        # 16 MB BSON cap. Read counts from the meta shard
                        # (or fall through to the legacy monolithic doc).
                        gdoc = await _kb_graph_col.find_one(
                            {"project_id": project_id, "kind": "meta"},
                            {"_id": 0, "stats": 1},
                        ) or await _kb_graph_col.find_one(
                            {"project_id": project_id, "kind": {"$exists": False}},
                            {"_id": 0, "stats": 1, "nodes": 1, "edges": 1, "graphify": 1},
                        )
                        if gdoc:
                            graph_meta["built"] = True
                            _stats = gdoc.get("stats") or {}
                            # Sharded: counts live in stats. Legacy: counts
                            # may live at top level inside nodes/edges arrays.
                            graph_meta["nodes_count"] = int(
                                _stats.get("nodes_count")
                                if _stats.get("nodes_count") is not None
                                else len(gdoc.get("nodes") or [])
                            )
                            graph_meta["edges_count"] = int(
                                _stats.get("edges_count")
                                if _stats.get("edges_count") is not None
                                else len(gdoc.get("edges") or [])
                            )
                            grfy = _stats.get("graphify") or gdoc.get("graphify") or {}
                            graph_meta["graphify_nodes_added"] = grfy.get("added_nodes", grfy.get("nodes_added", 0))
                            _added = grfy.get("added_nodes", grfy.get("nodes_added", 0)) + \
                                     grfy.get("added_edges", grfy.get("edges_added", 0))
                            graph_meta["enriched"] = _added > 0
                except Exception:  # noqa: BLE001
                    pass

                await _broadcast({
                    "type": "start",
                    "total": len(SECTION_CONFIGS),
                    "project": proj.get("name", ""),
                    "graph_meta": graph_meta,
                    "mode": ("batch" if batch_mode else "per-section"),
                })

                # ── iter-13.41 — BATCH MODE branch ──────────────────────
                # Single LLM call streams every section delimited by
                # `<<<SECTION:key>>>...<<<END_SECTION:key>>>`. Saves
                # ~85% of input tokens by sending the shared KB / TOON /
                # graph / governance preamble exactly ONCE. The same
                # `section_complete` SSE events fire as in per-section
                # mode, so the SRSPanel renders progressively.
                # Sections that come back short (or missing entirely) are
                # repaired through the existing _gen_one_section path
                # below — re-using all the per-section retry safety net.
                if batch_mode:
                    batched_sections, _batched_tokens, repair_keys = await _run_batch_srs(
                        project_id=project_id, proj=proj, full_toon=full_toon,
                        summary=summary, convo_text=convo_text,
                        analysis_digest=analysis_digest, model=model,
                        broadcast=_broadcast, control=control,
                    )
                    sections.update(batched_sections)
                    # Section 9 (entity_model) is always deterministic — generate it now.
                    try:
                        em = await _gen_entity_model(project_id)
                        sections["entity_model"] = em.get("content", "")
                        try:
                            await srs_documents.update_one(
                                {"project_id": project_id},
                                {"$set": {"sections.entity_model": sections["entity_model"],
                                          "updated_at": datetime.now(timezone.utc).isoformat()}},
                                upsert=True,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        em_cfg = next((c for c in SECTION_CONFIGS if c["key"] == "entity_model"), None)
                        if em_cfg:
                            await _broadcast({
                                "type": "section_complete",
                                "section": "entity_model", "label": em_cfg["label"],
                                "index": SECTION_CONFIGS.index(em_cfg) + 1,
                                "total": len(SECTION_CONFIGS),
                                "content": sections["entity_model"], "tokens": 0,
                                "model_requested": model or "", "model_used": "(deterministic)",
                                "batched": True,
                            })
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Entity-model gen failed in batch: %s", exc)

                    # Repair short / missing sections.
                    # iter-13.43 — first try a SECOND batched call that
                    # regenerates the missing subset in ONE shot, reusing
                    # the shared system prompt. Only if that still leaves
                    # gaps do we fall through to the per-section path.
                    # This eliminates the "every section re-scans the
                    # legacy code" symptom users reported.
                    if repair_keys and not control.get("cancelled"):
                        try:
                            repaired_subset, _subset_tokens, repair_keys = await _run_batch_srs_subset(
                                project_id=project_id, proj=proj,
                                full_toon=full_toon, summary=summary,
                                convo_text=convo_text,
                                analysis_digest=analysis_digest, model=model,
                                subset_keys=repair_keys,
                                broadcast=_broadcast, control=control,
                            )
                            sections.update(repaired_subset)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Batched repair pass failed (%s) — "
                                           "falling back to per-section repair", exc)

                    for rk in repair_keys:
                        if control.get("cancelled"):
                            break
                        cfg = next((c for c in SECTION_CONFIGS if c["key"] == rk), None)
                        if not cfg or cfg["key"] in _BATCH_SKIP_KEYS:
                            continue
                        await _broadcast({
                            "type": "section_repair",
                            "section": cfg["key"], "label": cfg["label"],
                            "reason": "below_min_chars_or_missing",
                        })
                        try:
                            r = await _gen_one_section(
                                cfg, proj, full_toon, summary, convo_text, model,
                                project_id=project_id, prior_sections=sections,
                                analysis_digest=analysis_digest, fast_mode=True,
                            )
                            sections[rk] = r.get("content", "")
                            total_tokens += r.get("tokens", 0)
                            try:
                                await srs_documents.update_one(
                                    {"project_id": project_id},
                                    {"$set": {f"sections.{rk}": sections[rk],
                                              "updated_at": datetime.now(timezone.utc).isoformat()}},
                                    upsert=True,
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            await _broadcast({
                                "type": "section_complete",
                                "section": rk, "label": cfg["label"],
                                "index": SECTION_CONFIGS.index(cfg) + 1,
                                "total": len(SECTION_CONFIGS),
                                "content": sections[rk],
                                "tokens": r.get("tokens", 0),
                                "model_requested": model or "",
                                "model_used": r.get("model_used", "") or model or "",
                                "repaired": True,
                            })
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Repair of %s failed: %s", rk, exc)

                    # Skip the per-section loop entirely — fall through to
                    # the post-pass revalidation/repair logic which still
                    # runs over the full `sections` dict.
                    version = await _persist_srs(project_id, existing, sections, total_tokens, model)
                    _bm_failed = [c["key"] for c in SECTION_CONFIGS
                                  if c["key"] != "entity_model"
                                  and _is_failed_section(sections.get(c["key"], ""))]
                    await _broadcast({
                        "type": "complete",
                        "version": version,
                        "total_tokens": total_tokens,
                        "sections_total": len(SECTION_CONFIGS),
                        "sections_ok": sum(1 for c in SECTION_CONFIGS if not _is_failed_section(sections.get(c["key"], ""))),
                        "sections_failed": _bm_failed,
                        # iter-13.90 — add `partial` flag (test contract;
                        # parity with per-section path at L~4257).
                        "partial": bool(_bm_failed),
                        "mode": "batch",
                    })
                    return

                # ─── iter-14.6 · A+C · wave-parallel per-section generation ───
                # Runs independent sections concurrently in "waves" so the UI
                # sees multiple section_start / section_complete events in
                # flight at once instead of waiting for section-N to finish
                # before section-N+1 even begins. Combined with the existing
                # section_progress 5s heartbeat (`_run_one_section` below),
                # this is the closest UX to token-streaming that the current
                # Factory.ai / droid transport can support (droid emits the
                # whole assistant message in one shot; there are no token
                # deltas to stream). See ChatPanel `factory-cli` inspection.
                #
                # TOKEN NEUTRAL — sections in the same wave share the same
                # `prior_sections` snapshot. Compared to sequential mode
                # (section N sees N-1 priors) each wave sees FEWER priors,
                # so the total input-token budget over a run is ≤ sequential.
                # Zero extra OUTPUT tokens (still 12 sections total).
                #
                # QUALITY GUARDS —
                #   * `_SOLO_KEYS` (currently just `detailed_use_cases`)
                #     always runs alone so its UC-ID enumeration sees the
                #     full prior-section set, matching sequential behaviour.
                #   * Waves execute strictly in SECTION_CONFIGS order, so
                #     dependency ordering is preserved.
                #
                # Configuration —
                #   * `LAMA_SRS_PARALLEL` env (default 3, clamped 1..6)
                #   * `payload["parallel"]` overrides (client-side)
                #   * `parallel=1` reproduces the old sequential behaviour.
                try:
                    _env_par = int(os.environ.get("LAMA_SRS_PARALLEL", "3") or "3")
                except (TypeError, ValueError):
                    _env_par = 3
                _pay_par = payload.get("parallel")
                if _pay_par is not None:
                    try:
                        _env_par = int(_pay_par)
                    except (TypeError, ValueError):
                        pass
                wave_size = max(1, min(_env_par, 6))
                _SOLO_KEYS = {"detailed_use_cases"}

                # Emit resumed sections up-front (parity with old loop).
                _wave_done: set[str] = set()
                if resume_mode and resume_skip_keys:
                    for _ri, _rcfg in enumerate(SECTION_CONFIGS, start=1):
                        if _rcfg["key"] not in resume_skip_keys:
                            continue
                        await _broadcast({
                            "type": "section_complete",
                            "section": _rcfg["key"],
                            "label": _rcfg["label"],
                            "index": _ri,
                            "total": len(SECTION_CONFIGS),
                            "content": sections.get(_rcfg["key"], ""),
                            "tokens": 0,
                            "model_requested": model or "",
                            "model_used": "",
                            "resumed": True,
                        })
                        _wave_done.add(_rcfg["key"])

                _pending_pairs: list[tuple[int, dict]] = [
                    (i, c) for i, c in enumerate(SECTION_CONFIGS, start=1)
                    if c["key"] not in _wave_done
                ]
                # Partition into waves. Solo keys always take their own wave.
                _waves: list[list[tuple[int, dict]]] = []
                _bucket: list[tuple[int, dict]] = []
                for _pair in _pending_pairs:
                    _, _c = _pair
                    if _c["key"] in _SOLO_KEYS:
                        if _bucket:
                            _waves.append(_bucket)
                            _bucket = []
                        _waves.append([_pair])
                    else:
                        _bucket.append(_pair)
                        if len(_bucket) >= wave_size:
                            _waves.append(_bucket)
                            _bucket = []
                if _bucket:
                    _waves.append(_bucket)

                logger.info(
                    "SRS[%s] · per-section wave plan: %d wave(s), sizes=%s (parallel=%d)",
                    project_id, len(_waves), [len(w) for w in _waves], wave_size,
                )

                _consec_fails = 0

                # ── iter-14.13 · Feed the per-section judge the same KB
                # digests the "Compute stage confidence" popover uses. The
                # prior implementation passed `kb_summary=""` and
                # `ground_truth=""` to `_score_section_now`, which forced
                # the evaluator to score BLIND — the artifact had no
                # anchor to compare against. Per the confidence-engine
                # rubric that produces either a rubric-fallback 100
                # ("kb_silent_on_topic") or middle-band conservatism
                # (80-88), so retries plateaued regardless of how good
                # the section actually was. Pre-computing the two digests
                # ONCE per run (outside the wave loop) keeps the token
                # cost bounded (~13k chars total, ~4k tokens) even when
                # every section triggers the max 3 retries.
                _eval_kb_summary = ""
                _eval_ground_truth = ""
                try:
                    from routes.living import (
                        _kb_summary_for_eval as _kbs,
                        _ground_truth_for_eval as _gts,
                    )
                    _eval_kb_summary = await _kbs(project_id)
                    _eval_ground_truth = await _gts(project_id)
                    logger.info(
                        "SRS[%s] · evaluator context ready · kb_summary=%d chars · ground_truth=%d chars",
                        project_id,
                        len(_eval_kb_summary or ""),
                        len(_eval_ground_truth or ""),
                    )
                    if not (_eval_kb_summary or _eval_ground_truth):
                        logger.warning(
                            "SRS[%s] · evaluator KB digests came back EMPTY — "
                            "the score-gated retry loop will run BLIND. Likely "
                            "root cause: KB not built (Discovery: click Build KB) "
                            "OR legacy_analysis not run (Discovery: click Analyze Legacy).",
                            project_id,
                        )
                except Exception as _exc:  # noqa: BLE001 — never block the run
                    logger.warning(
                        "SRS[%s] · could not build evaluator context (%s) — "
                        "falling back to blind scoring",
                        project_id, _exc,
                    )

                async def _run_one_section(_i: int, _cfg: dict, _prior_snapshot: dict):
                    """Run ONE section with a 5-second section_progress heartbeat,
                    then score-gate the result and auto-retry up to
                    MAX_AUTO_RETRIES times until confidence >= MIN_SECTION_CONFIDENCE.

                    Broadcasts section_start / section_progress / section_retry /
                    section_complete (or section_error). Persists incrementally.
                    Returns (i, cfg, r_dict, err_msg_or_None).
                    """
                    logger.info(
                        "SRS[%s] · starting section %d/%d (%s · %s · min_words=%d)",
                        project_id, _i, len(SECTION_CONFIGS),
                        _cfg["key"], _cfg["label"], _cfg.get("min_words", 0),
                    )
                    await _broadcast({
                        "type": "section_start",
                        "section": _cfg["key"],
                        "label": _cfg["label"],
                        "index": _i,
                        "total": len(SECTION_CONFIGS),
                    })

                    async def _gen_with_heartbeat(_prior_for_call: dict):
                        """Run one attempt of `_gen_one_section` with a 5s
                        `section_progress` heartbeat so the frontend + any
                        SSE proxy stays alive. Returns (_r_dict, _err_msg_or_None).
                        """
                        _section_task = asyncio.create_task(
                            _gen_one_section(
                                _cfg, proj, full_toon, summary, convo_text, model,
                                project_id=project_id,
                                prior_sections=_prior_for_call,
                                analysis_digest=analysis_digest,
                            )
                        )
                        while not _section_task.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(_section_task), timeout=5.0)
                            except asyncio.TimeoutError:
                                await _broadcast({
                                    "type": "section_progress",
                                    "section": _cfg["key"],
                                    "label": _cfg["label"],
                                    "index": _i,
                                    "total": len(SECTION_CONFIGS),
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception:  # noqa: BLE001 — unwrap below
                                break
                        _err_local: str | None = None
                        try:
                            _r_local = await _section_task
                        except Exception as _exc:  # noqa: BLE001
                            _err_local = str(_exc)[:240]
                            logger.warning("SRS section %s failed: %s", _cfg["key"], _err_local)
                            _r_local = {
                                "content": (
                                    f"> ⚠️ **Section generation failed** — `{_err_local}`\n\n"
                                    "The pipeline continued with the remaining sections. "
                                    "Click *Regenerate* on this section from the SRS panel "
                                    "once the underlying issue (provider credits / API key "
                                    "/ network) is resolved."
                                ),
                                "tokens": 0,
                                "error": "exception",
                            }
                        return _r_local, _err_local

                    # ---- iter-14.11 : score-gated retry loop ----
                    _prior_for_call = dict(_prior_snapshot)
                    _attempts_detail: list[dict] = []
                    _initial_score: float | None = None
                    _r: dict = {"content": "", "tokens": 0}
                    _err_msg: str | None = None
                    _judge: dict = {"score": 0.0, "band": "poor", "gaps": [],
                                    "rationale": "", "model": ""}
                    _max_attempts = (1 + MAX_AUTO_RETRIES) if AUTORETRY_ENABLED else 1
                    _attempt = 0
                    _total_tokens_this_section = 0
                    while _attempt < _max_attempts:
                        _attempt += 1
                        _r, _err_msg = await _gen_with_heartbeat(_prior_for_call)
                        _total_tokens_this_section += int(_r.get("tokens") or 0)
                        if _err_msg:
                            await _broadcast({
                                "type": "section_error",
                                "section": _cfg["key"],
                                "label": _cfg["label"],
                                "error": _err_msg,
                            })
                        # Only score real content — no point judging failure markers.
                        _judge = {"score": 0.0, "band": "poor", "gaps": [],
                                  "rationale": "", "model": ""}
                        if (AUTORETRY_ENABLED
                                and not _err_msg
                                and not _is_failed_section(_r.get("content", ""))):
                            _judge = await _score_section_now(
                                project_id, _cfg, _r.get("content", ""),
                                kb_summary=_eval_kb_summary,
                                ground_truth=_eval_ground_truth,
                            )
                        if _initial_score is None:
                            _initial_score = float(_judge.get("score", 0.0) or 0.0)
                        _prior_score = _attempts_detail[-1]["score"] if _attempts_detail else 0.0
                        _attempts_detail.append({
                            "attempt": _attempt,
                            "score": round(float(_judge.get("score", 0.0) or 0.0), 2),
                            "band": _judge.get("band", "poor"),
                            "delta_from_prior": (
                                round(float(_judge.get("score", 0.0) or 0.0) - _prior_score, 2)
                                if len(_attempts_detail) > 0 else 0.0
                            ),
                            "gaps": (_judge.get("gaps") or [])[:5],
                            "model_used": _r.get("model_used", "") or model,
                            "rationale": (_judge.get("rationale") or "")[:240],
                            "at": datetime.now(timezone.utc).isoformat(),
                        })
                        try:
                            await audit_log.insert_one({
                                "action": "srs.autoretry",
                                "project_id": project_id,
                                "at": datetime.now(timezone.utc).isoformat(),
                                "details": {
                                    "section": _cfg["key"],
                                    "attempt": _attempt,
                                    "max_attempts": _max_attempts,
                                    "score": _attempts_detail[-1]["score"],
                                    "prior_score": (
                                        round(_prior_score, 2)
                                        if len(_attempts_detail) > 1 else None
                                    ),
                                    "delta": _attempts_detail[-1]["delta_from_prior"],
                                    "min_confidence": MIN_SECTION_CONFIDENCE,
                                    "model_used": _attempts_detail[-1]["model_used"],
                                    "gaps": _attempts_detail[-1]["gaps"],
                                    "generation_error": _err_msg,
                                    # iter-14.13 — Distinguish scorer-context
                                    # gap (evaluator had no KB digests) from a
                                    # genuine evidence gap (evaluator had
                                    # digests but LLM section still missed).
                                    "context_missing": bool(_judge.get("context_missing")),
                                    "kb_evidence_hint": (
                                        "evaluator ran WITHOUT kb_summary/ground_truth — score is not authoritative; "
                                        "rebuild KB (Discovery → Build KB) or run legacy analysis to restore judge context"
                                        if _judge.get("context_missing")
                                        else (
                                            "plateau likely a KB evidence gap — not a prompt bug"
                                            if (len(_attempts_detail) >= 2
                                                and abs(_attempts_detail[-1]["delta_from_prior"]) < 1.0)
                                            else ""
                                        )
                                    ),
                                },
                            })
                        except Exception:  # noqa: BLE001
                            pass
                        # Exit conditions
                        if _err_msg or _is_failed_section(_r.get("content", "")):
                            break  # let the repair pass handle failures
                        if float(_judge.get("score", 0.0) or 0.0) >= MIN_SECTION_CONFIDENCE:
                            break
                        if _attempt >= _max_attempts:
                            break
                        # Retry — surface the intention to the UI and feed
                        # this attempt's content back so the DEBT LEDGER in
                        # `_gen_one_section` shows the LLM the gaps.
                        await _broadcast({
                            "type": "section_retry",
                            "section": _cfg["key"],
                            "label": _cfg["label"],
                            "index": _i,
                            "total": len(SECTION_CONFIGS),
                            "attempt": _attempt,
                            "next_attempt": _attempt + 1,
                            "max_attempts": _max_attempts,
                            "score": _attempts_detail[-1]["score"],
                            "min_confidence": MIN_SECTION_CONFIDENCE,
                            "gaps": _attempts_detail[-1]["gaps"],
                        })
                        _prior_for_call = dict(_prior_for_call)
                        _prior_for_call[_cfg["key"]] = _r.get("content", "")

                    # Persist section content (was per-section persist below).
                    try:
                        await srs_documents.update_one(
                            {"project_id": project_id},
                            {"$set": {
                                "project_id": project_id,
                                f"sections.{_cfg['key']}": _r["content"],
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }},
                            upsert=True,
                        )
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning("SRS persist of %s failed: %s", _cfg["key"], _exc)

                    # Build + persist section meta so the UI can render
                    # color-coded backgrounds and the freeze gate can
                    # evaluate against real per-section scores.
                    _final_score = round(float(_judge.get("score", 0.0) or 0.0), 2)
                    _plateaued = bool(
                        AUTORETRY_ENABLED
                        and _final_score < MIN_SECTION_CONFIDENCE
                        and not _err_msg
                        and not _is_failed_section(_r.get("content", ""))
                        and _attempt >= _max_attempts
                    )
                    # iter-14.13 — Explicit plateau reason so the operator can
                    # act on it. Three tiers of "why is confidence stuck":
                    #   evaluator_context_missing → KB not built / analysis
                    #     not run → rebuild KB and re-generate
                    #   kb_evidence_gap → legacy code doesn't cover the
                    #     section → augment KB via UploadPanel + re-generate
                    #   converged → cleared MIN_SECTION_CONFIDENCE
                    if _final_score >= MIN_SECTION_CONFIDENCE:
                        _plateau_reason = "converged"
                    elif bool(_judge.get("context_missing")):
                        _plateau_reason = "evaluator_context_missing"
                    elif _plateaued:
                        _plateau_reason = "kb_evidence_gap"
                    else:
                        _plateau_reason = "in_progress"
                    _meta_row = {
                        "attempts": _attempt,
                        "max_attempts": _max_attempts,
                        "initial_score": round(_initial_score or 0.0, 2),
                        "final_score": _final_score,
                        "delta": round(_final_score - (_initial_score or 0.0), 2),
                        "band": _judge.get("band") or _band_of(_final_score),
                        "plateaued": _plateaued,
                        "plateau_reason": _plateau_reason,
                        "context_missing": bool(_judge.get("context_missing")),
                        "min_confidence": MIN_SECTION_CONFIDENCE,
                        "attempts_detail": _attempts_detail,
                        "tokens": _total_tokens_this_section,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    section_meta[_cfg["key"]] = _meta_row
                    try:
                        await srs_documents.update_one(
                            {"project_id": project_id},
                            {"$set": {f"sections_meta.{_cfg['key']}": _meta_row}},
                        )
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning("SRS meta persist of %s failed: %s", _cfg["key"], _exc)

                    logger.info(
                        "SRS[%s] · finished section %d/%d (%s · %d chars · "
                        "attempt=%d/%d · score=%.1f · plateaued=%s · model=%s)",
                        project_id, _i, len(SECTION_CONFIGS),
                        _cfg["key"], len((_r.get("content") or "").strip()),
                        _attempt, _max_attempts, _final_score, _plateaued,
                        _r.get("model_used", model),
                    )
                    # Roll retry-loop tokens back onto _r so the outer wave
                    # sum (`total_tokens += _rres["tokens"]`) charges for every
                    # attempt, not just the last one.
                    _r["tokens"] = _total_tokens_this_section
                    await _broadcast({
                        "type": "section_complete",
                        "section": _cfg["key"],
                        "label": _cfg["label"],
                        "index": _i,
                        "total": len(SECTION_CONFIGS),
                        "content": _r["content"],
                        "tokens": _total_tokens_this_section,
                        "model_requested": model or "",
                        "model_used": _r.get("model_used", "") or model or "",
                        # iter-14.11 — score-gated retry metadata for the UI
                        "attempts": _attempt,
                        "max_attempts": _max_attempts,
                        "initial_score": _meta_row["initial_score"],
                        "final_score": _final_score,
                        "band": _meta_row["band"],
                        "plateaued": _plateaued,
                        "min_confidence": MIN_SECTION_CONFIDENCE,
                        # iter-14.13 — plateau diagnosis on the wire so the
                        # SRS panel can render an actionable hint next to the
                        # confidence pill (rebuild KB vs. augment KB).
                        "plateau_reason": _plateau_reason,
                        "context_missing": bool(_judge.get("context_missing")),
                    })
                    return _i, _cfg, _r, _err_msg

                for _wave in _waves:
                    # Pause / cancel are checked at wave boundary. In wave_size=1
                    # this matches the original per-section semantics.
                    if control.get("cancelled"):
                        _first_i, _first_cfg = _wave[0]
                        logger.info(
                            "SRS[%s] · user cancelled before wave starting at section %d (%s)",
                            project_id, _first_i, _first_cfg.get("key"),
                        )
                        await _broadcast({
                            "type": "run_aborted",
                            "reason": "cancelled",
                            "section": _first_cfg.get("key"),
                            "label": _first_cfg.get("label"),
                            "message": "Generation cancelled by user.",
                        })
                        existing_doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0, "version": 1})
                        version = (existing_doc.get("version", 0) + 1) if existing_doc else 1
                        await _broadcast({
                            "type": "complete",
                            "version": version,
                            "total_tokens": total_tokens,
                            "sections_total": len(SECTION_CONFIGS),
                            "sections_ok": sum(1 for c in SECTION_CONFIGS if not _is_failed_section(sections.get(c["key"], ""))),
                            "sections_failed": [c["key"] for c in SECTION_CONFIGS if c["key"] != "entity_model" and _is_failed_section(sections.get(c["key"], ""))],
                            "partial": True,
                            "aborted": True,
                            "abort_reason": "cancelled",
                        })
                        return
                    if control.get("paused"):
                        _first_i, _first_cfg = _wave[0]
                        await _broadcast({
                            "type": "paused",
                            "section": _first_cfg["key"],
                            "label": _first_cfg["label"],
                            "index": _first_i,
                            "total": len(SECTION_CONFIGS),
                        })
                        _ticks = 0
                        while control.get("paused") and not control.get("cancelled"):
                            await asyncio.sleep(1.0)
                            _ticks += 1
                            if _ticks % 10 == 0:
                                await _broadcast({
                                    "type": "paused_heartbeat",
                                    "section": _first_cfg["key"],
                                    "index": _first_i,
                                    "total": len(SECTION_CONFIGS),
                                })
                        if control.get("cancelled"):
                            await _broadcast({
                                "type": "run_aborted",
                                "reason": "cancelled",
                                "section": _first_cfg["key"],
                                "label": _first_cfg["label"],
                                "message": "Generation cancelled by user.",
                            })
                            return
                        await _broadcast({
                            "type": "resumed",
                            "section": _first_cfg["key"],
                            "label": _first_cfg["label"],
                            "index": _first_i,
                            "total": len(SECTION_CONFIGS),
                        })

                    # Snapshot prior_sections ONCE per wave so every section in
                    # this wave sees the identical prior set (as sequential mode
                    # would give the FIRST section of the wave). Sections within
                    # a wave never cross-reference each other — waves are the
                    # dependency boundary.
                    _prior_snap = dict(sections)
                    _results = await asyncio.gather(
                        *[_run_one_section(_i, _c, _prior_snap) for _i, _c in _wave],
                        return_exceptions=False,
                    )
                    _results_sorted = sorted(_results, key=lambda t: t[0])
                    _abort_now = False
                    _abort_i = 0
                    _abort_cfg: dict = {}
                    _abort_r: dict = {}
                    for _ri, _rcfg, _rres, _rerr in _results_sorted:
                        sections[_rcfg["key"]] = _rres["content"]
                        total_tokens += _rres.get("tokens", 0)
                        _wave_done.add(_rcfg["key"])
                        if _is_failed_section(_rres["content"]):
                            _consec_fails += 1
                        else:
                            _consec_fails = 0
                        _max_fails = int(os.environ.get("LAMA_SRS_MAX_CONSEC_FAILS", "3") or "3")
                        if _consec_fails >= _max_fails and not _abort_now:
                            _abort_now = True
                            _abort_i = _ri
                            _abort_cfg = _rcfg
                            _abort_r = _rres

                    if _abort_now:
                        abort_reason = (
                            f"SRS run aborted — {_consec_fails} consecutive "
                            f"sections failed (threshold={_max_fails}). The "
                            "upstream provider is likely fully down: Factory.ai "
                            "Droid Computer offline, every fabric provider out "
                            "of credits, or every API key rejected. Fix the "
                            "upstream issue and click Regenerate SRS — the "
                            "sections that DID succeed are preserved."
                        )
                        logger.warning(
                            "SRS[%s] · %d consecutive failures (≥%d) — aborting. preview=%r",
                            project_id, _consec_fails, _max_fails,
                            (_abort_r.get("content") or "")[:200],
                        )
                        await _broadcast({
                            "type": "run_aborted",
                            "reason": "consecutive_failures",
                            "consecutive_failures": _consec_fails,
                            "threshold": _max_fails,
                            "section": _abort_cfg.get("key", ""),
                            "label": _abort_cfg.get("label", ""),
                            "message": abort_reason,
                            "model_used": _abort_r.get("model_used", "") or model or "",
                        })
                        existing = await srs_documents.find_one({"project_id": project_id}, {"_id": 0, "version": 1})
                        version = (existing.get("version", 0) + 1) if existing else 1
                        _ok_count = sum(
                            1 for c in SECTION_CONFIGS
                            if c["key"] in sections and not _is_failed_section(sections.get(c["key"], ""))
                        )
                        await _broadcast({
                            "type": "complete",
                            "version": version,
                            "total_tokens": total_tokens,
                            "sections_total": len(SECTION_CONFIGS),
                            "sections_ok": _ok_count,
                            "sections_failed": [
                                c["key"] for c in SECTION_CONFIGS
                                if c["key"] in sections and _is_failed_section(sections.get(c["key"], ""))
                            ],
                            "partial": True,
                            "aborted": True,
                            "abort_reason": abort_reason,
                        })
                        return

                # Legacy sequential loop kept as a defensive fallback — the
                # wave path above already handles every SECTION_CONFIGS key
                # (including resume-skips) so this loop no-ops in practice.
                for i, cfg in enumerate(SECTION_CONFIGS, start=1):
                    if cfg["key"] in _wave_done:
                        continue
                    # iter-14.5 — RESUME: skip sections already present and good.
                    if resume_mode and cfg["key"] in resume_skip_keys:
                        await _broadcast({
                            "type": "section_complete",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "index": i,
                            "total": len(SECTION_CONFIGS),
                            "content": sections.get(cfg["key"], ""),
                            "tokens": 0,
                            "model_requested": model or "",
                            "model_used": "",
                            "resumed": True,
                        })
                        continue
                    # iter-13.35 — honour user pause/cancel BEFORE each section.
                    if control.get("cancelled"):
                        logger.info("SRS[%s] · user cancelled before section %d", project_id, i)
                        await _broadcast({
                            "type": "run_aborted",
                            "reason": "cancelled",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "message": "Generation cancelled by user.",
                        })
                        existing_doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0, "version": 1})
                        version = (existing_doc.get("version", 0) + 1) if existing_doc else 1
                        await _broadcast({
                            "type": "complete",
                            "version": version,
                            "total_tokens": total_tokens,
                            "sections_total": len(SECTION_CONFIGS),
                            "sections_ok": sum(1 for c in SECTION_CONFIGS if not _is_failed_section(sections.get(c["key"], ""))),
                            "sections_failed": [c["key"] for c in SECTION_CONFIGS if c["key"] != "entity_model" and _is_failed_section(sections.get(c["key"], ""))],
                            "partial": True,
                            "aborted": True,
                            "abort_reason": "cancelled",
                        })
                        return
                    if control.get("paused"):
                        await _broadcast({
                            "type": "paused",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "index": i,
                            "total": len(SECTION_CONFIGS),
                        })
                        # Poll for resume/cancel every 1s. Heartbeat every 10s
                        # so SSE proxies don't time out.
                        ticks = 0
                        while control.get("paused") and not control.get("cancelled"):
                            await asyncio.sleep(1.0)
                            ticks += 1
                            if ticks % 10 == 0:
                                await _broadcast({
                                    "type": "paused_heartbeat",
                                    "section": cfg["key"],
                                    "index": i,
                                    "total": len(SECTION_CONFIGS),
                                })
                        if control.get("cancelled"):
                            await _broadcast({
                                "type": "run_aborted",
                                "reason": "cancelled",
                                "section": cfg["key"],
                                "label": cfg["label"],
                                "message": "Generation cancelled by user.",
                            })
                            return
                        await _broadcast({
                            "type": "resumed",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "index": i,
                            "total": len(SECTION_CONFIGS),
                        })
                    logger.info(
                        "SRS[%s] · starting section %d/%d (%s · %s · min_words=%d)",
                        project_id, i, len(SECTION_CONFIGS),
                        cfg["key"], cfg["label"], cfg.get("min_words", 0),
                    )
                    await _broadcast({
                        "type": "section_start",
                        "section": cfg["key"],
                        "label": cfg["label"],
                        "index": i,
                        "total": len(SECTION_CONFIGS),
                    })
                    # Wrap the LLM call in a task + heartbeat loop so the SSE
                    # relay never sits idle for >5 s. Without this, a 90-second
                    # heavy LLM call meant the only thing flowing through the
                    # SSE pipe was the relay's own 4-s pings — and if any one
                    # of those pings was dropped by a flaky proxy, the
                    # connection got closed mid-section.   (iter-13.14 fix.)
                    section_task = asyncio.create_task(
                        _gen_one_section(
                            cfg, proj, full_toon, summary, convo_text, model,
                            project_id=project_id, prior_sections=sections,
                            analysis_digest=analysis_digest,
                        )
                    )
                    while not section_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(section_task), timeout=5.0)
                        except asyncio.TimeoutError:
                            # Emit a fresh heartbeat event with which section
                            # we're on so the UI can show "Writing section N…".
                            await _broadcast({
                                "type": "section_progress",
                                "section": cfg["key"],
                                "label": cfg["label"],
                                "index": i,
                                "total": len(SECTION_CONFIGS),
                                "ts": datetime.now(timezone.utc).isoformat(),
                            })
                        except Exception:  # noqa: BLE001 — the task raised; unwrap below
                            break
                    try:
                        r = await section_task
                    except Exception as exc:  # noqa: BLE001 — never abort the run
                        err_msg = str(exc)[:240]
                        logger.warning("SRS section %s failed: %s", cfg["key"], err_msg)
                        r = {
                            "content": (
                                f"> ⚠️ **Section generation failed** — `{err_msg}`\n\n"
                                "The pipeline continued with the remaining sections. "
                                "Click *Regenerate* on this section from the SRS panel "
                                "once the underlying issue (provider credits / API key "
                                "/ network) is resolved."
                            ),
                            "tokens": 0,
                            "error": "exception",
                        }
                        await _broadcast({
                            "type": "section_error",
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "error": err_msg,
                        })
                    sections[cfg["key"]] = r["content"]
                    total_tokens += r.get("tokens", 0)
                    logger.info(
                        "SRS[%s] · finished section %d/%d (%s · %d chars · attempt=%s · model=%s)",
                        project_id, i, len(SECTION_CONFIGS),
                        cfg["key"], len((r.get("content") or "").strip()),
                        r.get("attempt", "?"), r.get("model_used", model),
                    )

                    # Persist incrementally so even a crashed run leaves
                    # behind whatever sections it did manage to produce.
                    try:
                        await srs_documents.update_one(
                            {"project_id": project_id},
                            {"$set": {
                                "project_id": project_id,
                                f"sections.{cfg['key']}": r["content"],
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }},
                            upsert=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("SRS persist of %s failed: %s", cfg["key"], exc)

                    await _broadcast({
                        "type": "section_complete",
                        "section": cfg["key"],
                        "label": cfg["label"],
                        "index": i,
                        "total": len(SECTION_CONFIGS),
                        "content": r["content"],
                        "tokens": r.get("tokens", 0),
                        # iter-13.34 — surface failover transparency. When the
                        # actual model used differs from the user-requested
                        # model, the UI now shows a "ran on X instead of Y"
                        # badge so the user understands they're seeing qwen
                        # output instead of Claude output (= Anthropic key
                        # ran out of credits and failover walked to OpenRouter).
                        "model_requested": model or "",
                        "model_used": r.get("model_used", "") or model or "",
                    })

                    # iter-13.35 — EARLY ABORT on first-section failure.
                    # iter-13.81.10 — SOFTENED. Aborting after a single
                    # failure was catastrophic for transient hiccups
                    # (Factory 424 cold-start, momentary credit blip,
                    # one bad SSE chunk). Now we tolerate up to
                    # LAMA_SRS_MAX_CONSEC_FAILS (default 3) consecutive
                    # failures before bailing. A single bad section in
                    # the middle of a run never aborts.
                    if _is_failed_section(r["content"]):
                        try:
                            _consec_fails += 1
                        except NameError:
                            _consec_fails = 1
                    else:
                        _consec_fails = 0
                    _max_fails = int(os.environ.get("LAMA_SRS_MAX_CONSEC_FAILS", "3") or "3")
                    if _consec_fails >= _max_fails:
                        abort_reason = (
                            f"SRS run aborted — {_consec_fails} consecutive "
                            f"sections failed (threshold={_max_fails}). The "
                            "upstream provider is likely fully down: Factory.ai "
                            "Droid Computer offline, every fabric provider out "
                            "of credits, or every API key rejected. Fix the "
                            "upstream issue and click Regenerate SRS — the "
                            "sections that DID succeed are preserved."
                        )
                        logger.warning(
                            "SRS[%s] · %d consecutive failures (≥%d) — aborting. preview=%r",
                            project_id, _consec_fails, _max_fails,
                            (r.get("content") or "")[:200],
                        )
                        await _broadcast({
                            "type": "run_aborted",
                            "reason": "consecutive_failures",
                            "consecutive_failures": _consec_fails,
                            "threshold": _max_fails,
                            "section": cfg["key"],
                            "label": cfg["label"],
                            "message": abort_reason,
                            "model_used": r.get("model_used", "") or model or "",
                        })
                        existing = await srs_documents.find_one({"project_id": project_id}, {"_id": 0, "version": 1})
                        version = (existing.get("version", 0) + 1) if existing else 1
                        _ok_count = sum(
                            1 for c in SECTION_CONFIGS
                            if c["key"] in sections and not _is_failed_section(sections.get(c["key"], ""))
                        )
                        await _broadcast({
                            "type": "complete",
                            "version": version,
                            "total_tokens": total_tokens,
                            "sections_total": len(SECTION_CONFIGS),
                            "sections_ok": _ok_count,
                            "sections_failed": [
                                c["key"] for c in SECTION_CONFIGS
                                if c["key"] in sections and _is_failed_section(sections.get(c["key"], ""))
                            ],
                            "partial": True,
                            "aborted": True,
                            "abort_reason": abort_reason,
                        })
                        return  # exits _run_job(); finally-block still emits __end__

                # ---- Second pass: SRS Revalidation ----
                await _broadcast({"type": "revalidation_start"})
                try:
                    revalidation = await _run_revalidation(project_id, proj, sections, model)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SRS revalidation crashed: %s", exc)
                    revalidation = {"applied": False, "skipped_reason": f"crashed: {str(exc)[:200]}"}
                if revalidation.get("applied"):
                    sections["specific_requirements"] = revalidation["specific_requirements"]
                    sections["detailed_use_cases"] = revalidation["detailed_use_cases"]
                    total_tokens += revalidation.get("tokens", 0)
                    try:
                        await srs_documents.update_one(
                            {"project_id": project_id},
                            {"$set": {
                                "sections.specific_requirements": sections["specific_requirements"],
                                "sections.detailed_use_cases": sections["detailed_use_cases"],
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }},
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("SRS revalidation persist failed: %s", exc)
                await _broadcast({
                    "type": "revalidation_complete",
                    "applied": revalidation.get("applied", False),
                    "model_used": revalidation.get("model_used"),
                    "skipped_reason": revalidation.get("skipped_reason"),
                    "tokens": revalidation.get("tokens", 0),
                    "specific_requirements": sections.get("specific_requirements", "") if revalidation.get("applied") else "",
                    "detailed_use_cases": sections.get("detailed_use_cases", "") if revalidation.get("applied") else "",
                })

                # ---- Repair pass: re-try sections that came out empty or with a transient failure marker ----
                # Without this, the user sees "SRS generated v1" while 3 of 12
                # sections are just `> ⚠️ Section generation failed` placeholders.
                #
                # iter-13.34 — REVERSED the iter-13.13 "credit errors are
                # permanent" policy. Real-world symptom (visible in the
                # screenshot user reported): Section 1 failed with the
                # "out of credits" marker, but by Section 3 the failover
                # walker had identified a working provider (qwen via
                # OpenRouter) and the rest of the run was succeeding.
                # Treating Section 1 as permanent meant the user shipped
                # an SRS with an "all providers exhausted" placeholder in
                # the introduction, despite the system having a working
                # provider in hand. New policy:
                #   • Credit / auth errors are RECOVERABLE at end-of-run
                #     IF at least one section succeeded normally — that
                #     proves the failover has discovered a healthy provider
                #     and the agent is now pinned to it.
                #   • The 4-section cap still applies as a "systemic
                #     outage" guard, but doesn't kick in when sections are
                #     visibly succeeding (success_count > 0).
                permanent_markers = (
                    "> ⚠️ **Section skipped — LLM provider out of credits.**",
                    "> ⚠️ **Section skipped — LLM provider rejected the API key.**",
                )
                def _is_permanent(content: str) -> bool:
                    head = (content or "").lstrip()[:200]
                    return any(head.startswith(m) for m in permanent_markers)

                # Did any section come back with real content? That's the
                # signal that the failover walker has settled on a working
                # provider, so credit-marked sections deserve a retry.
                success_count = sum(
                    1 for cfg in SECTION_CONFIGS
                    if cfg["key"] != "entity_model"
                    and not _is_failed_section(sections.get(cfg["key"], ""))
                )

                failed_keys = [
                    cfg["key"] for cfg in SECTION_CONFIGS
                    if cfg["key"] != "entity_model"
                    and _is_failed_section(sections.get(cfg["key"], ""))
                    # Only skip credit/auth-marked sections when NOTHING
                    # has succeeded — that's the true "systemic outage"
                    # case where retrying would just burn cycles.
                    and (success_count > 0 or not _is_permanent(sections.get(cfg["key"], "")))
                ]
                # Cap repair attempts at 6 (was 4) so partial outages on
                # large SRS runs (12 sections) can still recover most of
                # the document. Don't cap when we KNOW providers work
                # (success_count > 0) — at that point every retry is
                # cheap and almost certain to succeed.
                if success_count == 0 and len(failed_keys) > 4:
                    logger.warning(
                        "SRS run for %s: %d sections failed with ZERO successes — "
                        "skipping repair pass (systemic outage)",
                        project_id, len(failed_keys),
                    )
                    failed_keys = []
                elif success_count > 0 and len(failed_keys) > 0:
                    logger.info(
                        "SRS run for %s: %d sections failed, %d succeeded — "
                        "running repair pass on the failures (working provider "
                        "is now pinned via failover)",
                        project_id, len(failed_keys), success_count,
                    )
                if failed_keys:
                    await _broadcast({
                        "type": "repair_start",
                        "failed_sections": failed_keys,
                        "count": len(failed_keys),
                    })
                    for fk in failed_keys:
                        cfg = next((c for c in SECTION_CONFIGS if c["key"] == fk), None)
                        if not cfg:
                            continue
                        # Brief pause between repair attempts to dodge rate-limit windows.
                        try:
                            await asyncio.sleep(3)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            r2 = await _gen_one_section(
                                cfg, proj, full_toon, summary, convo_text, model,
                                project_id=project_id, prior_sections=sections,
                                analysis_digest=analysis_digest,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("SRS repair of %s failed: %s", fk, exc)
                            continue
                        # Only replace if the repair attempt actually produced something real.
                        new_content = (r2 or {}).get("content", "") or ""
                        if new_content.strip() and not _is_failed_section(new_content):
                            sections[fk] = new_content
                            total_tokens += r2.get("tokens", 0)
                            try:
                                await srs_documents.update_one(
                                    {"project_id": project_id},
                                    {"$set": {
                                        f"sections.{fk}": new_content,
                                        "updated_at": datetime.now(timezone.utc).isoformat(),
                                    }},
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("SRS repair persist of %s failed: %s", fk, exc)
                            await _broadcast({
                                "type": "section_complete",
                                "section": fk,
                                "label": cfg["label"],
                                "index": [c["key"] for c in SECTION_CONFIGS].index(fk) + 1,
                                "total": len(SECTION_CONFIGS),
                                "content": new_content,
                                "tokens": r2.get("tokens", 0),
                                "repaired": True,
                            })
                    # Recompute the still-failed list so the final complete event
                    # honestly reports partial success.
                    still_failed = [
                        cfg["key"] for cfg in SECTION_CONFIGS
                        if cfg["key"] != "entity_model" and _is_failed_section(sections.get(cfg["key"], ""))
                    ]
                    await _broadcast({
                        "type": "repair_complete",
                        "repaired_count": len(failed_keys) - len([k for k in failed_keys if k in still_failed]),
                        "still_failed": still_failed,
                    })

                try:
                    version = await _persist_srs(project_id, existing, sections, total_tokens, model)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SRS final persist failed: %s", exc)
                    version = (existing.get("version", 0) + 1) if existing else 1
                # Honest accounting of which sections actually have real content.
                non_failed = [
                    cfg["key"] for cfg in SECTION_CONFIGS
                    if not _is_failed_section(sections.get(cfg["key"], ""))
                ]
                final_failed = [
                    cfg["key"] for cfg in SECTION_CONFIGS
                    if cfg["key"] != "entity_model" and _is_failed_section(sections.get(cfg["key"], ""))
                ]
                await _broadcast({
                    "type": "complete",
                    "version": version,
                    "total_tokens": total_tokens,
                    "sections_total": len(SECTION_CONFIGS),
                    "sections_ok": len(non_failed),
                    "sections_failed": final_failed,
                    "partial": bool(final_failed),
                })
            except asyncio.CancelledError:
                # iter-13.36 — hard-cancel path from /generate/cancel. Persist
                # whatever sections we'd already completed so the user keeps
                # the partial work, then re-raise to let the cancellation
                # propagate cleanly. The cancel endpoint already broadcast
                # run_aborted + complete + __end__, so we don't double-emit.
                logger.info("SRS[%s] · CancelledError — persisting partial (%d sections)",
                            project_id, sum(1 for v in sections.values() if v))
                try:
                    if sections:
                        await _persist_srs(project_id, existing, sections, total_tokens, model)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("SRS[%s] · partial persist on cancel failed: %s", project_id, exc)
                raise
            except Exception as exc:  # noqa: BLE001 — top-level safety net
                logger.exception("SRS background job crashed: %s", exc)
                await _broadcast({"type": "error", "message": str(exc)[:240]})
            finally:
                # Sentinel so every listener loop knows to terminate.
                # iter-13.36 — guarded against double-emit when /cancel already
                # sent __end__ before cancelling the task.
                try:
                    await _broadcast({"type": "__end__"})
                except Exception:  # noqa: BLE001
                    pass
                _SRS_JOBS.pop(project_id, None)

        job_task = asyncio.create_task(_run_job())
        _SRS_JOBS[project_id] = {
            "task": job_task,
            "listeners": listeners,
            "history": history,
            "control": control,
            "broadcast": _broadcast,
        }

    async def event_gen():
        # Relay events from the per-listener queue. If the client disconnects,
        # this generator is cancelled — but the background _run_job() task is
        # NOT (it was created with asyncio.create_task and is not awaited or
        # shielded from this generator), so generation continues to completion.
        #
        # iter-13.14: emit pings as actual `data:` events at 4-second cadence
        # instead of `:comment` lines at 10 s. Several common reverse proxies
        # (Hostinger / corporate Zscaler / certain CDN edges) do NOT forward
        # SSE comment lines as keep-alive traffic and will close the idle
        # connection in 15–30 s. A real `data:` event is unambiguous keep-alive
        # traffic that every SSE-aware proxy propagates.
        #
        # iter-13.15: ALSO clean up our listener queue from the job's
        # `listeners` list on exit. Without this, `_broadcast` keeps trying
        # to push every event into a queue no one is reading — memory grows
        # unboundedly across reconnects, and (more visibly to the user) when
        # they hit Regenerate after a disconnect we replay an inflated
        # history that contains events from a *previous* attached client.
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=4.0)
                except asyncio.TimeoutError:
                    yield "data: " + json.dumps({
                        "type": "ping",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }) + "\n\n"
                    continue
                if evt.get("type") == "__end__":
                    break
                yield "data: " + json.dumps(evt) + "\n\n"
                if evt.get("type") in ("complete", "error"):
                    # Terminal event delivered; no more updates coming.
                    break
        except asyncio.CancelledError:
            # Client went away — that's fine, background job carries on.
            raise
        finally:
            # De-register this listener so _broadcast doesn't keep pushing
            # into a queue we'll never read. Safe whether or not the job
            # is still running (the job entry may have been popped from
            # _SRS_JOBS already; in that case `listeners` is now an
            # orphan list that we just remove ourselves from quickly).
            try:
                job_now = _SRS_JOBS.get(project_id)
                live_listeners = (job_now or {}).get("listeners") if job_now else listeners
                if live_listeners and queue in live_listeners:
                    live_listeners.remove(queue)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{project_id}/generate/status")
async def generate_srs_status(project_id: str):
    """Polling fallback — lets the UI confirm whether a background SRS run
    is still in progress and how far it's progressed. Used when the SSE
    connection has dropped but the user wants to know if generation is
    still running server-side."""
    job = _SRS_JOBS.get(project_id)
    if not job or job["task"].done():
        # No live job. Caller should fall back to GET /srs/{project_id}
        # to see what's persisted.
        return {"running": False}
    # Pull the latest section_complete event from history to report progress.
    last_idx = 0
    last_section = None
    for evt in reversed(job["history"]):
        if evt.get("type") == "section_complete":
            last_idx = evt.get("index", 0)
            last_section = evt.get("section")
            break
    return {
        "running": True,
        "last_section_index": last_idx,
        "last_section_key": last_section,
        "total_sections": len(SECTION_CONFIGS),
        "history_events": len(job["history"]),
    }


@router.get("/{project_id}")
async def get_srs(project_id: str):
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    # Keep this list in sync with SECTION_CONFIGS — single source of truth.
    empty_keys = [c["key"] for c in SECTION_CONFIGS]
    if not doc:
        return {
            "project_id": project_id,
            "sections": dict.fromkeys(empty_keys, ""),
            "frozen": False,
            "version": 0,
        }
    # Migrate any legacy section keys that may still exist on older
    # SRS documents into the new key space, so the UI never shows blanks
    # for projects generated before the IEEE 29148 restructure.
    sections = dict(doc.get("sections") or {})
    for old_key, new_key in LEGACY_SECTION_ALIAS.items():
        if old_key in sections and not sections.get(new_key):
            sections[new_key] = sections[old_key]
    # Ensure every current key exists (even if empty) for the UI loop.
    for k in empty_keys:
        sections.setdefault(k, "")
    doc["sections"] = sections
    return doc


@router.put("/{project_id}/section")
async def update_section(project_id: str, payload: SRSSectionUpdate):
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "SRS not found — generate first")
    if doc.get("frozen"):
        raise HTTPException(400, "SRS is frozen")
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": {
            f"sections.{payload.section}": payload.content,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# iter-13.35 — Live-job controls: pause / resume / cancel
# ─────────────────────────────────────────────────────────────────────
def _get_running_job_or_404(project_id: str) -> dict:
    job = _SRS_JOBS.get(project_id)
    if not job or job["task"].done():
        raise HTTPException(404, "No SRS generation is running for this project")
    return job


@router.post("/{project_id}/generate/pause")
async def pause_srs_generation(project_id: str):
    """Set the paused flag on the running job. The section loop checks this
    BETWEEN sections (never mid-LLM-call — that would leak tokens), so the
    pause takes effect within ~5–60 s depending on the current section."""
    job = _get_running_job_or_404(project_id)
    job["control"]["paused"] = True
    return {"ok": True, "paused": True}


@router.post("/{project_id}/generate/resume")
async def resume_srs_generation(project_id: str):
    """Clear the paused flag."""
    job = _get_running_job_or_404(project_id)
    job["control"]["paused"] = False
    return {"ok": True, "paused": False}


@router.post("/{project_id}/generate/cancel")
async def cancel_srs_generation(project_id: str):
    """Hard-stop SRS generation. iter-13.36 — previously this only set a
    cooperative flag and waited up to 60 s for the in-flight LLM call to
    finish (the loop only checks BETWEEN sections). That meant clicking
    Cancel during a slow Opus call burned both tokens and wall-time. We
    now: (a) flip the cancelled flag, (b) broadcast run_aborted + complete
    so SSE listeners update immediately, (c) cancel the asyncio task right
    away — which raises CancelledError into the LLM await and unwinds the
    job. Whatever sections were already persisted are kept; the in-flight
    section is discarded."""
    job = _get_running_job_or_404(project_id)
    job["control"]["cancelled"] = True
    job["control"]["paused"] = False  # unblock any pause-sleep loop

    # Best-effort: tell every attached SSE listener we're aborting NOW so
    # the UI flips state without waiting for the task-cancellation unwind
    # to reach the loop's between-section checkpoint.
    try:
        broadcast = job.get("broadcast")
        if broadcast:
            await broadcast({
                "type": "run_aborted",
                "reason": "cancelled",
                "message": "Generation cancelled by user (hard-stop).",
            })
            await broadcast({
                "type": "complete",
                "aborted": True,
                "abort_reason": "cancelled",
                "partial": True,
            })
            await broadcast({"type": "__end__"})
    except Exception as e:  # noqa: BLE001 — observability only
        logger.warning("SRS[%s] · cancel broadcast failed: %s", project_id, e)

    # Hard-cancel the running task — interrupts the in-flight LLM await.
    try:
        if not job["task"].done():
            job["task"].cancel()
    except Exception as e:  # noqa: BLE001
        logger.warning("SRS[%s] · task.cancel() failed: %s", project_id, e)

    # Drop the job entry so a subsequent /generate call starts fresh
    # instead of trying to re-attach to a cancelled task.
    _SRS_JOBS.pop(project_id, None)
    return {"ok": True, "cancelled": True, "hard_stop": True}


# ─────────────────────────────────────────────────────────────────────
# iter-13.35 — Per-section regenerate
# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────
# iter-13.35 — Prompt preview (debug). Lets you `curl` and SEE the exact
# system + user messages that will be sent to the LLM / Factory Droid
# for a given section, without spending tokens. Use this to confirm that
# the LEGACY CODEBASE LOCATION block is being assembled correctly for
# your project.
#
# Example:
#   curl http://127.0.0.1:8382/api/srs/{pid}/section/introduction/prompt-preview
# ─────────────────────────────────────────────────────────────────────
@router.get("/{project_id}/section/{section_key}/prompt-preview")
async def preview_section_prompt(project_id: str, section_key: str):
    cfg = next((c for c in SECTION_CONFIGS if c["key"] == section_key), None)
    if not cfg:
        raise HTTPException(400, f"Unknown section key: {section_key}")
    # Build the same context the real run would use. We monkey-patch
    # _gen_one_section by extracting the bits we need.
    proj, existing, full_toon, summary, convo_text, analysis_digest = await _load_srs_context(project_id, None)
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0, "sections": 1})
    prior_sections = (doc or {}).get("sections") or {}
    # Re-use the section generator but capture, do not call the LLM.
    # The cleanest way: replicate the prompt-assembly block — but that
    # would drift. Instead, monkey-patch chat_completion temporarily.
    import llm as _llm_mod
    captured: dict = {"messages": None, "kwargs": None}

    async def _capture(messages, **kw):
        captured["messages"] = messages
        captured["kwargs"] = kw
        # Return a stub so _gen_one_section's post-processing doesn't crash.
        return {"content": "(PREVIEW — no LLM call made)", "model": "preview", "usage": {"total_tokens": 0}}

    original = _llm_mod.fabric_call
    try:
        _llm_mod.fabric_call = _capture  # type: ignore
        await _gen_one_section(
            cfg, proj, full_toon, summary, convo_text, "",
            project_id=project_id, prior_sections=prior_sections,
            analysis_digest=analysis_digest,
        )
    finally:
        _llm_mod.fabric_call = original  # type: ignore

    msgs = captured["messages"] or []
    sys_msg = next((m for m in msgs if m.get("role") == "system"), {})
    usr_msg = next((m for m in msgs if m.get("role") == "user"), {})
    sys_content = sys_msg.get("content", "") or ""
    has_legacy_block = "LEGACY CODEBASE LOCATION" in sys_content
    has_factory_block = "FACTORY DROID FILESYSTEM ACCESS" in sys_content
    return {
        "section": section_key,
        "label": cfg["label"],
        "legacy_path_persisted": (proj.get("legacy_path") or ""),
        "factory_orchestrator_enabled": bool(((proj.get("settings") or {}).get("factory_orchestrator") or {}).get("enabled")),
        "factory_cwd": (((proj.get("settings") or {}).get("factory_orchestrator") or {}).get("cwd") or ""),
        "system_prompt": sys_content,
        "system_prompt_chars": len(sys_content),
        "user_prompt": usr_msg.get("content", ""),
        "checks": {
            "legacy_codebase_location_block_present": has_legacy_block,
            "factory_droid_filesystem_access_block_present": has_factory_block,
        },
    }


@router.post("/{project_id}/section/{section_key}/regenerate")
async def regenerate_one_section(project_id: str, section_key: str, payload: dict = None):
    """Re-run a single SRS section without touching the others.

    Body (all optional):
      {"model": "<model id>", "conversation_id": "<id>"}

    Returns the freshly generated content + tokens. Persisted to
    `srs_documents.sections.<key>` and a new version is bumped at write
    time so the UI's poll-on-mount picks up the change.
    """
    payload = payload or {}
    # Validate section key against SECTION_CONFIGS.
    cfg = next((c for c in SECTION_CONFIGS if c["key"] == section_key), None)
    if not cfg:
        raise HTTPException(400, f"Unknown section key: {section_key}")
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "SRS not found — run /generate first")
    if doc.get("frozen"):
        raise HTTPException(400, "SRS is frozen — unfreeze before regenerating a section")

    # Clear any stale provider pin so this single-section regen uses the
    # current Console default + failover ladder, not whatever provider was
    # pinned by the previous full run.
    try:
        from db import agent_configs as _ac
        await _ac.update_one(
            {"key": "srs.generate"},
            {"$set": {"provider_id": ""}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass

    conversation_id = payload.get("conversation_id")
    model = payload.get("model") or ""
    proj, _existing, full_toon, summary, convo_text, analysis_digest = await _load_srs_context(project_id, conversation_id)
    prior_sections = doc.get("sections") or {}
    r = await _gen_one_section(
        cfg, proj, full_toon, summary, convo_text, model,
        project_id=project_id, prior_sections=prior_sections,
        analysis_digest=analysis_digest,
        agent_key="srs.regenerate",   # iter-13.76 — regenerate → Opus (high tier)
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    # Bump version on every single-section regen so the SRS list shows
    # something changed (and PDF export reflects the new content).
    new_version = int(doc.get("version", 0) or 0) + 1
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": {
            f"sections.{section_key}": r["content"],
            "version": new_version,
            "updated_at": now_iso,
        }},
    )
    # Audit-log per CLAUDE.md §7.
    try:
        await audit_log.insert_one({
            "action": "srs.regenerate_section",
            "project_id": project_id,
            "section": section_key,
            "model_requested": model,
            "model_used": r.get("model_used", ""),
            "tokens": r.get("tokens", 0),
            "version": new_version,
            "ts": now_iso,
        })
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "section": section_key,
        "label": cfg["label"],
        "content": r["content"],
        "tokens": r.get("tokens", 0),
        "model_requested": model,
        "model_used": r.get("model_used", ""),
        "version": new_version,
        "failed": _is_failed_section(r["content"]),
    }


# ─────────────────────────────────────────────────────────────────────
# iter-13.37 — Per-section regenerate, SSE variant.
# The plain POST `/section/{key}/regenerate` above runs synchronously and
# can take 2–4 minutes inside `_gen_one_section` (attempt schedule 240 s
# + 200 s + 150 s + coverage-gate 180 s). Production K8s / Hostinger
# ingress closes idle HTTP connections at ~60 s, so axios surfaces a
# `Request failed with status code 504` to the user even though the
# backend would eventually have returned a perfectly good section.
#
# This streaming variant keeps the connection alive by emitting a
# `data: {"type":"ping"}` event every 4 s while the LLM call is in flight,
# and finally emits a `data: {"type":"complete", ...}` event with the
# generated section. Same heartbeat strategy proven by `/generate/stream`.
#
# The frontend's `regenerateSRSSection` helper now uses this endpoint via
# `fetch()` + a streaming reader (same pattern as the full-SRS run).
# ─────────────────────────────────────────────────────────────────────
@router.post("/{project_id}/section/{section_key}/regenerate/stream")
async def regenerate_one_section_stream(project_id: str, section_key: str, payload: dict = None):
    payload = payload or {}
    cfg = next((c for c in SECTION_CONFIGS if c["key"] == section_key), None)
    if not cfg:
        raise HTTPException(400, f"Unknown section key: {section_key}")
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "SRS not found — run /generate first")
    if doc.get("frozen"):
        raise HTTPException(400, "SRS is frozen — unfreeze before regenerating a section")

    # Clear stale provider pin so this regen uses the current Console default.
    try:
        from db import agent_configs as _ac
        await _ac.update_one(
            {"key": "srs.generate"}, {"$set": {"provider_id": ""}}, upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass

    conversation_id = payload.get("conversation_id")
    model = payload.get("model") or ""
    proj, _existing, full_toon, summary, convo_text, analysis_digest = await _load_srs_context(project_id, conversation_id)
    prior_sections = doc.get("sections") or {}

    async def event_gen():
        # Kick off the section work as a detached task so the SSE relay
        # can interleave heartbeat pings while the LLM call is in flight.
        # `fast_mode=True` skips the coverage-gate continuation pass to
        # cut single-section regen latency by 30–180 s.
        section_task = asyncio.create_task(
            _gen_one_section(
                cfg, proj, full_toon, summary, convo_text, model,
                project_id=project_id, prior_sections=prior_sections,
                analysis_digest=analysis_digest, fast_mode=True,
                agent_key="srs.regenerate",   # iter-13.76 — regenerate → Opus
            )
        )
        # Initial start event so the UI can show "Regenerating <label>…".
        yield "data: " + json.dumps({
            "type": "section_start",
            "section": section_key,
            "label": cfg["label"],
            "ts": datetime.now(timezone.utc).isoformat(),
        }) + "\n\n"

        # Heartbeat loop. 4 s cadence — well inside every common proxy /
        # ingress idle timeout (15–60 s) and visible as keep-alive traffic
        # to every SSE-aware reverse proxy.
        while not section_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(section_task), timeout=4.0)
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({
                    "type": "ping",
                    "section": section_key,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }) + "\n\n"
            except Exception:  # noqa: BLE001 — task raised; unwrap on the await below
                break

        try:
            r = await section_task
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)[:240]
            logger.warning("SRS single-section regen %s failed: %s", section_key, err_msg)
            yield "data: " + json.dumps({
                "type": "error",
                "section": section_key,
                "message": err_msg,
            }) + "\n\n"
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        new_version = int(doc.get("version", 0) or 0) + 1
        try:
            await srs_documents.update_one(
                {"project_id": project_id},
                {"$set": {
                    f"sections.{section_key}": r["content"],
                    "version": new_version,
                    "updated_at": now_iso,
                }},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SRS single-section regen persist of %s failed: %s", section_key, exc)
        try:
            await audit_log.insert_one({
                "action": "srs.regenerate_section",
                "project_id": project_id,
                "section": section_key,
                "model_requested": model,
                "model_used": r.get("model_used", ""),
                "tokens": r.get("tokens", 0),
                "version": new_version,
                "ts": now_iso,
                "stream": True,
            })
        except Exception:  # noqa: BLE001
            pass

        yield "data: " + json.dumps({
            "type": "complete",
            "section": section_key,
            "label": cfg["label"],
            "content": r["content"],
            "tokens": r.get("tokens", 0),
            "model_requested": model,
            "model_used": r.get("model_used", ""),
            "version": new_version,
            "failed": _is_failed_section(r["content"]),
        }) + "\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/freeze")
async def freeze_srs(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    user = payload.get("user", "system")
    srs_doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not srs_doc:
        raise HTTPException(404, "SRS not found")

    # iter-14.11 — Hard-block freeze when mean per-section confidence is
    # below FREEZE_MIN_CONFIDENCE (default 95). Uses the retry-loop's
    # `sections_meta.<key>.final_score` because those numbers were
    # produced by the same judge that gated regeneration. Falls back to
    # the `stage_confidence` collection when meta is missing (e.g. an
    # SRS generated before iter-14.11).
    #
    # Override is a typed-confirmation identical to the RESET / FREEZE
    # pattern already used elsewhere — the payload must carry
    # `override: "OVERRIDE"` verbatim.
    override_raw = str(payload.get("override") or "").strip()
    override_ok = override_raw == "OVERRIDE"
    section_meta = (srs_doc.get("sections_meta") or {})
    scored_finals: list[float] = []
    plateaued_keys: list[str] = []
    for _k, _m in section_meta.items():
        if not isinstance(_m, dict):
            continue
        try:
            _fs = float(_m.get("final_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            _fs = 0.0
        scored_finals.append(_fs)
        if _m.get("plateaued"):
            plateaued_keys.append(_k)
    mean_meta_score = round(sum(scored_finals) / len(scored_finals), 2) if scored_finals else None
    # Legacy fallback — pre-14.11 SRS docs have no meta.
    legacy_overall = None
    if mean_meta_score is None:
        try:
            from db import stage_confidence as _sc_col
            _sc_doc = await _sc_col.find_one(
                {"project_id": project_id, "stage": "Discovery"},
                {"_id": 0, "overall_score": 1},
            )
            if _sc_doc and _sc_doc.get("overall_score") is not None:
                legacy_overall = round(float(_sc_doc["overall_score"] or 0.0), 2)
        except Exception:
            legacy_overall = None
    effective_score = mean_meta_score if mean_meta_score is not None else legacy_overall
    if (
        effective_score is not None
        and effective_score < FREEZE_MIN_CONFIDENCE
        and not override_ok
    ):
        # Record the refusal so we can trace override abuse later.
        try:
            await audit_log.insert_one({
                "action": "srs.freeze.blocked",
                "project_id": project_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "details": {
                    "by": user,
                    "effective_score": effective_score,
                    "threshold": FREEZE_MIN_CONFIDENCE,
                    "source": "sections_meta" if mean_meta_score is not None else "stage_confidence",
                    "plateaued_sections": plateaued_keys,
                    "override_supplied": bool(override_raw),
                },
            })
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=422,
            detail={
                "error": "SRS_CONFIDENCE_BELOW_THRESHOLD",
                "stage": "srs",
                "effective_score": effective_score,
                "threshold": FREEZE_MIN_CONFIDENCE,
                "plateaued_sections": plateaued_keys,
                "source": "sections_meta" if mean_meta_score is not None else "stage_confidence",
                "message": (
                    f"SRS mean per-section confidence is {effective_score:.1f}% "
                    f"(threshold {FREEZE_MIN_CONFIDENCE:.0f}%). "
                    "Click *Regenerate* on the amber-tinted sections until each "
                    "clears the threshold, or resubmit with "
                    "`override:\"OVERRIDE\"` to freeze anyway "
                    "(mirrors the RESET / FREEZE typed-confirmation pattern)."
                ),
            },
        )

    # iter-13.30 — Thumb rule: 100% legacy business rules must be reflected
    # in the SRS before it can be frozen. Computes BR coverage from the
    # `legacy_analysis.business_rules[]` list against the frozen sections'
    # text. Hard-blocks when `LAMA_BR_ENFORCE=1`; warn-only otherwise.
    from kb.br_tracker import assert_coverage_or_warn, BRCoverageError
    section_texts = [
        (v or "") for v in ((srs_doc.get("sections") or {}).values())
    ]
    try:
        br_coverage = await assert_coverage_or_warn(project_id, "srs", section_texts)
    except BRCoverageError as bce:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "BR_COVERAGE_BELOW_THRESHOLD",
                "stage": "srs",
                "message": str(bce),
                "coverage": bce.coverage,
            },
        )

    now = datetime.now(timezone.utc).isoformat()
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": {"frozen": True, "frozen_at": now, "frozen_by": user,
                  "updated_at": now, "br_coverage": br_coverage}},
    )
    await projects.update_one(
        {"id": project_id},
        {"$set": {
            "stage_status.Discovery": "frozen",
            "freeze_gates.Discovery": {"at": now, "by": user},
            "updated_at": now,
        }},
    )
    await audit_log.insert_one({
        "action": "srs.freeze",
        "project_id": project_id,
        "at": now,
        "details": {
            "by": user,
            "br_coverage": br_coverage,
            # iter-14.11
            "effective_score": effective_score,
            "threshold": FREEZE_MIN_CONFIDENCE,
            "override_used": bool(override_ok and effective_score is not None
                                  and effective_score < FREEZE_MIN_CONFIDENCE),
            "plateaued_sections": plateaued_keys,
        },
    })

    # ------------------------------------------------------------------
    # Pipeline handoff: persist a StageContext snapshot for downstream stages.
    # Best-effort — never fail the freeze if the snapshot build hiccups.
    # ------------------------------------------------------------------
    try:
        entities = await kb_entities.find({"project_id": project_id}, {"_id": 0}).to_list(100000)
        toon_doc = await kb_toon.find_one({"project_id": project_id}, {"_id": 0})
        proj = await projects.find_one({"id": project_id}, {"_id": 0})

        owl = export_owl(proj or {}, entities, (srs_doc or {}).get("sections", {}))

        domain_map = owl.get("data_model_hints", {}).get("domains", {})
        high_risk = owl.get("data_model_hints", {}).get("high_risk_tables", [])
        boundaries = owl.get("microservice_hints", {}).get("suggested_boundaries", [])
        stats = (toon_doc or {}).get("stats", {})

        tables_only = [e for e in entities if e.get("type") == "TABLE"]
        classes_only = [e for e in entities if e.get("type") == "CLASS"]
        key_tables = sorted(tables_only, key=lambda t: len(t.get("fks") or []), reverse=True)[:80]
        key_classes = sorted(classes_only, key=lambda c: len(c.get("methods") or []), reverse=True)[:60]
        toon_summary = toon_serialise(key_tables + key_classes)[:8000]

        er_nodes = []
        er_edges = []
        edge_set: set[str] = set()
        for e in tables_only:
            parts = e.get("name", "").split("_")
            domain = parts[0] if len(parts) > 1 else "other"
            er_nodes.append({
                "id": e.get("name"),
                "name": e.get("name"),
                "pk": e.get("pk", ""),
                "domain": domain,
                "col_count": len(e.get("columns") or []),
                "fk_count": len(e.get("fks") or []),
            })
            for fk in (e.get("fks") or []):
                key = f"{e.get('name')}.{fk.get('column')}->{fk.get('ref_table')}"
                if key not in edge_set:
                    edge_set.add(key)
                    er_edges.append({
                        "from_table": e.get("name"),
                        "from_col": fk.get("column"),
                        "to_table": fk.get("ref_table"),
                        "type": "fk",
                    })

        # iter-13.17 — pull the deep legacy-logic analysis (if it exists)
        # so we can persist it into StageContext for downstream stages.
        legacy_analysis_doc = await legacy_analysis_col.find_one(
            {"project_id": project_id}, {"_id": 0}
        )
        legacy_handoff: dict = {}
        if legacy_analysis_doc:
            legacy_handoff = {
                "legacy_analysis": legacy_analysis_doc.get("analysis") or {},
                "legacy_analysis_meta": {
                    "version": legacy_analysis_doc.get("version", 1),
                    "model_used": legacy_analysis_doc.get("model_used", ""),
                    "tokens": legacy_analysis_doc.get("tokens", 0),
                    "updated_at": legacy_analysis_doc.get("updated_at", ""),
                    "coverage": legacy_analysis_doc.get("coverage", {}),
                },
            }

        ctx = StageContext(
            project_id=project_id,
            stage="Discovery",
            frozen_at=now,
            frozen_by=user,
            version=(srs_doc or {}).get("version", 1),
            outputs={
                "srs_sections": (srs_doc or {}).get("sections", {}),
                "srs_version": (srs_doc or {}).get("version", 1),
                "kb_summary": stats,
                "domain_map": {
                    k: {
                        "tables": v.get("tables", [])[:30],
                        "classes": list(set(v.get("classes", [])))[:15],
                    }
                    for k, v in domain_map.items()
                },
                "high_risk_entities": high_risk[:20],
                "suggested_service_boundaries": boundaries,
                "er_model": {
                    "nodes": er_nodes,
                    "edges": er_edges,
                    "stats": {
                        "total_tables": len(er_nodes),
                        "total_relationships": len(er_edges),
                        "domains": len(domain_map),
                    },
                },
                "owl_export_endpoint": f"/api/kb/{project_id}/owl-export",
                "data_model_hints": owl.get("data_model_hints", {}),
                "microservice_hints": owl.get("microservice_hints", {}),
                # iter-13.17 — deep legacy-logic analysis lifted into the
                # stage handoff so DataModel / Architecture / CodeGen all
                # read the same structured workflows, business rules,
                # state machines, integrations and user journeys without
                # re-deriving them from raw KB. This is the "proper
                # knowledgebase for future code generation" promised at
                # iter-13.17: workflows -> sequence diagrams + service
                # handlers, business_rules -> validators, integrations
                # -> outbound clients, data_flows -> repo methods.
                **legacy_handoff,
            },
            toon_summary=toon_summary,
            sources={
                "kb_entities_count": len(entities),
                "kb_stats": stats,
                "srs_version": (srs_doc or {}).get("version", 1),
                "model_used": "multi-model",
                "prompts_used": ["srs.generate", "srs.gap_question", "legacy.deep_analyzer"],
            },
        )

        await stage_context_col.update_one(
            {"project_id": project_id, "stage": "Discovery"},
            {"$set": ctx.model_dump()},
            upsert=True,
        )
        await projects.update_one(
            {"id": project_id},
            {"$set": {
                "stage_status.DataModel": "available",
                "updated_at": now,
            }},
        )
    except Exception as exc:
        # Pipeline handoff is best-effort — surface in audit log but don't fail the freeze.
        await audit_log.insert_one({
            "action": "stage_context.build_failed",
            "project_id": project_id,
            "at": now,
            "details": {"stage": "Discovery", "error": str(exc)[:500]},
        })

    return {"ok": True, "frozen_at": now}


@router.post("/unfreeze")
async def unfreeze_srs(payload: dict):
    project_id = payload.get("project_id")
    set_current_project_id(project_id)  # iter-13.38
    now = datetime.now(timezone.utc).isoformat()
    await srs_documents.update_one(
        {"project_id": project_id},
        {"$set": {"frozen": False, "updated_at": now}},
    )
    await projects.update_one(
        {"id": project_id},
        {"$set": {"stage_status.Discovery": "active", "updated_at": now}},
    )
    return {"ok": True}


# iter-14.25.9 — SRS-only reset. Wipes the entire SRS document + freeze
# gate + downstream stage_context so the user can start §1..§12 from a
# clean slate WITHOUT nuking the KB / analysis_digest / chat / journey
# artefacts (unlike factory_reset in datamodel.py). Typed-confirmation
# guarded — mirrors the RESET / FREEZE pattern from other stages.
@router.post("/{project_id}/reset")
async def reset_srs(project_id: str, payload: dict | None = None):
    """Wipe the SRS document and start Discovery over.

    Preserves: KB (kb_files/kb_chunks/kb_entities/kb_toon), chat
    (conversations/messages), analysis_digest (legacy_analysis), journey
    KB (kb_graph / business_ontologies / ontology_snapshots), Qdrant
    vectors, and project metadata / detected_tech.

    Wipes:    srs_documents, freeze_gates(Discovery), stage_context
    (Discovery + every downstream stage — they're only valid if
    Discovery is frozen), stage_confidence(Discovery), any in-flight
    SRS background job, plus arch/codegen/living artifacts (all
    downstream and now stale).

    Typed confirmation required in payload: {"confirm": "RESET"}.
    """
    payload = payload or {}
    confirm = str(payload.get("confirm", "")).strip().upper()
    if confirm != "RESET":
        raise HTTPException(400, "Typed confirmation required: send {\"confirm\": \"RESET\"}.")

    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    set_current_project_id(project_id)
    now = datetime.now(timezone.utc).isoformat()

    # 1) Kill any in-flight SRS job (mirrors factory_reset in datamodel.py).
    killed_job = False
    try:
        job = _SRS_JOBS.get(project_id)
        if job:
            try:
                job.get("control", {})["cancelled"] = True
                job.get("control", {})["paused"] = False
            except Exception:  # noqa: BLE001
                pass
            try:
                t = job.get("task")
                if t and not t.done():
                    t.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                broadcast = job.get("broadcast")
                if broadcast:
                    await broadcast({
                        "type": "run_aborted",
                        "reason": "srs_reset",
                        "message": "SRS was reset; run cancelled.",
                    })
                    await broadcast({"type": "__end__"})
            except Exception:  # noqa: BLE001
                pass
            _SRS_JOBS.pop(project_id, None)
            killed_job = True
    except Exception:  # noqa: BLE001
        pass

    deleted: dict[str, int | str] = {}

    # 2) SRS document itself.
    try:
        r = await srs_documents.delete_many({"project_id": project_id})
        deleted["srs_documents"] = r.deleted_count
    except Exception as e:  # noqa: BLE001
        deleted["srs_documents"] = f"err: {e}"

    # 3) Freeze gates for Discovery + every downstream stage.
    try:
        from db import freeze_gates as _fg
        r = await _fg.delete_many({"project_id": project_id})
        deleted["freeze_gates"] = r.deleted_count
    except Exception as e:  # noqa: BLE001
        deleted["freeze_gates"] = f"err: {e}"

    # 4) StageContext(Discovery) + every downstream (they only exist
    # because Discovery was frozen — no Discovery = no downstream).
    try:
        r = await stage_context_col.delete_many({"project_id": project_id})
        deleted["stage_context"] = r.deleted_count
    except Exception as e:  # noqa: BLE001
        deleted["stage_context"] = f"err: {e}"

    # 5) Stage confidence snapshots (all stages — every downstream
    # stage's confidence is grounded in a now-deleted Discovery).
    try:
        from db import stage_confidence as _sc
        r = await _sc.delete_many({"project_id": project_id})
        deleted["stage_confidence"] = r.deleted_count
    except Exception as e:  # noqa: BLE001
        deleted["stage_confidence"] = f"err: {e}"

    # 6) Downstream artefacts — DataModel / Architecture / CodeGen /
    # Living all lose their upstream contract when SRS is wiped.
    try:
        from db import (
            data_models as _dm, bus_matrix as _bm, olap_models as _olap,
            migration_artifacts as _ma,
            arch_documents as _ad, arch_services as _as,
            codegen_files as _cf, codegen_runs as _cr,
            living_artifacts as _la, living_runs as _lr,
        )
        for col, name in (
            (_dm, "data_models"),
            (_bm, "bus_matrix"),
            (_olap, "olap_models"),
            (_ma, "migration_artifacts"),
            (_ad, "arch_documents"),
            (_as, "arch_services"),
            (_cf, "codegen_files"),
            (_cr, "codegen_runs"),
            (_la, "living_artifacts"),
            (_lr, "living_runs"),
        ):
            try:
                r = await col.delete_many({"project_id": project_id})
                deleted[name] = r.deleted_count
            except Exception as e:  # noqa: BLE001
                deleted[name] = f"err: {e}"
    except Exception as e:  # noqa: BLE001
        deleted["downstream_import"] = f"err: {e}"

    # 7) Reset stage_status: Discovery active, everything below locked.
    # Also clear the persisted freeze_gates map on the project doc.
    await projects.update_one(
        {"id": project_id},
        {"$set": {
            "stage": "Discovery",
            "stage_status": {
                "Discovery": "active",
                "DataModel": "locked",
                "Architecture": "locked",
                "CodeGen": "locked",
                "Living": "locked",
            },
            "freeze_gates": {},
            "updated_at": now,
        }},
    )

    await audit_log.insert_one({
        "action": "srs.reset",
        "project_id": project_id,
        "at": now,
        "details": {**deleted, "killed_job": killed_job},
    })

    return {"ok": True, "deleted": deleted, "killed_job": killed_job, "at": now}


@router.get("/{project_id}/export.pdf")
async def export_pdf(project_id: str):
    doc = await srs_documents.find_one({"project_id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "SRS not found")
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    pdf_bytes = _render_pdf(proj, doc)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="SRS_{proj.get("name","project")}.pdf"'},
    )


def _render_pdf(project: dict, doc: dict) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
        ListFlowable, ListItem,
    )
    from reportlab.lib.units import cm

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"SRS - {project.get('name','')}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=22, spaceAfter=20, textColor=colors.HexColor("#0A2540"))
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceBefore=16, spaceAfter=10, textColor=colors.HexColor("#0A2540"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#0A2540"))
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#1f2937"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=4)

    def _inline(text: str) -> str:
        """Convert inline markdown to ReportLab mini-HTML.

        Code spans must be processed FIRST and protected by placeholders so
        that the bold / italic regexes can't match asterisks living inside
        a code span. Without this protection, input like ``com.ahct.*``
        produces overlapping tags (`<font>com.ahct.*</font>` followed by
        another `*` elsewhere → an `<i>` straddles a `</font>`) and
        reportlab's paragraph parser throws
        `Parse error: saw </font> instead of expected </i>`  — which used
        to 500 the entire PDF export.  (Iter-13.9 fix.)
        """
        t = text
        # 1) Pull code spans out, replace with opaque placeholders.
        code_slots: list[str] = []

        def _stash_code(m):
            inner = m.group(1).replace("<", "&lt;").replace(">", "&gt;")
            code_slots.append(f'<font face="Courier">{inner}</font>')
            return f"\x00CODE{len(code_slots) - 1}\x00"

        t = _re.sub(r"`([^`\n]+)`", _stash_code, t)
        # 2) Bold then italic — asterisks inside code spans are now gone.
        t = _re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
        t = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", t)
        # 3) Scrub orphan emphasis chars that survived (un-paired `*` or
        #    `_`). MUST run BEFORE we restore code spans, otherwise we'd
        #    strip the asterisks the user typed inside backticks. Once a
        #    stray `*` sneaks next to a `<font>` boundary the reportlab
        #    mini-XML parser blows up with `saw </font> instead of </i>`.
        t = t.replace("*", "").replace("_", " ")
        # 4) Restore code spans (asterisks inside are preserved verbatim).
        for idx, repl in enumerate(code_slots):
            t = t.replace(f"\x00CODE{idx}\x00", repl)
        return t

    def _safe_para(text: str, style):
        """Build a Paragraph that never raises.

        ReportLab's mini-XML parser is strict about balanced tags. If the
        markdown→XML conversion produces something it dislikes (overlapping
        emphasis, unclosed `<font>`, unexpected entities …), fall back to a
        fully-escaped plain-text paragraph rather than 500-ing the whole
        export. The error is logged so it can be diagnosed.
        """
        import html as _html
        try:
            return Paragraph(text, style)
        except Exception as exc:  # noqa: BLE001 — defensive
            try:
                logger.warning("PDF paragraph fallback (parse error): %s | text=%r", exc, text[:200])
            except Exception:
                pass
            # Strip every <tag> first, then escape — guarantees a clean
            # body even when the upstream text was already pre-tagged.
            stripped = _re.sub(r"<[^>]+>", "", text)
            try:
                return Paragraph(_html.escape(stripped), style)
            except Exception:  # noqa: BLE001 — last resort
                return Paragraph("[paragraph could not be rendered]", style)


    def _render_table(md_lines: list[str]) -> Table | None:
        """Convert a contiguous markdown table block (pipe-delimited) to a ReportLab Table."""
        rows: list[list[str]] = []
        for ln in md_lines:
            if _re.match(r"^\|[\s\-:|]+\|$", ln.strip()):
                continue  # separator row
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows.append(cells)
        if not rows:
            return None
        # Wrap each cell content in Paragraph for wrapping
        wrapped = [[_safe_para(_inline(c) or "&nbsp;", body) for c in r] for r in rows]
        # Explicit equal-width columns instead of auto-size — without this
        # ReportLab's auto-sizer can produce a column narrower than its own
        # padding and raises `ValueError: flowable given negative
        # availWidth`. Frame width for Letter @ 36pt margins ≈ 540pt.
        ncols = max(len(r) for r in rows) if rows else 1
        col_w = 530.0 / ncols if ncols else None
        # splitByRow=1 + splitInRow=1 lets ReportLab break a tall cell across
        # pages instead of raising LayoutError("Flowable … too large on
        # page"). Required because LLM-generated SRS tables (e.g. the long
        # field-traceability table in Section 5) frequently have one row
        # whose wrapped height exceeds the printable area.  (Iter-13.9 fix.)
        tbl = Table(
            wrapped,
            colWidths=[col_w] * ncols if col_w else None,
            repeatRows=1, hAlign="LEFT",
            splitByRow=1, splitInRow=1,
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A2540")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            # Padding kept TINY so reportlab's auto-sizer never produces a
            # cell where left+right padding > column width (raises
            # `ValueError: flowable given negative availWidth`).
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return tbl

    def _md_to_flowables(md: str) -> list:
        out: list = []
        if not md:
            out.append(_safe_para("<i>(empty)</i>", body))
            return out
        lines = md.split("\n")
        i = 0
        bullet_buf: list[str] = []
        table_buf: list[str] = []

        def _flush_list():
            nonlocal bullet_buf
            if bullet_buf:
                items = [ListItem(_safe_para(_inline(b), body), leftIndent=6) for b in bullet_buf]
                out.append(ListFlowable(items, bulletType="bullet", leftIndent=12, bulletFontSize=8))
                out.append(Spacer(1, 4))
                bullet_buf = []

        def _flush_table():
            nonlocal table_buf
            if table_buf:
                t = _render_table(table_buf)
                if t is not None:
                    out.append(t)
                    out.append(Spacer(1, 6))
                table_buf = []

        while i < len(lines):
            ln = lines[i].rstrip()
            # tables: contiguous block of lines starting with '|'
            if ln.lstrip().startswith("|"):
                _flush_list()
                table_buf.append(ln)
                i += 1
                continue
            else:
                _flush_table()

            if ln.startswith("### "):
                _flush_list()
                out.append(_safe_para(_inline(ln[4:].strip()), h3))
            elif ln.startswith("## "):
                _flush_list()
                out.append(_safe_para(_inline(ln[3:].strip()), h2))
            elif ln.startswith("# "):
                _flush_list()
                out.append(_safe_para(_inline(ln[2:].strip()), h1))
            elif _re.match(r"^\s*[-*]\s+", ln):
                bullet_buf.append(_inline(_re.sub(r"^\s*[-*]\s+", "", ln)))
            elif _re.match(r"^\s*\d+\.\s+", ln):
                bullet_buf.append(_inline(_re.sub(r"^\s*\d+\.\s+", "", ln)))
            elif ln.strip() == "":
                _flush_list()
                out.append(Spacer(1, 4))
            else:
                _flush_list()
                out.append(_safe_para(_inline(ln), body))
            i += 1

        _flush_list()
        _flush_table()
        return out

    elements: list = []
    elements.append(_safe_para("Software Requirements Specification", title_style))
    elements.append(_safe_para(f"<b>Project:</b> {project.get('name','')}", body))
    elements.append(_safe_para(f"<b>Source:</b> {project.get('source_tech','')}  &rarr;  <b>Target:</b> {project.get('target_tech','')}", body))
    elements.append(_safe_para(f"<b>Version:</b> {doc.get('version',1)} &nbsp;|&nbsp; <b>Frozen:</b> {doc.get('frozen', False)}", body))
    if doc.get("frozen_at"):
        elements.append(_safe_para(f"<b>Frozen at:</b> {doc.get('frozen_at')} by {doc.get('frozen_by','')}", body))
    elements.append(Spacer(1, 16))

    # Derived from SECTION_CONFIGS — single source of truth for order/labels.
    section_order = [(c["key"], c["label"]) for c in SECTION_CONFIGS]
    sections = doc.get("sections", {})
    # Backwards-compat: if the SRS doc was generated before the IEEE 29148
    # restructure, fill missing new keys from legacy ones so the PDF still
    # has content (rather than blank pages) for those upgrade-era documents.
    for old_key, new_key in LEGACY_SECTION_ALIAS.items():
        if old_key in sections and not sections.get(new_key):
            sections[new_key] = sections[old_key]
    for idx, (key, label) in enumerate(section_order):
        if idx > 0:
            elements.append(PageBreak())
        elements.append(_safe_para(label, h1))
        if key == "entity_model":
            # ER section is JSON in storage — render as proper tables in the PDF
            # so freezable SRS exports show the entity inventory + relationship
            # matrix instead of just a one-line summary.
            raw = sections.get(key) or ""
            er = {}
            try:
                er = json.loads(raw) if raw else {}
            except Exception:
                er = {}
            nodes = (er or {}).get("nodes", []) or []
            edges = (er or {}).get("edges", []) or []
            stats = (er or {}).get("stats", {}) or {}
            domains_map = (er or {}).get("domains", {}) or {}
            tables_n = stats.get("total_tables", len(nodes))
            rels_n = stats.get("total_relationships", len(edges))
            domains_n = stats.get("domains", len(domains_map))

            elements.append(_safe_para(
                f"Entity Relationship Model: <b>{tables_n}</b> tables across "
                f"<b>{domains_n}</b> domains with <b>{rels_n}</b> foreign-key "
                "relationships. The interactive ER diagram is available in the "
                "LAMA Discovery page.",
                body,
            ))
            elements.append(Spacer(1, 8))

            # ---- 12.1 Entities table -------------------------------------
            if nodes:
                elements.append(_safe_para("12.1 Entities", h2))
                ent_rows = [["Table", "Domain", "Primary Key", "Columns", "FKs"]]
                for n in sorted(nodes, key=lambda x: (x.get("domain", ""), x.get("name", ""))):
                    ent_rows.append([
                        str(n.get("name", "")),
                        str(n.get("domain", "")),
                        str(n.get("pk", "") or "—"),
                        str(n.get("col_count", len(n.get("columns") or []))),
                        str(n.get("fk_count", 0)),
                    ])
                wrapped = [
                    [_safe_para(_inline(c) or "&nbsp;", body) for c in r]
                    for r in ent_rows
                ]
                ent_tbl = Table(
                    wrapped,
                    colWidths=[170, 110, 110, 70, 70],
                    repeatRows=1, hAlign="LEFT",
                    splitByRow=1, splitInRow=1,
                )
                ent_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A2540")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                elements.append(ent_tbl)
                elements.append(Spacer(1, 12))

            # ---- 12.2 Relationships table --------------------------------
            if edges:
                elements.append(_safe_para("12.2 Relationships (Foreign Keys)", h2))
                rel_rows = [["From Table", "From Column", "→", "To Table", "To Column", "Cardinality"]]
                for e in sorted(edges, key=lambda x: (x.get("from_table", ""), x.get("from_col", ""))):
                    rel_rows.append([
                        str(e.get("from_table", "")),
                        str(e.get("from_col", "")),
                        "→",
                        str(e.get("to_table", "")),
                        str(e.get("to_col", "") or "id"),
                        str(e.get("cardinality", "many-to-one")),
                    ])
                wrapped = [
                    [_safe_para(_inline(c) or "&nbsp;", body) for c in r]
                    for r in rel_rows
                ]
                rel_tbl = Table(
                    wrapped,
                    colWidths=[120, 100, 20, 120, 80, 90],
                    repeatRows=1, hAlign="LEFT",
                    splitByRow=1, splitInRow=1,
                )
                rel_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A2540")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (2, 0), (2, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                elements.append(rel_tbl)
                elements.append(Spacer(1, 12))

            # ---- 12.3 Domain breakdown ----------------------------------
            if domains_map:
                elements.append(_safe_para("12.3 Domain Breakdown", h2))
                dom_rows = [["Domain", "Table Count", "Tables"]]
                for dname, dinfo in sorted(domains_map.items()):
                    tbls = (dinfo or {}).get("tables", []) if isinstance(dinfo, dict) else []
                    dom_rows.append([
                        str(dname),
                        str(len(tbls)),
                        ", ".join(tbls[:25]) + (" …" if len(tbls) > 25 else ""),
                    ])
                wrapped = [
                    [_safe_para(_inline(c) or "&nbsp;", body) for c in r]
                    for r in dom_rows
                ]
                dom_tbl = Table(
                    wrapped,
                    colWidths=[120, 80, 330],
                    repeatRows=1, hAlign="LEFT",
                    splitByRow=1, splitInRow=1,
                )
                dom_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A2540")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                elements.append(dom_tbl)

            if not nodes and not edges:
                elements.append(_safe_para(
                    "<i>No entities discovered in the KB — run Build KB first.</i>",
                    body,
                ))
            continue
        # Per-section safety net — if a section's flowables cause a
        # ReportLab layout error at build-time, swap them for a
        # safe-paragraph dump so the rest of the PDF still renders.
        # Layout errors only surface in `pdf.build` AFTER this loop, so
        # also wrap the final `pdf.build` below.
        elements.extend(_md_to_flowables(sections.get(key) or ""))

    try:
        pdf.build(elements)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("PDF build() failed (%s) — retrying with plain-text fallback for every section.", exc)
        # Rebuild with EVERY section rendered as escaped plain text — no
        # markdown, no tables, no inline formatting. Guaranteed to lay out.
        import html as _html
        buf = io.BytesIO()
        pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=48, bottomMargin=36)
        elements = [
            _safe_para("Software Requirements Specification", title_style),
            _safe_para(f"<b>Project:</b> {project.get('name','')}", body),
            _safe_para(f"<b>Source:</b> {project.get('source_tech','')}  &rarr;  <b>Target:</b> {project.get('target_tech','')}", body),
            Spacer(1, 16),
        ]
        for idx, (key, label) in enumerate(section_order):
            if idx > 0:
                elements.append(PageBreak())
            elements.append(_safe_para(label, h1))
            raw = sections.get(key) or "(empty)"
            # split into paragraphs by blank line so output is at least readable.
            for chunk in raw.split("\n\n"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                elements.append(_safe_para(_html.escape(chunk).replace("\n", "<br/>"), body))
                elements.append(Spacer(1, 4))
        pdf.build(elements)
    return buf.getvalue()
