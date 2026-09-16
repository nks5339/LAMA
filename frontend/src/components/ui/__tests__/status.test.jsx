/**
 * Status vocabulary — the fix for WCAG 1.4.1 (colour as the only cue).
 *
 * Before this, "frozen" / "passing" / "healthy" were styled differently on
 * every stage, and status was frequently carried by colour alone. Every
 * chip here must carry THREE channels — icon, text, colour — so removing
 * any one still leaves the meaning intact.
 */
import { render, screen } from "@testing-library/react";
import { StatusChip, StageBadge, ProgressBar, STAGE_STATE } from "@/components/ui/status";

describe("StatusChip", () => {
  it("renders a text label, not colour alone", () => {
    render(<StatusChip tone="ok">Frozen</StatusChip>);
    expect(screen.getByText("Frozen")).toBeInTheDocument();
  });

  it("renders an icon alongside the label", () => {
    const { container } = render(<StatusChip tone="crit">Failed</StatusChip>);
    expect(container.querySelector("svg")).toBeInTheDocument();
  });

  it("marks the icon aria-hidden so it is not read twice", () => {
    const { container } = render(<StatusChip tone="ok">Frozen</StatusChip>);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it.each(["ok", "warn", "crit", "info", "brand", "idle", "busy"])(
    "tone=%s resolves to tokens, never a literal colour",
    (tone) => {
      const { container } = render(<StatusChip tone={tone}>x</StatusChip>);
      const cls = container.firstChild.className;
      expect(cls).not.toMatch(/#[0-9a-f]{3,8}/i);
      expect(cls).not.toMatch(/\b(slate|gray|zinc|neutral)-\d{2,3}\b/);
    }
  );

  it("spins only the busy tone", () => {
    const { container: busy } = render(<StatusChip tone="busy">Running</StatusChip>);
    expect(busy.querySelector("svg").getAttribute("class")).toContain("animate-spin");
    const { container: ok } = render(<StatusChip tone="ok">Done</StatusChip>);
    expect(ok.querySelector("svg").getAttribute("class")).not.toContain("animate-spin");
  });
});

describe("STAGE_STATE", () => {
  it("never paints an unreached stage as an error", () => {
    // The old stepper used text-red-500 for `locked`, which made a healthy
    // new project look broken. Not-yet-started is not a failure.
    expect(STAGE_STATE.locked.tone).not.toBe("crit");
    expect(STAGE_STATE.locked.tone).toBe("idle");
  });

  it("gives every state an icon and a screen-reader phrase", () => {
    for (const [name, meta] of Object.entries(STAGE_STATE)) {
      // Three channels per state: icon, visible label, sr phrase.
      expect({ name, has: Boolean(meta.Icon && meta.label && meta.sr) }).toEqual({
        name,
        has: true,
      });
    }
  });

  it("explains WHY a locked stage is locked", () => {
    expect(STAGE_STATE.locked.sr).toMatch(/frozen/i);
  });

  it("covers all five statuses the backend emits", () => {
    expect(Object.keys(STAGE_STATE).sort()).toEqual(
      ["active", "available", "frozen", "locked", "skipped"].sort()
    );
  });
});

describe("StageBadge", () => {
  it("exposes the data-testid the testing agent asserts on", () => {
    render(<StageBadge status="frozen" />);
    expect(screen.getByTestId("stage-badge-frozen")).toBeInTheDocument();
  });

  it("falls back to locked for an unknown status rather than rendering blank", () => {
    render(<StageBadge status="who-knows" />);
    expect(screen.getByText("Locked")).toBeInTheDocument();
  });
});

describe("ProgressBar", () => {
  // The previous ProgressBar had no role and no value attributes, so a
  // screen reader announced nothing at all.
  it("is a real progressbar with a value", () => {
    render(<ProgressBar value={64} label="Generating" />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "64");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
  });

  it("is labelled even without a visible label", () => {
    render(<ProgressBar value={10} />);
    expect(screen.getByRole("progressbar")).toHaveAccessibleName("Progress");
  });

  it("clamps out-of-range values instead of overflowing the track", () => {
    render(<ProgressBar value={250} label="over" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    render(<ProgressBar value={-40} label="under" />);
    expect(screen.getAllByRole("progressbar")[1]).toHaveAttribute("aria-valuenow", "0");
  });

  it("scales against a custom max", () => {
    render(<ProgressBar value={25} max={50} label="half" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  });
});
