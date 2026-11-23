"""The owner's CFD journal (ADR 0027 amendment 2026-11-23): buttons, /cfd, real P&L, no ledger writes."""
from datetime import timedelta
from decimal import Decimal

import pytest
from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, cfd_journal, telegram
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskCfdJournal, DeskEvent, DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx(key="DAX", fx="1", index="25000"):
    return cfd.CfdContext(
        key=key, r1=Decimal("0.004"), proxy_prev_close=Decimal(100), index_prev_close=Decimal(index),
        index_level_est=Decimal(index), currency="EUR" if key in ("DAX", "ES50") else "USD",
        eur_per_unit=Decimal(fx), sigma=Decimal("0.01"), session_minutes=510, enter_by=NOW, exit_at=NOW,
        exposure_eur=Decimal("500.00"), quantity_t212=Decimal("0.02"), stop_points=Decimal(250),
        typical_30m_points=Decimal(60), spread_points=Decimal(1), margin_eur=Decimal(25),
        overnight_eur_per_night=Decimal("0.04"))


def _signal(db, user, key="DAX", direction="long", fx="1", index="25000"):
    ctx = _ctx(key, fx, index)
    p = cfd.record_signal(db, user.id, key, ALIASES[key], direction, NOW + timedelta(minutes=27), NOW,
                          Decimal(100), Decimal("0.01"), context=ctx)
    db.commit()
    return p, cfd_journal.for_position(db, p.id)


class _Account:
    def __init__(self, user_id):
        self.user_id = user_id
        self.chat_id = "1"


def _say_log(monkeypatch):
    log = []
    monkeypatch.setattr(telegram, "_say", lambda db, chat, text, markup=None: log.append(text))
    return log


def test_signal_creates_one_journal_row_with_the_context():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    assert j.status == "sent" and j.quantity_suggested == Decimal("0.02") and j.market_key == "DAX"
    assert db.query(DeskCfdJournal).count() == 1


def test_button_flow_sent_entered_closed_with_real_pnl_eur():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    cfd_journal.mark_entered(db, j, NOW, Decimal("25400"), None)
    assert j.status == "entered" and j.quantity == Decimal("0.02")
    assert cfd_journal.stop_level(j) == Decimal("25150")
    cfd_journal.mark_closed(db, j, NOW + timedelta(minutes=27), Decimal("25450"))
    assert j.status == "closed" and j.real_pnl_eur == Decimal("1.0000")  # 50 pts x 0.02
    assert cfd_journal.nights_held(j) == 0


def test_short_and_usd_result_carries_the_fx_fee(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user, key="SPX", direction="short", fx="0.9")
    j.index_level_est = Decimal("6800")
    monkeypatch.setattr("app.foundation.ecb_fx.eur_per_unit", lambda db, c, d: (0.9, "ecb"))
    cfd_journal.mark_entered(db, j, NOW, Decimal("6800"), Decimal("0.1"))
    cfd_journal.mark_closed(db, j, NOW, Decimal("6790"))
    gross = Decimal(10) * Decimal("0.1") * Decimal("0.9")  # short, +10 pts
    assert j.real_pnl_eur == (gross - Decimal("0.005") * gross).quantize(Decimal("0.0001"))


def test_skip_only_from_sent():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    cfd_journal.mark_entered(db, j, NOW)
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.mark_skipped(db, j, NOW)
    p2, j2 = _signal(db, make_user(db, name="o2"))
    cfd_journal.mark_skipped(db, j2, NOW)
    assert j2.status == "skipped"


def test_another_users_row_is_refused(monkeypatch):
    db = _memory_db()
    owner, other = make_user(db), make_user(db, name="other")
    p, j = _signal(db, owner)
    assert telegram.handle_cfd_button(db, _Account(other.id), "1", f"cfd:in:{p.id}") in (
        "Unknown trade.", "The desk is for the admin account.")
    db.refresh(j)
    assert j.status == "sent"


def test_button_handler_marks_and_prompts(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    log = _say_log(monkeypatch)
    assert telegram.handle_cfd_button(db, _Account(user.id), "1", f"cfd:in:{p.id}") == "Entered."
    assert j.status == "entered" and "Reply to this message with your fill price" in log[0]
    assert f"/cfd in <price> [quantity] {p.id[:8]}" in log[0] and "24500" not in log[0]
    assert len(f"cfd:in:{p.id}") <= 64
    assert telegram.cfd_markup(j)["inline_keyboard"][0][0]["text"] == "Closed"


@pytest.mark.parametrize("raw,ok", [("24500.5", Decimal("24500.5")), ("24500,5", Decimal("24500.5")),
                                    ("24.500,5", None), ("abc", None)])
def test_number_parsing(raw, ok):
    if ok is None:
        with pytest.raises(cfd_journal.JournalError):
            cfd_journal.parse_number(raw)
    else:
        assert cfd_journal.parse_number(raw) == ok


def test_cfd_in_command_names_the_stop_and_takes_an_optional_quantity(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    monkeypatch.setattr(cfd_journal, "find_for_owner", lambda db_, uid, ref, now=None, **kw: j)
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd in 25400,5 0.0113")
    assert j.entry_level == Decimal("25400.5") and j.quantity == Decimal("0.0113")
    assert "Stop for your fill: 25,150.5 (250 pts)" in log[-1]
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd out 25450")
    assert "Your result +0.56 EUR" in log[-1] and "paper result still open" in log[-1]
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd in 1.234,5")
    assert "decimal separator" in log[-1]


def test_level_sanity_and_quantity_bounds():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.mark_entered(db, j, NOW, Decimal("30500"))  # 22 % above the estimate
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.mark_entered(db, j, NOW, Decimal("25000"), Decimal("1001"))
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.mark_entered(db, j, NOW, Decimal("-1"))


def test_id_prefix_lookup_and_todays_default():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    assert cfd_journal.find_for_owner(db, user.id, p.id[:8]) is j
    assert cfd_journal.find_for_owner(db, user.id, "zzzzzzzz") is None
    j.created_at = NOW
    assert cfd_journal.find_for_owner(db, user.id, None, NOW) is j
    cfd_journal.mark_skipped(db, j, NOW)
    assert cfd_journal.find_for_owner(db, user.id, None, NOW) is None


def test_no_ledger_row_and_no_position_change_from_journal_actions():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    events = db.query(DeskEvent).count()
    before = {c: getattr(p, c) for c in ("status", "net_eur", "exit_price", "stop_price", "quantity")}
    cfd_journal.mark_entered(db, j, NOW, Decimal("25400"))
    cfd_journal.mark_closed(db, j, NOW, Decimal("25450"))
    db.commit()
    db.refresh(p)
    assert db.query(DeskEvent).count() == events
    assert {c: getattr(p, c) for c in before} == before and db.query(DeskPosition).count() == 1


def test_closed_button_then_cfd_out_with_the_prompted_id_sets_the_exit(monkeypatch):
    """pullfrog on public #17: the Closed button's prompt must lead to a command that finds this row."""
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    acct = _Account(user.id)
    telegram.handle_cfd_button(db, acct, "1", f"cfd:in:{p.id}")
    telegram.handle_cfd(db, acct, "1", f"/cfd in 25000 0.02 {p.id[:8]}")
    assert telegram.handle_cfd_button(db, acct, "1", f"cfd:out:{p.id}") == "Closed."
    assert j.status == "closed" and j.exit_level is None and p.id[:8] in log[-1]
    closed_at = j.closed_at
    assert f"/cfd out <price> {p.id[:8]}" in log[-1]
    telegram.handle_cfd(db, acct, "1", f"/cfd out 25100 {p.id[:8]}")
    assert j.exit_level == Decimal("25100") and j.real_pnl_eur == Decimal("2.0000")
    assert j.closed_at == closed_at  # the button's time stays the close time
    with pytest.raises(cfd_journal.JournalError):  # an exit level is set once
        cfd_journal.mark_closed(db, j, NOW, Decimal("25200"))


def test_ref_less_lookup_finds_a_row_closed_without_level_but_not_one_with_it():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    j.created_at = NOW  # the lookup is by today's date (Berlin)
    cfd_journal.mark_entered(db, j, NOW, Decimal("25000"))
    cfd_journal.mark_closed(db, j, NOW)
    assert cfd_journal.find_for_owner(db, user.id, None, NOW) is j
    cfd_journal.mark_closed(db, j, NOW, Decimal("25050"))
    assert cfd_journal.find_for_owner(db, user.id, None, NOW) is None


def test_level_without_an_estimate_is_still_bounded():
    """pullfrog on public #17: without index_level_est a huge level overflowed real_pnl_eur at commit."""
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    j.index_level_est = None
    db.commit()
    cfd_journal.mark_entered(db, j, NOW, Decimal(1), Decimal(1000))
    with pytest.raises(cfd_journal.JournalError):
        cfd_journal.mark_closed(db, j, NOW, Decimal("1e12"))
    cfd_journal.mark_closed(db, j, NOW, Decimal(999_999))
    db.commit()
    assert j.real_pnl_eur == Decimal("999998000.0000")


# -- 2026-11-23, the first live night: two trades open, a fill without an id, replies --------------------------


def _two_us_trades(db, user):
    spx_p, spx = _signal(db, user, "SPX", fx="0.9", index="7000")
    ndx_p, ndx = _signal(db, user, "NDX", fx="0.9", index="26000")
    spx.created_at, ndx.created_at = NOW, NOW + timedelta(seconds=1)  # NDX is the newer row, as on prod
    for j in (spx, ndx):
        cfd_journal.mark_entered(db, j, NOW)
    db.commit()
    return spx, ndx


def test_a_fill_without_id_goes_to_the_trade_whose_index_is_near_it(monkeypatch):
    """On the first live night the newest row (NDX) took an S&P fill and the answer named the wrong index."""
    db = _memory_db()
    user = make_user(db)
    spx, ndx = _two_us_trades(db, user)
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    monkeypatch.setattr(telegram, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    telegram.handle_cfd(db, _Account(user.id), "1", "/cfd in 7020.5 0.0400")
    assert spx.entry_level == Decimal("7020.5") and spx.quantity == Decimal("0.0400")
    assert ndx.entry_level is None
    assert "SPX in at 7,020.5 (20.5 pts worse than the signal's index level)" in log[-1]


def test_a_level_near_no_trade_and_two_near_trades_are_refused_with_the_ids():
    db = _memory_db()
    user = make_user(db)
    spx, ndx = _two_us_trades(db, user)
    with pytest.raises(cfd_journal.JournalError, match="not near any of today's trades"):
        cfd_journal.find_for_owner(db, user.id, None, NOW, action="in", level=Decimal("702.05"))
    dax_p, dax = _signal(db, user, "DAX", index="25000")
    dax.created_at = NOW
    cfd_journal.mark_entered(db, dax, NOW)
    with pytest.raises(cfd_journal.JournalError, match="More than one trade fits") as err:
        cfd_journal.find_for_owner(db, user.id, None, NOW, action="in", level=Decimal("26000"))
    assert dax.position_id[:8] in str(err.value) and ndx.position_id[:8] in str(err.value)


def test_in_prefers_the_row_marked_entered_and_out_needs_an_entered_row():
    db = _memory_db()
    user = make_user(db)
    a_p, a = _signal(db, user, "SPX", fx="0.9", index="7000")
    b_p, b = _signal(db, user, "SPX", fx="0.9", index="7000")
    a.created_at, b.created_at = NOW, NOW + timedelta(seconds=1)
    cfd_journal.mark_entered(db, a, NOW)
    assert cfd_journal.find_for_owner(db, user.id, None, NOW, action="in", level=Decimal("7250")) is a
    assert cfd_journal.find_for_owner(db, user.id, None, NOW, action="out", level=Decimal("7250")) is a
    assert cfd_journal.find_for_owner(db, user.id, None, NOW, action="skip") is b


@pytest.mark.parametrize("text,entry,exit_,qty", [
    ("7020.5", "7020.5", None, None),
    ("7020,5 0.0400", "7020.5", None, "0.0400"),
    ("7020.5 7005.5", "7020.5", "7005.5", None),
    ("/cfd in 7020.5 0.0400", "7020.5", None, "0.0400"),
    ("out 7005.5", None, "7005.5", None),
    ("0.05", None, None, "0.05"),
])
def test_parse_reply(text, entry, exit_, qty):
    db = _memory_db()
    user = make_user(db)
    _p, j = _signal(db, user, "SPX", fx="0.9", index="7000")
    fill = cfd_journal.parse_reply(j, text)
    as_dec = lambda v: Decimal(v) if v else None  # noqa: E731
    assert (fill.entry, fill.exit, fill.quantity) == (as_dec(entry), as_dec(exit_), as_dec(qty))


@pytest.mark.parametrize("text,msg", [
    ("702.05", "neither a quantity or a price near 7,000"),
    ("702.05 0.0400", "neither a quantity"),
    ("bought 7020", "is not a price"),
    ("7020 7005 7010", "Too many prices"),
    ("out", "Reply with your price"),
    ("1.234,5", "decimal separator"),
])
def test_parse_reply_refuses_typos(text, msg):
    db = _memory_db()
    user = make_user(db)
    _p, j = _signal(db, user, "SPX", fx="0.9", index="7000")
    with pytest.raises(cfd_journal.JournalError, match=msg):
        cfd_journal.parse_reply(j, text)


def test_a_reply_to_the_signal_message_logs_entry_then_exit(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    spx, ndx = _two_us_trades(db, user)
    spx.telegram_message_id, ndx.telegram_message_id = 106, 107
    db.commit()
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    monkeypatch.setattr("app.foundation.ecb_fx.eur_per_unit", lambda db, c, d: (0.9, "ecb"))
    acct, bot = _Account(user.id), {"id": 1, "is_bot": True}
    assert telegram.handle_cfd_reply(db, acct, "1", "26050.25 0.0060", {"message_id": 107, "from": bot})
    assert ndx.entry_level == Decimal("26050.25") and ndx.quantity == Decimal("0.0060") and spx.entry_level is None
    assert telegram.handle_cfd_reply(db, acct, "1", "26000.75", {"message_id": 107, "from": bot})
    assert ndx.status == "closed" and ndx.exit_level == Decimal("26000.75")
    assert ndx.real_pnl_eur == Decimal("-0.2686")  # -49.5 x 0.006 x 0.9 = -0.2673, plus the 0.5 % fee
    assert "Your result -0.27 EUR" in log[-1]
    # Both prices at once, after the fact; and a typo is refused, nothing saved.
    assert telegram.handle_cfd_reply(db, acct, "1", "702.05", {"message_id": 106, "from": bot})
    assert "neither a quantity" in log[-1] and spx.entry_level is None
    assert telegram.handle_cfd_reply(db, acct, "1", "7020.5 7005.5", {"message_id": 106, "from": bot})
    assert spx.entry_level == Decimal("7020.5") and spx.exit_level == Decimal("7005.5")


def test_a_reply_to_a_prompt_or_close_message_finds_the_trade_by_the_id_in_its_text(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    acct, bot = _Account(user.id), {"id": 1, "is_bot": True}
    telegram.handle_cfd_button(db, acct, "1", f"cfd:in:{p.id}")
    prompt = {"message_id": 900, "from": bot, "text": log[-1]}
    assert telegram.handle_cfd_reply(db, acct, "1", "25010", prompt)
    assert j.entry_level == Decimal("25010")
    close_text = cfd.close_message(p, "time", j)
    assert f"To log your trade ({p.id[:8]}), reply to this message with: <exit price>" in close_text
    assert telegram.handle_cfd_reply(db, acct, "1", "25030", {"message_id": 901, "from": bot, "text": close_text})
    assert j.exit_level == Decimal("25030") and j.status == "closed"
    # A reply to some other bot message is not ours: the usual handling sees it.
    assert not telegram.handle_cfd_reply(db, acct, "1", "3.50 coffee", {"message_id": 5, "from": bot, "text": "Hi"})
    assert not telegram.handle_cfd_reply(db, acct, "1", "/cfd", prompt)


def test_the_close_message_asks_for_both_prices_when_none_was_sent():
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    assert "reply to this message" not in cfd.close_message(p, "time", j)  # not entered: nothing to log
    cfd_journal.mark_entered(db, j, NOW)
    assert "reply to this message with: <entry price> <exit price>" in cfd.close_message(p, "time", j)


def test_closed_by_button_first_then_both_prices_by_reply(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p, j = _signal(db, user)
    log = _say_log(monkeypatch)
    monkeypatch.setattr(telegram, "_is_admin", lambda db, a: True)
    acct, bot = _Account(user.id), {"id": 1, "is_bot": True}
    j.telegram_message_id = 300
    telegram.handle_cfd_button(db, acct, "1", f"cfd:in:{p.id}")
    telegram.handle_cfd_button(db, acct, "1", f"cfd:out:{p.id}")
    assert telegram.handle_cfd_reply(db, acct, "1", "25100", {"message_id": 300, "from": bot})
    assert "Send both prices" in log[-1] and j.entry_level is None
    assert telegram.handle_cfd_reply(db, acct, "1", "25000 0.02 25100", {"message_id": 300, "from": bot})
    assert (j.status, j.entry_level, j.exit_level, j.real_pnl_eur) == (
        "closed", Decimal("25000"), Decimal("25100"), Decimal("2.0000"))
    assert telegram.handle_cfd_reply(db, acct, "1", "25200", {"message_id": 300, "from": bot})
    assert "both prices already" in log[-1]
