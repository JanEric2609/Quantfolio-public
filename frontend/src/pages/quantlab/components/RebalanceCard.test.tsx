import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../../test/render";
import type { RealRebalanceResponse } from "../../../lib/api";

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("../../../lib/api", async (orig) => ({ ...(await orig<typeof import("../../../lib/api")>()), api: apiMock }));

import { RebalanceCard } from "./RebalanceCard";

const data: RealRebalanceResponse = {
  available: true,
  contribution_eur: 100,
  band_pp: 5,
  sells: true,
  tax_eur: 0,
  current_weights: {},
  optimal_weights: {},
  suggestions: [],
  diagnostics: {},
  lines: [
    {
      key: "IE00B4L5Y983", isin: "IE00B4L5Y983", ticker: "EUNL.DE", name: "MSCI World", value_eur: 15000,
      current_weight: 0.9, target_weight: 0.5, drift_pp: 40, in_band: false, buy_eur: 0, sell_eur: 5000,
      after_weight: 0.5, taxable_gain_eur: null, tax_eur: null, gain_complete: false, cost_unknown_depots: ["DKB"],
      depots: [{
        source: "dkb", broker: "DKB", account_id: "a1", position_id: "p1", quantity: 120.5, value_eur: 15000,
        cost_eur: null, cost_source: null, can_enter_cost: true,
      }],
    },
  ],
};

describe("RebalanceCard cost entry", () => {
  beforeEach(() => apiMock.mockReset().mockResolvedValue({}));

  it("shows cost unknown and saves an Einstandswert for the DKB position", async () => {
    renderWithProviders(<RebalanceCard data={data} loading={false} target="erc" onTarget={() => {}} />);
    expect(screen.getByText("cost unknown")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /enter Einstandswert/ }));
    await userEvent.type(screen.getByLabelText("Einstandswert DKB"), "30000,50");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalled());
    expect(apiMock).toHaveBeenCalledWith("/api/dkb/positions/p1/cost", {
      method: "PUT",
      body: JSON.stringify({ einstandswert_eur: 30000.5 }),
    });
  });
});

describe("RebalanceCard sleeve mode", () => {
  it("shows the plan's sleeves, a buy for an unheld sleeve and an incomplete tax note", () => {
    const sleeveData: RealRebalanceResponse = {
      ...data,
      tax_complete: false,
      sleeves: [
        { key: "core", label: "Core", current_pct: 96, target_pct: 85, after_pct: 90, buy_eur: 0, sell_eur: 0, unlocked: true },
        { key: "tilt", label: "Factor tilt", current_pct: 0, target_pct: 15, after_pct: 1, buy_eur: 100, sell_eur: 0, unlocked: true },
        { key: "satellite", label: "Stock picks", current_pct: 4, target_pct: 0, after_pct: 4, buy_eur: 0, sell_eur: 0, unlocked: false },
      ],
      suggestions: [
        { ticker: "Factor tilt (no fund chosen)", action: "buy", isin: null, sleeve: "tilt", current_weight: 0, target_weight: 0.15, diff: 0.15, estimated_amount: 100 },
      ],
    };
    renderWithProviders(<RebalanceCard data={sleeveData} loading={false} target="sleeves" onTarget={() => {}} />);
    expect(screen.getByRole("table", { name: "Sleeves" })).toBeInTheDocument();
    expect(screen.getByText(/locked: held, never bought or sold here/)).toBeInTheDocument();
    expect(screen.getByText(/choose a fund for this sleeve in Plan settings/)).toBeInTheDocument();
    expect(screen.getByText(/incomplete: a depot has no cost basis/)).toBeInTheDocument();
  });
});
