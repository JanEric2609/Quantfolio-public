# 0007 — Fama-French Factor Source: US → Developed Europe

**Status:** Accepted
**Date:** 2026-09-01
**Context source:** memory `project_quantfolio_2026-08-31_implementation_decisions.md`
(Topic 2 of the consolidated implementation plan)
**Consumed by:** `backend/app/services/quant_factors.py`,
`backend/app/services/verification/attribution.py`,
`backend/app/services/attribution/factor_attrib.py`,
`backend/app/services/discover/pipeline.py`,
`backend/app/api/quant/factors.py`

## Context

`services/quant_factors.py` downloaded Kenneth French's **US** 3-factor
(Mkt-RF, SMB, HML) and US momentum daily series directly from Dartmouth, and
used them to compute portfolio/per-stock factor betas and alpha throughout
the verification and discover pipelines (`_regression_factor_attribution`,
`stage_quant_signals`, `factors/attribution` endpoint). This is a scoping
mismatch for the same reason the regime index was (ADR 0006): the portfolio
this app manages is EUR/DE-scoped (`currency=EUR`,
`tax_residency_country=DE`, DKB/German-broker-only, §20 InvStG tax cockpit).
Regressing a EUR portfolio's returns against US factor returns produces
betas/alpha that reflect currency and market mismatch noise rather than the
portfolio's actual style tilts.

Kenneth French's data library also publishes a Developed Europe regional
factor set, in the same file format, downloadable from the same Dartmouth
FTP host:

- `Europe_5_Factors_Daily_CSV.zip` — Mkt-RF, SMB, HML, RMW, CMA, RF (a 5-factor
  set, not 3 — the US download used only the 3-factor file even though a
  5-factor US file was also fetchable via the unused `ff5` zoo entry).
- `Europe_Mom_Factor_Daily_CSV.zip` — WML (Europe's momentum factor, labelled
  differently from the US "Mom" but functionally equivalent).

## Decision

**Full replace, no dual-region fallback.** `_FF5_URL` and `_MOM_URL` now
point at the Europe files; the separate `_FF3_URL` (US 3-factor) constant and
the never-fetched `ff5` (US 5-factor) zoo entry are removed entirely.
`_load_ff3()` (kept under its historical name — `get_ff3_returns()` is the
public API and is called from ~10 sites; renaming it would be a larger,
riskier diff for no behavioural benefit) now sources the Europe 5-factor file
and maps all five columns (`mkt_rf`, `smb`, `hml`, `rmw`, `cma`) instead of
just three. If the Europe download fails, callers keep their existing
ETF-proxy fallback behavior (`etf_proxy_fallback` in `factors/attribution`) —
there is no silent reversion to US data.

**Cache is region-scoped by filename, not wiped.** The on-disk parquet cache
files are renamed `ff5_europe.parquet` / `mom_europe.parquet` (were
`ff3.parquet` / `mom.parquet`). This is the mechanism that makes the
"drop the US-factor cache entirely" requirement concrete: a stale US-cache
file from before this deploy is simply never looked up again under the new
name, so the very next call forces a fresh Europe download rather than
serving up to 7 more days of US data under the old freshness TTL. No
manual `rm` on any host is required.

**Missing-data sentinel now masked.** Dartmouth's documented convention marks
a missing daily observation as `-99.99`; the prior US-only code parsed this
through unmasked, so a data gap would have silently entered the regression as
a `-0.9999` return. Values `<= -99.0` are now masked to `NaN` before the
percent-to-decimal scaling, for both the factor file and the momentum file.
This was latent in the US path too, but is fixed here since it's the same
`_download_ff_csv` codepath.

**Historical beta/alpha "recompute" is implicit, not a migration.** Factor
betas/alpha from this module are computed live on every call
(`compute_factor_attribution`) — nothing persists a historical beta or alpha
value in the database keyed by date. `QuantFactorScore` rows (the one
FF-adjacent persisted table) store AlphaCrafter's own OHLCV-derived factors
(`momentum_12_1`, `rsi_14`, etc.), not Fama-French regression output. So
"recompute retroactively" requires no backfill job or migration: the next
call to `factors/attribution`, `stage_quant_signals`, or the verification
dashboard after this deploy uses Europe data automatically.

## Alternatives considered

**Keep US 3-factor, add Europe as a second option.** Rejected: the plan
calls for a full replace, and maintaining two regional factor sets doubles
the download/cache/parsing surface for a value (US factors) nothing in this
app should ever want, given the portfolio's exclusive EUR/DE scope.

**Fall back to US factors if the Europe download fails.** Rejected: mixing
regions silently (a EUR portfolio regressed against US factors on a bad day)
reintroduces exactly the mismatch this change removes, and would do so
invisibly. The existing ETF-proxy fallback is the correct degraded mode.

**Backfill/migrate historical factor-exposure rows.** Not applicable: no such
rows exist to migrate (see "recompute is implicit" above).

**Wire RMW into `verification/attribution.py`'s quality style tilt now that
it's available.** Deferred: RMW/CMA becoming available is a side effect of
this change, not its goal. Extending the quality-tilt calculation to use a
real regression factor is a separate feature decision with its own scope
(would change `_REGRESSION_FACTOR_TO_STYLE` and the quality tilt's
semantics) and is left for a future pass; the misleading "doesn't exist yet"
comment was corrected to avoid stale documentation.

## Consequences

- Every consumer of `get_ff3_returns()` (verification attribution, discover
  `stage_quant_signals`, `attribution/factor_attrib.py`, the
  `/factors/attribution` and `/factors/ff3` endpoints) now regresses against
  Developed Europe factors instead of US, and exposes `rmw`/`cma` in addition
  to the three factors previously available — a superset, so no existing
  dict-key access breaks.
- `factors/attribution`'s response `source` field changed from
  `"fama_french_3"` to `"fama_french_europe_5"` to reflect the new source
  accurately (no test or frontend code asserted the old literal).
- The `/factors/ff3` and `/factors/zoo` route paths and zoo entry `id`
  ("ff3") are kept unchanged for API-contract stability, even though the
  underlying data is now the Europe 5-factor set — renaming the route would
  be a breaking wire change for zero functional benefit.
- The `ff5` zoo entry (previously present but never fetched by any loader)
  is removed; its factors are now what `ff3`'s `id` actually returns.
- Regression coverage: `tests/test_quant_factors.py::TestEuropeFactorCutover`
  (URL cutover, full 5-factor exposure, `-99.99` sentinel masking,
  Europe-scoped cache filenames); existing attribution/discover/tax-contract
  tests already stub `get_ff3_returns`/`get_momentum_returns` directly and
  needed no changes.

## Amendment (2026-09-28): per-listing factors for single-stock regressions

**Status:** Accepted

The full replace was right for **portfolio-level** attribution, which
regresses a EUR/DE-scoped portfolio. It was wrong for Discover's
**single-stock** regression (`stage_quant_signals`), once the universe held
US shares again (ADR 0017). A stock's return must be regressed on factors
that trade in the same market session. US shares regressed on Developed
Europe factors produced nonsense on prod (Discover run 71d23ec4):

- SPY loaded 0.50 on Europe Mkt-RF (R² 0.54), against 0.99 (R² 0.99) on
  the US factors.
- MU showed an RMW loading of −5.3.

The dossier fed both into its reasoning.

**Decision.** Add a US 5-factor loader, `_load_ff5_us`, reading
`F-F_Research_Data_5_Factors_2x3_daily_CSV.zip` and caching to
`ff5_us.parquet`. Add `get_ff5_returns_for_listing(us_listing, ...)`, which
returns the US set for a US listing and the Developed Europe set otherwise,
tagged with `region`. `stage_quant_signals` uses it:

- It requires at least 120 date-aligned observations.
- It records `factor_fit = {region, r_squared, n_obs}`, so a poor fit is
  visible.

`get_ff3_returns` and every portfolio-level consumer are unchanged. The
"no US data" rule above still holds for them.

**Known limitation (resolved by amendment 2).** Ken French publishes the
Developed Europe factors in USD, while an EU listing's return is in EUR.

## Amendment 2 (2026-09-28): returns in USD, and factors for every region

**Status:** Accepted

**FX.** All of Ken French's international factor files are USD returns, and
the Fama-French international studies regress USD stock returns on them. The
long-short factors are close to currency-neutral, but the market factor is
not. So regressing a EUR return on them reads the EURUSD move as exposure.
On 2019–2026 daily data:

| Asset | Mkt (EUR → USD) | SMB (EUR → USD) | R² (EUR → USD) |
|---|---|---|---|
| SAP.DE | 0.77 → 1.01 | −0.42 → −0.11 | 0.31 → 0.40 |
| SIE.DE | 1.04 → 1.28 | −0.65 → −0.34 | 0.53 → 0.61 |
| EUNL.DE (MSCI World) | 0.58 → 0.82 | −0.35 → −0.04 | 0.51 → 0.74 |

Every EUR asset showed a spurious large-cap tilt of about −0.3 SMB and a
market beta about 0.25 too low. That included the portfolio's own
style tilts.

**Decision.** Restate returns in USD before any regression on these factors.
`quant_factors.restate_in_usd` applies `(1+r)·(fx_t/fx_{t−1}) − 1`, and
`market.usd_per_unit_by_date` reads the rate from the `USD<ccy>=X` bars
Discover already ingests. The four consumers:

- Discover's per-stock regression, from the listing currency.
- Verification style tilts, from EUR portfolio snapshots.
- `/api/quant/factors`, from EUR portfolio returns.
- `/api/attribution/factor`, from each holding's and the benchmark's bar
  currency. It uses the bars' currency, not `Holding.currency`: DKB books a
  Nasdaq share in EUR while its bars are USD.

Without a rate the local series is used and `factor_fit.returns_in` says so.
Request paths read stored rates only (`allow_live=False`).

**Regions.** `get_ff5_returns_for_listing` now takes
`providers.utils.listing_region(symbol)`:

- US → US.
- European venues → Developed Europe.
- Tokyo → Japan (`Japan_5_Factors_Daily_CSV.zip`).
- Anything else → Developed (`Developed_5_Factors_Daily_CSV.zip`).

Before this, the 27 Tokyo names in the universe had no known suffix. They
counted as US listings: US-only providers were asked for them, and their yen
returns were regressed on US factors against SPY unconverted. Tokyo, Hong
Kong, Korea and ASX suffixes are now known, `listing_currency` maps them
(JPY, HKD, KRW, AUD, CAD), and Tokyo listings benchmark against EWJ.

## Amendment 3 (2026-09-28): the currency a price is quoted in

Amendment 2 converts each series from "the bars' currency", and that label
was wrong for 429 of the 780 symbols with bars:

- The ingester took `assets.currency`, a fund's base currency, so EUNL.DE
  read USD although Xetra quotes it in EUR.
- Otherwise it fell back to "EUR", so AAPL, MU, NESN.SW and SHEL.L all read
  EUR.

The prices themselves were native (SHEL.L in pence, NOVO-B.CO in kroner).
The listing suffix, which Discover used, was right for 765 of the 780. It
cannot know multi-currency venues: the LSE quotes IWDA.L, CSPX.L, IWVL.L,
IHG.L and CPG.L in USD, and CBE3.L in EUR. yfinance also serves some Xetra
ETF lines in USD (SXRM.DE, SXRL.DE).

**Decision.** yfinance names the quote unit in every history response
(`history_metadata["currency"]`). Readers resolve a symbol's unit in this
order (`data_backbone.listing_currency`):

1. `listing_currencies`, filled from that metadata by ingestion and by the
   `listing_currency_audit` job. The job runs at worker start and daily,
   and re-probes a symbol after 30 days.
2. A label the caller holds from a live quote (valuation's cached price).
3. The listing suffix, now in `providers.utils`.

Ingestion stamps the resolved unit on the bars, and the audit relabels
stored `bar_prices` and `price_cache` rows. Minor units keep their case:
"GBp" is pence, and `fx_rates.convert` divides it by 100. Upper-casing it
would have valued 3,611p as GBP 3,611.

Discover's regression and benchmark FX adjustment, the outcome resolution,
`/api/attribution/factor`, valuation, and verification's currency exposure
and FX stress all use the resolved currency. The last two used
`Holding.currency`, the booking currency: an AAPL position bought in EUR
escaped every FX shock. A UCITS ETF still counts as its listing currency;
its real exposure is the basket underneath.

The stress set also changed:

- "Currency Crisis" is the euro falling 15% against the dollar. Its
  description claimed that foreign holdings lose, but they gain in euros,
  and the code already computed it that way.
- A "Dollar Slump" scenario (EUR +15% vs USD, markets flat) now covers the
  FX risk of a euro investor in global equities. EUR/USD went from about
  1.03 to 1.17 in the first half of 2025.
- `worst_case_loss` counted a holding's gain as a loss (`abs(shock)`).

## Amendment 4 (2026-09-29): what an index ETF is exposed to

Amendment 3 stopped at the listing: "a UCITS ETF still counts as its listing
currency". For the owner's portfolio that is the whole question. It is one
MSCI World ETF (IE00B4L5Y983), quoted in EUR on Xetra (EUNL.DE). On
2026-09-28 the index's constituents traded:

| Currency | USD | EUR | JPY | GBP | CAD | CHF | AUD | other |
|---|---|---|---|---|---|---|---|---|
| Share | 73.3% | 8.2% | 5.9% | 3.4% | 3.3% | 2.2% | 1.5% | 2.3% |

Verification showed that portfolio as 100% EUR. "Dollar Slump" left it
untouched.

No data source in the app carried an ETF's currency or country weights:

- yfinance's `funds_data` has no country weights, so `etf_lookup`'s regions
  are empty for every ETF outside its five hard-coded ones. It also lists
  only the top 10 holdings.
- iShares' holdings CSV endpoint now returns an HTML page.

State Street's SPDR UCITS range publishes daily holdings files. Each file
lists every constituent with its trading currency (1,241 rows for MSCI
World).

**Decision** (owner, 2026-09-29): an index-weights table
(`foundation.etf_currency`, table `index_currency_weights`, migration 0118).

- **Weights per index.** The trading-currency weights of an index's
  constituents, which is what an unhedged fund's value moves with:
  - SPDR files for MSCI World, ACWI, ACWI IMI, EM and Europe.
  - Single-currency by construction: S&P 500, Nasdaq-100, Euro Stoxx 50
    and DAX.
  - Read through a close index: FTSE All-World (via ACWI) and STOXX Europe
    600 (via MSCI Europe).
- **Refresh.** The `index_currency_weights` job reloads the files on the
  3rd of each month. Seed values from 2026-09-28 apply until then. Weights
  older than 183 days raise a risk alert.
- **ETF to index.** 161 ISINs, curated from the justETF universe: plain
  index ETFs in every unhedged share class. Currency-hedged classes map to
  their hedge currency. Sector, factor, ESG, regional-subset and leveraged
  variants are left out. The `etf_index_map` public setting adds more.
- **Everything else** keeps its priced-in currency: shares, and unmapped
  funds.

`risk.py` spreads each holding's value over its currency weights. `stress.py`
applies the FX shock to the non-EUR share of each holding, where it used to
apply to all of a foreign-listed holding or none of a domestic one. Under
"Dollar Slump" (EUR +15%), EUNL.DE now moves by −15% × 91.8% = −13.8%;
before, it didn't move at all.

Rejected:

- Estimating exposures by regressing ETF returns on exchange rates. Equity
  moves and FX are correlated, so the estimates are off by 10–20 points.
- Keeping the listing currency with a label.

