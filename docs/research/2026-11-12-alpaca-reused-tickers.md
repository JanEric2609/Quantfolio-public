# Alpaca bars and reused tickers (WP6, checked 2026-11-12)

**Finding (web check, not tested against the live API).** Alpaca keys its historical bars by ticker symbol, not
by asset id. Forum threads report the history of one symbol joining two companies: "Q" (staff confirmed, fix
requested) and HLTH (Nobilis Health before 2019, Cue Health after its IPO). Staff also say renames are not
followed in raw requests, delisted symbols stay in the database, and the `asof` parameter selects the data tied
to a symbol on a given date. So the audit's TEVA and PARA suspicion is plausible, but the assets endpoint alone
does not fix it: it lists current assets with an id, not the old company's dates.

**Why not keyed on the asset id here.** The series is requested by symbol; an asset id would not select a
different series. A correct fix needs the corporate-actions feed (symbol changes with dates) to cut the series at
the change date, and that has not been built.

**What already limits the damage.** The bar guard quarantines split-like jumps and reversal days, and the full
re-pull compares providers. A reused ticker usually shows as a step in price level that looks like a split, so
the guard catches many of them; a step that does not match a split ratio is stored as real.

**Proposal (owner decision).** Cut an Alpaca series at any gap of 60 or more calendar days inside the stored history
and keep only the segment that ends today. Cheap, loses at most the pre-gap history of a name that really paused,
and needs no new feed. Not built.

Sources: forum.alpaca.markets threads "Get bars returning data from 2 different companies",
"Fixing Data: Historical bars of symbol Q are comprised of two different stocks", "Delisted tickers".
