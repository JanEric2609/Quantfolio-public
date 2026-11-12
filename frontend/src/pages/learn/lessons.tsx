import { useMemo, useState } from "react";
import { formatPercentPoints } from "../../lib/format";
import { useBookPerformance } from "../quantlab/hooks/useBookPerformance";
import { Guess, type StationProps } from "./stations";
import { Lesson } from "./Lesson";
import { annualisedReturn, annualisedVolatility, arithmeticAnnual, dailyReturns, ols } from "./lessonMath";

const pct = (v: number, digits = 1) => formatPercentPoints(v * 100, { digits });

/** The reader's own series: daily returns of the real book, and of the benchmark on the same days. */
function useOwnSeries() {
  const perf = useBookPerformance();
  return useMemo(() => {
    const series = perf.data?.available ? perf.data.series : [];
    const book: number[] = [];
    const bench: number[] = [];
    for (let i = 1; i < series.length; i++) {
      const a = series[i - 1];
      const b = series[i];
      if (a.unit_value > 0 && a.benchmark && b.benchmark) {
        book.push(b.unit_value / a.unit_value - 1);
        bench.push(b.benchmark / a.benchmark - 1);
      }
    }
    return { book, bench, loading: perf.isLoading, days: book.length };
  }, [perf.data, perf.isLoading]);
}

function NoData({ loading, days, need }: { loading: boolean; days: number; need: number }) {
  return (
    <p className="text-sm text-text-muted" data-testid="lesson-empty">
      {loading ? "Loading your book." : `Your book has ${days} priced days; this lesson needs ${need}. Skipped for now.`}
    </p>
  );
}

/** 1. Returns and annualising. */
export function ReturnsLesson(p: StationProps) {
  const { book, loading, days } = useOwnSeries();
  const [dropBest, setDropBest] = useState(0);
  const kept = useMemo(() => [...book].sort((a, b) => a - b).slice(0, Math.max(0, book.length - dropBest)), [book, dropBest]);
  const guess = p.state.answers["lesson-returns"];
  return (
    <Lesson
      id="returns" title="1. Returns and annualising"
      misconception="average daily return times 252 is your yearly return."
      idea="Returns multiply, they do not add: +10 % then -10 % leaves you 1 % down. A yearly figure is the daily growth rate compounded for 252 trading days."
      formula="annualised = (Π (1 + rₜ))^(252 / N) − 1"
      symbols={[["rₜ", "return on day t"], ["N", "number of days in the sample"], ["252", "trading days in a year"], ["Π", "the product of all the terms"]]}
      sourceName="annualised_return"
      quiz={{
        question: "Two years: +50 % then -50 %. What happened to your money?",
        options: ["Nothing, the average is 0 %", "You lost 25 %", "You gained 25 %"],
        correct: 1,
        explanation: "1.5 × 0.5 = 0.75. The average of the returns is 0 %, the compounded result is -25 %.",
      }}
    >
      {days < 20 ? <NoData loading={loading} days={days} need={20} /> : (
        <>
          <Guess id="lesson-returns" {...p} min={-20} max={40} step={1} unit=" % a year" label="Guess your book's annualised return" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>Compounded: <strong>{pct(annualisedReturn(book))}</strong> a year. Average-times-252 shortcut: {pct(arithmeticAnnual(book))}. You guessed {guess} %.</p>
              <label className="block text-xs text-text-secondary">
                Remove your {dropBest} best days
                <input type="range" min={0} max={Math.min(10, Math.floor(days / 4))} value={dropBest} onChange={(e) => setDropBest(Number(e.target.value))}
                  className="mt-1 w-full accent-[rgb(var(--c-accent))]" aria-label="Best days removed" />
              </label>
              <p>Without them: <strong>{pct(annualisedReturn(kept))}</strong> a year. A few days carry much of any record.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 2. Variance and the square root of 252. */
export function VolatilityLesson(p: StationProps) {
  const { book, loading, days } = useOwnSeries();
  const [span, setSpan] = useState(5);
  const guess = p.state.answers["lesson-vol"];
  const spanned = useMemo(() => {
    const out: number[] = [];
    for (let i = 0; i + span <= book.length; i += span) out.push(book.slice(i, i + span).reduce((a, r) => a * (1 + r), 1) - 1);
    return out;
  }, [book, span]);
  const daily = annualisedVolatility(book, 1);
  return (
    <Lesson
      id="vol" title="2. Variance and the square root of 252"
      misconception="yearly volatility is daily volatility times 252."
      idea="Independent days add up their variances, not their sizes. So the spread of k days is the daily spread times √k, and a year (252 days) is √252 ≈ 15.9 times the daily spread."
      formula="σ_year = σ_day × √252"
      symbols={[["σ_day", "sample standard deviation of daily returns (N − 1 in the denominator)"], ["√252", "square root of the trading days in a year"]]}
      sourceName="annualised_volatility"
      quiz={{
        question: "Daily volatility is 1 %. Roughly what is the yearly volatility?",
        options: ["252 %", "About 16 %", "About 4 %"],
        correct: 1,
        explanation: "1 % × √252 ≈ 15.9 %. Multiplying by 252 would assume every day moves the same way.",
      }}
    >
      {days < 30 ? <NoData loading={loading} days={days} need={30} /> : (
        <>
          <Guess id="lesson-vol" {...p} min={0} max={60} step={1} unit=" % a year" label="Guess your book's yearly volatility" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>Daily spread {pct(daily)}; × √252 gives <strong>{pct(annualisedVolatility(book))}</strong> a year. You guessed {guess} %.</p>
              <label className="block text-xs text-text-secondary">
                Look at {span}-day blocks instead
                <input type="range" min={1} max={21} value={span} onChange={(e) => setSpan(Number(e.target.value))}
                  className="mt-1 w-full accent-[rgb(var(--c-accent))]" aria-label="Block length in days" />
              </label>
              <p>
                Measured on {span}-day blocks: {pct(annualisedVolatility(spanned, 252 / span))}. The √ rule predicts {pct(annualisedVolatility(book))}.
                They agree when days are roughly independent; a gap shows days that follow each other.
              </p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 3. OLS beta, alpha and R². */
export function BetaLesson(p: StationProps) {
  const { book, bench, loading, days } = useOwnSeries();
  const [window, setWindow] = useState(0);
  const n = window === 0 ? days : Math.min(window, days);
  const fit = useMemo(() => ols(book.slice(-n), bench.slice(-n)), [book, bench, n]);
  const guess = p.state.answers["lesson-beta"];
  return (
    <Lesson
      id="beta" title="3. Beta, alpha and R²"
      misconception="a positive alpha number means skill."
      idea="Regress your daily return on the benchmark's: the slope is how much of the market's move you took (beta), the intercept is what is left over (alpha). R² says how much of your ups and downs the market explains."
      formula="rₚ = α + β · r_b + ε"
      symbols={[["rₚ", "your daily return"], ["r_b", "the benchmark's daily return"], ["β", "slope: sensitivity to the benchmark"], ["α", "intercept: return not explained by the benchmark, per day"], ["ε", "the part neither explains"]]}
      sourceName="beta"
      quiz={{
        question: "Your alpha is +2 % a year with a standard error of 3 %. What can you say?",
        options: ["You beat the market", "It is well within noise", "The market is wrong"],
        correct: 1,
        explanation: "An estimate smaller than its own standard error cannot be told from zero. Alpha needs years of data to stand out.",
      }}
    >
      {days < 30 ? <NoData loading={loading} days={days} need={30} /> : (
        <>
          <Guess id="lesson-beta" {...p} min={0} max={2} step={0.1} unit=" beta" label="Guess your book's beta to the benchmark" />
          {guess != null && fit ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>
                β = <strong>{fit.beta.toFixed(2)}</strong> (± {fit.seBeta.toFixed(2)}), R² = {fit.r2.toFixed(2)}, α = {pct(fit.alpha * 252)} a year
                over {fit.n} days. You guessed {guess}.
              </p>
              <label className="block text-xs text-text-secondary">
                Use the last {window === 0 ? "all" : window} days
                <input type="range" min={0} max={Math.min(days, 500)} step={10} value={window} onChange={(e) => setWindow(Number(e.target.value))}
                  className="mt-1 w-full accent-[rgb(var(--c-accent))]" aria-label="Window length in days" />
              </label>
              <p className="text-xs text-text-secondary">Shorten the window and watch alpha jump around while beta barely moves: beta is well measured, alpha is not.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}
