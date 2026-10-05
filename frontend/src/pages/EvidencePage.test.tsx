import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { EvidencePage } from "./EvidencePage";

describe("EvidencePage", () => {
  it("renders an empty ledger when the API answers with a partial payload", async () => {
    // The test setup's fetch stub answers every request with `{}`: no
    // by_context, and no array for the ledger. This used to throw in render
    // and flake router.test.tsx's /lab → /evidence redirect.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <EvidencePage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("No ledger entries yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Evidence/ })).toBeInTheDocument();
  });
});
