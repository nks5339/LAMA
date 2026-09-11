"""iter-14.17 — In-memory log ring-buffer + tail API.

Purpose
-------
Give the operator a live view of what the backend is doing right now — LLM
routing decisions, provider errors, Factory CLI calls, HF model loading,
confidence-loop progress — from inside the `Discovery · Live Telemetry`
popup, without needing to `docker compose logs -f lama`.

Design
------
* A `RingBufferHandler` is attached to the ROOT logger at process start.
* Every `LogRecord` is captured into a bounded `deque(maxlen=CAP)` with a
  monotonically-increasing sequence number so the frontend can poll for
  "everything since seq=N".
* Payloads are pre-rendered (`Formatter.format`) so the API call is
  O(new_records) with no per-poll formatting cost.
* Level filter + substring search are applied at read time — the ring
  itself keeps everything so filters can be tightened without losing
  history.
* No SSE — plain GET polling. Keeps us clear of the K8s 60s ingress
  timeout that hurt the Architecture stage.

Contract preservation
---------------------
* The handler NEVER swallows records — it also lets the record propagate
  to whatever handlers uvicorn / supervisord already have wired up, so
  `docker compose logs` still works.
* No PII / secret redaction beyond what the calling code already emits.
  The ring stays in-process (no persistence), lives only as long as the
  container.
"""
from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional

# Cap the ring at ~1500 lines (~500 KB @ 300B/record). Enough for a
# ~15-minute Confidence run at the typical 2 records/second cadence
# with headroom, without pinning meaningful memory.
CAP = int(1500)

_LOCK = threading.Lock()
_SEQ = itertools.count(start=1)
_RING: "deque[Dict[str, Any]]" = deque(maxlen=CAP)


class RingBufferHandler(logging.Handler):
    """A `logging.Handler` that appends structured records into `_RING`.

    Errors raised inside `emit` are swallowed (via `handleError`) — a
    broken telemetry pipe MUST NOT crash the actual work.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        # Match the existing supervisord log format loosely — the
        # frontend renders `ts | level | name | msg` so keep those
        # fields intact.
        self._fmt = logging.Formatter("%(message)s")

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self._fmt.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        capped = msg[:2000]
        with _LOCK:
            # iter-14.17.1 — Collapse consecutive identical records into
            # a single "…  ×N" row so noisy log lines (e.g. the
            # LAMA_JWT_SECRET dev-fallback warning firing on every
            # authenticated request) don't drown out real signal.
            #
            # "Consecutive" means the immediately-previous ring entry
            # has the same (level, name, msg). We bump its `repeat`
            # counter and its `ts` (so age-sorting stays honest) rather
            # than pushing a new record and burning a seq.
            if _RING:
                prev = _RING[-1]
                if (prev["level"] == record.levelname
                        and prev["name"] == record.name
                        and prev["msg"] == capped):
                    prev["repeat"] = int(prev.get("repeat", 1)) + 1
                    prev["ts"] = record.created
                    prev["seq"] = next(_SEQ)  # so pollers see it as new
                    return
            _RING.append({
                "seq":    next(_SEQ),
                # `record.created` is float seconds since epoch.
                "ts":     record.created,
                "level":  record.levelname,
                "name":   record.name,
                "msg":    capped,
                "repeat": 1,
            })


_INSTALLED = False


def install_log_tail_handler(min_level: int = logging.INFO) -> None:
    """Idempotent — safe to call from `server.py` startup.

    Attaches the ring handler to the ROOT logger so every module
    (including third-party libraries like httpx, uvicorn, transformers,
    langgraph) funnels through it. Existing handlers are preserved.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    root = logging.getLogger()
    # If nothing set the root level yet (fresh process before uvicorn
    # config kicks in), give the ring something to work with.
    if root.level == logging.NOTSET or root.level > min_level:
        root.setLevel(min_level)
    handler = RingBufferHandler(level=min_level)
    root.addHandler(handler)
    _INSTALLED = True


def tail(
    since_seq: int = 0,
    limit: int = 300,
    min_level: Optional[str] = None,
    contains: Optional[str] = None,
) -> Dict[str, Any]:
    """Return every ring entry with `seq > since_seq`, newest last.

    * `min_level` — one of DEBUG / INFO / WARNING / ERROR / CRITICAL.
      Records below this level are filtered OUT (case-insensitive).
    * `contains` — case-insensitive substring filter against `msg`
      and `name`. Empty / None disables the filter.
    * `limit` — hard cap on returned entries; if the ring has more
      pending records the caller should re-poll with `next_seq`.

    The return shape:
      {
        "records": [{seq, ts, level, name, msg}, ...],
        "next_seq": int,     # highest seq the caller should send next
        "dropped":  int,     # how many records were evicted from the
                             # ring between `since_seq` and the oldest
                             # currently-held seq (i.e. the caller
                             # missed them; UI can warn "log gap")
        "capacity": CAP,
        "size":     len(_RING),
      }
    """
    lvl_num = _level_num(min_level) if min_level else logging.NOTSET
    substr = (contains or "").strip().lower() or None

    with _LOCK:
        snapshot = list(_RING)

    if not snapshot:
        return {"records": [], "next_seq": since_seq, "dropped": 0,
                "capacity": CAP, "size": 0}

    oldest_seq = snapshot[0]["seq"]
    dropped = 0
    if since_seq > 0 and since_seq + 1 < oldest_seq:
        # The caller asked for everything after seq N but the ring has
        # already evicted some entries between N+1 and oldest_seq.
        dropped = oldest_seq - (since_seq + 1)

    out: List[Dict[str, Any]] = []
    next_seq = since_seq
    for rec in snapshot:
        if rec["seq"] <= since_seq:
            continue
        if lvl_num and _level_num(rec["level"]) < lvl_num:
            next_seq = rec["seq"]  # advance so we don't re-scan
            continue
        if substr and substr not in rec["msg"].lower() and substr not in rec["name"].lower():
            next_seq = rec["seq"]
            continue
        out.append(rec)
        next_seq = rec["seq"]
        if len(out) >= max(1, limit):
            break

    return {
        "records":  out,
        "next_seq": next_seq,
        "dropped":  dropped,
        "capacity": CAP,
        "size":     len(snapshot),
    }


_LEVEL_MAP = {
    "DEBUG":    logging.DEBUG,
    "INFO":     logging.INFO,
    "WARNING":  logging.WARNING,
    "WARN":     logging.WARNING,
    "ERROR":    logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL":    logging.CRITICAL,
}


def _level_num(name: str) -> int:
    return _LEVEL_MAP.get((name or "").upper(), logging.NOTSET)
