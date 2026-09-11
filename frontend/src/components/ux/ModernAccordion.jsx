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
    idle: "bg-gray-300",
  };

  const statusDot = status ? statusColors[status] || "bg-gray-300" : null;

  return (
    <div
      className={cn(
        "border rounded-lg overflow-hidden transition-all",
        isOpen ? "border-[#FFE600] shadow-md bg-white" : "border-[#E6E6E6] bg-white hover:border-[#D1D5DB] hover:shadow-sm",
        className
      )}
      data-testid={testId}
    >
      {/* Header */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[#F6F6FA] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#FFE600] focus-visible:ring-offset-2"
        aria-expanded={isOpen}
      >
        {/* Icon with optional status indicator */}
        <div className="relative">
          <div
            className={cn(
              "w-10 h-10 rounded-lg flex items-center justify-center transition-all",
              isOpen
                ? "bg-[#FFE600] text-[#2E2E38]"
                : "bg-[#F6F6FA] text-[#747480]"
            )}
          >
            {Icon && <Icon className="w-5 h-5" />}
          </div>
          {statusDot && (
            <div
              className={cn(
                "absolute -top-1 -right-1 w-3 h-3 rounded-full border-2 border-white",
                statusDot,
                isLoading && "animate-pulse"
              )}
            />
          )}
        </div>

        {/* Text content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold text-sm text-[#2E2E38]">
              {title}
            </h3>
            {badge && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-[#F6F6FA] text-[#747480] font-medium">
                {badge}
              </span>
            )}
          </div>
          {subtitle && (
            <p className="text-xs text-[#747480] mt-0.5 truncate">
              {subtitle}
            </p>
          )}
        </div>

        {/* Loading or chevron */}
        {isLoading ? (
          <div className="flex items-center gap-2 text-xs text-[#747480] shrink-0">
            <Loader2 className="w-4 h-4 animate-spin text-[#FFE600]" />
            {loadingText && <span className="hidden sm:inline">{loadingText}</span>}
          </div>
        ) : (
          <ChevronDown
            className={cn(
              "w-5 h-5 text-[#747480] transition-transform duration-200 shrink-0",
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
            "bg-gradient-to-b from-[#FAFAFC] to-white border-t border-[#E6E6E6]",
            isOpen ? "px-4 py-4" : "px-4 py-0"
          )}>
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
