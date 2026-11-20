import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { StatusStrip } from "./StatusStrip";
import type { MonthlyPlan } from "../../lib/api";

vi.mock("../../lib/api", async (orig) => ({ ...(await orig<typeof import("../../lib/api")>()), getCrashGuard: vi.fn().mockResolvedValue({ market: { level_label: "Calm" } }) }));

function strip(cash: Record<string, unknown>) {
  const plan = { cash: { savings_eur: 0, broker_cash_eur: 0, emergency_reserve_eur: 0, reserve_set: true, investable_eur: 500, ...cash } } as unknown as MonthlyPlan;
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><StatusStrip plan={plan} /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StatusStrip savings pot", () => {
  it("shows the Trading 212 savings pot next to the investable cash, not inside it", () => {
    strip({ savings_pot_eur: 673.32 });
    const chip = screen.getByText("Savings pot").closest("a");
    expect(chip).toHaveTextContent("673,32");
    expect(screen.getByText("Investable cash").closest("a")).toHaveTextContent("500,00");
  });

  it("shows no pot chip when there is none", () => {
    strip({ savings_pot_eur: 0 });
    expect(screen.queryByText("Savings pot")).toBeNull();
  });
});
