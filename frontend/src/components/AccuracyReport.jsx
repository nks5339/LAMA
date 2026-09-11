import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, Wand2, Gauge, ArrowRight, AlertTriangle, CheckCircle2,
  ChevronDown, ChevronRight as ChevronRightIcon,
  Search, Filter, Sparkles, ShieldAlert, ShieldCheck, Clock,
  Cpu, Database, Layers, Code2, Activity, XCircle,
  BarChart3, TrendingUp,
} from "lucide-react";
import { toast } from "sonner";
import {
  startAccuracyReport, getLatestAccuracyReport, getLivingJob,
} from "@/lib/api";
import { Button } from "@/components/ui/button";

// iter-13.70 — Living-stage Accuracy Report.
// iter-14.60 — Full UX redesign: hero gauge, stage summary strip,
// filter bar, elevated section cards. Data model & data-testids
// preserved so backend + tests keep working.

// ─────────────────────────────────────────────────────────────────────
// Score → color palette (single source of truth for every visual).
// ─────────────────────────────────────────────────────────────────────
const bucket = (score) => {
  const s = Number(score) || 0;
  if (s >= 95) return "excellent";
  if (s >= 85) return "good";
  if (s >= 70) return "warn";
  return "risk";
};

const PALETTE = {
  excellent: {
    bar:        "bg-emerald-500",
    bg:         "bg-emerald-50",
    bgSolid:    "bg-emerald-500",
    text:       "text-emerald-700",
    textStrong: "text-emerald-800",
    border:     "border-emerald-200",
    ring:       "stroke-emerald-500",
    chip:       "bg-emerald-50 text-emerald-700 border-emerald-200",
    label:      "Excellent",
    icon:       ShieldCheck,
  },
  good: {
    bar:        "bg-amber-500",
    bg:         "bg-amber-50",
    bgSolid:    "bg-amber-500",
    text:       "text-amber-700",
    textStrong: "text-amber-800",
    border:     "border-amber-200",
    ring:       "stroke-amber-500",
    chip:       "bg-amber-50 text-amber-700 border-amber-200",
    label:      "Good",
    icon:       Sparkles,
  },
  warn: {
    bar:        "bg-orange-500",
    bg:         "bg-orange-50",
    bgSolid:    "bg-orange-500",
    text:       "text-orange-700",
    textStrong: "text-orange-800",
    border:     "border-orange-200",
    ring:       "stroke-orange-500",
    chip:       "bg-orange-50 text-orange-700 border-orange-200",
    label:      "Needs Attention",
    icon:       AlertTriangle,
  },
  risk: {
    bar:        "bg-rose-500",
    bg:         "bg-rose-50",
    bgSolid:    "bg-rose-500",
    text:       "text-rose-700",
    textStrong: "text-rose-800",
    border:     "border-rose-200",
    ring:       "stroke-rose-500",
    chip:       "bg-rose-50 text-rose-700 border-rose-200",
    label:      "At Risk",
    icon:       ShieldAlert,
  },
};

const COLOR = (score) => PALETTE[bucket(score)];

// ─────────────────────────────────────────────────────────────────────
// Stage metadata (icon + accent) for the summary strip.
// ─────────────────────────────────────────────────────────────────────
const STAGE_META = {
  Discovery:    { icon: Sparkles, route: "/",             label: "Discovery"    },
  DataModel:    { icon: Database, route: "/data-model",   label: "DataModel"    },
  Architecture: { icon: Layers,   route: "/architecture", label: "Architecture" },
  CodeGen:      { icon: Code2,    route: "/code-gen",     label: "CodeGen"      },
  Living:       { icon: Activity, route: "/living",       label: "Living"       },
};
const STAGE_ORDER = ["Discovery", "DataModel", "Architecture", "CodeGen", "Living"];

// ─────────────────────────────────────────────────────────────────────
// Circular gauge — SVG donut with animated arc.
// ─────────────────────────────────────────────────────────────────────
function CircularGauge({ score, size = 128, stroke = 10 }) {
  const c = COLOR(score);
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  const offset = circ - (pct / 100) * circ;
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2} cy={size / 2} r={r}
          className="stroke-[#F1F1F4]"
          strokeWidth={stroke}
          fill="none"
        />
        <circle
          cx={size / 2} cy={size / 2} r={r}
          className={`${c.ring} transition-[stroke-dashoffset] duration-700 ease-out`}
          strokeWidth={stroke}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={circ}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className={`text-2xl font-display font-bold ${c.textStrong}`}>
          {pct.toFixed(1)}
          <span className="text-sm">%</span>
        </div>
        <div className={`text-[9px] uppercase tracking-wider ${c.text} font-semibold mt-0.5`}>
          {c.label}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Score bar — linear progress (used in section rows).
// ─────────────────────────────────────────────────────────────────────
function ScoreBar({ score, height = "h-1.5" }) {
  const c = COLOR(score);
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  return (
    <div className={`${height} bg-[#F1F1F4] rounded-full overflow-hidden`}>
      <div
        className={`h-full ${c.bar} transition-all duration-500`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Stat pill for the hero KPI row.
// ─────────────────────────────────────────────────────────────────────
function StatPill({ label, value, tone = "neutral" }) {
  const toneCls = {
    neutral:  "bg-[#F6F6FA] text-[#2E2E38]",
    good:     "bg-emerald-50 text-emerald-800 border-emerald-100",
    warn:     "bg-amber-50 text-amber-800 border-amber-100",
    risk:     "bg-rose-50 text-rose-800 border-rose-100",
  }[tone];
  return (
    <div className={`px-3 py-2 rounded-md border border-[#E6E6E6] ${toneCls}`}>
      <div className="text-[9px] uppercase tracking-wider opacity-70 font-semibold">{label}</div>
      <div className="text-base font-display font-bold leading-tight mt-0.5">{value}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Stage summary card — one per pipeline stage in the strip.
// ─────────────────────────────────────────────────────────────────────
function StageCard({ stage, agg, active, onClick }) {
  const meta = STAGE_META[stage] || { icon: Cpu, label: stage };
  const Icon = meta.icon;
  const has = agg && agg.count > 0;
  const c = has ? COLOR(agg.avg) : PALETTE.risk;
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`acc-stage-card-${stage}`}
      className={
        "text-left px-3 py-2.5 rounded-md border transition-all group " +
        (active
          ? "border-[#2E2E38] bg-white shadow-sm"
          : "border-[#E6E6E6] bg-white hover:border-[#B3B3BC] hover:shadow-sm")
      }
    >
      <div className="flex items-center gap-2 mb-1.5">
        <div className={`p-1.5 rounded ${has ? c.bg : "bg-[#F6F6FA]"}`}>
          <Icon className={`w-3.5 h-3.5 ${has ? c.text : "text-[#B3B3BC]"}`} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-[11px] font-semibold text-[#2E2E38] truncate">{meta.label}</div>
          <div className="text-[9px] text-[#747480]">
            {has ? `${agg.count} section${agg.count === 1 ? "" : "s"}` : "no data"}
          </div>
        </div>
      </div>
      {has ? (
        <>
          <div className="flex items-baseline justify-between mb-1">
            <span className={`text-base font-display font-bold ${c.textStrong}`}>
              {agg.avg.toFixed(1)}<span className="text-[10px]">%</span>
            </span>
            {agg.below > 0 && (
              <span className="text-[9px] font-mono text-rose-700 font-bold">
                {agg.below} &lt; 95
              </span>
            )}
          </div>
          <ScoreBar score={agg.avg} height="h-1" />
        </>
      ) : (
        <div className="text-[10px] text-[#B3B3BC] italic">Not scored yet</div>
      )}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Section row — collapsible card with rich details.
// ─────────────────────────────────────────────────────────────────────
function SectionRow({ row, index, onJump }) {
  const [open, setOpen] = useState(false);
  const c = COLOR(row.score);
  const isMissing = row.missing;
  const needsAction = (row.score || 0) < 95;
  const meta = STAGE_META[row.stage] || {};
  const StageIcon = meta.icon || Cpu;
  return (
    <div
      data-testid={`acc-row-${row.key}`}
      className="border border-[#E6E6E6] rounded-md bg-white overflow-hidden transition-shadow hover:shadow-sm"
    >
      <div className="flex">
        {/* Colored side strip — instant visual bucket cue */}
        <div className={`w-1 shrink-0 ${c.bgSolid}`} />
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex-1 flex items-center gap-3 px-3 py-2.5 text-left hover:bg-[#FAFAFC]"
        >
          <span className="w-5 text-center text-[10px] font-mono text-[#B3B3BC] font-semibold shrink-0">
            {String(index + 1).padStart(2, "0")}
          </span>
          <div className={`p-1.5 rounded ${c.bg} shrink-0`}>
            <StageIcon className={`w-3.5 h-3.5 ${c.text}`} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[12px] font-semibold text-[#2E2E38] truncate">
                {row.label}
              </span>
              <span className={`text-[9px] uppercase tracking-wider border ${c.chip} px-1.5 py-[1px] rounded-sm font-semibold`}>
                {row.stage}
              </span>
              {isMissing && (
                <span className="text-[9px] uppercase tracking-wider bg-rose-100 text-rose-800 border border-rose-200 px-1.5 py-[1px] rounded-sm font-semibold flex items-center gap-1">
                  <XCircle className="w-2.5 h-2.5" /> Not generated
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-1.5">
              <div className="flex-1 min-w-0"><ScoreBar score={row.score} /></div>
              <span className={`text-[11px] font-mono font-bold ${c.textStrong} w-14 text-right tabular-nums`}>
                {Number(row.score || 0).toFixed(1)}%
              </span>
            </div>
          </div>
          {needsAction && (
            <span
              role="button"
              data-testid={`acc-jump-${row.key}`}
              onClick={(e) => { e.stopPropagation(); onJump(row); }}
              className="text-[10px] px-2.5 py-1.5 bg-[#2E2E38] text-white font-bold rounded-md hover:bg-[#1F1F26] flex items-center gap-1 shrink-0 transition-colors"
              title={`Open ${row.stage} stage to regenerate this section`}
            >
              <Wand2 className="w-3 h-3" /> Regenerate <ArrowRight className="w-3 h-3" />
            </span>
          )}
          <div className="shrink-0 text-[#B3B3BC] hover:text-[#2E2E38]">
            {open
              ? <ChevronDown className="w-4 h-4" />
              : <ChevronRightIcon className="w-4 h-4" />}
          </div>
        </button>
      </div>
      {open && (
        <div className="border-t border-[#E6E6E6] bg-[#FAFAFC] px-4 py-3 space-y-3" data-testid={`acc-row-detail-${row.key}`}>
          {row.rationale && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-[#747480] font-semibold mb-1 flex items-center gap-1">
                <Gauge className="w-3 h-3" /> Rationale
              </div>
              <div className="text-[11px] text-[#2E2E38] leading-relaxed bg-white border border-[#E6E6E6] rounded p-2">
                {row.rationale}
              </div>
            </div>
          )}
          {(row.gaps || []).length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-[#747480] font-semibold mb-1 flex items-center gap-1">
                <AlertTriangle className="w-3 h-3 text-amber-600" /> Open gaps ({row.gaps.length})
              </div>
              <ul className="text-[11px] text-[#2E2E38] space-y-1">
                {row.gaps.map((g, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 bg-white border border-amber-100 rounded p-2"
                  >
                    <AlertTriangle className="w-3 h-3 text-amber-600 shrink-0 mt-[3px]" />
                    <span className="leading-relaxed">{g}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(row.evidence || []).length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-[#747480] font-semibold mb-1 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3 text-emerald-600" /> Evidence cited ({row.evidence.length})
              </div>
              <div className="flex flex-wrap gap-1">
                {row.evidence.map((e, i) => (
                  <span
                    key={i}
                    className="text-[10px] font-mono bg-white border border-[#E6E6E6] px-2 py-0.5 rounded"
                  >
                    {e}
                  </span>
                ))}
              </div>
            </div>
          )}
          {(row.votes || []).length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-wider text-[#747480] font-semibold mb-1 flex items-center gap-1">
                <Cpu className="w-3 h-3" /> Model votes
                {row.model_agreement_spread != null && (
                  <span className="normal-case font-normal text-[#9CA3AF] ml-1">
                    · agreement spread {row.model_agreement_spread.toFixed?.(1)}
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-1">
                {row.votes.map((v, i) => {
                  const vc = COLOR(v.score);
                  return (
                    <div
                      key={i}
                      className="text-[10px] bg-white border border-[#E6E6E6] rounded px-2 py-1 flex items-center justify-between"
                    >
                      <span className="font-mono text-[#2E2E38] truncate">{v.model}</span>
                      <span className={`font-mono font-bold ${vc.text} ml-2`}>
                        {Number(v.score).toFixed(0)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────
export default function AccuracyReport({ projectId }) {
  const navigate = useNavigate();
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);
  const [job, setJob] = useState(null);
  const pollRef = useRef(null);

  // Filter / search state
  const [query, setQuery] = useState("");
  const [tier, setTier] = useState("all");        // all | attention | risk | missing
  const [stageFilter, setStageFilter] = useState(""); // "" = all
  const [sort, setSort] = useState("score-asc");  // score-asc | score-desc | stage

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const r = await getLatestAccuracyReport(projectId);
      setReport(r);
    } catch {
      setReport(null);
    }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current); }, []);

  const poll = useCallback((jid) => {
    const tick = async () => {
      try {
        const j = await getLivingJob(jid);
        setJob(j);
        if (j.status === "complete") { setLoading(false); await load(); return; }
        if (j.status === "error") { setLoading(false); toast.error("Accuracy report failed", { description: j.error }); return; }
      } catch { /* ignore transient */ }
      pollRef.current = setTimeout(tick, 2000);
    };
    tick();
  }, [load]);

  const onRun = async () => {
    if (!projectId) return;
    setLoading(true);
    setJob({ status: "queued", step: "queued", pct: 0 });
    try {
      const r = await startAccuracyReport(projectId);
      poll(r.job_id);
    } catch (e) {
      setLoading(false);
      toast.error("Could not start accuracy report", { description: e?.response?.data?.detail || e.message });
    }
  };

  const onJump = (row) => {
    const url = `${row.stage_route || "/"}?focus=${encodeURIComponent(row.key)}`;
    navigate(url);
  };

  const overall = report?.overall_score ?? 0;
  const c = COLOR(overall);
  const HeroIcon = c.icon;

  // Per-stage aggregates for the summary strip
  const stageAgg = useMemo(() => {
    const out = {};
    (report?.sections || []).forEach((s) => {
      const st = s.stage || "unknown";
      if (!out[st]) out[st] = { count: 0, sum: 0, below: 0, missing: 0 };
      out[st].count += 1;
      out[st].sum += Number(s.score || 0);
      if ((s.score || 0) < 95) out[st].below += 1;
      if (s.missing) out[st].missing += 1;
    });
    Object.values(out).forEach((a) => { a.avg = a.count ? a.sum / a.count : 0; });
    return out;
  }, [report]);

  // Counters for the hero KPI row
  const kpi = useMemo(() => {
    const sec = report?.sections || [];
    const k = { total: sec.length, excellent: 0, good: 0, warn: 0, risk: 0, missing: 0 };
    sec.forEach((s) => {
      const b = bucket(s.score);
      k[b] += 1;
      if (s.missing) k.missing += 1;
    });
    return k;
  }, [report]);

  // Filtered + sorted section list for the main pane
  const visibleSections = useMemo(() => {
    let list = report?.sections || [];
    if (stageFilter) list = list.filter((s) => s.stage === stageFilter);
    if (tier === "attention") list = list.filter((s) => (s.score || 0) < 95);
    else if (tier === "risk") list = list.filter((s) => (s.score || 0) < 85);
    else if (tier === "missing") list = list.filter((s) => s.missing);
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      list = list.filter((s) =>
        (s.label || "").toLowerCase().includes(q)
        || (s.key || "").toLowerCase().includes(q)
        || (s.stage || "").toLowerCase().includes(q)
      );
    }
    const sorted = [...list];
    if (sort === "score-asc")  sorted.sort((a, b) => (a.score || 0) - (b.score || 0));
    if (sort === "score-desc") sorted.sort((a, b) => (b.score || 0) - (a.score || 0));
    if (sort === "stage") {
      const rank = Object.fromEntries(STAGE_ORDER.map((s, i) => [s, i]));
      sorted.sort((a, b) => (rank[a.stage] ?? 99) - (rank[b.stage] ?? 99));
    }
    return sorted;
  }, [report, tier, stageFilter, query, sort]);

  const below = report?.sections?.filter((s) => (s.score || 0) < 95) || [];

  return (
    <div className="space-y-3" data-testid="accuracy-report">

      {/* ─────────────── Empty state ─────────────── */}
      {!report && !loading && (
        <div className="bg-gradient-to-br from-white via-white to-[#FFFCE0] border border-[#E6E6E6] rounded-lg p-8 text-center">
          <div className="inline-flex p-3 rounded-full bg-[#FFE600]/20 mb-3">
            <Gauge className="w-8 h-8 text-[#2E2E38]" />
          </div>
          <div className="text-sm font-display font-bold text-[#2E2E38] mb-1">
            KB vs Generated · Accuracy &amp; Confidence
          </div>
          <div className="text-[11px] text-[#747480] max-w-md mx-auto mb-4 leading-relaxed">
            Score every stage's latest artifacts against the Knowledge Base with a
            multi-model jury. Sections below 95% expose a one-click regenerate
            deep-link so you can close the gap immediately.
          </div>
          <Button
            onClick={onRun}
            data-testid="btn-run-accuracy-report"
            className="h-9 bg-[#FFE600] text-[#2E2E38] hover:bg-[#FFD500] font-bold"
          >
            <Wand2 className="w-3.5 h-3.5 mr-1.5" />
            Run accuracy report
          </Button>
        </div>
      )}

      {/* ─────────────── Progress while running ─────────────── */}
      {loading && (
        <div
          className="bg-white border border-[#E6E6E6] rounded-lg p-4"
          data-testid="acc-progress"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-full bg-[#FFE600]/20">
              <Loader2 className="w-5 h-5 text-[#2E2E38] animate-spin" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[12px] font-semibold text-[#2E2E38] truncate">
                {job?.step || "Working…"}
              </div>
              <div className="text-[10px] text-[#747480]">
                Scoring every stage against the Knowledge Base. This typically
                takes 30–90 seconds.
              </div>
            </div>
            <div className="text-2xl font-display font-bold text-[#2E2E38] tabular-nums shrink-0">
              {job?.pct || 0}<span className="text-sm">%</span>
            </div>
          </div>
          <div className="h-1.5 bg-[#F6F6FA] rounded-full overflow-hidden mt-3">
            <div
              className="h-full bg-gradient-to-r from-[#FFE600] to-[#FFD500] transition-all duration-500"
              style={{ width: `${job?.pct || 0}%` }}
            />
          </div>
        </div>
      )}

      {/* ─────────────── Report ─────────────── */}
      {report && (
        <>
          {/* Hero — big gauge + KPI grid */}
          <div
            className={`border ${c.border} rounded-lg overflow-hidden bg-white`}
            data-testid="acc-hero"
          >
            <div className={`${c.bg} px-4 py-3 border-b ${c.border} flex items-center justify-between`}>
              <div className="flex items-center gap-2">
                <HeroIcon className={`w-4 h-4 ${c.textStrong}`} />
                <span className={`text-[12px] font-display font-bold ${c.textStrong}`}>
                  KB vs Generated · Accuracy &amp; Confidence
                </span>
              </div>
              <Button
                onClick={onRun}
                disabled={loading}
                data-testid="btn-run-accuracy-report"
                className="h-7 text-[11px] bg-white text-[#2E2E38] hover:bg-[#F6F6FA] border border-[#E6E6E6]"
                variant="ghost"
              >
                {loading
                  ? <Loader2 className="w-3 h-3 animate-spin mr-1" />
                  : <Wand2 className="w-3 h-3 mr-1" />}
                Re-run
              </Button>
            </div>

            <div className="p-4 flex flex-col sm:flex-row items-start gap-4">
              {/* Circular gauge */}
              <div className="shrink-0" data-testid="acc-overall-gauge">
                <CircularGauge score={overall} />
                <div className="sr-only" data-testid="acc-overall-score">
                  {overall.toFixed(1)}%
                </div>
              </div>

              {/* KPI grid */}
              <div className="flex-1 min-w-0 w-full">
                <div className="text-[10px] uppercase tracking-wider text-[#747480] font-semibold mb-2 flex items-center gap-1">
                  <BarChart3 className="w-3 h-3" /> Coverage breakdown
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                  <StatPill label="Total sections" value={kpi.total} />
                  <StatPill label="≥ 95%"          value={kpi.excellent} tone="good" />
                  <StatPill label="85–94%"         value={kpi.good}      tone="warn" />
                  <StatPill label="70–84%"         value={kpi.warn}      tone="warn" />
                  <StatPill label="< 70% / missing"
                    value={kpi.risk + kpi.missing}
                    tone={kpi.risk + kpi.missing > 0 ? "risk" : "neutral"} />
                </div>

                {/* Verdict + provenance */}
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
                  {overall >= 95 ? (
                    <span className="flex items-center gap-1 text-emerald-700 font-bold">
                      <CheckCircle2 className="w-3.5 h-3.5" /> Ready to freeze with minimal review
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-amber-800 font-bold">
                      <AlertTriangle className="w-3.5 h-3.5" /> {below.length} section{below.length === 1 ? "" : "s"} need attention
                    </span>
                  )}
                  <span className="text-[#747480] flex items-center gap-1">
                    <Cpu className="w-3 h-3" />
                    {(report.models_used || []).length > 0
                      ? `${(report.models_used || []).length} model${(report.models_used || []).length === 1 ? "" : "s"}`
                      : "default jury"}
                    {(report.models_used || []).length > 0 && (
                      <span
                        className="font-mono text-[10px] text-[#B3B3BC] truncate max-w-[220px]"
                        title={(report.models_used || []).join(", ")}
                      >
                        ({(report.models_used || []).join(", ")})
                      </span>
                    )}
                  </span>
                  <span className="text-[#747480] flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {new Date(report.generated_at).toLocaleString()}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* ─────────────── Stage summary strip ─────────────── */}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[#747480] font-semibold mb-1.5 flex items-center gap-1 px-1">
              <TrendingUp className="w-3 h-3" /> Stage health · click to filter
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
              {STAGE_ORDER.map((st) => (
                <StageCard
                  key={st}
                  stage={st}
                  agg={stageAgg[st]}
                  active={stageFilter === st}
                  onClick={() => setStageFilter(stageFilter === st ? "" : st)}
                />
              ))}
            </div>
          </div>

          {/* ─────────────── Filter bar ─────────────── */}
          <div className="bg-white border border-[#E6E6E6] rounded-md px-3 py-2 flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[180px]">
              <Search className="w-3.5 h-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-[#B3B3BC]" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search sections (label, key, stage)…"
                data-testid="acc-search"
                className="w-full text-[11px] pl-7 pr-2 py-1.5 border border-[#E6E6E6] rounded focus:outline-none focus:border-[#2E2E38] focus:ring-1 focus:ring-[#FFE600]"
              />
            </div>
            <div className="flex items-center gap-1" data-testid="acc-tier-filter">
              <Filter className="w-3 h-3 text-[#747480]" />
              {[
                { k: "all",       label: "All",              n: kpi.total },
                { k: "attention", label: "Needs attention",  n: kpi.good + kpi.warn + kpi.risk },
                { k: "risk",      label: "At risk",          n: kpi.warn + kpi.risk },
                { k: "missing",   label: "Missing",          n: kpi.missing },
              ].map((f) => (
                <button
                  key={f.k}
                  type="button"
                  onClick={() => setTier(f.k)}
                  data-testid={`acc-tier-${f.k}`}
                  className={
                    "text-[10px] px-2 py-1 rounded border transition-colors flex items-center gap-1 " +
                    (tier === f.k
                      ? "bg-[#2E2E38] text-white border-[#2E2E38]"
                      : "bg-white text-[#2E2E38] border-[#E6E6E6] hover:bg-[#F6F6FA]")
                  }
                >
                  {f.label}
                  <span className={
                    "font-mono text-[9px] px-1 rounded " +
                    (tier === f.k ? "bg-white/20" : "bg-[#F6F6FA] text-[#747480]")
                  }>
                    {f.n}
                  </span>
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 ml-auto">
              <span className="text-[9px] text-[#747480] uppercase">Sort</span>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value)}
                data-testid="acc-sort"
                className="text-[10px] border border-[#E6E6E6] rounded px-1.5 py-1 bg-white focus:outline-none focus:border-[#2E2E38]"
              >
                <option value="score-asc">Score ↑ (worst first)</option>
                <option value="score-desc">Score ↓ (best first)</option>
                <option value="stage">By stage</option>
              </select>
            </div>
          </div>

          {/* Applied-filter chip strip */}
          {(stageFilter || tier !== "all" || query) && (
            <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-[#747480] px-1">
              <span>Filters:</span>
              {stageFilter && (
                <span className="bg-[#F6F6FA] border border-[#E6E6E6] px-1.5 py-0.5 rounded flex items-center gap-1">
                  stage = <b className="text-[#2E2E38]">{stageFilter}</b>
                  <button onClick={() => setStageFilter("")} className="text-[#B3B3BC] hover:text-rose-600">×</button>
                </span>
              )}
              {tier !== "all" && (
                <span className="bg-[#F6F6FA] border border-[#E6E6E6] px-1.5 py-0.5 rounded flex items-center gap-1">
                  tier = <b className="text-[#2E2E38]">{tier}</b>
                  <button onClick={() => setTier("all")} className="text-[#B3B3BC] hover:text-rose-600">×</button>
                </span>
              )}
              {query && (
                <span className="bg-[#F6F6FA] border border-[#E6E6E6] px-1.5 py-0.5 rounded flex items-center gap-1">
                  query = <b className="text-[#2E2E38]">{query}</b>
                  <button onClick={() => setQuery("")} className="text-[#B3B3BC] hover:text-rose-600">×</button>
                </span>
              )}
              <span className="ml-auto">
                Showing <b className="text-[#2E2E38]">{visibleSections.length}</b> of {report.sections?.length || 0}
              </span>
            </div>
          )}

          {/* ─────────────── Per-section list ─────────────── */}
          <div className="space-y-2">
            {visibleSections.length === 0 ? (
              <div className="bg-white border border-dashed border-[#E6E6E6] rounded-md p-6 text-center text-[11px] text-[#747480]">
                <CheckCircle2 className="w-6 h-6 mx-auto mb-2 text-emerald-500" />
                No sections match the current filter.
                {(stageFilter || tier !== "all" || query) && (
                  <button
                    onClick={() => { setStageFilter(""); setTier("all"); setQuery(""); }}
                    className="ml-2 underline hover:text-[#2E2E38]"
                  >
                    Clear filters
                  </button>
                )}
              </div>
            ) : (
              visibleSections.map((s, i) => (
                <SectionRow key={s.key} row={s} index={i} onJump={onJump} />
              ))
            )}
          </div>
        </>
      )}
    </div>
  );
}
