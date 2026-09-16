/**
 * Hooks — the perf and correctness fixes.
 *
 * useBreakpoint  was setState-per-resize-frame, re-rendering the whole tree
 *                during a window drag. Now matchMedia + useSyncExternalStore.
 * usePolling     replaces 22 setInterval sites that never paused on a hidden
 *                tab, so a backgrounded LAMA tab hit the API forever.
 * useJobProgress supplies the elapsed/phase numbers the "AI is working"
 *                surfaces render.
 */
import { renderHook, act, render } from "@testing-library/react";
import { useIsMobile, useMediaQuery, usePrefersReducedMotion } from "@/hooks/useBreakpoint";
import { usePolling, pollDelay } from "@/hooks/usePolling";
import { useElapsed, formatDuration, phaseLabel, PHASE_LABELS } from "@/hooks/useJobProgress";

// ── useBreakpoint ────────────────────────────────────────────────────

describe("useMediaQuery / useIsMobile", () => {
  it("reads the current match", () => {
    global.__setMatchMedia(true);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("re-renders when the query flips", () => {
    global.__setMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
    act(() => global.__setMatchMedia(true));
    expect(result.current).toBe(true);
  });

  it("does NOT re-render for a resize that stays inside a breakpoint", () => {
    // This is the whole point of the rewrite: the old hook stored
    // window.innerWidth, so every resize frame re-rendered the tree.
    let renders = 0;
    function Probe() {
      renders += 1;
      useIsMobile();
      return null;
    }
    global.__setMatchMedia(false);
    render(<Probe />);
    const before = renders;

    // Same match value, fired repeatedly — as a drag within one breakpoint
    // would do.
    act(() => {
      global.__setMatchMedia(false);
      global.__setMatchMedia(false);
      global.__setMatchMedia(false);
    });
    expect(renders).toBe(before);
  });

  it("shares one subscription across components watching the same query", () => {
    const { result: a } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    const { result: b } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    act(() => global.__setMatchMedia(true));
    expect(a.current).toBe(true);
    expect(b.current).toBe(true);
  });

  it("exposes the reduced-motion preference", () => {
    global.__setMatchMedia(true);
    const { result } = renderHook(() => usePrefersReducedMotion());
    expect(result.current).toBe(true);
  });
});

// ── usePolling ───────────────────────────────────────────────────────

describe("usePolling", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => false,
    });
  });
  afterEach(() => jest.useRealTimers());

  const setHidden = (v) =>
    Object.defineProperty(document, "hidden", { configurable: true, get: () => v });

  it("calls back on the interval", () => {
    const cb = jest.fn();
    renderHook(() => usePolling(cb, 1000));
    act(() => jest.advanceTimersByTime(3000));
    expect(cb).toHaveBeenCalledTimes(3);
  });

  it("does not poll when delay is null", () => {
    const cb = jest.fn();
    renderHook(() => usePolling(cb, null));
    act(() => jest.advanceTimersByTime(10000));
    expect(cb).not.toHaveBeenCalled();
  });

  it("stops on unmount", () => {
    const cb = jest.fn();
    const { unmount } = renderHook(() => usePolling(cb, 1000));
    act(() => jest.advanceTimersByTime(1000));
    unmount();
    act(() => jest.advanceTimersByTime(5000));
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it("pauses while the tab is hidden", () => {
    // The defect this replaces: 22 timers that kept firing in a
    // backgrounded tab, several per second between them.
    const cb = jest.fn();
    renderHook(() => usePolling(cb, 1000));
    act(() => jest.advanceTimersByTime(2000));
    expect(cb).toHaveBeenCalledTimes(2);

    act(() => {
      setHidden(true);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => jest.advanceTimersByTime(10000));
    expect(cb).toHaveBeenCalledTimes(2); // nothing while hidden
  });

  it("catches up immediately when the tab becomes visible again", () => {
    const cb = jest.fn();
    renderHook(() => usePolling(cb, 5000));
    act(() => {
      setHidden(true);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => jest.advanceTimersByTime(20000));
    const hiddenCalls = cb.mock.calls.length;

    act(() => {
      setHidden(false);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    // Fires at once rather than making the user wait a full interval
    // staring at stale data.
    expect(cb).toHaveBeenCalledTimes(hiddenCalls + 1);
  });

  it("does not restart the timer when the callback identity changes", () => {
    const cb = jest.fn();
    const { rerender } = renderHook(({ fn }) => usePolling(fn, 1000), {
      initialProps: { fn: cb },
    });
    act(() => jest.advanceTimersByTime(900));
    rerender({ fn: jest.fn() }); // new closure each render, as in real code
    act(() => jest.advanceTimersByTime(100));
    // The interval kept its phase instead of resetting to 0.
    expect(cb).toHaveBeenCalledTimes(0);
    act(() => jest.advanceTimersByTime(900));
  });
});

describe("pollDelay", () => {
  it.each(["completed", "failed", "stopped", "cancelled", "done", "frozen", "ERROR"])(
    "returns null for terminal status %s so a finished job stops polling",
    (s) => expect(pollDelay(s)).toBeNull()
  );

  it("polls fast while running", () => {
    expect(pollDelay("running", { active: 2000 })).toBe(2000);
  });

  it("backs off when the status is unknown", () => {
    expect(pollDelay(null, { idle: 9000 })).toBe(9000);
  });
});

// ── useJobProgress ───────────────────────────────────────────────────

describe("useElapsed", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("stays at zero while not running", () => {
    const { result } = renderHook(() => useElapsed(false));
    act(() => jest.advanceTimersByTime(5000));
    expect(result.current).toBe(0);
  });

  it("counts seconds while running", () => {
    const { result } = renderHook(() => useElapsed(true));
    act(() => jest.advanceTimersByTime(3000));
    expect(result.current).toBeGreaterThanOrEqual(2);
  });
});

describe("formatDuration", () => {
  it.each([
    [null, "—"],
    [0, "0s"],
    [45, "45s"],
    [60, "1m"],
    [95, "1m 35s"],
    [3600, "1h 0m"],
    [3725, "1h 2m"],
  ])("%s → %s", (input, expected) => {
    expect(formatDuration(input)).toBe(expected);
  });

  it("never renders a negative duration", () => {
    expect(formatDuration(-10)).toBe("0s");
  });
});

describe("phaseLabel", () => {
  it("maps the multi-agent CodeGen agent names to readable phases", () => {
    expect(phaseLabel("coder_be")).toBe("Writing backend code");
    expect(phaseLabel("traceability_gate")).toBe("Checking requirement traceability");
  });

  it("humanises an unknown phase rather than rendering blank", () => {
    // A new backend phase must never produce an empty status line.
    expect(phaseLabel("some_new_phase")).toBe("Some new phase");
  });

  it("returns null for no phase", () => {
    expect(phaseLabel(null)).toBeNull();
    expect(phaseLabel("")).toBeNull();
  });

  it("covers every agent in the documented pipeline", () => {
    for (const agent of [
      "context_manager", "planner", "coder_be", "coder_fe",
      "verifier", "reviewer", "tester", "traceability_gate", "finalizer",
    ]) {
      expect(PHASE_LABELS[agent]).toBeTruthy();
    }
  });
});
