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
| `service.py` | Position lifecycle, halt flag (`desk_halt_json`), health (`desk_health_json`, written by the monitor only; the news stream's state is `desk_news_stream_json`), live rehearsal. `underlying_price` returns only a fresh price (not outdated, at most 60 s old; a thin US stock whose Alpaca quote is wider than 1 % gets its last real IEX trade instead, at most 300 s old, which entries, stops, exits and the quote-lost clock all act on), in the proxy's own units (DAX ETF, SPY, a US stock's EUR line). `knock_out`: the barrier is converted at the proposal into those units with the listing's distance and a reference price read in the same snapshot (`barrier_ref` in the proposal and fill events; a knock-out whose reference cannot be read is not proposed, `no_reference_price`), and a knock-out with no bid closes at 0 when the reference's path since the fill (Alpaca 1-minute lows/highs, sc's 7-day chart) or its current price touched it |
| `prereg.py`, `scoring.py` | The pre-registered spec/hash, per-cell bootstrap, placebo, Holm |
| `news.py` | Guard and log only; news never closes or blocks a position (the event desk's two news cells open paper positions from the wire, ADR 0028) |
| `newswire.py` | The news wire: Alpaca stream (worker thread), Fed/ECB, German ad-hoc (by ISIN), Handelsblatt, SEC 8-K (job `desk_news_wire`), local-model rating, Telegram alerts; alert-only Alert rules since 2026-11-25: Handelsblatt headlines match a holding by company name (never a fund) and can no longer raise a market-wide alert (only Alpaca's wire can, once per 3 hours); an identical headline within 12 h is stored once (`dup_headline`); the alert is three plain lines with the holding's share of the book, sent without a link preview |
| `news_digest.py` | The 08:17 Berlin weekday digest (rides `desk_reconcile`): stored wire items rated 1+ and provider headlines per holding, central-bank items rated 2+; silent when empty; once a day by `desk_news_digest_json` |
| `cfd.py` | The index rule as a manual Trading 212 CFD line, paper-scored in `index_momentum:cfd`. `prepare_context` (a read) gives the v2 message its index level, Trading 212 quantity, stop in points and costs; `T212` holds the broker facts of 2026-11-23 (display only); `live_text` rebuilds the message for in-place edits |
| `cfd_journal.py` | The owner's journal of the manual CFD trades (`desk_cfd_journal`): entered, skipped, closed, real P&L. Owner input outside the ledger: never a `desk_events` row, never a change to a paper position |
| `intraday.py` | The board snapshot for the Intraday page (`desk_intraday_board_json`, every five minutes); reads first, one write last |
| `cfd_instruments.py` | The event desk's universe (ADR 0028): the four indices and 20 large stocks with Trading 212's observed facts (per-name spread, margin, overnight rates, index contract expiries); `for_symbol` makes an ad-hoc stock instrument; `reference_price` delegates to `service.underlying_price` |
| `prereg_events.py`, `event_limits.py`, `scoring_events.py` | The event desk's second registration (`EVENT_HASH`, seven cells, rule parameters from `foundation/technical.py:EVENT_RULES`, costs, `EVENT_LIMITS`) and its scorecard; `PREREG_HASH` stays untouched |
| `event_rules.py`, `event_sources.py`, `event_sizing.py`, `bankroll.py` | Daily-bar technical rules (evaluated 20 minutes before each close), hype and news-drift candidates (reads only), risk-based size (5 EUR at the stop, costs included), the bankroll guard on the owner's logged real losses |
| `events.py`, `event_messages.py`, `event_report.py`, `outmsg.py` | The engine (one monitor stage after the index rule: reads, then exits and entries in savepoints, one commit, messages from stored rows), the one CFD message format with its evidence line, the Signals page and weekly digest data, the shared `OutMsg` and buttons. `cfd.manage` skips the event cells; `cfd.EVENT_OWNER_CLOSE` routes the owner's Close to `events.owner_close` (a hook, so `cfd` never imports the engine) |
| `instrument.py` | Names the exact product: issuer name, ISIN/WKN, leverage and barrier or omega and strike, expiry, order line |
| `telegram.py`, `jobs.py` | `/idea /desk /stop /close /cfd` + "Paper: take" and the CFD buttons (`cfd:in`, `cfd:skip`, `cfd:out`); 1-minute monitor (exits first, then the index rule; an outdated wrapper fails the tick and sets `wrapper_outdated`, which only a tick that made reads clears; other failed reads become `last_error`; quote ages and fills use the wall clock; each index in its own savepoint, so one failed read rolls back only that index; fills announced by the ids opened this tick), hourly reconcile, 1-minute news wire (a feed's first successful read is stored as `desk_news_wire_read_json`; only that read is a silent baseline) |

## Where it shows

- The Desk page (`/desk`), a research tool in the sidebar (shown with "Show research tools"), also in the command palette: limits, today's live-rehearsal counts, health (last check, failing reads, the news stream), ideas and taps, open positions with Close, recently closed exits with their reason in words, the scorecard, the news wire and the event log.
- The Control Center's attention list (and the 06:00 Telegram self-check built from it): halt, broken chain, outdated wrapper, failing market reads, a news stream down for 15 minutes, positions without a quote, external trades.
- The Intraday CFD page (`/desk/intraday`): the four indices' board, a position calculator, the journal.
- The Signals page (`/desk/signals`): the bankroll, open event positions, every cell of both registrations with verdict chips (too early / failing / not significant / passing), the replay beside the live paper record, the owner's real trades beside the paper ones.
- Telegram: `/idea`, `/desk`, `/stop`, `/close`, `/cfd`, the "Paper: take" and CFD (Entered, Skipped, Closed) buttons, the morning heads-up and get-ready messages, one live-edited CFD message per trade, exit messages, news alerts.

## Public surface

The package root (`app.decision.desk`). A facade-only import-linter contract forbids deep imports from
every other context, with zero seeded exceptions.

## Invariants

- Limits are never settings and never raised from the UI or API.
- Every state change is a ledger event in the same transaction as the position update.
- One fill and one exit per position: a tap locks the position row (`take`), and every close takes the
  position out of `open` with a conditional UPDATE (`claim_open`), so of two closes at once only one is recorded.
- One lock order on every desk write path: the desk's write lock (`ledger.desk_write_lock`, a PostgreSQL
  transaction advisory lock), then a position row, then the ledger's next seq. The monitor and an owner's Close
  cannot deadlock.
- The lock is never held across network reads: a proposal reads everything first (`prepare_proposal`) and
  writes last (`record_proposal`; "both" reads both products first), the exit checks read every price before
  closing, the monitor commits after each stage (expiry, exits, index rule, news) and sends that stage's
  messages right after, and a tap re-reads the clock once it holds the lock.
- The index rule reads every index (r1, both products with their fill quotes, the reference price, the
  CFD's) before its first write. A failed knock-out or warrant read is logged for that product
  (`read_failed`); the CFD, priced from the reference alone, still goes out.
- A halt, horizon or cut-off exit without a fresh bid (Scalable products and CFDs) waits for one, up to the
  10 minutes `quote_lost` allows or the monitor's last tick of the day (21:59), before closing at the last bid.
- A tap reads the quote and the underlying's price before it takes the write lock, and re-checks
  everything under it. The wrapper-outdated flag is cleared only by a tick whose sc reads went through.
- The news stream's receive loop never waits on the model: a bounded queue (500) feeds one rating worker.
- The Desk page's position list always holds every open position; logged external trades are left out of it.
- A holding is watched by its ISIN, and by a ticker only when that is a US one (no suffix, or Trading 212's
  US line): `ALV.DE` is never matched by `ALV`.
- Telegram's webhook answers at once and handles the update after the response; a re-delivered update id
  is skipped (an idea is never proposed twice). Feed links are kept only when they are http(s).
- The news stream's state (`desk_news_stream_json`) is "connected" only after Alpaca accepted the login, and its
  `news_stream_at` is when the state began; a desk position on a US stock is watched by its ticker as well.
- `external` positions (the owner's own app trades) are logged and scored separately, never in a pass-rule cell. They are found from the Scalable activity ledger itself (a round trip between two syncs counts), each ISIN classified by the depot snapshots or, once, by sc's quote (`desk_isin_types_json`).
- A trade without a recorded underlying move is left out of the placebo; over 10 % of a cell without one: no placebo, no pass (pre-registered, `missing_move`). The fill falls back to the proposal's reference read, the exit to the reference's last chart point of 30 minutes.
- Proposal expiry skips a row a tap holds and never overwrites a status that is no longer `proposed`.
- Phase 2 (real orders) is Proposed in ADR 0027 and not built.
- A CFD is never automated (`would_take_live` is always False); Trading 212 stays read-only.
- Only the event desk's `hype_fade` and `news_drift` cells read `news_wire_items`, to open **paper** positions (ADR 0028, which changed this ADR 0027 invariant). Nothing else reads it to act, and nothing reads it to close or block a position.
- The journal (`desk_cfd_journal`) and the heads-up notices (`cfd_notices` in the health key) are owner-facing
  records and previews. `preview_due` is a read phase plus one `set_health` write: no `append_event`, and
  `fire_due_signals` recomputes r1 itself. The Trading 212 facts in `cfd.T212` are not limits and never scoring inputs.
- No network read after the first write of a stage: the live edit step builds its text from stored rows.
