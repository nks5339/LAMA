/**
 * useAutoSaveTracker — extracted from SettingsMenu.jsx.
 *
 * The extraction was a bundle fix: the Sidebar needed this hook, importing
 * it dragged SettingsMenu's 1,378 lines and recharts onto the eager
 * critical path for a dialog that is closed almost all the time. These
 * tests pin the behaviour so the split stays safe.
 */
import { renderHook } from "@testing-library/react";
import {
  useAutoSaveTracker,
  resumePathFor,
  STAGE_PATHS,
  STAGE_ORDER,
  lsKey,
  AUTOSAVE_KEY,
} from "@/hooks/useAutoSaveTracker";

beforeEach(() => window.localStorage.clear());

describe("useAutoSaveTracker", () => {
  it("records the current stage path for the active project", () => {
    renderHook(() => useAutoSaveTracker("p1", "/architecture"));
    expect(window.localStorage.getItem(lsKey("p1"))).toBe("/architecture");
  });

  it("ignores a path that is not a pipeline stage", () => {
    renderHook(() => useAutoSaveTracker("p1", "/console"));
    expect(window.localStorage.getItem(lsKey("p1"))).toBeNull();
  });

  it("does nothing without an active project", () => {
    renderHook(() => useAutoSaveTracker(undefined, "/architecture"));
    expect(window.localStorage.length).toBe(0);
  });

  it("respects the auto-save off switch", () => {
    window.localStorage.setItem(AUTOSAVE_KEY, "off");
    renderHook(() => useAutoSaveTracker("p1", "/architecture"));
    expect(window.localStorage.getItem(lsKey("p1"))).toBeNull();
  });

  it("keeps per-project records separate", () => {
    renderHook(() => useAutoSaveTracker("p1", "/data-model"));
    renderHook(() => useAutoSaveTracker("p2", "/code-gen"));
    expect(window.localStorage.getItem(lsKey("p1"))).toBe("/data-model");
    expect(window.localStorage.getItem(lsKey("p2"))).toBe("/code-gen");
  });

  it("survives localStorage being unavailable", () => {
    const spy = jest
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("QuotaExceeded");
      });
    // Private mode must degrade the feature, not crash the shell.
    expect(() =>
      renderHook(() => useAutoSaveTracker("p1", "/architecture"))
    ).not.toThrow();
    spy.mockRestore();
  });
});

describe("resumePathFor", () => {
  it("returns the saved stage when one was recorded", () => {
    window.localStorage.setItem(lsKey("p1"), "/code-gen");
    expect(resumePathFor({ id: "p1" })).toBe("/code-gen");
  });

  it("ignores a saved value that is not a known stage path", () => {
    window.localStorage.setItem(lsKey("p1"), "/somewhere-else");
    expect(resumePathFor({ id: "p1" })).toBe("/");
  });

  it("falls back to the furthest reached stage", () => {
    expect(
      resumePathFor({
        id: "p2",
        stage_status: { Discovery: "frozen", DataModel: "frozen", Architecture: "available" },
      })
    ).toBe("/architecture");
  });

  it("starts at Discovery for a brand-new project", () => {
    expect(resumePathFor({ id: "p3", stage_status: {} })).toBe("/");
  });

  it("returns / for a missing project", () => {
    expect(resumePathFor(null)).toBe("/");
    expect(resumePathFor({})).toBe("/");
  });
});

describe("stage constants", () => {
  it("covers the five pipeline stages in order", () => {
    expect(STAGE_ORDER).toEqual([
      "Discovery", "DataModel", "Architecture", "CodeGen", "Living",
    ]);
  });

  it("maps every stage to a route", () => {
    for (const s of STAGE_ORDER) expect(STAGE_PATHS[s]).toBeTruthy();
  });
});
