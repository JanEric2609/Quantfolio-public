# Quantpedia mining: ranked shortlist for Quantfolio (2026-11-25)

Research and report only. No code touched. Evidence tags: **[read]** = page or abstract read this session, **[memory]** = from my own knowledge, not re-verified today, **[Quantpedia]** = Quantpedia's own page, which I treat as a pointer, not as proof.

## 1. What I could and could not do on quantpedia.com

- robots.txt (read) allows strategy pages; it disallows only `/wp-admin/`, `/?s=`, `/search/`. Sitemaps are listed.
- The cloud container's proxy refused quantpedia.com (HTTP 403). I read the pages through the browser pane on your computer, one page at a time, as a normal visitor. No bulk scraping, no login, no paywall workaround.
- Pages actually read: robots.txt, the screener, Turn of the Month, Asset Class Trend-Following, FOMC Meeting Effect. An attempt to batch-fetch 16 more pages by script returned nothing, and I did not retry it. **The blog and research pages were not read.**
- The screener shows **82 free strategies out of 1000+**; most rows say "Upgrade your subscription". So this is a view of the free tier only (roughly the first ~170 IDs are visible, about half of them locked). The out-of-sample backtest pages (800+) are paywalled; I did not see any.
- Quantpedia's headline numbers are the source paper's in-sample results, with costs "Not Reported" or "Excluded". They are not out-of-sample evidence.

## 2. Ranked shortlist

Filter used: monthly or slower, long-only, UCITS ETFs, whole shares, 1.25 EUR minimum per order, no derivatives, no shorting.

| # | Candidate | Quantpedia says | Cross-check | Expected decay | Cost at small size | Verdict |
|---|---|---|---|---|---|---|
| 1 | **Slow trend filter (Faber 10-month SMA, 5 asset classes)** | 1973-2008: 11.27 % a year, vol 6.87 %, max drawdown -29.43 %, Sharpe 1.06; "Strong" confidence; costs excluded [Quantpedia] | Zakamulin, 155 years: "no statistically significant evidence that market timing strategies outperformed the market in the second half" but they tend to do better in bear states [read]. Huang, Li, Wang, Zhou (JFE 2020): little evidence of time-series momentum in or out of sample; performance "virtually the same" as a sample-mean strategy [read] | Large for excess return; the drawdown reduction is the part most likely to survive | 5 sleeves, about 3-4 round trips a year in total per Faber: 10-20 orders a year, roughly 12-25 EUR at 1.25 EUR each. Fine above about 2,000 EUR, heavy at 250 EUR | **Register, but judge it on drawdown and CVaR against buy-and-hold, not on excess return** (see section 3) |
| 2 | **Halloween / sell-in-May switch** (EUNL in winter, cash ETF in summer) | Not read on Quantpedia | The paper I fetched for this was a different one (Lee 2021, long-term reversal; preprint) and does not test it [read]. The original is Bouman and Jacobsen (2002) and a 2021 follow-up by Zhang and Jacobsen [memory, not verified] | Probably large; calendar effects "vanish or rotate" (Quantpedia's own caution on Turn of the Month) | 2 round trips a year, about 5 EUR | **Forward-lab cell only, weak prior.** Verify the primary papers before registering |
| 3 | **Dual / relative momentum between equity and bond ETFs** (Quantpedia "Paired Switching", "Momentum Asset Allocation") | Both visible in the free list, pages not read | Same objections as 1 (Huang et al.) [read]. Faber-style filters are the same family | Large | Low turnover | Count it in the same trial family as 1; do not register separately |
| 4 | **Factor ETF tilts** (min-vol, momentum, quality UCITS ETFs vs MSCI World) | Low Volatility Factor and Momentum Factor are on the free list | Post-publication decay: McLean and Pontiff (about 58 % lower after publication) and Jacobs and Müller (decay is weaker outside the US) [memory; the Jacobs-Müller page redirected and I did not read it] | Large in the US, possibly smaller in Europe | Cheap: one ETF, few orders | Research only; a one-line benchmark-relative cell in the forward lab at most |
| 5 | **Volatility-managed overlay on MSCI World** | Not a Quantpedia free page I read | Cederburg et al. (JFE 2020), 103 strategies: managed portfolios do not systematically beat unmanaged ones, and out-of-sample versions "generally earn lower certainty equivalent returns and Sharpe ratios" [read] | Already negative out of sample | Monthly rescale, extra orders | **Do not build** |
| 6 | **Turn of the Month** (long from the last day to the 3rd trading day) | 1926-2005: 7.2 % a year, vol 6.9 %, Sharpe 1.04, no costs; "Strong" [Quantpedia] | Caveat from Quantpedia itself: calendar effects can vanish [Quantpedia]. No out-of-sample test read | Unknown, likely large | 12 round trips = 24 orders = about 30 EUR a year at 1.25 EUR. That is 3 % a year of 1,000 EUR, more than the whole effect | **Fails the cost rule at this size** (`breakeven_cost_bps`); keep only as a paper null |
| 7 | **Pre-FOMC drift** | "Strong" confidence, 6.19 % a year [Quantpedia] | Kurov, Wolfe, Gilbert (2021): the drift "essentially disappeared after 2015" [read] | Already gone | 8 meetings = 16 orders a year, and the effect sits in US hours | **Reject.** Quantpedia's "Strong" rating is stale here |

Rejected without a deeper check: short-term reversal and pairs trading (turnover and shorting), 52-week high and stock picking (a 250 EUR pot cannot hold a diversified stock book), variance risk premium and futures carry/momentum (derivatives), soccer clubs' stocks (42 % in-sample, tiny capacity), Alpha Cloning of 13F filings (US filings, 45-day lag, and already read as "alternative data"; not worth a cell).

## 3. A problem with the current gate

ADR 0029 section 3 forbids absolute-return criteria and scores **excess return over MSCI World**. A trend filter is a risk-reduction tool by design: Faber's own claim is "equity-like returns with bond-like volatility and drawdowns" [Quantpedia]. In a long bull market it will usually trail the benchmark, so an excess-return e-process will mostly produce "no promotion", which is correct about alpha but silent about the thing the strategy is for. Proposal: pre-register **two** outcomes in the strategy card, with the e-process on the primary one:

- Primary (authorises anything): excess return net of costs and tax drag, as today.
- Declared risk objective (descriptive, cannot authorise): maximum drawdown and CVaR(5 %) versus buy-and-hold of the same basket, paired block bootstrap. If the strategy wins here and loses on return, the honest label is "a drawdown product", which is a decision for the owner, not for the gate.

## 4. Draft pre-registration specs (proposals, not decisions)

Common fields: frozen hash before the first scored day; benchmark `EUNL.DE`; decision monthly at the last trading day's close, fill at the next close; cost model = IBKR tiered with 1.25 EUR minimum plus half-spread from the repo's quote data, recomputed for the actual order size; tax drag estimated with Vorabpauschale and 25 % on realised gains; a pre-declared family of at most 3 variants; no tuning after registration; trials counted per family via `record_trial`.

**Spec A, `trend_sma10_ucits`**
- Universe: one UCITS ETF per sleeve (world equity, emerging or ex-US equity, aggregate or government bond, real estate, broad commodities), EUR lines, each with at least 2 years of history. Names are fixed in the card.
- Signal: hold a sleeve when its close is above its 10-month simple moving average at month end; otherwise hold the cash proxy (short-term money-market UCITS ETF, or cash).
- Weights: equal across sleeves, as in Faber.
- Variants (the whole family): 10-month SMA; 12-month time-series momentum; 6-month SMA. Nothing else.
- Replay: monthly data back to the longest common history, 2016-2026 in `signal_replay`-style harness, reported as a **warm-up only** (it is not evidence, because the rule was chosen after seeing the literature).
- Scoring: forward only, paper, daily mark.
- Primary metric: excess return net of costs and tax drag versus `EUNL.DE`. Declared risk objective: max drawdown and CVaR versus the equal-weight buy-and-hold basket.
- Expected decay (my prior, not measured): treat Quantpedia's Sharpe of 1.06 as an upper bound; plan for a Sharpe gain near zero and a drawdown reduction of perhaps a third. Not verified.
- Kill: basket cost drag above 0.5 % of sleeve value in any rolling 12 months, or a break in the ledger chain.

**Spec B, `halloween_eunl`**
- Signal: hold EUNL from 1 November to 30 April, cash proxy otherwise. One variant only.
- Benchmark: `EUNL.DE` buy-and-hold. Only about 2 switches a year, so decisions arrive very slowly: a handful of observations over the first years, so the e-process will stay near its starting value for a long time. That is an honest property, not a defect.
- Before registering: read Bouman and Jacobsen (2002) and the later replications myself and record what they find after 2002.

**Spec C, `tom_null` (paper only, as a negative control)**
- Signal: long EUNL from the last trading day of the month to the third trading day of the next, else cash.
- Purpose: it should lose to costs at 1.25 EUR per order, which makes it a useful control that the cost model and the ledger behave. It has no promotion path.

## 5. Fit with existing Quantfolio tools

- `lab/algo_lab` (PR #442, not merged): e-process ledger and `gate_math` take specs A and B directly. `years_to_detect` already says a Sharpe of 0.5 needs about 25 years, so no spec here can be promoted in a useful time; they are learning sleeves.
- `lab/forward_lab`: right home for B and C as cells scored "before the outcome".
- `lab/signal_replay.py` / `desk_replay.py`: daily-bar replay exists; monthly-bar replay for A is a small extension. Replays are warm-up only.
- `foundation/eur_prices.py` (EUR closes, `benchmark_ticker`) already supplies what the cost-and-benchmark maths needs.
- Venue note from ADR 0029: Trading 212 or gettex stay cheaper than IBKR for tiny slow rotations; the venue is a card field.

## 6. What I did not verify (please do not rely on these)

- Bouman and Jacobsen (Halloween), McLean and Pontiff (decay), Jacobs and Müller (global decay): memory only. The Jacobs-Müller page redirected to another host and I did not follow it.
- Quantpedia's paywalled out-of-sample backtests, blog and research pages.
- Hurst, Ooi, Pedersen "A Century of Evidence on Trend-Following" and Moskowitz, Ooi, Pedersen: cited in ADR 0029's note, not re-read by me today. Note they cover diversified futures, a different object from a 5-ETF basket.
- The IBKR cost figures (1.25 EUR minimum) come from the brief, not from a source I read today.

## 7. Questions for the owner

1. Is a drawdown-product outcome acceptable as a valid "keep" verdict for a trend filter, or must every candidate beat the benchmark on return?
2. May I read the primary Halloween and decay papers and then draft the real strategy cards for A and B on the PR #442 branch?
3. Which cash proxy do you prefer for the out-of-market leg (money-market UCITS ETF or plain cash)?

---

## Addendum (round 2, same day)

New reads this round (browser pane, one page at a time, robots.txt respected):

- Quantpedia "Market Seasonality Effect in World Equity Indexes" (the Halloween page): Quantpedia rates the confidence in the anomaly **Weak** and writes that its own out-of-sample back-test "shows significantly negative performance" and the in-sample result "might have been data mined" [Quantpedia, read]. This replaces my round-1 plan to register a Halloween arm. Verdict: **watch item, not registered.**
- Quantpedia blog "Active Dual Momentum GTAA Strategy" (22 May 2026, own research, weekly rebalance, 9 US-listed ETFs, 2007-2026, benchmark equal-weight of the same 9): every lookback except 1 week beat the benchmark in that sample [Quantpedia, read]. It is in-sample, US ETFs, weekly turnover, no cost model visible in the part I read. Weekly turnover fails the 1.25 EUR minimum at this size. Not a candidate; it is evidence only that the family is robust to the lookback within one sample.
- Quantpedia "Paired Switching": Maewal and Bock, 1991-2011, VFINX vs VUSTX, 11.3 % a year, volatility 9.3 %, Sharpe 0.78, max drawdown -18.41 %, transaction costs excluded, "no attempt to tweak parameters" [Quantpedia, read]. Same family as the trend arms; counted in the same trial family.
- Bouman and Jacobsen (2002, AER 92(5)), working-paper abstract: effect found in 36 of 37 markets; "robust over time"; UK "noticeable since 1694". No post-publication test in the abstract [read, SSRN abstract].
- Jacobs and Mueller (2020, JFE 135(1)), abstract: 241 anomalies in 39 markets; "the United States is the only country with a reliable post-publication decline in long-short returns" [read, IDEAS abstract]. This **softens** my round-1 assumption of large decay for non-US anomalies; it concerns long-short cross-sectional anomalies, not timing rules on one index.
- McLean and Pontiff (2016): the IDEAS page has no abstract; the 58 % figure remains [memory].
- Zhang and Jacobsen (2021) and Maberly and Pierce (2004): not retrieved.

Coverage of the free tier is still partial: 42 strategy or blog pages read across rounds 1-5 (Turn of the Month, Asset Class Trend-Following, FOMC, Market Seasonality, Paired Switching, the Dual Momentum GTAA blog post, Momentum Asset Allocation, Market Sentiment and an Overnight Anomaly, Time Series Momentum Effect, Reversal Effect in International Equity ETFs, Crude Oil Predicts Equity Returns), out of 82 free strategy pages. The screener rows (title, rebalancing period, markets, headline return and volatility) were read for all 82 free IDs.

## Addendum 2 (round 3, same day)

The strategy sitemap (`/strategies/sitemap.xml`, listed in robots.txt) shows about 58 public strategy URLs, not 82; the screener's 82 "free" rows include IDs that have no sitemap entry. I used the sitemap for slugs.

Pages read this round:

- **Momentum Asset Allocation (#0002, Faber relative strength, top 3 of 5 asset-class ETFs by 12-month return, monthly):** 1973-2009, 14.49 % a year, volatility 11 %, **max drawdown -47.77 %**, Sharpe 0.78, costs and taxes excluded; the paper itself notes 1 % round-trip cost and 50-150 bp extra tax when turnover rises from 5-20 % to 70-100 % [Quantpedia, read]. Verdict: higher return, much deeper drawdown than the trend filter; it is not a risk overlay and would have to pass the alpha gate. Same trial family; no separate card.
- **Market Sentiment and an Overnight Anomaly (Vojtko, Hanicova):** 2018-2021 sample, 15.58 % a year, Sharpe 2.12, daily trading; the authors write that costs "would be high" and recommend using it as an overlay, not alone [Quantpedia, read]. Reject: daily round trips against a 1.25 EUR minimum, three-year sample.
- **Time Series Momentum (#0118, Moskowitz, Ooi, Pedersen):** 58 liquid futures, long and short, monthly, 1965-2009, **gross of costs** ("Gross Sharpe ratio"); Quantpedia's own max drawdown line is -33.87 % and Sharpe 1.31 [Quantpedia, read]. **This is a different object from Card B** (long-only, 4-6 UCITS ETFs, no shorts, no futures). The 58-futures evidence does not transfer; Huang et al. then find weak evidence for the pooled effect.
- **Reversal Effect in International Equity ETFs (#0016):** 3-year horizon, 1969-1995, 6.4 % a year; Quantpedia rates it **Weak**, "OOS back-test shows significantly negative performance", possibly data mined [Quantpedia, read]. Reject.
- **Crude Oil Predicts Equity Returns (#0096, Driesprong, Jacobsen, Maat):** monthly timing of the equity index, 1988-2003, 11.9 % vs 10.7 % buy-and-hold, volatility 9.8 % vs 15.1 %, alpha 4.58 % a year (t = 2.32) after 0.10 % per switch, about 6 switches a year; the authors say the table is "not fully free of look-ahead bias" [Quantpedia, read]. Interesting as a risk-overlay candidate (lower volatility), but the sample ends in 2003 and I have no post-2003 test. Watch item; read Jacobsen et al. "Return Predictability Revisited" before any card.

**Cross-page pattern worth recording:** Quantpedia's own "Confidence in anomaly's validity" is "Weak" with a stated negative out-of-sample result on at least two free pages (Market Seasonality / Halloween, Reversal Effect in International Equity ETFs) and "Strong" on pages whose sample ends before 2010 and ignore costs (Turn of the Month, FOMC). The rating is not a decay-adjusted verdict and should not be used as one in a card.

**Still unread:** about 45 of the sitemap's strategy pages and all blog and research pages except the Dual Momentum GTAA post. Their screener rows (rebalancing period, markets, headline numbers) were read. None of the unread rows is slow, long-only, ETF-compatible and relevant to a risk overlay except these, which I would read next: Low Volatility Factor Effect, Trend-following Effect in Stocks, Momentum Factor and Style Rotation, Payday Anomaly, January Barometer, Pre-Holiday Effect, Combining Smart Factors, Momentum and Market Portfolio, ESG factor momentum.

## Addendum 3 (round 4, same day)

More pages read:

- **Trend-following Effect in Stocks (#0144, Wilcox and Crittenden):** all-time-high entry with a 10-day ATR stop, about 1,000 US stocks, daily, 1983-2004, 19.3 % a year, Sharpe 1.24, max drawdown -33.74 %, net of 0.5 % per round trip [Quantpedia, read]. Reject: daily stock picking over a stock universe, US only, sample ends 2004.
- **Low Volatility Factor Effect in Stocks (#0007):** 1986-2006, decile of the lowest-volatility large caps, 11.3 % a year, volatility 10.1 %, max drawdown -45.92 %, Sharpe 0.72, costs excluded, 50 stocks [Quantpedia, read]. Not an algorithm at this size; the investable form is a min-volatility UCITS ETF, an allocation decision for the monthly plan.
- **Momentum Factor and Style Rotation (#0091):** long-short Russell style ETFs, 1972-2005, 9.25 %, volatility 16.01 %, max drawdown -52.49 %, Sharpe 0.33 [Quantpedia, read]. Reject: long-short, US style ETFs, weak risk-adjusted result.
- **Payday Anomaly:** hold the S&P 500 on the 16th of each month only, 1980-2010, **2.57 % a year**, Sharpe 0.6, costs "Not Reported", Quantpedia confidence "Strong" [Quantpedia, read]. Reject: 24 orders a year for 2.57 %; the source paper itself says the effect becomes less prominent as pay schedules change.
- **Blog, "Refining ETF Asset Momentum Strategy" (10 Jan 2025, own research):** 13 US-listed ETFs 2006-2023; their finding is that long-short momentum "underperformed the benchmark across all ranking periods" while the long leg alone beat it [Quantpedia, read]. Relevant as a negative result on shorting; retail shorting of UCITS ETFs is not available to this project anyway.
- **Blog, "Pragmatic Asset Allocation Model for Semi-Active Investors" (11 Jan 2024, own research):** a global tactical allocation with **quarterly tranched rebalancing and a 12-month minimum holding period**, Dec 1927-Aug 2023 on monthly proxies (Nasdaq 100, MSCI ACWI, MSCI EM, US 10Y, gold, cash), with a yield-curve predictor, hedging portfolio and stop loss: 10.73 % a year, Sharpe 0.93, max drawdown -23.98 % [Quantpedia, read]. Two lessons for the cards, neither a proof: (a) a **tranche design** (rebalance only a pre-selected slice each quarter) is a cheap way to lower the number of orders; (b) its tax reason for the 12-month holding period is the US long-term rate; **I do not know of an equivalent holding-period exemption for German fund gains and have not checked**, so that part must not be imported. The result uses constructed proxies and a stack of added components (yield curve, hedge portfolio, stop loss), which makes it a weak candidate for a frozen card.

**Not opened, classified from the screener row and the sitemap only:** the remaining strategy pages (futures, FX, commodities, long-short stock factors, fundamental stock screens, crypto, ESG, text-analysis, VIX term structure, dispersion), and the blog and research index. The screener rows show that none of them is a monthly-or-slower, long-only, ETF-implementable rule that is not already in the same family as Cards A to C, except possibly the calendar ones (January barometer, January effect, pre-holiday, option-expiration week), whose screener rows show daily or monthly timing of one index and fail the 1.25 EUR minimum for the same reason as Turn of the Month. This is a judgement from titles and headline numbers, not from reading the pages.

**Blog posts found by search, not read:** "A Turn of the Month Strategy in Asset Allocation", "Leveraged ETFs in Asset Allocation: Opportunity or Trap?", "Dual vs. Single Momentum in Commodities", "Systematic Tactical Allocation in Emerging Markets vs. U.S.", "Sectoral Intramonth Momentum Cycle". Leveraged ETFs are out of scope by ADR 0029 section 2.

## Addendum 4 (round 5, same day): coverage statement and rating audit

**Pages read in this round** (all via the browser pane, one at a time, intro and profile section only for most):
Value and Momentum Factors across Asset Classes (#0028), Betting Against Beta in International Equities (#0078), 52-Weeks High (#0088), Short Term Reversal in Stocks (#0013), January Barometer (#0113), Pre-Holiday Effect (#0083), Option-Expiration Week (#0102), Earnings Announcement Premium (#0080), Combining Smart Factors, Momentum and Market Portfolio, Synthetic Lending Rates, ESG Factor Momentum, Pairs Trading with Stocks (#0012), Size Factor (#0025), Value (Book-to-Market) (#0026), Momentum in Mutual Fund Returns (#0085), January Effect (#0114), Post-Earnings Announcement (#0033), Dispersion Trading, VIX Futures Term Structure, Pairs Trading with Country ETFs (#0055), Ramadan Effect (#0140), 12 Month Cycle (#0125), Rebalancing Premium in Cryptocurrencies; blog: A Turn of the Month Strategy in Asset Allocation, The Capacity of Equity Anomalies (12 Nov 2026).

**None of them is a candidate.** Reasons by group:

- **Long-short, shorting or leverage, not available as a retail UCITS long-only rule:** Value and Momentum across Asset Classes (turnover 728 % one-way; 4.6 % a year net at 50 bp [Quantpedia, read]), Betting Against Beta (Sharpe 0.51, max drawdown -46.56 %), Style rotation, Short Term Reversal, Smart Factors, 52-Weeks High, Earnings Announcement Premium (max drawdown -57.25 %), Pairs Trading, Size, Value.
- **Stock-picking over about 1,000 names or fundamentals data:** trend following in stocks, PEAD, 12 Month Cycle, ESG momentum, Momentum in Mutual Fund Returns (US no-load funds only).
- **Derivatives or futures:** Dispersion Trading (21 options positions), VIX term structure, TSMOM (58 futures), Synthetic Lending Rates (daily SPY long/short).
- **Calendar rules on one index, fail the 1.25 EUR minimum at the stated frequency:** Turn of the Month, Payday, Pre-Holiday, Option-Expiration Week, January Barometer, FOMC.
- **Rebalancing premium** (crypto, daily, 2018-2021 sample, costs not reported): the underlying idea (periodic rebalancing earns a diversification return, Willenbrock 2021) is not tested by Quantpedia on ETFs; not carded.

**Audit of Quantpedia's "Confidence in anomaly's validity" label** (what I saw on the pages above):

| Page | Label | What the same page says |
|---|---|---|
| Market Seasonality (Halloween) | Weak | own OOS back-test "significantly negative", possibly data mined |
| Reversal in International Equity ETFs | Weak | same wording |
| Pairs Trading with Country ETFs | Weak | same wording, although the paper's Sharpe is 1.66 (1996-2009) |
| 52-Weeks High; January Barometer; ESG Factor Momentum; VIX term structure; Size factor | Moderate | OOS "slightly negative", alpha deteriorating |
| Pre-Holiday Effect | Strong | robustness note: significance "largely absent after the late 1980s/early 1990s" for DJIA (Hansen and Lunde) |
| Payday Anomaly | Strong | 2.57 % a year gross; the paper says the effect fades as pay schedules change |
| Turn of the Month, FOMC, Momentum Asset Allocation, Time Series Momentum, Option-Expiration Week | Strong | samples end 2005-2010, costs not reported or excluded |

So the label tracks the page's own out-of-sample check only where one exists; "Strong" does not mean decay-adjusted or cost-adjusted, and several "Strong" rules (FOMC) have independent evidence of disappearance (Kurov et al. 2021).

**Not opened (25 public strategy pages, classified from the sitemap title and the screener row only):** Soccer Clubs' Stocks Arbitrage, Reversal in PEAD, Short Term Reversal with Futures, Earnings Quality Factor, Earnings Announcements with Stock Repurchases, Term Structure in Commodities, ROA Effect, Dollar Carry Trade, Momentum with Asset Growth, Asset Growth, WTI/Brent Spread, Investment Factor, Net Payout Yield, Net Current Asset Value, ESG Factor Investing, F-Score with Short-Term Reversals, Momentum and Reversal with Volatility, Value within Countries, Reversal during Earnings Announcements, ESG Price Momentum, Positive Similarity of Filings, Lexical Density of Filings, R&D Expenditures, Return Asymmetry in Commodities, Intraday Seasonality in Bitcoin. Every one is a stock-factor, text-analysis, commodity, FX, or crypto strategy; none is a slow long-only UCITS ETF rule. I judged that reading them would not change the shortlist; this is a judgement from titles and numbers, not from the pages.

**Not covered at all:** the rest of the blog (hundreds of posts; I read 6 plus the posts surfaced by search), the research pages, the paywalled strategies (about 1,300 of about 1,390 in the screener header: 82 Free, 130 Prime, 1,143 Premium, 39 Pro) and the paywalled out-of-sample backtest pages. These are behind a subscription and I did not try to get around that.

**Conclusion unchanged.** The only candidates that survive the filter remain the three `slow_etf` cards, judged as risk overlays. The marginal value of reading more of the free tier is near zero; the remaining value is in the paywalled out-of-sample backtests, which the owner would need to buy to see.

## Addendum 5 (round 6, same day): re-check at zero commission (Trading 212 route)

Context from the coordinator: real-money execution would more likely be Trading 212 (zero commission, fractional shares) than IBKR, which stays paper and research. So the question is which rules I rejected **only** because of the 1.25 EUR per-order minimum.

**Assumption, not a measurement:** a half-spread of 2 bp on a liquid EUR-listed UCITS equity ETF, so a round trip costs 4 bp. I have not measured Trading 212 spreads or fills; the repo's own quote data should replace this number before any card is frozen. All edges below are the source paper's gross, in-sample figures against cash, taken from the Quantpedia pages I read.

| Rule | Orders a year | Cost at 4 bp a round trip | Gross edge a year (source, in-sample) | Edge / cost | Rejected only for the minimum? |
|---|---|---|---|---|---|
| Turn of the Month | 12 round trips | 0.48 % | 7.2 % (days -1 to +3, 1926-2005) | about 15x | **Yes for the fee part; it now passes the cost test.** It is also an overlay with exposure on 4 days a month, so against a buy-and-hold benchmark it earns less than the benchmark by construction; the gate would have to be a risk-overlay or a "cash plus" test. McGroarty et al. report out-of-sample portfolio benefits net of costs for a ToM factor as an add-on (abstract level only [read]) |
| Option-Expiration Week | 12 | 0.48 % | about 6.3 % (S&P 100 stocks, 1988-2010) | about 13x | Cost: yes. Evidence: US single-stock result, no out-of-sample test read, and I do not know a UCITS ETF that tracks the S&P 100 at Trading 212 |
| Payday (16th of the month) | 12 | 0.48 % | 2.57 % (1980-2010) | about 5x | Cost: yes. Evidence: the paper says it fades as pay schedules change |
| Pre-Holiday | about 10 | 0.40 % | about 2.4 % over cash (1896-2002) | about 6x | **No.** Hansen and Lunde: significance "largely absent after the late 1980s/early 1990s" for DJIA |
| Pre-FOMC drift | 8 | 0.32 % | about 2.2 % | about 7x | **No.** Disappeared after 2015 (Kurov et al. 2021) |
| Overnight anomaly with sentiment filters | up to about 250 | up to about 10 % if traded daily; about 5 % if filters allow half the days | 15.58 % (2018-2021, SPY, Sharpe 2.12) | about 1.5-3x, and falls with any extra spread | **No.** Daily round trips, three-year sample, close/open fills not controllable on a retail route (my assumption, unchecked) |
| Halloween | 2 | 0.08 % | 8.8 % over 6 months (1970-2003) | n/a | **No.** Quantpedia's own out-of-sample result is "significantly negative"; the cost was never the problem |
| Weekly dual momentum (Quantpedia blog, 22 May 2026) | many | depends on turnover, which I did not see | Sharpe and Calmar above the equal-weight benchmark in one 2007-2026 sample | n/a | Cost: partly. Evidence: US-listed ETFs, in-sample, own research; would need UCITS equivalents |
| Cards A, B, C (monthly trend / vol-scaled) | 10-20 | 0.4-0.8 % | none expected beyond drawdown reduction | n/a | They were never rejected for the fee; the plan already marks them viable on a zero-commission route and not viable on IBKR at 50 EUR slices |

**What changes if execution is Trading 212:**

1. Three rules (Turn of the Month, Option-Expiration Week, Payday) flip from "fails the cost test" to "passes the cost test". None of them flips on evidence: all three are in-sample, pre-2010 or US-only, and none has an out-of-sample test I could read.
2. Per-trade edge is now the issue, not the fee. A rule with a gross edge of about 5-15 times its cost is exactly the kind that small unmodelled costs (wider spreads at the open and close, price at the next available quote, tax) can erase. The e-process ledger will not be able to tell a 15x rule from a 3x rule in less than a decade, which is the plan's own power statement.
3. **Tax (my recollection, not verified here):** each realised gain is taxed at the flat rate unless covered by the saver's allowance; at a 250-1,000 EUR scale that allowance probably absorbs all gains, which would make tax drag near zero for small sleeves. The plan's `estimate_tax_drag` should test this before any card relies on it.
4. **Practical:** the Trading 212 integration in this repo is read-only (AGENTS.md safety constraints), and ADR 0029 phase 4 has the owner place orders by hand. Twelve to twenty-four manual orders a year at specific closes is a real burden; Cards A to C need 10-20 and are scheduled monthly, which fits that constraint better.
5. **Open (unverified):** Trading 212's spreads on the relevant UCITS ETFs, whether orders can be timed to a close or an open, the ETF list, and fractional-share availability for each ISIN. The `broker_instruments.py` module already holds Trading 212's instrument list by ISIN and could answer the availability question without any new data.

**Consequence for the cards:** no new card from this re-check. If the owner wants one calendar rule as a negative control or a cheap learning sleeve on Trading 212, Turn of the Month is the only one with an out-of-sample abstract (McGroarty et al.) and a plausible role as a portfolio add-on; it should be registered as a risk overlay or "cash plus" rule, not scored on excess return over MSCI World.
