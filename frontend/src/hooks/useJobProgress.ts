/**
 * Long-job progress contract.
 *
 * Every long-running surface in LAMA (SRS generation, HLD/LLD, DDL, the
 * multi-agent CodeGen run, a Transformer pass) previously showed the same
 * thing: a spinner. There was no elapsed time, no phase, no expected range
 * and no cancel — so a 90-second generation looked identical to a hung one,
 * and users re-triggered jobs that were already running.
 *
 * This hook supplies the numbers; <JobProgress /> renders them.
 */
import { useEffect, useRef, useState } from "react";

/** Ticks once a second while `running`, and freezes on the final value. */
export function useElapsed(running: boolean): number {
  const startedAt = useRef<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!running) {
      startedAt.current = null;
      return undefined;
    }
    startedAt.current = Date.now();
    setElapsed(0);
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - (startedAt.current ?? Date.now())) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [running]);

  return elapsed;
}

/** 95s → "1m 35s". Kept short so it fits a status line. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * Human phase labels. Keys match the agent/phase names the backend already
 * emits, so a new phase falls back to a readable version of its own key
 * rather than rendering blank.
 */
export const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  uploading: "Uploading source files",
  scanning: "Scanning the source tree",
  parsing: "Parsing source files",
  building_kb: "Building the knowledge base",
  embedding: "Indexing for retrieval",
  retrieving: "Searching the knowledge base",
  thinking: "Reasoning",
  generating: "Generating",
  transforming: "Transforming source files",
  // multi-agent CodeGen
  context_manager: "Gathering stage context",
  planner: "Planning the work",
  coder_be: "Writing backend code",
  coder_fe: "Writing frontend code",
  verifier: "Verifying output",
  reviewer: "Reviewing changes",
  tester: "Running tests",
  traceability_gate: "Checking requirement traceability",
  finalizer: "Finalising",
  paused: "Paused",
};

export function phaseLabel(phase: string | null | undefined): string | null {
  if (!phase) return null;
  return (
    PHASE_LABELS[phase] ||
    String(phase).replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase())
  );
}
