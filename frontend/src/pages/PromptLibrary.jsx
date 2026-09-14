import { useEffect, useState } from "react";
import { listPrompts, updatePrompt, listProjectPrompts, updateProjectPrompt } from "@/lib/api";
import { useProjects } from "@/state/ProjectContext";
import HelpIcon from "@/components/HelpIcon";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

function PromptCard({ p, scope, onSave, onReset }) {
  const [template, setTemplate] = useState(p.template);
  const [dirty, setDirty] = useState(false);

  useEffect(() => { setTemplate(p.template); setDirty(false); }, [p.template]);

  return (
    <div className="mos-panel p-5" data-testid={`prompt-card-${p.key}`}>
      <div className="flex items-start justify-between mb-2">
        <div>
          <div className="font-display font-bold text-sm text-[#2E2E38]">{p.key}</div>
          <div className="text-[11px] text-slate-500 mt-0.5">{p.description || "—"}</div>
        </div>
        <div className="text-right">
          <div className="text-[10px] uppercase tracking-wider text-slate-500">Stage</div>
          <div className="text-xs font-semibold">{p.stage}</div>
          <div className="text-[10px] text-slate-400 mt-1">v{p.version}</div>
        </div>
      </div>
      <textarea
        value={template}
        rows={8}
        onChange={(e) => { setTemplate(e.target.value); setDirty(true); }}
        className="w-full font-mono text-xs bg-slate-50 border border-[#E6E6E6] rounded-sm p-2 focus:border-[#2E2E38] focus:ring-1 focus:ring-[#2E2E38] outline-none"
        data-testid={`prompt-textarea-${p.key}`}
      />
      <div className="mt-2 flex items-center justify-between">
        <div className="text-[10px] text-slate-400">{scope === "project" ? "Project-specific override" : "Global"}</div>
        <div className="flex gap-2">
          {scope === "project" && onReset && (
            <Button size="sm" variant="outline" onClick={onReset} className="text-xs h-8 rounded-sm" data-testid={`prompt-reset-${p.key}`}>
              Remove override
            </Button>
          )}
          <Button
            size="sm"
            disabled={!dirty}
            onClick={() => onSave(template)}
            className="bg-[#2E2E38] text-white hover:bg-[#1A1A24] text-xs h-8 rounded-sm"
            data-testid={`prompt-save-${p.key}`}
          >
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function PromptLibraryPage() {
  const { active } = useProjects();
  const [global, setGlobal] = useState([]);
  const [proj, setProj] = useState([]);
  const [tab, setTab] = useState("global");

  const refresh = async () => {
    const g = await listPrompts();
    setGlobal(g);
    if (active?.id) {
      const p = await listProjectPrompts(active.id);
      setProj(p);
    }
  };

  useEffect(() => { refresh(); }, [active?.id]);

  const saveGlobal = async (key, template) => {
    try {
      await updatePrompt(key, { template });
      toast.success("Prompt updated");
      refresh();
    } catch (e) { toast.error("Save failed"); }
  };

  const saveProject = async (key, template, description) => {
    if (!active?.id) return;
    try {
      await updateProjectPrompt(active.id, key, { template, description });
      toast.success("Project override saved");
      refresh();
    } catch (e) { toast.error("Save failed"); }
  };

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      <header className="bg-white border-b border-[#E6E6E6] px-6 py-3 flex items-center justify-between">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-slate-500">Library</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38] flex items-center">
            Prompt Library
            <HelpIcon text="Versioned system prompts driving each stage. Global prompts are the defaults; project prompts override per-project." testId="help-prompt-library" />
          </h1>
        </div>
        <div className="flex border-b border-[#E6E6E6] -mb-3" data-testid="prompt-tabs">
          <button
            data-testid="tab-global"
            onClick={() => setTab("global")}
            className={`px-4 pb-2 text-sm font-semibold ${tab === "global" ? "border-b-2 border-[#2E2E38] text-[#2E2E38]" : "text-slate-500 hover:text-slate-700"}`}
          >
            Global ({global.length})
          </button>
          <button
            data-testid="tab-project"
            onClick={() => setTab("project")}
            className={`px-4 pb-2 text-sm font-semibold ${tab === "project" ? "border-b-2 border-[#2E2E38] text-[#2E2E38]" : "text-slate-500 hover:text-slate-700"}`}
          >
            Project ({proj.length})
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-[#F6F6FA]">
        <div className="max-w-5xl mx-auto grid grid-cols-1 md:grid-cols-2 gap-4">
          {tab === "global" && global.map((p) => (
            <PromptCard key={p.key} p={p} scope="global" onSave={(t) => saveGlobal(p.key, t)} />
          ))}
          {tab === "project" && (
            <>
              {!active && <div className="col-span-2 text-slate-500 text-sm">Select a project to manage overrides.</div>}
              {active && global.map((g) => {
                const override = proj.find((x) => x.key === g.key);
                const merged = override ? { ...g, template: override.template, version: override.version } : g;
                return (
                  <PromptCard
                    key={g.key}
                    p={merged}
                    scope="project"
                    onSave={(t) => saveProject(g.key, t, g.description)}
                    onReset={override ? async () => { /* simple delete via direct fetch */
                      await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/prompts/project/${active.id}/${g.key}`, { method: "DELETE" });
                      toast.success("Override removed");
                      refresh();
                    } : null}
                  />
                );
              })}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
