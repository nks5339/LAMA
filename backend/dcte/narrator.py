"""DCTE live-progress narrator — iter-18.9.

Runs alongside a DCTE job while its status is IN_PROGRESS. Every N seconds
it pulls the last few unnarrated events, asks a small LLM to summarise
what's happening in ONE user-facing sentence, and emits a `phase="narration"`
event that the FE renders as a live "currently doing X…" banner.

Design:
    - agent_key `dcte.narrator` (tier=low) so it uses whichever small
      model the Console has routed for tier=low (Ollama qwen3:4b is fine).
    - non-blocking: any fabric error → skip that tick, never blocks the job.
    - deduped: won't emit a narration line if the summary matches the
      previous one (avoids "still transforming" spam).
    - loop cancels itself when job status becomes terminal (completed /
      failed / cancelled / rolled_back).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger("lama.dcte.narrator")

_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "rolled_back"}

_SYSTEM = (
    "You narrate a legacy-to-modern code migration in progress. Given the "
    "latest engine events (analyze / transform / ai_refactor / validate / "
    "report / cicd), reply with ONE short present-continuous sentence that "
    "tells the user what the tool is doing RIGHT NOW. "
    "Style: concise, active voice, no emojis, no markdown, max ~14 words. "
    "Examples: 'Rewriting 12 Helidon REST resources to Spring @RestController.' "
    "'Migrating pom.xml to Spring Boot 3.3 with Java 21 + Swagger.' "
    "'Running AI transformer on 87 Java files (batch 5 of 30).' "
    "'Generating GitHub Actions pipeline and traceability matrix.' "
    "Do NOT restate the whole event log — just describe the current phase."
)


async def _summarise(events: list[dict[str, Any]], progress_pct: int) -> str:
    """Return a one-sentence narration, or empty string on failure."""
    if not events:
        return ""
    try:
        from llm import fabric_call
    except Exception:
        return ""
    lines = []
    for e in events[-8:]:
        phase = e.get("phase") or "?"
        msg = (e.get("message") or "").strip().replace("\n", " ")
        lines.append(f"[{phase}] {msg}"[:200])
    user = (
        f"Progress {progress_pct}%.\n"
        f"Recent events:\n" + "\n".join(lines) +
        "\n\nWhat is the tool doing right now? One sentence."
    )
    # iter-22 (DT-2) — prefer the operator-editable `dcte.narrator` row.
    try:
        from .prompt_store import get_dcte_prompt
        system = await get_dcte_prompt("dcte.narrator") or _SYSTEM
    except Exception:  # noqa: BLE001 — narration must never block the job
        system = _SYSTEM
    try:
        resp = await fabric_call(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            agent_key="dcte.narrator",
            temperature=0.3,
            max_tokens=60,
        )
    except Exception as e:
        logger.debug("narrator fabric_call failed: %s", e)
        return ""
    text = (resp or {}).get("content") if isinstance(resp, dict) else str(resp or "")
    if not text:
        return ""
    # Take the first non-empty line, strip fences / quotes.
    for raw in text.splitlines():
        s = raw.strip().strip('`"\'')
        if s:
            # Cap at ~140 chars just in case the model ignored the length.
            return s[:140]
    return ""


async def narration_loop(
    job_id: str,
    get_job: Callable[[str], Any],
    get_recent_events: Callable[[str, int], Any],
    emit_narration: Callable[[str, str, int], None],
    interval_s: float = 6.0,
) -> None:
    """Loop until the job hits a terminal status.

    Parameters:
        get_job(job_id) -> awaitable returning the job dict (must have `status` + `progress`).
        get_recent_events(job_id, limit) -> awaitable returning list of event dicts.
        emit_narration(job_id, message, progress_pct) -> called synchronously
            when a fresh narration line is available.
    """
    last_line = ""
    consecutive_empty = 0
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                job = await get_job(job_id)
            except Exception:
                continue
            if not job:
                return
            status = (job.get("status") or "").lower()
            if status in _TERMINAL_STATUSES:
                return
            try:
                events = await get_recent_events(job_id, 20)
            except Exception:
                events = []
            progress_pct = int(round(float(job.get("progress") or 0.0) * 100))
            line = await _summarise(events, progress_pct)
            if not line:
                consecutive_empty += 1
                # after many empties (model unreachable / weak), stop trying —
                # let the plain % bar carry the UX.
                if consecutive_empty >= 5:
                    return
                continue
            consecutive_empty = 0
            if line == last_line:
                continue
            last_line = line
            try:
                emit_narration(job_id, line, progress_pct)
            except Exception as e:
                logger.debug("narrator emit failed: %s", e)
    except asyncio.CancelledError:
        return
