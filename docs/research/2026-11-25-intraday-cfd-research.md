# Intraday CFD: research findings and prioritised plan (2026-11-25)

Status: findings and plan only. Nothing built. Everything stays paper-only, brokers read-only.
Evidence tags: [read] = I read the source page; [abstract] = abstract/secondary only; [repo] = our own code or replay.
Limits: the Baltussen paper PDF and the Cambridge CFD-app paper could not be fetched, so no coefficients are quoted from them.

## 1. Short answer
Intraday retail CFD trading has no demonstrated edge after costs, here or in the literature I could reach. Adding more signals will not fix that, it will add trials and lower the odds that anything real is found. The useful work is (a) make the messages simpler, (b) make the algo section visible, (c) test the one untested predictor properly on paper, (d) use the idle PC for research, not for live intraday.

## 2. Why the last 30 minutes "doesn't behave"
- Baltussen, Da, Lammers, Martens (JFE 2021) [abstract]: the return from yesterday's close to the start of the last half hour (rest of day, ROD) predicts the last-half-hour return, on 60+ futures, 1974-2020. Mechanism: market makers short gamma (options, leveraged ETFs) must trade with the move near the close. The effect then reverts over the following days.
- Alpha Architect's summary [read, secondary]: the effect is strong on negative net-gamma days and not significant on positive-gamma days. So an unconditional rule is expected to look unreliable: it only works on the days dealers are short gamma, and we have no free gamma data.
- Our desk does **not** trade that predictor. It trades the Gao-Han-Li-Zhou one (yesterday's close to 09:30 first-half-hour return), with no threshold [repo, gap list]. Different signal, so "the last 30 minutes don't behave" is partly "we test the wrong predictor, unconditionally".
- Our replay: SPY/QQQ 2016-26 gross -0.70 / -0.25 bp, CFD net about -0.18 / -0.13 EUR per trade [repo]. Costs alone are larger than any gross effect. The ROD predictor replay on SPY/QQQ is written (`desk_replay.py --predictors`, volatility terciles, Holm) but not yet run; DAX/EU50 have no intraday history.
- Other plausible reasons the close misbehaves (my reasoning, not sourced): closing-auction order imbalances, index rebalancing and expiry days, month-end flows, wide CFD spreads at the close, and exiting 3 minutes before the uncross on a CFD that tracks futures.

## 3. Shorter-horizon and flexible-exit strategies
The desk's fixed-time exit is not the problem; the lack of gross edge is. Flexible exits (trailing stop, target/stop, volatility exits) change the shape of losses, not the expected value, once costs are fixed. Our own exit-variant replay on 1-minute bars already tested variants [repo] without finding an edge. A 30-60 minute hold costs the same spread as a 27-minute one, so shorter horizons make cost drag larger relative to the move, not smaller. Recommendation: do not build new exit logic; run one pre-registered replay of ROD with a volatility threshold on DAX/EU50/SPY/QQQ, with a flexible-exit arm, and accept the null.

## 4. "Use all signals and indicators"
Every extra signal is a trial. Our own plan counts trials per family and the budget is small. EMA, Bollinger and RSI(2) event cells already measure at -0.16 to -0.49 EUR per trade with t between -2.5 and -6.5 [repo, plan doc]. Stacking indicators on a cost-dominated rule is a known way to overfit. I recommend a small, declared set (ROD, a vol filter, the calendar skip list) instead of "everything".

## 5. Linking to the algo section, and why it is not visible on prod
Found: `lab/algo_lab` contains only `eprocess.py` and `gate_math.py` (library maths). It has no API router, no job registrar, no frontend page and no route in `routeManifest.ts`. The plan doc says "nothing in it is built beyond lab/algo_lab". So it is not a deploy problem: there is nothing to see. The merged work is ADR, plan, strategy cards and maths.
Fix (small): a read-only `/api/algo/overview` listing cards, hashes, verdict classes and the gate maths status, plus a page "Algo" in the sidebar saying plainly "research, paper only, no candidate funded". Then the CFD desk page links to it. I can do this as a PR.

## 6. Idle PC compute
The linked machine reports an Intel Arc 140V integrated GPU (8 GB shared), Core Ultra 7 266V, 16 GB RAM, which looks like a laptop, not a gaming/GPU PC. If you meant another machine, tell me its GPU. Honest use cases:
- Backtests and replays (CPU, parallel, vectorbt/DuckDB): useful, big wins vs the prod LXC.
- Local LLM headline reading: 16 GB shared memory limits this to roughly the size you already run (Qwen 9B decided earlier). Intel Arc works through llama.cpp SYCL/Vulkan or OpenVINO, slower than NVIDIA.
- Live intraday models: no. There is no edge to accelerate; latency is not the constraint for a manual Trading 212 CFD.
Design if wanted: a pull-based worker on the PC that fetches replay jobs from the API, runs them, and posts a result artefact. Outbound-only, no new inbound ports, no secrets on the PC.

## 7. Scraping news and trader forums
- Reddit [read, secondary vendor page, not Reddit's own terms]: the free API tier is for personal non-commercial use at about 100 queries/min with OAuth; no self-serve checkout; the page says new public Data API requests stop 2026-10-31 and access ends March 2027. Verify at redditinc.com before relying on it. Scraping is a separate and riskier question.
- StockTwits [read, own terms, revised 2026-07-10]: forbids scraping, data-mining and "signal service" use by automated means unless through an approved API in writing. So scraping StockTwits is a terms violation. An approved API would need their written permission.
- Value: social sentiment has weak and decaying predictive power, and for a retail trader reading it after it is public, the move is mostly gone. I did not find strong evidence in this session; treat as unproven. Pump/hype risk is also an adverse-selection problem for a retail reader.
- Feasible, legal alternatives: RSS from ECB/Fed/SEC (already in), Alpaca news stream (already in), GDELT, official company disclosures. Latency of 590 s on messages is a pipeline question for the other thread.
- Germany: scraping under copyright/UrhG and database rights, plus contract terms. I am not giving legal advice; terms of service are the binding constraint here.

## 8. Smaller trades with bigger leverage
- ESMA caps retail CFDs at 20:1 on major indices and 5:1 on single stocks, with 50 % margin close-out and negative balance protection [read, Eliner thesis]. You cannot get more than 20:1 on DAX/EU50 as a retail client at Trading 212 (professional status would lift that and remove protections).
- Leverage does not change the edge; it scales gains and losses and the spread cost in proportion to exposure. Cost in bp of notional is the same whatever the margin. The thesis finds leverage lowers trade-level returns by 1.9 bp at 2:1 and by 7.6 bp at 400:1, against unleveraged trades [read]. Authors: leverage is "highly detrimental" to retail wealth.
- "Smaller trades so I can sell quickly" is fine as a position size, but quick exits shorten the hold while the spread stays, so cost per EUR of expected move rises. With a negative expectancy, more trades mean faster ruin. Risk of ruin falls only with smaller risk per trade, not bigger leverage.
- Retail CFD loss rates: 70-80 % of accounts lose money per regulator surveys cited in the same thesis [read].

## 9. Telegram messages
Current open message is 9-11 lines (rule, quantity, stop, typical move, spread, index level, exit, cost line, evidence, disclaimer). Proposal: 4 lines.
```
DAX SELL CFD (T212), enter by 16:57
Qty 0.0123 (about 1,840 EUR, margin 92 EUR)  Stop 118 pts from your fill
Exit 17:27, I will tell you
Edge check: replay says -0.18 EUR/trade
```
Details stay behind a "Details" button or the Desk page. This is a PR-sized change in `decision/desk/cfd.py` plus test updates.

## 10. Prioritised plan
1. **PR now (public mirror):** simplified CFD message; `/api/algo/overview` + Algo page so it shows on prod after your deploy.
2. **Research replay (paper, pre-registered):** ROD predictor with vol threshold on DAX/EU50/SPY/QQQ, flexible-exit arm, run on the PC or prod. Expect null; that is a result.
3. **Pair cap/shadow for DAX+EU50** (gap list item 3) and journal `rule` column.
4. **PC worker** for replays (pull-based). Only after (2) shows a reason.
5. **Forums:** only via approved APIs; low priority.
6. **Stop:** more indicators, higher leverage, live-execution ideas.

Decision for you: should the CFD desk keep sending live signals at all while the registered prior is "every index-rule cell fails"? My recommendation: keep paper scoring, make Telegram messages opt-in on days when ROD vol filter is on, and treat real trades as entertainment budget, not a strategy.
