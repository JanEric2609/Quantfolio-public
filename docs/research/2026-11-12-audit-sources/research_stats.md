# Quantfolio statistics audit: literature research (2026-11-12)

Verification legend used in the source list: FULL = I opened the full text (alphaXiv content tool). SEARCH = I saw a search record (title/abstract/bibliographic summary produced by a search tool), not the paper. CITED = I only saw it named inside another record. Every formula marked (memory) is from my own knowledge and was NOT confirmed against a source this session. Several searches (Pocock/O'Brien-Fleming, Newey-West 1987, Fama-MacBeth, Hansen-Hodrick, Politis-Romano, Li-Ji, Kelly 1956, Angelopoulos-Bates, BaFin, FCA, behavioural evidence on uncertainty communication, arch/scipy/confseq/skfolio docs) were not run (usage limit hit mid-run); those items are in "Unverified" below and carry no citation numbers.

## Item 1. Diebold-Mariano promotion test

Findings
- DM is known to over-reject for multi-step horizons in small samples; HLN [1] proposed a finite-sample correction and t(T-1) reference. HLW 2017 [4] show DM-type tests can yield NEGATIVE long-run variance estimates for multi-step forecasts in small samples and that the Coroneo-Iacone test [2] (weighted-periodogram LRV, fixed-smoothing) had the best finite-sample size and power in their Monte Carlo. Coroneo-Iacone [2]: fixed-b and fixed-m ("fixed-smoothing") asymptotics deliver correctly sized DM tests with few out-of-sample observations, power comparable to bootstrap.
- Kiefer-Vogelsang [7]: with bandwidth a fixed fraction b=M/T, HAC t-statistics have a nonstandard limit; conventional (b->0) critical values over-reject; Bartlett gives highest power but largest size distortion, QS the least distortion.
- Lazarus-Lewis-Stock-Watson [3]: if Newey-West, use truncation S=1.3*T^(1/2) with fixed-b critical values; for a single restriction prefer equal-weighted cosine (EWC) with nu=0.4*T^(2/3) cosine terms and a t_nu reference.
- Diebold 2015 [11]: DM is for comparing forecasts, not models; for model comparison full-sample procedures are simpler. Clark-West [8] is the nested-model fix (MSPE adjusted for the noise of estimating zero parameters, normal critical values slightly conservative); Clark-McCracken survey [14]. Giacomini-White [9] test conditional predictive ability (and a 2019 comment shows the normality claim can fail with fixed estimation windows [82]). Model Confidence Set [10]. Simulation evidence on DM/MCS finite-sample behaviour for tail forecasts [57]; DM under strong dependence loses power [54]; heavy tails/infinite variance of loss differentials break DM asymptotics [56]; self-normalised multistep CPA tests [55]; sample-splitting bandwidth-free inference [61]; a nested-model alternative [62]. A 2026 paper on overlapping delayed feedback in multi-horizon forecasting [60] (alphaXiv id as returned: 2610.calm; id looks nonstandard, treat as low-confidence).
- Bootstrap: stationary bootstrap block length by Politis-White with Patton-Politis-White correction [12] (arch: optimal_block_length, memory).
- Group-sequential: Lan-DeMets alpha spending [13] needs only a spending function, not the number of looks; designed for independent increments with known information fractions. Looks on a DM statistic computed from overlapping h=21 forecasts are not independent increments (the canonical joint distribution assumption fails), which is a plausible cause of the 2-5% vs 0.33% distortion; this is my inference, not a sourced result.
- Anytime-valid replacement: Henzi-Ziegel [5] (e-values for score differences, valid under optional stopping, no DGP assumptions); Choe-Ramdas [6] (confidence sequences + e/p-processes for the weak null "forecaster A better on average"; bounded scores, unbounded via other methods). Related 2026 work: betting tests for stochastic dominance [63], conditional quantile auditing [64], sequential probabilistic inflation forecasts [65], e-backtesting [20,66,67,80], promotion tests [23,73,74,78].

Recommended method
1. Stop using Pocock on DM. Compute d_t = L(e_A,t) - L(e_B,t) only for origins whose outcome has fully realised (t+h<=now).
2. Interim analysis, if kept, with fixed-smoothing: EWC statistic: project d on first nu cosines, nu=floor(0.4*T^(2/3)) -> T=40: 4; T=170: 12; reference t_nu [3]. Bartlett with 20 lags at T=40 means b=0.5 (deep in the fixed-b regime; conventional critical values invalid [7]); at T=170, b=0.12.
3. HLN factor (memory): DM* = sqrt((T+1-2h+h(h-1)/T)/T) * DM, reference t_{T-1}. Arithmetic, h=21: T=40 -> (41-42+10.5)/40=0.2375, factor 0.487 (statistic halves); T=170 -> (129+2.47)/170=0.773, factor 0.879. For T<=2h-1 the bracket can go negative: HLN is unusable at the low end, which supports [4].
4. Preferred: replace by an e-process per residue class. Split origins into h=21 classes (origin index mod 21); within a class forecasts are non-overlapping so score differences are plausible martingale differences; run a Choe-Ramdas/betting e-process [6,16] per class and average the 21 e-processes (an average of e-processes is an e-process; my reasoning, consistent with the fixed mixture in [23]). Promote at E>=1/alpha. Update each class only when its target is realised (delayed feedback).
5. Effective sample: T origins at horizon h carry roughly T/h independent blocks: 40/21 ~ 2, 170/21 ~ 8. Any test with fewer than ~10 blocks has little power; say so in the UI instead of a p-value.
Library: statsmodels.stats.sandwich_covariance.cov_hac(results, nlags, weights_func=weights_bartlett, use_correction=True) [50] (docs warn it was verified only for nlags=0); fixed-b/EWC must be hand-coded (no library function found). arch.bootstrap.StationaryBootstrap, MCS (memory). 
Pre-registration: loss function (and its robustness), h, which origins count, the null (equal expected loss), alpha, the grid/lambda, the stopping rule, min blocks before any decision.
Contested: whether DM may be used for model (vs forecast) comparison [11]; Bartlett vs QS vs EWC [3,7]; conditional vs unconditional ability [9,82].

## Item 2. Capital ladder e-processes

Findings
- Betting CS for bounded data [16]; formal framework [19]; mixture/Chernoff CS [17]; coin-betting optimality for bounded means [69]; power of betting tests [68]; optimal sequential tests are log-optimal e-processes [70]; GRO e-variables [22] (Kelly-type growth optimality).
- Heavy tails: Catoni-style CS needs only a variance bound [15] and extends to 1<p<2 moments; Berry-Esseen for Catoni [77]; a quadratic-envelope e-process for unbounded means [71]; heavy-tailed predictive testing [56].
- Calibration of e-values and combination: arbitrary-dependence e-BH FDR control [18]; improved thresholds [72]; Promote [23] (FULL TEXT, see below).
- Sequential Sharpe/alpha tests: I found no paper giving an anytime-valid test for Sharpe ratio or CAPM intercept. Closest: stochastic dominance betting [63], post-selection Sharpe estimation [76]. State as a gap.

Promote (arXiv 2611.03384, read in full) matches the ladder design: Y=clip(r_net/s,-1,1); E_t=prod(1+lambda*Y); mixture over grid {.025,.05,.10,.20,.35}; mass pi_h with sum<=1; promote at E>=1/(alpha*pi_h); epochs with fresh mass omega_h*q_e; two ledgers. Its stated cautions: clipping is part of the ESTIMAND (null is on the clipped bounded score, not the mean return); validity needs the conditional-mean-nonpositive null at every date; rolling products / resets are not e-processes; certificate is historical, not a profitability guarantee. Its numbers: lambda-mixture costs at most log5 vs best component; with gross alpha 14.4%/yr per ETF component, power 70.1% and median crossing session 2,327 (about 9.2 years) at boundary 480 (= 24/0.05); at 9.6%/yr power 7.7%; at 4.8%/yr zero; 19.2%/yr median 1,743 sessions; 28.8%/yr 1,049 sessions (~4.2 yrs). Its own zero-promotion result on real data shows what honest boundaries imply.

Quantfolio-specific issues (my analysis)
- Excess return vs MSCI World is not alpha: if beta != 1 the null "E[r - r_bm] <= 0" is violated whenever beta>1 in a rising market, so harm/skill processes track market direction. Fix: score r_p - beta_hat_{t-1}*r_bm with beta from data strictly before t (predictable), a legitimate predictable transformation (Promote requires predictable scale/cost [23]); null is then zero conditional alpha under predictable beta.
- Clipping at +-3 sigma is not mean-preserving under skew: the tested mean is that of the clipped variable [23]. State that skill/harm are claims about the clipped score; keep unclipped P&L separate.
- Volatility clustering: validity needs only conditional-mean sign, not iid, if sigma_{t-1}, lambda_t predictable [23]; but power falls when EWMA sigma lags. Heavy-tail alternative: sign-based scores Y=sign(excess) have mean 2p-1 and are bounded without clipping (tests median/symmetry; Shekhar-Ramdas cover symmetry testing [21]); lower power but tail-robust.
- Skill and harm are two separate one-sided tests; alpha must be split (or fund each with mass 1/2), otherwise FWER doubles.

Expected time to cross (arithmetic)
Let s=3*sigma_{t-1}, so Y has sd about 1/3, v=E[Y^2]=1/9, mean mu=SR_d/3 (SR_d=daily information ratio of excess return, IR/sqrt(252)). Log growth with bet lambda ~ lambda*mu - lambda^2*v/2 (bound in [23], eq 7); optimum lambda*=mu/v=3*SR_d; growth g=mu^2/(2v)=SR_d^2/2 per day = IR^2/2 per year. (Valid while lambda*<=0.35, i.e. IR<=~1.85.)
Years to E=10 (ln10=2.303): 4.6/IR^2. IR=1.0: 4.6; 0.75: 8.2; 0.5: 18.4; 0.4: 28.8; 0.25: 73.7.
Years to E=40 (ln40=3.689): 7.4/IR^2. IR=0.5: 29.5; IR=1: 7.4.
Mixture over 5 bets costs up to ln5=1.61 extra log-evidence: at IR=0.5 up to +12.9 yrs (worst case bound; realistic about ln(grid) less when a grid point is near lambda*=3*SR_d=0.095 -> 0.10 is on grid). Funding pi<1 raises the threshold: E>=1/(alpha*pi). A paper portfolio with +2%/yr excess and 5% tracking error has IR 0.4 -> about 29 years to E=10 on this approximation. Typical active IR of 0.3-0.5 therefore means decades; E>=10 is only reachable in under ~10 years for IR>~0.7. (Assumes Y approx continuous, sd 1/3, no fat-tail loss, no beta drift; an approximation, not a simulation.)
Bet sizing recommended by the literature: predictable, data-adaptive bets such as ApproxKelly / aGRAPA in [16]; fixed grid mixture is the conservative choice used in [23]. I did not open the aGRAPA formula this session (memory).
Alternatives: SPRT (optimal for simple vs simple, needs a specified effect size; memory), Lan-DeMets [13] (needs independence). 
Pre-registration: benchmark and beta rule, scale s, clip level, bet grid, pi allocations/epoch masses q_e, alpha split between skill and harm, restart rules (restart = new funded test) [23].
Contested: FWER (Promote) vs FDR (e-BH [18]); whether clipping at 3 sigma should be replaced by Catoni-type [15] or sign scores.

## Item 3. Power of the IC gate

Findings: HLZ [24] propose t>3.0 for new factors under multiple testing; Harvey-Liu [29] calibrate Type I and II errors (double bootstrap, hurdle at chosen FDR, low power); Chen [28] argues most claimed findings are true and HLZ's conclusion rests on misinterpreting insignificance as false discovery; JKP [27] Bayesian hierarchical model: 85% of factors replicate, >75% in 10 of 13 themes; Barras-Scaillet-Wermers FDR [47] (and the reassessment [81]); minimum backtest length [25]; DSR [26]. Fundamental law: IR = IC*sqrt(breadth) (Grinold [30]); transfer coefficient (Clarke et al [31], CITED only); Qian-Hua active risk/IR with IC dispersion [32].
Not found/verified: closed-form variance of cross-sectional rank IC (memory: under H0 Spearman rho has var 1/(N-1)); Newey-West lag choice for overlapping horizon h is h-1 (memory; Hansen-Hodrick uses uniform weights, not guaranteed PSD); Fama-MacBeth. Practitioner IC/ICIR figures: not sourced.

Power arithmetic (assumption: IC sd across dates sigma_IC, NOT sourced; cross-sectional correlation makes it far above 1/sqrt(N): null 1/sqrt(N-1)=0.082 at N=150, 0.027 at N=1400)
Overlap h=20 makes n_eff ~ T/20 = 850/20 = 42.5 independent periods. t = IC/(sigma_IC/sqrt(42.5)).
- sigma_IC=0.10: IC .025 -> 1.63; .03 -> 1.96; .045 -> 2.93. P(t>=3) = P(Z>3-nc): .025: 9%; .03: 15%; .045: 47%.
- sigma_IC=0.05: IC .025 -> 3.3; .03 -> 3.9; .045 -> 5.9 (power 99%+).
- t>=3 is reached by expectation when IC/sigma_IC >= 3/sqrt(42.5)=0.46.
- Sample for 80% power at t>=3: nc=3+0.84=3.84; n_eff=(3.84*sigma_IC/IC)^2; sigma_IC=.10, IC=.03: n_eff=164 periods x 20 days = 3,280 days ~ 13 years of data. Hence a 3.4-year (850-day) sample cannot reliably confirm IC=0.03 unless sigma_IC is small. Bailey et al.'s MinBTL [25] makes the same point as a necessary condition (formula not verified).
Recommendation: t>=3 is calibrated to mined factor zoos [24]; a single pre-registered hypothesis needs only the 1.96-2.58 level (multiplicity count = 1); a mined search should use the actual trial count (item 6). Prefer reporting an empirical-Bayes shrunk IC with interval [27] and decide on posterior probability that IC>0 and exceeds cost-implied hurdle, rather than a hard gate whose power is 15-47% for realistic ICs. The fundamental law gives economic relevance: IR=IC*sqrt(breadth) with IC=0.03 and breadth ~ N*12 independent bets/yr (N=150 -> 1,800 -> IR 1.3 before TC and TC<1; unrealistic) -> shows breadth is overstated when predictions are correlated [32].

## Item 4. Confidence display for BUY/HOLD/SELL

Findings: verbalised LLM confidence is typically overconfident, clustering at 80-100%; no method dominates; hard on professional-knowledge tasks [33] (note AUROC 0.52-0.61 for white-box in their comparison). Verbalised confidences can be better calibrated than token probabilities for RLHF models, ~50% ECE reduction [34]; surveys [36]; internal-state/probability methods [35]. Sampling consistency (self-consistency) is a sampling+aggregation family [41,33]. Conformal: ACI updates alpha_{t+1}=alpha_t+gamma(alpha-err_t) with long-run coverage under shift [37]; weighted conformal beyond exchangeability [38]. Calibration of modern nets, ECE definition, temperature scaling [39]. Proper scoring rules [40]. Retail communication: ESMA marketing-communication guidelines (past performance must not be the main content, with no-prediction statement; forward-looking claims need reasonable assumptions) [51]; PRIIPs RTS made performance scenarios not read as forecasts [52]; ESMA SMSG notes scenario complexity and cognitive bias in past performance, recommends clear warnings [53].
Recommended: do NOT show LLM-verbalised confidence as a probability. Show (a) the empirical hit rate of past verdicts of the same class with a Wilson/Beta interval and n; (b) once enough data, an isotonic or Platt-recalibrated probability on out-of-sample verdicts; score with Brier (reliability-resolution-uncertainty decomposition, memory) and log loss [40]; (c) if intervals on returns are wanted, ACI [37]. Minimum n: with k classes and a reliability diagram of ~10 bins, ECE is badly biased below a few hundred verdicts; for a hit-rate interval of +-10 pp at p~0.5 need n ~ 96 (1.96^2*.25/.01), +-5pp ~ 385 (arithmetic). Because verdicts on overlapping 20-day windows are dependent, divide n by the overlap (n_eff ~ n/20) or cluster by date (see item 5). ECE pitfalls (bin-count dependence, bias at small n): [39] definition; the bias statement is memory.
Gap: BaFin and FCA guidance and behavioural experiments on numeric confidence were not retrieved.

## Item 5. Smaller items

- Max drawdown: include the start value as the first peak (equity curve begins at 1, so a loss from day 0 counts); MDD=min_t(V_t/max_{s<=t}V_s - 1) with V_0 included. No source retrieved for the convention (memory); long-memory/non-Brownian drawdown framework seen [record: 2608.00127, listed as source 85].
- Empirical VaR quantile index: Hyndman-Fan define 9 types (3 rounding, 6 interpolation); which is the default differs by package and version (R doc snippet inconclusive on 7 vs 8) [42]. Pick one explicitly (numpy.quantile method=...), state it; for conservative VaR with n small use the lower/higher variant.
- Efficient-frontier failures: return infeasible points as gaps with a status flag, never substitute the previous solution silently (engineering advice, no source).
- Time-weighted return, missing prices: GIPS 2020 requires TWR, valuation at least monthly and at large external cash flows; income/gains/expenses inside the portfolio are part of the return, not external flows (search summaries of GIPS via secondary sources) [43]. Missing prices: carry-forward must be disclosed, or the sub-period is chained with Modified Dietz (memory). Withholding tax on dividends: my search did not find the GIPS text; GIPS requires returns net of withholding unless reclaimable (memory, unverified) - confirm in 2020 GIPS provisions before changing.
- Beta-adjusted vs raw excess: Jensen alpha = r_p - r_f - beta(r_b - r_f); appraisal ratio = alpha/sd(residual) (memory). Ties to item 2.
- Overlapping hit-rate dependence: n_eff ~ n/h for h-day outcomes; cluster-by-date or use non-overlapping blocks; block bootstrap [12]; kernel variance [3,7].

## Item 6. Multiple-testing bookkeeping

Findings: DSR deflates SR by expected maximum SR under N unskilled trials, using skew, kurtosis, sample length, SR variance [26]; PBO via CSCV [49]; effective number of independent trials by clustering (ONC) [48] (secondary report that the published code has a bug: unverified); Reality Check [44], SPA [45, CITED], StepM [46]; FDR in funds [47,81]; factor hurdles [24,29]; honest search-aware assessment of LLM-driven strategy discovery [75]; "Propose, don't judge" anytime-valid factor referee [73]; evidence-gated adoption [74]; Promote's wealth-split online Bonferroni vs alpha-investing/SAFFRON (named in [23], not separately read); e-BH [18]; improved e-value thresholds [72]; a MinervaScore robustness grade (blog-like preprint, low authority) [79b below].
Recommendation: count trials per family and report N_eff (ONC or eigenvalue-based, Li-Ji not retrieved); report DSR sensitivity at N, N_eff and 2N; for anytime-valid control assign each family a lifetime mass (sum<=1) per [23]; use e-BH [18] only for discovery-type decisions.
Library: arch.bootstrap SPA/StepM/MCS (memory), scipy.stats.false_discovery_control (BH, memory).

## Claims I could not verify
- All formulae marked (memory): HLN factor, Pocock constants, Hyndman-Fan type numbers, GIPS withholding rule, aGRAPA, SPRT, Brier decomposition, Politis-White functions.
- Library functions other than cov_hac: not looked up.
- sigma_IC=0.05-0.10 is an assumption, not a literature value.
- Whether ONC code bug claim is true; whether Pocock boundary under dependent looks explains the observed 2-5%.
- No sequential Sharpe/alpha e-process paper found.
- The id 2610.calm [60] is odd; may not resolve.

## Sources (85)
Status: F=full text opened, S=search record/abstract, C=cited only.
1 Harvey, Leybourne, Newbold (1997) Testing the equality of prediction mean squared errors, Int. J. Forecasting 13(2):281-291, doi 10.1016/S0169-2070(96)00719-4. S
2 Coroneo, Iacone (2020) Comparing predictive accuracy in small samples using fixed-smoothing asymptotics, J. Applied Econometrics 35(4), doi 10.1002/jae.2756, https://eprints.whiterose.ac.uk/155897. S
3 Lazarus, Lewis, Stock, Watson (2018) HAR inference: recommendations for practice, JBES 36(4):541-574. S
4 Harvey, Leybourne, Whitehouse (2017) Forecast evaluation tests and negative long-run variance estimates in small samples, IJF 33(4):833-847, https://eprints.nottingham.ac.uk/43017. S
5 Henzi, Ziegel (2022) Valid sequential inference on probability forecast performance, Biometrika 109(3), arXiv 2103.08402. S
6 Choe, Ramdas (2024) Comparing sequential forecasters, Operations Research 72(4), arXiv 2110.00115. S
7 Kiefer, Vogelsang (2005) A new asymptotic theory for HAC robust tests, Econometric Theory 21(6):1130-1164. S
8 Clark, West (2007) Approximately normal tests for equal predictive accuracy in nested models, J. Econometrics 138(1):291-311. S
9 Giacomini, White (2006) Tests of conditional predictive ability, Econometrica 74(6):1545-1578. S
10 Hansen, Lunde, Nason (2011) The Model Confidence Set, Econometrica 79(2):453-497, doi 10.3982/ECTA5771. S
11 Diebold (2015) Comparing predictive accuracy, twenty years later, JBES 33(1), doi 10.1080/07350015.2014.983236, NBER w18391. S
12 Politis, White (2004) Automatic block-length selection for the dependent bootstrap; Patton, Politis, White (2009) Correction, Econometric Reviews 28(4):372-375, doi 10.1080/07474930802459016. S (correction only)
13 Lan, DeMets (1983) Discrete sequential boundaries for clinical trials, Biometrika 70(3):659-663. S
14 Clark, McCracken (2013) Advances in forecast evaluation, Handbook of Economic Forecasting vol 2 ch 20, pp 1107-1201. S
15 Wang, Ramdas, Catoni-style confidence sequences for heavy-tailed mean estimation, arXiv 2202.01250 (Stoch. Proc. Appl.). S
16 Waudby-Smith, Ramdas (2024) Estimating means of bounded random variables by betting, JRSS-B 86(1), arXiv 2010.09686. S
17 Howard, Ramdas, McAuliffe, Sekhon (2020) Time-uniform Chernoff bounds via nonnegative supermartingales, Probability Surveys 17, arXiv 1808.03204. S
18 Wang, Ramdas (2022) False discovery rate control with e-values, JRSS-B 84(3), arXiv 2009.02824. S
19 Ramdas, Grünwald, Vovk, Shafer (2023) Game-theoretic statistics and safe anytime-valid inference, Statistical Science 38(4), arXiv 2210.01948. S
20 Wang, Wang, Ziegel (2025) E-backtesting, Management Science, arXiv 2209.00991. S
21 Shekhar, Ramdas, Nonparametric two-sample testing by betting, arXiv 2112.09162. S
22 Grünwald, de Heide, Koolen (2024) Safe testing, JRSS-B 86(5), arXiv 1906.07801. S
23 Benhamou et al. (2026) Can we stop the backtest yet? Anytime-valid promotion tests for adaptive financial research agents, arXiv 2611.03384. F
24 Harvey, Liu, Zhu (2016) ... and the cross-section of expected returns, RFS 29(1):5-68, NBER w20592. S
25 Bailey, Borwein, López de Prado, Zhu (2014) Pseudo-mathematics and financial charlatanism, Notices AMS (May 2014). S
26 Bailey, López de Prado (2014) The deflated Sharpe ratio, J. Portfolio Mgmt 40(5):94-107, SSRN 2460551. S
27 Jensen, Kelly, Pedersen (2023) Is there a replication crisis in finance?, J. Finance 78(5):2465-2518, NBER w28432. S
28 Chen, Most claimed statistical findings in cross-sectional return predictability are likely true, arXiv 2206.15365. S
29 Harvey, Liu (2020) False (and missed) discoveries in financial economics, J. Finance 75(5):2503-2553, arXiv 2006.04269. S
30 Grinold (1989) The fundamental law of active management (via CFA Institute reading). C
31 Clarke, de Silva, Thorley (2002) Portfolio constraints and the fundamental law of active management, FAJ. C
32 Qian, Hua (2004) Active risk and information ratio, J. Investment Management (PanAgora PDF). S
33 Xiong et al. (2024) Can LLMs express their uncertainty?, ICLR, arXiv 2306.13063. S
34 Tian et al. (2023) Just ask for calibration, EMNLP, arXiv 2305.14975. S
35 Kadavath et al. (2022) Language models (mostly) know what they know, arXiv 2207.05221. C
36 Geng et al. (2024) A survey of confidence estimation and calibration in LLMs, NAACL, arXiv 2311.08298. S
37 Gibbs, Candès (2021) Adaptive conformal inference under distribution shift, NeurIPS, arXiv 2106.00170. S
38 Barber, Candès, Ramdas, Tibshirani (2023) Conformal prediction beyond exchangeability, Annals of Statistics 51(2), arXiv 2202.13415. S
39 Guo, Pleiss, Sun, Weinberger (2017) On calibration of modern neural networks, ICML, arXiv 1706.04599. S
40 Gneiting, Raftery (2007) Strictly proper scoring rules, prediction, and estimation, JASA 102(477):359-378. S
41 Wang et al. (2023) Self-consistency improves chain of thought reasoning, ICLR, arXiv 2203.11171. S
42 Hyndman, Fan (1996) Sample quantiles in statistical packages, American Statistician 50(4):361-365. S
43 GIPS Standards 2020 (CFA Institute); secondary summaries: PwC GIPS 2020 https://www.pwc.ch/en/publications/2020/PwC-GIPS-2020.pdf, CFA Institute refresher reading. S (secondary)
44 White (2000) A reality check for data snooping, Econometrica 68(5):1097-1126. S
45 Hansen (2005) A test for superior predictive ability, JBES 23(4):365-380. C
46 Romano, Wolf (2005) Stepwise multiple testing as formalized data snooping, Econometrica 73(4):1237-1282. S
47 Barras, Scaillet, Wermers (2010) False discoveries in mutual fund performance, J. Finance 65(1):179-216. S
48 López de Prado, Lewis (2019) Detection of false investment strategies using unsupervised learning methods, Quantitative Finance 19(9):1555-1565. S
49 Bailey, Borwein, López de Prado, Zhu (2017) The probability of backtest overfitting, J. Computational Finance 20(4), SSRN 2326253. S
50 statsmodels.stats.sandwich_covariance.cov_hac documentation, https://www.statsmodels.org/stable/generated/statsmodels.stats.sandwich_covariance.cov_hac.html. S
51 ESMA guidelines on marketing communications (summaries: EY, AIMA https://www.aima.org/article/esma-guidelines-on-marketing-communications.html). S (secondary)
52 Commission Delegated Regulation (EU) 2021/2268 (PRIIPs KID), via JC 2020 66 final report https://www.esma.europa.eu/sites/default/files/library/jc_2020_66_final_report_on_draft_rts_to_amend_the_priips_kid.pdf. S
53 ESMA SMSG advice on joint CP on PRIIPs KID, esma22-106-2077. S
54 Testing for equal predictive accuracy with strong dependence, arXiv 2409.12662. S
55 Self-normalized tests for multistep conditional predictive ability, arXiv 2605.07404. S
56 Heavy tails and predictive ability testing, arXiv 2605.16866. S
57 Evaluating financial tail risk forecasts: testing equal predictive ability, arXiv 2505.23333. S
58 Uncertainty quantification in forecast comparisons, arXiv 2605.03997. S
59 Comparing tail forecasts: the number of exceedances is the sample size, arXiv 2610.40366. S
60 Stay CALM: calibrated online aggregation for multi-horizon forecasting under delayed, overlapping feedback, alphaXiv id 2610.calm. S (id unusual)
61 Another look at bandwidth-free inference: a sample splitting approach, arXiv 2606.00864. S
62 A novel approach to predictive accuracy testing in nested environments, arXiv 2008.08387. S
63 Betting on bets: anytime-valid tests for stochastic dominance, arXiv 2604.21851. S
64 Bet on features: anytime-valid auditing of conditional quantile forecasters, arXiv 2607.11653. S
65 Sequentially valid inference for probabilistic inflation forecasts, arXiv 2608.23064. S
66 E-backtesting expected shortfall without VaR forecasts under time-series models, arXiv 2610.17681. S
67 Comparative e-backtesting, arXiv 2511.05840. S
68 Power comparison of sequential testing by betting procedures, arXiv 2504.00593. S
69 On the optimality of coin-betting for mean estimation, arXiv 2412.02640. S
70 Optimal sequential tests yield log-optimal e-processes, arXiv 2605.12720. S
71 Moment-orthogonal quadratic-envelope e-processes for anytime-valid location inference, arXiv 2611.01637. S
72 Improved e-value thresholds with applications to multiple and sequential testing, arXiv 2610.11186. S
73 Propose, don't judge: an anytime-valid referee for LLM agents that mine investment factors, arXiv 2609.27051. S
74 Evidence-gated research: statistically controlled model adoption in adaptive search, arXiv 2610.01751. S
75 What survives honest evaluation? Leakage-safe, search-aware assessment of LLM-driven trading strategy discovery, arXiv 2608.27734. S
76 Post selection estimation of Sharpe ratios, arXiv 2606.01650. S
77 Berry-Esseen bounds and moderate deviations for Catoni-type robust estimation, arXiv 2602.12589. S
78 PACE: anytime-valid acceptance tests for self-evolving agents, arXiv 2606.08106. S
79 When the martingale never stops firing: anytime-valid gating on real forecast streams, arXiv 2608.30502. S
79b Equity strategy backtesting: luck or edge? The MinervaScore, arXiv 2608.23808 (low authority, vendor preprint). S
80 On E-backtesting: generalizations and sample size determination, arXiv 2609.05089. S
81 Andrikogiannopoulou, Papakonstantinou (2019) Reassessing false discoveries in mutual fund performance, J. Finance. S
82 Tests of conditional predictive ability: a comment (2019, St. Louis Fed working paper). S
83 Diebold, Mariano (1995) Comparing predictive accuracy, JBES 13(3). C
84 Chen, Zimmermann, Publication bias in asset pricing research, arXiv 2209.13623. C
85 Drawdown risk beyond Brownian motion: Monte-Carlo framework, non-Gaussian extensions, long memory, arXiv 2608.00127. S
