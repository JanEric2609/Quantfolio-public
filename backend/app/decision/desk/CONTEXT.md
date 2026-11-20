# Context: Decision / desk (guarded derivatives desk, ADR 0027)

## Responsibility

Paper-only trading of knock-outs and warrants with real `sc` quotes and fake fills, run by strict
limits, until a pre-registered rule shows an edge after costs. **No order path exists**: no trading
user, no trade wrapper, no `trade` argv; the broker allow-lists stay read-only.

## Modules

| Module | Role |
|---|---|
| `limits.py` | Frozen `LIMITS` (code constants, pinned by a test, part of the pre-registration) |
| `calendar.py` | Trading days, 21:30 Berlin cut-offs, `MACRO_EVENTS_BY_YEAR` (maintain each year) |
| `guard.py` | Pure `check_entry` / `check_exit`; live-only caps vs reasons that stop a paper entry too. `quote_lost` counts from the later of the last quote and today's window start (an overnight position is not lost at 08:05). A US stock whose earnings calendar cannot be read is `earnings_unknown` (the owner's `event` accepts it). The service adds two exits the pure check cannot see: `knock_out` and refusing a proposal or fill whose latest exit has passed (`exit_time_passed`) |
| `sizing.py` | Units and stop from the 5 EUR budget (both fees included), against the whole pot: the pot already used is a live-only cap (`pot_exceeded`), paper is not shrunk by it |
| `ledger.py` | Append-only hash-chained `desk_events` (verification lives in `foundation/desk_chain.py`) |
| `products.py`, `underlyings.py` | Search, local filters, per-candidate quotes, ranking; the four index aliases |
| `signals/` | `owner_calls` (`/idea`, tap) and `index_momentum` (frozen rule) |
| `service.py` | Position lifecycle, halt flag (`desk_halt_json`), health (`desk_health_json`, written by the monitor only; the news stream's state is `desk_news_stream_json`), live rehearsal. `underlying_price` returns only a fresh price (not outdated, at most 60 s old), in the proxy's own units (DAX ETF, SPY, a US stock's EUR line). `knock_out`: the barrier is converted at the proposal into those units with the listing's distance and a reference price read in the same snapshot (`barrier_ref` in the proposal and fill events; a knock-out whose reference cannot be read is not proposed, `no_reference_price`), and a knock-out with no bid closes at 0 when the reference's path since the fill (Alpaca 1-minute lows/highs, sc's 7-day chart) or its current price touched it |
| `prereg.py`, `scoring.py` | The pre-registered spec/hash, per-cell bootstrap, placebo, Holm |
| `news.py` | Guard and log only; news never opens or closes a position |
| `newswire.py` | The news wire: Alpaca stream (worker thread), Fed/ECB, German ad-hoc (by ISIN), Handelsblatt, SEC 8-K (job `desk_news_wire`), local-model rating, Telegram alerts; alert-only |
| `cfd.py` | The index rule as a manual Trading 212 CFD line, paper-scored in `index_momentum:cfd` |
| `instrument.py` | Names the exact product: issuer name, ISIN/WKN, leverage and barrier or omega and strike, expiry, order line |
| `telegram.py`, `jobs.py` | `/idea /desk /stop /close` + "Paper: take"; 1-minute monitor (exits first, then the index rule; an outdated wrapper fails the tick and sets `wrapper_outdated`, which only a tick that made reads clears; other failed reads become `last_error`; quote ages and fills use the wall clock; each index in its own savepoint, so one failed read rolls back only that index; fills announced by the ids opened this tick), hourly reconcile, 1-minute news wire (a feed's first successful read is stored as `desk_news_wire_read_json`; only that read is a silent baseline) |

## Where it shows

- The Desk page (`/desk`), a research tool in the sidebar (shown with "Show research tools"), also in the command palette: limits, today's live-rehearsal counts, health (last check, failing reads, the news stream), ideas and taps, open positions with Close, recently closed exits with their reason in words, the scorecard, the news wire and the event log.
- The Control Center's attention list (and the 06:00 Telegram self-check built from it): halt, broken chain, outdated wrapper, failing market reads, a news stream down for 15 minutes, positions without a quote, external trades.
- Telegram: `/idea`, `/desk`, `/stop`, `/close`, the "Paper: take" button, exit and CFD messages, news alerts.

## Public surface

The package root (`app.decision.desk`). A facade-only import-linter contract forbids deep imports from
every other context, with zero seeded exceptions.

## Invariants

- Limits are never settings and never raised from the UI or API.
- Every state change is a ledger event in the same transaction as the position update.
- `external` positions (the owner's own app trades) are logged and scored separately, never in a pass-rule cell.
- Phase 2 (real orders) is Proposed in ADR 0027 and not built.
- A CFD is never automated (`would_take_live` is always False); Trading 212 stays read-only.
- Nothing reads `news_wire_items` to open, close or block a position.
