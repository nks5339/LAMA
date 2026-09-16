/**
 * Discovery — the app's landing route, and where the P0 bug shipped.
 *
 * `kbStatus` was never imported; the call resolved to the useState variable
 * of the same name, threw "kbStatus is not a function", and an empty catch
 * hid it. Every metric tile read 0 on every load, and `kbReady` stayed
 * false — which gated the Generate SRS step behind a card that looked
 * pressable and silently did nothing.
 *
 * The first test here fails on the original code. The rest pin the
 * surrounding behaviour so the fix cannot be undone quietly.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

const mockKbStatus = jest.fn();
const mockSkipStage = jest.fn();
let mockActive = { id: "p1", name: "PMIS", stage_status: {} };

jest.mock("@/lib/api", () => ({
  kbStatus: (...a) => mockKbStatus(...a),
  skipStage: (...a) => mockSkipStage(...a),
}));

jest.mock("@/state/ProjectContext", () => ({
  useProjects: () => ({ active: mockActive }),
}));

// The heavy children are exercised by their own suites; stub them so this
// one is about the page's own logic.
jest.mock("@/components/UploadPanelV2", () => () => <div data-testid="upload-panel" />);
jest.mock("@/components/DataSourcePanel", () => () => <div data-testid="data-source-panel" />);
jest.mock("@/components/SRSPanel", () => () => <div data-testid="srs-panel" />);
jest.mock("@/components/TargetStackSuggester", () => () => <div data-testid="stack-suggester" />);
jest.mock("@/components/FloatingChat", () => () => <div data-testid="floating-chat" />);

const mockToastError = jest.fn();
jest.mock("sonner", () => ({
  toast: {
    error: (...a) => mockToastError(...a),
    success: jest.fn(),
    message: jest.fn(),
    info: jest.fn(),
  },
}));

// Imported after the jest.mock calls above — Jest hoists those, and the
// component must resolve its mocked dependencies at import time.
import DiscoveryV2 from "@/pages/DiscoveryV2";

const renderPage = () =>
  render(
    <MemoryRouter>
      <DiscoveryV2 />
    </MemoryRouter>
  );

beforeEach(() => {
  jest.clearAllMocks();
  mockActive = { id: "p1", name: "PMIS", stage_status: {} };
  mockKbStatus.mockResolvedValue({ files: 1284, entities: 9317, chunks: 24806 });
});

describe("Discovery — knowledge-base status (P0-1)", () => {
  it("calls the API function, not the state variable of the same name", async () => {
    // The regression guard. On the original code this never fired.
    renderPage();
    await waitFor(() => expect(mockKbStatus).toHaveBeenCalledWith("p1"));
  });

  it("renders the real counts instead of zeroes", async () => {
    renderPage();
    expect(await screen.findByText("1284")).toBeInTheDocument();
    expect(screen.getByText("9317")).toBeInTheDocument();
    expect(screen.getByText("24806")).toBeInTheDocument();
  });

  it("marks the tiles busy while loading", async () => {
    let resolve;
    mockKbStatus.mockReturnValue(new Promise((r) => { resolve = r; }));
    const { container } = renderPage();
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
    resolve({ files: 1 });
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).not.toBeInTheDocument()
    );
  });

  it("surfaces a load failure instead of silently showing zeroes", async () => {
    // The empty `catch (_) {}` is what made the original bug invisible.
    mockKbStatus.mockRejectedValue(new Error("Qdrant unreachable"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /couldn't load knowledge-base status/i
    );
    expect(mockToastError).toHaveBeenCalled();
  });

  it("offers a retry that re-calls the API", async () => {
    mockKbStatus.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    const retry = await screen.findByRole("button", { name: "Retry" });
    mockKbStatus.mockResolvedValue({ files: 7 });
    await userEvent.click(retry);
    await waitFor(() => expect(mockKbStatus).toHaveBeenCalledTimes(2));
  });
});

describe("Discovery — the SRS step gate", () => {
  it("blocks the SRS step and says why when the KB is empty", async () => {
    mockKbStatus.mockResolvedValue({ files: 0, entities: 0, chunks: 0 });
    renderPage();
    const step = await screen.findByTestId("step-srs");
    expect(step).toHaveAttribute("aria-disabled", "true");
    // The old version was `onClick={() => kbReady && setActiveTab("srs")}` —
    // pressable-looking, silent, no reason given.
    expect(step).toHaveAccessibleDescription(/build the knowledge base first/i);
  });

  it("enables the SRS step once the KB has content", async () => {
    renderPage();
    const step = await screen.findByTestId("step-srs");
    expect(step).not.toHaveAttribute("aria-disabled");
  });

  it("switches to the SRS panel when the step is pressed", async () => {
    renderPage();
    await userEvent.click(await screen.findByTestId("step-srs"));
    expect(screen.getByTestId("srs-panel")).toBeInTheDocument();
  });

  it("keeps a keyboard path between panels via a real tab list", async () => {
    // The tab list used to be className="hidden", discarding Radix's
    // roving-tabindex model entirely.
    renderPage();
    await waitFor(() => expect(mockKbStatus).toHaveBeenCalled());
    expect(screen.getByRole("tablist")).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(2);
  });
});

describe("Discovery — onboarding and empty states", () => {
  it("shows the onboarding banner only before the first upload", async () => {
    mockKbStatus.mockResolvedValue({ files: 0 });
    renderPage();
    expect(await screen.findByTestId("onboarding-banner")).toBeInTheDocument();
  });

  it("hides the banner once files exist", async () => {
    renderPage();
    await waitFor(() => expect(mockKbStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("onboarding-banner")).not.toBeInTheDocument();
  });

  it("gives the no-project state a way out", () => {
    mockActive = null;
    renderPage();
    expect(screen.getByTestId("empty-state")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /project switcher/i })).toBeInTheDocument();
  });
});

describe("Discovery — skip to Architecture", () => {
  it("is offered only once Discovery is frozen", async () => {
    renderPage();
    await waitFor(() => expect(mockKbStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("skip-to-architecture-btn")).not.toBeInTheDocument();

    mockActive = { id: "p1", name: "PMIS", stage_status: { Discovery: "frozen" } };
    renderPage();
    expect(await screen.findByTestId("skip-to-architecture-btn")).toBeInTheDocument();
  });

  it("forces the skip so an already-skipped stage still passes", async () => {
    mockActive = { id: "p1", name: "PMIS", stage_status: { Discovery: "frozen" } };
    mockSkipStage.mockResolvedValue({});
    renderPage();
    await userEvent.click(await screen.findByTestId("skip-to-architecture-btn"));
    await waitFor(() =>
      expect(mockSkipStage).toHaveBeenCalledWith("p1", "DataModel", { force: true })
    );
  });
});
