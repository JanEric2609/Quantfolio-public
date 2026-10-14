import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import crash_guard
from app.decision.crash_guard import find_episodes, level_for, market_state_from
from app.foundation.core.db import Base
from app.foundation.models.entities import BookPositionSnapshot, ConnectedAccount, Notification, User


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _series(values, start=date(2020, 1, 1)):
    return [(start + timedelta(days=i), float(v)) for i, v in enumerate(values)]


# --- levels and episodes ---------------------------------------------------------


def test_levels():
    assert level_for(0.05) == ("calm", "Calm")
    assert level_for(0.10)[0] == "correction"
    assert level_for(0.25)[0] == "bear"
    assert level_for(0.31)[0] == "crash"


def test_a_recovered_fall_is_one_episode_with_its_way_back():
    closes = _series([100, 90, 80, 85, 95, 100, 105])
    [episode] = find_episodes(closes)
    assert episode.depth == 0.2
    assert episode.peak_date == "2020-01-01"
    assert episode.trough_date == "2020-01-03"
    assert episode.recovery_date == "2020-01-06"


def test_shallow_dips_are_not_episodes():
    assert find_episodes(_series([100, 95, 99, 92, 101])) == []


def test_an_open_fall_has_no_recovery():
    [episode] = find_episodes(_series([100, 120, 100, 90]))
    assert episode.peak_date == "2020-01-02"
    assert episode.recovery_date is None and episode.months_to_recover is None
    assert episode.depth == 0.25


def test_state_measures_from_the_all_time_high():
    state = market_state_from("EUNL.DE", _series([100, 120, 96]))
    assert state.drawdown == 0.2
    assert state.level == "bear"
    assert state.peak_date == "2020-01-02"


def test_no_prices_is_calm():
    state = market_state_from("EUNL.DE", [])
    assert state.level == "calm" and state.drawdown == 0.0


# --- what the investor did ---------------------------------------------------------


def _user(db, role="admin"):
    user = User(username="owner", password_hash="!", role=role)
    db.add(user)
    db.commit()
    return user


def _patch_prices(monkeypatch, values, start=date(2026, 1, 1)):
    closes = _series(values, start)
    monkeypatch.setattr(crash_guard, "_closes", lambda db, ticker: closes)


def test_a_paused_savings_plan_during_a_fall_is_flagged(monkeypatch):
    db = _memory_db()
    user = _user(db)
    db.add(ConnectedAccount(
        user_id=user.id, source="scalable", name="Depot", account_type="depot",
        raw_json=json.dumps({"savings_plans": [
            {"isin": "IE00B4L5Y983", "name": "Core MSCI World", "amount": 500, "frequency": "MONTHLY", "paused": True},
        ]}),
    ))
    db.commit()
    _patch_prices(monkeypatch, [100, 110, 85])
    result = crash_guard.crash_guard(db, user.id)
    assert result["active"] is True
    kinds = [f["kind"] for f in result["behaviour"]]
    assert kinds == ["savings_plan_stopped"]


def test_core_units_sold_after_the_high_are_flagged(monkeypatch):
    db = _memory_db()
    user = _user(db)
    start = date(2026, 1, 1)
    for offset, qty in ((1, 100), (2, 100), (3, 60)):
        db.add(BookPositionSnapshot(
            user_id=user.id, snapshot_date=start + timedelta(days=offset), source="scalable",
            account_id="a", isin="IE00B4L5Y983", name="Core", quantity=qty,
        ))
    db.commit()
    _patch_prices(monkeypatch, [100, 110, 90, 85], start)
    findings = crash_guard.crash_guard(db, user.id)["behaviour"]
    assert [f["kind"] for f in findings] == ["core_sold"]
    assert "40%" in findings[0]["title"]


def test_nothing_is_flagged_when_markets_are_calm(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _patch_prices(monkeypatch, [100, 101, 99])
    result = crash_guard.crash_guard(db, user.id)
    assert result["active"] is False and result["behaviour"] == []


def test_the_owners_rule_is_shown_back(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    upsert_public_settings(db, {"crash_plan_note": "Sell nothing."})
    _patch_prices(monkeypatch, [100, 70])
    assert crash_guard.crash_guard(db, user.id)["plan_note"] == "Sell nothing."


# --- the daily alert ---------------------------------------------------------------


def _capture(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("app.foundation.telegram_bot.send_telegram_message",
                        lambda db, chat_id, text: sent.append(text) or True)
    return sent


def _with_telegram(db, user):
    from app.foundation.models.entities import TelegramAccount

    db.add(TelegramAccount(user_id=user.id, chat_id="7"))
    db.commit()


def test_alerts_once_per_new_worst_level(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)

    _patch_prices(monkeypatch, [100, 88])          # correction
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [100, 88, 79])      # bear
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [100, 88, 79, 82])  # back to correction: no message
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [100, 88, 79, 82, 79])  # bear again: no message
    crash_guard.run_daily(db, now=now)

    assert len(sent) == 2
    assert "correction" in sent[0] and "bear market" in sent[1]


def test_recovery_to_the_high_is_announced_once_and_resets(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)

    _patch_prices(monkeypatch, [100, 75])
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [100, 75, 95])   # still below the high: quiet
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [100, 75, 95, 101])
    crash_guard.run_daily(db, now=now)
    crash_guard.run_daily(db, now=now)          # no second "back at the high"

    assert len(sent) == 2
    assert "back at its high" in sent[1]
    assert db.query(Notification).filter(Notification.source == "crash_guard").count() == 2


def test_the_rule_goes_into_the_level_alert(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    upsert_public_settings(db, {"crash_plan_note": "Keep buying."})
    sent = _capture(monkeypatch)
    _patch_prices(monkeypatch, [100, 85])
    crash_guard.run_daily(db, now=datetime(2026, 10, 15, 7, tzinfo=UTC))
    assert "Keep buying." in sent[0]
