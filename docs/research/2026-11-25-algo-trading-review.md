# Skeptical review of the algorithmic trading section (PR #442, order-guard assumptions of PR #443), 2026-11-25

Scope: ADR 0029, the plan, the literature note, `lab/algo_lab`, and the IBKR guard assumptions in PR #443. Method: recompute every number, read the cited papers (alphaXiv full text), read the IBKR fee page and German tax secondary sources, look for contradicting work. Not done: SSRN (unreachable), primary German law (gesetze-im-internet.de and the BMF site are blocked from this environment, so **no primary tax text was read**), Quantpedia (another thread).

Tags: **[verified]** reproduced or read verbatim this session; **[corrected]** wrong or overstated, fixed on the branch; **[open]** not resolved.

## 1. Verdict

The strategic conclusion holds: intraday and event trading will not pay, slow rules are unprovable at this budget, IBKR is a poor venue at 250 EUR. Three things were wrong or overstated and are fixed: the promotion ledger's guarantee (it is about the clipped score), the quoted power numbers, and two source figures (Faber, IBKR API fee rule). Several German tax statements rest on secondary sources only and one date/letter reference needs updating.

## 2. Gate maths

| Item | Result |
|---|---|
| `years_to_detect` (24.7 / 6.2 / 2.7 years at Sharpe 0.5 / 1 / 1.5; 38.0 with 4 trials) | **[verified]** formula reproduced. Valid for the null variance only. Under the alternative the Sharpe estimate has standard error sqrt((1+SR²/2)/T) (Lo 2002), so power needs 25.8 / 7.2 / 3.7 years. **[corrected]** `alt_se=True` added, default unchanged, both numbers in the plan. |
| `oos_replication_ratio` (56 % at 5 years, 72 % at 10, true Sharpe 0.5) | **[verified]** the code matches eq. 3.3 of Jacquier et al. term by term (read in the paper). Scope: one signal, one asset, OLS-fitted, iid signals; a fixed rule such as SMA10 has no fitted coefficient, so this is a bound for fitted models only. |
| Jacquier "ten-year backtest keeps about 30 %" in the literature note | **[corrected]** not found; the paper's real-data figures are 9 % (full model) and 68 % (pooled). |
| `breakeven_cost_bps` 57 bp | **[verified]** 1e4 x 0.4 x 0.10 / 7. Assumes 10 % volatility while invested; an equity-ETF sleeve is nearer 15 %, which raises it. Fine. |
| Drawdown odds (92 % for 20 % in 5 years at 15 % vol, zero edge) | **[verified]** 0.921 by Monte Carlo (0.902 with zero log drift); also 3 years 0.76, 10 % about 1.00. A 15 % halt: 0.53 / 0.83 / 0.99 after 1 / 2 / 5 years; a 25 % halt: 0.10 / 0.36 / 0.78. Gaussian, so a lower bound. The proposed -15 % sleeve halt is a budget expiry within about two years. |
| Promote boundary 1/(alpha x mass) = 60 for three arms; epoch masses 6/(pi²(e+1)²) | **[verified]** against Benhamou et al. (Ville bound, sum of masses at most 1, epochs funded as new candidates). |

### 2.1 The e-process guarantee is for the clipped score (important)

Benhamou et al. state it themselves: "Clipping is part of the estimand: the test concerns the conditional mean of the bounded score, not the unbounded mean return" (Sec. 2). The prototype's docstring and the plan said the false-promotion bound holds for any candidate with a non-positive mean return. That is false for skewed returns. Demonstration with this ledger: daily net excess +0.2 % with 95 % and -3.8 % with 5 % (mean exactly 0), scale 1 %, mass 1/3: **300 of 300 simulated 10-year paths promote** against a bound of 1.7 % per candidate. With scale 5 % (no clipping) the same series never promotes (test added). Consequences: (a) choose the scale from the 99.5th percentile of the absolute net score as the paper does, not smaller; (b) report `clipped_fraction` (added to `to_dict`), keep unclipped returns beside it; (c) negatively skewed candidates (short volatility, carry) are the dangerous class; trend rules are positively skewed, where clipping is conservative. **[corrected]** docstring, counter and test.

### 2.2 Power is far lower than the quoted 2,327 sessions

Benhamou's median crossing of 2,327 sessions is for a 15 bp/day shift, about 14 % a year. Simulating this ledger (Gaussian daily returns, three equal masses): median crossing about 56 years at Sharpe 0.4 (54 % of paths cross within 60 years), 12 to 15 years at 0.8, 4 to 5 years at 1.5, for scale 1 or 4 daily sigma. The plan scores **excess return over a benchmark**; for a timing overlay the excess information ratio is much below the overlay's own Sharpe (Faber: 10.5 % vs 9.9 % return, the gain is mostly lower volatility). So the e-process on excess return will practically never cross for the proposed arms. The plan says this in words ("expected to be inconclusive") but also says "the question for a timing overlay is risk reduction per unit of cost, not alpha" (5.2) and then scores alpha (5.3). **[open, design]** Either test a risk-adjusted or drawdown-based claim with its own null, or state that route (a) is effectively unreachable and the section runs on route (b) only.

## 3. Literature: what was read and what it says

| Claim in plan | Check |
|---|---|
| Kurth et al.: Sharpe 0.70 to 0.40 +-0.26 at tau 50, fast trend 0.84 to 0.12, equity indices and FX lost trend, tick size is the variable | **[verified]** read in full, Table 1 matches; also zero-lag no-cost execution stays flat. The post-2009 slow-trend Sharpe is 1.5 standard errors above zero. |
| Sepp and Lucic: 37-41 bp break-even at AR(1) 0.05 | **[verified]** (their cost unit is per unit of volatility-normalised turnover, realistic 40-60 bp). The "net Sharpe 0.47-0.55 for a replicated CTA" is not in the pages retrieved this session. **[open]** |
| Darmanin: 0 of 6 supported, trend inconclusive, MDE 0.60, EU grid 4/18 cells all negative, 0 after correction, trend and RSI above 10 % liquidation at 5:1 | **[verified]**. Caveat: single-author preprint, no peer review, written with Claude Code assistance (its own disclosure); trend means 50/200 golden cross on NASDAQ-100, not monthly 10-month SMA. |
| Benhamou et al. (94.5 % vs 0.0 %, 70.1 % power, 2,327 sessions) | **[verified]**; accepted ICAIF 2026, AI-assisted LaTeX; see 2.1 and 2.2. |
| Faber "Sharpe 1.06" | **[corrected]** Faber's own numbers (via CXO Advisory 2013): SMA10 five-asset 1973-2012 return 10.5 % (9.9 % buy-and-hold), volatility 7.0 % (10.3 %), Sharpe 0.73 (0.44), max annual drawdown -9.5 % (-46.0 %), gross, indices not ETFs, no frictions, author flags possible snooping in the 10-month choice. Quantpedia's 1.06 differs and is not reproducible from Faber. |

### 3.1 Contradicting evidence the plan did not cite

- **Huang, Li, Wang, Zhou, "Time series momentum: Is it there?", JFE 135(3), 2020** (read: abstract, text to about half). Same 55 futures as Moskowitz-Ooi-Pedersen: asset by asset only 8 of 55 significant at 10 % in-sample, 45 of 55 negative out-of-sample R², average -0.67 %; the pooled t of 4.34 is not significant against bootstrap critical values (12.5 wild, 4.8 pairs); TSMOM performs the same as simply holding every asset with a positive full-sample mean. This is the main academic challenge to the "slow trend works" prior and belongs in ADR 0029 context item 2.
- **Quality of the 2026 sources**: most 2026 papers behind the plan are single-author or small-team preprints, several AI-assisted, few peer reviewed (Benhamou, Darmanin, Mesfin, Gencay, Zheng, Santoni). The conclusions are consistent with older peer-reviewed work (Park and Irwin 2007, Sullivan-Timmermann-White 1999, Bajgrowicz-Scaillet 2012, Harvey-Liu-Zhu 2016, McLean-Pontiff 2016), which the note only cites. Weight accordingly.
- Not rechecked here (cited only): Moskowitz-Ooi-Pedersen 2012, Hurst-Ooi-Pedersen 2017 (a century of trend, positive), Cederburg et al. 2020. A fair prior is "trend positive in futures over long samples, weak and unproven for a handful of equity/bond ETFs".

### 3.2 The "veto-only guard" source

The plan cites arXiv 2610.24289 (Wang and Saxena, "Unbounded Losses, Unchecked Inputs") for "strategy code may only veto, never widen". I read it. It is a **measurement study of 26 LLM options-trading agents**; "A1: the LLM can only veto" is one autonomy category of its codebook, and the example (Theta Desk, 1 star) places no real orders. The paper does not test or prove that veto-only is sufficient. What it does support: pre-trade limits must sit outside the model and be unclearable by the agent (C8), 20 of 22 order-capable systems check no inputs (market-data cross-check, source authentication), and even a human approval step can be a routine ticket. Treat the rule as sound design practice, not as evidence-backed. The plan's second-source price check (ADR 0024) is the right answer to the paper's C2 finding.

## 4. IBKR facts (PR #442 plan, PR #443 docs)

Read 2026-11-25: interactivebrokers.ie commissions page, Germany table verbatim (the Poland..UK tail only summarised).
- Tiered: 0.05 % of trade value, **min EUR 1.25, max EUR 29**, plus exchange, clearing and regulatory fees. **[verified]**; the plan's 50 bp at 250 EUR omits the third-party fees, so it is a floor.
- Fixed SmartRouting 0.05 %, min EUR 3.00; Fixed GETTEX direct 0.05 %, min EUR 3.00; other direct 0.10 % min EUR 4 or 0.12 % min EUR 6. **[verified]**
- **New constraint:** "directed API orders cannot use the Tiered fee structure; SmartRouted API orders can use either". A bot with direct routing pays Fixed (min EUR 3 to 6). **[corrected]** in plan section 8.
- "No fractional shares via the API" is **not** on this page (the Germany rows list fractional rates for both plans). Still unverified, and the plan builds on it. **[open]**
- PR #443 setup claims (offline IB Gateway versus IBC, weekly 2FA after Sunday 01:00 ET, daily restart, `ReadOnlyApi` key name) are already marked unverified in that doc; none was independently checked here. The read-only design (allow-list, `readonly=True`, no order method, a test scanning for order names, paper/live account guard) is consistent with the repo's other brokers; a client-side `readonly=True` flag plus Gateway's Read-Only API tick is defence in depth, but only the Gateway setting is enforced outside Quantfolio's own process.

## 5. German tax (secondary sources only)

- JStG 2024: § 20 Abs. 6 Satz 5 (Termingeschäfte, 20,000 EUR) and Satz 6 abolished, for all open cases (§ 52 Abs. 28 Satz 25, 26 EStG per EY and Der Betrieb), banks may implement from 1 Jan 2026 (Finanzausschuss report per EY). **[verified at secondary level]** Satz 4 (share losses against share gains) is not listed as changed. Correction: the sources describe Satz 6 as the cap on **Forderungsausfälle**, not "worthless assets" as the plan wrote **[corrected]**. The sources disagree on the first affected year (2020 vs 2021) and no BGBl citation or promulgation date was found; the October 2024 articles pre-date enactment. **[open]** read the final BGBl text.
- BMF on warrants and certificates: the 2021 article (4 June 2021, PDF dated 3 June 2021) quotes "Zertifikate und Optionsscheine gehören nicht zu den Termingeschäften"; the current consolidated letter is BMF 19 May 2022, IV C 1 - S 2252/19/10003 :009 (BStBl I 2022, 742), whose Randziffern were paywalled. **[corrected]** reference; text not verified. The article itself is inconsistent on whether a worthless expiry hits a 20,000 EUR cap. BFH 8 Dec 2021, I R 24/19: GmbH, § 15(4) EStG; extension to private investors is inference.
- Not covered by the plan, **[open]**: the proposed basket includes bonds and gold. Bond funds get no Teilfreistellung, and a gold ETC is not a fund at all, so the 30 % equity-fund rate in the replay would not apply to those sleeves; the tax-drag input must be per asset class. No source was read on this.
- Teilfreistellung 30 / 15 / 60 % and the omission of 80 % for foreign real-estate funds: statutory knowledge, not re-read.

## 6. Smaller issues

- Plan 4: "7 one-way units a year" is a turnover assumption from Quantpedia; with a 12-month SMA switch on ETFs it varies widely, so the 57 bp break-even is illustrative.
- ADR 0029 item 3 and plan 2 quote "25 years" without stating one-sided testing of Sharpe greater than zero; a verdict on "does not beat buy-and-hold" needs the excess Sharpe, which is lower still.
- The cost cap in plan 7 (order refused if cost over 25 % of break-even, i.e. about 14 bp) will refuse every IBKR order at the 100 EUR tuition cap (125 bp). That is consistent with "IBKR is research only" but means the IBKR order path can never be exercised under its own limits.

## 7. What changed on the branch

`eprocess.py`: docstring scope, `n_clipped`, `clipped_fraction`. `gate_math.py`: `alt_se` option. Tests: clipped-null demonstration, alt-SE. Plan and literature note: Faber figures, Jacquier 30 %, power statement, IBKR API fee rule, tax references. 13 tests pass (`pytest --noconftest -o addopts=""`; the full environment is not installed here). Nothing merged.

## 8. Sources read this session

arXiv 2611.03384, 2501.03938, 2607.01550, 2607.19497 (partial pages), 2607.20093, 2610.24289 (full text via alphaXiv); IBKR Ireland commissions page; Huang et al. (core.ac.uk preprint), Alpha Architect and CXO Advisory summaries; CMS, Der Betrieb, EY (JStG 2024); Private Banking Magazin, PwC (knock-outs), Haufe (BMF table of contents only).

## 9. Round 2 (2026-11-25, later)

Four further read-through passes (trend and timing evidence, statistics, costs and retail, German tax and IBKR), about 75 distinct arXiv papers read in full via alphaXiv plus web pages read through fetch workers. Individual verdicts are in the passes' notes; the points that change the plan:

**Contradicting or qualifying evidence on trend and timing.** Huang, Li, Wang, Zhou (JFE 2020) is now in ADR 0029 context item 2: 8 of 55 assets significant at 10 %, 45 of 55 negative out-of-sample R^2, pooled t of 4.34 against bootstrap critical values of 4.8 to 12.5. Small-ETF-universe preprints (2610.36491, 2611.05310, 2610.27751, 2605.20636) show tactical rules not beating the passive benchmark on Sharpe. Defensible prior: **drawdown reduction, not alpha.** Moskowitz-Ooi-Pedersen, Zakamulin and Cederburg et al. were **not** read.

**Statistics.**
- Time to cross a funded e-process boundary also carries a ln(1/mass) term: a candidate funded with 10 % of the budget needs more evidence than the plan's examples assume.
- Deflated Sharpe and PBO should run on excess returns, with the nominal number of trials.
- Use a strict walk-forward for the final gate; CSCV mixes future into past.
- Gaussian drawdown Monte Carlo is a lower bound: non-Gaussian multipliers on the 90th-percentile drawdown 1.06 to 1.34, Sharpe uncertainty 1.21 (Landolfi 2608.00127). Added to the plan.
- Not verified from primary sources: Holm versus Bonferroni wording, the fractional-Kelly rule, the driftless sqrt(pi/2) drawdown constant.

**Fractional shares (closed).** IBKR's own API pages say the APIs do not support fractional or cash-quantity trading except crypto and FX; error 10243 reads "Fractional-sized order cannot be placed via API". Page dates not shown. The plan's "no fractional shares via the API" is confirmed. Whether Irish accounts qualify for GUI fractions is unverified.

**ETF tax note (added to plan s7).** Statute text for section 20 InvStG read on gesetze-im-internet.de: 30 / 15 / 60 / 80 %. Bond funds below the quotas get no Teilfreistellung (inferred). Gold ETC: Xetra-Gold-type delivery claims tax-free after one year (BFH VIII R 4/15, secondary report); other ETCs taxed under Abgeltungsteuer; individual ETC status unverified. Basiszins 2.53 % (2025) and 3.20 % (2026, single source). Replay tax drag must be set per sleeve.

**Still open.** No primary BMF or BGBl text read; first affected year of the JStG 2024 change (2020 or 2021) unresolved. IBKR Ireland German withholding unknown. Treat all tax lines as estimates and have a tax adviser confirm before real money.

## 10. Round 3 (2026-11-25)

**Read in full or in part (reader limits noted).**
- Moskowitz, Ooi, Pedersen (JFE 2012), Yale-hosted PDF, first ~40 % verbatim, the rest a model summary: 58 instruments 1965-2009, main results 1985-2009; diversified TSMOM Sharpe "greater than one", about 2.5 times the equity market; positions scaled to 40 % ex ante volatility; **no transaction-cost analysis in the part read.** The authors say the result is not necessarily a rejection of efficiency. A 52-of-58 significance count is unverified. The paper is a diversified futures portfolio, not a basket of equity ETFs: the Sharpe above one is a futures-universe number and does not transfer.
- Cederburg, O'Doherty, Wang, Yan (JFE 2020), first ~40 % verbatim: volatility-managed versions beat the original on Sharpe in only 53 of 103 equity strategies (8 significant for, 4 against); momentum is the exception (9 of 9). Real-time combinations lowered certainty-equivalent return in 72 of 103 cases; the market example is Sharpe 0.42 managed versus 0.46 unmanaged. Required leverage often exceeds 400 %. Implication: a volatility-targeted overlay on a basket of ordinary assets has no support in this paper; only momentum-type scaling does. Transaction costs not seen in the read part.
- Valeyre (2504.10914), read in part: single-EMA trend on a 70-instrument futures universe, gross Sharpe 1.24 over 1990-2023 (gross; "costs very small at that frequency" is for futures). A 70-instrument cross-asset universe is far from 5 to 10 ETFs. Valeyre also notes Moskowitz et al.'s Table 2 peaks at 9 to 12 months.
- Columbia replication of Zarattini-Antonacci industry trend (2412.14361), read in full: parameter optimisation and extra filters did not generalise; the strict walk-forward result fell short of the market benchmark. A student project, but it shows the same drift as the plan's own warning.
- Nikolopoulos (2604.15531), read in part: in-sample winner statistic grows like sqrt(log K_eff); a walk-forward evaluation stays null-like; detection power collapses for rare signals (4.8 % at phi 0.05, theta 3). Supports the plan's strict walk-forward and effective-trials stance.

**Not read.** Zakamulin (the fetch returned a different paper, Lim, Wang, Yao on individual stocks, so Zakamulin and Giner is still **unread**). Neither JFE paper's second half or cost sections were read verbatim.

**Trading 212 costs (for the cost sections).** From Trading 212's own help page (undated) and a 2021 community announcement: commission free; FX fee **0.15 %** when the instrument's currency differs from the account's; stamp duty reserve tax 0.5 % on LSE share purchases (none on ETFs); French FTT 0.4 % on large French shares; SEC and FINRA sale fees. The plan said 0.5 % FX; that figure was not confirmed and the plan row now carries both with a check-your-account note. Not confirmed: the fee for a EUR account buying EUR-listed ETFs (inferred zero), the EU-entity version of the page, API order limits, any German tax reporting by Trading 212. The official fee page could not be fetched.
