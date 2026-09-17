"""Prompt Library access for the DCTE agents — iter-22.

Resolves HUMAN_INTERVENTION DT-2. Every other LLM agent in LAMA reads its
system prompt from the `prompts` collection, seeded in `seed.py` with
`force_update: True` for rev-bumps, and is editable in Prompt Library. The
DCTE agents held theirs as module constants, so an operator who went looking
for them found four tools' prompts and not the fifth's.

The split is deliberate and is the whole point of the hybrid:

  * The **fixed** half — role, the three strict rules, the JSON contract the
    parser has to satisfy — lives in the seeded row and is editable.
  * The **pair-specific** half — target idioms, the no-residue clause, the
    API-docs clause — stays computed from `stacks.py`, because it is derived
    from the operator's own selection and cannot be written down ahead of
    time for 98 pairs.
  * The **guardrails** — size ratios, the residue reject list, the path
    confinement in the DevOps escalation — stay in code, where an edit
    cannot silently weaken them.

A seeded template marks where the computed half goes with the literal
placeholder ``{stack_sections}``. A row without it still works: the computed
sections are appended rather than dropped, because losing the target's
conventions is worse than an oddly-ordered prompt.

Every lookup degrades to the module constant. DCTE runs in a worker thread
off the main loop and its agents must never fail because Mongo blinked.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("lama.dcte.prompt_store")

STACK_SECTIONS_PLACEHOLDER = "{stack_sections}"

# Prompts change when an operator edits them, which is rare, and are read
# once per file in the AI sweep, which is not. A short TTL keeps an edit
# visible within a phase without a round-trip per file.
_TTL_SECONDS = 60.0
_CACHE: dict[str, tuple[float, str]] = {}


async def get_dcte_prompt(key: str) -> str:
    """Seeded template for a `dcte.*` agent, or "" when unavailable."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _TTL_SECONDS:
        return hit[1]
    template = ""
    try:
        from db import prompts
        row: dict[str, Any] | None = await prompts.find_one({"key": key}, {"_id": 0})
        template = ((row or {}).get("template") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("prompt row %s unavailable (%s); using the module default", key, exc)
        template = ""
    _CACHE[key] = (now, template)
    return template


def compose(template: str, stack_sections: str, fallback: str) -> str:
    """Splice the computed stack sections into a seeded template.

    ``fallback`` is the fully module-built brief, returned when there is no
    row. An empty ``template`` is the normal case on a fresh database and
    before `run_seed` has been reached, not an error.
    """
    tpl = (template or "").strip()
    if not tpl:
        return fallback
    if STACK_SECTIONS_PLACEHOLDER in tpl:
        return tpl.replace(STACK_SECTIONS_PLACEHOLDER, stack_sections)
    return f"{tpl}\n\n{stack_sections}"


def reset_cache() -> None:
    """Test helper — drop the TTL cache."""
    _CACHE.clear()
