# Context: Lab (layer)

## Responsibility

The research / backtest / signal-search / measurement harness. Produces
gated evidence and candidates for the decision loop to consume. Reads
`foundation`; never reads `decision` or `interface`.

Contents: `alphacrafter` (LLM-assisted factor search — the lab search
engine), `quant_lab` (custom backtest engine, trial ledger, CSCV/PBO),
`backtest_vbt` (vectorbt harness, retained as a test oracle only),
`quant_ml` (feature pipelines, walk-forward CV, model registry), `quant_rl`
(frozen — zero outgoing `app.*` imports), `regime` (HMM + rule-based regime
classification and the factor-affinity gate), `attribution`,
`performance_ledger`, `factor_premia` (pre-registered factor portfolios on the
JKP panel and the evidence cards that gate the monthly plan's factor tilt),
`pooled_model` (ridge and LightGBM over every JKP characteristic under purged
walk-forward CV; a mined signal, so it gates nothing before DSR/PBO),
`satellite` (the stock-picking sleeve those scores would drive, simulated
through `quant_lab`'s engine net of broker costs and German tax),
`evidence_gate` (Deflated Sharpe and PBO over those pre-registered records:
the step a mined score must pass before it may touch money).

## Public surface (facade `app.lab.<package>`)

Each sub-package is its own facade and carries its own `CONTEXT.md`; the
layer root `app/lab/__init__.py` is intentionally empty. Of the eight,
`alphacrafter` is additionally enrolled as a *source* module on the
decision-loop facade-only contracts (it may not deep-import decision
internals).

## Key collaborators

- In: `app.decision` (candidates, evidence, regime labels), `app.interface`
  (research/measurement endpoints), `app.worker` (job registrars), plus the
  three seeded `foundation → regime` readers below.
- Out: `app.foundation` only.

## Contract invariants

- **"Global layering"** — `lab` sits above `foundation` and below
  `decision`. A `lab -> decision` import is forbidden with no exceptions;
  this is why `verification` is decision-tier rather than lab-tier despite
  the ADR calling it "the evaluation layer" (see
  `app/decision/CONTEXT.md`).
- **"Services never import the interface layer"** — zero seeded debt.
- **Documented cross-layer exception (regime)**: `regime` is lab-tier
  because `classifier.py` imports `quant_ml.registry` at module level, yet
  three foundation-tier modules read it through lazy, function-local
  imports (a fourth, `portfolio.metrics_wrappers -> regime.gate`, went away
  on 2026-10-04):
  - `app.foundation.portfolio_analysis -> app.lab.regime.macro_snapshot`
  - `app.foundation.research -> app.lab.regime.macro_snapshot`
  - `app.foundation.llm_research -> app.lab.regime`

  These three are seeded verbatim on the "Global layering" contract's
  `ignore_imports` ledger. Do not "fix" them by reclassifying the three
  readers into `lab` — that is an architectural change, not a relocation,
  and was explicitly out of scope for Phase 5. The ledger may only shrink.
- No `lab` package is a member of "Decision-loop packages are independent".

## Owner-wave notes

Phase 5 relocated all eight packages out of `app/services/` with zero
behaviour change. Three ADR-0015 verdicts are reflected physically here:
`alphacrafter` promoted to the lab search engine, `backtest_vbt` demoted to
test oracle, `quant_rl` frozen. `regime`'s demotion is forced by its
module-level `quant_ml` import, not chosen — see the exception above.
