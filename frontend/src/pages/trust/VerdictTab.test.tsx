import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { TrustState, TrustTypeVerdict, TrustVerdict } from "../../lib/api";
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

function makeVerdict(over: Partial<TrustVerdict> = {}): TrustVerdict {
  return {
    headline: "Too early to tell: no calls have resolved yet.",
    resolved_calls: 0,
    skill: null,
    state: "too_early",
    frozen_at_issue: true,
    method_note: "A call is a hit when it beat your passive core ETF.",
    types: [
      makeType({ type: "ideas", label: "Ideas (Discover)" }),
      makeType({ type: "advisor", label: "Advisor" }),
      makeType({ type: "mandates", label: "Mandates", benchmarked: false, benchmark_label: "a coin flip" }),
      makeType({ type: "regime", label: "Regime", n_needed: null, benchmarked: false, note: "3 daily regime calls recorded since 2026-09-01." }),
    ],
    ...over,
  };
}

function renderTab(verdict: TrustVerdict | Error) {
  apiMock.mockReset();
  if (verdict instanceof Error) apiMock.mockRejectedValue(verdict);
  else apiMock.mockResolvedValue(verdict);
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
      // Three numbers: the sample, the skill with its missing interval, the state.
      expect(screen.getByText("Independent rebalance dates")).toBeInTheDocument();
      expect(screen.getByTestId("trust-units")).toHaveTextContent("12 of ~600");
      expect(screen.getByText(/interval needs 8 dates · 12 dates/)).toBeInTheDocument();
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
      expect(screen.getByText("+0.9 pp")).toBeInTheDocument();
      expect(screen.getByText(/90 % interval \+0\.2 pp to \+1\.6 pp \(Newey-West\) · 200 dates/)).toBeInTheDocument();
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
      expect(screen.getByText("−2.1 pp")).toBeInTheDocument();
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

  it("shows a glossary and the method note at the bottom", async () => {
    renderTab(makeVerdict());
    const glossary = await screen.findByRole("heading", { name: "Glossary" });
    expect(glossary).toBeInTheDocument();
    expect(screen.getByText("Frozen at issue time", { selector: "dt" })).toBeInTheDocument();
    expect(screen.getByText("Evidence (e-value)")).toBeInTheDocument();
    expect(screen.getByText("A call is a hit when it beat your passive core ETF.")).toBeInTheDocument();
  });

  it("shows a loading state, then an error with retry", async () => {
    const user = userEvent.setup();
    renderTab(new Error("boom"));
    expect(screen.getByLabelText("Loading the verdict")).toBeInTheDocument();

    expect(await screen.findByText("Couldn't load the verdict")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();

    apiMock.mockResolvedValue(makeVerdict());
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
