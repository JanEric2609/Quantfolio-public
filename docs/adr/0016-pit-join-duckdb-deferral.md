# 0016 — PIT Panel Reads: Defer the DuckDB/Polars Migration, Set a Data-Scale Trigger

**Status:** Accepted; Track A implemented 2026-09-23
**Date:** 2026-09-22
**Context source:** `docs/archive/audits/2026-09-22-discover-oom-remediation-plan.md`
§3.1 (Gemini research, 3 rounds) and independent verification of the actual
on-disk data shape in this session.
**Consumed by:** `backend/app/foundation/data_engineering/pit_panel_joins.py`,
`_parquet_combine.py`, `wrds_factors_loader.py`, `ibes_estimates_loader.py`,
`insider_trading_loader.py`, `CONTEXT.md`

## Context

The 2026-09-15 through 2026-09-21 Discover/AlphaCrafter OOM outage (full
writeup: the remediation plan doc above) was caused by a naive
`pd.concat([pd.read_parquet(p) for p in parts])` read pattern across three PIT
loaders, compounded by an unbounded-by-size `lru_cache`. Both were fixed and
shipped (`#264`, `#265`): a single `pyarrow.dataset(...).to_table()` scan
replaces the concat, and `cachetools.LRUCache` with a `getsizeof` callback
replaces the plain `lru_cache`, with per-source byte budgets (WRDS 2GB, IBES
1GB, insider trading 512MB) against a container since bumped to 16GB.

Gemini research (recorded in the plan doc, not repeated here) proposed
migrating the PIT join layer to DuckDB's `ASOF JOIN`, framing it as closing a
**residual** risk the shipped fix doesn't cover: byte-bounded caching stops
*accumulated* cache growth but doesn't bound the *peak* memory of a single
`to_pandas()` conversion or `merge_asof` result. One sufficiently large single
read can still spike memory before it is ever cached — the cache only helps on
the *second* and subsequent calls. DuckDB's out-of-core (spill-to-disk)
execution is a structural fix for that; `pyarrow.dataset().to_table()` is not.
This framing is correct in the abstract and the category of risk is real.

**What changes the calculus here:** the research's sizing anchor — JKP's own
documentation putting a full multi-decade, multi-country extract at
5-15GB — describes WRDS's *raw* product, not what this codebase actually
reads back. Tracing the three loaders end to end:

- All three (`wrds_factors_loader.py`, `ibes_estimates_loader.py`,
  `insider_trading_loader.py`) ingest via `usecols`-restricted, chunked
  (`_CHUNK_ROWS = 20_000`) CSV/TSV reads — `_TARGET_SOURCES`/
  `_CHARACTERISTICS_TARGET_SOURCES` project down to a curated schema (13
  columns for WRDS characteristics: `gvkey`, `permno`, `eom`, `excntry`,
  `size_grp`, `source`, `ingested_at` + 6 numeric value columns) **before**
  anything is written to Parquet.
- The Parquet partitions these loaders read back (`read_parquet_partitions`,
  `read_wrds_factor_characteristics`, etc.) therefore already carry only that
  curated schema — never JKP's full ~150-column raw panel. The "5-15GB"
  figure describes a shape this codebase never persists.
- `pit_panel_joins.py`'s consumers narrow further still: only 5 of the 13
  stored WRDS columns are actually read as signal input
  (`FACTOR_CHARACTERISTIC_VALUE_COLUMNS`).

This was independently confirmed by reading the loader source in this
session, not inferred from the docstrings alone (one of which — the module
docstring in `wrds_factors_loader.py` — is itself stale, still reading "Not
yet exercised against real data" though `CONTEXT.md` and #251 record it was
verified 2026-09-14; worth a one-line fix whenever that file is next touched,
not urgent enough to justify its own PR).

**What doesn't change:** the underlying incident *did* happen on real,
already-ingested production data, on an 8GB container, before the 16GB bump —
so this is not a purely theoretical risk being waved away. What's missing to
make a confident go/no-go call on DuckDB specifically is the one number that
would settle it: actual on-disk Parquet directory size per source in
production, and peak RSS during a real `read_wrds_factor_characteristics()`
call post-#264/#265. That number could not be obtained this session — it
requires a production SSH read, which the harness's own auto-mode classifier
denies outright regardless of in-conversation permission, and getting it
non-urgently doesn't justify escalating for that access today.

## Decision

**Do not build the DuckDB/Polars migration now.** The acute incident is
closed (#264/#265, verified: full backend suite green, byte-bounded cache
confirmed to degrade gracefully rather than crash on overflow). The residual
risk Gemini's research identifies is real in category but its magnitude, on
the evidence available, is materially smaller than the research's own sizing
anchor assumed — a pruned 13-column schema, not a 150-column raw extract —
and the concrete number needed to size it with confidence isn't available
without a production check that isn't urgent enough to pursue out of band.
Building async-executor integration, DuckDB `memory_limit`/`threads` tuning,
and a from-scratch reimplementation of insider trading's bounded-range join
(not an as-of join at all — see Implementation reference) is real engineering
cost that a bounded, currently-unrealized risk doesn't yet justify.

**Set a concrete trigger for revisiting this ADR**, rather than leaving it an
open-ended "someday": migrate whichever source's on-disk Parquet directory
(`get_panel_dir() / "<source>_pit"`) exceeds **3GB** in production (measured
via `du -sh` on LXC 111, or logged from `_dataframe_deep_bytes` the next time
a `pit_panel_joins.py` cache entry is populated — that helper already exists
from #265 and could be logged at INFO on first-populate as a cheap,
already-built early-warning signal). 3GB is chosen to leave the 16GB
container comfortable headroom against a ~2x-3x transient multiplier during
`pyarrow` Table → pandas conversion, alongside the worker process's other
~11 registered cron jobs and their own memory footprints.

**Action item, not gated on the trigger:** the next time WRDS/IBES/insider
data is ingested at real production scale (not the one 2026-09-14
verification pass), record the resulting Parquet directory sizes in
`CONTEXT.md` next to the "verified against real data" note. That number is
what actually resolves whether the 3GB trigger is close or far.

## Implementation reference (preserved for when the trigger fires)

Independently verified this session against DuckDB's own docs/blog, not just
carried over from the research:

- DuckDB's `ASOF JOIN ... USING (key, date)` **coalesces the date column**,
  silently discarding the right-hand table's actual matched date — confirmed
  against duckdb.org/2023/09/15/asof-joins-fuzzy-temporal-lookups and the
  docs. Use an explicit `ON` clause with `SELECT r.date AS right_date`
  instead of `USING`.
- DuckDB's default `memory_limit` is 80% of **physical host RAM**, not the
  container's cgroup limit — confirmed via
  duckdb.org/docs/current/operations_manual/limits. Must be set explicitly
  in a constrained LXC. Some DuckDB operations bypass the buffer manager
  entirely and can exceed even an explicitly-set `memory_limit` — it is a
  soft cap, not a hard guarantee.
- Polars' `join_asof` requires physically sorted input and **raises** rather
  than silently misbehaving — stricter than pandas' `merge_asof`, which
  assumes sortedness and is undefined-behavior on a violation.

Concrete migration shape, if/when the trigger fires:

1. Simple as-of lookup (WRDS factors, IBES) — `ASOF JOIN` with an explicit
   `ROW_NUMBER() OVER (PARTITION BY key, date ORDER BY ingested_at DESC)`
   dedup CTE for "last-update-wins" semantics (mirrors the current
   `drop_duplicates(subset=[...], keep="last")` pandas logic).
2. Two-hop as-of chain (symbol → gvkey via ISIN, then gvkey → characteristics
   via eom) — sequential CTEs, not one flat join list.
3. **Insider trading is not an as-of join.** It is two trailing-window
   rolling aggregations (30-day distinct-`cik` count, 90-day signed-dollar
   sum) per requested date — needs a bounded range join
   (`BETWEEN ... AND ...` + `GROUP BY`), not `ASOF JOIN` or a naive window
   function (breaks on irregular/missing dates in the panel). This is the
   largest of the three to actually implement.
4. Must run inside a thread-pool executor (`loop.run_in_executor`) — DuckDB
   execution is synchronous/CPU-bound and would otherwise block the asyncio
   event loop `quantfolio-worker` also uses to schedule its other ~11
   registered cron jobs in the same process.
5. Safe config for that shared process: explicit `memory_limit` (leave
   headroom for sibling pandas-based jobs, not DuckDB's 80%-of-host default),
   explicit `threads` (leave a core free for the event loop), and a
   `temp_directory` with real disk behind it (verify `/tmp`'s backing store
   on the LXC before relying on spill-to-disk).

## Consequences

- No code changes ship from this ADR (superseded in part — see Addendum below). `pit_panel_joins.py` and the three
  loaders are unchanged as of the original decision.
- The 3GB-per-source trigger is a manual check, not an automated alert —
  nothing pages on it today. If that's later judged insufficient, wiring
  `_dataframe_deep_bytes` into a logged metric on cache populate (mentioned
  above) is the cheap first step before building any alerting around it.
- If production data never approaches 3GB per source (plausible: the
  Discover/AlphaCrafter universe is tradeability-filtered to instruments
  actually reachable at DKB, not the full CRSP/IBES/SEC universe), this ADR's
  natural outcome is that the migration never happens — that is an accepted,
  intended possibility, not a deferred failure.
- The stale "Not yet exercised against real data" docstring in
  `wrds_factors_loader.py` is left as-is; noted here so it isn't rediscovered
  as a fresh finding later.

## Alternatives considered

**Build the DuckDB migration now, as the research recommended.** Rejected for
now: real engineering cost (executor-thread integration into a shared
asyncio worker process, a from-scratch bounded-range-join reimplementation
for insider trading, new `ASOF JOIN`/`memory_limit` correctness surface)
against a risk whose magnitude, on the evidence actually available, is
smaller than the research's sizing anchor assumed and is not yet confirmed to
be realized in production at all.

**Do nothing further, close the topic entirely.** Rejected: the risk category
is real (a single sufficiently large read is not protected by a byte-bounded
*cache*, which only bounds accumulation) and the incident did happen once on
real data. Closing the topic without a concrete revisit trigger would repeat
the exact failure mode that caused this ADR to be needed — a documented
concern with no owner and no threshold.

**Add `columns=` projection to `read_parquet_partitions` now, as a cheap
partial hardening independent of the DuckDB question.** Considered
reasonable but out of scope for this ADR — the three current callers already
write pruned schemas at ingest time, so this would only protect a
hypothetical future loader that doesn't. Worth doing opportunistically
whenever a new PIT loader is added, not urgent enough to justify its own PR
today.

## Addendum (2026-09-23): Track A shipped

**What changed:** The async-executor concern in the original Implementation
reference (item 4, `loop.run_in_executor`) does not apply to this codebase.
`quantfolio-worker` uses `BackgroundScheduler` (thread pool), not
`AsyncIOScheduler`, so DuckDB execution does not block any asyncio event loop.
The API-path call sites are plain `def` handlers, also thread-pooled by
Starlette. This removes an entire engineering track the original deferral
budgeted for.

Additionally, the actual risk (peak memory on a single full-dataset read) can
be closed much more cheaply via DuckDB predicate pushdown at the Parquet-scan
level: filtering to the requested symbol's `gvkey`/ticker *before* the
DataFrame is ever materialized keeps the in-memory frame symbol-sized, not
dataset-sized, for any dataset size. This is **Track A** in
`docs/archive/plans/2026-09-22-pit-join-duckdb-migration.md`: `read_parquet(...) WHERE
gvkey IN (...)` queries replacing the cached full-frame read, keeping existing
pandas `merge_asof`/trailing-window logic unchanged.

**What was implemented:** Track A (Phases 1-6 of the migration plan) shipped
2026-09-23 independent of the 3GB trigger, because it is cheap and strictly
safer than the status quo with no behavior change:
- New module `backend/app/foundation/data_engineering/_pit_duckdb.py`: lazy
  process-lifetime DuckDB connection, config resolver, three query functions
  (one per source: WRDS factors, IBES, insider trading).
- `pit_panel_joins.py` rewired to use the new DuckDB path instead of
  cached full-frame reads; byte-bounded caches removed.
- Parity tests added (old-path-vs-new-path golden comparison).
- Docs updated (this ADR, remediation plan, CONTEXT.md, AGENTS.md).

**Track B (shipped 2026-09-24, ahead of the trigger by owner decision):**
`pit_insider_signal_for_symbol`'s per-date Python loop (30-day distinct-`cik`
cluster count, 90-day signed-dollar flow) is now one bounded-range join in
`_pit_duckdb.insider_trailing_window_signal` (`filing_date > start AND
filing_date <= d`, `GROUP BY` date; the 30-day window is nested in the 90-day
one, so one join feeds both aggregates). Routine-insider classification
stays in pandas. The flow sum uses Kahan summation (`fsum`), so it can differ
from the numpy result in the last bits only; `NaN` propagation is preserved
explicitly. Parity is pinned by randomized tests against a verbatim copy of
the old loop.

**New dependency:** `duckdb>=1.5,<2` added to core dependencies (already
installed 1.5.5); `memory_limit`, `threads`, and optional `temp_directory`
configured explicitly (not relying on DuckDB's 80%-of-host-RAM default).
