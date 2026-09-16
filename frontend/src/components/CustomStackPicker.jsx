import { useMemo, useState } from "react";
import { Layers, Loader2, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { selectTargetStack } from "@/lib/api";

/**
 * CustomStackPicker
 * -----------------
 * An à-la-carte target-stack builder for users who want something the top-3
 * suggester didn't recommend. Walks the user through five short choices:
 *
 *   1. Backend language        (Java / Python / Node-TS / Go / .NET / Rust)
 *   2. Backend framework       (filtered by language)
 *   3. Database                (Postgres / MySQL / MSSQL / Oracle / Mongo)
 *   4. Frontend                (React / Next / Vue / Angular / Svelte / None)
 *   5. Architecture pattern    (Microservices / Modular Monolith / …)
 *
 * The composed label (e.g. "Spring Boot 3.3 / Java 21 / PostgreSQL 16 /
 * React 19 / Microservices") is POSTed to the same `selectTargetStack`
 * endpoint the suggester uses, so it goes through `validate_target_stack`
 * guardrails and ends up on `project.target_tech` just like a recommended
 * pick. No new backend route is required.
 */

const BACKEND_LANGS = [
  { id: "java",     label: "Java 21",            family: "java" },
  { id: "python",   label: "Python 3.12",        family: "python" },
  { id: "node",     label: "Node.js / TypeScript", family: "node" },
  { id: "go",       label: "Go 1.22",            family: "go" },
  { id: "dotnet",   label: ".NET 8 (C#)",        family: "dotnet" },
  { id: "rust",     label: "Rust",               family: "rust" },
];

// Frameworks grouped by language family. The first entry is the default.
const BACKEND_FRAMEWORKS = {
  java:   ["Spring Boot 3.3", "Quarkus 3", "Micronaut 4", "Helidon 4"],
  python: ["FastAPI", "Django 5", "Flask", "Litestar"],
  node:   ["NestJS", "Fastify", "Express 5", "Hono"],
  go:     ["Echo v4", "Gin", "Fiber v2", "Chi v5"],
  dotnet: ["ASP.NET Core 8 Minimal APIs", "ASP.NET Core 8 MVC"],
  rust:   ["Axum", "Actix-web 4", "Rocket"],
};

const DATABASES = [
  "PostgreSQL 16",
  "MySQL 8",
  "MariaDB 11",
  "SQL Server 2022",
  "Oracle 23ai",
  "MongoDB 7",
  "CockroachDB",
];

const FRONTENDS = [
  "React 19",
  "Next.js 14",
  "Vue 3 / Nuxt 3",
  "Angular 18",
  "Svelte 5",
  "(API-only — no frontend)",
];

const PATTERNS = [
  "Microservices",
  "Modular Monolith",
  "Hexagonal (Ports & Adapters)",
  "Event-Driven (CQRS)",
  "Layered (n-tier)",
];

function composeLabel({ frameworkLabel, langLabel, db, frontend, pattern }) {
  const parts = [];
  if (frameworkLabel) parts.push(frameworkLabel);
  if (langLabel) parts.push(langLabel);
  if (db) parts.push(db);
  if (frontend && !frontend.startsWith("(API-only")) parts.push(frontend);
  if (pattern) parts.push(pattern);
  return parts.join(" / ");
}

export default function CustomStackPicker({ open, onOpenChange, projectId, onApplied }) {
  const [langId, setLangId] = useState(BACKEND_LANGS[0].id);
  const [framework, setFramework] = useState(BACKEND_FRAMEWORKS[BACKEND_LANGS[0].id][0]);
  const [db, setDb] = useState(DATABASES[0]);
  const [frontend, setFrontend] = useState(FRONTENDS[0]);
  const [pattern, setPattern] = useState(PATTERNS[0]);
  const [applying, setApplying] = useState(false);

  const lang = BACKEND_LANGS.find((l) => l.id === langId) || BACKEND_LANGS[0];
  const frameworkChoices = BACKEND_FRAMEWORKS[lang.id] || [];

  // When language changes, reset framework to the default of the new language
  // so we don't ship "FastAPI / Java 21" to validation.
  const handleLangChange = (id) => {
    setLangId(id);
    const choices = BACKEND_FRAMEWORKS[id] || [];
    if (choices.length) setFramework(choices[0]);
  };

  const label = useMemo(
    () => composeLabel({ frameworkLabel: framework, langLabel: lang.label, db, frontend, pattern }),
    [framework, lang.label, db, frontend, pattern],
  );

  const apply = async () => {
    if (!label) return;
    setApplying(true);
    try {
      const r = await selectTargetStack(projectId, {
        // No `id` — this is a custom-built label. Backend resolves by label
        // through validate_target_stack and persists it on the project.
        label,
        rationale: "Custom stack assembled via the Others picker",
        kind: "custom",
        pattern,
      });
      toast.success("Custom target stack applied", { description: r.selection?.selected_label || label });
      onApplied?.(r.project, r.selection);
      onOpenChange(false);
    } catch (e) {
      toast.error("Apply failed", { description: e?.response?.data?.detail || e?.message });
    } finally {
      setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="custom-stack-picker-dialog" className="max-w-2xl p-0 gap-0 overflow-hidden">
        <DialogHeader className="px-5 py-3 border-b border-border">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Layers className="w-4 h-4" />
            Build your own target stack
          </DialogTitle>
        </DialogHeader>

        <div className="p-5 space-y-4 max-h-[70vh] overflow-y-auto">
          <p className="text-[12px] text-fg-muted leading-snug">
            Pick the modern stack layer-by-layer. Your selection is validated
            against LAMA's hybrid-stack guardrails (e.g.{" "}
            <span className="font-mono">PHP FastAPI</span> will be
            auto-corrected) and drives every downstream SRS / Architecture /
            CodeGen prompt.
          </p>

          <PickerRow label="Backend language" testid="picker-lang">
            {BACKEND_LANGS.map((l) => (
              <Chip
                key={l.id}
                active={l.id === langId}
                onClick={() => handleLangChange(l.id)}
                testId={`picker-lang-${l.id}`}
              >
                {l.label}
              </Chip>
            ))}
          </PickerRow>

          <PickerRow label="Backend framework" testid="picker-framework">
            {frameworkChoices.map((f) => (
              <Chip
                key={f}
                active={f === framework}
                onClick={() => setFramework(f)}
                testId={`picker-framework-${f.replace(/\W+/g, "-").toLowerCase()}`}
              >
                {f}
              </Chip>
            ))}
          </PickerRow>

          <PickerRow label="Database" testid="picker-db">
            {DATABASES.map((d) => (
              <Chip key={d} active={d === db} onClick={() => setDb(d)} testId={`picker-db-${d.replace(/\W+/g, "-").toLowerCase()}`}>
                {d}
              </Chip>
            ))}
          </PickerRow>

          <PickerRow label="Frontend" testid="picker-frontend">
            {FRONTENDS.map((f) => (
              <Chip key={f} active={f === frontend} onClick={() => setFrontend(f)} testId={`picker-frontend-${f.replace(/\W+/g, "-").toLowerCase()}`}>
                {f}
              </Chip>
            ))}
          </PickerRow>

          <PickerRow label="Architecture pattern" testid="picker-pattern">
            {PATTERNS.map((p) => (
              <Chip key={p} active={p === pattern} onClick={() => setPattern(p)} testId={`picker-pattern-${p.replace(/\W+/g, "-").toLowerCase()}`}>
                {p}
              </Chip>
            ))}
          </PickerRow>

          <div className="border-t border-border pt-4">
            <div className="mos-label flex items-center gap-1">
              <Sparkles className="w-3 h-3" /> Composed target stack
            </div>
            <div
              data-testid="custom-stack-preview"
              className="mt-1 px-3 py-2 bg-brand-tint border border-brand rounded-sm font-mono text-[13px] text-fg"
            >
              {label}
            </div>
          </div>
        </div>

        <div className="border-t border-border px-5 py-3 flex items-center justify-end gap-2 bg-surface-2">
          <button
            onClick={() => onOpenChange(false)}
            className="px-3 py-1.5 text-[12px] text-fg-muted hover:text-fg"
          >
            Cancel
          </button>
          <button
            data-testid="custom-stack-apply"
            onClick={apply}
            disabled={applying || !label}
            className="px-4 py-1.5 bg-ink text-ink-fg text-[12px] font-semibold rounded-sm hover:bg-ink disabled:opacity-40 flex items-center gap-1.5"
          >
            {applying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Layers className="w-3.5 h-3.5" />}
            Apply custom stack
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function PickerRow({ label, testid, children }) {
  return (
    <div data-testid={testid}>
      <div className="mos-label mb-1.5">{label}</div>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}

function Chip({ active, onClick, testId, children }) {
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      className={`px-2.5 py-1 rounded-sm border text-micro ${
        active
          ? "border-fg bg-ink text-ink-fg font-semibold"
          : "border-border bg-surface hover:bg-surface-2 text-fg-muted"
      }`}
    >
      {children}
    </button>
  );
}


