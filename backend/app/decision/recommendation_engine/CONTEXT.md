# Context: Recommendation Engine

## Responsibility

Fact-grounded buy/hold/sell/avoid recommendations for a candidate ticker
universe. Gathers a `ContextBundle` from portfolio, quant metrics,
fundamentals, sentiment, track record, regime, news, and research services
with per-source failure isolation (context_builder), builds an
anti-hallucination LLM prompt that lists only the data sources actually
available (prompts), calls the LLM and parses its JSON response
(orchestrator), strips any claim whose cited source path doesn't exist in
the context bundle (validator), and adapts a validated `RecommendationItem`
into the richer `RecommendationPayloadV2` schema used by the newer
recommendations API by reusing AlphaCrafter's conviction/sizing formulas
(v2_adapter).

Track record (`_build_ticker_track_record`, council-wiring consolidation) is
the one evidence category that reaches into the decision-loop: it reads
`advisor.get_scorecard_history` (portfolio-level 4-axis composite — the same
figure across every ticker in one report, since advisor's scorecard isn't
ticker-specific) and `llm_portfolio.get_decision_verdicts` (genuinely
per-ticker hit/miss/partial history from mandate reviews A and B) — both
read-only facade calls, never a submodule reach-in.

## Public surface (facade `app.decision.recommendation_engine`)

`ContextBundle`, `DataHealth`, `Evidence`, `RecommendationItem`,
`RecommendationReport`, `SourceHealth`, `generate_recommendations`,
`recommendation_item_to_v2`.

## Key collaborators

- In: `app.decision.ai` (`generate_recommendations_for_user_async` — the sole
  cross-context caller, imports both facade symbols above; previously reached
  into `orchestrator`/`v2_adapter` directly, fixed to use the package root),
  `app.interface.api.ai` / `app.worker` (via `app.decision.ai`).
- Out: `app.foundation.portfolio_service` (holdings/allocation),
  `app.foundation.data_backbone.regime_store` (`RegimeStore`), `app.foundation.market`
  + `app.foundation.quant_metrics` (per-ticker metrics), `app.foundation.piotroski`
  (fundamentals), `app.foundation.sentiment` (FinBERT), `app.foundation.models.entities`
  (`NewsItem`, `StockResearchReport` — presence checks only), `app.foundation.llm.router`
  (LLM call), `app.lab.alphacrafter.dossier` (`compute_conviction`, used by
  `v2_adapter` — a decision-loop reach-in already sanctioned for `alphacrafter` as
  a source module of the six facade-only contracts, not yet formalized for this
  package specifically). `app.decision.advisor` (`get_scorecard_history`) and
  `app.decision.llm_portfolio` (`get_decision_verdicts`) — package roots only,
  read-only aggregations for `_build_ticker_track_record` (council-wiring
  consolidation).

## Contract invariants

- Not a member of "Decision-loop packages are independent" — sits outside the
  7-member cluster (`app.decision.ai`, its sole cross-context consumer, is a
  flat module and likewise outside it).
- Decision-tier under "Global layering" (ADR 0015 Phase 5); the retired
  "Foundation services never import the decision loop" contract is gone.
- "Decision-loop internals are facade-only: recommendation_engine" (new,
  2026-08-26): `app.decision.ai` is forbidden from importing
  `app.decision.recommendation_engine.*` directly — must go through this
  package's root. No seeded rows; the one prior violation (`v2_adapter`
  imported directly, no facade export existed) was fixed by adding
  `recommendation_item_to_v2` to `__all__` in the same change that added the
  contract.
- This package is now itself listed as a `source_module` on the "facade-only:
  advisor" and "facade-only: llm_portfolio" contracts (council-wiring
  consolidation) — mirrors the `app.decision.ai` gate above in the opposite
  direction: `_build_ticker_track_record` may only call
  `app.decision.advisor.get_scorecard_history` /
  `app.decision.llm_portfolio.get_decision_verdicts` (package roots), never
  `advisor.scorecard`/`llm_portfolio.scoring` directly.

## Owner-wave notes

`v2_adapter.recommendation_item_to_v2` is live production code (the
`RecommendationPayloadV2` persistence path used by `generate_recommendations_for_user_async`)
that had zero import-linter coverage and its only tests lived in
`tests/test_research_system.py` (a file name that gives no hint it covers this
package) — see `tests/test_recommendation_engine.py` for the consolidated,
discoverable test coverage of `_verdict_from_conviction`, `_portfolio_fit_score`,
`_expected_role`, `_risk_score`, and the facade-export guard added alongside
the 2026-08-26 fix. `orchestrator.py`'s `_persist_report`/item insert use raw
SQL (`recommendation_reports`/`recommendation_items` tables) rather than ORM
entities — check `alembic/versions/` before assuming those tables' shape.
