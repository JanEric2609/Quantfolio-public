from datetime import UTC, datetime
from decimal import Decimal

from test_desk_events import NOW, _cand, _setup

from app.decision.desk import cfd_journal, event_report, events, jobs
from app.foundation.models.entities import DeskCfdJournal


def test_overview_shape_and_empty_states():
    db, user = _setup()
    ov = event_report.signals_overview(db, user.id, NOW)
    cells = {(c["registration"], c["cell"]) for c in ov["cells"]}
    assert ("first", "index_momentum:cfd") in cells and ("events", "owner_call:cfd") in cells
    assert len(ov["cells"]) == 11 and ov["open_positions"] == [] and ov["journal"] == []
    assert ov["bankroll"]["ok"] is True and ov["bankroll"]["monthly_stop"] == "60"
    assert all(c["verdict"] == "too early" for c in ov["cells"]) and ov["estimate"] is True
    assert ov["owner_call"]["cell"] == "owner_call:cfd"


def test_overview_real_vs_paper_and_open_positions():
    db, user = _setup()
    p, _ = events._open_one(db, user.id, _cand("DE:SAP", "owner_call:cfd"), NOW, Decimal(1))
    q, _ = events._open_one(db, user.id, _cand("DE:SIE", "ta_band:cfd"), NOW, Decimal(1))
    events.event_close(db, q, Decimal("101"), "time", NOW, events.cfd_instruments.by_key("DE:SIE"))
    j = cfd_journal.for_position(db, q.id)
    cfd_journal.mark_entered(db, j, NOW, Decimal("100"), Decimal("0.5"))
    cfd_journal.mark_closed(db, j, NOW, Decimal("101"))
    db.commit()
    ov = event_report.signals_overview(db, user.id, NOW)
    assert [o["cell"] for o in ov["open_positions"]] == ["owner_call:cfd"] and p.status == "open"
    band = next(c for c in ov["cells"] if c["cell"] == "ta_band:cfd")
    assert band["real"]["n"] == 1 and band["real"]["real_total"] > 0
    assert band["real"]["paper_total_same_trades"] == float(q.net_eur)
    assert ov["journal"][0]["real_pnl_eur"] is not None


def test_weekly_digest_text():
    db, user = _setup()
    assert "No paper trades closed this week." in event_report.weekly_digest(db, user.id, NOW)
    p, _ = events._open_one(db, user.id, _cand("DE:SAP"), NOW, Decimal(1))
    db.add(DeskCfdJournal(position_id="x", user_id=user.id, market_key="SPX", direction="long", currency="USD",
                          status="closed", real_pnl_eur=Decimal("-7"), closed_at=NOW))
    db.commit()
    text = event_report.weekly_digest(db, user.id, NOW)
    assert text.startswith("Desk week") and "ta_trend:cfd: 0 trades" in text
    assert "month -7.00 EUR (1)" in text and "still open: SAP" in text and "Bankroll: month -7 of the 60 EUR stop." in text


def test_digest_rides_the_reconcile_job_on_sunday(monkeypatch):
    db, user = _setup()
    sent = []
    from app.decision.desk import telegram

    monkeypatch.setattr(telegram, "alert", lambda db, uid, text, markup=None: sent.append(text))
    assert jobs.send_weekly_digest(db, datetime(2026, 11, 22, 17, 17, tzinfo=UTC)) == {"sent": True}
    assert sent and sent[0].startswith("Desk week")
