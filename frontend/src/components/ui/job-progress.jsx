/**
 * The "AI is working" surfaces.
 *
 * Three components, all of which announce themselves. The audit found zero
 * aria-live regions in an app whose core loop is long-running async AI work,
 * so a screen-reader user was never told a job started, progressed or ended.
 *
 *   <JobProgress />    a running job: phase, elapsed, expected range, cancel
 *   <ThinkingDots />   the AI is composing a reply
 *   <AgentTimeline />  what the agents actually did, collapsed by default
 */
import { useId } from "react";
import { Loader2, X, ChevronRight, CircleDot, CheckCircle2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ui/status";
import { formatDuration, phaseLabel } from "@/hooks/useJobProgress";

/**
 * A running job, narrated.
 *
 * `expected` is a plain string like "60–120s" — showing the normal range is
 * what lets a user tell "slow" from "stuck" without us having to guess.
 */
export function JobProgress({
  phase,
  elapsed,
  expected,
  percent,
  detail,
  onCancel,
  className,
}) {
  const label = phaseLabel(phase) || "Working";

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        "flex items-start gap-3 rounded border border-border bg-surface-2 px-4 py-3",
        className
      )}
      data-testid="job-progress"
    >
      <Loader2 className="size-4 mt-0.5 shrink-0 animate-spin text-fg-muted" aria-hidden />

      <div className="min-w-0 flex-1 flex flex-col gap-1.5">
        <p className="text-sm font-medium text-fg">{label}</p>

        {detail && <p className="text-xs text-fg-muted truncate">{detail}</p>}

        {typeof percent === "number" && (
          <ProgressBar value={percent} showPercentage={false} tone="brand" />
        )}

        <p className="text-micro text-fg-subtle tabular-nums">
          {formatDuration(elapsed)} elapsed
          {expected ? ` · typically ${expected}` : ""}
        </p>
      </div>

      {onCancel && (
        <Button
          size="sm"
          variant="ghost"
          onClick={onCancel}
          className="shrink-0"
          data-testid="job-cancel-btn"
        >
          <X className="size-3.5" aria-hidden />
          Cancel
        </Button>
      )}
    </div>
  );
}

/**
 * Composing indicator for chat. The pulse is decorative and aria-hidden;
 * the sr-only text is what actually gets announced.
 */
export function ThinkingDots({ label = "Thinking", className }) {
  return (
    <div
      className={cn("flex items-center gap-2 text-xs text-fg-muted", className)}
      data-testid="thinking-indicator"
    >
      <span className="relative flex size-2" aria-hidden>
        <span className="absolute inline-flex size-full rounded-lg bg-brand opacity-75 motion-safe:animate-ping" />
        <span className="relative inline-flex size-2 rounded-lg bg-brand-edge" />
      </span>
      <span aria-hidden>{label}…</span>
      <span className="sr-only" role="status" aria-live="polite">
        {label}
      </span>
    </div>
  );
}

const STEP_ICON = {
  done: CheckCircle2,
  running: Loader2,
  failed: XCircle,
  pending: CircleDot,
};

const STEP_TONE = {
  done: "text-ok",
  running: "text-info",
  failed: "text-crit",
  pending: "text-fg-subtle",
};

/**
 * What the agents did. Collapsed by default so it is progressive disclosure
 * rather than noise, but the summary line always states the current agent —
 * which is the one thing a waiting user wants to know.
 *
 * `steps`: [{ id, agent, summary, status, source, duration }]
 */
export function AgentTimeline({ steps = [], defaultOpen = false, className }) {
  const id = useId();
  if (!steps.length) return null;

  const current =
    steps.find((s) => s.status === "running") || steps[steps.length - 1];

  return (
    <details
      open={defaultOpen}
      className={cn(
        "group rounded border border-border bg-surface-2 overflow-hidden",
        className
      )}
      data-testid="agent-timeline"
    >
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer text-micro text-fg-muted hover:bg-surface-3 select-none">
        <ChevronRight
          className="size-3 shrink-0 transition-transform duration-fast ease group-open:rotate-90"
          aria-hidden
        />
        <span className="font-medium">
          {steps.length} step{steps.length === 1 ? "" : "s"}
        </span>
        {current?.agent && (
          <>
            <span aria-hidden className="text-fg-subtle">
              ·
            </span>
            <span className="truncate font-mono">{phaseLabel(current.agent)}</span>
          </>
        )}
      </summary>

      <ol className="px-3 pb-2 pt-0.5" aria-label="Agent steps" id={id}>
        {steps.map((s, i) => {
          const Icon = STEP_ICON[s.status] || CircleDot;
          return (
            <li
              key={s.id ?? i}
              className="flex items-start gap-2 py-1 text-micro border-l border-border pl-3 -ml-px last:border-l-transparent"
            >
              <Icon
                className={cn(
                  "size-3 mt-0.5 shrink-0",
                  STEP_TONE[s.status] || STEP_TONE.pending,
                  s.status === "running" && "animate-spin"
                )}
                aria-hidden
              />
              <span className="font-mono text-fg-subtle shrink-0">
                {phaseLabel(s.agent)}
              </span>
              {s.summary && (
                <span className="text-fg-muted truncate min-w-0">{s.summary}</span>
              )}
              {s.source && (
                <cite className="ml-auto shrink-0 not-italic text-info font-mono">
                  {s.source}
                </cite>
              )}
              {s.duration != null && (
                <span className="ml-auto shrink-0 text-fg-subtle tabular-nums">
                  {formatDuration(s.duration)}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </details>
  );
}

export default JobProgress;
