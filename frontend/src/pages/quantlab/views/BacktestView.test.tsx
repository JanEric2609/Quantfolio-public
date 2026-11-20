import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const state = vi.hoisted(() => ({ run: {} as Record<string, unknown>, mutate: vi.fn() }));
vi.mock("../hooks/useBacktest", () => ({
  useBacktestStrategies: () => ({
    data: [
      { key: "buy_hold", label: "Buy and hold", about: "Always invested.", defaults: {}, grid_size: 0 },
      { key: "trend", label: "Above its moving average", about: "Faber 2007.", defaults: { window: 200 }, grid_size: 5 },
    ],
  }),
  useBacktest: () => ({ ...state.run, mutate: state.mutate }),
}));
vi.mock("../../../components/charts/LineChart", () => ({ LineChart: () => null }));

import { BacktestView } from "./BacktestView";

const line = { total_return: 0.5, cagr: 0.07, volatility: 0.15, sharpe: 0.4, max_drawdown: -0.3 };

describe("BacktestView", () => {
  it("sends the rule with its defaults and shows an insufficient-evidence verdict", () => {
    state.run = {
      isPending: false, error: null,
      data: {
        ticker: "EUNL.DE", strategy: "trend", label: "Above its moving average", params: { window: 200 }, available: true,
        currency: "EUR", start: "2020-03-01", end: "2026-10-02", years: 6.6, warmup_days: 250, benchmark: "EUNL.DE",
        risk_free: 0.02, series: [], trades: [],
        stats: { strategy: { ...line, exposure: 0.8, trades: 12, costs_eur: 30, taxes_eur: 120 }, buy_hold: line, benchmark: line },
        vs_buy_hold: { n: 1600, alpha: -0.01, alpha_t_stat: -0.4, beta: 0.8, beta_t_stat: 30, r_squared: 0.8, tracking_error: 0.07, information_ratio: -0.1 },
        vs_benchmark: null,
        evidence: {
          verdict: "insufficient_evidence", why: "Insufficient evidence: luck.", n_trials: 37, n_trials_effective: 9,
          dsr: 0.12, dsr_effective: 0.2, dsr_threshold: 0.95, pbo: 0.62, pbo_threshold: 0.5, pbo_grid_size: 5,
          min_track_record_years: null,
        },
        assumptions: { costs: "10 bp commission + 5 bp half-spread per trade." },
        estimate: true,
      },
    };
    render(<BacktestView />);
    expect(screen.getByTestId("verdict")).toHaveTextContent("Insufficient evidence");
    expect(screen.getByText(/at 37 trials/)).toBeInTheDocument();
    expect(screen.getByText("62 %")).toBeInTheDocument();
    expect(screen.getByText(/no track record would make it significant/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Run backtest/ }));
    expect(state.mutate).toHaveBeenCalledWith(expect.objectContaining({
      ticker: "EUNL.DE", strategy: "trend", params: { window: 200 }, fund_class: "aktien", apply_tax: true,
    }));
  });
});
