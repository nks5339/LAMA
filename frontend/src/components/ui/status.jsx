/**
 * One status vocabulary for the whole product.
 *
 * Before this, "passing" / "frozen" / "healthy" were styled differently on
 * every stage — Living alone carried 154 hex literals — and status was
 * frequently encoded by colour alone, which fails WCAG 1.4.1.
 *
 * Every chip here carries three channels: an icon, a text label, and a
 * colour. Drop any one and the meaning survives.
 */
import { cva } from "class-variance-authority";
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Info,
  Lock,
  Loader2,
  Circle,
  MinusCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";

const chip = cva(
  "inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-micro font-semibold uppercase tracking-wide whitespace-nowrap",
  {
    variants: {
      tone: {
        ok: "bg-ok-bg text-ok border-ok-edge",
        warn: "bg-warn-bg text-warn border-warn-edge",
        crit: "bg-crit-bg text-crit border-crit-edge",
        info: "bg-info-bg text-info border-info-edge",
        brand: "bg-brand-tint text-fg border-brand-edge",
        idle: "bg-surface-2 text-fg-subtle border-border",
        busy: "bg-info-bg text-info border-info-edge",
      },
      size: {
        sm: "text-micro px-1.5 py-0",
        md: "text-micro px-2 py-0.5",
        lg: "text-xs px-3 py-1",
      },
    },
    defaultVariants: { tone: "idle", size: "md" },
  }
);

const TONE_ICON = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  crit: XCircle,
  info: Info,
  brand: Circle,
  idle: MinusCircle,
  busy: Loader2,
};

/**
 * Generic chip. `children` is the visible label — never omit it, because
 * the label is one of the three channels.
 */
export function StatusChip({ tone = "idle", size, icon, children, className, ...rest }) {
  const Icon = icon ?? TONE_ICON[tone];
  return (
    <span className={cn(chip({ tone, size }), className)} {...rest}>
      {Icon && (
        <Icon
          className={cn("size-3 shrink-0", tone === "busy" && "animate-spin")}
          aria-hidden
        />
      )}
      {children}
    </span>
  );
}

/**
 * The five pipeline-stage states, mapped once so Sidebar, StageProgress and
 * every stage page agree. `locked` is deliberately NOT critical: a stage the
 * user has not reached yet is not an error, and painting it red made healthy
 * new projects look broken.
 */
export const STAGE_STATE = {
  frozen: { tone: "ok", Icon: CheckCircle2, label: "Frozen", sr: "complete and frozen" },
  active: { tone: "brand", Icon: Circle, label: "In progress", sr: "current stage" },
  available: { tone: "info", Icon: Circle, label: "Ready", sr: "ready to start" },
  skipped: { tone: "idle", Icon: MinusCircle, label: "Skipped", sr: "skipped" },
  locked: {
    tone: "idle",
    Icon: Lock,
    label: "Locked",
    sr: "locked until the previous stage is frozen",
  },
};

export function StageBadge({ status = "locked", className }) {
  const s = STAGE_STATE[status] ?? STAGE_STATE.locked;
  return (
    <StatusChip
      tone={s.tone}
      icon={s.Icon}
      className={className}
      data-testid={`stage-badge-${status}`}
    >
      {s.label}
    </StatusChip>
  );
}

/**
 * Accessible progress bar. The previous ProgressBar had no role and no
 * value attributes, so screen readers announced nothing at all.
 */
export function ProgressBar({
  value,
  max = 100,
  label,
  showPercentage = true,
  tone = "brand",
  className,
}) {
  const pct = Math.max(0, Math.min(100, Math.round((value / max) * 100)));
  const fill = {
    brand: "bg-brand",
    ok: "bg-ok",
    warn: "bg-warn",
    crit: "bg-crit",
    info: "bg-info",
  }[tone];

  return (
    <div className={cn("w-full", className)}>
      {label && (
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-xs font-medium text-fg">{label}</span>
          {showPercentage && (
            <span className="text-xs text-fg-muted tabular-nums">{pct}%</span>
          )}
        </div>
      )}
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label || "Progress"}
        className="w-full h-2 bg-surface-3 rounded-lg overflow-hidden"
      >
        <div
          className={cn("h-full rounded-lg transition-[width] duration-slow ease", fill)}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default StatusChip;
