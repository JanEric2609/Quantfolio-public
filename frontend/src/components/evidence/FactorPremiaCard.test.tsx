import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FactorPremiaCard } from "./FactorPremiaCard";
import type { FactorEvidenceCard } from "../../lib/api";

let cards: FactorEvidenceCard[] = [];
vi.mock("../../lib/api", () => ({ getFactorPremia: async () => cards }));

function card(overrides: Partial<FactorEvidenceCard>): FactorEvidenceCard {
  return {
    strategy: "value", label: "Value", region: "world", citation: "Fama & French (1992)",
    etf_hint: "a world value factor ETF", months: 384, start: "1994-02", end: "2026-01",
    long_short_annual: 0.052, long_short_t: 3.1, post_publication_months: 360,
    post_publication_annual: 0.041, long_only_annual: 0.021, expected_annual: 0.0088,
    net_expected_annual: 0.0039, tracking_error_annual: 0.04, worst_5y_excess: -0.18,
    book_worst_5y_lag: -0.027, tilt_cap_pct: 15, passed: true,
    checks: [{ name: "history", label: "At least 20 years of monthly data", value: 384, passed: true }],
    yearly_long_only: {}, computed_at: "2026-09-25T10:00:00Z",
    ...overrides,
  };
}

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <FactorPremiaCard />
    </QueryClientProvider>,
  );
}

describe("FactorPremiaCard", () => {
  it("shows the run command before the study has run", async () => {
    cards = [];
    renderCard();
    expect(await screen.findByText(/has not been run yet/)).toBeInTheDocument();
    expect(screen.getByText(/python -m app.lab.factor_premia/)).toBeInTheDocument();
  });

  it("lists each strategy with its net expected premium and failed checks", async () => {
    cards = [
      card({}),
      card({
        strategy: "momentum", label: "Momentum", passed: false, net_expected_annual: -0.002,
        checks: [{ name: "premium", label: "Long-short premium, Newey-West t ≥ 2", value: 1.4, passed: false }],
      }),
    ];
    renderCard();
    expect(await screen.findByText("Value")).toBeInTheDocument();
    expect(screen.getByText("+0,4 %")).toBeInTheDocument();
    expect(screen.getByText(/Failed: Long-short premium/)).toBeInTheDocument();
  });

  it("shows one table per region and says which one gates the tilt", async () => {
    cards = [card({}), card({ region: "europe", passed: false, label: "Value" })];
    renderCard();
    expect(await screen.findByText(/World \(23 MSCI World markets\)/)).toBeInTheDocument();
    expect(screen.getByText("Gates the tilt")).toBeInTheDocument();
    expect(screen.getByText(/Europe \(15 MSCI Europe markets\)/)).toBeInTheDocument();
    expect(screen.getByText("For comparison only")).toBeInTheDocument();
    expect(screen.queryByText(/No world run yet/)).not.toBeInTheDocument();
  });

  it("asks for a world run when only Europe has been graded", async () => {
    cards = [card({ region: "europe" })];
    renderCard();
    expect(await screen.findByText(/No world run yet/)).toBeInTheDocument();
  });
});
