/**
 * StageProgress — the rewritten pipeline stepper.
 *
 * The previous version drew a red→amber→green gradient with 8px tick
 * buttons and 9px labels, and had four measured defects. These tests pin
 * each fix so it cannot regress:
 *
 *   1. unreached stages were `text-red-500` — a healthy new project read
 *      as broken
 *   2. status was colour-only (WCAG 1.4.1)
 *   3. the current label measured 2.15:1 at 9px
 *   4. each stage rendered twice, so 5 destinations cost 10 tab stops,
 *      and the 8px ticks were under the 24px minimum (WCAG 2.5.8)
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import StageProgress from "@/components/StageProgress";

const mockNavigate = jest.fn();
let mockActive = null;

jest.mock("react-router-dom", () => ({
  ...jest.requireActual("react-router-dom"),
  useNavigate: () => mockNavigate,
}));

jest.mock("@/state/ProjectContext", () => ({
  useProjects: () => ({ active: mockActive }),
}));

const legacy = (stage_status = {}) => ({
  id: "p1",
  name: "PMIS",
  project_type: "legacy_migration",
  stage_status,
});

function renderAt(path = "/", project = legacy()) {
  mockActive = project;
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StageProgress />
    </MemoryRouter>
  );
}

beforeEach(() => {
  mockNavigate.mockClear();
  try {
    window.localStorage.clear();
  } catch {
    /* jsdom always has it */
  }
});

describe("StageProgress", () => {
  it("renders nothing without an active project", () => {
    const { container } = renderAt("/", null);
    expect(container).toBeEmptyDOMElement();
  });

  it("is a labelled navigation landmark with an ordered list", () => {
    renderAt();
    const nav = screen.getByRole("navigation", { name: /migration pipeline/i });
    expect(within(nav).getByRole("list")).toBeInTheDocument();
  });

  it("renders all five legacy stages", () => {
    renderAt();
    for (const key of ["discovery", "datamodel", "architecture", "codegen", "living"]) {
      expect(screen.getByTestId(`stage-progress-${key}`)).toBeInTheDocument();
    }
  });

  // ── defect 4: one tab stop per destination ─────────────────────────
  it("renders exactly one button per stage, not two", () => {
    renderAt();
    // Five stages → five buttons. The old version drew a tick button AND a
    // label button for each, so keyboard users tabbed through 10.
    expect(screen.getAllByRole("button")).toHaveLength(5);
  });

  // ── defect 1: locked is not an error ───────────────────────────────
  it("never paints an unreached stage with the critical colour", () => {
    renderAt();
    const locked = screen.getByTestId("stage-progress-architecture");
    expect(locked.className).not.toMatch(/text-(red|crit)/);
  });

  // ── defect 2: status carried by three channels ─────────────────────
  it("states each stage's status as text for screen readers", () => {
    renderAt("/", legacy({ Discovery: "frozen", DataModel: "available" }));
    expect(
      within(screen.getByTestId("stage-progress-discovery")).getByText(
        /complete and frozen/i
      )
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("stage-progress-architecture")).getByText(
        /locked until the previous stage is frozen/i
      )
    ).toBeInTheDocument();
  });

  it("renders an icon per stage alongside the label", () => {
    renderAt();
    const btn = screen.getByTestId("stage-progress-discovery");
    expect(btn.querySelector("svg")).toBeInTheDocument();
    expect(btn).toHaveTextContent(/discovery/i);
  });

  // ── defect 3: no sub-12px type ─────────────────────────────────────
  it("uses no type below the 12px floor", () => {
    renderAt();
    for (const btn of screen.getAllByRole("button")) {
      expect(btn.className).not.toMatch(/text-\[(8|9|10|11)px\]/);
    }
  });

  // ── navigation semantics ───────────────────────────────────────────
  it("marks the current stage with aria-current=step", () => {
    renderAt("/data-model", legacy({ Discovery: "frozen", DataModel: "available" }));
    expect(screen.getByTestId("stage-progress-datamodel")).toHaveAttribute(
      "aria-current",
      "step"
    );
    expect(screen.getByTestId("stage-progress-discovery")).not.toHaveAttribute(
      "aria-current"
    );
  });

  it("navigates on click when a stage is reachable", async () => {
    renderAt("/", legacy({ Discovery: "frozen", DataModel: "available" }));
    await userEvent.click(screen.getByTestId("stage-progress-datamodel"));
    expect(mockNavigate).toHaveBeenCalledWith("/data-model");
  });

  it("disables locked stages and does not navigate", async () => {
    renderAt();
    const locked = screen.getByTestId("stage-progress-living");
    expect(locked).toBeDisabled();
    await userEvent.click(locked, { pointerEventsCheck: 0 });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("counts frozen stages for the summary", () => {
    renderAt("/", legacy({ Discovery: "frozen", DataModel: "frozen" }));
    expect(screen.getByText("2/5 frozen")).toBeInTheDocument();
  });

  // ── project-type awareness (iter-14.94) ────────────────────────────
  it("renders the 3-stage tool pipeline for gap_analysis", () => {
    renderAt("/gap-analyzer#kb", {
      id: "p2",
      project_type: "gap_analysis",
      stage_status: {},
    });
    expect(screen.getAllByRole("button")).toHaveLength(3);
    expect(screen.getByTestId("stage-progress-report")).toBeInTheDocument();
  });

  it("resolves the tool stage from the URL hash", () => {
    renderAt("/gap-analyzer#kb", {
      id: "p2",
      project_type: "gap_analysis",
      stage_status: {},
    });
    expect(screen.getByTestId("stage-progress-knowledgebase")).toHaveAttribute(
      "aria-current",
      "step"
    );
  });

  // ── transformer phase override (iter-15.14) ────────────────────────
  it("honours the broadcast transformer phase over the hash", () => {
    window.localStorage.setItem("lama:transformer:phase", "transforming");
    renderAt("/transformer#input", {
      id: "p3",
      project_type: "tech_transformer",
      stage_status: {},
    });
    expect(screen.getByTestId("stage-progress-output")).toHaveAttribute(
      "aria-current",
      "step"
    );
  });

  it("marks every stage frozen when the transform completes", () => {
    window.localStorage.setItem("lama:transformer:phase", "completed");
    renderAt("/transformer#output", {
      id: "p3",
      project_type: "tech_transformer",
      stage_status: {},
    });
    expect(screen.getByText("3/3 frozen")).toBeInTheDocument();
  });
});
