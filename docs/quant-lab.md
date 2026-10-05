# Quant Lab

The Quant Lab (`/quantlab`, `frontend/src/pages/quantlab/`) is the analysis workspace behind the everyday pages: the real book's risk and return, target allocations, a wealth projection, backtests judged against luck, and the research tools. It sits behind the sidebar's "Show research tools" switch.

Every return is in EUR: a close in another currency is restated at that day's rate before any return is taken (`foundation/eur_prices.py`). The benchmark is MSCI World, net total return, in EUR and unhedged (`EUNL.DE`, setting `benchmark_ticker`), aligned to the book by date.

## Views

| View | Route | What it shows |
|---|---|---|
| Overview | `/quantlab/overview` | The book's headline metrics and links into the views below |
| Holdings | `/quantlab/holdings` | Positions at DKB and Scalable (All / DKB / Scalable switch, one card per depot). Snapshots are taken automatically after every sync. |
| Risk | `/quantlab/risk` | Volatility, drawdown, VaR/CVaR, beta, alpha and R² against the benchmark, inner-joined on date |
| Optimisation | `/quantlab/optimisation` | Covariance-only target weights and the trades a ±5 pp band would make |
| Monte Carlo | `/quantlab/scenarios` | The wealth projection in today's euros |
| Correlation | `/quantlab/correlation` | Correlation matrix and rolling correlation |
| Backtest | `/quantlab/backtest` | One rule on one instrument, with costs and German tax, and a verdict on the evidence |
| Regime | `/quantlab/regime` | The jump model's state and what it changes |
| Goals | `/quantlab/goals` | Goals with their probability of being reached |
| Factors, Attribution | `/quantlab/factors`, `/quantlab/attribution` | Factor exposures (Fama-French, ETF proxies) and attribution |
| Research, LLM research, Intelligence | `/quantlab/research`, `/quantlab/llm-research`, `/quantlab/intelligence` | The research library and LLM-assisted analysis |
| Runs, Experiments | `/quantlab/runs`, `/quantlab/experiments` | Recorded runs and the experiment registry |

## Equity curve

The book's curve is a time-weighted unit value (GIPS) from the real position snapshots and the cash flows, `U(i+1) = U(i) · V(i+1) / (V(i) + C(i))`, so deposits do not count as return. The benchmark is rebased to the same start; an IRR is shown next to it (`GET /api/quant/portfolio/real/performance`).

## Target allocation and rebalancing

`foundation/allocation.py`. Mean-variance optimisation on historical means is an error maximiser, and for a few broad ETFs it adds nothing over a fixed allocation, so every candidate uses the covariance matrix only, shrunk by Ledoit-Wolf, long-only, fully invested and optionally capped per line: equal weight, inverse volatility, minimum variance, equal risk contribution and hierarchical risk parity (`GET /api/quant/portfolio/optimization`).

Rebalancing uses bands (`GET /api/quant/portfolio/real/rebalance`): new money goes to the most underweight lines first, and a line is sold only when it sits more than 5 percentage points above target and a year of contributions would not bring it back, with the tax cost of the sale shown. The monthly plan applies the same rule at sleeve level.

The skfolio research endpoints (`POST /api/quant/optim/run`, `/optim/all`, `/optim/stress`) remain for exploration; they are not what the plan follows.

## Wealth projection

`foundation/wealth_planner.py`, `GET /api/quant/portfolio/projection`. Ten thousand monthly paths in today's euros:

- the drift is a published long-run real return estimate (setting `mc_cma_real_return`, with its source and date), simulated in log space;
- each path draws its own drift with a standard error of σ/√25, because nobody knows the future average;
- monthly shocks are Student-t with ν = 5 (fat tails);
- volatility is the book's own Ledoit-Wolf estimate when 252 days of history exist, else 16 %;
- contributions come from the monthly plan.

The median is the planning number; the mean is shown only for contrast. The probability of a goal carries its Monte Carlo standard error and the range for a drift one standard error lower or higher (`POST /api/quant/goals/{goal_id}/mc` uses the same engine).

## Backtest

`lab/quant_lab/strategy_backtest.py`, `GET /api/quant/backtest/strategies`, `POST /api/quant/backtest/run`.

- Rules: buy and hold, trend (price above its moving average), moving-average cross, momentum, RSI mean reversion (Wilder smoothing), Bollinger breakout.
- A signal at the close of day t trades at the close of day t+1.
- Costs (commission and half-spread in basis points) and German tax (Teilfreistellung by fund class, loss offset, the annual allowance) are applied on the lab engine.
- The result is compared with holding the same instrument and with the benchmark.
- Every run is written to the trial ledger (`context="backtest"`). The verdict uses the deflated Sharpe ratio of the excess over holding, at the real number of trials and at the effective number, and the probability of backtest overfitting from CSCV over the rule's parameter grid (16 partitions).

| Verdict | When |
|---|---|
| `reference` | buy and hold itself |
| `too_short` | under three years of data |
| `evidence` | deflated Sharpe ≥ 0.95 and PBO ≤ 0.5 |
| `insufficient_evidence` | everything else, which is the usual answer |

## Regime

The jump model (`lab/regime/`, `jumpmodels`) labels the market `bull`, `sideways` or `bear`, with a crisis flag when VIX, the credit spread or the drawdown passes its threshold (`lab/regime/crisis_gate.py`). It is shown as a state with the date it began. What it changes is stated on the page: Discover pauses new picks in a bear state or a crisis. It sets no optimiser constraints and no factor weights.

## Endpoints

```
GET  /api/quant/lab/overview
GET  /api/quant/portfolio/real/{holdings,summary,risk,performance,rebalance}
GET  /api/quant/portfolio/optimization?max_weight=
GET  /api/quant/portfolio/projection?years=&goal_eur=&contribution_eur=&real_return=
GET  /api/quant/backtest/strategies
POST /api/quant/backtest/run
GET  /api/quant/regime/current
GET  /api/quant/correlation-matrix, /api/quant/correlation/rolling
GET  /api/quant/factors/{zoo,attribution,dashboard,ff3,rotation,smart-beta}
POST /api/quant/optim/{run,all,stress}
GET  /api/quant/goals, POST /api/quant/goals/{goal_id}/mc
```

`docs/api/openapi.json` is the complete, authoritative list.

## Tests

`backend/tests/test_quant_metrics.py` (golden values), `test_goal_monte_carlo.py`, `test_strategy_backtest.py`, `test_quant_optim.py`, `test_quant_factors.py`, `test_quant_ml.py`, `test_quant_rl.py`.
