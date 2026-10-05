import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EnvelopeBudgetsTab } from "./EnvelopeBudgetsTab";
import { api } from "../../lib/api";

vi.mock("../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const envelopes = {
  money_to_budget: 900,
  envelopes: [
    {
      category_id: "cat-groceries",
      category_name: "Groceries",
      color: "#F59E0B",
      icon: "shopping-cart",
      budgeted: 200,
      spent: 50,
      remaining: 150,
      overspent: false,
      goal: null,
    },
  ],
};

function renderWithClient() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <EnvelopeBudgetsTab />
    </QueryClientProvider>
  );
  return { ...utils, queryClient };
}

const julyOverspentEnvelopes = {
  money_to_budget: 900,
  envelopes: [
    {
      category_id: "cat-groceries",
      category_name: "Groceries",
      color: "#F59E0B",
      icon: "shopping-cart",
      budgeted: 200,
      spent: 250,
      remaining: -50,
      overspent: true,
      goal: null,
    },
    {
      category_id: "cat-travel",
      category_name: "Travel",
      color: "#10B981",
      icon: "plane",
      budgeted: 100,
      spent: 20,
      remaining: 80,
      overspent: false,
      goal: null,
    },
  ],
};

const augustOverspentEnvelopes = {
  money_to_budget: 900,
  envelopes: [
    {
      category_id: "cat-groceries",
      category_name: "Groceries",
      color: "#F59E0B",
      icon: "shopping-cart",
      budgeted: 200,
      spent: 300,
      remaining: -100,
      overspent: true,
      goal: null,
    },
    {
      category_id: "cat-travel",
      category_name: "Travel",
      color: "#10B981",
      icon: "plane",
      budgeted: 100,
      spent: 20,
      remaining: 80,
      overspent: false,
      goal: null,
    },
  ],
};

describe("EnvelopeBudgetsTab", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/budget/envelopes")) return envelopes;
      return undefined;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders envelope progress for each category", async () => {
    renderWithClient();

    expect(await screen.findByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("150,00 € left")).toBeInTheDocument();
    expect(screen.getByText("50,00 € spent")).toBeInTheDocument();
  });

  it("submits an updated budget amount via PUT", async () => {
    const user = userEvent.setup();
    renderWithClient();

    await screen.findByText("Groceries");
    const input = screen.getByLabelText("Budget for Groceries");
    await user.clear(input);
    await user.type(input, "250");
    await user.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        "/api/budget/envelopes/cat-groceries",
        expect.objectContaining({ method: "PUT" })
      );
    });
    const call = mockedApi.mock.calls.find(
      ([path, init]) => path === "/api/budget/envelopes/cat-groceries" && (init as RequestInit)?.method === "PUT"
    );
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body.budgeted_amount).toBe(250);
  });

  it("resets a dismissed cover suggestion when the displayed month changes", async () => {
    // No vi.useFakeTimers() here — per project lesson (student-budget Phase 1), enabling
    // fake timers hangs waitFor/findByRole in this codebase when the component under test
    // has no `now` injection point. vi.setSystemTime() alone only mocks Date.*/new Date()
    // and leaves real timers (and RTL's polling) untouched.
    vi.setSystemTime(new Date(2026, 6, 15)); // July 15, 2026
    mockedApi.mockImplementation(async (path: string) => {
      if (path.includes("month=7")) return julyOverspentEnvelopes;
      if (path.includes("month=8")) return augustOverspentEnvelopes;
      return { money_to_budget: 0, envelopes: [] };
    });

    const user = userEvent.setup();
    const { rerender, queryClient } = renderWithClient();

    await screen.findByText("250,00 € spent");
    expect(screen.getByRole("button", { name: "Dismiss suggestion" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(screen.queryByRole("button", { name: "Dismiss suggestion" })).not.toBeInTheDocument();

    // Simulate the browser tab staying open across the month boundary and August's data
    // already being warm in the query cache (e.g. a background refetch completed, or the
    // query was prefetched) — so the transition to August updates the existing envelopes
    // query in place, with no intervening "isLoading" render. This is deliberate: if we let
    // React Query go through an actual never-before-fetched query key here, switching to the
    // new key transiently yields `data: undefined`, `rows` becomes `[]`, and every <Card>
    // unmounts and remounts regardless of its key — that incidental full unmount would mask
    // the bug under test (it resets EnvelopeCoverSuggestion's state for an unrelated reason).
    // Pre-warming the cache isolates the one thing this test cares about: whether the same
    // <Card> element/key is reused in place across the month boundary.
    queryClient.setQueryData(["envelopes", 2026, 8], augustOverspentEnvelopes);
    vi.setSystemTime(new Date(2026, 7, 15)); // August 15, 2026
    rerender(
      <QueryClientProvider client={queryClient}>
        <EnvelopeBudgetsTab />
      </QueryClientProvider>
    );

    // August's overspend is a distinct, unrelated event — it must not stay hidden behind
    // July's stale dismissal. The data updated in place (no loading flash — see above), so
    // if this assertion passes it's specifically because the <Card> was remounted on its key.
    expect(await screen.findByText("300,00 € spent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss suggestion" })).toBeInTheDocument();
  });
});

describe("EnvelopeBudgetsTab goal progress", () => {
  beforeEach(() => {
    mockedApi.mockReset();
  });

  it("shows the money-to-budget header total", async () => {
    mockedApi.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/budget/envelopes")) return { money_to_budget: 900, envelopes: [] };
      return undefined;
    });
    renderWithClient();

    expect(await screen.findByText(/of 900,00\s€ budgeted this month/i)).toBeInTheDocument();
  });

  it("shows goal progress and an apply-suggested action for a goal category", async () => {
    const user = userEvent.setup();
    mockedApi.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/budget/envelopes")) {
        return {
          money_to_budget: 0,
          envelopes: [
            {
              category_id: "cat-laptop",
              category_name: "Laptop Fund",
              color: "#3B82F6",
              icon: "tag",
              budgeted: 200,
              spent: 0,
              remaining: 200,
              overspent: false,
              goal: { target_amount: 1200, target_date: "2027-01-01", progress: 200, required_monthly: 166.67 },
            },
          ],
        };
      }
      return undefined;
    });
    renderWithClient();

    // formatCurrency is fixed to de-DE: "." thousands separator, "," decimals, trailing €.
    expect(await screen.findByText(/200,00\s€ \/ 1\.200,00\s€/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /use suggested/i }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        "/api/budget/envelopes/cat-laptop",
        expect.objectContaining({ method: "PUT" })
      );
    });
    const call = mockedApi.mock.calls.find(
      ([path, init]) => path === "/api/budget/envelopes/cat-laptop" && (init as RequestInit)?.method === "PUT"
    );
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body.budgeted_amount).toBeCloseTo(166.67, 2);
  });
});
