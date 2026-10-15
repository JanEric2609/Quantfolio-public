# Calibration of the Trust tests, 2026-10-14 (plan WP-D2)

**Run:** `backend/scripts/run_calibration.py` on 2026-10-14.
- 2,000 paths for the false-alarm rate, and 1,000 per edge.
- Wall time 59 s; peak RSS 0.6 GB.
- Raw output: `calibration-2026-10-14.json`.

Every test is built from `decision/verification/daily_tests.py`'s own constants: clip, prior
variance, bet cap, threshold and futility shift. This is not a trial. It makes no return claim,
and nothing goes into the trial ledger.

## False alarms over 20 years, at the null's boundary

| Test | Nominal | iid normal | t(4)-GARCH | regime switch | AR(1) 0.15 | overlap 21d |
|---|---|---|---|---|---|---|
| F1 skill | 2.5 % | 1.9 % | 1.5 % | 1.7 % | 5.9 % | 52 % |
| F1 harm | 2.5 % | 1.4 % | 1.4 % | 1.4 % | 5.1 % | 52 % |
| S skill | 2.5 % | 1.9 % | 1.5 % | 1.8 % | 6.0 % | 52 % |
| S harm | 2.5 % | 1.5 % | 1.4 % | 1.5 % | 5.1 % | 52 % |
| U futility | 5 % | 3.7 % | 3.1 % | 2.9 % | 8.5 % | 58 % |
| M futility | 2.5 % | 1.5 % | 1.4 % | 1.4 % | 5.1 % | 52 % |

"Nominal" is 1 / threshold per test (e ≥ 40, U e ≥ 20) before the K-family correction.

## Reading

1. **Heavy tails, volatility clustering and regimes are fine.**
   - The clip and the aGRAPA bet keep every test at or below its nominal rate.
2. **Serial correlation is the weakness.**
   - At a daily autocorrelation of 0.15, false alarms run about 2-2.5 times the nominal rate.
3. **Under `overlap_21d`, the tests are not valid.**
   - That model gives each open pick a drift that persists through its 21 days and carries
     30 % of the day's variance.
   - The null "no edge on average" then still allows days whose expected return, given the
     past, is positive. An e-process bets on exactly that.
   - This is a stress model, not an estimate. Daily idiosyncratic stock returns show almost no
     persistence over a month, and slight reversal if anything. But nothing measured yet rules
     it out for these baskets.

## Recommendation (owner decision; not implemented)

Add a pre-registered **noise check** to ADR 0018's amendment before any e-value is read. It
belongs in the same window in which M measures σ.
- **When:** on day 63 after T0.
- **What:** compute each series' variance ratio VR(21), the variance of non-overlapping 21-day
  sums over 21 × the daily variance, with its standard error.
- **If VR(21) > 1.3:** the Trust page marks that series' S/U/M verdicts "not valid: serial
  correlation", and a new amendment moves that test to non-overlapping cohorts. Each cohort is
  then one observation of its whole 21-day return.
- **Otherwise:** nothing changes.

The check uses only the series' own noise, not its mean, so it does not peek at the answer.

## Time to a verdict (iid normal; t(4)-GARCH in brackets)

Median years and P(verdict within 10 years) at a true edge per 21 days:

| Test (sd per 21 d) | 0.5 % | 1 % | 2 % |
|---|---|---|---|
| S skill (2.3 %) | 16.4 y, 31 % (12.6 y, 39 %) | 3.8 y, 94 % (2.4 y, 98 %) | 0.9 y (0.7 y) |
| F1 skill (5.0 %) | > 60 y, 7 % | 19.5 y, 25 % (15.2 y, 34 %) | 4.6 y, 89 % (2.9 y, 96 %) |
| M futility (3.7 %) | 43.6 y, 10 % | 9.7 y, 51 % (7.4 y, 64 %) | 2.2 y, 99 % |

- **Edge direction:** for futility, the edge is the distance below δ. U at 0.5 % beyond the
  null is a true return of +0.1 %/21 d, and that takes 12.5 years.
- **Minimum edge detected within 5 / 10 years (median):**
  - S: 1 % / 0.75 % per 21 days;
  - F1: 2 % / 1.5 %;
  - M: 1.5 % / 1.5 %.
- **Matched controls work:** S needs half the edge F1 does, which is the 4× variance cut the
  audit measured.
