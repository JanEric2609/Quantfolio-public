# Review of the CFD desk against the pasted research (2026-11-25)

Companion to the ADR 0027 amendment of the same date. Claims in the owner's pasted reports were checked against
primary pages where the network allowed. "Verified" means a page was read in full or in its readable part on
2026-11-25; everything else is marked.

## Verified
| Claim | Result | Source |
|---|---|---|
| Fed range 3.75-4.00 %, 10-2 hold on 28 Oct, Cook and Hammack dissenting for a hike | Verified | federalreserve.gov press release monetary20261028a |
| Predictor of the last half hour is the rest-of-day return, previous close to close - 30 min | Verified, eq. (2) | Baltussen, Da, Lammers, Martens, JFE 142 (2021), academicweb.nd.edu/~zda/intramom.pdf |
| 17 equity futures (6 North America, 8 Europe, 3 Asia-Pacific), Dec 1974 - May 2020, DAX and Euro Stoxx 50 in Table 1 | Verified | same |
| Asset-class Sharpe ratios 0.87 to 1.73 | Verified (introduction) | same |
| Pooled equity R2 out of sample 2.88 % (rest of day), -1.71 % (overnight plus first half hour); 14 of 17 contracts significant | Verified (Table 2 panel A, Table B1) | same |
| The next three days reverse the last half hour | Verified qualitatively; coefficients not read | same |

## Contradicted
- **"Trading 212 index CFD swap -6.4 % APR."** BrokerChooser's table (updated 2026-11-24) lists Trading 212's S&P 500
  and EU 50 financing as not available. The -6.4 % belongs to eToro (EU 50 long) and Oanda (S&P 500 long). The
  owner's own app reading on 2026-11-23 was 0.0083 % a night (about 3 % a year), which the desk already uses.
  Same-day exits before the 22:00 GMT cutoff pay nothing either way.

## Not verified
- Net-gamma split (beta 6.63 against 0.82, R2 3.58 %): Table 7 was beyond the readable part of the PDF.
- Reversal coefficients (-14.51, -29.05, -27.98) and any after-cost result: not in the readable part.
- VSTOXX: no current level on stoxx.com; the cached Investing.com page shows 20.60 dated 28/03 without a year.
  VDAX shows 23.82 at 17:30 CET on stoxx.com with no calendar date. The report's 20.92/21.89 and 26.02/25.23
  could not be confirmed or refuted.
- Constituent overlap percentages, ETF volatility figures, FESX/FDAX volumes, Xetra auction statistics, the
  14:30 and 20:00 release calendar: not checked.

## Quantpedia and QuantConnect
- Quantpedia's own pages were not reachable in a form that could be read; the digest post (June 2024) lists
  #1014 Cross-Market Intraday Time-Series Momentum (Xu et al., SSRN 4651331, indicative 20 %) and #1016 Intraday
  Momentum Strategy for S&P500 ETF (Zarattini, Aziz, Barbon, SSRN 4824172, indicative 19.6 %, Sharpe 1.33 net of
  costs per its abstract). Both are in-paper results, not independent. Each adopted idea counts as a trial.
- QuantConnect's Gao et al. recreation (SPY, IWM, IYR after the universe was narrowed to raise the backtest)
  shows a Sharpe of -0.628 for 2015 to August 2020, no fees modelled, and is marked "pending review".
  That agrees with the desk's own replay: the first-half-hour rule did not hold in 2016-2026.

## Further literature read (abstract page or readable part of the paper; partial)
- Rosa (2022), Journal of Futures Markets 42(12): predictability of the last half hour from the overnight return
  "disappears in the out-of-sample period"; a two-regime model finds a thresholded strategy beats an always-on one.
  That supports a volatility or signal-strength filter as a question, and argues against expecting the
  unconditional rule to work.
- Gao, Han, Li, Zhou (draft Dec 2015): SPY 1993-2013, r1 predicts the last half hour, out-of-sample R2 1.2 %;
  the effect is weaker without the 2007-2009 crisis days (in-sample R2 0.8 %); robustness on FTSE 100 and Euro Stoxx
  50 futures 2003-2015 and no effect in currencies or commodities. (Transaction-cost and post-decimalisation
  figures come from the tool's summary of the PDF, not read verbatim.)
- Zarattini, Aziz, Barbon (SSRN 4824172): a different strategy (time-of-day noise-area breakout on SPY with
  VWAP/band exits), not a last-half-hour predictor; 19.6 % a year, Sharpe 1.33 net of commission and slippage,
  2007-2024. Author-run, not independently replicated; costs modelled for a US ETF, not a CFD.
- Quantpedia's public pages (home and brokerage-API list) are readable without login; the free strategy list
  has four strategies, none intraday. The broker list names no EU CFD broker with an order API except IG's UK site.

## Second pass: what a further 64 pages say (all read 2026-11-25; PDFs were often only half-readable, noted)

**Regulation and retail outcomes**
- ESMA's 2018 measures (press release, FAQ, notice of decisions): 20:1 for major indices (initial margin 5 %), 50 % margin
  close-out per account, negative balance protection, standardised risk warning; the 2018 loss share was 74-89 % of
  retail accounts. The 2018 texts are temporary 3-month decisions; BaFin's page says its ban took effect on expiry of the
  EU decision. Whether each measure is in force today is not stated on any of these pages: the Saturday list checks the
  current regulation. The card's 1:20 matches.
- Current loss shares are broker self-disclosures: 71.0 % average over 49 brokers, range 51 % (eToro) to 81 % (BrokerRank,
  2026); 70.0 % mean over 14 FCA brokers (April 2026); Finder gives 51-77 %. ESMA's 2025 cross-border report: about 1.3
  million EU retail CFD clients, 6.7 % of reported holdings, 23 % of product-specific complaints. No source gives an
  index-CFD-only loss rate.

**Trading 212 costs**
- Help centre (read): commission free; 0.5 % FX fee on the result when the instrument currency differs from the account's;
  overnight = quantity x price x the instrument's long or short rate, charged at 22:00 GMT Monday to Thursday, weekend
  positions at 22:00 GMT on Sunday; rates are per instrument in the app. Its disclosure PDF's index example (USA Tech 100):
  long -0.02312 % a day, short 0 %, spread 1.25 pips. No public page gives the DAX or Euro Stoxx 50 rate or spread.
- Peers for scale: IG charges (3 % +/- SOFR)/360 a day on indices, Pepperstone (2.5 % +/- benchmark)/360 with US500 spread
  0.4; both much higher than the owner's 0.0083 % a night reading (3 % a year) only if the rate there is a benchmark-plus-mark-up.

**Xetra** (Deutsche Boerse trading parameters, Dec 2025, and the Trade-at-Close factsheet, July 2026): DAX closing auction call
phase from 17:30, price determination 17:35, Trade-at-Close to 17:40, with a random end of the call phase. The desk's exit at
17:27 is before the call phase. No page gives the closing auction's share of daily volume. 360X now quotes DAX 40 stocks
around the clock on request, leaving Xetra's hours unchanged.

**Index closing-momentum evidence, with decay and costs**
- Baltussen (JFE 2021) is the strongest and most conditional (see above). Li, Sakkas, Urquhart (thesis 2021 and the Nottingham
  draft): first-half-hour predicts last half hour in 12 of 16 developed markets, pooled slope 2.68 (t 7.53), crisis 3.71 against
  2.09, positive out-of-sample R2 in 11 of 16; stronger when liquidity is low and volatility high; costs not discussed.
- Counter-evidence: Rosa (2022) out-of-sample disappearance; Zhang and Hua (Risks 2024, SPY and QQQ 2007-2018) "overnight
  effects and intraday momentum have diminished over time"; QuantConnect's recreation negative 2015-2020; a Stanford
  2017 student test on stocks found no effect; Quantpedia's own overnight-effect post says it is "slowly losing its strength".
- Mechanism papers are in-sample: Tuzun (Fed 2013): leveraged-ETF flow is 2-3 % of last-hour volume in large caps;
  Barbon et al. (2021): a one-SD LETF rebalancing move lifts end-of-day returns by about four times the average
  last-half-hour return, more than 80 % of the dealer part reverses at the next open, Sharpe about 3-5 in sample;
  Lenkey's 2024 survey says most of these papers have "potentially serious methodological errors" and the economic
  effect "appears to be insignificant". Xetra evidence (Serikova 2019): auction price impact and infrequent rebalancing,
  1.89 bp a day, not cost-adjusted.
- Costs kill the neighbouring effects: Boyarchenko et al. (NY Fed 917) overnight drift pre-cost Sharpe 1.1 becomes -0.5 after
  spreads; Baltussen, Da, Soebhag (Sept 2026) end-of-day reversal 3.55 bp a day value-weighted, "may be unexploitable
  after costs". Lou, Polk, Skouras (FMG DP961, 2026): intraday returns mean-revert to smoothed overnight returns (t -3.98).
- None of the pages reports an after-cost, out-of-sample result for the last half hour of an index traded as a retail CFD.

**Overfitting guard** (Bailey, Lopez de Prado and co-authors): five years of daily data allow no more than about 45
variations of a strategy; a Sharpe of 2.5 over five years loses 95 % confidence after 89 independent trials; "as few as
three independent trials suffice to produce an investment strategy that is likely false". The desk already counts every
cell as a trial, and the six-test predictor replay is Holm-adjusted for that reason. A PBO above 0.05 is the customary
reject level.

**Quantpedia, thoroughly:** its free strategy list has four entries, none intraday. Intraday items reachable from public
posts: #564 Intraday Momentum in Fixed Income (bonds, 1974-2020), #1014 Cross-Market Intraday Time-Series Momentum
(1993-2013), #1016 Intraday Momentum Strategy for S&P500 ETF (2007-2024, author-run), the Lunch Effect post (SPY 2010-2024,
author says the overnight spread is no longer significant), and a Monday-close/Tuesday-open reversal on HYG. The strategy
screener behind the paywall was not searched. Nothing there adds a closing-time rule with after-cost evidence.

## What this changes
The desk's registered predictor is the one the Baltussen table scores worse out of sample. The rest-of-day
predictor and the volatility split are the open research question (replay added, run pending). The paper's own
edge is conditional on dealers being short gamma, which cannot be read from free data.

## Third pass: Baltussen records, Fed and exchange pages (14 more read; 3 more failed)
- Baltussen, Da, Lammers, Martens (JFE 142(1), 2021, pp. 377-403): over 60 futures, 1974-2020, the rest-of-day return predicts the
  last 30 minutes and the power "reverts over the next days". The three record pages carry no Sharpe or coefficients (full text paywalled).
- The EFMA 2021 file id-237 is not Baltussen. It is Beckmeyer and Moerke, "End-of-Day Momentum in the Cross-Section and Option
  Hedging" (options 1996-2019): 1 % hedging pressure moves the last half hour by 18.3 bp (t -3.6), Sharpe "above 4" (in sample, before
  costs), and returns "more than reverse" in the next day's first half hour. Supports the mechanism, not a retail after-cost edge.
- Fed (read): Sept 16 2026 statement raised the range by 25 bp to 3.75-4 %, vote 12-0. Oct 27-28 minutes: held, vote 10-2, Cook and Hammack
  wanted a hike; 12-month PCE 3.6 %, core 3.1 %. Next meeting Dec 8-9 (from the summarised half of the minutes only). Relevant: PCE and
  FOMC days remain unfiltered in the desk (see "Not verified").
- Xetra trading parameters as of 16 Nov 2026 confirm closing-auction call phase 17:30 and price 17:35; the 17:40 Trade-at-Close end is unconfirmed (garbled table).
- Saxo UK: index CFD financing +/-3 % (Classic), +/-2 % (VIP); Germany 40 normal spread 1.5 points; "61 % of retail accounts lose money".
  Euromoney 2019: XTB "just under 71 %" lost before the 2018 rules, "just under 65 %" after. ESMA (Oct 2026): 15.5 million EU retail clients
  of cross-border services, none of it CFD-specific. A vendor article (hyperdash) cites 74-89 %, uncited overnight rates: treated as unverified.
- Failed fetches (permanent): Advisor Perspectives, Xetra TAC page, Pepperstone fees. Pages marked partial were read verbatim for roughly the first half only.

## Source count
About 92 pages read across the three passes (14 + 64 + 14); several PDFs were only half-read and are marked. The 100-source target was
not reached. The sandbox's network policy denies direct fetches, and only the fetch tool works, one page at a time. The Saturday list
names the reading that still matters (the paywalled Baltussen tables, the Quantpedia screener, T212's DAX/ES50 rates and the ESMA measures in force today).

## Saturday list (needs the production host or a key this checkout does not have)
1. Run `python scripts/desk_replay.py --dump bars.json`, then `--bars bars.json --predictors` on the app host (stored
   Alpaca key). Read: the rest-of-day predictor against the registered first-half-hour rule, per volatility
   tercile, Holm-adjusted. Save the JSON as `docs/research/desk-predictors-2026-11.json`. Nothing is decided before
   that; a positive result means a new registration, not an edit.
2. Choose and fetch DAX and Euro Stoxx 50 intraday history (the replay covers only SPY and QQQ); then rerun.
3. Apply migration 0160 on prod (the deploy does it) and check `/cfd rule other` on a real row.
4. After a week of signals run `pair_shadow.report(db)` on the prod database for the first shadow pair numbers.
5. Open the Trading 212 app: GER40 and EU50 spread in points at 16:55-17:00 and 17:25, overnight rate in the
   instrument panel, hours, margin and the December expiry; the public pages could not be fetched here.
6. Read Baltussen et al. Table 7 (net gamma) and Section 3.5 (costs) in the PDF; the fetch tool returned only the
   first half. Check VSTOXX and VDAX-NEW on stoxx.com with a date.
7. Decide whether the card's cost line stays at "+5 points" over break-even, and whether a pair cap is wanted once
   the shadow has data.
