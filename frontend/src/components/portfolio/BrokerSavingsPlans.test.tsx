import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { BrokerSavingsPlans } from "./BrokerSavingsPlans";

const piePlan = vi.hoisted(() => ({ set: vi.fn(async () => []), clear: vi.fn(async () => []) }));

vi.mock("../../lib/api", () => ({
  setTrading212PiePlan: piePlan.set,
  clearTrading212PiePlan: piePlan.clear,
  getScalableStatus: async () => ({ enabled: true, state: "ready", last_success_at: "2026-10-27T08:00:00Z" }),
  getTrading212Status: async () => ({ enabled: true, state: "ready", last_success_at: "2026-10-27T08:00:00Z" }),
  getScalableSavingsPlans: async () => [
    { isin: "IE00B4L5Y983", name: "iShares Core MSCI World", amount: "100", frequency: "MONTHLY",
      day_of_month: 1, next_execution_date: null, kind: "security" },
  ],
  getTrading212SavingsPlans: async () => [
    { isin: "IE00B8GKDB10", name: "Vanguard High Dividend (pie Dividends)", amount: "50.00", frequency: "MONTHLY",
      next_execution_date: "2026-11-21", last_execution_date: "2026-10-22", runs: 3, pie: "Dividends", inferred: true },
    { isin: "US1696561059", name: "Chipotle", amount: "25.00", frequency: "UNKNOWN",
      next_execution_date: null, last_execution_date: "2026-10-24", runs: 1, pie: null, inferred: true },
    { isin: null, name: "Pie Income", amount: "74.28", frequency: "UNKNOWN", next_execution_date: null,
      last_execution_date: "2026-11-02", runs: 4, pie: "Income", pie_id: "7", declared: false, inferred: true },
  ],
}));

describe("BrokerSavingsPlans", () => {
  it("lists every broker's plans, saying Trading 212's are read back from its orders", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <BrokerSavingsPlans />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Vanguard High Dividend (pie Dividends)")).toBeInTheDocument();
    expect(await screen.findByText("iShares Core MSCI World")).toBeInTheDocument();
    expect(screen.getByText(/read back from the orders they placed/)).toBeInTheDocument();
    expect(screen.getByText("schedule shows after the next run")).toBeInTheDocument();
    const badges = screen.getAllByText(/^(Scalable|Trading 212)$/).map((b) => b.textContent);
    expect(badges).toEqual(["Scalable", "Trading 212", "Trading 212", "Trading 212"]);
  });

  it("lets the owner say what a pie's recurring plan is", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <BrokerSavingsPlans />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("last buy, not counted")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Set the recurring plan" }));
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Amount of Pie Income"), { target: { value: "70" } });
    fireEvent.click(save);

    await waitFor(() => expect(piePlan.set).toHaveBeenCalledWith("7", 70, "MONTHLY"));
  });
});
