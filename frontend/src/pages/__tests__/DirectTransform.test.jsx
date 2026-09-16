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
  mockApi.dcteListPlugins.mockResolvedValue({ plugins: PLUGINS });
  mockApi.dcteListJobs.mockResolvedValue({ jobs: [] });
  mockApi.dcteGetEvents.mockResolvedValue({ events: [] });
  mockApi.dcteGetReports.mockResolvedValue({ reports: [] });
  mockApi.dcteGetTransforms.mockResolvedValue({ transforms: [] });
});

describe("Direct Transform page", () => {
  it("renders the shared Tools page shell and loads the plugin list", async () => {
    renderPage();
    expect(await screen.findByTestId("dcte-title")).toHaveTextContent("Direct Transform");
    await waitFor(() => expect(mockApi.dcteListPlugins).toHaveBeenCalled());
    const picker = await screen.findByTestId("dcte-select-stack-0");
    expect(within(picker).getAllByRole("option")).toHaveLength(2);
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
    expect(screen.getByTestId("dcte-select-stack-0")).toHaveValue("oracle→postgres-15");
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

  it("says so when the plugin list cannot be loaded, instead of an empty picker", async () => {
    mockApi.dcteListPlugins.mockRejectedValue(new Error("network down"));
    renderPage();
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Could not load transformation plugins"),
    );
    const picker = await screen.findByTestId("dcte-select-stack-0");
    expect(within(picker).getByRole("option")).toHaveTextContent("Plugins unavailable");
  });

  it("says so when the job list cannot be loaded", async () => {
    mockApi.dcteListJobs.mockRejectedValue(new Error("network down"));
    renderPage();
    await waitFor(() =>
      expect(mockToastError).toHaveBeenCalledWith("Could not load Direct Transform jobs"),
    );
    expect(screen.getByTestId("dcte-page")).toBeInTheDocument();
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
