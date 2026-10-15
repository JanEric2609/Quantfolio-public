"""E-backtests of VaR and ES forecasts (Wang, Wang & Ziegel 2022, arXiv 2209.00991).

Under a correct forecast each e-value below has mean at most 1, so a betting
product of them is an e-process: it stays near or below 1 when the forecasts
are right and grows when they understate risk. A value of 1 / alpha rejects
"the forecasts are correct" at level alpha, at any time it is looked at.

Losses are positive (a loss of 3 % is 0.03).
"""
from __future__ import annotations

from typing import Iterable

#: Largest share of the wealth bet on one e-value; keeps every factor at least 0.5.
LAMBDA_MAX = 0.5


def var_e(loss: float, var: float, level: float = 0.95) -> float:
    """1{loss > VaR} / (1 - level): mean 1 when VaR is the true level-quantile."""
    return (1.0 if loss > var else 0.0) / (1.0 - level)


def es_e(loss: float, var: float, es: float, level: float = 0.975) -> float:
    """(loss - VaR)+ / ((1 - level)(ES - VaR)): mean at most 1 when VaR and ES are both right."""
    gap = max(es - var, 1e-12)
    return max(loss - var, 0.0) / ((1.0 - level) * gap)


def e_process(e_values: Iterable[float]) -> list[float]:
    """The running product of (1 - lambda + lambda * e), lambda chosen from past e-values only (GREE).

    lambda_t = (mean(e) - 1) / mean((e - 1)^2) over the earlier values, clipped
    to [0, LAMBDA_MAX]; 0 for the first value.
    """
    out: list[float] = []
    wealth = 1.0
    past: list[float] = []
    for e in e_values:
        lam = 0.0
        if past:
            mean = sum(past) / len(past)
            second = sum((x - 1.0) ** 2 for x in past) / len(past)
            if second > 0:
                lam = min(LAMBDA_MAX, max(0.0, (mean - 1.0) / second))
        wealth *= 1.0 - lam + lam * e
        out.append(wealth)
        past.append(e)
    return out

