import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { EvidenceConsequences } from "./EvidenceConsequences";
import type { MonthlyPlan, PlanSleeve } from "../../lib/api";

function sleeve(overrides: Partial<PlanSleeve>): PlanSleeve {
  return {
    key: "core", label: "Core", current_eur: 0, current_pct: 0, target_pct: 0, max_pct: 100,
    unlocked: true, status: "", contribution_eur: 0, after_eur: 0, after_pct: 0, positions: [],
    ...overrides,
  };
}

vi.mock("../../lib/api", () => ({
  getMonthlyPlan: async (): Promise<Partial<MonthlyPlan>> => ({
    sleeves: [
      sleeve({ key: "core", label: "Core", status: "Receives the monthly savings plan." }),
      sleeve({ key: "tilt", label: "Factor tilt", max_pct: 15, unlocked: true, status: "Unlocked: Value + momentum passed." }),
      sleeve({
        key: "satellite", label: "Stock picks", max_pct: 10, unlocked: false,
        status: "No stock-picking strategy passed the evidence gate.",
      }),
    ],
  }),
}));

describe("EvidenceConsequences", () => {
  it("says what each gated part of the book may do with money", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <EvidenceConsequences />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/Factor tilt: may receive money, up to 15 % of the book/)).toBeInTheDocument();
    expect(screen.getByText(/Stock picks: receives no money \(target 0 %\)/)).toBeInTheDocument();
    expect(screen.getByText("No stock-picking strategy passed the evidence gate.")).toBeInTheDocument();
    expect(screen.queryByText(/^Core:/)).not.toBeInTheDocument();
  });
});
