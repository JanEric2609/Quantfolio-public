# Context: lab.pooled_model

## Responsibility

Phase 3, step 2 of docs/archive/audits/2026-09-24-why-it-does-not-work: one pooled
cross-sectional model over every JKP characteristic, fitted under purged
walk-forward CV, and its honest out-of-sample record. Two pre-registered models
(`spec.py`): ridge and LightGBM. Features and the label are centred ranks within
country and month; the universe is `factor_premia`'s. Each test month is scored
only by models fitted on earlier, purged and embargoed months, and turned into
the same tercile portfolios `factor_premia` builds, so the models can be
compared month by month with the value + momentum baseline.

These are mined signals. Under the two-tier evidence standard they gate
nothing until DSR/PBO (step 4) passes them; nothing here is read by the
monthly plan.

## Public surface (facade `app.lab.pooled_model`)

- `run_model_study(db, region=..., panel_dir=..., persist=..., n_folds=...)` →
  `ModelStudy` (cards for `ridge`, `gbm` and the `value_momentum` baseline,
  monthly series, fold summaries). With `persist`, records each model once on
  the trial ledger (context `pooled_model`) and writes
  `<panel>/derived/pooled_model_<region>_results.json` (`results_path`, read
  by `lab.evidence_gate`), plus every stock's
  out-of-sample scores to `scores_path(panel_dir, region)`
  (`pooled_model_<region>_scores.parquet`), which `lab.satellite` (step 3)
  picks from.
- `month_series(frame, score)`: tercile portfolios and rank IC for any score.
- CLI: `python -m app.lab.pooled_model [--region world|europe] [--dry-run]`.

## Collaborators

- Out: `foundation.data_engineering.panel_sql` (DuckDB), `lab.factor_premia.strategies`
  (regions, universe rules), `lab.quant_lab.cv` (`purged_embargo_splits`),
  `foundation.quant_metrics` (`newey_west_t_stat`, `record_trial`), LightGBM.

## Invariants

- `spec.py` is fixed before any result is seen; a change after a run is a new trial.
- No full-panel matrix in memory: DuckDB writes the universe once, month-sorted,
  to `<panel>/derived/`; ridge is solved from per-month X'X / X'y; the trees
  train on a fixed-seed sample of at most `GBM_MAX_ROWS` rows.
- A fold never sees its own test months, the purge month before them, or the
  embargo month after an earlier test block.
