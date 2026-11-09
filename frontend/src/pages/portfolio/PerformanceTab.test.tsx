import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BookPerformance } from "../../lib/api";
import { PerformanceTab } from "./PerformanceTab";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));
vi.mock("../../components/charts/LineChart", () => ({ LineChart: () => null }));
vi.mock("../../components/charts/AreaChart", () => ({ AreaChart: () => null }));

const perf: BookPerformance = {
  available: true,
  currency: "EUR",
  estimate: true,
  start: "2026-06-06",
  end: "2026-11-09",
  days: 156,
  benchmark: "EUNL.DE",
  series: [
    { date: "2026-06-06", unit_value: 1, value: 30000, net_flow: 0, benchmark: 100 },
    { date: "2026-06-30", unit_value: 1.02, value: 30600, net_flow: 0, benchmark: 101 },
    { date: "2026-11-09", unit_value: 1.0447, value: 35600, net_flow: 4142, benchmark: 103.17 },
  ],
  value_end_eur: 35600,
  net_contributions_eur: 4142,
  twr: 0.0447,
  twr_annualised: null,
  benchmark_return: 0.0317,
  irr_period: 0.0413,
  irr_annualised: null,
  method: "TWR",
};

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PerformanceTab />
    </QueryClientProvider>,
  );
}

describe("PerformanceTab", () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue(perf);
  });

  it("shows the book's time-weighted return against MSCI World from the book endpoint", async () => {
    renderTab();
    const stats = await screen.findByTestId("performance-stats");
    expect(apiMock).toHaveBeenCalledWith("/api/quant/portfolio/real/performance");
    expect(stats).toHaveTextContent(/4[.,]5 %/);
    expect(stats).toHaveTextContent(/3[.,]2 %/);
    expect(stats).toHaveTextContent(/\+1[.,]3 pp/);
    expect(stats).toHaveTextContent(/4[.,]1 %/);
    expect(screen.getByText(/cash is not included/)).toBeInTheDocument();
    expect(screen.queryByText("Snapshot Ledger")).not.toBeInTheDocument();
    expect(screen.getByTestId("performance-months")).toHaveTextContent("2026-11");
  });

  it("drops the IRR outside the whole period", async () => {
    renderTab();
    await screen.findByTestId("performance-stats");
    await userEvent.setup().click(screen.getByRole("button", { name: "This year" }));
    expect(screen.getByTestId("performance-stats")).toHaveTextContent("whole period only");
  });

  it("says why when there is no history yet", async () => {
    apiMock.mockResolvedValue({ available: false, reason: "No position snapshots yet.", currency: "EUR", estimate: true, series: [] });
    renderTab();
    expect(await screen.findByTestId("performance-unavailable")).toHaveTextContent("No position snapshots yet.");
  });
});
