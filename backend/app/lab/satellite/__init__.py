"""The satellite, simulated net of DKB costs and German tax (report Phase 3, step 3).

Each out-of-sample score from ``pooled_model`` picks a mega-cap satellite
under pre-registered rules, once per broker (three stocks at DKB, ten at
Scalable), traded through ``quant_lab``'s engine and graded against the MSCI
World core. See ``CONTEXT.md``.
"""

from app.lab.satellite.picks import Review, pick
from app.lab.satellite.rf import load_risk_free, parse_monthly_rf
from app.lab.satellite.study import (
    TRIAL_CONTEXT,
    SatelliteCard,
    SatelliteStudy,
    core_tax_annual,
    results_path,
    run_satellite_study,
)

__all__ = [
    "TRIAL_CONTEXT",
    "Review",
    "SatelliteCard",
    "SatelliteStudy",
    "core_tax_annual",
    "load_risk_free",
    "parse_monthly_rf",
    "pick",
    "results_path",
    "run_satellite_study",
]
