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

## What would make us stop

A cell with n >= 40 whose bootstrap mean is below 0 at Holm-adjusted p < 0.05 stops sending order details. It
keeps running on paper.

## Phase 2 (real orders)

Out of scope.

## Replay (2016-2026 daily bars, 24 instruments, research only)

Per-trade mean in EUR at 5 EUR risk at the stop, after spread, FX fee and overnight interest. Raw output: `docs/research/signal-replay-2026-11.json`. This is not the pass test; all cells are scored live.

| Cell | Trades | Mean EUR | t | Hit rate | Mean hold (d) | Max drawdown EUR |
|---|---|---|---|---|---|---|
| ta_trend (EMA cross) | 1071 | -0.46 | -2.80 | 0.41 | 11.7 | 508 |
| ta_band (Bollinger fade) | 3817 | -0.52 | -6.47 | 0.43 | 5.7 | 2160 |
| ta_rsi2 (RSI(2)) | 3202 | -0.18 | -2.61 | 0.60 | 3.5 | 595 |

All three technical rules lose after costs on the replay (with a 15 bp stock spread the trend cell is -0.46, t -2.83). The expectation going in is therefore that they fail the pre-registered rule; the live paper score decides, and the desk keeps them only as a measured null.

(The Alpaca entitlement findings are added when the first live run reports them.)
