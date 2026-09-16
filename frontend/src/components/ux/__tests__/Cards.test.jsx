/**
 * Cards — the keyboard-trap and dead-click fixes.
 *
 * MetricCard put onClick on a bare <div>: no role, no tab stop, no keyboard
 * path. It also took a five-way rainbow `color` prop that carried no
 * meaning — four neutral counts on Discovery rendered in four colours.
 *
 * StepCard's blocked state was the product's most common dead-click: the
 * card looked pressable, rendered at opacity-60 (dropping the description
 * to ~2.1:1), and silently did nothing.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MetricCard, StepCard, StatusBadge, EmptyState } from "@/components/ux/Cards";

describe("MetricCard", () => {
  it("renders label and value", () => {
    render(<MetricCard label="Source files" value={1284} />);
    expect(screen.getByText("Source files")).toBeInTheDocument();
    expect(screen.getByText("1284")).toBeInTheDocument();
  });

  it("renders a plain div when not interactive", () => {
    render(<MetricCard label="KB chunks" value={7} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("becomes a real button when clickable", () => {
    // The fix: the old version was a <div onClick>.
    render(<MetricCard label="Files" value={3} onClick={() => {}} />);
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("is reachable and operable by keyboard when clickable", async () => {
    const onClick = jest.fn();
    render(<MetricCard label="Files" value={3} onClick={onClick} />);
    await userEvent.tab();
    expect(screen.getByRole("button")).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalled();
  });

  it("shows a skeleton instead of a misleading zero while loading", () => {
    // Discovery's tiles read 0 for months because of the kbStatus bug.
    // A loading tile must not look like a real count of zero.
    const { container } = render(<MetricCard label="Files" value={0} loading />);
    expect(container.querySelector(".skeleton")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("reserves colour for actual state, defaulting to neutral", () => {
    const { container: plain } = render(<MetricCard label="a" value={1} />);
    expect(plain.firstChild.className).not.toMatch(/border-l-(ok|warn|crit|brand)/);
    const { container: ok } = render(<MetricCard label="b" value={1} tone="ok" />);
    expect(ok.firstChild.className).toContain("border-l-ok");
  });

  it("shows trend direction with a semantic colour", () => {
    render(<MetricCard label="x" value={1} trend={12} />);
    expect(screen.getByText(/\+12%/).className).toContain("text-ok");
    render(<MetricCard label="y" value={1} trend={-4} />);
    expect(screen.getByText(/-4%/).className).toContain("text-crit");
  });
});

describe("StepCard", () => {
  it("fires onClick when enabled", async () => {
    const onClick = jest.fn();
    render(<StepCard stepNumber={1} title="Upload" onClick={onClick} />);
    await userEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalled();
  });

  describe("blocked state", () => {
    const blocked = (props = {}) =>
      render(
        <StepCard
          stepNumber={2}
          title="Generate SRS"
          description="IEEE-830 specification"
          disabled
          disabledReason="Build the knowledge base first."
          onClick={jest.fn()}
          {...props}
        />
      );

    it("states its own precondition visibly", () => {
      blocked();
      expect(screen.getByText("Build the knowledge base first.")).toBeInTheDocument();
    });

    it("exposes the reason as the accessible description", () => {
      blocked();
      expect(screen.getByRole("button")).toHaveAccessibleDescription(
        /build the knowledge base first/i
      );
    });

    it("is marked aria-disabled", () => {
      blocked();
      expect(screen.getByRole("button")).toHaveAttribute("aria-disabled", "true");
    });

    it("does not fire onClick", async () => {
      const onClick = jest.fn();
      blocked({ onClick });
      await userEvent.click(screen.getByRole("button"), { pointerEventsCheck: 0 });
      expect(onClick).not.toHaveBeenCalled();
    });

    it("does not dim the whole card below readable contrast", () => {
      // The old pending state was opacity-60, taking the description to
      // roughly 2.1:1.
      const { container } = blocked();
      expect(container.firstChild.className).not.toMatch(/opacity-(50|60|70)/);
    });
  });

  it.each(["pending", "active", "complete", "error"])(
    "status=%s resolves to tokens, not literals",
    (status) => {
      const { container } = render(
        <StepCard stepNumber={1} title="t" status={status} />
      );
      expect(container.firstChild.className).not.toMatch(/#[0-9a-f]{6}/i);
    }
  );

  it("uses the flipping on-fill token for filled badges", () => {
    const { container } = render(
      <StepCard stepNumber={1} title="t" status="complete" />
    );
    expect(container.innerHTML).toContain("text-ok-fg");
    expect(container.innerHTML).not.toContain("bg-ok text-white");
  });
});

describe("StatusBadge", () => {
  it("maps legacy status names onto the shared vocabulary", () => {
    render(<StatusBadge status="success" label="Frozen" />);
    expect(screen.getByText("Frozen")).toBeInTheDocument();
  });

  it("falls back to idle for an unknown status", () => {
    render(<StatusBadge status="mystery" label="Unknown" />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders title and description", () => {
    render(<EmptyState title="No project selected" description="Pick one." />);
    expect(screen.getByText("No project selected")).toBeInTheDocument();
  });

  it("offers a way out rather than dead-ending the user", async () => {
    // Discovery's "No Project Selected" passed no action, dead-ending a
    // first-run user on the screen meant to onboard them.
    const onAction = jest.fn();
    render(
      <EmptyState title="No project" actionLabel="Create project" onAction={onAction} />
    );
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(onAction).toHaveBeenCalled();
  });

  it("accepts a custom action node", () => {
    render(<EmptyState title="x" action={<a href="/new">Custom</a>} />);
    expect(screen.getByRole("link", { name: "Custom" })).toBeInTheDocument();
  });
});
