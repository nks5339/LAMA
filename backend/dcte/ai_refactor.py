"""DCTE AI Transformer — iter-18.5.

Real code-rewriting agent that sits *after* the deterministic
Helidon→Spring / Oracle→PG regex plugins. Its job is to take files the
regex pass emitted and finish the migration:

- Complete any Helidon→Spring rewrite the regex could not resolve
  (MP annotations, JAX-RS shapes, Config, DI, Security, Health, Metrics,
  Filters/Interceptors).
- Add / wire Swagger (springdoc-openapi) where missing.
- Ensure every file it returns compiles under Java 21 + Spring Boot 3.x.

Response contract (strict JSON — the model is told this explicitly):

    [
      {
        "file": "<relative or absolute path echoed back verbatim>",
        "action": "rewrite" | "leave",
        "content": "<full file source if action=='rewrite'>",
        "changes": ["short bullet", "short bullet"],
        "risk": "low" | "medium" | "high"
      }
    ]

The engine applies `content` back to disk with guardrails: skip if
content is empty, if the size delta vs original is > 300% or < 30%, or
if the file is not writable. Guardrail defence covers "do not conclude
your job with broken code".
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .prompt_builder import (
    build_transformer_brief,
    build_fixup_directive,
)
from .prompt_builder import stack_sections
from .prompt_store import compose, get_dcte_prompt
from .stacks import ai_sweep_suffixes, residue_markers

logger = logging.getLogger("lama.dcte.ai_refactor")


# ── The user-approved production prompt ─────────────────────────────
# Role, task, and strict rules mirror the exact spec that was proven to
# work in the reference "factory-ai" conversion. Do NOT weaken these
# without rev-bumping and re-verifying on the PMIS pilot.
# iter-21 — The stack-specific half of this prompt is now BUILT per job from
# the selected (source, target) pair; see dcte/prompt_builder.py. What stays
# here is the part that is true for every pair: the JSON the parser below
# has to read, and the speed clause.
#
# It used to be one hardcoded essay that opened "Convert the given project
# files from Helidon (MicroProfile / SE) to Spring Boot 3.x". Every job got
# it, so a JSP -> React job was instructed to migrate Helidon and to
# eradicate @ApplicationScoped. That is not a prompt with a bug in it; it is
# a prompt for a different job.
_RESPONSE_CONTRACT = """

RESPONSE FORMAT — STRICT JSON, no prose, no code fences around the object.
Emit exactly one JSON OBJECT with a single key "files" holding an array:
  {"files": [
    {"file": "<path exactly as given in the FILE header>",
     "action": "rewrite" | "leave",
     "content": "<full replacement file source, only when action==rewrite>",
     "changes": ["one short bullet per material change"],
     "risk": "low" | "medium" | "high"}
  ]}

WORKED EXAMPLE — the shape, not the content. Note that "content" is a single
JSON string: newlines are \\n and every embedded quote is escaped.
  {"files": [
    {"file": "/work/src/main/java/com/acme/OrderResource.java",
     "action": "rewrite",
     "content": "package com.acme;\\n\\nimport org.springframework...;\\n\\n@RestController\\npublic class OrderResource {\\n}\\n",
     "changes": ["JAX-RS @Path -> @RestController + @RequestMapping",
                 "field @Inject -> constructor injection"],
     "risk": "low"},
    {"file": "/work/src/main/java/com/acme/OrderDto.java",
     "action": "leave",
     "changes": ["already valid for the target stack"],
     "risk": "low"}
  ]}

SPEED (iter-18.13 — added at user request)
  Migrate FAST. Respond in a single turn. Do NOT explore the workspace,
  do NOT run shell commands, do NOT read other files, do NOT ask
  clarifying questions, and do NOT emit any preamble, chain-of-thought,
  planning notes, or "I will now…" narration. Treat each file in
  isolation: read its content from the FILE block, apply the conventions
  above, and emit the JSON array in one shot. Every extra round-trip /
  tool call costs the user real wall-clock time, so keep the response
  terse: only the JSON array, nothing else.
"""


def _system_prompt(source_stack: str, target_stack: str, template: str = "") -> str:
    """Migration brief for this pair + the JSON contract the parser needs.

    iter-22 — ``template`` is the seeded `dcte.transformer` row when one
    exists (DT-2). The pair-specific sections are spliced into it; the
    module-built brief below is the floor when no row is reachable.
    """
    fallback = build_transformer_brief(source_stack, target_stack) + _RESPONSE_CONTRACT
    return compose(template, stack_sections(source_stack, target_stack), fallback)


def _fixup_prompt(source_stack: str, target_stack: str, template: str = "") -> str:
    """Stricter variant for a second pass over a file that still has residue.

    The fix-up directive is appended AFTER the (possibly operator-edited)
    base prompt, so a weakened edit cannot remove the no-escape clause that
    exists because the base prompt already had its chance on this file.
    """
    return (_system_prompt(source_stack, target_stack, template)
            + build_fixup_directive(source_stack, target_stack))


# ── Guardrails against mangled rewrites ─────────────────────────────
_MIN_SIZE_RATIO = 0.55   # rewritten content must be ≥ 55% of original.
                         # Iter-18.8 tightened this from 0.30 — a 30% floor
                         # let truncated single-page rewrites of a 200-line
                         # file survive as apparently-successful, silently
                         # deleting business logic below the visible window.
_MAX_SIZE_RATIO = 3.00   # …and ≤ 300%.  Beyond that we assume the model
                         #  hallucinated or truncated and reject the patch.
_MAX_BATCH_FILES = 1     # iter-18.8 — one file per prompt. Batching three
                         # files caused the model to confuse identity and
                         # sometimes emit one file's content under another
                         # file's path; the strict-JSON contract survived
                         # but the output was garbage.
# Files larger than this are NOT sent to the LLM — they're marked for
# manual review instead of risking silent truncation.
_MAX_FILE_SIZE_FOR_LLM = 40000
# iter-22 — was 16000, i.e. BELOW the eligibility ceiling, so every file
# between 16 KB and 40 KB was sent truncated with nothing in the prompt
# saying so, and then size-checked against its full length. iter-18.8 had
# already raised this once (4000 → 16000) for exactly this reason and left
# the same bug one band higher. Matching the ceiling closes it: a file is
# either sent whole or not sent at all. Any residual truncation (a caller
# passing a smaller budget) is now STATED in the prompt and makes
# `_safe_apply` refuse the reply rather than write back a half file.
_MAX_CHARS_PER_FILE = _MAX_FILE_SIZE_FOR_LLM

# iter-18.15 — Max number of fix-up rounds after the primary sweep.
# Each round re-runs the LLM against every file that still carries
# legacy markers.  2 is enough in practice: the model corrects most
# residues on the first retry with the stricter prompt; a second pass
# catches anything the retry itself missed.  Overridable for tests
# and for aggressive customers via env-var.
_FIXUP_MAX_ROUNDS = int(os.environ.get("LAMA_DCTE_FIXUP_MAX_ROUNDS", "2") or "2")
# Half-migrated markers the rewrite MUST NOT reintroduce into a .java
# output.  If any of these is present we reject.
_JAVA_HALF_MIGRATED = (
    "jakarta.ws.rs.",
    "io.helidon.",
    "@ApplicationScoped",
    "@ConfigProperty",
    "org.eclipse.microprofile.",
)

# iter-18.15 — Extended Helidon/JAX-RS residue detectors used by the
# post-sweep "residual scan".  Broader than ``_JAVA_HALF_MIGRATED``
# because the guardrail is checking OUR output while these look at the
# whole file (including places the LLM might have missed).
_JAVA_RESIDUE_MARKERS: tuple[str, ...] = (
    "io.helidon.",
    "org.eclipse.microprofile.",
    "jakarta.ws.rs.",
    "javax.ws.rs.",
    "javax.enterprise.",
    "javax.inject.Inject",
    "jakarta.enterprise.",
    "@ApplicationScoped",
    "@RequestScoped",
    "@SessionScoped",
    "@ConfigProperty",
    "@Inject",
    "@Path(",
    "@ApplicationPath(",
    "@Produces(",
    "@Consumes(",
    "@PathParam(",
    "@QueryParam(",
    "@HeaderParam(",
    "@FormParam(",
    "@BeanParam(",
    "MicroProfile",
    "SeContainer",
    "WebTarget",
)

# iter-18.15 — Oracle → PostgreSQL residue detectors for .sql files.
# We use word-boundary substring checks (case-insensitive at scan time)
# so ``VARCHAR2`` still hits inside ``COLUMN v VARCHAR2(50)``.
_SQL_RESIDUE_MARKERS: tuple[str, ...] = (
    "VARCHAR2",
    "NVL(",
    "SYSDATE",
    "DECODE(",
    "FROM DUAL",
    "FROM dual",
    "SEQUENCE.NEXTVAL",
    ".NEXTVAL",
    "TO_DATE(",
    "TO_CHAR(",
    "MINUS ",  # Oracle set-op — PG uses EXCEPT
    "PRAGMA ",
    "CREATE OR REPLACE PACKAGE",
    "IS TABLE OF ",
)


# iter-20 — Blank out comments before the residue scan so it reads CODE,
# not prose.
#
# Without this, DCTE flagged its OWN output: bootstrap_writer emits an
# Application.java whose Javadoc explains what happened to the original
# entrypoint ("Original entrypoint (io.helidon.microprofile.server.Main)
# does not survive the migration"), and the plugin leaves `// TODO(dcte):`
# markers quoting the JAX-RS annotation they replaced. Both are correct,
# useful output; a raw substring scan called them residue. The cost was
# not cosmetic: every fix-up round re-sent those files to the model to
# "fix" a sentence, and the job reported needs_manual on a clean migration.
#
# Scoped to the two suffixes scan_residual actually walks. routes/tools.py
# has a general multi-language version (`_strip_comments_for_scan`) for the
# Transformer; it is deliberately NOT shared, because the suite that pins it
# reads tools.py's AST and requires those symbols to be defined there.
#
# Comments become spaces rather than vanishing, so any offset arithmetic
# downstream still lines up. Quote-aware, so a `"http://x"` literal or an
# SQL string containing `--` is not mistaken for a comment opener.
# iter-22 — line-comment opener per suffix. Was `"--" if .sql else "//"`,
# which is wrong for every language the catalogue added: `#` in Python/Ruby,
# `<%--` in JSP. A wrong opener does not just miss comments, it leaves prose
# in the scanned text and produces residue findings on a clean file.
_LINE_COMMENT: dict[str, str] = {
    ".sql": "--", ".pks": "--", ".pkb": "--", ".prc": "--", ".fnc": "--",
    ".py": "#", ".rb": "#",
}


def _blank_comments(body: str, suffix: str) -> str:
    line_marker = _LINE_COMMENT.get(suffix, "//")
    out: list[str] = []
    i, n = 0, len(body)
    quote = ""
    while i < n:
        ch = body[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:          # escaped char inside a literal
                out.append(body[i + 1]); i += 2; continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch; out.append(ch); i += 1; continue
        if body.startswith("/*", i):
            end = body.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join(" " if c != "\n" else "\n" for c in body[i:end]))
            i = end; continue
        if body.startswith(line_marker, i):
            end = body.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end; continue
        out.append(ch); i += 1
    return "".join(out)


def _scan_residue_in_file(
    path: Path,
    markers: "tuple[str, ...] | None" = None,
    suffixes: "frozenset[str] | None" = None,
) -> list[str]:
    """iter-18.15 — Return the list of legacy markers still present in
    ``path`` after the AI sweep.  Empty list = clean.  Silent on I/O
    error (returns empty).

    iter-20 — scans with comments blanked; see :func:`_blank_comments`.

    iter-22 — ``markers`` and ``suffixes`` now come from the selected stack
    pair (``stacks.residue_markers`` / ``stacks.ai_sweep_suffixes``). They
    used to be the module constants below, which are Helidon and Oracle
    only, so this gate — the thing that decides whether a file is reported
    as migrated or as needing manual work — was inert for every other pair
    the iter-21 catalogue offers. A `.jsx` file was not even opened.

    The constants remain as the fallback for a caller with no pair in hand.
    """
    try:
        body = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    suffixes = suffixes if suffixes is not None else frozenset({".java", ".sql"})
    if path.suffix not in suffixes:
        return []
    body = _blank_comments(body, path.suffix)
    if markers is None:
        markers = (_SQL_RESIDUE_MARKERS if path.suffix in _LINE_COMMENT
                   else _JAVA_RESIDUE_MARKERS)
    return [m for m in markers if m in body]


def scan_residual(
    root: Path,
    *,
    suffixes: "tuple[str, ...] | None" = None,
    source_stack: str = "",
    target_stack: str = "",
) -> list[dict[str, Any]]:
    """iter-18.15 — Public helper: walk ``root`` and return every file
    that still carries legacy markers.  Shape::

        [{"file": "<abs path>", "markers": ["io.helidon.", "@Inject", ...]}]

    iter-22 — pass ``source_stack``/``target_stack`` to scan the extensions
    and markers the selected pair actually implies. Omitting them keeps the
    historical Java+SQL behaviour so existing callers are unchanged.
    """
    out: list[dict[str, Any]] = []
    if not root or not root.exists():
        return out
    if source_stack or target_stack:
        sufs = ai_sweep_suffixes(source_stack, target_stack)
        marks: tuple[str, ...] | None = residue_markers(source_stack, target_stack) or None
    else:
        sufs = frozenset(suffixes or (".java", ".sql"))
        marks = None
    for suf in sorted(sufs):
        for p in root.rglob(f"*{suf}"):
            if not p.is_file():
                continue
            hits = _scan_residue_in_file(p, marks, sufs)
            if hits:
                out.append({"file": str(p), "markers": hits})
    return out




def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        if s.lstrip().lower().startswith("json"):
            s = s.split("\n", 1)[1] if "\n" in s else s
    return s.strip()


def _coerce_file_entries(payload: Any) -> "list | None":
    """Normalise a parsed reply into the list of per-file entries.

    iter-22 — the contract became `{"files":[...]}` so that it satisfies
    `llm.parses_as_json_object`, which is what gates the ONE bounded JSON
    repair re-ask inside `fabric_call`. DCTE was the only LLM subsystem in
    the app that never passed `response_format`, so a model that answered in
    prose simply lost the file; every other track got a free retry.

    A bare array is still accepted: older prompts, cached replies, and small
    local models that ignore the wrapper all produce one, and rejecting them
    would trade a working path for a stricter one.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("files", "results", "entries"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
        # A single un-wrapped entry, which small models emit when the batch
        # is one file — which it always is (`_MAX_BATCH_FILES == 1`).
        if "file" in payload and "action" in payload:
            return [payload]
    return None


def _extract_json_array(text: str) -> str | None:
    """iter-18.7 — Local Ollama models (7B) often reply with prose around
    the JSON array. Grab the first balanced `[...]` block so we still get
    something usable. Returns None if no balanced array is found.
    """
    if not text:
        return None
    s = _strip_json_fence(text)
    # Fast path: already a bare JSON array
    if s.startswith("[") and s.rstrip().endswith("]"):
        return s
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
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
            continue
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                return s[start:i + 1]
    return None


def _safe_apply(
    path: Path,
    original: str,
    new_content: str,
    markers: "tuple[str, ...] | None" = None,
    sent_len: int = 0,
) -> tuple[bool, str]:
    """Return (applied, reason).

    ``sent_len`` — how many characters of ``original`` the model was actually
    shown. iter-22: the size ratio used to be measured against the FULL
    original while the prompt had been silently truncated to 16 000 chars, so
    a faithful rewrite of a 30 KB file scored 0.55 and was either rejected as
    "too small" or — worse — accepted at just over the floor, writing back a
    file whose bottom half had been deleted. The ratio is now measured
    against what the model could see.

    ``markers`` — the residue reject list for the selected pair. Defaults to
    the Helidon constants for callers with no pair in hand.
    """
    if not new_content or not new_content.strip():
        return False, "empty content"
    # A truncated prompt cannot produce a complete file: writing the reply
    # back would delete everything past the cut. Checked BEFORE the size
    # bounds because it is categorical, not a size judgment — a short reply
    # to a truncated prompt would otherwise be reported as "too small",
    # which reads as a model failure when the cause was ours.
    was_truncated = 0 < sent_len < len(original)
    if was_truncated:
        return False, (
            f"rejected: only the first {sent_len} of {len(original)} chars were "
            "shown to the model, so its reply cannot be a complete file"
        )
    baseline = sent_len if sent_len > 0 else len(original)
    orig_len = max(1, baseline)
    new_len = len(new_content)
    ratio = new_len / orig_len
    if ratio < _MIN_SIZE_RATIO:
        return False, f"content too small ({ratio:.2f}× original)"
    if ratio > _MAX_SIZE_RATIO:
        return False, f"content too large ({ratio:.2f}× original)"
    # Blank comments before the marker check, for the reason iter-20
    # documented on the residue scan: `bootstrap_writer` and the plugins emit
    # correct code whose comments legitimately NAME the annotation they
    # replaced ("// TODO(dcte): was @Path(...)"). A raw substring check calls
    # that residue and sends a clean file round the fix-up loop to reword a
    # sentence.
    _scan_text = _blank_comments(new_content, path.suffix)
    for marker in (markers if markers is not None else _JAVA_HALF_MIGRATED):
        if marker in _scan_text:
            return False, f"rejected: still contains legacy marker '{marker}'"
    if path.suffix == ".java":
        # class name / file name sanity. Java only: it is the one language
        # here that *requires* the public type to match the file name.
        stem = path.stem
        if re.search(rf"\b(class|interface|enum|record)\s+{re.escape(stem)}\b", new_content) is None:
            return False, f"rejected: no top-level type named '{stem}'"
    try:
        path.write_text(new_content, encoding="utf-8")
    except Exception as e:  # pragma: no cover — defensive
        return False, f"write failed: {e}"
    return True, "applied"


async def transform_files(
    files: Iterable[Path],
    *,
    source_stack: str = "",
    target_stack: str = "",
    agent_key: str = "dcte.transformer",
    max_files_per_batch: int = _MAX_BATCH_FILES,
    max_chars_per_file: int = _MAX_CHARS_PER_FILE,
    progress_cb: "Callable[[int, int, list[Path], str], None] | None" = None,
    model: str | None = None,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Run the DCTE AI Transformer over ``files``.

    Batches are executed **concurrently** (iter-18.14).  Each batch is one
    ``fabric_call`` + local JSON parse + local ``_safe_apply`` disk write,
    all independent across batches, so we gate them with an
    ``asyncio.Semaphore`` and drive them with ``asyncio.gather``.  With
    173 files at ~20 s per batch that turns a ~1 h serial sweep into a
    ~20 min run at ``concurrency=3``.

    ``concurrency`` defaults to ``LAMA_DCTE_TRANSFORMER_CONCURRENCY`` env
    (fallback ``3``).  Set to ``1`` to restore the legacy serial mode if a
    provider rate-limits the parallel round-trips.

    Returns::

        {
          "rewritten": [{"file": str, "changes": [...], "risk": str}, ...],
          "skipped":   [{"file": str, "reason": str}, ...],
          "notes":     [str, ...],   # human-readable summary lines
          "suggestions": [ ... ],    # raw model output for the report
        }
    """
    try:
        from llm import fabric_call
    except Exception as e:
        logger.warning("fabric_call unavailable, skipping AI transform: %s", e)
        return {"rewritten": [], "skipped": [], "notes": [f"fabric unavailable: {e}"], "suggestions": []}

    import asyncio as _asyncio
    import os as _os
    if concurrency is None:
        try:
            concurrency = int(_os.environ.get("LAMA_DCTE_TRANSFORMER_CONCURRENCY", "3"))
        except ValueError:
            concurrency = 3
    concurrency = max(1, concurrency)

    real_files_raw = [Path(f) for f in files if f and Path(f).exists() and Path(f).is_file()]
    # iter-18.8 — split into "LLM-eligible" and "too-large-for-LLM". Silently
    # truncating a big file corrupts business logic, so we NEVER pass files
    # over _MAX_FILE_SIZE_FOR_LLM to the model. They surface as diagnostics.
    # iter-22 — resolved once for the whole sweep from the selected pair.
    # Everything downstream (the leave-gate, the guardrail reject list, the
    # post-sweep residue report) used module constants that only described
    # Helidon and Oracle, so those gates did nothing on any other pair.
    _pair_markers: tuple[str, ...] | None = (
        residue_markers(source_stack, target_stack) or None
    )
    _pair_suffixes: frozenset[str] = ai_sweep_suffixes(source_stack, target_stack)
    # iter-22 (DT-2) — the operator-editable half of the prompt, fetched
    # once per sweep. Empty on a fresh database or a Mongo blip, which
    # `_system_prompt` handles by falling back to the module brief.
    _template: str = await get_dcte_prompt("dcte.transformer")

    real_files: list[Path] = []
    oversized: list[Path] = []
    for f in real_files_raw:
        try:
            if f.stat().st_size > _MAX_FILE_SIZE_FOR_LLM:
                oversized.append(f)
            else:
                real_files.append(f)
        except Exception:
            real_files.append(f)
    rewritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    notes: list[str] = []
    suggestions: list[dict[str, Any]] = []
    for f in oversized:
        notes.append(f"skipped {f.name}: file > {_MAX_FILE_SIZE_FOR_LLM // 1000}KB — needs manual migration")

    # iter-18.13 — model forwarding.
    fc_model_kwargs: dict[str, Any] = {}
    if model and model.strip() and model.strip().lower() != "auto":
        fc_model_kwargs["model"] = model.strip()

    # Pre-compute batch list so batch_index is stable regardless of the
    # order in which coroutines complete.
    batches: list[list[Path]] = [
        real_files[i:i + max_files_per_batch]
        for i in range(0, len(real_files), max_files_per_batch)
    ]
    total_batches = max(1, len(batches))

    # Serialise progress_cb calls (the FE relies on monotonic-ish
    # progress).  The callback itself is fast; the lock just makes sure
    # two batches that finish at the same microsecond don't race their
    # event insertions.
    cb_lock = _asyncio.Lock()

    async def _emit(batch_i: int, batch: list[Path], phase: str) -> None:
        if not progress_cb:
            return
        async with cb_lock:
            try:
                progress_cb(batch_i, total_batches, batch, phase)
            except Exception:
                pass

    sem = _asyncio.Semaphore(concurrency)

    # iter-18.15 — Files that need a fix-up pass because either:
    #   • the model said ``action=leave`` on a file with legacy markers, or
    #   • the model's rewrite was rejected by _safe_apply, or
    #   • the model failed to parse / errored / returned empty.
    # Populated during the primary sweep; consumed by _run_fixup_round.
    needs_fixup: dict[str, list[str]] = {}

    async def _process_batch(
        batch_i: int,
        batch: list[Path],
        *,
        fixup_hints: dict[str, list[str]] | None = None,
    ) -> None:
        """Run one batch through the LLM.

        ``fixup_hints`` — when non-empty, activates fix-up mode: the
        stricter fix-up brief is used and each file's
        residual markers are enumerated in the user block so the model
        knows exactly what to eradicate.
        """
        is_fixup = bool(fixup_hints)
        system_prompt = (_fixup_prompt(source_stack, target_stack, _template)
                         if is_fixup
                         else _system_prompt(source_stack, target_stack, _template))
        async with sem:
            await _emit(batch_i, batch, "start" if not is_fixup else "fixup-start")
            originals: dict[str, tuple[Path, str, int]] = {}
            user_blocks: list[str] = []
            for f in batch:
                try:
                    body = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                trimmed = body[:max_chars_per_file]
                originals[str(f)] = (f, body, len(trimmed))
                header = f"---FILE: {f}---"
                if is_fixup and fixup_hints:
                    hits = fixup_hints.get(str(f)) or []
                    if hits:
                        header += (
                            "\n---LEGACY-MARKERS-STILL-PRESENT: "
                            + ", ".join(hits)
                            + " ---"
                        )
                # iter-22 — say it when we cut. `_MAX_CHARS_PER_FILE` now
                # matches the eligibility ceiling so this should never fire
                # in the normal path, but a caller can still pass a smaller
                # budget, and a model that is not told the file was cut will
                # confidently return a "complete" rewrite of the visible
                # half. routes/tools.py does the same for the Coder.
                if len(trimmed) < len(body):
                    header += (
                        f"\n---!!! TRUNCATED: you are seeing the first "
                        f"{len(trimmed)} of {len(body)} characters. This is "
                        "NOT the whole file. Return \"action\":\"leave\" — a "
                        "partial rewrite cannot be merged and will be "
                        "discarded. ---"
                    )
                user_blocks.append(f"{header}\n```\n{trimmed}\n```")
            if not user_blocks:
                await _emit(batch_i, batch, "skipped_read")
                return
            user = "\n\n".join(user_blocks)

            try:
                resp = await fabric_call(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user},
                    ],
                    agent_key=agent_key,
                    temperature=0.1,
                    max_tokens=8000,
                    # iter-22 — DCTE was the ONLY LLM subsystem in the app
                    # that never asked for JSON mode, despite having the
                    # strictest contract of any of them (whole file bodies
                    # embedded as JSON strings). Asking for it also arms the
                    # one bounded repair re-ask in `llm.fabric_call`, which
                    # fires when the reply does not parse as a JSON object —
                    # hence the `{"files": [...]}` wrapper on the contract.
                    response_format={"type": "json_object"},
                    **fc_model_kwargs,
                )
            except Exception as e:
                logger.warning("fabric_call failed for AI transform batch: %s", e)
                notes.append(f"batch skipped: {e}")
                # Queue for fix-up if this is the primary sweep.
                if not is_fixup:
                    for f in batch:
                        needs_fixup.setdefault(str(f), []).append(f"primary-error: {e}")
                await _emit(batch_i, batch, f"error: {e}")
                return

            text = (resp or {}).get("content") if isinstance(resp, dict) else str(resp or "")
            if not text:
                notes.append("batch skipped: empty model response")
                if not is_fixup:
                    for f in batch:
                        needs_fixup.setdefault(str(f), []).append("primary-empty-response")
                await _emit(batch_i, batch, "done rewrote=0 skipped=0")
                return

            parsed: Any = None
            # Try the object contract first, then the historical bare array.
            for candidate in (_strip_json_fence(text), _extract_json_array(text)):
                if not candidate:
                    continue
                try:
                    parsed = json.loads(candidate)
                    break
                except Exception:
                    continue
            data = _coerce_file_entries(parsed)
            if data is None:
                logger.debug("AI transform response not valid JSON, skipping batch")
                preview = (text or "")[:180].replace("\n", " ")
                notes.append(f"batch skipped: non-JSON model response ({preview!r})")
                if not is_fixup:
                    for f in batch:
                        needs_fixup.setdefault(str(f), []).append("primary-non-json")
                await _emit(batch_i, batch, "done rewrote=0 skipped=0")
                return

            suggestions.extend(data)
            batch_rewritten = 0
            batch_skipped = 0
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                fpath = str(entry.get("file") or "")
                action = str(entry.get("action") or "leave").lower()
                content = entry.get("content") or ""
                changes = entry.get("changes") or []
                risk = str(entry.get("risk") or "low").lower()
                match = originals.get(fpath)
                if not match:
                    for key, val in originals.items():
                        if Path(key).name == Path(fpath).name:
                            match = val
                            break
                if not match:
                    skipped.append({"file": fpath, "reason": "unknown file in response"})
                    batch_skipped += 1
                    continue
                target, original_body, sent_len = match
                if action != "rewrite":
                    # iter-18.15 — ``leave`` gate: if the model said the
                    # file is fine but it STILL carries legacy markers,
                    # queue it for a fix-up pass (we can't trust the
                    # verdict).
                    residue = _scan_residue_in_file(target, _pair_markers, _pair_suffixes)
                    if residue and not is_fixup:
                        needs_fixup[str(target)] = residue
                        notes.append(
                            f"queued {target.name} for fix-up: model said 'leave' but "
                            f"legacy markers remain ({', '.join(residue[:3])}"
                            f"{'…' if len(residue) > 3 else ''})"
                        )
                    continue
                # ``_safe_apply`` is a sync disk write. Push to a worker
                # thread so we don't block the loop while N-way parallel
                # batches are hammering it in tandem.
                ok, reason = await _asyncio.to_thread(
                    _safe_apply, target, original_body, content,
                    _pair_markers, sent_len,
                )
                if ok:
                    rewritten.append({"file": str(target), "changes": changes, "risk": risk})
                    notes.append(f"rewrote {target.name}: {', '.join(str(c) for c in changes[:3])}")
                    batch_rewritten += 1
                    # If a *fix-up* rewrite left residue, do NOT re-queue —
                    # we've now made our best attempt and downstream will
                    # flag the residue as needs_manual.
                else:
                    skipped.append({"file": str(target), "reason": reason})
                    notes.append(f"kept {target.name} unchanged ({reason})")
                    batch_skipped += 1
                    if not is_fixup:
                        # Queue the guardrail-rejected file for a fix-up
                        # attempt with the stricter prompt.  The reason
                        # doubles as the marker hint.
                        residue = _scan_residue_in_file(target, _pair_markers, _pair_suffixes) or [reason]
                        needs_fixup[str(target)] = residue
            phase_tag = "done" if not is_fixup else "fixup-done"
            await _emit(batch_i, batch, f"{phase_tag} rewrote={batch_rewritten} skipped={batch_skipped}")

    if batches:
        await _asyncio.gather(
            *(_process_batch(i + 1, b) for i, b in enumerate(batches)),
            return_exceptions=False,
        )

    # iter-18.15 — Post-sweep residue scan + up to _FIXUP_MAX_ROUNDS
    # fix-up rounds.  Any file that STILL has legacy markers after all
    # rounds is recorded as needs-manual in the return payload so the
    # engine can surface it in the migration report.
    async def _run_fixup_round(hints: dict[str, list[str]]) -> None:
        if not hints:
            return
        fix_paths = [Path(p) for p in hints.keys() if Path(p).exists()]
        # Batch one file per LLM call, same policy as the primary sweep.
        fixup_batches = [
            fix_paths[i:i + max_files_per_batch]
            for i in range(0, len(fix_paths), max_files_per_batch)
        ]
        notes.append(
            f"fix-up pass: {len(fix_paths)} file(s) still carry legacy markers — retrying with stricter prompt"
        )
        await _asyncio.gather(
            *(
                _process_batch(i + 1, b, fixup_hints=hints)
                for i, b in enumerate(fixup_batches)
            ),
            return_exceptions=False,
        )

    fixup_rounds = 0
    while fixup_rounds < _FIXUP_MAX_ROUNDS:
        # 1. Snapshot the queue built during the previous round (or the
        #    primary sweep for round 0).
        queue = dict(needs_fixup)
        # 2. Also add any file whose residue we can still detect on disk
        #    but which is NOT already in the queue (belt-and-braces —
        #    catches files that the primary sweep silently ignored).
        for f in real_files:
            if str(f) in queue:
                continue
            residue = _scan_residue_in_file(f, _pair_markers, _pair_suffixes)
            if residue:
                queue[str(f)] = residue
        if not queue:
            break
        # 3. Clear needs_fixup so the fix-up round can re-populate it
        #    with anything that STILL fails.
        needs_fixup.clear()
        await _run_fixup_round(queue)
        fixup_rounds += 1

    # Final residue report.
    residual: list[dict[str, Any]] = []
    for f in real_files:
        hits = _scan_residue_in_file(f, _pair_markers, _pair_suffixes)
        if hits:
            residual.append({"file": str(f), "markers": hits})
            # Also add to notes so the engine's ai_refactor summary
            # includes the marker names.
            notes.append(
                f"needs_manual {Path(f).name}: legacy markers remain "
                f"({', '.join(hits[:5])}{'…' if len(hits) > 5 else ''})"
            )

    return {
        "rewritten": rewritten,
        "skipped": skipped,
        "notes": notes,
        "suggestions": suggestions,
        "residual": residual,
        "fixup_rounds": fixup_rounds,
    }
