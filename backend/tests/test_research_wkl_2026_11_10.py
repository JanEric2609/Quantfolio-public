"""The Wolters Kluwer research report (2026-11-10).

The report called WKL.AS "flat at 88.34" with "no support or resistance",
"volume 11 % of normal", a "value trap" near "equity erosion" from D/E 590 %,
and "revenue stagnation" from -0.6 %. The stock had risen from 67.48 to 88 in
six weeks; the model saw one price row (today's partial session from the
price cache), the midday volume, raw book ratios on a balance sheet whose book
equity buybacks have shrunk (P/B 21; net debt / EBITDA 2.0x per the company's
2026-11-04 update), and one quarter's reported-currency revenue change.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.foundation.research as research
from app.foundation.core.db import Base
from app.foundation.fundamentals_context import net_debt_to_ebitda
from app.foundation.models.entities import Fundamental
from app.foundation.piotroski import _score, compute_piotroski


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# Prod's stored payload for WKL.AS on 2026-11-10, plus Yahoo's totalDebt,
# totalCash and ebitda for the same day.
WKL = {
    "pe_ratio": 15.063249, "forward_pe": 14.216802, "pb_ratio": 20.926147, "ev_ebitda": 12.264,
    "fcf_yield": 0.0637, "roe": 1.46141, "profit_margin": 0.21864, "gross_margin": 0.74075,
    "debt_equity": 590.213, "revenue_growth": -0.006, "earnings_growth": 0.089, "market_cap": 19600924672,
    "sector": "Industrials", "industry": "Specialty Business Services", "beta": 0.2,
    "volume": 143542, "average_volume": 1238018, "dividend_yield": 0.0293,
}
WKL_LEVERAGE = {"total_debt": 5548000256, "total_cash": 1494000000, "ebitda": 1947000064}

TODAY = date(2026, 11, 10)


def _bars(n: int = 260, start: float = 67.48, end: float = 88.06) -> list[dict]:
    """*n* sessions ending today: flat for most of the year, then the climb."""
    rows = []
    for i in range(n):
        day = TODAY - timedelta(days=(n - 1 - i))
        close = start if i < n - 32 else start + (end - start) * (i - (n - 32)) / 31
        rows.append({"date": day.isoformat(), "open": close, "high": close * 1.01, "low": close * 0.99,
                     "close": close, "volume": 1_000_000})
    rows[-1]["volume"] = 132_856  # today's session, in progress
    return rows


def test_net_debt_to_ebitda_matches_the_company_figure():
    assert net_debt_to_ebitda({**WKL, **WKL_LEVERAGE}) == 2.08
    assert net_debt_to_ebitda(WKL) is None
    assert net_debt_to_ebitda({"total_debt": 1, "ebitda": -5}) is None


def test_technicals_show_the_climb_and_ignore_the_session_in_progress():
    t = research.price_technicals(_bars(), today=TODAY)
    assert t is not None
    assert t["return_1m_pct"] > 15
    assert t["above_sma_50"] is True
    assert t["last_session_in_progress"] is True
    # Twenty completed sessions of 1,000,000; today's 132,856 is not in the average.
    assert t["avg_volume_20d_completed"] == 1_000_000
    assert t["last_completed_volume"] == 1_000_000
    assert 50 < t["rsi_14"] <= 100


def test_technicals_rsi_of_a_steady_fall_is_low():
    rows = [{"date": (TODAY - timedelta(days=40 - i)).isoformat(), "close": 100 - i, "volume": 1}
            for i in range(40)]
    assert research._rsi([r["close"] for r in rows]) == 0.0


def test_price_history_comes_from_the_stored_bars(monkeypatch):
    seen = {}

    def _history(db, ticker, days=365, allow_live=True):
        seen["args"] = (ticker, days)
        return [{"date": datetime(2026, 11, 9, tzinfo=UTC), "open": 88.0, "high": 89.0, "low": 87.0,
                 "close": 88.82, "volume": 884169.0}]

    monkeypatch.setattr(research, "history", _history)
    rows = research._price_history(None, "WKL.AS")  # type: ignore[arg-type]
    assert seen["args"] == ("WKL.AS", 365)
    assert rows == [{"date": "2026-11-09", "open": 88.0, "high": 89.0, "low": 87.0, "close": 88.82,
                     "volume": 884169}]


def test_the_prompt_judges_leverage_by_net_debt_and_labels_the_figures():
    context = {
        "ticker": "WKL.AS", "quote": {"price": 88.34, "currency": "EUR", "source": "yfinance"},
        "risk_profile": "moderate", "regime": "sideways",
        "technicals": research.price_technicals(_bars(), today=TODAY),
        "fundamentals": {**WKL, **WKL_LEVERAGE, "growth_basis": "quarter_yoy"}, "recent_news": [], "recommendations": [],
        "portfolio_context": {},
    }
    msg = research._build_user_message(context)
    assert "590" not in msg and "146" not in msg
    assert "Net debt / EBITDA: 2.1x (manageable)" in msg
    assert "Book equity is too small" in msg
    assert ("Revenue growth in the latest quarter against the same quarter a year earlier, "
            "reported currency: -0.6 %") in msg
    assert "Broad-market regime (not about this stock):** sideways" in msg
    assert "Return 1 month %" in msg and "RSI 14 days" in msg
    assert "the last session still in progress" in msg
    assert "143542" not in msg and "143,542" not in msg  # midday volume is gone
    assert "none stored for this stock" in msg
    assert "infer what investors think" in research._SYSTEM_PROMPT


def test_an_ordinary_balance_sheet_keeps_its_ratios():
    msg = "\n".join(research._fundamentals_lines({"pb_ratio": 2.0, "roe": 0.15, "debt_equity": 80.0}))
    assert "Return on equity: 15.0 %" in msg and "Debt / equity: 80 %" in msg
    assert "Book equity is too small" not in msg


def test_the_score_leaves_roe_out_and_judges_debt_by_net_debt():
    result = _score({**WKL, **WKL_LEVERAGE})
    by_name = {s["name"]: s for s in result["signals"]}
    assert by_name["ROE > 0"]["pass"] is None
    assert by_name["Net debt/EBITDA < 3×"]["pass"] is True
    assert "Debt/equity < 1.5×" not in by_name
    assert result["score"] == 7 and result["max"] == 8
    assert result["score_9"] == 8
    assert "reported currency" in by_name["Revenue growth > 0%"]["note"]
    assert _score(WKL, "yfinance")["signals"][7]["note"].startswith("In the latest quarter")
    assert _score(WKL, "finnhub")["signals"][7]["note"].startswith("Over the last twelve months")
    assert by_name["ROE > 0"]["unit"] == "pct"


def test_without_net_debt_the_debt_criterion_is_left_out_not_failed():
    result = _score(WKL)
    by_name = {s["name"]: s for s in result["signals"]}
    assert by_name["Debt/equity < 1.5×"]["pass"] is None
    assert result["max"] == 7 and result["score"] == 6


def test_an_ordinary_balance_sheet_scores_on_nine():
    result = _score({"roe": 0.15, "pb_ratio": 2.0, "debt_equity": 80.0, "pe_ratio": 12.0})
    assert result["max"] == 9 and result["score_9"] == result["score"]


def test_the_universe_screen_stub_is_no_data_not_a_weak_score():
    db = _memory_db()
    db.add(Fundamental(ticker="WKL.AS", source="universe_screen", fetched_at=datetime.now(UTC),
                       data_json=json.dumps({"market_cap": 1.96e10})))
    db.commit()
    assert compute_piotroski(db, "WKL.AS")["score"] is None


def test_growth_says_which_period_it_covers():
    """Endesa's +27 % (yfinance) was Q3 2026 against Q3 2025; the dossier said "on the year"."""
    from app.decision.discover.dossier_facts import build_facts
    from app.foundation.fundamentals_context import growth_basis

    assert growth_basis({}, "yfinance") == "quarter_yoy"
    assert growth_basis({}, "finnhub") == "ttm_yoy"
    assert growth_basis({"growth_basis": "ttm_yoy"}, "yfinance") == "ttm_yoy"
    assert growth_basis({}, None) is None

    def _fact(basis):
        facts = build_facts({}, annual={}, expected_return_pct=2.3, horizon_months=6,
                            fundamentals={"revenue_growth": 0.27, "growth_basis": basis}, macro=None)
        return next(f.text for f in facts if f.key == "revenue_growth")

    assert _fact("quarter_yoy") == "revenue growth of +27.0 % in the latest quarter against the same quarter a year earlier"
    assert _fact("ttm_yoy").endswith("over the last twelve months against the twelve before")
    assert _fact(None).endswith("against a year earlier")
