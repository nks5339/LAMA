import React from "react";
import { X, ChevronDown, ChevronRight } from "lucide-react";

/**
 * PropertyInspector - Professional property/details panel
 * Similar to VS Code properties panel / IntelliJ inspector
 * 
 * Shows detailed information about selected items
 * with collapsible sections
 */
export default function PropertyInspector({
  title = "Properties",
  properties = [],
  onClose,
  className = ""
}) {
  const [expandedSections, setExpandedSections] = React.useState(
    new Set(properties.filter(p => p.defaultExpanded !== false).map(p => p.section))
  );

  const toggleSection = (section) => {
    const newExpanded = new Set(expandedSections);
    if (newExpanded.has(section)) {
      newExpanded.delete(section);
    } else {
      newExpanded.add(section);
    }
    setExpandedSections(newExpanded);
  };

  // Group properties by section
  const sections = properties.reduce((acc, prop) => {
    const section = prop.section || "General";
    if (!acc[section]) acc[section] = [];
    acc[section].push(prop);
    return acc;
  }, {});

  return (
    <div className={`flex flex-col bg-surface border-l border-border ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border bg-surface-2">
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        {onClose && (
          <button
            onClick={onClose}
            className="w-6 h-6 flex items-center justify-center rounded hover:bg-border text-fg-muted hover:text-fg transition-colors"
            title="Close"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Properties */}
      <div className="flex-1 overflow-y-auto mos-scroll">
        {Object.keys(sections).length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-fg-subtle">
            No properties to display
          </div>
        ) : (
          Object.entries(sections).map(([sectionName, props]) => {
            const isExpanded = expandedSections.has(sectionName);

            return (
              <div key={sectionName} className="border-b border-surface-2 last:border-b-0">
                {/* Section Header */}
                <button
                  onClick={() => toggleSection(sectionName)}
                  className="w-full flex items-center justify-between px-3 py-2 bg-surface-2 hover:bg-surface-2 transition-colors"
                >
                  <span className="text-xs font-semibold text-fg-muted uppercase tracking-wide">
                    {sectionName}
                  </span>
                  {isExpanded ? (
                    <ChevronDown className="w-3.5 h-3.5 text-fg-subtle" />
                  ) : (
                    <ChevronRight className="w-3.5 h-3.5 text-fg-subtle" />
                  )}
                </button>

                {/* Section Content */}
                {isExpanded && (
                  <div className="divide-y divide-surface-2">
                    {props.map((prop, idx) => (
                      <PropertyRow key={idx} {...prop} />
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function PropertyRow({ label, value, type = "text", editable = false, onChange, options, render }) {
  if (render) {
    return (
      <div className="px-3 py-2">
        {render(value, onChange)}
      </div>
    );
  }

  return (
    <div className="px-3 py-2.5 hover:bg-surface-2 transition-colors">
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-fg-muted">{label}</label>
        
        {!editable ? (
          <div className="text-sm text-fg font-mono">
            {value !== null && value !== undefined ? String(value) : "—"}
          </div>
        ) : type === "select" ? (
          <select
            value={value}
            onChange={(e) => onChange?.(e.target.value)}
            className="text-sm px-2 py-1 border border-border-strong rounded focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
          >
            {options?.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        ) : type === "checkbox" ? (
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={Boolean(value)}
              onChange={(e) => onChange?.(e.target.checked)}
              className="w-4 h-4 accent-brand cursor-pointer"
            />
            <span className="text-sm text-fg-muted">Enabled</span>
          </label>
        ) : type === "textarea" ? (
          <textarea
            value={value || ""}
            onChange={(e) => onChange?.(e.target.value)}
            rows={3}
            className="text-sm px-2 py-1 border border-border-strong rounded focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand font-mono resize-none"
          />
        ) : (
          <input
            type={type}
            value={value || ""}
            onChange={(e) => onChange?.(e.target.value)}
            className="text-sm px-2 py-1 border border-border-strong rounded focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand"
          />
        )}
      </div>
    </div>
  );
}

/**
 * Property shape:
 * {
 *   label: string,
 *   value: any,
 *   section?: string,     // Group name (default: "General")
 *   type?: "text" | "select" | "checkbox" | "textarea" | "number",
 *   editable?: boolean,
 *   onChange?: (value) => void,
 *   options?: [{ label, value }],  // For select type
 *   render?: (value, onChange) => ReactNode  // Custom renderer
 * }
 */
