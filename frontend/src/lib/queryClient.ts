/**
 * Shared React Query client.
 *
 * The audit found 22 independent `setInterval` polls across 12 components
 * with no shared cache, no request dedup, no backoff, and — crucially — no
 * pause when the tab is hidden. CodeGenMultiAgentPanel alone ran four
 * concurrent timers (state 2s, runs 3s, files 3s, clock 1s), and Transformer
 * created a fresh interval per transform at seven different call sites.
 *
 * `refetchIntervalInBackground: false` is the default here, so a backgrounded
 * tab stops generating network traffic entirely.
 */
import { QueryClient, type Query } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      gcTime: 5 * 60_000,
      retry: (failureCount: number, error: any) => {
        // Never retry auth or client errors — they will not get better,
        // and retrying a 401 six times is how a token expiry turns into a
        // burst of requests.
        const status = error?.response?.status;
        if (status && status >= 400 && status < 500) return false;
        return failureCount < 2;
      },
      retryDelay: (attempt: number) => Math.min(1000 * 2 ** attempt, 8000),
      refetchOnWindowFocus: true,
      refetchIntervalInBackground: false,
      refetchOnReconnect: true,
    },
    mutations: { retry: 0 },
  },
});

/**
 * Poll interval that backs off as a job settles, and stops when it ends.
 *
 * Pass as `refetchInterval`. Terminal statuses return false, so a finished
 * run costs nothing — the previous code polled completed transforms forever
 * until the component unmounted.
 */
export const TERMINAL = new Set<string>([
  "completed",
  "complete",
  "failed",
  "error",
  "stopped",
  "cancelled",
  "canceled",
  "frozen",
  "done",
]);

export function jobPollInterval(
  getStatus: (data: unknown) => string | undefined | null,
  { active = 2000, idle = 8000 }: { active?: number; idle?: number } = {},
) {
  return (query: Query): number | false => {
    const status = getStatus(query.state.data);
    if (!status) return idle;
    if (TERMINAL.has(String(status).toLowerCase())) return false;
    return active;
  };
}

export default queryClient;
