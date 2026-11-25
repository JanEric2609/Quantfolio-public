# Swing CFD desk, machine learning and the pve host: findings and design (2026-11-25)

Status: research and design only. Nothing built, nothing ordered. Paper only, brokers read-only.
Tags: [read] = I read the page; [abstract] = search result or abstract only; [repo] = our code or replay; [mine] = my reasoning, not sourced.
Not read: the Baltussen, Moskowitz and earnings-premium papers themselves, and the Augustin et al. full text (members only).

## 1. Does holding longer beat the 30-minute rule after costs?
Plainly: **unproven, and not shown by any source I read.** What changes with a longer hold is the cost structure and the risk, not the edge.
- Cost per unit of expected move falls: the spread is paid once for a move that can be several times larger. [mine]
- Overnight financing is small per night but is a cost with no offset. The desk's constant is 0.0083 % a night, about 3 % a year [repo]. Trading 212 charges non-FX CFDs as quantity x price x daily rate, rates change daily and sit in the app's Instrument Details, shorts include borrow cost, long and short are charged separately, charged at 22:00 GMT (21:00 DST); a weekend position is charged on Sunday and the page states no triple charge [read, T212 help centre]. A third-party table lists eToro index longs at -6.4 to -8.3 % a year and Oanda at -4.4 to -6.4 % [read, BrokerChooser, not Trading 212]. Check the live rate per instrument before any real trade.
- The risk changes shape: a stop is not guaranteed over a gap. At 20:1 (ESMA cap on major indices), the 50 % margin close-out [read, Eliner thesis] with a 5 % initial margin means about a 2.5 % adverse move triggers the close-out (about 5 % would wipe the margin); indices have gapped more than that over weekends. [mine, arithmetic]
- Verification gets slower: a swing rule that trades 20 times a year needs about 8 years to reach the 153 trades that detect a 60 % hit rate at 80 % power [repo gap list, my division]. The intraday rule at least produces data quickly, and it shows no edge.

## 2. The anecdotes
"Many people held NVIDIA over the weekend", "many shorted VIX and held to 25": this is survivorship. Winners post; those stopped out or liquidated do not. It also needs the denominator: how many shorted VIX at 20 and were forced out at 30 first. [mine] The right test is a pre-registered rule run on all cases, not a remembered winner.

## 3. Specific cases
- **VIX short after a spike.** VIX mean-reverts in a statistical sense, but the index is not tradable; a CFD or ETP follows futures and pays roll decay in contango. The cautionary evidence: on 5 Feb 2018 short-vol ETPs (XIV, SVXY) lost over 90 % in one day; VIX jumped a record 20 points and XIV fell from 1.9 bn USD to 63 m in one session [read, CFA Institute summary of Augustin, Cheng, Van den Bergen, FAJ 2021; Bloomberg opening text]. Those products rebalance daily, so a hand-held short is different, but the payoff shape is the same: many small wins, one large loss. A VIX short held to 25 was right this time; it says nothing about the path of those who were stopped out. I found no statistics on VIX reversion after spikes in the pages I read.
- **NVIDIA over the weekend / earnings.** Search results point to an earnings announcement premium literature (NBER) [abstract only]. I did not read it, so I make no claim on size or whether it survives costs. A CFD on a single stock is capped at 5:1 [read, thesis] and charges financing plus a gap you cannot stop.
- **Joining a move already underway ("clearly will fall further").** Momentum at one to twelve months exists in the literature but Huang et al. (JFE 2020) find little time-series momentum evidence, and the vol-scaling out-of-sample record is negative [repo plan doc, abstracts]. Intraday returns show momentum alongside short-term reversal, overnight returns show no momentum and long-term reversal [read, Alpha Architect summary of Barardehi et al., US equities 1926-2019, no magnitudes]. News-confirmed momentum: I found no source in this session. "Clearly" is hindsight: on the day, the same chart is one of many that reverse. [mine]

## 4. Machine learning for intraday prediction
- The most relevant paper I read: Mesfin (arXiv 2605.17724, 2026), MNQ 5-minute bars, 944 days, gradient boosting with 29 daily and 30 first-hour features and an LSTM, expanding-window walk-forward, test years 2023-25. Accuracy 50.0 to 50.9 % against a 51.8 % base rate; permutation p-values 0.135 to 0.515; no model beat the base rate; the paper reports no returns or costs; feature importance shifted between folds, read as noise fitting [read]. Single independent author, one instrument.
- Our own plan doc already records 14 intraday families failing on MNQ and our replays failing [repo]. A model adds more degrees of freedom to the same costs.
- If tried, the minimum protocol: label by triple barrier or fixed horizon with the CFD cost deducted; purged and embargoed walk-forward; every model, feature set and seed counted as a trial (`record_trial`); deflated Sharpe; a pre-committed holdout; benchmark against "always flat" and the rule without ML. Expect a null.
- Where ML could earn its place: not predicting direction, but filtering (skip days where the cost is a large share of the typical move) and sizing. Even those gains must beat the trial penalty.

## 5. The pve host (Ryzen 9 5900, 24 threads, 31 GB RAM, RTX 3060 8 GB per the owner)
- Idle capacity is CPU. Walk-forward replays, LightGBM and vectorbt grids run well on 24 threads; GPU adds nothing for trees on this data size.
- The GPU is already shared with the local LLM (LXC 112) if passed through. A second consumer on 8 GB VRAM must wait for the LLM, so ML training should be CPU or scheduled off the news-reading hours. I could not verify the passthrough from here; the owner should run `nvidia-smi` in LXC 112.
- Swap at 71 % on a 31 GB host with 34 % RAM used is a symptom to look at before adding load (check per-LXC memory limits and swappiness). [from the owner's screenshot via the coordinator]
- Design: a pull worker in its own small LXC (or a job inside LXC 111) that fetches a replay spec from the API, runs it, and posts a JSON artefact. Outbound/internal only, no broker credentials, no write path. The deploy is the owner's step.

## 6. Paper design: swing cells (pre-registered, paper only)
Hash-frozen like ADR 0027/0028; every arm counts as a trial; start date after approval; nothing blocks or orders.
1. `swing_trend`: indices DAX, EU50, SPX, NDX. Long/short by the sign of the 20-day return, entry at next open only if the 1-day return agrees ("already moving"); exit by a 3x ATR(14) trailing stop or after 10 days. Risk 0.5 % of the pot per trade (the desk's event cells use 1.8 %, above the usual band [repo]). Financing booked nightly at the instrument's rate; weekend gaps taken at the real open, so stops can be overrun, as in life.
2. `swing_news_flag`: not a filter. Log whether live news confirmed the move, and compare outcomes afterwards. Making it a filter would multiply trials.
3. `vix_fade` (research only): replayable only with VIX futures or an ETP series, not the index. Fixed rule written before looking, loss cap, and a stated expectation that a rare large loss dominates.
4. ML arm: one gradient-boosting filter on arm 1 under the protocol in section 4.
Gates: same as the ladder (skill and harm e-processes, Holm). Costs and the 25 % tax are in the score. Expect "inconclusive" for years; that is the honest outcome.

## 7. Replays to run on prod (read only)
Correction to my earlier summary: the rest-of-day (ROD) predictor replay already exists (`desk_replay.py --predictors`, ADR 0027 amendment: r1 against ROD on SPY/QQQ, volatility terciles, Holm over six tests). It has not been run because it needs the stored Alpaca key. DAX/EU50 have no intraday history in our stores.
```
cd /opt/quantfolio/backend
set -a; . /opt/quantfolio/.env; set +a
python scripts/desk_replay.py --dump /tmp/bars.json > /dev/null   # fetches SPY/QQQ 30-minute bars with the stored key
python scripts/desk_replay.py --bars /tmp/bars.json --predictors
python scripts/swing_replay.py --write                            # swing_trend on SPX, NDX, DAX, ES50 daily bars
```
`swing_replay.py` reads daily bars through `market.history`, writes only `docs/research/swing-replay-2026-11.json` as a result (on a cache miss `market.history` may store price bars in the price cache, as any chart does) and sends no message and calls no broker. The rule and constants are frozen in `lab/swing_replay.py`, with a flipped-side control and a count of stops filled through a gap.

## 8. Recommendation
Keep intraday paper scoring as the measured null. Add the swing cells on paper, because they test the owner's actual intuition without risking money and with lower cost drag. Do not start with real money, VIX shorts or leverage. If real money is ever used, the pot is an entertainment budget with a hard stop, not a strategy.
