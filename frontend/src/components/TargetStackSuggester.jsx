import { useCallback, useEffect, useState } from "react";
import { Sparkles, CheckCircle2, RefreshCw, Loader2, Layers } from "lucide-react";
import { toast } from "sonner";
import { getTargetStackSuggestions, selectTargetStack } from "@/lib/api";
import HelpIcon from "@/components/HelpIcon";
import CustomStackPicker from "@/components/CustomStackPicker";

/**
 * TargetStackSuggester
 * --------------------
 * Predicts top-N candidate modern stacks from the knowledge graph and lets
 * the user pick one. The chosen label is written to `project.target_tech`,
 * which is the single field read by the SRS, Architecture, CodeGen and
 * Living pipelines (target_stack guardrails block, prompt bundles, etc.).
 *
 * Renders nothing useful until KB build has produced a `detected_tech`
 * fingerprint — call after `kbReady`.
 */
export default function TargetStackSuggester({ projectId, kbReady, onApplied }) {
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [data, setData] = useState(null);
  const [chosenId, setChosenId] = useState(null);
  // iter-13.59 — "Others" custom-stack picker (à-la-carte assembler).
  const [customOpen, setCustomOpen] = useState(false);
  // iter-13.60 — last applied selection meta (used to enrich the synthetic
  // entry when the chosen label is not in the predicted top-N).
  const [lastSelection, setLastSelection] = useState(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const r = await getTargetStackSuggestions(projectId, 3);
      // iter-13.60 — if the user has applied a stack (recommended or custom
      // via the "Others" picker) that isn't in the freshly-predicted top-N,
      // surface it as a synthetic suggestion at the top so the list reflects
      // the active choice and the radio is pre-checked. Without this, custom
      // selections vanished from the UI as soon as the dialog closed.
      const suggestions = Array.isArray(r?.suggestions) ? [...r.suggestions] : [];
      const currentLabel = (r?.current_target_tech || "").trim();
      const matched = suggestions.find((s) => s.current || s.label === currentLabel);
      if (currentLabel && !matched) {
        const sel = lastSelection || r?.project?.target_stack_selection || {};
        suggestions.unshift({
          id: `custom:${currentLabel}`,
          label: currentLabel,
          kind: sel.kind || "custom",
          pattern: sel.pattern || "",
          rationale:
            sel.rationale ||
            "Custom target stack applied for this project (not in the auto-predicted top-3).",
          current: true,
        });
      }
      const enriched = { ...r, suggestions };
      setData(enriched);
      const current = suggestions.find((s) => s.current);
      if (current) setChosenId(current.id);
      else if (suggestions[0]) setChosenId(suggestions[0].id);
    } catch (e) {
      toast.error("Could not load stack suggestions", {
        description: e.response?.data?.detail || e.message,
      });
    } finally {
      setLoading(false);
    }
  }, [projectId, lastSelection]);

  useEffect(() => {
    if (kbReady) load();
  }, [kbReady, load]);

  const handleApply = async () => {
    if (!chosenId) return;
    const sel = (data?.suggestions || []).find((s) => s.id === chosenId);
    if (!sel) return;
    setApplying(true);
    try {
      const r = await selectTargetStack(projectId, {
        id: sel.id,
        label: sel.label,
        rationale: sel.rationale,
        kind: sel.kind,
        pattern: sel.pattern,
      });
      toast.success("Target stack applied", { description: r.selection?.selected_label });
      setLastSelection(r.selection || null);
      onApplied?.(r.project);
      // Reload to refresh `current` markers.
      load();
    } catch (e) {
      toast.error("Apply failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setApplying(false);
    }
  };

  if (!kbReady) {
    return (
      <div
        className="mos-panel p-6 border-2 border-[#FFE600] bg-gradient-to-br from-[#FFFEF5] to-white shadow-sm mt-4"
        data-testid="target-stack-card-locked"
      >
        <div className="flex items-center gap-2 mb-3">
          <div className="w-8 h-8 rounded-sm bg-[#FFE600] flex items-center justify-center shrink-0">
            <Sparkles className="w-4 h-4 text-[#2E2E38]" />
          </div>
          <div className="flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] uppercase tracking-widest text-[#747480] font-bold">
                Suggested Target Stack
              </span>
              <HelpIcon
                text="After you Build the Knowledge Base, LAMA inspects the detected legacy language, frameworks, database and KB size to predict the top-3 modern target stacks. Pick one and it drives SRS, Architecture and CodeGen generation."
                testId="help-target-stack"
              />
            </div>
            <div className="text-sm font-display font-semibold text-[#2E2E38] mt-0.5">
              Awaiting Knowledge Graph
            </div>
          </div>
        </div>
        <div className="text-xs text-[#2E2E38] leading-relaxed bg-white/60 rounded-sm p-3 border border-[#FFE600]/30">
          Run <span className="font-semibold bg-[#FFE600]/20 px-1 rounded">Build Knowledge Base</span> above
          and the top-3 modern target stacks (e.g.{" "}
          <span className="font-mono font-semibold">Laravel · FastAPI · Spring Boot</span>)
          will be predicted from the detected legacy language, frameworks and
          DB engine. The pick you apply here drives every SRS, Architecture
          and CodeGen prompt.
        </div>
      </div>
    );
  }

  return (
    <div
      className="mos-panel p-6 border-2 border-[#FFE600] bg-gradient-to-br from-[#FFFEF5] to-white shadow-sm mt-4"
      data-testid="target-stack-card"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-sm bg-[#FFE600] flex items-center justify-center shrink-0">
            <Sparkles className="w-4 h-4 text-[#2E2E38]" />
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] uppercase tracking-widest text-[#747480] font-bold">
                Suggested Target Stack
              </span>
              <HelpIcon
                text="LAMA predicts the top-3 modern target stacks from your knowledge graph (detected language, frameworks, database, KB size). The selected one is written to project.target_tech and drives every downstream prompt (SRS, Architecture, CodeGen, Living)."
                testId="help-target-stack"
              />
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          data-testid="refresh-target-stack"
          aria-label="Refresh stack suggestions"
          title="Refresh"
          className="text-[#747480] hover:text-[#2E2E38] disabled:opacity-40 p-0.5"
        >
          <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {data?.detected_tech?.summary && (
        <div className="text-[10px] text-[#747480] mb-2 font-mono truncate" data-testid="detected-summary">
          Detected: {data.detected_tech.summary}
        </div>
      )}

      {loading && (
        <div className="text-[10px] text-[#747480] flex items-center gap-1">
          <Loader2 className="w-3 h-3 animate-spin" /> Predicting…
        </div>
      )}

      {!loading && (data?.suggestions || []).length === 0 && (
        <div className="text-[10px] text-[#747480]">
          No automated suggestions for this project yet — use <b>Others</b> below to assemble a custom stack.
        </div>
      )}

      <div className="space-y-1.5">
        {(data?.suggestions || []).map((s, idx) => {
          const checked = chosenId === s.id;
          return (
            <label
              key={s.id}
              data-testid={`stack-option-${idx}`}
              className={`block border rounded-sm p-2 cursor-pointer transition-colors ${
                checked
                  ? "border-[#2E2E38] bg-[#FFFCE0]"
                  : "border-[#E6E6E6] bg-white hover:border-[#747480]"
              }`}
            >
              <div className="flex items-start gap-2">
                <input
                  type="radio"
                  name="target-stack"
                  className="mt-0.5"
                  checked={checked}
                  onChange={() => setChosenId(s.id)}
                  data-testid={`stack-radio-${idx}`}
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="text-[11px] font-semibold text-[#2E2E38]">{s.label}</span>
                    {s.current && (
                      <span
                        className="inline-flex items-center text-[9px] uppercase tracking-wider px-1 py-0.5 bg-emerald-100 text-emerald-800 border border-emerald-300 rounded-sm"
                        data-testid={`stack-current-${idx}`}
                      >
                        <CheckCircle2 className="w-2.5 h-2.5 mr-0.5" />
                        Current
                      </span>
                    )}
                    <span className="text-[9px] text-[#747480] uppercase tracking-wider">
                      {s.kind?.replace("-", " ")}
                    </span>
                  </div>
                  <div className="text-[10px] text-[#747480] mt-0.5 line-clamp-2">
                    {s.rationale}
                  </div>
                </div>
              </div>
            </label>
          );
        })}
      </div>

      {(data?.suggestions || []).length > 0 && (
        <div className="mt-2 flex items-center gap-1.5">
          <button
            type="button"
            onClick={handleApply}
            disabled={!chosenId || applying}
            data-testid="apply-target-stack-btn"
            className="flex-1 bg-[#2E2E38] text-white hover:bg-[#1A1A24] rounded-sm text-xs h-8 font-semibold disabled:opacity-40"
          >
            {applying ? (
              <span className="inline-flex items-center justify-center gap-1">
                <Loader2 className="w-3 h-3 animate-spin" /> Applying…
              </span>
            ) : (
              "Apply to Project"
            )}
          </button>
          <button
            type="button"
            onClick={() => setCustomOpen(true)}
            disabled={applying}
            data-testid="open-custom-stack-btn"
            title="Build your own target stack from a tech catalog"
            className="bg-white border border-[#2E2E38] text-[#2E2E38] hover:bg-[#FFFCE6] rounded-sm text-xs h-8 px-3 font-semibold disabled:opacity-40 inline-flex items-center gap-1"
          >
            <Layers className="w-3 h-3" />
            Others
          </button>
        </div>
      )}

      {/* Fallback Others button — always available when the suggester has
          no recommendations, so the user is never blocked from picking a
          target stack. */}
      {!loading && (data?.suggestions || []).length === 0 && (
        <button
          type="button"
          onClick={() => setCustomOpen(true)}
          data-testid="open-custom-stack-btn-fallback"
          className="w-full mt-2 bg-white border border-[#2E2E38] text-[#2E2E38] hover:bg-[#FFFCE6] rounded-sm text-xs h-8 font-semibold inline-flex items-center justify-center gap-1"
        >
          <Layers className="w-3 h-3" />
          Build your own (Others)
        </button>
      )}

      <CustomStackPicker
        open={customOpen}
        onOpenChange={setCustomOpen}
        projectId={projectId}
        onApplied={(proj, selection) => {
          if (selection) setLastSelection(selection);
          else if (proj?.target_stack_selection) setLastSelection(proj.target_stack_selection);
          onApplied?.(proj);
          // Refresh suggestions so the "Current" badge moves onto the new
          // pick. For a fully custom stack none of the top-3 will show it —
          // the synthetic entry inserted in load() ensures the chosen label
          // still appears at the top with its radio pre-checked.
          load();
        }}
      />
    </div>
  );
}

