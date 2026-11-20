# Context: AlphaCrafter

## Responsibility

Three-agent quantitative factor research pipeline: miner proposes and tests
factors (miner, factor_dsl, llm_factor_proposer), screener ranks candidates
(screener), trader backtests allocations (trader), over a shared panel and
HDF5 memory (panel, shared_memory). Includes dossier generation (dossier),
daily orchestration + run reaping (orchestrator), walk-forward tuning jobs
(tuning/jobs), IC-decay factor retirement (ic_decay), and trial-ledger
persistence.

## Public surface (facade `app.lab.alphacrafter`)

`build_dossier`, `compute_conviction`, `orchestrator`,
`reap_stale_job_runs`, `run_daily_alphacrafter`, `run_miner`,
`run_screener`, `run_trader`.

Not re-exported by cycle constraint — `jobs.py` imports this package root,
so a root→jobs edge would close a module-level import cycle:
`jobs.register_alphacrafter_daily_job`, `jobs.register_alphacrafter_tuning_job`
(app.worker imports `app.lab.alphacrafter.jobs` directly).

## Key collaborators

- In: `discover.pipeline` (miner/screener modules + panel),
  `recommendation_engine.v2_adapter` (`compute_conviction`),
  `app.interface.api.alphacrafter`, `app.worker`, `app.main`.
- Out: `finagent.rag` (`RAGRetriever` for the LLM factor proposer).

## Contract invariants

- Member of "Decision-loop packages are independent" (independence).
- Not covered by the Phase-F facade-only bans (not a decision-loop context).

## Owner-wave notes

Tuning-trial ledger lives in `ac_trial_ledger` (migration 0096); job runs in
`alphacrafter_job_runs`. Diagnostics endpoint exposes pipeline health.

**Miner universe (2026-09-28).** `universe.MINER_UNIVERSE` has 150 names:
Euro Stoxx 50 plus S&P 100, from `foundation.index_constituents`. It is
the cross-section the IC/ICIR gate is measured on.
`resolve_miner_universe` resolves which universe to mine:
- An explicit `universe` argument is used as given.
- Otherwise `alphacrafter_index_basket`, when it has at least 30 names.
- Otherwise `MINER_UNIVERSE`. The old four-ETF default could not produce
  a meaningful rank IC.

`data_ingestion.AlphaCrafterDataIngestion` keeps stored bars that already
cover the window. Otherwise it ingests incrementally through
`DataIngester.ingest_bar_prices`.

Discover consumes the library through `miner.factor_values`: its
`stage_alpha_miner` scores a candidate's exposure to each validated factor
(see `decision/discover/CONTEXT.md`).
