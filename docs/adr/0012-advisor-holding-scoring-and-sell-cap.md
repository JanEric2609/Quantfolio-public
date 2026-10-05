# 0012 — Advisor Loop: Holdings Compete as Candidates, Per-Position Sell Cap

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-09-01_underperformance_fix_decisions.md`
(root-cause investigation gated behind Topic 7 of
`project_quantfolio_2026-08-31_implementation_decisions.md`; grilled over 8
questions across 2 rounds)
**Consumed by:** `backend/app/services/advisor/cycle.py`,
`backend/app/services/advisor/llm_decision.py`

## Context

A live-read investigation (`mcp__postgres__query` against the champion paper
portfolio, seeded with IWDA.L as its core holding on 2026-07-21) found
two compounding effects behind its underperformance:

1. **2026-07-21 to 2026-08-07**: the advisor's own decision loop
   (`advisor/llm_decision.py`) failed JSON parsing on 30 of 34 attempts. The
   portfolio sat frozen near its inherited allocation — accidental protection,
   not skill.
2. **From 2026-08-11 on**: every successful decision cycle rotated capital
   *out* of IWDA.L (the diversified core holding) *into* a rotating set of
   concentrated single-country EU stocks (IBE.MC, BBVA.MC, REP.MC, UCG.MI,
   ENR.DE — the same names flagged in a prior discover-diversity audit). Each
   rotation was justified by a rosy 21-day Monte-Carlo forecast for the *new*
   name; the first rebalance alone spent ~100% of the daily turnover budget
   in one shot. By 2026-09-01 the book had drifted to ~48% IWDA.L against ~7
   satellite names that missed the rally IWDA.L itself captured.

This is a documented failure class, not a one-off: LLM trading agents are
shown to overtrade and underperform passive benchmarks in bull markets
([arXiv:2505.07078](https://arxiv.org/abs/2505.07078)), and to reproduce the
human disposition effect — selling winners early, holding losers — even
controlling for forward-looking beliefs
([arXiv:2604.18373](https://arxiv.org/pdf/2604.18373)).

Code-level tracing during implementation found the *diagnosis* correct at the
symptom level but not fully precise at the mechanism level:
`quant_proposal.build_trade_proposal` already unions current holdings with new
candidates and runs the skfolio optimizer over both together, and
`llm_decision._build_user_prompt` already presents every holding in the same
`candidates` block as new names, with its own Monte-Carlo forward-return
summary. A holding was never *structurally* absent as a candidate. What was
missing was (a) a hard backstop bounding how much of any one position a single
cycle could liquidate, regardless of what the model or the optimizer's funding
logic decided, and (b) prompt language making explicit that a held name's own
forecast — not the optimizer's diversification target alone — is what should
justify selling it.

The memory's decision location (`advisor/context.py`) does not exist in the
current codebase; the analogous code lives in `advisor/llm_decision.py`
(prompt construction, `_build_user_prompt`) and `advisor/cycle.py` (trade
sizing and execution, `_plan_decision` / `_raise_cash`).

## Decision

**1. Per-position sell cap (the primary code change).** `cycle.py` gains
`DEFAULT_MAX_SELL_PCT_OF_POSITION = 1/3`, applied in two places:

- `_plan_decision`: a model-directed sell is clamped to at most 1/3 of the
  position's current value, regardless of how far the requested
  `target_weight` is below the current weight.
- `_raise_cash`: the deterministic funding-trim loop (which sells overweight
  positions to pay for a buy the model can't otherwise afford) is clamped by
  the same cap per symbol, on top of its existing per-symbol excess-over-target
  and per-cycle budget limits.

Both call sites take `max_sell_pct_of_position` as a parameter (default
`DEFAULT_MAX_SELL_PCT_OF_POSITION`), resolved once per cycle from
`config.get("max_sell_pct_of_position", ...)` in `run_advisor_cycle` alongside
the existing `max_turnover_pct` / `min_ticket_pct` config reads, and recorded
in the cycle's audit trail (`trade_flow.max_sell_pct_of_position`) for
observability. This is orthogonal to the aggregate turnover budget: a single
concentrated position could previously be fully drained in one cycle while
still fitting comfortably inside the overall turnover cap, because the budget
bounds *gross* notional, not *concentration* of the sell into one name.

**2. Holdings framed explicitly as competing candidates (reinforcement, not a
new data path).** `llm_decision._SYSTEM_PROMPT` gains language stating that
every held position competes on its own Monte-Carlo forecast exactly like a
new candidate, that `sell_candidates` is what the optimizer's diversification
target implies is overweight rather than an instruction to sell, and that a
sell is capped at a new `max_sellable_eur` field this cycle. `_build_user_prompt`
gains a `max_sell_pct_of_position` parameter; when passed, it computes
`max_sellable_eur = held_eur * max_sell_pct_of_position` for every held
candidate entry and for every `sell_candidates` row, making the cap in (1)
legible to the model rather than a value it discovers only by having its
request silently clamped downstream. `decide_trades` threads the parameter
through to `_build_user_prompt`; `cycle.py`'s call site passes the same
per-cycle value used for enforcement, so the number shown to the model always
matches what the executor will actually allow.

This is additive to, not a replacement for, the pre-existing candidate
scoring: `quant_proposal.py`'s holdings-plus-candidates union and the
per-holding Monte-Carlo/`suggested_weight` fields already in the prompt are
unchanged. The gap this closes is prompt *framing* — making the cap and the
non-mandatory nature of `sell_candidates` explicit — layered on top of data
that was already present.

**3. JSON retry — confirmed already shipped, no new code.** `decide_trades`
already retries a parse failure with a correction nudge
(`_MAX_JSON_RETRIES = 2`, `build_retry_messages`), matching the pattern
Topic 3 added for the mandate loop. The 30/34 failure rate observed
2026-07-21–2026-08-07 predates this mechanism; no further change is needed
here. This is noted rather than re-implemented, to avoid duplicating existing
retry logic.

## Alternatives considered

**Hurdle rate on sell-to-fund (require a minimum forecast edge before selling
a holding to fund a buy).** Not chosen — deferred pending live observation of
whether the sell cap and prompt framing are sufficient on their own; a hurdle
rate is a second independent knob and the grilling explicitly wanted one
variable changed at a time.

**Benchmark-relative tracking as a first-class metric** (per CLQT/OpenPM,
which score excess return / information ratio vs. a passive index rather than
absolute return alone). Not chosen for this pass — a larger scoring-model
change than the immediate fix warrants; absolute-return-only scoring not
penalizing drift from a benchmark-tracking position is a real gap but a
separate design question from bounding single-cycle liquidation.

**Special-casing IWDA.L (or "the largest holding") instead of a generic
per-position cap.** Rejected per the grilling: the cap applies uniformly to
every held position so a future core holding doesn't reproduce the same
failure mode under a different ticker.

**Re-implementing the JSON retry.** Rejected after finding it already present
and unit-tested (`tests/test_advisor_llm_truncation.py`,
`tests/test_advisor_sell_side.py`) — the observed failure window predates this
code, and duplicating it would add no coverage.

## Consequences

- A model (or the deterministic funding-trim logic) that wants to fully exit a
  position now takes at least 3 cycles to do so (a 1/3 cap compounds:
  ~1/3, ~4/9, ~8/27, ... of the *original* value sold per cycle if repeated,
  since each cap is computed against the *current* value at the start of that
  cycle) instead of one. This directly prevents the observed pattern of one
  rebalance spending ~100% of the turnover budget liquidating a single
  concentrated position.
- The cap is config-overridable per portfolio/strategy
  (`max_sell_pct_of_position` in the advisor `config` dict) with the same
  override mechanism as `max_turnover_pct` and `min_ticket_pct`, so a
  strategy that legitimately needs faster exits is not permanently blocked.
- `max_sellable_eur` is a purely advisory prompt field — the model can still
  request a larger sell, but `_plan_decision`/`_raise_cash` clamp it
  regardless, so the cap holds even if the model or prompt framing is ignored.
- No change to the risk gate (C2), the optimizer's target weights, or the
  discover pipeline — the fix is scoped entirely to the advisor's own decision
  and execution layer (C3 prompt + C1/C3 sizing), per the grilling's location
  decision.
- Historical replay against the champion's actual Jul–Aug decision history
  (`mcp__postgres__query` against `paper_trades`/`paper_holdings` for
  portfolio `be80b6c7-8d90-48b7-b963-a1337c594846`) was run before trusting
  this live, per the grilling's explicit requirement. **Finding: the
  per-position cap alone would not have altered this specific historical
  drift.** IWDA.L's actual sells were nine trades from 2026-08-11 to
  2026-09-01, each 2.7%–8.8% of the position as it stood then (one outlier at
  ~15% near the end as the position shrank) — every single trade was already
  well inside the 1/3 cap, because the *aggregate* 15%-of-NAV daily turnover
  budget was independently binding first and kept each day's IWDA.L leg small.
  The observed failure was not one cycle liquidating a third or more of the
  position in one shot (which is the pattern the cap was designed to catch,
  per the grilling's own framing of "one rebalance fully liquidating an
  overweight in a single shot"); it was ~15 small, individually
  turnover-compliant trims compounding over three weeks into a ~57% reduction of
  the position. The per-position cap is still correct to ship —
  it is a real backstop against the single-shot liquidation pattern the
  grilling named, config-overridable, and covered by
  `tests/test_advisor_trade_flow.py`'s end-to-end regression — but it is not,
  by itself, sufficient evidence that this specific historical drift would
  not recur. The prompt-reframing change (item 2: holdings compete on their
  own forecast, `sell_candidates` is not an instruction) is the piece actually
  aimed at the repeated-cycle mechanism, and it is a nudge, not a structural
  guarantee — nothing currently stops a model from repeatedly deciding, cycle
  after cycle, that a held name's own forecast justifies trimming it further.
  **Follow-up flagged, not implemented here**: if live monitoring after this
  fix still shows multi-cycle rotation draining a core holding, the next
  increment should be a *cumulative* sell-rate cap over a trailing window
  (e.g. a position may not lose more than some share of its value across the
  last N trading days, not just in the current cycle), rather than assuming
  the per-cycle cap or the prompt nudge alone closes this gap.
