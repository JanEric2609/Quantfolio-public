"""Alert thresholds and single-name concentration alerts for Portfolio -> Risk.

What is left of the old verification alert generator. The underperformance,
volatility-spike and confidence alerts are gone: the first two were computed
from snapshot ``total_value`` (which counts salary and purchases as return) and
the last from a score nobody ever compared to an outcome. Drawdown alerts live
in ``risk.check_risk_alerts`` on a cash-flow-free return series; this module
judges *single names* only, because a 55 % position in an MSCI World ETF is
~1,400 companies, not single-stock risk.

Nothing is persisted: the alerts are computed on request from the holdings.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.models.entities import Holding
from app.foundation.portfolio_utils import holding_market_value
from app.foundation.settings import get_setting_row

logger = logging.getLogger(__name__)


@dataclass
class RiskAlert:
    """Single actionable risk alert (shared by ``risk`` and the concentration checks)."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = "general"  # drawdown | concentration_* | currency | concentration
    severity: str = "info"  # info | warning | critical
    title: str = ""
    message: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Thresholds
#
# These module constants are the code-fallback defaults. Each is overridable
# via the matching ``verification_*`` public setting (catalog group
# ``verification``); resolve the effective values with
# ``get_alert_thresholds(db)`` instead of reading the constants.
# ---------------------------------------------------------------------------
DRAWDOWN_WARNING = -0.10             # 10 % below the peak
DRAWDOWN_CRITICAL = -0.20            # 20 % below the peak
CONCENTRATION_SINGLE = 0.30          # one single name > 30 % of portfolio
CONCENTRATION_TOP3 = 0.60            # top-3 single names > 60 %


@dataclass(frozen=True)
class AlertThresholds:
    """Effective alert thresholds resolved from settings."""

    drawdown_warning: float = DRAWDOWN_WARNING
    drawdown_critical: float = DRAWDOWN_CRITICAL
    concentration_single: float = CONCENTRATION_SINGLE
    concentration_top3: float = CONCENTRATION_TOP3


_THRESHOLD_SETTING_KEYS: dict[str, str] = {
    "drawdown_warning": "verification_drawdown_warning",
    "drawdown_critical": "verification_drawdown_critical",
    "concentration_single": "verification_concentration_single",
    "concentration_top3": "verification_concentration_top3",
}


def _coerce_threshold(raw: Any) -> float | None:
    """Coerce a stored setting value to a finite number; None = malformed."""
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_alert_thresholds(db: Session) -> AlertThresholds:
    """Resolve the alert thresholds from stored settings.

    Each ``verification_*`` AppSetting overrides the matching code default;
    unset or malformed values fall back to the default with a warning log,
    so a bad Control-Center entry can never crash the alert checks.
    """
    defaults = AlertThresholds()
    overrides: dict[str, float] = {}
    for attr, key in _THRESHOLD_SETTING_KEYS.items():
        try:
            stored = get_setting_row(db, key)
        except Exception:
            stored = None
        if stored is None:
            continue
        coerced = _coerce_threshold(stored)
        if coerced is None:
            logger.warning(
                "verification: malformed setting %s=%r — falling back to "
                "default %s", key, stored, getattr(defaults, attr),
            )
            continue
        overrides[attr] = coerced
    return AlertThresholds(**overrides)


# ---------------------------------------------------------------------------
# Single-name concentration
# ---------------------------------------------------------------------------

# Holdings of these types are baskets, not single issuers: a 55% position in an
# MSCI World ETF is ~1,400 companies, not "single-stock risk". Concentration
# alerts judge single names only.
_DIVERSIFIED_ASSET_TYPES = frozenset({"etf", "fund", "money_market", "bond"})


def _holdings(db: Session, portfolio_id: str) -> list[Holding]:
    """Return current holdings for a portfolio."""
    return db.query(Holding).filter(Holding.portfolio_id == portfolio_id).all()


def _is_diversified_fund(holding: Holding) -> bool:
    if (holding.asset_type or "").lower() in _DIVERSIFIED_ASSET_TYPES:
        return True
    if not holding.ticker:
        return False
    try:
        return classify_instrument(holding.ticker, name=holding.name) != "equity"
    except Exception:  # noqa: BLE001 — classification is best-effort; default to single name
        logger.debug("alerts: classify_instrument failed for %s", holding.ticker, exc_info=True)
        return False


def check_concentration_alerts(portfolio_id: str, db: Session) -> list[RiskAlert]:
    """Single-name and top-3 concentration alerts from the current holdings."""
    alerts: list[RiskAlert] = []
    thresholds = get_alert_thresholds(db)
    holdings = _holdings(db, portfolio_id)

    if not holdings:
        return alerts

    total_value = sum(holding_market_value(db, h) for h in holdings)
    if total_value <= 0:
        return alerts

    holdings_by_weight = sorted(
        holdings,
        key=lambda h: holding_market_value(db, h),
        reverse=True,
    )

    largest = holdings_by_weight[0]
    single_names = [h for h in holdings_by_weight if not _is_diversified_fund(h)]

    if len(holdings) == 1:
        # Single-holding portfolio: the lone position is 100% of the book by
        # construction, so concentration_single would always fire (critical,
        # since 100% >= 50%) — a structural false positive for e.g. an
        # EUNL.DE-only portfolio. Emit an educational info note instead.
        alerts.append(
            RiskAlert(
                type="concentration_context",
                severity="info",
                title="Single-holding portfolio — diversification context",
                message=(
                    f"{largest.name} is currently your only holding and therefore "
                    f"makes up 100% of this portfolio. That is not flagged as a "
                    f"risk by itself, but a single position carries undiversified "
                    f"single-asset risk — consider adding further positions over "
                    f"time."
                ),
            )
        )
    elif single_names:
        largest_single = single_names[0]
        largest_pct = holding_market_value(db, largest_single) / total_value
        if largest_pct >= thresholds.concentration_single:
            alerts.append(
                RiskAlert(
                    type="concentration_single",
                    severity="critical" if largest_pct >= 0.50 else "warning",
                    title="Single-position concentration risk",
                    message=(
                        f"{largest_single.name} represents {largest_pct:.0%} of your portfolio. "
                        f"Consider diversifying to reduce single-stock risk."
                    ),
                )
            )

    if len(single_names) >= 3:
        top3_value = sum(
            holding_market_value(db, h)
            for h in single_names[:3]
        )
        top3_pct = top3_value / total_value
        if top3_pct >= thresholds.concentration_top3:
            alerts.append(
                RiskAlert(
                    type="concentration_top3",
                    severity="warning",
                    title="Top-3 concentration is high",
                    message=(
                        f"Your top 3 single-name holdings make up {top3_pct:.0%} of the portfolio. "
                        f"A broader allocation may reduce volatility."
                    ),
                )
            )

    return alerts
