import { Check } from "lucide-react";

/**
 * iter-15.54 — Input-flow wizard rail for the Code Transformer page.
 *
 * Vertical on wide viewports (lg+), horizontal scroll-strip on narrow ones.
 * Purely presentational: the parent computes each step's `status`
 * ("pending" | "current" | "done") and handles navigation via `onStepClick`.
 *
 * Props:
 *   steps        Array<{ key, label, sublabel, status }>
 *   currentStep  key of the active step (for the violet ring)
 *   onStepClick  (key) => void   — invoked for done/current steps
 */
export default function TransformerStepper({ steps = [], currentStep, onStepClick }) {
  return (
    <nav
      aria-label="Transformation setup progress"
      className="flex lg:flex-col gap-1 lg:gap-0 overflow-x-auto lg:overflow-visible"
    >
      {steps.map((step, idx) => {
        const status = step.status || "pending";
        const isDone = status === "done";
        const isCurrent = status === "current" || step.key === currentStep;
        const clickable = isDone || isCurrent;
        const isLast = idx === steps.length - 1;

        const bubble = isDone
          ? "bg-emerald-600 text-white border-emerald-600"
          : isCurrent
            ? "bg-violet-600 text-white border-violet-600 ring-2 ring-violet-200"
            : "bg-surface-2 text-fg-subtle border-border";

        return (
          <div
            key={step.key}
            className="relative flex lg:flex-row flex-col items-center lg:items-stretch lg:gap-3 min-w-[128px] lg:min-w-0"
          >
            {/* Rail column: bubble + connector */}
            <div className="flex lg:flex-col items-center">
              <button
                type="button"
                onClick={() => clickable && onStepClick?.(step.key)}
                disabled={!clickable}
                aria-current={isCurrent ? "step" : undefined}
                aria-label={`Step ${idx + 1}: ${step.label}${isDone ? " (done)" : isCurrent ? " (current)" : ""}`}
                className={`flex items-center justify-center w-6 h-6 rounded-full border text-xs font-semibold shrink-0 transition-all duration-200 ${bubble} ${
                  clickable
                    ? "cursor-pointer hover:brightness-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500"
                    : "cursor-default"
                }`}
              >
                {isDone ? <Check size={13} className="motion-safe:animate-in" /> : idx + 1}
              </button>
              {!isLast && (
                <span
                  className={`hidden lg:block w-px flex-1 my-1 ${isDone ? "bg-emerald-300" : "bg-surface-3"}`}
                  style={{ minHeight: 22 }}
                  aria-hidden="true"
                />
              )}
            </div>

            {/* Label column */}
            <div className={`text-center lg:text-left mt-1 lg:mt-0 ${isLast ? "" : "lg:pb-4"}`}>
              <button
                type="button"
                onClick={() => clickable && onStepClick?.(step.key)}
                disabled={!clickable}
                className={`text-left ${clickable ? "cursor-pointer" : "cursor-default"} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 rounded`}
              >
                <div
                  className={`text-xs font-semibold leading-tight ${
                    isCurrent ? "text-violet-700" : isDone ? "text-fg" : "text-fg-subtle"
                  }`}
                >
                  {step.label}
                </div>
                {step.sublabel && (
                  <div className="hidden lg:block text-xs text-fg-subtle mt-0.5">{step.sublabel}</div>
                )}
              </button>
            </div>

            {/* Horizontal connector (narrow only) */}
            {!isLast && (
              <span
                className={`lg:hidden absolute top-3 left-[calc(50%+16px)] right-0 h-px ${isDone ? "bg-emerald-300" : "bg-surface-3"}`}
                aria-hidden="true"
              />
            )}
          </div>
        );
      })}
    </nav>
  );
}
