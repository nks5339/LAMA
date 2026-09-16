import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Upload,
  MessageSquare,
  FileText,
  Boxes,
  Folder,
  FileCode,
  Sparkles,
  ArrowRight,
  AlertTriangle,
} from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
// kbStatus is aliased because the component also holds a state variable of
// the same shape. The previous version imported only skipStage, so the call
// on line 54 resolved to the useState variable (null), threw
// "kbStatus is not a function", and was swallowed by an empty catch — which
// is why all four metric tiles read 0 on every load and kbReady never
// became true. ESLint had been reporting it as a missing dependency.
import { skipStage, kbStatus as fetchKbStatus } from "@/lib/api";
import UploadPanel from "@/components/UploadPanelV2";
import DataSourcePanel from "@/components/DataSourcePanel";
import SRSPanel from "@/components/SRSPanel";
import TargetStackSuggester from "@/components/TargetStackSuggester";
import FloatingChat from "@/components/FloatingChat";
import { MetricCard, StepCard, StatusBadge, EmptyState } from "@/components/ux/Cards";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "sonner";

/**
 * Discovery — Stage 1.
 *
 * Upload legacy source, build the knowledge base, generate and freeze an
 * IEEE-830 SRS.
 */
export default function DiscoveryV2() {
  const { active } = useProjects();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState("upload");
  const [kb, setKb] = useState(null);
  const [kbLoading, setKbLoading] = useState(true);
  const [kbError, setKbError] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const [srsRefreshKey, setSrsRefreshKey] = useState(0);
  const [skippingDM, setSkippingDM] = useState(false);

  // Read-only: nothing in the UI sets this today. The picker lived in
  // ChatPanel, which had no render site and was removed. See DEC-8.
  const [chatModel] = useState(() => {
    try {
      return localStorage.getItem("lama:chat:model") || "";
    } catch {
      return "";
    }
  });

  const loadKb = useCallback(async () => {
    if (!active?.id) return;
    setKbLoading(true);
    setKbError(null);
    try {
      const h = await fetchKbStatus(active.id);
      setKb(h);
    } catch (e) {
      // Never swallow this: the user needs to know why the tiles are empty.
      const msg = e?.response?.data?.detail || e?.message || "Request failed";
      setKbError(msg);
      toast.error("Couldn't load knowledge-base status", {
        description: msg,
        action: { label: "Retry", onClick: () => loadKb() },
      });
    } finally {
      setKbLoading(false);
    }
  }, [active?.id]);

  useEffect(() => {
    let cancelled = false;
    if (!active?.id) {
      setKbLoading(false);
      return undefined;
    }
    (async () => {
      if (!cancelled) await loadKb();
    })();
    return () => {
      cancelled = true;
    };
  }, [active?.id, loadKb]);

  // iter-13.120 — "Skip DataModel → Architecture" fast-path. Requires
  // Discovery frozen (the backend rejects otherwise); marks DataModel
  // skipped in stage_context so Architecture becomes available.
  const handleSkipToArchitecture = async () => {
    if (!active?.id) return;
    if (active.stage_status?.Discovery !== "frozen") {
      toast.error("Freeze the SRS first", {
        description: "Discovery must be frozen before you can skip to Architecture.",
      });
      return;
    }
    setSkippingDM(true);
    try {
      await skipStage(active.id, "DataModel", { force: true });
      toast.success("Skipped Data Model — going to Architecture");
      navigate("/architecture");
    } catch (e) {
      toast.error("Could not skip Data Model", {
        description: e?.response?.data?.detail || e.message,
      });
    } finally {
      setSkippingDM(false);
    }
  };

  const handleConversationUpdated = (cid, srsTriggered) => {
    setConversationId(cid);
    if (srsTriggered) {
      setSrsRefreshKey((k) => k + 1);
      setActiveTab("srs");
    }
  };

  const kbReady =
    (kb?.entities || 0) > 0 || (kb?.chunks || 0) > 0 || (kb?.files || 0) > 0;
  const frozen = active?.stage_status?.Discovery === "frozen";

  if (!active) {
    return (
      <EmptyState
        icon={Folder}
        title="No project selected"
        description="Pick a project from the sidebar, or create one, to start discovery."
        actionLabel="Open project switcher"
        onAction={() => {
          // The switcher lives in the sidebar; on mobile it is behind the
          // menu, so surface that rather than leaving a dead end.
          document
            .querySelector('[data-testid="project-switcher"]')
            ?.scrollIntoView({ behavior: "smooth", block: "center" });
          document.querySelector('[data-testid="mobile-menu-toggle"]')?.click();
        }}
      />
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-bg">
      {/* Header */}
      <header className="bg-surface border-b border-border px-4 sm:px-6 py-3 shrink-0">
        <div className="flex items-start sm:items-center justify-between gap-3 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="font-display text-xl font-bold text-fg">
                Discovery &amp; Requirements
              </h1>
              <StatusBadge
                status={frozen ? "success" : "active"}
                label={frozen ? "Frozen" : "In progress"}
                size="sm"
              />
            </div>
            <p className="text-xs text-fg-muted mt-0.5">
              Stage 1 of 5 · Upload source code, analyse with AI, generate SRS
            </p>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {frozen && (
              <Button
                variant="brand"
                size="sm"
                onClick={handleSkipToArchitecture}
                loading={skippingDM}
                data-testid="skip-to-architecture-btn"
              >
                Skip to Architecture
                <ArrowRight className="size-3.5" aria-hidden />
              </Button>
            )}
            <Button variant="primary" size="sm" asChild>
              <Link to="/ontology-studio">
                <Boxes className="size-3.5" aria-hidden />
                Ontology Studio
              </Link>
            </Button>
          </div>
        </div>
      </header>

      {/* Onboarding — only before the first upload. */}
      {!kbLoading && !kbError && !kb?.files && !frozen && (
        <div
          className="bg-brand-tint border-b border-brand-edge px-4 sm:px-6 py-3 shrink-0"
          data-testid="onboarding-banner"
        >
          <div className="flex items-start gap-3">
            <div className="size-7 rounded-lg bg-brand text-brand-fg grid place-items-center shrink-0 font-bold text-xs">
              1
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-fg">
                Start by uploading your legacy source
              </p>
              <p className="text-xs text-fg-muted mt-0.5">
                Drop a .zip, or individual .php / .java / .sql files, then choose{" "}
                <span className="font-mono font-semibold">Build Knowledge Base</span>.
                Chat with the AI to generate an IEEE-830 SRS, and freeze it to
                unlock Stage&nbsp;2.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* KB status failed to load — say so rather than showing zeroes. */}
      {kbError && (
        <div
          className="bg-crit-bg border-b border-crit-edge px-4 sm:px-6 py-2.5 shrink-0"
          role="alert"
        >
          <div className="flex items-center gap-3">
            <AlertTriangle className="size-4 text-crit shrink-0" aria-hidden />
            <p className="text-xs text-fg flex-1 min-w-0">
              Couldn&apos;t load knowledge-base status — the counts below may be
              out of date.{" "}
              <span className="text-fg-muted">{kbError}</span>
            </p>
            <Button size="xs" variant="outline" onClick={loadKb}>
              Retry
            </Button>
          </div>
        </div>
      )}

      {/* Metrics */}
      <div className="bg-surface border-b border-border px-4 sm:px-6 py-2 shrink-0">
        <div
          className="grid grid-cols-2 md:grid-cols-4 gap-2"
          aria-busy={kbLoading}
        >
          <MetricCard
            label="Source files"
            value={kb?.files ?? 0}
            icon={FileCode}
            loading={kbLoading}
            tone={kb?.files ? "ok" : "neutral"}
            data-testid="metric-files"
          />
          <MetricCard
            label="Code entities"
            value={kb?.entities ?? 0}
            icon={Sparkles}
            loading={kbLoading}
            data-testid="metric-entities"
          />
          <MetricCard
            label="KB chunks"
            value={kb?.chunks ?? 0}
            icon={Boxes}
            loading={kbLoading}
            data-testid="metric-chunks"
          />
          <MetricCard
            label="Chat sessions"
            value={conversationId ? 1 : 0}
            icon={MessageSquare}
            data-testid="metric-sessions"
          />
        </div>
      </div>

      {/* Workflow steps — these ARE the tab navigation. */}
      <div className="bg-surface border-b border-border px-4 sm:px-6 py-3 shrink-0">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <StepCard
            stepNumber={1}
            title="Upload knowledge base"
            description="Import your legacy codebase"
            status={
              activeTab === "upload" ? "active" : kbReady ? "complete" : "pending"
            }
            icon={Upload}
            onClick={() => setActiveTab("upload")}
            data-testid="step-upload"
          />
          <StepCard
            stepNumber={2}
            title="Generate SRS"
            description="IEEE-830 compliant specification"
            status={activeTab === "srs" ? "active" : frozen ? "complete" : "pending"}
            icon={FileText}
            disabled={!kbReady}
            // The old version was `onClick={() => kbReady && setActiveTab("srs")}`,
            // so a blocked step looked pressable and silently did nothing.
            disabledReason="Build the knowledge base first — upload source files, then choose Build Knowledge Base."
            onClick={() => setActiveTab("srs")}
            data-testid="step-srs"
          />
        </div>
      </div>

      {/* Panels */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <Tabs
          value={activeTab}
          onValueChange={setActiveTab}
          className="h-full flex flex-col"
        >
          {/* The step cards above are the visible navigation; this list is
              kept mounted and screen-reader reachable so Radix's roving
              tabindex still provides a keyboard path between panels. */}
          <TabsList className="sr-only">
            <TabsTrigger value="upload">Knowledge base</TabsTrigger>
            <TabsTrigger value="srs" disabled={!kbReady}>
              SRS document
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value="upload"
            className="flex-1 flex flex-col overflow-hidden mt-0 data-[state=active]:flex data-[state=inactive]:hidden"
          >
            <div className="flex-1 min-h-0 overflow-y-auto mos-scroll">
              <div className="w-full p-4 sm:p-6 flex flex-col gap-6">
                <div className="bg-surface rounded border border-border shadow-raised">
                  <UploadPanel projectId={active.id} onKBUpdated={setKb} />
                </div>

                {/* Live data sources — DB + app URL (iter 13.8) */}
                <section className="bg-surface rounded border border-border shadow-raised p-4 sm:p-6">
                  <div className="mb-4">
                    <h2 className="text-sm font-semibold text-fg uppercase tracking-wide">
                      Live data sources
                    </h2>
                    <p className="text-xs text-fg-muted mt-1 max-w-prose">
                      Connect a live database and/or the running application URL
                      so LAMA can ingest schema and endpoints directly into the
                      knowledge base.
                    </p>
                  </div>
                  <DataSourcePanel
                    projectId={active.id}
                    onSchemaIngested={loadKb}
                  />
                </section>

                <section className="bg-surface rounded border border-border shadow-raised p-4 sm:p-6">
                  <TargetStackSuggester
                    projectId={active.id}
                    kbReady={kbReady}
                    onApplied={() => loadKb()}
                  />
                </section>
              </div>
            </div>
          </TabsContent>

          <TabsContent
            value="srs"
            className="flex-1 flex flex-col overflow-hidden mt-0 data-[state=active]:flex data-[state=inactive]:hidden"
          >
            <div className="flex-1 min-h-0 bg-surface border-t border-border overflow-hidden">
              <SRSPanel
                key={srsRefreshKey}
                projectId={active.id}
                conversationId={conversationId}
                model={chatModel}
                kbReady={kbReady}
                onCollapse={null}
              />
            </div>
          </TabsContent>
        </Tabs>
      </div>

      <FloatingChat
        projectId={active.id}
        kbReady={kbReady}
        model={chatModel}
        onConversationUpdated={handleConversationUpdated}
        stage="Discovery"
        agentKey="srs.chat"
        enableSrsEdit
        chatTitle="Discovery Chat"
      />
    </div>
  );
}
