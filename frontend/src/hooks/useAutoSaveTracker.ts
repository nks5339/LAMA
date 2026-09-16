/**
 * Auto-save of the last-visited stage per project.
 *
 * Extracted from SettingsMenu.jsx so the Sidebar can use the hook without
 * statically importing that module — SettingsMenu pulls in recharts
 * (BarChart, PieChart) plus 1,378 lines for a dialog that is closed almost
 * all of the time, and the Sidebar is on the eager critical path, so the
 * static import was keeping recharts in main.js.
 *
 * Mounted once at the app shell. When auto-save is on, every navigation
 * persists { projectId, pathname } so re-opening a project from History
 * returns the user to the same stage.
 */
import { useEffect } from "react";

export const STAGE_PATHS: Record<string, string> = {
  Discovery: "/",
  DataModel: "/data-model",
  Architecture: "/architecture",
  CodeGen: "/code-gen",
  Living: "/living",
};

export const STAGE_ORDER = [
  "Discovery",
  "DataModel",
  "Architecture",
  "CodeGen",
  "Living",
];

export const lsKey = (pid: string): string => `lama:lastStage:${pid}`;
export const AUTOSAVE_KEY = "lama:autoSave";

export function resumePathFor(project: { id?: string; stage_status?: Record<string, string> } | null | undefined): string {
  if (!project?.id) return "/";
  try {
    const saved = localStorage.getItem(lsKey(project.id));
    if (saved && Object.values(STAGE_PATHS).includes(saved)) return saved;
  } catch {
    /* ignore */
  }
  const status = project.stage_status || {};
  let target = "Discovery";
  for (const k of STAGE_ORDER) {
    const s = status[k];
    if (s === "frozen" || s === "available" || s === "active") target = k;
  }
  return STAGE_PATHS[target] || "/";
}

export function useAutoSaveTracker(activeProjectId: string | undefined, pathname: string): void {
  useEffect(() => {
    if (!activeProjectId) return;
    try {
      if (localStorage.getItem(AUTOSAVE_KEY) === "off") return;
      if (Object.values(STAGE_PATHS).includes(pathname)) {
        localStorage.setItem(lsKey(activeProjectId), pathname);
      }
    } catch {
      /* ignore */
    }
  }, [activeProjectId, pathname]);
}

export default useAutoSaveTracker;
