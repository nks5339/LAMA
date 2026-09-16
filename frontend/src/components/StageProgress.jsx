import React from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useProjects } from "@/state/ProjectContext";
import { STAGE_STATE } from "@/components/ui/status";
import { cn } from "@/lib/utils";

/**
 * StageProgress — the pipeline stepper.
 *
 * Rewritten. The previous version (iter-14.93) drew a red→amber→green
 * gradient track with 8px tick buttons and 9px labels. Four problems:
 *
 *  1. Stages the user had not reached yet were painted `text-red-500`.
 *     Not-yet-started is not an error; a healthy new project looked broken.
 *  2. Status was carried by colour alone — WCAG 1.4.1.
 *  3. The current stage's label measured 2.15:1 at 9px, making the most
 *     important label the least readable on the screen.
 *  4. Each stage rendered twice (tick + label), so five destinations cost
 *     ten tab stops, and the 8px ticks were well under the 24px target
 *     minimum in WCAG 2.5.8.
 *
 * Now: one button per stage, status carried by icon + text + colour, a
 * neutral rail with a filled completed segment, and a real <ol> so the
 * sequence is exposed to assistive tech.
 *
 * Project-type awareness (iter-14.94) and the Transformer phase override
 * (iter-15.14) are preserved unchanged.
 */
const LEGACY_STAGES = [
  { key: "Discovery",    n: 1, label: "Discovery",    short: "Discovery", path: "/" },
  { key: "DataModel",    n: 2, label: "Data Model",   short: "Data",      path: "/data-model" },
  { key: "Architecture", n: 3, label: "Architecture", short: "Arch",      path: "/architecture" },
  { key: "CodeGen",      n: 4, label: "Code Gen",     short: "Code",      path: "/code-gen" },
  { key: "Living",       n: 5, label: "Living",       short: "Living",    path: "/living" },
];

const GAP_ANALYSIS_STAGES = [
  { key: "Input",         n: 1, label: "Input",          short: "Input",  path: "/gap-analyzer#input"  },
  { key: "KnowledgeBase", n: 2, label: "Knowledge Base", short: "KB",     path: "/gap-analyzer#kb"     },
  { key: "Report",        n: 3, label: "Gap Report",     short: "Report", path: "/gap-analyzer#report" },
];

const TRANSFORMER_STAGES = [
  { key: "Input",         n: 1, label: "Input",       short: "Input",  path: "/transformer#input"  },
  { key: "KnowledgeBase", n: 2, label: "Knowledge Base", short: "KB",   path: "/transformer#kb"     },
  { key: "Output",        n: 3, label: "Transformed", short: "Output", path: "/transformer#output" },
];

const STAGES_BY_TYPE = {
  legacy_migration: LEGACY_STAGES,
  gap_analysis: GAP_ANALYSIS_STAGES,
  tech_transformer: TRANSFORMER_STAGES,
};

// iter-14.94 — tool project types resolve their stage from the URL hash.
const HASH_TO_STAGE = {
  input: "Input",
  kb: "KnowledgeBase",
  report: "Report",
  output: "Output",
};

// iter-15.14 — phase-driven active-stage override for tech_transformer.
const PHASE_TO_IDX = {
  uploading:    { activeIdx: 1, frozenBefore: 1, allDone: false },
  queued:       { activeIdx: 1, frozenBefore: 1, allDone: false },
  building_kb:  { activeIdx: 1, frozenBefore: 1, allDone: false },
  transforming: { activeIdx: 2, frozenBefore: 2, allDone: false },
  paused:       { activeIdx: 2, frozenBefore: 2, allDone: false },
  completed:    { activeIdx: 2, frozenBefore: 3, allDone: true },
  failed:       { activeIdx: 2, frozenBefore: 2, allDone: false },
  stopped:      { activeIdx: 2, frozenBefore: 2, allDone: false },
};

export default function StageProgress() {
  const { active } = useProjects();
  const navigate = useNavigate();
  const location = useLocation();

  // iter-15.14 — Transformer broadcasts its phase so this strip reflects
  // real progress rather than merely the current tab hash.
  const [transformerPhase, setTransformerPhase] = React.useState(() => {
    try {
      return window.localStorage.getItem("lama:transformer:phase");
    } catch {
      return null;
    }
  });

  React.useEffect(() => {
    const onEvt = (e) => setTransformerPhase(e?.detail?.phase || null);
    const onStorage = (e) => {
      if (e.key === "lama:transformer:phase") setTransformerPhase(e.newValue);
    };
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

  const isToolProject = ptype === "gap_analysis" || ptype === "tech_transformer";
  const isTransformer = ptype === "tech_transformer";

  let currentStage = null;
  if (isToolProject) {
    const hash = (location.hash || "").replace("#", "");
    currentStage = HASH_TO_STAGE[hash] || STAGES[0].key;
  } else {
    const routeMap = STAGES.reduce((m, s) => {
      m[s.path] = s.key;
      return m;
    }, {});
    currentStage = routeMap[location.pathname];
  }

  const transformerMap =
    isTransformer && transformerPhase ? PHASE_TO_IDX[transformerPhase] : null;
  const currentIdx = transformerMap
    ? transformerMap.activeIdx
    : STAGES.findIndex((s) => s.key === currentStage);

  if (transformerMap) {
    currentStage = STAGES[transformerMap.activeIdx]?.key || currentStage;
  }

  const statusMap = active.stage_status || {};

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

  const completed = STAGES.filter(
    (s, i) => effectiveStatus(s, i) === "frozen"
  ).length;

  return (
    <nav
      aria-label="Migration pipeline"
      data-testid="stage-progress"
      className="bg-surface px-4 sm:px-6 py-2 shrink-0 border-b border-border"
    >
      <div className="max-w-5xl mx-auto">
        <ol className="flex items-center gap-0.5 sm:gap-1 overflow-x-auto mos-scroll">
          {STAGES.map((s, i) => {
            const status = effectiveStatus(s, i);
            const meta = STAGE_STATE[status] || STAGE_STATE.locked;
            const Icon = meta.Icon;
            const isCurrent = currentStage === s.key;
            const isLocked = status === "locked";
            const isFrozen = status === "frozen";

            return (
              <li key={s.key} className="flex items-center gap-0.5 sm:gap-1 min-w-0">
                <button
                  type="button"
                  disabled={isLocked}
                  aria-current={isCurrent ? "step" : undefined}
                  aria-disabled={isLocked || undefined}
                  onClick={() => !isLocked && navigate(s.path)}
                  data-testid={`stage-progress-${s.key.toLowerCase()}`}
                  className={cn(
                    // 2.5.8: 32px target, and the label is inside it, so
                    // one tab stop per destination instead of two.
                    "group flex items-center gap-2 h-8 pl-1.5 pr-2 sm:pr-3 rounded",
                    "text-micro sm:text-xs font-medium min-w-0",
                    "transition-colors duration-fast ease",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-surface",
                    isLocked
                      ? "cursor-not-allowed text-fg-subtle"
                      : "hover:bg-surface-2 cursor-pointer",
                    isCurrent ? "text-fg font-semibold" : "text-fg-muted"
                  )}
                >
                  <span
                    className={cn(
                      "grid place-items-center size-5 shrink-0 rounded-lg border",
                      isFrozen && "bg-ok text-ok-fg border-ok",
                      isCurrent && !isFrozen &&
                        "bg-brand text-brand-fg border-brand-edge ring-2 ring-brand/30",
                      !isCurrent && !isFrozen && !isLocked &&
                        "bg-surface text-fg-muted border-border-strong",
                      isLocked && "bg-surface-2 text-fg-subtle border-border"
                    )}
                  >
                    <Icon className="size-3" aria-hidden />
                  </span>

                  <span className="truncate">
                    <span className="hidden sm:inline">{s.label}</span>
                    <span className="sm:hidden">{s.short}</span>
                  </span>

                  {/* The third channel: status as text, for anyone who
                      cannot use the icon or the colour. */}
                  <span className="sr-only">, {meta.sr}</span>
                </button>

                {i < STAGES.length - 1 && (
                  <span
                    aria-hidden
                    className={cn(
                      "h-px w-3 sm:w-5 shrink-0 transition-colors duration-fast ease",
                      isFrozen ? "bg-ok" : "bg-border"
                    )}
                  />
                )}
              </li>
            );
          })}

          <li className="ml-auto pl-3 shrink-0 hidden md:block">
            <span className="text-micro text-fg-subtle tabular-nums">
              {completed}/{STAGES.length} frozen
            </span>
          </li>
        </ol>
      </div>
    </nav>
  );
}
