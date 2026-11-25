# Context: Algo lab (lab sub-package, research only)

## Responsibility

ADR 0029 (Proposed). The measurement tools for an algorithmic-trading section, kept out of the
decision loop until the owner accepts the ADR: how long a verdict takes, whether an edge can pay
its venue costs, and a funded promotion ledger. Pure functions and an in-memory ledger.

- `eprocess.py` — `PromotionLedger`: lifetime-budgeted, cost-aware e-process (Benhamou et al.,
  arXiv 2611.03384). Candidates get a share of a budget of 1 at birth; promotion at
  `1 / (alpha * mass)`; the crossing observation is not tradable; restarts are new candidates;
  idempotent per event key.
- `trend_cell.py` — the slow-trend paper cell as pure rules on closes (100-day breakout entry, 50-day EMA or 5 ATR state exit, no clock); frozen constants; `lab/trend_replay.py` scores it with costs.
- `gate_math.py` — `years_to_detect`, `minimum_detectable_sharpe`, `holm_alpha`,
  `oos_replication_ratio` (Jacquier et al., arXiv 2501.03938, one signal), `breakeven_cost_bps`,
  `net_sharpe`, `per_order_cost_eur`, `prob_drawdown_breach` (Monte Carlo).

## Contract invariants

- No database, no broker, no scheduler job, no API, no setting. Imports only numpy and the standard library.
- Gates nothing: no output of this package may authorise, size or send an order.
- Scores for "beats the benchmark" claims are computed on excess returns (ADR 0029 rule 3.1).
- A new tool here is research until an ADR amendment moves it into `decision/`.
