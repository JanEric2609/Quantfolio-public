import json
from datetime import UTC, datetime
from decimal import Decimal

from test_desk_events import BERLIN, NOW, _cand, _setup
from test_desk_events import ci as ci

from app.decision.desk import cfd_journal, event_messages, events
from app.decision.desk.event_sources import Candidate
from app.foundation.models.entities import DeskPosition
from app.foundation.settings import upsert_public_settings

US_NOW = datetime(2026, 11, 18, 16, 30, tzinfo=BERLIN)


def _hype(db, user):
    c = Candidate(cell="hype_fade:cfd", inst=ci.by_key("US:NVDA"), direction="short", ref_price=Decimal("480"),
                  stop_distance=Decimal("30"), max_exit_at=datetime(2026, 12, 2, 20, 40, tzinfo=UTC),
                  reason="hype fade. +11.2% today on 6x its usual news and 4x its usual volume; no filing or earnings.",
                  evidence={"stop_atr": 1.5})
    p, why = events._open_one(db, user.id, c, US_NOW, Decimal("0.9"))
    assert p is not None, why
    return p


def test_hype_message_golden():
    db, user = _setup()
    p = _hype(db, user)
    text = event_messages.live_text(db, p, cfd_journal.for_position(db, p.id), US_NOW)
    assert text.splitlines() == [
        "NVDA short: CFD, manual (Trading 212: NVIDIA (NVDA))",
        "Why: hype fade. +11.2% today on 6x its usual news and 4x its usual volume; no filing or earnings.",
        "Research: after retail herding, such stocks gave back about 5 % over 20 days (Barber, Huang, Odean, Schwarz).",
        "Evidence: replay 2016-26 n/a (no history for this rule) | live paper 0 trades (too early).",
        "Quantity 0.1813 (~ 78 EUR, margin ~ 16 EUR, leverage 5x). Loss at the stop ~ 5.00 EUR.",
        "Stop: 510.00 (1.5 ATR). Exit: by 2026-12-02 at 21:40 Berlin, or when I tell you.",
        "Costs: spread ~ 0.08 %, overnight ~ 0.02 EUR per night on 78 EUR, 0.5 % FX on the result.",
        "Bankroll: month +0 EUR of the 60 EUR stop.",
        "Paper-scored as hype_fade:cfd. If Trading 212 has no CFD on NVDA, skip; nothing is ordered for you.",
    ]


def test_each_cell_has_its_own_research_line_and_name():
    db, user = _setup()
    for key, cell in (("DE:SAP", "ta_trend:cfd"), ("DE:SIE", "ta_band:cfd"), ("DE:ALV", "owner_call:cfd")):
        p, why = events._open_one(db, user.id, _cand(key, cell), NOW, Decimal(1))
        text = event_messages.live_text(db, p, cfd_journal.for_position(db, p.id), NOW)
        assert event_messages.RESEARCH[cell] in text and f"Paper-scored as {cell}" in text
        assert "SAP" in text or "SIE" in text or "ALV" in text


def test_evidence_line_with_a_replay():
    db, _ = _setup()
    assert "replay 2016-26 n/a" in event_messages.evidence_line(db, "ta_trend:cfd")
    upsert_public_settings(db, {"desk_signal_replay_json": json.dumps({"ta_trend:cfd": {"n": 120, "mean_eur": -0.31}})})
    db.commit()
    line = event_messages.evidence_line(db, "ta_trend:cfd")
    assert "replay 2016-26 n=120, -0.31 EUR a trade" in line and "too early" in line


def test_failing_cell_is_paper_only():
    db, user = _setup()
    for i in range(40):
        db.add(DeskPosition(
            user_id=user.id, mode="paper", cell="ta_band:cfd", signal="ta_band", product_type="cfd", isin="DE1",
            underlying_isin="DE1", underlying_label="X", direction="long", liquid=True, quantity=Decimal("1"),
            entry_price=Decimal("10"), entry_at=datetime(2026, 11, 19, 10, tzinfo=UTC), status="closed",
            net_eur=Decimal("-0.5"), underlying_move=Decimal("-0.01"), prereg_hash=events.EVENT_HASH,
            product_name="p", leverage_or_omega=Decimal(1), created_at=datetime(2026, 11, 2, tzinfo=UTC)))
    db.commit()
    assert event_messages.cell_state(db, "ta_band:cfd")["verdict"] == "failing"
    p, _ = events._open_one(db, user.id, _cand("DE:SIE", "ta_band:cfd"), NOW, Decimal(1))
    text = event_messages.live_text(db, p, cfd_journal.for_position(db, p.id), NOW)
    assert "failing its own test" in text and "Quantity" not in text and "Exit (paper)" in text


def test_close_message():
    db, user = _setup()
    p = _hype(db, user)
    j = cfd_journal.for_position(db, p.id)
    events.event_close(db, p, Decimal("470"), "stop", US_NOW, ci.by_key("US:NVDA"))
    text = event_messages.close_text(db, p, "stop", j)
    assert text.startswith("NVIDIA (NVDA): CLOSE now (stop-loss level reached). Paper result ")
    assert "hype_fade:cfd" in text
    cfd_journal.mark_entered(db, j, US_NOW, Decimal("480"), Decimal("0.18"))
    assert "reply to this message with: <exit price>" in event_messages.close_text(db, p, "time", j)
    assert "contract rolls" not in event_messages.close_text(db, p, "time", j)
    assert "close yours too" in event_messages.close_text(db, p, "rollover", j)


def test_index_message_gets_one_evidence_line():
    from app.decision.desk import cfd

    db, _ = _setup()
    line = cfd.evidence_line(db)
    assert line.startswith("Evidence: replay 2016-26 -0.18 EUR a trade (SPY") and "(too early)" in line
