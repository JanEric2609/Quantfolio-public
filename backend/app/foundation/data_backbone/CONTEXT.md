# Context: Data Backbone

## Responsibility

Time-series storage layer for OHLCV bars, factor loadings, and regime
snapshots — the read/write API over three flat hypertables (`bar_prices`,
`factor_loadings_daily`, `regime_snapshots`), each accessed via raw SQL
(`text(...)`), not ORM entities: none of the three tables is
SQLAlchemy-mapped, so `Base.metadata.create_all()` in test fixtures never
creates them (tests seed them via raw `CREATE TABLE`/`INSERT`, see
`tests/test_regime_input_hygiene.py`'s `_memory_db()`/`_seed_bars` for the
pattern).

- `bars.py::BarStore` — reads `bar_prices` (`get_bars`, `get_coverage`,
  `get_latest_bar`, `list_symbols_with_data`). Read-only; nothing here writes
  bars.
- `ingest.py::DataIngester` — the write path. Pulls OHLCV/macro data through
  `app.foundation.providers.registry.build_provider_registry` (the 12-provider
  fallback chain) and upserts into `bar_prices`/`MacroIndicator`, recording
  provider outcomes to `ProviderHealth` via `_record_provider_health`.
- `factors_store.py::FactorStore` — read/write API over
  `factor_loadings_daily` (AlphaCrafter's per-symbol factor exposures).
- `regime_store.py::RegimeStore` — read/write API over `regime_snapshots`
  (the regime engine's persisted classifications).
- `listing_currency.py` — the unit a symbol's stored prices are quoted in
  (ADR 0007 amendment 3). `resolve_quote_currency` / `resolve_currency`
  read the `listing_currencies` table (the provider-reported currency),
  then a caller's live-quote label, then the listing suffix. Ingestion
  records the provider's currency and stamps it on the bars. The worker's
  `listing_currency_audit` job (`audit_listing_currencies`, at start and
  daily) fills the table for older symbols and relabels their stored
  `bar_prices`/`price_cache` rows. Do not read `bar_prices.currency` or
  `Holding.currency` as a price's currency: until the audit, 429 of 780
  symbols carried a guessed label, and DKB books foreign shares in EUR.

## Public surface (facade `app.foundation.data_backbone`)

`BarStore`, `DataIngester`, `FactorStore`, `RegimeStore`; `listing_currency` is
addressed as its own module.

## Key collaborators

- In: `app.foundation.market` (`BarStore`, highest-fidelity source in
  `history()`'s cache chain — checked before `PriceCache`), `app.lab.regime.classifier`
  (`BarStore` for HMM feature bars), `app.decision.discover.pipeline` /
  `discover.regime_gate` (`RegimeStore`/`BarStore`), `app.lab.alphacrafter.{miner,orchestrator,panel,screener}`
  (`FactorStore`), `app.decision.llm_portfolio.gates` (`RegimeStore`),
  `app.decision.recommendation_engine.context_builder` (`RegimeStore` — the
  documented route for regime state, not `app.lab.regime` directly, see
  `regime/CONTEXT.md`), `app.foundation.price_backfill` / `app.worker`
  (`DataIngester`), `app.interface.api.{alphacrafter,attribution,data}`.
- Out: `app.foundation.models.entities` (`MacroIndicator`, `Asset`, `ProviderHealth`),
  `app.foundation.providers` (`build_provider_registry`, `FredProvider`).

## Contract invariants

- Named `source_module` on "Foundation services never import the decision
  loop" — must never import `advisor`/`discover`/`graduation`/`llm_portfolio`/
  `paper_portfolio`/`jobs`. Confirmed clean: its only imports are
  `app.foundation.models.entities` and `app.foundation.providers` (see above) — no
  decision-loop or even sibling-foundation imports beyond those two.
- Not a member of "Decision-loop packages are independent" and not a member
  of any of the six facade-only contracts — it sits below the decision loop
  entirely, consumed by it rather than participating in it.
- Global layering holds (interface → decision → lab → foundation).

## Owner-wave notes

Three genuinely separate concerns share this package only because they're
all "flat hypertable read/write APIs" — there's no shared state or base
class between `BarStore`/`FactorStore`/`RegimeStore`, each just wraps its own
table. `bar_prices` is the highest-fidelity price source in
`market.history()`'s three-tier fallback (bars → `PriceCache` → live
provider fetch); a symbol with rows in `bar_prices` always wins over
`PriceCache`, so a stale/wrong `bar_prices` row is invisible to `PriceCache`
staleness checks and will not self-heal via `market.history()`'s normal TTL
path — check `bar_prices` directly, not just `PriceCache`, when debugging a
price that "should have refreshed but didn't." `_estimate_gaps` in
`bars.py` uses `generate_series`, a PostgreSQL-only function — silently
returns `-1` ("unknown") on SQLite (dev/test), not a bug so much as a
reminder that gap-coverage reporting is untested outside Postgres.
