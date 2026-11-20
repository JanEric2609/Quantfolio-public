import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const state = vi.hoisted(() => ({ current: {} as Record<string, unknown>, save: vi.fn() }));
vi.mock("../hooks/useQuantAllocator", () => ({
  useQuantAllocator: () => state.current,
  useSaveAllocatorUniverse: () => ({ mutate: state.save, isPending: false }),
}));

import { AllocatorView } from "./AllocatorView";

const plan = {
  label: "Equal risk contribution",
  target_weights: { "EUNL.DE": 0.5, "VWRA.L": 0.5 },
  volatility: 0.14,
  risk_contributions: {},
  eur_this_month: { "EUNL.DE": 0, "VWRA.L": 100 },
  weights_after: { "EUNL.DE": 0.8, "VWRA.L": 0.2 },
  months: 12,
  summary: "Equal risk contribution: this month's money goes to VWRA.L 100 €.",
};

describe("AllocatorView", () => {
  it("shows the euro split, the truncation warning and edits the candidate list", async () => {
    state.current = {
      isLoading: false, error: null,
      data: {
        available: true, assets: ["EUNL.DE", "VWRA.L"], contribution_eur: 100,
        weights_current: { "EUNL.DE": 0.97, "VWRA.L": 0.03 },
        plans: { erc: plan },
        universe: [
          { isin: "IE00B4L5Y983", ticker: "EUNL.DE", name: "World", held_value_eur: 9000 },
          { isin: "IE00BK5BQT80", ticker: "VWRA.L", name: "All-World", held_value_eur: 300 },
        ],
        history: { days: 120, start: "2026-04-01", end: "2026-09-30", limited_by: "VWRA.L", first_dates: {} },
      },
    };
    render(<AllocatorView />);
    expect(screen.getByTestId("plan-erc")).toHaveTextContent("Equal risk contribution");
    expect(screen.getByTestId("summary-erc")).toHaveTextContent("VWRA.L 100");
    expect(screen.getByTestId("history-warning")).toHaveTextContent("120 days, limited by VWRA.L");
    await userEvent.click(screen.getByRole("button", { name: "Remove IE00BK5BQT80" }));
    expect(state.save).toHaveBeenCalledWith(["IE00B4L5Y983"]);
    await userEvent.type(screen.getByLabelText("ISIN to add"), "IE00BKM4GZ66");
    await userEvent.click(screen.getByRole("button", { name: "Add ETF" }));
    expect(state.save).toHaveBeenLastCalledWith(["IE00B4L5Y983", "IE00BK5BQT80", "IE00BKM4GZ66"], expect.anything());
  });
});
