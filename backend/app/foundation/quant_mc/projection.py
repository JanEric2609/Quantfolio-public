"""Fan-chart portfolio projection on the canonical quant_mc path simulators.

ADR 0003 decision 2: ``quant_mc`` is the only Monte Carlo engine. This module
is the sanctioned replacement for the retired pure-Python GBM loop that used
to live in ``quant.py`` — it composes the same primitives the engine facade
uses (:func:`create_simulator` + :func:`create_distribution`) instead of
carrying a second simulation loop.

Response shape is byte-compatible with the retired function (keys, types, and
array lengths are deterministic functions of ``years``); the sampled VALUES
differ because this engine draws from ``numpy.random.default_rng`` rather
than the global ``random`` stream. An optional ``seed`` makes runs
reproducible, which the legacy implementation could not offer.
"""

import math

import numpy as np

from .distributions import create_distribution
from .paths import create_simulator

_TRADING_DAYS_PER_YEAR = 252
_PATH_CHUNK = 2000


def gbm_fan_projection(
    start_value: float,
    annual_return: float,
    annual_volatility: float,
    years: int,
    simulations: int = 1000,
    seed: int | None = None,
) -> dict:
    """Simulate GBM paths and return terminal percentiles plus fan-chart bands.

    Returns ``{"p05", "median", "p95", "p05_terminal", "median_terminal",
    "p95_terminal", "fan_data": {"steps", "bands", "p05", "p25", "median",
    "p75", "p95"}}`` — the exact contract the Quant Lab UI consumes.
    """
    years = max(1, int(years))
    simulations = max(1, int(simulations))
    periods = years * _TRADING_DAYS_PER_YEAR
    # Keep chart storage bounded while still showing roughly monthly path shape.
    capture_every = max(1, math.ceil(periods / min(periods, max(12, years * 12))))
    capture_days = [
        day for day in range(1, periods + 1) if day % capture_every == 0 or day == periods
    ]

    simulator = create_simulator(
        "gbm_euler", {"mu": float(annual_return), "sigma": float(annual_volatility)}
    )
    distribution = create_distribution("normal", {})
    rng = np.random.default_rng(seed)

    n_captures = len(capture_days) + 1
    captured = np.empty((simulations, n_captures), dtype=float)
    endings = np.empty(simulations, dtype=float)

    # Chunked simulation bounds peak memory (paths matrix is chunk x periods);
    # the RNG streams sequentially across chunks so a seeded run stays
    # reproducible regardless of chunk size.
    done = 0
    while done < simulations:
        n = min(_PATH_CHUNK, simulations - done)
        paths = simulator.simulate(
            float(start_value),
            periods / _TRADING_DAYS_PER_YEAR,
            periods,
            n,
            rng,
            distribution,
        )
        captured[done : done + n, 0] = start_value
        captured[done : done + n, 1:] = paths[:, capture_days]
        endings[done : done + n] = paths[:, -1]
        done += n

    p05, median, p95 = (float(v) for v in np.percentile(endings, [5, 50, 95]))
    bands = [_band_percentiles(captured[:, i].tolist()) for i in range(n_captures)]
    return {
        "p05": p05,
        "median": median,
        "p95": p95,
        "p05_terminal": p05,
        "median_terminal": median,
        "p95_terminal": p95,
        "fan_data": {
            "steps": len(bands),
            "bands": bands,
            "p05": [band["p05"] for band in bands],
            "p25": [band["p25"] for band in bands],
            "median": [band["median"] for band in bands],
            "p75": [band["p75"] for band in bands],
            "p95": [band["p95"] for band in bands],
        },
    }


def _band_percentiles(values: list[float]) -> dict[str, float]:
    """Floor-index percentile band, ported unchanged from the retired copy so
    the band estimator's sampling distribution matches what the UI showed before."""
    ordered = sorted(values)

    def at(percentile: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, int(percentile * len(ordered))))]

    return {
        "p05": at(0.05),
        "p25": at(0.25),
        "median": at(0.5),
        "p75": at(0.75),
        "p95": at(0.95),
    }
