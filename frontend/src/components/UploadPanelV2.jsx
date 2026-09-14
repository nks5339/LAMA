import { useState, useEffect, useRef } from "react";
import { Upload, FolderSearch, GitBranch, Loader2, Boxes, ExternalLink } from "lucide-react";
import { 
  uploadKBFiles, 
  scanFolder, 
  cloneGitRepoAndWait,
  kbStatus,
  listKBFiles,
  buildKB,
  deleteKBFile,
  getGitSource,
  FILE_KINDS
} from "@/lib/api";
import { ModernAccordion, AccordionItem } from "@/components/ux/ModernAccordion";
import { ProgressBar } from "@/components/ux/Cards";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import BuildKBProgressDialog from "@/components/BuildKBProgressDialog";
import { toast } from "sonner";

const ACCEPT = ".php,.sql,.java,.jsp,.cs,.py,.js,.jsx,.ts,.tsx,.html,.xml,.json,.yaml,.pdf,.zip";

// iter-13.120 — Reusable file-type selector. Rendered inside every ingest
// path (upload / scan-folder / clone-git) so the user can force the kind
// regardless of extension. Empty value = auto-detect on the backend.
function FileKindSelector({ value, onChange, testId, scopeLabel = "upload" }) {
  return (
    <div>
      <label className="block text-xs font-medium text-[#2E2E38] mb-1">
        File type <span className="text-[#747480] font-normal">(applied to every file in this {scopeLabel})</span>
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full text-sm border border-[#E6E6E6] rounded-md px-3 py-2 bg-white"
        data-testid={testId}
      >
        <option value="">Auto-detect from extension / filename</option>
        {FILE_KINDS.map((k) => (
          <option key={k.value} value={k.value}>{k.label}</option>
        ))}
      </select>
      {value && (
        <p className="text-[10px] text-[#747480] mt-1">
          {(FILE_KINDS.find((k) => k.value === value) || {}).hint || ""}
        </p>
      )}
    </div>
  );
}

export default function UploadPanelV2({ projectId, onKBUpdated }) {
  const [activeSection, setActiveSection] = useState("files");
  const [status, setStatus] = useState(null);
  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [building, setBuilding] = useState(false);
  // iter-14.10 — small live-tracking popup during Build KB.
  const [buildDialogOpen, setBuildDialogOpen] = useState(false);
  // iter-13.120 — File-kind selector restored. The backend uses this to
  // route each ingested file through the correct downstream pipeline
  // (legacy_code → OWL/TOON, legacy_db → DDL parser, existing_srs → RAG
  // citation, api_spec → arch stage 3, etc.). Empty string = auto-detect.
  const [fileKind, setFileKind] = useState("");
  
  // Folder scan
  const [folderPath, setFolderPath] = useState("");
  const [scanning, setScanning] = useState(false);
  
  // Git clone
  const [gitUrl, setGitUrl] = useState("");
  const [gitBranch, setGitBranch] = useState("main");
  const [gitToken, setGitToken] = useState("");
  const [gitUsername, setGitUsername] = useState("");
  const [cloning, setCloning] = useState(false);
  // iter-13.120 — Track the latest git clone so we can render the cloned
  // repo card (url/branch/commit/counts/status) + its file list inside the
  // "Clone Git Repository" accordion instead of dumping them under
  // "Upload Files" where the user cannot tell where each file came from.
  const [gitSource, setGitSource] = useState(null);
  
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (projectId) refresh();
  }, [projectId]);

  // iter-13.120 — while a clone is ingesting, poll refresh every 2s so
  // the "Cloned Repository" card + progress bar update live without the
  // user having to reload the page.
  useEffect(() => {
    if (!gitSource || gitSource.status !== "ingesting") return;
    const t = setInterval(() => { refresh(); }, 2000);
    return () => clearInterval(t);
  }, [gitSource?.status, gitSource?.id]);

  const refresh = async () => {
    if (!projectId) return;
    try {
      const [s, f, gs] = await Promise.all([
        kbStatus(projectId),
        listKBFiles(projectId),
        getGitSource(projectId).catch(() => null),
      ]);
      setStatus(s);
      setFiles(f);
      setGitSource(gs && gs.id ? gs : null);
      if (onKBUpdated) onKBUpdated(s);
      // If a clone exists, default the open accordion to the Git panel
      // so the user immediately sees the cloned repo + its files.
      if (gs && gs.id && activeSection === "files" && (f?.length || 0) === 0) {
        setActiveSection("git");
      }
    } catch (e) {
      console.error("Refresh failed:", e);
    }
  };

  const handleUpload = async (fileList) => {
    if (!fileList || fileList.length === 0 || !projectId) return;
    setUploading(true);
    try {
      await uploadKBFiles(projectId, Array.from(fileList), fileKind);
      toast.success(`Uploaded ${fileList.length} file(s)${fileKind ? ` as ${fileKind}` : ""}`);
      await refresh();
    } catch (e) {
      toast.error("Upload failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setUploading(false);
    }
  };

  const handleFolderScan = async () => {
    if (!folderPath.trim() || !projectId) return;
    setScanning(true);
    try {
      const r = await scanFolder(projectId, folderPath.trim(), fileKind);
      toast.success(`Scanned ${r.files_found || 0} files`);
      await refresh();
    } catch (e) {
      toast.error("Scan failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setScanning(false);
    }
  };

  const handleGitClone = async () => {
    if (!gitUrl.trim() || !projectId) return;
    setCloning(true);
    // iter-14.5 — /clone-git now returns 202 after the fast git-clone
    // step; ingest runs on the backend in a background task. Poll the
    // /clone-git/status endpoint until it resolves so the user sees a
    // real completion toast instead of the previous "Network Error"
    // (which happened when the request outlived nginx / axios timeouts
    // even though the clone had already succeeded on disk).
    const cloningToastId = toast.loading("Cloning repository…");
    try {
      const r = await cloneGitRepoAndWait(
        projectId,
        {
          url: gitUrl.trim(),
          branch: gitBranch.trim() || "main",
          token: gitToken.trim() || undefined,
          username: gitUsername.trim() || undefined,
          kind: fileKind || undefined,
        },
        true,
        {
          intervalMs: 2000,
          timeoutMs: 30 * 60 * 1000,   // 30 min hard cap on the poll loop
          onTick: (doc) => {
            if (doc?.status === "ingesting") {
              const done = doc.file_count ?? 0;
              const total = doc.total_files ?? 0;
              const skipped = doc.skipped_count ?? 0;
              const pct = doc.progress_pct ?? 0;
              // iter-14.9 — show the file currently being processed so a
              // slow ingest doesn't look frozen. `current_file` is the
              // first in-flight entry from the backend; `last_file` is
              // the most-recently completed one (fallback when the pool
              // briefly empties between batches).
              const current = doc.current_file || doc.last_file || "";
              const currentShort = current
                ? (current.length > 60 ? "…" + current.slice(-60) : current)
                : "";
              const head = total > 0
                ? `Ingesting ${done}/${total} files (${pct}%)${skipped ? ` • ${skipped} skipped` : ""}`
                : `Ingesting files… (commit ${doc.commit?.slice(0, 8) || "?"})`;
              const label = currentShort ? `${head}\n${currentShort}` : head;
              toast.loading(label, { id: cloningToastId });
            }
          },
        },
      );
      if (r?.status === "failed") {
        toast.error("Clone failed during ingest", {
          id: cloningToastId,
          description: r.error || "unknown error",
        });
      } else {
        toast.success(
          `Cloned ${r.file_count ?? 0} files from repository` +
          (r.skipped_count ? ` (${r.skipped_count} skipped)` : ""),
          { id: cloningToastId },
        );
      }
      setGitUrl("");
      setGitBranch("main");
      setGitToken("");
      setGitUsername("");
      setActiveSection("git");  // keep the git panel expanded so the user sees the cloned repo
      await refresh();
    } catch (e) {
      toast.error("Clone failed", {
        id: cloningToastId,
        description: e.response?.data?.detail || e.message,
      });
    } finally {
      setCloning(false);
    }
  };

  const handleBuildKB = async () => {
    if (!projectId) return;
    setBuilding(true);
    // iter-14.10 — open the live-tracking popup the instant the user
    // clicks Build KB, before the (potentially long) POST completes.
    setBuildDialogOpen(true);
    try {
      await buildKB(projectId);
      // Popup will auto-close on phase=done. If the build finished so
      // fast the poll never observed "done", nudge it closed here.
      setTimeout(() => setBuildDialogOpen(false), 1200);
      toast.success("Knowledge base built successfully");
      await refresh();
    } catch (e) {
      // Keep the popup open so the user can read the error phase; close
      // it explicitly so the "X" button pattern is consistent.
      toast.error("Build failed", { description: e.response?.data?.detail || e.message });
    } finally {
      setBuilding(false);
    }
  };

  const handleDelete = async (id) => {
    try {
      await deleteKBFile(id);
      toast.success("File deleted");
      await refresh();
    } catch (e) {
      toast.error("Delete failed");
    }
  };

  const kbReady = (status?.files || 0) > 0;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* iter-14.10 — live-tracking Build KB popup */}
      <BuildKBProgressDialog
        open={buildDialogOpen}
        projectId={projectId}
        onClose={() => setBuildDialogOpen(false)}
      />
      {/* Build KB Action - fixed at top */}
      <div className="p-4 border-b border-[#E6E6E6] bg-white shrink-0">
        <Button
          onClick={handleBuildKB}
          disabled={!kbReady || building}
          className="w-full bg-[#FFE600] hover:bg-[#FFD700] text-[#2E2E38] font-semibold"
        >
          {building ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              Building Knowledge Base...
            </>
          ) : (
            <>
              <Boxes className="w-4 h-4 mr-2" />
              Build Knowledge Base
            </>
          )}
        </Button>
      </div>

      {/* Scrollable main content */}
      <div className="flex-1 min-h-0 overflow-y-auto p-4">{/* Added min-h-0 and overflow-y-auto */}
        <ModernAccordion value={activeSection} onValueChange={setActiveSection}>
          {/* File Upload */}
          <AccordionItem
            value="files"
            icon={Upload}
            title="Upload Files"
            subtitle="Drag & drop or browse files (.php, .java, .py, .zip, etc.)"
            badge={`${files.length} files`}
            status={files.length > 0 ? "success" : "idle"}
            isLoading={uploading}
            loadingText="Uploading..."
            testId="accordion-files"
          >
            <div className="space-y-4">
              {/* iter-13.120 — File type selector (persists across uploads) */}
              <FileKindSelector
                value={fileKind}
                onChange={setFileKind}
                testId="upload-file-kind"
                scopeLabel="upload"
              />

              <div
                className="border-2 border-dashed border-[#E6E6E6] rounded-lg p-8 text-center hover:border-[#FFE600] hover:bg-[#FFFEF0] transition-colors cursor-pointer"
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  handleUpload(e.dataTransfer.files);
                }}
              >
                <Upload className="w-12 h-12 mx-auto mb-3 text-[#747480]" />
                <p className="text-sm font-medium text-[#2E2E38] mb-1">
                  Drop files here or click to browse
                </p>
                <p className="text-xs text-[#747480]">
                  Supports: {ACCEPT.split(',').slice(0, 8).join(', ')}...
                </p>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={ACCEPT}
                  className="hidden"
                  onChange={(e) => handleUpload(e.target.files)}
                />
              </div>

              {files.length > 0 && !gitSource && (
                <div className="space-y-2">
                  <h4 className="text-xs font-semibold text-[#2E2E38] uppercase tracking-wide">
                    Uploaded Files ({files.length})
                  </h4>
                  <div className="max-h-48 overflow-y-auto space-y-1">
                    {files.slice(0, 20).map((f) => (
                      <div
                        key={f.id}
                        className="flex items-center justify-between p-2 bg-[#F6F6FA] rounded text-xs hover:bg-[#E6E6E6]"
                      >
                        <span className="truncate flex-1">{f.filename}</span>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => handleDelete(f.id)}
                          className="text-red-500 hover:text-red-700"
                        >
                          Delete
                        </Button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {gitSource && files.length > 0 && (
                <div className="text-xs text-[#747480] bg-[#FFFEF0] border border-[#FFE600] rounded-md p-3">
                  {files.length} file{files.length === 1 ? "" : "s"} were imported from the cloned repository —
                  see them under <strong>Clone Git Repository</strong> below.
                </div>
              )}
            </div>
          </AccordionItem>

          {/* Folder Scan */}
          <AccordionItem
            value="folder"
            icon={FolderSearch}
            title="Scan Local Folder"
            subtitle="Provide an absolute path to scan files on the server"
            status="idle"
            isLoading={scanning}
            loadingText="Scanning..."
            testId="accordion-folder"
          >
            <div className="space-y-3">
              <FileKindSelector
                value={fileKind}
                onChange={setFileKind}
                testId="scan-file-kind"
                scopeLabel="scan"
              />
              <div>
                <label className="block text-xs font-medium text-[#2E2E38] mb-1">
                  Folder Path
                </label>
                <Input
                  type="text"
                  placeholder="/path/to/legacy/code"
                  value={folderPath}
                  onChange={(e) => setFolderPath(e.target.value)}
                  className="font-mono text-sm"
                  onKeyDown={(e) => e.key === "Enter" && handleFolderScan()}
                />
              </div>
              <Button
                onClick={handleFolderScan}
                disabled={!folderPath.trim() || scanning}
                className="w-full"
              >
                {scanning ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Scanning...
                  </>
                ) : (
                  <>
                    <FolderSearch className="w-4 h-4 mr-2" />
                    Scan Folder
                  </>
                )}
              </Button>
            </div>
          </AccordionItem>

          {/* Git Clone */}
          <AccordionItem
            value="git"
            icon={GitBranch}
            title="Clone Git Repository"
            subtitle={gitSource ? `Cloned: ${gitSource.url} (${gitSource.branch})` : "Clone a repository via HTTPS (token optional)"}
            badge={gitSource ? `${gitSource.file_count || files.length || 0} files` : undefined}
            status={gitSource ? (gitSource.status === "failed" ? "error" : "success") : "idle"}
            isLoading={cloning}
            loadingText="Cloning..."
            testId="accordion-git"
          >
            <div className="space-y-3">
              {/* Cloned repository card (iter-13.120) */}
              {gitSource && (
                <div className="rounded-md border border-[#E6E6E6] bg-[#F6F6FA] p-3 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-[#2E2E38] uppercase tracking-wide flex items-center gap-1">
                        <GitBranch className="w-3 h-3" /> Cloned Repository
                      </div>
                      <a
                        href={gitSource.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs font-mono text-blue-700 hover:underline break-all flex items-center gap-1"
                      >
                        {gitSource.url}
                        <ExternalLink className="w-3 h-3 shrink-0" />
                      </a>
                    </div>
                    <span className={
                      "text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-sm font-bold " +
                      (gitSource.status === "failed"
                        ? "bg-red-100 text-red-700"
                        : gitSource.status === "ingesting"
                        ? "bg-yellow-100 text-yellow-800"
                        : "bg-green-100 text-green-700")
                    }>
                      {gitSource.status || "done"}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] text-[#2E2E38]">
                    <div><span className="text-[#747480]">Branch:</span> <span className="font-mono">{gitSource.branch || "(default)"}</span></div>
                    <div><span className="text-[#747480]">Commit:</span> <span className="font-mono">{(gitSource.commit || "").slice(0, 8) || "—"}</span></div>
                    <div><span className="text-[#747480]">Files:</span> <span className="font-mono">{gitSource.file_count ?? files.length ?? 0}</span></div>
                    <div><span className="text-[#747480]">Skipped:</span> <span className="font-mono">{gitSource.skipped_count ?? 0}</span></div>
                  </div>
                  {gitSource.status === "ingesting" && (gitSource.total_files || 0) > 0 && (
                    <ProgressBar
                      value={gitSource.progress_pct || 0}
                      max={100}
                      label={`Ingesting ${gitSource.file_count || 0}/${gitSource.total_files || 0}`}
                    />
                  )}
                  {gitSource.status === "ingesting" && (gitSource.current_file || gitSource.last_file) && (
                    <div className="text-[11px] text-[#747480] font-mono truncate" title={gitSource.current_file || gitSource.last_file}>
                      <span className="text-[#2E2E38] font-sans font-medium not-italic mr-1">Processing:</span>
                      {gitSource.current_file || gitSource.last_file}
                    </div>
                  )}
                  {gitSource.error && (
                    <div className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded p-2">
                      {gitSource.error}
                    </div>
                  )}

                  {files.length > 0 && (
                    <div className="space-y-1 pt-1">
                      <h4 className="text-xs font-semibold text-[#2E2E38] uppercase tracking-wide">
                        Cloned Files ({files.length})
                      </h4>
                      <div className="max-h-56 overflow-y-auto space-y-1 border border-[#E6E6E6] rounded bg-white">
                        {files.slice(0, 100).map((f) => (
                          <div
                            key={f.id}
                            className="flex items-center justify-between px-2 py-1 text-[11px] border-b border-[#F0F0F0] last:border-b-0 hover:bg-[#F6F6FA]"
                          >
                            <span className="truncate flex-1 font-mono" title={f.filename}>{f.filename}</span>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => handleDelete(f.id)}
                              className="text-red-500 hover:text-red-700 h-6 px-2 text-[10px]"
                            >
                              Delete
                            </Button>
                          </div>
                        ))}
                        {files.length > 100 && (
                          <div className="text-center text-[10px] text-[#747480] py-1">
                            + {files.length - 100} more
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              <FileKindSelector
                value={fileKind}
                onChange={setFileKind}
                testId="clone-file-kind"
                scopeLabel="clone"
              />

              <div>
                <label className="block text-xs font-medium text-[#2E2E38] mb-1">
                  {gitSource ? "Re-clone a different Repository URL" : "Repository URL"}
                </label>
                <Input
                  type="text"
                  placeholder="https://github.com/user/repo.git"
                  value={gitUrl}
                  onChange={(e) => setGitUrl(e.target.value)}
                  className="font-mono text-sm"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-[#2E2E38] mb-1">
                    Branch
                  </label>
                  <Input
                    type="text"
                    placeholder="main"
                    value={gitBranch}
                    onChange={(e) => setGitBranch(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#2E2E38] mb-1">
                    Token (optional)
                  </label>
                  <Input
                    type="password"
                    placeholder="ghp_... / glpat_..."
                    value={gitToken}
                    onChange={(e) => setGitToken(e.target.value)}
                    autoComplete="new-password"
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-[#2E2E38] mb-1">
                  Username (optional)
                </label>
                <Input
                  type="text"
                  placeholder="Leave blank → uses 'oauth2' (works for GitHub PAT + GitLab PAT)"
                  value={gitUsername}
                  onChange={(e) => setGitUsername(e.target.value)}
                  autoComplete="off"
                />
                <p className="text-[10px] text-[#747480] mt-1 leading-snug">
                  <strong>Only fill if you hit "HTTP Basic: Access denied":</strong>{" "}
                  GitHub PAT → any string (or leave blank). GitLab PAT → leave blank (uses <code className="font-mono">oauth2</code>).
                  GitLab <em>Deploy Token</em> → put the token's <em>name</em> here. Bitbucket App Password → your Bitbucket username.
                </p>
              </div>
              <Button
                onClick={handleGitClone}
                disabled={!gitUrl.trim() || cloning}
                className="w-full"
              >
                {cloning ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Cloning...
                  </>
                ) : (
                  <>
                    <GitBranch className="w-4 h-4 mr-2" />
                    Clone Repository
                  </>
                )}
              </Button>
            </div>
          </AccordionItem>
        </ModernAccordion>
      </div>
    </div>
  );
}
