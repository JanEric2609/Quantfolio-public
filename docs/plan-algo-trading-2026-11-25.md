# Plan: the algorithmic trading section (2026-11-25)

**Status:** Proposal for the owner. Merged as a proposal; nothing in it is built beyond `lab/algo_lab` (research only). Decision record: [ADR 0029](adr/0029-algorithmic-trading-section.md).
Evidence: `docs/research/2026-11-25-algo-trading-literature.md` (every claim there is tagged read / page / abstract / cited).
Standing rules kept: brokers read-only, desks paper-only, no order path, the code and docs here are mirrored like any other, with no private data.

## 1. The short version

1. **Intraday and event trading will not be the product.** Our own replays and the outside literature agree. The new section should
   not try to rescue them with cleverness.
2. **The product is a validated, cost-honest, auditable way to run slow rules on paper, then through the ladder with tiny money.**
   The first candidates are monthly, EUR-listed UCITS ETF rules (moving-average switch, volatility-scaled time-series momentum).
   They are expected to be **inconclusive** against buy-and-hold for years. That is a result, not a failure.
3. **The statistical limit is the binding constraint, not the broker.** A Sharpe of 0.5 needs about 25 years to detect (38 with four
   candidates). We cannot prove skill. We can: refuse to promote what does not clear a funded, cost-aware, anytime-valid gate;
   measure drawdown and cost behaviour; and learn with a capped budget.
4. **IBKR is worth wiring for research reasons** (paper account, API, data, a venue for later). It is a poor venue at 250 EUR:
   a 1.25 EUR minimum is 50 bp per order, 250 bp on a 50 EUR ETF slice. At this size the cheap venue is a zero-commission route.
5. **Build tools first.** Two are prototyped on this branch (`lab/algo_lab`: funded promotion ledger, gate maths). Four more are specified.

## 2. What the evidence means for a small account

| Question | Answer | Basis |
|---|---|---|
| Does intraday momentum pay after costs? | No: 14 families fail on MNQ, ours fail on SPY/QQQ, CFDs lose, knock-outs lose heavily | Mesfin 2026; ADR 0027/0028 replays |
| Do retail day traders improve? | No evidence of learning; 97 % of persistent traders lose | Chague et al. 2020 |
| Do popular chart rules work? | Refuted or inconclusive, none supported, EU ETF replication negative | Darmanin 2026 |
| Does anything survive? | Slow trend (months), Sharpe about 0.4 +-0.26 post-2009 in a 100-futures portfolio; net about 0.5 for a replicated CTA | Kurth et al. 2026; Sepp, Lucic 2026 |
| Does it work in our instruments? | Unknown. Equity indices and FX are the small-tick markets where trend broke; a 5-ETF basket is a different object | Kurth et al. 2026 |
| Does vol-managed sizing help? | In-sample yes, out-of-sample generally no | Cederburg et al. 2020 |
| Can a small edge be verified? | 0.5 Sharpe: about 25 years; 1.0: 6 years; 1.5: 2.7 years (one-sided, 80 % power, alpha 0.05, i.i.d. approximation; with the larger Sharpe standard error under the alternative, `alt_se=True`, frequency-matched for daily data: 24.7, 6.2 and 2.8 years, under 1 % longer) | `years_to_detect` |
| How much of a backtest Sharpe survives? | One fitted signal, true Sharpe 0.5, 5 years of training: about 56 %; 10 years 72 %; many signals much less | Jacquier et al. 2025, `oos_replication_ratio` |

Consequences for design:

- **Priors beat tests.** With so little power, a candidate must come with an economic reason fixed in advance (diversification,
  drawdown control) and a verdict rule that can say "inconclusive".
- **Few candidates.** Each extra candidate costs evidence (budget split, Holm). Three at most per family, registered together.
- **Costs and tax enter the score**, not a footnote.
- **Survival is a design input.** With zero edge and 15 % volatility, the chance of a 20 % drawdown within five years is about 92 %,
  and of 10 % about 100 % (`prob_drawdown_breach`, 20,000 daily Gaussian paths; fat tails make it worse). A halt level is therefore
  a **budget expiry**, not a protection that rarely fires.

## 3. What Quantfolio already has, and what each piece does in the new section

| Existing | Role in the algo section |
|---|---|
| ADR 0018 pre-registration, hash-frozen specs, calendar-time ledger | Strategy card + hash, the format of every candidate |
| ADR 0025 ladder (stages 0-3, harm e-process, 126 days, 24-month expiry, `ladder_learning_eur_per_month` 20 EUR) | The only route to money; algo candidates are new ladder candidates |
| `quant_metrics`: deflated Sharpe, PBO/CSCV (`probability_of_backtest_overfitting`), Holm, `effective_n_trials`, `record_trial`, DM test, Mincer-Zarnowitz, `kelly_fraction` | Secondary, descriptive evidence; trial counting per family |
| `lab/quant_lab`: engine, `CostModel`, `estimate_tax_drag`, purged/embargo splits | The cost- and tax-aware replay of slow strategies |
| `lab/forward_lab` (nightly forward test, robustness) | Scoring of published signals before the outcome; same pattern for candidate rules |
| `lab/desk_replay.py`, `signal_replay.py` | The model for a replay with a committed JSON artefact and a research-only label |
| `decision/desk` (limits as code constants, hash-chained `desk_events`, bankroll guard, halts, Telegram taps) | Pattern for limits, ledger, kill switches, owner confirmation |
| `decision/paper_portfolio.py` (paper runs in EUR, benchmark in MSCI World EUR) | Paper book for algo sleeves; benchmark already defined |
| `decision/trade_check.py` (does the shown edge cover fee, spread, tax; shadow) | The pre-trade cost gate, extended to algo orders |
| `foundation/eur_prices.py`, `market.py`, `ecb_fx.py`, `live_positions.py`, `book_performance.py` | EUR price history, FX, real-book reconciliation, performance |
| `foundation/control_center.py`, `self_check.py`, Telegram | Needs-attention list, daily check, alerts |
| `foundation/broker_instruments.py`, `trading212/` (read-only), `scalable/` | Venue facts per ISIN; Trading 212 is the cheap venue for slow ETF rotation |
| Other thread: IBKR integration (read-only first) | Positions, cash, paper-account fills, data; **no orders** |

## 3b. Added after round 2 research

- **Whole shares only at IBKR.** With no fractional orders through the API, a 250 EUR pot buys whole units of EUR UCITS ETFs: position granularity (one unit of a 100 EUR ETF is 40 % of the pot) is a design constraint, rebalancing becomes lumpy, and the cost model must round to whole units and add the 1.25 EUR minimum per order.
- **Guard rule: strategy code may only veto, never widen.** Strategy and sizing code returns a target; the guard layer (`decision/algo/limits.py`, code constants) takes the minimum of that target and every limit, may reduce or cancel an order, and can never raise size, add a symbol, change side, or loosen a limit. A strategy cannot change limits at run time (source: arXiv 2610.24289 as relayed by the IBKR thread; not read in this thread).
- **Evidence additions.** A pre-committed, bar-independent holdout endpoint and a fail-closed "no certifiable discovery" outcome (Gencay); count trades before trusting a Sharpe (PEP-KO: one out-of-sample trade); the effective trial count is far below the nominal one (100 nominal trials = 10-32 effective); purge or embargo by label horizon plus two to three decay lengths (impossible trinity); paper-to-live gaps of 3 to 19 bp per order and 10 pp median return in the crypto study justify the shadow-fill stage S3; a drawdown halt is set from the Monte Carlo of the assumed Sharpe and horizon, with Sharpe uncertainty adding about 23 % to the 90th-percentile drawdown, and stated in writing together with those assumptions.

## 4. Strategy family triage

"Net-viable at 250 EUR" means: edge before costs larger than the cheapest eligible venue's cost at the order sizes the pot allows.

| Family | Evidence | Net-viable at 250 EUR + 20 EUR/month | Tax (DE) | Verdict |
|---|---|---|---|---|
| Intraday index rule (CFD, knock-out, warrant) | Negative (own replay, Mesfin) | No: 3.3 % of a 60 EUR position in fees per round trip | Cap abolished by JStG 2024; knock-outs are not Termingeschaefte per the BMF letter 2021 (secondary sources) | **Stop investing; keep the paper cell as a measured null** |
| Technical event cells (EMA, Bollinger, RSI(2)) | Replay -0.16 to -0.49 EUR per trade, t -2.5 to -6.5 | No | same | **Measured null (already live on paper)** |
| Hype fade / news drift / owner call | No published edge after costs; LLM news edge "memorised" | No | same | Keep paper, no promotion path |
| **Slow ETF trend/timing (monthly)** | Sharpe 0.4 +-0.26 post-2009 futures; Faber 5-asset SMA10 1973-2012 gross: Sharpe 0.73 vs 0.44 buy-and-hold, return 10.5 % vs 9.9 %, so the gain is mostly lower volatility and drawdown (review 2026-11-25; Quantpedia's 1.06 does not match Faber's own figures); Huang et al. JFE 2020 find little asset-by-asset TSMOM evidence; Zakamulin out-of-sample marginal | Yes on a zero-commission route; **no on IBKR at 50 EUR slices** | Switching realises gains: Abgeltungsteuer, Teilfreistellung 30 % for equity funds, Vorabpauschale; `estimate_tax_drag` models it | **Phase 2 candidate (register 3)** |
| Volatility-scaled exposure on one equity ETF | In-sample alpha, out-of-sample no | Yes (same turnover class) | as above | Register only as arm of the slow family, not separately |
| Cross-sectional momentum / factor ETFs | Real, crash-prone (max drawdown 46 % unlevered in the benchmark); MDE 0.6 | Buy-and-hold factor UCITS ETF is a holding, not an algorithm | Teilfreistellung | Out of scope: allocation decision for the monthly plan |
| European insider buys | CAR(0,180) +6 % event study; not net of cost; threshold unresolved | Small-cap spreads likely eat it | normal | Research only (collector already exists) |
| Option smirk | Decaying (t -1.5 in 2023-26) | n/a | n/a | Research only |
| Pairs / stat arb | PEP-KO failed; capacity and borrow | No | | Not registered |
| ML cross-sectional (pooled ridge/LightGBM) | Existing `pooled_model`, gates nothing before DSR/PBO | Needs a turnover cap | | Stays a research input |
| LLM strategy discovery | 91-paper audit: short windows, unmodelled costs; edge "memorised" | n/a | | Only inside the funded ledger, never as a shortcut |
| Crypto | Not researched | | Holding rules differ | Out of scope |

**First registration (proposed, nothing frozen yet):** family `slow_etf`, basket of 4-6 EUR-listed UCITS ETFs by asset class
(world equity `EUNL.DE`, plus bond, gold, real estate, EM if the owner wants it), three arms:

1. `sma10_switch`: hold the ETF only above its 10-month SMA at the month-end close, else the cash proxy; execute next session.
2. `tsmom12_volscale`: weight 0..1 by 12-month return sign scaled to a 10 % volatility target, monthly.
3. `sma10_switch_bond`: as 1, with an aggregate-bond ETF instead of cash.

Benchmarks fixed in the card: equal-weight buy-and-hold of the same basket and MSCI World EUR. Score = daily net excess return.
Expected turnover about 7 one-way units a year per sleeve (3-4 round trips, Quantpedia), so the break-even cost at a gross Sharpe
of 0.4 and volatility 10 % is 1e4 x 0.4 x 0.10 / 7 = **57 bp per one-way trade**. A zero-commission route with 2-10 bp spread clears
that easily. IBKR's tiered minimum on a 250 EUR position is **50 bp**, on a 50 EUR slice **250 bp**, and fails it.

## 5. Research protocol (what every candidate must pass before it scores a single paper day)

### 5.1 Strategy card (the registered object)

A canonical JSON file, hashed with SHA-256, stored in the repo and as a row: universe (ISINs, listing currency), signal formula,
parameters (all frozen), data fields and vintage, calendar and execution timing (signal at close t, trade at t+1), position
sizing, benchmark, cost model id (venue, schedule, spread assumption), tax model id, verdict class (`alpha` or `risk_overlay`), scale for the score, the clipped-score null (the estimand the e-process guarantee is about, with the verdict of its induced-null audit), trial family, start date.
Changing any field is a **new candidate** with fresh mass. Pattern: `decision/desk/prereg.py`, `prereg_events.EVENT_HASH`.

### 5.2 Replay, before registration (research only)

- Daily bars from `eur_prices`, EUR returns, delisted/merged ETFs treated as in the forward lab. History length: as long as the
  ETFs allow; for the trend rules also the JST long-history harness (`lab/long_history`) to show behaviour across regimes.
- Costs: commission, spread by instrument, FX 0.5 % where a non-EUR listing is used, tax drag via `estimate_tax_drag`
  (documented simplification). A replay is labelled *research, not the pass test*, as for the desk replays.
- Overfitting and selection: purged and embargoed walk-forward folds (`quant_lab/cv.py`), CSCV/PBO and deflated Sharpe across the
  registered family and its trial count, Holm across arms. A **paired block bootstrap** of drawdown and CVaR against the benchmark (the Gaussian drawdown Monte Carlo in `gate_math` is a lower bound: non-Gaussian multipliers on the 90th-percentile drawdown are 1.06 to 1.34 and Sharpe uncertainty 1.21, Landolfi 2608.00127)
  (the question for a timing overlay is risk reduction per unit of cost, not alpha).
- **Induced-null audit** (new tool T3): run the identical pipeline, from signal to costs to scoring, on (a) i.i.d. and regime-
  switching zero-mean returns, (b) a bid-ask-bounce placebo, (c) permuted-date placebos. If the pipeline shows significant
  evidence on a null, it is falsified and fixed before it sees real data. Report the audit's power honestly: it cannot detect
  sparse signals. Use a **blind-parameter** variant: the null parameters are drawn by the audit, not hard-coded.
- **Excess returns only** for any "beats benchmark" criterion. Print per-criterion rejection rates for a planted true signal
  and for noise (Zheng's injected-truth control) so a silently dead gate is visible.
- **Random-admission control:** every gate in the section reports its false-discovery rate against a coin flip at the same adoption
  rate. A gate that does not beat it is only a stricter threshold.

### 5.3 Forward scoring (the pass test)

- Registered candidates are scored daily from the registration date on the paper book and on the **excess net score** of the
  e-process ledger (`lab/algo_lab/eprocess.py`). The ledger is append-only; the minimum row key is candidate hash, event time,
  data-version hash, score, cost, evidence, boundary, decision, first executable session; replays are idempotent.
- Budget: lifetime 1, equal mass per registered candidate, alpha 0.05. With 3 arms the boundary is 1/(0.05 x 0.333) = **60**.
  Adding a fourth later reduces nothing already spent; masses are never recycled.
- Epochs: authorisation expires after 24 months; renewal is a new, funded epoch with mass q_e (for example 6/(pi^2 (e+1)^2)).
- Power is low and stated on the card: simulated with this ledger (3 equal masses, boundary 60, Gaussian daily returns, scale 1 or 4 daily sigma), the **median crossing time is about 56 years at Sharpe 0.4 (only 54 % of paths cross within 60 years), 12-15 years at Sharpe 0.8 and 4-5 years at Sharpe 1.5**. Benhamou's 2,327-session median is for a 15 bp/day (about 14 % a year) edge. The score is excess return over the benchmark, whose information ratio is far below the strategy's own Sharpe, so expect no crossing in practice. A card also states the verdicts that are possible: *crossed*, *inconclusive* (default), *harmed* (separate harm e-process,
  the ladder's H >= 10).

## 6. Promotion gates (paper to money)

```
S0 Research ──register──▶ S1 Registered ──forward paper──▶ S2 Paper-scored
   ▲ replay + null audit       hash frozen, mass funded         daily net excess score, evidence ledger
                                                                      │ (a) e-process crossed, or (b) owner opts in as learning expense
                                                                      ▼
                                                 S3 Broker-shadow (optional: IBKR paper / T212 demo, no money)
                                                    20 trading days; fills reproduce the model within the cost-gate margin
                                                                      │ owner written approval per candidate
                                                                      ▼
                                      S4 Tuition (ladder stage 1: ≤ 20 EUR/month, 126 days, harm H < 10, owner executes by hand)
                                                                      ▼
                                           S5 Earned / S6 Proven (ladder stages 2-3, E ≥ 10·e² / 40·e²)  ── no algorithm path here yet
```

- **Statistical route (a)** needs the funded boundary. **Learning-expense route (b)** is ADR 0025's own: the owner chooses to spend up to
  20 EUR a month with the wording "a learning expense you chose, not evidence". Route (b) is how a slow candidate with an economic
  prior and an inconclusive test can still be run for years without pretending it is validated.
- **Cost gate (every order):** modelled edge per trade must exceed the venue's cost at the real order size plus a safety margin; the
  existing `trade_check` is extended with `breakeven_cost_bps` and `per_order_cost_eur`.
- **Shadow gate (new):** the model and the broker's paper (or demo) account run side by side; the realised slippage and fill rate
  are logged; the gate passes if realised cost per order is within the modelled cost plus the margin on 20 consecutive trading days.
  Caveat: broker paper fills are optimistic (no queue, simulated); they test plumbing and the cost model, not market impact.
- **Demotion:** harm e-process H >= 10, relative drawdown outside the band, owner veto, broker-shadow breach, ledger break, or expiry.
- **No order path** in S0-S4 (S4: the owner places the order). `OrderPort` has only a null implementation.

## 7. Sizing, risk and kill switches (proposed values; owner decides)

Sizing logic, from least to most aggressive; the plan uses the first two.

1. **Fixed small sleeve** (the pot is the loss limit).
2. **Volatility-targeted weight** inside the sleeve, capped at 1x (no leverage); position floor from the cost gate (do not trade slices
   whose cost exceeds the break-even).
3. **Fractional Kelly** is **not used for money**: with an unknown and probably near-zero edge the Kelly fraction is near zero. The
   repo's `kelly_fraction` is a reporting tool. If ever used: at most a quarter of the point estimate, shrunk by the standard error.

Proposed constants (`decision/algo/limits.py`, pinned by a test, never settings):

| Limit | Proposed |
|---|---|
| Sleeve (real money, stage 4) | 20 EUR per month, at most 250 EUR cumulative while on tuition |
| Leverage | none; no CFD, knock-out, warrant or margin product in any algo sleeve |
| Orders per day / per month per sleeve | 2 / 12 |
| Per-order notional cap | 100 EUR at tuition stage |
| Modelled cost share cap | order refused when cost > 25 % of the break-even edge for that trade |
| Sleeve drawdown halt | -15 % flat, -25 % remove from the ladder |
| Daily data/price staleness | halt on any price older than 1 trading day or deviating > 3 % from the second source (ADR 0024) |
| Heartbeat | worker silent > 2 h in market hours: Telegram, no new proposals |
| Unknown order state | freeze everything until reconciled |

Kill switches (all must exist before stage 4): worker heartbeat, daily loss and drawdown halt, stale or inconsistent data, ledger chain
break, broker connectivity loss (the hold position stays, no new orders), external trade detector (`live_positions` position moved
without a proposal), Telegram `/halt`, owner veto setting. Because a halt is likely to fire within a few years even with a fair edge,
**the halt rules are documented as budget expiry, with the Monte-Carlo probability on the card**.

## 8. Execution and cost model

| Venue | Order cost | Fits | Notes |
|---|---|---|---|
| Trading 212 Invest (read-only now, API orders need a key with orders permission, which today's rule forbids) | 0 commission; FX fee on instruments quoted in a currency other than the account's: **0.15 %** on Trading 212's own help page (UK Invest/ISA article, undated; the 2021 announcement says the same) but 0.5 % in an earlier version of this plan, so **check the fee in the owner's own EUR account before relying on either**; stamp duty on LSE shares 0.5 %, French FTT 0.4 % on large French shares, none on ETFs; spread | Slow ETF rotation of tiny size, EUR-listed ETFs | POST orders are not idempotent: never retry a timeout |
| Scalable (agentic CLI, approval per order) | 0.99 EUR on gettex per ADR 0027 | Owner-confirmed orders | ADR 0027 Phase 2 stays unbuilt |
| IBKR Ireland | Germany (page read 2026-11-25, Germany rows verbatim): Tiered 0.05 % min EUR 1.25, max EUR 29 plus exchange/clearing/regulatory fees; Fixed SmartRouting 0.05 % min EUR 3.00; Fixed GETTEX direct 0.05 % min EUR 3.00; other direct routes 0.10 % min EUR 4 or 0.12 % min EUR 6. **"Directed API orders cannot use the Tiered fee structure"; SmartRouted API orders can use Tiered or Fixed.** The 1.25 EUR figure excludes third-party fees | Research, data, paper account; real money only if slices are large enough that 1.25 EUR is under about 10 bp, which is above 1,250 EUR per order | One brokerage session per username (second username for the bot); **no fractional shares via the API** (confirmed in IBKR's own pages: the API docs state that the APIs do not support fractional or cash-quantity trading except crypto and FX, and error 10243 reads "Fractional-sized order cannot be placed via API"; fractions work in TWS and Client Portal only; page dates not shown), so the bot is whole-shares-only; daily restart; weekly re-authentication; market data paid |

Execution rules for slow rules: signal at the close, order next session, limit near mid inside the continuous session away from the first
and last 30 minutes, no market orders on thin ETFs, price check against the second source before sending (when an order path exists),
client order id written to the ledger before any request, read broker state before any retry.

Cost model object per candidate: commission schedule, half-spread by instrument (from observed quotes where available, else a stated
assumption with a sensitivity of x2), FX fee, tax-drag id. All outputs carry `estimate: True` and `not_tax_advice: True` where tax enters.

## 9. Monitoring

- Daily: data freshness and second-source check; signal and target weight per sleeve; ledger row written; paper versus shadow-fill
  gap; evidence and boundary; drawdown versus the halt; costs paid versus modelled.
- Weekly digest in Telegram (rides the existing hourly job pattern): evidence status per candidate, cost drift, halts, open questions.
- Control Center "needs attention" entries: algo ledger break, shadow gate breach, stale data, candidate near expiry, any halt.
- UI page `/algo` (later): strategy cards with hash and status, the evidence curve against the boundary, cost and slippage panels, the
  Monte-Carlo drawdown note, the ladder stage, and always the sentence "A learning expense you chose. It is not evidence that the picks work."

## 10. German tax

- Gains from ETF switches are realised: Abgeltungsteuer 25 % + Soli (+ church tax), Sparer-Pauschbetrag, Teilfreistellung (equity funds
  30 %, mixed 15 %, real estate 60 %), Vorabpauschale on accumulating funds: all in `tax_cockpit.py`/`tax_calc`; `estimate_tax_drag` approximates
  the drag in replays (documented simplification: one running allowance, no calendar-year reset).
- JStG 2024 abolished the EUR 20,000 yearly caps on Termingeschaefte losses (section 20(6) sentence 5) and on Forderungsausfaelle (sentence 6; the sources read describe sentence 6 as the loss on uncollectable claims, not on worthless shares, which is the sentence 4 pot),
  retroactive for open cases back to 2021; banks implement from 1 Jan 2026. The share-loss pot (sentence 4) remains and is provisional pending
  the Constitutional Court (2 BvL 3/21). CFDs are Termingeschaefte. Knock-outs and warrants are **not** Termingeschaefte per the BMF letter of
  3 June 2021 (reported 4 June 2021; the consolidated re-issue is BMF 19 May 2022, IV C 1 - S 2252/19/10003 :009, whose Randziffern were not readable; BFH 8 Dec 2021, I R 24/19, concerned a GmbH under section 15(4) EStG); the extrapolation to private investors rests on the BMF letter. Since the cap is gone the
  classification matters little for the cap. Secondary sources only (see the literature note, section 4). The plan still excludes derivatives from algo sleeves.
- IBKR Ireland does not withhold German tax: the owner declares in Anlage KAP / KAP-INV, no Freistellungsauftrag is possible (the
  Sparer-Pauschbetrag is claimed in the return), no German Steuerbescheinigung exists, cross-broker loss offset needs the German bank's
  Verlustbescheinigung (request by 15 Dec). Which IBKR year-end report maps to which Anlage KAP line is unverified. This favours the existing flow
  where Trading 212/Scalable statements feed the tax cockpit. Needs the owner's tax adviser before stage 4 at IBKR.
- **Per asset class (review 2026-11-25; statute text for section 20 InvStG read on gesetze-im-internet.de via a fetch worker):** equity funds 30 % Teilfreistellung (more than 50 % equity by the fund terms), mixed funds 15 % (at least 25 %), real-estate funds 60 % and foreign real-estate funds 80 %. A bond or money-market fund below those quotas gets **no** Teilfreistellung (inferred from the statute, not quoted). A physically backed gold ETC is not a fund: Xetra-Gold-type delivery-claim bonds are tax-free after one year (BFH VIII R 4/15, 12 May 2015, secondary report), other ETCs without a delivery claim are taxed under Abgeltungsteuer; the status of individual ETCs (for example iShares Physical Gold) is unverified. The replay's tax drag must therefore be set per sleeve, not 30 % for the whole basket.
- Vorabpauschale: 70 % of the Basiszins times the redemption price, capped at the year's gain; Basiszins 2.53 % (2 Jan 2025) and 3.20 % (BMF 13 Jan 2026, one source); deemed received on the first working day of the next year. Fund losses sit in the general pot, not the share-loss pot (Stiftung Warentest 2016, secondary). Lots are FIFO per depot (section 20(4) sentence 7 EStG).
- IBKR Ireland: no IBKR page was found saying whether it withholds German tax; its German tax report is informational and "not filed with the authorities".
- Every tax output keeps `estimate: True` and `not_tax_advice: True`; broker statements remain the source of truth.

## 11. Tools (new and existing)

| # | Tool | Purpose | Status |
|---|---|---|---|
| T1 | `lab/algo_lab/eprocess.py` funded promotion ledger | Anytime-valid, cost-aware promotion with a lifetime budget | **Prototype built**, 6 tests (null promotion rate 0 of 300 at alpha 0.05) |
| T2 | `lab/algo_lab/gate_math.py` | Time to a verdict, minimum detectable Sharpe, out-of-sample haircut, break-even cost, per-order cost, drawdown-breach Monte Carlo | **Prototype built**, tests |
| T3 | Induced-null falsification harness (`lab/algo_lab/null_audit.py`) | Run a whole pipeline on martingale, regime-switch and bid-ask-bounce nulls with blind parameters | Specified, not built |
| T4 | Strategy card + hash registry (`lab/algo_lab/card.py`, later table `algo_strategy_cards`) | One frozen registered object per candidate | Specified |
| T5 | Cost-aware slow-strategy replay (`lab/algo_lab/slow_replay.py`, script `scripts/algo_slow_replay.py`) on `quant_lab.engine` | Replay of the 3 arms with `CostModel`, tax drag, purged CV, DSR, PBO | Specified |
| T6 | Shadow-fill ledger (`decision/algo/shadow.py`) | Model fills versus broker paper fills, slippage and fill rate | Needs the IBKR read adapter |
| T7 | Gate benchmark: random-admission control and per-criterion rejection printout | Prove each gate beats a coin flip at equal adoption | Specified |
| T8 | Cost and break-even page (`interface`) | Show the cost per order, the share of the break-even edge, the venue choice | Later |
| Existing | `quant_metrics`, `quant_lab`, `forward_lab`, `desk_replay`, `trade_check`, ladder, Control Center | See section 3 | In use |

## 12. Roadmap, effort, exit tests

| Phase | Work | Effort | Exit test |
|---|---|---|---|
| 1a (done on this branch) | T1, T2, ADR, plan, literature note | done | Reviewed by the owner; open questions answered |
| 1b | T3 null audit, T4 card, T5 slow replay, T7 control; commit `docs/research/algo-slow-replay-2026-xx.json` | M (several days) | The replay of the 3 arms with costs, tax, DSR/PBO, drawdown bootstrap; the audit passes on nulls and detects a planted trend (positive control) |
| 2 | `decision/algo` package: card registry (migration), daily job, paper book via `paper_portfolio`, ledger rows, weekly digest, Control Center items, API and `/algo` page, `limits.py` pinned test, import-linter facade contract | L | 20 trading days of ledger with no gap; golden test for idempotent replay; contracts and cycle ledger green |
| 3 | **Optional.** Shadow-fill ledger and, if wanted, an IBKR paper account run via the read adapter (other thread) | M-L | Shadow gate evaluated for 20 trading days; cost model corrected from it |
| 4 | Ladder stage 1 for at most one slow candidate; owner places orders; 12-month review | S plus time | Owner written approval; harm H below 10; no halt |
| 5 | `OrderPort` + IBKR order adapter | XL | Own ADR; outside this plan |

Each phase may end the programme. After Phase 1b: if the replay shows the arms cannot beat the cheapest venue's cost, stop and keep T1-T7 as
measurement tools for the other desks.

## 13. Risks and what would make us stop

- **Nothing is ever significant** (expected): then the section is a risk-management lab and a learning budget. Say so on the page.
- **Overfitting the audit itself**: use blind parameters and a held-out family of nulls.
- **Choice of the benchmark and basket after seeing results**: forbidden; both are in the hash.
- **Survivorship and ETF history**: short UCITS histories (many ETFs under 10 years); long-history proxies must be labelled as such.
- **Broker paper is optimistic**: the shadow gate tests plumbing, not impact.
- **Operational**: IBKR session limits and restarts; Trading 212 non-idempotent orders; stale prices; time zones (Berlin, Xetra close 17:30).
- **Behavioural**: halts that are overridden by hand; the owner's own journal of manual CFD trades already shows how this goes (bankroll guard).
- **Regulatory/tax**: secondary sources only; the IBKR report-to-Anlage KAP mapping and primary texts remain unverified.

## 14. Questions for the owner

1. **What budget is "fixed"?** I assumed 250 EUR (desk pot) plus 20 EUR a month (ladder). The design changes above about 5,000 EUR: then IBKR's
   minimums stop dominating, micro futures become possible, and a 20 % halt is a real number. With 250 EUR the honest venue is a zero-commission broker.
2. Is **slow ETF timing** (monthly, EUR-listed UCITS) the right first family, or do you want a different asset universe (single stocks, sectors)?
3. May I register the **three arms** (section 4) once the Phase 1b replay exists, or do you want to name the basket and arms yourself?
4. The learning money: **"learning expense, inconclusive is fine"** (route b) or **"only promote on the funded e-process"** (route a)?
5. Do you accept that **IBKR is for research, paper fills and data** until the budget changes, with orders at Trading 212 or Scalable by hand?
6. Tax adviser check on IBKR Ireland reporting (Anlage KAP mapping, Verlustbescheinigung): do you want that as a prerequisite for stage 4?

## 15. Decisions (2026-11-25)

The owner delegated the section 14 questions ("choose the best option"), and later said IBKR is not worth it at this size. Each answer below is the most conservative well-reasoned option, taken on the owner's behalf. Any of them can be reversed by the owner.

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Budget | **Fixed at 250 EUR plus 20 EUR a month**, no top-up assumed. | The evidence says an edge at this size is unlikely to survive costs; a small pot caps the loss. Designing for 5,000 EUR would be designing for money that has not been committed. |
| 2 | First family | **Slow ETF timing, monthly, EUR-listed UCITS.** No single stocks, no derivatives, no intraday. | The only family with a surviving post-2009 signal, low turnover, and a tax model already in the repo. |
| 3 | Register the three arms | **Yes, after the Phase 1b replay exists**, with the arms and basket as written in section 4. Registration is frozen by hash and nothing is tuned afterwards. | Registration costs nothing and is the precondition for any evidence. The arms are the simplest rules; naming them in advance prevents a search. |
| 4 | Learning money | **Route b: a learning expense, inconclusive is fine**, with the ADR 0025 wording ("A learning expense you chose. It is not evidence that the picks work."). No promotion on anything weaker than the funded e-process. | Route a would take about nine years at realistic effect sizes; route b caps the cost at 20 EUR a month and claims nothing. |
| 5 | IBKR | **Paper and research account only.** The Fixed API minimum of about 3 to 4 EUR per order is 120 to 160 bp on 250 EUR and fractional shares are not available through the API, so IBKR is not worth real money at this size. **Real-money execution, if it is ever approved, goes through Trading 212** (zero commission, fractional shares). | Cost and granularity. Trading 212's API is a beta with non-idempotent order calls, so any order path there needs the guard layer, a client-order ledger with dedupe before send, and a reconciliation step; that belongs to its own ADR, not this one. |
| 6 | Tax adviser | **Yes, as a prerequisite before any real money moves at any broker**, covering Trading 212 and Scalable reporting and loss pots, and IBKR only if it is ever used for real money. | Cheap, and the sources read are secondary. |

Stage 4 (ladder tuition by hand) is unchanged: written owner approval per step. Stage 5 (an order path) stays out of scope.

### New verdict class: `risk_overlay`

Triage verdicts have a third kind besides "candidate" and "measured null". The class is **defined by ADR 0029 Amendment 1** (this plan only mirrors it): a card declares `alpha` or `risk_overlay`, and the class is a hashed card field. A `risk_overlay` is judged on drawdown and CVaR reduction against buy-and-hold of the same registered basket, within a cap on trailing return and a cost cap, not on excess return; it needs no funded e-process, never authorises capital, and is still pre-registered and replayed with the same costs and tax model. Its structural limit: it may only scale exposure of the registered basket down, or rotate part of it into a declared cash or bond leg, and gross exposure never exceeds the basket's own (100 % of the sleeve). It cannot add a risk asset beyond the declared legs or raise size, which matches the guard rule that strategy code may only veto. Where a family's verdict in section 4 says "candidate", a `risk_overlay` variant of the same rule is the lower-risk way to test it first.

### Paper trading does not depend on IBKR

The paper phase (stages S0 to S2, phases 1 and 2) runs entirely on Quantfolio's own paper ledger and its own market data (the existing price providers, `foundation/market.py`, EUR prices via `eur_prices.py`, and the paper-portfolio and desk ledger patterns). Candidates are registered, replayed, forward-scored and measured there with the modelled costs of the venue the owner would actually use (Trading 212, zero commission) and the tax model. Nothing in these phases reads from or waits for IBKR, so the algorithmic section works on paper with no broker account at all.

An IBKR paper account is an **optional extra execution check** (S3): it compares modelled fills against a broker's simulated fills and can correct the slippage assumption, but it never gates, feeds or blocks the paper phase. If it is never connected, S3 is skipped and the cost model keeps the conservative defaults, with the sizes the owner is willing to risk unchanged.
