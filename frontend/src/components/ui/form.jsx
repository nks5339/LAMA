/**
 * Validated form primitives — React Hook Form + Zod.
 *
 * The audit counted 96 raw form controls across the app with no schema and
 * no inline validation: Console alone has 33, and a mistyped provider key
 * there produces the documented "cascade of 401s" footgun three stages
 * downstream rather than an error at the field.
 *
 * <Field> wires the four things that are easy to forget and invisible when
 * missing: a real <label htmlFor>, aria-invalid, aria-describedby pointing
 * at both the hint and the error, and role="alert" on the error so it is
 * announced the moment it appears.
 */
import { useId } from "react";
import { cn } from "@/lib/utils";

export function Field({
  label,
  htmlFor,
  hint,
  error,
  required = false,
  className,
  children,
}) {
  const auto = useId();
  const id = htmlFor || auto;
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-error` : undefined;

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-xs font-medium text-fg">
        {label}
        {required && (
          <span className="text-crit ml-0.5" aria-hidden>
            *
          </span>
        )}
        {required && <span className="sr-only"> (required)</span>}
      </label>

      {typeof children === "function"
        ? children({
            id,
            "aria-invalid": error ? true : undefined,
            "aria-describedby":
              [hintId, errId].filter(Boolean).join(" ") || undefined,
            "aria-required": required || undefined,
          })
        : children}

      {hint && !error && (
        <p id={hintId} className="text-micro text-fg-subtle">
          {hint}
        </p>
      )}
      {error && (
        <p id={errId} role="alert" className="text-micro text-crit font-medium">
          {error}
        </p>
      )}
    </div>
  );
}

/** Text input styled from tokens, with the error state wired to the border. */
export function TextInput({ className, invalid, ...props }) {
  return (
    <input
      className={cn(
        "w-full h-9 px-3 rounded text-sm",
        "bg-surface text-fg placeholder:text-fg-subtle",
        "border transition-[border-color,box-shadow] duration-fast ease",
        invalid ? "border-crit" : "border-border-strong hover:border-fg-subtle",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-surface",
        "disabled:bg-surface-2 disabled:text-fg-subtle disabled:cursor-not-allowed",
        className
      )}
      {...props}
    />
  );
}

export function SelectInput({ className, invalid, children, ...props }) {
  return (
    <select
      className={cn(
        "w-full h-9 px-3 rounded text-sm",
        "bg-surface text-fg",
        "border transition-[border-color] duration-fast ease",
        invalid ? "border-crit" : "border-border-strong hover:border-fg-subtle",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-surface",
        "disabled:bg-surface-2 disabled:text-fg-subtle disabled:cursor-not-allowed",
        className
      )}
      {...props}
    >
      {children}
    </select>
  );
}

/**
 * Summary of every error in a form, rendered above the submit.
 *
 * On a form the size of Console's, a single inline error can be scrolled
 * out of view — the user presses Save, nothing appears to happen, and the
 * reason is 400px up the page. Each entry focuses its field.
 */
export function FormErrorSummary({ errors, className }) {
  const entries = Object.entries(errors || {}).filter(([, v]) => v?.message);
  if (!entries.length) return null;

  return (
    <div
      role="alert"
      className={cn(
        "rounded border border-crit-edge bg-crit-bg px-3 py-2",
        className
      )}
    >
      <p className="text-xs font-semibold text-crit">
        {entries.length} field{entries.length === 1 ? "" : "s"} need
        {entries.length === 1 ? "s" : ""} attention
      </p>
      <ul className="mt-1 flex flex-col gap-0.5">
        {entries.map(([name, err]) => (
          <li key={name}>
            <button
              type="button"
              className="text-micro text-fg-muted hover:text-fg underline underline-offset-2 text-left"
              onClick={() =>
                document
                  .querySelector(`[name="${name}"]`)
                  ?.focus({ preventScroll: false })
              }
            >
              {err.message}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
