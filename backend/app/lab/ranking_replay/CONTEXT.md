# Context: lab.ranking_replay

## Responsibility

The historical replay of Discover's non-AI composite ingredients (ADR 0019
§4). Takes per-period cross-sections of ingredient scores with their forward
excess returns and reports rank IC with a Newey-West t-test per ingredient
plus the composite, Holm-corrected across ingredients, with every ingredient
test entered in the trial ledger. Lab only: it reads nothing live and feeds
no gate — a fail ends stock-ranking effort, a pass only routes through the
unchanged evidence gate.

## Public surface (facade `app.lab.ranking_replay`)

- `run_replay(db, frames, *, years, spec_version, persist)` → report dict
  with the written yes/no verdict. `frames` maps ingredient (and
  `"composite"`) to a DataFrame with `period`, `entity`, `score`, `forward`.
- `rank_ic_series(frame)` → per-period Spearman IC series.
- `EXAM_START_YEAR`, `EXAM_END_YEAR` (2019–2025, opened once, at the end),
  `NON_AI_INGREDIENTS`, `TRIAL_CONTEXT`, `PASS_T`.
- CLI: `python -m app.lab.ranking_replay --years 2019-2025 --input-dir DIR [--dry-run]`.

## Collaborators

- In: nothing yet (CLI/manual run opens the locked exam once).
- Out: `foundation.quant_metrics` (`newey_west_t_stat`, `holm_bonferroni`,
  `record_trial`), `foundation.models` (trial ledger).

## Invariants

- The exam years are fixed before opening: inputs outside 2019–2025 are
  refused. Re-running the same spec is idempotent on the ledger (one
  `trial_key` per spec and ingredient), so the exam stays one trial no
  matter how often it is inspected.
- Only the five non-AI ingredients (`momentum`, `risk`, `benchmark`,
  `portfolio`, `fundamentals`) are replayed. AI/external-data signals
  (`ic_icir`, `analyst`, `sentiment`, `ml_signal`, `estimate_revision`,
  `insider_signal`) are refused as inputs.
