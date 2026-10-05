"""Variance reduction techniques for Monte Carlo."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np
from typing import Tuple


@dataclass
class VarianceReducer(ABC):
    """Base class for variance reduction techniques."""

    @abstractmethod
    def reduce(
        self,
        payoffs: np.ndarray,
        additional_data: dict,
    ) -> Tuple[np.ndarray, float]:
        """Apply variance reduction. Returns (adjusted_payoffs, variance_ratio)."""
        pass


@dataclass
class ControlVariateReducer(VarianceReducer):
    """Control variate: use correlated cheaper-to-compute variable.

    E[Y - β(Z - E[Z])] has lower variance than E[Y] if Z is correlated with Y.
    β optimal = Cov(Y, Z) / Var(Z)
    """

    def reduce(
        self,
        payoffs: np.ndarray,
        additional_data: dict,
    ) -> Tuple[np.ndarray, float]:
        # additional_data should contain 'control_values' (cheap-to-compute variable)
        control = additional_data.get("control_values", None)
        if control is None:
            return payoffs, 1.0

        beta = np.cov(payoffs, control)[0, 1] / np.var(control)
        control_mean = np.mean(control)
        adjusted = payoffs - beta * (control - control_mean)

        # Variance reduction ratio
        var_original = np.var(payoffs)
        var_reduced = np.var(adjusted)
        ratio = var_original / max(var_reduced, 1e-10)

        return adjusted, float(ratio)


@dataclass
class AntitheticReducer(VarianceReducer):
    """Antithetic variate: pair each path with its negation.

    If (S_1, ..., S_n) is a path, (S̄_1, ..., S̄_n) uses -Z instead of Z.
    Reduces variance by pairing dependent opposite outcomes.
    """

    def reduce(
        self,
        payoffs: np.ndarray,
        additional_data: dict,
    ) -> Tuple[np.ndarray, float]:
        antithetic_payoffs = additional_data.get("antithetic_payoffs", None)
        if antithetic_payoffs is None:
            return payoffs, 1.0

        paired = (payoffs + antithetic_payoffs) / 2
        var_original = np.var(payoffs)
        var_reduced = np.var(paired)
        ratio = var_original / max(var_reduced, 1e-10)

        return paired, float(ratio)


@dataclass
class ImportanceSamplingReducer(VarianceReducer):
    """Importance sampling: reweight paths by likelihood ratio.

    Useful for rare-event estimation (e.g., deep OTM options).
    """

    drift_shift: float = 0.0  # Shift in drift for sampling

    def reduce(
        self,
        payoffs: np.ndarray,
        additional_data: dict,
    ) -> Tuple[np.ndarray, float]:
        # Likelihood ratio weights from the probability density change
        log_likelihood = additional_data.get("log_likelihood_ratio", None)
        if log_likelihood is None:
            return payoffs, 1.0

        weights = np.exp(log_likelihood)
        weights /= np.mean(weights)  # Normalize
        adjusted = payoffs * weights

        var_original = np.var(payoffs)
        var_reduced = np.var(adjusted)
        ratio = var_original / max(var_reduced, 1e-10)

        return adjusted, float(ratio)


@dataclass
class StratifiedReducer(VarianceReducer):
    """Stratified sampling: divide domain into strata, sample uniformly from each.

    Reduces variance by ensuring coverage across the distribution.
    """

    n_strata: int = 10

    def reduce(
        self,
        payoffs: np.ndarray,
        additional_data: dict,
    ) -> Tuple[np.ndarray, float]:
        stratum_indices = additional_data.get("stratum_indices", None)
        if stratum_indices is None:
            return payoffs, 1.0

        # Stratum mean adjustment: replace each stratum with its mean
        adjusted = np.zeros_like(payoffs)
        for s in range(self.n_strata):
            mask = stratum_indices == s
            stratum_mean = np.mean(payoffs[mask])
            adjusted[mask] = stratum_mean

        var_original = np.var(payoffs)
        var_reduced = np.var(adjusted)
        ratio = var_original / max(var_reduced, 1e-10)

        return adjusted, float(ratio)


def apply_variance_reduction(
    payoffs: np.ndarray,
    techniques: list[str],
    additional_data: dict,
) -> Tuple[np.ndarray, dict]:
    """Apply one or more variance reduction techniques.

    Returns adjusted payoffs and a summary of variance ratios.
    """
    adjusted = payoffs.copy()
    ratios = {}

    for technique in techniques:
        if technique == "control_variate":
            reducer = ControlVariateReducer()
        elif technique == "antithetic":
            reducer = AntitheticReducer()
        elif technique == "importance_sampling":
            reducer = ImportanceSamplingReducer()
        elif technique == "stratified":
            reducer = StratifiedReducer()
        else:
            continue

        adjusted, ratio = reducer.reduce(adjusted, additional_data)
        ratios[technique] = ratio

    return adjusted, ratios
