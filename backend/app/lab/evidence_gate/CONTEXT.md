# Context: lab.evidence_gate

## Responsibility

Phase 3, step 4 of docs/archive/audits/2026-09-24-why-it-does-not-work: the gate a
mined signal must pass before it touches money (the report's two-tier
evidence standard). It grades the monthly records steps 2 and 3 stored, and
adds no trial of its own.

Families (one per question a selection could be made in):

- `pooled_long_short`: step 2's tercile long-short per score, gross.
- `satellite_after_tax`: step 3's satellite over the core after costs and
  German tax, per `broker:score`. The only family that gates money.
- `satellite_tax_free`: the same after costs only. While a
  Nichtveranlagungsbescheinigung covers the depot neither sleeve pays tax.
  Shown, never gating, because the ten-year horizon is mostly taxed years.

Per candidate: Deflated Sharpe Ratio against the whole trial ledger
(`resolve_n_trials`, at least 500), Newey-West t, Sharpe. Per family: CSCV
PBO over the months every candidate covers, or none when too short to
measure (fails closed, never shown as a measured 100 %).

The satellite unlocks only when a mined score (ridge or trees, never the
value + momentum baseline) has DSR >= 0.95 in `satellite_after_tax` and that
family's PBO is measured and <= 50 % (`spec.py`, fixed before any result).
The verdict is stored as an `EvidenceGateRun` row; `decision.monthly_plan`
reads the latest one (through `foundation.factor_evidence.latest_evidence_gate`)
to set the satellite's target (report Phase 4).

## Public surface (facade `app.lab.evidence_gate`)

- `run_evidence_gate(db, region=..., panel_dir=..., persist=...)` →
  `GateResult` (families, `satellite_unlocked`, `reason`, `unlocked_by`), or
  None when neither step has stored results. With `persist`, writes
  `<panel>/derived/evidence_gate_<region>_results.json` and commits an
  `EvidenceGateRun` row.
- `grade_family`, `pbo_or_none`, `results_path`, `GATING_FAMILY`.
- CLI: `python -m app.lab.evidence_gate [--region world|europe] [--dry-run]`.

## Collaborators

- Out: `lab.pooled_model` and `lab.satellite` (`results_path`, `MODELS`),
  `foundation.quant_metrics` (`deflated_sharpe_ratio`,
  `probability_of_backtest_overfitting`, `resolve_n_trials`,
  `newey_west_t_stat`).

## Invariants

- Never writes the trial ledger: grading a record is not a new trial.
- `spec.py` is fixed before any result is seen.
