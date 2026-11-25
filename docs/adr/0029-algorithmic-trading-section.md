# 0029 — The algorithmic trading section: evidence first, execution last

**Status:** Proposed 2026-11-25. Not accepted as a build commitment; merged as a research-only proposal (docs plus `lab/algo_lab`, which gates nothing). The owner delegated the open questions; the answers are in section 15 of
`docs/plan-algo-trading-2026-11-25.md`. Written owner approval is still needed before any phase after Phase 1 starts.
**Date:** 2026-11-25
**Builds on:** [0018](0018-trust-evidence-pre-registration.md) (pre-registration), [0025](0025-capital-ladder-and-paired-promotion.md)
(ladder, paired promotion, trials per family), [0027](0027-guarded-derivatives-desk.md) and [0028](0028-event-desk.md)
(paper desks, replays, limits as code constants)
**Plan:** `docs/plan-algo-trading-2026-11-25.md`  **Literature:** `docs/research/2026-11-25-algo-trading-literature.md`
**Consumed by (proposed, none built):** `lab/algo_lab/` (prototype only), a later `decision/algo/`, `interface/api/algo.py`

## Context

The owner wants an algorithmic trading section and expects Interactive Brokers (IBKR, Ireland) as the future broker. The
budget on record is small: a 250 EUR desk pot (ADR 0027) and 20 EUR a month of learning money (ADR 0025). The standing rules
are unchanged: brokers read-only, every desk paper-only, no order path.

What the evidence says (details and tags in the literature note):

1. Short-horizon rules lose after costs. The repo's own replays agree: the index rule has no gross edge (2016-2026, SPY/QQQ),
   CFDs lose after costs, knock-outs lose heavily, the three technical event cells lose (ADR 0028). Outside the repo, 14
   intraday families on MNQ fail (gross edge 0.07-1.5 points against 2 points of friction), 97 % of persistent Brazilian day
   traders lose, and five popular retail signal families are refuted or inconclusive with none supported.
2. What survives is slow: trend with horizons of months still earns a Sharpe of about 0.4 (+-0.26) in a 100-futures
   portfolio, fast trend collapsed after 2009 on small-tick contracts (equity indices, FX). A small retail universe is
   not that portfolio, and no published test of ETF-level trend with German tax and EU costs was found. The academic
   evidence is also contested: Huang, Li, Wang, Zhou (JFE 2020) find little time-series-momentum evidence asset by asset
   (8 of 55 significant at 10 %, 45 of 55 negative out-of-sample R^2) and a pooled t-statistic of 4.34 that does not survive
   bootstrap critical values (4.8 to 12.5); in the small-ETF-universe preprints reviewed (2610.36491, 2611.05310, 2610.27751,
   2605.20636) tactical rules do not beat the passive benchmark on Sharpe. The defensible prior for a monthly ETF rule is
   a **drawdown reduction**, not an alpha (Faber's own figures: return 10.5 % vs 9.9 %, volatility 7.0 % vs 10.3 %,
   gross, indices). See `docs/research/2026-11-25-algo-trading-review.md`.
3. A small edge cannot be proven in a human lifetime of a small account: a Sharpe of 0.5 needs about 25 years for a
   one-sided test at 80 % power, 38 years with four candidates (`lab/algo_lab/gate_math.years_to_detect`).
4. Per-order minimums decide small accounts: IBKR's tiered minimum of 1.25 EUR is 50 bp on a 250 EUR order, the fixed
   minimum of 3.00 EUR is 120 bp.
5. Promotion rules that tolerate continuous inspection and a growing candidate list exist and are cheap to build
   (Promote, e-process ledger with a lifetime budget).

## Decision

### 1. The section is a measurement system with a broker boundary, not a signal factory

Build in this order; each phase has an exit test in the plan, and a phase may stop the programme.

| Phase | What | Money | Status |
|---|---|---|---|
| 1 | `lab/algo_lab`: promotion ledger, gate maths, null-audit harness, strategy card + hash, cost-aware slow-strategy replay | none | prototype of the first two included on this branch |
| 2 | `decision/algo`: pre-registered slow candidates scored daily on paper with venue cost models; evidence on excess returns | none (paper) | proposed |
| 3 | **Optional.** IBKR read-only integration (the other thread) plus a **shadow-fill ledger**: what the broker's paper account fills versus the paper model | none | proposed; phases 1 and 2 run on Quantfolio's own paper ledger and data and never depend on it |
| 4 | Ladder tuition for at most one slow candidate, through the existing 20 EUR/month line, executed **by the owner by hand** | learning budget | needs a separate written owner approval |
| 5 | An `OrderPort` with an IBKR implementation behind code-constant limits | capped pot | **out of scope; needs its own ADR** |

### 2. Candidate families allowed to register (pre-registration)

- Allowed: slow, low-turnover, EUR-listed UCITS ETF rules (monthly decision, at most 12 round trips a year per sleeve):
  10-month moving-average switch, volatility-scaled 12-month time-series momentum, each against buy-and-hold of the same
  basket and against MSCI World EUR (`EUNL.DE`).
- Allowed as research only (no promotion path in this ADR): insider buys (Europe), option-smirk, earnings/news cells
  that already run in the forward lab and event desk.
- Not allowed to register: intraday rules, leveraged products as strategy engines, anything whose break-even cost is below
  the cost of its cheapest eligible venue at the actual order size (`gate_math.breakeven_cost_bps`).

### 3. Evidence rules (all pre-registered in the strategy card, hash frozen before the first scored day)

1. Scores are computed on **excess return over the declared benchmark**, net of the **actual venue's** costs and an
   estimate of German tax drag. Absolute-return criteria are forbidden (they reject true signals).
2. The primary test is the funded e-process ledger (`lab/algo_lab/eprocess.py`): lifetime budget 1, equal mass across
   registered candidates, alpha 0.05, a crossing authorises a request, never an order, tradable from the next session.
   Restarts and rescales are new, funded candidates. Trials are counted per family through `record_trial` as in ADR 0025.
3. Secondary, descriptive: deflated Sharpe, PBO/CSCV and Holm over the registered family, paired block bootstrap of
   drawdown and CVaR against buy-and-hold. They inform, they do not authorise.
4. Every pipeline change must first pass the **induced-null audit** (zero-predictability and bid-ask-bounce placebos)
   before it sees real returns; passing is necessary, not sufficient.
5. No parameter is tuned after registration. A grid is allowed only as a pre-declared family of at most 3 members.

### 3b. Review amendments (2026-11-25, proposed)

- The e-process guarantee is for the **clipped** score. The strategy card fixes a scale at or above the 99.5th percentile of the absolute net score
  and the ledger reports `clipped_fraction`; unclipped returns are kept beside the evidence.
  The percentile floor is **necessary but not sufficient**: a zero-mean, negatively skewed score that clips only 0.1 % of days can still have a clipped mean near +1 and promote on almost every path (`test_clipped_null_is_not_the_return_null`). The strategy card therefore states the clipped-score null as an explicit field, and a crossing counts as evidence only after the induced-null audit (T3) has been run on that exact clipped score. `clipped_fraction` is descriptive only.
- The ledger rejects non-finite scores (`LedgerError`); an inactive or cash day is passed as an explicit `0.0`, as in Benhamou et al.
- For a timing overlay, an e-process on excess return practically never crosses (simulated median about 56 years at a Sharpe of 0.4).
  A card must either state a risk claim with its own null and metric (drawdown or CVaR against buy-and-hold) or say that route (a) is unreachable.
- Detection times use the Sharpe standard error under the alternative (`years_to_detect(..., alt_se=True)`).

### 4. Ladder and promotion

The algo candidates enter ADR 0025's ladder as new candidates with the same stage rules (126 trading days, harm e-process
below 10, no owner veto). The ladder's own wording applies everywhere: **a learning expense the owner chose, not evidence the
strategy works.** What the new section adds: a shadow-fill gate before stage 1 (broker paper fills must reproduce modelled
fills within a stated slippage for 20 trading days) and an expiry of authorisation after 24 months, renewed as a funded epoch.

### 5. Limits are code constants, never settings

Following ADR 0027, the algo limits live in `decision/algo/limits.py` (proposed) and are pinned by a test: sleeve cap, per-order
notional cap, orders per day, drawdown halts, quote-age and spread caps, venue allow-list. Changing one is a commit plus an
amendment here. Proposed starting values are in plan section 7; they are suggestions for the owner, not decisions.

### 6. Order path

None in this ADR. The broker boundary is an interface (`OrderPort`) with only a null (paper) implementation until a later ADR
is accepted. The IBKR adapter, when it exists, is read-only: `ALLOWED` request set, no order or cancel endpoints, a
separate username for the bot (IBKR allows one brokerage session per username), paper account first. The bot is whole-shares-only (fractional shares are not available via the IBKR API). Guard rule: strategy code may only veto or shrink an order, never widen it, add a symbol or change a limit. The existing read-only
rules for DKB, Scalable and Trading 212 are untouched.

## What was built on this branch (research only)

`backend/app/lab/algo_lab/`: `eprocess.py` (funded promotion ledger), `gate_math.py` (years to detect, minimum detectable
Sharpe, out-of-sample replication ratio, break-even cost, per-order cost, drawdown-breach Monte Carlo), a `CONTEXT.md`
and `tests/test_algo_lab.py`. No migration, no API, no job, no setting, no import from `decision` or `interface`.

## Consequences

- A strategy cannot be promoted by a good backtest alone; the best realistic outcome for a small account is a verdict of
  "inconclusive, cheap to run, kept as a learning sleeve" for years. That is the honest answer to a 0.5 Sharpe.
- IBKR is chosen for the research value of its paper account and API, not because it is cheap at 250 EUR (it is not).
- Trading 212 or a gettex route stays the cheaper venue for slow ETF rotation of tiny size; the venue is a field of the strategy
  card, decided by cost, not by the section.

## What would make us stop

- Phase 2: two registered candidates are inconclusive after 24 months and cost more than 0.3 Sharpe in modelled costs: stop
  registering, keep the measurement tools.
- Phase 3: broker-paper fills differ from the model by more than the cost-gate margin: the venue is unfit for that size.
- Any phase: a broken ledger chain, an unknown order state, or the owner's veto.
