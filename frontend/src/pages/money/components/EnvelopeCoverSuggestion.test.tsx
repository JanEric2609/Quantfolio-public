import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EnvelopeCoverSuggestion } from "./EnvelopeCoverSuggestion";
import { api } from "../../../lib/api";

vi.mock("../../../lib/api", () => ({ api: vi.fn() }));
const mockedApi = vi.mocked(api);

const candidates = [{ category_id: "cat-travel", category_name: "Travel", remaining: 40 }];

function renderWithClient(props: Partial<React.ComponentProps<typeof EnvelopeCoverSuggestion>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <EnvelopeCoverSuggestion
        toCategoryId="cat-groceries"
        toCategoryName="Groceries"
        deficit={20}
        year={2026}
        month={8}
        candidates={candidates}
        {...props}
      />
    </QueryClientProvider>
  );
}

describe("EnvelopeCoverSuggestion", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    mockedApi.mockResolvedValue({});
  });

  it("posts a cover request with the selected source category on Move", async () => {
    const user = userEvent.setup();
    renderWithClient();

    await user.click(screen.getByRole("button", { name: "Move" }));

    await waitFor(() => {
      expect(mockedApi).toHaveBeenCalledWith(
        "/api/budget/envelopes/cover",
        expect.objectContaining({ method: "POST" })
      );
    });
    const call = mockedApi.mock.calls.find(([path]) => path === "/api/budget/envelopes/cover");
    const body = JSON.parse((call?.[1] as RequestInit).body as string);
    expect(body).toMatchObject({
      from_category_id: "cat-travel",
      to_category_id: "cat-groceries",
      amount: 20,
      year: 2026,
      month: 8,
    });
  });

  it("hides the suggestion when dismissed without calling the API", async () => {
    const user = userEvent.setup();
    renderWithClient();

    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));

    expect(screen.queryByRole("button", { name: "Move" })).not.toBeInTheDocument();
    expect(mockedApi).not.toHaveBeenCalled();
  });

  it("renders nothing when there are no cover candidates", () => {
    renderWithClient({ candidates: [] });

    expect(screen.queryByRole("button", { name: "Move" })).not.toBeInTheDocument();
  });
});
