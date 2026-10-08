import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders as render } from "../../test/render";
import { ShortlistCards, sourceLabel } from "./ShortlistCards";
import { type DiscoverCandidate } from "../../lib/api";

function candidate(symbol: string, conviction: number | null, overrides: Partial<DiscoverCandidate> = {}) {
  return {
    id: symbol,
    symbol,
    isin: null,
    name: symbol,
    source: "screen_index",
    status: "shortlisted",
    reject_stage: null,
    reject_reason: null,
    scores: { composite: 0.5 },
    tradeable: {},
    dossier_id: null,
    recommendation_id: null,
    conviction,
    ...overrides,
  } as DiscoverCandidate;
}

describe("ShortlistCards", () => {
  it("lists the strongest candidate first, not the first in the alphabet", () => {
    render(
      <ShortlistCards
        candidates={[
          candidate("AMD", 0.60),
          candidate("MU", 0.634),
          candidate("GS", null, { scores: { composite: 0.62 } }),
        ]}
        onOpenDossier={() => {}}
      />,
    );
    const titles = screen.getAllByText(/^(AMD|MU|GS)$/).map((el) => el.textContent);
    expect(titles).toEqual(["MU", "GS", "AMD"]);
  });

  it("shows the company name and a readable origin, not the ticker and screen_index", () => {
    render(
      <ShortlistCards
        candidates={[candidate("MU", 0.6, { name: "Micron Technology, Inc." }), candidate("GS", 0.5)]}
        onOpenDossier={() => {}}
      />,
    );
    expect(screen.getByText("Micron Technology, Inc. · Index screen")).toBeInTheDocument();
    expect(screen.getAllByText("Index screen")).toHaveLength(1); // GS: name repeated the ticker
    expect(screen.queryByText(/screen_index/)).not.toBeInTheDocument();
    expect(sourceLabel("screen_etf")).toBe("ETF screen");
  });
});

describe("ShortlistCards pool rank (ADR 0018 §6)", () => {
  const base = {
    id: "c1", symbol: "AAPL", isin: null, name: "Apple", source: "screen_index", status: "shortlisted",
    reject_stage: null, reject_reason: null, scores: { composite: 0.8 }, tradeable: {}, dossier_id: null,
    recommendation_id: null,
  };

  it("shows the rank in the run's pool as a tier, not a probability", () => {
    render(<ShortlistCards candidates={[{ ...base, pool_rank: 3, pool_size: 187 }]} onOpenDossier={() => {}} />);
    const line = screen.getByTestId("pool-rank");
    expect(line).toHaveTextContent("Rank 3 of 187 scored stocks (top third of this run)");
    expect(line).toHaveTextContent("Not a probability");
  });

  it("adds the tier's measured hit rate and the base rate when there are outcomes", () => {
    const tiers = {
      n_eff: 40,
      base: { n_runs: 40, hit_rate: 0.5, range: [0.4, 0.6] as [number, number] },
      tiers: [
        { tier: "top" as const, n_runs: 40, hit_rate: 0.56, range: [0.45, 0.66] as [number, number] },
        { tier: "middle" as const, n_runs: 40, hit_rate: 0.5, range: null },
        { tier: "bottom" as const, n_runs: 40, hit_rate: 0.44, range: null },
      ],
    };
    render(<ShortlistCards candidates={[{ ...base, pool_rank: 150, pool_size: 187 }]} onOpenDossier={() => {}} tiers={tiers} />);
    expect(screen.getByTestId("pool-rank")).toHaveTextContent(
      "(bottom third of this run). Stocks in that tier beat your ETF over 21 days 44 % of the time, against 50 % for every scored stock, over 40 dates.",
    );
  });

  it("shows nothing for a run before the ledger existed", () => {
    render(<ShortlistCards candidates={[base]} onOpenDossier={() => {}} />);
    expect(screen.queryByTestId("pool-rank")).not.toBeInTheDocument();
  });
});
