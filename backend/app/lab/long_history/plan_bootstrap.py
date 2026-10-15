"""A monthly saver's plan under each ADR 0022 rule, by stationary block bootstrap. NumPy only.

Units: one year's contribution is 12 (the monthly amount is 1), so wealth is
in "monthly contributions". With CRRA utility the comparison between rules
does not depend on the amount.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.lab.long_history.jst_loader import ASSETS

#: Columns of the return array, as :data:`jst_loader.ASSETS`.
GLOBAL, HOME, INTL, BOND, BILL = range(5)
EQUITY = (GLOBAL, HOME, INTL)
YEARLY_CONTRIBUTION = 12.0
MEAN_BLOCK_YEARS = 10


@dataclass(frozen=True)
class PlanCosts:
    """ADR 0022, "Simulation": TER, trading cost, German tax on rebalancing gains."""

    ter: tuple[float, float, float, float, float] = (0.0020, 0.0020, 0.0020, 0.0015, 0.0010)
    trade_cost: float = 0.0010
    tax_rate: float = 0.26375
    equity_exempt: float = 0.30
    #: The yearly allowance, in monthly contributions: EUR 1,000 against a EUR 100 plan.
    allowance: float = 10.0
    taxed: bool = True


def _weights(rule: str, year: int, horizon: int) -> np.ndarray:
    """Target weights over :data:`ASSETS` for *rule* in plan year *year* (0-based)."""
    w = np.zeros(len(ASSETS))
    if rule == "R0" or rule == "R5":
        w[GLOBAL] = 1.0
    elif rule == "R1":
        w[HOME], w[INTL] = 0.5, 0.5
    elif rule == "R2":
        w[GLOBAL], w[BOND] = 0.8, 0.2
    elif rule == "R3":
        w[GLOBAL], w[BOND] = 0.6, 0.4
    elif rule == "R4":
        start = horizon - 10
        eq = 1.0 if year < start else 1.0 - 0.5 * (year - start + 1) / 10.0
        w[GLOBAL], w[BOND] = eq, 1.0 - eq
    else:
        raise ValueError(f"unknown rule {rule}")
    return w


RULES = ("R0", "R1", "R2", "R3", "R4", "R5")
#: R5's planned withdrawal at the horizon: 24 monthly contributions, held in bills from 5 years before.
R5_WITHDRAWAL = 24.0
R5_YEARS = 5


def bootstrap_indices(pool: np.ndarray, n_paths: int, horizon: int, rng: np.random.Generator,
                      mean_block: int = MEAN_BLOCK_YEARS) -> np.ndarray:
    """Stationary-bootstrap row indices into *pool* (Politis & Romano 1994), shape (paths, horizon).

    A path starts at a random position; each next year continues the block
    (wrapping around) or, with probability 1 / mean_block, jumps to a new one.
    """
    n = len(pool)
    pos = rng.integers(0, n, n_paths)
    out = np.empty((n_paths, horizon), dtype=int)
    for t in range(horizon):
        out[:, t] = pool[pos]
        jump = rng.random(n_paths) < 1.0 / mean_block
        pos = np.where(jump, rng.integers(0, n, n_paths), (pos + 1) % n)
    return out


def simulate_rule(returns: np.ndarray, idx: np.ndarray, rule: str, horizon: int,
                  costs: PlanCosts) -> dict[str, np.ndarray]:
    """Terminal real wealth, max drawdown and R5's withdrawal shortfall per path.

    *returns* is (years, assets) real returns; *idx* the bootstrap rows (paths, horizon).
    Each year: contribute, rebalance to target (paying trading cost and the tax
    on realised gains above the allowance), then earn the year's returns net of TER.
    """
    n_paths = idx.shape[0]
    k = returns.shape[1]
    value = np.zeros((n_paths, k))
    basis = np.zeros((n_paths, k))
    ter = np.asarray(costs.ter)
    unit = np.ones(n_paths)  # time-weighted index for drawdowns
    peak = np.ones(n_paths)
    max_dd = np.zeros(n_paths)
    taxable_share = np.where(np.isin(np.arange(k), EQUITY), 1.0 - costs.equity_exempt, 1.0)
    for t in range(horizon):
        value[:, GLOBAL] += YEARLY_CONTRIBUTION  # new money lands in the core, then rebalancing moves it
        basis[:, GLOBAL] += YEARLY_CONTRIBUTION
        wealth = value.sum(axis=1)
        target = np.outer(wealth, _weights(rule, t, horizon))
        if rule == "R5" and t >= horizon - R5_YEARS:
            sleeve = np.minimum(R5_WITHDRAWAL, wealth)
            target = np.zeros_like(target)
            target[:, BILL] = sleeve
            target[:, GLOBAL] = wealth - sleeve
        trade = target - value
        sell = np.clip(-trade, 0.0, None)
        held = np.where(value > 0, value, 1.0)
        gain_share = np.where(value > 0, np.clip((value - basis) / held, 0.0, None), 0.0)
        realised = (sell * gain_share * taxable_share).sum(axis=1)
        basis = basis - np.where(value > 0, sell * basis / held, 0.0)
        tax = costs.tax_rate * np.clip(realised - costs.allowance, 0.0, None) if costs.taxed else 0.0
        cost = costs.trade_cost * np.abs(trade).sum(axis=1) + tax
        buy = np.clip(trade, 0.0, None)
        basis = basis + buy
        value = target * ((wealth - cost) / np.where(wealth > 0, wealth, 1.0))[:, None]
        r = returns[idx[:, t]] - ter
        before = value.sum(axis=1)
        value = value * (1.0 + r)
        after = value.sum(axis=1)
        unit = unit * np.where(before > 0, after / np.where(before > 0, before, 1.0), 1.0)
        peak = np.maximum(peak, unit)
        max_dd = np.maximum(max_dd, 1.0 - unit / peak)
    wealth = value.sum(axis=1)
    out = {"wealth": wealth, "max_dd": max_dd}
    if rule == "R5":
        out["withdrawal_short"] = value[:, BILL] < R5_WITHDRAWAL - 1e-9
    return out


def certainty_equivalent(wealth: np.ndarray, gamma: float) -> float:
    """CRRA certainty equivalent of terminal wealth."""
    w = np.clip(wealth, 1e-9, None)
    if gamma == 1.0:
        return float(np.exp(np.mean(np.log(w))))
    return float(np.mean(w ** (1.0 - gamma)) ** (1.0 / (1.0 - gamma)))


def utility_moment(wealth: np.ndarray, gamma: float) -> float:
    """E[W^(1-gamma)]: averaged over countries, it gives the pooled certainty equivalent."""
    return float(np.mean(np.clip(wealth, 1e-9, None) ** (1.0 - gamma)))


def haircut(returns: np.ndarray, share: float = 0.30) -> np.ndarray:
    """Lower each equity series by *share* of its own mean real premium over bills (ADR 0022)."""
    out = returns.copy()
    for j in EQUITY:
        # A haircut only ever lowers returns: a negative premium is not raised.
        premium = max(float(np.mean(returns[:, j] - returns[:, BILL])), 0.0)
        out[:, j] = returns[:, j] - share * premium
    return out

