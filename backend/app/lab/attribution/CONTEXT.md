# Context: Attribution

## Responsibility

Portfolio return-attribution analysis: decomposes active return (portfolio
vs. benchmark) into interpretable pieces and persists the result.

- `brinson.py::brinson_fachler` — Brinson-Fachler allocation/selection/
  interaction decomposition, per-security (`SecurityAttribution`).
- `factor_attrib.py::factor_attribution` — Fama-French factor-based
  decomposition (exposure × factor return = contribution), via
  `app.foundation.quant_factors`.
- `contributors.py::top_contributors` — ranks a Brinson result's
  `SecurityAttribution` list into top contributors/detractors
  (`Contributor`), pure post-processing, no I/O.
- `storage.py::store_attribution_run` — persists a run's `result_dict` (JSON)
  to the `AttributionRun` entity; the only DB-touching module here.

## Public surface (facade `app.lab.attribution`)

`brinson_fachler`, `BrinsonResult`, `SecurityAttribution`,
`factor_attribution`, `FactorAttributionResult`, `FactorContribution`,
`top_contributors`, `Contributor`, `store_attribution_run`.

## Key collaborators

- In: `app.interface.api.attribution` — sole cross-context caller.
- Out: `app.foundation.quant_factors` (Fama-French factor data, foundation-tier
  flat module), `app.foundation.models.entities.AttributionRun`.

## Contract invariants

- Not a member of any import-linter contract (foundation source_modules,
  decision-loop independence, or any facade-only list) — sits outside every
  list, so edges into/out of it aren't checked by any contract.
- Global layering (interface → decision → lab → foundation) holds by inspection: no imports
  beyond `app.foundation.quant_factors` and `app.foundation.models.entities`.

## Owner-wave notes

Single consumer (`app.interface.api.attribution`) and no decision-loop entanglement —
the lowest-risk of the eight undocumented sub-packages backfilled in Track F
of the 2026-08-27 audit. `brinson.py`/`contributors.py` are pure
dataclass/computation modules with zero imports beyond stdlib and each
other; only `factor_attrib.py` and `storage.py` reach outside the package.
