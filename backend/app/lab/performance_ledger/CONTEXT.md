# Context: Performance Ledger

## Responsibility

GIPS-style performance measurement: time/money-weighted returns, composite
portfolio grouping, dispersion, and ex-post risk, persisted as a ledger.

- `composites.py` — user-defined composite (portfolio grouping) CRUD:
  `create_composite`, `add_portfolio_to_composite`/`remove_portfolio_from_composite`,
  `get_composite_members`.
- `twr.py::time_weighted_return` — TWR, independent of external cashflows
  (manager-performance measure).
- `composite_twr.py::composite_time_weighted_return` — TWR across a
  composite that reweights correctly when a member portfolio joins/leaves
  mid-period, instead of booking the joining portfolio's whole market value
  as an instant gain (the naive summed-TWR bug this module exists to avoid).
- `mwr.py::money_weighted_return`/`money_weighted_return_irr` — MWR/IRR,
  accounting for the timing and size of external cashflows (actual investor
  experience, as opposed to TWR's manager-performance framing).
- `dispersion.py::asset_weighted_dispersion` — variation across a
  composite's member portfolios (`DispersionMetrics`).
- `ex_post_risk.py::calculate_ex_post_risk` — realised risk metrics via
  `quant_metrics` (`ExPostRiskMetrics`).
- `ledger_writer.py` — persists snapshots (including ex-post risk) to
  `PerformanceLedgerEntry`.

## Public surface (facade `app.lab.performance_ledger`)

`create_composite`, `list_composites`, `ensure_default_composite`,
`get_composite`, `add_portfolio_to_composite`, `remove_portfolio_from_composite`,
`get_composite_members`, `time_weighted_return`, `TWRPeriod`,
`composite_time_weighted_return`, `money_weighted_return`,
`money_weighted_return_irr`, `asset_weighted_dispersion`, `DispersionMetrics`,
`calculate_ex_post_risk`, `ex_post_risk_to_dict`, `ExPostRiskMetrics`.

## Key collaborators

- In: `app.interface.api.performance` — sole cross-context caller.
- Out: `app.foundation.quant_metrics` (ex-post risk), `app.foundation.models.entities`
  (`Composite`, `CompositeMembership`, `Portfolio`, `PerformanceLedgerEntry`).

## Contract invariants

- Not a member of any import-linter contract (foundation source_modules,
  decision-loop independence, or any facade-only list) — sits outside every
  list, so edges into/out of it aren't checked by any contract.
- Global layering (interface → decision → lab → foundation) holds by inspection.

## Owner-wave notes

Single API-layer consumer and no decision-loop entanglement — the GIPS
performance-reporting analogue of `attribution`'s isolation. The
composite-reweighting logic in `composite_twr.py` is the one genuinely
tricky piece here (documented inline as "P5") — treat any change to
membership-change handling as high-risk regardless of the package's small
size, since a silent regression there produces a plausible-looking but
wrong composite return rather than an obvious error.
