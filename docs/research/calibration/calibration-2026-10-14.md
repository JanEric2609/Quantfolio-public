# Calibration of the Trust tests, 2026-10-14 (plan WP-D2)

**Run:** `backend/scripts/run_calibration.py` on 2026-10-14.
- 2,000 paths for the false-alarm rate, and 1,000 per edge.
- Wall time 59 s; peak RSS 0.6 GB.
- Raw output: `calibration-2026-10-14.json`.
- **Re-run 2026-10-15** after review: the `overlap_21d` model had re-centred and re-scaled each
  path on its own realised mean, which uses future days and removes part of the drifts'
  sampling variation. It now uses its theoretical scale. The minimum detectable edge now uses
  the time-to-verdict seed. Only the overlap column and M's 10-year edge changed.

Every test is built from `decision/verification/daily_tests.py`'s own constants: clip, prior
variance, bet cap, threshold and futility shift. This is not a trial. It makes no return claim,
and nothing goes into the trial ledger.

## False alarms over 20 years, at the null's boundary

| Test | Nominal | iid normal | t(4)-GARCH | regime switch | AR(1) 0.15 | overlap 21d |
|---|---|---|---|---|---|---|
| F1 skill | 2.5 % | 1.9 % | 1.5 % | 1.7 % | 5.9 % | 58 % |
| F1 harm | 2.5 % | 1.4 % | 1.4 % | 1.4 % | 5.1 % | 56 % |
| S skill | 2.5 % | 1.9 % | 1.5 % | 1.8 % | 6.0 % | 58 % |
| S harm | 2.5 % | 1.5 % | 1.4 % | 1.5 % | 5.1 % | 56 % |
| U futility | 5 % | 3.7 % | 3.1 % | 2.9 % | 8.5 % | 62 % |
| M futility | 2.5 % | 1.5 % | 1.4 % | 1.4 % | 5.1 % | 56 % |

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

## The noise check (adopted 2026-10-15; ADR 0018 amendment of 2026-10-14, §11)

The owner adopted a pre-registered **noise check** for S, U and M, measured on the series' own
noise, not its mean.

- **Statistic:** VR(21), Lo and MacKinlay's overlapping variance ratio
  (`foundation.forecast_verification.variance_ratio`).
- **Looks and cutoffs:** at 63, 126 and 252 observed days, failing above 3.0, 2.3 and 1.8.
- **On failure:** the failure is final. The series' verdicts read "not valid: serial
  correlation". ADR 0018 then moves its test to non-overlapping observations; a further
  amendment builds that test before its first value is read (not built yet).
- **Until the day-252 look passes:** S/U verdicts are provisional, and no mandate retires.

The single cutoff first recommended here (1.3 on day 63) was dropped before adoption. With 63
days, VR(21) has a standard error of about 0.6, so 1.3 fails about a quarter of clean series.

How often the check fails, from 20,000 paths per model
(`run_calibration.py --noise-check`, `noise-check-2026-10-15.json`):

| Model | Fails at day 63 | Fails at any look |
|---|---|---|
| iid normal | 2.0 % | 4.9 % |
| t(4)-GARCH | 2.0 % | 6.1 % |
| regime switch | 2.5 % | 8.3 % |
| AR(1) 0.15 | 5.8 % | 19.9 % |
| overlap 21d | 52.6 % | 99.2 % |

- **What it catches:** the check catches the failure that matters, persistent per-pick drift.
- **What it misses:** it cannot reliably catch AR(1) 0.15 within a year. In that case false
  alarms stay at about 2× nominal, and the page does not claim otherwise.

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
  - M: 1.5 % / 1 % (the 1 % edge's median verdict is 9.7 years, right at the 10-year line).
- **Matched controls work:** S needs half the edge F1 does, which is the 4× variance cut the
  audit measured.
