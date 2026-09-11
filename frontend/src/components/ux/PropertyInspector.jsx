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
    <div className={`flex flex-col bg-white border-l border-[#E6E6E6] ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-[#E6E6E6] bg-[#F9FAFB]">
        <h3 className="text-sm font-semibold text-[#2E2E38]">{title}</h3>
        {onClose && (
          <button
            onClick={onClose}
            className="w-6 h-6 flex items-center justify-center rounded hover:bg-[#E6E6E6] text-[#6B7280] hover:text-[#2E2E38] transition-colors"
            title="Close"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Properties */}
      <div className="flex-1 overflow-y-auto mos-scroll">
        {Object.keys(sections).length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-[#9CA3AF]">
            No properties to display
          </div>
        ) : (
          Object.entries(sections).map(([sectionName, props]) => {
            const isExpanded = expandedSections.has(sectionName);

            return (
              <div key={sectionName} className="border-b border-[#F3F4F6] last:border-b-0">
                {/* Section Header */}
                <button
                  onClick={() => toggleSection(sectionName)}
                  className="w-full flex items-center justify-between px-3 py-2 bg-[#F9FAFB] hover:bg-[#F3F4F6] transition-colors"
                >
                  <span className="text-xs font-semibold text-[#6B7280] uppercase tracking-wide">
                    {sectionName}
                  </span>
                  {isExpanded ? (
                    <ChevronDown className="w-3.5 h-3.5 text-[#9CA3AF]" />
                  ) : (
                    <ChevronRight className="w-3.5 h-3.5 text-[#9CA3AF]" />
                  )}
                </button>

                {/* Section Content */}
                {isExpanded && (
                  <div className="divide-y divide-[#F3F4F6]">
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
    <div className="px-3 py-2.5 hover:bg-[#F9FAFB] transition-colors">
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-[#6B7280]">{label}</label>
        
        {!editable ? (
          <div className="text-sm text-[#2E2E38] font-mono">
            {value !== null && value !== undefined ? String(value) : "—"}
          </div>
        ) : type === "select" ? (
          <select
            value={value}
            onChange={(e) => onChange?.(e.target.value)}
            className="text-sm px-2 py-1 border border-[#D1D5DB] rounded focus:outline-none focus:ring-2 focus:ring-[#FFE600] focus:border-[#FFE600]"
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
              className="w-4 h-4 accent-[#FFE600] cursor-pointer"
            />
            <span className="text-sm text-[#6B7280]">Enabled</span>
          </label>
        ) : type === "textarea" ? (
          <textarea
            value={value || ""}
            onChange={(e) => onChange?.(e.target.value)}
            rows={3}
            className="text-sm px-2 py-1 border border-[#D1D5DB] rounded focus:outline-none focus:ring-2 focus:ring-[#FFE600] focus:border-[#FFE600] font-mono resize-none"
          />
        ) : (
          <input
            type={type}
            value={value || ""}
            onChange={(e) => onChange?.(e.target.value)}
            className="text-sm px-2 py-1 border border-[#D1D5DB] rounded focus:outline-none focus:ring-2 focus:ring-[#FFE600] focus:border-[#FFE600]"
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
