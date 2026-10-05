import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ThisMonthPage } from "./ThisMonthPage";
import type { MonthlyPlan, PlanSleeve } from "../lib/api";

// A plain function, not vi.fn(): a spy's result bookkeeping reports the
// deliberately rejected promise of the error-state test as a failure.
let loadPlan: () => Promise<MonthlyPlan> = async () => plan;
vi.mock("../lib/api", () => ({ getMonthlyPlan: () => loadPlan() }));

function sleeve(overrides: Partial<PlanSleeve>): PlanSleeve {
  return {
    key: "core",
    label: "Core",
    current_eur: 0,
    current_pct: 0,
    target_pct: 0,
    max_pct: 100,
    unlocked: false,
    status: "",
    contribution_eur: 0,
    after_eur: 0,
    after_pct: 0,
    positions: [],
    ...overrides,
  };
}

const plan: MonthlyPlan = {
  month: "2026-10",
  generated_at: "2026-10-01T09:00:00Z",
  headline: "Let your 1.000 € savings plan run into EUNL.DE. Nothing else to do this month.",
  no_change: true,
  contribution_eur: 1000,
  book_eur: 36000,
  holdings_synced_at: "2026-09-30T18:00:00Z",
  has_holdings: true,
  tracking_error_budget: { label: "Tight", tilt_max_pct: 15, satellite_max_pct: 10 },
  min_order_eur: 1000,
  sleeves: [
    sleeve({
      key: "core", label: "Core", current_eur: 36000, current_pct: 100, target_pct: 100, unlocked: true,
      status: "Receives the monthly savings plan.", contribution_eur: 1000, after_eur: 37000, after_pct: 100,
      positions: [{ isin: "IE00B4L5Y983", ticker: "EUNL.DE", name: "iShares Core MSCI World", value_eur: 36000 }],
    }),
    sleeve({ key: "tilt", label: "Factor tilt", max_pct: 15, status: "No factor tilt has passed its evidence check yet." }),
    sleeve({ key: "satellite", label: "Stock picks", max_pct: 10, status: "No stock-picking strategy has passed the evidence gate." }),
  ],
  actions: [{
    sleeve: "core", kind: "savings_plan", amount_eur: 1000, instrument: "iShares Core MSCI World (Acc)",
    ticker: "EUNL.DE", isin: "IE00B4L5Y983", note: "Your savings plan at DKB, 1,50 € per execution (0.15 %), nothing if it is one of DKB's promotional ETFs.",
  }],
  never_sells: true,
  not_investment_advice: true,
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ThisMonthPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ThisMonthPage", () => {
  beforeEach(() => {
    loadPlan = async () => plan;
  });

  it("shows the no-change headline, the savings-plan action and all three sleeves", async () => {
    renderPage();

    expect(await screen.findByText(plan.headline)).toBeInTheDocument();
    expect(screen.getByText("No change")).toBeInTheDocument();
    expect(screen.getByText("No sale")).toBeInTheDocument();
    expect(screen.getByText("Savings plan")).toBeInTheDocument();
    expect(screen.getByText("iShares Core MSCI World (Acc)")).toBeInTheDocument();
    for (const label of ["Core", "Factor tilt", "Stock picks"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText(/No stock-picking strategy has passed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Plan settings/ })).toHaveAttribute("href", "/settings/profile#monthly-plan");
  });

  it("shows a drift-band sale and its reinvestment as a rebalance", async () => {
    loadPlan = async () => ({
      ...plan,
      headline: "Invest 1.000 € and rebalance: sell 14.850 € and reinvest it, as listed below.",
      no_change: false,
      never_sells: false,
      drift_band_pp: 5,
      actions: [
        {
          sleeve: "tilt", kind: "sale", amount_eur: 14850, instrument: "World Momentum", ticker: null,
          isin: "IE00BP3QZ825", note: "Sells back to the 15 % target.",
        },
        plan.actions[0],
        {
          sleeve: "core", kind: "order", amount_eur: 14850, instrument: "iShares Core MSCI World (Acc)",
          ticker: "EUNL.DE", isin: "IE00B4L5Y983", note: "One order from the sale proceeds.",
        },
      ],
    });
    renderPage();

    expect(await screen.findByText(/rebalance: sell 14.850/)).toBeInTheDocument();
    expect(screen.getByText("Rebalance")).toBeInTheDocument();
    expect(screen.queryByText("No change")).not.toBeInTheDocument();
    expect(screen.getByText("Sale")).toBeInTheDocument();
    expect(screen.getByText("Single order")).toBeInTheDocument();
    expect(screen.getByText("World Momentum")).toBeInTheDocument();
  });

  it("names the broker of each buy and shows running savings plans, cash and notes", async () => {
    loadPlan = async () => ({
      ...plan,
      headline: "Raise your core savings plans to 1.000 € a month (now 35 €).",
      no_change: false,
      broker: "scalable",
      broker_label: "Scalable Capital",
      broker_choice: "auto",
      actions: [
        { ...plan.actions[0], broker: "scalable", broker_label: "Scalable Capital", note: "Your core savings plans run 35 € a month." },
        {
          sleeve: "core", kind: "one_off", amount_eur: 319.50, instrument: "iShares Core MSCI World (Acc)",
          ticker: "EUNL.DE", isin: "IE00B4L5Y983", note: "Optional one-off from cash above your 500 € reserve.",
          broker: "scalable", broker_label: "Scalable Capital",
        },
      ],
      savings_plans: {
        items: [
          {
            broker: "scalable", broker_label: "Scalable Capital", name: "NVIDIA", isin: "US67066G1040",
            sleeve: "satellite", amount_eur: 35, frequency: "MONTHLY", monthly_eur: 35, next_execution_date: "2026-11-04",
          },
        ],
        monthly_eur: 35,
        by_sleeve: { core: 0, tilt: 0, satellite: 35 },
      },
      cash: {
        savings_eur: 812.40, broker_cash_eur: 7.10, emergency_reserve_eur: 500, reserve_set: true, investable_eur: 319.50,
      },
      core_look_through: { em_eur: 400, em_pct: 1.3, world_em_pct: 10, unknown_eur: 0 },
      notes: ["NVIDIA: 35 € a month goes into stock picks, which no strategy has unlocked yet (target 0 %)."],
    });
    renderPage();

    expect(await screen.findByText(/Raise your core savings plans/)).toBeInTheDocument();
    expect(screen.getAllByText("Scalable Capital").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Optional one-off")).toBeInTheDocument();
    expect(screen.getByText("Savings plans that already run")).toBeInTheDocument();
    expect(screen.getByText("NVIDIA")).toBeInTheDocument();
    expect(screen.getByText("Investable")).toBeInTheDocument();
    expect(screen.getByText(/goes into stock picks/)).toBeInTheDocument();
    expect(screen.getByText(/of the core is emerging markets/)).toBeInTheDocument();
  });

  it("says when the emerging-markets split of the core is not known", async () => {
    loadPlan = async () => ({
      ...plan,
      core_look_through: { em_eur: 0, em_pct: null, world_em_pct: 10, unknown_eur: 5000 },
    });
    renderPage();

    expect(await screen.findByText(/split of your core funds is not known/)).toBeInTheDocument();
    expect(screen.queryByText(/of the core is emerging markets/)).not.toBeInTheDocument();
  });

  it("asks for an emergency reserve before calling any cash investable", async () => {
    loadPlan = async () => ({
      ...plan,
      cash: { savings_eur: 1000, broker_cash_eur: 0, emergency_reserve_eur: 0, reserve_set: false, investable_eur: 0 },
    });
    renderPage();

    expect(await screen.findByText(/to see how much is investable/)).toBeInTheDocument();
    expect(screen.queryByText("Investable")).not.toBeInTheDocument();
  });

  it("shows an error with a retry button when the plan cannot load", async () => {
    loadPlan = async () => {
      throw new Error("boom");
    };
    renderPage();

    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/ })).toBeInTheDocument();
  });
});
