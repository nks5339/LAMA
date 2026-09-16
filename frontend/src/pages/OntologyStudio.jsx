import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft, Loader2, Network, ListTree, Search, Download,
  ZoomIn, ZoomOut, RotateCcw, Filter, Boxes, RefreshCw, X,
  Users, Database, FileCode, Tag, Building2, AlertTriangle,
} from "lucide-react";
import { toast } from "sonner";
import { useProjects } from "@/state/ProjectContext";
import {
  getBusinessOntology, startBusinessOntologyJob, getBusinessOntologyJob,
} from "@/lib/api";
import { Button } from "@/components/ui/button";

// ────────────────────────────────────────────────────────────────────
// A consistent yellow-accented palette per domain bucket
// ────────────────────────────────────────────────────────────────────
const DOMAIN_PALETTE = [
  { bg: "#FFFCE6", border: "#CCB800", color: "#2E2E38" },
  { bg: "#E0F2FE", border: "#0284C7", color: "#0369A1" },
  { bg: "#D1FAE5", border: "#059669", color: "#065F46" },
  { bg: "#FCE7F3", border: "#BE185D", color: "#9D174D" },
  { bg: "#F3EFFF", border: "#7C3AED", color: "#5B21B6" },
  { bg: "#FEF3C7", border: "#B45309", color: "#92400E" },
  { bg: "#FEE2E2", border: "#B91C1C", color: "#7F1D1D" },
  { bg: "#F3F4F6", border: "#9CA3AF", color: "#374151" },
];

function paletteForDomains(domains) {
  const out = {};
  domains.forEach((d, i) => { out[d] = DOMAIN_PALETTE[i % DOMAIN_PALETTE.length]; });
  return out;
}

// ────────────────────────────────────────────────────────────────────
// Domain-clustered layout: each domain becomes a cluster; entities arrange
// in a circle around the cluster centre. Cross-domain edges become long
// curved lines, intra-domain edges stay short. Far more readable than a
// raw force layout on small graphs.
// ────────────────────────────────────────────────────────────────────
const W = 2800;
const H = 1900;

function clusteredLayout(entities, relationships, domains) {
  if (!entities.length) return { nodes: [], domainPositions: {} };

  // Per-domain bucketing first so we know cluster sizes before placing centres.
  const buckets = {};
  for (const e of entities) {
    buckets[e.domain] = buckets[e.domain] || [];
    buckets[e.domain].push(e);
  }
  const usedDomains = domains.filter((d) => buckets[d] && buckets[d].length > 0);
  const D = usedDomains.length || 1;

  // Cluster radii based on member counts (sized for larger cards).
  const clusterRadius = {};
  for (const d of usedDomains) {
    const n = buckets[d].length;
    if (n === 1)      clusterRadius[d] = 100;
    else if (n === 2) clusterRadius[d] = 180;
    else if (n <= 4)  clusterRadius[d] = 230;
    else if (n <= 8)  clusterRadius[d] = 310;
    else              clusterRadius[d] = 400;
  }
  // Outer ring big enough that the biggest two halos can't touch each other
  // but small enough that all clusters stay inside the viewBox.
  const maxR = Math.max(...Object.values(clusterRadius), 120);
  const halfMin = Math.min(W, H) / 2;
  const ringR = Math.max(0, halfMin - maxR - 110);
  const cx = W / 2;
  const cy = H / 2;

  const domainPositions = {};
  usedDomains.forEach((d, i) => {
    if (D === 1) {
      domainPositions[d] = { cx, cy, r: clusterRadius[d] };
      return;
    }
    const angle = (2 * Math.PI * i) / D - Math.PI / 2;
    domainPositions[d] = {
      cx: cx + ringR * Math.cos(angle),
      cy: cy + ringR * Math.sin(angle),
      r:  clusterRadius[d],
    };
  });

  // Place entities inside each cluster, most-connected ones to centre.
  const degree = {};
  for (const r of relationships) {
    degree[r.source] = (degree[r.source] || 0) + 1;
    degree[r.target] = (degree[r.target] || 0) + 1;
  }

  const placed = [];
  for (const d of usedDomains) {
    const pos = domainPositions[d];
    const list = [...buckets[d]].sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0));
    const n = list.length;
    if (n === 1) {
      placed.push({ ...list[0], x: pos.cx, y: pos.cy });
    } else if (n === 2) {
      placed.push({ ...list[0], x: pos.cx - 130, y: pos.cy });
      placed.push({ ...list[1], x: pos.cx + 130, y: pos.cy });
    } else {
      const r = pos.r - 40;
      list.forEach((e, i) => {
        const a = (2 * Math.PI * i) / n - Math.PI / 2;
        placed.push({ ...e, x: pos.cx + r * Math.cos(a), y: pos.cy + r * Math.sin(a) });
      });
    }
  }
  return { nodes: placed, domainPositions };
}

// Quadratic bezier path between two points, curved away from the chord centre.
function bezierPath(ax, ay, bx, by, curve = 0.18) {
  const mx = (ax + bx) / 2;
  const my = (ay + by) / 2;
  // perpendicular offset
  const dx = bx - ax;
  const dy = by - ay;
  const ox = -dy * curve;
  const oy = dx * curve;
  const px = mx + ox;
  const py = my + oy;
  return { d: `M${ax},${ay} Q${px},${py} ${bx},${by}`, lx: px, ly: py };
}

// ────────────────────────────────────────────────────────────────────
// Graph view
// ────────────────────────────────────────────────────────────────────
function GraphView({ entities, relationships, search, activeDomains, palette, onSelect, selected }) {
  const svgRef = useRef(null);
  const [view, setView] = useState({ scale: 1, tx: 0, ty: 0 });
  const dragRef = useRef(null);
  const [hoverId, setHoverId] = useState(null);

  const filteredEntities = useMemo(() => entities.filter((e) =>
    activeDomains.has(e.domain) &&
    (!search || (e.name || "").toLowerCase().includes(search.toLowerCase())
              || (e.description || "").toLowerCase().includes(search.toLowerCase()))
  ), [entities, activeDomains, search]);

  const activeDomainList = useMemo(
    () => Array.from(new Set(filteredEntities.map((e) => e.domain))),
    [filteredEntities]
  );

  const visibleIds = useMemo(() => new Set(filteredEntities.map((n) => n.id)), [filteredEntities]);
  const filteredRels = useMemo(() => relationships.filter(
    (r) => visibleIds.has(r.source) && visibleIds.has(r.target)
  ), [relationships, visibleIds]);

  const { nodes: positioned, domainPositions } = useMemo(
    () => clusteredLayout(filteredEntities, filteredRels, activeDomainList),
    [filteredEntities, filteredRels, activeDomainList]
  );
  const byId = useMemo(() => Object.fromEntries(positioned.map((n) => [n.id, n])), [positioned]);

  // Auto fit-to-view whenever node set changes meaningfully
  const lastFitKey = useRef("");
  useEffect(() => {
    const key = `${positioned.length}-${activeDomainList.join("|")}`;
    if (positioned.length === 0 || key === lastFitKey.current) return;
    lastFitKey.current = key;
    setView({ scale: 1, tx: 0, ty: 0 });
  }, [positioned, activeDomainList]);

  const onWheel = useCallback((e) => {
    e.preventDefault();
    const dir = e.deltaY < 0 ? 1.1 : 1 / 1.1;
    setView((v) => ({ ...v, scale: Math.max(0.2, Math.min(4, v.scale * dir)) }));
  }, []);

  const onMouseDown = (e) => {
    if (e.target.dataset && e.target.dataset.node) return;
    dragRef.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
  };
  const onMouseMove = (e) => {
    const drag = dragRef.current;
    if (!drag) return;
    setView((v) => ({
      ...v,
      tx: drag.tx + (e.clientX - drag.x),
      ty: drag.ty + (e.clientY - drag.y),
    }));
  };
  const onMouseUp = () => { dragRef.current = null; };

  const focusId = selected || hoverId;
  const isFocused = (nid) => {
    if (!focusId) return true;
    if (nid === focusId) return true;
    return filteredRels.some((r) =>
      (r.source === focusId && r.target === nid) ||
      (r.target === focusId && r.source === nid)
    );
  };

  return (
    <div className="relative w-full h-full overflow-hidden bg-surface-2" data-testid="ontology-graph">
      <div className="absolute top-3 right-3 z-10 flex flex-col gap-1 bg-surface border border-border rounded-sm shadow-sm">
        <button onClick={() => setView((v) => ({ ...v, scale: Math.min(4, v.scale * 1.2) }))} className="p-1.5 hover:bg-bg" data-testid="graph-zoom-in"><ZoomIn className="w-3 h-3" /></button>
        <button onClick={() => setView((v) => ({ ...v, scale: Math.max(0.2, v.scale / 1.2) }))} className="p-1.5 hover:bg-bg" data-testid="graph-zoom-out"><ZoomOut className="w-3 h-3" /></button>
        <button onClick={() => setView({ scale: 1, tx: 0, ty: 0 })} className="p-1.5 hover:bg-bg" data-testid="graph-reset"><RotateCcw className="w-3 h-3" /></button>
      </div>
      <div className="absolute bottom-3 left-3 z-10 bg-surface border border-border rounded-sm px-2 py-1 text-micro text-fg-muted" data-testid="graph-stats">
        {positioned.length} entities · {filteredRels.length} relationships · zoom {Math.round(view.scale * 100)}%
      </div>
      <svg
        ref={svgRef}
        width="100%" height="100%"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
        style={{ cursor: dragRef.current ? "grabbing" : "grab" }}
      >
        <defs>
          <marker id="biz-arrow" viewBox="0 -5 10 10" refX="10" refY="0" markerWidth="8" markerHeight="8" orient="auto">
            <path d="M0,-5L10,0L0,5" fill="#2E2E38" />
          </marker>
          <marker id="biz-arrow-fk" viewBox="0 -5 10 10" refX="10" refY="0" markerWidth="8" markerHeight="8" orient="auto">
            <path d="M0,-5L10,0L0,5" fill="#9CA3AF" />
          </marker>
          <filter id="card-shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="2" floodOpacity="0.12" />
          </filter>
        </defs>
        <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
          {/* Domain halos */}
          {Object.entries(domainPositions).map(([dom, pos]) => {
            const meta = palette[dom] || DOMAIN_PALETTE[0];
            const memberCount = positioned.filter((n) => n.domain === dom).length;
            const r = pos.r + 50;
            return (
              <g key={`halo-${dom}`}>
                <circle cx={pos.cx} cy={pos.cy} r={r}
                  fill={meta.bg} fillOpacity={0.45}
                  stroke={meta.border} strokeOpacity={0.5}
                  strokeDasharray="6 5" strokeWidth={1.5} />
                <rect x={pos.cx - 150} y={pos.cy - r - 44}
                  width={300} height={36} rx={18} ry={18}
                  fill={meta.border} />
                <text x={pos.cx} y={pos.cy - r - 20}
                  textAnchor="middle" fontSize={18} fontWeight={800}
                  fill="white" style={{ letterSpacing: "0.08em", textTransform: "uppercase" }}>
                  {dom}
                </text>
                <text x={pos.cx} y={pos.cy - r + 8}
                  textAnchor="middle" fontSize={14} fontWeight={700}
                  fill={meta.color} opacity={0.85}>
                  {memberCount} {memberCount === 1 ? "entity" : "entities"}
                </text>
              </g>
            );
          })}

          {/* Edges (curved) */}
          {filteredRels.map((r, i) => {
            const a = byId[r.source], b = byId[r.target];
            if (!a || !b) return null;
            const dim = focusId && !(a.id === focusId || b.id === focusId);
            const isFk = (r.kind || "business") === "fk";
            // Curve more when both endpoints are in the same domain (avoid overlap with halo).
            const sameDomain = a.domain === b.domain;
            const { d, lx, ly } = bezierPath(a.x, a.y, b.x, b.y, sameDomain ? 0.32 : 0.16);
            return (
              <g key={`r${i}`} opacity={dim ? 0.1 : 1}>
                <path d={d} fill="none"
                  stroke={isFk ? "#9CA3AF" : "#2E2E38"}
                  strokeWidth={isFk ? 1.2 : 1.8}
                  strokeDasharray={isFk ? "5 4" : ""}
                  markerEnd={isFk ? "url(#biz-arrow-fk)" : "url(#biz-arrow)"} />
                {r.verb && (
                  <g>
                    <rect x={lx - r.verb.length * 4.5 - 6} y={ly - 12} rx={4} ry={4}
                      width={r.verb.length * 9 + 12} height={22}
                      fill="white" stroke={isFk ? "#E6E6E6" : "#D1D5DB"} strokeWidth={1} />
                    <text x={lx} y={ly + 4} textAnchor="middle"
                      fontSize={13} fontWeight={700}
                      fill={isFk ? "#6B7280" : "#2E2E38"}
                      style={{ textTransform: "uppercase", letterSpacing: "0.04em" }}>
                      {r.verb}
                    </text>
                  </g>
                )}
              </g>
            );
          })}

          {/* Entity cards */}
          {positioned.map((n) => {
            const meta = palette[n.domain] || DOMAIN_PALETTE[0];
            const w = Math.max(180, Math.min(280, (n.name || "").length * 12 + 48));
            const h = 68;
            const focused = isFocused(n.id);
            const isSel = n.id === selected;
            const tblCount = (n.backed_by_tables || []).length;
            const clsCount = (n.implemented_in_classes || []).length;
            return (
              <g key={n.id} data-node={n.id}
                 onClick={(e) => { e.stopPropagation(); onSelect(n); }}
                 onMouseEnter={() => setHoverId(n.id)}
                 onMouseLeave={() => setHoverId(null)}
                 style={{ cursor: "pointer" }}
                 opacity={focused ? 1 : 0.22}>
                {/* Card shadow */}
                <rect x={n.x - w / 2} y={n.y - h / 2}
                  rx={12} ry={12} width={w} height={h}
                  fill="white"
                  stroke={isSel ? meta.border : "#E6E6E6"}
                  strokeWidth={isSel ? 4 : 1.4}
                  filter="url(#card-shadow)" />
                {/* Left color stripe */}
                <rect x={n.x - w / 2} y={n.y - h / 2}
                  width={8} height={h} rx={4} ry={4}
                  fill={meta.border} />
                {/* Name */}
                <text x={n.x - w / 2 + 18} y={n.y - 4}
                  fontSize={17} fontWeight={700} fill="#2E2E38">
                  {(n.name || "").length > 20 ? (n.name || "").slice(0, 20) + "…" : n.name}
                </text>
                {/* Stats row */}
                <text x={n.x - w / 2 + 18} y={n.y + 18}
                  fontSize={12} fontWeight={500} fill="#747480"
                  style={{ letterSpacing: "0.02em" }}>
                  {tblCount > 0 && <tspan>{tblCount} table{tblCount !== 1 ? "s" : ""}</tspan>}
                  {tblCount > 0 && clsCount > 0 && <tspan>  ·  </tspan>}
                  {clsCount > 0 && <tspan>{clsCount} class{clsCount !== 1 ? "es" : ""}</tspan>}
                  {tblCount === 0 && clsCount === 0 && <tspan>—</tspan>}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// Tree view — group entities by domain
// ────────────────────────────────────────────────────────────────────
function TreeView({ entities, search, activeDomains, palette, onSelect, selected }) {
  const grouped = useMemo(() => {
    const out = {};
    for (const e of entities) {
      if (!activeDomains.has(e.domain)) continue;
      if (search && !(
        (e.name || "").toLowerCase().includes(search.toLowerCase()) ||
        (e.description || "").toLowerCase().includes(search.toLowerCase())
      )) continue;
      out[e.domain] = out[e.domain] || [];
      out[e.domain].push(e);
    }
    Object.values(out).forEach((arr) => arr.sort((a, b) => (a.name || "").localeCompare(b.name || "")));
    return out;
  }, [entities, search, activeDomains]);

  return (
    <div className="overflow-y-auto h-full p-3 bg-surface border-r border-border" data-testid="ontology-tree">
      {Object.entries(grouped).map(([dom, list]) => {
        const meta = palette[dom] || DOMAIN_PALETTE[0];
        return (
          <div key={dom} className="mb-3">
            <div className="text-micro uppercase font-bold mb-1" style={{ color: meta.color }}>
              {dom} <span className="text-fg-muted">({list.length})</span>
            </div>
            <div className="space-y-0.5">
              {list.map((e) => (
                <button key={e.id} onClick={() => onSelect(e)}
                  data-testid={`tree-${e.id}`}
                  className={`w-full text-left text-micro px-2 py-1 rounded-sm truncate ${
                    selected === e.id ? "bg-brand-tint border border-brand" : "hover:bg-bg"
                  }`}
                  style={{ borderLeft: `3px solid ${meta.border}` }}>
                  <span className="font-semibold">{e.name}</span>
                  {e.lifecycle_states && e.lifecycle_states.length > 0 && (
                    <span className="text-micro text-fg-muted ml-1">· {e.lifecycle_states.length} states</span>
                  )}
                </button>
              ))}
            </div>
          </div>
        );
      })}
      {Object.keys(grouped).length === 0 && (
        <div className="text-micro text-fg-muted py-4">No entities match.</div>
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// Detail panel — full business-entity card
// ────────────────────────────────────────────────────────────────────
function DetailPanel({ entity, relationships, byId, palette, onSelect }) {
  if (!entity) {
    return (
      <div className="p-4 text-micro text-fg-muted flex items-center justify-center h-full">
        <div className="text-center">
          <Building2 className="w-6 h-6 mx-auto mb-2 text-brand" />
          Click any entity to see its description, source tables, owning role and lifecycle.
        </div>
      </div>
    );
  }
  const meta = palette[entity.domain] || DOMAIN_PALETTE[0];
  const rels = relationships.filter((r) => r.source === entity.id || r.target === entity.id);
  return (
    <div className="p-3 overflow-y-auto h-full bg-surface" data-testid={`detail-${entity.id}`}>
      <div className="text-micro uppercase font-bold tracking-wider mb-1" style={{ color: meta.color }}>
        {entity.domain}
      </div>
      <div className="font-display font-bold text-fg text-base break-all leading-tight">{entity.name}</div>
      <div className="text-micro font-mono text-fg-muted break-all mt-0.5">id: {entity.id}</div>

      {entity.description && (
        <div className="mt-3 text-micro text-fg leading-relaxed bg-surface-2 border-l-2 border-brand px-2 py-1.5">
          {entity.description}
        </div>
      )}

      <div className="mt-3 space-y-2">
        {entity.business_owner && (
          <DetailRow icon={Users} label="Business owner" value={entity.business_owner} />
        )}
        {entity.lifecycle_states && entity.lifecycle_states.length > 0 && (
          <div>
            <div className="text-micro uppercase font-bold text-fg-muted flex items-center gap-1 mb-0.5">
              <Tag className="w-3 h-3" /> Lifecycle states
            </div>
            <div className="flex flex-wrap gap-1">
              {entity.lifecycle_states.map((s, i) => (
                <span key={i} className="text-micro bg-brand-tint border border-brand px-1.5 py-0.5 rounded-sm font-mono">
                  {s}
                </span>
              ))}
            </div>
          </div>
        )}
        {entity.backed_by_tables && entity.backed_by_tables.length > 0 && (
          <ListSection icon={Database} label={`Backed by tables (${entity.backed_by_tables.length})`}
            items={entity.backed_by_tables} />
        )}
        {entity.implemented_in_classes && entity.implemented_in_classes.length > 0 && (
          <ListSection icon={FileCode} label={`Implemented in classes (${entity.implemented_in_classes.length})`}
            items={entity.implemented_in_classes} />
        )}
      </div>

      {rels.length > 0 && (
        <div className="mt-4">
          <div className="text-micro uppercase font-bold text-fg-muted mb-1">
            Relationships ({rels.length})
          </div>
          <div className="space-y-0.5">
            {rels.map((r, i) => {
              const isOut = r.source === entity.id;
              const otherId = isOut ? r.target : r.source;
              const other = byId[otherId];
              if (!other) return null;
              const otherMeta = palette[other.domain] || DOMAIN_PALETTE[0];
              return (
                <button key={i} onClick={() => onSelect(other)}
                  className="w-full flex items-center gap-2 text-micro px-2 py-1 rounded-sm hover:bg-bg text-left">
                  <span className="text-micro font-mono text-fg-muted w-4">{isOut ? "→" : "←"}</span>
                  <span className="text-fg font-semibold uppercase text-micro tracking-wide">
                    {r.verb || "relates to"}
                  </span>
                  <span className="font-semibold truncate" style={{ color: otherMeta.color }}>
                    {other.name}
                  </span>
                  {r.kind === "fk" && (
                    <span className="text-micro text-fg-subtle uppercase ml-auto">fk</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function DetailRow({ icon: Icon, label, value }) {
  return (
    <div>
      <div className="text-micro uppercase font-bold text-fg-muted flex items-center gap-1 mb-0.5">
        <Icon className="w-3 h-3" /> {label}
      </div>
      <div className="text-micro text-fg">{value}</div>
    </div>
  );
}

function ListSection({ icon: Icon, label, items }) {
  return (
    <div>
      <div className="text-micro uppercase font-bold text-fg-muted flex items-center gap-1 mb-0.5">
        <Icon className="w-3 h-3" /> {label}
      </div>
      <div className="flex flex-wrap gap-1">
        {items.slice(0, 30).map((s, i) => (
          <span key={i} className="text-micro bg-bg border border-border px-1.5 py-0.5 rounded-sm font-mono break-all">
            {s}
          </span>
        ))}
        {items.length > 30 && (
          <span className="text-micro text-fg-muted">+{items.length - 30} more</span>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// Main page
// ────────────────────────────────────────────────────────────────────
export default function OntologyStudioPage() {
  const { active } = useProjects();
  const projectId = active?.id;
  const [mode, setMode] = useState("graph");
  const [data, setData] = useState(null);
  const [search, setSearch] = useState("");
  const [activeDomains, setActiveDomains] = useState(new Set());
  const [selectedId, setSelectedId] = useState(null);

  const [jobStatus, setJobStatus] = useState("idle"); // idle | queued | running | done | error
  const [jobError, setJobError] = useState("");
  const [progress, setProgress] = useState(0);
  const pollRef = useRef(null);

  // Try to load cached ontology on mount
  const loadCached = useCallback(async () => {
    if (!projectId) return;
    try {
      const r = await getBusinessOntology(projectId);
      // Guard against late-arriving response for a project the user has
      // already switched away from (iter-13.62).
      if (r?.project_id && r.project_id !== projectId) return;
      applyResult(r);
    } catch {
      // 404 — no cache yet, leave empty
      setData(null);
    }
  }, [projectId]);

  const applyResult = useCallback((r) => {
    setData(r);
    setActiveDomains(new Set(r.domains || []));
    setSelectedId(null);
  }, []);

  // Reset per-project state whenever the active project changes — otherwise
  // we'd briefly render project-A's graph while project-B's cache is loading.
  useEffect(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setData(null);
    setSelectedId(null);
    setSearch("");
    setActiveDomains(new Set());
    setJobStatus("idle");
    setJobError("");
    setProgress(0);
    loadCached();
  }, [projectId, loadCached]);

  // Job polling for generation / regeneration
  const startJob = useCallback(async (force = false) => {
    if (!projectId) return;
    setJobStatus("queued");
    setJobError("");
    setProgress(0.05);
    try {
      const start = await startBusinessOntologyJob(projectId, force);
      if (start.status === "done" && start.cached) {
        // Server returned cached immediately
        await loadCached();
        setJobStatus("done");
        setProgress(1);
        return;
      }
      const jobId = start.job_id;
      // Poll every 2s
      pollRef.current = setInterval(async () => {
      if (document.hidden) return;
        try {
          const j = await getBusinessOntologyJob(projectId, jobId);
          setJobStatus(j.status);
          setProgress((p) => Math.min(0.95, Math.max(p, j.progress || p + 0.05)));
          if (j.status === "done") {
            clearInterval(pollRef.current); pollRef.current = null;
            setProgress(1);
            if (j.result) applyResult(j.result);
            else await loadCached();
            toast.success(force ? "Business ontology regenerated." : "Business ontology built.");
          } else if (j.status === "error") {
            clearInterval(pollRef.current); pollRef.current = null;
            setJobError(j.error || "Generation failed.");
            toast.error("Generation failed: " + (j.error || "unknown"));
          }
        } catch (e) {
          clearInterval(pollRef.current); pollRef.current = null;
          setJobStatus("error");
          setJobError(e?.response?.data?.detail || e.message);
        }
      }, 2000);
    } catch (e) {
      setJobStatus("error");
      setJobError(e?.response?.data?.detail || e.message);
      toast.error("Failed to start job: " + (e?.response?.data?.detail || e.message));
    }
  }, [projectId, applyResult, loadCached]);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const palette = useMemo(() => paletteForDomains(data?.domains || []), [data]);
  const byId = useMemo(
    () => Object.fromEntries((data?.entities || []).map((e) => [e.id, e])),
    [data]
  );
  const selected = selectedId ? byId[selectedId] : null;

  const exportJson = () => {
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `business_ontology_${projectId}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const toggleDomain = (d) => {
    setActiveDomains((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d); else next.add(d);
      return next;
    });
  };

  if (!projectId) {
    return <div className="p-8 text-fg-muted" data-testid="ontology-no-project">No active project selected.</div>;
  }

  const running = jobStatus === "queued" || jobStatus === "running";

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0 bg-bg" data-testid="ontology-studio-page">
      {/* Header */}
      <header className="bg-surface border-b-2 border-brand px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link to="/discovery" data-testid="back-to-discovery" className="text-micro uppercase tracking-widest text-fg-muted flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="w-3 h-3" /> Back to Discovery
          </Link>
          <h1 className="font-display text-lg font-bold tracking-tight text-fg flex items-center gap-2">
            <Boxes className="w-4 h-4 text-brand" /> Ontology Studio · Business Domain
            {active?.name && (
              <span
                data-testid="ontology-active-project"
                title={`Project: ${active.name} (${projectId})`}
                className="ml-2 text-micro font-mono font-normal text-fg-muted bg-surface-2 border border-border rounded-sm px-1.5 py-0.5">
                {active.name}
              </span>
            )}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {data && (
            <span className="text-micro text-fg-muted" data-testid="ontology-overall-stats">
              {data.stats?.total_entities || 0} entities · {data.stats?.total_relationships || 0} relationships · {data.stats?.total_domains || 0} domains
            </span>
          )}
          <div className="flex border border-border rounded-sm overflow-hidden">
            <button onClick={() => setMode("graph")} data-testid="mode-graph"
              className={`text-micro px-2 py-1 ${mode === "graph" ? "bg-brand text-fg font-bold" : "bg-surface text-fg-muted"}`}>
              <Network className="w-3 h-3 inline mr-1" /> Graph
            </button>
            <button onClick={() => setMode("tree")} data-testid="mode-tree"
              className={`text-micro px-2 py-1 ${mode === "tree" ? "bg-brand text-fg font-bold" : "bg-surface text-fg-muted"}`}>
              <ListTree className="w-3 h-3 inline mr-1" /> Tree
            </button>
          </div>
          <Button onClick={() => startJob(true)} disabled={running}
            variant="outline" className="h-7 text-micro" data-testid="regenerate-btn">
            {running ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <RefreshCw className="w-3 h-3 mr-1" />}
            {data ? "Regenerate" : "Build now"}
          </Button>
          <Button onClick={exportJson} disabled={!data}
            variant="outline" className="h-7 text-micro" data-testid="export-ontology">
            <Download className="w-3 h-3 mr-1" /> Export JSON
          </Button>
        </div>
      </header>

      {/* Stale banner */}
      {data?.stale && (
        <div className="bg-amber-50 border-b border-amber-200 px-6 py-2 text-micro text-amber-800 flex items-center gap-2" data-testid="stale-banner">
          <AlertTriangle className="w-3 h-3" />
          <span>Your KB has changed since this ontology was generated. Click <strong>Regenerate</strong> to refresh.</span>
        </div>
      )}

      {/* Filter strip */}
      {data && (
        <div className="bg-surface border-b border-border px-6 py-2 flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-1">
            <Search className="w-3 h-3 text-fg-muted" />
            <input value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="Search entities…"
              data-testid="ontology-search"
              className="text-micro border border-border focus:border-fg outline-none rounded-sm px-2 py-1 w-44" />
          </div>
          <div className="flex items-center gap-1 flex-wrap">
            <Filter className="w-3 h-3 text-fg-muted" />
            {(data.domains || []).map((d) => {
              const meta = palette[d] || DOMAIN_PALETTE[0];
              const on = activeDomains.has(d);
              const count = (data.entities || []).filter((e) => e.domain === d).length;
              return (
                <button key={d} onClick={() => toggleDomain(d)}
                  data-testid={`filter-${d}`}
                  className={`text-micro px-1.5 py-0.5 rounded-sm border ${on ? "" : "opacity-40"}`}
                  style={{ background: meta.bg, color: meta.color, borderColor: meta.border }}>
                  {d} <span className="opacity-70">·{count}</span>
                </button>
              );
            })}
          </div>
          {data.source === "deterministic" && (
            <span className="ml-auto text-micro text-amber-700">
              ⚠ LLM enrichment was skipped — showing deterministic clusters only.
            </span>
          )}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 min-h-0 grid grid-cols-12 overflow-hidden">
        {!data ? (
          <div className="col-span-12 flex items-center justify-center text-center p-8" data-testid="ontology-empty">
            {running ? (
              <div>
                <Loader2 className="w-8 h-8 text-brand animate-spin mx-auto mb-3" />
                <div className="font-display text-sm font-bold text-fg">Building business ontology…</div>
                <div className="text-micro text-fg-muted mt-1">
                  Clustering by domain, then asking the LLM to rename + enrich entities. Usually 30–90s.
                </div>
                <div className="w-64 h-1 bg-border rounded-sm mt-3 mx-auto overflow-hidden">
                  <div className="h-full bg-brand transition-all" style={{ width: `${Math.round(progress * 100)}%` }} />
                </div>
              </div>
            ) : jobStatus === "error" ? (
              <div>
                <AlertTriangle className="w-8 h-8 text-rose-500 mx-auto mb-3" />
                <div className="font-display text-sm font-bold text-fg">Generation failed</div>
                <div className="text-micro text-rose-700 mt-1 max-w-md">{jobError}</div>
                <Button onClick={() => startJob(true)} className="mt-3 h-7 text-micro bg-brand text-fg hover:bg-brand-hover" data-testid="retry-btn">
                  Retry
                </Button>
              </div>
            ) : (
              <div>
                <Boxes className="w-10 h-10 text-brand mx-auto mb-3" />
                <div className="font-display text-sm font-bold text-fg">No business ontology yet</div>
                <div className="text-micro text-fg-muted mt-1 max-w-md">
                  Click <strong>Build now</strong> to derive business-domain entities from your KB
                  (deterministic clustering + LLM enrichment).
                </div>
                <Button onClick={() => startJob(false)} className="mt-3 h-8 text-micro bg-brand text-fg hover:bg-brand-hover" data-testid="build-now-btn">
                  <RefreshCw className="w-3 h-3 mr-1" /> Build now
                </Button>
              </div>
            )}
          </div>
        ) : (
          <>
            <div className="col-span-2 min-w-0 overflow-hidden border-r border-border">
              <TreeView
                entities={data.entities || []}
                search={search} activeDomains={activeDomains} palette={palette}
                selected={selectedId} onSelect={(e) => setSelectedId(e.id)}
              />
            </div>
            <div className={`${selected ? "col-span-7" : "col-span-10"} min-w-0 overflow-hidden`}>
              {mode === "graph" ? (
                <GraphView
                  entities={data.entities || []} relationships={data.relationships || []}
                  search={search} activeDomains={activeDomains} palette={palette}
                  selected={selectedId} onSelect={(e) => setSelectedId(e.id)}
                />
              ) : (
                <TreeView
                  entities={data.entities || []}
                  search={search} activeDomains={activeDomains} palette={palette}
                  selected={selectedId} onSelect={(e) => setSelectedId(e.id)}
                />
              )}
            </div>
            {selected && (
              <div className="col-span-3 min-w-0 overflow-hidden border-l border-border relative">
                <button onClick={() => setSelectedId(null)}
                  data-testid="detail-close"
                  className="absolute top-2 right-2 z-10 text-fg-muted hover:text-fg bg-surface border border-border rounded-sm p-0.5">
                  <X className="w-3 h-3" />
                </button>
                <DetailPanel entity={selected} relationships={data.relationships || []}
                  byId={byId} palette={palette} onSelect={(e) => setSelectedId(e.id)} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
