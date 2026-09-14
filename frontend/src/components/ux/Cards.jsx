import { cn } from "@/lib/utils";

/**
 * MetricCard - Display key metrics with clean visual hierarchy
 */
export function MetricCard({ label, value, trend, icon: Icon, color = "blue", className, onClick }) {
  const colorClasses = {
    blue: "bg-blue-50 border-blue-200 text-blue-900",
    green: "bg-green-50 border-green-200 text-green-900",
    yellow: "bg-yellow-50 border-yellow-200 text-yellow-900",
    purple: "bg-purple-50 border-purple-200 text-purple-900",
    gray: "bg-gray-50 border-gray-200 text-gray-900",
  };

  return (
    <div
      className={cn(
        "border rounded-lg p-2 transition-all hover:shadow-md",
        colorClasses[color],
        onClick && "cursor-pointer hover:scale-105",
        className
      )}
      onClick={onClick}
    >
      <div className="flex items-start justify-between mb-1">
        <p className="text-[10px] font-medium uppercase tracking-wide opacity-70">{label}</p>
        {Icon && <Icon className="w-3 h-3 opacity-60" />}
      </div>
      <div className="flex items-baseline gap-2">
        <p className="text-xl font-bold">{value}</p>
        {trend && (
          <span className="text-[10px] font-medium">
            {trend > 0 ? "+" : ""}
            {trend}%
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * StepCard - Wizard-style step card for guided workflows
 * Can be clickable for navigation
 */
export function StepCard({ 
  stepNumber, 
  title, 
  description, 
  status = "pending", // pending | active | complete | error
  icon: Icon,
  onClick,
  className 
}) {
  const statusStyles = {
    pending: "border-gray-200 bg-white opacity-60",
    active: "border-[#FFE600] bg-white shadow-md",
    complete: "border-green-300 bg-green-50",
    error: "border-red-300 bg-red-50",
  };

  const iconStyles = {
    pending: "bg-gray-100 text-gray-400",
    active: "bg-[#FFE600] text-[#2E2E38]",
    complete: "bg-green-500 text-white",
    error: "bg-red-500 text-white",
  };

  const Component = onClick ? "button" : "div";

  return (
    <Component
      onClick={onClick}
      className={cn(
        "border-2 rounded-lg transition-all w-full text-left",
        statusStyles[status],
        onClick && "cursor-pointer hover:shadow-lg hover:scale-105",
        className
      )}
    >
      <div className="px-3 py-2 flex items-center gap-2">
        <div className={cn("w-8 h-8 rounded-full flex items-center justify-center shrink-0", iconStyles[status])}>
          {Icon ? <Icon className="w-4 h-4" /> : <span className="font-bold text-sm">{stepNumber}</span>}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-sm text-[#2E2E38] leading-tight">{title}</h3>
          <p className="text-xs text-[#747480] leading-tight mt-0.5">{description}</p>
        </div>
      </div>
    </Component>
  );
}

/**
 * StatusBadge - Consistent status indicators
 */
export function StatusBadge({ status, label, size = "md" }) {
  const sizeClasses = {
    sm: "text-xs px-2 py-0.5",
    md: "text-sm px-3 py-1",
    lg: "text-base px-4 py-1.5",
  };

  const statusStyles = {
    success: "bg-green-100 text-green-800 border-green-300",
    warning: "bg-yellow-100 text-yellow-800 border-yellow-300",
    error: "bg-red-100 text-red-800 border-red-300",
    info: "bg-blue-100 text-blue-800 border-blue-300",
    neutral: "bg-gray-100 text-gray-800 border-gray-300",
    active: "bg-[#FFE600] text-[#2E2E38] border-[#FFE600]",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 font-medium border rounded-full",
        sizeClasses[size],
        statusStyles[status]
      )}
    >
      <div className={cn("w-1.5 h-1.5 rounded-full", {
        "bg-green-500": status === "success",
        "bg-yellow-500": status === "warning",
        "bg-red-500": status === "error",
        "bg-blue-500": status === "info",
        "bg-gray-500": status === "neutral",
        "bg-[#2E2E38]": status === "active",
      })} />
      {label}
    </span>
  );
}

/**
 * EmptyState - Consistent empty state messaging
 */
export function EmptyState({ icon: Icon, title, description, action }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
      {Icon && (
        <div className="w-16 h-16 rounded-full bg-gray-100 flex items-center justify-center mb-4">
          <Icon className="w-8 h-8 text-gray-400" />
        </div>
      )}
      <h3 className="text-lg font-semibold text-[#2E2E38] mb-2">{title}</h3>
      <p className="text-sm text-[#747480] max-w-md mb-4">{description}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/**
 * ActionPanel - Sticky bottom action panel with CTAs
 */
export function ActionPanel({ children, className }) {
  return (
    <div className={cn(
      "sticky bottom-0 left-0 right-0 bg-white border-t-2 border-[#E6E6E6] px-6 py-4 flex items-center justify-between gap-4 shadow-lg",
      className
    )}>
      {children}
    </div>
  );
}

/**
 * ProgressBar - Visual progress indicator
 */
export function ProgressBar({ value, max = 100, label, showPercentage = true, color = "blue" }) {
  const percentage = Math.min(100, Math.round((value / max) * 100));
  
  const colorClasses = {
    blue: "bg-blue-500",
    green: "bg-green-500",
    yellow: "bg-[#FFE600]",
    red: "bg-red-500",
  };

  return (
    <div className="w-full">
      {label && (
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-[#2E2E38]">{label}</span>
          {showPercentage && <span className="text-sm text-[#747480]">{percentage}%</span>}
        </div>
      )}
      <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className={cn("h-full transition-all duration-300 rounded-full", colorClasses[color])}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
}
