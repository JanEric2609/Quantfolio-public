"""DEPRECATED: duplicate tax-hints service — prefer ``services/tax_cockpit.py``.

Kept only for the ``GET /api/portfolio/tax/hints`` endpoint (api/portfolio.py)
until that consumer is merged onto the cockpit service. Do not add new
callers; full merge consciously deferred (audit-fixes-2026-08 todo 24d).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.settings import get_public_settings
from app.foundation.tax_cockpit import _disabled_payload, _estimation_allowed


def german_tax_hints(db: Session, user_id: str) -> dict[str, Any]:
    allowed, reason = _estimation_allowed(db)
    if not allowed:
        return _disabled_payload(reason)
    settings = get_public_settings(db)
    allowance = Decimal(str(settings.get("freistellungsauftrag_amount", 1000)))
    used = Decimal(str(settings.get("freistellungsauftrag_used", 0)))
    church_tax = settings.get("church_tax", "none")
    church_rate = Decimal("0") if church_tax == "none" else Decimal(str(church_tax)) / Decimal("100")
    capital_gains_tax = Decimal("0.25")
    soli = capital_gains_tax * Decimal("0.055")
    return {
        "label": "Tax estimate / planning hint",
        "not_tax_advice": True,
        "broker_statements_source_of_truth": True,
        "tax_residency_country": settings.get("tax_residency_country", "DE"),
        "capital_gains_tax_rate": float(capital_gains_tax),
        "soli_on_tax_rate": 0.055,
        "effective_tax_without_church": float(capital_gains_tax + soli),
        "church_tax_rate": float(church_rate),
        "freistellungsauftrag_amount": float(allowance),
        "freistellungsauftrag_used": float(used),
        "freistellungsauftrag_remaining": float(max(Decimal("0"), allowance - used)),
        "vorabpauschale": {
            "not_implemented": True,
            "message": (
                "ETF/fund Vorabpauschale is not computed here — it requires fund data and "
                "official base interest inputs. Use the tax cockpit year overview for an estimate."
            ),
        },
        "dividend_tax_estimate": {
            "not_implemented": True,
            "message": (
                "Dividend tax is not estimated here — it requires realised dividend cashflows. "
                "Use the tax cockpit year overview for an estimate."
            ),
        },
    }
