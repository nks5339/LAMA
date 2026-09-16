/**
 * ConfidenceBadge - iter-13.71 (job-aware variant)
 *
 * Per-stage Accuracy / Confidence pill rendered next to every Freeze
 * button. Clicking the pill opens a popover. If no run has happened yet
 * (or the user clicks "Recompute"), it kicks off a BACKGROUND job and
 * the popover live-streams: percentage done, current section, running
 * score, plus Pause / Resume / Stop controls.
 *
 * Backend contract: see backend/routes/pipeline.py
 *   POST  /pipeline/{pid}/confidence/{stage}/recompute  -> { job_id }
 *   GET   /pipeline/{pid}/confidence/jobs/{jid}         -> live progress
 *   POST  .../jobs/{jid}/pause | resume | stop          -> control
 *   GET   /pipeline/{pid}/confidence/{stage}            -> latest result
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Loader2, RefreshCw, Shield, ShieldAlert, ShieldCheck, ShieldQuestion,
  Pause, Play, Square,
} from "lucide-react";
import { toast } from "sonner";

import {
  getStageConfidence,
  recomputeStageConfidence,
  getConfidenceJob,
  pauseConfidenceJob,
  resumeConfidenceJob,
  stopConfidenceJob,
} from "../lib/api";

const BAND_STYLE = {
  excellent: { bg: "bg-emerald-50", border: "border-emerald-300", text: "text-emerald-800", Icon: ShieldCheck, dot: "bg-emerald-500", bar: "bg-emerald-500" },
  good:      { bg: "bg-sky-50",     border: "border-sky-300",     text: "text-sky-800",     Icon: Shield,      dot: "bg-sky-500",     bar: "bg-sky-500"     },
  moderate:  { bg: "bg-amber-50",   border: "border-amber-300",   text: "text-amber-800",   Icon: ShieldAlert, dot: "bg-amber-500",   bar: "bg-amber-500"   },
  poor:      { bg: "bg-rose-50",    border: "border-rose-300",    text: "text-rose-800",    Icon: ShieldAlert, dot: "bg-rose-500",    bar: "bg-rose-500"    },
  unknown:   { bg: "bg-bg",  border: "border-border",   text: "text-fg-muted",   Icon: ShieldQuestion, dot: "bg-fg-subtle", bar: "bg-fg-subtle" },
};

function bandOf(score) {
  if (score >= 95) return "excellent";
  if (score >= 85) return "good";
  if (score >= 70) return "moderate";
  return "poor";
}

const POLL_INTERVAL_MS = 1500;
const TERMINAL_STATUSES = new Set(["complete", "error", "stopped"]);

export default function ConfidenceBadge({ projectId, stage, compact = false, onScore }) {
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);

  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const pollRef = useRef(null);
  // iter-13.71b — ref on the anchor element so the portal popover can
  // position itself relative to the badge (avoiding parent overflow
  // clipping that destructed the layout in the DataModel toolbar).
  const anchorRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const fetchLatest = useCallback(async () => {
    if (!projectId || !stage) return;
    setLoading(true);
    try {
      const d = await getStageConfidence(projectId, stage);
      setDoc(d);
      if (onScore && d?.present) onScore(d);
    } catch (e) {
      setDoc({ present: false });
    } finally {
      setLoading(false);
    }
  }, [projectId, stage, onScore]);

  useEffect(() => { fetchLatest(); }, [fetchLatest]);

  const beginPoll = useCallback((jid) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      if (document.hidden) return;
      try {
        const j = await getConfidenceJob(projectId, jid);
        setJob(j);
        if (TERMINAL_STATUSES.has(j.status)) {
          stopPolling();
          await fetchLatest();
          if (j.status === "complete") {
            const latest = j.result?.overall_score;
            const best = j.result?.best_overall_score;
            const label =
              typeof best === "number" && typeof latest === "number" && latest + 0.05 < best
                ? `${stage} confidence: ${best.toFixed(1)}% (this run ${latest.toFixed(1)}%)`
                : `${stage} confidence: ${(best ?? latest)?.toFixed?.(1) ?? "-"}%`;
            toast.success(label);
          } else if (j.status === "stopped") {
            toast.message(`${stage} confidence stopped`, {
              description: j.result?.partial ? "Partial result persisted." : "",
            });
          } else if (j.status === "error") {
            toast.error(`${stage} confidence failed`, { description: (j.error || "").slice(0, 200) });
          }
        }
      } catch (e) {
        stopPolling();
      }
    }, POLL_INTERVAL_MS);
  }, [projectId, stage, stopPolling, fetchLatest]);

  // iter-14.22 — Removed `Quick` (fast) recompute + `Improve → ≥95%`
  // buttons. The Quick path became redundant when confidence scoring
  // moved to HF-only (iter-14.19/21): full recompute is already
  // token-free and ~4s. The Improve loop was the only remaining
  // Factory-CLI consumer on the confidence popover — it regenerated
  // below-threshold sections via `srs.regenerate` → fabric_call → droid.
  // Per user direction, both are gone. The score-gated auto-retry inside
  // `_run_one_section` (during SRS generation, target = MIN_SECTION_
  // CONFIDENCE) still closes gaps against legacy KB automatically.
  const startRecompute = useCallback(async () => {
    setOpen(true);
    // If a run is already in flight, stop it first — otherwise the button
    // appears to do nothing (previous behaviour: silent early-return).
    if (job && !TERMINAL_STATUSES.has(job.status)) {
      try {
        if (jobId) {
          await stopConfidenceJob(projectId, jobId);
        }
        toast.message(`${stage} confidence: previous run stopped, starting fresh…`);
        stopPolling();
        setJob((j) => (j ? { ...j, status: "stopped", control: "stopped" } : j));
      } catch (e) {
        toast.error("Could not stop the running confidence job", {
          description: e.response?.data?.detail || e.message,
        });
        return;
      }
    }
    try {
      const r = await recomputeStageConfidence(projectId, stage, {});
      if (r?.job_id) {
        setJobId(r.job_id);
        setJob({ id: r.job_id, status: "queued", pct: 0,
                 step: "Queued...",
                 section_done: 0, section_total: 0,
                 kind: "recompute" });
        beginPoll(r.job_id);
      } else {
        setDoc({ present: true, ...r });
      }
    } catch (e) {
      toast.error("Could not start confidence run", {
        description: e.response?.data?.detail || e.message,
      });
    }
  }, [projectId, stage, job, jobId, beginPoll, stopPolling]);

  // iter-14.22 — Removed the `startImprove` auto-improve-to-≥95% callback
  // + its "Improve" button below. It was the only path on the confidence
  // popover that regenerated SRS sections through fabric_call → Factory-
  // CLI (via the `srs.regenerate` agent), and the user asked for zero
  // Factory involvement on the confidence surface. The score-gated
  // auto-retry inside `_run_one_section` (during generation, target =
  // MIN_SECTION_CONFIDENCE) still closes gaps automatically against
  // legacy KB, so this path is not needed on the popover.

  const onPause = async () => {
    try {
      await pauseConfidenceJob(projectId, jobId);
      setJob((j) => (j ? { ...j, control: "paused", status: "paused" } : j));
    } catch (e) { toast.error("Pause failed", { description: e.message }); }
  };
  const onResume = async () => {
    try {
      await resumeConfidenceJob(projectId, jobId);
      setJob((j) => (j ? { ...j, control: "running", status: "running" } : j));
    } catch (e) { toast.error("Resume failed", { description: e.message }); }
  };
  const onStop = async () => {
    try {
      await stopConfidenceJob(projectId, jobId);
      setJob((j) => (j ? { ...j, control: "stopping", step: "Stopping..." } : j));
    } catch (e) { toast.error("Stop failed", { description: e.message }); }
  };

  const present = doc?.present;
  // iter-14.9 — pill renders the high-water mark, not the LATEST run.
  // The multi-model evaluator is stochastic (±1-3% between identical runs)
  // and prior behaviour let the pill regress on recompute even when
  // nothing about the artifacts had changed. `best_overall_score` is now
  // persisted by the backend across recomputes; we fall back to the
  // latest for older docs that pre-date iter-14.9.
  const latestScore = present ? Number(doc.overall_score || 0) : null;
  const bestScore = present
    ? Number(doc.best_overall_score ?? doc.overall_score ?? 0)
    : null;
  const score = present ? Math.max(bestScore ?? 0, latestScore ?? 0) : null;
  const band = present ? (bandOf(score)) : "unknown";
  const style = BAND_STYLE[band] || BAND_STYLE.unknown;
  const Icon = style.Icon;
  const running = !!job && !TERMINAL_STATUSES.has(job.status);
  const computing = running;

  if (compact) {
    return (
      <div ref={anchorRef} className="relative inline-flex" data-testid={`stage-confidence-${stage}`}>
        <button
          type="button"
          onClick={() => {
            if (!present && !running) startRecompute();
            else setOpen((o) => !o);
          }}
          onContextMenu={(e) => { e.preventDefault(); startRecompute(); }}
          title={present
            ? `${stage} confidence ${score?.toFixed?.(1)}% - click to expand, right-click to recompute`
            : `Compute ${stage} confidence`}
          data-testid={`stage-confidence-pill-${stage}`}
          className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-1 text-micro font-semibold ${style.bg} ${style.border} ${style.text} hover:opacity-90 whitespace-nowrap`}
        >
          {computing
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <Icon className="w-3.5 h-3.5" />}
          <span className="uppercase tracking-wider text-micro font-bold opacity-80">Confidence</span>
          <span className="font-bold tabular-nums">
            {computing
              ? `${job?.pct ?? 0}%`
              : (present ? `${score.toFixed(1)}%` : "-")}
          </span>
        </button>
        {open && (
          <PopoverPanel
            anchorRef={anchorRef}
            doc={doc} job={job} stage={stage}
            style={style}
            running={running}
            onClose={() => setOpen(false)}
            onRecompute={startRecompute}
            onPause={onPause} onResume={onResume} onStop={onStop}
          />
        )}
      </div>
    );
  }

  return (
    <div ref={anchorRef} className="relative inline-flex flex-col items-stretch" data-testid={`stage-confidence-${stage}`}>
      <div className={`inline-flex items-center gap-2 rounded-sm border px-2 py-1 text-micro ${style.bg} ${style.border} ${style.text}`}>
        {computing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Icon className="w-3.5 h-3.5" />}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="font-semibold underline-offset-2 hover:underline"
          data-testid={`stage-confidence-toggle-${stage}`}
        >
          Confidence {computing ? `${job?.pct ?? 0}%` : (present ? `${score.toFixed(1)}%` : "-")}
        </button>
        <span className="capitalize text-micro opacity-80">{band}</span>
        <button
          type="button"
          onClick={() => startRecompute()}
          disabled={loading}
          className="ml-1 inline-flex items-center gap-1 rounded-sm border border-current/30 px-1.5 py-0.5 text-micro hover:bg-surface/40 disabled:opacity-50"
          data-testid={`stage-confidence-recompute-${stage}`}
          title={computing
            ? "A confidence run is in progress — click to stop it and start a fresh recompute."
            : (present ? "Score this stage again from scratch." : "Compute the confidence score.")}
        >
          {computing ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          {computing ? "Restart" : (present ? "Recompute" : "Compute")}
        </button>
      </div>
      {open && (
        <PopoverPanel
          anchorRef={anchorRef}
          doc={doc} job={job} stage={stage}
          style={style}
          running={running}
          onClose={() => setOpen(false)}
          onRecompute={startRecompute}
          onPause={onPause} onResume={onResume} onStop={onStop}
        />
      )}
    </div>
  );
}


function PopoverPanel({
  anchorRef, doc, job, stage, style, running, onClose, onRecompute,
  onPause, onResume, onStop,
}) {
  const present = doc?.present;
  const isPaused = job?.status === "paused" || job?.control === "paused";
  const isStopping = job?.control === "stopping";

  // iter-13.71b — viewport-bound position computed from the anchor's
  // bounding rect; recomputed on resize/scroll so the popover stays
  // anchored even when the user scrolls the underlying panel.
  // iter-13.72 — bumped width from 400 → 440 and made the inner table
  // table-layout:fixed + overflow-x:hidden so wide cell content (long
  // section names, models-used line, gap text) ellipsizes instead of
  // forcing horizontal scroll — which was auto-scrolling the popup
  // and clipping the left of every visible row in the screenshot bug.
  const PANEL_WIDTH = 440;
  const [pos, setPos] = useState({ top: 0, left: 0 });

  useEffect(() => {
    const recompute = () => {
      const el = anchorRef?.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const margin = 8;
      let left = r.right - PANEL_WIDTH;        // anchor right edge
      const minLeft = margin;
      const maxLeft = window.innerWidth - PANEL_WIDTH - margin;
      if (left < minLeft) left = minLeft;
      if (left > maxLeft) left = maxLeft;
      setPos({ top: r.bottom + 4, left });
    };
    recompute();
    window.addEventListener("resize", recompute);
    window.addEventListener("scroll", recompute, true);   // capture phase = catches scrolls in any ancestor
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
    };
  }, [anchorRef]);

  const panel = (
    <>
      <button
        type="button"
        aria-label="Close confidence panel"
        onClick={onClose}
        className="fixed inset-0 z-[1000] cursor-default"
      />
      <div role="presentation"
        className="fixed z-[1001] max-h-[440px] overflow-x-hidden overflow-y-auto rounded-sm border border-border bg-surface shadow-xl text-micro"
        style={{ top: pos.top, left: pos.left, width: PANEL_WIDTH }}
        data-testid={`stage-confidence-detail-${stage}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={`px-3 py-2 border-b border-border flex items-center justify-between gap-2 ${style.bg}`}>
          <div className="flex items-center gap-1.5 min-w-0 flex-1 flex-wrap">
            <span className={`uppercase tracking-wider text-micro font-bold ${style.text} truncate`}>
              {stage} - Confidence
            </span>
            {present && !running && (() => {
              // iter-14.9 — headline shows the high-water mark; if the
              // latest run regressed, surface both so the drift is visible.
              const latest = Number(doc.overall_score || 0);
              const best = Number(doc.best_overall_score ?? doc.overall_score ?? 0);
              const regressed = latest + 0.05 < best;
              return (
                <>
                  <span
                    className={`text-micro font-bold tabular-nums ${style.text} shrink-0`}
                    title={regressed
                      ? `Best ever: ${best.toFixed(1)}% · this run: ${latest.toFixed(1)}%`
                      : `Score: ${best.toFixed(1)}%`}
                    data-testid={`stage-confidence-best-${stage}`}
                  >
                    {best.toFixed(1)}%
                  </span>
                  {regressed && (
                    <span
                      className="text-micro text-amber-700 font-semibold shrink-0"
                      data-testid={`stage-confidence-latest-${stage}`}
                      title="Latest recompute scored lower than the best on record. The pill retains the best; drill down for the drift."
                    >
                      (this run {latest.toFixed(1)}%)
                    </span>
                  )}
                </>
              );
            })()}
          </div>
          <div className="shrink-0 inline-flex items-center gap-1">
            {/* iter-14.22 — Removed `Improve → ≥95%` (emerald) and `Quick`
                (sky) buttons. Improve was the only remaining Factory-CLI
                consumer on this popover — it went through fabric_call
                → droid via the `srs.regenerate` agent. Quick was made
                redundant when confidence scoring moved to HF-only
                (iter-14.19/21): full recompute is already token-free
                and completes in ~4s. Full `Recompute` is retained. */}
            <button
              type="button"
              onClick={() => onRecompute()}
              className="inline-flex items-center gap-1 rounded-sm border border-fg/20 bg-surface px-1.5 py-0.5 text-micro text-fg hover:bg-bg"
              data-testid={`stage-confidence-recompute-action-${stage}`}
              title={running
                ? "A confidence run is in progress — click to stop it and start a fresh recompute."
                : (present ? "Full recompute: multi-model panel over every section." : "Compute the confidence score for this stage.")}
            >
              {running ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
              {running ? "Restart" : (present ? "Recompute" : "Compute now")}
            </button>
          </div>
        </div>

        {job && (
          <div className="px-3 py-2 border-b border-border bg-brand-tint">
            <div className="flex items-center justify-between gap-2 mb-1">
              <div className="flex items-center gap-1.5 text-fg font-semibold min-w-0 flex-1">
                {isPaused
                  ? <Pause className="w-3 h-3 text-amber-600 shrink-0" />
                  : isStopping
                    ? <Square className="w-3 h-3 text-rose-600 shrink-0" />
                    : <Loader2 className="w-3 h-3 animate-spin text-fg shrink-0" />}
                <span className="truncate" title={job.step}>{job.step || "Working..."}</span>
              </div>
              <span className="tabular-nums font-bold text-fg shrink-0">{job.pct ?? 0}%</span>
            </div>
            <div className="h-1.5 w-full rounded-full bg-border overflow-hidden">
              <div
                className={`h-full transition-[width] duration-300 ${style.bar}`}
                style={{ width: `${Math.max(0, Math.min(100, job.pct ?? 0))}%` }}
              />
            </div>
            <div className="mt-1 flex items-center justify-between text-micro text-fg-muted">
              <span>
                Section {job.section_done ?? 0} / {job.section_total ?? 0}
                {job.current_section ? ` - ${job.current_section}` : ""}
              </span>
              {job.running_score != null && (
                <span className="font-semibold text-fg tabular-nums">
                  running: {Number(job.running_score).toFixed(1)}%
                </span>
              )}
            </div>
            {Array.isArray(job.iterations) && job.iterations.length > 0 && (
              <div
                className="mt-2 rounded-sm border border-emerald-200 bg-emerald-50/60 p-1.5"
                data-testid={`stage-confidence-trajectory-${stage}`}
              >
                <div className="mb-1 flex items-center justify-between text-micro uppercase tracking-wider font-bold text-emerald-800">
                  <span>Auto-improve trajectory</span>
                  {typeof job.tokens_used === 'number' && (
                    <span
                      className="normal-case tracking-normal font-normal text-emerald-700"
                      data-testid={`stage-confidence-tokens-${stage}`}
                      title={job.token_budget ? `Budget ${job.token_budget.toLocaleString()}` : ''}
                    >
                      tokens {job.tokens_used.toLocaleString()}
                      {job.token_budget ? ` / ${(job.token_budget/1000).toFixed(0)}k` : ''}
                    </span>
                  )}
                </div>
                <div className="flex flex-col gap-0.5">
                  {job.iterations.map((it) => (
                    <div key={it.iteration} className="flex items-center justify-between text-micro">
                      <span className="text-fg font-semibold">
                        Iter {it.iteration}
                        {it.scoring_mode === 'lean-delta' && (
                          <span className="ml-1 text-fg-muted font-normal">(δ)</span>
                        )}
                        {it.scoring_mode === 'final-seal' && (
                          <span className="ml-1 text-emerald-700 font-normal">(seal)</span>
                        )}
                      </span>
                      <span className="tabular-nums">
                        score {Number(it.overall_score ?? 0).toFixed(1)}%
                        {it.best_overall_score != null && (
                          <span className="ml-1 text-emerald-700 font-semibold">
                            best {Number(it.best_overall_score).toFixed(1)}%
                          </span>
                        )}
                      </span>
                      <span className="text-fg-muted">
                        {it.below_count ?? 0} below · regen {(it.regenerated || []).length}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="mt-2 flex items-center gap-1">
              {isPaused ? (
                <button
                  type="button"
                  onClick={onResume}
                  disabled={isStopping}
                  data-testid={`stage-confidence-resume-${stage}`}
                  title="Resume"
                  aria-label="Resume"
                  className="inline-flex items-center justify-center rounded-sm border border-border bg-surface w-7 h-7 text-emerald-700 hover:bg-emerald-50 disabled:opacity-50"
                >
                  <Play className="w-3.5 h-3.5" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onPause}
                  disabled={isStopping}
                  data-testid={`stage-confidence-pause-${stage}`}
                  title="Pause"
                  aria-label="Pause"
                  className="inline-flex items-center justify-center rounded-sm border border-border bg-surface w-7 h-7 text-amber-700 hover:bg-amber-50 disabled:opacity-50"
                >
                  <Pause className="w-3.5 h-3.5" />
                </button>
              )}
              <button
                type="button"
                onClick={onStop}
                disabled={isStopping}
                data-testid={`stage-confidence-stop-${stage}`}
                title="Stop"
                aria-label="Stop"
                className="inline-flex items-center justify-center rounded-sm border border-rose-300 bg-surface w-7 h-7 text-rose-700 hover:bg-rose-50 disabled:opacity-50"
              >
                <Square className="w-3.5 h-3.5" />
              </button>
              <span className="ml-2 text-micro text-fg-muted italic truncate flex-1">
                {isStopping ? "Finishing current section…" : (isPaused ? "Paused" : "Working…")}
              </span>
            </div>
          </div>
        )}

        {present ? (
          <div className="px-3 py-2">
            <div className="mb-1 text-micro text-fg-muted truncate" title={`${(doc.models_used || []).join(", ")}${doc.generated_at ? ` - ${new Date(doc.generated_at).toLocaleString()}` : ""}`}>
              Models: {(doc.models_used || []).join(", ") || "(default)"}
              {doc.generated_at && <> - {new Date(doc.generated_at).toLocaleString()}</>}
              {doc.partial && <span className="ml-1 text-amber-700 font-semibold">- partial</span>}
            </div>
            <table className="w-full text-micro table-fixed">
              <colgroup>
                <col style={{ width: "44%" }} />
                <col style={{ width: "16%" }} />
                <col style={{ width: "40%" }} />
              </colgroup>
              <thead>
                <tr className="text-left text-micro text-fg-muted">
                  <th className="py-0.5 pr-2 font-medium">Section</th>
                  <th className="py-0.5 pr-2 font-medium text-right">Score</th>
                  <th className="py-0.5 font-medium">Top gap</th>
                </tr>
              </thead>
              <tbody>
                {(doc.sections || []).map((s) => {
                  const b = BAND_STYLE[s.band] || BAND_STYLE.unknown;
                  const topGap = s.missing
                    ? "Artifact not generated yet"
                    : (s.gaps?.[0]) || s.rationale || "-";
                  return (
                    <tr key={s.key} className="border-t border-surface-2 align-top">
                      <td className="py-1 pr-2 font-medium truncate" title={s.label || s.key}>
                        {s.label || s.key}
                      </td>
                      <td className={`py-1 pr-2 font-semibold ${b.text} tabular-nums text-right whitespace-nowrap`}>
                        {typeof s.score === "number" ? `${s.score.toFixed(1)}%` : "-"}
                      </td>
                      <td className="py-1 text-fg truncate" title={topGap}>
                        {topGap}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {doc.sections_below_95 === 0 && doc.missing_artifacts === 0 ? (
              <div className="mt-2 text-micro text-emerald-700">
                All sections &gt;= 95% - recommended to freeze.
              </div>
            ) : (
              <div className="mt-2 text-micro text-amber-700">
                Regenerate sections below 95% (or fill missing artifacts) for best fidelity.
              </div>
            )}
          </div>
        ) : !job ? (
          <div className="px-3 py-4 text-fg-muted text-center">
            No confidence report yet. Click "Compute now" to run the multi-model evaluator.
          </div>
        ) : null}
      </div>
    </>
  );

  // iter-13.71b — portal to <body> so the popover escapes any parent
  // overflow-hidden / overflow-auto wrapper (the bug that destructed
  // the DataModel toolbar in the screenshot).
  return createPortal(panel, document.body);
}


