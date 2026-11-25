# Algorithmic trading for a small fixed budget: what the literature says (2026-11-25)

Research notes behind ADR 0029 and `docs/plan-algo-trading-2026-11-25.md`. Research only. No order path exists or is proposed here.

## How this was read

Every claim carries a tag saying how far I got:

- **[read]** the full text (alphaXiv/arXiv PDF) was read this session; numbers are from the paper.
- **[page]** a web page was fetched and read; secondary sources (Quantpedia, law-firm notes, a broker's fee page) are marked as such.
- **[abstract]** only the title and abstract/summary returned by alphaXiv search were read.
- **[cited]** known from the repo's own ADRs or from the reference list of a paper I read; I did not open the paper.

About 45 papers surfaced by search. Round 1 read 8 in full. Round 2 added 27 papers read through arXiv PDFs (about half of them in full, the rest verbatim for roughly the first 40k characters with the remainder as a model-extracted summary, marked **[read-part]**; two were rate-limited and not read) and 15 web pages. Not a systematic review. Things I could **not** verify are listed at the end.

- **[read-part]** the arXiv PDF was read verbatim for roughly the first 40k characters; the rest came from a model-extracted summary.

## 1. Selection, overfitting and how to promote a strategy

| Finding | Source | Tag |
|---|---|---|
| Repeated unadjusted p < .05 checks promote a null strategy in **94.5 %** of paths; a wealth-budgeted e-process gate (Promote) promotes **0.0 %** (3,000 paths, 24 hypotheses, 5 bp cost). Price: 70 % power at a 15 bp/day perturbation (about 14 % a year, a huge edge) only after a **median 2,327 sessions** (about 9 years); for a Sharpe-0.4 edge the median is about 56 years (see the review note). The guarantee is for the CLIPPED score, so choose a scale that clips rarely. On public ETF/industry data **no** calendar rule was promoted after costs. | Benhamou et al., arXiv 2611.03384 (ICAIF 2026) | read |
| The guarantee needs: candidate frozen and hashed at birth, a share of a lifetime budget (sum of masses at most 1), cost-adjusted bounded scores, crossing observation not tradable, restarts funded as new candidates, ledger fails closed on missing prices. A promotion is "historical evidence, not a certificate of current profitability". | same | read |
| Admission gates cut false discoveries to ~0 only when the true signal's training t is below about 2.5, at the price of adopting 1-7 % of the time; above t 2.5 they only reduce adoption. Gates must be judged against a **random-admission control at equal adoption rate**. Criteria computed on **absolute returns reject every candidate, including a true t = 4 signal**; they must use excess returns. A published robustness score (MinervaScore) showed no forward relation (Spearman 0.013, p 0.40). | Zheng, arXiv 2610.07701 | read |
| Adaptive specification search produces significant walk-forward backtests under a pure martingale null; expected maximum statistic grows like sqrt(2 ln 2K). A **falsification audit** (run the whole pipeline on induced nulls and microstructure placebos) plus the gap between in-sample winner and walk-forward value is proposed. Power collapses for sparse signals (5 % at phi 0.05, theta 3), so a failed audit is not proof of no edge. Blind-parameter protocol against meta-overfitting. | Nikolopoulos, arXiv 2604.15531 | read |
| Out-of-sample Sharpe haircut for fitted linear predictors: replication ratio rises with true Sharpe and training length, falls with number of signals and assets; on the real commodity data a full 37-signal model kept about 9 % of its in-sample Sharpe and a pooled (panel) model about 68 % (Sec. 5.3); the "30 %" in the earlier version of this note could not be matched to the paper and is withdrawn. Advice: few signals, longest history, pooled coefficients. | Jacquier, Muhle-Karbe, Mulligan, arXiv 2501.03938 | read |
| Retail signal families (RSI, OBV, candlesticks, calendar, golden cross) tested with predeclared statistical, cost and **survival** gates: 4 of 6 refuted, 2 inconclusive, **0 supported**. Diversified momentum benchmark itself inconclusive (minimum detectable Sharpe gap about 0.6 at 312 months). EU replication: 18 country-ETF cells, 4 significant, all negative, 0 after multiplicity correction. Under the ESMA 5:1 equity-CFD scenario, trend and RSI breach a 10 % quarterly liquidation probability. | Darmanin, arXiv 2607.20093 | read |
| Pre-2026 foundations used by the repo: deflated Sharpe (Bailey, Lopez de Prado), PBO/CSCV (Bailey et al.), Reality Check/SPA (White, Hansen), t > 3 hurdle (Harvey, Liu, Zhu), anomalies shrink 26 % out of sample and 58 % after publication (McLean, Pontiff), 65 % of 452 anomalies fail a single t test (Hou, Xue, Zhang). | cited in 2610.07701 and ADR 0015/0025 | cited |
| Eleven other 2026 preprints on backtest honesty (MinervaScore, GT-Score, in-loop deflation exploits, LLM-trading evidence audit of 91 papers: short windows, unmodelled costs, unknown cutoffs; "the only edge was memorized in training") | arXiv 2608.23808, 2602.00080, 2611.14539, 2611.18934, 2611.22177 | abstract |

## 2. Strategy families and what survives costs

**Trend following.** Slow trend is still positive; fast trend broke in 2009. Across about 100 futures, Sharpe by signal speed 1995-2009 versus 2009-2025: tau 5: 0.84 to 0.12 (+-0.24); tau 20: 0.79 to 0.27; tau 50: 0.70 to **0.40 (+-0.26)**. The break is tied to **volatility-normalised tick size**: small-tick contracts (equity indices, FX) lost trend at every horizon, large-tick contracts (yields, many commodities) kept Sharpe 1.0-1.2. The signal degraded even with zero-lag, zero-cost execution, so it is not just cost [read: Kurth, Eisler, Rej, Bouchaud, arXiv 2607.01550]. Equity indices and FX, the natural retail universe, are exactly where it broke.

Closed-form net Sharpe: for a short-memory AR(1) return with lag-1 autocorrelation 0.05 the break-even cost is only **37-41 bp per unit turnover**, at or below realistic costs of 40-60 bp, so short-memory predictability is not exploitable; long-memory components carry the net edge at one-to-three-month spans. A replicated CTA (84 futures, 2 and 20 fees) had net Sharpe about **0.47-0.55** (1999-2026), indistinguishable from the SG Trend index [read: Sepp, Lucic, arXiv 2607.19497].

Asset-class trend following (Faber, 10-month SMA, 5 asset-class indices, equal weight, monthly): Quantpedia quotes 11.27 % a year, volatility 6.87 %, max drawdown -29.43 %, Sharpe 1.06 for 1973-2008; **Faber's own figures as summarised by CXO Advisory (2013) differ: 10.5 % vs 9.9 % buy-and-hold, volatility 7.0 % vs 10.3 %, Sharpe 0.73 vs 0.44, max annual drawdown -9.5 % vs -46.0 %, 1973-2012**, gross, indices not ETFs, no frictions or tax; trust the second set (review 2026-11-25); the same page cites Zakamulin's out-of-sample test with realistic costs finding it only marginally better than passive [page: Quantpedia, secondary].

**Volatility-managed portfolios.** In-sample alphas (77 of 103 positive) vanish in real time; out-of-sample versions generally earn lower Sharpe and certainty equivalents than unmanaged, because of structural instability of the spanning regressions [page: Cederburg, O'Doherty, Wang, Yan, JFE 2020 abstract and Alpha Architect summary].

**Intraday and short-horizon.** 14 OHLCV intraday-momentum families on 947 days of MNQ futures: none passed (T >= 2 net, 30 trades per fold, direction stable across years, permutation p); 11 fail because gross edge is **0.07-1.5 points against a 2-point friction floor**. Two "positive controls" survive but were chosen after 53 parameter combinations, one flips sign under a one-bar delay [read: Mesfin, arXiv 2605.04004]. Consistent with the repo's own replays (ADR 0027/0028: no gross edge for the index rule; technical rules negative after costs).

**Retail day trading.** 19,646 Brazilian futures day traders who started 2013-2015: of the 1,551 who persisted more than 300 days **97 %** lost money, 0.4-0.5 % earned more than a bank teller, **no evidence of learning** [page: Chague, De-Losso, Giovannetti abstract via RePEc/Quantpedia; the page gives 0.4 % in the abstract and 0.5 % in the body, not reconciled]. Barber, Lee, Liu, Odean (Taiwan, under 1 % reliably profitable) is cited in ADR 0027 [cited].

**Others.** Pairs trading PEP-KO failed out of sample [abstract: 2609.35359]. Insider purchases (European, buys only) CAR(0,180) +6.3 % but event-study, not net of costs [from `docs/research/2026-11-25-improvements-and-data-sources.md`, read there]. Option smirk signal decaying to t -1.5 [same note]. Post-earnings drift small in large caps [same note].

## 3. Costs, sizing and risk

- Fractional Kelly and drawdown control: see section 5 (read in round 2). None of the papers gives a rule for a near-zero, uncertain edge; the planning rule stays: size from uncertainty about the edge, not from the point estimate (`quant_metrics.kelly_fraction`), with the drawdown halt set from the Monte Carlo of section 5 and the Sharpe uncertainty.
- ESMA retail CFD caps: 5:1 on individual equities, close-out at 50 % of initial margin [as cited in 2607.20093].
- IBKR Ireland shares, Germany: Tiered 0.05 % with minimum **EUR 1.25**, maximum EUR 29; Fixed (SmartRouting) 0.05 % with minimum **EUR 3.00**; exchange and clearing fees come on top under Tiered; fractional shares offered on Tiered [page: IBKR commissions page, **truncated; the German rows are a model-extracted summary, verify before relying**].
- On a EUR 250 order those minimums are 50 bp (Tiered) and 120 bp (Fixed) per order; 208 bp / 500 bp on EUR 60. Computed in `lab/algo_lab/gate_math.per_order_cost_eur`.

## 4. German tax (round 2: pages read, secondary sources only)

No primary text (EStG, BGBl., BMF letter) was opened. Everything below is from law-firm, trade-press and broker pages. It is research input for the owner's tax adviser, not tax advice.

**Knock-outs and warrants are not Termingeschaefte.**
- BMF letter of 3 June 2021 (supplement to the 18 Jan 2016 letter on Abgeltungsteuer): certificates and warrants are not Termingeschaefte; they are "sonstige Kapitalforderungen". Real Termingeschaefte are options, swaps, forwards, CFDs and futures [page: boerse-online 2021-06-21, private-banking-magazin 2021-06-04].
- BFH, 8 Dec 2021, I R 24/19: knock-out certificates are ordinary debt securities settled on the spot, not Termingeschaefte. The case was a GmbH under section 15(4) sentence 3 EStG, so applying it to a private investor under section 20 is an extrapolation; for individuals the BMF letter carries the rule [page: PwC blog and LTO, both 2022-06-23].
- Sources agree on the result and differ on the basis; KPMG says the tax authority originally wanted the opposite. A tax adviser should confirm the current BMF text.

**Loss offsetting after JStG 2024.**
- Abolished: section 20(6) sentence 5 (Termingeschaefte, EUR 20,000 cap) and sentence 6 (Forderungsausfaelle and worthless assets, EUR 20,000 cap). Retroactive for all open cases back to 2021; finally assessed years keep the old rule. Forvis dates the law 5 Dec 2024 and boerse-online cites the Bundesrat approval of 22 Nov 2024; these are consistent (approval, then promulgation). Banks implement from 1 Jan 2026, with a transitional pot for worthless shares in 2025 [page: Forvis Mazars, Haufe, fundresearch, boerse-online, Der Betrieb].
- Remains: sentence 4, share-sale losses only against share gains. The BFH referred it to the Constitutional Court (2 BvL 3/21); assessments are provisional. Worthless-share losses are booked in the "sonstige Verluste" pot under the transitional BMF letter (Rz. 118), which one source says fully offsettable; whether sentence 4 reaches them is described differently by sources.
- One broker page summary (LYNX, partly model-extracted) still says the EUR 20,000 cap applies to worthless securities; the four other pages say it is gone. I treat the cap as abolished and flag the disagreement.
- Consequence for this project: a knock-out or warrant that expires worthless no longer carries a special cap; the old advice to sell before the barrier is moot. CFDs are Termingeschaefte and, since 2024, offset against all capital income. The classification question no longer changes the cap, but it still decides which loss pot a loss enters.

**Interactive Brokers Ireland and German tax.**
- A foreign broker does not withhold German Kapitalertragsteuer, so no Abgeltungswirkung: the client declares in Anlage KAP (and KAP-INV for funds) [page: Haufe handbook, capitalo 2026-07-14 modified 2026-10-16].
- IBKR's own German tax-forms page lists the US form 1042-S (US-source income, US withholding) for non-US persons; there is no German Steuerbescheinigung or Gewinn- und Verlustbescheinigung. The broker's activity statements are the input. I did not read IBKR's year-end reports page, so what exactly it contains and whether it supports the Anlage KAP lines is **unverified** [page: IBKR, LYNX, which runs depots at IB Ireland].
- No Freistellungsauftrag can be given to IBKR; the Sparer-Pauschbetrag (EUR 1,000, 2,000 joint) is claimed in the return. Losses at IBKR are not netted with a German bank automatically; a Verlustbescheinigung from the German bank (request by 15 Dec, irrevocable) is needed to offset German-bank losses against IBKR gains. The Finanzamt applies Teilfreistellung; the client enters fund amounts before it. Foreign withholding is credited in Anlage KAP. Line numbers change yearly and were not confirmed.
- Foreign-currency trades convert at the ECB reference rate on the day (already modelled by `foundation/ecb_fx.py`).

## 5. Round 2: papers read

Validation and overfitting
- Gencay 2611.14539 [read-part]: moving the deflation bar inside an LLM strategy search made 36 of 37 gate-active runs report "no certifiable discovery"; ungated searches always find something, every selected strategy's out-of-sample CI includes zero (control median OOS Sharpe -0.22). 100 nominal trials had an effective size of 10-32. Bar-independent holdout endpoint, frozen by commit. No planted true edge, so the false-negative rate is unmeasured.
- Zhang 2611.18934 [read]: of 91 LLM-trading papers, 51 % of 75 trading tests deduct no costs, median test window 12 months, 98 % of the uncertain-cutoff papers lack leakage control, 9 % report an adjusted result.
- Santoni et al. 2608.23808 [read-part]: five-gate MinervaScore; on 352 real strategies none sealed, Spearman 0.013 (p 0.40). At 100 trials the deflated Sharpe needs roughly 1.9 annualised on five years.
- AlgoXpert 2603.09219 [read-part]: IS plateau, purged walk-forward with a two-thirds pass rule, catastrophic-drawdown veto, one-shot OOS. The worked example passes WFA with one failing fold and uses one configuration; an example of what not to accept (four variants, no deflation).
- Impossible Trinity of Time-Series Validation 2609.29530 [read-part]: no split gives full training sufficiency, coverage and causality. Pure-noise simulation: shuffled 5-fold IC +0.318, contiguous +0.004, purged 0.000, walk-forward +0.017. Purge or embargo of about the label horizon plus two to three decay lengths.
- GT-Score 2602.00080 [read]: composite objective, 50 stocks 2010-2024, generalisation ratio 0.365 versus 0.185, but Wilcoxon p 0.139 against Sharpe and Cohen's d under 0.2; no selection correction. Not adopted.
- Zheng and Nikolopoulos (round 1) are confirmed by these papers: search objectives and validators must be separate.

LLM trading
- 2611.22177 [abstract plus overview]: agents do not beat buy-before-earnings or buy-every-drop; named-minus-blind alpha +30.3 bp before the training cutoff and -22.0 bp after. Clean post-cutoff samples are 75 announcements and 53 drops.
- Yao et al. 2606.08285 [read-part]: of 53 charted LLM-trading studies 68 % have recoverable costs; no backtest-versus-live gaps reported.

Backtest to live
- Yu et al. 2609.34510 [read-part, crypto, OKX]: 23 of 24 ML methods profitable in backtest; ML median fell from +36.0 % to -9.8 % on paper trading; live (4 weeks, 4 methods) median -0.56 % against BTC +11.63 %. Paper-versus-live price difference about 3 bp versus 19 bp, latency 39-47 s. Short live window.
- Bysik and Slepaczuk 2606.00060 [read-part]: hourly BTC, 10 bp cost; naive sign rule -64 % a year with 10,619 trades; a cost-aware filter (expected move above two times the cost) cut trades to 251 and gave +65 % (summary figures). Cost filter as a design idea.

Sizing and drawdown
- Ryan 2608.01494 [read-part]: conformal Kelly sizing, development growth 28 % a year on 8 ETFs, sealed 2022-2024 window 7-8.5 % versus 9.5 % for unlevered equal weight; about 200 configurations searched, 11 of 13 bootstrap contrasts include zero.
- Lillo et al. 2508.18868 [read]: with a put, Kelly's growth rate is unchanged when the model is right and robust when it is wrong, asymptotically only; no costs.
- Hakobyan and Lototsky 2503.17927 [abstract plus overview]: variance-penalised Kelly; with p=0.75, Kelly f=0.5, ridge f about 0.2 gives about 60 % less stake, 38 % less growth, 86 % less variance.
- Wysocki 2508.16598 and Hsieh and Yu 2410.23536 [abstract plus overview]: put-writing with a VIX-rank Kelly scale (GBM assumption), and a Wasserstein-robust log-optimal portfolio. Not used.
- Landolfi 2608.00127 [read-part]: drawdown Monte Carlo. Median to 90th percentile maximum drawdown in annual-volatility units: Sharpe 1.0 over 2 years 1.01 to 1.68; Sharpe 2.0 over 2 years 0.75 to 1.15; Sharpe uncertainty raises the 90th percentile by 1.23x; persistence (H=0.8) multiplies depth about 8x. For a Sharpe near zero the drawdown is far worse and the horizon dominates.
- Hsieh 2609.23272 [read]: finite-horizon rule keeping drawdown under a limit with probability one on the assumed return support; safe iff the position fits the cushion above the floor; no guarantee outside the support.
- Wu 2610.41990 [read-part]: the deepest and the longest drawdown are distinct under light tails but coincide under heavy tails; Gaussian duration ratio 1/2.
- CAST 2609.14205 [read-part]: low drawdown can come with negative Sharpe; frictionless.

Trend, momentum, seasonality
- Butler et al. 2611.15646 [read-part]: 50 futures, walk-forward 1995-2026, net Sharpe 0.54 linear versus 0.57-0.60 nonlinear per market, not significant; at extreme momentum values every portfolio loses.
- Butler et al. 2611.15657 [read-part]: seasonality factors: day-of-week gross 0.50, net about -4.0; half-year net 0.33; year net 0.39; equal-weight ensemble net 0.62. Fast calendar effects die on costs, slow ones survive in futures at USD 100M scale.
- Benhamou et al. 2507.15876 [read]: CTA decomposition 2010-2025, short-term trend alone Sharpe 0.37, long-term 0.70, benchmark SG CTA Trend 0.14; costs 1.4-1.7 % a year at 4x leverage. Futures and leverage; weak relevance.
- 2611.01392 and 2607.00475 were rate-limited and are not read.

Events and anomalies
- Zhao 2602.06198 [read-part]: insider purchases (US Form 4, microcap USD 30-500M, 2018-2024): day-1 abnormal return 4.13 % for the furthest quintile below the 52-week high versus 0.86 %; most of the gradient is volatility scaling; the 29-day drift is not significant; flagged-line net about 2 % after a 3.2 % round trip (summary); a USD 50k order exceeds 5 % of volume in 53 % of events; survivor-only panel. The author does not claim a profitable rule.
- Graziano 2609.35359 [read]: PEP-KO pairs: in-sample net Sharpe 0.67, walk-forward 0.64 aggregate from folds of 2.14, -0.07, -0.50 on 12, 1 and 1 trades, deflated Sharpe 0.423 over 901 candidates, final out-of-sample year one trade at -USD 28k. Count trades before trusting a Sharpe.
- Dickerson et al. 2604.07880 [read-part]: of 108 corporate-bond signals only 26 of 432 CAPM alphas survive FDR; six-month momentum is an artefact of asymmetric winsorisation; short-term reversal premium of -0.99 % a month falls to -0.09 % once implementation bias is removed. Lesson: ex-ante filters, no month-end price artefacts.
- Lee 2512.11913 [read-part]: a return-decay residual does not improve tail forecasts; overlapping-window Sharpe artefacts. Factor-decay monitoring design only.
- Vlasiuk and Smirnov 2511.06177 [read]: SPY push-response, descriptive, no costs, no out-of-sample.
- Landolfi, Hsieh and the rest of section 3 supersede the earlier "abstract only" note on those papers.

Pattern across rounds 1 and 2: every paper that tests out of sample, after costs, or live finds an effect a fraction of the in-sample or gross one, and several find none.

## 6. What I could not verify

- Quantpedia's strategy database beyond two pages; Moskowitz-Ooi-Pedersen, Hurst-Ooi-Pedersen, Asness-Moskowitz-Pedersen, Gao et al. were not re-read (cited only).
- IBKR's German year-end reports page (tax-nonus-reports) and whether its figures map to Anlage KAP lines; current Anlage KAP line numbers; primary tax texts (EStG, BMF letter, BGBl.).
- 2611.01392 and 2607.00475 (rate-limited); the alphaXiv-only reads in section 5 are abstract plus overview, not full text.
- Whether IBKR's API supports fractional orders for EU ETFs, current Xetra exchange fees, market data cost, IB Gateway 2FA and restart behaviour (broker research already lists these as open).
- EU leveraged-ETF, turbo and ETC alternatives for small-ticket trend exposure.
- Any paper on **European** ETF-level trend with German taxes. None found.
- Alpha decay of calendar and event effects beyond the sources above.
