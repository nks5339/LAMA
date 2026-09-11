"""Multi-model **Confidence Engine** (iter-13.70).

Asks 2-3 independent LLMs to score a generated artifact section-by-section
against the project's Knowledge Base (TOON + business rules + legacy
analysis + source inventory). Aggregates the votes into a confidence
envelope used by:

  • Stage freeze gates (Discovery / DataModel / Architecture / CodeGen)
    — surfaced on the UI as a per-stage confidence badge.
  • The new Living-stage **Accuracy Report** which compares the *latest*
    code/docs against the KB section-by-section.

Design goals
------------
1. **Drastically reduce human intervention.** When 2-3 strong models all
   converge on ≥95% the user can freeze without re-reading every line.
   When they don't, the report tells the user EXACTLY which section to
   regenerate (click-through deep-link).
2. **Model-agnostic.** Uses Console-configured providers via
   `llm.fabric_call`. Cross-tier on purpose (medium + high) so we don't
   double-count a single model's bias.
3. **Deterministic JSON contract.** Each evaluator response MUST match
   the schema below or it's discarded from the vote.

Public API
----------
    from confidence import score_artifact_multi_model

    report = await score_artifact_multi_model(
        project_id="p-1234",
        stage="SRS",
        artifact_text="<full markdown>",
        sections=[
            {"key": "workflow",        "label": "Workflow",         "what_to_check": "..."},
            {"key": "business_rules",  "label": "Business rules",   "what_to_check": "..."},
            ...
        ],
        kb_summary="<concise KB digest>",
        ground_truth="<authoritative excerpts: legacy analysis + module inv>",
    )

Returned shape
--------------
    {
      "stage": "SRS",
      "overall_score": 92.4,
      "overall_band": "good",            # excellent / good / moderate / poor
      "models_used": ["anthropic/...", "openai/..."],
      "sections": [
        { "key": "workflow", "label": "Workflow",
          "score": 96.0, "band": "excellent",
          "rationale": "All workflows in KB present; verified vs UC-04…",
          "gaps": ["UC-09 approval step missing"],
          "votes": [{"model":"a/...", "score":97}, {"model":"o/...","score":95}],
          "evidence": ["legacy_analysis::workflows[3]", ...]
        },
        ...
      ],
      "generated_at": "2026-06-14T..."
    }
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from llm import fabric_call

logger = logging.getLogger("lama.confidence")


# ----------------------------------------------------------------------------
# Bands
# ----------------------------------------------------------------------------
def band_of(score: float) -> str:
    if score >= 95:
        return "excellent"
    if score >= 85:
        return "good"
    if score >= 70:
        return "moderate"
    return "poor"


# ----------------------------------------------------------------------------
# Evaluator prompt — JSON-only output
# ----------------------------------------------------------------------------
_EVAL_SYSTEM = """ROLE: Senior QA Architect + Forensic Auditor.

You SCORE a generated artifact against an authoritative Knowledge Base for
a legacy-application modernization. You are NOT a writer here — you are a
JUDGE. Your job is to assign a confidence score (0-100) to every section
of the artifact based on how faithfully it reflects the KB GROUND TRUTH.

══════════════════════════════════════════════════════════════════════
SCORING RUBRIC (apply per section)
══════════════════════════════════════════════════════════════════════
100  — Every KB element relevant to this section is present, verifiable
       and correctly attributed. Zero deviations.
 97  — All material claims correct; only cosmetic / phrasing differences
       (word order, synonym choice, minor formatting).
 95  — All material claims correct; ≤2 minor omissions OR ≤2 minor
       phrasing gaps; NO contradictions with the KB. **This is the
       target for a well-generated artifact — do NOT reserve 95 for
       perfection; reserve it for "correct + a couple of small nits".**
 90  — All material claims correct; 3-5 minor omissions OR minor
       phrasing gaps; NO contradictions.
 85  — All major claims correct; 1 minor contradiction OR >5 minor
       omissions; the artifact is still USABLE without regeneration.
 70  — Mostly correct but ≥1 major omission OR ≥1 minor contradiction;
       the artifact needs one targeted regeneration.
 50  — Substantial omissions or 1 hard contradiction with the KB.
 25  — Largely fabricated / contradicts the KB.
  0  — Missing or unrelated content.

CALIBRATION HINTS (read before scoring):
• "Minor omission" = a single KB row (e.g. one BR-* rule, one UC-* step,
  one role) that could have been included but wasn't. It is NOT a
  reason to drop below 95 unless there are 3+ of them.
• "Cosmetic / phrasing difference" = the artifact says the same thing
  in different words. This never costs more than 3 points.
• When you're tempted to score 85 because "it's good but not perfect",
  check the rubric again — 85 is for artifacts with a CONTRADICTION
  or >5 omissions. If neither applies, you should be scoring 90-97.
• When you're tempted to score 80-88 out of "conservatism", stop and
  ask: is this artifact usable AS-IS without further regeneration?
  If yes → score ≥ 90. If it needs a targeted regenerate → 70-85.

══════════════════════════════════════════════════════════════════════
HARD RULES
══════════════════════════════════════════════════════════════════════
• Trust ONLY the GROUND TRUTH + KB SUMMARY blocks. The artifact under
  review is the THING BEING JUDGED — never use it as evidence of itself.
• When the KB has NO information about a section, score 100 by default
  (cannot deviate from a non-existent truth) and mark
  rationale="kb_silent_on_topic".
• **SEMANTIC MATCH beats label match.** Do NOT dock points because the
  artifact wrote a KB rule in narrative form ("The system SHALL validate
  DOB before dispatch") instead of the exact KB label ("BR-07: DOB
  validation"). If the RULE, ACTOR, TABLE COLUMN, or WORKFLOW STEP is
  captured with the same meaning as the KB row, that KB row is COVERED
  — regardless of whether the artifact uses BR-*, UC-*, R-*, T-* IDs
  or a narrative paragraph. Only dock points when the SEMANTIC content
  is missing / wrong / contradicted.
• **Absent structural labels is NOT a fabrication.** Missing BR-*,
  UC-*, WF-*, A-* ID tags is at most a MINOR omission (2-3 points),
  never a "largely fabricated" (25) or "unrelated" (0) verdict. Reserve
  scores below 50 for artifacts that OMIT the underlying substance or
  CONTRADICT the KB, not for artifacts that just chose different labels.
• **Presence bar for coverage credit**: an artifact "covers" a KB row
  when a reasonable reader would recognise the same rule / actor /
  table / step in the artifact prose. Direct quotation is NOT required.
• `gaps` = SHORT bullets ("UC-09 approval missing"). NEVER prose.
• `evidence` = comma-separated KB locator strings the artifact reflects
  ("legacy_analysis.workflows[3]", "TOON::TABLE.orders", etc.). MAX 5.
• Output STRICT JSON ONLY — no markdown fences, no prose, no preamble.

══════════════════════════════════════════════════════════════════════
OUTPUT SCHEMA (return EXACTLY this top-level object)
══════════════════════════════════════════════════════════════════════
{
  "sections": [
    {
      "key": "<one of the section keys you were asked to score>",
      "score": 0-100,
      "rationale": "<≤220 chars>",
      "gaps": ["<≤80 chars>", ...],
      "evidence": ["<KB locator>", ...]
    }
  ]
}

You MUST use the exact keys shown above: top-level `sections` (a LIST),
each row with `key`, `score`, `rationale`, `gaps`, `evidence`. Do NOT
substitute `verdict` / `summary` / `notes` / `assessment` — those keys
are IGNORED by the aggregator and your vote is DISCARDED (scored 0).
Do NOT nest the section under its own name (WRONG: `{"workflow":{...}}`).
Do NOT wrap in an outer envelope like `{"result":{...}}`.

WORKED EXAMPLE (verbatim shape — no extra keys, no missing keys):
{"sections":[{"key":"workflow","score":87,"rationale":"6 of 8 KB workflows covered; WF-05, WF-07 missing.","gaps":["WF-05 approval branch missing","WF-07 escalation absent"],"evidence":["legacy_analysis.workflows[0..5]","TOON::ROUTES[/orders/*]"]}]}
"""


def _build_eval_user(
    stage: str,
    sections: list[dict],
    artifact_text: str,
    kb_summary: str,
    ground_truth: str,
    max_artifact_chars: int = 32000,
    max_kb_chars: int = 6000,
    max_truth_chars: int = 8000,
) -> str:
    sec_block = "\n".join(
        f"  • `{s['key']}` — {s.get('label', s['key'])}: {s.get('what_to_check', '').strip()}"
        for s in sections
    )
    artifact_clip = (artifact_text or "")[:max_artifact_chars]
    if len(artifact_text or "") > max_artifact_chars:
        artifact_clip += "\n… (truncated)"
    kb_clip = (kb_summary or "")[:max_kb_chars]
    truth_clip = (ground_truth or "")[:max_truth_chars]
    return f"""STAGE UNDER REVIEW: {stage}

SECTIONS TO SCORE (return ONE entry per key, in this order):
{sec_block}

══════════════════════════════════════════════════════════════════════
GROUND TRUTH (authoritative — score against this)
══════════════════════════════════════════════════════════════════════
{truth_clip or '(no ground-truth digest available — score on KB SUMMARY only)'}

══════════════════════════════════════════════════════════════════════
KB SUMMARY (compact stats + entity index)
══════════════════════════════════════════════════════════════════════
{kb_clip or '(no KB summary)'}

══════════════════════════════════════════════════════════════════════
ARTIFACT UNDER REVIEW (the THING you are scoring)
══════════════════════════════════════════════════════════════════════
{artifact_clip or '(empty artifact — every section score 0)'}

Respond with the JSON schema described in the system prompt. NO prose."""


# ----------------------------------------------------------------------------
# JSON extraction — tolerates models that wrap the JSON in ``` fences.
# ----------------------------------------------------------------------------
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```", re.IGNORECASE)
# iter — Robust fence-open detector for evaluators that emit ```json\n{...
# but forget (or run out of tokens before) the closing ``` fence.
_JSON_FENCE_OPEN_RE = re.compile(r"```(?:json|JSON)?\s*\n?", re.IGNORECASE)


def _try_repair_truncated_json(s: str) -> Optional[dict]:
    """Best-effort recovery for JSON payloads the LLM truncated mid-object
    (usually because `max_tokens` clipped the tail). Closes dangling arrays
    / objects / strings and re-parses. Returns None when even a repaired
    parse fails so the caller can fall back cleanly."""
    if not s:
        return None
    src = s.rstrip().rstrip(",")
    # Track brace/bracket depth while respecting strings & escapes.
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in src:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]":
                if stack and stack[-1] == ch:
                    stack.pop()
    # Close any still-open string first, then unwind the container stack.
    tail = ""
    if in_str:
        tail += '"'
    while stack:
        tail += stack.pop()
    try:
        return json.loads(src + tail)
    except Exception:  # noqa: BLE001
        return None


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    candidate = raw.strip()
    # Prefer a properly closed ```json … ``` block if present.
    m = _JSON_FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    else:
        # iter — Tolerate an *unclosed* opening fence (`\`\`\`json\n…`).
        # Evaluators frequently open the fence and then either hit
        # `max_tokens` mid-JSON or just forget the closer. The old code
        # left `candidate` as the entire raw response, which then made
        # the balanced-brace pass parse the wrong prefix or fail outright,
        # and the numeric-recovery fallback grabbed the first stray digit
        # (this is where the bogus 2% / 4% scores come from).
        open_m = _JSON_FENCE_OPEN_RE.match(candidate)
        if open_m:
            candidate = candidate[open_m.end():]
        # Also handle `\`\`\`json` appearing after a leading rationale
        # line ("Here's my scoring: \`\`\`json\n…").
        else:
            inline = _JSON_FENCE_OPEN_RE.search(candidate)
            if inline and "{" in candidate[inline.end():]:
                candidate = candidate[inline.end():]
    # Find the first balanced object.
    start = candidate.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i, ch in enumerate(candidate[start:], start):
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
                end = i + 1
                break
    if end < 0:
        # iter — Truncated JSON: no matching closing brace within the
        # visible tokens. Try to auto-close the dangling structure so a
        # partial verdict is still usable.
        return _try_repair_truncated_json(candidate[start:])
    try:
        return json.loads(candidate[start:end])
    except Exception:  # noqa: BLE001
        # Last chance — parser choked on a trailing comma or unterminated
        # string. Feed it through the auto-repair too.
        return _try_repair_truncated_json(candidate[start:end])


# iter-14.7 — Schema-tolerant normalizer.
# Some evaluators (notably Factory.ai droid → claude-opus on `auto` mode)
# consistently ignore the prompt's `{"sections":[{"key":...}]}` schema and
# instead return one of these variants:
#   • `{"workflow": {"score": 43, "verdict": "...", "summary": "...", ...}}`
#     (section name as top-level key, with `verdict`/`summary`/`notes`
#     instead of `rationale`).
#   • `{"result": {"sections":[...]}}` (extra envelope).
#   • `{"sections":{"workflow":{...}}}` (object keyed by section, not list).
# Before this fix the aggregator's `isinstance(parsed.get("sections"), list)`
# guard rejected all of these and the section score fell back to 0 → the
# infamous "confidence 0%" that appeared even when the underlying score
# would have been in the 40-95 band. Normalize into the canonical shape.
_RATIONALE_ALIASES = ("rationale", "verdict", "summary", "notes",
                      "assessment", "explanation", "reason", "comment")
_GAPS_ALIASES = ("gaps", "missing", "issues", "omissions", "problems")
_EVIDENCE_ALIASES = ("evidence", "citations", "references", "sources")


def _coerce_score(row: dict) -> Optional[float]:
    """Pull a numeric 0-100 score out of common alias fields. Returns None
    when the row contains no interpretable score at all."""
    for k in ("score", "confidence", "confidence_score", "value",
              "percentage", "pct", "rating"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().rstrip("%").strip()
            try:
                return float(s)
            except ValueError:
                continue
    return None


def _coerce_rationale(row: dict) -> str:
    parts: list[str] = []
    for k in _RATIONALE_ALIASES:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " · ".join(parts)[:240]


def _coerce_list(row: dict, aliases: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for k in aliases:
        v = row.get(k)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, str) and it.strip():
                    out.append(it.strip())
                elif isinstance(it, dict):
                    # e.g. `{"gap":"...","severity":"..."}` — flatten
                    txt = it.get("gap") or it.get("text") or it.get("description") or ""
                    if isinstance(txt, str) and txt.strip():
                        out.append(txt.strip())
        elif isinstance(v, str) and v.strip():
            out.append(v.strip())
    # dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for it in out:
        if it not in seen:
            seen.add(it)
            uniq.append(it)
    return uniq


def _normalize_verdict(parsed: dict, expected_keys: list[str]) -> Optional[list[dict]]:
    """Best-effort normalization of an evaluator's JSON into the canonical
    shape `[{"key":..., "score":..., "rationale":..., "gaps":[], "evidence":[]}, ...]`.
    Accepts:
      1. Canonical    — `{"sections":[{"key":...}]}`
      2. Envelope     — `{"result":{"sections":[...]}}`, `{"data":{"sections":[...]}}`
      3. Sections-obj — `{"sections":{"<key>":{...}}}`
      4. Flat-by-key  — `{"<key>":{"score":...,"verdict":"...","gaps":[]}}`
      5. Flat-list    — `[{"key":...}]` (rare, but seen)
    Rows whose `key` is not in `expected_keys` are dropped.
    Returns `None` when NO recognisable rows could be salvaged.
    """
    if parsed is None:
        return None
    exp_set = {str(k) for k in expected_keys}

    # 2. Unwrap common envelopes.
    if isinstance(parsed, dict):
        for env in ("result", "data", "response", "output", "verdict"):
            inner = parsed.get(env)
            if isinstance(inner, dict) and (
                "sections" in inner or any(k in inner for k in exp_set)
            ):
                parsed = inner
                break

    rows_raw: list[dict] = []
    if isinstance(parsed, list):
        # 5. Flat list.
        for it in parsed:
            if isinstance(it, dict):
                rows_raw.append(it)
    elif isinstance(parsed, dict):
        # 6. `{"scores":[...]}`, `{"results":[...]}`, `{"evaluations":[...]}`
        # — sections-list under an alias.  Also try `items`/`rows`.
        list_alias = None
        for alias in ("sections", "scores", "results", "evaluations",
                      "items", "rows", "reviews", "assessments"):
            v = parsed.get(alias)
            if isinstance(v, list) and v:
                list_alias = alias
                break
        if list_alias:
            for it in parsed[list_alias]:
                if isinstance(it, dict):
                    rows_raw.append(it)
        elif isinstance(parsed.get("sections"), dict):
            # 3. Sections-obj: `{"sections":{"workflow":{...}}}`
            for k, v in parsed["sections"].items():
                if isinstance(v, dict):
                    v2 = dict(v)
                    v2.setdefault("key", k)
                    rows_raw.append(v2)
        else:
            # 4. Flat-by-key (or mixed).
            # Only lift entries whose value is a dict AND either the key
            # matches an expected section OR the dict looks like a score
            # row (has a score-ish field).
            for k, v in parsed.items():
                if not isinstance(v, dict):
                    continue
                if k in exp_set or _coerce_score(v) is not None:
                    v2 = dict(v)
                    v2.setdefault("key", k)
                    rows_raw.append(v2)

    if not rows_raw:
        return None

    out: list[dict] = []
    for row in rows_raw:
        key = str(row.get("key") or row.get("section") or row.get("name") or "").strip()
        if not key:
            continue
        # Some models emit "Workflow" for key "workflow". Case-normalize match.
        if key not in exp_set:
            match = next((ek for ek in exp_set if ek.lower() == key.lower()), None)
            if match:
                key = match
            else:
                # Drop rows for keys we didn't ask about.
                continue
        score = _coerce_score(row)
        if score is None:
            continue
        out.append({
            "key": key,
            "score": max(0.0, min(100.0, float(score))),
            "rationale": _coerce_rationale(row),
            "gaps": _coerce_list(row, _GAPS_ALIASES)[:8],
            "evidence": _coerce_list(row, _EVIDENCE_ALIASES)[:8],
        })
    return out or None


# iter-14.9 — Numeric-recovery fallback for evaluators that ignore the
# entire JSON schema. Scans the raw response for the FIRST 0-100 number
# associated with a "score" / "confidence" / "rating" / "%" cue and
# attributes it to the (single) requested section key. Only kicks in
# when `_normalize_verdict` has already given up.
_NUMERIC_HINT_RE = re.compile(
    r"(?ix)"
    r"(?:score|confidence|rating|coverage|match|fidelity|accuracy)"
    r"[\"'\s:=\-]*"
    r"(\d{1,3}(?:\.\d+)?)"
    r"(?:\s*(?:/\s*100|%|\s*out\s*of\s*100))?"
)
_BARE_PCT_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%")


def _numeric_recovery(raw: str, expected_keys: list[str]) -> Optional[list[dict]]:
    if not raw or not expected_keys:
        return None
    candidates: list[float] = []
    for m in _NUMERIC_HINT_RE.finditer(raw):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if 0.0 <= v <= 100.0:
            candidates.append(v)
    if not candidates:
        for m in _BARE_PCT_RE.finditer(raw):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if 0.0 <= v <= 100.0:
                candidates.append(v)
    if not candidates:
        return None
    # Use the FIRST hit — evaluators usually put the headline score up
    # top ("Score: 87. Rationale: …"). Attribute to every requested key
    # (recovery is best-effort — this is what would have been dropped).
    score = max(0.0, min(100.0, candidates[0]))
    rationale = raw.strip().splitlines()[0][:220] if raw.strip() else "schema-drift recovered"
    return [
        {"key": k, "score": score,
         "rationale": f"[schema-drift recovery] {rationale}",
         "gaps": [], "evidence": []}
        for k in expected_keys
    ]


# ----------------------------------------------------------------------------
# Single-model evaluation
# ----------------------------------------------------------------------------
async def _score_with_model(
    *,
    project_id: str,
    stage: str,
    sections: list[dict],
    artifact_text: str,
    kb_summary: str,
    ground_truth: str,
    model_override: str = "",
    agent_key: str = "confidence.evaluator",
) -> Optional[dict]:
    """Call ONE model and return its parsed verdict (or None on failure)."""
    user_msg = _build_eval_user(stage, sections, artifact_text, kb_summary, ground_truth)
    try:
        r = await fabric_call(
            messages=[
                {"role": "system", "content": _EVAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            agent_key=agent_key,
            project_id=project_id,
            model_override=model_override,
            max_tokens=3000,
            temperature=0.0,   # deterministic scoring
            timeout=120.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("confidence eval failed for model=%r stage=%s: %s",
                       model_override or "(default)", stage, exc)
        return None
    raw = (r.get("content") or "").strip()
    parsed = _extract_json(raw)
    if parsed is None:
        logger.warning("confidence eval returned non-JSON "
                       "(model=%r stage=%s, raw_head=%r)",
                       r.get("model"), stage, raw[:200])
        return None
    # iter-14.7 — schema-tolerant normalization. Accepts the canonical
    # `{"sections":[...]}` shape AND the four drift variants observed in
    # production (envelope-wrapped, sections-as-object, flat-by-key, top-
    # level list). See `_normalize_verdict` for the full list.
    expected_keys = [str(s.get("key")) for s in sections if s.get("key")]
    norm = _normalize_verdict(parsed, expected_keys)
    if not norm:
        # iter-14.9 — Last-ditch numeric recovery.
        # Some evaluators (Anthropic Opus on `auto` mode via factory)
        # occasionally return a well-formed rationale in prose with a
        # number embedded ("Overall I'd score this at 87/100 because…").
        # `_normalize_verdict` correctly rejects that shape, but the
        # aggregator then treats the entire section as unscored → 0.0,
        # which tanked the pill in the screenshot the user reported.
        # Try to salvage a single numeric score per expected key.
        recovered = _numeric_recovery(raw, expected_keys)
        if recovered:
            logger.info("confidence eval schema-drift recovered numerically "
                        "(model=%r stage=%s, keys_recovered=%d)",
                        r.get("model"), stage, len(recovered))
            return {"model": r.get("model") or model_override or "default",
                    "sections": recovered}
        logger.warning("confidence eval returned unrecognisable schema "
                       "(model=%r stage=%s, keys=%r, raw_head=%r)",
                       r.get("model"), stage, list(parsed.keys()) if isinstance(parsed, dict) else "list",
                       raw[:200])
        return None
    return {"model": r.get("model") or model_override or "default",
            "sections": norm}


# ----------------------------------------------------------------------------
# Public — multi-model aggregator
# ----------------------------------------------------------------------------
async def score_artifact_multi_model(
    *,
    project_id: str,
    stage: str,
    artifact_text: str,
    sections: list[dict],
    kb_summary: str = "",
    ground_truth: str = "",
    models: Optional[list[str]] = None,
    min_models: int = 1,
) -> dict:
    """Score `artifact_text` against `sections` using multiple models.

    Aggregates by simple mean across models that produced parseable JSON.
    When `models` is None / empty the function makes ONE call with the
    Console-routed default model for the agent_key.
    """
    if not sections:
        return {"stage": stage, "overall_score": 0.0, "overall_band": "poor",
                "models_used": [], "sections": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error": "no_sections_defined"}

    mlist = [m for m in (models or [""]) if m is not None]
    if not mlist:
        mlist = [""]

    verdicts: list[dict] = []
    for m in mlist:
        v = await _score_with_model(
            project_id=project_id,
            stage=stage,
            sections=sections,
            artifact_text=artifact_text,
            kb_summary=kb_summary,
            ground_truth=ground_truth,
            model_override=m,
        )
        if v:
            verdicts.append(v)

    if len(verdicts) < max(1, min_models):
        return {
            "stage": stage, "overall_score": 0.0, "overall_band": "poor",
            "models_used": [v["model"] for v in verdicts],
            "sections": [
                {"key": s["key"], "label": s.get("label", s["key"]),
                 "score": 0.0, "band": "poor",
                 "rationale": ("Evaluator returned an unrecognisable JSON "
                               "schema — see server logs for the raw head."),
                 "gaps": [], "evidence": [], "votes": []}
                for s in sections
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "evaluator_returned_no_parseable_json",
        }

    # Aggregate per section
    agg_sections: list[dict] = []
    overall_total = 0.0
    overall_n = 0
    for s in sections:
        key = s["key"]
        votes = []
        rationales: list[str] = []
        gaps: set[str] = set()
        evidence: set[str] = set()
        for v in verdicts:
            for row in v["sections"]:
                if row.get("key") == key and isinstance(row.get("score"), (int, float)):
                    score = max(0.0, min(100.0, float(row["score"])))
                    votes.append({"model": v["model"], "score": round(score, 2)})
                    if row.get("rationale"):
                        rationales.append(str(row["rationale"])[:240])
                    for g in (row.get("gaps") or []):
                        if isinstance(g, str) and g.strip():
                            gaps.add(g.strip()[:120])
                    for e in (row.get("evidence") or []):
                        if isinstance(e, str) and e.strip():
                            evidence.add(e.strip()[:120])
        if not votes:
            agg = {
                "key": key, "label": s.get("label", key),
                "score": 0.0, "band": "poor",
                "rationale": "No evaluator returned a score for this section",
                "gaps": [], "evidence": [], "votes": [],
            }
        else:
            mean = sum(v["score"] for v in votes) / len(votes)
            spread = max(v["score"] for v in votes) - min(v["score"] for v in votes)
            agg = {
                "key": key, "label": s.get("label", key),
                "score": round(mean, 2),
                "band": band_of(mean),
                "rationale": rationales[0] if rationales else "",
                "model_agreement_spread": round(spread, 2),
                "gaps": sorted(gaps)[:8],
                "evidence": sorted(evidence)[:8],
                "votes": votes,
            }
            overall_total += mean
            overall_n += 1
        agg_sections.append(agg)

    overall_score = round(overall_total / overall_n, 2) if overall_n else 0.0
    return {
        "stage": stage,
        "overall_score": overall_score,
        "overall_band": band_of(overall_score),
        "models_used": [v["model"] for v in verdicts],
        "sections": agg_sections,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------------------
# Helper — pick cross-tier model list from Console routing
# ----------------------------------------------------------------------------
async def pick_evaluator_models() -> list[str]:
    """Return up to 3 distinct models from Console's routing tiers so the
    multi-model vote is genuinely cross-vendor (or cross-tier when only
    one vendor is configured).

    iter-14.9 — Robustness upgrades after the screenshot showed only one
    `(default)` model was ever picked:
      • Also inspect `default_model` and `models` alias fields on the
        provider doc — the factory/auto preset stores its choices there
        instead of in `routing`.
      • Fall back to the fabric preset `default_models` when routing
        tiers are empty or absent.
      • Deduplicate case-insensitively so `deepseek-chat` and
        `DeepSeek-Chat` don't count twice.
    """
    seen: list[str] = []
    seen_lc: set[str] = set()

    def _push(m: Optional[str]):
        if not m:
            return
        s = str(m).strip()
        if not s:
            return
        if s.lower() in seen_lc:
            return
        seen.append(s)
        seen_lc.add(s.lower())

    try:
        from db import model_providers
    except Exception:  # noqa: BLE001
        model_providers = None  # type: ignore
    try:
        if model_providers is not None:
            cur = model_providers.find(
                {"is_active": True},
                {"_id": 0, "routing": 1, "preset_id": 1,
                 "default_model": 1, "models": 1},
            )
            async for prov in cur:
                routing = prov.get("routing") or {}
                for tier in ("high", "medium", "low"):
                    _push((routing.get(tier) or "").strip() if isinstance(routing, dict) else "")
                    if len(seen) >= 3:
                        break
                if len(seen) >= 3:
                    break
                _push(prov.get("default_model"))
                _models = prov.get("models")
                if isinstance(_models, list):
                    for m in _models[:3]:
                        if isinstance(m, str):
                            _push(m)
                        elif isinstance(m, dict):
                            _push(m.get("id") or m.get("name"))
                        if len(seen) >= 3:
                            break
                if len(seen) >= 3:
                    break
    except Exception:  # noqa: BLE001
        pass

    # Fallback to fabric preset defaults so we always have SOMETHING to
    # cross-check against — otherwise every eval is a single-model call
    # and the "spread" metric is meaningless.
    if len(seen) < 2:
        try:
            from fabric.model_fabric import PROVIDER_PRESETS  # type: ignore
            for preset in PROVIDER_PRESETS.values():
                defaults = (preset or {}).get("default_models") or {}
                for tier in ("high", "medium", "low"):
                    _push(defaults.get(tier))
                    if len(seen) >= 3:
                        break
                if len(seen) >= 3:
                    break
        except Exception:  # noqa: BLE001
            pass

    return seen or [""]

