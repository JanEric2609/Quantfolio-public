"""The desk API (admin only, no order endpoint) and its Control Center items (ADR 0027)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, memory_db, standard_handler
from fastapi.testclient import TestClient

from app.decision.desk import ledger, prereg
from app.foundation import control_center
from app.foundation.models.entities import DeskEvent, DeskPosition
from app.foundation.scalable import service as scalable_service


@pytest.fixture
def api(monkeypatch):
    from app.decision.desk import news
    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(news, "alpaca_for", lambda db: None)
    db = memory_db()
    admin = make_user(db, "admin", "owner")
    guest = make_user(db, "user", "guest")
    state = {"user": admin}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: state["user"]
    try:
        yield TestClient(app), db, admin, guest, state
    finally:
        app.dependency_overrides.clear()


def test_every_desk_route_is_admin_only(api):
    client, _db, _admin, guest, state = api
    state["user"] = guest
    for method, path in [("get", "/api/desk/status"), ("get", "/api/desk/positions"), ("get", "/api/desk/events"),
                         ("get", "/api/desk/scorecard"), ("get", "/api/desk/chain"), ("get", "/api/desk/news"),
                         ("post", "/api/desk/ideas"), ("post", "/api/desk/halt"), ("post", "/api/desk/resume"),
                         ("post", "/api/desk/positions/x/take"), ("post", "/api/desk/positions/x/close")]:
        resp = getattr(client, method)(path, **({"json": {"text": "DAX up", "nonce": "abcd"}} if method == "post" else {}))
        assert resp.status_code == 403, (path, resp.status_code)


def test_no_route_can_place_an_order(api):
    from app.main import app

    desk_paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/desk")]
    assert desk_paths
    assert not [p for p in desk_paths if any(w in p for w in ("order", "buy", "sell", "trade", "limit"))]


def test_status_shows_paper_banner_limits_and_chain(api):
    client, db, *_ = api
    prereg.register(db, now=NOW)
    body = client.get("/api/desk/status").json()
    assert body["mode"] == "paper" and "no real orders" in body["banner"]
    assert body["limits"]["pot"] == "250" and body["limits"]["max_loss_per_trade"] == "5"
    assert body["chain"] == {"ok": True, "first_bad_seq": None}
    assert body["usage_today"]["max_entries"] == 2 and body["halt"] == {"halted": False}


def test_halt_resume_and_idea_blocked_while_halted(api):
    client, db, *_ = api
    halted = client.post("/api/desk/halt", json={"reason": "testing"}).json()
    assert halted["halt"]["halted"] is True and halted["halt"]["by"].startswith("app:")
    assert client.post("/api/desk/ideas", json={"text": "DAX up"}).status_code == 409
    assert client.post("/api/desk/resume").json()["halt"]["halted"] is False
    kinds = [e["kind"] for e in client.get("/api/desk/events").json()]
    assert kinds == ["halt", "resume"]
    assert client.get("/api/desk/events", params={"after_seq": 1}).json()[0]["kind"] == "resume"
    assert client.get("/api/desk/events", params={"after_seq": 2**70}).status_code == 422  # beyond a bigint


def test_idea_then_confirm_fills_a_paper_position(api, monkeypatch):
    client, db, *_ = api
    import app.decision.desk.api_ops as ops

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(ops, "datetime", Fixed)
    sc = FakeSc(standard_handler({"DE000KO000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item("DE000KO000A1")]}, daily_points(), now=NOW))
    monkeypatch.setattr(scalable_service, "make_cli", lambda db: sc.cli())
    assert client.post("/api/desk/ideas", json={"text": "DAX banana"}).status_code == 422
    resp = client.post("/api/desk/ideas", json={"text": "DAX up today ko"})
    assert resp.status_code == 200, resp.text
    proposal = resp.json()["proposals"][0]
    pid, nonce = proposal["position"]["id"], proposal["nonce"]
    assert proposal["position"]["status"] == "proposed" and resp.json()["tap_window_s"] == 180
    assert client.post(f"/api/desk/positions/{pid}/take", json={"nonce": "wrong-nonce"}).json()["status"] == "unknown"
    taken = client.post(f"/api/desk/positions/{pid}/take", json={"nonce": nonce}).json()
    assert taken["status"] == "filled" and taken["position"]["status"] == "open"
    assert [p["id"] for p in client.get("/api/desk/positions", params={"status": "open"}).json()] == [pid]
    assert client.get("/api/desk/positions", params={"status": "bogus"}).status_code == 422
    closed = client.post(f"/api/desk/positions/{pid}/close").json()
    assert closed["closed"] is True and closed["position"]["exit_reason"] == "owner_close"
    assert client.get("/api/desk/chain").json()["ok"] is True
    card = client.get("/api/desk/scorecard").json()
    assert set(card["cells"]) == set(prereg.CELLS) and card["cells"]["owner_call:knockout"]["n"] == 0


def test_chain_endpoint_detects_a_changed_row(api):
    client, db, *_ = api
    ledger.append_event(db, "error", "paper", {"a": 1}, now=NOW)
    db.commit()
    assert client.get("/api/desk/chain").json() == {"ok": True, "first_bad_seq": None, "events": 1}
    row = db.query(DeskEvent).one()
    row.payload = '{"a":2}'
    db.commit()
    assert client.get("/api/desk/chain").json()["first_bad_seq"] == 1


# --- Control Center ---------------------------------------------------------------


def _ids(db, now=None):
    return {i["id"]: i for i in control_center.attention_items(db) if i["id"].startswith("desk-")}


def test_control_center_desk_items(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = memory_db()
    owner = make_user(db)
    assert _ids(db) == {}
    upsert_public_settings(db, {"desk_halt_json": {"halted": True, "reason": "x"},
                                "desk_health_json": {"wrapper_outdated": True, "macro_years": [2026]}})
    items = _ids(db)
    assert items["desk-halted"]["severity"] == "warning" and items["desk-wrapper"]["severity"] == "warning"
    ledger.append_event(db, "external_trade", "paper", {"ledger_id": "1"})
    db.commit()
    assert _ids(db)["desk-external"]["severity"] == "info"
    row = db.query(DeskEvent).one()
    row.payload = "{}"
    db.commit()
    assert _ids(db)["desk-chain-broken"]["severity"] == "error"
    # An open position without a quote for 10 minutes only warns inside trading hours.
    db.add(DeskPosition(user_id=owner.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin="DE000KO000A1", underlying_isin="DE0008469008", direction="long", status="open",
                        units=1, entry_price=Decimal(1), last_quote_at=datetime.now(UTC) - timedelta(minutes=30)))
    db.commit()
    from zoneinfo import ZoneInfo

    local = datetime.now(UTC).astimezone(ZoneInfo("Europe/Berlin"))
    in_hours = local.weekday() < 5 and 8 <= local.hour < 22
    assert ("desk-quote-lost" in _ids(db)) == in_hours


def test_control_center_macro_calendar_warning_only_in_december(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = memory_db()
    make_user(db)
    upsert_public_settings(db, {"desk_health_json": {"macro_years": [2026]}})

    class Dec(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 2, 12, tzinfo=UTC)

    monkeypatch.setattr(control_center, "datetime", Dec)
    assert "desk-macro-calendar" in _ids(db)
    upsert_public_settings(db, {"desk_health_json": {"macro_years": [2026, 2027]}})
    assert "desk-macro-calendar" not in _ids(db)


def test_news_wire_lists_newest_first_and_can_filter_sent(api):
    from app.foundation.models.entities import NewsWireItem

    client, db, *_ = api
    t = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    db.add(NewsWireItem(id="alpaca:1", source="alpaca", received_at=t, headline="old", symbols="NVDA",
                        relevance="holding:NVDA", materiality=1, alert=""))
    db.add(NewsWireItem(id="fed:a", source="fed", received_at=t + timedelta(minutes=1), headline="new",
                        relevance="central_bank", materiality=2, direction="down", alert="sent"))
    db.commit()
    rows = client.get("/api/desk/news").json()
    assert [r["headline"] for r in rows] == ["new", "old"] and rows[1]["symbols"] == ["NVDA"]
    assert [r["id"] for r in client.get("/api/desk/news?sent_only=true").json()] == ["fed:a"]
    assert client.get("/api/desk/news?limit=0").status_code == 422


def test_positions_carry_the_exact_instrument(api):
    client, db, admin, *_ = api
    db.add(DeskPosition(user_id=admin.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin="DE000HT4LYZ5", underlying_isin=DAX_ETF, direction="long", status="open",
                        product_name="S&P 500 Long HSBC", wkn="HT4LYZ", barrier_or_strike=Decimal("5812.34"),
                        leverage_or_omega=Decimal("4.8"), created_at=NOW))
    db.commit()
    row = client.get("/api/desk/positions").json()[0]
    assert (row["product_name"], row["wkn"], row["barrier_or_strike"], row["expiry"]) == (
        "S&P 500 Long HSBC", "HT4LYZ", "5812.340000", None)
