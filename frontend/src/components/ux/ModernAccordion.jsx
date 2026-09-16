import React from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * ModernAccordion - Clean, card-based accordion with smooth animations
 * 
 * Usage:
 * <ModernAccordion value={openKey} onValueChange={setOpenKey}>
 *   <AccordionItem value="item1" icon={Upload} title="Upload Files">
 *     <p>Content here</p>
 *   </AccordionItem>
 * </ModernAccordion>
 */

export function ModernAccordion({ value, onValueChange, children, className }) {
  return (
    <div className={cn("space-y-2", className)}>
      {React.Children.map(children, (child) =>
        React.cloneElement(child, {
          isOpen: child.props.value === value,
          onToggle: () => onValueChange(child.props.value === value ? "" : child.props.value),
        })
      )}
    </div>
  );
}

export function AccordionItem({
  value,
  icon: Icon,
  title,
  subtitle,
  status,
  isLoading,
  loadingText,
  badge,
  isOpen,
  onToggle,
  children,
  className,
  testId,
}) {
  const statusColors = {
    success: "bg-green-500",
    warning: "bg-yellow-500",
    error: "bg-red-500",
    info: "bg-blue-500",
    idle: "bg-surface-3",
  };

  const statusDot = status ? statusColors[status] || "bg-surface-3" : null;

  return (
    <div
      className={cn(
        "border rounded-lg overflow-hidden transition-all",
        isOpen ? "border-brand shadow-md bg-surface" : "border-border bg-surface hover:border-border-strong hover:shadow-sm",
        className
      )}
      data-testid={testId}
    >
      {/* Header */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-bg focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2"
        aria-expanded={isOpen}
      >
        {/* Icon with optional status indicator */}
        <div className="relative">
          <div
            className={cn(
              "w-10 h-10 rounded-lg flex items-center justify-center transition-all",
              isOpen
                ? "bg-brand text-fg"
                : "bg-bg text-fg-muted"
            )}
          >
            {Icon && <Icon className="w-5 h-5" />}
          </div>
          {statusDot && (
            <div
              className={cn(
                "absolute -top-1 -right-1 w-3 h-3 rounded-full border-2 border-surface",
                statusDot,
                isLoading && "animate-pulse"
              )}
            />
          )}
        </div>

        {/* Text content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold text-sm text-fg">
              {title}
            </h3>
            {badge && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-bg text-fg-muted font-medium">
                {badge}
              </span>
            )}
          </div>
          {subtitle && (
            <p className="text-xs text-fg-muted mt-0.5 truncate">
              {subtitle}
            </p>
          )}
        </div>

        {/* Loading or chevron */}
        {isLoading ? (
          <div className="flex items-center gap-2 text-xs text-fg-muted shrink-0">
            <Loader2 className="w-4 h-4 animate-spin text-brand" />
            {loadingText && <span className="hidden sm:inline">{loadingText}</span>}
          </div>
        ) : (
          <ChevronDown
            className={cn(
              "w-5 h-5 text-fg-muted transition-transform duration-200 shrink-0",
              isOpen && "rotate-180"
            )}
          />
        )}
      </button>

      {/* Content with smooth animation */}
      <div
        className={cn(
          "grid transition-all duration-300 ease-in-out",
          isOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
        )}
      >
        <div className="overflow-hidden">
          <div className={cn(
            "bg-gradient-to-b from-surface-2 to-white border-t border-border",
            isOpen ? "px-4 py-4" : "px-4 py-0"
          )}>
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
