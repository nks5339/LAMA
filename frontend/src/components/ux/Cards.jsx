import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { StatusChip, ProgressBar } from "@/components/ui/status";

export { ProgressBar };

/**
 * MetricCard — a single figure.
 *
 * Changes: the old version put `onClick` on a bare <div>, so a clickable
 * tile had no keyboard path and no role. It also took a `color` prop from a
 * five-way rainbow (blue / green / yellow / purple / gray) that carried no
 * meaning — four tiles on Discovery were four different colours for four
 * equally neutral counts. Colour is now reserved for actual state via
 * `tone`, and the default is neutral.
 */
export function MetricCard({
  label,
  value,
  trend,
  icon: Icon,
  tone = "neutral",
  hint,
  className,
  onClick,
  loading = false,
  ...rest
}) {
  const toneRing = {
    neutral: "",
    ok: "border-l-2 border-l-ok",
    warn: "border-l-2 border-l-warn",
    crit: "border-l-2 border-l-crit",
    brand: "border-l-2 border-l-brand",
  }[tone];

  const Comp = onClick ? "button" : "div";

  return (
    <Comp
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "rounded border border-border bg-surface p-3 text-left w-full",
        "transition-[border-color,box-shadow] duration-fast ease",
        toneRing,
        onClick &&
          "hover:border-border-strong hover:shadow-raised cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        className
      )}
      {...rest}
    >
      <div className="flex items-start justify-between gap-2 mb-1">
        <p className="text-micro font-medium uppercase tracking-wide text-fg-subtle truncate">
          {label}
        </p>
        {Icon && <Icon className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />}
      </div>

      <div className="flex items-baseline gap-2">
        {loading ? (
          <span className="skeleton h-6 w-12 rounded-sm" aria-hidden />
        ) : (
          <p className="text-xl font-display font-bold text-fg tabular-nums">
            {value}
          </p>
        )}
        {trend != null && (
          <span
            className={cn(
              "text-micro font-medium tabular-nums",
              trend > 0 ? "text-ok" : trend < 0 ? "text-crit" : "text-fg-subtle"
            )}
          >
            {trend > 0 ? "+" : ""}
            {trend}%
          </span>
        )}
      </div>

      {hint && <p className="text-micro text-fg-subtle mt-1 truncate">{hint}</p>}
    </Comp>
  );
}

/**
 * StepCard — a step in a guided workflow.
 *
 * Changes: `pending` used to be `opacity-60`, which dropped the description
 * to roughly 2.1:1. More importantly, a disabled step used to be a card
 * that looked pressable and silently did nothing when clicked — the most
 * common dead-click in the product. A blocked step now states its own
 * precondition, both visibly and as the accessible description.
 */
export function StepCard({
  stepNumber,
  title,
  description,
  status = "pending", // pending | active | complete | error
  icon: Icon,
  onClick,
  disabled = false,
  disabledReason,
  className,
  ...rest
}) {
  const shell = {
    pending: "border-border bg-surface",
    active: "border-brand bg-surface shadow-raised",
    complete: "border-ok-edge bg-ok-bg",
    error: "border-crit-edge bg-crit-bg",
  }[status];

  const badge = {
    pending: "bg-surface-2 text-fg-subtle border border-border",
    active: "bg-brand text-brand-fg",
    complete: "bg-ok text-ok-fg",
    error: "bg-crit text-crit-fg",
  }[status];

  const hintId = disabledReason ? `step-${stepNumber}-hint` : undefined;
  const interactive = Boolean(onClick);
  const Comp = interactive ? "button" : "div";

  return (
    <Comp
      type={interactive ? "button" : undefined}
      onClick={interactive && !disabled ? onClick : undefined}
      aria-disabled={disabled || undefined}
      aria-describedby={hintId}
      className={cn(
        "border rounded w-full text-left transition-[border-color,box-shadow] duration-fast ease",
        shell,
        interactive &&
          !disabled &&
          "cursor-pointer hover:shadow-overlay hover:border-border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-bg",
        disabled && "cursor-not-allowed",
        className
      )}
      {...rest}
    >
      <div className="px-3 py-2.5 flex items-center gap-3">
        <div
          className={cn(
            "size-8 rounded-lg grid place-items-center shrink-0 font-bold text-sm",
            badge
          )}
        >
          {Icon ? <Icon className="size-4" aria-hidden /> : stepNumber}
        </div>

        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-sm text-fg leading-tight truncate">
            {title}
          </h3>
          {description && (
            <p className="text-xs text-fg-muted leading-tight mt-0.5 truncate">
              {description}
            </p>
          )}
          {disabled && disabledReason && (
            <p id={hintId} className="text-micro text-warn mt-1">
              {disabledReason}
            </p>
          )}
        </div>
      </div>
    </Comp>
  );
}

/**
 * StatusBadge — kept for the existing call sites; delegates to the one
 * shared StatusChip vocabulary so "success" looks the same everywhere.
 */
export function StatusBadge({ status, label, size = "md" }) {
  const tone =
    { success: "ok", warning: "warn", error: "crit", info: "info", active: "brand", neutral: "idle" }[
      status
    ] || "idle";
  return (
    <StatusChip tone={tone} size={size}>
      {label}
    </StatusChip>
  );
}

/**
 * EmptyState — now requires somewhere to go.
 *
 * The Discovery "No Project Selected" state passed no action, dead-ending
 * first-run users on the screen meant to onboard them. `action` is still
 * optional, but `actionLabel` + `onAction` gives callers a one-liner.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  actionLabel,
  onAction,
  className,
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center py-12 px-4 text-center",
        className
      )}
      data-testid="empty-state"
    >
      {Icon && (
        <div className="size-14 rounded-lg bg-surface-2 border border-border grid place-items-center mb-4">
          <Icon className="size-6 text-fg-subtle" aria-hidden />
        </div>
      )}
      <h3 className="text-lg font-display font-bold text-fg mb-1">{title}</h3>
      {description && (
        <p className="text-sm text-fg-muted max-w-md mb-4">{description}</p>
      )}
      {action ??
        (actionLabel && onAction ? (
          <Button variant="primary" size="sm" onClick={onAction}>
            {actionLabel}
          </Button>
        ) : null)}
    </div>
  );
}
