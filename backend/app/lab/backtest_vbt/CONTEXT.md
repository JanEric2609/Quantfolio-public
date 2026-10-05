# Context: Backtest VBT

## Responsibility

`vectorbt`-based backtest engine for AlphaCrafter's Trader, producing
consistent results with the strategy-as-code runner.

- `engine.py::run_backtest` — the core engine (`BacktestSpec` in,
  vectorbt `Portfolio` out). Regime-aware: when a `RegimeContext` is
  attached to the spec, it adjusts position sizing and suppresses momentum
  strategies in adverse regimes (bear/high_vol/sideways). Applies configured
  slippage and routes metrics through `compute_metrics` (Track C0,
  2026-08-27 audit — previously slippage was silently dropped and metrics
  were computed inline with a hardcoded `ann_factor=252`, bypassing this
  package's own canonical builder). `vectorbt` itself is imported lazily
  inside the function, not at module top — its import pulls in numba and
  thousands of files, so deferring it keeps package/worker import cheap
  until a backtest actually runs.
- `metrics.py::compute_metrics` — the canonical metrics builder, delegating
  to `quant_metrics` for Sharpe/Sortino/etc. (ADR 0003 decision 4's
  "single-source correctness" rule) rather than a parallel hand-rolled
  computation.
- `signals.py::SmaCrossoverSignalGenerator` — a fittable dual-moving-average
  strategy: `fit` grid-searches (fast, slow) on a training window, `generate`
  turns a fitted pair into entry/exit signals on any window. Fixed to accept
  trailing history ahead of the test window (Track C1a, 2026-08-27 audit) —
  previously called independently on train/test slices with no warm-up,
  silently going flat whenever the fitted `slow` window exceeded the test
  window's length.
- `walk_forward.py::walk_forward_test` — walk-forward cross-validation
  (`SignalGenerator` protocol: fit-on-train, generate-on-test) producing a
  genuine out-of-sample Sharpe. Wired into AlphaCrafter's Trader as a real
  pre-selection gate before a config sweep result is promoted (Track C1b,
  2026-08-27 audit) — previously built but never called from any production
  path.

## Public surface (facade `app.lab.backtest_vbt`)

`run_backtest`, `walk_forward_test`, `SmaCrossoverSignalGenerator`,
`FittedParams`, `compute_metrics`, `BacktestMetrics`.

## Key collaborators

- In: `app.lab.alphacrafter.trader` (`run_backtest`,
  `walk_forward_test` — the real pre-selection gate), `app.interface.api.quant.backtest`.
- Out: `app.foundation.quant_metrics` (canonical Sharpe/Sortino/etc.),
  `vectorbt` (lazy import, `engine.py` only).

## Contract invariants

- Not a member of any import-linter contract (foundation source_modules,
  decision-loop independence, or any facade-only list) — sits outside every
  list, so edges into/out of it aren't checked by any contract.
- Global layering (interface → decision → lab → foundation) holds by inspection.

## Owner-wave notes

`vectorbt` is version-capped to `<7` — its bundled chart templates reference
`scattermapbox`, which plotly 7 removed (see AGENTS.md's dependency
watch-list); any figure this package produces would crash under a
plotly-7-compatible vectorbt until that cap is lifted deliberately.
`walk_forward.py`'s Sharpe computation needs at least 2 windows — a spec
whose date range doesn't divide into ≥2 train/test windows degrades to a
`RuntimeWarning: divide by zero` rather than raising (see `trader.py`'s
comment at its call site), so a caller passing a short date range gets a
silently-degenerate walk-forward result, not an error.
