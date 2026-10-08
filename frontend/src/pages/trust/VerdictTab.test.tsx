import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { TrustFamilyTest, TrustRanking, TrustState, TrustTypeVerdict, TrustVerdict } from "../../lib/api";
import { VerdictTab } from "./VerdictTab";

const apiMock = vi.fn();
vi.mock("../../lib/api", () => ({ api: (...args: unknown[]) => apiMock(...args) }));

// jsdom has no canvas; render the chart shell without echarts.
vi.mock("../../components/charts/EChart", () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />,
}));

function makeType(over: Partial<TrustTypeVerdict> & Pick<TrustTypeVerdict, "type" | "label">): TrustTypeVerdict {
  return {
    n: 0,
    n_needed: 617,
    n_issue_days: 0,
    hits: 0,
    hit_rate: null,
    hit_ci: null,
    mean_excess: null,
    mean_excess_ci: null,
    e_skill: 1,
    e_harm: 1,
    e_skill_crossed_strong: false,
    state: "too_early",
    benchmarked: true,
    benchmark_label: "your passive core ETF (EUNL.DE)",
    brier: null,
    bss: null,
    bss_ci: null,
    n_stated_p: 0,
    spiegelhalter_z: null,
    reliability: [],
    range_coverage: null,
    calibration_caption: null,
    next_resolution_at: null,
    note: null,
    ...over,
  };
}

function makeFamily(family: TrustFamilyTest["family"], over: Partial<TrustFamilyTest> = {}): TrustFamilyTest {
  const series = family === "F1" ? "ideas" : family === "F2" ? "ranking" : "advisor";
  return {
    family,
    series,
    role: family === "F3" ? "secondary" : "primary",
    question: `Question ${family}?`,
    start: "2026-10-12",
    n_days: 0,
    first_day: null,
    last_day: null,
    mean_21d: null,
    mean_ci_21d: null,
    e_skill: 1,
    e_harm: 1,
    threshold: 40,
    n_tests: 2,
    state: "too_early",
    pre_registration: { n_days: 0, mean_21d: null },
    path: [],
    posterior: null,
    time_to_know: {
      assumption_unit: family === "F2" ? "rank_ic" : "excess_per_21d",
      assumption: family === "F2" ? 0.07 : 0.01,
      ic_sd: family === "F2" ? 0.12 : null,
      basket_sd_21d: family === "F2" ? null : 0.05,
      sd_daily: 0.0109,
      sd_measured: false,
      q10_days: 1498,
      q50_days: 4692,
      q90_days: 10677,
      q10_years: 5.9,
      q50_years: 18.6,
      q90_years: 42.4,
      max_days: 15000,
      p_within: [
        { years: 5, p: 0.08 },
        { years: 10, p: 0.21 },
        { years: 20, p: 0.54 },
      ],
      grid: [{ assumption: 0.01, q50_days: 4692, q50_years: 18.6 }],
    },
    ...over,
  };
}

function makeTests(f1: Partial<TrustFamilyTest> = {}) {
  return {
    start: "2026-10-12",
    clip: 0.05,
    sigma_ref: 0.0109,
    prior_var: 0.0121,
    bet_cap: 0.75,
    fdr_level: 0.05,
    threshold: 40,
    min_days_for_state: 63,
    horizon_days: 21,
    families: [makeFamily("F1", f1), makeFamily("F2"), makeFamily("F3")],
  };
}

const emptySection = {
  n_runs: 0,
  n_cohort_runs: 0,
  n_snapshots: 0,
  n_outcomes: 0,
  outcome_status: {},
  first_issue: null,
  last_issue: null,
  ic: { n_runs: 0, mean: null, t_nw: null, icir: null, share_positive: null },
  ic_decay: [],
  sector_neutral_ic: { n_runs: 0, mean: null, t_nw: null, icir: null, share_positive: null },
  quintiles: [],
  picked_vs_rest: { n_runs: 0, mean_gap: null, ci: null },
  gate_check: [],
  tiers: null,
  etf_ic: { n_runs: 0, mean: null, t_nw: null, icir: null, share_positive: null },
};

const RANKING: TrustRanking = {
  horizon_days: 21,
  min_stocks: 30,
  ic_hac_lag: 5,
  live: emptySection,
  exploratory: null,
  verdict_note: "Descriptive only.",
};

function makeVerdict(over: Partial<TrustVerdict> = {}): TrustVerdict {
  return {
    headline: "Too early to tell: no calls have resolved yet.",
    resolved_calls: 0,
    skill: null,
    state: "too_early",
    frozen_at_issue: true,
    method_note: "A call is a hit when it beat your passive core ETF.",
    min_units_for_interval: 8,
    min_calls_for_interval: 15,
    min_calls_for_reliability: 30,
    min_calls_for_state: 20,
    ebh_threshold: 40,
    daily_tests: makeTests(),
    types: [
      makeType({ type: "ideas", label: "Ideas (Discover)" }),
      makeType({ type: "advisor", label: "Advisor" }),
      makeType({ type: "mandates", label: "Mandates", benchmarked: false, benchmark_label: "a coin flip" }),
      makeType({ type: "regime", label: "Regime", n_needed: null, benchmarked: false, note: "3 daily regime calls recorded since 2026-09-01." }),
    ],
    ...over,
  };
}

function routeApi(verdict: TrustVerdict | Error) {
  apiMock.mockImplementation((url: string) => {
    if (url === "/api/trust/ranking") return Promise.resolve(RANKING);
    if (url === "/api/trust/history") return Promise.reject(new Error("no history in this test"));
    return verdict instanceof Error ? Promise.reject(verdict) : Promise.resolve(verdict);
  });
}

function renderTab(verdict: TrustVerdict | Error) {
  apiMock.mockReset();
  routeApi(verdict);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <VerdictTab />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function chipFor(typeKey: string): HTMLElement {
  const chip = within(screen.getByTestId(`type-${typeKey}`))
    .getAllByTitle(/./)
    .find((el) => el.hasAttribute("data-state"));
  if (!chip) throw new Error(`no evidence chip in ${typeKey}`);
  return chip;
}

describe("VerdictTab", () => {
  beforeEach(() => {
    apiMock.mockReset();
  });

  it("asks the trust verdict endpoint", async () => {
    renderTab(makeVerdict());
    await screen.findByTestId("trust-headline");
    expect(apiMock).toHaveBeenCalledWith("/api/trust/verdict");
  });

  it("shows thresholds from the API, the lower-bound marker and the unscored count", async () => {
    renderTab(
      makeVerdict({
        resolved_units: 5,
        n_needed: 15000,
        n_needed_is_lower_bound: true,
        n_unscored: 3,
        n_tests: 4,
        cadence_days: 5,
        h_eff: 5,
        years_needed: 21,
        assumed_edge: 0.01,
        assumed_sd: 0.05,
        excess_clip: 0.2,
        horizon_days: 21,
        min_calls_for_reliability: 45,
        types: [
          makeType({ type: "ideas", label: "Ideas (Discover)", n: 5, cadence_days: 5, years_needed: 21, hits: 3, hit_rate: 0.6, n_tests: 4, n_unscored: 3, n_needed: 15000, n_needed_is_lower_bound: true }),
          ...makeVerdict().types.slice(1),
        ],
      }),
    );
    expect(await screen.findByTestId("trust-units")).toHaveTextContent("0");
    expect(screen.getByTestId("type-ideas")).toHaveTextContent("3 resolved calls could not be scored: no benchmark price");
    expect(screen.getByTestId("type-ideas")).toHaveTextContent("which needs 40");
    expect(screen.getByText(/appears once 45 calls with a stated probability/)).toBeInTheDocument();
    expect(screen.getByText(/one verdict needs 40, which keeps false discoveries at 5 %/)).toBeInTheDocument();
    expect(screen.getByText(/plus or minus 5 % a day/)).toBeInTheDocument();
    // The time to know is a range, from the simulation.
    expect(screen.getAllByText(/about 19 years \(10–90 %: 5\.9 to 42\)/).length).toBeGreaterThan(0);
    expect(screen.getByTestId("family-F1")).toHaveTextContent("Primary");
    expect(screen.getByTestId("family-F3")).toHaveTextContent("Secondary");
  });

  describe("too early", () => {
    it("shows the headline, grey chips with N of needed, and what to wait for", async () => {
      const due = "2026-10-06T05:00:00+00:00";
      const verdict = makeVerdict({
        headline: "Too early to tell: 12 of ~600 independent rebalance dates needed to tell a 55 % hit rate from a coin flip.",
        resolved_calls: 30,
        resolved_units: 12,
        n_needed: 617,
        skill: { metric: "mean_excess_return", value: 0.012, ci_low: null, ci_high: null, n: 12, benchmark_label: "your passive core ETF (EUNL.DE)" },
        types: [
          makeType({
            type: "ideas",
            label: "Ideas (Discover)",
            n: 12,
            hits: 9,
            hit_rate: 0.75,
            hit_ci: [0.46, 0.93],
            n_issue_days: 12,
            n_calls: 30,
            next_resolution_at: due,
          }),
          ...makeVerdict().types.slice(1),
        ],
      });
      renderTab(verdict);

      expect(await screen.findByTestId("trust-headline")).toHaveTextContent("Too early to tell: 12 of ~600");
      expect(screen.getByText("Frozen at issue time", { selector: "div" })).toBeInTheDocument();
      // Three numbers: trading days in the pre-registered test, the picks vs the ETF, the state.
      expect(screen.getByText("Trading days in the test")).toBeInTheDocument();
      expect(screen.getByTestId("trust-units")).toHaveTextContent("0");
      expect(screen.getByText("no trading day with open picks recorded yet")).toBeInTheDocument();
      // Chip per type, grey, with N of needed.
      const ideasChip = chipFor("ideas");
      expect(ideasChip).toHaveAttribute("data-state", "too_early");
      expect(ideasChip).toHaveTextContent("Too early (12 of ~600)");
      expect(chipFor("regime")).toHaveTextContent("Not scored yet");
      // Hit rate with N and its interval, never bare.
      const ideas = screen.getByTestId("type-ideas");
      expect(ideas).toHaveTextContent("75 % (9 of 12)");
      expect(ideas).toHaveTextContent("90 % interval 46 % to 93 %");
      expect(ideas).toHaveTextContent("30 calls");
      // The reliability plot waits for 30 calls.
      expect(screen.getByText(/appears once 30 calls with a stated probability/)).toBeInTheDocument();
      expect(screen.queryByTestId("reliability-ideas")).not.toBeInTheDocument();
    });

    it("an empty ledger explains when the first resolutions are due instead of rendering blank", async () => {
      const verdict = makeVerdict({
        types: [
          makeType({ type: "ideas", label: "Ideas (Discover)", next_resolution_at: "2026-10-06T05:00:00+00:00" }),
          ...makeVerdict().types.slice(1),
        ],
      });
      renderTab(verdict);

      const status = await screen.findByRole("status");
      expect(status).toHaveTextContent(/First resolutions due/);
      expect(status).toHaveTextContent(/2026/);
      expect(screen.getAllByText(/No resolved calls yet/).length).toBeGreaterThan(0);
    });

    it("an empty ledger with nothing pending says how to start the record", async () => {
      renderTab(makeVerdict());
      const status = await screen.findByRole("status");
      expect(status).toHaveTextContent(/nothing is waiting/i);
      expect(status).toHaveTextContent(/Run Discover/);
    });
  });

  describe("evidence of skill", () => {
    it("shows a green skill chip, the interval around the skill and the 100 threshold", async () => {
      const verdict = makeVerdict({
        headline: "Evidence of skill vs your ETF over 200 calls.",
        resolved_calls: 200,
        state: "skill",
        skill: { metric: "mean_excess_return", value: 0.0089, ci_low: 0.0021, ci_high: 0.0158, n: 200, benchmark_label: "your passive core ETF (EUNL.DE)" },
        daily_tests: makeTests({
          n_days: 400, first_day: "2026-10-13", state: "skill", e_skill: 52, mean_21d: 0.0089, mean_ci_21d: [0.0021, 0.0158],
          posterior: {
            tau_21d: 0.005, n: 400, sample_mean_21d: 0.0089, sample_se_21d: 0.004, shrinkage: 0.6, mean_21d: 0.005,
            ci_low_21d: 0.001, ci_high_21d: 0.009, p_positive: 0.97,
            sensitivity: [{ tau_21d: 0.0025, p_positive: 0.93, mean_21d: 0.003 }],
          },
          path: [
            { day: "2026-10-13", e_skill: 1, e_harm: 1 },
            { day: "2028-04-01", e_skill: 52, e_harm: 0.1 },
          ],
        }),
        types: [
          makeType({
            type: "ideas",
            label: "Ideas (Discover)",
            n: 200,
            hits: 140,
            hit_rate: 0.7,
            hit_ci: [0.64, 0.76],
            mean_excess: 0.0089,
            mean_excess_ci: [0.0021, 0.0158],
            e_skill: 4200,
            e_skill_crossed_strong: true,
            state: "skill",
            n_issue_days: 120,
          }),
          ...makeVerdict().types.slice(1),
        ],
      });
      renderTab(verdict);

      expect(await screen.findByTestId("trust-headline")).toHaveTextContent("Evidence of skill vs your ETF over 200 calls.");
      expect(screen.getAllByText("+0.89 pp").length).toBeGreaterThan(0);
      expect(screen.getByText(/90 % interval \+0\.21 pp to \+1\.58 pp · mean active/)).toBeInTheDocument();
      expect(screen.getByTestId("trust-units")).toHaveTextContent("400");
      // The posterior is shown, with its prior, and says it is not the verdict.
      const posterior = screen.getByTestId("posterior-F1");
      expect(posterior).toHaveTextContent("Chance the edge is positive: 97 %");
      expect(posterior).toHaveTextContent("cannot declare skill");
      expect(screen.getByRole("img", { name: /picks vs your ETF: evidence over time/i })).toBeInTheDocument();
      const chip = chipFor("ideas");
      expect(chip).toHaveAttribute("data-state", "skill");
      expect(chip).toHaveTextContent("Evidence of skill");
      expect(chip.className).toMatch(/bg-success/);
      expect(screen.getByTestId("type-ideas")).toHaveTextContent("70 % (140 of 200)");
      expect(screen.getByTestId("type-ideas")).toHaveTextContent("90 % interval +0.2 pp to +1.6 pp");
      // The resolved-call count no longer shows the empty-ledger notice.
      expect(screen.queryByText(/First resolutions due/)).not.toBeInTheDocument();
    });
  });

  describe("evidence of harm", () => {
    it("shows a red chip and the harm headline", async () => {
      const verdict = makeVerdict({
        headline: "Evidence of harm: the calls have done worse than simply holding your ETF (200 calls).",
        resolved_calls: 200,
        state: "harm",
        skill: { metric: "mean_excess_return", value: -0.021, ci_low: -0.03, ci_high: -0.012, n: 200, benchmark_label: "your passive core ETF (EUNL.DE)" },
        daily_tests: makeTests({ n_days: 300, state: "harm", e_harm: 310, mean_21d: -0.021 }),
        types: [
          makeType({ type: "ideas", label: "Ideas (Discover)", n: 200, hits: 60, hit_rate: 0.3, hit_ci: [0.25, 0.36], e_harm: 310, state: "harm" }),
          ...makeVerdict().types.slice(1),
        ],
      });
      renderTab(verdict);

      expect(await screen.findByTestId("trust-headline")).toHaveTextContent(/^Evidence of harm/);
      const chip = chipFor("ideas");
      expect(chip).toHaveAttribute("data-state", "harm");
      expect(chip).toHaveTextContent("Evidence of harm vs your ETF");
      expect(chip.className).toMatch(/bg-danger/);
      expect(screen.getAllByText("−2.10 pp").length).toBeGreaterThan(0);
      expect(within(screen.getByTestId("family-F1")).getAllByTitle(/./).find((el) => el.hasAttribute("data-state"))).toHaveAttribute(
        "data-state",
        "harm",
      );
    });
  });

  it("amber for no evidence yet", async () => {
    renderTab(
      makeVerdict({
        headline: "No evidence yet that the calls beat your ETF: 60 resolved, about 153 needed to tell a 60 % hit rate from a coin flip.",
        resolved_calls: 60,
        state: "no_evidence",
        types: [
          makeType({ type: "ideas", label: "Ideas (Discover)", n: 60, hits: 30, hit_rate: 0.5, hit_ci: [0.39, 0.61], state: "no_evidence" }),
          ...makeVerdict().types.slice(1),
        ],
      }),
    );
    await screen.findByTestId("trust-headline");
    const chip = chipFor("ideas");
    expect(chip).toHaveAttribute("data-state", "no_evidence");
    expect(chip).toHaveTextContent("No evidence yet");
    expect(chip.className).toMatch(/bg-warn/);
  });

  describe("calibration", () => {
    it("draws the reliability plot and the caption only when the backend sends bins", async () => {
      const bins = [
        { p_mean: 0.3, hit_rate: 0.3, n: 20 },
        { p_mean: 0.7, hit_rate: 0.6, n: 20 },
      ];
      renderTab(
        makeVerdict({
          resolved_calls: 40,
          types: [
            makeType({
              type: "ideas",
              label: "Ideas (Discover)",
              n: 40,
              hits: 18,
              hit_rate: 0.45,
              n_stated_p: 40,
              brier: 0.231,
              bss: 0.05,
              bss_ci: [-0.04, 0.13],
              calibration_caption: "When we said 70 %, it happened 12 of 20 times.",
              reliability: bins,
              spiegelhalter_z: -1.2,
            }),
            ...makeVerdict().types.slice(1),
          ],
        }),
      );

      const plot = await screen.findByTestId("reliability-ideas");
      expect(plot).toHaveTextContent("When we said 70 %, it happened 12 of 20 times.");
      expect(within(plot).getByRole("table", { name: /reliability bins/i })).toBeInTheDocument();
      expect(plot).toHaveTextContent("Spiegelhalter Z -1.20");
      // The same caption and a Brier line with its interval sit in the type card.
      expect(screen.getByTestId("type-ideas")).toHaveTextContent("Brier 0.231 on 40 calls with a stated probability");
      expect(screen.getByTestId("type-ideas")).toHaveTextContent("90 % interval -0.04 to 0.13");
    });
  });

  it("shows the range coverage with its exact interval", async () => {
    renderTab(
      makeVerdict({
        types: [
          makeType({
            type: "ideas",
            label: "Ideas (Discover)",
            n: 20,
            hits: 12,
            hit_rate: 0.6,
            range_coverage: { k: 16, n: 20, rate: 0.8, ci_low: 0.62, ci_high: 0.92, nominal: 0.8 },
          }),
          ...makeVerdict().types.slice(1),
        ],
      }),
    );
    expect(await screen.findByTestId("type-ideas")).toHaveTextContent(
      "Stated ranges (80 % nominal): the outcome landed inside 16 of 20 times (90 % interval 62 % to 92 %)",
    );
  });

  it("shows the conformal range correction once active, and when it starts before that", async () => {
    const aci = {
      active: true,
      matured_weeks: 40,
      weeks_needed: 30,
      alpha_target: 0.2,
      alpha_now: 0.18,
      gamma: 0.005,
      widen_now: 0.25,
      per_date: [],
      corrected: { k: 40, n: 50, rate: 0.8, ci_low: 0.68, ci_high: 0.89, nominal: 0.8 },
      raw_same_dates: { k: 30, n: 50, rate: 0.6, ci_low: 0.47, ci_high: 0.72, nominal: 0.8 },
    };
    const cov = { k: 30, n: 50, rate: 0.6, ci_low: 0.47, ci_high: 0.72, nominal: 0.8 };
    renderTab(
      makeVerdict({
        types: [
          makeType({ type: "ideas", label: "Ideas (Discover)", n: 50, range_coverage: { ...cov, aci } }),
          makeType({
            type: "advisor",
            label: "Advisor",
            n: 5,
            range_coverage: { ...cov, aci: { ...aci, active: false, matured_weeks: 4, widen_now: null, corrected: null, raw_same_dates: null } },
          }),
          ...makeVerdict().types.slice(2),
        ],
      }),
    );
    expect(await screen.findByTestId("aci-active")).toHaveTextContent(
      "the next range would widen each range by 25 % of its width on each side. Since it started: corrected ranges held 40 of 50, the stated ones 30.",
    );
    expect(screen.getByTestId("aci-waiting")).toHaveTextContent("starts after 30 matured weeks; 4 so far");
  });

  it("shows the factor study and the advisor-vs-picks comparison as descriptive cards", async () => {
    renderTab(
      makeVerdict({
        daily_tests: {
          ...makeTests(),
          paired: {
            question: "Did the advisor's paper calls beat Discover's picks on the days both were open?",
            start: "2026-10-12",
            n_days: 70,
            n_days_pre_registration: 0,
            n_days_advisor_only: 3,
            n_days_ideas_only: 1,
            mean_21d: -0.004,
            mean_ci_21d: [-0.02, 0.012],
            share_advisor_ahead: 0.47,
            enough_days: true,
            path: [],
          },
          factor_neutral: {
            study: { status: "waiting", spec_version: 1, n_backfilled_picks: 12 },
            row: null,
            gate: { build: 0.3, drop: 0.15, min_oos_days: 126, min_fit_days: 126 },
          },
        },
      }),
    );
    expect(await screen.findByTestId("paired-comparison")).toHaveTextContent(
      "On 70 common trading days the advisor was −0.40 pp per 21 days against the picks",
    );
    expect(screen.getByTestId("paired-comparison")).toHaveTextContent("ahead on 47 % of days");
    expect(screen.getByTestId("factor-neutral")).toHaveTextContent(
      "needs 252 trading days of back-filled baskets (12 back-filled picks so far)",
    );
  });

  it("shows a glossary and the method note at the bottom", async () => {
    renderTab(makeVerdict());
    const glossary = await screen.findByRole("heading", { name: "Glossary" });
    expect(glossary).toBeInTheDocument();
    expect(screen.getByText("Frozen at issue time", { selector: "dt" })).toBeInTheDocument();
    expect(screen.getByText("Pre-registered test")).toBeInTheDocument();
    expect(screen.getByText("Time to know")).toBeInTheDocument();
    expect(screen.getByText("A call is a hit when it beat your passive core ETF.")).toBeInTheDocument();
  });

  it("shows a loading state, then an error with retry", async () => {
    const user = userEvent.setup();
    renderTab(new Error("boom"));
    expect(screen.getByLabelText("Loading the verdict")).toBeInTheDocument();

    expect(await screen.findByText("Couldn't load the verdict")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();

    routeApi(makeVerdict());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("trust-headline")).toBeInTheDocument();
  });
});

describe("state vocabulary", () => {
  const states: TrustState[] = ["too_early", "skill", "no_evidence", "harm"];
  it("every state has a chip with words, not colour alone", async () => {
    for (const state of states) {
      const { unmount } = renderTab(
        makeVerdict({
          types: [makeType({ type: "ideas", label: "Ideas (Discover)", state }), ...makeVerdict().types.slice(1)],
        }),
      );
      await screen.findByTestId("type-ideas");
      expect(chipFor("ideas").textContent?.trim().length).toBeGreaterThan(5);
      unmount();
    }
  });
});
