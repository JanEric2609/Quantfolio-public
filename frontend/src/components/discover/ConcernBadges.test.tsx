import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders as render } from "../../test/render";
import { ConcernBadges, CONCERN_LABELS, resolveConcernMeta } from "./ConcernBadges";
import { ShortlistCards } from "./ShortlistCards";
import { DossierDrawer } from "./DossierDrawer";
import { RejectedTable } from "./RejectedTable";
import { type DiscoverCandidate, type DiscoverDossier } from "../../lib/api";

// ---------------------------------------------------------------------------
// Fixtures — partial objects cast structurally; no MSW, no heavy api mocks.
// ---------------------------------------------------------------------------

function candidateFixture(
  overrides: Partial<DiscoverCandidate> = {},
): DiscoverCandidate {
  return {
    id: "cand-1",
    symbol: "TLT",
    isin: null,
    name: "iShares 20+ Year Treasury",
    source: "etf_core",
    status: "shortlisted",
    reject_stage: null,
    reject_reason: null,
    scores: {},
    tradeable: {},
    dossier_id: null,
    recommendation_id: null,
    ...overrides,
  } as DiscoverCandidate;
}

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
    signal_breakdown: { momentum: 0.4 },
    estimate: true,
    not_financial_advice: true,
    created_at: null,
    ...overrides,
  } as DiscoverDossier;
}

/** Radix tooltips open on hover or focus; focus is the reliable jsdom path. */
function openTooltip(trigger: Element) {
  fireEvent.pointerEnter(trigger);
  fireEvent.focus(trigger);
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

describe("ConcernBadges", () => {
  it("renders one badge per concern with stable sanitized testids", () => {
    render(
      <ConcernBadges
        concerns={[
          "short_history",
          "data_gap",
          "SPY data stale since 2026-07-16",
        ]}
      />,
    );
    expect(screen.getByTestId("concern-badge-short_history")).toBeInTheDocument();
    expect(screen.getByTestId("concern-badge-data_gap")).toBeInTheDocument();
    // slug: lowercase + specials collapsed to underscores → stable across runs
    expect(
      screen.getByTestId("concern-badge-spy_data_stale_since_2026_07_16"),
    ).toBeInTheDocument();
  });

  it("renders nothing for [] and undefined", () => {
    const empty = render(<ConcernBadges concerns={[]} />);
    expect(empty.container).toBeEmptyDOMElement();
    empty.unmount();

    const missing = render(<ConcernBadges concerns={undefined} />);
    expect(missing.container).toBeEmptyDOMElement();
  });

  it("shows the human label for a known slug", () => {
    render(<ConcernBadges concerns={["alphacrafter_miner_unavailable"]} />);
    expect(
      screen.getByText(/predictive model unavailable/i),
    ).toBeInTheDocument();
  });

  it("renders unknown slugs raw (forward-compatible)", () => {
    render(<ConcernBadges concerns={["brand_new_engine_flag"]} />);
    expect(screen.getByText("brand_new_engine_flag")).toBeInTheDocument();
  });

  it("truncates at max and lists hidden slugs in the '+N more' tooltip", async () => {
    render(
      <ConcernBadges
        concerns={[
          "short_history",
          "data_gap",
          "sentiment_unavailable",
          "fundamentals_unavailable",
          "alphacrafter_miner_no_valid_factors",
        ]}
      />,
    );
    // default max = 4 visible
    expect(screen.getByTestId("concern-badge-short_history")).toBeInTheDocument();
    expect(
      screen.getByTestId("concern-badge-fundamentals_unavailable"),
    ).toBeInTheDocument();
    const more = screen.getByTestId("concern-badge-more");
    expect(more).toHaveTextContent("+1 more");

    openTooltip(more);
    expect(
      await screen.findByText(/alphacrafter_miner_no_valid_factors/),
    ).toBeInTheDocument();
  });

  it("maps the dynamic stale-data pattern to '<SYMBOL> price data stale'", () => {
    render(<ConcernBadges concerns={["QQQ data stale since 2025-11-03"]} />);
    expect(screen.getByText("QQQ price data stale")).toBeInTheDocument();
  });

  it("maps an upcoming earnings report to a warning badge", () => {
    render(<ConcernBadges concerns={["earnings on 2026-09-30 (in 2 days)"]} />);
    const badge = screen.getByTestId("concern-badge-earnings_on_2026_09_30_in_2_days");
    expect(badge).toHaveTextContent("Earnings in 2 days");
    expect(resolveConcernMeta("earnings on 2026-09-29 (in 1 days)").label).toBe("Earnings in 1 day");
    expect(resolveConcernMeta("earnings on 2026-09-28 (in 0 days)").severity).toBe("warn");
  });

  it("shows an honest, non-alarming label for ml_signal_unavailable", () => {
    render(<ConcernBadges concerns={["ml_signal_unavailable"]} />);
    expect(screen.getByText(/no validated ml model/i)).toBeInTheDocument();
    expect(screen.getByTestId("concern-badge-ml_signal_unavailable")).toBeInTheDocument();
  });

  it("shows an honest, non-alarming label for estimate_data_unavailable", () => {
    render(<ConcernBadges concerns={["estimate_data_unavailable"]} />);
    expect(screen.getByText(/no analyst-estimate data/i)).toBeInTheDocument();
    expect(screen.getByTestId("concern-badge-estimate_data_unavailable")).toBeInTheDocument();
  });

  it("shows an honest, non-alarming label for insider_data_unavailable", () => {
    render(<ConcernBadges concerns={["insider_data_unavailable"]} />);
    expect(screen.getByText(/no insider-trading data/i)).toBeInTheDocument();
    expect(screen.getByTestId("concern-badge-insider_data_unavailable")).toBeInTheDocument();
  });

  it("exposes metadata for every known slug", () => {
    for (const [slug, meta] of Object.entries(CONCERN_LABELS)) {
      expect(meta.label.length).toBeGreaterThan(0);
      expect(meta.hint.length).toBeGreaterThan(0);
      expect(["warn", "info"]).toContain(meta.severity);
      expect(slug).toMatch(/^[a-z0-9_]+$/);
    }
  });
});

// ---------------------------------------------------------------------------
// Integration: surfaces that consume candidate scores
// ---------------------------------------------------------------------------

describe("ShortlistCards concern badges", () => {
  it("renders badges from candidate.scores.concerns", () => {
    render(
      <ShortlistCards
        candidates={[
          candidateFixture({ scores: { concerns: ["short_history"] } }),
        ]}
        onOpenDossier={() => {}}
      />,
    );
    expect(screen.getByTestId("concern-badge-short_history")).toBeInTheDocument();
  });

  it("renders no badges when scores carry no concerns", () => {
    const { container } = render(
      <ShortlistCards candidates={[candidateFixture()]} onOpenDossier={() => {}} />,
    );
    expect(container.querySelector("[data-testid^='concern-badge-']")).toBeNull();
  });
});

describe("DossierDrawer concern badges", () => {
  it("renders badges when the concerns prop is provided", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="TLT"
        concerns={["alphacrafter_miner_unavailable"]}
      />,
    );
    expect(
      screen.getByTestId("concern-badge-alphacrafter_miner_unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/predictive model unavailable/i),
    ).toBeInTheDocument();
  });

  it("renders no caveat section without concerns", () => {
    const { container } = render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="TLT"
      />,
    );
    expect(container.querySelector("[data-testid^='concern-badge-']")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// M5 Option 0: honest return labels + P10/P50/P90 band
// ---------------------------------------------------------------------------

const KIND_HEADLINES: [
  NonNullable<DiscoverDossier["expected_return_anchor_kind"]>,
  RegExp,
][] = [
  ["trailing_3y_annualized_return", /trailing-return estimate/i],
  ["trailing_1m_annualized_return", /trailing-return estimate/i],
  ["momentum_12_1m", /momentum-based estimate/i],
  ["building_blocks_credibility", /building-block estimate/i],
  ["bl_posterior", /model estimate \(black-litterman\)/i],
];

describe("DossierDrawer honest return headline (M5 opt0)", () => {
  it.each(KIND_HEADLINES)(
    "anchor kind %s renders its mapped label without 'expected'/'forecast'",
    (kind, labelRe) => {
      render(
        <DossierDrawer
          open
          onClose={() => {}}
          dossier={dossierFixture({ expected_return_anchor_kind: kind })}
          symbol="TLT"
        />,
      );
      const heading = screen.getByText(labelRe);
      expect(heading.textContent).toMatch(labelRe);
      expect(heading.textContent).toMatch(/mo horizon/);
      expect(heading.textContent).not.toMatch(/expected|forecast/i);
    },
  );

  it("falls back to 'Return estimate' for unknown/absent anchor kinds", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({ expected_return_anchor_kind: null })}
        symbol="TLT"
      />,
    );
    const heading = screen.getByText(/return estimate/i);
    expect(heading.textContent).toMatch(/6mo horizon/);
    expect(heading.textContent).not.toMatch(/expected|forecast/i);
  });
});

describe("DossierDrawer P10/P50/P90 band (M5 opt0)", () => {
  const band = {
    p10: -0.08,
    p50: 0.03,
    p90: 0.14,
    method: "historical_simulation",
    n_paths: 2000,
    estimate: true,
    not_tax_advice: true,
  };

  it("renders the ordered P10/P50/P90 range under the headline value", () => {
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
    expect(text).toMatch(/P10\s*-8,0\s*%\s*·\s*P50\s*3,0\s*%\s*·\s*P90\s*14,0\s*%/);
    expect(text.indexOf("P10")).toBeLessThan(text.indexOf("P50"));
    expect(text.indexOf("P50")).toBeLessThan(text.indexOf("P90"));
    expect(text).not.toMatch(/widened/i);
  });

  it("appends the widened caption for gbm_fallback bands", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture({
          expected_return_band: { ...band, method: "gbm_fallback" },
        })}
        symbol="TLT"
      />,
    );
    expect(screen.getByText(/widened — short history/)).toBeInTheDocument();
  });

  it("renders no range row when the band is absent", () => {
    render(
      <DossierDrawer
        open
        onClose={() => {}}
        dossier={dossierFixture()}
        symbol="TLT"
      />,
    );
    expect(screen.queryByTestId("return-band")).toBeNull();
  });
});

describe("ShortlistCards honest return chip (M5 opt0)", () => {
  it("labels the chip 'Ret. est.' instead of 'E[Return]'", () => {
    render(
      <ShortlistCards
        candidates={[candidateFixture({ expected_return: 4.2 })]}
        onOpenDossier={() => {}}
      />,
    );
    expect(screen.getByText(/Ret\. est\./)).toBeInTheDocument();
    expect(screen.queryByText(/E\[Return\]/)).toBeNull();
  });
});

describe("RejectedTable flags column", () => {
  function expandTable() {
    fireEvent.click(screen.getByRole("button", { name: /rejected \(1\)/i }));
  }

  it("renders stage concerns for a verification-gate-rejected candidate", () => {
    // Review-blocker regression: rejected candidates used to persist
    // scores.concerns == [] (aggregation was gated on reject_stage), leaving
    // this column permanently "—" for exactly the most-informative rows.
    render(
      <RejectedTable
        candidates={[
          candidateFixture({
            status: "rejected",
            reject_stage: "verification_gate",
            reject_reason: "max drawdown 45% exceeds the 30% limit",
            scores: { concerns: ["max drawdown exceeds risk limit"] },
          }),
        ]}
      />,
    );
    expandTable();
    expect(
      screen.getByTestId("concern-badge-max_drawdown_exceeds_risk_limit"),
    ).toBeInTheDocument();
    expect(screen.getByText("verification_gate")).toBeInTheDocument();
  });

  it("renders an em-dash when a rejected candidate has no concerns", () => {
    render(
      <RejectedTable
        candidates={[candidateFixture({ status: "rejected", reject_stage: "shortlist_limit" })]}
      />,
    );
    expandTable();
    const stageBadge = screen.getByText("shortlist_limit");
    const flagsCell = stageBadge.closest("tr")?.querySelectorAll("td")[2];
    expect(flagsCell?.textContent).toContain("—");
  });
});
