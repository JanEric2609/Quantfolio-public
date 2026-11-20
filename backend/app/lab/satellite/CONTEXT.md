# Context: lab.satellite

## Responsibility

Phase 3, step 3 of docs/archive/audits/2026-09-24-why-it-does-not-work: would the
stock-picking satellite be worth having, once broker costs and German tax are
paid? Step 2 (`pooled_model`) grades scores as tercile portfolios of
thousands of stocks, which no retail book can hold. Here each out-of-sample
score picks the satellite the report's target shape allows, and the picks
are traded through `quant_lab`'s engine against the MSCI World core.

The satellite (`spec.py`, fixed before any result is seen) is EUR 3,600 of mega
caps, reviewed at quarter ends. A holding is kept while its score stays in the
top tenth of the eligible stocks, and free slots are filled with the
best-scored stocks not held. Nothing is ever trimmed. The broker's order fee
sets how many stocks that money can hold, so each broker is its own variant:

- DKB: EUR 10 per order, three stocks of EUR 1,200, about 83 bps each way;
- Scalable Capital: EUR 0.99 per order, ten stocks of EUR 360, about 28 bps.

Both pay a 10 bps half-spread.

Each score is run three times:
- gross: no costs, no tax;
- net: order fee and spread;
- after tax: also 25 % KESt + Soli on every realized gain, losses carried
  forward, no Sparer-Pauschbetrag (the core's Vorabpauschale uses it).

Prices are total-return indices (JKP's excess return plus the US T-bill rate
from Ken French's library, `rf.py`), because tax falls on the whole gain. The
core is the cap-weighted universe less a 0.20 % TER. It is taxed once, after 20
years, with the equity fund's 30 % partial exemption, expressed as a yearly
drag (`core_tax_annual`). JKP returns are in US dollars; currency moves hit
the satellite and a world core alike and are left out.

The net run is also the record for years when no tax is due at all (a
Nichtveranlagungsbescheinigung): the core then pays none either, so net minus
core is the tax-free excess. `lab.evidence_gate` grades it as its own family.

These are still mined signals: nothing here gates money before DSR/PBO
(step 4, `lab.evidence_gate`) and nothing is read by the monthly plan.

## Public surface (facade `app.lab.satellite`)

- `run_satellite_study(db, region=..., panel_dir=..., persist=...)` →
  `SatelliteStudy` (a `SatelliteCard` per broker for `ridge`, `gbm` and the
  `value_momentum` baseline, the monthly series, every review's trades), or
  None when step 2's scores file is missing. With `persist`, it records each
  model once per broker on the trial ledger (context `satellite`, key
  `<region>:<broker>:<model>`) and writes
  `<panel>/derived/satellite_<region>_results.json` (`results_path`, read by
  `lab.evidence_gate`).
- `pick`, `Review`: the buy and hold rules.
- `load_risk_free`, `parse_monthly_rf`: the monthly T-bill rate, cached as
  `<panel>/derived/ff_us_rf_monthly.parquet`.
- `core_tax_annual`: the core's deferred tax as a yearly drag.
- CLI: `python -m app.lab.satellite [--region world|europe] [--dry-run]`.

## Collaborators

- Out: `lab.pooled_model` (`scores_path`), `lab.quant_lab` (`run_lab_backtest`
  in `rebalance="trades"` mode with `offset_losses`, `CostModel`),
  `foundation.data_engineering.panel_sql` (DuckDB), `foundation.quant_metrics`
  (`newey_west_t_stat`, `record_trial`), Ken French's data library over HTTPS.

## Invariants

- `spec.py` is fixed before any result is seen; a change after a run is a new trial.
- Only stocks some score could buy are loaded (the `2 * positions` best
  eligible per review month, across all months), so memory does not grow with
  the universe.
- Costs and taxes come from `quant_lab` (`CostModel`, `estimate_tax_drag`,
  FIFO lots). This package adds no second tax model.
