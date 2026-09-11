"""Flag resolution for the hybrid DataModel pipeline (iter-13.44).

Precedence (highest → lowest):
    1. Per-request `payload["generation"]`     (e.g. POST body / job start)
    2. Per-project `project.settings.datamodel_generation`
    3. Env var (`LAMA_<KEY>_DETERMINISTIC` / `LAMA_<KEY>_POLISH`)
    4. Built-in defaults below

Keys returned by `resolve_modes()`:
    {
      "oltp":      "deterministic" | "llm",
      "olap":      "deterministic" | "llm",
      "migration": "deterministic" | "llm",
      "enum_polish":    bool,   # small LLM call after deterministic OLTP
      "comment_polish": bool,   # small LLM call after deterministic OLTP
    }
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


# Built-in defaults — deterministic ON for the three heavy artifacts,
# polish OFF (opt-in, costs tokens).
_DEFAULTS: Dict[str, Any] = {
    "oltp":           "deterministic",
    "olap":           "deterministic",
    "migration":      "deterministic",
    "enum_polish":    False,
    "comment_polish": False,
}

_VALID_MODES = {"deterministic", "llm"}


def _env_mode(key: str) -> Optional[str]:
    """`LAMA_OLTP_DETERMINISTIC=0` → "llm", `=1` → "deterministic"."""
    raw = (os.environ.get(f"LAMA_{key.upper()}_DETERMINISTIC", "") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return "deterministic"
    if raw in {"0", "false", "no", "off"}:
        return "llm"
    return None


def _env_polish(key: str) -> Optional[bool]:
    raw = (os.environ.get(f"LAMA_{key.upper()}_POLISH", "") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def resolve_modes(
    project: Optional[dict] = None,
    request_overrides: Optional[dict] = None,
) -> Dict[str, Any]:
    """Resolve effective generation modes for OLTP / OLAP / Migration / polish.

    `project`           — full Mongo doc; reads `settings.datamodel_generation`.
    `request_overrides` — usually `payload.get("generation")` from a job-start POST.
    """
    out = dict(_DEFAULTS)

    # Env layer
    for k in ("oltp", "olap", "migration"):
        m = _env_mode(k)
        if m:
            out[k] = m
    for k in ("enum", "comment"):
        b = _env_polish(k)
        if b is not None:
            out[f"{k}_polish"] = b

    # Per-project layer
    proj_cfg = ((project or {}).get("settings") or {}).get("datamodel_generation") or {}
    for k in ("oltp", "olap", "migration"):
        v = (proj_cfg.get(k) or "").strip().lower()
        if v in _VALID_MODES:
            out[k] = v
    for k in ("enum_polish", "comment_polish"):
        if k in proj_cfg:
            out[k] = bool(proj_cfg[k])

    # Per-request layer
    req = request_overrides or {}
    for k in ("oltp", "olap", "migration"):
        v = (req.get(k) or "").strip().lower() if isinstance(req.get(k), str) else None
        if v in _VALID_MODES:
            out[k] = v
    for k in ("enum_polish", "comment_polish"):
        if k in req:
            out[k] = bool(req[k])

    return out


def describe_mode(modes: Dict[str, Any]) -> str:
    """Compact human-readable summary, for audit logs / progress messages."""
    polish_bits = []
    if modes.get("enum_polish"):
        polish_bits.append("enum")
    if modes.get("comment_polish"):
        polish_bits.append("comment")
    polish = ("+polish[" + ",".join(polish_bits) + "]") if polish_bits else ""
    return (
        f"oltp={modes['oltp']} · olap={modes['olap']} · "
        f"migration={modes['migration']}{polish}"
    )

