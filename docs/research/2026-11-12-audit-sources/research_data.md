# Quantfolio data-defect research (2026-11-12)

Method note: a usage limit interrupted the run once; the work was resumed. Only 6 pages were fetched in full or in part (marked READ). Everything else was seen as a search snippet only (marked SNIP). Search snippets are summaries, not primary reading. "[own]" means a recipe from my own knowledge, not from a cited source, so verify it before building on it. Low-authority sources are marked (LOW).

## A. FCF yield (freeCashflow / marketCap)

Findings
- I found NO source stating Yahoo's exact definition of `freeCashflow`, and none for `financialCurrency` vs `currency` in yfinance. The one search that touched it returned only an unanswered node-yahoo-finance2 discussion where the API value differed from the website [19]. Treat Yahoo's definition as UNVERIFIED.
- Yahoo mixes pence and pounds (GBp/GBP). Portfolio Performance and GnuCash users report the unit flipping [21], and yfinance has a 100x "repair" feature for it [20][38][39]. Prices and statement currency are therefore both unsafe without assertions.
- "Levered FCF" has several competing textbook formulas: OCF − capex − debt payments, EBITDA − ΔNWC − capex − debt payments, or NI + D&A − ΔNWC − capex + net borrowing [120 low sources]. A vendor's label cannot be trusted without checking it.
- Damodaran: FCFF is discounted at the cost of capital and values all claims, so debt must be subtracted. For a consolidated subsidiary, the parent's enterprise value includes 100% of it. Damodaran subtracts minority interest, converted to market value (book value × the subsidiary's sector P/B), to reach parent equity [22][23].
- Banks and insurers: FCF is not meaningful because debt and cash are operating items and operating and financing flows blur [120][121 low]. Data vendors still print values [119].
- IFRS 16 moves lease cash payments out of operating cash flow into financing (principal) and interest. Reported OCF and FCF therefore rise versus pre-2019 [25][26][24]. This is partly supported by the sources and partly [own]; check the cash-flow statement for the lease-principal line.

Fix recipe
1. Compute a consistent FCF yield: `fcf_yield = (CFO − capex − lease principal repaid) / market cap`, with the numerator in `financialCurrency` and market cap converted to that same currency at the period-end or latest ECB rate. This lease deduction is [own], and it makes IFRS and US GAAP operating-lease reporters more comparable.
2. Never divide figures across `info['financialCurrency']` and `info['currency']` without FX conversion. If `currency == 'GBp'`, divide the price by 100. Quarantine the row if the market-cap currency is unknown.
3. For minority-heavy groups, either (a) use EV-based FCFF yield: `FCFF / (market cap + net debt incl. leases + minority interest at market or book − associates)` [22][23], or (b) attribute FCF to the parent by multiplying by the ownership share. Flag companies with `minorityInterest / marketCap > ~10%` [own threshold]. Deutsche Telekom (T-Mobile US) is the example case.
4. Exclude GICS Financials (banks, insurers; keep exchanges and payment networks if desired [121 low]) and optionally REITs from FCF yield. Use P/B, ROE or dividend-based measures for them.
Limits: yfinance fields are trailing figures with undocumented construction. Prefer deriving FCF from the statement rows (`cashflow` → "Operating Cash Flow", "Capital Expenditure") over `.info['freeCashflow']`. [own]

## B. Net debt / EBITDA and leases

Findings
- IFRS 16 capitalises all leases. Net debt then includes lease liabilities, and EBITDA rises because rent becomes depreciation and interest [25][26]. EY notes that some companies restate adjusted EBITDA.
- Under US GAAP ASC 842, operating leases keep a single straight-line expense above EBITDA, so EBITDA is lower than under IFRS 16 for the same lease. Finance leases match IFRS 16 [104][106][105]. One study table contradicts this [Acadifi, LOW; not listed separately].
- Rating agencies: Moody's has adjusted for lease debt since 2006 [107]. Rating agencies add the present value of leases to debt for both lease types [108]. Moody's and S&P include leases in net debt, while Fitch treats them as an operating liability [109]. S&P found the IFRS 16 effect on its adjusted ratios mostly immaterial, because its adjusted EBITDA already compensated [24]. I could NOT find Moody's current exact formula.
- Valuation consistency: if lease liabilities are in EV or net debt, use post-IFRS 16 EBITDA (excluding rent). If they are not, use EBITDA after rent. Mixing double-counts [26][104].
Fix recipe
- Compute two ratios and label them: `net_debt_incl_leases / EBITDA(post-IFRS16)` for IFRS reporters, and `(net_debt + operating lease liability) / EBITDAR` or `(…)/EBITDA + operating lease cost` for US GAAP reporters [own]. Compare like with like. Do not mix with company-reported ratios, which exclude leases.
- Thresholds: I found no sourced sector thresholds. Common practitioner rules [own, unverified]: ND/EBITDA under 2x is comfortable for industrials, and 3–4x or more is stretched. Utilities, telecoms, infrastructure and real estate tolerate 4–6x. Banks and insurers are excluded. Airlines, retailers and hotels have large lease balances, so show both ratios.
Limits: yfinance gives "Total Debt" that may or may not include "Capital Lease Obligations". Check the balance-sheet rows by name.

## C. Latest-quarter growth for semi-annual reporters, and fundamentals vendors

Findings on reporting frequency
- Many European countries are semi-annual in practice (Austria, Belgium, France, Germany, Switzerland per a TradingView table [41]). Goldman found that reporting frequency does not affect valuations [40 SNIP]. Missing or sparse Yahoo quarterly data is a known Yahoo gap [43]. I found no source on how yfinance behaves for semi-annual reporters. The `mostRecentQuarter` field was NOT verified.
- EDINET note: a source says quarterly reports were dropped for fiscal years from April 2024, with semi-annual reports used instead [66 LOW; unverified].
Recipe (own, mostly)
1. Pull `quarterly_income_stmt` and take the column dates. Infer frequency from the median gap: about 90 days = quarterly, about 180 days = semi-annual, about 365 days = annual only. Store `period_months` per row.
2. TTM for semi-annual reporters = latest FY + latest H1 − prior-year H1. Quarterly reporters: the sum of the last four quarters. Balance sheet items use the latest date.
3. Growth: compare same-length periods (H1 vs prior H1, or TTM vs prior TTM). Do not label an H1 comparison as "quarter". Treat trading updates as not statements and drop them.
4. Always store `period_end`, `period_months` and `fetched_at` with each fundamental value so staleness is visible.
Regulatory sources
- ESEF/iXBRL: filings.xbrl.org exposes a JSON:API at `https://filings.xbrl.org/api/filings` [4]. Coverage is incomplete: Germany and Ireland cannot be reliably discovered [4], and a university project listed Germany, Ireland, Bulgaria and Slovakia with no filings [9]. An Apify listing claims 28 countries and 27,626 reports [12 LOW]. ESEF filings are annual reports, so there is no quarterly data. This is [own] knowledge and partly supported by the pages (the about page does not state "annual only"). Python wrapper: `xbrl-filings-api` [10][11].
- ESAP: collection starts July 2026, the public platform opens 10 July 2027, then phased expansion in January 2028, 2029 and 2030 [89][90][91][92]. One source says full rollout by 2031 [92]. It will not help before 2027.
- UK: Companies House free Accounts Data Product, daily and monthly ZIPs of iXBRL accounts, unparsed [101][102][103]. Coverage figures conflict (60% vs 88% in iXBRL).
- US: SEC `data.sec.gov` companyfacts and frames, max 10 requests per second, and a declared User-Agent is required [51]. The companyfacts docs themselves were not found.
- Japan: EDINET API v2 needs a free key [66][67]. TDnet is covered in D.
Vendor comparison (prices as seen; verify before buying)

| Vendor | Price | EU/JP fundamentals | Personal-use licence | Notes / source |
|---|---|---|---|---|
| EODHD | Fundamentals feed EUR 59.99/mo (EUR 49.99/mo annual); EOD All-World EUR 19.99; All-in-one EUR 99.99 | Claims US, UK, EU, Asia fundamentals; no per-exchange list | "Personal" vs "Commercial" tracks; terms text not shown [1] | READ [1]; fundamentals plan has no EOD prices |
| FMP | Free 250 calls/day; paid tiers conflict ($139–150/mo cited) | Lower tiers US/UK/CA only; global on top tier (SNIP) | Individual plan "personal use only" (SNIP, LOW) | [6][7]; page returned 403 |
| Twelve Data | Individual: Basic $0, Grow $29–79, Pro $99–229, Ultra $329–999 | Grow: EOD global; Pro: real-time EU; fundamentals on Grow+ | Individual = personal/non-commercial; business plan required for companies [2] | READ [2] |
| Finnhub | Page fetch gave no data; third-party claims international needs paid plan, $11.99–99.99 or higher | Paid only per SNIP | Free = non-commercial per one LOW source | [5][8][30][31]; UNVERIFIED |
| Tiingo | Official page not found | Fundamentals add-on; mostly US per SNIP | Not found | [33][35]; UNVERIFIED |
| SimFin | Free; $15/$35/$71 per month billed annually | Page states only "5,000 US stocks" for free | Not stated | READ [3] |
| Alpha Vantage | Free 25/day; premium about $49.99–249.99 | Fundamentals strongest for US | Not verified | [36][37] LOW |
| Massive (ex-Polygon) | Stocks $0/29/79/199 (third party) | US only; financials from EDGAR | Individual plans may not suit commercial use | [114][115] |
| Morningstar / others | Not researched | | | gap |
No independent accuracy study of EODHD/FMP for European fundamentals was found [127][128]. Sources only advise reconciling about 10 known issuers against filings. Recommended test: pull a restated issuer and a half-yearly reporter from EODHD and Twelve Data, then reconcile with ESEF XBRL.

## D. Company news (EU and Japan)

| Source | Access | Cost / terms | Source |
|---|---|---|---|
| EQS News (DE) | No public API or RSS found; closed/2FA, distributed to terminals | Contact EQS | [116][117][130 LOW] |
| Oslo NewsWeb / Euronext | Paid TCP news feed via order form; no RSS found; archive pages on live.euronext.com | Paid | [57] |
| Nasdaq Nordic / GlobeNewswire | Feeds exist; no URLs found | Not found | [112][113] |
| London RNS | Official REST API and websocket require licence: 2026 guideline GBP 6,500 data feed licence, real-time distribution GBP 26,000; 2027 guideline GBP 7,150 licence | Real-time free for private investors on the LSE site; no current official RSS | [58][59][60][61] |
| TDnet (JP) | Free web view for 31 days; paid API JPY 70,000/mo base plus volume fee; J-Quants TDnet add-on since 2026-05-18 | Redistribution allowed per JPX API page | [62][63][64][65] |
| EDINET (JP) | Free key; XBRL by `type` parameter | Free | [66][67] |
| Finnhub company-news | International requires paid plan (SNIP) | Unverified | [5] |
| Marketaux | Free: 5,000+ sources, 80+ markets, 30+ languages; about 100 req/day (LOW); Pro $99/mo | Terms not read | [53][54] |
| NewsAPI | Developer plan: 24 h delay, dev/test only; Business $449/mo | Not production-usable free | [55][56] |
| Alpha Vantage News | Premium | | [36] |
| Benzinga | Stock news scoped to US equities; no official rate card found | | [118] |
| GDELT DOC 2.0 | 250 records/query, rolling about 3 months, 65 languages, 429 "one request per 5 s" (all from third-party wrappers) | Free | [69][70][71] LOW; official docs NOT found |
| Google News RSS | Old Google terms: personal, non-commercial, no robots; current feed terms not found | Gray area | [110][111] |
| Cision, PR Newswire, Business Wire, Borsa Italiana, BME, SIX, Tiingo News | NOT researched / no result | | gap |

Mapping a release to a ticker
- OpenFIGI `POST https://api.openfigi.com/v3/mapping`, jobs like `{"idType":"ID_ISIN","idValue":...}`, header `X-OPENFIGI-APIKEY`; limits are approximate (no key: 25 requests per minute, 10 jobs per request; with key: 25 per 6 seconds, 100 jobs) [47][48].
- GLEIF publishes a free daily ISIN-to-LEI CSV built from ANNA, but only for national numbering agencies that opted in [49][50]. Use it to link a release carrying an LEI to an ISIN. Many releases carry an ISIN in the text, which can be matched with a regex [own].
Practical advice: RNS, NewsWeb and EQS are not free machine feeds. For a personal app, the cheapest route is a paid aggregator (EODHD news is included in the fundamentals plan [1]; Marketaux free), plus TDnet web view and GDELT as supplements.

## E. Daily bars and adjusted prices

Findings
- yfinance `history(auto_adjust=True)` adjusts OHLC for splits and dividends; `repair` defaults to False; multi-day interval adjustment is "fundamentally broken", so the code fetches 1d and resamples [38][39]. With `auto_adjust=False` you get raw OHLC plus Adj Close [122 LOW][123]. One LOW source claims raw Close is split-adjusted; VERIFY with a test on a known split.
- I found no yfinance doc or issue stating that the in-session bar is returned; search results covered only other platforms (Wealth-Lab, Alpaca, NinjaTrader) [14][15]. Yahoo is known to serve partial daily bars [14 LOW], and the behaviour should be tested.
- Calendars: `exchange_calendars` uses ISO-10383 MIC codes and has early-close days [44]. An extensions package adds special closes but cannot recover each day's time [45]. `pandas_market_calendars` mirrors them and its `schedule()` returns market_open/market_close incl. early closes [46].
Recipe (own)
1. For each exchange, get `cal = exchange_calendars.get_calendar(MIC)`. The last completed session is the latest session whose `cal.session_close(s)` plus a settle buffer (about 30–60 min for data vendors [own]) is before now (UTC). Drop any bar with date greater than that session before storing as daily.
2. 52-week high/low from unadjusted (split-adjusted only) High/Low: `auto_adjust=False`; because Yahoo Close can already be split-adjusted, verify, and apply `Stock Splits` yourself only if needed. Use Adj Close (total return) for returns and drawdowns.
3. Cross-check against a second source: Stooq (symbol.suffix, e.g. `AAPL.US`; no official API; daily hit quota) [99][100], Twelve Data splits endpoint (field names unverified) [126], and Alpaca corporate actions announcements (may lag; reconcile on ex-date) [125].
4. Quarantine rule: if a day's close moves >40% with no split or dividend, or volume is 0 with a changed price, fail the row [own].

## F. Ticker validation

Findings
- yfinance for an unknown symbol: the download path warns "No data found, symbol may be delisted" [93][94]. I found no source confirming what `.info` returns (empty vs sparse), so test it. Guard missing keys such as `quoteType` [93: issue 1929].
- Suffixes: Yahoo uses exchange suffixes (`SAP.DE`, `HSBA.L`) [93] and none for US. Stooq needs `.US` [99]. EODHD uses `.US` per its docs (not verified in this research). Trading 212 conventions not researched.
- Alpaca: the assets endpoint is the master list, with `status` active/inactive and `tradable`. Delisted usually shows inactive/tradable=false. A symbol that changes may get a suffix or number [95][96][97]. An inactive symbol may be retrievable singly but not in the list [98]. Alpaca asks you to refresh the list daily before market open [95]. No official statement on ticker reuse was found. A history that begins late for a long-listed name is consistent with a reused symbol or a new asset ID after a corporate action [own inference]; key on Alpaca asset `id`, not symbol.
Recipe: store ISIN as the primary key. Map to listing tickers via OpenFIGI [47]. Validate each ticker at ingestion: `quoteType` present, a non-empty last 5-day history, currency matches the expected listing currency, last bar date within N sessions of the calendar. Keep a `symbol_history` table (symbol, valid_from, valid_to, asset_id).

## G. Piotroski and alternatives

Piotroski (2000), nine binary signals [16][17][18]:
1. ROA > 0 (net income before extraordinary items / beginning-of-year total assets).
2. CFO > 0 (CFO / beginning total assets).
3. ΔROA > 0.
4. ACCRUAL: CFO > ROA (CFO/assets greater than ROA).
5. ΔLEVER: long-term debt / average total assets fell.
6. ΔLIQUID: current ratio rose.
7. EQ_OFFER: no common equity issued in the year.
8. ΔMARGIN: gross margin ratio rose.
9. ΔTURN: asset turnover rose.
Sources differ on the leverage denominator [17]. The score was built for high book-to-market stocks, and the 8–9 and 0–2 cutoffs are not from the paper [16].
Data needs: two consecutive years of net income, total assets, CFO, long-term debt, current assets and liabilities, shares outstanding (or equity issuance in the cash flow), revenue and cost of revenue. yfinance annual `income_stmt`, `balance_sheet` and `cashflow` provide these rows, usually only 4 years (3 transitions) and with gaps for non-US names [own; not verified]. A "level ratios" proxy omits all the Δ signals, so it is not the F-score.
Evidence: Walkshäusl (2020) found a strong relation for 2000–2018 in 20 developed non-US and 15 emerging markets [72]. Other European studies are positive [73][74][75]. A Seeking Alpha article argues it no longer works in the US (LOW, opinion) [76]. CXO notes returns concentrate in small, illiquid firms [18]. I did not find a rigorous European post-publication decay estimate.
Alternatives
- Novy-Marx gross profitability = (revenue − COGS) / total assets; predicts returns about as well as book-to-market and is leverage-independent [77][78]. Cheapest to implement.
- Mohanram G-score: 8 signals for the lowest-book-to-market quintile (growth stocks) [79][80][81]. I did not see the 8 definitions in these snippets; read the paper [80].
- QMJ (Asness, Frazzini, Pedersen): profitability, growth, safety, payout; the ranks need five-year histories and beta [82][83].
- Beneish M-score: 8 ratios (DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI); the commonly used cutoff is −1.78 (the −2.22 value was unsupported); excludes financials [84].
- Altman Z'': non-manufacturer variant; Z-score is generally not used for banks (practitioner view only; contested) [85].

## H. Data validation

- pandera: `Check.in_range(min, max, include_min, include_max)` and column schemas for ingestion [86][87]. Great Expectations: `expect_column_values_to_be_between` (inclusive; NULL handling and `result_format` tips are from LOW blogs) [88].
- Recommended (own): (1) a pandera schema per vendor payload with units/currency columns mandatory; (2) range checks with a quarantine table rather than silent clipping (e.g. FCF yield outside −50% to +50%, ND/EBITDA outside −5 to 30, market cap < 1e6); (3) currency assertion `financialCurrency` in ISO set, GBp/GBX handled; (4) vendor reconciliation: compare Yahoo vs EODHD/Twelve Data close within 1% and fundamentals revenue within 2%, and log mismatches; (5) append-only rows with `source`, `fetched_at`, `period_end`.

## Claims I could not verify
- Yahoo's definition of `freeCashflow`; yfinance `financialCurrency` semantics; `mostRecentQuarter` behaviour; yfinance `.info` for unknown symbols; whether yfinance returns the in-session daily bar.
- Whether raw Close with `auto_adjust=False` is split-adjusted.
- Finnhub, Tiingo, FMP and Alpha Vantage official prices and international fundamentals depth; EODHD licence wording.
- EQS, Euronext, Borsa Italiana, BME, SIX, Cision, Business Wire, PR Newswire access (not searched or no result).
- GDELT official limits (only third-party wrappers).
- Moody's exact lease formula; sector leverage thresholds; Altman bank exclusion (practitioner source only).
- Companies House coverage (60% vs 88%); ESEF country list; EDINET quarterly status.
- Morningstar and similar vendors were not researched.

## Numbered URL list (S = search snippet only, R = read via fetch)
1 https://eodhd.com/pricing R
2 https://twelvedata.com/pricing.md R
3 https://simfin.com/en/prices R
4 https://filings.xbrl.org/docs/about R
5 https://finnhub.io/pricing R (page gave only a tagline)
6 https://site.financialmodelingprep.com/developer/docs/pricing S (fetch 403)
7 https://multiples.vc/compare/fmp-api-alternatives S LOW
8 https://www.roic.ai/blog/finnhub-alternative-roic-ai-vs-finnhub-api S LOW (competitor)
9 https://www.xbrl.org/news/well-over-3000-esef-filings-at-filings-xbrl-org-where-are-they-coming-from-and-how-can-we-improve-access/ S
10 https://pypi.org/project/xbrl-filings-api/ S
11 https://github.com/lsalmela/xbrl-filings-api S
12 https://apify.com/bosco_labs/esef-annual-reports S LOW
13 https://github.com/ranaroussi/yfinance/issues/2353 S (title only)
14 https://wl6.wealth-lab.com/Forum/Posts/Yahoo-Provider-gt-Partial-bar-34246 S LOW
15 https://forum.alpaca.markets/t/does-barset-include-the-days-data/2918 S
16 https://alphaarchitect.com/value-investing-research-simple-methods-to-improve-the-piotroski-f-score/ S
17 https://www.gurufocus.com/news/1463975/an-introduction-to-the-piotroski-fscore S
18 https://www.cxoadvisory.com/fundamental-valuation/revisiting-performance-of-piotroskis-fscore/ S
19 https://www.github.com/gadicc/node-yahoo-finance2/discussions/595 S
20 https://github-redirect.dependabot.com/ranaroussi/yfinance/issues/1110 S
21 https://forum.portfolio-performance.info/t/issue-with-yahoo-finance-data/27100 S LOW
22 https://pages.stern.nyu.edu/~adamodar/podcasts/valspr23/session11slides.pdf S
23 https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/valpacket1spr22.pdf S
24 https://www.spglobal.com/ratings/en/research/articles/200226-ahead-of-the-game-minimal-impact-on-s-p-global-ratings-outlook-triggers-from-ifrs-16-lease-changes-for-cana-11363310 S
25 https://www.ey.com/en_gl/ifrs/how-the-leases-standard-impacts-company-balance-sheets S
26 https://www.fticonsulting.com/insights/articles/valuation-implications-ifrs-16 S
27 https://support.twelvedata.com/en/articles/12656239-european-equities-market-data S
28 https://twelvedata.com/llms-full.txt S
29 https://nexusfi.com/d/data-providers/twelve-data S LOW
30 https://dataglobehub.com/api-finder/finnhub-api/ S LOW
31 https://blog.stackademic.com/stock-price-api-best-free-vs-paid-options-2026-82370a377819 S LOW
32 https://eulerpool.com/blog/best-fundamental-data-api S LOW
33 https://tiingo-python.readthedocs.io/en/latest/readme.html S
34 https://quantconnect.com/docs/alternative-data/tiingo-news S
35 https://daytradereview.com/tiingo-review S LOW
36 https://alphalog.ai/blog/alphavantage-api-complete-guide S LOW
37 https://multiples.vc/compare/alpha-vantage-api-alternatives S LOW
38 https://ranaroussi.github.io/yfinance/reference/yfinance.price_history.html S
39 https://ranaroussi.github.io/yfinance/advanced/price_repair.html S
40 https://seekingalpha.com/news/4496798-goldman-analysis-of-european-firms-finds-reporting-frequency-has-no-impact-on-valuations S
41 https://vn.tradingview.com/support/solutions/43000540147/ S
42 https://community.developers.lseg.com/discussion/49988/reporting-frequency-for-fundamental-data S
43 https://github.com/dpguthrie/yahooquery/issues/258 S
44 https://pypi.org/project/exchange-calendars/3.5/ S
45 https://github.com/jenskeiner/exchange_calendars_extensions S
46 https://pandas-market-calendars.readthedocs.io/en/latest/index.html S
47 https://www.openfigi.com/api/documentation S
48 https://openfigi.com/api S
49 https://www.gleif.org/en/lei-data/lei-mapping/download-isin-to-lei-relationship-files S
50 https://anna-web.org/bridging-issuers-and-instruments-7-years-of-isin-lei-mapping/ S
51 https://www.sec.gov/os/accessing-edgar-data S
52 https://edgartools.readthedocs.io/en/stable/resources/sec-compliance/ S LOW
53 https://marketaux.com/pricing S
54 https://www.marketaux.com/faq S
55 https://newsapi.org/pricing S
56 https://geekflare.com/guides/global-news-api/ S LOW
57 https://euronext.com/en/corporate-services/oslo-bors-publication-service S
58 https://www.lseg.com/content/dam/lseg/en_us/documents/rns/rns-pricing-and-policy-guidelines-2026.pdf S
59 https://www.lseg.com/content/dam/lseg/en_us/documents/londonstockexchange-backup/rns-data-feed-technical-specification-v1.3.pdf S
60 https://community.developers.lseg.com/discussion/116201/how-to-i-access-the-rns-announcements-api S
61 https://www.ticker.app/services/rns-data S LOW
62 https://www.jpx.co.jp/english/equities/listing/disclosure/tdnet/ S
63 https://www.jpx.co.jp/english/markets/paid-info-listing/tdnet/ S
64 https://www.jpx.co.jp/english/markets/paid-info-equities/listing/p1j4l40000000q03-att/API_ServiceGuideE.pdf S
65 https://mondovisione.com/media-and-resources/news/jpx-market-innovation-and-research-enhancement-of-j-quants-api-data-service-lau-2026518/ S
66 https://pypi.org/project/edinet/0.3.4/ S LOW
67 https://jpx-jquants.com/en/spec/edinet-major-shareholders S
68 https://www.ifrs.org/content/dam/ifrs/meetings/2025/october/itcg/ap5-jicpa-activities-electronic-disclosures-edinet.pdf S
69 https://rbozydar.github.io/py-gdelt/getting-started/data-sources S LOW
70 https://jentic.com/apis/api.gdeltproject.org/gdelt.md S LOW
71 https://libraries.io/pypi/gdeltdoc/1.8.0 S LOW
72 https://epub.uni-regensburg.de/44019/1/Walksh%C3%A4usl2020_Article_PiotroskiSFSCOREInternationalE.pdf S
73 https://link.springer.com/article/10.1057/s41260-018-0098-3 S
74 https://ideas.repec.org/a/fau/fauart/v66y2016i1p70-94.html S
75 https://www.stockopedia.com/content/piotroskis-powerful-f-score-still-finding-value-shares-in-europe-83743 S LOW
76 https://seekingalpha.com/article/4407684-why-piotroskis-f-score-no-longer-works S LOW
77 https://nber.org/papers/w15940 S
78 https://econpapers.repec.org/RePEc:eee:jfinec:v:108:y:2013:i:1:p:1-28 S
79 https://www.aaii.com/stockideas/article/23359-separating-winning-and-losing-growth-stocks-with-the-g-score S
80 https://springerlink.bibliotecabuap.elogim.com/content/pdf/10.1007/s11142-005-1526-4.pdf S (mirror)
81 https://www.cxoadvisory.com/fundamental-valuation/classic-paper-mohanrams-efficient-growth-investing/ S
82 https://research-api.cbs.dk/ws/portalfiles/portal/60211462/lasse_heje_pedersen_et_al_quality_minus_junk_publishersversion.pdf S
83 https://conference.nber.org/conf_papers/f69835.pdf S
84 https://www.wikipedia.org/wiki/Beneish_M-score S LOW
85 https://en.wikipedia.org/wiki/Altman_Z-score S LOW
86 https://pandera.readthedocs.io/en/stable/reference/generated/methods/pandera.api.checks.Check.in_range.html S
87 https://pandera.readthedocs.io/en/latest/ S
88 https://greatexpectations.io/expectations/expect_column_values_to_be_between S
89 https://esma.europa.eu/esmas-activities/data/european-single-access-point-esap S
90 https://www.securities-services.societegenerale.com/en/insights/views/news/esap-the-platform-will-open-on-10-july-2027/ S
91 https://www.dfsa.dk/financial-themes/capital-market/esap S
92 https://accountancyeurope.eu/publications/european-single-access-point-esap-what-do-auditors-need-to-know/ S
93 https://github.com/ranaroussi/yfinance/issues/1268 S (also issues 359 and 1929 seen as snippets)
94 https://yfin-docs.pages.dev/concepts/missing-data S LOW
95 https://docs.alpaca.markets/reference/get-v2-assets S
96 https://docs.alpaca.markets/eu/docs/broker-api-faq S
97 https://forum.alpaca.markets/t/delisted-tickers/18227 S
98 https://forum.alpaca.markets/t/assets-api-missing-available-symbols/7195 S
99 https://pandas-datareader.readthedocs.io/en/latest/readers/stooq.html S
100 https://www.quantstart.com/articles/an-introduction-to-stooq-pricing-data S LOW
101 https://download.companieshouse.gov.uk/en_accountsdata.html S
102 https://www.icaew.com/insights/viewpoints-on-the-news/2025/jun-2025/frc-marks-completion-of-gamechanging-data-access-project S
103 https://www.gov.uk/guidance/companies-house-data-products S
104 https://advisory.kpmg.us/articles/2018/ifrs-16-asc-842-differences.html S
105 https://www.jonesday.com/en/insights/2018/09/new-ifrs-and-us-gaap-lease-accounting-rules-impact S
106 https://accaglobal.com/gb/en/member/discover/cpd-articles/corporate-reporting/ifrs16-dellernovcpd.html S
107 https://www.ifrs.org/-/media/feature/meetings/2018/may/eeg/ap1b-leases.pdf S
108 https://kroll.com/en/insights/publications/transfer-pricing/transfer-pricing-times-third-quarter-2020/new-lease-accounting-standards-intercompany-loan-benchmarking S
109 https://oldwp.irglobal.com/?p=31015 S LOW
110 https://www.google.com/intl/en_ie/terms_google_news.html S
111 https://codewords.ai/blog/google-news-rss S LOW
112 https://rss.globenewswire.com/Home/Learning-Support/Knowledge-Base/GlobeNewswire-News-Distribution-Service-Nordics.pdf S
113 https://globenewswire.com/Home/About/Nordic-Baltic S
114 https://massive.com/knowledge-base/categories/data-coverage S
115 https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html S LOW
116 https://eqs.com/en-us/ir-services/regulatory-news S
117 https://eqs.com/ir-services/newswire S
118 https://www.benzinga.com/apis/cloud-product/financial-data-apis S
119 https://www.gurufocus.com/term/fcf-yield/LTS:0ICL S LOW
120 https://robinhood.com/us/en/learn/weekly-rundown/19D1bTvUQrbkWNnaNt8fM2/ S LOW (also covers the levered FCF glossary sites agicap, wallstreetprep as snippets)
121 https://themoneybees.co/academy/courses/investing-like-an-institution/8-7 S LOW
122 https://softhints.com/understanding-yfinance-auto_adjust-true-what-changed-and-how-to-fix-it S LOW
123 https://yfin-docs.pages.dev/concepts/adjusted-prices S LOW
124 https://massive.com/docs/migration/guides/yfinance S LOW
125 https://docs.alpaca.markets/reference/CorporateActions S
126 https://qveris.ai/tools/twelvedata.splits.retrieve.v1.43527690 S LOW
127 https://www.findmymoat.com/vs/eodhd-eod-historical-data-vs-financial-modeling-prep-fmp S LOW
128 https://nl-be.trustpilot.com/review/financialmodelingprep.com?page=3 S LOW
129 https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds S
130 https://app.boersengefluester.de/en/newswire S LOW

Total: 130 distinct URLs (6 touched by fetch, of which 4 yielded real content: 1, 2, 3, 4; one gave a tagline, one a 403). The remaining 124 are search snippets only. Citation numbers in the text map to this list; a few places cite grouped snippets (e.g. [120]).
