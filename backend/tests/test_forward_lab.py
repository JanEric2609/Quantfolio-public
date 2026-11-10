"""The nightly forward-test lab, its robustness checks and the overnight news reading (plan 2026-11-09)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import price_signals as ps
from app.foundation.core.db import Base
from app.foundation.models.entities import (
    ForwardLabDaily,
    ForwardLabSnapshot,
    IndexUniverseMember,
    NewsToneScore,
)
from app.lab.forward_lab import news_tone, robustness as rb, service


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# --- shared signal kernels ------------------------------------------------------------


def test_lab_signals_match_discovers_formation_measures():
    from app.decision.discover.pipeline import _largest_day_in_formation

    rng = np.random.default_rng(3)
    closes = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, 300))
    closes[150] *= 1.3  # one jump inside the formation window
    s = pd.Series(closes)
    mom = ps.momentum_12_1(closes)
    assert mom is not None
    discover = _largest_day_in_formation(s, pd.Series(pd.date_range("2025-01-01", periods=300)), mom)
    assert ps.information_discreteness(closes) == pytest.approx(discover["information_discreteness"], abs=1e-4)
    assert ps.momentum_without_largest_day(closes) == pytest.approx(discover["momentum_without_largest_day"], abs=1e-4)


def test_short_history_gives_no_score():
    assert ps.momentum_12_1([100.0] * 100) is None
    assert ps.high_52w_ratio([100.0] * 100) is None
    assert ps.volatility([100.0] * 50, 126) is None


# --- evidence and robustness -----------------------------------------------------------


def test_predictable_clip_uses_only_earlier_days():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.01, 200)
    y = rb.to_unit_predictable(x[None, :])
    x2 = x.copy()
    x2[150:] *= 50  # a wild future must not change earlier days' betting scale
    y2 = rb.to_unit_predictable(x2[None, :])
    cut = 150 - rb.MIN_OBS
    np.testing.assert_allclose(y[0, :cut], y2[0, :cut])  # days before 150
    assert y.min() >= 0 and y.max() <= 1


def test_noise_rarely_shows_evidence_and_a_real_edge_does():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, 600)
    skill, harm = rb.e_values(noise)
    assert skill < 20 and harm < 20
    edge = rng.normal(0.004, 0.01, 600)
    assert rb.e_values(edge)[0] > 1000


def test_null_false_alarm_is_near_the_nominal_rate_on_iid_noise():
    rng = np.random.default_rng(2)
    rate = rb.null_false_alarm(rng.normal(0.002, 0.01, 400), 100.0, draws=400, seed=0)
    assert rate is not None and rate <= 0.05


def test_e_bh_needs_more_evidence_for_more_signals():
    assert rb.e_bh({"a": 150.0}) == {"a"}
    # With nine signals the strongest must reach 9 / 0.01 = 900.
    many = {"a": 150.0, **{f"s{i}": 1.0 for i in range(8)}}
    assert rb.e_bh(many) == set()
    assert rb.e_bh({**many, "a": 1000.0}) == {"a"}


def test_removing_the_best_days_can_only_lower_the_evidence_of_a_lucky_series():
    x = np.zeros(200)
    x[[20, 60, 100, 140, 180]] = 0.05
    x += np.random.default_rng(4).normal(0, 0.001, 200)
    assert rb.without_best_days(x, clip=0.02) < rb.e_values(x, clip=0.02)[0]


# --- the nightly lab end to end --------------------------------------------------------


def _panel(days: list[str], n: int, seed: int = 5) -> dict[str, dict[str, float]]:
    """EUR closes where yesterday's 12-1 momentum rank nudges tomorrow's return up."""
    rng = np.random.default_rng(seed)
    out: dict[str, list[float]] = {f"S{i}": [100.0] for i in range(n)}
    drift = rng.normal(0, 0.001, n)
    for _ in days[1:]:
        for i in range(n):
            out[f"S{i}"].append(out[f"S{i}"][-1] * (1 + drift[i] + rng.normal(0, 0.015)))
    return {sym: dict(zip(days, vals, strict=True)) for sym, vals in out.items()}


@pytest.fixture
def lab(monkeypatch):
    db = _memory_db()
    days = [d.date().isoformat() for d in pd.bdate_range("2024-06-03", "2026-11-20")]
    closes = _panel(days, 60)
    bench = {d: 100.0 * (1.0002 ** i) for i, d in enumerate(days)}
    for sym in closes:
        db.add(IndexUniverseMember(index_key="msci_world", isin=f"XX{sym:0>10}", name=sym, symbol=sym, symbol_ok=True))
    db.commit()
    state = {"cutoff": days[-1]}

    def fake_closes(_db, symbol, days=0, allow_live=True, rate_cache=None):
        series = bench if symbol == "EUNL.DE" else closes.get(symbol, {})
        return {d: v for d, v in series.items() if d <= state["cutoff"]}

    monkeypatch.setattr(service.eur_prices, "eur_closes", fake_closes)
    monkeypatch.setattr(service.eur_prices, "benchmark_ticker", lambda _db: "EUNL.DE")
    return db, days, closes, state


def test_nightly_scores_then_resolves_without_looking_ahead(lab):
    db, days, closes, state = lab
    i = days.index("2026-11-10")
    state["cutoff"] = days[i]
    out = service.run_nightly(db, today=date.fromisoformat(days[i + 1]))
    assert out["as_of"] == "2026-11-10" and out["snapshots"] == 8  # every price signal; no news yet
    assert out["resolved"] == 0
    snap = db.query(ForwardLabSnapshot).filter_by(signal="momentum_12_1").one()
    frozen = dict(snap.scores_json)
    assert sum(w for w in snap.weights_json["rank"].values() if w > 0) == pytest.approx(1.0)
    assert sum(w for w in snap.weights_json["efficient"].values() if w < 0) == pytest.approx(-1.0)

    # The next night: yesterday's snapshot earns today's return; the frozen scores never change.
    state["cutoff"] = days[i + 1]
    out = service.run_nightly(db, today=date.fromisoformat(days[i + 2]))
    assert out["resolved"] == 8
    db.refresh(snap)
    assert snap.scores_json == frozen
    row = db.query(ForwardLabDaily).filter_by(signal="momentum_12_1").one()
    assert row.day.isoformat() == days[i + 1]
    w = snap.weights_json["rank"]
    expected = sum(wt * (closes[s][days[i + 1]] / closes[s][days[i]] - 1) for s, wt in w.items())
    assert row.value_rank == pytest.approx(expected, rel=1e-4)

    # Running the same night again writes nothing twice.
    again = service.run_nightly(db, today=date.fromisoformat(days[i + 2]))
    assert again["resolved"] == 0
    assert db.query(ForwardLabSnapshot).count() == 16


def test_a_missed_night_is_not_scored_late(lab):
    db, days, _closes, state = lab
    i = days.index("2026-11-12")
    state["cutoff"] = days[i]
    # Run two weekdays late: the previous weekday (11-13) has no bar yet in this panel.
    out = service.run_nightly(db, today=date(2026, 11, 14))
    assert out["as_of"] is None and out["snapshots"] == 0


def test_news_night_stops_when_the_model_is_down(monkeypatch):
    db = _memory_db()
    for k in range(12):
        db.add(IndexUniverseMember(index_key="msci_world", isin=f"US{k:010d}", name=f"N{k}", symbol=f"N{k}", symbol_ok=True))
    db.commit()
    now = datetime(2026, 11, 10, 5, tzinfo=UTC)

    class FakeFinnhub:
        name = "finnhub"
        api_key = "k"

        def get_news(self, symbol, limit=50):
            return {"ok": True, "data": [{"title": "x", "published_at": (now - timedelta(hours=1)).timestamp()}]}

    async def down(_db, _headlines):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(news_tone, "_finnhub", lambda _db: FakeFinnhub())
    monkeypatch.setattr(news_tone, "_rate", down)
    monkeypatch.setattr(news_tone, "FETCH_PAUSE_S", 0.0)
    out = news_tone.run_nightly(db, now=now)
    assert out["failed"] == news_tone.MAX_CONSECUTIVE_FAILURES and out["stopped_early"] is True


def test_nothing_is_scored_before_the_start(lab):
    db, days, _closes, state = lab
    state["cutoff"] = "2026-11-06"
    out = service.run_nightly(db, today=date(2026, 11, 7))
    assert out["snapshots"] == 0


def test_news_tone_uses_only_nights_before_the_scoring_day(lab):
    db, days, _closes, state = lab
    i = days.index("2026-11-12")
    for k in range(40):
        db.add(NewsToneScore(symbol=f"S{k}", day=date(2026, 11, 11), tone=0.1 * (k % 5), n_headlines=2, events_json={}))
        # Read on the morning of the scoring day itself: not yet usable for that day's close.
        db.add(NewsToneScore(symbol=f"S{k}", day=date(2026, 11, 12), tone=-1.0, n_headlines=2, events_json={}))
    db.commit()
    state["cutoff"] = days[i]
    service.run_nightly(db, today=date.fromisoformat(days[i + 1]))
    snap = db.query(ForwardLabSnapshot).filter_by(signal="news_tone").one()
    assert snap.n == 40
    assert min(snap.scores_json.values()) >= 0.0  # the -1.0 rows of 11-12 were left out


def test_summary_reports_every_signal(lab):
    db, days, _closes, state = lab
    start = days.index("2026-11-10")
    for k in range(start, start + 8):
        state["cutoff"] = days[k]
        service.run_nightly(db, today=date.fromisoformat(days[k + 1]))
    out = service.summary(db)
    names = {s["signal"] for s in out["signals"]}
    assert "momentum_12_1" in names and "news_tone" in names
    mom = next(s for s in out["signals"] if s["signal"] == "momentum_12_1")
    assert mom["days"] == 7 and mom["state"] == "collecting"
    news = next(s for s in out["signals"] if s["signal"] == "news_tone")
    assert news["state"] == "not_started"
    # The page reads the night's copy, made with 10,000 resamples.
    stored = service.stored_summary(db)
    assert stored["resamples"] == 10_000
    assert [s["days"] for s in stored["signals"]] == [s["days"] for s in out["signals"]]


def test_forward_lab_endpoint(lab):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.foundation.models.entities import User
    from app.main import app

    db = lab[0]
    user = User(username="lab-viewer", password_hash="hash")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        resp = TestClient(app).get("/api/trust/forward-lab")
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_user, None)
    assert resp.status_code == 200
    body = resp.json()
    assert {s["state"] for s in body["signals"]} == {"not_started"}
    assert body["alpha"] == 0.01


# --- overnight news reading --------------------------------------------------------------


def test_parse_ratings_keeps_only_valid_items():
    raw = '```json\n[{"i":0,"tone":0.5,"event":"earnings"},{"i":0,"tone":-1,"event":"x"},' \
          '{"i":7,"tone":0.2},{"i":1,"tone":3},{"i":2,"tone":-0.4,"event":"weird"}]\n```'
    out = news_tone.parse_ratings(raw, 3)
    assert [(r.i, r.tone, r.event) for r in out] == [(0, 0.5, "earnings"), (2, -0.4, "other")]
    assert news_tone.parse_ratings("no json here", 3) == []


def test_mask_hides_the_company_and_ticker():
    text = news_tone._mask("Apple beats estimates; AAPL up 3%", "AAPL", "APPLE INC")
    assert "apple" not in text.lower() and "AAPL" not in text


def test_headlines_window_and_duplicates():
    end = datetime(2026, 11, 10, 5, tzinfo=UTC)
    rows = [
        {"title": "A", "published_at": (end - timedelta(hours=2)).timestamp()},
        {"title": "a", "published_at": (end - timedelta(hours=3)).timestamp()},
        {"title": "Old", "published_at": (end - timedelta(hours=30)).timestamp()},
        {"title": "Future", "published_at": (end + timedelta(minutes=1)).timestamp()},
    ]
    assert news_tone._headlines(rows, end - timedelta(hours=24), end) == ["A"]


def test_news_night_rates_us_members_and_skips_failures(monkeypatch):
    db = _memory_db()
    for sym in ("AAPL", "MSFT", "SAP.DE", "XOM"):
        db.add(IndexUniverseMember(index_key="msci_world", isin=f"XX{sym:0>10}"[:12], name=sym, symbol=sym, symbol_ok=True))
    db.commit()
    now = datetime(2026, 11, 10, 5, tzinfo=UTC)

    class FakeFinnhub:
        name = "finnhub"
        api_key = "k"

        def get_news(self, symbol, limit=50):
            if symbol == "XOM":
                return {"ok": True, "data": []}
            return {"ok": True, "data": [{"title": f"{symbol} news", "published_at": (now - timedelta(hours=1)).timestamp()}]}

    calls: list[list[str]] = []

    async def fake_rate(_db, headlines):
        calls.append(headlines)
        if "MSFT" in str(headlines) or len(calls) == 2:
            raise RuntimeError("model busy")
        return [news_tone._Rating(i=0, tone=0.6, event="product")], "qwen"

    monkeypatch.setattr(news_tone, "_finnhub", lambda _db: FakeFinnhub())
    monkeypatch.setattr(news_tone, "_rate", fake_rate)
    monkeypatch.setattr(news_tone, "FETCH_PAUSE_S", 0.0)
    out = news_tone.run_nightly(db, now=now)
    assert out["members"] == 4  # SAP.DE has no GDELT headline, so it is skipped
    assert out["rated"] == 1 and out["failed"] == 1 and out["with_news"] == 2
    row = db.query(NewsToneScore).one()
    assert row.symbol == "AAPL" and row.tone == pytest.approx(0.6) and row.events_json == {"product": 1}
    assert row.source == "finnhub"
    # Masked before the model saw it.
    assert all("AAPL" not in h for h in calls[0])


def test_news_night_reads_8k_items_and_gdelt_headlines(monkeypatch):
    from app.foundation.models.entities import NewsMention, Sec8kEvent

    db = _memory_db()
    for sym, name in (("AAPL", "APPLE INC"), ("SAP.DE", "SAP SE"), ("XOM", "EXXON MOBIL CORP")):
        db.add(IndexUniverseMember(index_key="msci_world", isin=f"XX{sym:0>10}"[:12], name=name, symbol=sym, symbol_ok=True))
    now = datetime(2026, 11, 10, 5, tzinfo=UTC)
    seen = now - timedelta(hours=3)
    db.add(Sec8kEvent(accession="0000320193-26-000001", symbol="AAPL", cik="0000320193", form="8-K",
                      filed_on=seen.date(), accepted_at=seen, items="4.02,9.01"))
    # Routine items only: nothing for the model to read.
    db.add(Sec8kEvent(accession="0000034088-26-000001", symbol="XOM", cik="0000034088", form="8-K",
                      filed_on=seen.date(), accepted_at=seen, items="2.02,9.01"))
    db.add(NewsMention(symbol="SAP.DE", day=seen.date(), seen_at=seen, source="gdelt", url="u",
                       title="SAP hebt Prognose an", title_key="sap hebt prognose an"))
    # Outside the window.
    db.add(NewsMention(symbol="SAP.DE", day=(now - timedelta(days=3)).date(), seen_at=now - timedelta(days=3),
                       source="gdelt", url="u2", title="SAP old", title_key="sap old"))
    db.commit()
    calls: dict[str, list[str]] = {}

    async def fake_rate(_db, headlines):
        calls[str(len(calls))] = headlines
        return [news_tone._Rating(i=i, tone=-0.5, event="legal") for i in range(len(headlines))], "qwen"

    monkeypatch.setattr(news_tone, "_finnhub", lambda _db: None)
    monkeypatch.setattr(news_tone, "_rate", fake_rate)
    out = news_tone.run_nightly(db, now=now)
    assert out["rated"] == 2 and out["by_source"] == {"sec": 1, "gdelt": 1}
    rows = {r.symbol: r for r in db.query(NewsToneScore)}
    assert set(rows) == {"AAPL", "SAP.DE"}
    assert rows["AAPL"].source == "sec" and rows["SAP.DE"].source == "gdelt" and rows["SAP.DE"].n_headlines == 1
    texts = [h for hs in calls.values() for h in hs]
    assert any("can no longer be relied on" in h for h in texts)
    assert all("SAP" not in h for h in texts)  # masked
