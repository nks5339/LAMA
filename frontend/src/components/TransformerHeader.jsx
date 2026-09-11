import React from "react";
import { Wand2, MoreVertical } from "lucide-react";

/**
 * iter-15.54 — 64px header for the Code Transformer page.
 *
 * Layout: [app icon] [title + subtitle] ....... [metric strip] [status pill]
 *         [actions segmented control] [kebab overflow]
 *
 * Pure layout shell — the parent supplies the interactive bits (status pill,
 * primary/secondary actions, kebab dropdown content) as rendered nodes so all
 * `data-testid` attributes and handlers stay co-located with their state.
 *
 * Props:
 *   title, subtitle   strings
 *   metrics           Array<{ label, value }>  (compact vertical stat strip)
 *   statusPill        node   (status chip)
 *   actions           node   (Start / Pause / Resume / Stop controls)
 *   kebabOpen         bool
 *   onKebabToggle     () => void
 *   kebabContent      node   (dropdown body, rendered when kebabOpen)
 */
export default function TransformerHeader({
  title,
  subtitle = "KB-driven cross-stack transformation",
  metrics = [],
  statusPill = null,
  actions = null,
  kebabOpen = false,
  onKebabToggle,
  kebabContent = null,
}) {
  return (
    <header className="h-16 px-6 flex items-center justify-between border-b border-slate-200 bg-white flex-shrink-0">
      {/* Left: icon + title */}
      <div className="flex items-center gap-3 min-w-0">
        <div className="w-8 h-8 rounded-lg bg-violet-600 flex items-center justify-center flex-shrink-0">
          <Wand2 size={16} className="text-white" />
        </div>
        <div className="min-w-0">
          <h1 className="text-base font-semibold text-slate-800 truncate leading-tight">
            {title || "Code Transformer"}
          </h1>
          <p className="text-xs text-slate-500 truncate leading-tight">{subtitle}</p>
        </div>
      </div>

      {/* Right: metrics + status + actions + kebab */}
      <div className="flex items-center gap-4 flex-shrink-0">
        {metrics.length > 0 && (
          <div className="hidden md:flex items-center gap-4">
            {metrics.map((m) => (
              <div key={m.label} className="flex flex-col items-end" title={m.title || undefined}>
                <span className="text-[11px] uppercase tracking-wide text-slate-500 leading-none">
                  {m.label}
                </span>
                <span className="text-sm font-semibold text-slate-800 tabular-nums leading-tight mt-0.5">
                  {m.value}
                </span>
              </div>
            ))}
          </div>
        )}

        {statusPill}

        {actions && <div className="flex items-center gap-2">{actions}</div>}

        <div className="relative">
          <button
            onClick={onKebabToggle}
            className="w-9 h-9 rounded-lg hover:bg-slate-100 flex items-center justify-center text-slate-500 hover:text-slate-800 transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500"
            data-testid="transformer-kebab-btn"
            title="More actions"
            aria-label="More actions"
            aria-haspopup="menu"
            aria-expanded={kebabOpen}
          >
            <MoreVertical size={18} />
          </button>
          {kebabOpen && (
            <>
              <div className="fixed inset-0 z-[60]" onClick={onKebabToggle} />
              <div
                role="menu"
                className="absolute right-0 top-11 z-[61] w-56 bg-white border border-slate-200 rounded-xl shadow-xl py-1"
              >
                {kebabContent}
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
