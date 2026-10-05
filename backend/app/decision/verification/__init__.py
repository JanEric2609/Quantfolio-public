"""Verification — "Can I trust it?" verdicts and Portfolio -> Risk analytics.

``trust.py`` scores the system's own frozen predictions (Discover picks, advisor
calls, mandate verdicts, regime history) with small-sample statistics;
``risk.py`` / ``stress.py`` / ``alerts.py`` back the Portfolio -> Risk page.
"""

from .alerts import AlertThresholds, RiskAlert, check_concentration_alerts, get_alert_thresholds
from .risk import ReturnBasis, RiskAssessment, compute_risk_metrics
from .stress import StressTestResult, run_stress_test
from .trust import build_verdict, list_calls

__all__ = [
    "AlertThresholds",
    "ReturnBasis",
    "RiskAlert",
    "RiskAssessment",
    "StressTestResult",
    "build_verdict",
    "check_concentration_alerts",
    "compute_risk_metrics",
    "get_alert_thresholds",
    "list_calls",
    "run_stress_test",
]
