import React from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useProjects } from "@/state/ProjectContext";

/**
 * StageProgress — iter-14.93: Sleek gradient slider with draggable-style thumb.
 * iter-14.94: Now project_type-aware. Renders 5 legacy stages for
 * legacy_migration projects and 3 tool stages (Input / Knowledge Base /
 * Report | Output) for gap_analysis / tech_transformer projects.
 * 
 * Full-width gradient track (red → amber → green) with stage tick marks
 * and a prominent circular thumb showing current position.
 */
const LEGACY_STAGES = [
  { key: "Discovery",    n: 1, label: "Discovery",    path: "/" },
  { key: "DataModel",    n: 2, label: "Data Model",   path: "/data-model" },
  { key: "Architecture", n: 3, label: "Architecture", path: "/architecture" },
  { key: "CodeGen",      n: 4, label: "Code Gen",     path: "/code-gen" },
  { key: "Living",       n: 5, label: "Living",       path: "/living" },
];

const GAP_ANALYSIS_STAGES = [
  { key: "Input",         n: 1, label: "Input",          path: "/gap-analyzer#input"  },
  { key: "KnowledgeBase", n: 2, label: "Knowledge Base", path: "/gap-analyzer#kb"     },
  { key: "Report",        n: 3, label: "Gap Report",     path: "/gap-analyzer#report" },
];

const TRANSFORMER_STAGES = [
  { key: "Input",         n: 1, label: "Input",          path: "/transformer#input"  },
  { key: "KnowledgeBase", n: 2, label: "Knowledge Base", path: "/transformer#kb"     },
  { key: "Output",        n: 3, label: "Transformed",    path: "/transformer#output" },
];

const STAGES_BY_TYPE = {
  legacy_migration:  LEGACY_STAGES,
  gap_analysis:      GAP_ANALYSIS_STAGES,
  tech_transformer:  TRANSFORMER_STAGES,
};

// iter-14.94 — for tool project types the "stage" for the current view is
// derived from window.location.hash rather than pathname.
const HASH_TO_STAGE = {
  input: "Input",
  kb: "KnowledgeBase",
  report: "Report",
  output: "Output",
};

export default function StageProgress() {
  const { active } = useProjects();
  const navigate = useNavigate();
  const location = useLocation();

  // iter-15.14 — Listen for Transformer phase broadcasts so the top status
  // strip highlights the actual phase (Input → KB → Transformed) rather
  // than merely the current tab hash. See Transformer.jsx::broadcastTransformerPhase.
  const [transformerPhase, setTransformerPhase] = React.useState(() => {
    try { return window.localStorage.getItem("lama:transformer:phase"); } catch (_) { return null; }
  });
  React.useEffect(() => {
    const onEvt = (e) => setTransformerPhase(e?.detail?.phase || null);
    const onStorage = (e) => { if (e.key === "lama:transformer:phase") setTransformerPhase(e.newValue); };
    window.addEventListener("lama:transformer:phase", onEvt);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("lama:transformer:phase", onEvt);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  if (!active) return null;

  const ptype = active.project_type || "legacy_migration";
  const STAGES = STAGES_BY_TYPE[ptype] || LEGACY_STAGES;

  // Route → stage mapping is different for tool projects: all stages share
  // a base path, so we resolve current stage from the URL hash.
  const isToolProject = ptype === "gap_analysis" || ptype === "tech_transformer";
  const isTransformer = ptype === "tech_transformer";
  let currentStage = null;
  if (isToolProject) {
    const hash = (location.hash || "").replace("#", "");
    currentStage = HASH_TO_STAGE[hash] || STAGES[0].key;
  } else {
    const routeMap = STAGES.reduce((m, s) => { m[s.path] = s.key; return m; }, {});
    currentStage = routeMap[location.pathname];
  }

  // iter-15.14 — Phase-driven active-stage override for tech_transformer.
  const PHASE_TO_IDX = {
    uploading: { activeIdx: 1, frozenBefore: 1, allDone: false },
    queued: { activeIdx: 1, frozenBefore: 1, allDone: false },
    building_kb: { activeIdx: 1, frozenBefore: 1, allDone: false },
    transforming: { activeIdx: 2, frozenBefore: 2, allDone: false },
    paused: { activeIdx: 2, frozenBefore: 2, allDone: false },
    completed: { activeIdx: 2, frozenBefore: 3, allDone: true },
    failed: { activeIdx: 2, frozenBefore: 2, allDone: false },
    stopped: { activeIdx: 2, frozenBefore: 2, allDone: false },
  };
  const transformerMap = (isTransformer && transformerPhase) ? PHASE_TO_IDX[transformerPhase] : null;
  const effectiveActiveIdx = transformerMap ? transformerMap.activeIdx : STAGES.findIndex(s => s.key === currentStage);

  const statusMap = active.stage_status || {};
  const currentIdx = effectiveActiveIdx;
  const thumbPosition = currentIdx >= 0 ? (currentIdx / Math.max(1, STAGES.length - 1)) * 100 : 0;
  if (transformerMap) currentStage = STAGES[transformerMap.activeIdx]?.key || currentStage;

  // For tool projects we override stage status from the URL: everything at
  // or before the current tab is "available" (or "frozen" if before), everything
  // after is "locked". This gives an accurate visual without a backend round-trip.
  const effectiveStatus = (s, i) => {
    if (transformerMap) {
      if (transformerMap.allDone) return "frozen";
      if (i < transformerMap.frozenBefore) return "frozen";
      if (i === transformerMap.activeIdx) return "active";
      return "locked";
    }
    if (isToolProject) {
      if (i < currentIdx) return "frozen";
      if (i === currentIdx) return "active";
      return "locked";
    }
    return statusMap[s.key] || (i === 0 ? "available" : "locked");
  };

  return (
    <nav
      aria-label="Migration pipeline progress"
      data-testid="stage-progress"
      className="bg-white px-6 py-2 shrink-0 border-b border-slate-100"
    >
      <div className="max-w-5xl mx-auto">
        {/* Slider track with gradient */}
        <div className="relative h-2 rounded-full overflow-visible"
          style={{
            background: "linear-gradient(to right, #ef4444, #f97316, #eab308, #84cc16, #22c55e)"
          }}
        >
          {/* Thumb indicator */}
          <div 
            className="absolute top-1/2 -translate-y-1/2 w-5 h-5 rounded-full bg-white border-[3px] border-[#2E2E38] shadow-lg z-20 transition-all duration-300"
            style={{ left: `calc(${thumbPosition}% - 10px)` }}
          />
          
          {/* Stage tick marks */}
          {STAGES.map((s, i) => {
            const status = effectiveStatus(s, i);
            const isFrozen = status === "frozen";
            const isLocked = status === "locked";
            const isCurrent = currentStage === s.key;
            const clickable = !isLocked;
            const position = (i / (STAGES.length - 1)) * 100;

            return (
              <button
                key={s.key}
                type="button"
                onClick={() => clickable && navigate(s.path)}
                disabled={!clickable}
                title={`${s.label}${isFrozen ? " ✓" : isLocked ? " 🔒" : ""}`}
                data-testid={`stage-progress-${s.key.toLowerCase()}`}
                className={`absolute top-1/2 -translate-y-1/2 z-10 transition-transform ${clickable ? "cursor-pointer hover:scale-125" : "cursor-not-allowed"}`}
                style={{ left: `calc(${position}% - 4px)` }}
              >
                {/* Tick mark */}
                <div className={`w-2 h-2 rounded-full ${
                  isCurrent ? "bg-transparent" : // hide tick where thumb is
                  isFrozen ? "bg-white ring-2 ring-white" : 
                  "bg-white/60"
                }`} />
              </button>
            );
          })}
        </div>

        {/* Stage labels below */}
        <div className="flex justify-between mt-1.5">
          {STAGES.map((s, i) => {
            const status = effectiveStatus(s, i);
            const isFrozen = status === "frozen";
            const isLocked = status === "locked";
            const isCurrent = currentStage === s.key;
            const clickable = !isLocked;

            return (
              <button
                key={s.key}
                type="button"
                onClick={() => clickable && navigate(s.path)}
                disabled={!clickable}
                className={`text-[9px] font-semibold uppercase tracking-wide transition-colors ${
                  clickable ? "cursor-pointer" : "cursor-not-allowed"
                } ${
                  isCurrent ? "text-amber-500 font-bold" : 
                  isFrozen ? "text-emerald-600" : 
                  isLocked ? "text-red-500" : "text-slate-500"
                }`}
                style={{ width: `${100 / STAGES.length}%`, textAlign: i === 0 ? "left" : i === STAGES.length - 1 ? "right" : "center" }}
              >
                {isFrozen && "✓ "}{s.label}
              </button>
            );
          })}
        </div>
      </div>
    </nav>
  );
}

