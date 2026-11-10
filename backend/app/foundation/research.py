"""Stock Research Hub — orchestrator service.

Assembles price history, TA indicators, fundamentals, news sentiment,
existing recommendations, portfolio context, regime state, and the
user's risk profile into a context payload.  Sends it to the LLM to
produce a structured research report that is cached 24 h in the DB.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.portfolio.name_resolver import resolve_position_name
from app.foundation.models.entities import (
    Asset,
    MultiHorizonVerdict,
    NewsItem,
    StockResearchReport,
    User,
)
from app.foundation.fundamentals_context import (
    NET_DEBT_EBITDA_OK,
    book_ratios_unreliable_in,
    growth_basis,
    growth_basis_text,
    net_debt_to_ebitda,
)
from app.foundation.market import fundamentals, history, quote
from app.foundation.text_safety import ascii_safe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report TTL
# ---------------------------------------------------------------------------
REPORT_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Data-gathering helpers (pure, testable)
# ---------------------------------------------------------------------------

def _price_history(db: Session, ticker: str, days: int = 365) -> list[dict[str, Any]]:
    """Daily OHLCV for the chart and the report, oldest first.

    Read through :func:`market.history`: stored daily bars first, then the
    price cache, then a live fetch. The price cache alone holds whatever
    quotes touched it: for WKL.AS on 2026-11-10 that was today's partial
    session (one row) beside 1,865 stored bars, so the chart showed one
    candle and the report called a stock up 30 % in six weeks "flat".
    """
    try:
        rows = history(db, ticker, days=days)
    except Exception:
        logger.warning("research: price history failed for %s", ticker, exc_info=True)
        return []
    out = []
    for r in rows:
        close = r.get("close")
        if close is None:
            continue
        day = r.get("date")
        out.append({
            "date": day.isoformat()[:10] if isinstance(day, (date, datetime)) else str(day)[:10],
            "open": float(r["open"]) if r.get("open") is not None else float(close),
            "high": float(r["high"]) if r.get("high") is not None else float(close),
            "low": float(r["low"]) if r.get("low") is not None else float(close),
            "close": float(close),
            "volume": int(r.get("volume") or 0),
        })
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over *closes*; None with too few prices."""
    if len(closes) <= period:
        return None
    gains = [max(b - a, 0.0) for a, b in zip(closes, closes[1:], strict=False)]
    losses = [max(a - b, 0.0) for a, b in zip(closes, closes[1:], strict=False)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l_ in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l_) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def price_technicals(history_rows: list[dict[str, Any]], today: date | None = None) -> dict[str, Any] | None:
    """The price and volume figures the report may quote, computed here.

    The model got a price range and nothing else, and filled the gap with
    guesses. Volume averages use completed sessions only: a bar dated today
    may be a session in progress (WKL.AS: 143,542 shares at midday read as
    "11 % of typical activity").
    """
    if not history_rows:
        return None
    today = today or datetime.now(UTC).date()
    closes = [float(r["close"]) for r in history_rows]
    last = history_rows[-1]
    last_close = closes[-1]

    def _ret(sessions: int) -> float | None:
        if len(closes) <= sessions or closes[-1 - sessions] <= 0:
            return None
        return round((last_close / closes[-1 - sessions] - 1) * 100, 1)

    def _sma(n: int) -> float | None:
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    completed = [r for r in history_rows if str(r["date"])[:10] < today.isoformat()]
    vols = [int(r.get("volume") or 0) for r in completed[-20:] if r.get("volume")]
    year = closes[-252:]
    daily = [b / a - 1 for a, b in zip(closes[-64:], closes[-63:], strict=False) if a > 0]
    vol_3m = None
    if len(daily) >= 20:
        mean = sum(daily) / len(daily)
        vol_3m = round((sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)) ** 0.5 * (252 ** 0.5) * 100, 1)
    year_rows = history_rows[-252:]
    peak, max_dd = year[0], 0.0
    for c in year:
        peak = max(peak, c)
        max_dd = min(max_dd, c / peak - 1)
    hi_i = max(range(len(year)), key=year.__getitem__)
    lo_i = min(range(len(year)), key=year.__getitem__)
    sma50, sma200 = _sma(50), _sma(200)
    return {
        "sessions": len(closes),
        "last_close": round(last_close, 4),
        "last_close_date": str(last["date"])[:10],
        "last_session_in_progress": str(last["date"])[:10] >= today.isoformat(),
        "return_1w_pct": _ret(5),
        "return_1m_pct": _ret(21),
        "return_3m_pct": _ret(63),
        "return_6m_pct": _ret(126),
        "return_12m_pct": _ret(252),
        "high_52w": round(max(year), 4),
        "high_52w_date": str(year_rows[hi_i]["date"])[:10],
        "low_52w": round(min(year), 4),
        "low_52w_date": str(year_rows[lo_i]["date"])[:10],
        "pct_below_52w_high": round((1 - last_close / max(year)) * 100, 1) if max(year) > 0 else None,
        "sma_50": sma50,
        "sma_200": sma200,
        "above_sma_50": None if sma50 is None else last_close > sma50,
        "above_sma_200": None if sma200 is None else last_close > sma200,
        "rsi_14": _rsi(closes[-120:]),
        "volatility_3m_annual_pct": vol_3m,
        "max_drawdown_12m_pct": round(max_dd * 100, 1),
        "avg_volume_20d_completed": round(sum(vols) / len(vols)) if vols else None,
        "last_completed_volume": int(completed[-1].get("volume") or 0) if completed else None,
    }


def _latest_fundamentals(db: Session, ticker: str) -> dict[str, Any] | None:
    """Fundamentals for the research view and prompt.

    Goes through :func:`market.fundamentals`, which re-fetches a row past
    its TTL or marked stale. Reading the table directly served rows that
    migration 0113 marked stale for their wrong units: EXI2.DE's dividend
    yield of 0.34 (a percent; the fund yields about 0.34 %) went to the
    research model as a 34 % yield.
    """
    try:
        result = fundamentals(db, ticker)
    except Exception:
        logger.warning("research: fundamentals lookup failed for %s", ticker, exc_info=True)
        return None
    data = result.get("data")
    if not isinstance(data, dict) or not data:
        return None
    out = dict(data)
    # ROE and debt/equity divide by a book equity too small to mean anything (WKL.AS:
    # 146 % and 590 %); the report hides both, the page greys them.
    out["book_ratios_unreliable"] = book_ratios_unreliable_in(data)
    # Leverage that does not divide by book equity, for the page and the prompt.
    nd = net_debt_to_ebitda(data)
    if nd is not None:
        out["net_debt_ebitda"] = nd
    # What the growth figures compare: one quarter (yfinance) or twelve months (Finnhub).
    basis = growth_basis(data, result.get("source"))
    if basis:
        out["growth_basis"] = basis
    return out


def _recent_news(db: Session, ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = (
        db.query(NewsItem)
        .filter(NewsItem.ticker == ticker)
        .order_by(NewsItem.published_at.desc())
        .limit(limit)
        .all()
    )
    from app.foundation.sentiment import has_real_sentiment

    return [{"title": r.title, "source": r.source, "published_at": str(r.published_at),
             "sentiment_label": r.sentiment_label if has_real_sentiment(r) else None,
             "sentiment_score": float(r.sentiment_score or 0) if has_real_sentiment(r) else None} for r in rows]


def _existing_recommendations(db: Session, ticker: str) -> list[dict[str, Any]]:
    from app.foundation.models.entities import Recommendation
    rows = (
        db.query(Recommendation)
        .filter(Recommendation.ticker == ticker)
        .order_by(Recommendation.created_at.desc())
        .limit(5)
        .all()
    )
    # ``confidence`` is on different scales per mode: 0-100 for the long-term
    # recommender, a 0-1 ranking score (not a probability) for Discover. The
    # mode travels with it so the reader can tell a 0.61 from a 61.
    return [{"verdict": r.verdict, "confidence": float(r.confidence), "mode": r.mode,
             "horizon": r.horizon, "created_at": str(r.created_at)} for r in rows]


def _portfolio_context(db: Session, user_id: str) -> dict[str, Any]:
    from app.foundation.models.entities import Holding, Portfolio
    holdings = (
        db.query(Holding)
        .join(Portfolio, Holding.portfolio_id == Portfolio.id)
        .filter(Portfolio.user_id == user_id)
        .all()
    )
    return {
        "holdings_count": len(holdings),
        "tickers": [h.ticker for h in holdings[:20]],
        "holdings": [
            {
                "name": resolve_position_name(
                    {"name": h.name, "ticker": h.ticker, "isin": h.isin}, db
                ),
                "ticker": h.ticker,
            }
            for h in holdings[:20]
        ],
    }


def _current_regime(db: Session) -> str:
    """Return the current market regime label or 'unknown'."""
    try:
        from app.lab.regime.macro_snapshot import get_or_refresh_regime
        regime = get_or_refresh_regime(db)
        return regime.get("label", "unknown") if isinstance(regime, dict) else "unknown"
    except Exception:
        return "unknown"


def _user_risk_profile(db: Session, user_id: str) -> str:
    user = db.get(User, user_id)
    return getattr(user, "risk_profile", "moderate") if user else "moderate"


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _company(db: Session, ticker: str, fund: dict[str, Any] | None) -> dict[str, Any]:
    """Who the stock is: name, sector and industry, so the report cannot be about someone else.

    The WKL.AS report was titled "Winkworth Group": the prompt opened with the
    ticker and never stated the company.
    """
    name = (fund or {}).get("name")
    if not name:
        asset = db.query(Asset).filter(Asset.symbol == ticker.upper()).first()
        name = asset.name if asset is not None else None
    return {
        "name": str(name) if name else None,
        "ticker": ticker,
        "sector": (fund or {}).get("sector"),
        "industry": (fund or {}).get("industry"),
    }


def build_context(db: Session, ticker: str, user_id: str) -> dict[str, Any]:
    """Gather all data dimensions into one dict for the LLM prompt."""
    q = quote(db, ticker)
    price_history = _price_history(db, ticker)
    fund = _latest_fundamentals(db, ticker)
    return {
        "ticker": ticker,
        "company": _company(db, ticker, fund),
        "quote": q,
        "price_history": price_history,
        "technicals": price_technicals(price_history),
        "fundamentals": fund,
        "recent_news": _recent_news(db, ticker),
        "recommendations": _existing_recommendations(db, ticker),
        "portfolio_context": _portfolio_context(db, user_id),
        "regime": _current_regime(db),
        "risk_profile": _user_risk_profile(db, user_id),
    }


# ---------------------------------------------------------------------------
# Report cache layer
# ---------------------------------------------------------------------------

def get_cached_report(db: Session, ticker: str, user_id: str) -> StockResearchReport | None:
    """Return a non-expired cached report or None."""
    row = (
        db.query(StockResearchReport)
        .filter(StockResearchReport.ticker == ticker, StockResearchReport.user_id == user_id)
        .order_by(StockResearchReport.generated_at.desc())
        .first()
    )
    if row and row.expires_at > datetime.now(UTC):
        return row
    return None


def save_report(db: Session, ticker: str, user_id: str, report_text: str,
                summary: str, context: dict[str, Any]) -> StockResearchReport:
    safe_report_text = ascii_safe(report_text)
    safe_summary = ascii_safe(summary)
    now = datetime.now(UTC)
    report = StockResearchReport(
        ticker=ticker,
        user_id=user_id,
        report_json=safe_report_text,
        executive_summary=safe_summary,
        data_snapshot_json=json.dumps(context, default=str),
        generated_at=now,
        expires_at=now + timedelta(hours=REPORT_TTL_HOURS),
    )
    try:
        db.add(report)
        db.commit()
        db.refresh(report)
    except Exception:
        db.rollback()
        raise
    return report


# ---------------------------------------------------------------------------
# LLM prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior equity research analyst.  Given the data below, produce
a concise but thorough stock research report.  The report must include:

1. **Executive Summary** — 2-3 sentence TL;DR.
2. **Price Action** — recent trend, support/resistance, volume.
3. **Technical View** — RSI, trend direction, momentum.
4. **Fundamental View** — valuation, profitability, balance sheet quality.
5. **Sentiment** — recent news and market sentiment.
6. **Risk Factors** — key risks specific to this stock.
7. **Conclusion & Recommendation** — actionable verdict (BUY / HOLD / SELL) with confidence (0-1).

Rules:
- Do not write a title or a company name as a heading; the report's title is
  added for you. The company is the one named on the "Company:" line, and no
  other company is the subject.
- Quote only figures given below, with the period they cover. Do not compute
  or estimate indicators yourself: the price figures were computed from the
  daily closes. If a figure is missing, say it is not available.
- Describe the trend from the stated returns and moving averages; never from
  one session. Judge volume only against completed sessions.
- The market regime describes the broad market, not this stock.
- Leverage: when the data says book equity is too small for debt/equity and
  ROE to mean anything, do not use them; judge debt by net debt / EBITDA.
- Growth figures cover the period their label states, in reported currency:
  one quarter can swing with prices, currency moves and disposals. Quote the
  period; do not call one quarter a trend or "stagnation", and do not name a
  cause the data does not give.
- Say "not given" rather than supply a date, a cause, a size or a history
  that is not in the data.
- With no news given, say sentiment cannot be judged from the data. Do not
  infer what investors think from volume or price. An empty news list means
  the feed stored nothing, not that nothing happened: never call it a quiet
  period, a lack of catalysts or a "value trap" risk, and never name results,
  buybacks or trading updates the data does not give.
- Name the period of any growth figure as given, and the basis of the
  free-cash-flow yield and net debt / EBITDA; they follow the provider's
  definitions, which differ from a company's own adjusted figures.

Adapt tone to the user's risk profile (conservative → cautious, aggressive → opportunity-focused).
Include brief explanations for technical terms.
Output in Markdown.
"""


def _pct(value: Any, digits: int = 1) -> str | None:
    try:
        return f"{float(value) * 100:.{digits}f} %"
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int = 2) -> str | None:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return None


def _market_cap(value: Any, currency: Any) -> str | None:
    """Market cap in billions with its size class (the model called a 19.6 bn company small-cap)."""
    try:
        cap = float(value)
    except (TypeError, ValueError):
        return None
    size = "large cap" if cap >= 10e9 else "mid cap" if cap >= 2e9 else "small cap"
    return f"{cap / 1e9:.1f} bn {currency or ''} ({size})".replace("  ", " ")


def _technicals_lines(t: dict[str, Any] | None) -> list[str]:
    if not t:
        return ["\n**Price history:** not available."]
    lines = [f"\n**Price figures** (from {t['sessions']} daily closes up to {t['last_close_date']}"
             + (", the last session still in progress" if t.get("last_session_in_progress") else "") + "):"]
    labels = [
        ("last_close", "Last close"), ("return_1w_pct", "Return 1 week %"), ("return_1m_pct", "Return 1 month %"),
        ("return_3m_pct", "Return 3 months %"), ("return_6m_pct", "Return 6 months %"),
        ("return_12m_pct", "Return 12 months %"), ("high_52w", "52-week high"), ("high_52w_date", "Date of the 52-week high"),
        ("low_52w", "52-week low"), ("low_52w_date", "Date of the 52-week low"),
        ("pct_below_52w_high", "Below 52-week high %"), ("sma_50", "50-day average"), ("sma_200", "200-day average"),
        ("above_sma_50", "Above 50-day average"), ("above_sma_200", "Above 200-day average"),
        ("rsi_14", "RSI 14 days"), ("volatility_3m_annual_pct", "Volatility, last 3 months, annualised %"),
        ("max_drawdown_12m_pct", "Largest fall from a peak, 12 months %"),
        ("avg_volume_20d_completed", "Average daily volume, last 20 completed sessions"),
        ("last_completed_volume", "Volume of the last completed session"),
    ]
    for key, label in labels:
        if t.get(key) is not None:
            lines.append(f"- {label}: {t[key]}")
    return lines


def _fundamentals_lines(fund: dict[str, Any] | None) -> list[str]:
    """Fundamentals as labelled lines; the provider's field names hid what each covers."""
    if not fund:
        return ["\n**Fundamentals:** not available."]
    unreliable = book_ratios_unreliable_in(fund)
    basis_text = growth_basis_text(growth_basis(fund))
    nd = net_debt_to_ebitda(fund)
    end = fund.get("latest_period_end")
    period_end = f" (reporting period ending {end}; a half-year reporter's latest period is six months)" if end else ""
    rows: list[tuple[str, str | None]] = [
        ("Sector / industry", " / ".join(str(x) for x in (fund.get("sector"), fund.get("industry")) if x) or None),
        ("Market cap", _market_cap(fund.get("market_cap"), fund.get("currency"))),
        ("Trailing P/E", _fmt(fund.get("pe_ratio"))),
        ("Forward P/E", _fmt(fund.get("forward_pe"))),
        ("EV / EBITDA", _fmt(fund.get("ev_ebitda"))),
        ("Price / book", _fmt(fund.get("pb_ratio"))),
        ("Free-cash-flow yield (provider cash flow before lease payments and minority interests, over market cap; not the company's own FCF)", _pct(fund.get("fcf_yield"))),
        ("Dividend yield", _pct(fund.get("dividend_yield"))),
        ("Gross margin", _pct(fund.get("gross_margin"))),
        ("Profit margin", _pct(fund.get("profit_margin"))),
        ("Return on equity", None if unreliable else _pct(fund.get("roe"))),
        ("Debt / equity", None if unreliable else (
            f"{float(fund['debt_equity']):.0f} %" if _fmt(fund.get("debt_equity")) else None)),
        ("Net debt / EBITDA", None if nd is None else f"{nd:.1f}x (provider debt incl. leases over unadjusted EBITDA)"
         + (" (manageable)" if nd <= NET_DEBT_EBITDA_OK else " (high)")),
        (f"Revenue growth {basis_text}{period_end}, reported currency", _pct(fund.get("revenue_growth"))),
        (f"Earnings growth {basis_text}{period_end}", _pct(fund.get("earnings_growth"))),
        ("Beta (provider, against its home index)", _fmt(fund.get("beta"))),
    ]
    lines = ["\n**Fundamentals** (provider data):"]
    lines += [f"- {label}: {value}" for label, value in rows if value]
    if unreliable:
        lines.append(
            "- Book equity is too small for debt/equity and return on equity to mean anything "
            f"(price/book {_fmt(fund.get('pb_ratio'))}; typically after years of buybacks); they are left out."
        )
    return lines


def _company_line(context: dict[str, Any]) -> str:
    company = context.get("company") or {}
    name = company.get("name") or context["ticker"]
    where = ", ".join(f"{label} {value}" for label, value in (("sector", company.get("sector")), ("industry", company.get("industry"))) if value)
    return f"Company: {name} ({context['ticker']})" + (f", {where}" if where else "")


def _build_user_message(context: dict[str, Any]) -> str:
    parts = [_company_line(context), f"\n## {context['ticker']} Research Context\n"]
    q = context.get("quote", {})
    if q:
        parts.append(f"**Current Price:** {q.get('price', 'N/A')} {q.get('currency') or ''} | Source: {q.get('source', 'N/A')}")
    parts.append(f"\n**Risk Profile:** {context.get('risk_profile', 'moderate')}")
    parts.append(f"**Broad-market regime (not about this stock):** {context.get('regime', 'unknown')}")

    parts += _technicals_lines(context.get("technicals"))
    parts += _fundamentals_lines(context.get("fundamentals"))

    # News
    news = context.get("recent_news", [])
    if news:
        parts.append("\n**Recent News:**")
        for n in news[:5]:
            tag = n.get("sentiment_label") or "not scored"
            parts.append(f"- [{tag}] {n['title']} ({n['source']}, {n['published_at'][:10]})")
    else:
        parts.append("\n**Recent News:** none stored for this stock.")

    # Existing recommendations
    recs = context.get("recommendations", [])
    if recs:
        parts.append("\n**Prior Recommendations:**")
        for r in recs:
            parts.append(f"- {r['verdict']} (confidence {r['confidence']}, {r.get('mode')}) — {r['horizon']}")

    # Portfolio context
    pc = context.get("portfolio_context", {})
    if pc.get("tickers"):
        in_portfolio = context["ticker"] in pc["tickers"]
        parts.append(f"\n**In Portfolio:** {'Yes' if in_portfolio else 'No'} ({pc['holdings_count']} total holdings)")

    parts.append("\n---\nGenerate the research report now.")
    return "\n".join(parts)



# ---------------------------------------------------------------------------
# Assembling the report: heading, Price Action and the company guard
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(?:#{1,4} .*|\*\*\d+\.[^*]+\*\*)$")  # callers pass stripped lines
_COMPANY_SUFFIX = re.compile(
    r"\b((?:[A-Z][\w&.\-]+)(?: [A-Z][\w&.\-]+){0,3}) (?:Group|Inc\.?|plc|PLC|Corp\.?|Corporation|N\.V\.|AG|SE|Ltd\.?|Holdings|S\.A\.)(?![\w])"
)


def report_title(context: dict[str, Any]) -> str:
    company = context.get("company") or {}
    name = company.get("name")
    return f"# {name} ({context['ticker']})" if name else f"# {context['ticker']}"


def _strip_leading_heading(text: str) -> str:
    """Drop any title the model wrote before its first section (it once named the wrong company)."""
    lines = text.lstrip().splitlines()
    while lines and (not lines[0].strip() or re.match(r"^#\s+\S", lines[0])):
        lines.pop(0)
    return "\n".join(lines)


def price_action_text(t: dict[str, Any] | None) -> str:
    """The Price Action section, written from the computed figures only."""
    if not t:
        return "Price history is not available."
    def n(key: str, suffix: str = "") -> str | None:
        return None if t.get(key) is None else f"{t[key]}{suffix}"
    parts: list[str] = []
    last = f"Last close {t['last_close']} on {t['last_close_date']}"
    parts.append(last + (" (that session was still in progress)." if t.get("last_session_in_progress") else "."))
    returns = [f"{label} {v}" for label, v in (("1 week", n("return_1w_pct", " %")), ("1 month", n("return_1m_pct", " %")),
                                              ("3 months", n("return_3m_pct", " %")), ("6 months", n("return_6m_pct", " %")),
                                              ("12 months", n("return_12m_pct", " %"))) if v]
    if returns:
        parts.append("Returns over the period: " + ", ".join(returns) + ".")
    if t.get("high_52w") is not None and t.get("low_52w") is not None:
        below = f", {t['pct_below_52w_high']} % below the high" if t.get("pct_below_52w_high") is not None else ""
        parts.append(f"52-week range {t['low_52w']} ({t['low_52w_date']}) to {t['high_52w']} ({t['high_52w_date']}){below}.")
    averages = []
    for key, label in (("sma_50", "50-day"), ("sma_200", "200-day")):
        if t.get(key) is not None:
            above = t.get("above_" + key)
            averages.append(f"{'above' if above else 'below'} its {label} average of {t[key]}")
    if averages:
        parts.append("The last close is " + " and ".join(averages) + ".")
    risk = []
    if t.get("volatility_3m_annual_pct") is not None:
        risk.append(f"volatility over the last 3 months {t['volatility_3m_annual_pct']} % annualised")
    if t.get("max_drawdown_12m_pct") is not None:
        risk.append(f"largest fall from a peak in 12 months {t['max_drawdown_12m_pct']} %")
    if risk:
        parts.append(risk[0][0].upper() + "; ".join(risk)[1:] + ".")
    if t.get("avg_volume_20d_completed"):
        parts.append(f"Average daily volume over the last 20 completed sessions: {t['avg_volume_20d_completed']:,}.")
    return " ".join(parts)


def replace_section(report: str, title_words: str, body: str) -> str:
    """Replace the body of the section whose heading contains *title_words*; append one when absent."""
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if _HEADING.match(line.strip()) and title_words.lower() in line.lower():
            j = i + 1
            while j < len(lines) and not _HEADING.match(lines[j].strip()):
                j += 1
            return "\n".join([*lines[: i + 1], "", body, "", *lines[j:]]).strip()
    return report.rstrip() + f"\n\n## Price Action\n\n{body}"


def summary_of(report_body: str) -> str:
    """The first prose paragraph of the report (headings skipped)."""
    paragraphs: list[list[str]] = [[]]
    for raw in report_body.splitlines():  # a blank line ends a paragraph (linear, no regex backtracking)
        line = raw.strip()
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    for lines in paragraphs:
        if lines and not all(_HEADING.match(line) for line in lines):
            return "\n".join(line for line in lines if not _HEADING.match(line)).strip()
    return ""


def foreign_companies(text: str, context: dict[str, Any]) -> list[str]:
    """Company-looking names in *text* that are neither the subject nor in the data given to the model."""
    company = context.get("company") or {}
    known = " ".join(
        str(x) for x in (company.get("name"), context["ticker"], company.get("industry"),
                         *[n.get("title") for n in context.get("recent_news", [])],
                         *[r.get("verdict") for r in context.get("recommendations", [])]) if x
    ).lower()
    out = []
    for m in _COMPANY_SUFFIX.finditer(text):
        first = m.group(1).split()[0].lower()
        if first in known or m.group(0).lower() in known:
            continue
        out.append(m.group(0))
    return out


def fallback_report(context: dict[str, Any]) -> str:
    """The report written from the figures alone, when the model's text named another company twice."""
    parts = [report_title(context), "", "## Price Action", "", price_action_text(context.get("technicals")), "",
             "## Fundamentals", ""]
    parts += [line for line in _fundamentals_lines(context.get("fundamentals")) if line.strip()]
    parts += ["", "_The written analysis was withheld: the model's text named a company that is not this stock._"]
    return "\n".join(parts)


def assemble_report(model_text: str, context: dict[str, Any]) -> str:
    body = _strip_leading_heading(model_text)
    body = replace_section(body, "price action", price_action_text(context.get("technicals")))
    return f"{report_title(context)}\n\n{body}"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def generate_report(db: Session, ticker: str, user_id: str, *,
                          force_refresh: bool = False) -> dict[str, Any]:
    """Generate or return a cached research report for *ticker*.

    Returns dict with keys: report, summary, context, cached, generated_at.
    """
    if not force_refresh:
        cached = get_cached_report(db, ticker, user_id)
        if cached:
            return {
                "report": cached.report_json,
                "summary": cached.executive_summary,
                "context": json.loads(cached.data_snapshot_json),
                "cached": True,
                "generated_at": cached.generated_at.isoformat(),
            }

    context = build_context(db, ticker, user_id)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(context)},
    ]

    from app.foundation.llm.router import call as llm_call
    try:
        completion = await llm_call(db, "batch_research", messages, timeout_s=120.0, user_id=user_id)
    except Exception as exc:
        logger.warning("LLM call failed for %s: %s", ticker, exc)
        return {
            "report": "",
            "summary": "Report generation unavailable.",
            "context": context,
            "cached": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
        }
    model_text = completion.content
    if model_text and foreign_companies(model_text, context):
        logger.warning("research: report for %s names another company %s; regenerating once",
                       ticker, foreign_companies(model_text, context))
        try:
            retry = await llm_call(db, "batch_research", messages, timeout_s=120.0, user_id=user_id)
            model_text = retry.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM retry failed for %s: %s", ticker, exc)
    if model_text and foreign_companies(model_text, context):
        report_text = fallback_report(context)
    elif model_text:
        report_text = assemble_report(model_text, context)
    else:
        # No model text after the retry: the figures still make a report.
        report_text = fallback_report(context)

    summary = summary_of(report_text.split("\n", 1)[1] if report_text else "")
    try:
        report = save_report(db, ticker, user_id, report_text, summary, context)
    except Exception as exc:
        logger.warning("Report persist failed: %s", exc)
        db.rollback()
        return {
            "report": report_text,
            "summary": summary,
            "context": context,
            "cached": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "persist_error": True,
        }

    return {
        "report": report_text,
        "summary": summary,
        "context": context,
        "cached": False,
        "generated_at": report.generated_at.isoformat(),
    }


VERDICT_HORIZONS = ("1m", "1-6m", "6m-1y", "1-3y", "3-5y", "5y+")


def normalise_verdicts(raw: Any) -> list[dict[str, Any]]:
    """Valid verdicts only, one per known horizon, in horizon order.

    The model's confidence is its own statement, not a calibrated probability:
    clamped to 0-1 and tagged ``source: "llm_uncalibrated"``. Unknown horizons,
    verdicts other than BUY/HOLD/SELL and non-numeric confidences are dropped.
    """
    if not isinstance(raw, list):
        return []
    found: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        horizon = str(item.get("horizon", "")).strip().lower().replace(" ", "")
        verdict = str(item.get("verdict", "")).strip().upper()
        try:
            confidence = float(item.get("confidence") or 0.0) if item.get("confidence") is not None else math.nan
        except (TypeError, ValueError):
            continue
        if horizon not in VERDICT_HORIZONS or verdict not in ("BUY", "HOLD", "SELL") or math.isnan(confidence):
            continue
        found.setdefault(horizon, {"horizon": horizon, "verdict": verdict,
                                   "confidence": round(min(1.0, max(0.0, confidence)), 2),
                                   "source": "llm_uncalibrated"})
    return [found[h] for h in VERDICT_HORIZONS if h in found]


async def generate_multi_horizon_verdicts(db: Session, ticker: str, user_id: str) -> dict[str, Any]:
    """Ask the LLM for multi-horizon BUY/HOLD/SELL verdicts and persist them."""
    # Return recent verdicts if they exist (avoid unbounded growth + redundant LLM calls)
    recent = (
        db.query(MultiHorizonVerdict)
        .filter(
            MultiHorizonVerdict.ticker == ticker,
            MultiHorizonVerdict.user_id == user_id,
            MultiHorizonVerdict.generated_at >= datetime.now(UTC) - timedelta(hours=REPORT_TTL_HOURS),
        )
        .order_by(MultiHorizonVerdict.generated_at.desc())
        .first()
    )
    # Rows stored before validation may hold a made-up "unknown" HOLD: ignore them.
    horizons = normalise_verdicts(json.loads(recent.horizons_json)) if recent else []
    if horizons and recent:
        return {"ticker": ticker, "verdicts": horizons, "generated_at": recent.generated_at.isoformat(), "cached": True}

    context = build_context(db, ticker, user_id)
    horizon_prompt = (
        f"You are a quantitative analyst.  Given the research context for {ticker}, "
        "produce a JSON array of verdicts for these horizons:\n"
        '1m, 1-6m, 6m-1y, 1-3y, 3-5y, 5y+\n'
        'Each item: {"horizon": "...", "verdict": "BUY|HOLD|SELL", "confidence": 0.0-1.0, "source": "llm"}\n'
        "Return ONLY the JSON array, no commentary."
    )
    messages = [
        {"role": "system", "content": "You are a quantitative analyst. Return only valid JSON."},
        {"role": "user", "content": horizon_prompt + "\n\nContext:\n" + _build_user_message(context)},
    ]

    from app.foundation.llm.router import call as llm_call
    completion = await llm_call(db, "batch_research", messages, timeout_s=60.0, user_id=user_id)

    # Try to parse JSON from response
    raw = completion.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        horizons = normalise_verdicts(json.loads(raw))
    except json.JSONDecodeError:
        horizons = []
    if not horizons:
        # Nothing usable: say so, store nothing, so the page shows a retry state
        # instead of a made-up HOLD at 50 %.
        return {"ticker": ticker, "verdicts": [], "generated_at": datetime.now(UTC).isoformat(),
                "error": "The model's answer could not be read as verdicts."}

    # Persist
    verdict = MultiHorizonVerdict(
        ticker=ticker,
        user_id=user_id,
        generated_at=datetime.now(UTC),
        horizons_json=json.dumps(horizons),
    )
    db.add(verdict)
    try:
        db.commit()
        db.refresh(verdict)
    except Exception:
        db.rollback()
        raise

    return {"ticker": ticker, "verdicts": horizons, "generated_at": verdict.generated_at.isoformat()}
