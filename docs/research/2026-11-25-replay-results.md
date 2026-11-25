# Replay results run on prod, 2026-11-25 (research only)

Public SPY/QQQ and index data only; nothing here comes from the owner's broker history. Rules and constants are frozen in the
modules named below; a result here never opens a position and never gates anything.

## 1. Swing trend, daily bars (`lab/swing_replay.py`, first run on the stored bars)
Coverage was only about two years (stored `bar_prices` series; 132 trades, nearly all 2025-26), so this says "no sign of an edge yet", not "none".
- swing_trend: n 132, mean -0.41 EUR per trade, t -1.25, hit 39 %, mean hold 12.5 days; 8 of 47 stops filled beyond the stop (gap overruns).
- flipped control: n 130, mean -0.40 EUR, t -1.40, hit 45 %. The two are indistinguishable, so the trend direction added nothing.
- Re-run with `--deep` (PR #38) for the ten-year version.

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

## 4. Conclusion so far
Across three families (daily swing, 30-minute first-half-hour and rest-of-day, 5-minute cross with exits) and two symbols, net effects are indistinguishable from zero at the registered spread and negative at wider spreads. Exit shape (stop, target, trailing) changes how losses look, not whether they exist. Open: the ten-year swing and shock-day runs (PR #38), and forward evidence for news-confirmed events.
