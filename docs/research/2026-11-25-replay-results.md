# Replay results run on prod, 2026-11-25 (research only)

Public SPY/QQQ and index data only; nothing here comes from the owner's broker history. Rules and constants are frozen in the
modules named below; a result here never opens a position and never gates anything.

## 1. Swing trend, daily bars (`lab/swing_replay.py`)
First run on the stored bars (about two years, 132 trades): n 132, mean -0.41 EUR, t -1.25, indistinguishable from the flipped control. Re-run with `--deep` (about 16 years from the providers, 2015-2026 trades, 2026-11-26):

| cell | n | mean EUR per trade | t | hit | mean hold | stops (gap overruns) |
|---|---|---|---|---|---|---|
| swing trend | 1036 | -0.10 | -0.82 | 46.7 % | 12.5 days | 343 (47) |
| flipped control | 1022 | -0.18 | -1.53 | 43.4 % | 12.7 days | 333 (55) |

Per instrument (trend cell): DAX -0.40 (t -1.67), ES50 -0.28 (t -1.14), NDX +0.14 (t +0.55), SPX +0.15 (t +0.65); by year the pooled total swings between about -77 and +80 EUR with no pattern. Reading: no edge. The trend is slightly less bad than its flipped control, but neither differs from zero, and the US indices' small positive means are within noise on 260 trades each.

## 2. First-half-hour and rest-of-day predictors, 30-minute bars 2016-2026 (`lab/desk_replay.py --predictors`)
Partial output only (head cut off). QQQ r1 (the desk's rule), all days: n 2072, gross -0.24 bp (t -0.32), CFD net -0.13 EUR per trade (t -4.8). Matches the registered replay. Knock-outs about -2.3 EUR per trade, 0.6 % wins.
SPY rest-of-day (position-inferred): middle volatility band CFD net -0.20 EUR (t -4.1, Holm 3e-4); high band gross +1.9 bp (t 1.1, Holm 0.91), CFD net about 0, sign flips by year (2020-22 positive, 2025 negative).

## 3. 5-minute EMA 9/26 cross with ATR stop, target, trailing and a volatility filter (`lab/intraday_rules_replay.py`)
SPY and QQQ, 1986 days; one observation per day; Holm over five cells per symbol and spread case; `_hv` cells use the full-sample top tercile (a descriptive split).

| cell | SPY 0.6 bp net, t | QQQ 0.6 bp net, t | SPY 3 bp net, t | QQQ 3 bp net, t |
|---|---|---|---|---|
| fixed (1 ATR stop, exit on opposite cross) | -0.25 bp, -0.46 | -0.66, -0.95 | -2.65, -4.75 | -3.06, -4.36 |
| tp1_5 (2 ATR stop, 1.5 x target) | +0.46, +0.63 | -0.88, -0.97 | -1.94, -2.65 | -3.28, -3.61 |
| trail (2 ATR) | -0.14, -0.26 | -0.90, -1.37 | -2.54, -4.69 | -3.30, -4.94 |
| tp1_5, high-vol days | +2.01, +1.11 | -2.30, -1.08 | -0.39, -0.22 | -4.70, -2.19 |
| trail, high-vol days | +0.45, +0.35 | -0.79, -0.49 | -1.95, -1.50 | -3.19, -1.96 |
| control (random direction, tp1_5) | +0.15, +0.20 | +0.03, +0.03 | -2.25, -2.98 | -2.37, -2.51 |

Reading: at the registered 0.6 bp spread no cell has a day-level |t| above 1.4 and every Holm p is 0.86 or 1. The random-direction control is as good as the cross (SPY +0.15 vs +0.46 bp), so the signal adds nothing measurable. At the 3 bp stress spread most cells lose significantly. The one cell that looks positive (SPY tp1_5 high-vol, +2.0 bp) is negative on QQQ in the same cell, which is what noise looks like. Even +2 bp on a typical 1,000 EUR exposure is 0.2 EUR per trade, before the stop slippage a live fill adds. Fixed 1 ATR stops are hit on 65 % of trades (hit rate 22 %).

## 4. Shock days, about 16 years (`shock_replay.py --deep`)
SPX, NDX, DAX and ES50 daily bars, about 4,200 daily bars each from 2010 to 2026, roughly 16 years (stored bars cover only 1.1 to 1.5 years). A shock day is a move of at least 2 or 3 trailing-20-day sigma that closes in the outer quarter of its range; entry at the next open, exit at the close 1 or 5 trading days after the entry day; one observation per entry date; Holm over eight cells.

| cell | events | per year | mean net bp | t (days) | Holm p |
|---|---|---|---|---|---|
| 2 sigma, continue, 1 day | 1045 | 62.5 | -8.0 | -1.20 | 1.00 |
| 2 sigma, continue, 5 days | 786 | 47.0 | -31.9 | -2.25 | 0.20 |
| 2 sigma, fade, 1 day | 1045 | 62.5 | -0.7 | -0.10 | 1.00 |
| 2 sigma, fade, 5 days | 786 | 47.0 | +9.9 | +0.70 | 1.00 |
| 3 sigma, continue, 1 day | 281 | 16.8 | +11.7 | +0.77 | 1.00 |
| 3 sigma, continue, 5 days | 253 | 15.1 | -14.3 | -0.56 | 1.00 |
| 3 sigma, fade, 1 day | 281 | 16.8 | -20.8 | -1.37 | 1.00 |
| 3 sigma, fade, 5 days | 253 | 15.1 | -7.8 | -0.31 | 1.00 |

Reading: no cell passes the correction. The price footprint of a big confirmed day does not by itself give an edge after costs. The only cell with a raw |t| above 2 is a negative one (following a 2 sigma day for five days: -32 bp per trade, raw t -2.25), and it does not survive the correction, so the replay shows no usable continue or fade edge in either direction. The 3 sigma cells have 15 to 17 events a year and flip sign between cells, which is noise. What this does not test: headlines, macro days and sentiment, which have no history here and can only be tested forward.

## 5. Slow trend, commodities and FX (`trend_replay.py`, `commodity_fx_replay.py`, 2000 onward, 200 placebo draws)
Rule: 100-day breakout, EMA50 or 5 ATR state exit, no clock. Costs for commodities and FX are assumptions; the second run doubles them.

- Trend, all three families (indices re-run 2026-11-26 after the loader fix: provider bars back to 1983 for SPX, NDX and DAX and 2007 for ES50, trades from 2000; FX rows updated because EURUSD also got its longer series):

| cell | n | mean EUR at 1x | t | mean EUR at 2x | t | Holm p (1x) |
|---|---|---|---|---|---|---|
| indices, EMA cross | 362 | -0.14 | -0.77 | -0.48 | -2.90 | 1.00 |
| indices, chandelier | 246 | -0.08 | -0.25 | -0.66 | -2.30 | 1.00 |
| commodities, EMA cross | 385 | +0.36 | +1.04 | -0.17 | -0.55 | 0.94 |
| commodities, chandelier | 330 | +0.44 | +1.20 | -0.15 | -0.44 | 0.89 |
| FX, EMA cross | 473 | -0.44 | -2.41 | -0.93 | -5.59 | 1.00 |
| FX, chandelier | 303 | -1.28 | -4.95 | -2.39 | -9.83 | 1.00 |

  Index trend: mean hold 48 days (EMA) and 96 days (chandelier), EMA hit rate 35 % (31 % at 2x); the side placebo (same breakout days, random side) is as good as the signal (p 0.55 and 0.97). It is flat at assumed costs and clearly negative when costs double (t about -2.3 to -2.9; the registered test is one-sided for a positive edge, so this is a description, not a tested loss).
- Volatility-scaled variants (`commodity_fx_replay.py`, EWMA vol): all twelve cells fail Holm (1.00), at both cost levels. Commodities with the trend, EMA exit: +0.06 EUR (t +0.40) at 1x, -0.13 at 2x. The other commodity cells are negative or within noise; FX is negative in every cell (down to -1.45 EUR per trade, t -6.1, at 1x). The side placebo (same breakout days, random side) is as good as the signal in all but one commodity cell (EWMA with, EMA exit, placebo p 0.015 at 1x, 0.025 at 2x), and that cell's mean is +0.06 EUR, so it is not tradable. The implied-volatility variants of FX did not run because the ^EVZ series is not available from any provider (commodity implied variants ran and are negative or null).
- Reading: only commodity trend is positive at assumed costs and it turns negative when costs double, on unadjusted continuous futures with roll splices, so even that is provisional and not an edge. FX trend loses at any cost level.

## 6. Conclusion so far
Across three families (daily swing, 30-minute first-half-hour and rest-of-day, 5-minute cross with exits) and two symbols, net effects are indistinguishable from zero at the registered spread and negative at wider spreads. Exit shape (stop, target, trailing) changes how losses look, not whether they exist. Shock days on about 16 years of index bars add nothing either (section 4). The deep swing run (section 1) and the slow-trend runs on indices, commodities and FX (section 5) add nothing either. Open: forward evidence for news-confirmed events.
