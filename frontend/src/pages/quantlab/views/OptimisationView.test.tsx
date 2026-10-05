import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const state = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("../hooks/useQuantOptimisation", () => ({ useQuantOptimisation: () => state.current }));
vi.mock("../../../components/charts/BarChart", () => ({ BarChart: () => null }));

import { OptimisationView } from "./OptimisationView";

const method = (label: string, a: number) => ({
  label, weights: { "EUNL.DE": a, "EIMI.L": 1 - a }, volatility: 0.15, risk_contributions: { "EUNL.DE": a, "EIMI.L": 1 - a },
  diversification_ratio: 1.05, effective_n: 1.8, turnover: Math.abs(0.99 - a),
});

describe("OptimisationView", () => {
  it("shows covariance-only candidates with current weights as fractions, no Sharpe", () => {
    state.current = {
      isLoading: false, error: null,
      data: {
        available: true, assets: ["EUNL.DE", "EIMI.L"],
        weights_current: { "EUNL.DE": 0.99, "EIMI.L": 0.01 },
        methods: { equal: method("Equal weight (1/N)", 0.5), erc: method("Equal risk contribution", 0.55) },
        current: { weights: { "EUNL.DE": 0.99, "EIMI.L": 0.01 }, volatility: 0.14, risk_contributions: {}, diversification_ratio: 1.0, effective_n: 1.02 },
        covariance: { estimator: "Ledoit-Wolf", shrinkage: 0.12, samples: 504, start: "2024-10-01", end: "2026-10-02", annualised_volatility: {} },
      },
    };
    render(<MemoryRouter><OptimisationView /></MemoryRouter>);
    expect(screen.getByTestId("method-erc")).toHaveTextContent("Equal risk contribution");
    expect(screen.getAllByText("99,0 %").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Sharpe/)).toBeNull();
    expect(screen.getAllByRole("link", { name: /Rebalance toward this/ })[0]).toHaveAttribute("href", "/quantlab/holdings?target=equal");
  });
});
