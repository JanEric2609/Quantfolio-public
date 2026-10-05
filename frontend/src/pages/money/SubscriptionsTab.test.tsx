import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SubscriptionsTab } from "./SubscriptionsTab";
import { api } from "../../lib/api";

vi.mock("../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const subscription = {
  id: "sub-1",
  name: "Streaming",
  amount: "9.99",
  currency: "EUR",
  billing_cycle: "monthly",
  next_due_date: "2026-09-01",
  active: true,
};

function renderWithClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <SubscriptionsTab />
    </QueryClientProvider>
  );
  return invalidateSpy;
}

function invalidatedKeys(spy: ReturnType<typeof vi.spyOn>): string[] {
  return spy.mock.calls.map((call: unknown[]) => ((call[0] as { queryKey?: unknown[] } | undefined)?.queryKey?.[0] as string) ?? "");
}

describe("SubscriptionsTab mark-paid cache invalidation", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/budget/subscriptions" && !init) return [subscription];
      if (path === "/api/budget/categories") return [];
      if (path === "/api/budget/subscriptions/suggestions") return [];
      if (path === `/api/budget/subscriptions/${subscription.id}/mark-paid`) return { ok: true };
      return undefined;
    });
  });

  it("mark-paid success invalidates budget-summary alongside the subscription keys", async () => {
    const user = userEvent.setup();
    const invalidateSpy = renderWithClient();

    await screen.findByText("Streaming");
    await user.click(screen.getByRole("button", { name: "Paid" }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        `/api/budget/subscriptions/${subscription.id}/mark-paid`,
        expect.objectContaining({ method: "POST" })
      );
    });

    await waitFor(() => {
      expect(invalidatedKeys(invalidateSpy)).toEqual(
        expect.arrayContaining(["budget-summary"])
      );
    });
    // The full staleness contract for a paid subscription:
    expect(invalidatedKeys(invalidateSpy)).toEqual(
      expect.arrayContaining(["subscriptions", "budget-summary", "subscription-suggestions", "expenses", "budget-cashflow"])
    );
  });
});
