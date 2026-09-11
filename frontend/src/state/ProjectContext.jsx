import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { useLocation } from "react-router-dom";
import { listProjects } from "@/lib/api";

const ProjectContext = createContext(null);

// iter-13.100 — Rolling-memory session IDs live in localStorage so they
// survive browser refresh and (critically) Droid 1 → Droid 2 handoff.
// Only the ID travels via this map; the heavy session payload (refs,
// summary, live turns) stays server-side under `agent_sessions`.
//
// Schema:  { [projectId]: { [`${stage}::${agentKey}`]: sessionId } }
const SESSION_MAP_KEY = "lama:agent_sessions:v1";

function loadSessionMap() {
  try {
    if (typeof window === "undefined") return {};
    const raw = window.localStorage.getItem(SESSION_MAP_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    return {};
  }
}

function persistSessionMap(map) {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(SESSION_MAP_KEY, JSON.stringify(map || {}));
  } catch (_) { /* localStorage may be blocked */ }
}

function sessionKey(stage, agentKey) {
  return `${(stage || "Discovery")}::${(agentKey || "default")}`;
}

export function ProjectProvider({ children }) {
  const [projects, setProjects] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [loading, setLoading] = useState(true);
  // iter-13.100 — Persisted (project → stage::agent → session_id) map.
  const [sessionMap, setSessionMap] = useState(loadSessionMap);
  const location = useLocation();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listProjects();
      // Defensive: backend should return List[Project], but guard against
      // error envelopes / unexpected shapes so the UI never white-screens.
      const list = Array.isArray(data)
        ? data
        : Array.isArray(data?.projects)
          ? data.projects
          : Array.isArray(data?.items)
            ? data.items
            : [];
      setProjects(list);
      if (!activeId && list.length > 0) {
        setActiveId(list[0].id);
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[ProjectContext] listProjects failed:", err);
      setProjects([]);
    } finally {
      setLoading(false);
    }
  }, [activeId]);

  // iter-13.21 — Refresh on every route change.
  // Without this, mutations made on Stage N (Freeze SRS, regenerate DDL,
  // unlock, …) that update `project.stage_status` on the backend were
  // never reflected in the in-memory project until the user did a hard
  // page reload. Symptom: Discovery freeze succeeds → sidebar shows
  // "Stage 2 READY" (computed from a different code path) → user opens
  // DataModel → page shows "Stage 2 is locked" banner forever.
  // useLocation fires on every navigation; one cheap GET /api/projects
  // per navigation keeps the state consistent end-to-end.
  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  const active = Array.isArray(projects)
    ? projects.find((p) => p.id === activeId) || null
    : null;

  // iter-13.100 — Session-ID accessors used by ChatPanel / future
  // factory orchestrator UI.
  const getSessionId = useCallback(
    (stage, agentKey) => {
      const pid = activeId || "";
      return ((sessionMap[pid] || {})[sessionKey(stage, agentKey)]) || "";
    },
    [activeId, sessionMap],
  );

  const setSessionId = useCallback(
    (stage, agentKey, sessionId) => {
      const pid = activeId || "";
      if (!pid) return;
      setSessionMap((prev) => {
        const next = { ...prev };
        const inner = { ...(next[pid] || {}) };
        if (sessionId) {
          inner[sessionKey(stage, agentKey)] = sessionId;
        } else {
          delete inner[sessionKey(stage, agentKey)];
        }
        next[pid] = inner;
        persistSessionMap(next);
        return next;
      });
    },
    [activeId],
  );

  const clearSession = useCallback(
    (stage, agentKey) => setSessionId(stage, agentKey, ""),
    [setSessionId],
  );

  return (
    <ProjectContext.Provider value={{
      projects, active, activeId, setActiveId, refresh, loading,
      // iter-13.100
      getSessionId, setSessionId, clearSession,
    }}>
      {children}
    </ProjectContext.Provider>
  );
}

export const useProjects = () => {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProjects must be inside ProjectProvider");
  return ctx;
};
