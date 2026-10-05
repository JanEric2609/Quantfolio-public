import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "../ui/tooltip";
import { RegimeChip, regimeStyle } from "./RegimeChip";

vi.mock("../../lib/api", () => ({
  getMacroRegime: async () => ({
    label: "sideways",
    confidence: null,
    model: "jump",
    available: true,
    vix: 16.2,
    credit_spread: 1.5,
    drawdown: -0.04,
    state_since: "2026-09-20",
    as_of: "2026-09-27",
    updated_at: "2026-09-28T08:00:00Z",
  }),
}));

describe("RegimeChip", () => {
  it("renders the jump model's state", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <TooltipProvider>
          <RegimeChip />
        </TooltipProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "Sideways" })).toBeInTheDocument();
  });

  it("keeps the colours of the known labels", () => {
    expect(regimeStyle("bull").label).toBe("Bull");
    expect(regimeStyle("bear").label).toBe("Bear");
    expect(regimeStyle("high_vol").label).toBe("High vol"); // a retired label still renders
    expect(regimeStyle(null).label).toBe("Unknown");
  });
});
