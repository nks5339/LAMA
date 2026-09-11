import React, { useState, useMemo } from "react";
import { ChevronDown, ChevronUp, ArrowUpDown, Search, Filter, Download, RefreshCw } from "lucide-react";

/**
 * Professional DataTable component
 * Similar to VS Code / IntelliJ table views
 * 
 * Features:
 * - Sortable columns
 * - Search/filter
 * - Row selection
 * - Pagination
 * - Export
 * - Compact density
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
  className = "",
  density = "normal" // "compact" | "normal" | "comfortable"
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const [sortConfig, setSortConfig] = useState({ key: null, direction: "asc" });
  const [selectedRows, setSelectedRows] = useState(new Set());

  // Filter data based on search
  const filteredData = useMemo(() => {
    if (!searchQuery.trim()) return data;
    
    const query = searchQuery.toLowerCase();
    return data.filter(row => 
      columns.some(col => {
        const value = row[col.key];
        return value && String(value).toLowerCase().includes(query);
      })
    );
  }, [data, searchQuery, columns]);

  // Sort data
  const sortedData = useMemo(() => {
    if (!sortConfig.key) return filteredData;
    
    return [...filteredData].sort((a, b) => {
      const aVal = a[sortConfig.key];
      const bVal = b[sortConfig.key];
      
      if (aVal === bVal) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      
      const comparison = aVal < bVal ? -1 : 1;
      return sortConfig.direction === "asc" ? comparison : -comparison;
    });
  }, [filteredData, sortConfig]);

  const handleSort = (key) => {
    if (!sortable) return;
    
    setSortConfig(prev => ({
      key,
      direction: prev.key === key && prev.direction === "asc" ? "desc" : "asc"
    }));
  };

  const handleSelectAll = () => {
    if (selectedRows.size === sortedData.length) {
      setSelectedRows(new Set());
    } else {
      setSelectedRows(new Set(sortedData.map((_, idx) => idx)));
    }
  };

  const handleSelectRow = (idx) => {
    const newSelected = new Set(selectedRows);
    if (newSelected.has(idx)) {
      newSelected.delete(idx);
    } else {
      newSelected.add(idx);
    }
    setSelectedRows(newSelected);
  };

  const densityClasses = {
    compact: "text-xs",
    normal: "text-sm",
    comfortable: "text-base"
  };

  const rowHeightClasses = {
    compact: "h-8",
    normal: "h-10",
    comfortable: "h-12"
  };

  return (
    <div className={`flex flex-col bg-white border border-[#E6E6E6] rounded ${className}`}>
      {/* Toolbar */}
      {(searchable || exportable) && (
        <div className="flex items-center justify-between gap-3 px-3 py-2 border-b border-[#E6E6E6] bg-[#F9FAFB]">
          {searchable && (
            <div className="flex-1 max-w-sm relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[#9CA3AF]" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search..."
                className="w-full pl-8 pr-3 py-1.5 text-sm border border-[#D1D5DB] rounded focus:outline-none focus:ring-2 focus:ring-[#FFE600] focus:border-[#FFE600]"
              />
            </div>
          )}
          
          <div className="flex items-center gap-2">
            {selectedRows.size > 0 && (
              <span className="text-xs text-[#6B7280] font-medium">
                {selectedRows.size} selected
              </span>
            )}
            {exportable && (
              <button
                onClick={onExport}
                className="px-2 py-1.5 text-xs font-medium text-[#6B7280] hover:text-[#2E2E38] hover:bg-white rounded border border-transparent hover:border-[#D1D5DB] transition-colors flex items-center gap-1"
                title="Export"
              >
                <Download className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">Export</span>
              </button>
            )}
          </div>
        </div>
      )}

      {/* Table */}
      <div className="flex-1 overflow-auto mos-scroll">
        <table className="w-full border-collapse">
          <thead className="sticky top-0 bg-[#F9FAFB] border-b border-[#E6E6E6] z-10">
            <tr>
              {selectable && (
                <th className="w-10 px-3 py-2 text-left">
                  <input
                    type="checkbox"
                    checked={selectedRows.size === sortedData.length && sortedData.length > 0}
                    onChange={handleSelectAll}
                    className="w-4 h-4 accent-[#FFE600] cursor-pointer"
                  />
                </th>
              )}
              {columns.map((col) => (
                <th
                  key={col.key}
                  onClick={() => col.sortable !== false && handleSort(col.key)}
                  className={`px-3 py-2 text-left text-xs font-semibold text-[#6B7280] uppercase tracking-wider ${
                    sortable && col.sortable !== false ? "cursor-pointer hover:bg-[#F3F4F6] select-none" : ""
                  }`}
                  style={{ width: col.width }}
                >
                  <div className="flex items-center gap-1.5">
                    <span>{col.label}</span>
                    {sortable && col.sortable !== false && (
                      <span className="inline-flex">
                        {sortConfig.key === col.key ? (
                          sortConfig.direction === "asc" ? (
                            <ChevronUp className="w-3.5 h-3.5 text-[#2E2E38]" />
                          ) : (
                            <ChevronDown className="w-3.5 h-3.5 text-[#2E2E38]" />
                          )
                        ) : (
                          <ArrowUpDown className="w-3.5 h-3.5 text-[#9CA3AF]" />
                        )}
                      </span>
                    )}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={columns.length + (selectable ? 1 : 0)} className="px-3 py-8 text-center text-sm text-[#9CA3AF]">
                  <RefreshCw className="w-5 h-5 mx-auto mb-2 animate-spin" />
                  Loading...
                </td>
              </tr>
            ) : sortedData.length === 0 ? (
              <tr>
                <td colSpan={columns.length + (selectable ? 1 : 0)} className="px-3 py-8 text-center text-sm text-[#9CA3AF]">
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              sortedData.map((row, idx) => (
                <tr
                  key={idx}
                  onClick={() => onRowClick?.(row, idx)}
                  className={`border-b border-[#F3F4F6] ${densityClasses[density]} ${rowHeightClasses[density]} ${
                    onRowClick ? "cursor-pointer hover:bg-[#FFFCE6]" : ""
                  } ${selectedRows.has(idx) ? "bg-[#FFFCE6]" : "hover:bg-[#F9FAFB]"} transition-colors`}
                >
                  {selectable && (
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selectedRows.has(idx)}
                        onChange={(e) => {
                          e.stopPropagation();
                          handleSelectRow(idx);
                        }}
                        className="w-4 h-4 accent-[#FFE600] cursor-pointer"
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td key={col.key} className="px-3 py-2 text-[#2E2E38]">
                      {col.render ? col.render(row[col.key], row, idx) : row[col.key]}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Footer (row count) */}
      <div className="px-3 py-2 border-t border-[#E6E6E6] bg-[#F9FAFB] text-xs text-[#6B7280]">
        {filteredData.length === data.length ? (
          <span>{data.length} rows</span>
        ) : (
          <span>{filteredData.length} of {data.length} rows</span>
        )}
      </div>
    </div>
  );
}
