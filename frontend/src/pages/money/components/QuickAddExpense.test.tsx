import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { QuickAddExpense } from "./QuickAddExpense";
import { api } from "../../../lib/api";

vi.mock("../../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const categories = [
  { id: "cat-groceries", name: "Groceries", color: "#F59E0B", icon: "shopping-cart", type: "expense" },
  { id: "cat-travel", name: "Travel", color: "#2DD4BF", icon: "plane", type: "expense" },
];

function renderWithClient() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <QuickAddExpense />
    </QueryClientProvider>
  );
}

describe("QuickAddExpense", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/budget/categories") return categories;
      if (path === "/api/budget/expenses") return [];
      return undefined;
    });
  });

  it("opens the sheet, enters an amount via the numpad, and saves on category tap", async () => {
    const user = userEvent.setup();
    renderWithClient();

    await user.click(screen.getByRole("button", { name: /Quick Add/i }));
    await screen.findByRole("group", { name: "Amount numpad" });

    await user.click(screen.getByRole("button", { name: "Digit 3" }));
    await user.click(screen.getByRole("button", { name: "Digit 5" }));
    await user.click(screen.getByRole("button", { name: "Digit 0" }));
    expect(screen.getByText("3,50 €")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Groceries/ }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        "/api/budget/expenses",
        expect.objectContaining({ method: "POST" })
      );
    });
    const call = mockedApi.mock.calls.find(([path, init]) => path === "/api/budget/expenses" && (init as RequestInit)?.method === "POST");
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body).toMatchObject({ amount: 3.5, currency: "EUR", category_id: "cat-groceries", description: "Groceries" });
  });

  it("excludes income-category expenses from the quick-repeat favorites", async () => {
    const daysAgo = (n: number) => new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
    const categoriesWithIncome = [
      ...categories,
      { id: "cat-salary", name: "Salary", color: "#22C55E", icon: "wallet", type: "income" },
    ];
    const expenses = [
      { date: daysAgo(20), description: "Paycheck", amount: "2000", category_id: "cat-salary" },
      { date: daysAgo(6), description: "Paycheck", amount: "2000", category_id: "cat-salary" },
      { date: daysAgo(19), description: "Coffee", amount: "3.50", category_id: "cat-groceries" },
      { date: daysAgo(5), description: "Coffee", amount: "3.50", category_id: "cat-groceries" },
    ];
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/budget/categories") return categoriesWithIncome;
      if (path === "/api/budget/expenses") return expenses;
      return undefined;
    });

    const user = userEvent.setup();
    renderWithClient();

    await user.click(screen.getByRole("button", { name: /Quick Add/i }));
    await screen.findByRole("group", { name: "Quick repeat" });

    expect(screen.getByText(/Coffee/)).toBeInTheDocument();
    expect(screen.queryByText(/Paycheck/)).not.toBeInTheDocument();
  });

  it("does not save when no amount has been entered", async () => {
    const user = userEvent.setup();
    renderWithClient();

    await user.click(screen.getByRole("button", { name: /Quick Add/i }));
    await screen.findByRole("group", { name: "Categories" });
    await user.click(screen.getByRole("button", { name: /Travel/ }));

    expect(mockedApi).not.toHaveBeenCalledWith(
      "/api/budget/expenses",
      expect.objectContaining({ method: "POST" })
    );
  });
});
