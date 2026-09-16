/**
 * Skeleton loaders.
 *
 * The audit counted 140 hand-rolled `animate-spin` call sites and zero
 * skeletons. A spinner says "something is happening"; a skeleton says
 * "here is the shape of what is coming", which is what makes a page feel
 * fast rather than merely busy.
 *
 * Every skeleton is aria-hidden and sits inside a container that carries
 * aria-busy, so assistive tech hears one "loading" rather than fifty.
 */
import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }) {
  return <div aria-hidden className={cn("skeleton", className)} {...props} />;
}

/** A run of text lines. The last line is short, the way real text wraps. */
export function SkeletonText({ lines = 3, className }) {
  return (
    <div className={cn("flex flex-col gap-2", className)} aria-hidden>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-3"
          style={{ width: i === lines - 1 ? "62%" : "100%" }}
        />
      ))}
    </div>
  );
}

/** Matches the stat tiles on Discovery / Gap Analyzer / Living. */
export function SkeletonStats({ count = 4, className }) {
  return (
    <div
      className={cn("grid grid-cols-2 md:grid-cols-4 gap-2", className)}
      aria-hidden
    >
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded border border-border bg-surface p-3 flex flex-col gap-2"
        >
          <Skeleton className="h-2 w-20" />
          <Skeleton className="h-6 w-12" />
        </div>
      ))}
    </div>
  );
}

/** Matches the DataTable shell so the swap-in doesn't shift layout. */
export function SkeletonTable({ rows = 6, cols = 4, className }) {
  return (
    <div
      className={cn("rounded border border-border overflow-hidden", className)}
      aria-hidden
    >
      <div className="flex gap-4 bg-surface-2 px-4 py-2 border-b border-border">
        {Array.from({ length: cols }).map((_, i) => (
          <Skeleton key={i} className="h-2 flex-1" />
        ))}
      </div>
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={r}
          className="flex gap-4 px-4 py-3 border-b border-border last:border-b-0"
        >
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className="h-3 flex-1"
              style={{ opacity: 1 - r * 0.1 }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * Route-level fallback for the lazy chunks in App.js. Approximates the
 * common page shape (header strip, stat row, body) so a route swap reads
 * as a page loading rather than the app blanking out.
 */
export function PageSkeleton() {
  return (
    <div
      className="flex-1 flex flex-col min-w-0 min-h-0 p-6 gap-6"
      role="status"
      aria-busy="true"
      aria-live="polite"
      aria-label="Loading page"
      data-testid="page-skeleton"
    >
      <div className="flex items-center gap-4">
        <Skeleton className="h-6 w-56" />
        <Skeleton className="h-5 w-20 rounded-lg" />
      </div>
      <SkeletonStats />
      <div className="flex-1 rounded border border-border bg-surface p-6 flex flex-col gap-4">
        <Skeleton className="h-4 w-40" />
        <SkeletonText lines={5} />
        <div className="mt-2">
          <SkeletonText lines={3} />
        </div>
      </div>
      <span className="sr-only">Loading page…</span>
    </div>
  );
}

export default Skeleton;
