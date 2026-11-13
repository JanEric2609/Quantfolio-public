"""How far a provider's balance-sheet ratios can be read at face value.

Buybacks and goodwill can leave book equity small next to the business. Then
debt/equity and ROE measure the accounting, not the company: Wolters Kluwer
read D/E 590 % and ROE 146 % (P/B 21) on 2026-11-10, and the research report
called it a "value trap" near "equity erosion", while the company's own
net debt / EBITDA was 2.0x (nine-month trading update, 2026-11-04). Net debt
over EBITDA does not divide by book equity, so it is the leverage figure
shown instead.

Shared by Discover's fundamentals stage, the research report and the
fundamental strength score.
"""
from __future__ import annotations

import math
import re
from typing import Any

FINANCIALS = re.compile(r"financ|bank|insurance|capital markets|mortgage", re.I)

#: Above this price-to-book, book equity is too small to divide by.
BOOK_MAX_PB = 50.0
#: Above this debt/equity (percent), outside financials, the same.
BOOK_MAX_DE_PCT = 1000.0
#: ROE above 100 % says so only together with heavy leverage (NVDA's 110 %
#: with D/E 15 % is real).
BOOK_MAX_ROE = 1.0
BOOK_LEVERED_DE_PCT = 300.0

#: Net debt / EBITDA at or below this counts as manageable leverage.
NET_DEBT_EBITDA_OK = 3.0


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def book_ratios_unreliable(
    pb: float | None, roe: float | None, de: float | None, *, sector: str | None, industry: str | None,
) -> bool:
    """True when buybacks or losses have left book equity near zero.

    PM's P/B of 2936 and D/E of 18608 % on 2026-11-05 scored as data (Discover
    audit, 2026-11-06). A high ROE alone is real, so it counts only with heavy
    leverage. A bank's debt/equity is its business: high leverage does not
    mark its equity as near zero.
    """
    if pb is not None and pb > BOOK_MAX_PB:
        return True
    if roe is not None and de is not None and roe > BOOK_MAX_ROE and de > BOOK_LEVERED_DE_PCT:
        return True
    financial = bool(FINANCIALS.search(f"{sector or ''} {industry or ''}"))
    return de is not None and de > BOOK_MAX_DE_PCT and not financial


def book_ratios_unreliable_in(data: dict[str, Any]) -> bool:
    """:func:`book_ratios_unreliable` on a provider fundamentals payload."""
    return book_ratios_unreliable(
        _num(data.get("pb_ratio")), _num(data.get("roe")), _num(data.get("debt_equity")),
        sector=data.get("sector"), industry=data.get("industry"),
    )


def net_debt_to_ebitda(data: dict[str, Any]) -> float | None:
    """(Total debt - cash) / EBITDA from a fundamentals payload, or None.

    Taken as stored when the provider sent it, else computed from
    ``total_debt``, ``total_cash`` and ``ebitda``. None without a positive
    EBITDA: a ratio over a loss says nothing.
    """
    stored = _num(data.get("net_debt_ebitda"))
    if stored is not None:
        return stored
    debt, cash, ebitda = _num(data.get("total_debt")), _num(data.get("total_cash")), _num(data.get("ebitda"))
    if debt is None or ebitda is None or ebitda <= 0:
        return None
    return round((debt - (cash or 0.0)) / ebitda, 2)


#: What a provider's revenue_growth and earnings_growth compare. Endesa's +27 %
#: (yfinance, 2026-11-10) was Q3 2026 against Q3 2025; over twelve months it
#: was about +7 %. Finnhub's growth fields are twelve months against twelve.
_BASIS_BY_SOURCE = {"yfinance": "quarter_yoy", "finnhub": "ttm_yoy"}
GROWTH_BASIS_TEXT = {
    "quarter_yoy": "in the latest quarter against the same quarter a year earlier",
    "ttm_yoy": "over the last twelve months against the twelve before",
}


def growth_basis(data: dict[str, Any], source: str | None = None) -> str | None:
    """``quarter_yoy``, ``ttm_yoy`` or None: stored with the payload, else known from its source."""
    basis = data.get("growth_basis")
    if basis in GROWTH_BASIS_TEXT:
        return str(basis)
    return _BASIS_BY_SOURCE.get(str(source or "").split("_")[0].lower())


def growth_basis_text(basis: str | None, period_months: int | float | None = None) -> str:
    """The period a growth figure covers, in words ("against a year earlier" when unknown).

    ``period_months`` is the length of the latest reporting period inferred from
    the statement dates: WKL.AS reports half-years, and "latest quarter" was
    wrong for it (audit 2026-11-12, WP2).
    """
    if basis == "quarter_yoy":
        word = {3: "quarter", 6: "half-year", 12: "year"}.get(int(period_months)) if period_months else None
        if word:
            return f"in the latest {word} against the same {word} a year earlier"
    return GROWTH_BASIS_TEXT.get(basis or "", "against a year earlier")


def period_months_from_dates(dates: list[Any]) -> int | None:
    """3, 6 or 12 from the gap between the two newest statement dates, else None."""
    from datetime import date, datetime

    days: list[date] = []
    for d in dates:
        if isinstance(d, datetime):
            days.append(d.date())
        elif isinstance(d, date):
            days.append(d)
        else:
            try:
                days.append(datetime.fromisoformat(str(d)[:10]).date())
            except ValueError:
                continue
    days = sorted(set(days), reverse=True)
    if len(days) < 2:
        return None
    gap = (days[0] - days[1]).days
    for months, lo, hi in ((3, 75, 110), (6, 160, 200), (12, 340, 390)):
        if lo <= gap <= hi:
            return months
    return None


#: Ingestion range checks: a value outside is stored as None with a reason.
#: Measured 2026-11-12: 10 rows with a free-cash-flow yield above 30 %, 7 with a
#: dividend yield above 12 %.
RANGE_CHECKS: dict[str, tuple[float, float]] = {
    "fcf_yield": (-1.0, 0.30),
    "dividend_yield": (0.0, 0.12),
}


def apply_range_checks(data: dict[str, Any]) -> dict[str, Any]:
    """Null out-of-range metrics and record ``<key>_reason`` for each."""
    for key, (lo, hi) in RANGE_CHECKS.items():
        v = _num(data.get(key))
        if v is not None and not lo <= v <= hi:
            data[key] = None
            data[f"{key}_reason"] = "out_of_range"
    return data


_FUNDAMENTALS_LABEL_KEYS = frozenset(
    {"name", "sector", "industry", "currency", "isin", "growth_basis", "latest_period_end", "period_months",
     "fcf_yield_reason", "dividend_yield_reason"}
)


def has_fundamentals_content(data: Any) -> bool:
    """True when a fundamentals payload carries at least one real metric.

    A ticker no provider knows comes back from Yahoo as a dict of nulls (plus
    the fixed ``growth_basis`` label). Storing that as a valid row scored the
    ticker 0 of 9 as if every check had failed (audit 2026-11-12, WP1).
    """
    if not isinstance(data, dict):
        return False
    return any(v is not None for k, v in data.items() if k not in _FUNDAMENTALS_LABEL_KEYS)
