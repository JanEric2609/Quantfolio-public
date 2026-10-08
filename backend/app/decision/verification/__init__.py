"""Verification — "Can I trust it?" verdicts and Portfolio -> Risk analytics.

``trust.py`` scores the system's own frozen predictions (Discover picks, advisor
calls, mandate verdicts, regime history) with small-sample statistics;
``daily_tests.py`` runs the pre-registered calendar-time tests and
``ranking_metrics.py`` the descriptive shadow-ledger metrics (ADR 0018);
``risk.py`` / ``stress.py`` / ``alerts.py`` back the Portfolio -> Risk page.
"""

from .alerts import AlertThresholds, RiskAlert, check_concentration_alerts, get_alert_thresholds
from .history import build_history
from .ranking_metrics import build_ranking_metrics
from .risk import ReturnBasis, RiskAssessment, compute_risk_metrics
from .stress import StressTestResult, run_stress_test
from .trust import build_verdict, list_calls

__all__ = [
    "AlertThresholds",
    "ReturnBasis",
    "RiskAlert",
    "RiskAssessment",
    "StressTestResult",
    "build_history",
    "build_ranking_metrics",
    "build_verdict",
    "check_concentration_alerts",
    "compute_risk_metrics",
    "get_alert_thresholds",
    "list_calls",
    "run_stress_test",
]
