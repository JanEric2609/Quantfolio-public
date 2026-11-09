# Context: lab/forward_lab

## Responsibility

The nightly forward-test lab (plan `docs/plan-forward-lab-2026-11-09.md`):
published signals score every MSCI World member each night, the scores and
their long-short portfolios are frozen before the next day's prices exist,
and the next night records how each did. A shadow record: nothing here feeds
Discover, a pick or a trade, and no backtest is ever written.

## Public surface

- `service.py`: `run_nightly` (score, resolve, store the summary), `summary`,
  `stored_summary` (the night's copy with 10,000 resamples, read by
  `GET /api/trust/forward-lab`), `portfolios`, `START` (2026-11-10).
- `signals.py`: the fixed list (`SIGNALS`); kernels in
  `foundation/price_signals.py`, shared with Discover's pipeline.
- `robustness.py`: e-values with a predictable clip or a fixed one, e-BH,
  the block-bootstrap false-alarm rate, the evidence without the best days
  and by halves. `decision/verification` uses it (as `robustness_checks`)
  for `selection_w`.
- `news_tone.py`: the overnight LLM reading of US members' headlines
  (Finnhub), masked names, one `news_tone_scores` row per stock and night.
- `jobs.py`: `register_forward_lab_job` (04:30 UTC),
  `register_news_tone_job` (05:00 UTC).

## Invariants

- Snapshots are written once and never updated; `created_at` precedes the
  resolving bar.
- A news row dated D is used from scoring day D + 1 on.
- Adding a signal is a code change with a published source, never a tuning
  step; e-BH over all signals keeps the family's error at 1 %.
- Imports only `foundation` and `lab.calibration`.
