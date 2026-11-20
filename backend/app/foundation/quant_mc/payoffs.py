"""Option payoff functions for Monte Carlo pricing."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np
from typing import Optional


@dataclass
class Payoff(ABC):
    """Base class for payoff functions."""

    @abstractmethod
    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        """Evaluate payoff for paths. paths shape: (n_paths, n_steps)."""
        pass

    @abstractmethod
    def name(self) -> str:
        pass


@dataclass
class EuropeanCallPayoff(Payoff):
    """European call: max(S_T - K, 0)."""

    strike: float

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        # paths: (n_paths, n_steps), use final value (last column)
        S_T = paths[:, -1]
        return np.maximum(S_T - self.strike, 0)

    def name(self) -> str:
        return f"european_call(K={self.strike})"


@dataclass
class EuropeanPutPayoff(Payoff):
    """European put: max(K - S_T, 0)."""

    strike: float

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        S_T = paths[:, -1]
        return np.maximum(self.strike - S_T, 0)

    def name(self) -> str:
        return f"european_put(K={self.strike})"


@dataclass
class AsianCallPayoff(Payoff):
    """Asian call: max(mean(S) - K, 0)."""

    strike: float

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        avg = np.mean(paths, axis=1)
        return np.maximum(avg - self.strike, 0)

    def name(self) -> str:
        return f"asian_call(K={self.strike})"


@dataclass
class LookbackCallPayoff(Payoff):
    """Lookback call: max(max(S) - K, 0)."""

    strike: float

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        max_s = np.max(paths, axis=1)
        return np.maximum(max_s - self.strike, 0)

    def name(self) -> str:
        return f"lookback_call(K={self.strike})"


@dataclass
class BarrierCallPayoff(Payoff):
    """Knock-out barrier call: payoff if barrier never breached."""

    strike: float
    barrier: float

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        max_s = np.max(paths, axis=1)
        knocked_out = max_s >= self.barrier
        payoff = np.maximum(paths[:, -1] - self.strike, 0)
        return np.where(knocked_out, 0, payoff)

    def name(self) -> str:
        return f"barrier_call(K={self.strike}, B={self.barrier})"


@dataclass
class PortfolioVaRPayoff(Payoff):
    """Portfolio return: final / initial - 1."""

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        # Return in bps (×10000)
        return (paths[:, -1] / paths[:, 0] - 1) * 10000

    def name(self) -> str:
        return "portfolio_return_bps"


@dataclass
class PortfolioESPayoff(Payoff):
    """Portfolio return for expected shortfall."""

    def evaluate(self, paths: np.ndarray) -> np.ndarray:
        return (paths[:, -1] / paths[:, 0] - 1) * 10000

    def name(self) -> str:
        return "portfolio_es_bps"


def create_payoff(payoff_type: str, payoff_params: Optional[dict] = None) -> Payoff:
    """Factory to create payoffs by type."""
    if payoff_params is None:
        payoff_params = {}

    if payoff_type == "european_call":
        return EuropeanCallPayoff(**payoff_params)
    elif payoff_type == "european_put":
        return EuropeanPutPayoff(**payoff_params)
    elif payoff_type == "asian_call":
        return AsianCallPayoff(**payoff_params)
    elif payoff_type == "lookback_call":
        return LookbackCallPayoff(**payoff_params)
    elif payoff_type == "barrier_call":
        return BarrierCallPayoff(**payoff_params)
    elif payoff_type == "portfolio_var":
        return PortfolioVaRPayoff()
    elif payoff_type == "portfolio_es":
        return PortfolioESPayoff()
    else:
        raise ValueError(f"Unknown payoff type: {payoff_type}")
