"""Monte Carlo engine for pricing and risk analysis.

Phase 2 implementation: Standard MC + variance reduction + MLMC + Greeks.
"""

from .engine import MonteCarloEngine, McSpec, McResult
from .distributions import Distribution, NormalDistribution, StudentTDistribution, NIGDistribution
from .paths import (
    GBMEuler,
    GBMMilstein,
    HestonEuler,
    OrnsteinUhlenbeckEuler,
    MertonJumpDiffusionEuler,
    CIREuler,
    create_simulator,
)
from .quasi_mc import SobolGenerator, generate_sobol_paths
from .calibration import (
    GBMParams,
    HestonParams,
    MertonParams,
    calibrate_gbm,
    calibrate_heston,
    calibrate_merton,
)

__all__ = [
    "MonteCarloEngine",
    "McSpec",
    "McResult",
    "Distribution",
    "NormalDistribution",
    "StudentTDistribution",
    "NIGDistribution",
    "GBMEuler",
    "GBMMilstein",
    "HestonEuler",
    "OrnsteinUhlenbeckEuler",
    "MertonJumpDiffusionEuler",
    "CIREuler",
    "create_simulator",
    "SobolGenerator",
    "generate_sobol_paths",
    "GBMParams",
    "HestonParams",
    "MertonParams",
    "calibrate_gbm",
    "calibrate_heston",
    "calibrate_merton",
]
