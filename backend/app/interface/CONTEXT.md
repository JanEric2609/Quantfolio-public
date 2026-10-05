# Context: Interface (layer)

## Responsibility

FastAPI transport. The only layer allowed to depend on all three layers
below it, and the only one nothing else depends on.

Contents:

- **`api/`** — routers, registered in `api/__init__.py`'s `routers` list,
  which `app/main.py` includes. The router-authoring convention is
  unchanged by Phase 5: create `app/interface/api/mymodule.py` with
  `router = APIRouter(prefix="/api/mymodule")`, then append it to
  `api/__init__.py`. The `api/quant/` subdirectory is preserved verbatim.
- **`envelope.py`** — the response-envelope wrapper, flattened from the
  single-file `app/lib/envelope.py` package. Its only consumers are two API
  routers. Not to be confused with `app.foundation.envelope`, an unrelated
  budget-domain module.

## Public surface (facade `app.interface.api.<module>`)

Routers are consumed only by `app/main.py` via `app.interface.api.routers`.
`app.interface.envelope` is imported directly by the two routers that use
it. The layer root `app/interface/__init__.py` is intentionally empty.

## Key collaborators

- In: `app.main` only.
- Out: `app.decision`, `app.lab`, `app.foundation`.

## Contract invariants

- **"Global layering"** — `interface` is the top layer; no seeded
  exceptions involve it.
- **"Services never import the interface layer"** (`app.foundation`,
  `app.lab`, `app.decision` → `app.interface` forbidden) — **zero ignore
  entries**. This is the layer's hardest invariant: it must stay at zero.
- Not a member of the independence or facade-only contracts. Routers may
  import decision-loop package roots and lower layers freely; they should
  still prefer facades over internal submodules.

## Owner-wave notes

`app/main.py` and `app/worker.py` stay at the `app` root, siblings of the
four layers rather than inside `interface`. Both genuinely import across all
four layers by design — they are process entrypoints, not business logic —
and are deliberately outside the "Global layering" contract's scope. This is
a documented exception, not an oversight.

The `docs/api/openapi.json` contract snapshot is the machine-checkable proof
that this layer's wire surface did not change during Phase 5.
