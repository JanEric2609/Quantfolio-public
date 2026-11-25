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

## What this changes
The desk's registered predictor is the one the Baltussen table scores worse out of sample. The rest-of-day
predictor and the volatility split are the open research question (replay added, run pending). The paper's own
edge is conditional on dealers being short gamma, which cannot be read from free data.

## Source count
Pages read in this pass: 14 (Fed release, the Baltussen PDF, BrokerChooser, two STOXX pages, Investing.com,
the Quantpedia digest post, QuantConnect, Rosa's abstract page, the Gao et al. and Zarattini et al. PDFs, an Alpha
Architect summary, two Quantpedia pages). Two Trading 212 instrument pages could not be fetched. The search tool returned about 50 further URLs that were not opened.
The 100-source target was not reached: the sandbox's network policy denies direct fetches, and only the fetch tool
works, one page at a time. The Saturday list names the reading that still matters.

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
