# Desk runbook (ADR 0027 and 0028)

Everything the desk sends is a paper signal. Nothing is ordered for you: you place any trade by hand in
Trading 212, and the broker connections stay read-only.

## What a message means

```
NVDA short: CFD, manual (Trading 212: NVIDIA (NVDA))
Why: <the rule that fired, with its numbers>
Research: <one line on what the literature says>
Evidence: replay 2016-26 <n and mean, or n/a> | live paper <n trades, mean> (<verdict>)
Quantity ... Loss at the stop ~ 5 EUR.
Stop: ...  Exit: by <date> at <time> Berlin, or when I tell you.
Costs: spread, overnight interest per night, 0.5 % FX on a USD result.
Bankroll: month -12 EUR of the 60 EUR stop.
Paper-scored as <cell>.
```

- **Verdict words.** `too early`: fewer than 40 paper trades (true for the first 2-3 months). `failing`: 40 or
  more and the mean is not above zero. `not significant`: positive but not past the Holm-corrected test.
  `passing`: past the pre-registered test (still paper until the capital ladder says otherwise).
- **Cells.** `ta_trend`, `ta_band`, `ta_rsi2` (daily-bar rules, evaluated at 17:10 Berlin for Xetra names and
  21:40 for US names and indices), `hype_fade` (US stocks), `news_drift` (primary-source news the local model
  rated 3), `owner_call` (your `/idea NVDA down cfd [stop 3%|512.4] [today|1d|5d|10d]`).
- At most 3 positions are open and 4 start per day; an instrument gets no second position within 24 hours.
- An index CFD still open an hour before its contract expires is closed on paper (`rollover`); close yours too,
  because Trading 212 rolls the contract and the price jumps by the futures basis.
  No index position starts in that last hour.
- The weekly digest comes on Sunday at 18:17 Berlin time, summer and winter.

## Logging a trade

Reply to the signal message (or its close message) with your prices as Trading 212 shows them:
`<entry>` and later `<exit>`, or `<entry> <exit>` in one reply, optionally the quantity. The Entered, Skipped and
Closed buttons do the same without prices. The Signals page (`/desk/signals`) and the Intraday page show the
journal. Only logged trades count for the bankroll stop, so log every one.

## Paper only

A message is paper only (no quantity, no stop line) when the bankroll stop is reached, or when its cell is failing.
Paper positions keep running and scoring either way.

## The bankroll stop

Code constants (`event_limits.py`), never settings: a real loss of 15 EUR in a day or 60 EUR in a month (Berlin
calendar), or three open real positions. The day stop lifts the next day, the month stop on the 1st of the next
month, an open-position stop when one closes.

## After a deploy

The event desk registers itself at the first monitor tick (one ledger event, six trials). Run
`python scripts/signal_replay.py --write` once with network to fill the replay evidence line. Do not deploy
between 15:25-16:35 UTC or 20:25-21:05 UTC.
