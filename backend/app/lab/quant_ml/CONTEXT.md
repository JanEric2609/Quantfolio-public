# Context: Quant ML

## Responsibility

ML Studio: sklearn/LightGBM/XGBoost/PyTorch pipelines for return
prediction, inspired by López de Prado's *Advances in Financial Machine
Learning* and Stefan Jansen's *Machine Learning for Trading*.

- `features.py::build_features` — technical-indicator + lagged-return +
  calendar features, plus `augment_with_regime_features` (one-hot regime
  label + numeric VIX/crisis-threshold context). Deliberately does **not**
  import `app.lab.regime` directly — regime state is passed in as a
  plain `regime_snapshot` dict by the caller, to keep this package
  decoupled from `app.lab.regime` (see the module's own comment). Wired
  into `build_features` in Track D1a (2026-08-27 audit), then **unwired again**
  by the 2026-08-28 audit sweep (67ef9a8) — correctly: the regime columns are
  broadcast as one scalar across every row, so they are constant at fit time and
  a *different* constant at predict time (train/serve skew, not a feature). The
  `regime_snapshot` parameter is still accepted by `build_features` but no
  caller passes it. Re-wiring requires a point-in-time regime history resolved
  per row; see the `augment_with_regime_features` docstring.
- `labels.py` — `fixed_horizon_labels`/`triple_barrier_labels` (López de
  Prado's triple-barrier method) and meta-labelling.
- `pipelines.py::build_pipeline`/`walk_forward_cv` — sklearn-style pipeline
  construction (LightGBM/XGBoost/PyTorch MLP behind one interface) and
  walk-forward CV (reuses skfolio's `WalkForward` splitter). `walk_forward_cv`
  gates on a naive majority-class-baseline accuracy check (Track B0,
  2026-08-27 audit): an OOS fit that doesn't clear baseline + margin returns
  `status="rejected_below_baseline"` instead of a usable
  `fitted_pipeline` — the trust mechanism every caller here (and
  `training.py` below) depends on. Also `write_signal_rows`, which persists
  `QuantSignal` rows read by `discover.pipeline::stage_ml_signal` (Track
  D1c) — the only reader of those rows; nothing trains on demand from
  inside the discover pipeline itself, it only ever reads the latest
  validated model.
- `registry.py::save_model`/`load_model` — joblib artefact persistence,
  restricted by `_SAFE_PREFIXES` to a class allow-list on unpickling
  (untrusted-artefact hardening — a `QuantMlModel.artefact_path` is loaded
  without re-validating its origin, so the allow-list is the actual defense).
- `training.py::train_and_validate_ticker_model` — the shared training core
  (Track D1b, 2026-08-27 audit), the shape of the `/ml/train` API endpoint
  (the `discover_ml_training` job trained through it until 2026-09-28 and
  now trains `quant_lab.pooled_ml` instead): builds
  aligned (X, y) via `prepare_training_data`, fits via `walk_forward_cv`,
  and **always** persists a `QuantMlModel` row regardless of outcome —
  `status` (`"completed"` vs `"rejected_below_baseline"`) is the sole
  signal of whether the artefact is trusted, mirroring `QuantRlPolicy`'s
  identical discipline in `quant_rl` (see below). Deliberately does not
  import `app.foundation.market` itself (see its own module docstring) — the
  same no-data-fetching-side-effects-inside-the-training-core discipline
  documented at length in `quant_rl/CONTEXT.md`; callers fetch prices/dates
  and pass them in.

- The pooled cross-sectional model Discover serves (ruling #24) lives in
  `app.lab.quant_lab.pooled_ml`, not here: it needs `quant_metrics` and
  `quant_lab.cv`, and a quant_ml edge to either closes the
  `foundation -> lab.regime -> quant_ml.registry` package cycle.

## Public surface (facade `app.lab.quant_ml`)

None re-exported at the package root (`__init__.py` is docstring-only) —
every caller imports submodules directly (`quant_ml.features`,
`quant_ml.training`, `quant_ml.pipelines`, `quant_ml.registry`), the same
practical-surface-vs-facade gap `regime/CONTEXT.md` documents for that
package.

## Key collaborators

- In: `app.decision.discover.jobs` (the scheduled
  `discover_ml_training` job, Track D1b), `app.decision.discover.pipeline`
  (`stage_ml_signal`, Track D1c — reads only), `app.lab.regime.{hmm_model,jump_model}`
  (`registry.save_model`/`_load_model_unvalidated` — regime's own HMM
  artefacts are persisted through this package's registry, not a separate
  one).
- Out: `app.foundation.models.entities` (`QuantMlModel`, `QuantSignal`) — nothing
  else; no `app.decision.*` imports anywhere in the package (confirmed by
  inspection), which is what keeps `decision.discover -> lab.quant_ml` a
  one-directional, layers-contract-legal edge instead of a cycle back into
  the decision loop.

## Contract invariants

- Not a member of any facade-only or decision-loop-independence contract —
  sits outside those lists. Lab-tier by physical placement (ADR 0015 Phase
  5): may import `app.foundation`, must never import `app.decision` or
  `app.interface`.
- Global layering (interface → decision → lab → foundation) holds by
  inspection and is enforced by the `Global layering` import-linter
  contract.

## Owner-wave notes

The "no `app.decision.*` imports" discipline here isn't a style preference —
it's load-bearing. Pre-Phase-5, this package (then a flat `app.services`
module) had to stay clear of the other flat `app.services` modules to avoid
being pulled into the decision loop's accepted dense import-cycle SCC (see
`check_no_new_cycles.py`); post-Phase-5 the same discipline is now the
`Global layering` contract itself — a `quant_ml -> decision.*` edge would be
a straightforward layers violation, not just a cycle risk, the instant
something inside `decision` (`discover`) imports `quant_ml` back.
`training.py`'s docstring documents this explicitly; `quant_rl`'s equivalent
discovery (Track D2) hit the exact same wall and is the more detailed
writeup — read `quant_rl/CONTEXT.md`'s Owner-wave notes before adding any
new `quant_ml` import.

The ML Studio HTTP endpoints (`/api/quant/ml/*`) were removed (zero UI, no tests);
models are trained by the scheduled `discover_ml_training` job and read by Discover.
