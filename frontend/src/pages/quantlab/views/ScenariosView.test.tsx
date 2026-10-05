import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const state = vi.hoisted(() => ({ current: {} as Record<string, unknown>, params: [] as unknown[] }));
vi.mock("../hooks/useProjection", () => ({
  useProjection: (p: unknown) => {
    state.params.push(p);
    return state.current;
  },
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
});
