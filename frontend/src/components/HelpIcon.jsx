import { HelpCircle } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

/**
 * Inline help icon with plain-English tooltip.
 * Usage: <HelpIcon text="What this field does and why" />
 */
export default function HelpIcon({ text, side = "top", testId }) {
  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          {/* Not a button — it performs no action. It is a focusable help
              affordance whose only job is to surface the tooltip, so
              role="button" was announcing a control that does nothing.
              tabIndex keeps it keyboard-reachable; Radix handles the rest. */}
          <span
            tabIndex={0}
            data-testid={testId || "help-icon"}
            className="inline-flex items-center justify-center w-4 h-4 ml-1 text-fg-subtle hover:text-fg-muted cursor-help"
            aria-label="Help"
          >
            <HelpCircle className="w-4 h-4" />
          </span>
        </TooltipTrigger>
        <TooltipContent side={side} className="max-w-xs text-xs leading-relaxed">
          {text}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
