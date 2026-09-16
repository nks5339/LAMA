/**
 * Loading and AI-work surfaces.
 *
 * The audit counted 140 hand-rolled `animate-spin` sites, zero skeletons,
 * and — the one that mattered most for an agentic product — zero aria-live
 * regions. A screen-reader user was never told a job started, progressed
 * or finished.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  Skeleton, SkeletonText, SkeletonStats, SkeletonTable, PageSkeleton,
} from "@/components/ui/skeleton";
import { JobProgress, ThinkingDots, AgentTimeline } from "@/components/ui/job-progress";

describe("Skeletons", () => {
  it("are hidden from assistive tech so they are not read as content", () => {
    const { container } = render(<Skeleton className="h-4 w-20" />);
    expect(container.firstChild).toHaveAttribute("aria-hidden", "true");
  });

  it("render the requested number of text lines", () => {
    const { container } = render(<SkeletonText lines={4} />);
    expect(container.querySelectorAll(".skeleton")).toHaveLength(4);
  });

  it("match the stat-tile grid so the swap-in does not shift layout", () => {
    const { container } = render(<SkeletonStats count={4} />);
    expect(container.querySelectorAll(".skeleton")).toHaveLength(8); // label + value
  });

  it("render a table shell with header and rows", () => {
    const { container } = render(<SkeletonTable rows={3} cols={4} />);
    expect(container.querySelectorAll(".skeleton").length).toBe(16); // 4 head + 12 body
  });

  describe("PageSkeleton", () => {
    it("announces itself as busy — it is the route-level Suspense fallback", () => {
      render(<PageSkeleton />);
      const status = screen.getByRole("status");
      expect(status).toHaveAttribute("aria-busy", "true");
      expect(status).toHaveAttribute("aria-live", "polite");
    });

    it("carries the testid the route-split fallback is asserted on", () => {
      render(<PageSkeleton />);
      expect(screen.getByTestId("page-skeleton")).toBeInTheDocument();
    });

    it("gives a screen reader something to say", () => {
      render(<PageSkeleton />);
      expect(screen.getByText("Loading page…")).toBeInTheDocument();
    });
  });
});

describe("JobProgress", () => {
  it("is a polite live region so progress is announced", () => {
    render(<JobProgress phase="generating" elapsed={12} />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("names the phase in human words", () => {
    render(<JobProgress phase="coder_be" elapsed={5} />);
    expect(screen.getByText("Writing backend code")).toBeInTheDocument();
  });

  it("shows elapsed time and the expected range", () => {
    // Without a range, a 90s generation is indistinguishable from a hang.
    render(<JobProgress phase="generating" elapsed={95} expected="60–120s" />);
    expect(screen.getByText(/1m 35s elapsed/)).toBeInTheDocument();
    expect(screen.getByText(/typically 60–120s/)).toBeInTheDocument();
  });

  it("renders a real progressbar when a percent is supplied", () => {
    render(<JobProgress phase="generating" elapsed={1} percent={64} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "64");
  });

  it("offers cancel only when the caller can cancel", async () => {
    const onCancel = jest.fn();
    const { rerender } = render(<JobProgress phase="x" elapsed={1} />);
    expect(screen.queryByTestId("job-cancel-btn")).not.toBeInTheDocument();
    rerender(<JobProgress phase="x" elapsed={1} onCancel={onCancel} />);
    await userEvent.click(screen.getByTestId("job-cancel-btn"));
    expect(onCancel).toHaveBeenCalled();
  });

  it("falls back to a generic label rather than rendering blank", () => {
    render(<JobProgress elapsed={0} />);
    expect(screen.getByText("Working")).toBeInTheDocument();
  });
});

describe("ThinkingDots", () => {
  it("announces the label to screen readers, not just the animation", () => {
    render(<ThinkingDots label="Searching the knowledge base" />);
    const live = screen.getByRole("status");
    expect(live).toHaveTextContent("Searching the knowledge base");
  });

  it("hides the decorative pulse from assistive tech", () => {
    const { container } = render(<ThinkingDots />);
    expect(container.querySelector("[aria-hidden='true']")).toBeInTheDocument();
  });
});

describe("AgentTimeline", () => {
  const steps = [
    { id: "1", agent: "context_manager", summary: "Gathered stage context", status: "done" },
    { id: "2", agent: "planner", summary: "Planned 12 tasks", status: "done" },
    { id: "3", agent: "coder_be", summary: "Writing services", status: "running" },
  ];

  it("renders nothing when there are no steps", () => {
    const { container } = render(<AgentTimeline steps={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("summarises the step count and the current agent", () => {
    // <details> keeps its children mounted when closed, so scope the
    // assertion to the <summary> the user actually sees collapsed.
    const { container } = render(<AgentTimeline steps={steps} />);
    const summary = container.querySelector("summary");
    expect(summary).toHaveTextContent("3 steps");
    expect(summary).toHaveTextContent("Writing backend code");
  });

  it("is collapsed by default — progressive disclosure, not noise", () => {
    const { container } = render(<AgentTimeline steps={steps} />);
    expect(container.querySelector("details")).not.toHaveAttribute("open");
  });

  it("can open to reveal every step", async () => {
    render(<AgentTimeline steps={steps} defaultOpen />);
    expect(screen.getByRole("list", { name: /agent steps/i })).toBeInTheDocument();
    expect(screen.getByText("Planned 12 tasks")).toBeInTheDocument();
  });

  it("renders a citation source when a step carries one", () => {
    render(
      <AgentTimeline
        defaultOpen
        steps={[{ id: "a", agent: "retrieving", summary: "Invoice.php", status: "done", source: "php" }]}
      />
    );
    expect(screen.getByText("php")).toBeInTheDocument();
  });

  it("singularises a one-step timeline", () => {
    render(<AgentTimeline steps={[steps[0]]} />);
    expect(screen.getByText("1 step")).toBeInTheDocument();
  });
});
