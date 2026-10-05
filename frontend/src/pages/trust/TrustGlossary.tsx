function terms(needed: number | null, target: number | null): { term: string; meaning: string }[] {
  const pct = Math.round((target ?? 0.55) * 100);
  return [
  {
    term: "Call",
    meaning:
      "One prediction the system made and wrote down before the outcome was known: a Discover pick, a daily advisor trade, a weekly mandate expectation, a daily regime label.",
  },
  {
    term: "Hit",
    meaning:
      "The equal-weight basket of one rebalance date beat your passive core ETF over its horizon (for mandates: the stated expectation held). Holding the ETF is always the alternative. A pick that stopped trading stays in, at its last close.",
  },
  {
    term: "Frozen at issue time",
    meaning:
      "Calls are stored when they are made and never rewritten, so the record cannot drift to look better afterwards.",
  },
  {
    term: "90 % interval",
    meaning:
      "The range the true value plausibly sits in given only this many dates. Exact (Clopper-Pearson) for hit rates, Newey-West for the mean excess because the holding windows of neighbouring dates overlap. Wide means we do not know yet.",
  },
  {
    term: "Evidence (e-value)",
    meaning:
      "A running score of how hard the record is to explain by luck, against a 50 % coin flip, valid however often you look. The page tests several types in two directions, so one score of 20 is not enough: the e-BH procedure raises the bar (to 80 for the strongest of four looks) and keeps false discoveries at 5 %.",
  },
  {
    term: "Too early",
    meaning: "Fewer than 20 rebalance dates and no evidence either way. This is an answer, not a gap.",
  },
  {
    term: "Calls needed",
    meaning:
      needed
        ? `Telling a ${pct} % hit rate from a coin flip takes about ${needed} independent rebalance dates. The picks of one day share one market move, so they count once.`
        : "The picks of one day share one market move, so they count once.",
  },
  {
    term: "Brier score and skill",
    meaning:
      "How close stated probabilities were to what happened (0 is perfect, 0.25 is always saying 50 %). Skill compares it with always quoting the base rate: above 0 is better.",
  },
  {
    term: "Calibration",
    meaning: "When we said 70 %, did it happen about 70 % of the time? Only drawn from 30 calls with a stated probability.",
  },
  {
    term: "Range coverage",
    meaning: "How often the outcome landed inside the range the system stated, against the share it claimed (for example 80 %).",
  },
  ];
}

/** Plain-language definitions of every term the page uses, at the bottom so the top stays readable. */
export function TrustGlossary({
  methodNote, needed = null, target = null,
}: { methodNote?: string; needed?: number | null; target?: number | null }) {
  return (
    <section aria-labelledby="trust-glossary" className="space-y-3">
      <h2 id="trust-glossary" className="text-sm font-semibold text-text-primary">
        Glossary
      </h2>
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {terms(needed, target).map((t) => (
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
