"""Statistics calibration harness for the trust tests (plan 2026-10-14, WP-D2).

Runs the calendar-time betting test of ADR 0018 on simulated daily series
and measures what the Trust page states: the false-alarm rate over 20 years,
the time to a verdict per edge and noise, and the smallest edge a verdict can
find in 5 and 10 years. Not a trial: it makes no return claim.
"""
from app.lab.calibration.harness import (
    MODELS,
    TestSpec,
    crossing_days,
    e_paths,
    minimum_detectable_edge,
    noise_check_fail_rates,
    simulate,
    time_to_verdict,
    type_i_error,
)

__all__ = [
    "MODELS",
    "TestSpec",
    "crossing_days",
    "e_paths",
    "minimum_detectable_edge",
    "noise_check_fail_rates",
    "simulate",
    "time_to_verdict",
    "type_i_error",
]
