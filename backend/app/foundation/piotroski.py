"""Piotroski-inspired Fundamental Strength Score (0–9).

The classic Piotroski F-Score uses 9 binary signals from raw financial
statements (balance sheet + income statement + cash flow).  Our providers
only expose *summary ratios* (ROE, profit margin, debt/equity, etc.), so
we map each of the 9 signals to the closest available proxy.

Scoring (each 0 or 1):

  Profitability (4 pts)
  ─────────────────────
  1. ROE > 0                       — profitable equity base
  2. FCF yield > 0                 — generating free cash
  3. Profit margin > 0             — net profitability
  4. Earnings growth > 0%          — improving earnings

  Leverage & Liquidity (3 pts)
  ─────────────────────────────
  5. Debt / equity < 1.5           — manageable leverage
  6. EV / EBITDA < 25              — reasonable enterprise valuation
  7. PE ratio > 0 AND < 40         — not distressed, not extreme

  Efficiency (2 pts)
  ──────────────────
  8. Revenue growth > 0%           — top-line expansion
  9. Gross margin > 20%            — operational efficiency

All inputs come from the ``fundamentals`` table (JSON blob).  Missing
metrics score 0 for that component and the total is rescaled to 0–9.

Return type is a plain dict safe for JSON serialisation.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import Fundamental


def _f(data: dict[str, Any], key: str) -> float | None:
    """Safely extract a numeric value."""
    val = data.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _score(data: dict[str, Any]) -> dict[str, Any]:
    """Compute 9 binary signals and return structured result."""
    signals: list[dict[str, Any]] = []

    # --- Profitability (4) ---
    roe = _f(data, "roe")
    signals.append({"name": "ROE > 0", "value": roe, "pass": roe is not None and roe > 0})

    fcf = _f(data, "fcf_yield")
    signals.append({"name": "FCF yield > 0", "value": fcf, "pass": fcf is not None and fcf > 0})

    pm = _f(data, "profit_margin")
    signals.append({"name": "Profit margin > 0", "value": pm, "pass": pm is not None and pm > 0})

    eg = _f(data, "earnings_growth")
    signals.append({"name": "Earnings growth > 0%", "value": eg, "pass": eg is not None and eg > 0})

    # --- Leverage & Liquidity (3) ---
    de = _f(data, "debt_equity")
    # debt_equity from providers is often percentage (e.g. 150 = 1.5x)
    de_ratio = de / 100 if de is not None and de > 10 else de
    signals.append({"name": "Debt/equity < 1.5×", "value": de, "pass": de_ratio is not None and de_ratio < 1.5})

    ev_ebitda = _f(data, "ev_ebitda")
    signals.append({"name": "EV/EBITDA < 25", "value": ev_ebitda, "pass": ev_ebitda is not None and 0 < ev_ebitda < 25})

    pe = _f(data, "pe_ratio")
    signals.append({"name": "P/E 0–40", "value": pe, "pass": pe is not None and 0 < pe < 40})

    # --- Efficiency (2) ---
    rg = _f(data, "revenue_growth")
    signals.append({"name": "Revenue growth > 0%", "value": rg, "pass": rg is not None and rg > 0})

    gm = _f(data, "gross_margin")
    signals.append({"name": "Gross margin > 20%", "value": gm, "pass": gm is not None and gm > 0.20})

    # "score", matching the no-data branch below and every reader (the
    # StockDetail card, the recommendation engine's fundamentals context).
    score = sum(1 for s in signals if s["pass"])
    details = {"roe": roe, "pe_ratio": pe, "debt_equity": de, "profit_margin": pm}
    return {"score": score, "max": 9, "signals": signals, "details": details}


def compute_piotroski(db: Session, ticker: str) -> dict[str, Any]:
    """Fetch latest fundamentals and compute the Piotroski-inspired score.

    Returns a dict suitable for JSON serialisation:
    ``{"ticker": ..., "score": 0-9, "signals": [...], "source": ...}``
    """
    row = (
        db.query(Fundamental)
        .filter(Fundamental.ticker == ticker.upper())
        .order_by(Fundamental.fetched_at.desc())
        .first()
    )
    if row is None:
        return {
            "ticker": ticker.upper(),
            "score": None,
            "max": 9,
            "signals": [],
            "source": "unavailable",
            "message": "No fundamentals cached for this ticker.",
        }

    try:
        data = json.loads(row.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}

    result = _score(data)
    result["ticker"] = ticker.upper()
    result["source"] = row.source
    return result
