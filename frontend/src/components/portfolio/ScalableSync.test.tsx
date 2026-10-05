import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ScalableReview, ScalableSyncButton } from "./ScalableSync";

const apply = vi.fn(async (decisions: Record<string, string>) => ({
  replaced: Object.values(decisions).filter((d) => d === "replace").length,
  kept: 0,
}));

vi.mock("../../lib/api", () => ({
  getScalableStatus: async () => ({
    source: "scalable", enabled: true, state: "login_required", message: "Scalable session expired",
    error_code: "no_session", last_sync_at: null, last_success_at: null, stale: false,
    portfolio_id: "pf-1", tracking_since: "2026-09-30", positions: 2, pending_reconciliation: 1, read_only: true,
  }),
  previewScalableReconcile: async () => [
    {
      holding_id: "h1", isin: "IE00B4L5Y983", name: "iShares Core MSCI World", manual_quantity: "10",
      manual_value: "1000", scalable_quantity: "10", scalable_value: "1000", decision: null, quantity_matches: true,
    },
    {
      holding_id: "h2", isin: "US0378331005", name: "Apple", manual_quantity: "3",
      manual_value: "300", scalable_quantity: "5", scalable_value: "500", decision: "keep_both", quantity_matches: false,
    },
  ],
  applyScalableReconcile: (decisions: Record<string, string>) => apply(decisions),
  syncScalable: vi.fn(),
  getScalableSavingsPlans: async () => [],
}));

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ScalableReview", () => {
  it("lists only undecided matches and applies nothing until a choice is made", async () => {
    wrap(<ScalableReview />);
    expect(await screen.findByText("iShares Core MSCI World")).toBeInTheDocument();
    // Already decided "keep both": not asked again.
    expect(screen.queryByText("Apple")).not.toBeInTheDocument();

    const applyButton = screen.getByRole("button", { name: /apply/i });
    expect(applyButton).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: "Replace" }));
    expect(applyButton).toBeEnabled();
    fireEvent.click(applyButton);
    await waitFor(() => expect(apply).toHaveBeenCalledWith({ h1: "replace" }));
  });
});

describe("ScalableSyncButton", () => {
  it("shows an expired login and links to the fix", async () => {
    wrap(<ScalableSyncButton />);
    expect(await screen.findByText(/Scalable: login expired/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fix" })).toHaveAttribute("href", "/settings/bank?connection=scalable");
  });
});
