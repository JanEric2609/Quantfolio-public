# ADR 0024 — Second-source price checks, 8-K filings and world news

Date: 2026-11-10. Status: accepted.

## Context

A provider review on prod (2026-11-09) found that 12 credentials were configured, but the app made little use of most of them:

- **One source for prices.** 886 of the 888 stored bar series came from yfinance. A dividend that was never adjusted, a 3:2 split on a foreign listing, bars that stopped moving or a ×100 break inside a quiet week all look like ordinary days to the bar guard. Only a second source can catch them. Alpaca (all US listings in a few multi-symbol calls), EODHD (20 free calls a day) and Tiingo sat idle for this.
- **Calls wasted on the wrong market.** The general provider chain asked US-only providers (Massive, Tiingo, Twelve Data, Databento, Alpaca, Finnhub) about European listings. Each call spent a rate-limit slot and came back with nothing. Massive's provider-health errors for `IWDA.AS` came from this.
- **Twelve Data unused.** It stays out of price ingestion until its adjusted closes are verified, and nothing ever ran the check.
- **News for US names only.** The nightly AI reading of headlines had Finnhub, whose free tier refuses non-US listings, so about 30 % of MSCI World was never read. The company's own statements (8-K filings) were not read at all.

## Decision

1. **Nightly second-source check** (`data_backbone/cross_check.py`, 04:20 UTC, table `price_cross_checks`).
   - It compares daily log-return differences, never levels.
     - Every US series is checked against Alpaca (SIP feed, falling back to IEX).
     - The other series rotate through EODHD at 18 a night, so each one is checked about once a month.
   - It recognises these patterns: unadjusted split, ×100, dividend mismatch, level break, one-day spike, stale bars.
   - A dividend step after the series' last full re-pull is drift, not an error: the weekly re-pull restates it.
   - A split or ×100 error triggers `DataIngester.repull_series` only when a second check confirms it: either a Tiingo tie-break agrees, or a split is on record within 5 days. Another provider's bars are never stored.
   - Unconfirmed errors appear on the Control Center's "needs attention" list.
   - The same job runs the Twelve Data acceptance probe once. If SPY's close for 2023-12-29 comes back below 472 (raw 475.31), Twelve Data is adjusted and joins the ingestion chain. The verdict is stored in `twelvedata_adjusted_probe_json`.
2. **The registry skips US-only providers for non-US symbols** for every capability whose first argument is a ticker. No call is made and no rate-limit slot is used.
3. **8-K item codes** (`data_engineering/sec_8k.py`, `sec_8k_events`).
   - They are read right after the insider-trading refresh (04:15 UTC), on the same daily EDGAR index and SEC rate limit. `company_tickers.json` maps tickers to CIKs; one `submissions` call per company that filed returns the item codes.
   - Material items (4.02 non-reliance, 4.01 auditor change, 3.01 delisting notice, 2.06 impairment, 5.02 officer change, …) are used in two places:
     - as extra lines in the nightly headline reading for US names;
     - as a `filings` fact in Discover dossiers (last 30 days).
4. **GDELT headlines for non-US members** (`data_engineering/gdelt_news.py`, 04:00 UTC, `news_mentions`).
   - It reads GDELT's 15-minute GKG files (English and translated) since the last run, capped at 30 hours.
   - An article is kept only when GDELT lists the company among its organisations *and* the company's name is in the title. One row per member, day and title.
   - The 05:00 headline reading rates them like Finnhub's, and `news_tone_scores.source` records where each night's headlines came from.
   - GDELT's search API is not used: it allows one query per 5 seconds and covers only the last 3 months.
5. **Worker pool.** The worker pool goes from 18 to 19 threads (55 jobs, three per thread), and the DB overflow from 26 to 28 to match. Prod Postgres allows 100 connections and used 28 when checked.

## Considered and not done

These items come from an outside research note (Gemini, 2026-11-10), checked against the code:

- **Broker execution APIs** (IBKR, Trading 212 orders): forbidden. DKB, Scalable and Trading 212 stay read-only (AGENTS.md).
- **Residual momentum:** already scored nightly by the forward lab (`residual_momentum`, Blitz, Huij & Martens 2011).
- **Factor momentum** (Ehsani & Linnainmaa): a signal on factor returns, not on stocks, so it does not fit the per-stock lab or the shortlist.
- **QMJ:** needs point-in-time fundamentals history the app does not keep for MSCI World.
- **Vector-memory reflection:** superseded by verified lessons (ADR 0023). Free-text memories on small samples were the problem.
- **FinBERT gatekeeping:** a 2025 comparison found 8B instruction models classify financial sentiment better than FinBERT, and the local model already reads the headlines.
- **LLM formulaic alpha search:** AlphaCrafter already does this. More searching raises the bar for every idea.
- **DSR/CPCV as the graduation gate:** the 2026-11-09 research recommends an evidence-budgeted capital ladder on paired daily excess returns. It is not decided yet.
- **German tax items:** Basiszins 3.20 % for 2026, Teilfreistellung and the holdings-based Vorabpauschale estimate are already in the tax cockpit. A January cash reserve does nothing while the NV certificate keeps tax from being withheld.

The one item adopted is the **momentum-crash guard in Mandate B** (Daniel & Moskowitz 2016; Barroso & Santa-Clara 2015).
- The guard fires when the core ETF's last 24 months are negative *and* its last month is over 10 % more volatile than its last year.
- When it fires, the stock share is halved.
- It halves rather than exits because most of a momentum crash is in the short leg, which the long-only book does not hold.
- The state is recorded in each rules review.

## Consequences

- Wrong prices get caught within a night (US listings) or a month (others), instead of after a dossier or a trust test has used them.
- The forward lab's `news_tone` signal now covers the whole MSCI World universe. Its evidence has to be read per source: Finnhub and SEC lines for US names, GDELT for the rest.
- GDELT costs about 200 file downloads a night, a few megabytes each, streamed and then discarded.
- Mandate B's rules gained one rule before its first run, so the A − B comparison stays pre-registered.
