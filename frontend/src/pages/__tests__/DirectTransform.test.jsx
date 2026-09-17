/**
 * Direct Transform — the fourth Tools section.
 *
 * Two things are pinned here:
 *
 *   1. Registration symmetry. Direct Transform must appear in the Tools
 *      accordion LAST, after Prompt Library, and must be reachable from
 *      the same registries the other three use (route chunk, breadcrumb,
 *      command palette). Getting a nav entry into three of four registries
 *      is the failure mode this catches — the tab works but the palette
 *      and breadcrumb quietly don't know about it.
 *
 *   2. The page's own primary flow: plugins load into the stack picker,
 *      detect fills the stacks in, create-and-start posts a well-formed
 *      job, and every failure path lands as a toast rather than a blank
 *      screen or an unhandled rejection.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import DirectTransform from "@/pages/DirectTransform";

// jest.mock() is hoisted above the imports, so its factory runs while
// `mockApi` is still in the temporal dead zone. Wrapping each export in an
// arrow defers the lookup to call time, which is the pattern the other
// page suite (DiscoveryV2) uses.
const mockApi = {
  dcteListPlugins: jest.fn(),
  dcteListStacks: jest.fn(),
  dcteDetectProject: jest.fn(),
  dcteCreateJob: jest.fn(),
  dcteListJobs: jest.fn(),
  dcteGetJob: jest.fn(),
  dcteStartJob: jest.fn(),
  dctePauseJob: jest.fn(),
  dcteResumeJob: jest.fn(),
  dcteRollbackJob: jest.fn(),
  dcteDeleteJob: jest.fn(),
  dcteGetReports: jest.fn(),
  dcteGetEvents: jest.fn(),
  dcteGetTransforms: jest.fn(),
  dcteBrowseFs: jest.fn(),
};
jest.mock("@/lib/api", () => ({
  dcteListPlugins: (...a) => mockApi.dcteListPlugins(...a),
  dcteListStacks: (...a) => mockApi.dcteListStacks(...a),
  dcteDetectProject: (...a) => mockApi.dcteDetectProject(...a),
  dcteCreateJob: (...a) => mockApi.dcteCreateJob(...a),
  dcteListJobs: (...a) => mockApi.dcteListJobs(...a),
  dcteGetJob: (...a) => mockApi.dcteGetJob(...a),
  dcteStartJob: (...a) => mockApi.dcteStartJob(...a),
  dctePauseJob: (...a) => mockApi.dctePauseJob(...a),
  dcteResumeJob: (...a) => mockApi.dcteResumeJob(...a),
  dcteRollbackJob: (...a) => mockApi.dcteRollbackJob(...a),
  dcteDeleteJob: (...a) => mockApi.dcteDeleteJob(...a),
  dcteGetReports: (...a) => mockApi.dcteGetReports(...a),
  dcteGetEvents: (...a) => mockApi.dcteGetEvents(...a),
  dcteGetTransforms: (...a) => mockApi.dcteGetTransforms(...a),
  dcteBrowseFs: (...a) => mockApi.dcteBrowseFs(...a),
}));

const mockToastError = jest.fn();
const mockToastSuccess = jest.fn();
jest.mock("sonner", () => ({
  toast: {
    error: (...a) => mockToastError(...a),
    success: (...a) => mockToastSuccess(...a),
    message: jest.fn(),
    info: jest.fn(),
  },
}));

jest.mock("@/components/HelpIcon", () => () => <span data-testid="help-icon" />);

// iter-22 — Direct Transform is a project type, so the page reads the active
// project and scopes its jobs to it.
let mockActive = { id: "p-dt-1", name: "PMIS Direct", project_type: "direct_transform" };
jest.mock("@/state/ProjectContext", () => ({
  useProjects: () => ({ active: mockActive, loading: false }),
}));

// iter-21 — the stack catalogue behind the two dropdowns. Mirrors the shape
// of GET /api/dcte/stacks (dcte/stacks.py).
const STACKS = {
  families: ["backend", "frontend", "database"],
  sources: [
    { id: "helidon-mp", label: "Helidon MicroProfile", family: "backend", language: "Java", version: "4.x", role: "source" },
    { id: "jsp", label: "JSP / Jakarta Pages 4.0 (Servlet)", family: "backend", language: "Java", version: "4.0", role: "source" },
    { id: "spring-boot-4", label: "Spring Boot 4.1 (Java 25 LTS)", family: "backend", language: "Java 25", version: "4.1", role: "both" },
    { id: "dotnet-10", label: ".NET 10 LTS (C# / ASP.NET Core)", family: "backend", language: "C# / .NET 10", version: "10.0", role: "both" },
    { id: "react-19", label: "React 19 (TypeScript, Node 26 LTS)", family: "frontend", language: "TypeScript / React 19", version: "19", role: "both" },
    { id: "angular-22", label: "Angular 22 (TypeScript, Node 26 LTS)", family: "frontend", language: "TypeScript / Angular 22", version: "22", role: "both" },
    { id: "oracle", label: "Oracle (SQL / PL-SQL)", family: "database", language: "Oracle SQL", version: "", role: "source" },
  ],
  targets: [
    { id: "spring-boot-4", label: "Spring Boot 4.1 (Java 25 LTS)", family: "backend", language: "Java 25", version: "4.1", role: "both" },
    { id: "spring-boot-3", label: "Spring Boot 3.x (Java 21) — legacy target", family: "backend", language: "Java 21", version: "3.5", role: "both" },
    { id: "dotnet-10", label: ".NET 10 LTS (C# / ASP.NET Core)", family: "backend", language: "C# / .NET 10", version: "10.0", role: "both" },
    { id: "react-19", label: "React 19 (TypeScript, Node 26 LTS)", family: "frontend", language: "TypeScript / React 19", version: "19", role: "both" },
    { id: "angular-22", label: "Angular 22 (TypeScript, Node 26 LTS)", family: "frontend", language: "TypeScript / Angular 22", version: "22", role: "both" },
    { id: "postgres-18", label: "PostgreSQL 18", family: "database", language: "PostgreSQL SQL / PL-pgSQL", version: "18", role: "both" },
    { id: "postgres-15", label: "PostgreSQL 15 — legacy target", family: "database", language: "PostgreSQL SQL / PL-pgSQL", version: "15", role: "both" },
  ],
};

const PLUGINS = [
  {
    id: "helidon-mp-to-spring-boot-3",
    display_name: "Helidon MicroProfile → Spring Boot 3",
    source_stack: "helidon-mp",
    target_stack: "spring-boot-3",
    version: "0.1.0",
  },
  {
    id: "oracle-to-postgres",
    display_name: "Oracle → PostgreSQL",
    source_stack: "oracle",
    target_stack: "postgres-15",
    version: "0.1.0",
  },
];

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/direct-transform"]}>
      <DirectTransform />
    </MemoryRouter>,
  );

beforeEach(() => {
  jest.clearAllMocks();
  window.localStorage.clear();
  mockActive = { id: "p-dt-1", name: "PMIS Direct", project_type: "direct_transform" };
  mockApi.dcteListPlugins.mockResolvedValue({ plugins: PLUGINS });
  mockApi.dcteListStacks.mockResolvedValue(STACKS);
  mockApi.dcteListJobs.mockResolvedValue({ jobs: [] });
  mockApi.dcteGetEvents.mockResolvedValue({ events: [] });
  mockApi.dcteGetReports.mockResolvedValue({ reports: [] });
  mockApi.dcteGetTransforms.mockResolvedValue({ transforms: [] });
});

describe("Direct Transform page", () => {
  it("renders the project page shell and loads the stack catalogue", async () => {
    renderPage();
    expect(await screen.findByTestId("dcte-title")).toHaveTextContent("PMIS Direct");
    await waitFor(() => expect(mockApi.dcteListStacks).toHaveBeenCalled());

    const source = await screen.findByTestId("dcte-select-source-0");
    const target = await screen.findByTestId("dcte-select-target-0");
    expect(within(source).getAllByRole("option")).toHaveLength(STACKS.sources.length);
    expect(within(target).getAllByRole("option")).toHaveLength(STACKS.targets.length);
  });

  it("offers JSP, React, Angular, .NET and Spring Boot from the dropdowns", async () => {
    // The five the operator asked for. Source and target are independent, so
    // each is checked in the list it belongs to rather than as a fixed pair.
    renderPage();
    const source = await screen.findByTestId("dcte-select-source-0");
    const target = await screen.findByTestId("dcte-select-target-0");
    const ids = (el) => within(el).getAllByRole("option").map((o) => o.value);

    expect(ids(source)).toEqual(expect.arrayContaining(["jsp"]));
    expect(ids(target)).toEqual(
      expect.arrayContaining(["react-19", "angular-22", "dotnet-10", "spring-boot-4"]),
    );
  });

  it("groups the stacks by family so a long list stays scannable", async () => {
    renderPage();
    const target = await screen.findByTestId("dcte-select-target-0");
    const groups = [...target.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groups).toEqual(["Backend", "Frontend", "Database"]);
  });

  it("says whether the chosen pair is deterministic or runs on the AI pass", async () => {
    const user = userEvent.setup();
    renderPage();
    // Default is helidon-mp -> spring-boot-3, which has a real transformer.
    expect(await screen.findByTestId("dcte-pair-mode-0"))
      .toHaveTextContent("Deterministic transformer available");

    await user.selectOptions(screen.getByTestId("dcte-select-source-0"), "jsp");
    await user.selectOptions(screen.getByTestId("dcte-select-target-0"), "react-19");
    expect(screen.getByTestId("dcte-pair-mode-0")).toHaveTextContent("AI pass");
  });

  it("keeps a stack that has left the catalogue selectable rather than silently re-pointing the job", async () => {
    mockApi.dcteListStacks.mockResolvedValue({
      ...STACKS,
      sources: STACKS.sources.filter((x) => x.id !== "helidon-mp"),
    });
    renderPage();
    const source = await screen.findByTestId("dcte-select-source-0");
    await waitFor(() => expect(source).toHaveValue("helidon-mp"));
    expect(within(source).getByText(/not in catalogue/)).toBeInTheDocument();
  });

  it("shows an empty state instead of a blank panel when there are no jobs", async () => {
    renderPage();
    expect(await screen.findByText("No Direct Transform jobs yet.")).toBeInTheDocument();
  });

  it("detect fills in the source and target stacks from the fingerprint", async () => {
    const user = userEvent.setup();
    mockApi.dcteDetectProject.mockResolvedValue({
      path: "/srv/legacy",
      exists: true,
      is_valid: true,
      detected_stack: "oracle",
      confidence: 0.8,
      hints: [],
      suggested_target: "postgres-15",
    });
    renderPage();
    await user.type(await screen.findByTestId("dcte-input-src-0"), "/srv/legacy");
    await user.click(screen.getByTestId("dcte-btn-detect-0"));

    await waitFor(() => expect(mockApi.dcteDetectProject).toHaveBeenCalledWith("/srv/legacy"));
    expect(await screen.findByTestId("dcte-detection-0")).toHaveTextContent("oracle");
    expect(screen.getByTestId("dcte-select-source-0")).toHaveValue("oracle");
    expect(screen.getByTestId("dcte-select-target-0")).toHaveValue("postgres-15");
  });

  it("refuses to start a job when a service is missing its paths", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId("dcte-btn-start"));
    expect(mockApi.dcteCreateJob).not.toHaveBeenCalled();
    expect(mockToastError).toHaveBeenCalledWith(
      "Every service needs source_path and destination_path",
    );
  });

  it("creates and starts a job with the roots derived from service 1", async () => {
    const user = userEvent.setup();
    mockApi.dcteCreateJob.mockResolvedValue({ id: "dcte_abc123" });
    mockApi.dcteStartJob.mockResolvedValue({ id: "dcte_abc123", status: "analyzing" });
    mockApi.dcteGetJob.mockResolvedValue({
      id: "dcte_abc123", name: "Direct Transform run 1", status: "analyzing",
      progress: 0.1, services: [{ id: "svc_1" }], error: null,
    });

    renderPage();
    await user.type(await screen.findByTestId("dcte-input-src-0"), "/srv/in");
    await user.type(screen.getByTestId("dcte-input-dst-0"), "/srv/out");
    await user.click(screen.getByTestId("dcte-btn-start"));

    await waitFor(() => expect(mockApi.dcteCreateJob).toHaveBeenCalled());
    const payload = mockApi.dcteCreateJob.mock.calls[0][0];
    expect(payload).toMatchObject({
      project_id: "p-dt-1",
      source_root: "/srv/in",
      output_root: "/srv/out",
      ai_refactor: true,
      generate_cicd: ["github"],
      use_droid_agent: false,
    });
    expect(payload.services[0]).toMatchObject({
      source_path: "/srv/in",
      destination_path: "/srv/out",
    });
    expect(mockApi.dcteStartJob).toHaveBeenCalledWith("dcte_abc123");
  });

  it("degrades the mode hint, not the dropdowns, when the plugin list fails", async () => {
    mockApi.dcteListPlugins.mockRejectedValue(new Error("network down"));
    renderPage();
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Could not load transformation plugins"),
    );
    // The selects come from /stacks, so they are still usable; only the
    // deterministic-vs-AI hint is unknown.
    const source = await screen.findByTestId("dcte-select-source-0");
    expect(within(source).getAllByRole("option").length).toBeGreaterThan(1);
    expect(screen.getByTestId("dcte-pair-mode-0")).toHaveTextContent("mode is unknown");
  });

  it("says so when the stack catalogue cannot be loaded, instead of an empty picker", async () => {
    mockApi.dcteListStacks.mockRejectedValue(new Error("network down"));
    renderPage();
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Could not load the stack catalogue"),
    );
    const source = await screen.findByTestId("dcte-select-source-0");
    expect(within(source).getByText(/Stacks unavailable/)).toBeInTheDocument();
  });

  it("says so when the job list cannot be loaded", async () => {
    mockApi.dcteListJobs.mockRejectedValue(new Error("network down"));
    renderPage();
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Could not load Direct Transform jobs"),
    );
    expect(screen.getByTestId("dcte-page")).toBeInTheDocument();
  });

  it("scopes the job list to the active project", async () => {
    // Unscoped, every Direct Transform project would list every other
    // project's jobs — which is why this is a project type, not a Tools page.
    renderPage();
    await waitFor(() =>
      expect(mockApi.dcteListJobs).toHaveBeenCalledWith(null, "p-dt-1"));
  });

  it("reloads and clears the selection when the project changes", async () => {
    const { rerender } = renderPage();
    await waitFor(() => expect(mockApi.dcteListJobs).toHaveBeenCalledWith(null, "p-dt-1"));

    mockActive = { id: "p-dt-2", name: "Other", project_type: "direct_transform" };
    rerender(
      <MemoryRouter initialEntries={["/direct-transform"]}>
        <DirectTransform />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(mockApi.dcteListJobs).toHaveBeenCalledWith(null, "p-dt-2"));
  });

  it("asks for a project instead of rendering an unusable form when none is active", async () => {
    mockActive = null;
    renderPage();
    expect(await screen.findByTestId("dcte-no-project")).toHaveTextContent(
      /New Project/);
    // Nothing is fetched for a project that does not exist.
    expect(mockApi.dcteListJobs).not.toHaveBeenCalled();
  });

  it("surfaces a backend failure as a toast, not an unhandled rejection", async () => {
    const user = userEvent.setup();
    mockApi.dcteCreateJob.mockRejectedValue(new Error("boom"));
    renderPage();
    await user.type(await screen.findByTestId("dcte-input-src-0"), "/srv/in");
    await user.type(screen.getByTestId("dcte-input-dst-0"), "/srv/out");
    await user.click(screen.getByTestId("dcte-btn-start"));

    await waitFor(() => expect(mockToastError).toHaveBeenCalledWith("Job creation failed"));
    expect(screen.getByTestId("dcte-page")).toBeInTheDocument();
  });
});
