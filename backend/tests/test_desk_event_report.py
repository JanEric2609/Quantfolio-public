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
    assert ov["pair_shadow"]["days_both_fired"] == 0 and ov["pair_shadow"]["days_same_side"] == 0
    assert all(c["name"] for c in ov["cells"]) and all(c["name"] != c["cell"] for c in ov["cells"])


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
    assert text.startswith("Desk week") and "No cell has a closed paper trade yet." in text
    assert "month -7.00 EUR (1)" in text and "Still open: SAP" in text and "Bankroll: month -7 of the 60 EUR stop." in text


def test_digest_rides_the_reconcile_job_on_sunday(monkeypatch):
    db, user = _setup()
    sent = []
    from app.decision.desk import telegram

    monkeypatch.setattr(telegram, "alert", lambda db, uid, text, markup=None: sent.append(text))
    assert jobs.send_weekly_digest(db, datetime(2026, 11, 22, 17, 17, tzinfo=UTC)) == {"sent": True}
    assert sent and sent[0].startswith("Desk week")


def test_digest_is_due_sunday_18_berlin_all_year():
    # Winter (UTC+1) and summer (UTC+2): 18:17 Berlin both times.
    assert jobs.digest_due(datetime(2026, 11, 22, 17, 17, tzinfo=UTC))
    assert not jobs.digest_due(datetime(2026, 11, 22, 16, 17, tzinfo=UTC))
    assert jobs.digest_due(datetime(2026, 7, 5, 16, 17, tzinfo=UTC))
    assert not jobs.digest_due(datetime(2026, 7, 5, 17, 17, tzinfo=UTC))  # 19:17 Berlin in summer
    assert not jobs.digest_due(datetime(2026, 11, 21, 17, 17, tzinfo=UTC))  # a Saturday


def test_digest_carries_the_pair_shadow_line_when_both_indices_fired(monkeypatch):
    from app.decision.desk import pair_shadow

    db, user = _setup()
    monkeypatch.setattr(pair_shadow, "report", lambda _db: {
        "days_both_fired": 3, "days_same_side": 2, "mean_full_eur": -1.5, "mean_half_eur": -0.75,
        "worst_full_eur": -4.0, "worst_half_eur": -2.0, "note": "", "estimate": True, "not_tax_advice": True})
    assert "DAX + ES50 on the same side (2 days, shadow)" in event_report.weekly_digest(db, user.id, NOW)
