# AlphaCrafter Shadow-Mode Enablement Gates

**Status: CHECKLIST ONLY — no code scheduled. Default is NO-GO.**
The `experimental_features_enabled` flag this checklist originally leaned on was retired end-to-end (rollback = git revert), so the gates below are enforced procedurally — by owner discipline over promotion decisions, not by a technical switch. Source of truth for the full fix-phase context: `docs/archive/audits/2026-08-discover-maths/blueprint_appendix.md` (section M6) and `docs/archive/audits/2026-08-discover-maths/AUDIT_REPORT.md`.

## Prerequisites

- **M3 trial ledger live**: `ac_trial_ledger` populated via signed-IC writes from `stage_alpha_miner` (`backend/app/decision/discover/trial_ledger.py`). Family size = `COUNT(DISTINCT config_hash)`.

## Go/No-Go Gates (G1–G7)

| # | Gate | Threshold / rule | Evidence artefact |
|---|------|------------------|-------------------|
| G1 | Trial ledger populated | ≥1 full shadow run with per-trial signed IC rows incl. `config_hash` families | ledger query |
| G2 | Deflated Sharpe Ratio | DSR > 0.95 strong; 0.50 < DSR ≤ 0.95 weak/watch; ≤ 0.50 no skill claim | computed over ledger family size |
| G3 | Multiple-testing honesty | White Reality Check / Hansen SPA p-value on best-of-family; BH-FDR q ≤ 0.10 for promoted screens | ledger families |
| G4 | Panel breadth | Cross-sectional IC trusted only at ≥50 names (≥100 preferred); below → IC suppressed + concern flag | panel builder row counts |
| G5 | Pretraining-cutoff hygiene | Factor/formula LLM assistance demonstrably blind to post-cutoff data; per-factor corpus-cutoff audit trail | factor provenance metadata |
| G6 | Shadow duration | ≥90 calendar days shadow accumulation before ANY scoring-weight promotion; no mid-run config changes (family resets) | ledger timestamps — filter by `evaluated_at` at QUERY time; never delete rows to age them out |
| G7 | Owner sign-off | Written owner approval citing G1–G6 evidence pack | repo review inbox |

## NO-GO default

Any unmet gate ⇒ AlphaCrafter output must not be trusted for scoring-weight promotion. Neutral miner scores (`ic=0.00/icir=0.00`) plus UI concern badges remain the user experience (`alphacrafter_miner_unavailable`).

## When activated (post-GO implementation notes)

- Shadow runner = existing orchestrator in shadow profile: computes miner stages, writes ledger, mutates nothing user-facing.
- DSR/SPA/BH utilities as pure functions in `services/alphacrafter/stats_gates.py` consuming ledger queries.
- Promotion path: ledger evidence pack → review inbox → owner approval (G7) only.
