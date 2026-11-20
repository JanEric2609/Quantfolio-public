"""Wire-compatibility snapshot test for the 3 previously-untyped risk endpoints
(`/api/quant/portfolio/risk`, `/portfolio/real/summary`, `/portfolio/real/risk`).

Context: docs/archive/audits/2026-08-26/deepdive-03-quantlab.md Issue 6. These handlers
used to return bare ``dict[str, Any]`` with no ``response_model=``, so FastAPI's
OpenAPI generator emitted ``{"type": "object", "additionalProperties": true}``
with zero declared fields — the CI ``api-contract`` oasdiff gate was a
structural no-op for this surface. `backend/app/api/quant/portfolio.py` now
declares Pydantic response models (`QuantPortfolioRiskResponse`,
`RealPortfolioMetricsResponse`, `RealPortfolioRiskResponse`) wired via
``response_model=`` + ``response_model_exclude_unset=True``.

This test proves that typing the response did NOT change the wire contract:
for each endpoint, across both its ``available: true`` success path and every
``available: false`` early-return branch, it calls the endpoint through a real
TestClient (exercising the actual FastAPI response-model serialization) and
asserts the resulting JSON has exactly the same *keys* (recursively, ignoring
order and list-element count) as calling the same underlying handler/service
function directly and json-round-tripping its raw dict — i.e. the Pydantic
model neither drops nor invents a field relative to the untyped dict the
handler used to return verbatim.

Follows the snapshot-testing spirit of tests/test_jobs_registration_snapshot.py
per AGENTS.md's Scheduler section, adapted to this subsystem (structural key-set
comparison rather than an exact frozen literal, since the underlying quant
metrics are computed from synthetic price data with floating-point outputs).
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal
from uuid import uuid4

import pandas as pd
from fastapi.testclient import TestClient

from conftest import _memory_db

from app.foundation.core.db import get_db
from app.main import create_app
from app.foundation.models.entities import DkbAccount, DkbPosition, User
from app.foundation.auth import current_user
import app.foundation.market as market_service
import app.foundation.portfolio.bridge as bridge_mod
from app.foundation.portfolio_price_service import PortfolioPriceService


def _client(db):
    """Build a TestClient wired to the given already-seeded session."""
    app = create_app()

    def _test_db():
        try:
            yield db
        finally:
            pass

    user = db.query(User).first()

    app.dependency_overrides[get_db] = _test_db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def _keys_recursive(obj, prefix=""):
    """Collect a set of dotted key-paths, recursing into dicts and (for lists)
    the shape of their first element only — list length legitimately differs
    run to run (e.g. equity_curve is capped/truncated), only key *shape*
    matters for the wire-contract claim being tested here."""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            keys |= _keys_recursive(v, path)
    elif isinstance(obj, list) and obj:
        keys |= _keys_recursive(obj[0], f"{prefix}[]")
    return keys


def _synthetic_matrix(n=60):
    start = datetime.date(2024, 1, 1)
    matrix = {}
    for ticker, base_price, drift in [("AAA", 100.0, 0.001), ("BBB", 50.0, -0.0005)]:
        series = {}
        price = base_price
        for i in range(n):
            d = (start + datetime.timedelta(days=i)).isoformat()
            price = price * (1 + drift + (0.01 if i % 5 == 0 else -0.003))
            series[d] = round(price, 4)
        matrix[ticker] = series
    weights = {"AAA": 6000.0, "BBB": 4000.0}
    diagnostics = {"priced_assets": 2, "missing_history": []}
    return matrix, weights, diagnostics


def _seed_real_holdings(db, user_id):
    account = DkbAccount(
        id=uuid4().hex, user_id=user_id, type="depot",
        iban=f"DE{uuid4().hex[:20]}", balance=Decimal("10000"), currency="EUR",
    )
    db.add(account)
    db.commit()
    db.add_all([
        DkbPosition(
            id=uuid4().hex, account_id=account.id, isin="US0378331005", ticker="AAPL",
            name="Apple Inc", quantity=Decimal("10"), avg_buy_price=Decimal("150"),
            current_price=Decimal("180"), current_value=Decimal("1800"),
        ),
        DkbPosition(
            id=uuid4().hex, account_id=account.id, isin="US5949181045", ticker="MSFT",
            name="Microsoft Corp", quantity=Decimal("5"), avg_buy_price=Decimal("300"),
            current_price=Decimal("320"), current_value=Decimal("1600"),
        ),
    ])
    db.commit()


def _real_price_df():
    start = datetime.date(2024, 1, 1)
    dates = [start + datetime.timedelta(days=i) for i in range(60)]
    return pd.DataFrame(
        {
            "AAPL": [180 + i * 0.1 - (2 if i % 7 == 0 else 0) for i in range(60)],
            "MSFT": [320 - i * 0.05 + (1 if i % 5 == 0 else 0) for i in range(60)],
        },
        index=pd.to_datetime(dates),
    )


def _fake_history(db, ticker, days=365):
    start = datetime.date(2024, 1, 1)
    rows = []
    price = 400.0
    for i in range(min(days, 90)):
        price *= 1 + (0.0006 if i % 4 else -0.0003)
        rows.append({"date": start + datetime.timedelta(days=i), "close": round(price, 4)})
    return rows


def _assert_wire_shape_matches(client, url, expected_dict):
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    actual = resp.json()

    expected_keys = _keys_recursive(json.loads(json.dumps(expected_dict, default=str)))
    actual_keys = _keys_recursive(actual)

    missing = expected_keys - actual_keys
    extra = actual_keys - expected_keys
    assert not missing, f"{url}: typed response dropped keys present in the raw handler dict: {sorted(missing)}"
    assert not extra, f"{url}: typed response introduced keys absent from the raw handler dict: {sorted(extra)}"
    assert actual.get("available") == expected_dict.get("available")


# ---------------------------------------------------------------------------
# GET /api/quant/portfolio/risk
# ---------------------------------------------------------------------------


def test_portfolio_risk_unavailable_branch_wire_shape():
    db = _memory_db()
    db.add(User(username="u1", password_hash="x"))
    db.commit()
    client = _client(db)

    import app.interface.api.quant.portfolio as portfolio_mod

    orig = PortfolioPriceService.price_matrix
    PortfolioPriceService.price_matrix = lambda self: ({}, {}, {"priced_assets": 0})
    try:
        user = db.query(User).first()
        expected = portfolio_mod.portfolio_quant_risk(
            benchmark=None, confidence=0.95, risk_free=None, db=db, user=user
        )
        _assert_wire_shape_matches(client, "/api/quant/portfolio/risk", expected)
    finally:
        PortfolioPriceService.price_matrix = orig


def test_portfolio_risk_available_branch_wire_shape():
    db = _memory_db()
    db.add(User(username="u2", password_hash="x"))
    db.commit()
    client = _client(db)

    import app.interface.api.quant.portfolio as portfolio_mod

    matrix, weights, diagnostics = _synthetic_matrix()
    orig = PortfolioPriceService.price_matrix
    PortfolioPriceService.price_matrix = lambda self: (matrix, weights, diagnostics)
    try:
        user = db.query(User).first()
        expected = portfolio_mod.portfolio_quant_risk(
            benchmark=None, confidence=0.95, risk_free=0.02, db=db, user=user
        )
        _assert_wire_shape_matches(client, "/api/quant/portfolio/risk", expected)
    finally:
        PortfolioPriceService.price_matrix = orig


# ---------------------------------------------------------------------------
# GET /api/quant/portfolio/real/summary and /api/quant/portfolio/real/risk
# ---------------------------------------------------------------------------


def test_real_summary_and_risk_unavailable_no_holdings_wire_shape():
    db = _memory_db()
    db.add(User(username="u3", password_hash="x"))
    db.commit()
    client = _client(db)

    import app.interface.api.quant.portfolio as portfolio_mod

    user = db.query(User).first()
    expected_summary = portfolio_mod.real_portfolio_summary(
        benchmark="SPY", lookback_days=365, db=db, user=user
    )
    expected_risk = portfolio_mod.real_portfolio_risk(
        benchmark="SPY", lookback_days=365, db=db, user=user
    )
    _assert_wire_shape_matches(client, "/api/quant/portfolio/real/summary", expected_summary)
    _assert_wire_shape_matches(client, "/api/quant/portfolio/real/risk", expected_risk)


def test_real_summary_and_risk_unavailable_no_price_data_wire_shape():
    db = _memory_db()
    db.add(User(username="u4", password_hash="x"))
    db.commit()
    user = db.query(User).first()
    _seed_real_holdings(db, user.id)
    client = _client(db)

    import app.interface.api.quant.portfolio as portfolio_mod

    orig_build = bridge_mod.build_real_price_matrix
    bridge_mod.build_real_price_matrix = lambda db, uid, lookback_days=365: None
    try:
        expected_summary = portfolio_mod.real_portfolio_summary(
            benchmark="SPY", lookback_days=365, db=db, user=user
        )
        expected_risk = portfolio_mod.real_portfolio_risk(
            benchmark="SPY", lookback_days=365, db=db, user=user
        )
        _assert_wire_shape_matches(client, "/api/quant/portfolio/real/summary", expected_summary)
        _assert_wire_shape_matches(client, "/api/quant/portfolio/real/risk", expected_risk)
    finally:
        bridge_mod.build_real_price_matrix = orig_build


def test_real_summary_and_risk_available_branch_wire_shape():
    db = _memory_db()
    db.add(User(username="u5", password_hash="x"))
    db.commit()
    user = db.query(User).first()
    _seed_real_holdings(db, user.id)
    client = _client(db)

    import app.interface.api.quant.portfolio as portfolio_mod

    df = _real_price_df()
    orig_build = bridge_mod.build_real_price_matrix
    orig_hist = market_service.history
    bridge_mod.build_real_price_matrix = lambda db, uid, lookback_days=365: df
    market_service.history = _fake_history
    try:
        expected_summary = portfolio_mod.real_portfolio_summary(
            benchmark="SPY", lookback_days=365, db=db, user=user
        )
        expected_risk = portfolio_mod.real_portfolio_risk(
            benchmark="SPY", lookback_days=365, db=db, user=user
        )
        assert expected_summary["available"] is True
        assert expected_risk["available"] is True
        _assert_wire_shape_matches(client, "/api/quant/portfolio/real/summary", expected_summary)
        _assert_wire_shape_matches(client, "/api/quant/portfolio/real/risk", expected_risk)
    finally:
        bridge_mod.build_real_price_matrix = orig_build
        market_service.history = orig_hist
