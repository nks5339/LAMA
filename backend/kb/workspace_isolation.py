"""iter-13.99.1 — Shared project-isolation primitives (workspace +
sanitizer).

This module is the SINGLE source of truth for the project-isolated
workspace + cross-project leak sanitizer. It was extracted from
``routes/srs.py`` so every stage (SRS, Architecture, CodeGen,
DataModel, Living, Chat) can apply the same hard guard without
duplicating logic — and so the guard can be hooked at the
``llm.fabric_call`` layer to give every LLM call site automatic
output sanitization.

Forensic history (do not delete):

    Iters 13.90 / 13.92 / 13.94 / 13.95 / 13.98 layered ever-stronger
    sanitizers but all were SUFFIX matchers. With ~700 files in a
    typical legacy KB, basename / 2-component-suffix collisions on
    generic names like ``LoginAction.java`` were inevitable, so
    foreign paths like ``src/com/ahct/login/.../LoginAction.java``
    kept leaking into TJHS SRS.

    iter-13.99 (in routes/srs.py) introduced the HARD PREFIX guard:
    every multi-component path must be rooted at
    ``/lama-workspaces/{tid}__{pid}/`` or it is unconditionally
    redacted. This module makes that guard project-wide.
"""
from __future__ import annotations

import logging
import re as _re
from typing import Iterable, Optional

logger = logging.getLogger("lama.workspace_isolation")


# ── 1. PATH REGEX & EXTENSIONS ──────────────────────────────────────────
# Conservative on purpose — we don't want to redact arbitrary `.md` /
# `.txt` mentions.
_PATH_EXTENSIONS: tuple[str, ...] = (
    "java", "py", "php", "phtml", "jsp", "jspx", "rb", "go", "kt", "kts",
    "scala", "groovy", "cs", "vb", "fs", "ts", "tsx", "js", "jsx", "mjs",
    "cjs", "sql", "ddl", "pks", "pkb", "plb", "prc", "fnc", "trg", "xml",
    "xsd", "wsdl", "yaml", "yml", "properties", "conf", "ini", "json",
    "html", "htm", "vue", "svelte", "ftl", "vm", "twig", "blade", "erb",
    "cbl", "cob", "for", "f90", "pas", "rs",
)

# Match a path-like token: ≥1 `/`, ends with a known source extension,
# optional `:LINE` or `::function_name` suffix. Word-bounded so prose
# fractions / URLs don't false-positive.
PATH_REGEX = _re.compile(
    r"(?<![\w./-])"
    r"([A-Za-z0-9_./\\-]+/[A-Za-z0-9_./\\-]+"
    r"\.(?:" + "|".join(_PATH_EXTENSIONS) + r")"
    r"(?:::[A-Za-z_][A-Za-z0-9_]*|:\d+(?:-\d+)?)?)"
    r"(?![\w-])"
)

# Cap suffix expansion at 8 components — beyond that we burn memory
# for diminishing accuracy.
_MAX_SUFFIX_DEPTH = 8


# ── 2. PROJECT WORKSPACE ROOT ──────────────────────────────────────────
def workspace_slug(s: str) -> str:
    """Slug a tenant_id / project_id for safe use in a pseudo-path."""
    return (s or "default").strip() or "default"


def workspace_root(tenant_id: str, project_id: str) -> str:
    """The canonical per-(tenant, project) pseudo-path used in EVERY
    LAMA-generated artifact (SRS, Architecture, CodeGen, DataModel,
    Living). Citations rooted here are the only legitimate ones; the
    hard guard rejects everything else.

    NOT a real filesystem path. It's the namespace label the prompt
    uses to anchor citations and the prefix the sanitizer matches on.
    """
    return f"/lama-workspaces/{workspace_slug(tenant_id)}__{workspace_slug(project_id)}"


# ── 3. PATH NORMALISATION + SUFFIX EXPANSION ───────────────────────────
def normalize_path(p: str) -> list[str]:
    """Return the path's components, lowercased, separators normalised."""
    s = (p or "").replace("\\", "/").strip().strip("/").lower()
    if not s:
        return []
    return [c for c in s.split("/") if c]


def path_suffixes(components: list[str],
                  max_depth: int = _MAX_SUFFIX_DEPTH) -> list[tuple[str, ...]]:
    """All suffixes of `components`, from 1 component up to
    min(len, max_depth)."""
    n = len(components)
    if not n:
        return []
    upper = min(n, max_depth)
    return [tuple(components[n - depth:]) for depth in range(1, upper + 1)]


def allowed_basenames(filenames: "Iterable[str]") -> set[str]:
    """Basename-only set used as the 1-component fallback (bare basename
    mentions in prose)."""
    out: set[str] = set()
    for f in filenames or ():
        if not f:
            continue
        comps = normalize_path(f)
        if comps:
            out.add(comps[-1])
    return out


def allowed_path_suffixes(filenames: "Iterable[str]") -> set[tuple[str, ...]]:
    """All suffix tuples (depth 1..8) for every kb_files filename. The
    sanitizer matches against this for the soft directory-discriminated
    fall-back path."""
    out: set[tuple[str, ...]] = set()
    for f in filenames or ():
        if not f:
            continue
        comps = normalize_path(f)
        for suf in path_suffixes(comps):
            out.add(suf)
    return out


# ── 4. THE TWO GUARDS ──────────────────────────────────────────────────
def is_workspace_path(path: str, ws_root: str) -> bool:
    """True iff `path` is rooted at `ws_root` (case-insensitive,
    separator-normalised)."""
    if not path or not ws_root:
        return False
    p = path.replace("\\", "/").strip().lower()
    r = ws_root.replace("\\", "/").strip().lower().rstrip("/")
    return p == r or p.startswith(r + "/")


def enforce_workspace_prefix(
    text: str,
    ws_root: str,
    allowed_bn: set[str],
    *,
    placeholder: str = "[redacted-foreign-path]",
) -> tuple[str, int]:
    """HARD GUARD — when a project has a workspace root, EVERY
    multi-component path in `text` must be rooted at `ws_root`.
    Bare basenames in prose fall back to a basename match for
    back-compat with casual mentions.
    """
    if not text or not ws_root:
        return text or "", 0
    redactions = 0
    root_lower = ws_root.replace("\\", "/").strip().lower().rstrip("/")

    def _maybe_redact(m: _re.Match[str]) -> str:
        nonlocal redactions
        tok = m.group(1)
        path_only = _re.split(r"::|:", tok, maxsplit=1)[0]
        comps = normalize_path(path_only)
        if not comps:
            return tok
        if len(comps) == 1:
            if comps[0] in allowed_bn:
                return tok
            redactions += 1
            return placeholder
        if is_workspace_path(path_only, root_lower):
            return tok
        redactions += 1
        return placeholder

    return PATH_REGEX.sub(_maybe_redact, text), redactions


def redact_foreign_paths(
    text: str,
    allowed_bn: set[str],
    *,
    placeholder: str = "[redacted-foreign-path]",
    allowed_suf: Optional[set[tuple[str, ...]]] = None,
) -> tuple[str, int]:
    """SOFT GUARD — directory-discriminated suffix matcher. Used in
    upstream context sources (analysis_digest, convo_text, summary)
    before they're concatenated into the prompt. Strictly weaker than
    ``enforce_workspace_prefix`` (allows any path whose ≥2-component
    suffix matches a kb_files filename) but useful as a first pass.
    """
    if not text:
        return "", 0
    if not allowed_bn and not allowed_suf:
        return text, 0
    redactions = 0
    suf_set = allowed_suf or set()

    def _maybe_redact(m: _re.Match[str]) -> str:
        nonlocal redactions
        tok = m.group(1)
        path_only = _re.split(r"::|:", tok, maxsplit=1)[0]
        comps = normalize_path(path_only)
        if not comps:
            return tok
        if len(comps) == 1:
            if comps[0] in allowed_bn:
                return tok
            redactions += 1
            return placeholder
        for suf in path_suffixes(comps)[1:]:  # depth ≥ 2
            if suf in suf_set:
                return tok
        redactions += 1
        return placeholder

    return PATH_REGEX.sub(_maybe_redact, text), redactions


# ── 5. PROJECT LOOKUPS ─────────────────────────────────────────────────
async def load_allowed_path_index(
    project_id: str, proj: Optional[dict] = None,
) -> tuple[set[str], set[tuple[str, ...]]]:
    """Load (basenames, path_suffixes) for a project, scope-checked by
    (tenant_id, project_id). Returns (set(), set()) on any plumbing
    failure — which disables redaction (the upstream `kb_file_count`
    guard is responsible for the empty-KB case).
    """
    if not project_id:
        return set(), set()
    try:
        from db import kb_files as _kbf, projects as _proj_col
        if proj is None:
            proj = await _proj_col.find_one(
                {"id": project_id}, {"_id": 0, "tenant_id": 1},
            ) or {}
        flt: dict = {"project_id": project_id}
        tid = ((proj or {}).get("tenant_id") or "").strip()
        if tid:
            flt["tenant_id"] = tid
        names: list[str] = []
        async for f in _kbf.find(flt, {"_id": 0, "filename": 1}):
            n = (f.get("filename") or "").strip()
            if n:
                names.append(n)
        return allowed_basenames(names), allowed_path_suffixes(names)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workspace_isolation[%s]: kb_files load failed (%s) — "
            "disabling foreign-path redaction for this call",
            project_id, exc,
        )
        return set(), set()


async def workspace_root_for_project(project_id: str,
                                     proj: Optional[dict] = None) -> str:
    """Look up the project's tenant_id and return the workspace root.

    Returns "" when project_id is empty (sanitizer no-ops in that case).
    """
    if not project_id:
        return ""
    try:
        if proj is None:
            from db import projects as _proj_col
            proj = await _proj_col.find_one(
                {"id": project_id}, {"_id": 0, "tenant_id": 1},
            ) or {}
        return workspace_root((proj or {}).get("tenant_id", ""), project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workspace_isolation[%s]: project lookup failed (%s) — "
            "returning empty workspace root",
            project_id, exc,
        )
        return ""


# ── 6. ONE-CALL OUTPUT SANITIZER (used by llm.fabric_call) ─────────────
async def sanitize_llm_output(
    content: str,
    project_id: str,
    *,
    source_label: str = "llm-output",
) -> str:
    """The single-call output sanitizer hooked into ``llm.fabric_call``.

    For any LLM response that carries a non-empty ``project_id``
    contextvar, run the hard workspace-prefix guard then the soft
    suffix fall-back. Always logs at WARNING so the operator can
    confirm in ``docker logs`` that the guard is firing per-call.

    Best-effort: any plumbing failure returns the original content
    unchanged. We NEVER block an LLM response on the sanitizer.
    """
    if not content or not project_id:
        return content
    try:
        ws_root = await workspace_root_for_project(project_id)
        bn, suf = await load_allowed_path_index(project_id)
        if not ws_root and not bn:
            return content
        cleaned, n_hard = enforce_workspace_prefix(content, ws_root, bn)
        n_soft = 0
        if bn or suf:
            cleaned, n_soft = redact_foreign_paths(
                cleaned, bn, allowed_suf=suf,
            )
        logger.warning(
            "iter-13.99 output-guard · pid=%s · src=%s · "
            "hard_redactions=%d soft_redactions=%d workspace_root=%s",
            project_id, source_label, n_hard, n_soft, ws_root,
        )
        return cleaned
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "iter-13.99 output-guard · pid=%s · src=%s · SANITIZER "
            "CRASHED (%s) — returning content unchanged",
            project_id, source_label, exc,
        )
        return content


# ── 7. STAGE PROMPT DIRECTIVE (re-usable across all stages) ────────────
def workspace_directive_for_prompt(ws_root: str) -> str:
    """A short, stage-agnostic instruction block to PREPEND to any LLM
    system prompt that touches project source files. Tells the model
    the workspace contract — same wording the post-LLM guard enforces.

    Returns "" when ws_root is empty so callers can unconditionally
    concatenate the result.

    iter-13.81.8 — WORDING HARDENED for Factory.ai Droid.
    Previous wording said "files live under <root>/" which Factory Droid
    read literally — it tried to `ls` / `cat` the path, found nothing
    (the path is virtual, not a real disk location), and concluded
    "no legacy source artifacts available" with coverage = 0.

    New wording leads with "VIRTUAL CITATION ROOT — DOES NOT EXIST ON
    DISK", forbids ALL shell tools by name, and points to the inlined
    KB block as the single source of truth.
    """
    if not ws_root:
        return ""
    return (
        "PROJECT-ISOLATED WORKSPACE (iter-13.99 — HARD CONTRACT):\n"
        "\n"
        "  ⚠️  `" + ws_root + "/` IS A VIRTUAL CITATION ROOT, NOT A REAL\n"
        "      FILESYSTEM PATH. It does NOT exist on the Droid Computer's\n"
        "      disk. Attempting `ls`, `cat`, `find`, `tree`, `stat`, `head`,\n"
        "      `grep`, `rg`, `sed`, `awk`, `wc`, or any other filesystem\n"
        "      probe against this path will return 'no such file or\n"
        "      directory' — that is NOT evidence that the project is empty.\n"
        "\n"
        "  ✅  THE REAL SOURCE OF TRUTH IS INLINE IN THIS PROMPT.\n"
        "      Every PHP / SQL / JSP / Java / Python / etc. artifact for\n"
        "      this project is embedded in the KNOWLEDGE BASE, TOON, and\n"
        "      LEGACY WORKSPACE blocks below — built from the project's\n"
        "      MongoDB collections (`kb_files`, `kb_entities`, `kb_toon`,\n"
        "      `kb_graph`) by LAMA's KB engine. Read those blocks. DO NOT\n"
        "      shell out.\n"
        "\n"
        "  📎  CITATION RULE — every file path in your response MUST be\n"
        "      rooted at `" + ws_root + "/...` (this is purely a label —\n"
        "      LAMA stamps it onto outputs to prove they belong to this\n"
        "      tenant+project) OR be a bare basename (e.g. `Foo.java`)\n"
        "      mentioned in prose. Any other prefix — `src/`, `app/`,\n"
        "      `legacy-code/`, `com/<vendor>/...`, or anything you\n"
        "      remember from training data — is a CROSS-PROJECT LEAK\n"
        "      and will be redacted server-side.\n"
        "\n"
        "  ✓  Valid:   `" + ws_root + "/src/com/yourproj/foo/Bar.java:42`\n"
        "  ✗  Invalid: `src/com/anything/Bar.java:42` (redacted)\n"
        "\n"
        "  If the KNOWLEDGE BASE / TOON blocks below appear empty, the KB\n"
        "  ingest genuinely produced zero artifacts — report that honestly\n"
        "  back to the user. Do NOT first try to read the filesystem.\n"
    )


async def workspace_kb_summary_for_prompt(project_id: str) -> str:
    """Return a one-paragraph plain-text summary of the project's KB so
    Factory Droid (and other models) see CONCRETE numbers up-front and
    don't waste a turn trying to verify the workspace via shell tools.

    iter-13.81.8 — added to address the "no files discovered" symptom
    where Factory probed the (non-existent) `/lama-workspaces/.../`
    path and concluded the project was empty.

    iter-13.81.9 — if the project has been materialised onto the Droid
    filesystem (see `kb.factory_materializer`), also surface the REAL
    on-disk path so Factory CAN `ls` / `cat` / `grep` it. This is the
    proper fix for autonomous coding agents that refuse to read inlined
    KB blocks.

    Returns "" when the project is unknown or the KB hasn't been built.
    """
    if not project_id:
        return ""
    try:
        from db import (
            kb_files as _kbf,
            kb_entities as _kbe,
            kb_toon as _kbt,
        )
        # Lightweight aggregations only.
        n_files = await _kbf.count_documents({"project_id": project_id})
        if n_files == 0:
            # Genuinely empty KB → caller decides what to report.
            return ""
        # Per-type entity counts (capped scan).
        type_counts: dict[str, int] = {}
        try:
            cursor = _kbe.find(
                {"project_id": project_id},
                {"_id": 0, "type": 1},
            ).limit(5000)
            async for doc in cursor:
                t = (doc.get("type") or "").upper() or "OTHER"
                type_counts[t] = type_counts.get(t, 0) + 1
        except Exception:  # noqa: BLE001
            type_counts = {}
        toon_doc = await _kbt.find_one(
            {"project_id": project_id}, {"_id": 0, "toon": 1},
        )
        toon_chars = len((toon_doc or {}).get("toon") or "")

        # Order entity types by count desc, take top 10.
        top = sorted(type_counts.items(), key=lambda kv: -kv[1])[:10]
        entity_summary = (
            ", ".join(f"{t}={n}" for t, n in top) if top else "(none)"
        )

        # iter-13.81.9 — surface materialised filesystem path when present.
        materialised_block = ""
        try:
            from kb.factory_materializer import get_materialized_state
            state = await get_materialized_state(project_id)
            if state and state.get("path"):
                materialised_block = (
                    "\n"
                    "  📁  REAL DROID FILESYSTEM PATH (iter-13.81.9 — populated by\n"
                    "      LAMA materialiser; the legacy project IS on disk here):\n"
                    f"        path           : {state['path']}/\n"
                    f"        file_count     : {state.get('file_count', 0)}\n"
                    f"        bytes_on_disk  : {state.get('bytes', 0):,}\n"
                    f"        materialized_at: {state.get('materialized_at', '')}\n"
                    "      You CAN `cd` into that path and use `ls`, `cat`, `grep`,\n"
                    "      `find`, `rg` to read any legacy file directly. Cite\n"
                    "      paths relative to that root.\n"
                )
        except Exception:  # noqa: BLE001
            pass

        return (
            "KB-READINESS SNAPSHOT (iter-13.81.8 — read this before reaching "
            "for shell tools):\n"
            f"  • kb_files     : {n_files} file(s) ingested\n"
            f"  • kb_entities  : {sum(type_counts.values())} entity records "
            f"({entity_summary})\n"
            f"  • kb_toon      : {toon_chars:,} chars of compact serialisation\n"
            "  The KNOWLEDGE BASE / TOON / LEGACY WORKSPACE blocks below\n"
            "  carry the actual contents — there is nothing to discover on\n"
            "  the Droid filesystem.\n"
            + materialised_block
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workspace_kb_summary_for_prompt[%s] failed: %s", project_id, exc,
        )
        return ""

