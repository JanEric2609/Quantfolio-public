# Context: lab/calibration

## Responsibility

Measures the trust tests (ADR 0018 and its amendment of 2026-10-14) on
simulated daily series: the false-alarm rate over 20 years, the years to a
verdict per edge and noise, and the smallest edge a verdict can find in 5 and
10 years. Plan 2026-10-14, WP-D2. Not a trial: it makes no return claim and
never touches the trial ledger.

## Public surface

`harness.py`: `TestSpec`, `e_paths` (the aGRAPA bettor across paths, the same
algebra as `foundation.forecast_verification.e_process_bernoulli`),
`crossing_days`, `simulate` (iid normal, t(4)-GARCH(1,1), AR(1) 0.15,
two-regime noise, F1-style overlapping membership), `type_i_error`,
`time_to_verdict`, `minimum_detectable_edge`, and `MODELS` (the noise models
by name).

## Invariants

- Reads nothing from `decision`: the test constants come in as `TestSpec`s;
  `scripts/run_calibration.py` builds them from `decision.verification.daily_tests`.
- Seeded; NumPy only; no I/O.
- A new experiment is registered only if this harness shows it can reach a
  verdict in under 10 years at a plausible edge (plan WP-D2).
