# Context: Portfolio Advisor

## Responsibility

On-demand LLM portfolio analysis reports in two depths: a fast pulse check
(pulse) and a full deep analysis with pluggable LLM backend (deep), sharing
analysis plumbing (analyzer). Registered as scheduled jobs by app.worker;
served to the UI via app.interface.api.portfolio_advisor.

## Public surface

Documented in the package docstring; deliberately NO eager re-exports:

- `pulse.run_pulse_check`
- `deep.run_deep_analysis`
- `deep.make_llm_func`

Consumers import these submodules directly until the constraint below lifts.

## Key collaborators

- In: `app.interface.api.portfolio_advisor`, `app.worker`.
- Out: `gap_analysis`, `llm.router` (from deep).

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Not covered by the Phase-F facade-only bans (not a decision-loop context).
- Cycle-ledger constraint: the package root must stay import-free. Any eager
  root import creates an `app.services -> portfolio_advisor` edge in the
  cycle ledger's package aggregation which, combined with the pre-existing
  `deep -> llm.router` edge, welds this context into the golden services
  tangle as a new package SCC.

## Owner-wave notes

Phase F wrote the documentation-only facade. If the golden services tangle
ever dissolves (Wave 5+ api layering / engine split), add eager re-exports
of the three surface names and switch consumers to the root.
