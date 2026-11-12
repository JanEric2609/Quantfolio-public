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
metrics score 0 for that component. A criterion that is not meaningful for
the company (ROE and debt/equity on near-zero book equity) is left out of
both the score and the maximum; ``score_9`` restates the score on 0–9.

Return type is a plain dict safe for JSON serialisation.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.fundamentals_context import (
    NET_DEBT_EBITDA_OK,
    book_ratios_unreliable_in,
    growth_basis,
    growth_basis_text,
    net_debt_to_ebitda,
)
from app.foundation.models.entities import Fundamental


MIN_AVAILABLE_CHECKS = 5


def _f(data: dict[str, Any], key: str) -> float | None:
    """Safely extract a numeric value."""
    val = data.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _signal(name: str, value: float | None, passed: bool | None, unit: str, note: str | None = None) -> dict[str, Any]:
    """One criterion. ``pass`` None means "not meaningful": it is left out of the score and the maximum."""
    out: dict[str, Any] = {"name": name, "value": value, "pass": passed, "unit": unit}
    if note:
        out["note"] = note
    return out


def _score(data: dict[str, Any], source: str | None = None) -> dict[str, Any]:
    """Compute the 9 binary signals and return structured result.

    When book equity is too small to divide by (``fundamentals_context``),
    ROE is not scored and debt is judged by net debt / EBITDA instead of
    debt/equity: Wolters Kluwer failed "D/E < 1.5x" at 590 % with a net debt /
    EBITDA of 2.0x, and passed "ROE > 0" on a 146 % that only measures its
    buybacks (2026-11-10).
    """
    signals: list[dict[str, Any]] = []
    small_book = book_ratios_unreliable_in(data)
    period = growth_basis_text(growth_basis(data, source))
    period = period[:1].upper() + period[1:]
    book_note = "Book equity is too small to divide by (price/book far above norms, typically after buybacks)."

    # --- Profitability (4) ---
    roe = _f(data, "roe")
    if small_book:
        signals.append(_signal("ROE > 0", roe, None, "pct", "Not meaningful: " + book_note))
    else:
        signals.append(_signal("ROE > 0", roe, roe is not None and roe > 0, "pct"))

    fcf = _f(data, "fcf_yield")
    signals.append(_signal("FCF yield > 0", fcf, fcf is not None and fcf > 0, "pct"))

    pm = _f(data, "profit_margin")
    signals.append(_signal("Profit margin > 0", pm, pm is not None and pm > 0, "pct"))

    eg = _f(data, "earnings_growth")
    signals.append(_signal("Earnings growth > 0%", eg, eg is not None and eg > 0, "pct", f"{period}."))

    # --- Leverage & Liquidity (3) ---
    de = _f(data, "debt_equity")
    nd = net_debt_to_ebitda(data)
    if small_book:
        if nd is not None:
            signals.append(_signal(f"Net debt/EBITDA < {NET_DEBT_EBITDA_OK:g}×", nd, nd < NET_DEBT_EBITDA_OK, "times",
                                   f"Used instead of debt/equity ({de:.0f} %): {book_note}" if de is not None else book_note))
        else:
            signals.append(_signal("Debt/equity < 1.5×", de, None, "pct_points",
                                   "Not meaningful: " + book_note + " Net debt/EBITDA is not available."))
    else:
        # debt_equity from providers is percent (150 = 1.5x)
        de_ratio = de / 100 if de is not None and de > 10 else de
        signals.append(_signal("Debt/equity < 1.5×", de, de_ratio is not None and de_ratio < 1.5, "pct_points"))

    ev_ebitda = _f(data, "ev_ebitda")
    signals.append(_signal("EV/EBITDA < 25", ev_ebitda, ev_ebitda is not None and 0 < ev_ebitda < 25, "times"))

    pe = _f(data, "pe_ratio")
    signals.append(_signal("P/E 0–40", pe, pe is not None and 0 < pe < 40, "times"))

    # --- Efficiency (2) ---
    rg = _f(data, "revenue_growth")
    signals.append(_signal("Revenue growth > 0%", rg, rg is not None and rg > 0, "pct",
                           f"{period}, in reported currency: currency moves and disposals count."))

    gm = _f(data, "gross_margin")
    signals.append(_signal("Gross margin > 20%", gm, gm is not None and gm > 0.20, "pct"))

    # A missing input is "not available", not a failed check (audit
    # 2026-11-12, WP1: an unknown ticker scored 0 of 9 as if all nine failed).
    not_meaningful = sum(1 for sig in signals if sig["pass"] is None)  # before missing data is marked
    for sig in signals:
        if sig["value"] is None and sig["pass"] is not None:
            sig["pass"] = None
            sig["note"] = "Not available: the provider returned no value."
            sig["available"] = False
    # "score", matching the no-data branch below and every reader (the
    # StockDetail card, the recommendation engine's fundamentals context).
    applicable = [s for s in signals if s["pass"] is not None]
    passed = sum(1 for s in applicable if s["pass"])
    maximum = len(applicable)
    # With too few checks available the count says nothing: no score.
    score = passed if maximum >= MIN_AVAILABLE_CHECKS else None
    details = {"roe": None if small_book else roe, "pe_ratio": pe,
               "debt_equity": None if small_book else de, "profit_margin": pm, "net_debt_ebitda": nd}
    return {
        "score": score,
        "max": maximum,
        "passed": passed,
        "available": maximum,
        "total": len(signals),
        # The same score on 0-9 when a criterion was not meaningful, for
        # readers that compare against 9 (the recommendation engine).
        # Missing data still counts against it here (the engine's old
        # behaviour); only criteria that are not meaningful scale it.
        "score_9": round(passed * 9 / (len(signals) - not_meaningful)) if score is not None else None,
        "signals": signals,
        "details": details,
        "book_equity_unreliable": small_book,
    }


def compute_piotroski(db: Session, ticker: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch latest fundamentals and compute the Piotroski-inspired score.

    ``refresh=True`` (the research page) reads through
    :func:`market.fundamentals`, which re-fetches a row past its TTL, the
    universe screen's market-cap-only stub, or a payload from before the
    leverage fields. Without it (the recommendation engine, many tickers) only
    the stored row is read; the stub then counts as no data, not as 0/9.

    Returns a dict suitable for JSON serialisation:
    ``{"ticker": ..., "score": 0-max, "max": ..., "signals": [...], "source": ...}``
    """
    ticker = ticker.upper()
    data: dict[str, Any] = {}
    source = "unavailable"
    if refresh:
        from app.foundation.market import fundamentals as market_fundamentals

        try:
            result = market_fundamentals(db, ticker)
        except Exception:  # noqa: BLE001 - a failed lookup is "no data", not a 500
            result = {}
        payload = result.get("data")
        if isinstance(payload, dict) and payload:
            data, source = payload, str(result.get("source") or "provider")
    else:
        row = (
            db.query(Fundamental)
            .filter(Fundamental.ticker == ticker)
            .order_by(Fundamental.fetched_at.desc())
            .first()
        )
        if row is not None and row.source != "universe_screen":
            try:
                loaded = json.loads(row.data_json or "{}")
            except (json.JSONDecodeError, TypeError):
                loaded = {}
            if isinstance(loaded, dict):
                data, source = loaded, row.source
    from app.foundation.fundamentals_context import has_fundamentals_content

    if not data or not has_fundamentals_content(data):
        return {
            "ticker": ticker,
            "score": None,
            "max": 9,
            "signals": [],
            "source": "unavailable",
            "message": "No fundamentals cached for this ticker.",
        }

    result = _score(data, source)
    result["ticker"] = ticker
    result["source"] = source
    return result
