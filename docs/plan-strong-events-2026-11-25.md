# Strong-event CFD trades: what can be tested now, and the paper alert cell (2026-11-25)

Status: the price half is built as a research replay (`lab/shock_replay.py`, `scripts/shock_replay.py`). The news and
sentiment half is a design for a paper alert cell that is registered only after the replay has been read. Paper only,
brokers read-only, no order path, no desk limit as a setting.

## The idea
Trade CFDs rarely, only when major headlines, trader sentiment and the price all say the same thing strongly.
Costs then matter less because there are few trades, and each trade has a bigger expected move than an ordinary day.

## What has history, what has not
| Ingredient | History in Quantfolio | How it can be tested |
|---|---|---|
| Price shock: a day of at least 2 or 3 sigma that closes in the outer quarter of its range | 2015-2026 daily bars (with `--deep`) | `shock_replay`: continue vs fade, 1 and 5 days, net of costs, one observation per date, Holm over 8 cells |
| Macro event days (CPI, FOMC, payrolls, ECB) | calendar holds 2026-2027 only | forward only; can be added to the filter without a backtest |
| Rated headlines (materiality 0-3, direction, scope) | news wire rows since 2026-11-20 | forward only |
| Trader sentiment (StockTwits, Reddit) | none; scraping is against StockTwits' terms and Reddit access is uncertain | not available; the hype cell uses the wire and price instead |
| Volume | the daily bar type has no volume | not in the replay; a later arm |

## The strict filter (proposed, to freeze after the replay is read)
A "strong event" fires for an index when at least three of these four hold in the same 60 minutes:
1. a market-scope headline rated materiality 3 with a direction (the local model's reading);
2. the index moved at least 1.5 trailing-20-day sigma from the previous close and is holding it (no giveback of more than a quarter of the move);
3. a macro release or central-bank decision is scheduled that day (calendar);
4. a second independent source says the same direction (a second wire item or the Fed/ECB feed).
Rate limit: at most one alert per index per day and three a week in total. The message names the product, the stop (1.5 daily ATR), the exit rule and says plainly that it is a paper signal.

## Registration
Adding a cell changes the event desk's frozen hash (`prereg_events.EVENT_HASH`), so it needs an ADR 0028 amendment and a
new registration with its own trial count, as for the other six cells. Order of work:
1. Read the `shock_replay` result. If neither the 2-sigma nor the 3-sigma cell shows a net effect with a corrected p below 0.05, the price footprint of a big day carries nothing and the filter above rests only on news, which can only be forward-tested.
2. Either way, register the cell as a paper alert and let it collect forward evidence; 40 closed positions is the registered minimum, so expect months at a rate of a few a month.
3. Only the paper ledger's result, never the replay, decides anything about real money.

## What a result means
A positive replay cell would say that big confirmed days tend to run on (or reverse) after costs. It would not say that headlines or sentiment add anything: that part has no history and is tested only forward.

## Exits: no fixed time window (owner direction, 2026-11-25)
The owner wants CFDs only on strong days and no fixed-time exit. The alert cell therefore exits on price, not on the clock:
a protective stop at 1.5 daily ATR from entry, a target at 2 x the stop distance, and a trailing stop that follows the best
price by 1 daily ATR once the trade is half way to the target. A position that has hit none of these is reviewed at each
close and is closed only when the signal behind it is gone (the confirming headline is contradicted, or the index closes
back through the previous close). The shock replay keeps its fixed 1 and 5 day exits on purpose: it measures what the
price footprint of a big day does, not how to exit. If it shows an effect, a second, separately registered replay tests the
stop/target/trailing exit on the same days before the paper cell is registered. Exit constants are code constants, never settings.
