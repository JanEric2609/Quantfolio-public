# 0019 — ETF Fund Choice Rule and the Stock-Ranking Replay

**Status:** Accepted
**Date:** 2026-10-08
**Context source:** `docs/plan-etf-view-and-stock-ranking.md` (plan only, nothing implemented)
**Extends:** [0015](0015-honest-measurement-rebuild.md) (trial ledger, Deflated Sharpe gate) and
[0018](0018-trust-evidence-pre-registration.md) (shadow ledger, provenance, factor study)
**Consumed by:** `backend/app/decision/monthly_plan_funds.py`,
`backend/app/lab/ranking_replay/`, `backend/app/lab/factor_premia/strategies.py`,
`backend/app/interface/api/plan.py`, `backend/app/interface/api/evidence.py`

This ADR is a **pre-registration**. It fixes the fund-ranking rule and the
historical replay's years, metrics and pass bars *before* either is run. At
the time of writing, no fund ranking had been computed and the 2019–2025
exam had never been opened. Changing the rule after seeing a ranking, or the
years after seeing the replay, turns the test into a search. Any amendment
must say what had been observed when it was made.

Not financial or tax advice. DKB and Scalable stay read-only; the app only
suggests a fund per sleeve and gives a reason. The owner confirms it.

## Decision 1 — one source for targets

`decision/portfolio_advisor/analyzer.py` no longer defines its own
`TARGET_ALLOCATION` (the hard-coded 60/25/10/5 stock/bond/etf/cash split).
`compute_gap_analysis` takes the target as an explicit argument and computes
no drift without one. The monthly plan's sleeves
(`decision/monthly_plan.py:sleeve_targets_from` — core, tilt, satellite) are
the single place that defines targets, and
`GET /api/portfolio/target-allocation/drift` reports those sleeve targets
next to the asset-taxonomy figures so one screen can be checked against the
other. A test pins that the analyzer defines no target constant and that the
drift response's sleeve targets equal `sleeve_targets_from`.

## Decision 2 — fund-choice rule (suggest only)

Per sleeve (core, tilt, satellite) the app suggests at most one fund and a
one-line reason. Nothing is bought automatically; `plan_tilt_isins` stays a
manual override (a set override wins and the why-line says so).

Candidates are justETF-listed UCITS funds that track the sleeve's index.
They are ranked by a fixed score, higher is better:

| Criterion | Weight | Measure |
|---|---|---|
| Yearly fee (TER) | 40 % | Cheaper wins, linearly over 0.05–0.50 % |
| Fund size | 20 % | Larger wins (log scale, caps at €10 bn) |
| Replication | 15 % | Full physical = 1, sampled = 0.5, synthetic = 0 |
| Xetra trading volume | 15 % | Higher average daily volume wins (log scale) |
| Savings plan at DKB or Scalable | 10 % | Available = 1, else 0 |

The score is deterministic and documented next to the suggestion; the
candidate table is curated in code (`monthly_plan_funds.py`) because there
is no justETF API, and changing a candidate or a weight is a code change a
reviewer can see.

Accumulating vs. distributing: while the NV-Bescheinigung (tax exemption
certificate) covers the gains it hardly matters — nothing is taxed either
way. Once it no longer covers the book, accumulating funds defer tax through
the Vorabpauschale (a small yearly advance payment) instead of yearly
distributions, so accumulating is the default suggestion. The app notes the
NV-Bescheinigung renewal date is the owner's to watch, at both DKB and
Scalable.

## Decision 3 — monthly plan actions are stored

Each month's computed actions are persisted (`monthly_plan_actions`, one row
per action, idempotent per month) so a placed order the next sync finds can
be linked back to the plan action it fulfils. The existing execution check
(`foundation/recommendation_execution.py:detect_executions`) does the
linking: same user, same ISIN (or ticker when there is no ISIN), action
still open. Plan actions never place orders themselves.

## Decision 4 — historical replay of Discover's non-AI ingredients

The satellite stays locked until a strategy passes the hard DSR/PBO gate.
The formerly best mined score (`scalable:gbm`) scored ~0.01 where ~0.95 is
needed, so this replay tests a different model: Discover's own composite
formula (`decision/discover/composite.py`), using **only the ingredients
that don't use AI**:

- `momentum`, `risk`, `benchmark`, `portfolio`, `fundamentals`

Excluded as AI- or external-data-dependent: `ic_icir` (AlphaCrafter),
`analyst`, `sentiment`, `ml_signal`, `estimate_revision`, `insider_signal`.

Rules, fixed here before opening the years:

- **Locked exam:** calendar years **2019–2025**, opened once, at the end.
  Inputs outside that window are refused.
- **Metric:** rank IC — the per-period Spearman correlation between the
  ingredient's cross-sectional score and the forward excess return — and its
  **Newey-West t-statistic** (6 lags, `foundation.quant_metrics`), which
  allows for overlapping periods.
- **Many ingredients:** the same code runs the per-ingredient ("more
  voices") analysis with **Holm correction** over the five ingredients.
- **Ledger:** every ingredient test is entered in the trial ledger
  (context `discover_replay`), so the search breadth is counted.
- **Pass bar:** "yes" iff the composite replay's Newey-West t ≥ 2.0 — the
  same prior-informed bar the factor tilt faces, not t > 3, because this is
  a pre-registered confirmation, not a mined discovery. The per-ingredient
  p-values are Holm-corrected across the five ingredients alongside it, so
  no single voice can be cherry-picked after the fact. A "no" ends
  stock-ranking effort; a "yes" only routes through the unchanged evidence
  gate, which alone can unlock the satellite.

## Decision 5 — per-ingredient results on live data (information only)

A weekly reader (`GET /api/evidence/ingredient-attribution`) aggregates the
shadow ledger's `components_json.composite_inputs` per ingredient against
resolved forward excess returns and reports per-ingredient rank IC with
Holm-corrected p-values. Stocks in one cohort move together, so the table
shows the effective number of independent stocks (mean-pairwise-correlation
adjustment), which is fewer than the ~200 names. Information only: it feeds
no score and changes no gate.

## Decision 6 — min-vol and size evidence cards

Two strategies join the pre-registered `factor_premia` set, tested by the
same four checks as the other five:

- **Low volatility** (`low_vol`, signal `-rvol_21d`, published 2006 —
  Ang et al.; Blitz & van Vliet 2007), implemented by a world
  minimum-volatility UCITS ETF.
- **Size** (`size`, signal `-me`, published 1981 — Banz; Fama & French
  1993), implemented by a world small-cap UCITS ETF, over the same
  ex-micro/nano universe as every other strategy.

Adding two strategies adds 2 strategies × 2 regions = 4 trials to the
`factor_premia` ledger context, which slightly raises the pass bar for every
other result. That is accepted. Each region-strategy pair stays its own
trial, so the ledger counts the widened search either way.

## Amendment 1 (2026-10-08) — how the 2019–2025 panels are built

Written before any panel was built and before the exam was opened. What
had been observed at that point: the panel's column list, its date range
(1963-01 to 2025-12), and per-year **non-null shares** of the inputs for
the top-100 US and eurozone stocks (95–100 %; eurozone `at_gr1` ~80 %). No
score, forward return or IC had been computed. The owner chose the data
source and the portfolio-fit reference.

**Source.** The point-in-time JKP global characteristics panel already on
prod (`parquet_panel/wrds_factor_characteristics_pit`). It keeps delisted
stocks, so the universe has no survivorship bias. Rows are deduplicated on
(`gvkey`, `permno`, `eom`), keeping the largest `me`. All returns are the
panel's USD returns.

**Periods.** The 84 month-ends 2019-01 … 2025-12. Every score uses data up to
and including its month-end only.

**Universe.** The analogue of Discover's live seed (S&P 100, Nasdaq-100,
DAX 40, EuroStoxx 50 and mid-caps): at each month-end, the **100 largest US
and the 100 largest eurozone stocks** by `me` (`excntry` in DEU, FRA, NLD,
ITA, ESP, BEL, FIN, IRL, AUT, PRT, LUX, GRC). A stock without a forward
return that month drops out of that month's IC.

**Forward return.** `ret_exc_lead1m` (next month) minus the same month's
return of the stock's region benchmark. This is the analogue of the live
excess over the benchmark, and it removes the common EUR/USD move from the
eurozone names.

**Benchmarks**, all value-weighted with the previous month's `me` over the
panel's `mega` and `large` size groups:
US = USA; Europe = eurozone plus GBR, CHE, DNK, SWE, NOR (live: a
developed-Europe ETF); world = Europe plus USA, CAN, JPN, AUS (live:
MSCI World).

**Ingredients.** The live formulas from `decision/discover/composite.py` and
`pipeline.py`. Daily inputs are rebuilt from monthly data where the panel
has no daily series:

| Ingredient | Score at month-end *t* | Difference from live |
|---|---|---|
| `momentum` | mid-rank percentile `(rank − 0.5)/n` of `mom_12_1` in the month's universe | none |
| `risk` | `1 − (0.4·min(1, σ/0.60) + 0.3·min(1, CVaR/0.06) + 0.3·min(1, MDD/0.30))`. σ = √(mean of `rvol_21d`² over months *t*−11…*t*)·√252; CVaR = daily σ·φ(1.645)/0.05 (normal); MDD = max drawdown of the 12-month path of `ret_1_0` | normal CVaR instead of historical; month-end drawdown misses drawdowns inside a month |
| `benchmark` | active returns vs the region benchmark over months *t*−35…*t* (at least 12): t = IR·√years, score `0.5 + 0.5·tanh(t/2)` | monthly instead of daily returns |
| `portfolio` | the owner's book is taken as the core MSCI World fund: corr = correlation of the stock's monthly returns with the world benchmark over *t*−23…*t* (at least 12, else 0.5); fit = `1 − 0.6·(corr+1)/2` | monthly instead of weekly returns; a fixed book |
| `fundamentals` | `factor_characteristics_score` from `be_me`, `gp_at`, `at_gr1`; missing if all three are null | none (the live provider blend has no history) |
| `composite` | live `WEIGHTS` renormalised over the ingredients present: portfolio, fundamentals and momentum 0.15 each, risk 0.10, benchmark 0.05 | the AI ingredients are absent by design |

The exam is opened once with these panels (`spec_version` 1). A bug fixed
after the opening is a new spec version that has to say what had been
observed, and both versions stay on the trial ledger.

## Result — exam v1 opened 2026-10-08: **no**

Opened once with the amendment-1 panels: 84 month-ends, about 199 stocks
each. Report: `docs/research/ranking-replay/exam-v1-report.json`; panel
checksums are next to it, and the panels are kept on prod in
`data/ranking_replay/exam-v1/`.

| Ingredient | Mean rank IC | Newey-West t | p | Holm p |
|---|---|---|---|---|
| momentum | 0.031 | 1.68 | 0.093 | 0.47 |
| fundamentals | 0.022 | 1.42 | 0.156 | 0.62 |
| benchmark | 0.028 | 0.93 | 0.353 | 1.00 |
| risk | −0.011 | −0.49 | 0.625 | 1.00 |
| portfolio | −0.010 | −0.65 | 0.513 | 1.00 |
| **composite** | **0.032** | **1.61** | 0.108 | — |

The composite's t of 1.61 is below the pre-registered 2.0, so the verdict is
**no**. No ingredient survives Holm. Momentum and fundamentals point the
right way, as the literature expects, but over seven years and ~200 large
caps their effect is too small to tell apart from noise. Risk and portfolio
fit point slightly the wrong way.

Consequence, as fixed in Decision 4: stock-ranking effort ends. The
satellite stays locked. Discover keeps running as a shadow measurement (ADR
0018), but no further tuning of its ranking is planned on the strength of
this replay. Re-running with changed ingredients or weights would be a new,
mined search, and would have to clear the t > 3 discovery bar on data it
was not tuned on.

## Amendment 2 (2026-10-08) — spec v2 fixes three panel-builder bugs

Written after exam v1 had been opened. **Observed by then: every v1 number
in the table above** (verdict no, composite t 1.61). Review of the v1 code
(PR #328) found three bugs in `panels.py`:

1. A benchmark member's return was dropped in a month in which it left
   the `mega`/`large` groups, which biased each benchmark slightly upward.
   This affects the `benchmark` and `portfolio` ingredients; the forward
   returns are not affected.
2. A path containing a −100 % month annualised to 0 % instead of −100 %.
   This cannot happen in a top-100 universe.
3. Benchmark weights were lagged by one *observed* month, not one
   calendar month. This cannot happen in this data, which has no missing
   months.

Spec v2 changes only these three things. The universe, ingredients,
weights, years and pass bar are unchanged. Both specs are entered on the
trial ledger (`exam-v1:*` and `exam-v2:*`), so the search breadth counts
both openings. **v2 is the verdict of record**, whichever way it falls, and
any later spec follows the same rule.

## Result — exam v2 opened 2026-10-08: **no** (verdict of record)

Report: `docs/research/ranking-replay/exam-v2-report.json`. Panels are on
prod in `data/ranking_replay/exam-v2/`. The `momentum`, `risk` and
`fundamentals` panels are byte-identical to v1, as expected.

| Ingredient | Mean rank IC | Newey-West t | Holm p |
|---|---|---|---|
| momentum | 0.031 | 1.68 | 0.47 |
| fundamentals | 0.022 | 1.42 | 0.62 |
| benchmark | 0.028 | 0.93 | 1.00 |
| risk | −0.011 | −0.49 | 1.00 |
| portfolio | −0.010 | −0.66 | 1.00 |
| **composite** | **0.032** | **1.61** | — |

The bug fixes move the composite t from 1.608 to 1.613. The verdict is
unchanged: **no**, with the consequences given under the v1 result.

## Amendment 3 (2026-10-14): tracking difference and domicile in the fund rule

From the 2026-10-14 audit plan (`docs/plan-audit-2026-10-14.md`, C4; finding M6). The weights
are the plan's defaults, accepted by the owner on 2026-10-14.

The fee alone does not tell a saver what a fund costs. What the fund actually returned against
funds tracking the same index does, along with the US withholding tax its domicile pays. Decision
2's table becomes:

| Criterion | Weight | Measure |
|---|---|---|
| Yearly fee (TER) | 25 % | Cheaper wins, linearly over 0.05-0.50 % |
| Tracking difference against peers | 20 % | See below |
| Fund size | 15 % | Larger wins (log scale, caps at €10 bn) |
| Replication | 10 % | Full physical = 1, sampled = 0.5, synthetic = 0 |
| Xetra trading volume | 15 % | Higher average daily volume wins (log scale) |
| Savings plan at DKB or Scalable | 10 % | Available = 1, else 0 |
| Domicile | 5 % | See below |

**Tracking difference against peers.**
- **Measure:** the fund's annualised return over 3 years from its stored closes, minus the
  median of the curated funds tracking the same index.
  - Every curated fund with a peer accumulates, so the price return is the total return.
- **Score:** 0.5 + 0.5 × TD / 0.15 %, clipped to 0-1.
- **No figure:** a fund without a same-index peer, or without 3 full years of closes, scores
  the neutral 0.5.
- **Refresh:** the worker's monthly `index_currency_weights` job (3rd, 05:15 UTC) measures it
  (`decision/fund_tracking.py`). The plan reads the stored result and fetches nothing per
  request.
- **Why-line:** when a figure exists, the why-line states it.

**Domicile.**
- **Score:** a non-Irish equity fund with at least 30 % US equity scores 0; every other fund
  scores 1.
  - "At least 30 % US" is matched on index words: World, All-World, ACWI, S&P 500, USA, Nasdaq
    or North America, and not "ex US".
- **Reason:** such a fund pays about 0.14 % a year more US withholding tax than an Irish one.
  The why-line says so.

Bond, cash and gold lines have no same-index peer among the curated candidates. They score 0.5
on tracking difference, and 1 on domicile.
