import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../components/ui/tooltip";
import type { AttentionItem, MonthlyPlan, PendingRecommendation, WealthSummary } from "../lib/api";
import { HomePage } from "./HomePage";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  getMacroRegime: vi.fn(),
  getMonthlyPlan: vi.fn(),
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
      if (path === "/api/portfolio/advisor/pending") return { items: PENDING, total: 1 };
      throw new Error(`unexpected ${path}`);
    });
    mocks.getMacroRegime.mockResolvedValue({
      label: "bull", confidence: 0.8, vix: 14, yield_spread: 0.5, yield_spread_10y3m: 0.5, yield_spread_10y2y: null,
      momentum_3m: 0.04, updated_at: "2026-10-01T06:00:00Z",
    });
    mocks.getMonthlyPlan.mockResolvedValue(PLAN);
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

  it("shows the status strip: value, regime, alerts, the book per broker with its last sync, and a Sync DKB button", async () => {
    renderHome();

    const strip = screen.getByRole("region", { name: "Status" });
    expect(await within(strip).findByText(/12\.345,67/)).toBeInTheDocument();
    expect(await within(strip).findByRole("button", { name: "Bull" })).toBeInTheDocument();
    expect(await within(strip).findByRole("link", { name: "2 unread alerts" })).toHaveAttribute("href", "/decide");
    const split = await within(strip).findByRole("list", { name: "Where the book is held" });
    expect(within(split).getByText("synced 3 hours ago")).toBeInTheDocument();
    expect(within(split).getByText("never synced")).toBeInTheDocument();
    expect(within(split).getByText("82 %")).toBeInTheDocument();
    expect(within(strip).getByRole("button", { name: /sync dkb/i })).toBeInTheDocument();
  });

  it("lists what needs you, problems only, with its fix link", async () => {
    renderHome();

    const section = screen.getByRole("heading", { name: "Needs you" }).closest("section")!;
    expect(await within(section).findByText("DKB sync is failing")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: /open bank settings/i })).toHaveAttribute("href", "/settings/bank");
    expect(within(section).queryByText("Add a Telegram bot")).not.toBeInTheDocument();
  });

  it("says all is good when nothing needs you", async () => {
    mocks.getSettingsAttention.mockResolvedValue({ items: [item({ severity: "info" })], pending_restart: [] });
    renderHome();

    expect(await screen.findByText(/nothing needs fixing/i)).toBeInTheDocument();
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

  it("says nothing is waiting when Decide is empty", async () => {
    mocks.api.mockImplementation(async (path: string) => (path === "/api/portfolio/wealth" ? WEALTH : { items: [], total: 0 }));
    mocks.listNotifications.mockResolvedValue({ items: [], unread_count: 0 });
    renderHome();

    expect(await screen.findByText("Nothing is waiting for a decision.")).toBeInTheDocument();
  });

  it("shows a loading skeleton, not stale numbers, before the data arrives", () => {
    mocks.api.mockImplementation(() => new Promise(() => {}));
    mocks.getMonthlyPlan.mockImplementation(() => new Promise(() => {}));
    renderHome();

    expect(screen.queryByText(/12,345/)).not.toBeInTheDocument();
    expect(screen.queryByText("Needs you")).toBeInTheDocument();
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
