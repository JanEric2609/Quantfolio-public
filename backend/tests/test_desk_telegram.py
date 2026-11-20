"""Telegram side of the desk: the registry, the admin-only button and the /idea /desk /stop /close commands."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, memory_db, standard_handler

from app.decision.desk import register_desk_telegram, service
from app.foundation import telegram_bot, telegram_updates
from app.foundation.models.entities import DeskEvent, DeskPosition, TelegramAccount
from app.foundation.scalable import service as scalable_service


class Sent:
    def __init__(self):
        self.messages, self.answers = [], []


@pytest.fixture
def bot(monkeypatch):
    sent = Sent()
    monkeypatch.setattr(telegram_bot, "send_telegram_message",
                        lambda db, chat, text, reply_markup=None: sent.messages.append((chat, text, reply_markup)) or True)
    monkeypatch.setattr(telegram_bot, "answer_callback_query",
                        lambda db, cb, text: sent.answers.append((cb, text)) or True)
    from app.decision.desk import news

    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(news, "alpaca_for", lambda db: None)
    register_desk_telegram()
    return sent


def _link(db, user, chat="555"):
    db.add(TelegramAccount(user_id=user.id, chat_id=chat))
    db.commit()


def _sc(monkeypatch, now=None):
    sc = FakeSc(standard_handler({"DE000KO000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item("DE000KO000A1")]}, daily_points(), now=datetime.now(UTC)))
    monkeypatch.setattr(scalable_service, "make_cli", lambda db: sc.cli())
    return sc


def _msg(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text}}


def _freeze(monkeypatch):
    """Run the desk's Telegram handlers at a fixed moment inside the Xetra trading window."""
    import app.decision.desk.telegram as tg

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(tg, "datetime", Fixed)


def test_idea_sends_a_button_and_the_tap_fills(monkeypatch, bot):
    db = memory_db()
    user = make_user(db)
    _link(db, user)
    _freeze(monkeypatch)
    sc = FakeSc(standard_handler({"DE000KO000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item("DE000KO000A1")]}, daily_points(), now=NOW))
    monkeypatch.setattr(scalable_service, "make_cli", lambda db: sc.cli())
    telegram_updates.handle_update(db, _msg("555", "/idea DAX up today ko"))
    chat, text, markup = bot.messages[-1]
    assert "PAPER idea" in text and "Paper only" in text and chat == "555"
    data = markup["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("desk:take:") and markup["inline_keyboard"][0][0]["text"] == "Paper: take"
    telegram_updates.handle_update(db, {"callback_query": {"id": "cb1", "data": data,
                                                           "message": {"chat": {"id": "555"}}}})
    assert bot.answers[-1] == ("cb1", "Paper position opened.")
    assert db.query(DeskPosition).one().status == "open"
    telegram_updates.handle_update(db, {"callback_query": {"id": "cb9", "data": data,
                                                           "message": {"chat": {"id": "555"}}}})
    assert bot.answers[-1] == ("cb9", "Already used.")


def test_idea_with_a_bad_command_or_a_halt_says_so(monkeypatch, bot):
    db = memory_db()
    user = make_user(db)
    _link(db, user)
    telegram_updates.handle_update(db, _msg("555", "/idea DAX sideways"))
    assert "up or down" in bot.messages[-1][1]
    service.set_halt(db, True, "x", "test")
    telegram_updates.handle_update(db, _msg("555", "/idea DAX up"))
    assert "halted" in bot.messages[-1][1]


def test_button_from_a_non_admin_or_unlinked_chat_is_refused(monkeypatch, bot):
    db = memory_db()
    user = make_user(db, role="user", name="guest")
    _link(db, user, "777")
    telegram_updates.handle_update(db, {"callback_query": {"id": "cb2", "data": "desk:take:x:y",
                                                           "message": {"chat": {"id": "777"}}}})
    telegram_updates.handle_update(db, {"callback_query": {"id": "cb3", "data": "desk:take:x:y",
                                                           "message": {"chat": {"id": "999"}}}})
    assert [a[1] for a in bot.answers] == ["Not allowed.", "Not allowed."]
    assert db.query(DeskEvent).count() == 0


def test_non_admin_command_is_refused_and_unlinked_chat_never_reaches_the_desk(monkeypatch, bot):
    db = memory_db()
    guest = make_user(db, role="user", name="guest")
    _link(db, guest, "777")
    telegram_updates.handle_update(db, _msg("777", "/stop now"))
    telegram_updates.handle_update(db, _msg("888", "/stop now"))
    assert not service.is_halted(db)
    assert "admin" in bot.messages[0][1] and "isn't linked" in bot.messages[1][1]


def test_stop_halts_and_desk_reports_and_resume_is_app_only(monkeypatch, bot):
    db = memory_db()
    owner = make_user(db)
    _link(db, owner)
    telegram_updates.handle_update(db, _msg("555", "/stop news looks odd"))
    assert service.is_halted(db) and service.halt_info(db)["by"] == "telegram"
    telegram_updates.handle_update(db, _msg("555", "/desk"))
    assert "HALTED" in bot.messages[-1][1] and "PAPER" in bot.messages[-1][1]
    telegram_updates.handle_update(db, _msg("555", "/resume"))  # not a command: parsed as an expense, never a resume
    assert service.is_halted(db)


def test_close_by_short_id(monkeypatch, bot):
    db = memory_db()
    owner = make_user(db)
    _link(db, owner)
    _sc(monkeypatch)
    pos = DeskPosition(user_id=owner.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                       isin="DE000KO000A1", underlying_isin="DE0008469008", direction="long", status="open",
                       units=10, entry_price=Decimal("1"), entry_fee=Decimal("0.99"), stop_price=Decimal("0.9"),
                       entry_at=NOW, last_bid=Decimal("1"))
    db.add(pos)
    db.commit()
    telegram_updates.handle_update(db, _msg("555", f"/close {pos.id[:8]}"))
    db.refresh(pos)
    assert pos.status == "closed" and pos.exit_reason == "owner_close"
    telegram_updates.handle_update(db, _msg("555", "/close zzzz"))
    assert "No single open position" in bot.messages[-1][1]


def test_sendmessage_and_getupdates_carry_markup_and_callback_updates(monkeypatch):
    seen = {}

    def fake_call(token, method, payload=None, **kw):
        seen[method] = payload
        return []

    db = memory_db()
    monkeypatch.setattr(telegram_bot, "get_bot_token", lambda db: "t")
    monkeypatch.setattr(telegram_bot, "_call_api", fake_call)
    telegram_bot.get_updates(db, None, 1)
    assert seen["getUpdates"]["allowed_updates"] == ["message", "callback_query"]
    assert telegram_bot.answer_callback_query(db, "cb", "ok") is True and seen["answerCallbackQuery"]["text"] == "ok"
    posted = {}

    class Resp:
        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            posted.update(json)
            return Resp()

    monkeypatch.setattr(telegram_bot.httpx, "Client", Client)
    telegram_bot.send_telegram_message(db, "1", "hi", {"inline_keyboard": []})
    assert posted["reply_markup"] == {"inline_keyboard": []}
    posted.clear()
    telegram_bot.send_telegram_message(db, "1", "hi")
    assert "reply_markup" not in posted
