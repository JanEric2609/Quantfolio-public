import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../components/ui/tooltip";
import type { AttentionItem, MonthlyPlan, PendingRecommendation, WealthSummary } from "../lib/api";
import { HomePage } from "./HomePage";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  getMacroRegime: vi.fn(),
  getMonthlyPlan: vi.fn(),
  getSettings: vi.fn(),
  getSettingsAttention: vi.fn(),
  listNotifications: vi.fn(),
}));
vi.mock("../lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../lib/api")>()), ...mocks }));

const WEALTH = {
  currency: "EUR",
  total_value: 12345.67,
  accounts: [
    { id: "a1", source: "dkb", name: "DKB Giro", type: "checking", balance: 100, currency: "EUR", last_synced: new Date(Date.now() - 3 * 3600_000).toISOString() },
    { id: "a2", source: "manual", name: "Scalable", type: "broker", balance: 0, currency: "EUR", last_synced: new Date().toISOString() },
  ],
  positions: [],
  by_broker: [
    { source: "dkb", label: "DKB", securities: 10000, cash: 100, total: 10100, last_synced: new Date(Date.now() - 3 * 3600_000).toISOString() },
    { source: "scalable", label: "Scalable Capital", securities: 2000, cash: 245.67, total: 2245.67, last_synced: null },
  ],
} as unknown as WealthSummary;

const PLAN = {
  headline: "Let your 1.000 € savings plan run. Nothing else to do this month.",
  no_change: true,
  never_sells: true,
} as unknown as MonthlyPlan;

function item(overrides: Partial<AttentionItem>): AttentionItem {
  return { id: "x", severity: "error", title: "Title", detail: "Detail", href: "/settings/bank", action: "Fix it", ...overrides };
}

const PENDING: PendingRecommendation[] = [
  { id: "r1", ticker: "AAA", name: null, verdict: "WATCH", confidence: 0.5, horizon: "mid", mode: "discover", approval_state: "approved_candidate", created_at: null, expires_at: null, summary: "", resurfaced: false },
];

function renderHome() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter><HomePage /></MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("HomePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/api/portfolio/wealth") return WEALTH;
      if (path === "/api/portfolio/advisor/pending") return { items: PENDING, total: 1, research_count: 0 };
      throw new Error(`unexpected ${path}`);
    });
    mocks.getMacroRegime.mockResolvedValue({
      label: "bull", confidence: 0.8, vix: 14, yield_spread: 0.5, yield_spread_10y3m: 0.5, yield_spread_10y2y: null,
      momentum_3m: 0.04, updated_at: "2026-10-01T06:00:00Z",
    });
    mocks.getMonthlyPlan.mockResolvedValue(PLAN);
    mocks.getSettings.mockResolvedValue({ settings: { scalable_sync_hours: 6 }, integrations: {} });
    mocks.getSettingsAttention.mockResolvedValue({
      items: [
        item({ id: "dkb", title: "DKB sync is failing", detail: "The last 3 syncs failed.", action: "Open bank settings" }),
        item({ id: "tip", severity: "info", title: "Add a Telegram bot" }),
      ],
      pending_restart: [],
    });
    mocks.listNotifications.mockResolvedValue({ items: [], unread_count: 2 });
  });

  it("has one h1 and links to the detailed overview", async () => {
    renderHome();

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Home");
    expect(screen.getByRole("link", { name: /detailed overview/i })).toHaveAttribute("href", "/dashboard");
  });

  it("shows the value, the book per broker with its last sync, and the regime as a chip in the header", async () => {
    renderHome();

    const strip = screen.getByRole("region", { name: "Status" });
    expect(await within(strip).findByText(/12\.345,67/)).toBeInTheDocument();
    const split = await within(strip).findByRole("list", { name: "Where the book is held" });
    expect(within(split).getByText("synced 3 hours ago")).toBeInTheDocument();
    expect(within(split).getByText("never synced")).toBeInTheDocument();
    expect(within(split).getByText("82 %")).toBeInTheDocument();
    // The regime is a small chip beside the page title, not a stat block; alerts are not on Home.
    expect(await screen.findByRole("button", { name: "Bull" })).toBeInTheDocument();
    expect(within(strip).queryByRole("button", { name: "Bull" })).not.toBeInTheDocument();
    expect(screen.queryByText("Regime")).not.toBeInTheDocument();
    expect(screen.queryByText("Alerts")).not.toBeInTheDocument();
  });

  it("offers a sync button only for a broker that is stale, never synced or failing", async () => {
    mocks.getSettingsAttention.mockResolvedValue({ items: [], pending_restart: [] });
    renderHome();

    const split = await screen.findByRole("list", { name: "Where the book is held" });
    // DKB synced 3 hours ago: only the small text. Scalable never synced: its button shows.
    expect(within(split).queryByRole("button", { name: /sync dkb/i })).not.toBeInTheDocument();
    expect(within(split).getByText("synced 3 hours ago")).toBeInTheDocument();
  });

  it("offers Sync DKB when the last DKB sync is older than a day", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/api/portfolio/wealth") {
        return { ...WEALTH, by_broker: [{ ...WEALTH.by_broker![0], last_synced: new Date(Date.now() - 30 * 3600_000).toISOString() }] };
      }
      return { items: [], total: 0 };
    });
    renderHome();

    expect(await screen.findByRole("button", { name: /sync dkb/i })).toBeInTheDocument();
  });

  it("offers Sync DKB when the DKB sync is failing, even if recent", async () => {
    renderHome();

    expect(await screen.findByRole("button", { name: /sync dkb/i })).toBeInTheDocument();
  });

  it("lists what needs you, problems only, with its fix link", async () => {
    renderHome();

    const section = (await screen.findByRole("heading", { name: "Needs you" })).closest("section")!;
    expect(await within(section).findByText("DKB sync is failing")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: /open bank settings/i })).toHaveAttribute("href", "/settings/bank");
    expect(within(section).queryByText("Add a Telegram bot")).not.toBeInTheDocument();
  });

  it("hides the Needs you section when only info items exist, with no all-good box", async () => {
    mocks.getSettingsAttention.mockResolvedValue({ items: [item({ severity: "info" })], pending_restart: [] });
    renderHome();

    await screen.findByText(PLAN.headline);
    await waitFor(() => expect(mocks.getSettingsAttention).toHaveBeenCalled());
    expect(screen.queryByText("Needs you")).not.toBeInTheDocument();
    expect(screen.queryByText(/all good/i)).not.toBeInTheDocument();
  });

  it("shows this month's headline and a link to the plan", async () => {
    renderHome();

    expect(await screen.findByText(PLAN.headline)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open the plan/i })).toHaveAttribute("href", "/plan");
    expect(screen.getByText("No change")).toBeInTheDocument();
  });

  it("teases Decide with what is waiting", async () => {
    renderHome();

    expect(await screen.findByText(/1 recommendation to decide, 2 unread alerts/)).toBeInTheDocument();
    expect(screen.getByText("AAA")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /review/i })).toHaveAttribute("href", "/decide");
  });

  it("shows one muted line, not a card, when only research ideas exist", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "/api/portfolio/wealth" ? WEALTH : { items: [], total: 0, research_count: 4 });
    renderHome();

    expect(await screen.findByText(/4 ideas are in Discover as research/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /review/i })).not.toBeInTheDocument();
  });

  it("shows nothing for Decide when nothing is waiting", async () => {
    mocks.api.mockImplementation(async (path: string) => (path === "/api/portfolio/wealth" ? WEALTH : { items: [], total: 0 }));
    renderHome();

    await screen.findByText(PLAN.headline);
    expect(screen.queryByText("Decide")).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing is waiting/i)).not.toBeInTheDocument();
  });

  it("shows a loading skeleton, not stale numbers, before the data arrives", () => {
    mocks.api.mockImplementation(() => new Promise(() => {}));
    mocks.getMonthlyPlan.mockImplementation(() => new Promise(() => {}));
    renderHome();

    expect(screen.queryByText(/12,345/)).not.toBeInTheDocument();
  });

  it("keeps the rest of the page when one part fails, each with its own retry", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/api/portfolio/wealth") throw new Error("down");
      return { items: [], total: 0 };
    });
    mocks.getMonthlyPlan.mockRejectedValue(new Error("down"));
    mocks.getSettingsAttention.mockRejectedValue(new Error("down"));
    renderHome();

    expect(await screen.findByText("Could not load your portfolio value.")).toBeInTheDocument();
    expect(await screen.findByText("Could not load this month's plan.")).toBeInTheDocument();
    expect(await screen.findByText("Could not load the list.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(3);
    expect(screen.getByRole("link", { name: /open the plan/i })).toBeInTheDocument();
  });
});
