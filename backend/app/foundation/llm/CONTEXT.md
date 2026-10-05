# Context: LLM

## Responsibility

Dual-backend (local llama.cpp + cloud Anthropic/OpenAI) LLM dispatcher with
priority-queue scheduling, audit logging, and shared hardening helpers used
by every LLM-calling context in the app.

- `router.py::call` — the single entry point: selects a backend by task type
  and available keys, submits through `scheduler.py`'s asyncio priority
  queue, and audit-logs the result.
- `base.py` — `LLMBackend` protocol and `Completion` model every backend
  implements.
- `cloud_anthropic.py` / `cloud_openai.py` / `local_llama.py` — the three
  concrete backends.
- `scheduler.py` — in-process asyncio priority-queue scheduler; caps
  concurrent LLM calls so a burst of requests (e.g. an advisor cycle across
  many users) doesn't overwhelm a local llama.cpp instance.
- `audit.py` — writes every call to `AuditLog` for diagnostics/compliance.
- `sampling.py` — shared sampling constants for local llama.cpp calls (Qwen
  non-thinking preset — temperature/top-p/top-k per the Qwen3 technical
  report).
- `structured.py` — shared hardening for structured-JSON completions
  (schema-drop detection, truncation salvage, retry-prompt budgeting).
  Extracted after `advisor/llm_decision.py`'s hardening (prod incidents)
  turned out to be needed identically by `advisor/reflection.py` and
  `discover/dossier_writer.py`.
- `untrusted.py` — prompt-injection hardening for third-party text (news
  headlines, analyst blurbs) that flows into prompts across the
  recommendation engine, discover, and the Multi-Agent Council — wraps
  externally-sourced strings so an embedded instruction ("SYSTEM: ignore
  previous instructions...") can't steer the model.

## Public surface (facade `app.foundation.llm`)

`Completion`, `LLMBackend`, `router_call` (aliased from `router.call`).

Submodules (`llm.structured`, `llm.untrusted`, `llm.sampling`, `llm.audit`)
are commonly imported directly by callers needing their specific helpers —
not re-exported at the package root, similar to `regime`'s documented
practical-surface-vs-facade gap (see `regime/CONTEXT.md`).

## Key collaborators

- In: nearly every LLM-calling context — `app.decision.advisor.{llm_decision,reflection}`,
  `app.decision.discover.dossier_writer`, `app.decision.llm_portfolio.agents.base`,
  `app.decision.recommendation_engine.{orchestrator,prompts}`,
  `app.lab.alphacrafter.llm_factor_proposer`, `app.foundation.finagent.*`,
  `app.decision.ai`, `app.foundation.{llm_research,portfolio_analysis,research}`,
  `app.interface.api.{budget,finagent}`.
- Out: `app.foundation.settings` (`get_public_settings`, `resolve_llm_base_url`,
  `resolve_llm_model`, `resolve_setting`, `get_secret` — endpoint/model/API-key
  resolution, DB → env → default), `app.foundation.models.entities.AuditLog`.

## Contract invariants

- Not a member of any import-linter contract (foundation source_modules,
  decision-loop independence, or any facade-only list) — sits outside every
  list; every decision-loop context is free to import it directly (and does).
- Global layering (interface → decision → lab → foundation) holds by inspection.

## Owner-wave notes

Widest fan-in of the eight packages backfilled in Track F (2026-08-27
audit) — nearly every context that calls an LLM goes through `router.call`,
so a change here (backend selection, scheduling, hardening) has blast radius
across advisor, discover, llm_portfolio, recommendation_engine, and
finagent simultaneously. `structured.py`'s hardening exists specifically
because three independent contexts each hit the same "runaway completion ->
not valid JSON" failure class before it was extracted — a fourth context
adding its own ad-hoc JSON-parsing retry logic instead of using this module
would very likely reproduce the same bug class.
