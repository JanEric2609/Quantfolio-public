# Context: Graduation

## Responsibility

The capital ladder (ADR 0025, `ladder.py`): how much of the real book the
satellite (single stocks) may hold, given a candidate's paper record against
MSCI World. Two slots (`llm` = advisor champion lineage, `rules` = Mandate B),
epochs with pi_e = 1/2 / e^2, skill and harm betting e-processes, stages
Paper 0 % / Tuition w1 / Earned 10 % / Proven 15-20 %. Nightly job
`ladder_evidence` (`jobs.py`, 06:45 UTC). The read side (parameters, weights,
which candidate funds the satellite) lives in `foundation/capital_ladder.py`
so `decision.monthly_plan` can read it without importing this package.
The old criteria (`evaluator.py`) are display only, marked `legacy`; its
DSR/PBO/MTRL checks moved to `lab.evidence_gate.admission`. `state.py` keeps
the one switch (`AdvisorGraduationState`), written by the ladder. Advisory
only: nothing here executes trades.

## Public surface (facade `app.decision.graduation`)

`GraduationConfig`, `GraduationCriterion`, `GraduationResult`,
`apply_graduation_state`, `evaluate_graduation`, `is_graduated`,
`run_ladder`, `ladder_state`, `ladder_weight`, `ladder_funding`.

## Key collaborators

- In: `advisor.recommendations` (reports is_graduated), `app.interface.api.graduation`
  (`/status`, `/ladder`), `app.worker` (ladder job).
- Out: `advisor.strategy.get_champion` (evaluator/state; lazy imports, seeded);
  the ladder reads `AdvisorStrategy` and `PaperPortfolio` entities directly.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- "Global layering" applies (the four-layer spine subsumes the retired
  "Foundation services never import the decision loop" contract).
- "Decision-loop internals are facade-only: graduation": one seeded row is
  left (`advisor.recommendations -> graduation.state`); the two
  `advisor.jobs -> graduation.*` rows went with ADR 0025.

## Owner-wave notes

Decision-loop context for the paper-to-real step (ladder + legacy evaluator + state). Safety invariant:
advisory-only output, consistent with the repo-wide no-live-trading
constraint. Debt = the seeded rows; they shrink when advisor consumes
graduation via the package root.
