import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Trading212Pies, Trading212SyncButton } from "./Trading212Sync";

vi.mock("../../lib/api", () => ({
  getTrading212Status: async () => ({
    source: "trading212", enabled: true, state: "key_scope", environment: "live",
    message: "The API key may not read /equity/history/orders", error_code: "forbidden",
    last_sync_at: null, last_success_at: null, stale: false, account_id: "4711", tracking_since: "2026-10-26",
    positions: 2, pies: 1, pies_synced_at: "2026-10-26T10:00:00Z", pies_unavailable: true, pies_stale: false,
    interest_30d_eur: "1.00", interest_rate_measured: "0.0399", pending_reconciliation: 0, read_only: true,
  }),
  getTrading212Pies: async () => [
    {
      id: "77", name: "Dividends", status: "ON_TRACK", cash: 5, invested: 600, value: 700, result: 100,
      result_pct: 0.1667, dividends_gained: 4, dividends_reinvested: 0, dividends_in_cash: 4,
      dividend_action: "TO_ACCOUNT_CASH", goal: 1000, end_date: null, synced_at: "2026-10-26T10:00:00Z",
      instruments: [
        {
          t212_ticker: "VHYLl_EQ", isin: "IE00B8GKDB10", name: "Vanguard All-World High Div", expected_share: "0.6",
          current_share: "0.55", owned_quantity: "10", value: "700", result: "100",
          issues: [{ name: "APPROACHING_MAX_POSITION_SIZE", severity: "INFORMATIVE" }],
        },
      ],
    },
  ],
  syncTrading212: vi.fn(),
  resetTrading212Data: vi.fn(),
}));

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Trading212SyncButton", () => {
  it("names a missing key permission, links to the fix and shows the measured cash interest", async () => {
    wrap(<Trading212SyncButton />);
    expect(await screen.findByText(/key lacks a permission/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fix" })).toHaveAttribute("href", "/settings/bank?connection=trading212");
    expect(screen.getByText(/cash interest 3\.99 % p\.a\. \(measured\)/)).toBeInTheDocument();
  });
});

describe("Trading212Pies", () => {
  it("shows target against actual share per holding and says when the last read failed", async () => {
    wrap(<Trading212Pies />);
    const pie = await screen.findByRole("region", { name: "Pie Dividends" });
    expect(within(pie).getByText("Vanguard All-World High Div")).toBeInTheDocument();
    expect(within(pie).getByText("60.0 %")).toBeInTheDocument();
    expect(within(pie).getByText("55.0 %")).toBeInTheDocument();
    expect(within(pie).getByText(/dividends paid out/)).toBeInTheDocument();
    expect(within(pie).getByText(/approaching max position size/)).toBeInTheDocument();
    expect(screen.getByText(/latest read failed/)).toBeInTheDocument();
  });
});
