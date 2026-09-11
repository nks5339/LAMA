import React, { useEffect, useRef, useState } from "react";
import { Loader2, CheckCircle2, AlertCircle, X, FileText } from "lucide-react";
import { kbBuildProgress } from "@/lib/api";

/**
 * BuildKBProgressDialog — iter 14.10
 *
 * Small modal popup that opens the moment the user clicks "Build Knowledge
 * Base" and polls `/api/kb/{pid}/build-progress` every 1s. Renders:
 *   - Current phase badge (queued → extracting → aggregating → … → done)
 *   - Live per-file counter + name during "extracting" (rebuilt-from-chunks
 *     OWL entity extraction; also chunk_indexed count during
 *     qdrant_indexing).
 *   - Fatal error message if the backend reports phase=error.
 *
 * Auto-closes 800ms after phase=done. On error the user must click Close.
 */

const PHASE_LABEL = {
  queued: "Queued",
  extracting: "Extracting entities",
  aggregating: "Aggregating",
  tech_detect: "Detecting tech stack",
  business_ontology: "Business ontology",
  toon_persist: "Persisting TOON",
  graph_build: "Building graph",
  graphify: "Graphifying entities",
  qdrant_indexing: "Indexing vectors",
  done: "Done",
  error: "Failed",
  idle: "Idle",
};

const PHASE_ORDER = [
  "queued",
  "extracting",
  "aggregating",
  "tech_detect",
  "business_ontology",
  "toon_persist",
  "graph_build",
  "graphify",
  "qdrant_indexing",
  "done",
];

export default function BuildKBProgressDialog({ open, projectId, onClose }) {
  const [state, setState] = useState(null);
  const [tickAt, setTickAt] = useState(Date.now());
  const stopRef = useRef(false);
  const startedAtRef = useRef(null);

  useEffect(() => {
    if (!open || !projectId) return;
    stopRef.current = false;
    startedAtRef.current = Date.now();
    setState(null);

    let cancelled = false;
    const tick = async () => {
      if (cancelled || stopRef.current) return;
      try {
        const s = await kbBuildProgress(projectId);
        if (cancelled) return;
        setState(s);
        setTickAt(Date.now());
        if (s?.phase === "done") {
          // give the UI ~800ms to show the green tick, then auto-close.
          setTimeout(() => { if (!cancelled) onClose?.(true); }, 800);
          return;
        }
        if (s?.phase === "error") {
          return; // wait for user to close
        }
      } catch (e) {
        // Ignore transient errors; keep polling.
      }
      setTimeout(tick, 1000);
    };
    tick();

    return () => {
      cancelled = true;
      stopRef.current = true;
    };
  }, [open, projectId, onClose]);

  if (!open) return null;

  const phase = state?.phase || "queued";
  const label = PHASE_LABEL[phase] || phase;
  const idx = Math.max(0, PHASE_ORDER.indexOf(phase));
  const pct = phase === "error" ? 100 : Math.round((idx / (PHASE_ORDER.length - 1)) * 100);
  const running = state?.running !== false && phase !== "done" && phase !== "error";
  const elapsedSec = startedAtRef.current ? Math.round((tickAt - startedAtRef.current) / 1000) : 0;

  const currentFile = state?.current_file || "";
  const extracted = state?.extracted_files ?? 0;
  const totalExtractable = state?.total_extractable ?? 0;
  const chunksIndexed = state?.chunks_indexed ?? 0;
  const graphNodes = state?.graph_nodes ?? 0;
  const graphEdges = state?.graph_edges ?? 0;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      data-testid="build-kb-progress-dialog"
    >
      <div className="bg-white rounded-lg shadow-2xl w-[520px] max-w-[92vw] p-5 border border-[#E6E6E6]">
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            {phase === "done" ? (
              <CheckCircle2 className="w-5 h-5 text-green-600" />
            ) : phase === "error" ? (
              <AlertCircle className="w-5 h-5 text-red-600" />
            ) : (
              <Loader2 className="w-5 h-5 text-[#2E2E38] animate-spin" />
            )}
            <h2 className="font-display text-base font-bold text-[#2E2E38]">
              Building Knowledge Base
            </h2>
          </div>
          {!running && (
            <button
              type="button"
              onClick={() => onClose?.(phase === "done")}
              className="text-[#747480] hover:text-[#2E2E38]"
              data-testid="build-kb-close"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Phase badge + elapsed */}
        <div className="flex items-center justify-between text-[11px] mb-2">
          <span
            className={
              "px-2 py-0.5 rounded-full font-medium uppercase tracking-wider " +
              (phase === "done"
                ? "bg-green-100 text-green-700"
                : phase === "error"
                ? "bg-red-100 text-red-700"
                : "bg-yellow-100 text-yellow-800")
            }
            data-testid="build-kb-phase"
          >
            {label}
          </span>
          <span className="text-[#747480]">
            {elapsedSec}s
          </span>
        </div>

        {/* Progress bar */}
        <div className="w-full h-2 bg-[#F2F2F2] rounded-full overflow-hidden mb-3">
          <div
            className={
              "h-full transition-all " +
              (phase === "error" ? "bg-red-500" : phase === "done" ? "bg-green-500" : "bg-[#FFE600]")
            }
            style={{ width: `${pct}%` }}
          />
        </div>

        {/* Per-phase details */}
        <div className="space-y-2 text-[12px] text-[#2E2E38]">
          {phase === "extracting" && (
            <>
              <div className="flex items-center justify-between">
                <span className="text-[#747480]">Files extracted</span>
                <span className="font-mono">
                  {extracted}
                  {totalExtractable ? ` / ${totalExtractable}` : ""}
                </span>
              </div>
              {currentFile && (
                <div
                  className="flex items-center gap-1.5 text-[11px] text-[#747480] font-mono truncate"
                  title={currentFile}
                  data-testid="build-kb-current-file"
                >
                  <FileText className="w-3 h-3 shrink-0" />
                  <span className="truncate">{currentFile}</span>
                </div>
              )}
            </>
          )}

          {phase === "qdrant_indexing" && (
            <div className="flex items-center justify-between">
              <span className="text-[#747480]">Chunks indexed</span>
              <span className="font-mono">{chunksIndexed}</span>
            </div>
          )}

          {(phase === "graph_build" || phase === "graphify" || phase === "done") && (graphNodes || graphEdges) ? (
            <div className="flex items-center justify-between">
              <span className="text-[#747480]">Graph</span>
              <span className="font-mono">
                {graphNodes} nodes · {graphEdges} edges
              </span>
            </div>
          ) : null}

          {phase === "done" && (
            <div className="text-[11px] text-green-700">
              Knowledge base ready. Closing…
            </div>
          )}

          {phase === "error" && (
            <div className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded p-2 break-words">
              {state?.error || "Unknown error. Check backend logs."}
            </div>
          )}
        </div>

        {/* Phase pipeline (compact) */}
        <div className="mt-4 flex items-center gap-1 text-[9px] text-[#747480] uppercase tracking-wider overflow-x-auto">
          {PHASE_ORDER.filter((p) => p !== "done").map((p, i) => (
            <React.Fragment key={p}>
              <span
                className={
                  "px-1.5 py-0.5 rounded whitespace-nowrap " +
                  (i < idx
                    ? "bg-[#2E2E38] text-white"
                    : i === idx
                    ? "bg-[#FFE600] text-[#2E2E38]"
                    : "bg-[#F2F2F2] text-[#747480]")
                }
              >
                {PHASE_LABEL[p] || p}
              </span>
              {i < PHASE_ORDER.length - 2 && <span>›</span>}
            </React.Fragment>
          ))}
        </div>
      </div>
    </div>
  );
}
