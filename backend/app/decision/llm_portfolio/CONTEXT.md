# Context: LLM Portfolio

## Responsibility

Mandate-driven LLM management of mirror paper portfolios. Provisions
portfolios that mirror real DKB holdings (mirror), assembles review context
(context), runs mandate-driven LLM reviews with deterministic buy gates
(review/gates), turns divergence into advice cards (divergence), scores
outcomes (scoring/assessment), runs the Multi-Agent Council (agents/) plus
its quant-proposal context bridge (council_context — wraps the foundation-tier
`quant_proposal` MC/skfolio builder into the council's 18-key context dict),
and registers scheduled jobs (jobs).

The Council (`agents/`) is wired into two call sites as of the council-wiring
consolidation: `review.run_mandate_review` folds `CouncilOrchestrator.run_council`'s
output into the LLM tool-loop's prompt as **advisory-only** context — the
tool-loop remains the sole author of the persisted decision — and
`advisor.evolution.run_evolution_round` (reached through this package's own
facade, see Key collaborators) runs the real `CompetitionOrchestrator.run_competition`
per round and persists each sleeve's own `CouncilResult` onto its
`CompetitionDecision.council_result_json` (previously always empty — the
journal API read a shape it was never written, so every competition entry
silently showed "hold").

`blm.py` (Black-Litterman model for stabilizing LLM-generated return views)
was relocated to `discover/blm.py` (2026-08-28 architecture seam cleanup): it
was never one of this context's own responsibilities — `run_mandate_review`/
`review.py` never called it, and no module in `llm_portfolio` referenced
`blm` at all. Its only consumer was always `discover.dossier_writer`
(`_llm_view` → `blm.multi_query_confidence`, `_try_bl_er` →
`blm.compute_equilibrium_returns` + `blm.blm_posterior_proportional`), gated
behind Discover's `er_mode == "bl"` public setting. `llm_portfolio`'s own
decision loop is a bounded single-round LLM tool-calling loop that ends in
free-form JSON — there is no integration point today for BLM's "query N
times, use variance as uncertainty" pattern. See
`docs/archive/audits/2026-08-26/deepdive-02-llm-portfolio.md` Gap 1 for the
pre-relocation trace.

## Public surface (facade `app.decision.llm_portfolio`)

`AgentConfig`, `CompetitionOrchestrator`, `GateResult`, `assemble_review_context`,
`build_council_context`, `check_buy_gates`, `compute_divergence`,
`create_default_config`, `ensure_mandate_portfolio`, `generate_advice_cards`,
`register_llm_portfolio_review_jobs`, `register_llm_review_scoring_job`,
`run_mandate_review`.

## Key collaborators

- In: `advisor.{llm_decision,reflection,diagnostics}` (`review._local_llm_sync`
  — seeded), `advisor.evolution` (root facade — `build_council_context`,
  `CompetitionOrchestrator`, `create_default_config`, to run the real council
  for each evolution round — seeded), `discover.dossier_writer` (`review`
  — seeded), `app.interface.api.llm_portfolio`, `app.worker`.
- Out: `paper_portfolio` root facade (holdings seeding, baseline recompute),
  `portfolio.name_resolver` (position names), `discover.tradeability`
  (buy gates — seeded), `quant_proposal` (foundation-tier MC/skfolio
  trade-proposal builder — `council_context.build_council_context`'s input).

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- "Global layering" applies (the four-layer spine subsumes the retired
  "Foundation services never import the decision loop" contract).
- "Decision-loop internals are facade-only: llm_portfolio": six seeded rows
  cover the advisor/discover reads above (five pre-existing plus
  `advisor.evolution -> app.decision.llm_portfolio`, root-level, added for
  the council). The `agents/` subpackage is **no longer internal-only** — it
  is reached from `review.py` (in-package) and from `advisor.evolution`
  (cross-context, through this package's root facade only, never
  `app.decision.llm_portfolio.agents` directly).

## Owner-wave notes

`_local_llm_sync` is consumed cross-context by three advisor modules and
dossier_writer but stays underscore-private — deliberately not blessed as
facade surface. Promoting a public LLM-sync seam (or absorbing it into
review) is the natural next cut; until then the seeded rows track it.
