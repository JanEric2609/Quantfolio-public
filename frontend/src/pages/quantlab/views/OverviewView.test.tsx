import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const summaryState = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
const perfState = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));

vi.mock("../hooks/useQuantSummary", () => ({ useQuantSummary: () => summaryState.current }));
vi.mock("../hooks/useBookPerformance", () => ({ useBookPerformance: () => perfState.current }));
vi.mock("../hooks/useBackfillJob", () => ({
  useBackfillJob: () => ({ isRunning: false, start: vi.fn() }),
}));
vi.mock("../../../components/charts/LineChart", () => ({ LineChart: () => null }));
vi.mock("../../../components/charts/AreaChart", () => ({ AreaChart: () => null }));
vi.mock("../../../components/charts/DonutChart", () => ({ DonutChart: () => null }));

import { OverviewView } from "./OverviewView";

function renderWith(days: number) {
  summaryState.current = {
    isLoading: false,
    error: null,
    data: { available: true, samples: 500, weights: { "EUNL.DE": 30000 }, risk: {} },
  };
  perfState.current = {
    data: {
      available: true, currency: "EUR", estimate: true, start: "2026-06-06", end: "2026-10-03", days,
      benchmark: "EUNL.DE",
      series: [
        { date: "2026-06-06", unit_value: 100, value: 30000, net_flow: 0, benchmark: 100 },
        { date: "2026-10-03", unit_value: 104.2, value: 34570, net_flow: 3300, benchmark: 104.0 },
      ],
      twr: 0.042, twr_annualised: days >= 365 ? 0.13 : null, benchmark_return: 0.04,
      benchmark_annualised: days >= 365 ? 0.12 : null, irr_period: 0.041, irr_annualised: days >= 365 ? 0.125 : null,
      net_contributions_eur: 3300, value_end_eur: 34570, unpriced: [],
    },
  };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <OverviewView />
    </QueryClientProvider>,
  );
}

describe("OverviewView", () => {
  it("shows the period return, not an annualised one, over less than a year", () => {
    renderWith(119);
    expect(screen.getByText("Return (TWR)")).toBeTruthy();
    expect(screen.getByText(/4,20/)).toBeTruthy();
    expect(screen.getByText("since 2026-06-06, not annualised")).toBeTruthy();
    expect(screen.queryByText("Return a year (TWR)")).toBeNull();
  });

  it("annualises once a year has passed", () => {
    renderWith(400);
    expect(screen.getByText("Return a year (TWR)")).toBeTruthy();
    expect(screen.getByText(/13,00/)).toBeTruthy();
  });
});
