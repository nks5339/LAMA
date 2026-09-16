import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva } from "class-variance-authority";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Button.
 *
 * Changes from the previous version:
 *  • Focus ring is --ring (4.9:1 on white), not the global #FFE600 outline
 *    that measured 1.27:1 against the 3:1 WCAG 1.4.11 floor.
 *  • Every size clears the WCAG 2.5.8 24px minimum target via --tap.
 *  • `loading` is a variant, not a caller concern — this is what replaces
 *    the 140 hand-rolled `animate-spin` call sites, and it sets aria-busy
 *    so the state is announced rather than merely drawn.
 *  • Variants are named by role (primary / brand / outline / ghost /
 *    destructive), so a call site states intent rather than a colour.
 */
const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap",
    "rounded font-medium",
    "transition-[background-color,border-color,color,box-shadow] duration-fast ease",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    "focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
    "disabled:pointer-events-none disabled:opacity-55",
    "min-h-tap",
    "[&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:pointer-events-none",
  ],
  {
    variants: {
      variant: {
        // `default` is an alias for `primary`, kept so the 15 existing
        // call sites keep compiling while they migrate to role names.
        default:
          "bg-ink text-ink-fg shadow-raised hover:bg-ink-hover active:translate-y-px",
        primary:
          "bg-ink text-ink-fg shadow-raised hover:bg-ink-hover active:translate-y-px",
        brand:
          "bg-brand text-brand-fg shadow-raised hover:bg-brand-hover active:translate-y-px",
        outline:
          "border border-border bg-surface text-fg hover:bg-surface-2 hover:border-border-strong",
        secondary: "bg-surface-2 text-fg hover:bg-surface-3",
        ghost: "text-fg-muted hover:bg-surface-2 hover:text-fg",
        destructive: "bg-crit text-crit-fg shadow-raised hover:bg-crit/90",
        link: "text-info underline-offset-4 hover:underline min-h-0 [--tap:0px]",
      },
      size: {
        // `default` alias, as above.
        default: "h-9 px-4 text-sm [--tap:36px]",
        xs: "h-6 px-2 text-micro [--tap:24px]",
        sm: "h-8 px-3 text-xs [--tap:32px]",
        md: "h-9 px-4 text-sm [--tap:36px]",
        lg: "h-11 px-6 text-base [--tap:44px]",
        icon: "h-9 w-9 p-0 [--tap:36px]",
        "icon-sm": "h-8 w-8 p-0 [--tap:32px]",
      },
    },
    defaultVariants: { variant: "outline", size: "md" },
  }
);

const Button = React.forwardRef(
  (
    { className, variant, size, asChild = false, loading = false, children, disabled, ...props },
    ref
  ) => {
    const Comp = asChild ? Slot : "button";

    // asChild delegates rendering, so the spinner wrapper would break the
    // single-child contract Slot requires. Keep it simple there.
    if (asChild) {
      return (
        <Comp
          ref={ref}
          className={cn(buttonVariants({ variant, size }), className)}
          {...props}
        >
          {children}
        </Comp>
      );
    }

    return (
      <button
        ref={ref}
        aria-busy={loading || undefined}
        disabled={disabled || loading}
        className={cn(
          buttonVariants({ variant, size }),
          loading && "relative",
          className
        )}
        {...props}
      >
        <span className={cn("contents", loading && "invisible")}>{children}</span>
        {loading && (
          <span className="absolute inset-0 grid place-items-center">
            <Loader2 className="size-4 animate-spin" aria-hidden />
          </span>
        )}
      </button>
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
