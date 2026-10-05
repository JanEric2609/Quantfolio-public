# Context: lab.factor_premia

## Responsibility

The evidence behind the factor tilt (docs/archive/audits/2026-09-24-why-it-does-not-work,
Phase 3, step 1). Builds monthly tercile portfolios for a fixed, pre-registered
set of documented premia (value, momentum, profitability, low investment,
value + momentum) from the JKP characteristics panel, within country and month.
Two regions: `world` (MSCI World's 23 markets, the default) and `europe` (MSCI
Europe's 15). Only `world` cards gate the monthly plan's tilt
(`foundation.factor_evidence.TILT_EVIDENCE_REGION`), because the tilt would be
held through a world factor ETF; `europe` is shown for comparison. Grades each with a prior-informed test:
enough history, Newey-West t ≥ 2, still positive after publication, and a
positive long-only premium after decay, factor-ETF costs and German tax.

## Public surface (facade `app.lab.factor_premia`)

- `run_study(db, region=..., panel_dir=..., persist=...)` → `EvidenceCard`s;
  stores one `FactorEvidenceCard` row per strategy and records each strategy
  once on the global trial ledger (context `factor_premia`).
- `latest_cards(db)` (each region's latest run, world first), `factor_series(panel_dir, countries)`, `evaluate(...)`.
- CLI: `python -m app.lab.factor_premia [--region world|europe] [--dry-run]`.

## Collaborators

- In: `app.interface.api.evidence` (`GET /api/evidence/factor-premia`).
  The monthly plan (`app.decision.monthly_plan`) reads the stored cards through
  `app.foundation.factor_evidence`, not through this package.
- Out: `app.foundation.data_engineering.panel_sql` (DuckDB over the Parquet
  panel, bounded by the shared `memory_limit`), `foundation.quant_metrics`
  (`newey_west_t_stat`, `record_trial`), `foundation.models`.

## Invariants

- The strategy set and the pass rules (`strategies.py`) are fixed before any
  result is seen. Changing them after a run is a new trial, not a correction.
  The `world` region was added (2026-09-24) after a first Europe dry run, to
  match the vehicle; each region-strategy pair is its own ledger trial, so the
  ledger counts both.
- Aggregation happens in DuckDB; no full-panel pandas frame is materialised.
