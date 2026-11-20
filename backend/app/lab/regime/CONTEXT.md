# Context: Regime

## Responsibility

Macro/market regime classification, two independent producers. `macro_snapshot.py`
computes a rule-based snapshot (`compute_regime_snapshot` — VIX level, 10Y-3M
yield spread, world-index momentum; `get_or_refresh_regime` — DB-cached
wrapper) labelling bull/bear/high_vol/low_vol/transition. `classifier.py`
(`classify_and_store`, `refit_regime_model`) fits/applies the statistical
jump model (`jump_model.py`, the live model since ADR 0004's 2026-09-28
amendment; setting `regime_model_kind`, default `jump`) with the Gaussian HMM
(`hmm_model.py`) as its fallback, over features built by `features.py`
(market index + FRED macro indicators, mapped via `MACRO_SERIES_MAP`, each
series carried forward on its own). Both label bull/sideways/bear by the
states' volatility level (low → bull), refit weekly, and write the snapshot
`source` (`jump` | `hmm`). The jump model's `score` is the calibrated
probability that today's online label survives hindsight (per label and
days-since-switch bucket, counted over the fit window; payload
`score_basis: "label_reliability"`), not the DP softmax, which sat at 1.00
(ADR 0004 amendment 2). The classifier refuses a feature row that lags
the newest bar, and the refit backfills a short index history once. VIXCLS
days FRED has not published are filled from the `^VIX` bars. The
result then runs through a stateless rule-based crisis override
(`crisis_gate.py` — VIX/credit-spread/drawdown conditions can force a
`crisis` label regardless of the model's output). `gate.py` maps either
producer's label to per-factor affinity weights (`RegimeGate`,
`get_factor_weights`, `apply_regime_gate`) consumed by portfolio-tilt logic.
A `bear` snapshot also blocks Discover (`MacroRegimeGate`).

## Public surface (facade `app.lab.regime`)

`RegimeContext`, `RegimeGate`, `compute_regime_snapshot`, `get_or_refresh_regime`.

`classify_and_store`, `refit_regime_model` (classifier.py),
`register_regime_daily_job`, `register_regime_refit_job` (jobs.py), and
`get_regime_adjusted_weights`/`get_regime_label`/`get_factor_weights`/
`apply_regime_gate` (gate.py) are **not** re-exported at the package root —
every current external caller imports these submodules directly
(`regime.classifier`, `regime.jobs`, `regime.gate`, `regime.macro_snapshot`).
This is permitted (regime is not one of the six facade-only-protected
contexts) but means the facade under-represents actual usage; treat the
submodule list above as the practical public surface until/unless this
package joins a facade-only contract.

## Key collaborators

- In: `app.worker` (`register_regime_daily_job`/`register_regime_refit_job`
  registration, `get_or_refresh_regime`, `regime_advisor` MWU updates),
  `app.interface.api.portfolio_advisor` / `app.interface.api.quant.{portfolio,regime,research}`,
  `app.decision.discover.regime_gate` (wraps `regime.gate` behind discover's
  own `MacroRegimeGate`), `app.foundation.portfolio.metrics_wrappers`,
  `app.foundation.portfolio_analysis`, `app.foundation.llm_research` / `research.py`.
  (Note: `recommendation_engine.context_builder` reads regime state via
  `data_backbone.regime_store.RegimeStore`, not via this package directly.)
- Out: `app.foundation.data_backbone.{bars,ingest,regime_store}` (`BarStore`,
  `DataIngester`, `RegimeStore` — regime snapshots are persisted through
  `RegimeStore`, not owned here), `app.foundation.settings`
  (`get_public_settings`), `app.foundation.models.entities.MacroIndicator`.

## Contract invariants

- Lab-tier under "Global layering" (ADR 0015 Phase 5). `regime` is not a
  member of "Decision-loop packages are independent" — it sits outside the
  7-member independence cluster, so member↔regime edges (e.g.
  `discover.regime_gate -> regime.gate`) aren't checked by that contract.
- **Documented cross-layer exception**: `regime` is lab-tier because
  `classifier.py` imports `quant_ml.registry` at module level, yet four
  foundation-tier modules read it through lazy, function-local imports
  (`portfolio.metrics_wrappers -> regime.gate`, `portfolio_analysis -> 
  regime.macro_snapshot`, `research -> regime.macro_snapshot`,
  `llm_research -> regime`). Those four edges are seeded on the "Global
  layering" contract's `ignore_imports` ledger — see `app/lab/CONTEXT.md`.
- Not a member of any of the six facade-only contracts.
- Global layering holds (interface → decision → lab → foundation).

## Owner-wave notes

Operationally fragile: `classify_and_store` can silently produce a stale/
frozen regime snapshot if feature rows are all NaN/constant (guarded by
`_validate_feature_matrix`'s `CONSTANT_STD_THRESHOLD` — the historical
"frozen `{sideways: 0.99994}`" production failure this guard was added for)
or if `register_regime_daily_job`'s inner function swallows an exception
without surfacing a warning status (see Batch A item 2 in the 2026-08-26 fix
plan — `regime_daily_inner` now re-raises after logging and returns a
`{"status": "warning"}` sentinel when `classify_and_store` reports
`written=False`, instead of reporting job success unconditionally). Consumed
by both `discover` (regime gate on the candidate pipeline) and
`llm_portfolio`-adjacent advisor/portfolio code — a stale regime here degrades
decisioning in two independent loops at once, so treat drift/staleness bugs
in this package as high priority regardless of its own line count.
