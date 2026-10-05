import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AllowancePlanCard } from "./AllowancePlanCard";
import type { TaxAllowanceBank, TaxAllowancePlan } from "../../../lib/api";

let payload: TaxAllowancePlan;
const requested: string[] = [];
vi.mock("../../../lib/api", () => ({
  api: async (path: string) => {
    requested.push(path);
    return payload;
  },
}));

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AllowancePlanCard year={2026} />
    </QueryClientProvider>,
  );
}

const bank = (overrides: Partial<TaxAllowanceBank>): TaxAllowanceBank => ({
  bank: "dkb",
  label: "DKB",
  fsa_eur: 600,
  nv_filed: false,
  nv_covers: false,
  booked_eur: 120,
  used_eur: 120,
  remaining_eur: 480,
  projected_eur: 450,
  projected_uncovered_eur: 0,
  projected_withheld_eur: 0,
  minimum_fsa_eur: 120,
  recommended_fsa_eur: 400,
  recommended_withheld_eur: 0,
  change_eur: -200,
  booked: { dividends_eur: 50, interest_eur: 70, vorabpauschale_eur: 0, gains_eur: 0, losses_eur: 0 },
  expected: { interest_eur: 130, interest_basis: "rate", dividends_eur: 150, vorabpauschale_eur: 0 },
  vorabpauschale_estimated_eur: 0,
  savings_balance_eur: 6000,
  interest_rate: 0.0225,
  loss_left: { aktien_eur: 0, other_eur: 0 },
  harvestable_gain_eur: 0,
  how_to: {},
  ...overrides,
});

const plan: TaxAllowancePlan = {
  tax_year: 2026,
  label: "Estimate",
  estimate: true,
  not_tax_advice: true,
  applicable: true,
  reason: null,
  allowance_eur: 1000,
  other_banks_eur: 0,
  available_eur: 1000,
  assigned_eur: 1000,
  over_assigned: false,
  withholding_rate: 0.26375,
  projected_withheld_eur: 150,
  recommended_withheld_eur: 20,
  changes_needed: true,
  projected_capital_income_eur: 900,
  unassigned: { events: 0, taxable_eur: null, vorabpauschale_eur: null },
  banks: [
    bank({}),
    bank({
      bank: "scalable",
      label: "Scalable Capital",
      fsa_eur: null,
      nv_filed: true,
      nv_covers: true,
      booked_eur: 80,
      projected_eur: 500,
      projected_withheld_eur: 150,
      recommended_fsa_eur: 600,
      recommended_withheld_eur: 20,
      change_eur: 600,
      expected: { interest_eur: 0, interest_basis: "unknown", dividends_eur: 0, vorabpauschale_eur: 75 },
      savings_balance_eur: null,
      interest_rate: null,
    }),
  ],
  actions: [
    {
      code: "nv_missing_at_bank",
      severity: "action",
      title: "Send the NV certificate to Scalable Capital",
      message: "Scalable Capital doesn't have your NV certificate, so it withholds tax above its Freistellungsauftrag.",
      bank: "scalable",
      deadline: "2026-12-15",
      how_to: "Upload a copy in the Scalable app under Profil.",
    },
    {
      code: "nv_banks_unknown",
      severity: "warning",
      title: "Which banks have your NV certificate?",
      message: "Tick it per bank in the tax settings.",
      bank: null,
      deadline: null,
      how_to: null,
    },
    {
      code: "events_without_bank",
      severity: "info",
      title: "2 tax events have no bank",
      message: "They count toward the yearly total only.",
      bank: null,
      deadline: null,
      how_to: null,
    },
  ],
  sources: [{ label: "BMF letter on Sec. 44a EStG", url: "https://example.org/bmf" }],
};

describe("AllowancePlanCard", () => {
  it("lists the actions most urgent first, with deadline and how-to", async () => {
    payload = plan;
    renderCard();
    expect(await screen.findByText("Send the NV certificate to Scalable Capital")).toBeInTheDocument();
    expect(requested).toContain("/api/tax/allowances?year=2026");
    expect(screen.getByText("by 15.12.2026")).toBeInTheDocument();
    expect(screen.getByText("How to do it at Scalable Capital")).toBeInTheDocument();
    expect(screen.getByText("Upload a copy in the Scalable app under Profil.")).toBeInTheDocument();
    const order = [
      "Send the NV certificate to Scalable Capital",
      "Which banks have your NV certificate?",
      "2 tax events have no bank",
    ].map((title) => screen.getByText(title));
    expect(order[0].compareDocumentPosition(order[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(order[1].compareDocumentPosition(order[2]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/Estimate · not tax advice/)).toBeInTheDocument();
  });

  it("shows one table row per bank", async () => {
    payload = plan;
    renderCard();
    await screen.findByRole("table");
    const dkb = screen.getByRole("row", { name: /^DKB/ });
    expect(within(dkb).getByText(/600,00\s€/)).toBeInTheDocument(); // on file
    expect(within(dkb).getByText("No")).toBeInTheDocument();
    expect(within(dkb).getByText(/450,00\s€/)).toBeInTheDocument(); // projected
    expect(within(dkb).getByText(/400,00\s€/)).toBeInTheDocument(); // suggested
    expect(within(dkb).getByText(/-200,00\s€/)).toBeInTheDocument(); // change

    const scalable = screen.getByRole("row", { name: /^Scalable Capital/ });
    expect(within(scalable).getByText("not entered")).toBeInTheDocument();
    expect(within(scalable).getByText("Yes")).toBeInTheDocument();
    expect(within(scalable).getByText("covers")).toBeInTheDocument();
    expect(within(scalable).getByText(/150,00\s€/)).toBeInTheDocument(); // withheld
    expect(within(scalable).getByText(/\+600,00\s€/)).toBeInTheDocument();
  });

  it("states the allowance, the saving and the bank-less events", async () => {
    payload = { ...plan, unassigned: { events: 2, taxable_eur: 40, vorabpauschale_eur: null } };
    renderCard();
    expect(await screen.findByText(/Sparer-Pauschbetrag/)).toHaveTextContent(
      /Sparer-Pauschbetrag 1\.000,00\s€ · at other banks 0,00\s€ · on file 1\.000,00\s€/,
    );
    expect(screen.getByText(/With the suggested split about 130,00\s€ less is withheld this year\./)).toBeInTheDocument();
    expect(screen.getByText(/2 tax events have no bank, so they count toward the yearly total/)).toBeInTheDocument();
  });

  it("explains how the interest was projected", async () => {
    payload = plan;
    renderCard();
    await screen.findByText("What the projection includes");
    expect(screen.getByText(/from balance × rate \(6\.000,00\s€ × 2,25\s%\)/)).toBeInTheDocument();
    expect(screen.getByText("unknown — no interest rate for this account yet")).toBeInTheDocument();
    expect(screen.getByText("Vorabpauschale in January")).toBeInTheDocument();
  });

  it("links the sources safely", async () => {
    payload = plan;
    renderCard();
    const link = await screen.findByRole("link", { name: "BMF letter on Sec. 44a EStG" });
    expect(link).toHaveAttribute("href", "https://example.org/bmf");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("omits the saving sentence when the suggested split changes nothing", async () => {
    payload = { ...plan, projected_withheld_eur: 20, recommended_withheld_eur: 20, actions: [] };
    renderCard();
    await screen.findByRole("table");
    expect(screen.queryByText(/less is withheld/)).not.toBeInTheDocument();
    expect(screen.getByText("Nothing to change right now.")).toBeInTheDocument();
  });

  it("shows only the reason when the plan does not apply", async () => {
    payload = {
      tax_year: 2026,
      estimate: true,
      not_tax_advice: true,
      applicable: false,
      reason: "Tax residence for 2026 is AT: a Freistellungsauftrag needs unlimited German tax liability.",
      banks: [],
      actions: [],
      sources: [],
    };
    renderCard();
    expect(await screen.findByText(/Tax residence for 2026 is AT/)).toBeInTheDocument();
    expect(screen.getByText("Freistellungsauftrag & NV certificate")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("What the projection includes")).not.toBeInTheDocument();
  });
});
