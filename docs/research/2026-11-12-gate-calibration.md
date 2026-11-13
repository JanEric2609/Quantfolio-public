# Gate null calibration and power (WP15, items 1 and 2)

Reproduce: `cd backend && .venv/bin/python scripts/gate_null_calibration.py --sims 2000 --seed 1`. Nothing here is a strategy trial and none of it goes in the trial ledger. The promotion test was calibrated separately by `scripts/promotion_null_sim.py` (WP10).

## What it shows

1. **The capital ladder does not fire by chance.** With no effect, the Earned bar was reached in under 1 % of 252-day runs (stated bound 10 %) and the Proven bar in about 0.05 % (bound 2.5 %), for normal, fat-tailed (t with 3 degrees of freedom) and volatility-clustered excess returns. The harm bar fires about as often as the skill bar, as the symmetry of the bet requires. The bounds hold with a wide margin.
2. **The price of that safety is power.** To reach the Earned bar in 80 % of runs the paired excess needs a mean/sd of about 0.3 per day within a year (annualised information ratio near 4.8), 0.1 to 0.15 within three years (IR 1.6 to 2.4) and 0.08 to 0.1 within five (IR 1.3 to 1.6). A real information ratio of 0.3 to 0.5 will almost never be detected within the horizons that matter to the owner. This is the arithmetic behind "the ladder stays at tuition": it is working as designed and cannot tell a modest edge from none. Do not read a long stay in tuition as evidence of no skill.
3. **The Newey-West t gate (2 lags) is slightly liberal under autocorrelation.** With independent periods it matches its stated level at the 2.0 cut-off (0.030 against 0.023) and is mildly high at 3.0 (0.0025 against 0.0013). With first-order autocorrelation 0.3, which overlapping returns produce, it fires at 0.05 for t ≥ 2 and 0.0085 for t ≥ 3, six times its stated 0.0013. A t ≥ 3 gate therefore holds about a 1 % false-positive rate at 42 or 120 periods, not 0.13 %. Remedy if the gate matters: more lags (the 21-day horizon suggests at least 20, as the promotion test already uses) or the cosine estimator from the WP10 harness.
4. **The deflated IC-Sharpe gate is conservative.** At one trial it fires in 4.6 % of null runs (stated 5 %); at 10 and 30 trials it essentially never fires, which is the intended deflation.
5. **Power of the t ≥ 3 gate.** The ICIR (mean information coefficient over its standard deviation, per period) needed for 80 % power is 0.59 at 42 independent periods, 0.35 at 120 and 0.24 at 250. The audit's pooled-gate figure (about 42 independent periods needing about 0.46) was in this range; the exact value depends on the cut-off and lags.

## Tables (2000 simulations per cell, seed 1)

| Daily excess | P(Earned bar ever) | stated <= | P(Proven bar ever) | stated <= | P(harm >= 10 ever) | stated <= |
|---|---|---|---|---|---|---|
| normal | 0.0080 | 0.1000 | 0.0005 | 0.0250 | 0.0040 | 0.1000 |
| t3 | 0.0080 | 0.1000 | 0.0000 | 0.0250 | 0.0030 | 0.1000 |
| clustered | 0.0060 | 0.1000 | 0.0005 | 0.0250 | 0.0075 | 0.1000 |

## Ladder power: smallest daily mean/sd (grid-resolved) that reaches the Earned bar within the horizon in 80 % of runs

| Daily excess | horizon (trading days) | mean/sd per day | annualised IR |
|---|---|---|---|
| normal | 252 | 0.3 | 4.76 |
| normal | 756 | 0.15 | 2.38 |
| normal | 1260 | 0.1 | 1.59 |
| t3 | 252 | 0.3 | 4.76 |
| t3 | 756 | 0.1 | 1.59 |
| t3 | 1260 | 0.08 | 1.27 |

## IC gates, no effect

| Gate | n | autocorr | measured | stated |
|---|---|---|---|---|
| Newey-West t >= 2 (one-sided) | 42 | 0.0 | 0.0300 | 0.0228 |
| Newey-West t >= 3 (one-sided) | 42 | 0.0 | 0.0025 | 0.0013 |
| Newey-West t >= 2 (one-sided) | 42 | 0.3 | 0.0500 | 0.0228 |
| Newey-West t >= 3 (one-sided) | 42 | 0.3 | 0.0085 | 0.0013 |
| Newey-West t >= 2 (one-sided) | 120 | 0.0 | 0.0305 | 0.0228 |
| Newey-West t >= 3 (one-sided) | 120 | 0.0 | 0.0020 | 0.0013 |
| Newey-West t >= 2 (one-sided) | 120 | 0.3 | 0.0495 | 0.0228 |
| Newey-West t >= 3 (one-sided) | 120 | 0.3 | 0.0080 | 0.0013 |
| Deflated IC-Sharpe > 0.95, N=1 | 42 | 0 | 0.0460 | 0.0500 |
| Deflated IC-Sharpe > 0.95, N=10 | 42 | 0 | 0.0010 | 0.0500 |
| Deflated IC-Sharpe > 0.95, N=30 | 42 | 0 | 0.0000 | 0.0500 |

## IC gate power (Newey-West t >= 3, 2 lags): ICIR per period needed for 80 % power

| n periods | needed ICIR (analytic, t=3+0.84) | measured power at that ICIR (rho 0) |
|---|---|---|
| 42 | 0.59 | 0.84 |
| 120 | 0.35 | 0.79 |
| 250 | 0.24 | 0.80 |

## Not covered here

The evidence gate (`evidence_gate`) combines several lab studies and has no single null distribution to simulate from. Its parts are covered above (Newey-West, deflated IC-Sharpe) and in WP10 (promotion). The LLM A/B, the ten-issuer truth set and the pre-registration draft (WP15 items 3 to 5) are separate documents.
