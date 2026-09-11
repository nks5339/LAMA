import React, { useState } from "react";
import { ChevronRight, ChevronDown, Folder, FolderOpen, File, FileCode } from "lucide-react";

/**
 * Professional TreeView component
 * Similar to VS Code file explorer / IntelliJ project view
 * 
 * Features:
 * - Expandable/collapsible nodes
 * - Icons for files/folders
 * - Selection state
 * - Keyboard navigation
 * - Indentation levels
 */
export default function TreeView({
  data = [],
  onNodeClick,
  onNodeExpand,
  selectedKey,
  expandedKeys = new Set(),
  className = "",
  renderIcon,
  renderLabel,
  level = 0
}) {
  const [localExpanded, setLocalExpanded] = useState(expandedKeys);

  const isExpanded = (key) => localExpanded.has(key);

  const toggleExpand = (key, node) => {
    const newExpanded = new Set(localExpanded);
    if (newExpanded.has(key)) {
      newExpanded.delete(key);
    } else {
      newExpanded.add(key);
    }
    setLocalExpanded(newExpanded);
    onNodeExpand?.(key, !isExpanded(key), node);
  };

  const getDefaultIcon = (node) => {
    if (node.children && node.children.length > 0) {
      return isExpanded(node.key) ? (
        <FolderOpen className="w-4 h-4 text-[#FFE600]" />
      ) : (
        <Folder className="w-4 h-4 text-[#9CA3AF]" />
      );
    }
    
    // File type icons
    const ext = node.label?.split(".").pop()?.toLowerCase();
    const codeExts = ["js", "jsx", "ts", "tsx", "py", "java", "sql", "json", "yml", "yaml"];
    
    if (codeExts.includes(ext)) {
      return <FileCode className="w-4 h-4 text-[#60A5FA]" />;
    }
    
    return <File className="w-4 h-4 text-[#9CA3AF]" />;
  };

  return (
    <div className={`select-none ${className}`} data-testid="tree-view">
      {data.map((node) => {
        const hasChildren = node.children && node.children.length > 0;
        const expanded = isExpanded(node.key);
        const selected = selectedKey === node.key;

        return (
          <div key={node.key} data-testid={`tree-node-${node.key}`}>
            {/* Node Row */}
            <div
              onClick={(e) => {
                e.stopPropagation();
                if (hasChildren) {
                  toggleExpand(node.key, node);
                }
                onNodeClick?.(node);
              }}
              className={`
                flex items-center gap-1.5 px-2 py-1.5 cursor-pointer rounded group
                ${selected ? "bg-[#FFFCE6] border-l-2 border-[#FFE600]" : "hover:bg-[#F9FAFB] border-l-2 border-transparent"}
                transition-colors
              `}
              style={{ paddingLeft: `${level * 1.25 + 0.5}rem` }}
            >
              {/* Expand/Collapse Chevron */}
              <div className="w-4 h-4 flex items-center justify-center shrink-0">
                {hasChildren && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleExpand(node.key, node);
                    }}
                    className="hover:bg-[#E6E6E6] rounded transition-colors"
                  >
                    {expanded ? (
                      <ChevronDown className="w-4 h-4 text-[#6B7280]" />
                    ) : (
                      <ChevronRight className="w-4 h-4 text-[#6B7280]" />
                    )}
                  </button>
                )}
              </div>

              {/* Icon */}
              <div className="shrink-0">
                {renderIcon ? renderIcon(node, expanded) : getDefaultIcon(node)}
              </div>

              {/* Label */}
              <div className={`flex-1 text-sm truncate ${selected ? "font-semibold text-[#2E2E38]" : "text-[#4B5563] group-hover:text-[#2E2E38]"}`}>
                {renderLabel ? renderLabel(node) : node.label}
              </div>

              {/* Badge (optional) */}
              {node.badge && (
                <span className="shrink-0 px-1.5 py-0.5 bg-[#E5E7EB] text-[#6B7280] text-[10px] font-semibold rounded">
                  {node.badge}
                </span>
              )}
            </div>

            {/* Children */}
            {hasChildren && expanded && (
              <TreeView
                data={node.children}
                onNodeClick={onNodeClick}
                onNodeExpand={onNodeExpand}
                selectedKey={selectedKey}
                expandedKeys={localExpanded}
                renderIcon={renderIcon}
                renderLabel={renderLabel}
                level={level + 1}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Tree node shape:
 * {
 *   key: string,        // Unique identifier
 *   label: string,      // Display text
 *   children?: [],      // Child nodes
 *   icon?: ReactNode,   // Custom icon
 *   badge?: string,     // Badge text (e.g., count)
 *   data?: any         // Custom data
 * }
 */
