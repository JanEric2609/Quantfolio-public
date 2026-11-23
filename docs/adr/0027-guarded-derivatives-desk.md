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
   weekend or holiday, all closed by 21:30 Europe/Berlin on the last trading day of the week; new entries
   08:05-21:45 Berlin (amended 2026-11-20 from 21:30, before the first registration: the US rule enters at
   21:30 and S&P 500 knock-outs are quoted until 22:00); no warrant under
   21 days to expiry; knock-out leverage at most 5 and barrier at least 3 daily sigma away; warrant |delta|
   0.3-0.7; spread cap 1.0 % of mid on the liquid list, 2.5 % otherwise; quote age at most 60 s.
5. **Signals:** (a) the owner's own calls (`/idea`); (b) a frozen index intraday-momentum rule (Gao et al.)
   on DAX, Euro Stoxx 50, S&P 500, Nasdaq-100: enter 30 minutes before the close, exit 3 minutes before
   it (issuers stop quoting at 22:00 Berlin; the 1-minute monitor gets two retries). No parameter is tuned.
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

## Amendment 2026-11-20: instrument identity, the manual CFD cell, the news wire, the replay

Accepted by the owner on 2026-11-20 ("go ahead with these things as you recommended"). Before the first
deploy, so migration 0157 itself gains the new columns and table; nothing registered on prod changes.

1. **Exactly which instrument.** Every position stores the issuer's product name (from the quote), the WKN
   (German ISINs), the barrier (knock-out) or strike (warrant) and the expiry. The `/idea` proposal, the
   index rule's paper-buy message, the exit message and the Desk page name the product, its terms and the
   order line as it would be typed into the Scalable app ("Buy 37 x HT4LYZ at limit 1.6200 EUR (gettex)").
2. **The index rule as a manual Trading 212 CFD** (`cfd.py`), cell `index_momentum:cfd`. Trading 212's API
   has no CFD endpoint, so the desk sends a Telegram line at entry ("CFD signal: BUY US 500 now") and at exit
   ("CLOSE your US 500 position now") and scores the trade on paper at the underlying's reference price.
   The cost model is frozen in the spec: an assumed round-trip spread (DAX, SPX, NDX 2 bp; ES50 4 bp) and
   Trading 212's 0.5 % FX fee on the result of a USD index (its fee page lists "FX fee: 0.5%" without saying on
   what; charging it on the converted result is our assumption); no order fee; no overnight financing (closed
   three minutes before the close). Size: 5 EUR loss on a one-sigma adverse move, at most 1,000 EUR exposure;
   simulated stop one daily sigma away. `would_take_live` is always False: no automated CFD path can exist.
   The pre-registration becomes version 2 with five cells, Holm across five.
3. **The news wire** (`foundation/news_wire.py` fetches, `decision/desk/newswire.py` decides). Sources, all
   free and checked live: Alpaca's news stream subscribed to every symbol (the free key is accepted; items
   arrive within seconds, European companies included), Fed and ECB press feeds, German ad-hoc
   disclosures (finanznachrichten.de's ad-hoc feed: every Art. 17 MAR release about a minute after it,
   each tagged with the company's ISIN, so it is matched to a holding by ISIN), Handelsblatt's headline
   feed (headlines only, a market-wide 3 is sent) and SEC's latest 8-K filings, polled every minute (job
   `desk_news_wire`). The RSS hosts get a neutral User-Agent; only SEC gets the configured one with its
   contact. The publishers' feeds are for personal reading: the wire alerts the owner and republishes
   nothing. The feeds need no login and none is stored (WiWo's headlines largely repeat Handelsblatt's,
   so it is left out). Every item is stored in `news_wire_items` for 30
   days; the local Qwen model rates each headline (materiality 0-3, direction, scope; about 0.7 s on the
   GPU), with the headline fenced as untrusted text. Telegram gets a line for a holding or an open desk
   position (materiality 2+), the Fed or ECB (2+), any 8-K of a holding, and a market-wide 3; quiet hours
   22:00-07:00 Berlin pass only a 3; at most 12 messages an hour. **Alert-only**: nothing reads the table
   to open, close or block a position.
4. **The replay** (`lab/desk_replay.py`, `scripts/desk_replay.py`): the frozen rule on SPY and QQQ 30-minute
   SIP bars from 2016-02 to 2026-11 (free history on the stored key), desk days only: the live rule's skip
   days (a Xetra trading day followed by a Xetra holiday or weekend, when the desk is flat by 21:30; re-run
   2026-11-21 so the replay skips exactly what the live rule skips; macro days are not replayed). Research,
   outside the pass test, not a trial:

   | | SPY | QQQ |
   |---|---|---|
   | Trades | 2,140 | 2,140 |
   | Gross last-half-hour return per trade | -0.70 bp (t -1.10), hit 48 % | -0.25 bp (t -0.34), hit 49 % |
   | Knock-out (leverage 5, desk sizing, 2 x 0.99 EUR, 0.5 % spread) | -2.38 EUR a trade | -2.29 EUR a trade |
   | CFD (2 bp spread, 0.5 % FX fee on the result) | -0.18 EUR a trade (t -6.2) | -0.13 EUR a trade (t -4.9) |

   Gao et al.'s effect (1993-2013) is not present in 2016-2026 on these ETFs. Only 2023 on SPY (+2.7 bp,
   t 1.7) came near significance; 2021 was significantly negative on both. The paper cells keep running as
   registered, unchanged, because changing the rule after seeing this would be a new trial. But the
   prior is now that every index-rule cell fails, the CFD one included; the owner should not trade the CFD
   signal with real money before its paper cell passes.

   Spread check by the owner, 2026-11-20 20:06 Berlin (a -2 % day; Xetra already closed): USA500 0.55 points
   (0.8 bp), TECH100 1.71 (0.7 bp), GER40 3.2 (1.3 bp), EU50 2.4 (4.0 bp). The registered 2 / 2 / 2 / 4 bp are
   at or above every reading, so they stay: the margin also covers the minute or two a manual order takes.
   Web check the same day: Trading 212's help centre lists commission "Free", "FX fee: 0.5%", overnight
   interest at 22:00 GMT (21:00 in summer time) Monday to Thursday (after the desk's exits), and "77% of
   retail investor accounts lose money when trading CFDs with this provider". Scalable lists gettex at
   07:30-23:00 and derivatives "generally" 08:00-22:00; SEC's fair-access limit is 10 requests a second.

## Amendment 2026-11-21: placebo with a missing underlying move (before the first registration)

A closed position whose underlying move could not be recorded (one failed price read at its entry or exit)
used to void the cell's placebo for good, so the cell could never pass. Now, pre-registered in
`prereg.SPEC["placebo"]["missing_move"]`: such trades are left out of the placebo and of the mean it is
compared with; if more than 10 % of a cell's trades lack the move there is no placebo and the cell cannot
pass. The desk also records the move far more often: the fill falls back to the reference price read at the
proposal, and the exit to the reference's last chart point of the past 30 minutes. The spec hash changed
(`86396b95…`); the desk had not been registered in production yet, so no registration is superseded.

## Amendment 2026-11-23: CFD messages v2, owner journal, intraday page

**No rule change.** `prereg.PREREG_HASH` stays `86396b951089aa96007d6227bbfda59991be23b7b8b8ecd2975af6afa5c4a8cd`
(a test pins the literal). r1, the entry and exit times, the sizing (`exposure = min(max_loss_per_trade / sigma,
max_exposure_eur)`), the paper stop of one daily sigma and the paper cost model (`SPREAD_BP`, `FX_FEE`) are as
registered. Everything below is display, owner input, or a read-only preview. No order path exists or is added:
no Trading 212 write, no new `sc` command or wrapper flag (`--offset` and `--limit` were already allowed), and no
desk limit becomes a setting.

- **Trading 212 facts (observed 2026-11-23, about 17:40 Berlin)**, code constants in `cfd.T212`, for display and the
  owner's calculator only: spreads DAX 0.5 bp, EU50 3.8 bp, USA500 0.75 bp, TECH100 0.6 bp; margin 5 % (1:20);
  overnight 0.0083 % of the position value per night; 0.5 % FX fee on the result of a USD market; minimum order
  20 EUR; quantities in 4 decimals. They are broker facts, not limits, and paper scoring keeps the registered values.
- **Message v2.** At the signal the desk reads the index's previous close and the proxy's (`cfd.prepare_context`,
  in the read phase) and sends the owner the quantity in Trading 212 units (rounded down), margin, the enter-by time,
  the stop in index points from the owner's own fill, the typical last-30-minute move and the spread in points.
  The reason now reads "DAX +0.42 % from yesterday's close to 09:30 Berlin" (it used to say "first half hour",
  which was wrong: r1 runs from the previous close).
- **One live message per trade.** The CFD message and the paper knock-out and warrant of the same signal go out as
  one message per index. The monitor edits it in place (at most once a minute, only when the text changed): the
  index level now, the paper result before costs, the time to the exit, the owner's answer. The close is a new
  message, so it notifies.
- **Owner journal.** `desk_cfd_journal` (migration 0158) is the owner's own record of the manual trades: Telegram
  buttons (Entered, Skipped, Closed), `/cfd in|out|skip`, or the Intraday page. It is owner input **outside the
  hash-chained ledger**: it never writes a `desk_events` row and never changes a paper position, and the cell
  `index_momentum:cfd` is scored on the rule's prices whatever the owner did. The real result is signed points x
  quantity x EUR per unit, less 0.5 % on a USD market; overnight interest is not included (nights held are shown).
- **Heads-up and get-ready messages.** From 45 minutes after the Xetra open (35 for NYSE) until ten minutes before
  the entry, the monitor sends once the day's plan per exchange, computed by the rule's own r1. Five minutes before
  the entry it sends a get-ready line for the indices the heads-up expects to fire. These are read-only previews;
  the entry recomputes r1 itself and never uses the heads-up.
- **Intraday page.** `/desk/intraday` shows a board snapshot (`desk_intraday_board_json`, an internal key like
  `desk_health_json`, rebuilt every five minutes), a client-side position calculator and the journal.
- **Warrant search.** Day 1 (2026-11-23) found no warrants for the DAX: the search stopped on a page that was short
  because the parser drops items without an ISIN, and a DAX universe is too large for six pages to reach
  |delta| <= 0.7. The ES50 search read all six pages, then hit `rate_limited` (the wrapper's `flock` on the `sc`
  session, not Scalable's own limit). The sort is unchanged; page one is read, then one-row probes bisect to the
  band, then full pages follow, advancing by the raw item count, with at most 8 `sc` reads per search.

