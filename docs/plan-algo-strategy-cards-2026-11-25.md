# Draft strategy cards for the slow ETF family (ADR 0029)

**Status:** draft, nothing frozen, no hash computed. Written 2026-11-25 from the Quantpedia review
(`docs/research/2026-11-25-quantpedia-review.md`). A card becomes a registered candidate only when the owner approves it,
the universe ISINs are filled in, and the card hash is stored before the first scored day (plan section 5.1).

Common fields for every card below:

| Field | Value |
|---|---|
| Family | `slow_etf` (one trial family; all arms below count as trials of it via `record_trial`) |
| Venue and cost model | decided per card at registration by cost: the shadow cost model uses the cheaper of the eligible venues at the real order size; IBKR tiered minimum 1.25 EUR is the pessimistic case |
| Tax model | `estimate_tax_drag` (Abgeltungsteuer on realised gains, Teilfreistellung per fund class, Vorabpauschale), simplification documented |
| Timing | signal at the month-end close t, fill at the close of t+1; no intramonth rebalancing |
| Benchmarks | (1) equal-weight buy-and-hold of the same basket, (2) MSCI World EUR (`EUNL.DE`) |
| Score | daily net excess return versus benchmark (2) for the alpha verdict; see the verdict class on each card |
| Start date | the first month-end after the owner's approval |
| Parameters | all frozen at registration; a change is a new candidate with fresh mass |
| Universe | 4-6 EUR-listed UCITS ETFs by asset class (world equity `EUNL.DE`, aggregate or government bond, gold, listed real estate, optionally emerging markets), ISINs to be filled in by the owner; plus a cash proxy (money-market UCITS ETF or cash) |

## Card A: `sma10_switch` (verdict class: risk overlay)

- Signal: sleeve held when its month-end close is above its 10-month simple moving average, else the cash proxy. Equal weights across sleeves.
- Source: Faber, "A Quantitative Approach to Tactical Asset Allocation" (SSRN 962461); Quantpedia #0001 reports 1973-2008 11.27 % a year, volatility 6.87 %, max drawdown -29.43 %, Sharpe 1.06, costs excluded [Quantpedia, read].
- Independent evidence: Zakamulin (SSRN 2677212, 155 years): "no statistically significant evidence that market timing strategies outperformed the market in the second half of our sample", better in bear states [read, abstract]. Huang, Li, Wang, Zhou (JFE 2020): little evidence of time series momentum in or out of sample [read, abstract].
- Expected behaviour (prior, not measured): no reliable excess return; the possible value is a smaller drawdown. Therefore declared as a **risk overlay** (see ADR 0029 Amendment 1), with these pre-declared limits:
  - Required: maximum drawdown at least 25 % smaller than the equal-weight buy-and-hold basket, and CVaR(5 %) of monthly returns at least 20 % smaller, paired block bootstrap, over the scored period including at least two drawdowns of 10 % or more in the benchmark (ADR 0029 Amendment 1, rule 3).
  - Cap on trailing: cumulative net return may trail the basket buy-and-hold by at most 2.0 percentage points a year after costs and tax drag.
  - Cost cap: modelled cost and tax drag at most 0.5 % of sleeve value in any rolling 12 months.
- Turnover expectation: 3-4 round trips a year per Faber, in total across the basket; each order is at least the venue minimum.
- Kill: cost cap breached, ledger chain broken, or owner veto.

## Card B: `tsmom12_volscale` (verdict class: risk overlay)

- Signal: per sleeve, weight 0 to 1 from the sign of the 12-month return, scaled to a 10 % volatility target estimated on 60 trading days, monthly.
- Source: Moskowitz, Ooi, Pedersen (2012) as cited in plan section 4; evidence tags in `docs/research/2026-11-25-algo-trading-literature.md`.
- Note on the source: Moskowitz, Ooi, Pedersen test 58 futures, long and short, gross of costs [Quantpedia #0118, read]; this card is long-only on 4-6 UCITS ETFs, so that evidence does not transfer directly.
- Counter-evidence: Huang et al. (above): performance "virtually the same" as a strategy based on the historical sample mean [read, abstract]. Cederburg et al. (JFE 2020, 103 strategies): volatility-managed portfolios do not systematically beat unmanaged ones; out-of-sample versions "generally earn lower certainty equivalent returns and Sharpe ratios" [read, abstract]. The vol-scaling part therefore has negative out-of-sample prior; this card exists to measure it, not because it is expected to win.
- Limits as Card A, except: trailing cap 3.0 percentage points a year (more turnover from rescaling), cost cap 0.7 %.

## Card C: `sma10_switch_bond` (verdict class: risk overlay)

- As Card A with an aggregate-bond UCITS ETF as the out-of-market leg instead of cash. Counted as a separate arm, so a separate trial.
- Tied to the paired-switching idea in Quantpedia (Maewal and Bock, SSRN 1917044, 1991-2011, VFINX vs VUSTX: 11.3 % a year, volatility 9.3 %, max drawdown -18.41 %, costs excluded) [Quantpedia, read]; same family, no separate evidence.

## Not carded, and why

| Idea | Why not |
|---|---|
| Halloween / sell in May | Quantpedia rates the anomaly "Weak" and states its own out-of-sample back-test is "significantly negative" and the result may be data mined [Quantpedia, read]. Bouman and Jacobsen found it in 36 of 37 markets in sample [read, abstract]; no post-publication test in that abstract. Watch item only. |
| Turn of the Month | 12 round trips a year; the venue minimum alone is about 3 % a year of a 1,000 EUR sleeve, more than the effect. Keep as a paper null if wanted. |
| Pre-FOMC drift | "Essentially disappeared after 2015" (Kurov, Wolfe, Gilbert, 2021) [read, abstract]. |
| Active dual momentum, weekly (Quantpedia blog, 22 May 2026) | Weekly turnover, US-listed ETFs, in-sample. Same family; not separate. |
| Relative-strength rotation (top 3 of 5 by 12-month return, Quantpedia #0002) | Reported max drawdown -47.77 %, Sharpe 0.78, costs excluded [Quantpedia, read]; not a risk overlay, so it would need the alpha gate. Same trial family; not carded. |
| Oil-price timing of equities (Quantpedia #0096) | Sample ends 2003; authors flag look-ahead; no post-2003 test read. Possible overlay later after reading the follow-up papers. |
| Overnight anomaly with sentiment filters | Daily round trips; 2018-2021 sample. |
| Quarterly tranched rebalancing (Quantpedia Pragmatic Asset Allocation, own research) | Idea only: rebalance a pre-selected slice each quarter to lower the order count. The family is capped at three arms, so it would replace an arm, not add one. Its tax rationale (US 12-month holding rule) is not imported. |
| Factor ETF tilts | A holding decision for the monthly plan, not an algorithm (plan section 4). |

## Power, stated on the cards

At the plan's committed cost and scale no arm is expected to cross the e-process boundary for a Sharpe-0.4 edge in under a decade.
For a risk-overlay verdict the corresponding statement is: the drawdown test has too few independent drawdowns in a few years to be
conclusive; the verdict is "descriptive, within limits" until the ledger has seen at least two benchmark drawdowns of 10 % or more.
