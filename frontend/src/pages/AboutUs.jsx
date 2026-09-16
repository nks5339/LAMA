import { useRef } from "react";
import {
  Info,
  Download,
  BookOpen,
  Database,
  Boxes,
  Code2,
  Activity,
  CheckCircle2,
  ArrowRight,
  Sparkles,
  Layers,
  GitBranch,
  Zap,
  Shield,
  Target,
  Users,
  Rocket,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import HelpIcon from "@/components/HelpIcon";

// ──────────────────────────────────────────────────────────────────────────────
// iter-14.20 — About Us page with comprehensive LAMA write-up + PDF download.
// ──────────────────────────────────────────────────────────────────────────────

const STAGES = [
  {
    key: "Discovery",
    icon: BookOpen,
    title: "1. Discovery & SRS",
    description:
      "Upload legacy codebase (PHP, JSP, .NET, Python, etc.). LAMA auto-detects the tech stack, builds a Knowledge Base (KB), extracts business ontology, and generates a comprehensive IEEE-830 compliant Software Requirements Specification (SRS) with 12 sections including ER diagrams.",
    outputs: ["Knowledge Base (YAML/TOON)", "IEEE-830 SRS (12 sections)", "Business Ontology", "ER Diagrams"],
  },
  {
    key: "DataModel",
    icon: Database,
    title: "2. Data Model",
    description:
      "Design the target PostgreSQL schema. LAMA generates OLTP DDL for transactional workloads, OLAP DDL for analytics (star schema), a Bus Matrix for dimensional modeling, and three migration scripts to move data from the legacy database.",
    outputs: ["OLTP DDL (PostgreSQL)", "OLAP DDL (Star Schema)", "Bus Matrix", "3 Migration Scripts"],
  },
  {
    key: "Architecture",
    icon: Boxes,
    title: "3. Architecture",
    description:
      "Decompose the monolith into cloud-native microservices. LAMA generates a Service Map, High-Level Design (HLD), Low-Level Design (LLD), OpenAPI contracts for each service, and Mermaid sequence diagrams for key flows.",
    outputs: ["Service Map", "HLD Document", "LLD Document", "API Contracts (OpenAPI)", "Sequence Diagrams"],
  },
  {
    key: "CodeGen",
    icon: Code2,
    title: "4. Code Generation",
    description:
      "Generate production-ready target code. LAMA produces FastAPI/Python backend services, React frontend, Dockerfiles, docker-compose.yml, and unit tests. All artifacts can be downloaded as a ZIP or pushed directly to GitHub.",
    outputs: ["FastAPI Backend", "React Frontend", "Dockerfiles", "Unit Tests", "GitHub Push"],
  },
  {
    key: "Living",
    icon: Activity,
    title: "5. Living System",
    description:
      "Post-migration continuous alignment. LAMA generates Selenium end-to-end tests, monitors SRS drift (detects when code diverges from requirements), and provides runtime observability dashboards.",
    outputs: ["Selenium E2E Tests", "SRS Drift Detection", "Observability Dashboards"],
  },
];

const FEATURES = [
  {
    icon: Sparkles,
    title: "AI-Powered Analysis",
    description:
      "Multi-provider LLM fabric routes prompts through OpenRouter, Anthropic, OpenAI, Groq, Ollama, or Gemini based on complexity tiers (low/medium/high) for optimal cost-quality trade-offs.",
  },
  {
    icon: Layers,
    title: "Knowledge Graph",
    description:
      "Property-graph KB with OWL-derived entities (classes, methods, tables, columns, routes, roles) serialized as TOON for token-efficient LLM context injection.",
  },
  {
    icon: GitBranch,
    title: "Deterministic Pipeline",
    description:
      "Strict stage gates ensure artifacts freeze before advancing. Each stage writes StageContext to MongoDB; downstream stages require upstream context via typed freeze confirmations.",
  },
  {
    icon: Zap,
    title: "Streaming & Background Jobs",
    description:
      "SSE streaming for real-time SRS generation; background tasks with 2-second polling for long Architecture jobs (K8s 60s ingress timeout safe).",
  },
  {
    icon: Shield,
    title: "Audit Trail",
    description:
      "Every state change (freeze, reset, edit) is logged to the audit_log collection with timestamps, user, and payload diffs for compliance and rollback.",
  },
  {
    icon: Target,
    title: "Business Ontology",
    description:
      "Deterministic clustering + LLM enrichment extracts high-level business domains from legacy code, enabling accurate SRS section generation and microservice boundaries.",
  },
];

export default function AboutUsPage() {
  const contentRef = useRef(null);

  // PDF download via browser print API (no external dependencies)
  const handleDownloadPDF = () => {
    const printContents = contentRef.current?.innerHTML;
    if (!printContents) return;

    const printWindow = window.open("", "_blank", "width=900,height=700");
    if (!printWindow) {
      alert("Please allow pop-ups to download PDF");
      return;
    }

    printWindow.document.write(`
      <!DOCTYPE html>
      <html>
      <head>
        <title>About LAMA - Legacy Application Modernisation AI Studio</title>
        <style>
          @page { margin: 20mm; size: A4; }
          body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            color: #2E2E38;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
          }
          h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; color: #2E2E38; }
          h2 { font-size: 20px; font-weight: 700; margin-top: 32px; margin-bottom: 12px; color: #2E2E38; border-bottom: 2px solid #FFE600; padding-bottom: 8px; }
          h3 { font-size: 16px; font-weight: 600; margin-top: 20px; margin-bottom: 8px; color: #2E2E38; }
          p { margin-bottom: 12px; font-size: 14px; }
          .tagline { font-size: 16px; color: #747480; margin-bottom: 24px; }
          .stage-card { border: 1px solid #E6E6E6; border-radius: 4px; padding: 16px; margin-bottom: 16px; page-break-inside: avoid; }
          .stage-title { font-weight: 600; font-size: 15px; margin-bottom: 8px; }
          .stage-outputs { font-size: 12px; color: #747480; margin-top: 8px; }
          .stage-outputs span { background: #F6F6FA; padding: 2px 6px; border-radius: 2px; margin-right: 4px; display: inline-block; margin-bottom: 4px; }
          .feature-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
          .feature-card { border: 1px solid #E6E6E6; border-radius: 4px; padding: 12px; }
          .feature-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
          .feature-desc { font-size: 12px; color: #555; }
          .tech-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
          .tech-table th, .tech-table td { border: 1px solid #E6E6E6; padding: 8px; text-align: left; }
          .tech-table th { background: #F6F6FA; font-weight: 600; }
          .footer { margin-top: 40px; padding-top: 16px; border-top: 1px solid #E6E6E6; font-size: 11px; color: #747480; text-align: center; }
          @media print {
            .no-print { display: none !important; }
          }
        </style>
      </head>
      <body>
        ${printContents}
        <div class="footer">
          <p>Generated by LAMA • Legacy Application Modernisation AI Studio</p>
          <p>© 2026 EY LLP GPS Services. All rights reserved.</p>
        </div>
      </body>
      </html>
    `);
    printWindow.document.close();

    // Wait for content to load, then trigger print
    printWindow.onload = () => {
      setTimeout(() => {
        printWindow.print();
        printWindow.onafterprint = () => printWindow.close();
      }, 250);
    };
  };

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      <header className="bg-surface border-b border-border px-6 py-3">
        <div className="text-micro uppercase tracking-widest text-fg-subtle">Settings</div>
        <div className="flex items-center justify-between">
          <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center">
            <Info className="w-5 h-5 mr-2" />
            About LAMA
            <HelpIcon
              text="Learn about LAMA's capabilities, architecture, and the 5-stage migration pipeline."
              testId="help-about-page"
            />
          </h1>
          <Button
            data-testid="download-pdf-btn"
            onClick={handleDownloadPDF}
            className="bg-ink text-ink-fg hover:bg-ink-hover rounded-sm text-xs h-8 gap-1.5"
          >
            <Download className="w-3.5 h-3.5" />
            Download PDF
          </Button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto mos-scroll p-6 bg-bg">
        <div className="max-w-4xl mx-auto space-y-8" ref={contentRef}>
          {/* Hero Section */}
          <section className="mos-panel p-8 text-center" data-testid="about-hero">
            <div className="inline-flex items-center justify-center w-16 h-16 bg-brand text-fg rounded-lg font-display font-bold text-3xl mb-4">
              L
            </div>
            <h1 className="font-display text-3xl font-bold tracking-tight text-fg mb-2">
              LAMA
            </h1>
            <p className="tagline text-lg text-fg-muted mb-6">
              Legacy Application Modernisation AI Studio
            </p>
            <p className="text-sm text-fg-muted max-w-2xl mx-auto leading-relaxed">
              LAMA is an AI-powered migration studio that transforms legacy applications into
              cloud-native systems. Point it at a folder of legacy code (PHP, JSP, .NET, Python,
              JavaScript, etc.) and LAMA walks the application through a deterministic{" "}
              <strong>5-stage pipeline</strong>, producing freezable, GitHub-pushable artifacts
              at each stage.
            </p>
          </section>

          {/* Vision & Mission */}
          <section className="mos-panel p-6" data-testid="about-vision">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Target className="w-5 h-5 mr-2 text-brand" />
              Vision & Mission
            </h2>
            <div className="grid md:grid-cols-2 gap-6">
              <div>
                <h3 className="font-semibold text-sm text-fg mb-2 flex items-center">
                  <Rocket className="w-4 h-4 mr-1.5 text-info" />
                  Vision
                </h3>
                <p className="text-sm text-fg-muted leading-relaxed">
                  To be the industry's most trusted AI-powered legacy modernization platform,
                  enabling enterprises to transform decades-old systems into cloud-native
                  architectures in weeks instead of years — with full traceability, compliance,
                  and zero knowledge loss.
                </p>
              </div>
              <div>
                <h3 className="font-semibold text-sm text-fg mb-2 flex items-center">
                  <Users className="w-4 h-4 mr-1.5 text-info" />
                  Mission
                </h3>
                <p className="text-sm text-fg-muted leading-relaxed">
                  Empower Migration Architects and Domain SMEs with AI-assisted tooling that
                  automates the tedious parts of modernization (code analysis, SRS generation,
                  schema design, code scaffolding) while keeping humans in control of every
                  freeze-gate decision.
                </p>
              </div>
            </div>
          </section>

          {/* The 5-Stage Pipeline */}
          <section className="mos-panel p-6" data-testid="about-pipeline">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Layers className="w-5 h-5 mr-2 text-brand" />
              The 5-Stage Migration Pipeline
            </h2>
            <p className="text-sm text-fg-muted mb-6">
              Stages are <strong>strictly sequential</strong>. Each stage must be frozen before
              the next unlocks. This ensures artifacts are reviewed and approved before downstream
              generation, maintaining traceability from requirements to code.
            </p>
            <div className="space-y-4">
              {STAGES.map((stage, idx) => {
                const Icon = stage.icon;
                return (
                  <div
                    key={stage.key}
                    className="stage-card border border-border rounded-sm p-4 bg-surface"
                    data-testid={`about-stage-${stage.key}`}
                  >
                    <div className="flex items-start gap-3">
                      <div className="w-10 h-10 rounded-sm bg-bg flex items-center justify-center shrink-0">
                        <Icon className="w-5 h-5 text-fg" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <h3 className="stage-title font-semibold text-sm text-fg mb-1">
                          {stage.title}
                        </h3>
                        <p className="text-xs text-fg-muted leading-relaxed mb-2">
                          {stage.description}
                        </p>
                        <div className="stage-outputs flex flex-wrap gap-1.5">
                          {stage.outputs.map((output) => (
                            <span
                              key={output}
                              className="inline-flex items-center text-micro bg-bg text-fg-muted px-2 py-0.5 rounded-sm"
                            >
                              <CheckCircle2 className="w-3 h-3 mr-1 text-emerald-600" />
                              {output}
                            </span>
                          ))}
                        </div>
                      </div>
                      {idx < STAGES.length - 1 && (
                        <ArrowRight className="w-4 h-4 text-fg-subtle mt-3 hidden md:block" />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          {/* Key Features */}
          <section className="mos-panel p-6" data-testid="about-features">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Sparkles className="w-5 h-5 mr-2 text-brand" />
              Key Features
            </h2>
            <div className="feature-grid grid md:grid-cols-2 gap-4">
              {FEATURES.map((feature) => {
                const Icon = feature.icon;
                return (
                  <div
                    key={feature.title}
                    className="feature-card border border-border rounded-sm p-4 bg-surface"
                  >
                    <div className="flex items-start gap-3">
                      <div className="w-8 h-8 rounded-sm bg-brand-tint flex items-center justify-center shrink-0">
                        <Icon className="w-4 h-4 text-fg" />
                      </div>
                      <div>
                        <h3 className="feature-title font-semibold text-sm text-fg mb-1">
                          {feature.title}
                        </h3>
                        <p className="feature-desc text-xs text-fg-muted leading-relaxed">
                          {feature.description}
                        </p>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          {/* Tech Stack */}
          <section className="mos-panel p-6" data-testid="about-tech-stack">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Code2 className="w-5 h-5 mr-2 text-brand" />
              Technology Stack
            </h2>
            <table className="tech-table w-full text-xs border-collapse">
              <thead>
                <tr className="bg-bg">
                  <th className="text-left px-3 py-2 font-semibold text-fg border border-border">
                    Layer
                  </th>
                  <th className="text-left px-3 py-2 font-semibold text-fg border border-border">
                    Technologies
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="px-3 py-2 border border-border font-medium">Backend</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    Python 3.11, FastAPI 0.110, Motor 3.3 (async MongoDB), Pydantic 2.13, httpx, PyGithub
                  </td>
                </tr>
                <tr className="bg-surface-2">
                  <td className="px-3 py-2 border border-border font-medium">LLM Fabric</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    OpenRouter, Anthropic, OpenAI, Groq, Ollama, Gemini — routed by complexity tier (low/medium/high)
                  </td>
                </tr>
                <tr>
                  <td className="px-3 py-2 border border-border font-medium">AI Frameworks</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    LangGraph (multi-agent orchestration, state machines), HuggingFace (confidence scoring, embeddings)
                  </td>
                </tr>
                <tr>
                  <td className="px-3 py-2 border border-border font-medium">Vector DB</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    Qdrant — hybrid retrieval (BM25 + dense embeddings) for RAG-powered chat
                  </td>
                </tr>
                <tr className="bg-surface-2">
                  <td className="px-3 py-2 border border-border font-medium">Database</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    MongoDB 7 — system of record for LAMA; PostgreSQL is the migration target
                  </td>
                </tr>
                <tr>
                  <td className="px-3 py-2 border border-border font-medium">Frontend</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    React 19, react-router-dom 7, Tailwind 3.4, Radix UI + shadcn/ui, D3, Mermaid, Monaco Editor
                  </td>
                </tr>
                <tr className="bg-surface-2">
                  <td className="px-3 py-2 border border-border font-medium">Runtime</td>
                  <td className="px-3 py-2 border border-border text-fg-muted">
                    Docker single-image bundle (nginx + mongod + uvicorn under supervisord), port 8382
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          {/* Reference Pilot */}
          <section className="mos-panel p-6" data-testid="about-pilot">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Rocket className="w-5 h-5 mr-2 text-brand" />
              Reference Pilot: PMIS Migration
            </h2>
            <p className="text-sm text-fg-muted leading-relaxed mb-4">
              LAMA's reference pilot is the <strong>PMIS (Project Management Information System)</strong>{" "}
              migration — a production PHP 8 / CodeIgniter 4 / MariaDB application being modernized to
              FastAPI / Python 3.12 / PostgreSQL.
            </p>
            <div className="flex flex-col md:flex-row gap-4">
              <div className="flex-1 bg-bg rounded-sm p-4 border border-border">
                <div className="text-micro uppercase tracking-wider text-fg-subtle mb-1">Source Stack</div>
                <div className="font-mono text-sm text-fg">PHP 8 / CodeIgniter 4 / MariaDB</div>
              </div>
              <div className="flex items-center justify-center">
                <ArrowRight className="w-5 h-5 text-brand" />
              </div>
              <div className="flex-1 bg-brand-tint rounded-sm p-4 border border-brand">
                <div className="text-micro uppercase tracking-wider text-fg-subtle mb-1">Target Stack</div>
                <div className="font-mono text-sm text-fg">FastAPI / Python 3.12 / PostgreSQL</div>
              </div>
            </div>
          </section>

          {/* Contact / Credits */}
          <section className="mos-panel p-6" data-testid="about-credits">
            <h2 className="font-display text-lg font-bold tracking-tight text-fg mb-4 flex items-center border-b border-border pb-2">
              <Users className="w-5 h-5 mr-2 text-brand" />
              Credits & Contact
            </h2>
            <p className="text-sm text-fg-muted leading-relaxed">
              LAMA is developed by the <strong>EY LLP GPS Services</strong> team as part of
              the enterprise legacy modernization practice. For questions, feedback, or collaboration
              inquiries, please contact your EY engagement lead or the LAMA product team.
            </p>
            <div className="mt-4 pt-4 border-t border-border text-xs text-fg-subtle">
              <p>Version: 1.0 • Built with ❤️ by EY LLP GPS Services</p>
              <p>© 2026 EY LLP GPS Services. All rights reserved.</p>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
