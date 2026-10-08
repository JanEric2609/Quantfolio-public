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
