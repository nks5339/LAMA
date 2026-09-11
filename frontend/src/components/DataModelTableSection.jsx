/**
 * DataModel Page Integration with TableBrowser
 * 
 * This file shows how to integrate the new TableBrowser component
 * into the existing DataModel page to handle 2000+ tables efficiently
 */

import React, { useState, useEffect } from "react";
import { TableBrowser } from "@/components/TableBrowser";
import { API } from "@/lib/api";

/**
 * Example: Using TableBrowser in DataModel page
 * 
 * Replace the existing table rendering section with this implementation
 */
export function DataModelTableSection({ projectId }) {
  const [tables, setTables] = useState([]);
  const [selectedTable, setSelectedTable] = useState(null);
  const [loading, setLoading] = useState(true);

  // Fetch tables from backend
  useEffect(() => {
    async function loadTables() {
      try {
        setLoading(true);
        
        // TODO: Replace with actual API endpoint
        // This is a placeholder - adjust according to your actual API
        const response = await fetch(`${API}/datamodel/${projectId}/tables`);
        const data = await response.json();
        
        // Transform data to match TableBrowser expected format
        const transformedTables = data.map(table => ({
          id: table.id || table.name,
          name: table.name,
          schema: table.schema || "default",
          type: table.type || "TABLE",
          columnCount: table.columns?.length || 0,
          // Add any other metadata you need
        }));
        
        setTables(transformedTables);
      } catch (error) {
        console.error("Failed to load tables:", error);
      } finally {
        setLoading(false);
      }
    }

    if (projectId) {
      loadTables();
    }
  }, [projectId]);

  const handleTableSelect = (table) => {
    setSelectedTable(table);
    // Load table details, show in panel, etc.
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-sm text-[#747480]">Loading tables...</div>
      </div>
    );
  }

  return (
    <div className="h-full flex gap-3">
      {/* Table Browser - Left Panel (40% width) */}
      <div className="w-2/5 h-full border border-[#E6E6E6] rounded-sm overflow-hidden">
        <TableBrowser
          tables={tables}
          onTableSelect={handleTableSelect}
          selectedTables={selectedTable ? [selectedTable] : []}
        />
      </div>

      {/* Table Details - Right Panel (60% width) */}
      <div className="flex-1 h-full border border-[#E6E6E6] rounded-sm overflow-hidden bg-white">
        {selectedTable ? (
          <div className="p-4">
            <h3 className="font-display font-semibold text-base text-[#2E2E38] mb-4">
              {selectedTable.schema}.{selectedTable.name}
            </h3>
            {/* Show table columns, ER diagram, DDL, etc. */}
            <div className="text-sm text-[#747480]">
              Table details will be displayed here...
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-sm text-[#747480]">
            Select a table to view details
          </div>
        )}
      </div>
    </div>
  );
}

export default DataModelTableSection;
