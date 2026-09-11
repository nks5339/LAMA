// iter-16.x — Project-type-aware labels for token-usage breakdowns.
//
// The legacy_migration pipeline tags every LLM call with a pipeline
// `stage` (Discovery / DataModel / Architecture / CodeGen / Living), so
// the existing MiniConsole + Settings→Statistics charts group tokens by
// that field. Gap Analyzer and Code Transformer project types don't have
// pipeline stages — every one of their calls is tagged `stage: "Tools"`
// (see backend/seed.py agent_configs), so grouping by stage collapses
// all of their usage into one undifferentiated bucket (or, worse, shows
// five permanently-zero legacy stage rows). Those project types DO tag
// calls with a granular `agent_key` (e.g. "tools.transformer.coder"),
// which the backend already aggregates as `by_agent` — this module turns
// those raw keys into human-friendly labels + stable colors so the UI
// can render a meaningful breakdown for every project type.

export const PROJECT_TYPE_META = {
  legacy_migration: { label: "Modernization", short: "Migration", color: "#7C3AED" },
  gap_analysis:     { label: "Gap Analyzer",  short: "Gap Analysis", color: "#0EA5E9" },
  tech_transformer: { label: "Code Transformer", short: "Transformer", color: "#F97316" },
};

export function projectTypeMeta(projectType) {
  return PROJECT_TYPE_META[projectType || "legacy_migration"] || PROJECT_TYPE_META.legacy_migration;
}

// agent_key → { label, color }. Keys are matched by exact string first,
// then by longest dotted-prefix (so "tools.transformer.coder" matches
// even if a new suffix agent is added later without updating this map).
const AGENT_LABELS = {
  "tools.transformer.super_agent":      { label: "Super Agent",         color: "#8B5CF6" },
  "tools.transformer.context_manager":  { label: "Context Manager",     color: "#0EA5E9" },
  "tools.transformer.planner":          { label: "Planner",             color: "#F59E0B" },
  "tools.transformer.coder":            { label: "Coder",               color: "#10B981" },
  "tools.transformer.verifier":         { label: "Verifier",            color: "#EF4444" },
  "tools.transformer.tester":           { label: "Tester",               color: "#EC4899" },
  "tools.transformer.pattern":          { label: "Pattern Applier",     color: "#06B6D4" },
  "tools.transformer.validator":        { label: "Validator",           color: "#84CC16" },
  "tools.transformer.devops_expert":    { label: "DevOps Expert",       color: "#6366F1" },
  "tools.transformer.chat":             { label: "Chat",                 color: "#94A3B8" },
  "tools.transformer":                  { label: "Transformer (setup)", color: "#A855F7" },
  "tools.gap_analyzer":                 { label: "Gap Analyzer",        color: "#0EA5E9" },
  "tools.gap_analyzer.doc_parser":      { label: "Document Parser",     color: "#22D3EE" },
  "tools.gap_verifier":                 { label: "Gap Verifier",        color: "#3B82F6" },
};

const FALLBACK_COLORS = ["#7C3AED", "#0EA5E9", "#10B981", "#F97316", "#EC4899", "#F59E0B", "#6366F1", "#84CC16"];

function prettify(key) {
  const last = (key || "").split(".").filter(Boolean).pop() || key || "(unknown)";
  return last
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

let _fallbackIdx = 0;
const _fallbackAssigned = new Map();

/** Human-friendly label for a raw agent_key (e.g. "tools.transformer.coder" → "Coder"). */
export function labelForAgentKey(key) {
  if (!key) return "(untagged)";
  if (AGENT_LABELS[key]) return AGENT_LABELS[key].label;
  return prettify(key);
}

/** Stable color for a raw agent_key, falling back to a deterministic
 *  cycling palette for keys we don't explicitly know about. */
export function colorForAgentKey(key) {
  if (!key) return "#94A3B8";
  if (AGENT_LABELS[key]) return AGENT_LABELS[key].color;
  if (_fallbackAssigned.has(key)) return _fallbackAssigned.get(key);
  const c = FALLBACK_COLORS[_fallbackIdx % FALLBACK_COLORS.length];
  _fallbackIdx += 1;
  _fallbackAssigned.set(key, c);
  return c;
}

/** True when a project type doesn't use the legacy 5-stage pipeline, so
 *  usage UIs should group by agent instead of by stage. */
export function isAgentDrivenProjectType(projectType) {
  return projectType === "gap_analysis" || projectType === "tech_transformer";
}
