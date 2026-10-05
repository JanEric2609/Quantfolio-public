# Context: Advisor

## Responsibility

Autonomous paper-trade decision loop. Builds Monte-Carlo forward return
distributions and skfolio portfolio weights (decision — now a thin re-export
shim over the foundation-tier `app.foundation.quant_proposal`, which holds the
actual builder so `llm_portfolio` can depend on it too without a cycle),
enforces hard VaR/CVaR/max-drawdown ceilings (risk_gate), lets a thin LLM
wrapper choose trades within the gate-passing set (llm_decision), and runs
the daily paper-trade cycle (cycle) over champion/challenger strategy state
(strategy) with self-critique lessons (reflection), evolution rounds
(evolution — each round now also runs the real Multi-Agent Council via the
`llm_portfolio` facade to populate `CompetitionDecision.council_result_json`;
promotion/scoring itself stays driven entirely by `AdvisorScorecard`, not the
council), per-cohort scorecards (scorecard), diagnostics, user-facing
recommendations, and scheduler registrations (jobs).

## Public surface (facade `app.decision.advisor`)

`ADVISOR_MANDATE`, `EVOLUTION_RUN_NAME`, `composite_score`,
`compute_what_would_change`, `get_active_challenger`, `get_champion`,
`next_resolution_at`, `register_advisor_cycle_job`, `register_evolution_job`,
`run_advisor_cycle`, `run_advisor_diagnostics`, `run_evolution_round`.

## Key collaborators

- In: `discover.jobs` (scorecard refresh), `graduation.evaluator` /
  `graduation.state` (champion read), `app.interface.api.advisor`, `app.worker`.
- Out: `paper_portfolio` (root facade — trade execution, summaries, seeding),
  `llm_portfolio.review._local_llm_sync`, `discover.{calibrator,ledger,
  predictor,framings}` — all seeded Wave-F debt, see below. `evolution` also
  imports the `llm_portfolio` root facade (`build_council_context`,
  `CompetitionOrchestrator`, `create_default_config`) to run the Multi-Agent
  Council per round — seeded, same direction as the pre-existing
  `_local_llm_sync` edges, no cycle (`llm_portfolio.agents` has no imports
  back into `advisor`). `decision.py`'s contents live in
  `app.foundation.quant_proposal` now; `decision.py` is a re-export shim.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- "Global layering" bans any `app.foundation` or `app.lab` module from
  importing `app.decision.advisor` (this subsumes the retired "Foundation
  services never import the decision loop" contract).
- "Decision-loop internals are facade-only: advisor" (forbidden, direct-only):
  cross-context consumers must use the package root. Seeded rows: discover.jobs
  → scorecard; graduation.{evaluator,state} → strategy.

## Owner-wave notes

Wave 4 cut the six-way tangle; Phase E relocated the evolution registrar from
jobs.py into advisor/jobs.py. Remaining debt = the three seeded deep-import
rows above plus the underscore reach-ins (`_local_llm_sync`) consumed from
llm_portfolio.review; migrate to facades when llm_portfolio exposes the LLM
sync seam publicly.
