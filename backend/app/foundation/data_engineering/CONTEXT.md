# Context: Data Engineering

## Responsibility

The point-in-time (PIT) Parquet data panel introduced by ADR 0015 Phase 3
("data spine"). A new, additive foundation-tier package — it does not move
or rename any existing `data_backbone`/`providers` code; that consolidation
is Phase 5's "zero behaviour change" restructure (ADR 0015 ruling #21:
measurement fix first, restructure after). Two storage systems coexist by
design during this phase (ADR 0015, Consequences): `bar_prices`/`PriceCache`
keep serving the live write/read path unchanged; this package adds a
Parquet-file panel for Phase 4's lab to read from.

- `panel_schema.py` — the canonical PIT panel schema (`symbol, isin,
  as_of_date, open, high, low, close, volume, currency, source,
  ingested_at`). `source` is `"internal"` or `"datastream_pit"`, so rows from
  both producers below coexist in the same panel without collision.
  `validate_panel_frame` fails loud (`PanelSchemaError`) on a missing column,
  unknown `source`, or a dtype that can't be coerced — never a silent
  partial import (see the ADR 0014 bare-except lesson referenced throughout
  this package).
- `panel_export.py::export_panel` — builds/refreshes the panel from
  `bar_prices`, via `app.foundation.data_backbone.BarStore` (no parallel raw-SQL
  path). One partition file per symbol
  (`<panel_dir>/internal/<symbol>.parquet`); idempotent, `--dry-run` by
  default (CLI: `python -m app.foundation.data_engineering.panel_export`).
- `datastream_loader.py::load_datastream_extract` — parses a manually
  exported Refinitiv/LSEG Datastream CSV (Excel Add-in / WRDS DSWS export
  shape), validates it against the same schema, tags rows
  `source="datastream_pit"`. **Not yet exercised against real data**: the
  owner's Datastream access isn't set up yet (deferred by explicit choice,
  2026-09-05) — this module exists ready for whenever that changes.
- `fundamentals_schema.py` / `fundamentals_loader.py` — the point-in-time
  *fundamentals* panel (Compustat Global `g_funda`), sibling to the OHLCV
  panel above. Separate schema for one structural reason: a price is knowable
  on its `as_of_date`, an accounting figure is not. Every row therefore
  carries both `datadate` (fiscal period end) and **`available_from`**
  (`datadate` + a publication lag, default 6 months — the Fama-French
  convention). Consumers filter on `available_from`; filtering on `datadate`
  reproduces the lookahead the module exists to remove — see
  `app/lab/alphacrafter/panel.py`'s own "Point-in-time caveat", which this is
  the eventual fix for. `read_fundamentals(as_of=...)` does that filtering.
  Two deliberate refusals: it will not pick between `indfmt=INDL` and
  `indfmt=FS` presentations of one company-year (filtering to INDL, the
  reflex from Compustat North America recipes, silently deletes every bank
  and insurer), and it converts no currencies — `curcd` (reporting) can
  differ from `curcdd` (quotation), which anything computing book-to-market
  must reconcile first. **Verified against real data 2026-09-14** (#251).
- `ccm_link_schema.py` / `ccm_link_loader.py` — the CRSP/Compustat Merged
  (CCM) link-history panel: which `permno`/`permco` *is* a given `gvkey`, and
  during which `[linkdt, linkenddt]` window (a null `linkenddt` means still
  active). Sibling of `security_identifiers_schema` in shape, but bridging
  CRSP and Compustat's independent identifier systems rather than gvkey and
  ISIN. `read_ccm_link(research_quality_only=True)` applies the standard
  `linktype in (LC, LU)` / `linkprim in (P, C)` recipe every WRDS/CCM guide
  recommends; off by default so raw-history inspection isn't pre-filtered.
  **Verified against real data 2026-09-14** (#251).
- `crsp_names_schema.py` / `crsp_names_loader.py` — CRSP ticker history
  (`permno`, `ticker`, `trading_symbol`, `[namedt, nameenddt]`), from a WRDS
  web export of either `crsp.stksecurityinfohist` (CIZ) or
  `crsp.stocknames`/`dsenames` (legacy). The only panel carrying a ticker on
  the WRDS side: `pit_panel_joins._resolve_gvkey_as_of` falls back to
  `ticker -> permno` (this panel) `-> gvkey` (research-quality CCM) for
  suffix-less (US) symbols the security master doesn't know — which on prod
  (2026-09-24) was every universe stock, since the security master holds only
  the ~31 securities users have held or imported. Ambiguous dates (a ticker on
  two permnos, a permno on two best-rank gvkeys) stay unresolved rather than
  guessed. Results are stamped `data_confidence="crsp_ticker_ccm"`.
- `ibes_estimates_schema.py` / `ibes_estimates_loader.py` — the IBES analyst
  consensus panel (`ibes.statsum_epsus`): monthly median/mean/std/count of
  analyst EPS estimates per security per forecast period. `statpers` is the
  point-in-time anchor (mirrors `fundamentals_schema.available_from`); do not
  confuse it with `fpedats` (which fiscal period the forecast concerns) or
  `anndats_act` (when the actual was announced). `ticker` is IBES's own
  internal identifier, not joinable to this app's gvkey/ISIN panels without a
  separate WRDS ICLINK+CCM chain this module does not attempt — see the
  schema module's docstring for the street-vs-GAAP earnings trap too.
  **Verified against real data 2026-09-14** (#251). The module's
  `read_ibes_estimates` function remains the public facade; as of 2026-09-23,
  `pit_panel_joins.py` calls the DuckDB query function
  `_pit_duckdb.ibes_estimates_for_oftic` instead, which filters in the
  Parquet scan to every IBES `ticker` that ever carried the requested `oftic`
  (the dedup-key group, so a superseded row can't leak), then dedups like
  `read_ibes_estimates`.
  Since no ICLINK crosswalk exists, the direct ticker-string match is tagged
  `data_confidence="ticker_match"` end-to-end (join → Pydantic model → LLM
  prompt caveat) to distinguish it from gvkey-verified sources.
- `wrds_factors_schema.py` / `wrds_factors_loader.py` — "WRDS Factors" is
  genuinely ambiguous between two structurally incompatible shapes (see the
  schema module's docstring): a portfolio-level Fama-French-style factor
  *return* time series (percent units — already covered live by
  `quant_factors.py`'s Ken French fetch, so confirm this isn't a redundant
  second copy before ingesting), or a JKP-style firm-level factor
  *characteristics* panel (decimal units, `gvkey`/`permno`/`eom`-keyed).
  `load_wrds_factors_extract` inspects the header via
  `detect_wrds_factor_shape` and routes to whichever matches, refusing
  rather than guessing when neither pattern is found. **Verified against
  real data 2026-09-14** (#251) as the JKP-style characteristics shape.
  The module's `read_wrds_factor_characteristics` function remains the
  public facade; as of 2026-09-23, `pit_panel_joins.py` calls the DuckDB
  query function `_pit_duckdb.wrds_factor_characteristics_for_gvkeys` instead,
  which reads via `read_parquet(...) WHERE gvkey IN (...)` at the Parquet-scan
  level for efficient predicate pushdown. **Widened 2026-09-24** from 4 to 41
  characteristics, two to five per JKP theme
  (`JKP_CHARACTERISTICS_BY_THEME`), plus `gics`. The loader reports any it
  asked for that the extract lacks (`missing_characteristics`), and both
  read paths scan with the partitions' unified schema
  (`_parquet_combine.unified_schema`), so older, narrower partitions read the
  new columns as null instead of hiding them. What to pull from WRDS:
  `docs/runbooks/wrds-jkp-extract.md`.
- `insider_trading_schema.py` / `insider_trading_loader.py` — SEC Form
  3/4/5-derived stock transactions. **Re-sourced 2026-09-14 (#251)** from the
  originally-planned WRDS Thomson Reuters/LSEG Insiders (Table 1) extract to
  SEC EDGAR's own quarterly bulk "Insider Transactions" data sets (no WRDS
  subscription needed): each quarterly zip's `SUBMISSION.tsv` (one row per
  filing), `REPORTINGOWNER.tsv` (one row per filing×insider — a filing can
  have 2+ co-filers, verified up to 10 on one real filing), and
  `NONDERIV_TRANS.tsv` (one row per transaction line) are joined on
  `ACCESSION_NUMBER`. Two consequences of the re-source: no `cusip`/
  `cleanse_code` (WRDS-derived fields SEC's raw filings don't carry — always
  null from this pipeline), and dates parsed explicitly from SEC's
  `DD-MON-YYYY` format rather than pandas' inference. `filing_date`, never
  `transaction_date`, is still the point-in-time anchor — a trade isn't
  public until its Form 4 is filed (up to 2 business days later), and
  validation refuses a row where `filing_date` precedes `transaction_date`.
  Only transaction codes P (open-market purchase) and S (open-market sale)
  carry research signal; every other code (option exercises, RSU vesting,
  tax withholding, gifts) is company- or tax-driven and is stored, not
  filtered, so row counts match what SEC shipped —
  `read_insider_trading(open_market_only=True)` applies the P/S filter on
  read. Not chunked on write (a full quarter is ~50-100k transactions,
  ~10-15MB of TSV — small enough for one pass). The module's
  `read_insider_trading` function remains the public facade; as of 2026-09-23,
  `pit_panel_joins.py` calls the DuckDB query function
  `_pit_duckdb.insider_trading_for_ticker` instead, which filters in the
  Parquet scan to every `issuer_cik` that ever filed under the ticker (the
  dedup-key group), then dedups like `read_insider_trading`.
- `sec_edgar_insider.py` (added 2026-09-29) — keeps the insider panel
  current. The panel had been a one-off extract ending 2024-03-29, so the
  insider signal was "unknown" for every US name.
  - The daily `insider_trading_refresh` job (04:15 UTC, registered in
    `worker.py`) first loads every quarterly set since 2024 Q2 that the
    panel lacks. It reads the links from SEC's page, because the 2026 Q2
    zip moved to a different path.
  - It then reads each Form 4 and 4/A filing from EDGAR's daily index for
    the days after the newest quarter, into `edgar_daily_YYYYMMDD.parquet`
    in the same schema.
  - Amounts are rounded half-up to 2 decimals, as the quarterly sets store
    them. With that, a day ingested this way matched SEC's quarterly set on
    all 2,054 rows (2026-06-15).
  - Loading a quarter removes its daily partitions.
  - Days go oldest first, skipping US federal holidays, and stop at the
    first unpublished day less than a week old, so the coverage end never
    runs past a gap.
  - SEC fair access: 8 requests a second with back-off, and a User-Agent
    from the `sec_edgar_user_agent` setting. SEC answers 403 ("Request Rate
    Threshold Exceeded") to a User-Agent without an email address, the
    built-in default included, so the setting is required in practice.
  - `plan_10b5_1` (added 2026-09-30) is the Rule 10b5-1 plan checkbox:
    `SUBMISSION.AFF10B5ONE` in the quarterly sets (SEC added it to the
    2023-2025 sets in July 2025), `<aff10b5One>` in the daily XML. It is one
    flag per filing, null before April 2023 and in partitions written
    earlier. `pit_insider_signal_for_symbol` drops ticked trades from the
    signal alongside the routine-trader filter (owner decision 2026-09-30).
  - A day takes ~90 s. The first run (9 quarters plus ~3 months of days)
    takes about an hour.
  - The loader also stopped rejecting a whole quarter over a line with no
    transaction code. 2026 Q2 has two such lines out of 78,328; they are now
    dropped and counted.
- `_pit_duckdb.py` (added 2026-09-23) — internal-use DuckDB module for
  predicate-pushdown queries. Provides a process-lifetime in-memory DuckDB
  connection (lock-guarded lazy singleton), configuration resolver
  (`QUANTFOLIO_DUCKDB_MEMORY_LIMIT` / `duckdb_memory_limit`, default "2GB";
  `QUANTFOLIO_DUCKDB_THREADS` / `duckdb_threads`, default "2";
  `QUANTFOLIO_DUCKDB_TMPDIR` / `duckdb_temp_directory`, default unset), and
  three query functions that filter Parquet scans to the requested symbol(s)
  before materialization (`wrds_factor_characteristics_for_gvkeys`,
  `ibes_estimates_for_oftic`, `insider_trading_for_ticker`). Not exported
  from the public facade — used internally by `pit_panel_joins.py` only.
- `panel_read.py::read_panel` — reads both partitions back as one DataFrame
  and resolves cross-partition duplicates by `panel_schema.SOURCE_PRECEDENCE`
  (`datastream_pit` before `internal`), warning whenever a row is dropped.
  Consumed by `app.lab.quant_lab.signal_batch.build_price_panel` (Phase 4);
  still not read by any live advisor/discover/graduation pathway — that
  repoint is a later ADR phase.
- `paths.py::get_panel_dir` — resolves the panel directory: env var
  (`QUANTFOLIO_PARQUET_PANEL_DIR`) → DB setting (`parquet_panel_dir`) →
  default (`/tmp/quantfolio_parquet_panel`), the exact pattern
  `quant_factors.py`'s `_get_cache_dir` uses for the Fama-French cache.
- `pit_panel_joins.py` (added 2026-09-15, #ca16016; rewired 2026-09-23 to use
  DuckDB predicate pushdown) — the cross-loader join helper. Mirrors
  `pit_fundamentals.py` for the two gvkey-keyed sources
  (`pit_wrds_factor_characteristics_for_symbol`,
  `pit_ccm_link_quality_for_symbol`): `symbol → ISIN → gvkey` via
  `security_master`/`security_identifiers_pit` (ISIN lookup pushed down
  into DuckDB since 2026-09-24), same coverage guarantees as
  fundamentals, falling back to CRSP ticker → permno → CCM gvkey for US
  symbols (see `crsp_names_loader` above); WRDS rows older than 92 days are
  not carried forward. `pit_ibes_estimate_signal_for_symbol` and
  `pit_insider_signal_for_symbol` are weaker — no WRDS ICLINK crosswalk
  exists for either, so both fall back to a direct ticker-string match and
  stamp every result `"data_confidence": "ticker_match"` (vs.
  `"gvkey_verified"`) so callers/dossiers can tell the two apart. Each of
  the three "simple" per-symbol joins is called once per symbol in a
  cross-sectional universe scan by `app.lab.alphacrafter.panel.build_panel`.
  As of 2026-09-23, `pit_wrds_factor_characteristics_for_symbol`,
  `pit_ibes_estimate_signal_for_symbol`, and `pit_insider_signal_for_symbol`
  use DuckDB predicate pushdown (`_pit_duckdb.py` query functions) to filter
  the Parquet scan to the requested symbol's `gvkey`/ticker *before*
  materialization, preventing the peak-memory spike on oversized single reads
  that the byte-bounded cache in #265 could not prevent (cache bounds
  accumulation, not the peak of one `to_pandas()` call). The symbol-filtered
  result is then processed with the existing pandas `merge_asof` logic
  unchanged; the insider signal's trailing windows (30-day cluster count,
  90-day dollar flow) run as one DuckDB bounded-range join
  (`_pit_duckdb.insider_trailing_window_signal`, ADR 0016 Track B,
  2026-09-24) over the already-filtered opportunistic frame. `_resolve_gvkey_as_of` is the shared first hop (dates →
  gvkey as of each date) for the two gvkey-keyed joins.

## Public surface (facade `app.foundation.data_engineering`)

`load_datastream_extract`, `LoadResult`, `export_panel`, `read_panel`,
`validate_panel_frame`, `PanelSchemaError`, `PANEL_COLUMNS`, `get_panel_dir`,
`load_fundamentals_extract`, `FundamentalsLoadResult`, `read_fundamentals`,
`validate_fundamentals_frame`, `FundamentalsSchemaError`,
`FUNDAMENTALS_COLUMNS`, `load_ccm_link_extract`, `CcmLinkLoadResult`,
`read_ccm_link`, `validate_ccm_link_frame`, `CcmLinkSchemaError`,
`CCM_LINK_COLUMNS`, `CCM_RESEARCH_QUALITY_LINKTYPES`,
`CCM_RESEARCH_QUALITY_LINKPRIMS`, `load_ibes_estimates_extract`,
`IbesEstimatesLoadResult`, `read_ibes_estimates`,
`validate_ibes_estimates_frame`, `IbesEstimatesSchemaError`,
`IBES_ESTIMATES_COLUMNS`, `load_wrds_factors_extract`,
`WrdsFactorsLoadResult`, `read_wrds_factor_returns`,
`read_wrds_factor_characteristics`, `detect_wrds_factor_shape`,
`validate_factor_returns_frame`, `validate_factor_characteristics_frame`,
`FactorReturnsSchemaError`, `FactorCharacteristicsSchemaError`,
`FACTOR_RETURNS_COLUMNS`, `FACTOR_CHARACTERISTICS_COLUMNS`,
`load_insider_trading_extract`, `InsiderTradingLoadResult`,
`read_insider_trading`, `validate_insider_trading_frame`,
`InsiderTradingSchemaError`, `INSIDER_TRADING_COLUMNS`,
`OPEN_MARKET_TRANSACTION_CODES`.

## Key collaborators

- In: `panel_export.py`/`datastream_loader.py` are still run manually
  (CLI/script), not called from any API route, job, or other service.
  `panel_read.py` is read by `app.lab.quant_lab.signal_batch` (Phase 4). As
  of 2026-09-15 (#ca16016), `pit_panel_joins.py` is read live by
  `app.decision.discover.pipeline`/`dossier_writer`/`composite`,
  `app.decision.recommendation_engine.context_builder`, and
  `app.lab.alphacrafter.panel` — allowed by the layering contract (this
  package is foundation-tier; those are decision/lab-tier, which may import
  down), but the practical effect is that this package's read path is now on
  the *live* Discover/AlphaCrafter critical path, not just a Phase-4-lab
  convenience — see the OOM incident noted throughout this doc.
- Out: `app.foundation.data_backbone` (`BarStore`), `app.foundation.settings`
  (`get_public_settings`, via `paths.py`), `app.foundation.core.db` (`SessionLocal`),
  `app.foundation.security_master` (`pit_panel_joins.py`'s gvkey resolution).

## Contract invariants

- Foundation-tier under "Global layering" (ADR 0015 Phase 5, which subsumed
  the retired "Foundation services never import the decision loop"); per the
  "In"/"Out" lists above it never imports `app.decision`/`app.lab`.
  Confirmed via `lint-imports` (10/10 contracts kept).
- Global layering holds trivially (foundation-tier; this package has
  no `app.interface.api`/`app.foundation.models` imports at all beyond `SessionLocal`).
- `scripts/check_no_new_cycles.py`'s golden ledger was regenerated to add
  this package to the pre-existing `app.services`-rooted package-level SCC
  (0 real module cycles both before and after — confirmed via a diff of
  `compute_current_state()` against the prior golden showing only this one
  member added, nothing else changed). This is a known artifact of that
  script's `_package_of` aggregation, which collapses any bare 3-part
  package name (e.g. `app.foundation.data_backbone`, imported via its facade)
  to `app.services` — so any new foundation package that (a) has a normal
  `__init__.py` facade and (b) imports a sibling package's facade
  mechanically joins that pre-existing blob. Not a new architectural
  entanglement; the real contracts above stayed green.

## Owner-wave notes

**Updated 2026-09-22 — largely superseded by events; kept for history.**
The OHLCV `panel_read.py` path is still not read by any live
advisor/discover/graduation pathway (that repoint is still a later phase),
and `load_datastream_extract` has still never run against real data
(deferred by explicit choice, 2026-09-05 — the owner's Datastream access
isn't set up). But the paragraph this replaces also said "no live
advisor/discover/graduation code reads this panel" and "none has a
cross-loader join helper" as if both were still true — as of 2026-09-15
(#ca16016) neither is: `pit_panel_joins.py` is exactly that cross-loader
join helper, and it is read live by Discover, the recommendation engine, and
AlphaCrafter (see `pit_panel_joins.py` and "Key collaborators" above). The
four WRDS/SEC loaders (`ccm_link_loader`, `ibes_estimates_loader`,
`wrds_factors_loader`, `insider_trading_loader`) were all verified against
real data 2026-09-14 (#251) — see each loader's entry above for what
"verified" means for that specific source (WRDS Factors resolved to the
characteristics shape; insider trading was re-sourced to SEC EDGAR entirely).
Two things to confirm before trusting these modules further against
production data:
- **WRDS Factors' actual product identity.** `wrds_factors_loader` auto-
  detects between a Fama-French-style return time series and a JKP-style
  firm-characteristics panel purely from the extract's header shape; confirm
  against the real WRDS product/table name which one an actual subscription
  provides, and whether the return-series shape would just duplicate
  `quant_factors.py`'s existing live Ken French fetch.
- **The `wrds` package stub.** `backend/wrds/__init__.py` is a vendored
  no-op (`Connection.__init__` raises `NotImplementedError`) that exists only
  to satisfy `finrl`'s unconditional import chain — see the Dependency
  watch-list in `AGENTS.md`. `ccm_link_loader`, `wrds_factors_loader`, and
  `fundamentals_loader` are CSV-extract loaders by design, matching
  `datastream_loader`'s own "manually exported, then ingested" pattern; a
  live `import wrds` pull is not available in this repo and was never the
  intended design. `insider_trading_loader` is the exception as of 2026-09-14
  (#251) — it pulls directly from SEC EDGAR's own public bulk data sets
  (zip-of-TSVs, not CSV), sidestepping the `wrds` stub/subscription question
  for that one source entirely; see its entry above.

Three properties the loader and reader deliberately do *not* guarantee, since
they belong to whoever prepares the extract:

- **Currency is stored as quoted and never converted.** A multi-currency panel
  therefore cannot support any factor built from a price *level*;
  `signal_batch.compute_microstructure_factors` omits `dollar_volume_20d`
  rather than compute one, unless given FX rates. Pence quotation (`GBp`) is
  the one exception it normalises itself, because 100 pence to the pound is
  definitional rather than a rate.
- **Splits are adjusted only when the extract says so.** A Compustat-style
  `ajexdi` column is applied (`price / ajexdi`, `shares * ajexdi`); a missing
  or non-positive factor fails the load rather than producing a half-adjusted
  panel. Datastream's default `P` arrives already adjusted and carries no such
  column.
- **Nothing reconciles the two producers.** `SOURCE_PRECEDENCE` picks a
  winner and logs; it does not check that the loser agreed.

## Prod panel size (measured 2026-09-24, LXC 111)

`/opt/quantfolio/data/parquet_panel` on ext4 (not tmpfs). Largest source
`wrds_factor_characteristics_pit` with **0.70 GB** / 15.5M rows (801 files);
`security_identifiers_pit` 0.25 GB / 16.7M rows (928 files); IBES, insider
and CCM each under 10 MB. ADR 0016's 3 GB-per-source trigger is far off.
`pit_ml_features_for_symbol` over 260 dates: 0.04–0.14 s per symbol, ~247 MB
process peak RSS. JKP characteristics end at `eom=2025-12-31`;
`pit_wrds_factor_characteristics_for_symbol` refuses rows older than 92 days,
so WRDS coverage for recent dates needs a fresh JKP extract.
