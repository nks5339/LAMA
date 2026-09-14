import { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { 
  Upload, 
  MessageSquare, 
  FileText, 
  Boxes, 
  Folder,
  FileCode,
  Sparkles,
  ArrowRight
} from "lucide-react";
import { useProjects } from "@/state/ProjectContext";
import { skipStage } from "@/lib/api";
import UploadPanel from "@/components/UploadPanelV2";
import DataSourcePanel from "@/components/DataSourcePanel";
import SRSPanel from "@/components/SRSPanel";
import TargetStackSuggester from "@/components/TargetStackSuggester";
import FloatingChat from "@/components/FloatingChat";
import { MetricCard, StepCard, StatusBadge, EmptyState } from "@/components/ux/Cards";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "sonner";

/**
 * Discovery Page - V2 Redesign
 * 
 * Modern tabbed interface with:
 * - Hero section with project stats
 * - Clear step-by-step workflow
 * - Visual progress indicators
 * - Tab-based navigation (Upload | Chat | SRS)
 */
export default function DiscoveryV2() {
  const { active } = useProjects();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState("upload");
  const [kbStatus, setKbStatus] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const [srsRefreshKey, setSrsRefreshKey] = useState(0);
  const [skippingDM, setSkippingDM] = useState(false);
  // Read-only: nothing in the UI can change this today. The picker that
  // used to set it lived in ChatPanel, which had no render site and was
  // removed. See HUMAN_INTERVENTION.md DEC-8.
  const [chatModel] = useState(
    typeof window !== "undefined" ? (localStorage.getItem("lama:chat:model") || "") : ""
  );

  // Load KB health on mount
  useEffect(() => {
    if (!active?.id) return;
    let cancelled = false;
    (async () => {
      try {
        const h = await kbStatus(active.id);
        if (!cancelled) setKbStatus(h);
      } catch (_) {}
    })();
    return () => { cancelled = true; };
  }, [active?.id]);

  // iter-13.120 — "Skip DataModel → Architecture" fast-path. Requires
  // Discovery to be frozen (backend rejects otherwise). Marks DataModel
  // as skipped in stage_context so Architecture becomes available.
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
      // DataModel may already be skipped or frozen — force=true handles both.
      await skipStage(active.id, "DataModel", { force: true });
      toast.success("Skipped DataModel → going to Architecture");
      navigate("/architecture");
    } catch (e) {
      toast.error("Could not skip DataModel", {
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
      setActiveTab("srs"); // Auto-switch to SRS tab
    }
  };

  const kbReady = (kbStatus?.entities || 0) > 0 || (kbStatus?.chunks || 0) > 0 || (kbStatus?.files || 0) > 0;

  // Determine step status

  if (!active) {
    return (
      <EmptyState
        icon={Folder}
        title="No Project Selected"
        description="Create or select a project from the sidebar to begin the discovery process."
      />
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-gradient-to-br from-[#F6F6FA] to-white">
      {/* Compact Header */}
      <header className="bg-white border-b border-[#E6E6E6] px-6 py-3 shrink-0">
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="font-display text-xl font-bold text-[#2E2E38]">
                Discovery & Requirements
              </h1>
              <StatusBadge 
                status={active.stage_status?.Discovery === "frozen" ? "success" : "active"}
                label={active.stage_status?.Discovery === "frozen" ? "Frozen" : "In Progress"}
                size="sm"
              />
            </div>
            <p className="text-xs text-[#747480] mt-0.5">
              Stage 1 of 5 • Upload source code, analyze with AI, generate SRS documentation
            </p>
          </div>
          <div className="flex items-center gap-2">
            {active?.stage_status?.Discovery === "frozen" && (
              <button
                onClick={handleSkipToArchitecture}
                disabled={skippingDM}
                data-testid="skip-to-architecture-btn"
                className="inline-flex items-center gap-2 px-3 py-1.5 bg-[#FFE600] text-[#2E2E38] text-xs font-bold rounded-lg border border-[#2E2E38] hover:bg-[#FFD700] transition-colors disabled:opacity-60"
                title="Mark DataModel as skipped and jump straight to Architecture"
              >
                {skippingDM ? "Skipping…" : "Skip to Architecture"}
                <ArrowRight className="w-3.5 h-3.5" />
              </button>
            )}
            <Link
              to="/ontology-studio"
              className="inline-flex items-center gap-2 px-3 py-1.5 bg-[#2E2E38] text-white text-xs font-medium rounded-lg hover:bg-[#FFE600] hover:text-[#2E2E38] transition-colors"
            >
              <Boxes className="w-3.5 h-3.5" />
              Ontology Studio
            </Link>
          </div>
        </div>
      </header>

      {/* iter-14.1 — Onboarding banner (shows only when no files yet) */}
      {(!kbStatus?.files || kbStatus.files === 0) && active.stage_status?.Discovery !== "frozen" && (
        <div className="bg-gradient-to-r from-[#FFFCE6] via-[#FFF9B0] to-[#FFFCE6] border-b-2 border-[#FFE600] px-6 py-3 shrink-0" data-testid="onboarding-banner">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-[#FFE600] text-[#2E2E38] flex items-center justify-center shrink-0 font-bold text-sm">
              👋
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-bold text-[#2E2E38]">
                Welcome to LAMA — let's migrate your legacy app in 5 steps
              </div>
              <div className="text-xs text-[#4B5563] mt-0.5">
                <span className="font-semibold">Start here:</span> upload your source folder (.zip / .php / .java / .sql) below,
                click <span className="inline-block px-1.5 py-0.5 bg-white border border-[#FFE600] rounded text-[10px] font-mono font-bold">Build Knowledge Base</span>,
                then chat with the AI to generate an IEEE-830 SRS. Freeze it to unlock Stage 2.
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Compact Metrics Row */}
      <div className="bg-white border-b border-[#E6E6E6] px-6 py-2 shrink-0">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          <MetricCard
            label="Source Files"
            value={kbStatus?.files || 0}
            icon={FileCode}
            color="blue"
          />
          <MetricCard
            label="Code Entities"
            value={kbStatus?.entities || 0}
            icon={Sparkles}
            color="purple"
          />
          <MetricCard
            label="KB Chunks"
            value={kbStatus?.chunks || 0}
            icon={Boxes}
            color="green"
          />
          <MetricCard
            label="Chat Sessions"
            value={conversationId ? 1 : 0}
            icon={MessageSquare}
            color="yellow"
          />
        </div>
      </div>

      {/* Clickable Workflow Steps - Compact */}
      <div className="bg-white border-b border-[#E6E6E6] px-6 py-3 shrink-0">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <StepCard
            stepNumber={1}
            title="Upload Knowledge Base"
            description="Import your legacy codebase"
            status={activeTab === "upload" ? "active" : (kbReady ? "complete" : "pending")}
            icon={Upload}
            onClick={() => setActiveTab("upload")}
          />
          <StepCard
            stepNumber={2}
            title="Generate SRS"
            description="IEEE-830 compliant specification"
            status={activeTab === "srs" ? "active" : (active?.stage_status?.Discovery === "frozen" ? "complete" : "pending")}
            icon={FileText}
            onClick={() => kbReady && setActiveTab("srs")}
          />
        </div>
      </div>

      {/* Main Content - Maximized Space */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="h-full flex flex-col">
          {/* Hide tab buttons - step cards are now the navigation */}
          <div className="hidden">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="upload">Knowledge Base</TabsTrigger>
              <TabsTrigger value="srs">SRS Document</TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="upload" className="flex-1 flex flex-col overflow-hidden mt-0 data-[state=active]:flex data-[state=inactive]:hidden">
            <div className="flex-1 min-h-0 overflow-y-auto">
              <div className="w-full p-6 space-y-6">
                {/* Upload Knowledge Base Section */}
                <div className="bg-white rounded-lg border border-[#E6E6E6] shadow-sm">
                  <UploadPanel
                    projectId={active.id}
                    onKBUpdated={setKbStatus}
                  />
                </div>

                {/* Live Data Sources — DB + App URL (iter 13.8) */}
                <div className="bg-white rounded-lg border border-[#E6E6E6] shadow-sm p-6">
                  <div className="mb-4">
                    <h3 className="text-sm font-semibold text-[#2E2E38] uppercase tracking-wide">
                      Live Data Sources
                    </h3>
                    <p className="text-xs text-[#747480] mt-1">
                      Connect a live database and/or the running application URL so LAMA can ingest schema + endpoints directly into the KB.
                    </p>
                  </div>
                  <DataSourcePanel
                    projectId={active.id}
                    onSchemaIngested={() => setKbStatus((s) => ({ ...(s || {}) }))}
                  />
                </div>

                {/* Target Stack Selection Section */}
                <div className="bg-white rounded-lg border border-[#E6E6E6] shadow-sm p-6">
                  <TargetStackSuggester
                    projectId={active.id}
                    kbReady={kbReady}
                    onApplied={(updatedProject) => {
                      // Optionally refresh project context if needed
                      console.log("Target stack applied:", updatedProject);
                    }}
                  />
                </div>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="srs" className="flex-1 flex flex-col overflow-hidden mt-0 data-[state=active]:flex data-[state=inactive]:hidden">
            <div className="flex-1 min-h-0 bg-white rounded-lg border border-[#E6E6E6] overflow-hidden">
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

      {/* Floating Chat - ChatGPT-like overlay */}
      <FloatingChat
        projectId={active.id}
        kbReady={kbReady}
        model={chatModel}
        onConversationUpdated={handleConversationUpdated}
        stage="Discovery"
        agentKey="srs.chat"
        enableSrsEdit={true}
        chatTitle="Discovery Chat"
      />
    </div>
  );
}
