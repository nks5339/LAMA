"""iter-14.21 — LangGraph orchestration around the CodeGen confidence loop.

The heavy lifting (parity scoring, gap-recovery regeneration) still
lives in `backend/codegen/parity_loop.py` + `routes/codegen.py`. This
module exposes a **thin LangGraph StateGraph** that models the
iteration as a proper state machine:

    ┌───────────┐   overall<threshold   ┌──────────────┐
    │   score   │──────────────────────▶│ gap_analysis │
    │           │                       │              │
    │           │◀─────regenerate──────│  regenerate  │
    └─────┬─────┘                       └──────────────┘
          │ overall ≥ threshold OR iter ≥ max_iter
          ▼
        (END)

Why LangGraph and not a plain `for` loop:

  * Named state transitions land in the run log with `route_taken` +
    per-node telemetry, which is what the operator sees in MiniConsole
    ("Iteration 2/3 — confidence 87 → gap_analysis → regen").
  * Interruptions (pause / resume / stop) plug in cleanly at edge
    boundaries — the `should_continue` decider owns the whole
    should-stop policy in one place.
  * The graph is import-safe: if `langgraph` is missing OR fails to
    build, `run_confidence_loop_langgraph()` falls back to a plain
    async `for` loop with identical semantics.

The functions here are pure orchestration; they call caller-provided
callables (`score_fn`, `regen_fn`, `log_fn`, `stop_check`) so this
module has NO dependency on FastAPI, Mongo, or the job registry.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("lama.codegen.confidence_graph")


# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────

def default_node_timeout() -> float:
    """Hard per-node wall-clock cap (seconds). iter-14.23 — the confidence
    loop used to hang at "Loading generated files… 2%" when a score/regen
    node blocked on a cold HF encoder or an unresponsive LLM endpoint. Each
    node is now bounded so the loop always makes progress or terminates.
    Default 90s; override via LAMA_CODEGEN_NODE_TIMEOUT (clamp [15, 1800])."""
    try:
        raw = float(os.environ.get("LAMA_CODEGEN_NODE_TIMEOUT", "90") or 90.0)
    except ValueError:
        raw = 90.0
    return max(15.0, min(1800.0, raw))


async def _run_node(coro: Awaitable[Any], *, node: str, log_fn) -> Any:
    """Await a node coroutine under a hard timeout, propagating cancellation.
    Raises asyncio.TimeoutError (surfaced by callers as a terminal route)."""
    timeout = default_node_timeout()
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        try:
            log_fn(f"⏱ node '{node}' exceeded {timeout:.0f}s — aborting node")
        except Exception:  # noqa: BLE001
            pass
        raise

def default_max_iterations() -> int:
    """Read LAMA_CODEGEN_MAX_ITERATIONS (default 3, clamp [1,10])."""
    try:
        raw = int(os.environ.get("LAMA_CODEGEN_MAX_ITERATIONS", "3") or 3)
    except ValueError:
        raw = 3
    return max(1, min(10, raw))


def is_langgraph_available() -> bool:
    try:
        from langgraph.graph import StateGraph, END  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ──────────────────────────────────────────────────────────────────────
# State + result shapes
# ──────────────────────────────────────────────────────────────────────

@dataclass
class IterationRecord:
    iteration: int
    overall_score: float
    below_threshold: int
    route_taken: str          # "converged" | "regenerated" | "max_iter"
    regen_applied: int = 0
    regen_failed: int = 0


@dataclass
class LoopResult:
    iterations: List[IterationRecord] = field(default_factory=list)
    final_score: float = 0.0
    converged: bool = False
    engine: str = "loop"       # "langgraph" | "loop"
    stopped_by_user: bool = False
    error: str = ""


# ──────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────

async def run_confidence_loop(
    *,
    threshold: float,
    max_iterations: int,
    score_fn: Callable[[int], Awaitable[Dict[str, Any]]],
    regen_fn: Callable[[List[Dict[str, Any]]], Awaitable[Dict[str, int]]],
    log_fn: Optional[Callable[[str], None]] = None,
    stop_check: Optional[Callable[[], Awaitable[bool]]] = None,
) -> LoopResult:
    """Iterate score→decide→(regen→score)* until converged or capped.

    Parameters
    ----------
    threshold : float
        Score at which the loop converges (0-100). 95 per PRD.
    max_iterations : int
        Hard cap on iterations. Default 3 (see `default_max_iterations`).
    score_fn : async (iter_num) -> {overall_score, below_threshold: list, targets: list}
        Caller-provided scorer. `targets` is the list forwarded verbatim
        into `regen_fn` — usually `parity_loop.select_recovery_targets()`
        output.
    regen_fn : async (targets) -> {applied, failed}
        Caller-provided regenerator. Runs Gap Recovery on each target.
    log_fn : (str) -> None, optional
        Called for each state transition. Passed to the MiniConsole tail
        via the job log.
    stop_check : async () -> bool, optional
        Called between transitions. Truthy → early-exit with
        `stopped_by_user=True`.
    """
    log = log_fn or (lambda _msg: None)
    result = LoopResult(engine="langgraph" if is_langgraph_available() else "loop")

    if result.engine == "langgraph":
        try:
            return await _run_via_langgraph(
                threshold=threshold, max_iterations=max_iterations,
                score_fn=score_fn, regen_fn=regen_fn, log_fn=log,
                stop_check=stop_check,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("confidence_graph: LangGraph path failed (%s) — "
                           "falling back to plain loop", e)
            log(f"⚠ LangGraph unavailable ({e!r}) — running plain loop")
            result.engine = "loop"

    return await _run_plain_loop(
        threshold=threshold, max_iterations=max_iterations,
        score_fn=score_fn, regen_fn=regen_fn, log_fn=log,
        stop_check=stop_check,
    )


# ──────────────────────────────────────────────────────────────────────
# LangGraph implementation
# ──────────────────────────────────────────────────────────────────────

async def _run_via_langgraph(
    *, threshold, max_iterations, score_fn, regen_fn, log_fn, stop_check,
) -> LoopResult:
    from typing import TypedDict
    from langgraph.graph import StateGraph, END

    class LoopState(TypedDict, total=False):
        iter_num: int
        overall_score: float
        targets: list
        below_threshold: int
        history: list
        stopped_by_user: bool
        converged: bool

    async def _score_node(state: "LoopState") -> dict:
        it = int(state.get("iter_num", 0)) + 1
        log_fn(f"━━ Iter {it}/{max_iterations} — scoring")
        r = await _run_node(score_fn(it), node=f"score#{it}", log_fn=log_fn)
        overall = float(r.get("overall_score") or 0.0)
        targets = list(r.get("targets") or [])
        below = int(r.get("below_threshold") or len(targets))
        log_fn(f"iter={it} score={overall:.2f}% below={below}")
        return {
            "iter_num": it,
            "overall_score": overall,
            "targets": targets,
            "below_threshold": below,
        }

    def _decide_edge(state: "LoopState") -> str:
        if stop_check is None:
            stopped = False
        else:
            # LangGraph edges are sync; we cache the last stop_check result
            # from the score/regen nodes via `state["stopped_by_user"]`.
            stopped = bool(state.get("stopped_by_user", False))
        if stopped:
            return "done"
        if float(state.get("overall_score", 0.0)) >= threshold:
            return "done"
        if int(state.get("iter_num", 0)) >= max_iterations:
            return "done"
        if not state.get("targets"):
            return "done"
        return "regen"

    async def _regen_node(state: "LoopState") -> dict:
        if stop_check and await stop_check():
            log_fn("⏸ stop requested — exiting graph")
            return {"stopped_by_user": True}
        targets = state.get("targets") or []
        log_fn(f"iter={state.get('iter_num')} → gap_analysis → regen {len(targets)} target(s)")
        stats = await _run_node(regen_fn(targets),
                                node=f"regen#{state.get('iter_num')}", log_fn=log_fn)
        applied = int(stats.get("applied") or 0)
        failed = int(stats.get("failed") or 0)
        log_fn(f"iter={state.get('iter_num')} regen applied={applied} failed={failed}")
        hist = list(state.get("history") or [])
        hist.append({
            "iteration": state.get("iter_num"),
            "overall_score": state.get("overall_score"),
            "below_threshold": state.get("below_threshold"),
            "regen_applied": applied,
            "regen_failed": failed,
            "route_taken": "regenerated",
        })
        return {"history": hist}

    async def _done_node(state: "LoopState") -> dict:
        overall = float(state.get("overall_score", 0.0))
        it = int(state.get("iter_num", 0))
        converged = overall >= threshold
        route = "converged" if converged else (
            "stopped" if state.get("stopped_by_user") else "max_iter"
        )
        hist = list(state.get("history") or [])
        # If the last score wasn't already recorded (converged/max-iter
        # w/o a regen node in this iteration), append it now.
        if not hist or hist[-1].get("iteration") != it or hist[-1].get("route_taken") == "regenerated":
            hist.append({
                "iteration": it,
                "overall_score": overall,
                "below_threshold": state.get("below_threshold", 0),
                "regen_applied": 0,
                "regen_failed": 0,
                "route_taken": route,
            })
        return {"history": hist, "converged": converged}

    g = StateGraph(LoopState)
    g.add_node("score", _score_node)
    g.add_node("regen", _regen_node)
    g.add_node("done", _done_node)
    g.set_entry_point("score")
    g.add_conditional_edges("score", _decide_edge, {"regen": "regen", "done": "done"})
    g.add_edge("regen", "score")
    g.add_edge("done", END)
    compiled = g.compile()

    initial: LoopState = {
        "iter_num": 0, "overall_score": 0.0, "targets": [],
        "below_threshold": 0, "history": [], "stopped_by_user": False,
        "converged": False,
    }
    # LangGraph's default recursion limit is 25; each iteration = 3
    # nodes, so allow up to 3*max_iter+3 hops (small guardrail).
    final = await compiled.ainvoke(
        initial, config={"recursion_limit": max(30, 3 * max_iterations + 5)},
    )

    result = LoopResult(engine="langgraph")
    for row in (final.get("history") or []):
        result.iterations.append(IterationRecord(
            iteration=int(row.get("iteration") or 0),
            overall_score=float(row.get("overall_score") or 0.0),
            below_threshold=int(row.get("below_threshold") or 0),
            route_taken=str(row.get("route_taken") or ""),
            regen_applied=int(row.get("regen_applied") or 0),
            regen_failed=int(row.get("regen_failed") or 0),
        ))
    result.final_score = float(final.get("overall_score", 0.0))
    result.converged = bool(final.get("converged", False))
    result.stopped_by_user = bool(final.get("stopped_by_user", False))
    return result


# ──────────────────────────────────────────────────────────────────────
# Plain-loop fallback (semantics MUST match _run_via_langgraph)
# ──────────────────────────────────────────────────────────────────────

async def _run_plain_loop(
    *, threshold, max_iterations, score_fn, regen_fn, log_fn, stop_check,
) -> LoopResult:
    result = LoopResult(engine="loop")
    for it in range(1, max_iterations + 1):
        if stop_check and await stop_check():
            result.stopped_by_user = True
            log_fn("⏸ stop requested — exiting loop")
            break
        log_fn(f"━━ Iter {it}/{max_iterations} — scoring")
        try:
            r = await _run_node(score_fn(it), node=f"score#{it}", log_fn=log_fn)
        except asyncio.TimeoutError:
            result.error = f"score_fn timed out (>{default_node_timeout():.0f}s) at iter {it}"
            result.iterations.append(IterationRecord(
                iteration=it, overall_score=result.final_score,
                below_threshold=0, route_taken="score_timeout",
            ))
            break
        except Exception as e:  # noqa: BLE001
            result.error = f"score_fn failed: {e}"
            break
        overall = float(r.get("overall_score") or 0.0)
        targets = list(r.get("targets") or [])
        below = int(r.get("below_threshold") or len(targets))
        log_fn(f"iter={it} score={overall:.2f}% below={below}")
        result.final_score = overall
        if overall >= threshold or it == max_iterations or not targets:
            route = "converged" if overall >= threshold else "max_iter"
            result.iterations.append(IterationRecord(
                iteration=it, overall_score=overall,
                below_threshold=below, route_taken=route,
            ))
            result.converged = overall >= threshold
            break
        if stop_check and await stop_check():
            result.stopped_by_user = True
            log_fn("⏸ stop requested — exiting loop")
            result.iterations.append(IterationRecord(
                iteration=it, overall_score=overall,
                below_threshold=below, route_taken="stopped",
            ))
            break
        log_fn(f"iter={it} → gap_analysis → regen {len(targets)} target(s)")
        try:
            stats = await _run_node(regen_fn(targets), node=f"regen#{it}", log_fn=log_fn)
        except asyncio.TimeoutError:
            result.error = f"regen_fn timed out (>{default_node_timeout():.0f}s) at iter {it}"
            result.iterations.append(IterationRecord(
                iteration=it, overall_score=overall,
                below_threshold=below, route_taken="regen_timeout",
            ))
            break
        except Exception as e:  # noqa: BLE001
            result.error = f"regen_fn failed: {e}"
            result.iterations.append(IterationRecord(
                iteration=it, overall_score=overall,
                below_threshold=below, route_taken="regen_error",
            ))
            break
        applied = int(stats.get("applied") or 0)
        failed = int(stats.get("failed") or 0)
        log_fn(f"iter={it} regen applied={applied} failed={failed}")
        result.iterations.append(IterationRecord(
            iteration=it, overall_score=overall,
            below_threshold=below, route_taken="regenerated",
            regen_applied=applied, regen_failed=failed,
        ))
    return result
