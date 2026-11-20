import type { TrustVerdict } from "../../lib/api";

function terms(v?: TrustVerdict): { term: string; meaning: string }[] {
  const tests = v?.daily_tests;
  const threshold = tests?.threshold ?? v?.ebh_threshold;
  const clip = tests?.clip != null ? `${Math.round(tests.clip * 100)} %` : "a fixed bound";
  return [
  {
    term: "Call",
    meaning:
      "One prediction the system made and wrote down before the outcome was known: a Discover pick, a daily advisor trade, a weekly mandate expectation, a daily regime label.",
  },
  {
    term: "Pre-registered test",
    meaning:
      `The test, its settings and its bar were fixed in advance (ADR 0018), before the results they judge${tests ? `; days count from ${tests.start}` : ""}. Changing them later would be a look at the data.`,
  },
  {
    term: "Trading day (the unit)",
    meaning:
      "Each trading day the open calls of a type form one portfolio held as issued, and its return against your passive core ETF that day is one observation. Overlapping holding windows are therefore not a problem, and a pick that stops trading turns into cash rather than disappearing.",
  },
  {
    term: "Frozen at issue time",
    meaning:
      "Calls are stored when they are made and each day is recorded once; nothing is rewritten afterwards, so the record cannot drift to look better.",
  },
  {
    term: "Evidence (e-value)",
    meaning:
      `A running score of how hard the record is to explain by luck: it bets on each day's return against your ETF (capped at plus or minus ${clip} a day), valid however often you look. Skill and harm are both tested, so one verdict needs ${threshold ?? "a higher bar"}, which keeps false discoveries at 5 %. Mandates have no benchmark and are not tested.`,
  },
  {
    term: "Chance the edge is positive",
    meaning:
      "A Bayesian summary with a sceptical starting point (an edge of a fraction of a percent per month). It is shown next to the test and never decides anything: with no edge at all it passes 95 % at some point in roughly one history in ten.",
  },
  {
    term: "Time to know",
    meaning:
      "Simulated: how long the test would most likely take to say skill if the true edge were the stated value, with the 10 and 90 % range. Halving the edge roughly quadruples the time. If there is no edge, no verdict ever comes.",
  },
  {
    term: "Too early",
    meaning: `Fewer than ${tests?.min_days_for_state ?? "the minimum number of"} trading days and no evidence either way. This is an answer, not a gap.`,
  },
  {
    term: "90 % interval",
    meaning:
      "The range the true value plausibly sits in given only this much data. Newey-West for means of overlapping or autocorrelated series, exact for hit rates. Wide means we do not know yet.",
  },
  {
    term: "Rank IC",
    meaning:
      "How well a score sorted stocks by their later return: 1 is a perfect order, 0 is random. Measured over every stock Discover scored, not only the picks.",
  },
  {
    term: "Brier score and skill",
    meaning:
      "How close stated probabilities were to what happened (0 is perfect, 0.25 is always saying 50 %). Skill compares it with always quoting the base rate: above 0 is better. Its interval resamples whole blocks of issue dates.",
  },
  {
    term: "Calibration",
    meaning: `When we said 70 %, did it happen about 70 % of the time? Drawn from ${v?.min_calls_for_reliability ?? "enough"} calls with a stated probability; no probability is stated at all before ${v?.min_n_eff_for_probability ?? 100} issue dates.`,
  },
  {
    term: "Range coverage",
    meaning: "How often the outcome landed inside the range the system stated, against the share it claimed (for example 80 %).",
  },
  {
    term: "Range correction",
    meaning:
      "Adaptive conformal inference: after 30 matured weeks, each new range is widened (or narrowed) by what the misses of earlier, already-finished dates call for, so the ranges keep the share they claim even when markets change.",
  },
  {
    term: "Factor moves",
    meaning:
      "Moves of the market, a stock's sector, the dollar, Europe against the world and the style factors (value, momentum, quality, low volatility, size). A pick that only rode one of them shows no edge once they are removed.",
  },
  ];
}

/** Plain-language definitions of every term the page uses, at the bottom so the top stays readable. */
export function TrustGlossary({ methodNote, verdict }: { methodNote?: string; verdict?: TrustVerdict }) {
  return (
    <section aria-labelledby="trust-glossary" className="space-y-3">
      <h2 id="trust-glossary" className="text-sm font-semibold text-text-primary">
        Glossary
      </h2>
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {terms(verdict).map((t) => (
          <div key={t.term}>
            <dt className="text-xs font-semibold text-text-primary">{t.term}</dt>
            <dd className="text-xs text-text-secondary">{t.meaning}</dd>
          </div>
        ))}
      </dl>
      {methodNote ? <p className="text-[11px] text-text-muted">{methodNote}</p> : null}
    </section>
  );
}
