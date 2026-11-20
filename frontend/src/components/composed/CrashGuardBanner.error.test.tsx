import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  getCrashGuard: vi.fn().mockRejectedValue(new Error("boom")),
}));

import { CrashGuardBanner } from "./CrashGuardBanner";

describe("CrashGuardBanner when the request fails", () => {
  it("says so instead of rendering nothing (silence reads as a calm market)", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><CrashGuardBanner /></MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
  });
});
