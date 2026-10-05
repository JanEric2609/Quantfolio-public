import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { RiskAssessment, VerificationStressResult } from "../../lib/api";
import { RiskTab } from "./RiskTab";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

// jsdom has no canvas; render the chart shell without echarts.
vi.mock("../../components/charts/EChart", () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />,
}));

function risk(over: Partial<RiskAssessment> = {}): RiskAssessment {
  return {
    var_95: 0.0123,
    var_99: 0.0211,
    max_drawdown: -0.1834,
    current_drawdown: -0.042,
    sharpe_ratio: 0.81,
    sortino_ratio: 1.12,
    concentration_index: 0.31,
    lookthrough_hhi: 0.04,
    lookthrough_count: 1480,
    asset_type_exposure: { etf: 0.8, stock: 0.2 },
    currency_exposure: { USD: 0.61, EUR: 0.3, JPY: 0.09 },
    alerts: [],
    insufficient_history: false,
    return_basis: {
      source: "holdings_price_history",
      n_obs: 252,
      start: "2025-10-01",
      end: "2026-09-30",
      priced_assets: ["EUNL.DE", "ASML.AS"],
      missing_history: [],
      message: null,
    },
    ...over,
  };
}

const stress: VerificationStressResult = {
  portfolio_id: "p1",
  note: "What-if scenarios on today's holdings, not forecasts.",
  created_at: "2026-09-30T00:00:00Z",
  scenarios: [
    { scenario_name: "Market Crash", description: "Broad market decline of 30%.", portfolio_impact_pct: -27.6, worst_case_loss: 4200, details: {} },
    { scenario_name: "Dollar Slump", description: "EUR appreciates 15% vs USD.", portfolio_impact_pct: -9.2, worst_case_loss: 1500, details: {} },
  ],
};

function mockApi(riskResult: RiskAssessment | Error, stressResult: VerificationStressResult | Error = stress) {
  apiMock.mockReset();
  apiMock.mockImplementation((url: string) => {
    const result = url.includes("/risk/") ? riskResult : stressResult;
    return result instanceof Error ? Promise.reject(result) : Promise.resolve(result);
  });
}

function renderRisk() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RiskTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Portfolio -> Risk tab", () => {
  beforeEach(() => {
    apiMock.mockReset();
  });

  it("reads the main portfolio's risk and stress", async () => {
    mockApi(risk());
    renderRisk();
    await screen.findByText("Loss and drawdown");
    expect(apiMock).toHaveBeenCalledWith("/api/verification/risk/main");
    expect(apiMock).toHaveBeenCalledWith("/api/verification/stress/main");
  });

  it("shows VaR and drawdown with the window they were computed on", async () => {
    mockApi(risk());
    renderRisk();

    expect(await screen.findByText("1.23 %")).toBeInTheDocument(); // VaR 95
    expect(screen.getByText("2.11 %")).toBeInTheDocument(); // VaR 99
    expect(screen.getByText("-18.3 %")).toBeInTheDocument(); // max drawdown
    expect(screen.getByText("0.81")).toBeInTheDocument();
    expect(screen.getByText(/Based on 252 daily returns of your current holdings \(2025-10-01 to 2026-09-30\)/)).toBeInTheDocument();
    expect(screen.getByText(/price history, not account balances/)).toBeInTheDocument();
  });

  it("holds the Sharpe and Sortino ratios back below 30 daily returns", async () => {
    mockApi(
      risk({
        insufficient_history: true,
        sharpe_ratio: 0,
        sortino_ratio: 0,
        return_basis: { ...risk().return_basis, n_obs: 12 },
      }),
    );
    renderRisk();

    expect(await screen.findAllByText("Too early")).toHaveLength(2);
    expect(screen.getAllByText("12 of 30 daily returns needed")).toHaveLength(2);
    // VaR and drawdown are still reported: they are meaningful with a short sample.
    expect(screen.getByText("1.23 %")).toBeInTheDocument();
  });

  it("explains a missing price history instead of showing zeros", async () => {
    mockApi(
      risk({
        var_95: 0,
        var_99: 0,
        max_drawdown: 0,
        current_drawdown: 0,
        insufficient_history: true,
        return_basis: { source: "holdings_price_history", n_obs: 0, start: null, end: null, priced_assets: [], missing_history: ["ABC.DE"], message: "Using 0/1 positions with available price data." },
      }),
    );
    renderRisk();

    const note = await screen.findByText(/Using 0\/1 positions with available price data\./);
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveTextContent("Missing: ABC.DE.");
    expect(screen.queryByText("0.00 %")).not.toBeInTheDocument();
  });

  it("looks ETFs through: HHI by holding, look-through HHI and the currency exposure", async () => {
    mockApi(risk());
    renderRisk();

    const concentration = (await screen.findByText("Concentration")).closest("div[class*='rounded-lg']") as HTMLElement;
    expect(within(concentration).getByText("By holding (HHI)")).toBeInTheDocument();
    expect(within(concentration).getByText("Look-through (ETFs opened up)")).toBeInTheDocument();
    expect(concentration).toHaveTextContent("About 1480 underlying positions");

    const values = screen.getByRole("list", { name: "Currency exposure values" });
    expect(values).toHaveTextContent("USD");
    expect(values).toHaveTextContent("61.0 %");
    expect(screen.getByText(/a euro-quoted MSCI World ETF is still mostly a dollar exposure/)).toBeInTheDocument();
  });

  it("lists the alerts with their severity and type", async () => {
    mockApi(
      risk({
        alerts: [
          { id: "a1", type: "concentration_single", severity: "critical", title: "Single-position concentration risk", message: "NVDA represents 55% of your portfolio.", created_at: "2026-09-30T00:00:00Z" },
          { id: "a2", type: "drawdown", severity: "warning", title: "Elevated drawdown", message: "Your current holdings are 12.0% below their peak over the last 252 trading days.", created_at: "2026-09-30T00:00:00Z" },
        ],
      }),
    );
    renderRisk();

    const heading = await screen.findByText("Risk alerts");
    const card = heading.closest("div[class*='rounded-lg']") as HTMLElement;
    expect(card).toHaveTextContent("Single-position concentration risk");
    expect(card).toHaveTextContent("Single name");
    expect(card).toHaveTextContent("NVDA represents 55% of your portfolio.");
    expect(card).toHaveTextContent("Drawdown");
    expect(card).toHaveTextContent("2 to look at");
  });

  it("says so when there are no alerts", async () => {
    mockApi(risk());
    renderRisk();
    expect(await screen.findByText(/No alerts\. Drawdown, currency and single-name concentration are inside your thresholds/)).toBeInTheDocument();
  });

  it("shows the what-if scenarios as illustrations, with no probabilities", async () => {
    mockApi(risk());
    renderRisk();

    const list = await screen.findByText("Dollar Slump");
    const item = list.closest("li") as HTMLElement;
    expect(item).toHaveTextContent("-9.2 %");
    expect(item).toHaveTextContent("EUR appreciates 15% vs USD.");
    expect(screen.getByText("What-if scenarios on today's holdings, not forecasts.")).toBeInTheDocument();
    expect(screen.queryByText(/freq/i)).not.toBeInTheDocument();
  });

  it("links to the advanced view in Quant Lab", async () => {
    mockApi(risk());
    renderRisk();
    expect(await screen.findByRole("link", { name: /Advanced risk in Quant Lab/ })).toHaveAttribute("href", "/quantlab/risk");
  });

  it("an empty portfolio gets a helpful empty state", async () => {
    mockApi(
      risk({
        asset_type_exposure: {},
        currency_exposure: {},
        insufficient_history: true,
        return_basis: { source: "holdings_price_history", n_obs: 0, start: null, end: null, priced_assets: [], missing_history: [], message: null },
      }),
    );
    renderRisk();
    expect(await screen.findByText("No holdings to assess yet")).toBeInTheDocument();
  });

  it("shows an error with retry when the risk endpoint fails", async () => {
    const user = userEvent.setup();
    mockApi(new Error("risk down"));
    renderRisk();

    expect(await screen.findByText("Couldn't load the risk view")).toBeInTheDocument();
    expect(screen.getByText("risk down")).toBeInTheDocument();

    mockApi(risk());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Loss and drawdown")).toBeInTheDocument();
  });

  it("keeps the rest of the page when only the stress test fails", async () => {
    mockApi(risk(), new Error("stress down"));
    renderRisk();
    expect(await screen.findByText("Loss and drawdown")).toBeInTheDocument();
    expect(await screen.findByText(/No scenarios yet/)).toBeInTheDocument();
  });
});
