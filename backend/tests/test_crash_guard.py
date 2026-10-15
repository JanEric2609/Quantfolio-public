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


def _held_now(db, user, isin="IE00B4L5Y983", qty=100):
    """The broker's current position, as the last sync left it."""
    from app.foundation.models.entities import BrokerPosition

    account = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user.id).first()
    if account is None:
        account = ConnectedAccount(user_id=user.id, source="scalable", name="Depot", account_type="depot")
        db.add(account)
        db.commit()
    db.add(BrokerPosition(user_id=user.id, connected_account_id=account.id, source="scalable",
                          isin=isin, name=isin, quantity=qty))
    db.commit()


def _snapshot(db, user, day, isin="IE00B4L5Y983", qty=100):
    db.add(BookPositionSnapshot(
        user_id=user.id, snapshot_date=day, source="scalable", account_id="a", isin=isin, name=isin, quantity=qty,
    ))
    db.commit()


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
        _snapshot(db, user, start + timedelta(days=offset), qty=qty)
    _held_now(db, user, qty=60)
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


def test_selling_the_whole_core_is_flagged(monkeypatch):
    # A fully sold position is gone from the broker's positions and the day's snapshot.
    db = _memory_db()
    user = _user(db)
    start = date(2026, 1, 1)
    _snapshot(db, user, start + timedelta(days=1))
    _snapshot(db, user, start + timedelta(days=2))
    _held_now(db, user, isin="IE00BKM4GZ66", qty=5)  # something else is still held
    _patch_prices(monkeypatch, [100, 110, 90, 85], start)
    findings = crash_guard.crash_guard(db, user.id)["behaviour"]
    assert [f["kind"] for f in findings] == ["core_sold"]
    assert "100%" in findings[0]["title"]


def test_a_day_without_a_sync_is_not_a_sale(monkeypatch):
    db = _memory_db()
    user = _user(db)
    start = date(2026, 1, 1)
    _snapshot(db, user, start + timedelta(days=1))
    _snapshot(db, user, start + timedelta(days=2))
    _held_now(db, user)
    _patch_prices(monkeypatch, [100, 110, 90, 85, 84, 83], start)  # prices go on; the book was not recorded
    assert crash_guard.crash_guard(db, user.id)["behaviour"] == []


def test_a_wealth_page_load_is_not_a_sale(monkeypatch):
    # Loading Home writes the day's dkb_mirror PortfolioSnapshot without book rows (pullfrog on #19).
    from app.foundation.models.entities import PortfolioSnapshot

    db = _memory_db()
    user = _user(db)
    start = date(2026, 1, 1)
    _snapshot(db, user, start + timedelta(days=1))
    _held_now(db, user)
    db.add(PortfolioSnapshot(user_id=user.id, date=start + timedelta(days=3), source="dkb_mirror"))
    db.commit()
    _patch_prices(monkeypatch, [100, 110, 90, 85], start)
    assert crash_guard.crash_guard(db, user.id)["behaviour"] == []

def test_missing_prices_are_not_a_recovery(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)

    _patch_prices(monkeypatch, [100, 75])
    crash_guard.run_daily(db, now=now)
    _patch_prices(monkeypatch, [])               # an empty price cache
    assert crash_guard.run_daily(db, now=now)["skipped"] == "no_prices"
    _patch_prices(monkeypatch, [100, 75, 74])    # prices back, market still down
    crash_guard.run_daily(db, now=now)

    assert len(sent) == 1 and "bear market" in sent[0]
    assert crash_guard._load_state(db)["worst_level"] == "bear"


def test_a_fall_of_exactly_ten_percent_is_an_episode():
    closes = _series([100, 90, 100])
    assert market_state_from("X", closes[:2]).level == "correction"
    [episode] = find_episodes(closes)
    assert episode.depth == 0.10 and episode.recovery_date is not None


def test_a_new_high_missed_between_runs_resets_the_alerts(monkeypatch):
    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)

    _patch_prices(monkeypatch, [100, 75])
    crash_guard.run_daily(db, now=now)
    # The run on the new high was missed; the market already fell 14 % from it.
    _patch_prices(monkeypatch, [100, 75, 105, 90])
    result = crash_guard.run_daily(db, now=now)

    assert result["sent"] == ["recovered", "level:correction"]
    assert "back at its high" in sent[1] and "correction" in sent[2]


def test_swapping_one_core_etf_for_another_is_still_a_sale(monkeypatch):
    db = _memory_db()
    user = _user(db)
    monkeypatch.setattr("app.decision.monthly_plan.plan_settings",
                        lambda db: {"core_isins": ["IE00B4L5Y983", "IE00BKM4GZ66"]})
    start = date(2026, 1, 1)
    for offset, world, em in ((1, 100, 0), (2, 50, 50)):
        for isin, qty in (("IE00B4L5Y983", world), ("IE00BKM4GZ66", em)):
            if qty:
                _snapshot(db, user, start + timedelta(days=offset), isin=isin, qty=qty)
    _held_now(db, user, "IE00B4L5Y983", 50)
    _held_now(db, user, "IE00BKM4GZ66", 50)
    _patch_prices(monkeypatch, [100, 110, 90, 85], start)
    findings = crash_guard.crash_guard(db, user.id)["behaviour"]
    assert [f["id"] for f in findings] == ["core-sold-IE00B4L5Y983"]
    assert "50%" in findings[0]["title"]


def test_one_admins_finding_does_not_silence_anothers(monkeypatch):
    db = _memory_db()
    first = _user(db)
    second = User(username="second", password_hash="!", role="admin")
    db.add(second)
    db.commit()
    _with_telegram(db, first)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)
    seller = {"id": first.id}
    finding = {"id": "core-sold-X", "kind": "core_sold", "title": "Core sold", "detail": "d"}
    monkeypatch.setattr(crash_guard, "behaviour",
                        lambda db, user_id, state: [finding] if user_id == seller["id"] else [])
    _patch_prices(monkeypatch, [100, 85])
    crash_guard.run_daily(db, now=now)
    seller["id"] = second.id  # the next day the other admin sells
    crash_guard.run_daily(db, now=now)
    assert sum("Core sold" in text for text in sent) == 2


def test_the_benchmark_is_read_in_eur(monkeypatch):
    calls = []

    def fake_eur_closes(db, symbol, days=730, **kw):
        calls.append((symbol, kw.get("allow_live")))
        return {"2026-01-02": 90.0, "2026-01-01": 100.0}

    monkeypatch.setattr("app.foundation.eur_prices.eur_closes", fake_eur_closes)
    closes = crash_guard._closes(_memory_db(), "IWDA.L")
    assert calls == [("IWDA.L", False)]
    assert closes == [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 90.0)]


def test_alerts_name_the_configured_benchmark(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    _patch_prices(monkeypatch, [100, 85])
    crash_guard.run_daily(db, now=datetime(2026, 10, 15, 7, tzinfo=UTC))
    assert "MSCI World is 15% below" in sent[0]

    upsert_public_settings(db, {"benchmark_ticker": "VWCE.DE"})
    assert crash_guard.crash_guard(db, user.id)["message"].startswith("VWCE.DE is 15% below")


def test_changing_the_benchmark_starts_the_alert_state_over(monkeypatch):
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = _user(db)
    _with_telegram(db, user)
    sent = _capture(monkeypatch)
    now = datetime(2026, 10, 15, 7, tzinfo=UTC)

    _patch_prices(monkeypatch, [100, 75])                  # MSCI World: bear market
    crash_guard.run_daily(db, now=now)
    upsert_public_settings(db, {"benchmark_ticker": "VWCE.DE"})
    _patch_prices(monkeypatch, [100, 110, 80])             # the new index: also a bear market (27 %)
    result = crash_guard.run_daily(db, now=now)

    assert result["sent"] == ["level:bear"]                # its own alert, no false recovery
    assert "VWCE.DE" in sent[-1]
    assert crash_guard._load_state(db)["ticker"] == "VWCE.DE"


def _band_row(db, day, loss, var975=0.02, ticker="EUNL.DE"):
    from app.foundation.models.entities import RiskForecastEntry, RiskForecastScore

    f = RiskForecastEntry(target=f"benchmark:{ticker}", label="Benchmark", forecast_day=day - timedelta(days=1),
                          horizon_days=1, model_version="t", model_hash="t", sigma=0.01, var95=0.016,
                          var975=var975, es975=0.025, n_obs=700, n_calibration=230, weights_json={ticker: 1.0})
    db.add(f)
    db.flush()
    db.add(RiskForecastScore(forecast_id=f.id, end_day=day, realized_return=-loss, loss=loss,
                             var95_breach=loss > 0.016, var_e=0.0, es_e=0.0))
    db.commit()


def test_a_fall_beyond_the_risk_band_is_quoted(monkeypatch):
    db = _memory_db()
    _patch_prices(monkeypatch, [100.0] * 5 + [85.0])
    last = date(2026, 1, 6)
    assert crash_guard.market_state(db).as_of == last.isoformat()

    _band_row(db, last, loss=0.031)
    out = crash_guard.crash_guard(db, _user(db).id)
    assert "beyond the app's own 97.5 % risk band (2.0%)" in out["message"]
    assert "3.1%" in out["message"]


def test_an_old_or_ordinary_day_is_not_quoted(monkeypatch):
    db = _memory_db()
    _patch_prices(monkeypatch, [100.0] * 5 + [85.0])
    _band_row(db, date(2026, 1, 5), loss=0.05)  # scored, but not on the latest close
    assert "risk band" not in crash_guard.crash_guard(db, _user(db).id)["message"]
    _band_row(db, date(2026, 1, 6), loss=0.012)  # within the band
    assert crash_guard.market_state(db).band["beyond"] is False
    assert "risk band" not in crash_guard.crash_guard(db, db.query(User).first().id)["message"]


def test_a_failed_band_query_leaves_the_session_usable(monkeypatch):
    db = _memory_db()
    _patch_prices(monkeypatch, [100.0] * 5 + [85.0])
    rolled: list[bool] = []
    real_rollback = db.rollback
    monkeypatch.setattr(db, "rollback", lambda: rolled.append(True) or real_rollback())

    def broken(*a, **kw):
        raise RuntimeError("risk_forecasts missing")

    monkeypatch.setattr("app.lab.risk_forecast.latest_day_against_band", broken)
    out = crash_guard.crash_guard(db, _user(db).id)
    assert rolled and "risk band" not in out["message"]
    assert "band" not in out["market"]
