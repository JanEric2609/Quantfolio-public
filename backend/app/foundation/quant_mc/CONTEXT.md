# Context: Quant MC

## Responsibility

The canonical Monte Carlo engine (ADR 0003 decision 2: "`quant_mc` is the
only Monte Carlo engine" — no other module in the codebase should implement
its own path simulation). Standard MC + variance reduction + MLMC + Greeks.

- `engine.py::MonteCarloEngine` — the facade entry point, orchestrating path
  simulation + variance reduction + Greeks + storage from a single `McSpec`.
- `paths.py` — path simulators (`GBMEuler`, `GBMMilstein`, `HestonEuler`,
  `OrnsteinUhlenbeckEuler`, `MertonJumpDiffusionEuler`, `CIREuler`,
  `create_simulator` factory).
- `distributions.py` — `NormalDistribution`/`StudentTDistribution`/
  `NIGDistribution` for innovations.
- `quasi_mc.py::SobolGenerator`/`generate_sobol_paths` — quasi-random
  low-discrepancy sequences for variance reduction.
- `variance_reduction.py` / `mlmc.py` — antithetic/control-variate
  techniques and Multilevel Monte Carlo (Giles 2008: `E[P] = E[P_0] +
  sum_l E[P_l - P_{l-1}]` across a discretization-level hierarchy) for
  efficient estimation.
- `greeks.py` — pathwise IPA and likelihood-ratio Greeks.
- `payoffs.py` — option payoff functions.
- `calibration.py` — `calibrate_gbm`/`calibrate_heston`/`calibrate_merton`
  fit model parameters from historical data.
- `projection.py` — fan-chart portfolio projection; the ADR-0003-sanctioned
  replacement for a retired pure-Python GBM loop that used to live in
  `quant.py`, composing the same `create_simulator`/`create_distribution`
  primitives the engine facade uses instead of a parallel implementation.
- `storage.py` — formats/downsamples large path matrices for storage
  (`downsample_paths`, `format_paths_for_storage`, `compute_path_summary`);
  does not touch the DB itself — callers persist the formatted result.

## Public surface (facade `app.foundation.quant_mc`)

`MonteCarloEngine`, `McSpec`, `McResult`, `Distribution`,
`NormalDistribution`, `StudentTDistribution`, `NIGDistribution`, `GBMEuler`,
`GBMMilstein`, `HestonEuler`, `OrnsteinUhlenbeckEuler`,
`MertonJumpDiffusionEuler`, `CIREuler`, `create_simulator`, `SobolGenerator`,
`generate_sobol_paths`, `GBMParams`, `HestonParams`, `MertonParams`,
`calibrate_gbm`, `calibrate_heston`, `calibrate_merton`.

## Key collaborators

- In: `app.foundation.quant_proposal` (MC-calibrated forward-return summaries
  for the advisor/llm_portfolio trade proposal), `app.foundation.return_band`,
  `app.interface.api.{quant_mc,quant.goals,quant.portfolio,quant.risk}`.
- Out: none — zero `app.*` imports anywhere in this package (confirmed by
  inspection); pure numpy/scipy/stdlib.

## Contract invariants

- Not a member of any import-linter contract (foundation source_modules,
  decision-loop independence, or any facade-only list) — sits outside every
  list. In practice the most foundation-tier of all eight packages backfilled
  in Track F: it has zero internal dependencies, only external ones.
- Global layering (interface → decision → lab → foundation) holds trivially — nothing to
  violate it with.

## Owner-wave notes

The "only MC engine" invariant (ADR 0003 decision 2) is the load-bearing
fact about this package: before adding Monte Carlo logic anywhere else in
the codebase, check whether it belongs here instead. `quant_proposal.py`
(the foundation-tier trade-proposal builder consumed by both `advisor` and
`llm_portfolio`) is this package's most consequential caller — its
MC-calibrated forward-return distributions feed directly into paper-trading
decisions.
