import React, { useEffect, useRef, useState } from "react";
import * as d3 from "d3";

export default function ERDiagram({ data, height = 500 }) {
  const svgRef = useRef(null);
  const [selected, setSelected] = useState(null);
  const [search, setSearch] = useState("");
  const [showLogs, setShowLogs] = useState(false);

  useEffect(() => {
    if (!data || !svgRef.current) return;
    const { nodes, edges } = data;

    const visibleNodes = showLogs
      ? nodes
      : nodes.filter((n) => !n.name.startsWith("logs_"));
    const visibleIds = new Set(visibleNodes.map((n) => n.id));
    const visibleEdges = edges.filter(
      (e) => visibleIds.has(e.from_table) && visibleIds.has(e.to_table)
    );

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const width = svgRef.current.clientWidth || 800;
    const g = svg.append("g");

    svg.call(
      d3
        .zoom()
        .scaleExtent([0.1, 3])
        .on("zoom", (ev) => g.attr("transform", ev.transform))
    );

    const domains = [...new Set(visibleNodes.map((n) => n.domain))];
    const domainColour = d3
      .scaleOrdinal()
      .domain(domains)
      .range([
        "#FFE600",
        "#2E2E38",
        "#747480",
        "#168736",
        "#0066CC",
        "#FF6D00",
        "#9B59B6",
        "#E74C3C",
      ]);

    const linkData = visibleEdges.map((e) => ({
      ...e,
      source: e.from_table,
      target: e.to_table,
    }));

    const simulation = d3
      .forceSimulation(visibleNodes)
      .force("link", d3.forceLink(linkData).id((d) => d.id).distance(180))
      .force("charge", d3.forceManyBody().strength(-400))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide(90));

    svg
      .append("defs")
      .append("marker")
      .attr("id", "arrow")
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 15)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-5L10,0L0,5")
      .attr("fill", "#747480");

    const link = g
      .append("g")
      .selectAll("line")
      .data(linkData)
      .join("line")
      .attr("stroke", "#E6E6E6")
      .attr("stroke-width", 1.5)
      .attr("marker-end", "url(#arrow)");

    const NODE_W = 160;
    const HEADER_H = 28;
    const ROW_H = 20;
    const MAX_COLS = 5;

    const nodeGroup = g
      .append("g")
      .selectAll("g")
      .data(visibleNodes)
      .join("g")
      .attr("cursor", "pointer")
      .call(
        d3
          .drag()
          .on("start", (ev, d) => {
            if (!ev.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (ev, d) => {
            d.fx = ev.x;
            d.fy = ev.y;
          })
          .on("end", (ev, d) => {
            if (!ev.active) simulation.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      )
      .on("click", (_ev, d) => setSelected(d));

    nodeGroup.each(function (d) {
      const el = d3.select(this);
      const visibleCols = (d.columns || []).slice(0, MAX_COLS);
      const boxH =
        HEADER_H +
        visibleCols.length * ROW_H +
        (d.columns?.length > MAX_COLS ? ROW_H : 0);
      const isMatch =
        search && d.name.toLowerCase().includes(search.toLowerCase());
      const dColour = domainColour(d.domain);

      el.append("rect")
        .attr("width", NODE_W)
        .attr("height", boxH)
        .attr("x", -NODE_W / 2)
        .attr("y", -HEADER_H / 2)
        .attr("rx", 3)
        .attr("fill", "white")
        .attr("stroke", isMatch ? "#FFE600" : "#E6E6E6")
        .attr("stroke-width", isMatch ? 3 : 1)
        .attr("filter", "drop-shadow(0 1px 3px rgba(0,0,0,0.1))");

      el.append("rect")
        .attr("width", 4)
        .attr("height", boxH)
        .attr("x", -NODE_W / 2)
        .attr("y", -HEADER_H / 2)
        .attr("rx", 3)
        .attr("fill", dColour);

      el.append("rect")
        .attr("width", NODE_W - 4)
        .attr("height", HEADER_H)
        .attr("x", -NODE_W / 2 + 4)
        .attr("y", -HEADER_H / 2)
        .attr("fill", "#2E2E38");

      el.append("text")
        .attr("x", -NODE_W / 2 + 10)
        .attr("y", 0)
        .attr("dominant-baseline", "middle")
        .attr("fill", "white")
        .attr("font-size", "10px")
        .attr("font-weight", "bold")
        .attr("font-family", "monospace")
        .text(d.name.length > 18 ? d.name.slice(0, 16) + "…" : d.name);

      visibleCols.forEach((col, i) => {
        const cy = HEADER_H / 2 + i * ROW_H + ROW_H / 2;
        if (col.is_pk) {
          el.append("text")
            .attr("x", -NODE_W / 2 + 8)
            .attr("y", cy)
            .attr("dominant-baseline", "middle")
            .attr("fill", "#FFE600")
            .attr("font-size", "8px")
            .text("PK");
        } else if (col.is_fk) {
          el.append("text")
            .attr("x", -NODE_W / 2 + 8)
            .attr("y", cy)
            .attr("dominant-baseline", "middle")
            .attr("fill", "#0066CC")
            .attr("font-size", "8px")
            .text("FK");
        }
        el.append("text")
          .attr("x", -NODE_W / 2 + 24)
          .attr("y", cy)
          .attr("dominant-baseline", "middle")
          .attr("fill", "#2E2E38")
          .attr("font-size", "9px")
          .text(col.name.length > 14 ? col.name.slice(0, 12) + "…" : col.name);
        el.append("text")
          .attr("x", NODE_W / 2 - 6)
          .attr("y", cy)
          .attr("dominant-baseline", "middle")
          .attr("text-anchor", "end")
          .attr("fill", "#747480")
          .attr("font-size", "8px")
          .text((col.type || "").split("(")[0].slice(0, 8));
      });

      if (d.columns?.length > MAX_COLS) {
        const moreY = HEADER_H / 2 + MAX_COLS * ROW_H + ROW_H / 2;
        el.append("text")
          .attr("x", 0)
          .attr("y", moreY)
          .attr("dominant-baseline", "middle")
          .attr("text-anchor", "middle")
          .attr("fill", "#747480")
          .attr("font-size", "9px")
          .text(`+${d.columns.length - MAX_COLS} more columns`);
      }
    });

    simulation.on("tick", () => {
      link
        .attr("x1", (d) => d.source.x)
        .attr("y1", (d) => d.source.y)
        .attr("x2", (d) => d.target.x)
        .attr("y2", (d) => d.target.y);
      nodeGroup.attr("transform", (d) => `translate(${d.x},${d.y})`);
    });

    return () => simulation.stop();
  }, [data, search, showLogs, height]);

  if (!data) {
    return (
      <div
        data-testid="er-diagram-empty"
        className="flex items-center justify-center h-48 text-sm text-[#747480]"
      >
        No entity data yet — generate SRS first.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" data-testid="er-diagram">
      <div className="flex items-center gap-3 px-3 py-2 border-b border-[#E6E6E6] bg-white flex-shrink-0">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search tables…"
          data-testid="er-search-input"
          className="text-xs border border-[#E6E6E6] rounded-sm px-2 py-1 w-40 focus:border-[#2E2E38] outline-none"
        />
        <label className="flex items-center gap-1 text-xs text-[#747480]">
          <input
            type="checkbox"
            checked={showLogs}
            onChange={(e) => setShowLogs(e.target.checked)}
            data-testid="er-show-logs"
          />
          Show logs tables
        </label>
        <span
          className="text-xs text-[#747480] ml-auto"
          data-testid="er-stats"
        >
          {data.stats?.total_tables} tables · {data.stats?.total_relationships} relationships · {data.stats?.domains} domains
        </span>
      </div>
      <svg
        ref={svgRef}
        className="flex-1 w-full bg-[#F6F6FA]"
        style={{ minHeight: height }}
      />
      {selected && (
        <div
          className="border-t border-[#E6E6E6] bg-white p-3 max-h-48 overflow-y-auto flex-shrink-0"
          data-testid="er-selected-detail"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="font-bold text-sm text-[#2E2E38] font-mono">
              {selected.name}
            </span>
            <button
              onClick={() => setSelected(null)}
              data-testid="er-close-detail"
              className="text-xs text-[#747480] hover:text-[#2E2E38]"
            >
              ✕ close
            </button>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-[#2E2E38] text-white">
                <th className="px-2 py-1 text-left">Column</th>
                <th className="px-2 py-1 text-left">Type</th>
                <th className="px-2 py-1 text-left">Key</th>
              </tr>
            </thead>
            <tbody>
              {(selected.columns || []).map((c) => (
                <tr
                  key={c.name}
                  className="border-b border-[#E6E6E6] even:bg-[#F6F6FA]"
                >
                  <td className="px-2 py-1 font-mono">{c.name}</td>
                  <td className="px-2 py-1 text-[#747480]">{c.type}</td>
                  <td className="px-2 py-1">
                    {c.is_pk && (
                      <span className="bg-[#FFE600] text-[#2E2E38] px-1 rounded text-[10px] font-bold">
                        PK
                      </span>
                    )}
                    {c.is_fk && (
                      <span className="bg-[#0066CC] text-white px-1 rounded text-[10px] ml-1">
                        FK
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
