# Context: Tax Calc

## Responsibility

Pure jurisdiction tax estimators. German (DE) implementations are the main
exports — allowances (Sparer-Pauschbetrag, Soli, church tax, KEST),
FIFO cost-basis consumption, Teilfreistellung, Vorabpauschale, foreign-WHT
capping, loss buckets, the NV-certificate tax-free room and gain-harvest plan — maintained as shims over `jurisdictions.de`; NL Box 3
support lives under `jurisdictions.nl`. All amounts EUR; all outputs are
estimates.

## Public surface (facade `app.foundation.tax_calc`)

`BASISZINS_BY_YEAR`, `FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR`,
`FUTURE_TAX_RATE`, `GRUNDFREIBETRAG_BY_YEAR`, `HarvestStep`, `Holding`,
`OpenLot`, `Room`, `plan_gain_harvest`, `tax_free_room`, `FifoSale`, `KEST_RATE`,
`SPARER_PAUSCHBETRAG_SINGLE`, `SPARER_PAUSCHBETRAG_SPOUSE`,
`apply_sparer_pauschbetrag`, `apply_teilfreistellung`, `calculate_box3`,
`cap_foreign_wht`, `classify_loss_bucket`, `classify_wealth_by_bucket`,
`fifo_consume`, `kirchensteuer_amount`, `reduced_kest_amount`,
`soli_amount`, `teilfreistellung_pct_for_fund_class`,
`vorabpauschale_estimate`.

## Key collaborators

- In: `tax_cockpit` (orchestrator), `app.interface.api.tax` (BASISZINS_BY_YEAR).
- Out: none cross-context — pure functions over models/core only.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Global layering holds (interface → decision → lab → foundation).
- API responses built from these outputs must carry `estimate: True` and
  `not_tax_advice: True`; broker statements are the source of truth.

## Owner-wave notes

Annual maintenance: update `BASISZINS_BY_YEAR` each January from the BMF
publication (§18 InvStG 2018). Teilfreistellung rates are statutory
(§20 InvStG: aktien 30%, misch 15%, immobilien 60%). Also yearly:
`GRUNDFREIBETRAG_BY_YEAR` and `FAMILIENVERSICHERUNG_MONTHLY_LIMIT_BY_YEAR`
(`jurisdictions/de/gain_harvest.py`). See docs/tax-cockpit.md.
