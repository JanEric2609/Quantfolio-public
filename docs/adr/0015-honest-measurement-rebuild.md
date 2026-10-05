# 0015 — Honest Measurement Rebuild: Research Harness with a Hard Statistical Gate

**Status:** Accepted (implemented)
**Date:** 2026-09-05
**Supersedes:** 0011 (graduation n_trials scope)
**Extends:** 0003 (quant metrics conventions)
**Context source:** Production forensics (ADR 0014 follow-up), a repo-wide audit of
the deflation machinery, and a 28-question decision grilling with the repository
owner on 2026-09-05. Companion artifact with full first-principles derivations:
https://claude.ai/code/artifact/124cd70f-8a90-4637-b711-594cddd0d2ed
**Consumed by:** `backend/app/services/quant_metrics.py`,
`backend/app/services/graduation/`, `backend/app/services/advisor/scorecard.py`,
`backend/app/services/discover/candidate_gate.py`,
`backend/app/services/alphacrafter/tuning.py`, `backend/app/api/evidence.py` (new),
`backend/app/services/data_backbone/`, `backend/app/services/regime/`

## Context

Quantfolio emits buy/sell recommendations whose statistical support is
indistinguishable from noise. Live mandates trail a plain world-equity tracker;
measured IC sits near zero while stated conviction reads 0.66 (ADR 0014
forensics).

The machinery required to detect this **already exists and is correct**:

| Statistic | Location |
|---|---|
| Sharpe + standard error | `quant_metrics.py:103`, `:128` |
| Probabilistic Sharpe Ratio | `quant_metrics.py:677` |
| Expected max Sharpe under N trials | `quant_metrics.py:700` |
| Deflated Sharpe Ratio | `quant_metrics.py:721` |
| Minimum Track Record Length | `quant_metrics.py:758` |
| Harvey–Liu–Zhu `t > 3.0` | `quant_metrics.py:814` |
| Newey–West t-stat | `quant_metrics.py:817` |
| Purge + embargo walk-forward | `alphacrafter/tuning.py:130–174` |

### Finding F15 — the deflation is starved of a real trial count

`n_trials` is the entire content of a Deflated Sharpe. The luck threshold is
`SE × m(N)` where `SE ≈ sqrt(252/n)` and `m(N)` is the extreme-value multiple.

| Call site | Passes as `n_trials` | Effect |
|---|---|---|
| `graduation/evaluator.py:268` | `max(2, len(portfolio_ids), competition_decisions)` | typically 2–6; deflation is a rounding error |
| `advisor/scorecard.py:216` | count of `AdvisorStrategy` rows | counts objects, not searches |
| `alphacrafter/tuning.py:614` | `len(grid)` | **correct** — the only honest site |
| `discover/candidate_gate.py:30` | — | imports `HLZ_T_THRESHOLD`, never calls the deflation |
| `app/api/**` | — | `grep -rln "deflated" app/api/` is empty; no route exposes any of it |

The same simulated 1.40-Sharpe track record scores DSR **0.808** at `n_trials=2`,
**0.537** at 6, and **0.046** at 550.

`m(N)` computed with this repo's own `expected_max_sharpe`:

| N | m(N) | threshold @1y daily (SE=1.00) | threshold @3y daily (SE=0.58) |
|---:|---:|---:|---:|
| 2 | 0.52 | 0.52 | 0.30 |
| 6 | 1.30 | 1.30 | 0.75 |
| 50 | 2.28 | 2.28 | 1.31 |
| 316 | 2.91 | 2.91 | 1.68 |
| 550 | 3.08 | 3.08 | 1.78 |
| 5000 | 3.69 | 3.69 | 2.13 |

N=316 is Harvey–Liu–Zhu's published-factor count; their `t > 3.0` recommendation
is visibly this calculation.

**PBO / CSCV is the one component genuinely absent** (verified by grep).

## Decision

Convert Quantfolio from a recommendation engine into a research harness with a
hard gate. Real money sits in a single global equity ETF until a signal clears
**all five** conditions, on purged/embargoed splits, net of costs and German tax:

1. Deflated Sharpe ≥ 0.95 with `n_trials = max(ledger_count, 500)`
2. Newey–West `t > 3.0` on the IC series
3. PBO ≤ 0.50 (CSCV)
4. observations ≥ its own MTRL
5. out-of-sample confirmation on data timestamped *after* the hypothesis was
   registered in the trial ledger

### Decision register (28 rulings, 2026-09-05)

| # | Question | Ruling |
|---:|---|---|
| 1 | Rebuild aim | Honest measurement first — research harness, not recommender |
| 2 | Existing prod data | Archive, then reset |
| 3 | Explanation depth | Derive the methods from first principles |
| 4 | Feature scope | Rule case-by-case (see Disposition) |
| 5 | First move | Fix the measurement wiring (F15) |
| 6 | Data spine | Datastream one-time PIT extract; EODHD deferred |
| 7 | Research storage | Migrate research + derived analytics out of Postgres entirely |
| 8 | Research object | Both, gated — single-stock lab feeding an ETF book |
| 9 | `n_trials` | Global experiment ledger floored at 500 — `max(ledger, 500)` |
| 10 | Gate failure mode | Hard gate — nothing ships |
| 11 | PBO | CSCV written in `quant_metrics.py`, no new dependency |
| 12 | Surfacing | New `/api/evidence` router |
| 13 | Silent period | Passive core + evidence dashboard |
| 14 | Migration scope | Prices, factors, backtests **and** all derived analytics |
| 15 | Universe | STOXX Europe 600 |
| 16 | LLM role | Explanation only — never a number reaching an optimiser |
| 17 | Decision-loop contexts | Repoint at the lab, not freeze or delete |
| 18 | Prod handling | Archive, reset, keep it running |
| 19 | Working split | Claude builds; every component ships with a derivation |
| 20 | EODHD | Do not subscribe yet — prove a gap first |
| 21 | Restructure timing | Measurement fix first, restructure after |
| 22 | Repo structure | In place, restructured into four layers |
| 23 | Passive core | Single global equity ETF (IWDA / VWCE) |
| 24 | Prediction target | Forward volatility-scaled return, cross-sectional |
| 25 | Backtest engine | Custom vectorised engine; vectorbt as test oracle |
| 26 | Initial signals | Classic factors + price/volume microstructure + ML on the panel |
| 27 | Cost model | Explicit costs + spread, and German tax drag |
| 28 | Thresholds | Standard + MTRL + out-of-sample confirmation |

Cadence: lab weekly, passive core rebalanced annually (minimises realised-gain
events under Abgeltungsteuer). One branch per phase, verified against the full
local gate before merge (CI minutes are exhausted).

## Context disposition

The owner's binding constraint is that no feature becomes stale. Every context
therefore receives an explicit ruling, and every retirement names its successor.

| Context | Ruling | Destination |
|---|---|---|
| `alphacrafter` | promote | lab search engine; first writer to the trial ledger |
| `quant_metrics` | promote | gains CSCV/PBO + ledger-backed `n_trials` resolver |
| `attribution` | promote | primary monthly question under a passive core |
| `performance_ledger` | promote | supplies the track record MTRL counts against |
| `tax_calc` | keep | now also feeds the backtest cost model (#27) |
| `dkb` | keep | read-only FinTS remains the holdings ingress |
| `imports` | keep | second holdings ingress |
| `portfolio` | keep | ISIN/ticker resolution + price matrices |
| `verification` | fold in | becomes the evaluation layer; confidence score rebuilt on DSR/PBO |
| `providers` | rebuild | into data_engineering; gains the PIT extract loader |
| `data_backbone` | rebuild | fix hypertable writes, then migrate to Parquet |
| `advisor` | repoint | output becomes lab candidates; `n_trials` bug fixed Phase 1 |
| `discover` | repoint | must call the deflation it currently only imports around |
| `graduation` | repoint | becomes the five-condition gate itself (supersedes ADR 0011) |
| `regime` | demote | fix 10Y-3M mislabel + snapshot persistence; lab conditioning variable only |
| `quant_ml` | repoint | ML on the factor panel under purged CV; mind the GKX microcap critique |
| `quant_mc` | keep | scenario work only; never evidence of skill |
| `backtest_vbt` | demote | vectorbt retained solely as a test oracle |
| `llm` | constrain | explanation only; no output may reach an optimiser or score |
| `llm_portfolio` | constrain | `er_mode=bl` LLM return-anchor path removed outright |
| `portfolio_advisor` | constrain | may narrate computed figures, never originate them |
| `recommendation_engine` | **retire** | port its source-path validator into the evidence layer, delete the rest |
| `quant_rl` | freeze | finrl dormant, two vendored stub deps, no measured value; keep code + tests, unregister jobs |
| `finagent` | out of scope | budget/expense agents; unrelated to the investment loop |

## Phases

| # | Branch | Scope |
|---:|---|---|
| 1 | `rebuild/measurement` | trial ledger + `resolve_n_trials()`; CSCV/PBO; fix the three call sites; `/api/evidence` + OpenAPI bump |
| 2 | `rebuild/passive-core` | switch to single global ETF; evidence dashboard; archive + reset prod, keep it running |
| 3 | `rebuild/data-spine` | fix hypertable writes; migrate to Parquet; one-time Datastream PIT extract |
| 4 | `rebuild/lab` | custom backtester (costs, spread, German tax); vol-scaled labels; first signal batch |
| 5 | `rebuild/restructure` | four-layer tree; rewrite 10 import-linter contracts; zero behaviour change |

## Consequences

**Accepted.** The app stops recommending for years — at `n_trials >= 500` the
luck threshold is a Sharpe near 3.1 on one year of data, and a genuinely good
signal at Sharpe 1.0 needs ~4 years (971 observations) to prove itself. Some real
signals will be rejected; given the current false-positive rate this is the right
trade, but it is a trade. The Datastream extract expires with the institutional licence,
cannot be redistributed or polled, and its PIT universe will drift. Two storage
systems coexist during Phase 3.

**Gained.** Every displayed number becomes defensible. The trial ledger makes the
gate un-gameable — searching harder raises the bar automatically. Costs and German
tax enter the backtest, so results describe the owner's after-tax outcome rather
than a US paper's pre-tax one.

**Rejected alternatives.** Greenfield repo (abandons rather than retires the
unported; the Phase 5 restructure gets the same clarity while keeping the 10
contracts and ~2,800 tests). Advisory-only thresholds (a number you can ignore
will be ignored). Retiring the LLM layer entirely (narration is useful; only the
numeric path is dangerous, and the boundary is enforceable). Pivoting to US data
via Open Source Asset Pricing (better data, wrong geography; retained as a future
validation set for condition 5).

## References

- Lo (2002), *The Statistics of Sharpe Ratios* — Sharpe standard error
- Bailey & López de Prado (2012), *The Sharpe Ratio Efficient Frontier* — PSR
- Bailey & López de Prado (2014), *The Deflated Sharpe Ratio* — DSR, expected max Sharpe, MTRL
- Bailey, Borwein, López de Prado & Zhu (2016), *The Probability of Backtest Overfitting* — CSCV
- Harvey, Liu & Zhu (2016), *…and the Cross-Section of Expected Returns*, RFS — the `t > 3.0` hurdle
- López de Prado (2018), *Advances in Financial Machine Learning*, ch. 3 & 7 — labelling, purging, embargo
