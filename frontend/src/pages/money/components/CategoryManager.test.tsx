import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CategoryManager } from "./CategoryManager";
import { api } from "../../../lib/api";

vi.mock("../../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const categories = [
  { id: "cat-1", name: "Groceries", color: "#F59E0B", icon: "shopping-cart", type: "expense" },
  {
    id: "cat-2",
    name: "Laptop Fund",
    color: "#3B82F6",
    icon: "tag",
    type: "expense",
    target_amount: 1200,
    target_date: "2027-01-01",
  },
];

function renderWithClient() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CategoryManager categories={categories} />
    </QueryClientProvider>
  );
}

describe("CategoryManager goal fields", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockResolvedValue(undefined);
  });

  it("shows a progress badge for a category with a goal", async () => {
    const user = userEvent.setup();
    renderWithClient();
    await user.click(screen.getByRole("button", { name: /categories/i }));

    expect(screen.getByText(/1\.200\s€/)).toBeInTheDocument();
  });

  it("submits target amount and target date when creating a category", async () => {
    const user = userEvent.setup();
    renderWithClient();
    await user.click(screen.getByRole("button", { name: /categories/i }));

    await user.type(screen.getByLabelText("Category name"), "Vacation Fund");
    await user.type(screen.getByLabelText("Goal target amount"), "800");
    await user.type(screen.getByLabelText("Goal target date"), "2027-06-01");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(mockedApi).toHaveBeenCalledWith(
      "/api/budget/categories",
      expect.objectContaining({ method: "POST" })
    );
    const call = mockedApi.mock.calls.find(([path]) => path === "/api/budget/categories");
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body.target_amount).toBe("800");
    expect(body.target_date).toBe("2027-06-01");
  });
});
