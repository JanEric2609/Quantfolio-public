import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PaperArchive, PaperPerformance, TradeCheckSummary } from "../../../lib/api";

const apiMock = vi.fn();
vi.mock("../../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));
vi.mock("../../../components/charts/LineChart", () => ({ LineChart: () => null }));

import { PaperRunCard } from "./PaperRunCard";

const perf: PaperPerformance = {
  summary: {
    id: "p1", name: "P", currency: "EUR", initial_cash: 1000, total_value: 1050, cash_balance: 50,
    securities_value: 1000, total_return_pct: 0.05, holding_count: 1, trade_count: 0,
    inception_at: "2026-10-06T08:00:00+00:00", baseline_value: 1000, dividends_eur: 4,
  } as PaperPerformance["summary"],
  benchmark: { symbol: "EUNL.DE", start: "2026-10-06", available: true, total_return_pct: 0.08, value: 1080, excess_return_pct: -0.03 },
  series: [],
  method: "",
};
const archives: PaperArchive[] = [
  { id: "a1", archived_at: "2026-10-06T07:00:00+00:00", inception_at: "2026-01-02T00:00:00+00:00", reason: "reset",
    trades: 12, snapshots: 180, last_total_return_pct: -0.02 },
];

const check: TradeCheckSummary = {
  mode: "shadow", trades_checked: 2, would_block: 1, cost_paid_eur: 4, cost_of_blocked_eur: 2,
  blocked_contribution_eur: -30, passed_contribution_eur: 12, open: 1, nav_eur: 1050, checked_nav_eur: 1080,
  recent: [
    { trade_id: "t1", traded_on: "2026-10-07", ticker: "AAPL", side: "buy", notional_eur: 500, cost_eur: 2,
      tax_eur: 0, edge_eur: 0, p_hit: null, p_source: "none", verdict: "block", reason: "Costs €2.00; no shown edge.",
      contribution_eur: -30, resolved: true },
  ],
  estimate: true, not_tax_advice: true,
};

describe("PaperRunCard", () => {
  it("shows the shadow trade check with the value the run would have with it on", async () => {
    apiMock.mockImplementation((url: string) => {
      if (url.endsWith("/performance")) return Promise.resolve(perf);
      if (url.endsWith("/archives")) return Promise.resolve([]);
      if (url.endsWith("/trade-check")) return Promise.resolve(check);
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <PaperRunCard portfolioId="p1" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/would have stopped 1 of 2 trades/)).toBeInTheDocument();
    expect(screen.getByText("would stop")).toBeInTheDocument();
    expect(screen.getByText(/\+.*30/)).toHaveClass("text-success");
  });

  it("shows the run next to MSCI World EUR and resets only after confirmation", async () => {
    apiMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/performance")) return Promise.resolve(perf);
      if (url.endsWith("/archives")) return Promise.resolve(archives);
      if (url.endsWith("/reset")) return Promise.resolve({ id: "p1", archive_id: "a2" });
      if (url.endsWith("/trade-check")) return Promise.resolve({ ...check, trades_checked: 0, recent: [] });
      return Promise.reject(new Error(`unexpected ${url} ${init?.method}`));
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <PaperRunCard portfolioId="p1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/EUNL\.DE/)).toBeInTheDocument();
    expect(screen.getByText(/vs\. the index/)).toHaveClass("text-danger");
    expect(await screen.findByText(/1 earlier run archived/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Reset run/ }));
    expect(apiMock).not.toHaveBeenCalledWith("/api/paper-portfolio/p1/reset", expect.anything());
    await userEvent.click(await screen.findByRole("button", { name: /Archive and reset/ }));
    expect(apiMock).toHaveBeenCalledWith(
      "/api/paper-portfolio/p1/reset",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ confirm: true }) }),
    );
  });
});
