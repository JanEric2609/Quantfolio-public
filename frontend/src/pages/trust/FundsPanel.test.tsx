import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TrustFunds } from "../../lib/api";
import { FundsPanel } from "./FundsPanel";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

function renderPanel(data: TrustFunds) {
  apiMock.mockReset();
  apiMock.mockResolvedValue(data);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <FundsPanel />
    </QueryClientProvider>,
  );
}

const base: TrustFunds = { as_of: "2026-10-01", years: 3, flag_pct: 0.1, funds: [], n_other_positions: 0, note: "Note." };

describe("FundsPanel", () => {
  it("says when no held position is a curated fund", async () => {
    renderPanel({ ...base, n_other_positions: 4 });
    expect(await screen.findByText(/nothing to compare/)).toBeInTheDocument();
  });

  it("shows each held fund against its peers and flags one that trails", async () => {
    renderPanel({
      ...base,
      n_other_positions: 1,
      funds: [
        {
          isin: "IE00B4L5Y983", name: "iShares Core MSCI World (Acc)", index: "MSCI World", value: 1100, td_pct: -0.1,
          best_peer: { isin: "IE00BJ0KDQ92", name: "Xtrackers MSCI World 1C", td_pct: 0.1 }, gap_pct: 0.2, flag: true,
        },
        {
          isin: "IE00BK5BQT80", name: "Vanguard FTSE All-World (Acc)", index: "FTSE All-World", value: 1000, td_pct: null,
          best_peer: null, gap_pct: null, flag: false,
        },
      ],
    });
    const world = await screen.findByTestId("fund-IE00B4L5Y983");
    expect(world).toHaveTextContent("MSCI World: −0.10 % a year against the median of its peers.");
    expect(world).toHaveTextContent("Xtrackers MSCI World 1C tracked 0.20 % a year better.");
    expect(world).toHaveTextContent("Trails a peer");
    expect(screen.getByTestId("fund-IE00BK5BQT80")).toHaveTextContent("no measured peer on the same index");
    expect(screen.getByText(/1 other positions are not compared/)).toBeInTheDocument();
  });
});
