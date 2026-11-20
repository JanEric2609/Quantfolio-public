# 0009 — Discover Diversity: Exponential-Decay Recency Penalty

**Status:** Accepted — strong-conviction exemption superseded by [0017](0017-discover-scoring-integrity.md) (relative "improved since last" exemption)
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-08-31_implementation_decisions.md`
(Topic 4 of the consolidated implementation plan — "validate against 53
historical runs" scope note)
**Consumed by:** `backend/app/services/discover/pipeline.py`,
`backend/app/services/discover/composite.py`,
`backend/app/services/discover/dossier_writer.py`,
`backend/app/services/settings.py`, `backend/app/services/settings_catalog.py`

## Context

An audit found the discover pipeline had no mechanism to penalize
re-suggesting a ticker it had already recommended recently: `composite_score`
(`pipeline.py`, via `compute_weighted_composite`) is a pure function of the
candidate's current signals, with no memory of the pipeline's own past
output. A stock with a durably strong signal profile (or a universe with few
alternatives passing the quant gates) could dominate every run's shortlist,
crowding out equally-plausible names the user hasn't seen yet.

## Decision

**Exponential-decay penalty, single-name equities only.** `_apply_recency_penalty`
(`pipeline.py`) multiplies `composite_score` by `(1 - penalty_fraction)` where
`penalty_fraction = 0.25 * 0.5 ** (days_since_last / halflife_days)` — a
ticker recommended today carries the full 0.25 scale, one half-life later
half that (0.125), two half-lives later a quarter, and so on toward zero.
`0.25` is a fixed ceiling (not user-configurable): this is meant as a
tie-breaker against staleness, not a veto — a genuinely strong signal should
still be able to outrank a weak, merely-unseen one even on day 0.

Applied only when `instrument_type == "equity"` (using the same
`classify_instrument()` call `pipeline.py` already made for the ETF/gate
logic) — a diversified ETF isn't "the same idea" the way a repeated single
stock is, so ETFs, money-market instruments, and bonds are exempt
unconditionally.

**Strong-conviction exemption reuses the existing BUY threshold.** A
candidate scoring `composite >= STRONG_CONVICTION_THRESHOLD` (0.65, moved
from a hardcoded literal in `dossier_writer.py`'s BUY-verdict check into a
shared constant in `composite.py`) is exempt — the plan's own wording
("strong-conviction candidates ... exempt") is deliberately tied to the same
number that already decides a BUY verdict elsewhere, rather than introducing
a second, potentially-drifting threshold for the same concept.

**History source: `DiscoveryPrediction`, not a new table.** "Days since last
recommended" is `now - max(predicted_at)` for `DiscoveryPrediction` rows
filtered by `(user_id, symbol)` — this table already gets one row per
shortlisted candidate on every run (`predictor.store_prediction`, called
from `orchestrator.py`), so no new table or migration is needed. A ticker
with no prior `DiscoveryPrediction` row is treated as never-recommended (no
penalty, `exempt_reason: "never_recommended"`).

**Configurable window, fixed ceiling.** The half-life is a public setting,
`discover_recency_penalty_halflife_days` (default 30, registered in
`settings.py`'s `DEFAULT_PUBLIC_SETTINGS` and `settings_catalog.py` under a
new `"discover"` group) — an operator can widen or narrow how long
"recently recommended" means without a deploy. The 0.25 penalty ceiling is
not exposed as a setting: the plan asked for the decay *window* to be
configurable, not the penalty's magnitude, and adding a second free
parameter here would let an operator inadvertently turn a tie-breaker into
a de facto veto.

**Transparency in the dossier.** `pipeline.py` stores the full penalty
detail (`penalty_fraction`, `days_since_last`, `exempt_reason`) under
`scores["recency_penalty"]` — a sibling to the other per-stage score
dicts, so it survives the existing `scores_json` persistence path in
`orchestrator.py` unchanged. `dossier_writer.py` reads it into
`signal_breakdown` as `recency_penalty_applied`,
`days_since_last_recommended`, and (only when non-null)
`recency_penalty_exempt_reason` — so a user who sees a familiar ticker
scored lower than a prior run can see why, and a user who sees a familiar
ticker recommended again despite the penalty can see it was exempted (ETF,
strong conviction, or genuinely new) rather than wondering if the penalty
silently didn't apply.

## Alternatives considered

**Weight the recency signal into `compute_weighted_composite`'s `WEIGHTS`
dict instead of post-multiplying the composite in `pipeline.py`.** Rejected:
`WEIGHTS` combines *quality* signals (momentum, risk, portfolio fit, ...)
that are meant to sum to 1.0 and represent "how good is this candidate on
its own merits." Recency isn't a quality signal — it's a diversity
constraint independent of merit — so multiplying it in after the composite
is computed keeps the two concerns separate and keeps `WEIGHTS` summing to
1.0 without a carve-out.

**Hard exclusion (drop a ticker entirely if recommended within N days)
instead of a decaying score penalty.** Rejected: a hard cutoff can reject a
name the user would still want to see again soon (e.g. after new
information), and gives no signal about *how* recently it was seen — a
soft, continuously-decaying penalty degrades gracefully and is what "recency
penalty" in the plan's own wording implies over a binary exclude/include
gate.

**Penalize ETFs too.** Rejected per the plan's explicit "per-ticker only"
scope — ETF repetition across runs is a much weaker diversity concern than
single-stock repetition, since an ETF holding is itself already diversified.

**Validate against 53 historical discover runs, as the plan specified.** Not
done in this session: this repo's local dev/test databases have no discover
run history (`discover_runs`/`discovery_prediction` are empty in every local
SQLite file checked, same finding as ADR 0008's mandate-loop validation
gap), and pulling 53 runs' worth of production history requires querying the
production database directly — a remote, shared-state action outside the
scope of a local code change, deliberately not attempted without explicit
go-ahead (see ADR 0008 for the same reasoning). Validation here is unit-level
instead: `tests/discover/test_recency_penalty.py` covers the ETF exemption,
the strong-conviction exemption, never-recommended (no penalty), day-0 (full
penalty scale), decay toward zero over multiple half-lives, and the
configurable-setting read path; `tests/discover/test_dossier_writer.py`
covers both the applied-penalty and exempted-penalty dossier transparency
shapes.

## Consequences

- `composite_score` for an individual stock recommended very recently is now
  strictly lower than an otherwise-identical candidate that hasn't been
  recommended — this can change shortlist ranking and which candidates clear
  `LLM_MAX_CANDIDATES`, but never for ETFs or strong-conviction names.
- `scores_json` (`DiscoverCandidate.scores_json`) gains an optional
  `recency_penalty` key for equity candidates; absent entirely for
  ETF/bond/money-market candidates. Any code reading `scores_json` generically
  (dict access, not strict schema validation) is unaffected.
- `dossier_writer.py`'s BUY-verdict threshold now references
  `composite.STRONG_CONVICTION_THRESHOLD` instead of a bare `0.65` literal —
  behaviourally identical, but the two "strong conviction" checks (BUY
  verdict, recency-penalty exemption) can no longer silently drift apart.
- A new public setting, `discover_recency_penalty_halflife_days` (default
  30), is exposed in Control Center under a new `"discover"` catalog group.
