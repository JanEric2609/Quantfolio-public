# Context: FinAgent

## Responsibility

Budget-focused AI agents over the user's own expense data: budget analysis
agent (budget_agent: `BudgetAgent`), food-substitution suggestions
(substitution_agent), natural-language explanations of money state
(explainer_agent), a RAG retriever over the local knowledge base (rag), and
a LoRA fine-tuning scaffold (lora_scaffold, offline tooling).

## Public surface (facade `app.foundation.finagent`)

`BudgetAgent`, `BudgetInsight`, `ExplainerAgent`, `ExplanationResponse`,
`RAGRetriever`, `SubstitutionAgent`, `SubstitutionAgentResponse`.

## Key collaborators

- In: `app.interface.api.finagent`, `alphacrafter.llm_factor_proposer`
  (`RAGRetriever` for factor-proposal context).
- Out: none cross-context.

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Not covered by the Phase-F facade-only bans (not a decision-loop context).

## Owner-wave notes

Phase F extended the facade with the response models and `RAGRetriever`
previously imported deeply by api/finagent. No seeded debt.
