# 0011 — Graduation Gate: `n_trials` Counts Strategy Variants, Not Reviews

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-08-31_implementation_decisions.md`
(Topic 7 of the consolidated implementation plan — gated behind a separate
underperformance root-cause investigation, completed in this same session)
**Consumed by:** `backend/app/services/graduation/evaluator.py`

## Context

`evaluate_graduation`'s statistical-skill criterion deflates the champion's
Sharpe ratio via the Deflated Sharpe Ratio (DSR, López de Prado) to correct
for multiple-testing bias in a best-of-N strategy selection. The DSR's
`n_trials` parameter is supposed to count the number of independent trials in
that search — i.e. how many distinct strategy/parameter variants competed for
the champion slot.

`_search_breadth()` previously computed `n_trials = max(2, len(portfolio_ids),
review_cycles + competition_decisions)`, where `review_cycles` is every
*completed mandate review* (a routine LLM-driven rebalancing of the
already-selected champion's existing sleeve) and `competition_decisions` is
every row from head-to-head champion/challenger competition rounds. On a
real account this makes `review_cycles` dominate the sum by an order of
magnitude or more — dozens of weekly rebalances vs. a handful of competition
rounds — which deflates the DSR far more aggressively than the actual search
breadth warrants and makes the gate practically unreachable regardless of
genuine skill.

López de Prado's own DSR papers define a "trial" as a distinct backtest
evaluated during strategy development/selection — explicitly not routine
review or rebalancing cycles of a strategy already in production. A mandate
review doesn't introduce a new candidate to select among; it re-optimises
the *same* champion strategy's weights. A competition round does introduce a
new candidate: it is a genuine best-of-two evaluation between the current
champion and a challenger variant, which is exactly what the DSR's trial
count is meant to discount for.

## Decision

**Drop `review_cycles` from the `n_trials` computation.** `_search_breadth()`
still counts and returns `review_cycles` (used in the criterion's `detail`
string for observability — an operator can still see how many reviews ran),
but only `competition_decisions` feeds the DSR:

```python
n_trials = max(2, len(portfolio_ids), competition_decisions)
```

The floor at `len(portfolio_ids)` and at 2 (the DSR's own minimum) is
unchanged — a user with only one paper portfolio and zero competition rounds
still gets `n_trials = 2`, the same conservative minimum as before this fix.

**DSR threshold (`cfg.min_dsr = 0.95`) is unchanged.** The plan explicitly
asked to fix one variable at a time — `n_trials` was the identified bug;
changing the pass bar in the same change would conflate two independent
decisions and make it impossible to attribute a graduation-status change to
either one specifically.

**Applied going forward only.** `evaluate_graduation` is a pure, live
computation over current DB state on every call — there is no persisted
historical `n_trials` value to retroactively recount. This fix changes what
the *next* evaluation computes; it does not and cannot rewrite past
evaluation results, satisfying the plan's "no retroactive recount" note
without any additional backfill code.

## Alternatives considered

**Count `review_cycles` at a discount factor (e.g. 0.1×) instead of dropping
it entirely.** Rejected: a discount factor is an arbitrary tuning knob with
no basis in the DSR literature, and picking one would just move the
unreachable-gate problem to a different number instead of fixing the
category error (reviews aren't trials at all, regardless of weight).

**Count competition rounds (`round_number`, deduplicated) instead of raw
`CompetitionDecision` rows.** Rejected for this pass: each `CompetitionDecision`
row is itself one candidate portfolio's evaluation within a round (currently
two rows per round, one per competing portfolio), so counting rows rather
than distinct rounds is at most a 2× difference and errs on the conservative
(more discounting) side — consistent with the plan's instruction to fix
`n_trials`'s dominant term (reviews) without opening a second design
question in the same change.

**Wait for real competition-round history before shipping.** Not chosen:
the fix is a pure formula correction with full unit coverage
(`tests/test_graduation.py::test_n_trials_reflects_search_breadth`), and
withholding it doesn't reduce risk — the current formula is actively wrong
in the more conservative direction (over-penalizing), so shipping the
correction promptly is lower risk than leaving it.

## Consequences

- A champion with zero competition rounds and one portfolio now computes
  `n_trials = 2` (the DSR floor) instead of `n_trials = review_cycles`
  (which was often 20-40+ on a live account) — meaningfully raising the
  computed DSR for accounts that have only ever run mandate reviews, never a
  champion/challenger competition.
- `n_review_cycles` remains in `evaluate_graduation`'s `metrics` dict and the
  criterion `detail` string unchanged in shape — no API contract break.
- This fix corrects the DSR's multiple-testing math; it does not address the
  separate, larger underperformance driver found during this session's
  root-cause investigation (systematic rotation out of the diversified core
  holding into concentrated single-name bets once the decision loop resumes
  functioning) — the graduation gate being statistically honest does not by
  itself mean the underlying strategy is good, which is why the plan gated
  this fix behind that investigation rather than treating them as the same
  problem.
