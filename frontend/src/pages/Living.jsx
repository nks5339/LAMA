import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, Lock, Activity, FlaskConical, GaugeCircle, FileSearch,
  GitCompare, Download, RotateCcw, Wand2,   AlertTriangle, Gauge, ListChecks, FileSpreadsheet, XCircle,
  ChevronLeft,
} from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useProjects } from "@/state/ProjectContext";
import { EmptyState } from "@/components/ux";
import { FolderOpen } from "lucide-react";
import {
  startLivingJob, getLivingJob,
  listLivingArtifacts, getLivingArtifact, updateLivingArtifact,
  freezeLivingArtifact, downloadLivingArtifactUrl,
  freezeLiving, resetLiving,
  startTestCases, cancelLivingJob, downloadTestCasesExcelUrl,
  startAccuracyReport,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import AccuracyReport from "@/components/AccuracyReport";

// iter-14.53 — Living page reorganised into two top-level tabs, with the
// pipeline sub-sections now rendered as their own tab row instead of an
// accordion (accordions hid siblings and made the workflow feel cramped).
// iter-14.70 — Redesigned with sidebar navigation, removing nested tabs.
const SECTIONS = [
  { id: "test_cases", label: "Test Cases",     icon: ListChecks,   group: "testing", desc: "Detailed test matrix" },
  { id: "selenium",   label: "Selenium",       icon: FlaskConical, group: "testing", desc: "UI automation tests" },
  { id: "jmeter",     label: "Load & API",     icon: GaugeCircle,  group: "testing", desc: "JMeter performance" },
  { id: "drift",      label: "Drift Detector", icon: FileSearch,   group: "quality", desc: "Live vs SRS comparison" },
  { id: "srs_diff",   label: "SRS Diff",       icon: GitCompare,   group: "quality", desc: "Version comparison" },
  { id: "accuracy",   label: "Accuracy",       icon: Gauge,        group: "quality", desc: "Confidence scoring" },
];

// poll a job until completion
function useJob(getter, onComplete) {
  const [job, setJob] = useState(null);
  const stopRef = React.useRef(false);
  const timerRef = React.useRef(null);
  useEffect(() => () => {
    stopRef.current = true;
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);
  const start = useCallback((jid) => {
    stopRef.current = false;
    setJob({ id: jid, status: "queued", step: "queued", pct: 0 });
    const tick = async () => {
      if (stopRef.current) return;
      try {
        const j = await getter(jid);
        setJob(j);
        if (j.status === "complete" || j.status === "error") { onComplete && onComplete(j); return; }
      } catch { /* */ }
      if (!stopRef.current) timerRef.current = setTimeout(tick, 2000);
    };
    tick();
  }, [getter, onComplete]);
  return { job, start, reset: () => setJob(null) };
}

function ResetModal({ open, onClose, onConfirm }) {
  const [t, setT] = useState("");
  useEffect(() => { if (!open) setT(""); }, [open]);
  if (!open) return null;
  const enabled = t === "RESET";
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center" data-testid="living-reset-modal">
      <div className="bg-white border-2 border-orange-500 max-w-md w-full rounded-sm p-5">
        <h3 className="font-display font-bold text-orange-700 flex items-center gap-1"><RotateCcw className="w-4 h-4" /> Reset Stage 5 — Living</h3>
        <p className="text-xs text-[#2E2E38] mt-2">Deletes all Selenium / JMeter / Drift / SRS-diff artifacts and unlocks Living for re-generation.</p>
        <p className="text-[10px] text-[#747480] mt-2">Type <code className="bg-[#F6F6FA] px-1">RESET</code> to confirm.</p>
        <input value={t} onChange={(e) => setT(e.target.value)} data-testid="living-reset-input" className="w-full text-sm border border-[#E6E6E6] focus:border-orange-500 outline-none rounded-sm px-2 py-1.5 mt-1" autoFocus />
        <div className="flex justify-end gap-2 mt-3">
          <button onClick={onClose} className="text-xs px-3 py-1.5 border border-[#E6E6E6] rounded-sm">Cancel</button>
          <button disabled={!enabled} onClick={onConfirm} data-testid="living-reset-confirm" className={`text-xs px-3 py-1.5 rounded-sm font-bold text-white ${enabled ? "bg-orange-600 hover:bg-orange-700" : "bg-orange-300 cursor-not-allowed"}`}>Reset Stage 5</button>
        </div>
      </div>
    </div>
  );
}

function ProgressBar({ job, label }) {
  if (!job) return null;
  if (job.status === "error") {
    return (
      <div className="text-[11px] bg-rose-50 border border-rose-200 text-rose-700 p-2 rounded-sm" data-testid={`job-error-${label}`}>
        <AlertTriangle className="w-3 h-3 inline mr-1" /> {job.error || "Failed"}
      </div>
    );
  }
  return (
    <div className="space-y-1" data-testid={`job-progress-${label}`}>
      <div className="flex items-center justify-between text-[10px]">
        <span className="text-[#747480] truncate">{label}: {job.step}</span>
        <span className="text-[#2E2E38] font-semibold">{job.pct || 0}%</span>
      </div>
      <div className="h-1 bg-[#F6F6FA] rounded-sm overflow-hidden">
        <div className="h-full bg-[#FFE600] transition-all" style={{ width: `${job.pct || 0}%` }} />
      </div>
    </div>
  );
}

// iter-13.100 — Test Coverage Visualization Meter
// iter-14.61 — Updated to use running job data when available
function TestCoverageMeter({ artifact, job, isVisible }) {
  // When a job is running, parse target from job step text (e.g., "0/50 so far")
  let jobTarget = null;
  let jobGenerated = null;
  let jobBatch = null;
  let jobBatchTotal = null;
  
  if (job && job.status === "running" && job.step) {
    // Parse: "Batch 1/2 · calling LLM ... · 0/50 so far..."
    const batchMatch = job.step.match(/Batch (\d+)\/(\d+)/);
    const progressMatch = job.step.match(/(\d+)\/(\d+) so far/);
    if (batchMatch) {
      jobBatch = parseInt(batchMatch[1], 10);
      jobBatchTotal = parseInt(batchMatch[2], 10);
    }
    if (progressMatch) {
      jobGenerated = parseInt(progressMatch[1], 10);
      jobTarget = parseInt(progressMatch[2], 10);
    }
  }
  
  // If no artifact yet but job is running, show job progress
  const hasJobData = jobTarget !== null;
  
  // Find test_cases.json file and parse coverage_summary from artifact
  let meta = null;
  if (artifact?.files) {
    const tcFile = artifact.files.find(f => f.path === "test_cases.json");
    if (tcFile?.content) {
      try {
        const parsed = JSON.parse(tcFile.content);
        meta = parsed.meta;
      } catch { /* ignore */ }
    }
  }
  
  // Use job data if running, otherwise use artifact data
  const target_total = hasJobData ? jobTarget : (meta?.target_total || 0);
  const total = hasJobData ? jobGenerated : (meta?.coverage_summary?.total || 0);
  const batches_completed = hasJobData ? jobBatch : (meta?.batches_completed || 0);
  const batches_target = hasJobData ? jobBatchTotal : (meta?.batches_target || 0);
  const n_screens = meta?.n_screens || 0;
  const n_endpoints = meta?.n_endpoints || 0;
  const n_nfr = meta?.n_nfr || 0;
  // iter-14.87: Get actual complexity counts that drive the target
  const n_use_cases = meta?.n_use_cases || 0;
  const n_business_rules = meta?.n_business_rules || 0;
  const n_routes = meta?.n_routes || 0;
  const status = hasJobData ? "generating" : (meta?.status || "");
  // iter-14.86: Incremental mode tracking
  const isIncremental = meta?.incremental_mode || false;
  const newTcsAdded = meta?.new_tcs_added || 0;
  
  // Don't render if no data at all
  if (!hasJobData && !meta?.coverage_summary) return null;
  
  const pct = target_total > 0 ? Math.round((total / target_total) * 100) : 0;
  
  // Color based on coverage percentage
  const getColor = (p) => {
    if (p >= 80) return "bg-green-500";
    if (p >= 50) return "bg-yellow-500";
    return "bg-orange-500";
  };
  
  const getTextColor = (p) => {
    if (p >= 80) return "text-green-500";
    if (p >= 50) return "text-yellow-500";
    return "text-orange-500";
  };
  
  return (
    <div className={`bg-gradient-to-r from-[#FFFCE6] to-[#F6F6FA] border border-[#E6E6E6] rounded-sm p-4 mb-4 ${isVisible ? '' : 'hidden'}`} data-testid="test-coverage-meter">
      {/* Main coverage meter */}
      <div className="flex items-center gap-4">
        {/* Circular gauge - larger and prominent */}
        <div className="relative w-20 h-20 flex-shrink-0">
          <svg className="w-20 h-20 transform -rotate-90" viewBox="0 0 36 36">
            <path
              className="text-[#E6E6E6]"
              strokeWidth="3"
              fill="none"
              stroke="currentColor"
              d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
            />
            <path
              className={getTextColor(pct)}
              strokeWidth="3"
              strokeLinecap="round"
              fill="none"
              stroke="currentColor"
              strokeDasharray={`${pct}, 100`}
              d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
            />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className={`text-xl font-bold ${getTextColor(pct)}`}>{pct}%</span>
            <span className="text-[8px] text-[#747480]">Coverage</span>
          </div>
        </div>
        
        {/* Progress bar and stats */}
        <div className="flex-1">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[12px] font-semibold text-[#2E2E38]">Test Case Generation Progress</span>
            <span className={`text-[11px] font-medium px-2 py-0.5 rounded ${status === "complete" ? "bg-green-100 text-green-700" : "bg-yellow-100 text-yellow-700"}`}>
              {status === "under_floor" ? "In Progress" : status === "complete" ? "Complete" : status}
            </span>
          </div>
          <div className="h-2 bg-[#E6E6E6] rounded-full overflow-hidden mb-2">
            <div 
              className={`h-full ${getColor(pct)} transition-all duration-500`} 
              style={{ width: `${Math.min(pct, 100)}%` }} 
            />
          </div>
          <div className="text-[10px] text-[#747480]">
            <span className="font-semibold text-[#2E2E38]">{total}</span> / {target_total} test cases
            {isIncremental && !hasJobData && (
              <span className="ml-1 text-green-600 font-medium">(+{newTcsAdded} new this run)</span>
            )}
            {batches_completed > 0 && <span className="ml-2">• Batch {batches_completed}/{batches_target || '?'}</span>}
          </div>
        </div>
        
        {/* Stats boxes — iter-14.87: Show counts that actually drive the target */}
        <div className="flex gap-2 flex-shrink-0">
          {/* Show Use Cases if available, else Screens */}
          <div className="bg-white rounded-sm px-3 py-2 border border-[#E6E6E6] text-center min-w-[55px]" title="Use Cases from SRS">
            <div className="text-lg font-bold text-[#2E2E38]">{n_use_cases || n_screens || 0}</div>
            <div className="text-[8px] text-[#747480]">{n_use_cases ? "UC" : "Screens"}</div>
          </div>
          {/* Show Business Rules if available, else Endpoints */}
          <div className="bg-white rounded-sm px-3 py-2 border border-[#E6E6E6] text-center min-w-[55px]" title="Business Rules from SRS">
            <div className="text-lg font-bold text-[#2E2E38]">{n_business_rules || n_endpoints || 0}</div>
            <div className="text-[8px] text-[#747480]">{n_business_rules ? "BR" : "APIs"}</div>
          </div>
          <div className="bg-white rounded-sm px-3 py-2 border border-[#E6E6E6] text-center min-w-[55px]" title="Non-Functional Requirements">
            <div className="text-lg font-bold text-[#2E2E38]">{n_nfr || 0}</div>
            <div className="text-[8px] text-[#747480]">NFR</div>
          </div>
          {/* Show KB Routes if available */}
          {n_routes > 0 && (
            <div className="bg-white rounded-sm px-3 py-2 border border-[#E6E6E6] text-center min-w-[55px]" title="Routes from Knowledge Base">
              <div className="text-lg font-bold text-[#2E2E38]">{n_routes}</div>
              <div className="text-[8px] text-[#747480]">Routes</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ArtifactPanel({ kind, artifact, projectId, onRefresh }) {
  const [editing, setEditing] = useState(false);
  const [editFiles, setEditFiles] = useState([]);
  const [openIdx, setOpenIdx] = useState(0);

  useEffect(() => { if (artifact) setEditFiles(artifact.files || []); }, [artifact?.id, artifact?.version]); // eslint-disable-line

  if (!artifact) {
    return (
      <div className="p-6 text-center text-[#747480] text-[11px]">
        Nothing generated yet for <strong>{kind}</strong>. Click <strong>Generate</strong> above.
      </div>
    );
  }

  const onSave = async () => {
    try { await updateLivingArtifact(projectId, artifact.id, editFiles); toast.success("Saved"); setEditing(false); onRefresh(); }
    catch (e) { toast.error("Save failed: " + (e?.response?.data?.detail || e.message)); }
  };
  const onFreeze = async () => {
    try { await freezeLivingArtifact(projectId, artifact.id); toast.success("Frozen"); onRefresh(); }
    catch (e) { toast.error("Freeze failed"); }
  };

  const files = editing ? editFiles : (artifact.files || []);
  const isMarkdownKind = kind === "drift" || kind === "srs_diff";

  return (
    <div className="bg-white border border-[#E6E6E6] rounded-sm" data-testid={`artifact-${kind}`}>
      <div className="px-3 py-2 border-b border-[#E6E6E6] flex items-center gap-2">
        <span className="text-[10px] uppercase font-bold text-[#747480]">Artifact</span>
        <span className="text-[11px] font-mono">v{artifact.version}</span>
        <span className="text-[10px] text-[#747480]">{files.length} file(s)</span>
        {artifact.frozen && <span className="text-[9px] uppercase bg-[#FFE600] text-[#2E2E38] font-bold px-1.5 py-0.5 rounded-sm flex items-center gap-1"><Lock className="w-2.5 h-2.5" /> Frozen</span>}
        <div className="ml-auto flex gap-1">
          {!artifact.frozen && (editing ? (
            <>
              <button onClick={() => setEditing(false)} className="text-[11px] px-2 py-1 border border-[#E6E6E6] rounded-sm">Cancel</button>
              <button onClick={onSave} data-testid={`save-${kind}`} className="text-[11px] px-2 py-1 bg-[#2E2E38] text-white rounded-sm">Save</button>
            </>
          ) : (
            <button onClick={() => setEditing(true)} data-testid={`edit-${kind}`} className="text-[11px] px-2 py-1 border border-[#E6E6E6] hover:bg-[#F6F6FA] rounded-sm">Edit</button>
          ))}
          {!artifact.frozen && <button onClick={onFreeze} data-testid={`freeze-${kind}`} className="text-[11px] px-2 py-1 bg-[#2E2E38] text-white rounded-sm flex items-center gap-1"><Lock className="w-3 h-3" /> Freeze</button>}
          <a href={downloadLivingArtifactUrl(projectId, artifact.id)} data-testid={`download-${kind}`} className="text-[11px] px-2 py-1 border border-[#E6E6E6] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1"><Download className="w-3 h-3" /></a>
        </div>
      </div>
      <div className="grid grid-cols-12 min-h-[420px]">
        {files.length > 1 && (
          <div className="col-span-3 border-r border-[#E6E6E6] overflow-y-auto max-h-[600px]">
            {files.map((f, i) => (
              <button
                key={i}
                onClick={() => setOpenIdx(i)}
                data-testid={`file-tab-${kind}-${i}`}
                className={`w-full text-left text-[11px] font-mono px-2 py-1 border-l-2 truncate ${openIdx === i ? "border-[#FFE600] bg-[#FFFCE6]" : "border-transparent hover:bg-[#F6F6FA]"}`}
              >
                {f.path}
              </button>
            ))}
          </div>
        )}
        <div className={`${files.length > 1 ? "col-span-9" : "col-span-12"} overflow-y-auto max-h-[600px]`}>
          {files[openIdx] && (editing ? (
            <textarea
              value={files[openIdx].content || ""}
              onChange={(e) => {
                const next = [...editFiles]; next[openIdx] = { ...next[openIdx], content: e.target.value };
                setEditFiles(next);
              }}
              className="w-full h-[420px] p-3 font-mono text-[12px] outline-none resize-none"
              spellCheck={false}
              data-testid={`edit-textarea-${kind}`}
            />
          ) : isMarkdownKind ? (
            <div className="prose prose-sm max-w-none p-4 text-[#2E2E38]">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{files[openIdx].content || ""}</ReactMarkdown>
            </div>
          ) : (
            <pre className="p-3 font-mono text-[12px] whitespace-pre-wrap text-[#2E2E38]">{files[openIdx].content}</pre>
          ))}
        </div>
      </div>
    </div>
  );
}

// iter-14.70 — SectionContent: renders the generator controls + artifact panel for each section
function SectionContent({
  section, artifact, artifacts, projectId, onGenerate,
  onCheckTcConfidence, tcConfidenceLoading,
  tcJob, seleniumJob, jmeterJob, driftJob, diffJob,
  liveSignals, setLiveSignals, srsA, setSrsA, srsB, setSrsB, refresh,
}) {
  if (!section) return null;
  const sec = section;
  const has = artifacts.find((a) => a.kind === sec.id);
  const existingTcCount = has && sec.id === "test_cases" ? (has.meta?.existing_tc_count ?? 0) : 0;
  const isIncremental = existingTcCount > 0 || (has && sec.id === "test_cases");

  return (
    <div className="space-y-4">
      {/* Section header */}
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg bg-[#FFFCE6] flex items-center justify-center">
          <sec.icon className="w-5 h-5 text-[#2E2E38]" />
        </div>
        <div>
          <h2 className="font-display font-bold text-[#2E2E38] text-lg">{sec.label}</h2>
          <p className="text-[11px] text-[#747480]">{sec.desc}</p>
        </div>
      </div>

      {/* Generator controls */}
      <div className="bg-white border border-[#E6E6E6] rounded-sm p-4">
        {sec.id === "test_cases" && (
          <>
            <div className="text-[12px] text-[#2E2E38] mb-3">
              {isIncremental ? (
                <>
                  <strong>Incremental Mode:</strong> Each regeneration <strong>adds more test cases</strong> on top of existing ones,
                  helping you reach full coverage iteratively.
                </>
              ) : (
                <>
                  Generate the <strong>detailed test-case matrix</strong> from KB + SRS + legacy screens / endpoints.
                  Covers positive / negative / security / contract / boundary / NFR / e2e.
                </>
              )}
              <span className="text-[10px] text-[#747480] ml-1">Editable in Prompt Library</span>
            </div>
            <div className="flex flex-wrap gap-2 items-center justify-between">
              <div className="flex gap-2 items-center">
                <Button
                  onClick={() => onGenerate("test_cases")}
                  disabled={!!tcJob.job && tcJob.job.status === "running"}
                  data-testid="btn-gen-test-cases"
                  className="h-9 text-[12px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]"
                >
                  {tcJob.job && tcJob.job.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                  {has ? "Add More Test Cases" : "Generate Test Cases"}
                </Button>
                {tcJob.job && tcJob.job.status === "running" && (
                  <Button
                    onClick={async () => {
                      try {
                        await cancelLivingJob(tcJob.job.id);
                        tcJob.stop?.();
                        refresh();
                      } catch (e) { console.error("Cancel failed:", e); }
                    }}
                    variant="outline"
                    className="h-9 text-[12px] border-red-400 text-red-600 hover:bg-red-50"
                    data-testid="btn-cancel-test-cases"
                  >
                    <XCircle className="w-3.5 h-3.5 mr-1.5" /> Cancel
                  </Button>
                )}
              </div>
              <div className="flex gap-2">
                {has && (
                  <a
                    href={downloadTestCasesExcelUrl(projectId, has.id)}
                    data-testid="btn-download-tc-excel"
                    className="h-9 text-[12px] px-3 border border-[#2E2E38] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1.5 text-[#2E2E38] font-semibold"
                  >
                    <FileSpreadsheet className="w-3.5 h-3.5" /> Download Excel
                  </a>
                )}
                {has && (
                  <Button
                    onClick={onCheckTcConfidence}
                    disabled={tcConfidenceLoading}
                    data-testid="btn-check-tc-confidence"
                    variant="outline"
                    className="h-9 text-[12px]"
                  >
                    {tcConfidenceLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Gauge className="w-3.5 h-3.5 mr-1.5" />}
                    Check Confidence
                  </Button>
                )}
              </div>
            </div>
          </>
        )}
        {sec.id === "selenium" && (
          <>
            <div className="text-[12px] text-[#2E2E38] mb-3">
              Production-grade Selenium suite (JUnit 5 + Page Object Model + WebDriverManager + Allure).
              Includes <code className="bg-[#F6F6FA] px-1 text-[11px]">pom.xml</code>, <code className="bg-[#F6F6FA] px-1 text-[11px]">BaseTest</code>, <code className="bg-[#F6F6FA] px-1 text-[11px]">DriverFactory</code>, GitHub Actions workflow.
            </div>
            <div className="flex flex-wrap gap-2 items-center justify-end">
              {has && (
                <a
                  href={downloadLivingArtifactUrl(projectId, has.id)}
                  data-testid="btn-download-selenium"
                  className="h-9 text-[12px] px-3 border border-[#2E2E38] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1.5 text-[#2E2E38] font-semibold"
                >
                  <Download className="w-3.5 h-3.5" /> Download ZIP
                </a>
              )}
              <Button
                onClick={() => onGenerate("selenium")}
                disabled={!!seleniumJob.job && seleniumJob.job.status === "running"}
                data-testid="btn-gen-selenium"
                className="h-9 text-[12px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]"
              >
                {seleniumJob.job && seleniumJob.job.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                {has ? "Regenerate" : "Generate"} Selenium
              </Button>
            </div>
          </>
        )}
        {sec.id === "jmeter" && (
          <>
            <div className="text-[12px] text-[#2E2E38] mb-3">
              Apache JMeter (.jmx) plan combining <strong>load AND API contract testing</strong>.
              4 samplers per endpoint: Positive-Load / Negative-Auth / Boundary / Contract.
              Downloadable as ZIP (JMX + CSV + Runbook).
            </div>
            <div className="flex flex-wrap gap-2 items-center justify-end">
              {has && (
                <a
                  href={downloadLivingArtifactUrl(projectId, has.id)}
                  data-testid="btn-download-jmeter"
                  className="h-9 text-[12px] px-3 border border-[#2E2E38] hover:bg-[#F6F6FA] rounded-sm flex items-center gap-1.5 text-[#2E2E38] font-semibold"
                >
                  <Download className="w-3.5 h-3.5" /> Download ZIP
                </a>
              )}
              <Button
                onClick={() => onGenerate("jmeter")}
                disabled={!!jmeterJob.job && jmeterJob.job.status === "running"}
                data-testid="btn-gen-jmeter"
                className="h-9 text-[12px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]"
              >
                {jmeterJob.job && jmeterJob.job.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                {has ? "Regenerate" : "Generate"} Plan
              </Button>
            </div>
          </>
        )}
        {sec.id === "drift" && (
          <>
            <div className="text-[12px] text-[#2E2E38] mb-3">
              Paste signals from your live system (logs, route inventory, telemetry, schema introspection).
              LAMA compares against the frozen SRS and produces a P0/P1/P2 drift report.
            </div>
            <textarea
              value={liveSignals}
              onChange={(e) => setLiveSignals(e.target.value)}
              placeholder="POST /api/orders responded with 503 12% of the time…&#10;GET /api/users — not present in deployed routes…&#10;dim_customer column 'tier' added on 2026-04-22…"
              data-testid="drift-signals-input"
              className="w-full h-32 border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm p-3 font-mono text-[12px] resize-y bg-[#FAFAFC] mb-3"
            />
            <div className="flex justify-end">
              <Button
                onClick={() => onGenerate("drift")}
                disabled={!liveSignals.trim() || (driftJob.job && driftJob.job.status === "running")}
                data-testid="btn-gen-drift"
                className="h-9 text-[12px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]"
              >
                {driftJob.job && driftJob.job.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                Generate Drift Report
              </Button>
            </div>
          </>
        )}
        {sec.id === "srs_diff" && (
          <>
            <div className="text-[12px] text-[#2E2E38] mb-3">
              Compare two SRS versions. Identifies added/removed/modified requirements + which downstream artifacts to regenerate.
            </div>
            <div className="grid grid-cols-2 gap-3 mb-3">
              <div>
                <label className="text-[10px] uppercase text-[#747480] font-semibold mb-1 block">SRS A (e.g. frozen v1)</label>
                <textarea
                  value={srsA}
                  onChange={(e) => setSrsA(e.target.value)}
                  placeholder="Paste first SRS version…"
                  data-testid="srs-a-input"
                  className="w-full h-32 border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm p-3 font-mono text-[12px] resize-y bg-[#FAFAFC]"
                />
              </div>
              <div>
                <label className="text-[10px] uppercase text-[#747480] font-semibold mb-1 block">SRS B (e.g. current draft)</label>
                <textarea
                  value={srsB}
                  onChange={(e) => setSrsB(e.target.value)}
                  placeholder="Paste second SRS version…"
                  data-testid="srs-b-input"
                  className="w-full h-32 border border-[#E6E6E6] focus:border-[#2E2E38] outline-none rounded-sm p-3 font-mono text-[12px] resize-y bg-[#FAFAFC]"
                />
              </div>
            </div>
            <div className="flex justify-end">
              <Button
                onClick={() => onGenerate("srs-diff")}
                disabled={!srsA.trim() || !srsB.trim() || (diffJob.job && diffJob.job.status === "running")}
                data-testid="btn-gen-srs-diff"
                className="h-9 text-[12px] bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500]"
              >
                {diffJob.job && diffJob.job.status === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                Diff SRS
              </Button>
            </div>
          </>
        )}

        {/* Progress bar */}
        <ProgressBar
          job={sec.id === "test_cases" ? tcJob.job : sec.id === "selenium" ? seleniumJob.job : sec.id === "jmeter" ? jmeterJob.job : sec.id === "drift" ? driftJob.job : diffJob.job}
          label={sec.id}
        />
      </div>

      {/* Test Coverage Meter for test_cases */}
      {sec.id === "test_cases" && (artifact || tcJob.job) && (
        <TestCoverageMeter artifact={artifact} job={tcJob.job} isVisible={true} />
      )}

      {/* Artifact panel */}
      <ArtifactPanel kind={sec.id} artifact={artifact} projectId={projectId} onRefresh={refresh} />
    </div>
  );
}

export default function LivingPage() {
  const navigate = useNavigate();
  const { active } = useProjects();
  const projectId = active?.id;
  const status = active?.stage_status?.["Living"] || "locked";
  const cgStatus = active?.stage_status?.["CodeGen"] || "locked";
  // iter-13.66 alignment — "skipped" unlocks downstream just like "frozen".
  const isLocked = cgStatus !== "frozen" && cgStatus !== "skipped";
  const isFrozen = status === "frozen";

  // iter-14.70 — Single activeSection state replaces nested tab/pipelineKind
  const [activeSection, setActiveSection] = useState("test_cases");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [artifacts, setArtifacts] = useState([]);
  const [resetOpen, setResetOpen] = useState(false);
  const [activeArtifact, setActiveArtifact] = useState(null);
  // iter-14.50 — track a section-scoped confidence run so the user
  // gets fast feedback next to the Test Cases artifact instead of
  // having to scroll to the Accuracy Report tab.
  const [tcConfidenceLoading, setTcConfidenceLoading] = useState(false);

  // Drift signals editor + SRS-diff inputs
  const [liveSignals, setLiveSignals] = useState("");
  const [srsA, setSrsA] = useState("");
  const [srsB, setSrsB] = useState("");

  const refresh = useCallback(async () => {
    if (!projectId) return;
    try {
      const r = await listLivingArtifacts(projectId);
      setArtifacts(r.artifacts || []);
      const cur = (r.artifacts || []).find((a) => a.kind === activeSection);
      if (cur) {
        const full = await getLivingArtifact(projectId, cur.id);
        setActiveArtifact(full);
      } else { setActiveArtifact(null); }
    } catch { /* */ }
  }, [projectId, activeSection]);
  useEffect(() => { refresh(); }, [refresh]);

  const seleniumJob = useJob(getLivingJob, refresh);
  const jmeterJob = useJob(getLivingJob, refresh);
  const driftJob = useJob(getLivingJob, refresh);
  const diffJob = useJob(getLivingJob, refresh);
  const tcJob = useJob(getLivingJob, refresh);

  const onGenerate = async (kind) => {
    if (!projectId) return;
    try {
      if (kind === "test_cases") {
        const r = await startTestCases(projectId);
        tcJob.start(r.job_id);
        toast.message("Detailed test-case generation started");
        return;
      }
      const extras = {};
      if (kind === "drift") extras.live_signals = liveSignals;
      if (kind === "srs-diff") { extras.srs_a = srsA; extras.srs_b = srsB; }
      const r = await startLivingJob(kind, projectId, extras);
      if (kind === "selenium") seleniumJob.start(r.job_id);
      else if (kind === "jmeter") jmeterJob.start(r.job_id);
      else if (kind === "drift") driftJob.start(r.job_id);
      else if (kind === "srs-diff") diffJob.start(r.job_id);
      toast.message(`${kind} job started`);
    } catch (e) { toast.error(`Could not start ${kind}: ` + (e?.response?.data?.detail || e.message)); }
  };

  const onCheckTcConfidence = async () => {
    if (!projectId) return;
    setTcConfidenceLoading(true);
    try {
      await startAccuracyReport(projectId, ["test_cases"]);
      toast.success("Confidence check started — opening Accuracy Report…");
      setActiveSection("accuracy");
    } catch (e) {
      toast.error("Could not start confidence check: " + (e?.response?.data?.detail || e.message));
    } finally {
      setTcConfidenceLoading(false);
    }
  };

  const onFreezeStage = async () => {
    try { await freezeLiving(projectId); toast.success("Living frozen"); }
    catch { toast.error("Freeze failed"); }
  };
  const onReset = async () => {
    try { await resetLiving(projectId); toast.success("Stage 5 reset"); setResetOpen(false); setArtifacts([]); setActiveArtifact(null); }
    catch { toast.error("Reset failed"); }
  };

  if (!projectId) return (
    <EmptyState
      icon={FolderOpen}
      title="No project selected"
      description="Create or open a project from the sidebar to run the living system."
      action={
        <button
          type="button"
          onClick={() => navigate("/")}
          className="text-xs px-3 py-1.5 bg-[#FFE600] text-[#2E2E38] rounded-sm hover:bg-yellow-300 font-semibold focus:outline-none focus:ring-2 focus:ring-[#2E2E38]"
          data-testid="empty-goto-discovery"
        >
          Go to Discovery →
        </button>
      }
    />
  );

  if (isLocked) {
    return (
      <div className="flex-1 flex flex-col bg-[#F6F6FA]" data-testid="living-locked">
        <header className="bg-white border-b-2 border-[#FFE600] px-6 py-3">
          <div className="text-[10px] uppercase tracking-widest text-[#747480]">Stage 5 of 5</div>
          <h1 className="font-display text-lg font-bold tracking-tight text-[#2E2E38]">Living System</h1>
        </header>
        <div className="flex-1 flex items-center justify-center p-8">
          <div className="max-w-md bg-white border border-[#E6E6E6] rounded-sm p-6 text-center">
            <Lock className="w-8 h-8 mx-auto text-[#747480] mb-3" />
            <h2 className="font-display font-bold text-[#2E2E38]">Locked — CodeGen not frozen</h2>
            <p className="text-xs text-[#747480] mt-2">Generate code and freeze Stage 4 to unlock Living.</p>
            <button onClick={() => navigate("/code-gen")} className="mt-4 text-xs px-3 py-1.5 bg-[#2E2E38] text-white rounded-sm">Open CodeGen →</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-[#F6F6FA]" data-testid="living-page">
      <header className="bg-white border-b-2 border-[#FFE600] px-4 sm:px-6 py-2 flex items-center justify-between gap-4 shrink-0">
        <div className="flex items-center gap-3">
          <Activity className="w-5 h-5 text-[#FFE600]" />
          <div>
            <div className="text-[10px] uppercase tracking-widest text-[#747480]">Stage 5 of 5</div>
            <h1 className="font-display text-base font-bold tracking-tight text-[#2E2E38]">Living System</h1>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-[#747480]">{artifacts.length} artifact(s)</span>
          {isFrozen && <span className="text-[10px] uppercase font-bold bg-[#FFE600] text-[#2E2E38] px-2 py-0.5 rounded-sm">Frozen</span>}
          {!isFrozen && artifacts.length > 0 && (
            <Button onClick={onFreezeStage} data-testid="freeze-living-stage" className="h-7 text-[11px] bg-[#2E2E38] text-white">
              <Lock className="w-3 h-3 mr-1" /> Freeze Stage 5
            </Button>
          )}
          <button onClick={() => setResetOpen(true)} data-testid="btn-reset-living" className="text-xs px-2 py-1 border border-orange-300 text-orange-700 hover:bg-orange-50 rounded-sm flex items-center gap-1">
            <RotateCcw className="w-3 h-3" /> Reset
          </button>
        </div>
      </header>

      {/* Main content - Sidebar + Content grid */}
      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* Collapsible Left Sidebar - LAMA style */}
        <aside className={`${sidebarCollapsed ? "w-12" : "w-52"} bg-white border-r border-[#E6E6E6] flex flex-col shrink-0 transition-all duration-200`}>
          {/* Header with inline collapse toggle */}
          {sidebarCollapsed ? (
            /* Collapsed: Show "QA" expand button */
            <button
              onClick={() => setSidebarCollapsed(false)}
              className="w-9 h-9 m-1.5 bg-[#FFE600] text-[#2E2E38] flex items-center justify-center rounded-sm font-display font-bold text-[10px]"
              title="Expand sidebar"
            >
              QA
            </button>
          ) : (
            /* Expanded: Header row with title + collapse chevron */
            <div className="flex items-center justify-between px-3 py-2 border-b border-[#E6E6E6]">
              <div className="flex items-center gap-2">
                <Activity className="w-4 h-4 text-[#FFE600]" />
                <span className="text-[11px] font-bold text-[#2E2E38] uppercase tracking-wide">QA Panel</span>
              </div>
              <button
                onClick={() => setSidebarCollapsed(true)}
                className="text-[#747480] hover:text-[#2E2E38] p-1 -mr-1"
                title="Collapse sidebar"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
            </div>
          )}
          
          <div className="flex-1 overflow-y-auto mos-scroll py-2">
            {/* Testing group */}
            {!sidebarCollapsed && (
              <div className="px-3 py-1.5">
                <div className="text-[9px] uppercase tracking-widest text-[#747480] font-semibold">Testing</div>
              </div>
            )}
            {SECTIONS.filter(s => s.group === "testing").map((sec) => {
              const has = artifacts.find((a) => a.kind === sec.id);
              const isActive = activeSection === sec.id;
              return (
                <button
                  key={sec.id}
                  data-testid={`nav-${sec.id}`}
                  onClick={() => setActiveSection(sec.id)}
                  title={sidebarCollapsed ? `${sec.label}${has ? ` (v${has.version})` : ""}` : undefined}
                  className={`w-full flex items-center ${sidebarCollapsed ? "justify-center px-2" : "gap-2.5 px-3"} py-2 text-left transition-colors ${
                    isActive
                      ? "bg-[#FFFCE6] border-l-2 border-[#FFE600]"
                      : "hover:bg-[#FAFAFC] border-l-2 border-transparent"
                  }`}
                >
                  <div className="relative">
                    <sec.icon className={`w-4 h-4 shrink-0 ${isActive ? "text-[#2E2E38]" : "text-[#747480]"}`} />
                    {sidebarCollapsed && has?.frozen && (
                      <Lock className="w-2 h-2 text-[#FFE600] absolute -top-1 -right-1" />
                    )}
                  </div>
                  {!sidebarCollapsed && (
                    <>
                      <div className="flex-1 min-w-0">
                        <div className={`text-[12px] truncate ${isActive ? "text-[#2E2E38] font-semibold" : "text-[#747480]"}`}>
                          {sec.label}
                        </div>
                        <div className="text-[9px] text-[#A0A0AB] truncate">{sec.desc}</div>
                      </div>
                      {has && (
                        <div className="flex items-center gap-1 shrink-0">
                          <span className="text-[9px] bg-[#F6F6FA] px-1 rounded-sm font-mono">v{has.version}</span>
                          {has.frozen && <Lock className="w-2.5 h-2.5 text-[#FFE600]" />}
                        </div>
                      )}
                    </>
                  )}
                </button>
              );
            })}
            
            {/* Quality group */}
            <div className={`${sidebarCollapsed ? "my-2 mx-2 border-t border-[#E6E6E6]" : "px-3 py-1.5 mt-3 border-t border-[#E6E6E6]"}`}>
              {!sidebarCollapsed && <div className="text-[9px] uppercase tracking-widest text-[#747480] font-semibold pt-2">Quality</div>}
            </div>
            {SECTIONS.filter(s => s.group === "quality").map((sec) => {
              const has = artifacts.find((a) => a.kind === sec.id);
              const isActive = activeSection === sec.id;
              return (
                <button
                  key={sec.id}
                  data-testid={`nav-${sec.id}`}
                  onClick={() => setActiveSection(sec.id)}
                  title={sidebarCollapsed ? `${sec.label}${has ? ` (v${has.version})` : ""}` : undefined}
                  className={`w-full flex items-center ${sidebarCollapsed ? "justify-center px-2" : "gap-2.5 px-3"} py-2 text-left transition-colors ${
                    isActive
                      ? "bg-[#FFFCE6] border-l-2 border-[#FFE600]"
                      : "hover:bg-[#FAFAFC] border-l-2 border-transparent"
                  }`}
                >
                  <div className="relative">
                    <sec.icon className={`w-4 h-4 shrink-0 ${isActive ? "text-[#2E2E38]" : "text-[#747480]"}`} />
                    {sidebarCollapsed && has?.frozen && (
                      <Lock className="w-2 h-2 text-[#FFE600] absolute -top-1 -right-1" />
                    )}
                  </div>
                  {!sidebarCollapsed && (
                    <>
                      <div className="flex-1 min-w-0">
                        <div className={`text-[12px] truncate ${isActive ? "text-[#2E2E38] font-semibold" : "text-[#747480]"}`}>
                          {sec.label}
                        </div>
                        <div className="text-[9px] text-[#A0A0AB] truncate">{sec.desc}</div>
                      </div>
                      {has && (
                        <div className="flex items-center gap-1 shrink-0">
                          <span className="text-[9px] bg-[#F6F6FA] px-1 rounded-sm font-mono">v{has.version}</span>
                          {has.frozen && <Lock className="w-2.5 h-2.5 text-[#FFE600]" />}
                        </div>
                      )}
                    </>
                  )}
                </button>
              );
            })}
          </div>
        </aside>

        {/* Main Content Area */}
        <main className="flex-1 overflow-y-auto mos-scroll p-4 space-y-3" data-testid="living-content">
          {activeSection === "accuracy" ? (
            <AccuracyReport projectId={projectId} />
          ) : (
            <SectionContent
              section={SECTIONS.find(s => s.id === activeSection)}
              artifact={activeArtifact}
              artifacts={artifacts}
              projectId={projectId}
              onGenerate={onGenerate}
              onCheckTcConfidence={onCheckTcConfidence}
              tcConfidenceLoading={tcConfidenceLoading}
              tcJob={tcJob}
              seleniumJob={seleniumJob}
              jmeterJob={jmeterJob}
              driftJob={driftJob}
              diffJob={diffJob}
              liveSignals={liveSignals}
              setLiveSignals={setLiveSignals}
              srsA={srsA}
              setSrsA={setSrsA}
              srsB={srsB}
              setSrsB={setSrsB}
              refresh={refresh}
            />
          )}
        </main>
      </div>

      <ResetModal open={resetOpen} onClose={() => setResetOpen(false)} onConfirm={onReset} />
    </div>
  );
}
