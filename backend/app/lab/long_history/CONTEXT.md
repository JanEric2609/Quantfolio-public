# Context: lab/long_history

## Responsibility

ADR 0022: the pre-registered test of the plan's asset-mix rules on the JST
Macrohistory panel (16 countries, 1870-2020), for a monthly saver, by a
stationary block bootstrap per home country. Plan 2026-10-14, WP-C.

## Public surface

`jst_loader.py` (`load_jst`, `home_panel`: real returns per home country:
global, home and international equity, home bonds, bills) and
`plan_bootstrap.py` (`RULES`, `PlanCosts`, `bootstrap_indices`,
`simulate_rule`, `certainty_equivalent`, `haircut`). The run is
`scripts/plan_rules_long_history.py`.

## Invariants

- The JST file is CC BY-NC-SA. It is read from `data/long_history/` (gitignored,
  excluded from the public export) and never committed; tests use synthetic data.
- The rule family, costs, metric and pass bar are ADR 0022's. Another rule is
  a new trial, recorded before it runs.
- Pure NumPy and pandas; no database, no network.

**Plan fan (D6).** `plan_fan.py` reuses the same construction for the owner's plan. It covers the
book's mix, the euro-area homes by default with all 16 next to them, and German tax on a full sale
at the horizon (nominal basis). It feeds:
- `GET /api/quant/portfolio/projection/long-history`;
- the `long_history` plan band of `lab/plan_bands`.

`load_histories()` returns None when the file is missing, and callers degrade.
