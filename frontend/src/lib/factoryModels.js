// iter-15.39 — Shared Factory/Droid model catalogue.
//
// Extracted from Console.jsx (Factory Orchestrator tab) so any other page
// that needs to offer Factory's selectable model list — e.g. the Code
// Transformer's per-agent "Prompt & Model" config panel — can reuse the
// exact same canonical set instead of drifting out of sync with an
// independently-maintained copy.
//
// Values are the literal droid model ids `route_via_factory_orchestrator`
// resolves through `_canonicalise_factory_model_id` (backend/factory_orchestrator.py).
// Keep this list in sync with any future droid model releases; unknown ids
// still pass through untouched server-side, so this list is a curated
// "known-good" set for the dropdown UI, not a hard allowlist.
export const FACTORY_MODEL_OPTIONS = [
  { value: "auto",                        label: "auto (droid picks)" },
  { value: "claude-opus-4-8",             label: "claude-opus-4-8 (flagship)" },
  { value: "claude-opus-4-7",             label: "claude-opus-4-7" },
  { value: "claude-opus-4-6",             label: "claude-opus-4-6" },
  { value: "claude-sonnet-4-6",           label: "claude-sonnet-4-6 (balanced)" },
  { value: "claude-sonnet-4-5-20250929",  label: "claude-sonnet-4-5" },
  { value: "claude-haiku-4-5-20251001",   label: "claude-haiku-4-5 (fast/cheap)" },
  { value: "gpt-5.5",                     label: "gpt-5.5" },
  { value: "gpt-5.5-pro",                 label: "gpt-5.5-pro" },
  { value: "gpt-5.4-mini",                label: "gpt-5.4-mini" },
  { value: "gpt-5.3-codex",               label: "gpt-5.3-codex" },
  { value: "gemini-3.1-pro-preview",      label: "gemini-3.1-pro" },
  { value: "gemini-3.5-flash",            label: "gemini-3.5-flash" },
];
