import React from "react";
import { useNavigate } from "react-router-dom";
import { useProjects } from "@/state/ProjectContext";
import { Boxes, Code2, Activity, ArrowLeft, Lock, Sparkles } from "lucide-react";

const STAGE_META = {
  Architecture: {
    n: 3,
    label: "Architecture",
    icon: Boxes,
    summary: "Decompose the data model into independently-deployable microservices.",
    deliverables: [
      "Service catalogue (one per domain boundary) — HLD + LLD",
      "Bounded contexts + service-to-service contracts (OpenAPI)",
      "Mermaid HLD diagram (service-to-service)",
      "Mermaid LLD per service (entities + endpoints)",
      "Sequence diagrams for the top 5 use cases",
      "RAG chat to refine the architecture",
    ],
    requires: "Stage 2 (Data Model) must be frozen — both OLTP + OLAP schemas locked.",
  },
  CodeGen: {
    n: 4,
    label: "Code Generation",
    icon: Code2,
    summary: "Generate the target codebase — backend services, frontend scaffolding, Dockerfiles, unit tests, and CI workflows.",
    deliverables: [
      "FastAPI service per bounded context with SQLAlchemy models, repositories, routes, Pydantic schemas",
      "React frontend scaffolding (per-entity CRUD + auth)",
      "Dockerfile + docker-compose.yml + Helm chart",
      "GitHub Actions CI workflow",
      "Pytest unit tests + Cypress E2E",
      "Single-click ZIP download or direct push to your GitHub repo",
    ],
    requires: "Stage 3 (Architecture) must be frozen — service catalogue and contracts locked.",
  },
  Living: {
    n: 5,
    label: "Living System",
    icon: Activity,
    summary: "Run the migrated application as a living artifact — continuous test, SRS-drift detection, and runtime observability.",
    deliverables: [
      "Selenium / Playwright regression suite generated from SRS use cases",
      "Live SRS diff: detect when running app drifts from frozen spec",
      "Per-service log + metric dashboards (Grafana/Prometheus)",
      "Alerting on FK-integrity or schema-shape regressions",
      "One-click re-generation when a frozen stage upstream changes",
    ],
    requires: "Stage 4 (Code Generation) must be frozen — codebase pushed to repo.",
  },
};

export default function StagePlaceholderPage({ stage }) {
  const meta = STAGE_META[stage];
  const navigate = useNavigate();
  const { active } = useProjects();
  if (!meta) return null;
  const Icon = meta.icon;
  const status = active?.stage_status?.[stage] || "locked";
  const isLocked = status === "locked";

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-[#F6F6FA]" data-testid={`stub-${stage.toLowerCase()}`}>
      <header className="bg-white border-b-2 border-[#FFE600] px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-[#747480]">Stage {meta.n} of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38]">{meta.label}</h1>
        </div>
        {active && (
          <div className="text-right">
            <div className="text-[10px] uppercase tracking-widest text-[#747480]">Project</div>
            <div className="text-sm font-semibold text-[#2E2E38]">{active.name}</div>
          </div>
        )}
      </header>

      <div className="flex-1 overflow-y-auto mos-scroll p-8">
        <div className="max-w-3xl mx-auto">
          <div className="bg-white border border-[#E6E6E6] rounded-sm p-8">
            <div className="flex items-start gap-4">
              <div className={`w-12 h-12 rounded-sm flex items-center justify-center ${isLocked ? "bg-[#F6F6FA] text-[#747480]" : "bg-[#FFE600] text-[#2E2E38]"}`}>
                {isLocked ? <Lock className="w-6 h-6" /> : <Icon className="w-6 h-6" />}
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-2">
                  <h2 className="font-display text-xl font-bold text-[#2E2E38] tracking-tight">{meta.label}</h2>
                  {isLocked ? (
                    <span className="text-[10px] uppercase tracking-wider bg-slate-200 text-slate-600 px-1.5 py-0.5 rounded-sm font-bold">Locked</span>
                  ) : (
                    <span className="text-[10px] uppercase tracking-wider bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded-sm font-bold">Ready</span>
                  )}
                  <span className="text-[10px] uppercase tracking-wider bg-[#FFFCE6] border border-[#FFE600] text-[#2E2E38] px-1.5 py-0.5 rounded-sm font-bold ml-auto" data-testid="coming-soon-badge">
                    <Sparkles className="w-2.5 h-2.5 inline mr-1" />
                    Coming Soon
                  </span>
                </div>
                <p className="text-sm text-[#747480] leading-relaxed">{meta.summary}</p>
              </div>
            </div>

            <div className="mt-6">
              <div className="text-[10px] uppercase tracking-wider text-[#747480] font-semibold mb-2">What this stage will produce</div>
              <ul className="space-y-1.5">
                {meta.deliverables.map((d, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm text-[#2E2E38]">
                    <span className="text-[#FFE600] font-bold mt-0.5">▸</span>
                    <span>{d}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="mt-6 p-3 bg-[#F6F6FA] rounded-sm border border-[#E6E6E6]">
              <div className="text-[10px] uppercase tracking-wider text-[#747480] font-semibold mb-1">Prerequisites</div>
              <div className="text-xs text-[#2E2E38]">{meta.requires}</div>
            </div>

            <div className="mt-6 flex items-center gap-2">
              <button
                type="button"
                onClick={() => navigate("/")}
                data-testid="back-to-discovery"
                className="text-xs px-3 py-1.5 border border-[#E6E6E6] rounded-sm hover:border-[#2E2E38] text-[#2E2E38] flex items-center gap-1"
              >
                <ArrowLeft className="w-3 h-3" /> Back to Discovery
              </button>
              <button
                type="button"
                onClick={() => navigate("/data-model")}
                className="text-xs px-3 py-1.5 border border-[#E6E6E6] rounded-sm hover:border-[#2E2E38] text-[#2E2E38]"
              >
                Open Data Model →
              </button>
            </div>
          </div>

          <div className="mt-3 text-[11px] text-[#747480] text-center">
            Stage {meta.n} backend implementation lands in the next prompt of the LAMA build series.
          </div>
        </div>
      </div>
    </div>
  );
}
