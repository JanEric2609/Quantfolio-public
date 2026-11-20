# 0027 — The guarded derivatives desk (paper first)

**Status:** Phase 1 (paper only) Accepted 2026-11-20. Phase 2 (real orders) **Proposed**: it needs a pass of
the pre-registered rule and a separate owner approval. Nothing of Phase 2 is built.
**Date:** 2026-11-20
**Amends:** ADR 0026 decision 6 ("derivatives are never recommended"), for the paper desk only.
**Consumed by:** `decision/desk/`, `foundation/scalable/market.py`, `foundation/models/entities/desk.py`,
`interface/api/desk.py`, `frontend/src/pages/DeskPage.tsx`, migration `0157_derivatives_desk`.
Plan: `docs/plan-derivatives-desk-2026-11-20.md`.

## Context

The owner wants a small, strictly guarded bot that trades knock-outs and warrants for small intraday (at most
two-day) profits through Scalable Capital. Small losses are accepted. Every trade must be documented and none
may happen undocumented. The research below says retail leveraged trading loses on average, so the desk runs
the whole process with real quotes and fake fills until a pre-registered rule shows an edge after costs.

## Research

- Barber, Lee, Liu, Odean (Taiwan): under 1 % of day traders are reliably profitable. Chague, De-Losso,
  Giovannetti (Brazil): day trading for a living is virtually impossible.
- BaFin turbo study 2019-23: 74 % of retail buyers lost, 3.4 bn EUR in total; experience did not help. A
  knowledge test plus a risk warning have been mandatory since 16 June 2026.
- Bryzgalova, Pavlova, Sikorskaya (JF 2023): retail options losses come mostly from spreads. de Silva, So,
  Smith (RF 2025). Baule and Blonski (JFM 2015): issuer margins.
- News: Jiang, Li, Wang (JFE 2021): underreaction drifts for days. arXiv 2611.03985: LLM news readings predict
  the overnight gap, not the move after the open; no strategy survived costs. arXiv 2611.22177: no LLM agent
  beat simple bets. arXiv 2608.14014: earnings-news drift over days +1 to +5 is -0.04 %.
- Index intraday momentum: Gao, Han, Li, Zhou (JFE 2018), out-of-sample R2 1.2 %; Li and Sakkas (12 of 16
  markets); Baltussen, Da, Lammers, Martens (gamma hedging). Opening-range breakout: an independent
  replication found it net zero. Pre-FOMC drift gone since 2015 (Kurov et al.).

## Decisions (Phase 1, accepted)

1. **Broker:** Scalable through the official `sc` CLI. Trading 212's API is Invest/ISA only, no CFDs.
2. **Who decides:** the bot proposes on Telegram; the owner taps to take each entry (180 s window). Exits are
   automatic.
3. **Products:** knock-outs and warrants of any issuer; factor certificates excluded.
4. **Limits (code constants in `decision/desk/limits.py`, pinned by a test; changing one is a commit plus an
   amendment here):** pot 250 EUR; daily loss stop 15 EUR; at most 5 EUR lost per trade at its stop, both
   fees (0.99 EUR each) included; 1 open position; 2 entries a day; at most 2 trading days held, never over a
   weekend or holiday, all closed by 21:30 Europe/Berlin on the last trading day of the week; no warrant under
   21 days to expiry; knock-out leverage at most 5 and barrier at least 3 daily sigma away; warrant |delta|
   0.3-0.7; spread cap 1.0 % of mid on the liquid list, 2.5 % otherwise; quote age at most 60 s.
5. **Signals:** (a) the owner's own calls (`/idea`); (b) a frozen index intraday-momentum rule (Gao et al.)
   on DAX, Euro Stoxx 50, S&P 500, Nasdaq-100. No parameter is tuned.
6. **News:** guard and log only. News never opens or closes a position.
7. **Everything is documented:** every state change is an event in an append-only, hash-chained ledger
   (`desk_events`); a PostgreSQL trigger refuses UPDATE and DELETE; a broken chain is a Control Center error.
8. **Pass rule (pre-registered, hash frozen in `decision/desk/prereg.py`):** four cells {owner_call,
   index_momentum} x {knockout, warrant}. Per cell: n >= 40 closed positions, mean net P&L > 0 with a one-sided
   day-block bootstrap p < 0.05 (10,000 draws, seed 20261120) Holm-adjusted across the 4 cells, and a real mean
   above the 95th percentile of a 2,000-draw random-direction placebo. Trials are counted through
   `record_trial(db, "desk", ...)`.
9. **Cost arithmetic:** a 5 EUR stop budget leaves 3.02 EUR of adverse move after both fees; the fees alone
   are about 3.3 % of a roughly 60 EUR position per round trip, against an expected index edge of 0.5-1.5 % on
   the product. The index rule is expected to lose after fees at this size. The paper phase shows it; it is not
   tuned. The scorecard shows the median cost share per cell.
10. **Read-only stays:** the desk adds four reads (`broker quote`, `broker derivatives search`,
    `broker chart`, `broker security-news`) to the allow-list and the wrapper. No order command, no `trade`
    argv, no write flag exists. `MUTATING_WORDS` is unchanged; Trading 212 and DKB are untouched.

## Phase 2 (Proposed, not built)

- A separate Unix user `scalable-trade-user` with its own `sc` login (a normal login the owner approves
  weekly) and its own root-owned wrapper allowing exactly: quote, derivatives search, `trade buy`/`sell`
  with `--isin --shares --order-type market|limit|stop --limit-price --stop-price [--confirm]`,
  `trade cancel`, transactions and transaction details. Never `--acknowledge-appropriateness-warning` (the
  owner passes the BaFin knowledge test in the app). Never savings plans.
- A root-owned helper writes `allowed_isins` (the approved product plus held desk products) and
  `max_order_notional` per trade; the app cannot write that config.
- Order flow: tap within 180 s; fresh quote and tolerance check; set the allow-list; Phase-1 preview checked
  against the proposal (ISIN, units, notional, fee); `--confirm`; poll `transactions` until filled; place the
  stop sell; the bot monitors.
- An order whose outcome is unknown freezes the desk until reconciled.
- Limits are enforced three times: `guard.py`, database constraints and `sc` trade controls.
- The pot must be funded in Scalable cash first.
- Halts: the same as paper, plus a lost worker heartbeat (the broker stop still protects).

## Consequences

- A new decision-layer package, `decision/desk/`, with a facade-only import-linter contract.
- Telegram gains inline buttons and a small command/callback registry in `foundation/telegram_updates.py`;
  foundation never imports the desk.
- The wrapper must be reinstalled (`infra/scalable/install.sh`) before the new reads work; until then the
  desk shows a "wrapper needs install.sh again" state.
