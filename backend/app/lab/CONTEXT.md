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
the step a mined score must pass before it may touch money),
`calibration` (the ADR 0018 trust tests run on simulated noise: false alarms,
time to a verdict; not a trial), `risk_forecast` (daily VaR/ES bands for each
sleeve, the real book and the benchmark, stored before the fact and scored after),
`long_history` (ADR 0022's harness on JST Macrohistory: the plan's asset mix
over 150 years), `plan_bands` (the wealth plan's one-year real-return bands,
published monthly and scored a year later) and `options_lab` (ADR 0026: the
protective-put replay and the issuer margin on held warrants; it gates
nothing, options are not recommended).

## Public surface (facade `app.lab.<package>`)

Each sub-package is its own facade and carries its own `CONTEXT.md`; the
layer root `app/lab/__init__.py` is intentionally empty. Of these,
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

## Event-desk research (ADR 0028)

- `signal_replay.py` (+ `scripts/signal_replay.py`): the replay of the three technical rules on 2016-2026 daily bars of the 24 CFD instruments, with the desk's costs (spread, overnight per calendar night, 0.5 % FX). It uses `foundation/technical.py`, never `decision/`. Research, not the pass test; its pooled per-cell summary is the internal `app_settings` key `desk_signal_replay_json`.
- `forward_lab/option_smirk.py`: the `option_smirk` forward-lab signal (Xing, Zhang & Zhao 2010) from Alpaca's option snapshots, collected before the nightly scoring (throttled to 150 requests a minute, 45 minute deadline) into the internal key `forward_lab_option_smirk_json`. Scored by the lab like the other signals; it earns a desk cell only by an ADR amendment after months of data.
