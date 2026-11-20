import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/render";
import { useUiStore } from "../../../lib/store";
import type { RealHoldingsSummary } from "../../../lib/api";

const holdingsState = vi.hoisted(() => ({ current: {} as { data?: unknown; isLoading: boolean } }));
const donut = vi.hoisted(() => ({ slices: [] as Array<{ name: string; value: number }> }));

vi.mock("../hooks/useRealHoldings", () => ({ useRealHoldings: () => holdingsState.current }));
vi.mock("../hooks/useRealPortfolioSummary", () => ({ useRealPortfolioSummary: () => ({ data: undefined, isLoading: false }) }));
vi.mock("../hooks/useRealPortfolioRisk", () => ({ useRealPortfolioRisk: () => ({ data: undefined, isLoading: false }) }));
vi.mock("../hooks/useRealRebalance", () => ({ useRealRebalance: () => ({ data: undefined, isLoading: false }) }));
vi.mock("../hooks/useBackfillJob", () => ({ useBackfillJob: () => ({ isRunning: false, start: vi.fn() }) }));
vi.mock("../../../components/charts/DonutChart", () => ({
  DonutChart: ({ slices }: { slices: Array<{ name: string; value: number }> }) => {
    donut.slices = slices;
    return null;
  },
}));

import { RealHoldingsView } from "./RealHoldingsView";

const MSCI_WORLD = "IE00B4L5Y983";

const summary: RealHoldingsSummary = {
  total_value: 10120,
  positions: [
    { isin: MSCI_WORLD, ticker: "EUNL.DE", name: "iShares Core MSCI World", quantity: 100, current_price: 100, current_value: 10000, weight: 10000 / 10120, avg_buy_price: 70, cost_basis: 7000, unrealized_pnl: 3000, source: "dkb", broker: "DKB", account_id: "d1" },
    { isin: MSCI_WORLD, ticker: null, name: "iShares Core MSCI World", quantity: 1, current_price: 100, current_value: 100, weight: 100 / 10120, avg_buy_price: 95, cost_basis: 95, unrealized_pnl: 5, source: "scalable", broker: "Scalable Capital", account_id: "s1" },
    { isin: "IE00BK5BQT80", ticker: "VWCE.DE", name: "Vanguard FTSE All-World", quantity: 0.2, current_price: 100, current_value: 20, weight: 20 / 10120, avg_buy_price: null, cost_basis: null, unrealized_pnl: null, source: "scalable", broker: "Scalable Capital", account_id: "s1" },
  ],
  by_ticker: {},
  by_broker: {
    dkb: { broker: "DKB", total_value: 10000, weight: 10000 / 10120, position_count: 1, cost_basis: 7000, costed_value: 10000, unrealized_pnl: 3000, unrealized_pnl_pct: 3000 / 7000 },
    scalable: { broker: "Scalable Capital", total_value: 120, weight: 120 / 10120, position_count: 2, cost_basis: 95, costed_value: 100, unrealized_pnl: 5, unrealized_pnl_pct: 5 / 95 },
  },
  position_count: 3,
  positions_with_ticker: 2,
  isin_only_count: 1,
};

beforeEach(() => {
  useUiStore.setState({ brokerFilter: "all" });
  holdingsState.current = { data: summary, isLoading: false };
  donut.slices = [];
});

describe("RealHoldingsView with two brokers", () => {
  it("lists every depot with its broker badge and one slice per fund", () => {
    renderWithProviders(<RealHoldingsView />);
    const list = screen.getByRole("heading", { name: "Positions" }).parentElement!;
    expect(within(list).getAllByText("Scalable")).toHaveLength(2);
    expect(within(list).getAllByText("DKB")).toHaveLength(1);
    // The MSCI World at both depots is one slice, under DKB's ticker.
    expect(donut.slices).toEqual([
      { name: "EUNL.DE", value: 10100 },
      { name: "VWCE.DE", value: 20 },
    ]);
  });

  it("shows each depot's value and unrealised P&L next to the total", () => {
    renderWithProviders(<RealHoldingsView />);
    const depots = screen.getByRole("region", { name: "Per depot" });
    expect(within(depots).getByText("All depots")).toBeInTheDocument();
    expect(within(depots).getByText(/\+3\.000,00\s€/)).toBeInTheDocument();
    // Scalable's P&L covers only the position with a known cost.
    expect(within(depots).getByText(/covers the 100,00\s€ with a known cost/)).toBeInTheDocument();
  });

  it("narrows the positions and the total to one broker, and says the risk metrics stay whole-book", async () => {
    renderWithProviders(<RealHoldingsView />);
    await userEvent.click(screen.getByRole("radio", { name: "Scalable" }));
    expect(useUiStore.getState().brokerFilter).toBe("scalable");
    const list = screen.getByRole("heading", { name: "Positions" }).parentElement!;
    expect(within(list).queryByText("DKB")).toBeNull();
    expect(within(list).getAllByText("Scalable")).toHaveLength(2);
    // 83 % of this depot, though about 1 % of the whole book: it gets the concentration warning.
    expect(within(list).getByText(/^83[,.]33\s?%$/)).toHaveClass("text-warn");
    expect(screen.getByText("Depot Value")).toBeInTheDocument();
    expect(screen.getAllByText(/^120,00\s€$/).length).toBeGreaterThanOrEqual(2); // the strip and the depot card
    expect(screen.getByText(/cover every depot together/)).toBeInTheDocument();
    expect(donut.slices).toEqual([
      { name: "IE00B4L5Y983", value: 100 },
      { name: "VWCE.DE", value: 20 },
    ]);
  });

  it("hides the filter and the per-depot cards with one broker", () => {
    holdingsState.current = {
      isLoading: false,
      data: { ...summary, positions: [summary.positions[0]], by_broker: { dkb: summary.by_broker!.dkb }, position_count: 1 },
    };
    renderWithProviders(<RealHoldingsView />);
    expect(screen.queryByRole("radio", { name: "Scalable" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Per depot" })).toBeNull();
  });

  it("falls back to all when the stored filter names a broker with no positions here", () => {
    useUiStore.setState({ brokerFilter: "scalable" });
    holdingsState.current = {
      isLoading: false,
      data: { ...summary, positions: [summary.positions[0]], by_broker: { dkb: summary.by_broker!.dkb }, position_count: 1 },
    };
    renderWithProviders(<RealHoldingsView />);
    expect(screen.getByText("iShares Core MSCI World")).toBeInTheDocument();
  });
});
