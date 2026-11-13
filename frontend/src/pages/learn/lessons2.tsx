import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { formatCurrency, formatNumber } from "../../lib/format";
import { Guess, type StationProps } from "./stations";
import { Lesson } from "./Lesson";
import { NoData, pct, useOwnSeries } from "./lessons";
import { bestOfK, bootstrapFan, eProcess, neweyWestT, sampleCov, sampleStd, shrinkToDiagonal, walkForward } from "./lessonMath";

const SLIDER = "mt-1 w-full accent-[rgb(var(--c-accent))]";

/** Your book's daily return minus the benchmark's, the series every test below looks at. */
function useExcess() {
  const s = useOwnSeries();
  const excess = useMemo(() => s.book.map((r, i) => r - s.bench[i]), [s.book, s.bench]);
  return { ...s, excess };
}

/** 4. Standard errors and the t-statistic. */
export function TStatLesson(p: StationProps) {
  const { excess, loading, days } = useExcess();
  const [window, setWindow] = useState(0);
  const x = window === 0 ? excess : excess.slice(-window);
  const m = x.length ? x.reduce((a, b) => a + b, 0) / x.length : 0;
  const se = x.length ? sampleStd(x) / Math.sqrt(x.length) : 0;
  const guess = p.state.answers["lesson-tstat"];
  return (
    <Lesson
      id="tstat" title="4. Standard errors and the t-statistic"
      misconception="a positive average means the effect is real."
      idea="An average of noisy numbers is itself noisy. The standard error says by how much; the t-statistic says how many standard errors the average sits from zero. Below about 2, zero is a fair explanation."
      formula="t = mean(x) / ( sd(x) / √N )"
      tex={String.raw`t = \frac{\bar{x}}{\operatorname{sd}(x) / \sqrt{N}}`}
      symbols={[["x", "daily excess return over the benchmark"], ["sd(x)", "sample standard deviation"], ["N", "number of days"], ["sd/√N", "the standard error of the mean"]]}
      sourceName="diebold_mariano_nonoverlapping"
      quiz={{
        question: "You have 4 times as many days. Roughly what happens to the standard error of the mean?",
        options: ["It halves", "It quarters", "It stays the same"],
        correct: 0,
        explanation: "The standard error falls with √N. Four times the data halves it, which is why a skill needs years, not months, to show.",
      }}
    >
      {days < 30 ? <NoData loading={loading} days={days} need={30} /> : (
        <>
          <Guess id="lesson-tstat" {...p} min={-4} max={4} step={0.5} unit=" t" label="Guess the t-statistic of your excess return over the benchmark" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>Mean {pct(m * 252)} a year over {x.length} days, standard error {pct(se * 252)}: t = <strong>{formatNumber(se ? m / se : 0, { digits: 2 })}</strong>. You guessed {guess}.</p>
              <label className="block text-xs text-text-secondary">
                Use the last {window === 0 ? "all" : window} days
                <input type="range" min={0} max={Math.min(days, 500)} step={10} value={window} onChange={(e) => setWindow(Number(e.target.value))} className={SLIDER} aria-label="Window length in days" />
              </label>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 5. Overlapping returns and Newey-West. */
export function NeweyWestLesson(p: StationProps) {
  const { excess, loading, days } = useExcess();
  const [lags, setLags] = useState(5);
  const guess = p.state.answers["lesson-nw"];
  return (
    <Lesson
      id="nw" title="5. Overlapping returns and Newey-West"
      misconception="a thousand overlapping observations are a thousand pieces of evidence."
      idea="A 21-day return measured every day shares 20 of its 21 days with the day before. The numbers move together, so you have far fewer independent observations than rows. Newey-West widens the standard error to account for it."
      formula="long-run variance = γ₀ + 2 Σₖ (1 − k/(L+1)) γₖ"
      tex={String.raw`\hat\sigma^2_{\text{long-run}} = \hat\gamma_0 + 2 \sum_{k=1}^{L} \Big(1 - \frac{k}{L+1}\Big)\hat\gamma_k`}
      symbols={[["γₖ", "covariance of the series with itself k days earlier"], ["L", "number of lags allowed"], ["1 − k/(L+1)", "Bartlett weight: far lags count less"]]}
      sourceName="newey_west_t_stat"
      quiz={{
        question: "Daily rows overlap by 20 of 21 days. The honest t-statistic, compared with the naive one, is usually…",
        options: ["The same", "Smaller", "Larger"],
        correct: 1,
        explanation: "Positive autocorrelation makes the naive standard error too small, so the naive t is too large. Newey-West pulls it back.",
      }}
    >
      {days < 30 ? <NoData loading={loading} days={days} need={30} /> : (
        <>
          <Guess id="lesson-nw" {...p} min={0} max={20} step={1} unit=" lags" label="How many lags would you allow for daily excess returns?" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>t with no correction: <strong>{formatNumber(neweyWestT(excess, 0), { digits: 2 })}</strong>. With {lags} lags: <strong>{formatNumber(neweyWestT(excess, lags), { digits: 2 })}</strong>. You chose {guess}.</p>
              <label className="block text-xs text-text-secondary">
                Lags {lags}
                <input type="range" min={0} max={30} value={lags} onChange={(e) => setLags(Number(e.target.value))} className={SLIDER} aria-label="Lags" />
              </label>
              <p className="text-xs text-text-secondary">Daily returns are close to independent, so little changes here. Run the same on a 21-day forward return and it changes a lot.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 6. Testing many things and t ≥ 3. Needs no data of yours: it is a simulation of nothing. */
export function MultipleTestingLesson(p: StationProps) {
  const [k, setK] = useState(50);
  const guess = p.state.answers["lesson-multi"];
  const best = useMemo(() => bestOfK(k, 250), [k]);
  return (
    <Lesson
      id="multi" title="6. Testing many things, and why t must reach 3"
      misconception="the best of many tries is a discovery."
      idea="Try 100 strategies that have no edge at all and a few will show t above 2 by luck. The more you try, the higher the best one looks. A bar of 3 and a count of how many things were tried keep luck from passing."
      formula="E[max of k noise t-stats] ≈ √(2 ln k)"
      tex={String.raw`\mathbb{E}\Big[\max_{i \le k} t_i\Big] \approx \sqrt{2 \ln k}`}
      symbols={[["k", "number of strategies tried"], ["t", "t-statistic of one strategy's mean return"], ["ln k", "natural logarithm of k"]]}
      sourceName="deflated_sharpe_ratio"
      quiz={{
        question: "You try 200 strategies with zero true edge. The best one has t = 2.5. Is it skill?",
        options: ["Yes, t above 2", "No, the best of 200 noise strategies usually reaches that", "Cannot tell without a chart"],
        correct: 1,
        explanation: "√(2 ln 200) ≈ 3.3. A best-of-200 of pure noise often tops 2.5, which is why the gate counts trials.",
      }}
    >
      <Guess id="lesson-multi" {...p} min={0} max={5} step={0.5} unit=" t" label="Best t among 50 strategies that all have zero edge?" />
      {guess != null ? (
        <div className="space-y-2 text-sm text-text-primary" role="status">
          <p>Simulated: best of {k} no-edge strategies over 250 days has t = <strong>{formatNumber(best, { digits: 2 })}</strong>. Rule of thumb {formatNumber(Math.sqrt(2 * Math.log(Math.max(k, 2))), { digits: 2 })}. You guessed {guess}.</p>
          <label className="block text-xs text-text-secondary">
            Strategies tried: {k}
            <input type="range" min={1} max={500} value={k} onChange={(e) => setK(Number(e.target.value))} className={SLIDER} aria-label="Strategies tried" />
          </label>
        </div>
      ) : null}
    </Lesson>
  );
}

/** 7. Out-of-sample and walk-forward. */
export function WalkForwardLesson(p: StationProps) {
  const { excess, loading, days } = useExcess();
  const [train, setTrain] = useState(60);
  const guess = p.state.answers["lesson-wf"];
  const w = useMemo(() => walkForward(excess, train, 20), [excess, train]);
  return (
    <Lesson
      id="wf" title="7. Out-of-sample and walk-forward"
      misconception="a rule that fitted the past will work on the future."
      idea="Pick a rule on a stretch of history, then score it only on the next stretch it has never seen, and roll forward. The in-sample score is always flattering; only the out-of-sample score is evidence."
      formula="rule: follow the sign of the trailing mean over the last T days"
      tex={String.raw`\text{position}_t = \operatorname{sign}\Big(\frac{1}{T}\sum_{s=t-T}^{t-1} x_s\Big)`}
      symbols={[["T", "training window, in days"], ["in-sample", "the same days that chose the rule"], ["out-of-sample", "the 20 days after, unseen when the rule was chosen"]]}
      sourceName="walk_forward_splits"
      quiz={{
        question: "A rule earns 12 % a year on the data it was tuned on and 0 % on new data. Which number do you believe?",
        options: ["12 %", "0 %", "The average, 6 %"],
        correct: 1,
        explanation: "Tuning chooses the rule that fits that data, noise included. The new-data figure is the honest estimate.",
      }}
    >
      {days < 120 ? <NoData loading={loading} days={days} need={120} /> : (
        <>
          <Guess id="lesson-wf" {...p} min={-10} max={20} step={1} unit=" % a year" label="Guess the out-of-sample return of 'follow your own recent trend'" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>In-sample: <strong>{pct(w.inSample * 252)}</strong> a year. Out-of-sample, over {w.folds} folds: <strong>{pct(w.outSample * 252)}</strong>. You guessed {guess} %.</p>
              <label className="block text-xs text-text-secondary">
                Training window {train} days
                <input type="range" min={20} max={Math.min(250, days - 40)} step={10} value={train} onChange={(e) => setTrain(Number(e.target.value))} className={SLIDER} aria-label="Training window in days" />
              </label>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 8. The ladder's e-process as a betting game. */
export function EProcessLesson(p: StationProps) {
  const { excess, loading, days } = useExcess();
  const [lambda, setLambda] = useState(0.5);
  const e = useMemo(() => eProcess(excess, lambda), [excess, lambda]);
  const last = e.length ? e[e.length - 1] : 1;
  const guess = p.state.answers["lesson-eproc"];
  return (
    <Lesson
      id="eproc" title="8. The ladder's e-process as a betting game"
      misconception="you must fix the sample size before you may look."
      idea="Start with 1 euro and bet a fixed share of it each day that your excess return is positive. If there is no edge the wealth drifts down; if there is one it compounds. Reaching 20 euros is a rare event under no edge (1 in 20 at most), so you may look every day."
      formula="Eₜ = Eₜ₋₁ · (1 + λ · yₜ),   yₜ = clip(xₜ / σₜ)"
      tex={String.raw`E_t = E_{t-1}\,(1 + \lambda\, y_t), \qquad y_t = \operatorname{clip}(x_t / \sigma_t)`}
      symbols={[["Eₜ", "your wealth after day t"], ["λ", "share of wealth bet, between 0 and 1"], ["xₜ", "excess return on day t"], ["σₜ", "volatility estimated from earlier days only"]]}
      sourceName="e_processes"
      quiz={{
        question: "Under no edge, how often can a betting wealth that starts at 1 ever reach 20?",
        options: ["At most 1 time in 20", "About half the time", "Never"],
        correct: 0,
        explanation: "Ville's inequality: a fair-or-worse bet reaches 20 with probability at most 1/20, however often you look. That is what lets the ladder check every night.",
      }}
    >
      {days < 60 ? <NoData loading={loading} days={days} need={60} /> : (
        <>
          <Guess id="lesson-eproc" {...p} min={0} max={20} step={1} unit=" € after betting" label="Start with 1 €: where does betting on your excess return end up?" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>1 € became <strong>{formatCurrency(last, "EUR", { digits: 2 })}</strong> over {days} days at λ = {formatNumber(lambda, { digits: 1 })}. The ladder's bar is 20 €. You guessed {guess} €.</p>
              <label className="block text-xs text-text-secondary">
                Share bet λ {formatNumber(lambda, { digits: 1 })}
                <input type="range" min={0.1} max={0.9} step={0.1} value={lambda} onChange={(e) => setLambda(Number(e.target.value))} className={SLIDER} aria-label="Share of wealth bet" />
              </label>
              <p className="text-xs text-text-secondary">Betting more makes a real edge pay faster and a missing one cost faster. The ladder averages several sizes so it need not guess.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 9. Covariance shrinkage. The two series are the reader's book and its benchmark. */
export function ShrinkageLesson(p: StationProps) {
  const { book, bench, loading, days } = useOwnSeries();
  const [s, setS] = useState(0.3);
  const guess = p.state.answers["lesson-shrink"];
  const cov = useMemo(() => {
    if (book.length < 30) return null;
    return [[sampleCov(book, book), sampleCov(book, bench)], [sampleCov(book, bench), sampleCov(bench, bench)]];
  }, [book, bench]);
  const sh = cov ? shrinkToDiagonal(cov, s) : null;
  const corr = (c: number[][]) => c[0][1] / Math.sqrt(c[0][0] * c[1][1]);
  return (
    <Lesson
      id="shrink" title="9. Covariance shrinkage"
      misconception="the sample covariance matrix is the truth."
      idea="With many assets and little history, the sample correlations are noisy and the optimiser bets on the noise. Shrinkage pulls every correlation part-way toward zero, trading a little bias for much less noise."
      formula="Σ_shrunk = (1 − s) · S + s · diag(S)"
      tex={String.raw`\hat\Sigma_{\text{shrunk}} = (1 - s)\, S + s \, \operatorname{diag}(S)`}
      symbols={[["S", "sample covariance matrix"], ["diag(S)", "its diagonal: the variances only"], ["s", "shrinkage, between 0 and 1; Ledoit-Wolf picks it from the data"]]}
      sourceName="shrunk_covariance"
      quiz={{
        question: "50 assets, 60 days of returns. Why is the sample covariance matrix a poor input to an optimiser?",
        options: ["It has too few observations for 1,275 numbers", "Returns are not normal", "Assets are too correlated"],
        correct: 0,
        explanation: "1,275 distinct covariances are estimated from 60 days. The optimiser then chases estimation noise. Shrinkage damps it.",
      }}
    >
      {days < 60 || !cov || !sh ? <NoData loading={loading} days={days} need={60} /> : (
        <>
          <Guess id="lesson-shrink" {...p} min={-1} max={1} step={0.1} unit=" correlation" label="Guess the correlation between your book and its benchmark" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>Sample correlation <strong>{formatNumber(corr(cov), { digits: 2 })}</strong>; after shrinking by {formatNumber(s, { digits: 1 })}: <strong>{formatNumber(corr(sh), { digits: 2 })}</strong>. You guessed {guess}.</p>
              <label className="block text-xs text-text-secondary">
                Shrinkage {formatNumber(s, { digits: 1 })}
                <input type="range" min={0} max={1} step={0.1} value={s} onChange={(e) => setS(Number(e.target.value))} className={SLIDER} aria-label="Shrinkage" />
              </label>
              <p className="text-xs text-text-secondary">Two series are well measured, so the sample value is fine here. The cost shows with dozens of assets.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

/** 10. Monte Carlo and the fan chart, resampled from the reader's own days. */
export function MonteCarloLesson(p: StationProps) {
  const { book, loading, days } = useOwnSeries();
  const [years, setYears] = useState(5);
  const guess = p.state.answers["lesson-mc"];
  const fan = useMemo(() => (book.length >= 60 ? bootstrapFan(book, years * 252) : null), [book, years]);
  return (
    <Lesson
      id="mc" title="10. Monte Carlo and the fan chart"
      misconception="the middle line of a projection is what will happen."
      idea="Draw your own past days at random, with replacement, to build many possible futures. The spread of where they end is the fan. It assumes the future resembles the sample, so a short or calm sample gives a fan that is too narrow."
      formula="W_T = Π (1 + r*ₜ),  r*ₜ drawn at random from your daily returns"
      tex={String.raw`W_T = \prod_{t=1}^{T} (1 + r^{*}_t), \qquad r^{*}_t \text{ drawn from your daily returns}`}
      symbols={[["r*ₜ", "a daily return drawn at random from your own history"], ["T", "horizon in trading days"], ["W_T", "final wealth per 1 € invested"]]}
      sourceName="gbm_fan_projection"
      quiz={{
        question: "The 5 % and 95 % lines of a fan chart are…",
        options: ["The worst and best case", "Where 9 in 10 simulated futures end between", "A forecast of next year"],
        correct: 1,
        explanation: "Worse and better outcomes exist outside the fan, and the fan itself only reflects the history it was drawn from.",
      }}
    >
      {days < 60 || !fan ? <NoData loading={loading} days={days} need={60} /> : (
        <>
          <Guess id="lesson-mc" {...p} min={0} max={3} step={0.1} unit=" € in 5 years (median)" label="Guess: 1 € in your book, where is the median after 5 years?" />
          {guess != null ? (
            <div className="space-y-2 text-sm text-text-primary" role="status">
              <p>After {years} years per 1 €: median <strong>{formatCurrency(fan.p50, "EUR", { digits: 2 })}</strong>, 9 in 10 paths between {formatCurrency(fan.p5, "EUR", { digits: 2 })} and {formatCurrency(fan.p95, "EUR", { digits: 2 })}.</p>
              <label className="block text-xs text-text-secondary">
                Horizon {years} years
                <input type="range" min={1} max={20} value={years} onChange={(e) => setYears(Number(e.target.value))} className={SLIDER} aria-label="Horizon in years" />
              </label>
              <p className="text-xs text-text-secondary">Your history is {days} days. Treat the median as an estimate with its own error, not a promise.</p>
            </div>
          ) : null}
        </>
      )}
    </Lesson>
  );
}

interface SaleTax {
  gain_eur: number; teilfreistellung_pct: number; taxable_gain_eur: number; kest_rate: number; soli_rate: number;
  tax_eur: number; estimate: boolean; not_tax_advice: boolean; note: string;
}

/** 11. German tax on a sale. The figure comes from the tax module, never from a copy of its rule. */
export function TaxLesson(p: StationProps) {
  const [gain, setGain] = useState(2000);
  const guess = p.state.answers["lesson-tax"];
  const tax = useQuery({
    queryKey: ["learn", "sale-tax", "lesson", gain],
    queryFn: () => api<SaleTax>(`/api/learn/sale-tax?proceeds_eur=${8000 + gain}&cost_eur=8000&fund_class=aktien`),
    enabled: guess != null,
  });
  const t = tax.data;
  return (
    <Lesson
      id="tax" title="11. German tax on a sale"
      misconception="you pay 26.4 % of the gain."
      idea="For an equity fund 30 % of the gain is exempt (Teilfreistellung), the rest is taxed at 25 % plus a 5.5 % solidarity surcharge on that tax. So the real rate on the gain is lower than the headline. Your annual saver's allowance reduces it further."
      formula="tax = (gain × (1 − 30 %)) × 25 % × (1 + 5.5 %)"
      tex={String.raw`\text{tax} = \text{gain} \times (1 - 30\,\%) \times 25\,\% \times (1 + 5.5\,\%)`}
      symbols={[["gain", "proceeds minus cost"], ["30 %", "partial exemption for equity funds (§20 InvStG)"], ["25 %", "flat withholding tax"], ["5.5 %", "solidarity surcharge on the tax"]]}
      sourceName="apply_teilfreistellung"
      quiz={{
        question: "An equity fund gains 1,000 €. Before any allowance, the tax is closest to…",
        options: ["264 €", "185 €", "0 €"],
        correct: 1,
        explanation: "70 % of 1,000 is 700; × 25 % is 175; plus 5.5 % of that is about 185 €. The allowance may cut it further.",
      }}
    >
      <Guess id="lesson-tax" {...p} min={0} max={800} step={20} unit=" € tax on a 2,000 € gain" label="Guess the tax on a 2,000 € gain in an equity fund" />
      {guess != null ? (
        <div className="space-y-2 text-sm text-text-primary" role="status">
          {t ? (
            <p>
              From the tax module: <strong>{formatCurrency(t.tax_eur, "EUR", { digits: 0 })}</strong> on {formatCurrency(t.gain_eur, "EUR", { digits: 0 })}
              {" "}(taxable after exemption {formatCurrency(t.taxable_gain_eur, "EUR", { digits: 0 })}). You guessed {guess} €. {t.note}{" "}
              <strong>Estimate, not tax advice.</strong>
            </p>
          ) : <p>{tax.isError ? "The tax estimate could not be loaded." : "Calculating."}</p>}
          <label className="block text-xs text-text-secondary">
            Gain {formatCurrency(gain, "EUR", { digits: 0 })}
            <input type="range" min={0} max={20000} step={500} value={gain} onChange={(e) => setGain(Number(e.target.value))} className={SLIDER} aria-label="Gain in euro" />
          </label>
        </div>
      ) : null}
    </Lesson>
  );
}


