"""Business-Rule reflection tracker (iter-13.30).

Thumb rule (per user directive): **100% of legacy business rules MUST be
reflected in SRS, Architecture/LLD, and CodeGen.**

How it works
============
- The deep legacy analyzer (`kb/legacy_analyzer.py`) emits a strict-JSON
  payload with a `business_rules[]` array of `{id, rule, scope, source}`
  items (e.g. `BR-01`, `BR-G-002`, `BR-M-014`). Each id is a stable
  identifier the LLM is instructed to cite verbatim wherever the rule
  surfaces.
- This module loads that list and computes how many of those ids appear
  as **literal tokens** inside the frozen text of a stage's artifacts:
    * SRS → concatenated frozen sections
    * Architecture → frozen LLD + HLD + service-map + API-contracts text
    * CodeGen → all generated source files concatenated

Public API
==========
- `load_business_rules(project_id)` → list of {id, rule, source, scope}
- `compute_coverage(rules, *texts)` → {total, cited, coverage_pct,
  missing_ids, missing_rules}
- `assert_coverage_or_warn(project_id, stage, texts)` →
    * Loads rules + computes coverage.
    * Always inserts an `audit_log` row with the coverage stats.
    * If env `LAMA_BR_ENFORCE in {"1","true","yes","on"}` AND coverage
      is below `LAMA_BR_MIN_COVERAGE` (default 100), raises
      `BRCoverageError` so the caller can return a 4xx (the freeze
      handlers convert this to HTTP 422).
    * Otherwise returns the coverage dict silently — operator sees it
      in the response payload and Audit Log.

Why not block by default?
=========================
The first run after this lands will almost certainly be < 100% on a
mature project. We surface the metric, log it, and let the operator opt
in to hard-enforce via `LAMA_BR_ENFORCE=1` once they've tuned the
generation prompts.
"""
from __future__ import annotations

import os
import re as _re
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger("lama.br_tracker")


# Match BR ids in three common shapes:
#   BR-01         (analyzer JSON shorthand)
#   BR-G-002      (global)
#   BR-AUTH-001   (module prefix, up to 16 chars)
#   BR-CLAIM-AMT-007 (multi-segment module)
# The module segment(s) may be alpha/numeric, separated by hyphens; the
# trailing numeric block is optional but typical.
_BR_ID_RE = _re.compile(
    r"\bBR-[A-Z0-9]+(?:-[A-Z0-9]+){0,3}\b",
    _re.IGNORECASE,
)


def _norm_id(s: str) -> str:
    return (s or "").strip().upper()


async def load_business_rules(project_id: str) -> list[dict]:
    """Return the business_rules list from the latest legacy_analysis doc.

    Returns ``[]`` if no analysis has been run yet so callers can degrade
    gracefully to "no rules tracked → 100% trivially satisfied".
    """
    try:
        from db import legacy_analysis
        doc = await legacy_analysis.find_one(
            {"project_id": project_id},
            {"_id": 0, "result": 1, "version": 1, "updated_at": 1},
            sort=[("version", -1)],
        )
    except Exception as e:
        logger.warning("br_tracker: failed to load legacy_analysis for %s: %s", project_id, e)
        return []
    if not doc:
        return []
    result = doc.get("result") or {}
    rules = result.get("business_rules") or []
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        rid = _norm_id(r.get("id") or "")
        if not rid:
            continue
        out.append({
            "id": rid,
            "rule": (r.get("rule") or "").strip(),
            "scope": (r.get("scope") or "").strip(),
            "source": (r.get("source") or "").strip(),
        })
    return out


def compute_coverage(rules: list[dict], *texts: str) -> dict[str, Any]:
    """Compute BR coverage % of `rules` cited in the concatenated `texts`.

    A rule counts as "cited" when its id (e.g. `BR-01`, `BR-G-007`)
    appears as a literal token in any of the supplied texts. This is a
    deliberately strict, deterministic check — the prompt seeds in
    `gov.business_rule_extraction` and `srs.spec.ieee29148` already
    instruct the model to cite ids verbatim.
    """
    total = len(rules)
    if total == 0:
        return {
            "total": 0, "cited": 0, "coverage_pct": 100.0,
            "missing_ids": [], "missing_rules": [],
        }
    blob = "\n".join(t or "" for t in texts)
    # Pull every BR-* token from the blob in one pass — cheaper than N
    # regex searches when there are 50+ rules.
    cited_in_text: set[str] = {_norm_id(m) for m in _BR_ID_RE.findall(blob)}
    cited: list[dict] = []
    missing: list[dict] = []
    for r in rules:
        if r["id"] in cited_in_text:
            cited.append(r)
        else:
            missing.append(r)
    pct = round(len(cited) * 100.0 / total, 2)
    return {
        "total": total,
        "cited": len(cited),
        "coverage_pct": pct,
        "missing_ids": [r["id"] for r in missing],
        "missing_rules": missing[:50],  # cap response size
    }


class BRCoverageError(Exception):
    """Raised when coverage is below the configured minimum and
    `LAMA_BR_ENFORCE` is set. Carries the coverage dict on `.coverage`."""

    def __init__(self, stage: str, coverage: dict):
        self.stage = stage
        self.coverage = coverage
        super().__init__(
            f"{stage} freeze blocked: business-rule coverage "
            f"{coverage['cited']}/{coverage['total']} "
            f"({coverage['coverage_pct']:.1f}%) is below the configured "
            f"minimum of {_min_coverage_pct():.0f}%. "
            f"Missing rule ids (first 20): {coverage['missing_ids'][:20]}"
        )


def _enforce_enabled() -> bool:
    return os.environ.get("LAMA_BR_ENFORCE", "").lower() in {"1", "true", "yes", "on"}


def _min_coverage_pct() -> float:
    raw = os.environ.get("LAMA_BR_MIN_COVERAGE", "100").strip()
    try:
        v = float(raw)
        return max(0.0, min(100.0, v))
    except ValueError:
        return 100.0


async def assert_coverage_or_warn(
    project_id: str,
    stage: str,
    texts: Iterable[str],
) -> dict:
    """Compute coverage, audit-log it, and raise if enforcement is on.

    Always returns the coverage dict so the caller can surface it in the
    freeze response payload.
    """
    rules = await load_business_rules(project_id)
    coverage = compute_coverage(rules, *list(texts))
    # Audit-log every freeze attempt, regardless of pass/fail.
    try:
        from db import audit_log
        await audit_log.insert_one({
            "action": f"br.coverage.{stage}",
            "project_id": project_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "details": {
                "total_rules": coverage["total"],
                "cited": coverage["cited"],
                "coverage_pct": coverage["coverage_pct"],
                "missing_ids_sample": coverage["missing_ids"][:20],
                "enforced": _enforce_enabled(),
                "min_required_pct": _min_coverage_pct(),
            },
        })
    except Exception:
        pass

    if not coverage["total"]:
        # No rules extracted yet (analyzer didn't run, or returned empty).
        # Treat as "trivially satisfied" — we can't gate on what we don't
        # know — but log a warning so operators see it.
        logger.warning(
            "br_tracker: no business_rules found in legacy_analysis for "
            "project=%s stage=%s — coverage gate skipped. Run Build KB to "
            "trigger the deep legacy analyzer.",
            project_id, stage,
        )
        return coverage

    if _enforce_enabled() and coverage["coverage_pct"] < _min_coverage_pct():
        raise BRCoverageError(stage, coverage)

    if coverage["coverage_pct"] < _min_coverage_pct():
        logger.warning(
            "br_tracker: %s freeze proceeding with BR coverage %.1f%% "
            "(< %.0f%% target). Set LAMA_BR_ENFORCE=1 to block. Missing: %s",
            stage, coverage["coverage_pct"], _min_coverage_pct(),
            coverage["missing_ids"][:10],
        )
    return coverage

