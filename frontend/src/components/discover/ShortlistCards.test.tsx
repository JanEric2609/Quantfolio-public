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
