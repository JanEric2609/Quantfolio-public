"""Tests for the admin data-repair endpoint (Wave 2, task T1.5).

Covers the T1.5 contract: junk FinTS wire names ("TERED SHS USD (ACC) O.N.")
repaired from Asset rows / security master, real names never overwritten,
DEC-E retroactive asset-type propagation onto stale Holding rows, dry-run
byte-identical guarantee, external-lookup opt-in behaviour, and the admin
auth gate.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DkbPosition, Holding

JUNK_NAME = "TERED SHS USD (ACC) O.N."
REAL_NAME = "iShares Core MSCI World UCITS ETF USD (Acc)"
ISIN_WORLD = "IE00B4L5Y983"
TICKER_WORLD = "SWDA.MI"


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _user(db, role: str = "admin"):
    from app.foundation.models.entities import User

    user = User(username="repair-admin", password_hash="dummy", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _portfolio(db, user_id: str):
    from app.foundation.models.entities import Portfolio

    portfolio = Portfolio(user_id=user_id, name="Main Portfolio", currency="EUR")
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


def _account(db, user_id: str):
    from app.foundation.models.entities import DkbAccount

    account = DkbAccount(user_id=user_id, type="security", iban="DE02120300000000202051")
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _position(db, account_id: str, isin: str, name: str, ticker: str | None):
    from decimal import Decimal

    from app.foundation.models.entities import DkbPosition

    pos = DkbPosition(
        account_id=account_id,
        isin=isin,
        ticker=ticker,
        name=name,
        quantity=Decimal("10"),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _asset(db, isin: str, symbol: str | None, name: str, asset_type: str = "etf"):
    from app.foundation.models.entities import Asset

    asset = Asset(isin=isin, symbol=symbol, exchange="XETRA", name=name, asset_type=asset_type)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _holding(db, portfolio_id: str, isin: str | None, name: str, asset_type: str = "stock"):
    from decimal import Decimal

    from app.foundation.models.entities import Holding

    holding = Holding(
        portfolio_id=portfolio_id,
        isin=isin,
        ticker=None,
        name=name,
        asset_type=asset_type,
        quantity=Decimal("5"),
    )
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return holding


def _api_client(db, user):
    """TestClient with the DB session and authenticated user injected."""
    from app.foundation.core.db import get_db
    from app.main import app
    from app.foundation.auth import current_user

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()


def _db_snapshot(db):
    """Plain-value snapshot of every repairable column, for byte-identity checks."""
    from app.foundation.models.entities import DkbPosition, Holding

    positions = sorted(
        (p.name, p.ticker) for p in db.query(DkbPosition).all()
    )
    holdings = sorted((h.name, h.ticker, h.asset_type) for h in db.query(Holding).all())
    return {"positions": positions, "holdings": holdings}


# --- Name/ticker repair -----------------------------------------------------


def test_junk_name_repaired_via_asset_then_idempotent(monkeypatch):
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, JUNK_NAME, None)
    _asset(db, ISIN_WORLD, TICKER_WORLD, REAL_NAME)

    # External lookup must stay out of the picture when an Asset row exists.
    def _fail(*args, **kwargs):
        raise AssertionError("resolve_security must not be called when an Asset row exists")

    monkeypatch.setattr("app.interface.api.admin_repair.resolve_security", _fail)

    client = _api_client(db, admin)
    try:
        preview = client.post("/api/admin/repair/dkb-data", json={"dry_run": True})
        assert preview.status_code == 200
        report = preview.json()
        assert report["dry_run"] is True
        assert report["positions_repaired"] == 1
        entry = report["positions"][0]
        assert entry["isin"] == ISIN_WORLD
        assert entry["before"] == {"name": JUNK_NAME, "ticker": None}
        assert entry["after"] == {"name": REAL_NAME, "ticker": TICKER_WORLD}
        assert entry["repaired_via"] == "asset"
        # Dry run proposed but changed nothing.
        assert _db_snapshot(db)["positions"] == [(JUNK_NAME, None)]

        apply_run = client.post("/api/admin/repair/dkb-data", json={"dry_run": False})
        assert apply_run.status_code == 200
        applied = apply_run.json()
        assert applied["dry_run"] is False
        assert applied["positions"][0]["after"]["name"] == REAL_NAME

        db.expire_all()
        stored = db.query(DkbPosition).first()
        assert stored.name == REAL_NAME
        assert stored.ticker == TICKER_WORLD

        # Second run: nothing left to repair (idempotent).
        rerun = client.post("/api/admin/repair/dkb-data", json={"dry_run": True})
        assert rerun.status_code == 200
        rerun_report = rerun.json()
        assert rerun_report["positions"] == []
        assert rerun_report["positions_repaired"] == 0
        assert all(count == 0 for count in rerun_report["asset_type_changes"].values())
    finally:
        _clear_overrides()


def test_real_named_position_untouched_even_when_asset_differs():
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, REAL_NAME, TICKER_WORLD)
    _asset(db, ISIN_WORLD, "OTHER.SY", "Totally Different Name")

    client = _api_client(db, admin)
    try:
        resp = client.post("/api/admin/repair/dkb-data", json={"dry_run": False})
        assert resp.status_code == 200
        report = resp.json()
        assert report["positions"] == []
        assert report["positions_repaired"] == 0

        db.expire_all()
        stored = db.query(DkbPosition).first()
        assert stored.name == REAL_NAME
        assert stored.ticker == TICKER_WORLD
    finally:
        _clear_overrides()


def test_real_name_kept_when_only_ticker_missing():
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, REAL_NAME, None)
    _asset(db, ISIN_WORLD, TICKER_WORLD, "Curated Name That Must Not Win")

    client = _api_client(db, admin)
    try:
        resp = client.post("/api/admin/repair/dkb-data", json={"dry_run": False})
        assert resp.status_code == 200
        entry = resp.json()["positions"][0]
        assert entry["repaired_via"] == "asset"
        assert entry["after"]["name"] == REAL_NAME  # real name survives
        assert entry["after"]["ticker"] == TICKER_WORLD  # only ticker filled

        db.expire_all()
        stored = db.query(DkbPosition).first()
        assert stored.name == REAL_NAME
        assert stored.ticker == TICKER_WORLD
    finally:
        _clear_overrides()


# --- DEC-E retroactive asset-type propagation -------------------------------


def test_propagate_retypes_stale_holdings_and_skips_unknown_isins():
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    portfolio = _portfolio(db, admin.id)
    isin_known = "IE00B4L5Y983"
    isin_unknown = "US0378331005"
    _position(db, account.id, isin_known, JUNK_NAME, None)
    _asset(db, isin_known, TICKER_WORLD, REAL_NAME, asset_type="etf")
    stale = _holding(db, portfolio.id, isin_known, "Stale Typed Holding", asset_type="stock")
    unknown = _holding(db, portfolio.id, isin_unknown, "No Asset Evidence", asset_type="stock")

    client = _api_client(db, admin)
    try:
        resp = client.post("/api/admin/repair/dkb-data", json={"dry_run": False})
        assert resp.status_code == 200
        changes = resp.json()["asset_type_changes"]
        assert changes[isin_known] == 1
        assert changes[isin_unknown] == 0  # no Asset evidence → untouched (DEC-E)

        db.expire_all()
        assert db.get(Holding, stale.id).asset_type == "etf"
        assert db.get(Holding, unknown.id).asset_type == "stock"
    finally:
        _clear_overrides()


# --- Dry-run byte-identity ---------------------------------------------------


def test_dry_run_leaves_db_byte_identical():
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    portfolio = _portfolio(db, admin.id)
    _position(db, account.id, ISIN_WORLD, JUNK_NAME, None)
    _asset(db, ISIN_WORLD, TICKER_WORLD, REAL_NAME, asset_type="etf")
    _holding(db, portfolio.id, ISIN_WORLD, "Stale", asset_type="stock")

    before = _db_snapshot(db)

    client = _api_client(db, admin)
    try:
        resp = client.post("/api/admin/repair/dkb-data", json={"dry_run": True})
        assert resp.status_code == 200
        report = resp.json()
        assert report["dry_run"] is True
        assert report["positions_repaired"] == 1
        assert report["asset_type_changes"][ISIN_WORLD] == 1  # computed, not applied

        db.expire_all()
        assert _db_snapshot(db) == before
    finally:
        _clear_overrides()


# --- External lookup gating --------------------------------------------------


def test_external_lookup_disabled_by_default_never_calls_security_master(monkeypatch):
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, JUNK_NAME, None)  # no Asset row exists

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("resolve_security called despite use_external_lookup=False")

    monkeypatch.setattr("app.interface.api.admin_repair.resolve_security", _must_not_be_called)

    client = _api_client(db, admin)
    try:
        # Empty body: both flags at their defaults.
        resp = client.post("/api/admin/repair/dkb-data")
        assert resp.status_code == 200
        report = resp.json()
        assert report["use_external_lookup"] is False
        assert report["positions"][0]["repaired_via"] == "skipped"

        db.expire_all()
        stored = db.query(DkbPosition).first()
        assert stored.name == JUNK_NAME  # unchanged
    finally:
        _clear_overrides()


def test_external_lookup_enabled_applies_security_master(monkeypatch):
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, JUNK_NAME, None)  # no Asset row

    from app.foundation.models.entities import Security, SecurityListing

    security = Security(
        isin=ISIN_WORLD,
        canonical_name=REAL_NAME,
        asset_type="etf",
        base_currency="EUR",
    )
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XETR",
            exchange_label="XETRA",
            symbol=TICKER_WORLD,
            currency="EUR",
            is_primary=True,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.interface.api.admin_repair.resolve_security",
        lambda db_, *, isin, **kwargs: security,
    )

    client = _api_client(db, admin)
    try:
        resp = client.post(
            "/api/admin/repair/dkb-data",
            json={"dry_run": False, "use_external_lookup": True},
        )
        assert resp.status_code == 200
        entry = resp.json()["positions"][0]
        assert entry["repaired_via"] == "security_master"
        assert entry["after"] == {"name": REAL_NAME, "ticker": TICKER_WORLD}

        db.expire_all()
        from app.foundation.models.entities import DkbPosition

        stored = db.query(DkbPosition).first()
        assert stored.name == REAL_NAME
        assert stored.ticker == TICKER_WORLD
    finally:
        _clear_overrides()


# --- Auth gate ---------------------------------------------------------------


def test_endpoints_require_authentication():
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    try:
        assert client.get("/api/admin/repair/dkb-data").status_code == 401
        assert client.post("/api/admin/repair/dkb-data", json={}).status_code == 401
    finally:
        _clear_overrides()


def test_endpoints_reject_non_admin():
    db = _memory_db()
    plain = _user(db, role="user")
    client = _api_client(db, plain)
    try:
        get_resp = client.get("/api/admin/repair/dkb-data?dry_run=true")
        post_resp = client.post("/api/admin/repair/dkb-data", json={"dry_run": True})
        assert get_resp.status_code == 403
        assert post_resp.status_code == 403
        # main.py wraps HTTPException bodies in {"error": {"code", "message"}}.
        assert get_resp.json()["error"]["message"] == "Admin role required"
    finally:
        _clear_overrides()


def test_get_alias_is_read_only_preview():
    db = _memory_db()
    admin = _user(db)
    account = _account(db, admin.id)
    _position(db, account.id, ISIN_WORLD, JUNK_NAME, None)
    _asset(db, ISIN_WORLD, TICKER_WORLD, REAL_NAME)

    before = _db_snapshot(db)

    client = _api_client(db, admin)
    try:
        resp = client.get("/api/admin/repair/dkb-data?dry_run=true")
        assert resp.status_code == 200
        report = resp.json()
        assert report["dry_run"] is True
        assert report["positions_repaired"] == 1

        db.expire_all()
        assert _db_snapshot(db) == before
    finally:
        _clear_overrides()
