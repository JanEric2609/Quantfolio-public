# ADR 0028: the event desk (dynamic CFD signals, paper-scored)

Status: Accepted (2026-11-24)

## Context

ADR 0027's desk sends one fixed signal: the index rule at 21:30 Berlin. The research review of 2026-11-23 found
no published intraday edge on index futures that survived since its paper; its own replay loses after costs. What
survives does so at longer horizons. The owner nevertheless wants messages that come when there is a reason (a
technical rule, a hype move, a primary-source news item, their own call), with direction, size, stop and exit,
and accepts losses as tuition (ADR 0025 amendment, 20 EUR a month learning budget). The account is about 280 EUR.

## Decision

A **second registration** (`prereg_events.py`, hash `EVENT_HASH`; ADR 0027's `PREREG_HASH` is untouched) with six
paper cells: `ta_trend:cfd`, `ta_band:cfd`, `ta_rsi2:cfd`, `hype_fade:cfd`, `news_drift:cfd`, `owner_call:cfd`.

| Cell | Entry | Exit | Stop |
|---|---|---|---|
| ta_trend | EMA 20 crosses EMA 50 | opposite cross, 10 trading days | 2 x ATR(14) |
| ta_band | close outside Bollinger (20, 2): fade | middle band, 5 days | 1.5 x ATR |
| ta_rsi2 | RSI(2) < 10 above the 200-day SMA (long) / > 90 below (short) | RSI(2) crosses 50, 5 days | 1.5 x ATR |
| hype_fade | US stock +8 % on 3x volume and 3x news, no primary-source item: short | 10 days | 1.5 x ATR |
| news_drift | primary-source release rated materiality 3 and the stock moved 2 sigma the same way | 5 days | 1.5 x ATR |
| owner_call | `/idea X up|down cfd` | stop or hold the owner names (default 5 days) | 1.5 x ATR |

Rule parameters are textbook values, frozen in `foundation/technical.py:EVENT_RULES`. Limits are code constants
(`event_limits.py`), pinned by tests and part of the hash: risk 5 EUR per trade at the stop, at most 3 open and
4 new a day, margin at most half of the pot (280 EUR), no second position in the same instrument within 24 hours.

**Costs** are charged on paper: the instrument's own spread (Trading 212 shows 2 bp to 17 bp across stocks),
the 0.5 % FX fee on USD results, and overnight interest for each night held (Friday counts 3x; stocks about
25 %/yr long, 9 %/yr short; indices 0.0083 % / 0.0139 % a night). Index CFDs roll at expiry; an event-desk
position still open 60 minutes before its contract's expiry is closed on paper with reason `rollover`. No index
position opens in that last hour; after the expiry time the next contract counts.

**Changed invariant.** ADR 0027 said nothing reads `news_wire_items` to open, close or block a position. Now
`hype_fade` and `news_drift` read it to open *paper* positions in their registered cells. Nothing else reads it
to act. There is still no order path: Trading 212 stays read-only, no sc command is added, Alpaca is
market-data GETs only, `would_take_live` stays False.

**Bankroll guard.** From the owner's logged trades (`desk_cfd_journal`): a real loss of 15 EUR in a day or 60 EUR
in a month, or 3 open real positions, switches every message to paper only (no quantity or order lines) until
the next day or the 1st of the month (Berlin). A cell with n >= 40 whose mean is not above zero also sends
paper-only messages.
Messages are rebuilt on every tick, so a stop that trips after a signal also switches that signal's message,
as long as the owner has not entered it; a trade already entered keeps its quantity and stop.

## Amendment 2026-11-24: thin US stocks (first news-drift trade, SHOE)

The first news-drift paper trade showed two faults in how a thin US stock is priced.

- **Entry on the opening print.** The SHOE short (earnings, rated 3 of 3, down) was entered at 15:30:13 Berlin
  at 10.445. That was the first IEX print. Three minutes later the stock traded near 11.
- **Stop on a quote nobody traded at.** The paper stop (11.72) fired at 16:28 on a midpoint of 11.985. Alpaca's
  free quote is from IEX alone; it read bid 11.18 / ask 12.79. No trade that day was above 11.27.

Changes:

1. **Rule change.** `news_drift` takes no entry in the first 15 minutes of the session
   (`open_delay_minutes: 15` in `EVENT_RULES`). The 30-minute news window is unchanged and counts back from the
   entry. At the first allowed entry (open + 15) it therefore covers news from 15 minutes before the open. This
   changes the registered rules, so `EVENT_HASH` changes:
   - the desk registers anew at the first tick after the deploy, and only positions entered after that count;
   - the SHOE trade, priced on the faults above, drops out of the score;
   - the trial ledger gains the six cells of the new hash.
2. **Price (not a rule).** For a US stock or index proxy, the quote midpoint is used only when the spread is at
   most 1 % (`service.MAX_QUOTE_SPREAD`). Above that, the last IEX trade is used if it is at most 5 minutes old
   (`TRADE_MAX_AGE_S`). Otherwise there is no price and the desk waits, as for any missing price (the error code
   is `wide_quote`). Entries, stops and exits all go through this one read.
3. **Messages.**
   - When a paper stop fires on a trade the owner logged with their own fill, the close message compares the
     owner's own stop (fill ± the stop distance) with the closing level. If the owner's stop is not reached, the
     message says so and no longer says "CLOSE now".
   - A stock fill is confirmed in cents and price words, not index points.

## What would make us stop

A cell with n >= 40 whose bootstrap mean is below 0 at Holm-adjusted p < 0.05 stops sending order details. It
keeps running on paper.

## Phase 2 (real orders)

Out of scope.

## Replay (2016-2026 daily bars, 24 instruments, research only)

Per-trade mean in EUR at 5 EUR risk at the stop (the size budgets the overnight interest of every night to the planned exit, as the live sizing does), after spread, FX fee and overnight interest. Raw output: `docs/research/signal-replay-2026-11.json`. This is not the pass test; all cells are scored live.

| Cell | Trades | Mean EUR | t | Hit rate | Mean hold (d) | Max drawdown EUR |
|---|---|---|---|---|---|---|
| ta_trend (EMA cross) | 1071 | -0.43 | -2.91 | 0.41 | 11.7 | 477 |
| ta_band (Bollinger fade) | 3817 | -0.49 | -6.49 | 0.43 | 5.7 | 2037 |
| ta_rsi2 (RSI(2)) | 3202 | -0.16 | -2.45 | 0.60 | 3.5 | 533 |

All three technical rules lose after costs on the replay (with a 15 bp stock spread the trend cell is -0.43, t -2.94). The expectation going in is therefore that they fail the pre-registered rule; the live paper score decides, and the desk keeps them only as a measured null.

(The Alpaca entitlement findings are added when the first live run reports them.)

## Amendment 2026-11-24 (evening): a replay that loses makes the message paper only

The replay summary (`desk_signal_replay_json`) had never been written on the server, so every card said
"replay n/a" and the SIE, BAS and IFX cards went out with a quantity and a stop. Written on 2026-11-24 from the
committed `docs/research/signal-replay-2026-11.json` (three research trials under the current event hash).

A cell whose replay loses after costs clearly (mean below zero, t at or below -2.0, at least 100 replayed
trades; `event_messages.REPLAY_LOSS_T`, `REPLAY_MIN_N`) now sends paper-only messages, like the bankroll stop and
a failing cell: no quantity or stop, "Exit (paper)" only. Today that is all three technical cells. The cells
are still opened and scored on paper exactly as registered; the pre-registration and its hash are unchanged
(message text only). A rule without history (hype fade, news drift, the owner's call) is never affected.

## Amendment 2026-11-26: the strong-event cell (a seventh cell, exits on price)

Owner direction (2026-11-25/26): trade CFDs rarely, only on strong, clear days, and drop the fixed time exit.
The price half was replayed on about 16 years of daily bars of SPX, NDX, DAX and ES50
(`docs/research/2026-11-25-replay-results.md`, section 4): no continue or fade cell passes Holm, so price alone
carries no edge. This cell therefore tests the combination the replay could not (price with news and the macro
calendar, which have no history) forward, on paper, with the owner's consent to register it unproven.

**Rule (`EVENT_RULES["strong_event"]`, frozen).** On each monitor tick, for each of the four index CFDs, outside
the first 15 minutes of its session: the index's day move is at least 1.5 trailing-20-day sigma, still holds
at least 75 % of its extreme (no giveback of more than a quarter), and at least two of three extra signals point
the same way within the last 60 minutes: (a) a market-scope headline rated 3 of 3 in that direction, (b) a macro
release scheduled today, (c) two independent wire sources in that direction (each rated at least 2). This is
stricter than the plan's "3 of 4": the price move is always required. At most three entries in any 7 days; the
desk's own limits (one position per instrument, 3 open, 4 new a day, risk 5 EUR at the stop) still apply.

**Exits (price, not the clock).** Initial stop 1.5 daily ATR; target at 2 x the initial risk; once the trade is 1 x
its risk in profit, the stop trails 1 x risk behind the best price (it never loosens); and the position closes
when the index closes back through the previous close (the evaluation at 17:10 / 21:40 Berlin). A 10-trading-day
backstop (the existing `max_hold_trading_days` limit) closes anything still open; it is a safety net, not an
exit rule. A hit on a trailed stop is recorded as `trail`, the target as `target`, the evaluation exit as
`signal_gone`. The initial stop is read back from the position's ledger `fill` event, so no schema changes.

**Snapshot, not a tick stream.** The move and the hold test read the provider's daily bar for today, once per
monitor tick, through the same stored/cached price history as every other daily-bar rule on the desk (`market.history`);
that bar can lag the live index by up to the cache age. This is deliberate: the rule is a daily-bar rule (like the
replay), a lagging bar only delays or skips an entry, and the entry price and every stop use the live reference
price. A live tick source for the four indices would be a new amendment and a new hash.

**Registration.** `EVENT_CELLS` gains `strong_event:cfd`, `EVENT_SPEC.version` is 2 and Holm runs across seven cells,
so `EVENT_HASH` changes (`9af8516f...`): the desk registers anew at the first tick after the deploy, only positions
entered after that count, and the trial ledger gains the seven cells of the new hash. The six older cells lose
their few days of paper evidence the same way as in the 2026-11-24 amendment. `PREREG_HASH` is untouched.

**What a result means.** The minimum is 40 closed positions, so expect many months at a few a month. Until then the
cell reads "too early" and the message says it is unproven. Only its paper ledger, never the replay, can justify
anything about real money; there is no order path and none is added.
