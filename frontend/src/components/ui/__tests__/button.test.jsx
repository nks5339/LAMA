/**
 * Button — the primitive 82% of the app's buttons were bypassing.
 *
 * These pin the three things the rewrite added, because each replaced a
 * defect the audit measured:
 *   • `loading` as a variant — replaces 140 hand-rolled animate-spin sites
 *     and, crucially, sets aria-busy so the state is announced not just drawn
 *   • a --tap floor on every size — WCAG 2.5.8 wants 24px minimum
 *   • role-named variants that resolve to tokens, never literals
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "@/components/ui/button";

describe("Button", () => {
  it("renders its label and fires onClick", async () => {
    const onClick = jest.fn();
    render(<Button onClick={onClick}>Freeze stage</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Freeze stage" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("is keyboard operable", async () => {
    const onClick = jest.fn();
    render(<Button onClick={onClick}>Generate</Button>);
    await userEvent.tab();
    expect(screen.getByRole("button")).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onClick).toHaveBeenCalled();
  });

  describe("loading", () => {
    it("sets aria-busy so the state is announced, not merely drawn", () => {
      render(<Button loading>Saving</Button>);
      expect(screen.getByRole("button")).toHaveAttribute("aria-busy", "true");
    });

    it("disables the button so a job cannot be double-submitted", async () => {
      const onClick = jest.fn();
      render(<Button loading onClick={onClick}>Saving</Button>);
      const btn = screen.getByRole("button");
      expect(btn).toBeDisabled();
      await userEvent.click(btn);
      expect(onClick).not.toHaveBeenCalled();
    });

    it("keeps the label in the accessible name while the spinner shows", () => {
      // The label is visually hidden, not removed — a screen reader must
      // still be able to say which button is busy.
      render(<Button loading>Build knowledge base</Button>);
      expect(
        screen.getByRole("button", { name: /build knowledge base/i })
      ).toBeInTheDocument();
    });

    it("carries no aria-busy when idle", () => {
      render(<Button>Idle</Button>);
      expect(screen.getByRole("button")).not.toHaveAttribute("aria-busy");
    });
  });

  describe("WCAG 2.5.8 target size", () => {
    // --tap is read back by min-h-tap. Every size must set one, and the
    // smallest must still clear 24px.
    it.each([
      ["xs", "24px"],
      ["sm", "32px"],
      ["md", "36px"],
      ["lg", "44px"],
      ["icon", "36px"],
    ])("size=%s sets --tap:%s", (size, tap) => {
      render(<Button size={size}>x</Button>);
      expect(screen.getByRole("button").className).toContain(`[--tap:${tap}]`);
    });
  });

  describe("variants", () => {
    it("resolves colours to tokens, never to literals", () => {
      render(<Button variant="primary">P</Button>);
      const cls = screen.getByRole("button").className;
      expect(cls).toContain("bg-ink");
      expect(cls).not.toMatch(/#[0-9a-f]{6}/i);
    });

    it("destructive uses the flipping on-fill token", () => {
      // bg-crit + text-white inverts to unreadable in dark, where --crit
      // becomes a light salmon. text-crit-fg flips with the theme.
      render(<Button variant="destructive">Delete</Button>);
      const cls = screen.getByRole("button").className;
      expect(cls).toContain("text-crit-fg");
      expect(cls).not.toContain("text-white");
    });

    it("keeps `default` working for the call sites that predate role names", () => {
      render(<Button variant="default" size="default">Legacy</Button>);
      expect(screen.getByRole("button").className).toContain("bg-ink");
    });

    it("every variant requests a visible focus ring", () => {
      for (const v of ["primary", "brand", "outline", "ghost", "destructive"]) {
        const { unmount } = render(<Button variant={v}>x</Button>);
        expect(screen.getByRole("button").className).toContain(
          "focus-visible:ring-ring"
        );
        unmount();
      }
    });
  });

  it("supports asChild without breaking the single-child contract", () => {
    render(
      <Button asChild>
        <a href="/data-model">Go</a>
      </Button>
    );
    expect(screen.getByRole("link", { name: "Go" })).toHaveAttribute(
      "href",
      "/data-model"
    );
  });
});
