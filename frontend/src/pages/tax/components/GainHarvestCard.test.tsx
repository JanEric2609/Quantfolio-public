import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GainHarvestCard } from "./GainHarvestCard";
import type { GainHarvest } from "../../../lib/api";

let payload: GainHarvest;
vi.mock("../../../lib/api", () => ({ api: async () => payload }));

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GainHarvestCard year={2026} />
    </QueryClientProvider>,
  );
}

const base: GainHarvest = {
  tax_year: 2026,
  estimate: true,
  not_tax_advice: true,
  enabled: true,
  room: {
    grundfreibetrag_eur: 12348,
    sparer_pauschbetrag_eur: 1000,
    other_income_eur: 0,
    capital_income_so_far_eur: 0,
    tax_free_room_eur: 13348,
    family_insurance_room_eur: null,
    room_eur: 13348,
  },
  steps: [
    {
      isin: "IE00B4L5Y983",
      name: "iShares Core MSCI World",
      sell_quantity: 200,
      notional_eur: 20000,
      gain_eur: 7000,
      taxable_gain_eur: 4900,
      trading_cost_eur: 50,
      future_tax_avoided_eur: 1292.38,
      net_benefit_eur: 1242.38,
      whole_position: true,
    },
  ],
  totals: { taxable_gain_eur: 4900, trading_cost_eur: 50, future_tax_avoided_eur: 1292.38 },
  skipped: [],
  warnings: [{ code: "foreign_insurance", message: "The German family-insurance limit does not apply." }],
};

describe("GainHarvestCard", () => {
  it("lists the sell-and-rebuy steps and the warnings", async () => {
    payload = base;
    renderCard();
    expect(await screen.findByText(/Sell all iShares Core MSCI World/)).toBeInTheDocument();
    expect(screen.getByText(/family-insurance limit does not apply/)).toBeInTheDocument();
    expect(screen.getByText(/not tax advice/i)).toBeInTheDocument();
  });

  it("titles the card by mode and labels the NV-mode steps", async () => {
    payload = { ...base, mode: "nv" };
    renderCard();
    expect(await screen.findByText("Tax-free gain harvesting (NV certificate)")).toBeInTheDocument();
    expect(screen.queryByText(/ at (DKB|Scalable)/)).not.toBeInTheDocument();
  });

  it("works in allowance mode: no Grundfreibetrag fields, bank per step, room per bank", async () => {
    payload = {
      ...base,
      mode: "allowance",
      nv_certificate: null,
      room: {
        mode: "allowance",
        sparer_pauschbetrag_eur: 1000,
        capital_income_so_far_eur: 120,
        projected_capital_income_eur: 700,
        room_eur: 300,
      },
      bank_rooms: [
        { bank: "dkb", label: "DKB", nv_covers: false, room_eur: 180 },
        { bank: "scalable", label: "Scalable Capital", nv_covers: false, room_eur: null },
      ],
      steps: [
        { ...base.steps[0], bank: "scalable", whole_position: false, sell_quantity: 12.5 },
        { ...base.steps[0], bank: "dkb" },
      ],
    };
    const { container } = renderCard();

    expect(await screen.findByText("Gain harvesting within your Freistellungsauftrag")).toBeInTheDocument();
    expect(screen.queryByText(/Tax-free gain harvesting/)).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/NaN|undefined/);

    // Same ISIN at two banks: both steps render, each with its bank.
    expect(screen.getByText(/Sell 12,5 iShares Core MSCI World at Scalable Capital, buy the same number back/)).toBeInTheDocument();
    expect(screen.getByText(/Sell all iShares Core MSCI World at DKB, buy the same number back/)).toBeInTheDocument();

    // Room per bank only where a cap applies.
    expect(screen.getByText(/DKB: 180,00\s€ left within its Freistellungsauftrag/)).toBeInTheDocument();
    expect(screen.queryByText(/Scalable Capital: .* left within/)).not.toBeInTheDocument();

    expect(screen.getByText(/700,00\s€ expected by year end/)).toBeInTheDocument();
    const roomTile = screen.getByText(/Room left in 2026/).parentElement as HTMLElement;
    expect(within(roomTile).getByText(/300,00\s€/)).toBeInTheDocument();
  });

  it("stays hidden without an NV certificate", async () => {
    payload = { ...base, enabled: false, room: undefined, steps: [], warnings: [] };
    const { container } = renderCard();
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });
});
