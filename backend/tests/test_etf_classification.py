"""Tests for ETF classification and asset enrichment service."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from conftest import _memory_db

from app.foundation.models.entities import Asset, Holding, Portfolio, User
from app.foundation.etf_classification import classify_and_enrich


def _seed_user_and_portfolio(db):
    user = User(id="user-1", username="testuser", password_hash="hash")
    db.add(user)
    db.flush()
    portfolio = Portfolio(id="port-1", user_id=user.id, name="Main", currency="EUR")
    db.add(portfolio)
    db.commit()
    return user, portfolio


class TestClassifyAndEnrich:
    """Tests for classify_and_enrich service function."""

    @patch(
        "app.foundation.etf_classification.EtfUniverseProvider.get_universe",
        return_value=[
            {
                "symbol": "EUNL.DE",
                "isin": "IE00B4L5Y983",
                "name": "iShares Core MSCI World UCITS ETF",
                "domicile_country": "IE",
                "currency": "EUR",
                "dividends": "accumulating",
                "replication": "physical",
                "ter": 0.20,
                "strategy": "Long-only",
                "tf_class": "aktien",
            }
        ],
    )
    def test_happy_path_justetf_lookup(self, mock_get_universe):
        """A known ISIN in the justETF universe should create an Asset row."""
        db = _memory_db()
        user, portfolio = _seed_user_and_portfolio(db)

        holding = Holding(
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            name="iShares Core MSCI World",
            asset_type="etf",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("80.00"),
            currency="EUR",
            source="manual",
        )
        db.add(holding)
        db.commit()

        result = classify_and_enrich(db, user.id)

        assert result["classified"] == 1
        assert result["updated"] == 0
        assert result["errors"] == []

        asset = db.query(Asset).filter(Asset.isin == "IE00B4L5Y983").first()
        assert asset is not None
        assert asset.symbol == "EUNL.DE"
        assert asset.exchange == "XETRA"
        assert asset.asset_type == "etf"
        assert asset.currency == "EUR"
        assert asset.country == "IE"
        assert asset.ucits is True
        assert asset.accumulating is True
        assert asset.distributing is False
        assert asset.ter == Decimal("0.2000")
        meta = json.loads(asset.provider_meta_json)
        assert meta["symbol"] == "EUNL.DE"

    @patch(
        "app.foundation.etf_classification.EtfUniverseProvider.get_universe",
        return_value=[],
    )
    @patch("yfinance.Ticker")
    def test_yfinance_fallback_for_unknown_isin(self, mock_ticker_class, mock_get_universe):
        """An unknown ISIN should fall back to yfinance and classify by quoteType."""
        db = _memory_db()
        user, portfolio = _seed_user_and_portfolio(db)

        mock_ticker = MagicMock()
        mock_ticker.info = {
            "quoteType": "EQUITY",
            "symbol": "AAPL",
            "longName": "Apple Inc.",
            "currency": "USD",
            "exchange": "NMS",
            "country": "United States",
        }
        mock_ticker_class.return_value = mock_ticker

        holding = Holding(
            portfolio_id=portfolio.id,
            isin="US0378331005",
            name="Apple Inc.",
            asset_type="stock",
            quantity=Decimal("5"),
            avg_buy_price=Decimal("150.00"),
            currency="USD",
            source="manual",
        )
        db.add(holding)
        db.commit()

        result = classify_and_enrich(db, user.id)

        assert result["classified"] == 1
        assert result["updated"] == 0
        assert result["errors"] == []

        asset = db.query(Asset).filter(Asset.isin == "US0378331005").first()
        assert asset is not None
        assert asset.symbol == "AAPL"
        assert asset.asset_type == "stock"
        assert asset.currency == "USD"
        assert asset.exchange == "NMS"
        assert asset.country == "United States"
        assert asset.ucits is False

    def test_empty_portfolio_returns_zero(self):
        """When the user has no holdings, classification should return zero counts."""
        db = _memory_db()
        user, portfolio = _seed_user_and_portfolio(db)

        result = classify_and_enrich(db, user.id)

        assert result == {"classified": 0, "updated": 0, "errors": []}

    @patch(
        "app.foundation.etf_classification.EtfUniverseProvider.get_universe",
        return_value=[
            {
                "symbol": "EUNL.DE",
                "isin": "IE00B4L5Y983",
                "name": "iShares Core MSCI World UCITS ETF",
                "domicile_country": "IE",
                "currency": "EUR",
                "dividends": "distributing",
                "replication": "physical",
                "ter": 0.20,
                "strategy": "Long-only",
                "tf_class": "aktien",
            }
        ],
    )
    def test_existing_asset_is_updated(self, mock_get_universe):
        """An existing Asset row should be updated rather than duplicated."""
        db = _memory_db()
        user, portfolio = _seed_user_and_portfolio(db)

        existing = Asset(
            isin="IE00B4L5Y983",
            symbol="OLD.DE",
            exchange="XETRA",
            name="Old Name",
            asset_type="stock",
            currency="EUR",
            country="DE",
            ucits=False,
            accumulating=None,
            distributing=None,
            ter=None,
            provider_meta_json="{}",
        )
        db.add(existing)

        holding = Holding(
            portfolio_id=portfolio.id,
            isin="IE00B4L5Y983",
            name="iShares Core MSCI World",
            asset_type="etf",
            quantity=Decimal("10"),
            avg_buy_price=Decimal("80.00"),
            currency="EUR",
            source="manual",
        )
        db.add(holding)
        db.commit()

        result = classify_and_enrich(db, user.id)

        assert result["classified"] == 0
        assert result["updated"] == 1
        assert result["errors"] == []

        asset = db.query(Asset).filter(Asset.isin == "IE00B4L5Y983").first()
        assert asset.symbol == "EUNL.DE"
        assert asset.name == "iShares Core MSCI World UCITS ETF"
        assert asset.asset_type == "etf"
        assert asset.ucits is True
        assert asset.distributing is True
        assert asset.accumulating is False
        assert asset.ter == Decimal("0.2000")
