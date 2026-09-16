/**
 * Visibility-aware polling.
 *
 * The audit found 22 `setInterval` polls across 12 components. They were
 * individually reasonable — most stop on a terminal status — but none of
 * them paused when the tab was hidden, so a backgrounded LAMA tab kept
 * hitting the API every 1–2 seconds indefinitely. With the multi-agent
 * panel's four concurrent timers plus MiniConsole's three plus the
 * Transformer's seven, an idle background tab generated several requests a
 * second forever.
 *
 * This hook:
 *   • pauses while `document.hidden`
 *   • fires once immediately on becoming visible again, so the user never
 *     looks at stale data while waiting for the next tick
 *   • keeps the callback in a ref, so a new closure each render does not
 *     restart the timer
 *   • accepts `delay = null` to stop entirely
 *
 * Usage:
 *   usePolling(fetchState, isTerminal ? null : 2000);
 */
import { useEffect, useRef } from "react";

export function usePolling(
  callback: () => void | Promise<void>,
  delay: number | null,
  { immediate = false }: { immediate?: boolean } = {},
): void {
  const saved = useRef(callback);

  useEffect(() => {
    saved.current = callback;
  }, [callback]);

  useEffect(() => {
    if (delay == null) return undefined;

    let id: ReturnType<typeof setInterval> | null = null;

    const tick = () => saved.current?.();

    const start = () => {
      if (id != null) return;
      id = setInterval(tick, delay);
    };

    const stop = () => {
      if (id == null) return;
      clearInterval(id);
      id = null;
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        // Catch up immediately rather than waiting a full interval.
        tick();
        start();
      }
    };

    if (immediate) tick();
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [delay, immediate]);
}

/**
 * Backoff helper for jobs that may sit in a non-terminal state for a long
 * time. Returns null once the status is terminal, so the caller can pass the
 * result straight to usePolling and stop polling a finished run.
 */
const TERMINAL = new Set<string>([
  "completed",
  "complete",
  "failed",
  "error",
  "stopped",
  "cancelled",
  "canceled",
  "done",
  "frozen",
]);

export function pollDelay(
  status: string | null | undefined,
  { active = 2000, idle = 10000 }: { active?: number; idle?: number } = {},
): number | null {
  if (!status) return idle;
  if (TERMINAL.has(String(status).toLowerCase())) return null;
  return active;
}

export default usePolling;
