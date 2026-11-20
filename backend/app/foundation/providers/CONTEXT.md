# Context: Providers

## Responsibility

Foundation-tier market-data provider abstraction. Defines the common
`MarketDataProvider` ABC and `provider_result()` envelope (ok/data/quality/error
— `quality` carries `stale`, `as_of`, `missing_fields`, `confidence`,
`warnings`) that every concrete provider returns (base). One adapter module
per external data source — Alpaca, Alpha Vantage, Databento, ECB SDW, EOD,
Finnhub, FRED, justETF, Massive, OpenBB, Tiingo, TwelveData, yfinance — each
implementing only the capabilities that source actually supports
(`capabilities: set[str]` on the class). `ProviderRegistry` (registry) builds
the enabled chain from `AppSetting.provider_chain_json` (DB → env → the
13-provider `_DEFAULT_CHAIN` fallback), fans out `first_success(capability, ...)`
across the chain with a per-provider timeout, sliding-window rate limiting
(rate_limiter), and an adaptive dead-provider cooldown (5 min, matches the
5-minute registry cache TTL) so a provider that just failed for one capability
is skipped on the next call rather than retried immediately. `utils.py` holds
shared adapter helpers (e.g. yfinance exchange-suffix normalization).

## Public surface (facade `app.foundation.providers`)

`ProviderRegistry`, `build_provider_registry`, `invalidate_registry_cache`.

Individual provider classes (`YFinanceProvider`, `OpenBBProvider`, etc.) and
`provider_result`/`MarketDataProvider` are **not** re-exported at the package
root — collaborators that need a single named provider (rare; most go through
the registry) import the submodule directly, which is permitted since
`providers` isn't one of the six facade-only-protected contexts.

## Key collaborators

- In: `app.foundation.market`, `app.foundation.fx_cvt`, `app.foundation.etf_lookup`,
  `app.foundation.news`, `app.foundation.data_backbone.ingest`,
  `app.decision.discover.{provider_degradation,orchestrator,tradeability,pipeline}`,
  `app.lab.alphacrafter.panel`, `app.decision.llm_portfolio.agents.base`,
  `app.worker` (provider-health probe job), `app.interface.api.{market,settings,alphacrafter}`,
  `app.interface.api.quant.{market,factors}` — one of the widest fan-ins in the codebase;
  effectively every price/fundamentals/news/macro/fx read in the app funnels
  through `build_provider_registry(db).first_success(...)`.
- Out: `app.foundation.settings` (`get_public_settings`, `get_secret` per-provider
  API keys, `resolve_openbb_default_provider`), `app.foundation.core.config` (`get_settings`).

## Contract invariants

- Foundation-tier under "Global layering" (ADR 0015 Phase 5, which subsumed
  the retired "Foundation services never import the decision loop") —
  forbidden from importing anything in `app.decision`, `app.lab` or
  `app.interface`. This is the highest-blast-radius invariant for
  this package: a violation here breaks the foundation/decision-loop boundary
  from the foundation side, silently, for every consumer listed above.
  Zero ignore entries for this contract.
- Not a member of "Decision-loop packages are independent" (it's Foundation,
  not decision-loop).
- Global layering holds (interface → decision → lab → foundation).

## Owner-wave notes

Rate limits are per process unless `REDIS_URL` is explicitly configured:
`rate_limiter.get_shared_limiter` then returns `RedisRateLimiter` (atomic Lua
sliding-window log, shared by the API and the worker) and falls back to the
in-process `RateLimiter` on any Redis error.

`ProviderRegistry` instances are process-local and cached for 5 minutes
(`_REGISTRY_CACHE_TTL`); `invalidate_registry_cache()` must be called after any
write to `provider_chain_json` or a provider API key, or the change won't take
effect until the cache naturally expires — check callers of
`upsert_public_settings`/`set_secret` for the relevant provider keys invoke it.
`ProviderHealth` (observability entity, `models/entities/provider.py`) is
written by the nightly `register_provider_health_probe_job` in `app.worker`
(not owned by this package — this package only supplies the providers being
probed) and read by `app.interface.api.market`/`app.interface.api.data`.
