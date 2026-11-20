# Context: Foundation (layer)

## Responsibility

The base layer of the four-layer tree introduced by ADR 0015 Phase 5. Owns
persistence, configuration, external data access, and the shared numerical
and narration kernels every other layer builds on. Nothing in `foundation`
may depend on `lab`, `decision` or `interface` (two seeded exceptions below).

Contents:

- **`core/`** — `db_base.py` holds ONLY the zero-dependency declarative
  `Base`; `db.py` owns engine/`SessionLocal` side effects and re-exports
  `Base`; `security.py:SecretBox` is the single sanctioned credential-crypto
  seam; `config.py`, `net.py`.
- **`models/entities/`** — SQLAlchemy 2.0 `Mapped`-style entity modules split
  by domain, re-exported from `entities/__init__.py`.
- **`schemas/`** — shared Pydantic request/response models.
- **Sub-packages** — `portfolio`, `dkb`, `imports`, `tax_calc`, `providers`,
  `data_backbone`, `data_engineering`, `quant_mc`, `finagent`, `llm`.
- **~60 flat modules** — `market.py`, `quant.py`, `quant_metrics.py`,
  `quant_optim.py`, `quant_proposal.py`, `quant_factors.py`,
  `expected_return.py`, `settings.py`, `instrument_taxonomy.py`,
  `etf_*.py`, `fund_class.py`, `tax_cockpit.py`, `jobs.py` (generic
  scheduler infra only), `auth.py`, `audit.py`, `notify.py`,
  `telegram_bot.py`, and the rest.

## Public surface (facade `app.foundation.<unit>`)

Every unit is addressed directly at `app.foundation.<name>`; the layer root
`app/foundation/__init__.py` is intentionally empty (no re-export hub) so a
`from app.foundation import X` never drags an unrelated subsystem into the
import graph. Sub-packages with their own `CONTEXT.md` document their own
facade; flat modules are their own facade.

## Key collaborators

- In: `app.lab`, `app.decision`, `app.interface`, `app.main`, `app.worker`.
- Out: nothing inside `app` except the two seeded exceptions below.

## Contract invariants

- **"Global layering"** (`type = layers`, `app.interface > app.decision >
  app.lab > app.foundation`) — this layer is the bottom. It subsumes the
  retired "Foundation services never import the decision loop" contract,
  which was a hand-maintained 11-source/6-forbidden list.
- **"Services never import the interface layer"** — zero seeded debt; must
  stay that way.
- **"Core never imports the models layer"** — intra-layer ordering
  (`app.foundation.core` must not import `app.foundation.models`). One
  documented bootstrap exception: `app.foundation.core.db ->
  app.foundation.models.entities`, the lazy entities import inside
  `create_all()`.
- One seeded upward edge (carried forward from before Phase 5, on the
  "Global layering" `ignore_imports` ledger): `portfolio.metrics_wrappers`,
  `portfolio_analysis`, `research` and `llm_research` reach `app.lab.regime`
  through lazy, function-local imports. `regime` is lab-tier because it
  imports `quant_ml.registry` at module level; reclassifying these four
  readers would be an architectural change, out of scope for a
  zero-behaviour-change phase. (The former second edge,
  `dkb.service -> app.decision.verification.engine`, was retired in 2026-10
  together with the confidence score it recomputed.)
- `app.foundation.portfolio` is additionally a member of "Decision-loop
  packages are independent" (see `app/decision/CONTEXT.md`).

## Owner-wave notes

Phase 5 was purely mechanical: `app/core/`, `app/models/`, `app/schemas/`
and ~73 units from `app/services/` were nested one level in under
`app/foundation/`, with zero behaviour change. Two ADR-0015 items are
deliberately deferred and remain open tickets:

- **`providers` → `data_engineering`**: the ADR calls for rebuilding
  `providers` into `data_engineering`. The dependency graph shows the merge
  has not happened (`data_engineering` does not import `providers`); that is
  a functional consolidation, not a relocation. Both moved into `foundation`
  as separate packages.
- `gap_analysis.py` has zero `app.*` imports of its own and its sole
  consumer is decision-tier `portfolio_advisor`; it is placed here under the
  "leaf, zero deps → foundation" rule.
