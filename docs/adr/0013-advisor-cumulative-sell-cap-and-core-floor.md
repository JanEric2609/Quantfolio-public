# 0013 — Advisor Loop: Trailing-Window Cumulative Sell Cap + Core-Holding Floor

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** [[ADR 0012]]'s own "Consequences" section flagged this as
an explicit, not-yet-implemented follow-up; memory
`project_quantfolio_2026-09-01_underperformance_fix_decisions.md`; a fresh
literature pass (four parallel Haiku research agents, arXiv/web) plus a
two-round AskUserQuestion grilling (7 questions) on this specific fix.
**Consumed by:** `backend/app/services/advisor/cycle.py`,
`backend/app/services/advisor/llm_decision.py`

## Context

ADR 0012 shipped a per-cycle sell cap (`DEFAULT_MAX_SELL_PCT_OF_POSITION =
1/3`) and validated it via historical replay against the champion sleeve's
actual IWDA.L trade history (portfolio `be80b6c7-8d90-48b7-b963-a1337c594846`,
2026-08-11 to 2026-09-01). That replay found the per-cycle cap alone
insufficient: every one of the nine actual IWDA.L sells (2.7%–8.8% of the
position each) was already well inside the 1/3-per-cycle limit,
because the sleeve's aggregate 15%-of-NAV daily turnover budget was
independently binding first and kept each day's IWDA.L leg small. The failure
was not one cycle liquidating a third of the position at once — it was ~15
small, individually turnover-compliant trims compounding over three weeks
into a ~57% reduction of the position. ADR 0012 flagged a cumulative
or rolling-window sell-rate cap as the next increment if this recurred, but
did not implement it.

**Literature review** (four parallel research agents, cross-checked by
directly fetching four of the load-bearing arXiv abstracts): rolling/cumulative
trading-rate constraints are an established, independently-arrived-at pattern
across turnover-constrained rebalancing, RL portfolio management, and
circuit-breaker literature — not an ad hoc invention for this fix. Relevant,
verified sources: arXiv:2604.18373 (LLM agents reproduce the disposition
effect; prompt-only mitigation is insufficient on its own — confirms ADR
0012's prompt-reframing piece needed a structural backstop, not a
replacement), arXiv:2510.06466 (RL portfolio management reward-shapes
turnover cost over a rolling window), arXiv:2512.22109 (low-turnover index
tracking — cited independently by two research agents), arXiv:2601.08721
(core-satellite portfolios with a structural "optionality budget" bounding
satellite size and turnover without return forecasts). Numeric specifics some
agents proposed (exact window lengths, basis-point costs) were not
independently verified against the abstracts and were treated as directional
suggestions, not fact, going into the grilling below.

## Decision

**1. Trailing-window cumulative sell cap (primary change).**
`cycle.py` gains:

- `DEFAULT_MAX_CUMULATIVE_SELL_PCT = 1/3` — same ratio as the per-cycle cap,
  so there is one mental model: no more than a third of a position's value
  gone, whether in one cycle or across the whole window.
- `DEFAULT_CUMULATIVE_SELL_WINDOW_DAYS = 28` — calendar days (no
  trading-calendar dependency exists elsewhere in this codebase), approximating
  the ~20-trading-day window decided in the grilling.

`_cumulative_sell_totals(db, portfolio_id, window_days)` sums each ticker's
sell and buy notional from `PaperTrade` over the trailing window. A
position's **window-start value** is reconstructed as
`current_value + sells_in_window − buys_in_window` — there is no daily
per-holding price snapshot to read a true historical value from (only
portfolio-level `PaperSnapshot` exists), so this is a value-based
approximation that ignores price drift within the window, in keeping with
the per-cycle cap's own use of "current value" as its baseline. The window
baseline does **not** reset on an interim buy (a fixed rolling
window-days-ago snapshot): a small top-up purchase must not be able to reset
the erosion clock and re-open room to keep trimming.

`_effective_sell_cap(...)` folds this into one per-ticker number together
with the per-cycle cap (and the floor below), taking the tightest of the
three. It is computed **once per cycle**, not per decision, into a `sell_caps:
dict[ticker, float]` passed to `_plan_decision`, `_raise_cash`, and
`decide_trades` alike — so the number enforced by the executor and the number
shown to the model (`max_sellable_eur`) are always the same value, per ADR
0012's own principle.

**Scope: uniform across every holding**, not just diversified/index ones —
same rule as ADR 0012's per-cycle cap, so a future core position under a
different ticker gets the same protection automatically.

**2. Core-holding floor (bundled defense-in-depth, per the grilling).**
`DEFAULT_CORE_HOLDING_FLOOR_PCT = 0.20`: a position whose `instrument_type ==
"etf"` (the existing classification already computed in
`quant_proposal.TradeProposal.instrument_types`, no new classifier needed)
may never be sold below 20% of NAV, folded into the same `_effective_sell_cap`
as a third, hard-block term — **no override path**, unlike the model-directed
cap which the model can request past (and be silently clamped). Scoped only
to ETF/index holdings, not single-stock satellite positions: a satellite pick
that has lost conviction is meant to be fully exitable, and floor-protecting
an arbitrary ticker would reintroduce exactly the kind of special-casing ADR
0012 explicitly rejected for the per-cycle cap — the floor's justification is
specific to protecting conviction-free diversification, not to any one
ticker.

This is wealth/composition-based (protects a position's *share of NAV*)
where the cumulative cap is velocity-based (protects the *rate* at which a
position may shrink) — the research (arXiv:1305.6831, arXiv:2601.08721)
frames these as complementary layers, not substitutes, which is why both
ship together rather than the floor being deferred as a second increment.

**3. Prompt update.** `_SYSTEM_PROMPT` gains one sentence stating that
`max_sellable_eur` also folds in the trailing-window cumulative limit and
(for a diversified/index holding) a floor, so "repeatedly trimming a name a
little each cycle does not evade either limit" is explicit to the model, not
just enforced silently downstream.

**4. Config-overridable**, same mechanism as `max_turnover_pct` /
`max_sell_pct_of_position`: `max_cumulative_sell_pct`,
`cumulative_sell_window_days`, `core_holding_floor_pct` in the advisor
`config` dict, recorded in the cycle's audit trail
(`trade_flow.max_cumulative_sell_pct` etc.) for observability.

## Historical replay (per ADR 0012's own required validation step)

Replayed against the exact same nine IWDA.L sells used in ADR 0012's replay
(`mcp__postgres__query` against `paper_trades`, portfolio
`be80b6c7-8d90-48b7-b963-a1337c594846`), simulating the cumulative cap alone
(28-day window, 1/3 cap, window-start value = the position's value just
before the first sell; amounts below are shares of it):

| Date | Requested sell | Allowed under cumulative cap | Cumulative sold |
|---|---|---|---|
| 08-11 | 2.8% | 2.8% | 2.8% |
| 08-12 | 7.9% | 7.9% | 10.7% |
| 08-13 | 7.9% | 7.9% | 18.6% |
| 08-25 | 7.9% | 7.9% | 26.5% |
| 08-26 | 7.9% | 6.9% (clamped) | 33.3% (cap) |
| 08-27 | 5.0% | 0.0% | 33.3% |
| 08-28 | 7.6% | 0.0% | 33.3% |
| 08-31 | 2.0% | 0.0% | 33.3% |
| 09-01 | 7.6% | 0.0% | 33.3% |

The cap binds starting 08-26 and holds the position at the cap for the rest
of the observed window (none of the trades age out of the 28-day window by
09-01, since only 21 days elapse). Total sold under the cap: 33% of the
position vs. 57% actually sold — **retaining ≈66% of the shares instead of
the actual 43%**. This is not zero recurrence — the position still loses
close to the target 1/3, exactly as designed — but it is a substantial,
directly-computed improvement over the per-cycle cap alone, which caught none
of these nine trades. The core-holding floor (20% NAV) does not additionally
bind in this specific historical case — the position never dropped below the
floor at any point in the observed window — confirming the floor is a
backstop for a more extreme scenario than this one, not what closes this
specific historical gap; the cumulative cap is what does that.

## Alternatives considered

**Reset the window baseline on any buy.** Rejected — a small top-up purchase
would reset the erosion clock and reopen room to resume trimming, defeating
the point of a cumulative cap.

**Uniform floor across every holding (not just ETFs).** Rejected per the
grilling — a single-stock satellite position that has genuinely lost
conviction should be fully exitable; floor-protecting it would block a
legitimate full exit for no benefit, and would reintroduce per-ticker
special-casing on different grounds than the diversification argument that
justifies protecting an ETF core holding.

**Overridable floor via an explicit high-conviction field.** Rejected per the
grilling in favor of a hard block — simpler to implement and reason about,
consistent with the per-cycle and cumulative caps also being hard clamps
regardless of what the model requests, no new prompt/schema surface.

**Config-driven-only thresholds with no shipped defaults.** Rejected — the
grilling chose concrete defaults (33% / 28 days / 20%) directly, all still
config-overridable per strategy for live tuning, matching the existing
pattern for `max_turnover_pct` and `max_sell_pct_of_position`.

## Consequences

- A position can now be trimmed by at most ~1/3 of its value both in any
  single cycle *and* cumulatively across any trailing 28-day window,
  regardless of how many small, individually-compliant trades it takes to get
  there. Closes the exact gap ADR 0012 flagged and left open.
- A diversified/index holding additionally can never be sold below 20% of
  NAV in any cycle, no override — a second, independent backstop against
  total core-holding liquidation that does not depend on trade timing at all.
- Both caps are config-overridable per strategy, same mechanism as the
  existing turnover/per-cycle-sell knobs.
- The window-start-value reconstruction is a value-based approximation (no
  daily per-holding price snapshot exists) — it ignores intra-window price
  drift. This is a known, documented limitation, consistent with the
  per-cycle cap's own approximation; a future increment could add daily
  per-holding value snapshots if this proves material in practice.
- Not a full guarantee against recurrence: the historical replay shows the
  cumulative cap would still have let the position drift down by close to the
  full 1/3 within one window — it bounds the *rate* of erosion to the
  intended target, it does not prevent erosion at that rate entirely. If live
  monitoring shows a position still eroding to its 1/3 cumulative floor
  window after window (i.e. the cap binds every cycle, not occasionally),
  that is a signal the underlying LLM decision quality — not the guardrail —
  needs attention next.
