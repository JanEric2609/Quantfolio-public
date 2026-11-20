# 0002 — Recommendation & Discovery Subsystem Consolidation

**Status:** Accepted
**Date:** 2026-08-22
**Depends on:** `docs/adr/0001-unified-portfolio-engine.md` (complete, merged to `main`)
**Implementation tracker:** `docs/archive/plans/recommendation-discovery-consolidation-implementation.md`

## Context

The user proposed replacing several "broken/misleading" Quantfolio modules with seven external GitHub repos (microsoft/RD-Agent, ZhuLinsen/daily_stock_analysis, microsoft/qlib, stefan-jansen/machine-learning-for-trading, UFund-Me/Qbot, je-suis-tm/quant-trading, TauricResearch/TradingAgents). A research pass — cloning each repo, reading source, and grounding the assessment in Quantfolio's actual code — found none of them are viable drop-in replacements:

| Repo | Proposed target | Verdict |
|---|---|---|
| RD-Agent | discovery funnel | Poor fit. Autonomous LLM hypothesis→code→backtest loop; needs Qlib+Docker+MLflow; wildly disproportionate to the actual gap (a bounded rank-correlation calc); uncapped LLM cost risk. |
| daily_stock_analysis | — (unclear) | Skip. Data providers (akshare/tushare/pytdx/baostock) are CN/HK/US/JP/KR/TW-only; zero European market support anywhere in the code. |
| qlib | quantlab | Cherry-pick only. Alpha158/360 price/volume factor formulas (~40 lines, MIT) worth reimplementing directly in `quant_ml/features.py`. Its own data layer is an opinionated CN/US-centric binary store not worth adopting. Has **zero fundamentals fields** — does not solve the PE/ROE/market-cap gap. |
| machine-learning-for-trading | quantlab feature source | Skip framework, mine one pattern: a SEC EDGAR XBRL fundamentals downloader with correct point-in-time handling. US-SEC-specific; no established European equivalent yet (open question, not resolved by this ADR). |
| Qbot | — (unclear) | **Disqualified.** CN-market-locked; contains live broker-execution code (`easytrader` clients wired to real Chinese brokerages) on a scheduled auto-trade GitHub Action — directly violates Quantfolio's no-live-trading constraint. Its backtest engine is also less robust than Quantfolio's existing `backtest_vbt`. |
| quant-trading (je-suis-tm) | quantlab | Skip. Frozen since April 2024, ~14 independent notebook-exported strategy scripts, no shared infra, broken deps, no tests. Quantfolio's own backtest infra is already better. |
| TradingAgents | stock discovery / R&D agent | Don't adopt the framework — would add a *fourth* overlapping multi-agent system on top of ones already in the codebase. The three-way risk-debate consensus pattern (aggressive/conservative/neutral → arbitration) is worth borrowing as a prompt pattern, not the framework. |

While investigating why the existing modules felt "broken," two follow-up audits (of `discover/`, `alphacrafter/`, `verification/`, and of `llm_portfolio/`, `recommendation_engine/`, `advisor/`, `competition/`, `finagent/`, `graduation/`) found the actual problem is different from the one the external repos were pitched against:

- **`docs/archive/TODO-experimental.md` is stale.** All four documented AlphaCrafter stubs (miner.py's IC, trader.py's backtest, screener.py's regime-blindness, ic_decay.py's synthetic curve) are already fixed in the current codebase (commit `54d9e37` and follow-ups) — the ledger describes a past state, not the present one. `quant_factors.py`'s fundamentals gap now fails open to `NaN` rather than a fake zero. Continuing to treat AlphaCrafter as unfinished stub code would have meant redoing already-real work.
- **Four independent "should I buy/sell X" code paths exist**, and the one the frontend actually calls is not the best-attested one: `llm_portfolio/` (real hard gates, real trade execution against paper holdings, real outcome grading — "the LLM never grades its own homework") and `recommendation_engine/` (real context-building and evidence validation) are both genuinely real, but `recommendation_engine/`'s only live caller (`/api/graduation/recommendations`) is gated behind `experimental_features_enabled` **and** a graduation verdict — reachable by almost no one. The frontend's actual "Generate recommendations" button calls two separate, un-audited implementations inside `api/ai.py`/`services/ai.py` (`deterministic_recommendation_v2` and an LLM-chat path via `analyse_report`).
- **`competition/` is dead code kept alive artificially.** The frontend already redirects `/competition` → `/llm-portfolio/evolution` (superseded by `advisor/evolution.py`'s champion/challenger loop), but `evolution.py` still writes synthetic `CompetitionRun`/`CompetitionDecision` rows purely so `graduation/`'s win-rate metric doesn't break.
- **`verification/attribution.py`** is the one place a real "looks legitimate, isn't" problem survives: it's labeled "Performance Attribution" in the UI but derives factor tilts from keyword string-matching on holding names, hardcodes momentum exposure to `0.0`, and applies hardcoded historical average factor premiums — while a working Fama-French regression (`quant_factors.get_ff3_returns()` / `compute_factor_attribution()`) already exists and is used correctly elsewhere in the same codebase (`discover/pipeline.py`).

The recurring property, same as ADR 0001's diagnosis but at the subsystem level rather than the formula level: **multiple subsystems independently implement the same "decide" responsibility, and only one of the four/six survives contact with the frontend** — the others are dead weight, partially-orphaned, or artificially propped up to satisfy a downstream consumer.

## Decision

1. **Consolidate the "buy/sell/decide" responsibility toward `llm_portfolio/` as the anchor.** It is the most mechanically rigorous of the four candidates (real hard gates, real trade execution, real post-hoc outcome grading) and is already what the Advisor Loop UI is built on.
2. **Fold `recommendation_engine/`'s real, validated logic in as an advisory-only mode** (produces a validated recommendation without executing a paper trade) rather than deleting well-built code — exact mechanism to be scoped during implementation, after auditing what `api/ai.py`'s two paths actually produce today (deferred from this ADR; see Implementation Phase 0).
3. **Retire `api/ai.py`'s two recommendation paths** (`deterministic_recommendation_v2`, the `analyse_report`/`analyse_saved_report` LLM-chat path) once their frontend call sites are repointed at the consolidated core.
4. **Retire `competition/` entirely.** Repoint `graduation/`'s win-rate metric to read `advisor/evolution` rounds directly instead of the synthetic `CompetitionRun` rows evolution currently writes only to keep it alive.
5. **Fix `verification/attribution.py`** to call the existing `quant_factors.get_ff3_returns()`/`compute_factor_attribution()` regression instead of the keyword-heuristic/hardcoded-premium approach.
6. **Correct `docs/archive/TODO-experimental.md`** to reflect actual current state (remove the four resolved AlphaCrafter findings; audit the rest of the ledger for the same staleness pattern while touching this file).
7. **No external repo is vendored or adopted as a dependency.** Two small, low-risk patterns are permitted to be reimplemented (not imported) where a genuine gap exists: qlib's Alpha158 price/volume factor formulas (into `quant_ml/features.py`), and TradingAgents' three-way risk-debate consensus shape (as a prompt pattern inside the consolidated recommendation core, if/when a richer critique step is wanted). The European-fundamentals-data-source question (raised by the ML-for-Trading research, which found only a US-SEC-specific pattern) is explicitly **not resolved by this ADR** and remains open.

### Rollout

Same posture as ADR 0001: one branch (`subsystem-consolidation`), downtime acceptable, no backward-compatibility shims for paper-trading/competition history — this is a personally-run deployment with no real-money stakes in the affected data.

## Alternatives considered

**Keep `competition/` and just stop writing to it from `evolution.py`.** Rejected — this leaves `graduation/`'s win-rate metric silently broken (reading a table nothing populates) rather than fixing the actual dependency, reintroducing the same "no single piece of code owns a concept" failure mode ADR 0001 was written to eliminate.

**Delete `recommendation_engine/` instead of folding it in.** Considered — it would be simpler. Rejected for now because its context-building and evidence-validation logic (`validator.py` genuinely strips unevidenced LLM claims) is real, tested work with no equivalent in `api/ai.py`'s two paths; deleting it would either lose that validation rigor or require rebuilding it inside `llm_portfolio/` from scratch. Final call deferred to Phase 0's audit of what `api/ai.py` actually produces.

## Consequences

- Frontend call sites for "Generate recommendations" change to call the consolidated core; users see the same feature, potentially with different (more validated) output.
- `competition/`'s standalone tables stop accumulating new rows; historical competition data is not migrated (matches ADR 0001's precedent on paper-trading/advisor history).
- `verification/attribution.py`'s numeric output will change when it starts using real FF3 regression instead of hardcoded premiums — expected and correctness-motivated, not a regression, per the same validation posture ADR 0001 used.
- `docs/archive/TODO-experimental.md` becomes a more trustworthy source of truth; this should reduce the risk of redoing already-complete work in the future.
