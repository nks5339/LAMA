/**
 * iter-15.9 — Shared component: an uploaded-file row that, when the file
 * is a ZIP, lazily reads its central directory (via jszip) and shows the
 * entries in a collapsible tree. Consumed by both the Gap Analyzer
 * Input tab and the Technology Transformer Input tab so the two tools
 * present source uploads identically.
 *
 * Props:
 *   file        — the File object (from <input type=file/>)
 *   onRemove    — () => void
 *   accentColor — Tailwind class (e.g. "violet" | "purple") for icons
 *   showSize    — bool (default true) — render human size on the right
 *   testId      — optional data-testid for the remove button
 */
import { useState, useMemo } from "react";
import { X, ChevronRight, ChevronDown, FileText, Archive, Folder, RefreshCw } from "lucide-react";
import JSZip from "jszip";

const fmtBytes = (b) => {
  if (b == null || Number.isNaN(b)) return "";
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(1)} GB`;
};

const isZipName = (name) =>
  /\.(zip|jar|war)$/i.test(name || "");

// Build a nested tree from a flat list of {path, size, isDir} entries.
const buildTree = (entries) => {
  const root = { name: "", children: {}, files: [] };
  for (const e of entries) {
    const parts = e.path.split("/").filter(Boolean);
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      if (!node.children[seg]) node.children[seg] = { name: seg, children: {}, files: [] };
      node = node.children[seg];
    }
    const leaf = parts[parts.length - 1];
    if (e.isDir) {
      if (!node.children[leaf]) node.children[leaf] = { name: leaf, children: {}, files: [] };
    } else {
      node.files.push({ name: leaf, size: e.size });
    }
  }
  return root;
};

const TreeNode = ({ node, depth = 0, defaultOpen = false }) => {
  const [open, setOpen] = useState(defaultOpen || depth < 1);
  const childDirs = Object.values(node.children || {}).sort((a, b) => a.name.localeCompare(b.name));
  const files = (node.files || []).sort((a, b) => a.name.localeCompare(b.name));
  const hasKids = childDirs.length + files.length > 0;

  return (
    <div>
      {node.name && (
        <button
          type="button"
          onClick={() => setOpen(v => !v)}
          className="flex items-center gap-1 w-full text-left px-1 py-0.5 hover:bg-slate-100 rounded"
          style={{ paddingLeft: `${depth * 12 + 4}px` }}
        >
          {hasKids ? (
            open ? <ChevronDown size={11} className="text-slate-400 flex-shrink-0" />
                 : <ChevronRight size={11} className="text-slate-400 flex-shrink-0" />
          ) : <span className="w-[11px] flex-shrink-0" />}
          <Folder size={11} className="text-amber-500 flex-shrink-0" />
          <span className="text-[11px] font-medium text-slate-700 truncate">{node.name}/</span>
          <span className="text-[9px] text-slate-400 ml-auto tabular-nums">
            {childDirs.length + files.length}
          </span>
        </button>
      )}
      {(!node.name || open) && (
        <div>
          {childDirs.map(c => (
            <TreeNode key={c.name} node={c} depth={depth + (node.name ? 1 : 0)} />
          ))}
          {files.map(f => (
            <div
              key={f.name}
              className="flex items-center gap-1 px-1 py-0.5 text-[11px] text-slate-600"
              style={{ paddingLeft: `${(depth + (node.name ? 1 : 0)) * 12 + 4}px` }}
            >
              <span className="w-[11px] flex-shrink-0" />
              <FileText size={11} className="text-slate-400 flex-shrink-0" />
              <span className="truncate">{f.name}</span>
              {f.size != null && (
                <span className="ml-auto text-[9px] text-slate-400 tabular-nums flex-shrink-0">
                  {fmtBytes(f.size)}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default function ZipFileRow({
  file,
  onRemove,
  accentColor = "violet",
  showSize = true,
  testId = null,
  RowIcon = null,
}) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState(null);

  const zip = isZipName(file?.name);
  const accent = {
    violet:  { bg: "bg-violet-50",  border: "border-violet-200",  fg: "text-violet-700", icon: "text-violet-500" },
    purple:  { bg: "bg-purple-50",  border: "border-purple-200",  fg: "text-purple-700", icon: "text-purple-500" },
    slate:   { bg: "bg-slate-50",   border: "border-slate-200",   fg: "text-slate-700",  icon: "text-slate-500"  },
  }[accentColor] || { bg: "bg-slate-50", border: "border-slate-200", fg: "text-slate-700", icon: "text-slate-500" };

  const loadEntries = async () => {
    if (entries || loading) return;
    setLoading(true);
    setError(null);
    try {
      const buf = await file.arrayBuffer();
      const z = await JSZip.loadAsync(buf);
      const list = [];
      z.forEach((relPath, obj) => {
        list.push({
          path: relPath,
          size: obj?._data?.uncompressedSize ?? null,
          isDir: !!obj.dir,
        });
      });
      setEntries(list);
    } catch (e) {
      setError(e?.message || "Could not read archive");
    } finally {
      setLoading(false);
    }
  };

  const toggle = async () => {
    if (!zip) return;
    if (!expanded) await loadEntries();
    setExpanded(v => !v);
  };

  const tree = useMemo(() => entries ? buildTree(entries) : null, [entries]);
  const fileCount = entries ? entries.filter(e => !e.isDir).length : 0;

  return (
    <div className="border border-slate-200 rounded-md overflow-hidden bg-white">
      <div className="flex items-center gap-2 px-2 py-1.5 hover:bg-slate-50 text-xs">
        {zip ? (
          <button
            type="button"
            onClick={toggle}
            className="text-slate-400 hover:text-slate-700 flex-shrink-0"
            title={expanded ? "Collapse archive" : "Preview archive contents"}
          >
            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
        ) : (
          <span className="w-[12px] flex-shrink-0" />
        )}
        {RowIcon ? <RowIcon size={12} className={`${accent.icon} flex-shrink-0`} />
                 : zip ? <Archive size={12} className={`${accent.icon} flex-shrink-0`} />
                 : <FileText size={12} className={`${accent.icon} flex-shrink-0`} />}
        <span className="truncate flex-1 text-slate-700 font-medium" title={file.name}>{file.name}</span>
        {zip && entries && (
          <span className={`text-[9px] px-1.5 py-0.5 rounded ${accent.bg} ${accent.fg} font-semibold tabular-nums`}>
            {fileCount} entr{fileCount === 1 ? "y" : "ies"}
          </span>
        )}
        {showSize && (
          <span className="text-[10px] text-slate-400 tabular-nums flex-shrink-0">
            {fmtBytes(file.size)}
          </span>
        )}
        <button
          type="button"
          onClick={onRemove}
          title="Remove file"
          data-testid={testId}
          className="text-slate-400 hover:text-red-600 hover:bg-red-50 rounded p-0.5 flex-shrink-0"
        >
          <X size={12} />
        </button>
      </div>
      {expanded && zip && (
        <div className="border-t border-slate-100 bg-slate-50/50 px-1 py-1 max-h-[220px] overflow-y-auto">
          {loading && (
            <div className="flex items-center gap-2 text-[11px] text-slate-500 px-2 py-1">
              <RefreshCw size={11} className="animate-spin" /> Reading archive…
            </div>
          )}
          {error && (
            <div className="text-[11px] text-red-600 px-2 py-1">Failed to read: {error}</div>
          )}
          {!loading && !error && tree && (
            <TreeNode node={tree} depth={0} defaultOpen />
          )}
        </div>
      )}
    </div>
  );
}
