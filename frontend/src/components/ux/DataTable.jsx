import { useState, useMemo, useRef, useCallback, useEffect } from "react";
import {
  ChevronDown,
  ChevronUp,
  ArrowUpDown,
  Search,
  Download,
  Columns3,
  X,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { SkeletonTable } from "@/components/ui/skeleton";

/**
 * DataTable.
 *
 * The previous version had sort, search, selection and export but only one
 * consumer, while thirteen other files hand-rolled raw <table> markup. The
 * four things those files needed and this did not have are added here, so
 * migrating onto it is an upgrade rather than a lateral move:
 *
 *   • pagination            — with an "all rows" mode
 *   • virtualization        — for the multi-thousand-row cases (generated
 *                             file lists, gap matrices, audit entries)
 *   • real sticky headers   — the old thead was `sticky top-0` inside a
 *                             non-scrolling parent, so it never stuck
 *   • column visibility     — wide tables are unreadable without it
 *
 * Accessibility: sortable headers are buttons carrying aria-sort, the
 * select-all checkbox reflects the indeterminate state, row selection is
 * announced, and the whole table is labelled.
 */
export default function DataTable({
  data = [],
  columns = [],
  onRowClick,
  selectable = false,
  searchable = true,
  sortable = true,
  exportable = false,
  onExport,
  loading = false,
  emptyMessage = "No data available",
  emptyAction = null,
  className = "",
  density = "normal", // "compact" | "normal" | "comfortable"
  pageSize: initialPageSize = 50,
  paginate = true,
  virtualize = "auto", // true | false | "auto" (auto = on above 200 rows)
  label = "Data table",
  getRowId,
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const [sortConfig, setSortConfig] = useState({ key: null, direction: "asc" });
  const [selectedRows, setSelectedRows] = useState(() => new Set());
  const [hiddenCols, setHiddenCols] = useState(() => new Set());
  const [colMenuOpen, setColMenuOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(initialPageSize);

  const scrollRef = useRef(null);
  const selectAllRef = useRef(null);

  const visibleColumns = useMemo(
    () => columns.filter((c) => !hiddenCols.has(c.key)),
    [columns, hiddenCols]
  );

  // ---- filter ---------------------------------------------------------
  const filteredData = useMemo(() => {
    if (!searchQuery.trim()) return data;
    const q = searchQuery.toLowerCase();
    return data.filter((row) =>
      columns.some((col) => {
        const v = row[col.key];
        return v != null && String(v).toLowerCase().includes(q);
      })
    );
  }, [data, columns, searchQuery]);

  // ---- sort -----------------------------------------------------------
  const sortedData = useMemo(() => {
    if (!sortConfig.key) return filteredData;
    return [...filteredData].sort((a, b) => {
      const av = a[sortConfig.key];
      const bv = b[sortConfig.key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      let cmp;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortConfig.direction === "asc" ? cmp : -cmp;
    });
  }, [filteredData, sortConfig]);

  // ---- paginate -------------------------------------------------------
  const showAll = pageSize === 0;
  const pageCount = showAll ? 1 : Math.max(1, Math.ceil(sortedData.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);

  const pageRows = useMemo(() => {
    if (!paginate || showAll) return sortedData;
    const start = safePage * pageSize;
    return sortedData.slice(start, start + pageSize);
  }, [sortedData, paginate, showAll, safePage, pageSize]);

  // Filtering that shortens the list must not strand the user on page 9.
  useEffect(() => setPage(0), [searchQuery, pageSize]);

  // ---- virtualize -----------------------------------------------------
  const rowPx = { compact: 32, normal: 40, comfortable: 48 }[density] ?? 40;
  const shouldVirtualize =
    virtualize === true || (virtualize === "auto" && pageRows.length > 200);

  const virtualizer = useVirtualizer({
    count: shouldVirtualize ? pageRows.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowPx,
    overscan: 12,
  });

  // ---- selection ------------------------------------------------------
  const rowKey = useCallback(
    (row, i) => (getRowId ? getRowId(row) : `${safePage}:${i}`),
    [getRowId, safePage]
  );

  const allOnPageSelected =
    pageRows.length > 0 && pageRows.every((r, i) => selectedRows.has(rowKey(r, i)));
  const someOnPageSelected =
    pageRows.some((r, i) => selectedRows.has(rowKey(r, i))) && !allOnPageSelected;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someOnPageSelected;
  }, [someOnPageSelected]);

  const toggleAll = () => {
    setSelectedRows((prev) => {
      const next = new Set(prev);
      pageRows.forEach((r, i) => {
        const k = rowKey(r, i);
        if (allOnPageSelected) next.delete(k);
        else next.add(k);
      });
      return next;
    });
  };

  const toggleRow = (k) =>
    setSelectedRows((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  // ---- sorting handler -------------------------------------------------
  const handleSort = (key) => {
    if (!sortable) return;
    setSortConfig((prev) =>
      prev.key === key
        ? { key, direction: prev.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "asc" }
    );
  };

  const ariaSort = (key) =>
    sortConfig.key === key
      ? sortConfig.direction === "asc"
        ? "ascending"
        : "descending"
      : "none";

  const cellPad = {
    compact: "px-2 py-1 text-micro",
    normal: "px-3 py-2 text-sm",
    comfortable: "px-4 py-3 text-sm",
  }[density];

  const colSpan = visibleColumns.length + (selectable ? 1 : 0);

  if (loading) {
    return (
      <div className={cn("flex flex-col", className)} aria-busy="true">
        <SkeletonTable rows={8} cols={Math.max(2, visibleColumns.length)} />
      </div>
    );
  }

  const renderRow = (row, i, style) => {
    const k = rowKey(row, i);
    const isSelected = selectedRows.has(k);
    return (
      <tr
        key={k}
        style={style}
        onClick={() => onRowClick?.(row, i)}
        aria-selected={selectable ? isSelected : undefined}
        className={cn(
          "border-b border-border",
          onRowClick && "cursor-pointer",
          isSelected ? "bg-brand-tint" : "hover:bg-surface-2",
          "transition-colors duration-fast ease"
        )}
      >
        {selectable && (
          <td className={cellPad}>
            <input
              type="checkbox"
              checked={isSelected}
              onClick={(e) => e.stopPropagation()}
              onChange={() => toggleRow(k)}
              aria-label={`Select row ${i + 1}`}
              className="size-4 accent-brand cursor-pointer"
            />
          </td>
        )}
        {visibleColumns.map((col) => (
          <td
            key={col.key}
            className={cn(cellPad, "text-fg", col.numeric && "tabular-nums text-right")}
          >
            {col.render ? col.render(row[col.key], row, i) : row[col.key]}
          </td>
        ))}
      </tr>
    );
  };

  return (
    <div
      className={cn(
        "flex flex-col min-h-0 bg-surface border border-border rounded overflow-hidden",
        className
      )}
    >
      {/* ---- toolbar ---- */}
      {(searchable || exportable || columns.length > 4) && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface-2 flex-wrap">
          {searchable && (
            <div className="flex-1 min-w-[180px] max-w-sm relative">
              <Search
                className="absolute left-2.5 top-1/2 -translate-y-1/2 size-4 text-fg-subtle pointer-events-none"
                aria-hidden
              />
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Filter rows…"
                aria-label={`Filter ${label}`}
                className="w-full pl-8 pr-8 py-1.5 text-sm rounded bg-surface text-fg border border-border-strong placeholder:text-fg-subtle focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery("")}
                  aria-label="Clear filter"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 size-5 grid place-items-center rounded hover:bg-surface-3 text-fg-subtle"
                >
                  <X className="size-3" aria-hidden />
                </button>
              )}
            </div>
          )}

          <div className="flex items-center gap-2 ml-auto">
            {selectedRows.size > 0 && (
              <span className="text-xs text-fg-muted font-medium tabular-nums" role="status">
                {selectedRows.size} selected
              </span>
            )}

            {columns.length > 4 && (
              <div className="relative">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setColMenuOpen((o) => !o)}
                  aria-expanded={colMenuOpen}
                  aria-haspopup="true"
                >
                  <Columns3 className="size-3.5" aria-hidden />
                  <span className="hidden sm:inline">Columns</span>
                </Button>
                {colMenuOpen && (
                  <>
                    <button
                      type="button"
                      className="fixed inset-0 z-40 cursor-default"
                      aria-label="Close column menu"
                      onClick={() => setColMenuOpen(false)}
                    />
                    <div className="absolute right-0 top-full mt-1 z-50 w-52 max-h-72 overflow-y-auto mos-scroll rounded border border-border bg-surface shadow-overlay p-1">
                      {columns.map((c) => (
                        <label
                          key={c.key}
                          className="flex items-center gap-2 px-2 py-1.5 text-sm text-fg rounded hover:bg-surface-2 cursor-pointer"
                        >
                          <input
                            type="checkbox"
                            className="size-4 accent-brand"
                            checked={!hiddenCols.has(c.key)}
                            onChange={() =>
                              setHiddenCols((prev) => {
                                const next = new Set(prev);
                                if (next.has(c.key)) next.delete(c.key);
                                // Never hide the last visible column.
                                else if (visibleColumns.length > 1) next.add(c.key);
                                return next;
                              })
                            }
                          />
                          <span className="truncate">{c.label}</span>
                        </label>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}

            {exportable && (
              <Button size="sm" variant="ghost" onClick={onExport}>
                <Download className="size-3.5" aria-hidden />
                <span className="hidden sm:inline">Export</span>
              </Button>
            )}
          </div>
        </div>
      )}

      {/* ---- table ---- */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-auto mos-scroll">
        <table className="w-full border-collapse" aria-label={label}>
          <thead>
            <tr>
              {selectable && (
                <th
                  scope="col"
                  className="w-10 px-3 py-2 text-left sticky top-0 z-10 bg-surface-2 border-b border-border"
                >
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    checked={allOnPageSelected}
                    onChange={toggleAll}
                    aria-label="Select all rows on this page"
                    className="size-4 accent-brand cursor-pointer"
                  />
                </th>
              )}
              {visibleColumns.map((col) => {
                const canSort = sortable && col.sortable !== false;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    aria-sort={canSort ? ariaSort(col.key) : undefined}
                    style={{ width: col.width }}
                    className={cn(
                      "text-left text-micro font-semibold text-fg-subtle uppercase tracking-wider",
                      "sticky top-0 z-10 bg-surface-2 border-b border-border",
                      col.numeric && "text-right"
                    )}
                  >
                    {canSort ? (
                      <button
                        type="button"
                        onClick={() => handleSort(col.key)}
                        className={cn(
                          "w-full flex items-center gap-1.5 px-3 py-2",
                          "hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:-ring-offset-2",
                          col.numeric && "justify-end"
                        )}
                      >
                        <span>{col.label}</span>
                        {sortConfig.key === col.key ? (
                          sortConfig.direction === "asc" ? (
                            <ChevronUp className="size-3.5 text-fg" aria-hidden />
                          ) : (
                            <ChevronDown className="size-3.5 text-fg" aria-hidden />
                          )
                        ) : (
                          <ArrowUpDown className="size-3.5 text-fg-subtle" aria-hidden />
                        )}
                      </button>
                    ) : (
                      <span className="block px-3 py-2">{col.label}</span>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>

          <tbody>
            {pageRows.length === 0 ? (
              <tr>
                <td colSpan={colSpan} className="px-3 py-10 text-center">
                  <p className="text-sm text-fg-muted">
                    {searchQuery ? `No rows match “${searchQuery}”` : emptyMessage}
                  </p>
                  {searchQuery ? (
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-3"
                      onClick={() => setSearchQuery("")}
                    >
                      Clear filter
                    </Button>
                  ) : (
                    emptyAction
                  )}
                </td>
              </tr>
            ) : shouldVirtualize ? (
              <>
                {virtualizer.getVirtualItems()[0]?.start > 0 && (
                  <tr style={{ height: virtualizer.getVirtualItems()[0].start }} aria-hidden>
                    <td colSpan={colSpan} />
                  </tr>
                )}
                {virtualizer
                  .getVirtualItems()
                  .map((v) => renderRow(pageRows[v.index], v.index, { height: rowPx }))}
                <tr
                  aria-hidden
                  style={{
                    height:
                      virtualizer.getTotalSize() -
                      (virtualizer.getVirtualItems().at(-1)?.end ?? 0),
                  }}
                >
                  <td colSpan={colSpan} />
                </tr>
              </>
            ) : (
              pageRows.map((row, i) => renderRow(row, i))
            )}
          </tbody>
        </table>
      </div>

      {/* ---- footer ---- */}
      <div className="flex items-center gap-3 px-3 py-2 border-t border-border bg-surface-2 text-micro text-fg-muted flex-wrap">
        <span className="tabular-nums" role="status">
          {filteredData.length === data.length
            ? `${data.length} row${data.length === 1 ? "" : "s"}`
            : `${filteredData.length} of ${data.length} rows`}
          {shouldVirtualize && " · virtualized"}
        </span>

        {paginate && !showAll && pageCount > 1 && (
          <div className="flex items-center gap-1 ml-auto">
            <Button
              size="xs"
              variant="ghost"
              disabled={safePage === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              aria-label="Previous page"
            >
              <ChevronLeft className="size-3.5" aria-hidden />
            </Button>
            <span className="tabular-nums px-1">
              Page {safePage + 1} of {pageCount}
            </span>
            <Button
              size="xs"
              variant="ghost"
              disabled={safePage >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              aria-label="Next page"
            >
              <ChevronRight className="size-3.5" aria-hidden />
            </Button>
          </div>
        )}

        {paginate && (
          <label className="flex items-center gap-1.5 ml-auto">
            <span className="sr-only">Rows per page</span>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              className="bg-surface border border-border rounded px-1.5 py-0.5 text-micro text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {[25, 50, 100, 250].map((n) => (
                <option key={n} value={n}>
                  {n} / page
                </option>
              ))}
              <option value={0}>All</option>
            </select>
          </label>
        )}
      </div>
    </div>
  );
}
