# Context: Graduation

## Responsibility

Paper-to-real promotion gate. Decides when the LLM has statistically proven
skill on the paper portfolio and may surface recommendations for the user's
real portfolio. Combines Deflated Sharpe Ratio / Minimum Track Record Length
(evaluator) with practitioner criteria — out-of-sample consistency, drawdown
ceiling, minimum decision sample, learning maturity — and persists verdicts
(state). Deliberately conservative: nothing here executes trades; a
"graduated" verdict only unlocked advisory recommendations, and since report
Phase 4 (2026-09-25) not even those: the LLM loop is paper-only, its
what-would-change diff is always research (`actionable: False`), and only the
monthly plan sets weights for the real book, from passing evidence cards.

## Public surface (facade `app.decision.graduation`)

`GraduationConfig`, `GraduationCriterion`, `GraduationResult`,
`apply_graduation_state`, `evaluate_graduation`, `is_graduated`.

## Key collaborators

- In: `advisor.jobs` (post-evolution evaluation), `advisor.recommendations`
  (reports is_graduated; no longer a gate), `app.interface.api.graduation`.
- Out: `advisor.strategy.get_champion` (strategy metrics input; lazy imports,
  seeded).

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- "Global layering" applies (the four-layer spine subsumes the retired
  "Foundation services never import the decision loop" contract).
- "Decision-loop internals are facade-only: graduation": seeded rows cover
  the three advisor reads above.

## Owner-wave notes

Smallest decision-loop context (evaluator + state only). Safety invariant:
advisory-only output, consistent with the repo-wide no-live-trading
constraint. Debt = the three seeded rows; they shrink when advisor consumes
graduation via the package root.
