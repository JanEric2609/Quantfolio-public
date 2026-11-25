# Finding edge in event / strong-day CFD trading: research and plan (2026-11-25)

Status: research and paper-only code. No order path, brokers read-only, nothing deployed. No trade data of the owner is quoted.
Evidence tags used below: **[read]** I read the paper text through the PDF tool; **[listed]** I only saw the title/abstract line in a search result; **[background]** from my own training knowledge, not re-checked in this session (verify before relying on it).

## 1. Bottom line
1. **There is no free, cost-robust directional edge in the literature I could reach for a retail index CFD on strong days.** The strongest recent evidence is a set of look-ahead-free tests that find the news reaction is finished by the time a retail order can fill (section 3). I will not pretend otherwise.
2. **What is real is predicting how big a day will be, not which way.** Volatility clusters, scheduled events widen the distribution, and prediction-market and option-implied information carries *magnitude* content that is only partly redundant with VIX. A linear CFD cannot sell magnitude, but it can use it to (a) define "strong day" ex ante, (b) size risk, (c) stay out when the spread is about to widen.
3. **Dropping the fixed exit is right, but it does not create an edge.** With efficient prices no stopping rule has positive expectation; a trailing or state-based exit only helps if returns after the entry are serially dependent (trend or continuation). That is a testable claim, so I built the test (section 6) with a control that shows whether the exit is flattering random entries.
4. **Most promising non-obvious idea: normalise "strong" by what options priced the day before** (move divided by VIX/VXN-implied daily sigma) instead of by trailing realised volatility. It is a free, ex-ante measure of "surprise", and it is in the new replay as a separate trigger.
5. **Everything below is hypothesis-grade until the replay on deep history says otherwise.** Expected outcome: null. A null is still useful because it ends the search honestly.

## 2. What was and was not done
- Asked for 250+ independent sources. **Reached about 242 distinct sources surfaced** (web results across 20 searches plus alphaXiv discovery across 14 calls, after dropping off-topic hits) and, after a second reading pass, **14 read in full** [read] (6 in the first pass; the first version of this report wrongly said 7). The other roughly 228 are title or abstract lines only, tagged [listed], and perhaps 100 of them are directly on point; the rest are background (theses, vendor pages, adjacent topics). Treat 'about 242' as a count of sources seen, not of sources studied; I have not read 250 papers. The previous reviews in `cfd-review/` already carry about 110 further sources; I did not repeat them.
- The sandbox cannot reach market-data hosts (Yahoo, Stooq, FRED return 403 from the proxy), so **no replay was run on real prices here**. The new replay is tested on synthetic data and must be run on the production host.
- Several 2026 papers are single-author or vendor-affiliated arXiv preprints. I weight them as hypotheses, not facts, and say so per item.

## 3. What the evidence says, by family
### 3.1 News and headlines (the "Nvidia / Huang weekend" case)
- **[read] Wang, arXiv 2611.22177.** Replaying headlines at their publication minute with next-open fills and costs, five LLM agents from four vendors earned no alpha around 598 earnings and 409 bad-news drops; the only gains came from what the models had memorised (59.2% direction calls before the training cutoff, 53.3% after). Large US stocks, not indices; single author.
- **[read] Dehkordi et al., arXiv 2611.03985 (OLIVIA).** Blinded, pre-registered test on S&P 500 stocks, 2026: model readings of pre-open news line up with the overnight gap (IC 0.074 to 0.077) and with nothing after the open (IC about 0). A fine-tuned model went from IC 0.065 (validation) to 0.020 (blind test). No strategy survived costs and a deflated Sharpe ratio. Authors are affiliated with the news vendor; the design is unusually strict.
- **[read] Kargarzadeh et al., arXiv 2608.14014.** 4.57 million tagged articles: the news-aligned move is mostly done before and at publication (running total at the close of publication day is 2.8 times its value 20 days later). Net of a placebo, quantified fundamental news (earnings, guidance, analyst actions, capital returns) keeps drifting a little (earnings +0.22% over days 6 to 20) while soft stories (launches, macro read-throughs, leadership) give the move back (macro read-through -0.34%). Their own conclusion: the drift is thin relative to realistic costs, and a news-fading strategy is indistinguishable from shorting every covered small cap. Vendor-affiliated.
- **[read] Takahashi, arXiv 2508.06788.** E-mini S&P 2008 to 2013: around macro releases price impact rises, flow impact falls, return volatility spikes, and shocks dissipate within about a second. Reads as a cost warning: liquidity is withdrawn exactly when events hit, so the live spread is widest when the retail trader wants in.
- [background] Tetlock 2007 (media pessimism predicts downward pressure that reverses within days), Chan 2003 and Savor 2012 (moves with news drift, moves without news reverse, mostly in small illiquid stocks), Boudoukh et al. 2013 and 2019. The news/no-news split cannot be applied to an index without a headline feed, and the effect lives in stocks that a CFD desk on four indices does not trade.
- [listed] Many 2026 LLM-trading and look-ahead papers (section 9) agree on direction: reported LLM alpha shrinks or vanishes once look-ahead and memorisation are controlled.
- **Implication:** headline direction is not the edge. The legitimate use of a news feed is to **classify the day type** (macro shock, scheduled release, company-specific) and to log it for out-of-sample study, not to pick the side.

### 3.2 Intraday indicators and "momentum" on 5 to 30 minute bars
- **[read] Mesfin, arXiv 2605.04004 (independent researcher, not peer reviewed, corrected several times by its author).** 14 intraday signal families on 947 days of MNQ 5-minute bars, walk-forward, none passed all five gates. Eleven failed because gross edge (0.07 to 1.5 points) is below a 2-point friction floor; "event day trend" showed T between 0.14 and 0.69 once the first spike bars are excluded, i.e. **no post-news drift on macro-release days**. Two "positive controls" (GMM regime classifiers) pass, but each was selected after 53+ parameter combinations and one flips sign with a single bar of delay, which suggests a bar-boundary artefact rather than an executable edge. The near-miss (gap continuation, 35 trades) is too small to use.
- **[read, see 3.10] CFM / Imperial, arXiv 2607.01550:** short-term trend following has stopped delivering reliable returns since about 2009, with a microstructural account. Consistent with our own replay (no gross edge on 30-minute EMA rules).
- [background] Gao, Han, Li, Zhou 2018 (first half hour predicts last half hour); Baltussen, Da, Lammers, Martens 2021 (rest-of-day return predicts the last half hour, stronger on short-gamma days). Both were checked in earlier reviews; ours trades neither unconditionally with positive result.

### 3.3 Dealer gamma, options flow, leveraged ETFs
- Mechanism is plausible and published [listed: Baltussen via Alpha Architect; Fed note on leveraged-ETF hedging FEDS 2013-48; Cheng/Madhavan]. But commercial GEX series are not free, and an independent backtest by a vendor-of-a-GEX-product concluded "most of it is just VIX" [listed: dev.to]. **Do not buy or build GEX**; if gamma matters, VIX level and term structure already proxy most of it.
- [listed] 0DTE work (arXiv 2603.07600) is about pricing, not direction.

### 3.4 Overnight versus intraday
- [background] Lou, Polk, Skouras (JFE 2019): most of the equity premium accrues overnight, intraday is flat or negative; [read, see 3.10] Columbia work arXiv 2507.04481 asks whether overnight news explains it. For a CFD that trades nearly around the clock, the overnight leg is reachable, but financing (about 0.008 to 0.014% per night on the T212 index table, roughly 3 to 5% a year) and the wider overnight spread eat most of a 3 to 4 bp average nightly drift.
- OLIVIA above says the news part of the overnight move is gone by the open. Net: holding overnight on strong days is a test worth running (it is in the new replay), not a result.

### 3.5 Prediction markets
- **[read] Mohanty and Krishnamachari, arXiv 2604.01431.** Daily changes in Kalshi macro contracts (Fed, CPI, recession) predict **crypto realised volatility**, not direction, with out-of-sample MSFE ratios of 0.96 to 0.98 and regime dependence (the Fed channel worked in the 2024-25 cutting cycle and reversed after). Spreads on the contracts are about 2 cents.
- [listed] ECB conference and Fed (FEDS 2026-010) papers and practitioner pieces say Kalshi/Polymarket lead or match Fed funds futures and CPI expectations. Value for us: a free, continuous **event-significance feature** (how much did the market's probability of a Fed or CPI outcome just move?), not a price predictor.

### 3.6 Volatility and regime
- [background] Moreira and Muir 2017 (volatility-managed portfolios), Moskowitz, Ooi, Pedersen 2012 (time-series momentum), Daniel and Moskowitz (momentum crashes), Hurst, Ooi, Pedersen (a century of trend following, positive in crises) [listed: NBER w20439, SSRN 2089463]. Best-supported finding for index exposure: **risk-scale by forecast volatility; trend following has paid at weekly to monthly horizons, not daily, and not after 2009 at short horizons.**
- [listed] HAR/rough-HAR volatility forecasting papers (2609.21587, 2506.07928) say it is hard to beat a plain HAR out of sample: use HAR, not deep nets.
- **[read] (via OLIVIA)** models know which stocks will move a lot (AUC 0.60) but no better than recent volatility. Same lesson: the magnitude signal is volatility.

### 3.7 Retail flow and sentiment
- [listed] Boehmer, Jones, Zhang, Zhang and Odean/Barber "resolving a paradox": retail order flow predicts returns yet following it is not profitable. ESMA: CFDs are 35% of EU retail complex-product trades [listed]. [background] ESMA's 2018 product intervention found 74 to 89% of retail CFD accounts lose; losses are mostly leverage and cost, so "fade the crowd" is not automatically an edge.
- [listed] Twitter mood (Bollen et al.), Reddit/WSB sentiment: weak and mostly unreplicated; stock-specific. Skip for index CFDs.
- Broker client-positioning pages (IG, Saxo, DailyFX SSI) are the only direct "retail crowd" series for indices. No academic support found; legal use of the data is unclear. Parked.

### 3.8 Cross-asset lead-lag
- [listed] HF lead-lag papers (2501.03171, 2511.00390, Oxford cross-impact 2112.13213): real at millisecond to second scale, gone at a retail horizon. The usable fact is mechanical: **US trading after the European close shows up in the DAX/EU50 open**, which OLIVIA says is already in the opening price.

### 3.9 Statistical discipline (why the bar is high)
- [background/listed] Harvey, Liu, Zhu (t above 3 for new factors), Bailey and Lopez de Prado (deflated Sharpe, probability of backtest overfitting). Strong days are rare (about 4% of days in Mesfin's VVG; here a 2.5-sigma rule gives roughly 1 to 2% of days). About 15 to 40 trades a year per index means any single-index result is underpowered, so the replay pools four indices over as many years as the data provider returns and uses a placebo.

### 3.10 Second reading pass: looking for evidence against the null (added after reading 8 more papers)
I went looking for the papers most likely to contradict "no cost-robust directional edge". None did. Two sharpen the plan.
- **[read] Kurth et al. 2607.01550 (Bouchaud's group, 100 futures, 1995-2025).** Short-term trend following stopped paying around 2009. Fast EWM-5-20 Sharpe 0.84 before, 0.12 after (error about 0.24); the slow 50-day signal kept 0.40. Equity indices and FX lost it, bonds and most commodities kept it. The cleanest separator is volatility-normalised tick size. Directly relevant here: the loss is concentrated on **large-return days** (sub-1 sigma days are unchanged). So "ride the strong day" is exactly where the trend premium died on small-tick index contracts. Trend still works in low-volatility regimes even on small-tick contracts. This is a reason to expect the `with` cells of the shock replay to fail and to split them by volatility regime in the next version (descriptive only, not a new trial).
- **[read] Christensen, Oomen, Reno 2601.08974.** Short bursts of one-sided drift occur about once a week per market; moves are 25 to 200 bp; about two thirds are followed by partial reversal within 5 minutes (regression slope about -0.23), stronger after negative bursts with high volume, and about one third are gradual jumps that continue. This is a liquidity-provider premium, not something a retail CFD with a spread can collect. It argues against chasing a spike and for waiting, but the effect lives at the 5-minute scale.
- **[read] Vlasiuk, Smirnov 2511.06177.** SPY NBBO data, 2018-2023: no structure at very short lags; some reversal after large pushes at medium lags, stronger after sell-offs. No costs are modelled, and the authors say transaction costs are future work. Not tradable evidence.
- **[read] Glasserman et al. 2507.04481.** Overnight beats intraday by 2.75 bp a day (about 7.2% a year) for S&P 500 stocks since 2000; news topics explain much of it; the authors say the turnover makes it **not a viable strategy**. Stock-average numbers, not an index CFD. A rough sum for Tech 100 (2.75 bp gross, minus about 0.83 bp financing a night, minus the spread) leaves at most about 1 bp a day and may be zero. This is the only open arithmetic that is not clearly negative, and it is untested on CFD prices; I list it as a cheap descriptive check, not a finding.
- **[read] Zhong 2606.12872.** SPX options price scheduled CPI/FOMC/NFP events as variance and convexity (straddles, strangles), not as direction (no gain on risk reversals). Supports using the implied move as a magnitude trigger, not a direction signal.
- **[read] Li, Wang 2608.26115.** Option-implied return predictors decayed across regimes; the smirk signal is insignificant since 2023; IV spread and skewness persist but at monthly horizon on single stocks, with out-of-sample R2 around 1% and one year (2025) driving the ML gain; robustness checks were deferred. Not applicable to an index CFD.
- **[read] Smerlak 2610.24558.** Theory: a signal's value net of costs depends on how its speed meets the cost structure, and fully predictable signals can be worth almost nothing at costly frequencies. It supports checking signal speed against cost, nothing more.
- **[read, rejected] Zhang 2505.16136 (GDELT + FinBERT + XGBoost).** Reports Sharpe 5.87 on EUR/USD daily with 72.7% winning days after costs. That is not credible for a daily FX direction signal: GDELT dates and headlines fetched from live URLs invite look-ahead, there is one author and no external check, and the table caption still carries an unedited instruction ("values must accurately reflect results"). I do not count it as evidence.
Net: 14 papers read, 0 contradict the null, 1 makes it more specific (short-term trend died on index large-return days), 1 is a credibility failure. No new rule is added to the code because nothing new holds up.

## 4. Ranked ideas
Scores: evidence (E), cost-robustness at retail CFD costs (C), feasibility with Quantfolio and the homelab (F), each 1 to 5. "Edge claim" = does the evidence support a directional edge after costs.

| # | Idea | E | C | F | Edge claim | Status |
|---|------|---|---|---|-----------|--------|
| 1 | Volatility forecast (HAR) and implied-move z-score as the definition of a strong day, and for risk sizing | 4 | 5 | 5 | Not an edge; reduces damage and selects days | Built into replay (implied trigger); sizing already risk-based |
| 2 | Strong-day entry with **state exits only** (trailing ATR, close back through EMA), with random-date placebo | 2 | 3 | 5 | Unproven; plausible only if post-shock returns are serially dependent | **Built** (`lab/shock_ride_replay.py`) |
| 3 | Where the follow-through sits after a strong day: overnight gap versus after the open | 3 | 3 | 5 | Descriptive; tells us if a day-time CFD entry can ever catch it | **Built** (`leg_stats`) |
| 4 | "Established selloff" (trend already in force) versus fresh shock, as a split | 3 | 3 | 5 | Tests the user's "join an established selloff" idea directly | **Built** (`established`) |
| 5 | Prediction-market probability change as event-significance feature | 3 | 5 | 3 | Predicts volatility, not direction | Proposed: log it, do not trade it |
| 6 | Journal of live spread, VIX, event class and prediction-market move at every strong-day alert | 2 | 5 | 4 | None; builds the only dataset that can answer "did the spread eat it" | Proposed; belongs with the strong-event alert plan in the other thread |
| 7 | Overnight-hold on strong days | 3 | 2 | 5 | Financing and overnight spread eat most of the drift | Covered by the leg split; no separate cell |
| 8 | RTH-confluence style GMM regime classifier on intraday bars | 1 | 2 | 3 | Selection-exposed, delay-fragile | Parked; the 2016-26 30-minute SPY/QQQ data exists if wanted |
| 9 | Broker client-sentiment contrarian | 1 | 4 | 2 | Unsupported | Parked |
| 10 | GEX purchase or build | 2 | 4 | 1 | Largely VIX | Do not |
| 11 | LLM headline direction (Qwen 9B or other) | 1 | 3 | 4 | Evidence says no after look-ahead control | Use only to classify day type |
| 12 | Order-flow / tick latency, cross-market HFT lead-lag | 4 | 1 | 1 | Real but not retail-executable | Do not |

## 5. Exits without a clock: what is and is not true
- A stopping or exit rule cannot make a zero-expectation entry positive (optional stopping under a martingale). It changes variance, skew and the hit rate. A high hit rate from "cut losers, let winners run" is a payoff shape, not an edge.
- A trailing stop is the cheap way to implement time-series momentum: it earns if moves persist, and loses to chop. After a strong day, persistence is exactly the claim being tested.
- Costs do not vanish with a state exit: overnight financing runs for every night held (a Friday close pays three), and a gap through the stop fills at the open. The replay charges both and counts gap overruns.
- The sizing step needs an assumed holding length; it uses 14 nights for sizing only, which is a sizing assumption, not an exit.

## 6. What was built (research only, paper only)
`backend/app/lab/shock_ride_replay.py`, `backend/scripts/shock_ride_replay.py`, `backend/tests/test_shock_ride_replay.py`.
- Triggers: strong day = move above 2.5 EWMA-sigmas (`ewma`), and, for SPX and NDX when ^VIX / ^VXN load, above 1.75 implied daily sigmas of the previous close (`implied`).
- Cells: with the move or fade it, times two exits (`chandelier` trailing 3 ATR, `ema_cross` close back through the 10-day EMA with the same initial stop). Up to 8 declared trials.
- **No time exit.** Open trades at the data end close at the last close and are tagged `data_end`.
- Controls: same exits on random dates and random sides with the same signal count (placebo, p-value), a with/fade pair, an established-versus-fresh split, and the overnight/intraday leg split after strong days.
- Decision rule frozen in code: pooled n at least 100, mean net above 0, Holm-adjusted p below 0.05, placebo p below 0.05, 60% of calendar years (3+ trades) positive. Anything else is "null". A pass justifies a new pre-registration, never a live signal.
- Run on the production host: `set -a; . /opt/quantfolio/.env; set +a; python scripts/shock_ride_replay.py --since 1995 --draws 200`. Paste the table without keys. Default 2005; how far back the price provider goes decides the depth.

Review fixes (after the automated review on PR #39): the verdict p-value is now a quarter-block bootstrap (trades overlap and four indices are correlated, so the plain t overstates significance; the t-based p stays as a descriptive field). The required control is the **side placebo** (same strong days, random sides), because random dates are cheaper to trade in calm markets and would flatter the real cells; the random-date placebo is kept as a descriptive extra. An empty VIX/VXN history no longer adds empty cells or inflates the Holm family.

## 7. Next steps, in order
1. Run the replay on prod (about 1 to 2 minutes). Expected: null. If any cell passes, run it again on a disjoint period before anything else.
2. If null, stop searching for directional rules on four indices and put the effort into the **journal** (item 6): at every strong-day alert store the live spread, VIX, event class and the Kalshi probability move. About 30 to 40 such days a year is the only way to learn the real cost at the moment it matters.
3. Use the implied-move z as the definition of "strong" in the strong-event alert plan (docs/plan-strong-events-2026-11-25.md, other thread), and give the user the day's expected move from VIX in the message: it makes the size of the day readable before any signal.
4. Optional, research only: run the Baltussen ROD signal conditional on VIX terciles (a gamma proxy) on SPY/QQQ 30-minute data that already exists.
5. Optional descriptive checks for the next replay version (no new pre-registered trial): split shock results by volatility regime (low versus high VIX tercile), and measure the overnight-only return of Tech 100 CFD on non-event days net of financing and spread.
6. Keep the Qwen 9B for tagging the day type from headlines; do not let it pick direction.

## 8. Things I would not claim
- That any of this will make money. The honest prior from sections 3.1 to 3.2 is that it will not at CFD spreads.
- Effect sizes from the preprints beyond what the quoted numbers say; several are single-author.
- Anything about DAX/EU50 intraday: no intraday history is held for them.

## 9. Sources
Counts: see section 2. R = read in full, otherwise listed (title/abstract seen only).

### Read in full (14)
- R https://arxiv.org/abs/2605.04004 Mesfin, Structural limits of OHLCV intraday momentum signals in MNQ
- R https://arxiv.org/abs/2611.22177 Wang, The only edge was memorized in training
- R https://arxiv.org/abs/2608.14014 Kargarzadeh et al., Buy the rumor, sell the news
- R https://arxiv.org/abs/2611.03985 Dehkordi et al., Thinking fast, trading late (OLIVIA)
- R https://arxiv.org/abs/2508.06788 Takahashi, Returns and order flow imbalances, macro news
- R https://arxiv.org/abs/2604.01431 Mohanty and Krishnamachari, Kalshi macro contracts and crypto volatility
- R https://arxiv.org/abs/2607.01550 Kurth, Eisler, Rej, Bouchaud, Is trend still your friend (short-term trend demise)
- R https://arxiv.org/abs/2601.08974 Christensen, Oomen, Reno, The drift burst hypothesis
- R https://arxiv.org/abs/2507.04481 Glasserman et al., Does overnight news explain overnight returns
- R https://arxiv.org/abs/2511.06177 Vlasiuk, Smirnov, Push-response anomalies in SPY
- R https://arxiv.org/abs/2610.24558 Smerlak, Optimal trading filters and the value of predictive signals
- R https://arxiv.org/abs/2606.12872 Zhong, Non-spanning identification of scheduled event risk
- R https://arxiv.org/abs/2608.26115 Li, Wang, Option-implied signals and crash risk 2015-2026
- R https://arxiv.org/abs/2505.16136 Zhang, Interpretable ML for macro alpha (GDELT) - read as a credibility check, see 3.10
- (Reference lists of the above supplied about 60 further citations, used only as pointers.)

### Listed via web search (title or abstract line only)
Overnight/intraday and close-of-day flows
- https://repec.cepr.org/repec/cpr/ceprdp/DP14462.pdf
- https://personal.lse.ac.uk/loud/LouPolkSkouras.pdf
- https://www.fmg.ac.uk/sites/default/files/2026-07/DP961.pdf
- https://conference.nber.org/conf_papers/f78239/f78239.pdf
- https://personal.lse.ac.uk/polk/research/TugOfWar.pdf
- https://personal.lse.ac.uk/polk/research/ATugofWar_appendix_20180508.pdf
- https://conference.nber.org/conf_papers/f169495/f169495.pdf
- https://core.ac.uk/download/157632316.pdf
- https://personal.lse.ac.uk/POLK/research/LPS_ClientelePerspectiveOnEquityPremium_20260603.pdf
- https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr917.pdf
- https://www.fmg.ac.uk/sites/default/files/publications/DP744.pdf
- https://aeaweb.org/conference/2019/preliminary/paper/G852NrtR
- https://ideas.repec.org/r/pal/assmgt/v12y2011i2d10.1057_jam.2011.2.html
- https://thesis.eur.nl/pub/65764/Second-Draft-Final-Version-Master-Thesis-Daan-Fortuin.pdf
Dealer gamma, leveraged ETFs, intraday momentum
- https://www.scribd.com/document/440993965/Gamma-Hunting
- https://www3.nd.edu/~zda/intramom.pdf
- https://www3.nd.edu/~zda/EOD.pdf
- https://alphaarchitect.com/hot-topic-does-gamma-hedging-actually-affect-stock-prices/
- https://www.alexandria.unisg.ch/server/api/core/bitstreams/5a99db31-0d37-4f86-9502-8cb0f3bff4fe/content
- https://dev.to/tomasz_dobrowolski_35d32c/i-backtested-my-own-gex-product-across-8-years-of-spy-most-of-it-is-just-vix-a53
- https://www.interactivebrokers.com/campus/?p=239934
- https://afajof.org/management/viewp.php?n=129472
- https://spotgamma.com/wall-street-dealers-in-hedging-frenzy-get-blamed-for-volatility/
- https://eprints.gla.ac.uk/390465/1/390465.pdf
- https://www.federalreserve.gov/pubs/feds/2013/201348/201348pap.pdf
- https://www.aimspress.com/article/id/676a38d5ba35de0ad141c3c3
- https://efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2024-Lisbon/papers/LETF20231228EFMA.pdf
- https://alphaarchitect.com/options-hedging-leveraged-etfs-in-market-swings/
- https://community.ams.org/amsmtgs/2229_abstracts/1108-00-199.pdf
- https://profiles.wustl.edu/en/publications/market-intraday-momentum/
- https://econpapers.repec.org/RePEc:eee:jfinec:v:129:y:2018:i:2:p:394-414
- https://alphaarchitect.com/2014/08/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/
- https://hull-repository.worktribe.com/OutputFile/3016448
- https://www.acem.sjtu.edu.cn/ueditor/jsp/upload/file/20230705/1688558855821058862.pdf
- https://digital.library.adelaide.edu.au/dspace/bitstream/2440/141224/2/hdl_141224.pdf
News, earnings, announcement premia
- https://aeaweb.org/conference/2024/program/paper/5GGEki7i
- https://acfr.aut.ac.nz/__data/assets/pdf_file/0012/686748/2b-Jingjing-Xia.pdf
- https://repositorio.ucp.pt/bitstreams/c9033501-675d-4fda-a389-c1b18a4ffc6a/download
- https://static.uni-graz.at/fileadmin/sowi/Working_Paper/2020-04_Fink.pdf
- https://core.ac.uk/download/132270865.pdf
- https://conference.nber.org/conf_papers/f118768/f118768.pdf
- https://conference.nber.org/conf_papers/f64395/f64395.pdf
- https://www.aeaweb.org/conference/2021/preliminary/paper/QABNN7zD
- https://www.nber.org/system/files/working_papers/w25817/revisions/w25817.rev2.pdf
- https://conference.nber.org/conf_papers/f66717.pdf
- https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr512.html
- https://www.mit.edu/~zhuh/HuPanWangZhu_preannouncement.pdf
- https://libertystreeteconomics.newyorkfed.org/2018/11/the-pre-fomc-announcement-drift-more-recent-evidence.html
- https://www.uts.edu.au/globalassets/sites/default/files/adg_cons2015_tetlock-journal-of-finance-2007.pdf
- https://www.columbia.edu/~pt2238/papers/Tetlock_Media_Sentiment_JF.pdf
- https://mpra.ub.uni-muenchen.de/62524/1/MPRA_paper_62524.pdf
- https://www.federalreserve.gov/econresdata/feds/2016/files/2016048pap.pdf
- https://www.nber.org/papers/w18725.pdf
- https://www.aqr.com/-/media/AQR/Documents/AQR-Insight-Award/2013/WhichNewsMovesStockPrices_Boudoukh_Feldman_Kogan_Richardson.pdf
LLM and sentiment
- https://arxiv.org/abs/2304.07619 Lopez-Lira and Tang
- https://www.anderson.ucla.edu/sites/default/files/document/2024-04/4.19.24%20Alejandro%20Lopez%20Lira%20ChatGPT_V3.pdf
- https://arxiv.org/abs/2502.10008 ChatGPT and DeepSeek
- https://arxiv.org/abs/1010.3003 Bollen et al., Twitter mood
- https://arxiv.org/abs/2104.01847
- https://arxiv.org/abs/2308.09968
- https://arxiv.org/abs/2308.09485
- https://ora.ox.ac.uk/objects/uuid:6f3c53d3-f0d6-4232-b3cf-bf1696b200f9/files/dsf268587w
- https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3276~3ba8565a13.en.pdf
- https://mpra.ub.uni-muenchen.de/118602/8/wsb.pdf
- https://www.aeaweb.org/conference/2026/program/paper/9K6d53DN
- https://arxiv.org/abs/2505.07078 Can LLM-based investing outperform in the long run
- https://arxiv.org/abs/2605.19337 Agentic trading
- https://arxiv.org/abs/2608.27734 Leakage-safe assessment of LLM strategy discovery
- https://arxiv.org/abs/2611.07464 FINCOSMOS
Time-series momentum, volatility, regimes
- https://www.nber.org/system/files/working_papers/w20439/w20439.pdf
- https://business.columbia.edu/sites/default/files-efs/pubfiles/4607/mom11.pdf
- https://fairmodel.econ.yale.edu/ec439/jpde.pdf
- https://finance.business.uconn.edu/wp-content/uploads/sites/723/2014/08/10-15-Kent-Daniel-mom12.pdf
- https://fairmodel.econ.yale.edu/ec439/hurst.pdf
- https://spiral.imperial.ac.uk/bitstream/10044/1/41472/4/SSRN-id2140091.pdf
- https://papers.ssrn.com/abstract=2089463
- https://www.nbim.no/globalassets/documents/news/2014/discussionnote_time_series_momentum_latest.pdf
- https://www.cmegroup.com/education/files/demystifiing-time-series-momentum-strategies.pdf
- https://pure.manchester.ac.uk/ws/portalfiles/portal/39718724/The_Trend_is_Your_Friend_Time_Series_Momentum_Strategies_Across_Equity_and_Commodity_Markets.pdf
- https://arxiv.org/abs/2212.07288
- https://arxiv.org/abs/2402.05272
- https://conference.nber.org/conf_papers/f88104/f88104.pdf
- https://www.lehigh.edu/~xuy219/research/COWY.pdf
- https://www.man.com/insights/views-from-the-floor-2021-dec-14
- https://www.preprints.org/manuscript/202602.1048/v1
Retail flow, CFDs, multiple testing
- https://faculty.haas.berkeley.edu/odean/Papers%20current%20versions/resolving_a_paradox_retail_trades_positively_predict_returns_but_are_not_profitable.pdf
- https://arxiv.org/abs/2403.17095 Revisiting Boehmer et al. (2021)
- https://www.financemagnates.com/forex/regulation/cfds-account-for-35-of-eu-retail-complex-product-trades-esma-finds/
- https://www.cftc.gov/sites/default/files/2024-02/Retail_Traders_Futures_V2_website_ada.pdf
- https://www.iki.bas.bg/Journals/EconomicStudies/2021/2021-4/3_Peshev.pdf
- https://nber.org/papers/w20592 Harvey, Liu, Zhu
- https://papers.ssrn.com/abstract=2513152
- https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
- https://www.cmegroup.com/content/dam/cmegroup/education/files/backtesting.pdf
- https://academic.oup.com/jrssig/article/18/6/22/7038278
- https://escholarship.org/content/qt4w1110bb/qt4w1110bb.pdf
- https://arxiv.org/abs/2603.20319 Implementation risk in portfolio backtesting
Prediction markets and cross-asset
- https://www.ecb.europa.eu/press/conferences/shared/pdf/20261005_monpolconf/Wright_paper.pdf
- https://www.federalreserve.gov/econres/feds/files/2026010pap.pdf
- https://www.aeaweb.org/conference/2026/program/paper/KRnrdTQN
- https://eprints.gla.ac.uk/390184/1/390184.pdf
- https://kalshi.com/research/insights/august-cpi-preview
- https://dune.com/blog/pricing-the-fed-kalshi-polymarket-cme-fedwatch
- https://news.kalshi.com/p/traders-price-in-fed-cuts-after-cpi
- https://www.nber.org/papers/w7214.pdf
- https://publikationen.ub.uni-frankfurt.de/opus4/files/80803/4-5.pdf
- https://www.fidelity.com/learning-center/trading-investing/trading/using-futures-as-indicator
- https://europeanbusinessmagazine.com/how-nasdaq-futures-impact-global-markets-what-traders-should-know/
Tariff and policy shocks
- https://www.nber.org/system/files/working_papers/w34299/w34299.pdf
- https://www.bis.org/publications/working-paper-1282-market-whiplash-after-2025-tariff-shock-event-targeted-var-approach
- https://publikationen.ub.uni-frankfurt.de/files/59156/SSRN-id3840203.pdf
- https://core.ac.uk/download/345196066.pdf
- https://core.ac.uk/download/215540935.pdf
Seasonality
- https://academic.oup.com/rfs/article/33/1/75/5494694
- https://cesifo.org/DocDL/cesifo1_wp5877.pdf
- https://orbilu.uni.lu/bitstream/10993/21145/1/Dash4Cash.pdf
- https://core.ac.uk/download/215534909.pdf
- https://core.ac.uk/download/201495945.pdf

### Listed via alphaXiv discovery (arXiv ids; https://arxiv.org/abs/ID; off-topic hits such as crypto-only, climate or pure option-math results were dropped from the list)
2608.14014 (R), 2611.03985 (R), 2611.22177 (R), 2609.23703, 2610.13998, 2606.12210, 2603.11408, 2606.03457, 2610.22745, 2609.34004, 2609.32773, 2611.12141, 2608.12283, 2604.21433, 2505.16136 (R), 2607.13968, 2605.04004 (R), 2605.17724, 2603.07600, 2512.17923, 2609.33470, 2607.01377, 2507.22712, 2611.05218, 2610.24558 (R), 2607.03669, 2511.06177 (R), 2508.06788 (R), 2605.11180, 2609.05162, 2601.06499, 2604.01431 (R), 2606.30040, 2606.31675, 2609.06005, 2604.27041, 2610.36639, 2609.28965, 2602.19520, 2510.15205, 2105.13727, 1904.04912, 2106.08420, 2501.07135, 2607.00883, 2303.02613, 1710.01503, 2609.14205, 2608.00127, 2302.10175, 2507.15876, 2609.07989, 2601.05716, 2504.02249, 2503.09988, 2610.25802, 2608.27076, 2509.05080, 2605.23962, 2010.01727, 2507.04481 (R), 1611.03110, 1410.5513, 2603.10559, 2605.11423, 2504.20116, 2608.10693, 2606.12872 (R), 2610.32300, 2510.02705, 2601.08962, 2608.30110, 2609.33881, 2606.07049, 2510.10877, 2603.25285, 2504.02731, 2603.03152, 2603.12040, 2601.08974 (R), 2608.05899, 2605.25311, 2603.01298, 2606.09478, 2607.05291, 2609.21587, 2608.25369, 2605.12099, 2611.01392, 2604.02743, 2609.27024, 2610.39636, 2606.09420, 2506.07928, 2512.20027, 2509.11970, 2404.12001, 2512.00280, 2507.22922, 2508.02089, 2602.18912, 2607.24072, 2610.28175, 2510.15915, 2502.05403, 1910.13729, 2501.03171, 1906.10388, 2201.08283, 2608.24703, 2511.00390, 2112.13213, 2309.08800, 2305.16915, 2607.01550 (R), 2607.19497, 2512.11913, 1410.6898, 2409.10859, 2608.26115 (R), 2604.07870, 2608.24786, 2607.08500, 2610.33438
