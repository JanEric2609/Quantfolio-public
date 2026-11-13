# Pre-registration draft: pooled study of non-price features (not yet run)

Status: draft for the owner. Nothing here has been run. Once the owner signs off, commit this file unchanged
before the first run, and change it afterwards only by an appended, dated amendment.

## Question
Do non-price features (fundamentals, analyst revisions, insider and news tone) rank next-month returns of
MSCI World constituents, after the price-based features (momentum, volatility, size) are controlled for?

## Data and period
- Universe: the MSCI World constituents held in the system's universe table, point in time where the data allows.
- History: the full price history available per name; features only from fields stamped before the decision date.
- Targets: forward 21-trading-day return in EUR, net of nothing (gross), ranked within date.

## Primary hypothesis (one only)
The cross-sectional rank information coefficient (IC) of the combined non-price score, orthogonalised
to the price features, has a mean greater than zero.

## Test and threshold
- Statistic: Newey-West t of the monthly IC series, with lags set to 4 (the doc 2026-11-12-gate-calibration.md
  shows 2 lags is liberal under autocorrelation).
- Decision rule: t >= 3.0 (decision D9, kept) AND the deflated IC-Sharpe positive with the trial count of
  every family tried so far.
- The rule is fixed before the run. No second threshold, no second hypothesis, no subgroup is promoted from this run.

## Power (from the calibration doc)
With about 42 independent monthly periods, 80 % power at t >= 3 needs an ICIR near 0.59. With 120 periods
it needs 0.35, with 250 periods 0.24. The earlier pooled-ML run measured about 0.2. Stated plainly: at the
present sample this study can only confirm a large effect. A null result does not show there is no effect.

## Forking paths ruled out
- Feature list, lags, winsorising and the orthogonalisation are fixed below and not tuned on the result.
- Walk-forward only: the model is fit on data before each test window, never on the window.
- Every variant tried is written to the trial ledger, including failed ones.

## Fixed analysis plan
1. Build features as of each month-end from stamped data.
2. Rank within date, orthogonalise to the price features by regression per date.
3. Combine by equal-weight z-scores (no fitted weights in the primary test).
4. Compute the IC series, the Newey-West t, the deflated IC-Sharpe.
5. Report the IC series, t, ICIR, number of periods and the trial count, whatever the outcome.

## What counts as a result
- Pass: both gate conditions. It then goes to the existing paper-trade ladder; this study grants no capital.
- Fail: report it as a fail. Do not widen the universe, change the horizon or add features to rescue it.

## Owner decisions needed
- Sign-off on the single hypothesis and the lag count.
- Whether to run it at all, given the power above.
