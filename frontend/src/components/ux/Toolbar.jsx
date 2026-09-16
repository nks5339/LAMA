import React from "react";
import { MoreVertical } from "lucide-react";

/**
 * Professional Toolbar component
 * Similar to VS Code / IntelliJ action bars
 * 
 * Features:
 * - Grouped actions
 * - Icon + text buttons
 * - Overflow menu for secondary actions
 * - Dividers between groups
 */
export default function Toolbar({ 
  actions = [], 
  secondaryActions = [],
  className = "" 
}) {
  const [overflowOpen, setOverflowOpen] = React.useState(false);

  return (
    <div className={`flex items-center gap-1 px-3 py-2 bg-surface border-b border-border ${className}`}>
      {/* Primary Actions */}
      {actions.map((action, idx) => {
        if (action.type === "divider") {
          return <div key={idx} className="w-px h-5 bg-border mx-1" />;
        }

        if (action.type === "group") {
          return (
            <div key={idx} className="flex items-center gap-1">
              {action.items.map((item, itemIdx) => (
                <ToolbarButton key={itemIdx} {...item} />
              ))}
            </div>
          );
        }

        return <ToolbarButton key={idx} {...action} />;
      })}

      {/* Secondary Actions (Overflow Menu) */}
      {secondaryActions.length > 0 && (
        <div className="ml-auto relative">
          <button
            onClick={() => setOverflowOpen(!overflowOpen)}
            className="w-8 h-8 flex items-center justify-center rounded hover:bg-bg text-fg-muted hover:text-fg transition-colors"
            title="More actions"
          >
            <MoreVertical className="w-4 h-4" />
          </button>

          {overflowOpen && (
            <>
              <div aria-hidden="true" 
                className="fixed inset-0 z-10" 
                onClick={() => setOverflowOpen(false)}
              />
              <div className="absolute right-0 top-full mt-1 min-w-48 bg-surface border border-border rounded shadow-lg z-20">
                {secondaryActions.map((action, idx) => (
                  action.type === "divider" ? (
                    <div key={idx} className="my-1 border-t border-border" />
                  ) : (
                    <button
                      key={idx}
                      onClick={() => {
                        action.onClick?.();
                        setOverflowOpen(false);
                      }}
                      disabled={action.disabled}
                      className="w-full flex items-center gap-3 px-3 py-2 text-left text-sm text-fg hover:bg-bg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      {action.icon && <action.icon className="w-4 h-4 text-fg-muted" />}
                      <span>{action.label}</span>
                    </button>
                  )
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ToolbarButton({ 
  label, 
  icon: Icon, 
  onClick, 
  disabled = false, 
  variant = "ghost",
  tooltip,
  showLabel = true 
}) {
  const baseClasses = "flex items-center gap-1.5 px-2.5 py-1.5 rounded text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed";
  
  const variantClasses = {
    primary: "bg-brand text-fg hover:bg-brand-hover",
    secondary: "bg-surface text-fg border border-border-strong hover:bg-surface-2",
    ghost: "text-fg-muted hover:text-fg hover:bg-bg",
    danger: "text-crit hover:bg-crit-bg"
  };

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={tooltip || label}
      className={`${baseClasses} ${variantClasses[variant]}`}
    >
      {Icon && <Icon className="w-4 h-4 shrink-0" />}
      {showLabel && <span className="hidden sm:inline">{label}</span>}
    </button>
  );
}

/**
 * Action shape:
 * {
 *   label: string,
 *   icon?: Component,
 *   onClick: () => void,
 *   disabled?: boolean,
 *   variant?: "primary" | "secondary" | "ghost" | "danger",
 *   tooltip?: string,
 *   showLabel?: boolean,
 *   type?: "divider" | "group"  // Special types
 *   items?: []  // For type="group"
 * }
 */
