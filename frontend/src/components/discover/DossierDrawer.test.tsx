import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders as render } from "../../test/render";
import { DossierDrawer } from "./DossierDrawer";
import { type DiscoverDossier } from "../../lib/api";

// ---------------------------------------------------------------------------
// Fixtures — partial objects cast structurally; no MSW, no heavy api mocks.
// Mirrors the dossierFixture pattern from ConcernBadges.test.tsx.
// ---------------------------------------------------------------------------

function dossierFixture(
  overrides: Partial<DiscoverDossier> = {},
): DiscoverDossier {
  return {
    id: "dossier-1",
    conviction: 0.62,
    direction: "long",
    horizon_months: 6,
    expected_return: 4.2,
    thesis: "Rates mean-reversion with carry.",
    key_risks: ["Duration risk"],
    signal_breakdown: {},
    estimate: true,
    not_financial_advice: true,
    created_at: null,
    ...overrides,
  } as DiscoverDossier;
}

const band = {
  p10: -0.08,
  p50: 0.03,
  p90: 0.14,
  method: "historical_simulation",
  n_paths: 2000,
  estimate: true,
  not_tax_advice: true,
};

// ---------------------------------------------------------------------------
// O2: honest dual-number labeling — when the band's P50 diverges from the
// shrunk headline estimate by more than 5 percentage points, say so.
// ---------------------------------------------------------------------------

describe("DossierDrawer band/headline divergence note (O2)", () => {
  it("renders the divergence subtitle when |p50 − headline| exceeds 5pp", () => {
    // p50 30% vs headline 4.2% → 25.8pp apart
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return: 4.2,
          expected_return_band: { ...band, p50: 0.3 },
        })}
        symbol="TLT"
      />,
    );
    const note = screen.getByTestId("band-anchor-note");
    expect(note.textContent).toMatch(
      /Range simulated around the undampened historical anchor/,
    );
    expect(note.textContent).toMatch(
      /median intentionally differs from our shrunk estimate above/,
    );
  });

  it("stays silent when p50 tracks the headline within 5pp", () => {
    // p50 3% vs headline 4.2% → 1.2pp apart
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return: 4.2,
          expected_return_band: band,
        })}
        symbol="TLT"
      />,
    );
    expect(screen.queryByTestId("band-anchor-note")).toBeNull();
  });

  it("treats exactly 5pp as within tolerance (strictly greater than)", () => {
    // Binary-exact values so the boundary is not floating-point noise:
    // p50 3.125% vs headline −1.875% → exactly 5.00pp apart
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return: -1.875,
          expected_return_band: { ...band, p50: 0.03125 },
        })}
        symbol="TLT"
      />,
    );
    expect(screen.queryByTestId("band-anchor-note")).toBeNull();
  });

  it("renders no note when the headline estimate is absent", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return: null,
          expected_return_band: { ...band, p50: 0.3 },
        })}
        symbol="TLT"
      />,
    );
    expect(screen.queryByTestId("band-anchor-note")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// O6: anchor-kind disclaimer coverage — the "Anchored on …" disclaimer must
// render for every anchor kind the backend emits, including the M5 kinds.
// ---------------------------------------------------------------------------

const DISCLAIMER_KINDS: [
  NonNullable<DiscoverDossier["expected_return_anchor_kind"]>,
  RegExp,
][] = [
  ["building_blocks_credibility", /Anchored on building-blocks credibility estimate/],
  ["bl_posterior", /Anchored on Black-Litterman posterior/],
];

describe("DossierDrawer anchor-kind disclaimer coverage (O6)", () => {
  it.each(DISCLAIMER_KINDS)(
    "renders the 'Anchored on …' disclaimer for %s",
    (kind, disclaimerRe) => {
      render(
        <DossierDrawer
          open
          onClose={() => {}}
          dossier={dossierFixture({ expected_return_anchor_kind: kind })}
          symbol="TLT"
        />,
      );
      expect(screen.getByText(disclaimerRe)).toBeInTheDocument();
    },
  );
});

// ---------------------------------------------------------------------------
// O5: percent-unit hygiene in signal_breakdown — known percent-unit fields
// render with a " %" suffix; ic/icir and unknown keys stay bare.
// ---------------------------------------------------------------------------

describe("DossierDrawer signal_breakdown unit suffixes (O5)", () => {
  function valueCellFor(label: string): string | undefined {
    const labelCell = screen.getByText(label);
    return labelCell.closest("tr")?.querySelectorAll("td")[1]?.textContent ?? undefined;
  }

  it("appends ' %' to known percent-unit fields and leaves others bare", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          signal_breakdown: {
            expected_return_anchor_pct: 44.5,
            ic: 0.03,
            icir: 0.12,
            mystery_metric: 1.23,
          },
        })}
        symbol="TLT"
      />,
    );
    expect(valueCellFor("expected return anchor pct")).toBe("44,50\u00a0%");
    expect(valueCellFor("ic")).toBe("0,03");
    expect(valueCellFor("icir")).toBe("0,12");
    expect(valueCellFor("mystery metric")).toBe("1,23");
  });

  it("renders null values as an em-dash regardless of unit", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({ signal_breakdown: { expected_return_anchor_pct: null } })}
        symbol="TLT"
      />,
    );
    expect(valueCellFor("expected return anchor pct")).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// O1: PRIIPs/estimate disclaimer copy — the band caption must clarify that the
// headline is the shrunk estimate while pointing to the divergence note,
// without claiming where the simulation is centred (backend may centre it on
// either the anchor or the shrunk estimate).
// ---------------------------------------------------------------------------

describe("DossierDrawer band disclaimer copy (O1)", () => {
  it("states the headline is a shrunk estimate and keeps the not-advice caveat", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({ expected_return_band: band })}
        symbol="TLT"
      />,
    );
    const row = screen.getByTestId("return-band");
    const text = row.textContent ?? "";
    expect(text).toMatch(/headline above is our shrunk estimate/i);
    expect(text).toMatch(/estimates, not advice/i);
  });
});

// ---------------------------------------------------------------------------
// ADR 0017: the dossier shows every score input and how the estimate is built
// ---------------------------------------------------------------------------

describe("DossierDrawer score drivers and estimate composition (ADR 0017)", () => {
  it("lists every composite input with weight and contribution", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          composite_breakdown: [
            { signal: "momentum", score: 0.99, weight: 0.1765, contribution: 0.1747 },
            { signal: "benchmark", score: 0.95, weight: 0.0588, contribution: 0.0559 },
          ],
        })}
        symbol="BBVA.MC"
      />,
    );
    const table = screen.getByTestId("composite-breakdown");
    expect(table.textContent).toContain("12-1m momentum (rank)");
    expect(table.textContent).toContain("18\u00a0%");
    expect(table.textContent).toContain("0,175");
    expect(table.textContent).toContain("Risk-adjusted 3y vs benchmark");
  });

  it("explains the estimate as market-implied prior plus a credibility share", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return: 8.6,
          expected_return_components: {
            anchor_pct: 59.77,
            risk_free_pct: 2.44,
            beta: 1.067,
            beta_source: "weekly",
            equity_premium_pct: 4.5,
            prior_pct: 7.24,
            credibility_weight: 0.0333,
            tilt_pct: 1.75,
            er_annual_pct: 8.99,
          },
        })}
        symbol="BBVA.MC"
      />,
    );
    const text = screen.getByTestId("estimate-composition").textContent ?? "";
    expect(text).toContain("market-implied 7,2\u00a0%");
    expect(text).toContain("beta 1,07");
    expect(text).toContain("3,3\u00a0% of the gap");
    expect(text).toContain("= 9,0\u00a0%");
  });

  it("omits both sections for dossiers written before ADR 0017", () => {
    render(<DossierDrawer open onClose={() => {}} dossier={dossierFixture()} symbol="OLD" />);
    expect(screen.queryByTestId("composite-breakdown")).toBeNull();
    expect(screen.queryByTestId("estimate-composition")).toBeNull();
  });

  it("does not print a saturated regime posterior as 100%", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          regime_context: { regime_label: "bull", risk_on: true, confidence: 0.9999956 },
        })}
        symbol="BBVA.MC"
      />,
    );
    expect(screen.getByText(">99 %")).toBeTruthy();
  });

  it("renders the anchor kind in the signal breakdown as a label", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          signal_breakdown: { expected_return_anchor_kind: "trailing_3y_annualized_return" },
        })}
        symbol="BBVA.MC"
      />,
    );
    expect(screen.getByText("trailing 3-year annualized return")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Buying at DKB — the tradeability check used to be computed for every
// shortlisted name and never shown (Discover run audit, 2026-09-28).
// ---------------------------------------------------------------------------

describe("DossierDrawer broker tradeability", () => {
  it("shows one row per broker with its own badge and link", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="MU"
        name="Micron"
        tradeable={{
          likely_tradeable: true,
          confidence: "medium",
          reasons: ["US-listed share — single shares need no key information document", "likely — confirm at your broker"],
          brokers: {
            dkb: { likely: true, confidence: "medium", note: "DKB trades US shares on Tradegate", url: "https://www.dkb.de/privatkunden/investieren/wertpapiersuche?isin=US5951121038" },
            scalable: { likely: true, confidence: "medium", note: "Scalable trades US shares on gettex", url: "https://de.scalable.capital/broker/security?isin=US5951121038" },
          },
        }}
      />,
    );
    expect(screen.getByTestId("broker-dkb").textContent).toMatch(/Probably buyable/);
    expect(screen.getByRole("link", { name: /Check at Scalable/ })).toHaveAttribute(
      "href", "https://de.scalable.capital/broker/security?isin=US5951121038",
    );
    expect(screen.getByText(/gettex/)).toBeInTheDocument();
  });

  it("shows the verdict, the reasons and a check link opened on the ISIN", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="QDVE.DE"
        name="iShares S&P 500 Information Technology Sector UCITS ETF"
        tradeable={{
          likely_tradeable: true,
          confidence: "high",
          reasons: ["EU-domiciled fund (IE) — UCITS", "likely — confirm at DKB"],
          manual_check_url: "https://www.dkb.de/privatkunden/investieren/wertpapiersuche?isin=IE00B3WJKG14",
        }}
      />,
    );
    const section = screen.getByTestId("broker-tradeability");
    expect(section.textContent).toMatch(/Likely buyable/);
    expect(section.textContent).toMatch(/EU-domiciled fund/);
    expect(section.textContent).not.toMatch(/confirm at DKB/);
    expect(screen.getByRole("link", { name: /Check at DKB/ })).toHaveAttribute(
      "href",
      "https://www.dkb.de/privatkunden/investieren/wertpapiersuche?isin=IE00B3WJKG14",
    );
    expect(screen.getByText(/iShares S&P 500 Information Technology/)).toBeInTheDocument();
  });

  it("says a failed check is unchecked and how to search without an ISIN", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="MU"
        name="MU"
        tradeable={{
          likely_tradeable: null,
          confidence: "unknown",
          reasons: ["Tradeability check failed — confirm at DKB"],
          manual_check_url: "https://www.dkb.de/privatkunden/investieren/wertpapiersuche",
        }}
      />,
    );
    expect(screen.getByText("Not checked")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Search DKB for MU/ })).toBeInTheDocument();
    expect(screen.getByText(/No ISIN on file/)).toBeInTheDocument();
  });

  it("names the regime model the dossier used", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          regime_context: { regime_label: "sideways", risk_on: false, crisis: false, confidence: 1, source: "hmm" },
        })}
        symbol="MU"
      />,
    );
    expect(screen.getByText(/Current regime \(HMM model\)/)).toBeInTheDocument();
    expect(screen.getByText(/header chip shows a separate rule-based regime/)).toBeInTheDocument();
  });

  it("names the jump model when the dossier's regime came from it", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          regime_context: { regime_label: "sideways", risk_on: false, crisis: false, confidence: 1, source: "jump" },
        })}
        symbol="MU"
      />,
    );
    expect(screen.getByText(/Current regime \(jump model\)/)).toBeInTheDocument();
    expect(screen.getByText(/header chip shows a separate rule-based regime/)).toBeInTheDocument();
  });
});
