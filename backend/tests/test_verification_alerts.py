"""Tests for Portfolio -> Risk alert thresholds and single-name concentration alerts.

The underperformance, volatility and confidence alerts (and their settings)
were removed with the confidence score; drawdown alerts live in
``risk.check_risk_alerts`` and are covered in test_verification_risk.py (their
thresholds are tested here).
"""
import dataclasses
import logging
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.verification.alerts import (
    AlertThresholds,
    check_concentration_alerts,
    get_alert_thresholds,
)
from app.decision.verification.risk import RiskAssessment, check_risk_alerts
from app.foundation.auth import current_user
from app.foundation.core.db import Base, get_db
from app.foundation.models.entities import Holding, Portfolio, User
from app.foundation.settings import DEFAULT_PUBLIC_SETTINGS, upsert_public_settings
from app.foundation.settings_catalog import get_catalog
from app.main import app

# The four alert-threshold keys that remain and their EXACT defaults.
THRESHOLD_KEYS_DEFAULTS = {
    "verification_drawdown_warning": -0.10,
    "verification_drawdown_critical": -0.20,
    "verification_concentration_single": 0.30,
    "verification_concentration_top3": 0.60,
}
REMOVED_KEYS = (
    "verification_benchmark_symbol",
    "verification_max_snapshots_lookback",
    "verification_underperformance_threshold",
    "verification_var_breach_threshold",
    "verification_confidence_low",
    "verification_confidence_warning",
)


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _make_user(db) -> User:
    user = User(id=str(uuid4()), username=f"user_{uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.flush()
    return user


def _make_portfolio(db, user_id: str) -> Portfolio:
    p = Portfolio(id=str(uuid4()), user_id=user_id, name="Test", currency="EUR")
    db.add(p)
    db.flush()
    return p


def _make_holding(
    db,
    portfolio_id: str,
    name: str = "Acme Corp",
    asset_type: str = "stock",
    qty: float = 100.0,
    price: float = 50.0,
) -> Holding:
    h = Holding(
        id=str(uuid4()),
        portfolio_id=portfolio_id,
        name=name,
        asset_type=asset_type,
        quantity=Decimal(str(qty)),
        avg_buy_price=Decimal(str(price)),
        currency="EUR",
    )
    db.add(h)
    db.flush()
    return h


def _book(db):
    user = _make_user(db)
    return _make_portfolio(db, user.id)


# ---------------------------------------------------------------------------
# Single-name concentration
# ---------------------------------------------------------------------------

class TestCheckConcentrationAlerts:
    def test_no_holdings(self):
        db = _memory_db()
        assert check_concentration_alerts(_book(db).id, db) == []

    def test_no_alerts_diversified(self):
        db = _memory_db()
        portfolio = _book(db)
        for i, atype in enumerate(["stock", "etf", "bond", "stock", "etf", "bond"]):
            _make_holding(db, portfolio.id, name=f"Holding {i}", asset_type=atype, qty=100, price=50)
        assert check_concentration_alerts(portfolio.id, db) == []

    def test_single_position_concentration_warning(self):
        db = _memory_db()
        portfolio = _book(db)
        # 35% in one position -> warning
        _make_holding(db, portfolio.id, name="Big Stock", qty=35, price=100)
        _make_holding(db, portfolio.id, name="Small Stock", qty=32, price=100)
        _make_holding(db, portfolio.id, name="Tiny Stock", qty=33, price=100)
        single = [a for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_single"]
        assert len(single) == 1
        assert single[0].severity == "warning"

    def test_extreme_concentration_critical(self):
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="Mega Stock", qty=60, price=100)
        _make_holding(db, portfolio.id, name="Other", qty=40, price=100)
        single = [a for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_single"]
        assert len(single) == 1
        assert single[0].severity == "critical"

    def test_single_holding_emits_concentration_context_info(self):
        """One-holding portfolio: educational context instead of a panic alert
        (a lone holding is 100% by construction, e.g. an IWDA.L-only portfolio)."""
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="IWDA.L", qty=100, price=100)
        alerts = check_concentration_alerts(portfolio.id, db)
        context = [a for a in alerts if a.type == "concentration_context"]
        assert len(context) == 1 and context[0].severity == "info"
        assert [a for a in alerts if a.type.startswith("concentration") and a.severity in ("warning", "critical")] == []

    def test_two_holding_80_20_still_warns(self):
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="Big", qty=80, price=100)
        _make_holding(db, portfolio.id, name="Small", qty=20, price=100)
        alerts = check_concentration_alerts(portfolio.id, db)
        single = [a for a in alerts if a.type == "concentration_single"]
        assert len(single) == 1 and single[0].severity == "critical"  # 80% >= 50%
        assert [a for a in alerts if a.type == "concentration_context"] == []

    def test_top3_concentration(self):
        db = _memory_db()
        portfolio = _book(db)
        for name, qty in [("A", 25), ("B", 20), ("C", 20), ("D", 18), ("E", 17)]:  # top 3 = 65%
            _make_holding(db, portfolio.id, name=f"Stock {name}", qty=qty, price=100)
        top3 = [a for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_top3"]
        assert len(top3) == 1

    def test_broad_etf_is_not_single_stock_concentration(self):
        """A 55% MSCI World ETF is a basket of ~1,400 companies, not single-stock risk."""
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="iShares Core MSCI World", asset_type="etf", qty=55, price=100)
        _make_holding(db, portfolio.id, name="iShares Core S&P 500", asset_type="etf", qty=27, price=100)
        _make_holding(db, portfolio.id, name="ASML Holding", qty=11, price=100)
        _make_holding(db, portfolio.id, name="SAP SE", qty=7, price=100)
        alerts = check_concentration_alerts(portfolio.id, db)
        assert [a for a in alerts if a.type in ("concentration_single", "concentration_top3")] == []

    def test_single_name_beside_etfs_still_warns(self):
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="World ETF", asset_type="etf", qty=60, price=100)
        _make_holding(db, portfolio.id, name="Big Single Stock", qty=35, price=100)
        _make_holding(db, portfolio.id, name="Small Stock", qty=5, price=100)
        single = [a for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_single"]
        assert len(single) == 1
        assert "Big Single Stock" in single[0].message
        assert single[0].severity == "warning"

    def test_alerts_are_computed_not_persisted(self):
        from app.foundation.models.entities import VerificationAlert

        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="Big", qty=80, price=100)
        _make_holding(db, portfolio.id, name="Small", qty=20, price=100)
        check_concentration_alerts(portfolio.id, db)
        assert db.query(VerificationAlert).count() == 0


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholdOverrides:
    def _35_percent_book(self, db):
        portfolio = _book(db)
        _make_holding(db, portfolio.id, name="Big Stock", qty=35, price=100)
        _make_holding(db, portfolio.id, name="Small Stock", qty=32, price=100)
        _make_holding(db, portfolio.id, name="Tiny Stock", qty=33, price=100)
        return portfolio

    def test_concentration_single_override_silences_the_warning(self):
        db = _memory_db()
        portfolio = self._35_percent_book(db)
        def single() -> list[str]:
            return [a.type for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_single"]

        assert single() == ["concentration_single"]
        upsert_public_settings(db, {"verification_concentration_single": 0.5})
        assert single() == []

    def test_concentration_single_override_can_tighten(self):
        db = _memory_db()
        portfolio = _book(db)
        for name, qty in [("A", 25), ("B", 24), ("C", 26), ("D", 25)]:
            _make_holding(db, portfolio.id, name=f"Stock {name}", qty=qty, price=100)

        def single() -> list[str]:
            return [a.type for a in check_concentration_alerts(portfolio.id, db) if a.type == "concentration_single"]

        assert single() == []  # the largest name is 26 %, below the default 30 %
        upsert_public_settings(db, {"verification_concentration_single": 0.20})
        assert single() == ["concentration_single"]

    def test_drawdown_thresholds_drive_the_risk_alert_severity(self):
        db = _memory_db()
        portfolio = _book(db)
        _make_holding(db, portfolio.id, qty=1, price=100)

        def assessment(drawdown: float) -> RiskAssessment:
            result = RiskAssessment(current_drawdown=drawdown)
            result.return_basis.n_obs = 120
            return result

        def severities(drawdown: float) -> list[str]:
            return [a.severity for a in check_risk_alerts(portfolio.id, db, assessment(drawdown)) if a.type == "drawdown"]

        assert severities(-0.07) == []
        upsert_public_settings(db, {"verification_drawdown_warning": -0.05})
        assert severities(-0.07) == ["warning"]
        assert severities(-0.25) == ["critical"]
        upsert_public_settings(db, {"verification_drawdown_critical": -0.30})
        assert severities(-0.25) == ["warning"]


class TestCatalogAndRouteKeys:
    def test_catalog_contains_the_four_threshold_entries(self):
        catalog = get_catalog()
        for key in THRESHOLD_KEYS_DEFAULTS:
            entry = catalog.get(key)
            assert entry is not None, f"missing catalog entry for {key}"
            assert entry["group"] == "verification"
            assert entry["input_type"] == "number"

    def test_defaults_in_default_public_settings_are_exact(self):
        for key, default in THRESHOLD_KEYS_DEFAULTS.items():
            assert DEFAULT_PUBLIC_SETTINGS[key] == default, key

    def test_removed_settings_are_gone_from_defaults_and_catalog(self):
        catalog = get_catalog()
        for key in REMOVED_KEYS:
            assert key not in DEFAULT_PUBLIC_SETTINGS, key
            assert key not in catalog, key

    def test_settings_route_and_schema_expose_the_remaining_keys(self):
        db = _memory_db()
        user = _make_user(db)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[current_user] = lambda: user
        try:
            client = TestClient(app)
            resp = client.get("/api/settings")
            assert resp.status_code == 200
            settings = resp.json()["settings"]
            for key, default in THRESHOLD_KEYS_DEFAULTS.items():
                assert settings[key] == default, key

            body = client.get("/api/settings/schema").json()
            for key in THRESHOLD_KEYS_DEFAULTS:
                assert key in body["catalog"], key
                assert key in body["completeness"]["public_setting_keys"], key
            for key in REMOVED_KEYS:
                assert key not in body["catalog"], key
        finally:
            app.dependency_overrides.clear()


class TestMalformedFallback:
    def test_defaults(self):
        thresholds = get_alert_thresholds(_memory_db())
        assert thresholds == AlertThresholds()
        assert (thresholds.drawdown_warning, thresholds.drawdown_critical) == (-0.10, -0.20)
        assert (thresholds.concentration_single, thresholds.concentration_top3) == (0.30, 0.60)

    def test_malformed_string_falls_back_with_warning(self, caplog):
        db = _memory_db()
        upsert_public_settings(db, {"verification_drawdown_warning": "abc"})
        with caplog.at_level(logging.WARNING, logger="app.decision.verification.alerts"):
            thresholds = get_alert_thresholds(db)
        assert thresholds.drawdown_warning == -0.10
        assert any("verification_drawdown_warning" in r.message for r in caplog.records)

    def test_boolean_is_not_a_number(self):
        db = _memory_db()
        upsert_public_settings(db, {"verification_concentration_single": True})
        assert get_alert_thresholds(db).concentration_single == 0.30

    def test_thresholds_are_frozen(self):
        thresholds = get_alert_thresholds(_memory_db())
        assert {f.name for f in dataclasses.fields(thresholds)} == {
            "drawdown_warning",
            "drawdown_critical",
            "concentration_single",
            "concentration_top3",
        }
        with pytest.raises(dataclasses.FrozenInstanceError):
            thresholds.drawdown_warning = -0.01  # type: ignore[misc]
