# Free vs. paid market data for point-in-time, survivorship-bias-free backtesting

Scope: what data provider(s) Quantfolio actually needs for rigorous backtesting
research (European + US equities/UCITS ETFs, point-in-time fundamentals,
survivorship-bias-free corporate actions/delistings), and whether the current
paid provider stack (`app/foundation/providers/`: Alpha Vantage, EODHD,
Finnhub, Databento, Alpaca) can be trimmed in favour of free sources.

**Provenance.** This is a synthesis of two independent LLM research passes
(Gemini and Claude, both web-search-grounded, run 2026-09-05) requested because
the owner is unhappy with current provider costs. Neither pass's per-provider
numbers (rate limits, pricing, exact free-tier caps) have been independently
re-verified against live vendor pages as part of this repo's own work — treat
figures below as a strong starting point, not a contract, and re-check the
vendor's current terms before committing money or code to any of them.
Provider terms in this space change often (Alpha Vantage cut its free tier
500 → 100 → 25 req/day over 2024–2026; IEX Cloud shut down entirely in 2024).

## TL;DR

- **No free-tier provider — retail or otherwise — delivers genuine
  point-in-time (PIT), survivorship-bias-free (SBF) fundamentals and
  corporate-actions history for European equities.** Free sources (yfinance,
  Stooq) silently drop delisted tickers and back-adjust corporate actions
  across the full price history, which is exactly what a PIT backtest must
  not do.
- **University WRDS/Datastream access is the only realistic free-to-you path**
  to close that gap — this is also already reflected in the codebase:
  `app/foundation/data_engineering/datastream_loader.py` was built in ADR 0015
  Phase 3 specifically for a Refinitiv/LSEG Datastream extract, and
  deliberately left unexercised pending the owner's Datastream access being
  set up (see that package's `CONTEXT.md`). Nothing in this research changes
  that call — it confirms it was the right one.
- **US-only point-in-time fundamentals *can* be done for free** via the SEC
  EDGAR XBRL API (`companyfacts`/`frames`), because every fact is stamped with
  its filing/accession date. No equivalent free path exists for Europe.
  Not currently relevant to Quantfolio's German/European focus, but useful if
  US-side research is ever added.
- **Free daily OHLCV is a solved problem** for both US and European tickers
  (Stooq for European breadth, yfinance as a broad fallback) — good enough as
  the *live update* layer sitting on top of a PIT gold copy, never as the
  backtest history itself.
- Of the current paid providers, **Alpha Vantage and Finnhub's free/cheap
  tiers are too rate-limited to be useful for batch research** (25 req/day and
  similar) regardless of what else changes — worth checking during a future
  provider-config pass whether they're still pulling weight.

## The core problem: PIT/SBF is not one requirement, it's four

| Component | Free source? | Gap |
|---|---|---|
| Delisted-security price history (US) | Partial (paid: Sharadar, Norgate) | No full free solution at retail scale |
| Delisted-security price history (Europe) | **No** | Stooq/yfinance drop delisted tickers; paid EODHD or WRDS/Datastream required |
| Delisting returns (terminal bankruptcy/M&A return) | No | Free feeds omit the terminal return entirely; CRSP is the reference |
| As-reported / PIT fundamentals (US) | **Yes** — SEC EDGAR XBRL (free) | XBRL only from ~2009; taxonomies inconsistent; PIT join is manual work |
| As-reported / PIT fundamentals (Europe) | No | Only Compustat Global / Datastream-Worldscope via WRDS |
| Corporate actions (splits/dividends) | Partial | Free retail data has revision/backfill errors; delistee actions specifically missing |
| Historical index membership (constituents-as-of-date) | No (Europe); Fama-French portfolios only (aggregated, not stock-level) | No free European constituent-history source |

Free sources solve at most the price-history box for the US, plus fundamentals
for the US. Everything European, and delisting returns everywhere, requires
paid or academic data — there is no way around this with a purely free stack.

## Recommended stack (converged from both research passes)

1. **Historical base layer (academic, effectively free): WRDS/Datastream.**
   One-time bulk pull of the full target universe (e.g. all Stoxx 600 /
   MSCI World constituents *ever*, including delisted names, unadjusted +
   adjusted prices, corporate actions) via the university's WRDS or Refinitiv
   Workspace access. Store this as the local "gold copy." This is exactly the
   shape `datastream_loader.py` already expects (Refinitiv/LSEG Datastream
   CSV / Excel Add-in / WRDS DSWS export format) — once access exists, this
   module is ready to exercise it.
   - Caveat: academic licenses are typically use-restricted to non-commercial
     research and may cap monthly data-point downloads (reported ~10M/month
     on some student licenses) — plan the pull as one bulk extract, not a
     recurring live feed.
   - Caveat: confirm the university's *current* Datastream/CRSP/Compustat
     subscription before relying on it — some institutions have been dropping
     LSEG/Datastream access (reported as ending mid-2026 at some schools).
2. **Daily incremental updates (free): yfinance / Stooq.** Survivorship bias
   only corrupts *history* (dead tickers vanishing, back-adjusted splits) —
   using a free source to append *today's* bar to an already-PIT-correct local
   database is safe, provided delistings occurring *after* the gold-copy pull
   are archived manually rather than silently dropped.
3. **If Europe-wide delisted coverage is needed sooner than WRDS access
   arrives:** EODHD's paid "All-World" plan (~€20/month, 50% student
   discount reported) is the cheapest retail source that explicitly supports
   `delisted=1` on European exchanges — with the caveat that pre-2018
   delisted coverage is price-only (no fundamentals/dividends/splits before
   that date per its own docs), and symbol-rename mapping is US-only.
4. **Factors/macro (free, no caveats):** Kenneth French Data Library already
   covers US + European FF3/FF5 factors built from CRSP/Compustat with
   delisting handling baked in — useful as a benchmark/sanity-check layer
   independent of whatever price data ends up used. ECB Data Portal /
   Eurostat / FRED fully cover macro context for free.

## Per-provider quick reference

Only providers either research pass rated as plausible candidates; consult
the full source discussion for the reasoning behind each verdict.

| Provider | Cost | Coverage | Survivorship bias | Verdict |
|---|---|---|---|---|
| Stooq | Free | Strong Europe (Xetra, Euronext, LSE) + US | Yes — no delisted retention | Best free bulk daily-OHLCV spine |
| yfinance | Free | Global, incl. UCITS ETFs | Yes — aggressive delisting drop | Fine for live daily updates only |
| SEC EDGAR XBRL | Free | US fundamentals only | N/A (as-reported by filing date) | Only free genuinely-PIT source, US-only |
| Kenneth French Library | Free | US + Europe + Japan + APAC, portfolio-level | Built-in delisting handling | Free factor/benchmark layer |
| Norgate Data | Paid (modest) | US/AU/CA only | No (SBF) | Excellent but **no European coverage** — not usable here |
| Tiingo | Free tier too small | Thin Europe | Yes on free tier | Not viable for this use case |
| EODHD | Paid (~€20/mo, cheap) | Strong Europe, 70+ exchanges | No (SBF), with caveats above | Best cheap-paid European fallback |
| Alpha Vantage | Free tier too small (25 req/day) | 20+ global exchanges | Yes | Unusable for batch; apply for its free open-source/education tier if relevant |
| Finnhub | Currently paid in Quantfolio | — | — | Re-evaluate necessity once WRDS/EODHD flow is in place |
| WRDS (CRSP/Compustat/Datastream) | Academic (effectively free) | US + Global/Europe | **No (SBF)**, incl. delisting returns and PIT fundamentals | The actual answer for this project |

## Open follow-up

- Once WRDS/Datastream access is confirmed and the bulk extract is pulled,
  exercise `load_datastream_extract` (`app/foundation/data_engineering/datastream_loader.py`)
  against the real export for the first time — it is currently tested only
  against synthetic fixtures.
- Revisit whether `providers/alphavantage_provider.py` and
  `providers/finnhub_provider.py` are still earning their keep once the
  PIT panel is populated from Datastream — both were flagged by this
  research as too rate-limited for batch research regardless of the PIT
  question.
- This access is expected to take a while to arrange — until then, the
  Phase 4 lab pipeline (`app/lab/quant_lab/signal_batch.py`) can keep
  running against the existing `bar_prices`-derived panel; see
  `docs/research/` and the 2026-09-05 IC-vs-conviction investigation for
  the first real run of that pipeline.
