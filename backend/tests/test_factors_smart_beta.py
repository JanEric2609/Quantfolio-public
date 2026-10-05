from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import User, Portfolio, Holding, PriceCache
from app.interface.api.quant.factors import factors_smart_beta


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_factors_smart_beta_returns_ucits_etfs_and_aligned_metrics():
    db = _memory_db()
    user = User(id="user-123", username="investor", password_hash="pw")
    db.add(user)
    db.commit()

    portfolio = Portfolio(id="port-1", user_id="user-123", name="Core Depot")
    db.add(portfolio)
    db.commit()

    holding = Holding(
        id="h-1",
        portfolio_id="port-1",
        name="SAP SE",
        ticker="SAP.DE",
        quantity=Decimal("10.0"),
        avg_buy_price=Decimal("150.0"),
        asset_type="equity",
    )
    db.add(holding)
    db.commit()

    # Seed 30 recent days of prices for SAP.DE and IS3R.DE within cutoff window
    now = datetime.now(timezone.utc)
    base_date = (now - timedelta(days=30)).date()
    for i in range(30):
        dt = base_date + timedelta(days=i)
        # Portfolio asset price
        db.add(PriceCache(ticker="SAP.DE", date=dt, close=150.0 + (i % 3) * 1.5, volume=100000, fetched_at=now))
        # Smart Beta ETF prices
        db.add(PriceCache(ticker="IS3R.DE", date=dt, close=50.0 + (i % 2) * 0.5, volume=50000, fetched_at=now))
        db.add(PriceCache(ticker="IS3Q.DE", date=dt, close=70.0 + (i % 2) * 0.7, volume=50000, fetched_at=now))
        db.add(PriceCache(ticker="IS3S.DE", date=dt, close=40.0 + (i % 2) * 0.4, volume=50000, fetched_at=now))
        db.add(PriceCache(ticker="IS3U.DE", date=dt, close=35.0 + (i % 2) * 0.3, volume=50000, fetched_at=now))
        db.add(PriceCache(ticker="IS3V.DE", date=dt, close=60.0 + (i % 2) * 0.2, volume=50000, fetched_at=now))
    db.commit()

    result = factors_smart_beta(db=db, user=user)

    assert result["available"] is True
    assert len(result["etfs"]) == 5
    tickers = [e["ticker"] for e in result["etfs"]]
    assert "IS3R.DE" in tickers
    assert "IS3Q.DE" in tickers

    # Check that tracking error and overlap score were computed with real values
    is3r = next(e for e in result["etfs"] if e["ticker"] == "IS3R.DE")
    assert is3r["tracking_error"] > 0.0
    assert -1.0 <= is3r["overlap_score"] <= 1.0
