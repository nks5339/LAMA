import { useState, useMemo } from "react";
import { Search, Table2, Key, Link2, ChevronDown, ChevronRight, Database, Filter, X } from "lucide-react";
import { cn } from "@/lib/utils";

export default function ERDiagramV3({ data, height = 600 }) {
  const [view, setView] = useState("tables"); // tables | relationships | graph
  const [search, setSearch] = useState("");
  const [expandedTables, setExpandedTables] = useState(new Set());
  const [showLogs, setShowLogs] = useState(false);
  const [domainFilter, setDomainFilter] = useState("all");

  // Process data
  const processedData = useMemo(() => {
    if (!data) return null;

    const nodes = showLogs 
      ? data.nodes 
      : data.nodes.filter(n => !n.name.startsWith("logs_"));

    const domains = [...new Set(nodes.map(n => n.domain || "Other"))];
    
    // Group by domain
    const byDomain = {};
    nodes.forEach(node => {
      const domain = node.domain || "Other";
      if (!byDomain[domain]) byDomain[domain] = [];
      byDomain[domain].push(node);
    });

    // Filter by search
    const filtered = nodes.filter(n => 
      n.name.toLowerCase().includes(search.toLowerCase()) ||
      n.domain?.toLowerCase().includes(search.toLowerCase())
    );

    // Filter by domain
    const domainFiltered = domainFilter === "all" 
      ? filtered 
      : filtered.filter(n => (n.domain || "Other") === domainFilter);

    return { 
      nodes: domainFiltered, 
      byDomain, 
      domains,
      edges: data.edges 
    };
  }, [data, search, showLogs, domainFilter]);

  const toggleExpand = (tableId) => {
    const newExpanded = new Set(expandedTables);
    if (newExpanded.has(tableId)) {
      newExpanded.delete(tableId);
    } else {
      newExpanded.add(tableId);
    }
    setExpandedTables(newExpanded);
  };

  if (!data) {
    return (
      <div className="flex items-center justify-center h-96 bg-surface-2 rounded-lg border-2 border-dashed border-border-strong">
        <div className="text-center">
          <Database className="w-16 h-16 mx-auto mb-4 text-fg-subtle" />
          <p className="text-sm text-fg-muted font-medium">No entity data available</p>
          <p className="text-xs text-fg-subtle mt-1">Generate the Data Model first</p>
        </div>
      </div>
    );
  }

  const { nodes, byDomain, domains, edges } = processedData;

  return (
    <div className="flex flex-col h-full bg-surface rounded-lg border border-border overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 bg-gradient-to-r from-gray-50 to-white border-b border-border">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Database className="w-5 h-5 text-fg-muted" />
            <h3 className="text-base font-bold text-fg">Database Schema</h3>
          </div>
          
          {/* View tabs */}
          <div className="flex items-center gap-1 bg-surface-2 rounded-lg p-1">
            <button
              onClick={() => setView("tables")}
              className={cn(
                "px-3 py-1.5 text-xs font-medium rounded transition-all",
                view === "tables"
                  ? "bg-surface shadow-sm text-fg"
                  : "text-fg-muted hover:text-fg"
              )}
            >
              <Table2 className="w-3.5 h-3.5 inline mr-1.5" />
              Tables
            </button>
            <button
              onClick={() => setView("relationships")}
              className={cn(
                "px-3 py-1.5 text-xs font-medium rounded transition-all",
                view === "relationships"
                  ? "bg-surface shadow-sm text-fg"
                  : "text-fg-muted hover:text-fg"
              )}
            >
              <Link2 className="w-3.5 h-3.5 inline mr-1.5" />
              Relationships
            </button>
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* Stats */}
          <div className="flex items-center gap-4 text-xs">
            <span className="flex items-center gap-1.5 text-fg-muted">
              <div className="w-2 h-2 rounded-full bg-blue-500" />
              {nodes.length} tables
            </span>
            <span className="flex items-center gap-1.5 text-fg-muted">
              <div className="w-2 h-2 rounded-full bg-green-500" />
              {edges.length} relationships
            </span>
          </div>
        </div>
      </div>

      {/* Filters bar */}
      <div className="flex items-center gap-3 px-6 py-3 bg-surface-2 border-b border-border">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-fg-subtle" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tables, columns, or domains..."
            className="w-full pl-10 pr-4 py-2 text-sm border border-border-strong rounded-lg focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 outline-none"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-fg-subtle hover:text-fg-muted"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Domain filter */}
        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-fg-subtle" />
          <select
            value={domainFilter}
            onChange={(e) => setDomainFilter(e.target.value)}
            className="px-3 py-2 text-sm border border-border-strong rounded-lg focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 outline-none bg-surface"
          >
            <option value="all">All Domains</option>
            {domains.map(d => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </div>

        {/* Show logs */}
        <label className="flex items-center gap-2 text-sm text-fg-muted cursor-pointer">
          <input
            type="checkbox"
            checked={showLogs}
            onChange={(e) => setShowLogs(e.target.checked)}
            className="w-4 h-4 rounded border-border-strong text-blue-600 focus:ring-blue-500"
          />
          Show log tables
        </label>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto" style={{ maxHeight: `${height}px` }}>
        {view === "tables" && (
          <div className="p-6">
            {/* Group by domain */}
            {domains.map(domain => {
              const domainTables = byDomain[domain] || [];
              if (domainFilter !== "all" && domainFilter !== domain) return null;
              if (search && !domainTables.some(t => 
                t.name.toLowerCase().includes(search.toLowerCase())
              )) return null;

              return (
                <div key={domain} className="mb-6">
                  <div className="flex items-center gap-2 mb-3">
                    <div className="w-1 h-6 bg-blue-500 rounded" />
                    <h4 className="text-sm font-bold text-fg">{domain}</h4>
                    <span className="text-xs text-fg-subtle">({domainTables.length} tables)</span>
                  </div>

                  <div className="space-y-2">
                    {domainTables.map(table => {
                      const isExpanded = expandedTables.has(table.id);
                      const relatedEdges = edges.filter(e => 
                        e.from_table === table.id || e.to_table === table.id
                      );

                      return (
                        <div
                          key={table.id}
                          className="bg-surface border border-border rounded-lg overflow-hidden hover:border-blue-300 hover:shadow-sm transition-all"
                        >
                          {/* Table header */}
                          <button
                            onClick={() => toggleExpand(table.id)}
                            className="w-full flex items-center justify-between px-4 py-3 hover:bg-surface-2 transition-colors text-left"
                          >
                            <div className="flex items-center gap-3 flex-1">
                              <div className="flex items-center justify-center w-8 h-8 bg-blue-100 rounded">
                                <Table2 className="w-4 h-4 text-blue-600" />
                              </div>
                              
                              <div className="flex-1">
                                <h5 className="text-sm font-semibold text-fg">{table.name}</h5>
                                <div className="flex items-center gap-3 mt-0.5">
                                  <span className="text-xs text-fg-subtle">
                                    {table.columns?.length || 0} columns
                                  </span>
                                  {relatedEdges.length > 0 && (
                                    <span className="text-xs text-fg-subtle flex items-center gap-1">
                                      <Link2 className="w-3 h-3" />
                                      {relatedEdges.length} relationships
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>

                            {isExpanded ? (
                              <ChevronDown className="w-5 h-5 text-fg-subtle" />
                            ) : (
                              <ChevronRight className="w-5 h-5 text-fg-subtle" />
                            )}
                          </button>

                          {/* Expanded columns */}
                          {isExpanded && (
                            <div className="border-t border-border bg-surface-2">
                              <div className="px-4 py-2">
                                <table className="w-full text-sm">
                                  <thead>
                                    <tr className="text-xs text-fg-subtle border-b border-border">
                                      <th className="text-left py-2 font-medium w-8"></th>
                                      <th className="text-left py-2 font-medium">Column Name</th>
                                      <th className="text-left py-2 font-medium">Type</th>
                                      <th className="text-left py-2 font-medium">Constraints</th>
                                    </tr>
                                  </thead>
                                  <tbody className="text-xs">
                                    {table.columns?.map((col, idx) => (
                                      <tr
                                        key={idx}
                                        className={cn(
                                          "border-b border-border last:border-0",
                                          idx % 2 === 0 ? "bg-surface" : "bg-surface-2/50"
                                        )}
                                      >
                                        <td className="py-2">
                                          {col.is_pk && (
                                            <div className="flex items-center justify-center w-6 h-6 bg-yellow-100 rounded" title="Primary Key">
                                              <Key className="w-3 h-3 text-yellow-600" />
                                            </div>
                                          )}
                                          {col.is_fk && !col.is_pk && (
                                            <div className="flex items-center justify-center w-6 h-6 bg-blue-100 rounded" title="Foreign Key">
                                              <Link2 className="w-3 h-3 text-blue-600" />
                                            </div>
                                          )}
                                        </td>
                                        <td className="py-2 font-medium text-fg">{col.name}</td>
                                        <td className="py-2 font-mono text-fg-muted">{col.type}</td>
                                        <td className="py-2 text-fg-subtle">
                                          {col.is_pk && <span className="inline-flex items-center px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-micro font-medium mr-1">PK</span>}
                                          {col.is_fk && <span className="inline-flex items-center px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-micro font-medium mr-1">FK</span>}
                                          {col.nullable === false && <span className="inline-flex items-center px-2 py-0.5 bg-surface-2 text-fg-muted rounded text-micro font-medium">NOT NULL</span>}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>

                              {/* Related tables */}
                              {relatedEdges.length > 0 && (
                                <div className="px-4 py-3 border-t border-border bg-surface">
                                  <h6 className="text-xs font-semibold text-fg-muted mb-2">Related Tables</h6>
                                  <div className="flex flex-wrap gap-2">
                                    {relatedEdges.map((edge, idx) => {
                                      const relatedTableId = edge.from_table === table.id ? edge.to_table : edge.from_table;
                                      const relatedTable = data.nodes.find(n => n.id === relatedTableId);
                                      const direction = edge.from_table === table.id ? "→" : "←";
                                      
                                      return (
                                        <button
                                          key={idx}
                                          onClick={() => {
                                            toggleExpand(relatedTableId);
                                            // Scroll to table
                                            setTimeout(() => {
                                              document.getElementById(`table-${relatedTableId}`)?.scrollIntoView({ 
                                                behavior: 'smooth', 
                                                block: 'center' 
                                              });
                                            }, 100);
                                          }}
                                          className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-surface-2 hover:bg-surface-3 text-fg-muted rounded text-xs transition-colors"
                                        >
                                          <span className="text-fg-subtle">{direction}</span>
                                          {relatedTable?.name || relatedTableId}
                                        </button>
                                      );
                                    })}
                                  </div>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}

            {nodes.length === 0 && (
              <div className="text-center py-12">
                <Search className="w-12 h-12 mx-auto mb-3 text-fg-subtle" />
                <p className="text-sm text-fg-subtle">No tables found</p>
                <p className="text-xs text-fg-subtle mt-1">Try adjusting your search or filters</p>
              </div>
            )}
          </div>
        )}

        {view === "relationships" && (
          <div className="p-6">
            <div className="bg-surface border border-border rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-surface-2 border-b border-border">
                  <tr className="text-xs text-fg-muted">
                    <th className="text-left px-4 py-3 font-semibold">From Table</th>
                    <th className="text-center px-4 py-3 font-semibold w-16">→</th>
                    <th className="text-left px-4 py-3 font-semibold">To Table</th>
                    <th className="text-left px-4 py-3 font-semibold">Type</th>
                  </tr>
                </thead>
                <tbody>
                  {edges.map((edge, idx) => {
                    const fromTable = data.nodes.find(n => n.id === edge.from_table);
                    const toTable = data.nodes.find(n => n.id === edge.to_table);
                    
                    return (
                      <tr
                        key={idx}
                        className={cn(
                          "border-b border-border hover:bg-surface-2 transition-colors",
                          idx % 2 === 0 ? "bg-surface" : "bg-surface-2/50"
                        )}
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <Table2 className="w-4 h-4 text-fg-subtle" />
                            <span className="font-medium text-fg">{fromTable?.name || edge.from_table}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-center">
                          <Link2 className="w-4 h-4 text-blue-500 mx-auto" />
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <Table2 className="w-4 h-4 text-fg-subtle" />
                            <span className="font-medium text-fg">{toTable?.name || edge.to_table}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span className="inline-flex items-center px-2 py-1 bg-blue-100 text-blue-700 rounded text-xs font-medium">
                            {edge.relationship_type || "Foreign Key"}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {edges.length === 0 && (
                <div className="text-center py-12">
                  <Link2 className="w-12 h-12 mx-auto mb-3 text-fg-subtle" />
                  <p className="text-sm text-fg-subtle">No relationships found</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
