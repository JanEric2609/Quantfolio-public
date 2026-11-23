"""The intraday board snapshot and its API (ADR 0027 amendment 2026-11-23, commit 6)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, flag, make_user, standard_handler
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, cfd_journal, intraday, news
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.settings import get_setting_row

ES50_ETF = "IE0008471009"
AT = NOW.replace(minute=7)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _sc(log, at=AT):
    quotes = {DAX_ETF: ("100.5", "100.5"), ES50_ETF: ("100.5", "100.5")}
    std = standard_handler(quotes, chart_pts=daily_points(now=at), now=at)

    def handler(args):
        log.append(args)
        if args[:2] == ["broker", "chart"] and flag(args, "--timeframe") == "7d":
            return {"data_points": [{"mid_price": 100, "timestamp_utc": "2026-11-17T16:30:00Z"},
                                    {"mid_price": 101, "timestamp_utc": "2026-11-18T08:30:00Z"}]}
        return std(args)
    return FakeSc(handler).cli()


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(news, "alpaca_for", lambda db: None)
    monkeypatch.setattr(cfd, "_eur_per_usd", lambda db, now: Decimal("0.9"))
    monkeypatch.setattr(cfd, "_index_prev_close", lambda db, ticker, day: Decimal("25000"))


def test_board_fields_one_failing_index_and_the_daily_context_is_cached():
    db = _memory_db()
    make_user(db)
    log: list = []
    board = intraday.build_board(db, _sc(log), AT)
    by = {c["key"]: c for c in board["indices"]}
    dax = by["DAX"]
    assert dax.get("error") is None, dax
    assert dax["t212_name"] == "Germany 40 (GER40)" and dax["session"]["status"] == "open"
    assert dax["enter_at"].startswith("2026-11-18T17:00") and dax["exit_at"].startswith("2026-11-18T17:27")
    assert Decimal(dax["move_pct"]) == Decimal("0.5") and Decimal(dax["index_level_est"]) == Decimal("25125.0")
    assert Decimal(dax["sigma_points"]) > 0 and Decimal(dax["spread_points"]) == Decimal("0.5") / 10000 * Decimal("25125.0")
    assert dax["eur_per_unit"] == "1" and dax["cfd_today"] is None and dax["skip_reason"] is None
    # The US indices have no Alpaca key here: they fail alone, the others stay intact.
    assert by["SPX"]["error"] == "no_data" and "index_level_est" not in by["SPX"]
    assert "index_level_est" in by["ES50"]
    intraday.store_board(db, board)
    db.commit()
    first_reads = len([a for a in log if a[:2] == ["broker", "chart"] and flag(a, "--timeframe") == "3m"])
    assert first_reads >= 1
    log.clear()
    later = AT + timedelta(minutes=5)
    board2 = intraday.build_board(db, _sc(log, later), later)
    assert [a for a in log if a[:2] == ["broker", "chart"]] == []  # the day's context is cached: no ctx reads
    assert next(c for c in board2["indices"] if c["key"] == "DAX")["index_level_est"] == dax["index_level_est"]
    stored = get_setting_row(db, intraday.BOARD_KEY)
    assert stored["day"] == "2026-11-18" and "DAX" in stored["ctx"]


def test_the_board_key_is_internal_not_a_public_setting():
    from app.foundation.settings import DEFAULT_PUBLIC_SETTINGS
    from app.foundation.settings_catalog import PAGES

    assert intraday.BOARD_KEY not in DEFAULT_PUBLIC_SETTINGS
    assert intraday.BOARD_KEY not in repr(PAGES)


def test_monitor_tick_writes_the_board_only_on_minute_2_mod_5():
    from app.decision.desk import jobs

    db = _memory_db()
    make_user(db)
    jobs.monitor_tick(db, NOW.replace(minute=3), _sc([]))
    assert get_setting_row(db, intraday.BOARD_KEY) is None
    jobs.monitor_tick(db, AT, _sc([]))
    assert get_setting_row(db, intraday.BOARD_KEY)["as_of"].startswith("2026-11-18T10:07")


# --- API -----------------------------------------------------------------------------


@pytest.fixture
def api():
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    admin, guest = make_user(db, "admin", "owner"), make_user(db, "user", "guest")
    state = {"user": admin}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: state["user"]
    try:
        yield TestClient(app), db, admin, guest, state
    finally:
        app.dependency_overrides.clear()


def _journal_row(db, user):
    ctx = cfd.CfdContext(
        key="DAX", r1=Decimal("0.004"), proxy_prev_close=Decimal(100), index_prev_close=Decimal(25000),
        index_level_est=Decimal(25000), currency="EUR", eur_per_unit=Decimal(1), sigma=Decimal("0.01"),
        session_minutes=510, enter_by=NOW, exit_at=NOW, exposure_eur=Decimal("500.00"),
        quantity_t212=Decimal("0.02"), stop_points=Decimal(250), typical_30m_points=Decimal(60),
        spread_points=Decimal(1), margin_eur=Decimal(25), overnight_eur_per_night=Decimal("0.04"))
    p = cfd.record_signal(db, user.id, "DAX", ALIASES["DAX"], "long", NOW + timedelta(minutes=27), NOW,
                          Decimal(100), Decimal("0.01"), context=ctx)
    db.commit()
    return p


def test_intraday_routes_are_admin_only(api):
    client, _db, _admin, guest, state = api
    state["user"] = guest
    assert client.get("/api/desk/intraday").status_code == 403
    assert client.post("/api/desk/cfd/x/journal", json={"action": "skipped"}).status_code == 403


def test_get_intraday_shape(api):
    client, db, admin, _g, _s = api
    p = _journal_row(db, admin)
    body = client.get("/api/desk/intraday").json()
    assert body["board"] is None and body["board_age_s"] is None
    assert body["t212"]["margin_rate"] == "0.05" and body["t212"]["spread_bp"]["DAX"] == "0.5"
    assert body["t212"]["quantity_decimals"] == "4"
    row = body["journal"][0]
    assert row["position_id"] == p.id and row["status"] == "sent" and row["quantity_suggested"] == "0.02000000"
    intraday.store_board(db, {"as_of": "2026-11-18T10:07:00+00:00", "indices": [], "ctx": {"DAX": {}}})
    db.commit()
    body = client.get("/api/desk/intraday").json()
    assert "ctx" not in body["board"] and body["board_age_s"] is not None


def test_post_journal_flow_and_errors(api):
    client, db, admin, guest, state = api
    p = _journal_row(db, admin)
    url = f"/api/desk/cfd/{p.id}/journal"
    r = client.post(url, json={"action": "entered", "level": "25400.5", "quantity": "0.0113", "note": "filled"})
    assert r.status_code == 200 and r.json()["status"] == "entered" and r.json()["stop_level"] == "25150.500000"
    r = client.post(url, json={"action": "closed", "level": "25450"})
    assert r.status_code == 200 and r.json()["real_pnl_eur"] is not None and r.json()["note"] == "filled"
    assert client.post(url, json={"action": "skipped"}).status_code == 422  # closed: cannot skip
    assert client.post(url, json={"action": "entered", "level": "99999999"}).status_code == 422  # not 20 % near
    assert client.post(url, json={"action": "bogus"}).status_code == 422
    assert client.post(url, json={"action": "entered", "quantity": "NaN"}).status_code == 422
    assert client.post("/api/desk/cfd/unknown/journal", json={"action": "skipped"}).status_code == 404
    state["user"] = guest
    assert client.post(url, json={"action": "skipped"}).status_code == 403
    assert cfd_journal.for_position(db, p.id).status == "closed"


def test_another_admins_trade_is_404(api):
    client, db, admin, _g, state = api
    p = _journal_row(db, admin)
    state["user"] = make_user(db, "admin", "second")
    assert client.post(f"/api/desk/cfd/{p.id}/journal", json={"action": "skipped"}).status_code == 404
