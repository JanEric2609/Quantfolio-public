from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import trade_check
from app.decision.trade_check import Edge, assess, horizon_end, sale_tax_eur, track_record
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    DiscoveryPrediction,
    PaperHolding,
    PaperPortfolio,
    PaperTrade,
    PaperTradeCheck,
    User,
)
from app.foundation.settings import upsert_public_settings


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


TRADE_DAY = date(2026, 9, 1)
TRADE_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    session = _memory_db()
    monkeypatch.setattr(
        "app.foundation.instrument_taxonomy.resolve_holding_asset_type",
        lambda db, **kw: "etf" if (kw.get("symbol") or "").endswith(".DE") else "stock",
    )
    yield session
    session.close()


def _user(db):
    user = User(username="owner", password_hash="x", role="admin")
    db.add(user)
    db.flush()
    return user


def _sleeve(db, user, managed_by="llm", mandate="advisor"):
    p = PaperPortfolio(user_id=user.id, name=mandate, mandate=mandate, managed_by=managed_by,
                       initial_cash=Decimal("10000"))
    db.add(p)
    db.flush()
    return p


def _trade(db, portfolio, ticker="AAPL", side="buy", qty="10", price="100", fee="1", when=TRADE_AT):
    q, px = Decimal(qty), Decimal(price)
    t = PaperTrade(portfolio_id=portfolio.id, ticker=ticker, side=side, quantity=q, price=px, value=q * px,
                   fee=Decimal(fee), date=when)
    db.add(t)
    db.flush()
    return t


def _closes(start: date, daily: float, days: int = 60, base: float = 100.0) -> dict[str, float]:
    out, value, day = {}, base, start
    for _ in range(days):
        if day.weekday() < 5:
            out[day.isoformat()] = value
            value *= 1 + daily
        day += timedelta(days=1)
    return out


def _prices(monkeypatch, series: dict[str, dict[str, float]]):
    monkeypatch.setattr("app.foundation.eur_prices.benchmark_ticker", lambda db: "EUNL.DE")
    monkeypatch.setattr(
        "app.foundation.eur_prices.eur_closes", lambda db, symbol, days=730, **kw: series.get(symbol, {})
    )


# --- the rule ---------------------------------------------------------------------


def test_no_shown_edge_blocks_any_costly_trade():
    v = assess(notional=1000, cost_eur=2.0, edge=Edge(None, "none", 0.04, "no history"))
    assert v.verdict == "block" and v.edge_eur == 0
    assert "no shown edge" in v.reason


def test_an_edge_above_cost_passes_and_below_cost_blocks():
    strong = assess(notional=1000, cost_eur=2.0, edge=Edge(0.7, "calibrated", 0.04, "x"))
    assert strong.verdict == "pass" and strong.edge_eur == pytest.approx(16.0)
    weak = assess(notional=1000, cost_eur=2.0, edge=Edge(0.52, "calibrated", 0.04, "x"))
    assert weak.verdict == "block" and "below it" in weak.reason


def test_a_losing_record_blocks_even_a_free_trade():
    assert assess(notional=1000, cost_eur=0.0, edge=Edge(0.4, "track_record", 0.04, "x")).verdict == "block"


def test_sale_tax_is_zero_under_nv_and_on_a_loss():
    assert sale_tax_eur(1000, etf=True, nv_valid=True) == 0
    assert sale_tax_eur(-50, etf=False, nv_valid=False) == 0
    assert sale_tax_eur(1000, etf=False, nv_valid=False) == pytest.approx(263.75)
    assert sale_tax_eur(1000, etf=True, nv_valid=False) == pytest.approx(700 * 0.26375)


def test_horizon_is_21_trading_days():
    assert horizon_end(date(2026, 9, 1)) == date(2026, 9, 30)


# --- the daily job ---------------------------------------------------------------------


def test_run_daily_checks_managed_trades_once_and_marks_them(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    manual = _sleeve(db, user, managed_by="manual", mandate="manual")
    _trade(db, sleeve)
    _trade(db, manual)
    db.commit()
    _prices(monkeypatch, {"AAPL": _closes(TRADE_DAY, 0.004), "EUNL.DE": _closes(TRADE_DAY, 0.001)})

    assert trade_check.run_daily(db, today=date(2026, 9, 10)) == {"checked": 1, "marked": 1}
    [check] = db.query(PaperTradeCheck).all()
    assert check.portfolio_id == sleeve.id
    assert check.cost_eur == pytest.approx(1.0 + 1.0)  # fee + 10 bp half spread on €1000
    assert check.verdict == "block" and check.p_source == "none"
    assert not check.resolved and check.contribution_eur is not None

    assert trade_check.run_daily(db, today=date(2026, 10, 20))["checked"] == 0
    db.refresh(check)
    assert check.resolved
    expected = 1000 * (check.asset_return - check.benchmark_return) - check.cost_eur
    assert check.contribution_eur == pytest.approx(expected)
    assert check.contribution_eur > 0


def test_a_sell_counts_the_other_way(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    _trade(db, sleeve, side="sell")
    db.commit()
    _prices(monkeypatch, {"AAPL": _closes(TRADE_DAY, 0.004), "EUNL.DE": _closes(TRADE_DAY, 0.001)})
    trade_check.run_daily(db, today=date(2026, 10, 20))
    [check] = db.query(PaperTradeCheck).all()
    assert check.contribution_eur < -check.cost_eur  # sold the name that went on to beat the index


def test_a_calibrated_call_is_used_for_its_trade(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    _trade(db, sleeve)
    db.add(DiscoveryPrediction(
        user_id=user.id, run_id="r", symbol="AAPL", predicted_at=TRADE_AT, horizon_days=21,
        resolve_at=TRADE_AT + timedelta(days=30), direction="buy", conviction=0.8, conviction_calibrated=0.7,
        portfolio_id=sleeve.id,
    ))
    db.commit()
    _prices(monkeypatch, {})
    trade_check.run_daily(db, today=date(2026, 9, 2))
    [check] = db.query(PaperTradeCheck).all()
    assert check.p_source == "calibrated" and check.p_hit == 0.7
    assert check.verdict == "pass"


def test_track_record_is_shrunk_toward_a_coin_flip_and_ignores_unknown_outcomes(db):
    user = _user(db)
    sleeve = _sleeve(db, user)
    for i in range(20):
        day = date(2026, 6, 1) + timedelta(days=i)
        trade = _trade(db, sleeve, when=datetime.combine(day, datetime.min.time(), tzinfo=UTC))
        db.add(PaperTradeCheck(
            trade_id=trade.id, portfolio_id=sleeve.id, traded_on=day, ticker="AAPL", side="buy",
            notional_eur=1000, cost_eur=1, p_source="none", mean_abs_excess=0.04, edge_eur=0, verdict="block",
            reason="", horizon_end=horizon_end(day), asset_return=0.05, benchmark_return=0.01, resolved=True,
        ))
    db.flush()
    p, n, mean_abs = track_record(db, sleeve.id, before=date(2026, 9, 1))
    assert n == 20 and p == pytest.approx(0.75) and mean_abs == pytest.approx(0.04)
    # Outcomes whose horizon had not ended by the trade day are not known yet.
    assert track_record(db, sleeve.id, before=date(2026, 6, 5))[1] == 0


def test_nv_makes_a_paper_sale_tax_free(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    db.add(PaperHolding(portfolio_id=sleeve.id, ticker="AAPL", name="Apple", quantity=Decimal("5"), avg_buy_price=Decimal("50"),
                        currency="EUR"))
    _trade(db, sleeve, side="sell")
    db.commit()
    _prices(monkeypatch, {})
    trade_check.run_daily(db, today=date(2026, 9, 2))
    [taxed] = db.query(PaperTradeCheck).all()
    assert taxed.tax_eur == pytest.approx(500 * 0.26375)

    db.delete(taxed)
    upsert_public_settings(db, {"tax_nv_certificate": True, "tax_nv_valid_until": "2026-12-31"})
    db.commit()
    trade_check.run_daily(db, today=date(2026, 9, 2))
    [free] = db.query(PaperTradeCheck).all()
    assert free.tax_eur == 0


def test_summary_adds_back_the_blocked_trades(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    _trade(db, sleeve)
    db.commit()
    _prices(monkeypatch, {"AAPL": _closes(TRADE_DAY, 0.004), "EUNL.DE": _closes(TRADE_DAY, 0.001)})
    trade_check.run_daily(db, today=date(2026, 10, 20))
    out = trade_check.summary(db, sleeve.id, nav_eur=10_500.0)
    assert out["mode"] == "shadow" and out["would_block"] == 1
    assert out["checked_nav_eur"] == pytest.approx(10_500.0 - out["blocked_contribution_eur"])
    assert out["estimate"] is True and out["not_tax_advice"] is True
    assert out["recent"][0]["verdict"] == "block"


def _sell_basis(db, sale):
    return trade_check._avg_cost_before(db, sale)


def test_basis_of_a_sale_after_a_full_sale_and_rebuy_is_the_rebuy(db):
    sleeve = _sleeve(db, _user(db))
    _trade(db, sleeve, qty="1", price="100", when=TRADE_AT)
    _trade(db, sleeve, side="sell", qty="1", price="120", when=TRADE_AT + timedelta(days=1))
    _trade(db, sleeve, qty="1", price="200", when=TRADE_AT + timedelta(days=2))
    sale = _trade(db, sleeve, side="sell", qty="1", price="210", when=TRADE_AT + timedelta(days=3))
    db.commit()
    assert _sell_basis(db, sale) == pytest.approx(200.0)


def test_basis_of_a_seeded_holding_ignores_buys_after_the_sale(db):
    sleeve = _sleeve(db, _user(db))
    # Seeded 10 @ 50 (no trade), bought 10 @ 70, sold 5, then bought 15 @ 90.
    _trade(db, sleeve, qty="10", price="70", when=TRADE_AT)
    sale = _trade(db, sleeve, side="sell", qty="5", price="80", when=TRADE_AT + timedelta(days=1))
    _trade(db, sleeve, qty="15", price="90", when=TRADE_AT + timedelta(days=2))
    db.add(PaperHolding(portfolio_id=sleeve.id, ticker="AAPL", name="Apple", quantity=Decimal("30"),
                        avg_buy_price=Decimal(str((15 * 60 + 15 * 90) / 30)), currency="EUR"))
    db.commit()
    assert _sell_basis(db, sale) == pytest.approx(60.0)


def test_a_call_from_a_later_cycle_is_not_used(db, monkeypatch):
    user = _user(db)
    sleeve = _sleeve(db, user)
    _trade(db, sleeve)
    for minutes, calibrated in ((2, 0.7), (60 * 20, 0.95)):
        at = TRADE_AT + timedelta(minutes=minutes)
        db.add(DiscoveryPrediction(
            user_id=user.id, run_id=f"r{minutes}", symbol="AAPL", predicted_at=at, horizon_days=21,
            resolve_at=at + timedelta(days=30), direction="buy", conviction=0.8,
            conviction_calibrated=calibrated, portfolio_id=sleeve.id,
        ))
    db.commit()
    _prices(monkeypatch, {})
    trade_check.run_daily(db, today=date(2026, 9, 2))
    [check] = db.query(PaperTradeCheck).all()
    # The call stored minutes after the trade (same cycle) counts; the next day's does not.
    assert check.p_source == "calibrated" and check.p_hit == 0.7


def test_a_check_without_a_close_on_its_horizon_day_resolves_after_the_grace(db, monkeypatch):
    sleeve = _sleeve(db, _user(db))
    _trade(db, sleeve)
    db.commit()
    end = horizon_end(TRADE_DAY)
    asset = {k: v for k, v in _closes(TRADE_DAY, 0.004).items() if k < end.isoformat()}
    bench = {k: v for k, v in _closes(TRADE_DAY, 0.001).items() if k < end.isoformat()}
    _prices(monkeypatch, {"AAPL": asset, "EUNL.DE": bench})

    trade_check.run_daily(db, today=end + timedelta(days=2))
    [check] = db.query(PaperTradeCheck).all()
    assert not check.resolved
    trade_check.run_daily(db, today=end + timedelta(days=trade_check.RESOLVE_GRACE_DAYS))
    db.refresh(check)
    assert check.resolved and check.asset_return is not None


def test_a_reset_archives_the_whole_check(db, monkeypatch):
    import json
    from unittest.mock import patch

    from app.decision.paper_portfolio import reset_portfolio
    from app.foundation.models.entities import PaperPortfolioArchive

    sleeve = _sleeve(db, _user(db))
    _trade(db, sleeve, side="sell")
    db.commit()
    _prices(monkeypatch, {})
    trade_check.run_daily(db, today=date(2026, 9, 2))
    with patch("app.decision.paper_portfolio.market_quote", return_value={"price": 100.0}):
        reset_portfolio(db, sleeve.id, reason="test")
    [row] = json.loads(db.query(PaperPortfolioArchive).one().payload_json)["trade_checks"]
    for key in ("fee_eur", "spread_eur", "tax_eur", "mean_abs_excess", "reason", "horizon_end",
                "asset_return", "benchmark_return", "checked_at"):
        assert key in row
    assert row["spread_eur"] == pytest.approx(1.0) and row["reason"]
