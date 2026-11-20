# 0001 — Unified Portfolio Engine

**Status:** Accepted
**Date:** 2026-08-21
**Depends on:** none (first ADR in this repo)
**Implementation tracker:** `docs/archive/plans/unified-portfolio-engine-implementation.md`

## Context

Discover (screening/recommendation), Quantlab (backtest/optimization workbench), Paper Portfolio (simulated holdings/trades), and the Advisor loop (LLM paper-trading with champion/challenger evolution) are linked in the database — `DiscoveryPrediction.portfolio_id`, `AdvisorScorecard.portfolio_id`, and `AdvisorStrategy.portfolio_id` all point at `paper_portfolios` — but share almost no code. Each subsystem independently computes the same underlying concepts, and they've drifted:

- **Sharpe ratio has three independent formulas**: `quant_metrics.sharpe_ratio()` (canonical), a full reimplementation in `verification/risk.py:_sharpe()`, and a differently-scaled one in `discover/config_review.py:_sharpe_ratio()`. Even where callers *do* share `quant_metrics.py`, they pass different `periods_per_year` (`paper_portfolio.py` uses 365, `performance_ledger/ex_post_risk.py` and `competition/scorer.py` use 252) — so the same portfolio shows different Sharpe numbers in different tabs.
- **"Expected return" has four-to-five independent definitions**: Markowitz optimizer dot product (`quant.py`), naive arithmetic mean × 252 (`verification/stress.py`), an LLM-invented number bounded by a heuristic anchor (`discover/dossier_writer.py`), and a Monte Carlo p50 (`advisor/cycle.py`).
- **Instrument taxonomy is inconsistent**: `paper_portfolio.py:resolve_paper_asset_type()` returns `"money_market"`, but the canonical `AssetType` Literal in `schemas/common.py:210` doesn't include it (`Literal["stock", "etf", "bond", "bond_etf", "fund", "cash", "other"]`). A second, differently-shaped `asset_type` Literal exists in the same file for goal targets (`["equities", "bonds", "cash", "crypto", "real_estate", "commodities"]`).
- **Config exists in two unsynced places**: `discover/config.py`'s `DEFAULT_PROMPT_TEMPLATE` (code) vs. the DB-seeded `DiscoveryConfig` row (production). Fixing the code default didn't fix production — a data migration (`0088_fix_dossier_prompt_anchor.py`) was needed to hunt down already-stale rows. This produced a real incident: every Discover dossier echoed a literal `"expected_return": 12.5` from a stale prompt example (fixed in `1b53be6`).
- **A retrospective/audit engine independently reimplemented live scoring logic and silently dropped 50% of production weight** (`d5504c0`) — `discover/config_review.py` had its own hand-copied signal-derivation until it was extracted into `discover/composite.py:derive_signals_from_scores()`.
- **LLM structured-output hardening (schema length caps, sampling preset, retry-with-excerpt) was fixed in `advisor/decision.py` across three commits (`e402161`, `709663a`, `b1a05f1`) and had to be independently rediscovered and patched one release later in the sibling `advisor/reflection.py`** (`5d8adb8`), because there was no shared helper.
- **Frontend mirrors the same fragmentation**: Discover has two near-duplicate page implementations (`DiscoverHubPage.tsx`, `research/DiscoverTab.tsx`) hitting identical endpoints. Quantlab's core data hooks (`useQuantSummary`, `useQuantRisk`, `useRealHoldings`, `useRealPortfolioRisk`) are typed `api<any>` — no compile-time contract with the backend at all — and contain `??` fallbacks (`risk.drawdown?.max_drawdown ?? risk.max_drawdown`) that are fossils of a past silent response-shape change nobody caught.

The recurring architectural property behind every one of these incidents: **no single piece of code owns a concept**, so each subsystem grows its own copy, and a fix to one copy doesn't propagate to the others.

## Decision

Build a unified portfolio engine as a real merge (not a thin shared-utility layer): one domain module owns both portfolio state and portfolio metrics, with Discover/Quantlab/Paper-Portfolio/Advisor becoming consumers of it rather than independent implementers.

### Scope

1. **Metrics**: Adopt [QuantStats](https://github.com/ranaroussi/quantstats) (Apache-2.0) as the shared metrics implementation, replacing the duplicated Sharpe/Sortino/Calmar/CVaR/skew/kurtosis formulas in `quant_metrics.py`, `verification/risk.py`, and `discover/config_review.py`. **Not** skfolio's `Portfolio` object — see Alternatives Considered. Metrics not covered by QuantStats (Probabilistic/Deflated Sharpe Ratio suite, Minimum Track Record Length, "False Strategy Theorem" `expected_max_sharpe`) remain custom, owned by the same engine module rather than scattered.
2. **Expected return**: One estimator producing `{value, method, horizon}`, not a bare number. The Discover dossier LLM stops inventing a number entirely — its role narrows to justifying/critiquing the quant-computed anchor in prose. This directly closes the `+12.5%` bug class.
3. **Instrument taxonomy**: One classifier covering `equity`, `etf` (including bond ETF), and `money_market` — the three classes actually in use. Crypto/real-estate/commodities (referenced today only in `goal.py`'s free-text allocation targets) are out of scope until the app actually screens or holds them.
4. **Config source of truth**: Hash/version the code-default config; DB-seeded rows are flagged stale automatically (surfaced in Control Center) when the code default's hash moves past what a row was seeded from, rather than silently drifting or silently overwriting.
5. **Schema**: A shared metrics-snapshot table, keyed by `(portfolio_id, as_of, context)`, replaces the independent metrics columns currently duplicated across `PaperSnapshot` (`sharpe`, `max_drawdown`) and `AdvisorScorecard` (`sharpe`, `sortino`, `calmar`, `cvar_95`, `max_drawdown`). Existing paper-trading history, advisor scorecards, and evolution/reflection data are **not migrated** — reset on deploy, since it's simulated data with no real-money stakes.
6. **LLM call hardening**: One structured-LLM-call helper (schema max-length bounds, sampling preset, `max_tokens`, retry-with-excerpt) used by both `advisor/decision.py` and `advisor/reflection.py`, so a future hardening fix applies to both automatically.
7. **Experimental code included**: `verification/risk.py` is gated by `experimental_features_enabled` (off by default) but is included in this refactor rather than left as a fourth divergent Sharpe formula.
8. **Frontend**: Ships in the same pass as the backend (not a follow-up). One shared TS type for the engine's metrics payload (explicit `number | null`, not `any`/optional-undefined), one shared `MetricsCard`/`MetricTile` rendering component, and the two duplicate Discover pages consolidated into one. Prediction-level metrics (Advisor scorecard) and portfolio-level metrics (Quantlab/Paper Portfolio) are legitimately different quantities — they get distinct labels (e.g. "Sharpe (portfolio)" vs. "Sharpe (predictions)"), not silently identical ones.

### Validation approach

Current outputs are known-buggy in places (three divergent Sharpe formulas can't all be right). Tests assert freshly-defined, mathematically correct expected values (hand-computed or QuantStats-verified) rather than locking in today's behavior as a regression baseline.

### Rollout

One PR, organized as a sequence of separate, logically-scoped commits (see implementation tracker for exact ordering and file lists). Downtime during deploy is acceptable — this is a personally-run pet-project deployment, not a system with uptime SLAs. Scheduled advisor-loop/Discover jobs on the live Proxmox worker keep running against the old schema until deploy time; the user pauses them manually when ready to cut over.

## Alternatives considered

**skfolio's `Portfolio` object as the shared metrics interface.** Rejected after a direct side-by-side comparison (see `docs/archive/plans/unified-portfolio-engine-implementation.md` for the full numeric comparison table and source-level divergence analysis). skfolio's `Portfolio` constructor wants an asset-weight matrix, which three of the four call sites (Discover scoring, Paper Portfolio snapshots, most Advisor scorecards) don't naturally have — only a returns Series. Worse, skfolio's own defaults (`compounded=False` → arithmetic not geometric drawdowns; `min_acceptable_return=mean` → a non-standard Sortino target) are easy to misconfigure differently across call sites, which would reintroduce the exact class of drift this ADR exists to eliminate. skfolio remains in use for what it already does well — `quant_optim.py`'s optimization — just not as the metrics-reporting interface.

**A shared-utility layer with subsystems staying otherwise independent** (i.e., don't merge schemas, just make every subsystem call the same functions). Considered and rejected in favor of the real merge: a utility-only fix is discipline-dependent — nothing structurally stops a future fifth subsystem from reinventing Sharpe again. The real merge removes the *place* such duplication could live, at the cost of a bigger single-pass migration. Given downtime and data-reset are both acceptable here, that cost is affordable.

**Preserving existing paper-trading/advisor history through the schema migration.** Rejected — it's simulated data with no real-money stakes; a clean reset is simpler than a backfill/mapping migration and the user confirmed they don't need the history kept.

## Consequences

- Metric values will shift by small, documented, defensible amounts when call sites switch from custom formulas to QuantStats (e.g. Sortino ~0.3%, CVaR ~6% in the spike comparison, due to differing MAR/tail-estimator conventions) — this is an intentional, correctness-motivated change, not a silent regression, and should be called out in the PR description.
- Paper-trading history, advisor scorecards, and evolution/reflection state reset on deploy.
- The two duplicate Discover frontend pages become one; any bookmarked/linked routes to the removed duplicate need redirecting.
- Frontend `api<any>` usages in Quantlab hooks are replaced with a real shared type — this is expected to surface latent bugs where the frontend was already silently tolerating a mismatched response shape.
