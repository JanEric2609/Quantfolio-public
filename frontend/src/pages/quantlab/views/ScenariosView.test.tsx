import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const state = vi.hoisted(() => ({
  current: {} as Record<string, unknown>,
  params: [] as unknown[],
  history: {} as Record<string, unknown>,
}));
vi.mock("../hooks/useProjection", () => ({
  useProjection: (p: unknown) => {
    state.params.push(p);
    return state.current;
  },
  useLongHistoryProjection: () => state.history,
}));
vi.mock("../hooks/useGoals", () => ({
  useGoals: () => ({ data: [{ id: "g1", title: "House", target_amount: 80000, target_date: "2040-01-01", progress: 0 }] }),
}));
vi.mock("../../../components/charts/FanChart", () => ({ FanChart: () => null }));

import { ScenariosView } from "./ScenariosView";

const row = (year: number) => ({ year, p5: 1000 * year, p25: 2000 * year, p50: 3000 * year, p75: 4000 * year, p95: 5000 * year, contributed: 2400 * year });

describe("ScenariosView", () => {
  it("plans with the median in real euros and shows the goal probability with its drift range", () => {
    state.current = {
      isLoading: false,
      data: {
        inputs: {
          start_value_eur: 10000, monthly_contribution_eur: 200, years: 30, real_return: 0.042, volatility: 0.15, nu: 5,
          calibration_years: 25, drift_standard_error: 0.03, paths: 10000, seed: 7, goal_eur: 250000,
        },
        fan: [0, 1, 2].map(row),
        terminal: { median: 180000, mean: 240000, p5: 70000, p95: 520000, contributed: 82000 },
        goal: {
          goal_eur: 250000, probability: 0.38, mc_standard_error: 0.005, probability_low_drift: 0.2,
          probability_high_drift: 0.58, median_reaches_in_years: null,
        },
        assumptions: {
          real_return_source: "AQR Capital Market Assumptions 2026", real_return_as_of: "2025-12-31",
          real_return_overridden: false, volatility_source: "your book", fat_tails: "Student-t (5 degrees of freedom)",
        },
        real_terms: true, estimate: true,
      },
    };
    render(<MemoryRouter><ScenariosView /></MemoryRouter>);
    expect(screen.getByText("Median in 30 years")).toBeInTheDocument();
    expect(screen.getByText(/180\.000/)).toBeInTheDocument();
    expect(screen.getByText("38 %")).toBeInTheDocument();
    expect(screen.getByText(/20 % – 58 %/)).toBeInTheDocument();
    expect(screen.getByText(/AQR Capital Market Assumptions 2026, as of 2025-12-31/)).toBeInTheDocument();
    expect(screen.queryByText(/Greeks|Delta|Gamma/)).toBeNull();
    // Empty inputs fall back to the book and the settings.
    expect(state.params.at(-1)).toEqual({ years: 30, goal: undefined, contribution: undefined, realReturn: undefined });
  });

  it("applies a goal preset from the link and passes it to the projection", () => {
    state.current = { isLoading: true, data: undefined };
    render(<MemoryRouter initialEntries={["/quantlab/scenarios?goal=g1"]}><ScenariosView /></MemoryRouter>);
    expect(screen.getByTestId("goal-presets")).toHaveTextContent("House");
    expect(state.params.at(-1)).toMatchObject({ goal: 80000 });
  });

  it("checks the plan against history: after tax, after a bad start, and says when the data is missing", () => {
    state.current = { isLoading: true, data: undefined };
    state.history = {
      data: {
        available: true,
        inputs: {
          start_value_eur: 10000, monthly_contribution_eur: 100, years: 30, mix: { equity: 0.9, bond: 0.1, cash: 0 },
          paths: 10000, seed: 1, goal_eur: null,
        },
        fan: [],
        terminal: { median: 121000, mean: 154000, p5: 44500, p95: 362000, contributed: 46000, p_below_contributed: 0.1 },
        after_tax: { median: 104600, p5: 40400, p95: 301000, median_tax: 16200 },
        bad_start: { first_years: 10, median: 84300, p5: 34200 },
        all_countries: { homes: 16, median: 127800, p5: 44000, p95: 380000 },
        assumptions: {
          homes: ["Belgium", "Germany"], years_of_history: 1062, haircut: 0.3, mean_block_years: 10, tax_rate: 0.26375,
          equity_exempt: 0.3, allowance_eur: 1000, source: "JST Macrohistory R.6", model_version: "v1",
        },
        estimate: true, not_tax_advice: true,
      },
    };
    const { unmount } = render(<MemoryRouter><ScenariosView /></MemoryRouter>);
    const card = screen.getByTestId("long-history");
    expect(card).toHaveTextContent(/104\.600/);
    expect(card).toHaveTextContent(/first 10 years/);
    expect(card).toHaveTextContent(/90 % equity/);
    expect(card).toHaveTextContent(/JST Macrohistory R\.6/);
    unmount();

    state.history = { data: { available: false, reason: "The long-history data is not installed.", estimate: true, not_tax_advice: true } };
    render(<MemoryRouter><ScenariosView /></MemoryRouter>);
    expect(screen.getByTestId("long-history")).toHaveTextContent("not installed");
  });
});
