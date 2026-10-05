# Context: Decision (layer)

## Responsibility

The live recommendation/execution loop plus its own post-hoc scorekeeping.
Consumes `foundation` and `lab`; never the reverse.

Contents: `advisor` (autonomous paper-trade decision loop),
`discover` (candidate discovery pipeline + dossier generation),
`graduation` (paper → real promotion gate), `llm_portfolio` (Multi-Agent
Council review loop), `portfolio_advisor`, `verification` ("Can I trust it?"
verdicts on the loop's own frozen predictions, plus Portfolio → Risk), `engine/` (paper-trade
execution; its only importer repo-wide is `llm_portfolio`),
`recommendation_engine`, plus the flat modules `paper_portfolio.py`,
`regime_advisor.py` (MWU strategy reweighting — a different module from the
`app.lab.regime` package) and `ai.py`.

## Public surface (facade `app.decision.<unit>`)

Sub-packages are facade-only: cross-context consumers must import the
package root, not internal submodules. Six import-linter contracts enforce
this for `advisor`, `discover`, `graduation`, `llm_portfolio`,
`recommendation_engine` and `verification`. `paper_portfolio` is a flat
module with no internal submodules, so no contract targets it. The layer
root `app/decision/__init__.py` is intentionally empty.

## Key collaborators

- In: `app.interface` (routers) and `app.worker` (job registrars). No
  `foundation` module reaches into this layer any more.
- Out: `app.foundation`, `app.lab`.

## Contract invariants

- **"Global layering"** — `decision` sits above `lab` and `foundation`, and
  below `interface`. It may import both lower layers freely.
- **"Services never import the interface layer"** — zero seeded debt.
- **"Decision-loop packages are independent"** (independence, 7 members):
  `advisor`, `discover`, `graduation`, `llm_portfolio`, `paper_portfolio`,
  `portfolio_advisor`, and `app.foundation.portfolio`. Membership shrank
  from 12 in Phase 5 — `alphacrafter` is lab-tier and `dkb`/`imports`/
  `tax_calc`/`finagent` are foundation-tier, so their edges are now ordinary
  cross-layer imports governed by "Global layering" instead. The Wave-3/4
  ignore ledger was re-partitioned accordingly: rows with both endpoints
  still inside the cluster carry forward verbatim under new paths; rows
  where one endpoint left were not re-seeded. `app.foundation.portfolio`
  deliberately stays a member — dropping it would loosen the contract.
- **Facade-only ×6** — see the six `Decision-loop internals are facade-only:
  *` contracts in `backend/pyproject.toml [tool.importlinter]`; each
  sub-package's own `CONTEXT.md` lists its seeded rows.
- **`verification` placement**: `verification` is decision-tier, not lab-tier,
  despite ADR 0015 calling it "the evaluation layer": it scorecards the
  decision loop's own output, and placing it in `lab` would invite a
  `lab → decision` edge, backwards from ADR ruling #17. It reads the prediction
  ledgers through the foundation entities, and has no seeded exceptions (the
  `dkb.service -> verification.engine` edge was retired in 2026-10).

## Owner-wave notes

Phase 5 relocated every unit out of `app/services/` and `app/engine/` with
zero behaviour change. One ADR-0015 verdict is deliberately deferred:

- **`recommendation_engine`**: the ADR says "retire — port the validator
  into the evidence layer, delete the rest". Deletion is a behaviour change
  (`ai.py` still calls into it today) and was out of scope for a
  zero-behaviour-change phase. It is relocated as-is into
  `app/decision/recommendation_engine/`; the actual retirement (port +
  delete) is its own future ticket.
