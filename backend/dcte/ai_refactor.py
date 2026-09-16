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

logger = logging.getLogger("lama.dcte.ai_refactor")


# ── The user-approved production prompt ─────────────────────────────
# Role, task, and strict rules mirror the exact spec that was proven to
# work in the reference "factory-ai" conversion. Do NOT weaken these
# without rev-bumping and re-verifying on the PMIS pilot.
_SYSTEM = """You are a Senior Backend Developer and Legacy-to-Modernization expert.

TASK
  1. Convert the given project files from Helidon (MicroProfile / SE) to Spring Boot 3.x on Java 21, and any Oracle SQL/PLSQL to PostgreSQL.
  2. Implement Swagger (springdoc-openapi) — annotate REST endpoints and DTOs with OpenAPI annotations, add @Tag / @Operation where useful.
  3. Ensure the emitted code is build-error free. Every rewritten file MUST compile as-is under Spring Boot 3.3+ / Java 21 with the standard starters (spring-boot-starter-web, spring-boot-starter-data-jpa, spring-boot-starter-security, spring-boot-starter-actuator, springdoc-openapi-starter-webmvc-ui, postgresql, flyway-core, micrometer-core).

STRICT RULES
  1. Do NOT alter any business logic — variable names, branch semantics, DB column names, request/response shapes, HTTP verbs, and URL paths must be preserved unless the source annotation forces a change.
  2. Do NOT conclude with broken code. Every `content` you return MUST be self-consistent: imports match usages, class name matches file name, no half-migrated symbols like `@Inject`, `@ApplicationScoped`, `@ConfigProperty`, `jakarta.ws.rs.*` in a Spring file.
  3. Do NOT truncate. Emit the FULL rewritten file from `package` line through the final closing brace. Never end with `...` or "rest of file omitted" — the tool has no way to merge a partial rewrite and will discard truncated output.
  4. Token efficiency with 100% accuracy — if a file is already correct Spring Boot + Java 21 + swagger-ready, return `"action":"leave"` with an empty `content` and a short `changes` array. Do not re-emit unchanged files.

MIGRATION MAP (non-exhaustive; apply consistently)
  Helidon → Spring
    @Path,@ApplicationPath    → @RestController + @RequestMapping
    @GET/@POST/@PUT/@DELETE   → @GetMapping/@PostMapping/@PutMapping/@DeleteMapping
    @Produces/@Consumes       → produces=/consumes= on the mapping
    @PathParam/@QueryParam    → @PathVariable/@RequestParam
    @Inject                   → @Autowired (constructor injection preferred)
    @ApplicationScoped/@Singleton → @Service / @Component
    @ConfigProperty(name=X)   → @Value("${X}")
    Helidon Config            → @ConfigurationProperties or application.properties
    Helidon Security          → Spring Security config bean
    Helidon Health checks     → Spring Boot Actuator (/actuator/health) + custom HealthIndicator
    Helidon Metrics           → Micrometer (@Timed / MeterRegistry)
    Helidon Filters           → Spring HandlerInterceptor + WebMvcConfigurer
    Helidon OpenAPI           → springdoc-openapi + @Operation/@Tag

  Oracle → PostgreSQL (in .sql files)
    VARCHAR2                  → VARCHAR
    NUMBER, NUMBER(p,s)       → NUMERIC(p,s) or BIGINT / INT as appropriate
    DATE                      → TIMESTAMP
    SYSDATE                   → CURRENT_TIMESTAMP
    NVL(a,b)                  → COALESCE(a,b)
    DECODE(x,a,b,c,d,e)       → CASE WHEN x=a THEN b WHEN x=c THEN d ELSE e END
    DUAL                      → drop (Postgres does not need it)
    CREATE SEQUENCE ...       → keep, adjust syntax if needed
    triggers/packages         → convert to CREATE OR REPLACE FUNCTION + TRIGGER

RESPONSE FORMAT — STRICT JSON, no prose, no code fences around the outer array.
Emit exactly one JSON array. Each element:
  {"file": "<path exactly as given in the FILE header>",
   "action": "rewrite" | "leave",
   "content": "<full replacement file source, only when action==rewrite>",
   "changes": ["one short bullet per material change"],
   "risk": "low" | "medium" | "high"}

SPEED (iter-18.13 — added at user request)
  Migrate FAST. Respond in a single turn. Do NOT explore the workspace,
  do NOT run shell commands, do NOT read other files, do NOT ask
  clarifying questions, and do NOT emit any preamble, chain-of-thought,
  planning notes, or "I will now…" narration. Treat each file in
  isolation: read its content from the FILE block, apply the migration
  map above, and emit the JSON array in one shot. Every extra
  round-trip / tool call costs the user real wall-clock time, so keep
  the response terse: only the JSON array, nothing else.
"""


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
_MAX_CHARS_PER_FILE = 16000  # iter-18.8 — raised from 4000. Truncated
                         # inputs were the root cause of the user's
                         # "inaccuracy" complaint: a 8k-char Java file was
                         # rewritten from its first 4k only, losing the
                         # bottom half.
# Files larger than this are NOT sent to the LLM — they're marked for
# manual review instead of risking silent truncation.
_MAX_FILE_SIZE_FOR_LLM = 40000

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
def _blank_comments(body: str, suffix: str) -> str:
    line_marker = "--" if suffix == ".sql" else "//"
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


def _scan_residue_in_file(path: Path) -> list[str]:
    """iter-18.15 — Return the list of legacy markers still present in
    ``path`` after the AI sweep.  Empty list = clean.  Silent on I/O
    error (returns empty).

    iter-20 — scans with comments blanked; see :func:`_blank_comments`.
    """
    try:
        body = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if path.suffix not in (".java", ".sql"):
        return []
    body = _blank_comments(body, path.suffix)
    markers = _JAVA_RESIDUE_MARKERS if path.suffix == ".java" else _SQL_RESIDUE_MARKERS
    return [m for m in markers if m in body]


def scan_residual(root: Path, *, suffixes: tuple[str, ...] = (".java", ".sql")) -> list[dict[str, Any]]:
    """iter-18.15 — Public helper: walk ``root`` and return every file
    that still carries legacy markers.  Shape::

        [{"file": "<abs path>", "markers": ["io.helidon.", "@Inject", ...]}]
    """
    out: list[dict[str, Any]] = []
    if not root or not root.exists():
        return out
    for suf in suffixes:
        for p in root.rglob(f"*{suf}"):
            if not p.is_file():
                continue
            hits = _scan_residue_in_file(p)
            if hits:
                out.append({"file": str(p), "markers": hits})
    return out


# iter-18.15 — Stricter prompt for the fix-up pass.  The primary system
# prompt is intentionally general ("do the migration"); this one adds a
# no-escape clause because the primary already had a chance and either
# returned ``action=leave`` on a dirty file, produced non-JSON, or
# rewrote with residue still in it.
_FIXUP_SYSTEM = _SYSTEM + """

FIX-UP DIRECTIVE (iter-18.15 — RESIDUE REMEDIATION)
  You are being called for a SECOND time on this file because the first
  pass left legacy markers behind. The user block below lists the exact
  markers (e.g. ``io.helidon.``, ``@ApplicationScoped``, ``VARCHAR2``)
  that are still present.

  HARD RULES for this pass:
    1. ``action`` MUST be ``rewrite``.  ``leave`` is FORBIDDEN.
    2. Every listed marker MUST be gone from the returned ``content``.
    3. Do NOT invent or hallucinate business logic to justify the
       rewrite — only convert the legacy construct to its Spring Boot 3.x /
       PostgreSQL equivalent per the MIGRATION MAP above.
    4. If a marker legitimately cannot be replaced (e.g. it appears
       inside a comment or a string literal that names the legacy
       framework), delete the offending line or convert the comment to
       refer to the Spring equivalent.  A leftover mention of Helidon
       in a comment is still a bug.
"""


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        if s.lstrip().lower().startswith("json"):
            s = s.split("\n", 1)[1] if "\n" in s else s
    return s.strip()


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


def _safe_apply(path: Path, original: str, new_content: str) -> tuple[bool, str]:
    """Return (applied, reason)."""
    if not new_content or not new_content.strip():
        return False, "empty content"
    orig_len = max(1, len(original))
    new_len = len(new_content)
    ratio = new_len / orig_len
    if ratio < _MIN_SIZE_RATIO:
        return False, f"content too small ({ratio:.2f}× original)"
    if ratio > _MAX_SIZE_RATIO:
        return False, f"content too large ({ratio:.2f}× original)"
    if path.suffix == ".java":
        for marker in _JAVA_HALF_MIGRATED:
            if marker in new_content:
                return False, f"rejected: still contains legacy marker '{marker}'"
        # class name / file name sanity
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
        stricter :data:`_FIXUP_SYSTEM` prompt is used and each file's
        residual markers are enumerated in the user block so the model
        knows exactly what to eradicate.
        """
        is_fixup = bool(fixup_hints)
        system_prompt = _FIXUP_SYSTEM if is_fixup else _SYSTEM
        async with sem:
            await _emit(batch_i, batch, "start" if not is_fixup else "fixup-start")
            originals: dict[str, tuple[Path, str]] = {}
            user_blocks: list[str] = []
            for f in batch:
                try:
                    body = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                trimmed = body[:max_chars_per_file]
                originals[str(f)] = (f, body)
                header = f"---FILE: {f}---"
                if is_fixup and fixup_hints:
                    hits = fixup_hints.get(str(f)) or []
                    if hits:
                        header += (
                            "\n---LEGACY-MARKERS-STILL-PRESENT: "
                            + ", ".join(hits)
                            + " ---"
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

            try:
                payload = _extract_json_array(text) or _strip_json_fence(text)
                data = json.loads(payload)
            except Exception:
                logger.debug("AI transform response not valid JSON, skipping batch")
                preview = (text or "")[:180].replace("\n", " ")
                notes.append(f"batch skipped: non-JSON model response ({preview!r})")
                if not is_fixup:
                    for f in batch:
                        needs_fixup.setdefault(str(f), []).append("primary-non-json")
                await _emit(batch_i, batch, "done rewrote=0 skipped=0")
                return

            if not isinstance(data, list):
                notes.append("batch skipped: response was not a JSON array")
                if not is_fixup:
                    for f in batch:
                        needs_fixup.setdefault(str(f), []).append("primary-non-array")
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
                target, original_body = match
                if action != "rewrite":
                    # iter-18.15 — ``leave`` gate: if the model said the
                    # file is fine but it STILL carries legacy markers,
                    # queue it for a fix-up pass (we can't trust the
                    # verdict).
                    residue = _scan_residue_in_file(target)
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
                        residue = _scan_residue_in_file(target) or [reason]
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
            residue = _scan_residue_in_file(f)
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
        hits = _scan_residue_in_file(f)
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
