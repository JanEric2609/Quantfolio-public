# Context: Quant Lab

## Responsibility

ADR 0015 Phase 4 ("data spine" -> "lab"). A new, additive foundation-tier
package: the custom vectorised backtest engine (ruling #25, superseding ADR
0003 decision 7's "two engines, unification deferred until a third engine
proposal appears" — this is that proposal), the explicit cost model (ruling
#27), and the first signal batch (ruling #26) over the PIT Parquet panel
Phase 3 built but left unconsumed.

- `costs.py` — `CostModel` (commission + spread bps) and
  `estimate_tax_drag` (composes existing `tax_calc` scalar building blocks
  into a per-realized-gain German tax estimate; documented simplification —
  single running Sparer-Pauschbetrag allowance for the whole backtest, no
  calendar-year reset, no loss-bucket offsetting).
- `cv.py` — `purged_embargo_splits`: AFML ch.7 purge + embargo walk-forward
  splits. Reimplements `alphacrafter/tuning.py::walk_forward_splits`'s
  algorithm standalone (see Contract invariants below for why).
- `engine.py::run_lab_backtest` — periodic target-weight rebalancing over a
  symbol panel, pure numpy/pandas (no vectorbt/numba), applying `costs.py`'s
  model on every trade including FIFO cost-basis tracking (via
  `tax_calc.fifo_consume`) so realized gains feed `estimate_tax_drag` for
  real. Two opt-in `LabBacktestSpec` options serve a retail book:
  `rebalance="trades"` (a rebalance row trades only the symbols it names:
  0 sells the whole position, NaN holds it untouched, positive weights split
  the cash between the buys, so nothing is ever trimmed) and
  `offset_losses=True` (realized losses carried forward against later gains,
  the Verlustverrechnungstopf; right for a book of one asset class).
  `backtest_vbt` is retained solely as the parity-check oracle (a test
  in `test_quant_lab_engine.py` runs an identical zero-cost buy-and-hold
  scenario through both engines and asserts numerical agreement) — not
  deleted, not otherwise touched.
- `app.lab.quant_ml.labels::vol_scaled_forward_return` (extends the
  existing file, not a new one) — the cross-sectional, volatility-scaled
  forward-return target (ruling #24).
- `pooled_ml.py` — the **pooled cross-sectional model** Discover's
  `stage_ml_signal` serves (ADR 0015 ruling #24, 2026-09-28). It replaces
  one triple-barrier classifier per shortlisted ticker; all 43 of those on
  prod were rejected against the majority-class baseline.
  - Features and label:
    - 10 price-only features. Each is computed on the symbol's own trading
      calendar, then turned into a centred rank within its date.
    - The label is `labels.vol_scaled_forward_return` over 20 days, as a
      centred rank.
  - Models:
    - Ridge and LightGBM. The spec is fixed in the module (`spec_hash`);
      any change to it is a new trial.
    - LightGBM is served; ridge is the reference.
    - Each is recorded once per spec on the global trial ledger (context
      `discover_pooled_ml`).
  - Validation and gate:
    - `run_study` evaluates both models with `quant_lab.cv.purged_embargo_splits`
      over dates, purge 20.
    - The gate is a mean OOS rank IC of at least 0.02 and a Newey-West t
      (lags 20) of at least `HLZ_T_THRESHOLD` (3.0).
  - Persistence:
    - `train_pooled_model` always writes a `QuantMlModel` row
      (`name="discover-ml-pooled"`, no ticker). Only a passing model gets
      an artefact.
    - `latest_pooled_row` returns the newest row. Only that row is served,
      so a newer rejection withdraws an older pass.
  - First real run (150-name miner universe, 2022-09 to 2026-08, one
    pre-registered run):
    - LightGBM: IC 0.014, t 1.17.
    - Ridge: IC 0.008, t 0.40.
    - Momentum 12-1 alone: IC 0.046, t 2.06.
    - Both models are rejected. In-sample IC is 0.25, so the trees overfit
      noise. On 150 mega-caps with price data only, there is little for a
      pooled model to learn. Studies with large gains (e.g. Filipović &
      Pasricha, arXiv 2212.01048: IC 5.9% on ~30k US stocks with 94
      characteristics) draw much of it from illiquid names and liquidity
      features. Their value-weighted results are far weaker.
  Lives here rather than in `quant_ml` because it uses `quant_metrics` and
  `cv.py`, and a quant_ml edge to either would close the
  `foundation -> lab.regime -> quant_ml.registry` package cycle.
- `signal_batch.py` — orchestrates `universe.py`'s practical universe →
  `data_engineering.panel_read.read_panel` (Phase 3, unused until now) →
  `compute_microstructure_factors` (momentum/vol/liquidity — see its
  docstring for why literal Fama-French value/size factors are deferred:
  the PIT panel has no fundamentals yet) → `vol_scaled_forward_return` →
  `cv.purged_embargo_splits` → per-fold cross-sectional IC/ICIR (mirrors
  `alphacrafter.miner`'s proven rank-correlation approach, reimplemented
  standalone) → gate by `quant_lab_min_ic`/`quant_lab_min_icir` → Parquet
  report (no new DB table this phase, matching Phase 3's precedent).
- `universe.py::LAB_UNIVERSE` — the 69-ticker STOXX-Europe-proxy list,
  duplicated from `discover/universe.py::_STOXX600` (see its docstring for
  why duplicated rather than imported, and why it's a proxy, not the ADR's
  full 600 constituents).

## Public surface (facade `app.lab.quant_lab`)

`CostModel`, `TaxDragResult`, `apply_trade_costs`, `estimate_tax_drag`,
`LabFold`, `purged_embargo_splits`, `LabBacktestResult`, `LabBacktestSpec`,
`run_lab_backtest`, `FactorResult`, `PricePanel`, `SignalBatchResult`,
`build_price_panel`, `compute_microstructure_factors`, `run_signal_batch`,
`LAB_UNIVERSE`.

## Key collaborators

- In: `lab.pooled_model` (`purged_embargo_splits`) and `lab.satellite`
  (engine + cost model), both run from their CLIs; otherwise
  `signal_batch.py`/`engine.py` are run manually (CLI/script), not called
  from any API route, job, or other service. That repoint
  (advisor/discover/graduation consuming lab output) is a later ADR phase.
- Out: `app.foundation.data_engineering` (`read_panel`), `app.foundation.tax_calc`
  (`fifo_consume`, `apply_teilfreistellung`, `apply_sparer_pauschbetrag`,
  `KEST_RATE`, `soli_amount`, `kirchensteuer_amount`), `app.lab.quant_ml`
  (`vol_scaled_forward_return`), `app.lab.backtest_vbt.metrics`
  (`compute_metrics`, `BacktestMetrics` — reused for the engine's post-run
  stats, ADR 0003 decision 4), `app.foundation.settings`, `app.foundation.core.db`.

## Contract invariants

- Lab-tier under "Global layering" (ADR 0015 Phase 5, which subsumed the
  retired "Foundation services never import the decision loop" contract that
  had enrolled this package alongside `data_backbone`/`providers`/
  `data_engineering`). Two things this package deliberately does
  **not** import despite obvious code reuse: `app.lab.alphacrafter`
  (has the real purge+embargo splitter and the real IC/ICIR method — both
  reimplemented standalone in `cv.py`/`signal_batch.py` instead) and
  `app.decision.discover` (has the real STOXX-600-shaped ticker list —
  duplicated into `universe.py` instead). Both are decision-loop packages;
  a foundation-tier package importing either would invert the required
  dependency direction. Confirmed via `lint-imports` (10/10 contracts kept).
- Global layering holds (lab-tier; foundation reach is `app.foundation.core.db` only).
- `scripts/check_no_new_cycles.py`'s golden ledger needed regenerating for
  this package too, for the exact same reason documented in
  `data_engineering/CONTEXT.md`: `_package_of` collapses a bare 3-part
  package name (e.g. `app.foundation.data_engineering`, imported via its
  facade) to `app.services`, so any new foundation package that (a) has a
  normal `__init__.py` facade and (b) imports a sibling package's facade
  mechanically joins the pre-existing `app.services`-rooted package-level
  SCC. 0 real module cycles before or after.

## Owner-wave notes

`run_lab_backtest`'s tax-drag handling is real (FIFO lots tracked per
symbol, `fifo_consume` called on every sell, `estimate_tax_drag` applied to
the actual realized gain) but intentionally simplified relative to the tax
cockpit: one running Sparer-Pauschbetrag allowance for the whole backtest
window (no per-calendar-year reset), no loss-bucket offsetting
(`tax_calc.classify_loss_bucket` untouched), Teilfreistellung percentage
passed in by the caller per symbol rather than derived from fund
classification. Good enough to make backtested Sharpe/return numbers
reflect *some* tax drag instead of none — not a substitute for
`tax_cockpit.py`'s `estimate: True`/`not_tax_advice: True` outputs, and this
package has no API surface to carry that convention on regardless.

`signal_batch.py`'s factor set is honestly scoped to what the OHLCV-only PIT
panel supports today (momentum, realized vol, dollar-volume liquidity) —
not literal Fama-French value/size, which need per-stock fundamentals the
panel doesn't carry yet. Extending `panel_schema.py` with fundamentals
fields (or a real Datastream PIT extract, deferred in Phase 3) is the
natural next step before a genuinely "classic factors" batch is possible.

Of those three, only dollar-volume liquidity depends on the *scale* prices
are quoted in; momentum and realized vol are ratios and survive any
denomination. `compute_microstructure_factors` therefore returns
`dollar_volume_20d` only when the panel can be put on one currency scale --
a single currency, or `fx_to_base` rates supplied by the caller (CLI:
`--fx GBP=1.17`). Pence quotation (`GBp`) is normalised without rates,
being definitional. A multi-currency panel with no rates yields two factors,
not three: an absent factor is recoverable, one that ranks quotation unit is
not.
