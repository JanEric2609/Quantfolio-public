# 0003 — Quant Metrics Conventions & Consolidation

**Status:** Accepted
**Date:** 2026-08-24
**Context source:** `docs/archive/audits/2026-08-comprehensive-audit.md` §10 duplication matrix (rows #1–#7) and §13 Tier-3 item 12
**Consumed by:** todos 27 (empirical-VaR consolidation), 28 (legacy MC retirement), 29 (dead Black-Litterman deletion), 30 (annualisation parameters) of `.omo/plans/audit-fixes-2026-08.md`

## Context

The August 2026 audit counted the sprawl: four empirical-VaR implementations, two Monte Carlo engines, two live Black-Litterman implementations plus one dead one, three functions named `compute_metrics` with differing annualisation conventions, three factor-attribution modules, and three backtest engines (one dormant). None of these is a bug on its own. The risk is drift: each copy carries its own tail convention, its own cadence assumption, its own docstring story, and nothing stops a future edit from changing one while the others quietly diverge.

This ADR pins the governance before any consolidation code lands. Every Wave-6 change on the branch traces to a numbered decision here, and reviewers can check each diff against its clause. Where the audit left a design decision open (matrix rows #2, #5, #6, #7), this document resolves it.

## Decisions

### 1. Canonical empirical VaR

`backend/app/services/quant_metrics.py:historical_var` is the single canonical empirical Value-at-Risk implementation. Its convention: the `1 - confidence` order statistic of the ascending-sorted return series (index `floor((1 - confidence) * n)`, clamped to `[0, n-1]`), returned as a positive loss magnitude floored at 0. `historical_cvar` shares this threshold, so `cvar >= var` holds by construction rather than by appeal to an external library's internals.

Disposition of the other three sites:

- `quant.py:historical_var` (numpy.percentile with method="lower") delegates to the canonical function, keeping its public name and signature so its call sites don't churn (todo 27).
- `verification/risk.py:_historical_var` delegates likewise (todo 27).
- `backtest_strategy.py:_historical_var` is unified onto the canonical implementation; see decision 6.

Convention note for migration: percentile-with-method="lower" and the floor index above can select neighbouring observations at some sample sizes. If todo 27's capture-and-compare step finds divergence on its fixed synthetic series, golden values move to the canonical outputs deliberately and visibly (a documented convention change in the commit body), never silently.

### 2. Canonical Monte Carlo engine

`backend/app/services/quant_mc/engine.py` is the canonical Monte Carlo engine. It already powers advisor stress testing, return bands, and verification stress scenarios. The legacy pure-Python GBM fan-chart projection at `quant.py:monte_carlo_projection` is RETIRED once its remaining callers are migrated onto `quant_mc` (todo 28). Until that commit lands, the legacy function stays frozen: fixes go to `quant_mc`, not to the doomed copy.

### 3. Canonical Black-Litterman

`backend/app/services/llm_portfolio/blm.py` is the canonical Black-Litterman implementation. It is the one with real consumers (the review flow and dossier generation). `quant_optim.black_litterman_optimize` has zero routes and zero application callers; it is deleted outright along with its test-only callers (todo 29). No deprecation shim: nothing in production calls it.

### 4. Annualisation conventions stay distinct, become explicit

The three metrics-bundle builders keep their distinct cadence conventions. These are intentional, and todo 30 makes each one an explicit `periods_per_year` parameter instead of flattening them to one value (audit matrix #2: "parameterize, don't flatten blindly").

| Builder | Cadence | Why |
|---|---|---|
| `backtest_vbt/metrics.py:compute_metrics` | Trading days, 252 | Equity curves come from market-data backtests sampled on trading sessions; Sharpe/Sortino/Calmar already delegate to `quant_metrics` defaults. |
| `paper_portfolio.py:compute_metrics` | Calendar days, 365 (`CALENDAR_DAYS_PER_YEAR`) | Paper-portfolio snapshots are taken daily including weekends, so per-period returns are calendar-day returns. Annualising them at 252 would overstate every ratio. |
| `verification/comparison.py:_compute_metrics` | Trading days, 252 | Comparison windows are rebuilt from market-service price history, which is trading-day data. |

Rule going forward: any new metrics builder must accept `periods_per_year` explicitly or pass one of the named constants (`TRADING_DAYS_PER_YEAR`, `CALENDAR_DAYS_PER_YEAR`). A silent hardcoded 252 inside a bundle builder is a review-blocking smell.

### 5. Factor-attribution trio: keep split, document it

`quant.py`'s factor-exposure helper, `attribution/factor_attrib.py`, and `verification/attribution.py` remain three separate modules. Matrix row #6 asked for a design decision; the decision is keep-and-document, the lowest-risk option, consistent with the audit's observation that the docstrings already acknowledge the split. They serve different consumers (Quant Lab exposure view, attribution pipeline regression, verification heuristic tilts respectively) and none of them disagrees about numbers today. Unification would be churn without a correctness payoff.

### 6. backtest_strategy inline VaR copy unified

The private `_historical_var` copy in `backtest_strategy.py` (whose comment claimed deliberate isolation, "zero imports from quant") is replaced by a call to the canonical `quant_metrics.historical_var`, with a comment citing this ADR. Perf note for that comment: `quant_metrics` imports numpy, pandas, and scipy at module level, while `backtest_strategy` historically imported pandas lazily inside functions. The import cost is accepted because every real backtest path already materialises pandas objects anyway, and single-source correctness outweighs cold-start microseconds on an interactive endpoint.

### 7. Two backtest engines remain, on purpose

After the lean package retirement removes the dormant third engine, exactly two backtest engines remain, intentionally:

- `backtest_strategy`: research lab. Single-ticker signal studies for the Quant Lab UI, pure pandas, cost and slippage assumptions surfaced in the response.
- `backtest_vbt`: AlphaCrafter pipeline. vectorbt-powered multi-asset portfolio simulation behind the orchestrator.

Unification is DEFERRED. Rationale: different domains (single-asset signal research vs portfolio simulation), different consumers (interactive lab endpoints vs async pipeline), different execution models (event-style loop vs vectorised). Forcing one engine would serve neither well. Revisit trigger: a third engine proposal appears, or a feature requires capabilities both engines must share.

### 8. advisor → discover private imports: known debt, deferred

`advisor/*` importing discover's `_latest_close` and `_STYLE_FRAMINGS` is recorded as known debt. Promote-to-public-api is DEFERRED to a future refactor. No consumer is blocked today and no package extraction is underway. Trigger for promotion: an actual extraction of either package, or reuse of these symbols beyond the current coupling. Until then the coupling stays visible here instead of growing new silent copies.

### 9. Canonical DSR and the IC-Sharpe variant

Returns-based Deflated Sharpe Ratio canonically lives at `quant_metrics.deflated_sharpe_ratio` (PSR evaluated against the expected-maximum Sharpe under `n_trials`; current caller: `advisor/scorecard.py`). The AlphaCrafter tuning job computes a distinct, documented variant: the same deflation machinery applied to an IC-Sharpe analog (mean(IC)/std(IC) across trials), citing Bailey & López de Prado 2014, "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality".

Why two variants: DSR's input random variable is a return series, from which a Sharpe ratio and its estimation variance are computed. An information-coefficient series is a cross-sectional rank correlation per period, a different random variable with different moments. Feeding ICs through the returns-based formula would misstate the variance term, so the tuning job applies the expected-maximum/PSR machinery to IC moments directly. The two share one statistical idea (deflate against the expected maximum of n_trials) but are not interchangeable implementations of one metric. Any future deflation code must state which random variable it deflates.

## Alternatives considered

**Flatten all annualisation to one constant.** Rejected: it would silently move published paper-portfolio Sharpes (calendar cadence) or backtest Sharpes (trading cadence). Parameterization gets interface consistency without moving numbers.

**Unify the two backtest engines now.** Rejected for the reasons in decision 7. The engines overlap in output shape, not in job.

**Promote `_latest_close`/`_STYLE_FRAMINGS` immediately.** Rejected: speculative API surface before an extraction exists. Recorded as debt instead (decision 8).

**Force all deflation math through the returns-based DSR.** Rejected: wrong random variable for IC series (decision 9).

## Consequences

- Todos 27 through 30 each have a citable governing clause; reviewers trace code changes to decisions rather than taste.
- `AGENTS.md`'s Quant section points here as the governing document, so repo doctrine matches reality.
- Numeric outputs move only where a decision says they may: VaR golden values under decision 1's deliberate-change rule; the retired MC projection and dead Black-Litterman path have no numeric survivors.
- New duplication has a place to be judged. If someone adds a fifth VaR or a fourth metrics builder, this ADR is what they're contradicting, and updating it is the explicit step.
