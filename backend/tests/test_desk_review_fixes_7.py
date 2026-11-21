"""Regression tests for the seventh 2026-11-20 review of the derivatives desk (pullfrog: lock order)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import ledger, service
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_the_write_lock_is_a_transaction_advisory_lock_on_postgres_and_a_no_op_on_sqlite():
    sql: list[str] = []
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
                           execute=lambda stmt, params=None: sql.append(f"{stmt} {params}"))
    ledger.desk_write_lock(fake)
    assert sql == ["SELECT pg_advisory_xact_lock(hashtext(:name)) {'name': 'desk_ledger_write'}"]
    ledger.desk_write_lock(_memory_db())  # SQLite: nothing to lock, no error


def test_every_desk_write_path_takes_the_write_lock_before_a_position_row(monkeypatch):
    """One lock order everywhere (write lock, row, ledger seq): the monitor and an owner's Close cannot
    deadlock (pullfrog, service.py:487)."""
    db = _memory_db()
    user = make_user(db)
    order: list[str] = []
    monkeypatch.setattr(service, "desk_write_lock", lambda db_: order.append("lock"))
    monkeypatch.setattr(ledger, "desk_write_lock", lambda db_: order.append("lock"))
    real_query = db.query

    def query(*a, **k):
        order.append("row")
        return real_query(*a, **k)

    open_p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                          isin="DE000KO000A1", underlying_isin=ALIASES["DAX"].isin, direction="long", status="open",
                          units=1, entry_price=Decimal(1), entry_at=NOW.astimezone(UTC))
    db.add(open_p)
    db.commit()
    monkeypatch.setattr(db, "query", query)
    assert service.claim_open(db, open_p) is True
    assert order[:2] == ["lock", "row"]
    order.clear()
    db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                        isin="DE000KO000A1", underlying_isin=ALIASES["DAX"].isin, direction="long",
                        status="proposed"))  # no proposal event: due at once
    db.commit()
    service.expire_proposals(db, NOW + timedelta(minutes=5))
    # One plain read finds what is due; the lock comes before the rows are locked and changed.
    assert order[0] == "row" and order[1:3] == ["lock", "row"]
    order.clear()
    real_get = db.get
    monkeypatch.setattr(db, "get", lambda *a, **k: order.append("row" if k.get("with_for_update") else "read")
                        or real_get(*a, **k))
    service.take(db, "missing", "n", None, NOW)
    # A plain read (and the network reads) first, without a lock; the locking read after the write lock.
    assert order[:3] == ["read", "lock", "row"]


# -- the /code-review of fb54e3d: the write lock must not be held across network reads ----------------

import httpx  # noqa: E402
import pytest  # noqa: E402
from desk_helpers import DAX_ETF, FakeSc, daily_points, ko_item, standard_handler  # noqa: E402

from app.decision.desk import jobs, news, telegram  # noqa: E402
from app.decision.desk.signals import owner_calls  # noqa: E402
from app.foundation import news_wire  # noqa: E402

KO = "DE000KO000A1"


@pytest.fixture
def no_news(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


def _logging_sc(log, quotes, listings=None):
    std = standard_handler(quotes, listings, chart_pts=daily_points())

    def handle(args):
        log.append("read")
        return std(args)
    return FakeSc(handle)


def test_an_idea_reads_every_product_before_its_first_ledger_write(monkeypatch, no_news):
    db = _memory_db()
    user = make_user(db)
    log: list[str] = []
    real = service.append_event
    monkeypatch.setattr(service, "append_event", lambda *a, **k: log.append("write") or real(*a, **k))
    wi = {"isin": "DE000WA000A1", "strategy": "call", "issuer": "X", "omega": 8, "delta": 0.5,
          "implied_volatility": 0.2, "underlying_isin": ALIASES["DAX"].isin,
          "expiry_date": {"epoch_day": 20819}}
    sc = _logging_sc(log, {KO: ("0.995", "1.00"), "DE000WA000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                     {"knockout": [ko_item(KO, lev=4, distance=0.25)], "warrant": [wi]})
    out = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up both"), sc.cli(), NOW)
    assert [r.position is not None for r in out.proposals] == [True, True]
    assert "write" in log and "read" not in log[log.index("write"):]  # both products read, then written


def test_the_exit_checks_read_every_price_before_closing(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    for isin in (KO, "DE000KO000B2"):
        db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                            isin=isin, underlying_isin=ALIASES["DAX"].isin, direction="long", status="open", units=1,
                            entry_price=Decimal(1), entry_fee=Decimal("0.99"), stop_price=Decimal("0.9"),
                            entry_at=NOW.astimezone(UTC), max_exit_at=(NOW + timedelta(hours=5)).astimezone(UTC)))
    db.commit()
    log: list[str] = []
    real = service.close_position
    monkeypatch.setattr(service, "close_position", lambda *a, **k: log.append("write") or real(*a, **k))
    sc = _logging_sc(log, {KO: ("0.5", "0.51"), "DE000KO000B2": ("0.5", "0.51"), DAX_ETF: ("100", "100.1")})
    assert len(service.manage_open_positions(db, sc.cli(), NOW, {})) == 2
    assert log.index("write") > max(i for i, x in enumerate(log) if x == "read")


def test_a_tap_that_waited_for_the_lock_is_judged_when_it_runs(monkeypatch, no_news):
    db = _memory_db()
    user = make_user(db)
    sc = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                                 {"knockout": [ko_item(KO, lev=4, distance=0.25)]}, chart_pts=daily_points()))
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=sc.cli(), now=NOW)
    arrived = NOW + timedelta(seconds=170)
    tap = service.take(db, res.position.id, res.nonce, sc.cli(), arrived, ALIASES["DAX"],
                       clock=lambda: arrived + timedelta(seconds=70))  # 70 s behind the monitor's tick
    assert tap.status == "expired"


def test_exit_messages_go_out_even_when_a_later_stage_fails(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                     isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="closed", units=1,
                     entry_price=Decimal(1), exit_price=Decimal("0.5"), net_eur=Decimal("-2.48"))
    db.add(p)
    db.commit()
    sent: list[str] = []
    monkeypatch.setattr(telegram, "alert", lambda db_, uid, text: sent.append(text))
    monkeypatch.setattr(service, "manage_open_positions", lambda *a, **k: [(p, "stop")])

    def fire_fails(*a, **k):
        raise httpx.ConnectTimeout("alpaca")

    monkeypatch.setattr("app.decision.desk.signals.index_momentum.fire_due_signals", fire_fails)
    with pytest.raises(httpx.ConnectTimeout):
        jobs.monitor_tick(db, NOW, FakeSc(standard_handler({})).cli())
    assert len(sent) == 1 and sent[0].startswith("Desk paper exit (stop)")


def test_spy_pre_market_prints_do_not_knock_out_an_spx_product(monkeypatch):
    class Provider:
        def get_intraday_bars(self, symbol, timeframe, start, end, **kw):
            pre = datetime(2026, 11, 18, 8, 0, tzinfo=service.cal.NEW_YORK).astimezone(UTC)
            regular = datetime(2026, 11, 18, 10, 0, tzinfo=service.cal.NEW_YORK).astimezone(UTC)
            return [{"t": pre, "l": Decimal("500"), "h": Decimal("501"), "c": Decimal("500")},
                    {"t": regular, "l": Decimal("600"), "h": Decimal("601"), "c": Decimal("600.5")}]

    monkeypatch.setattr(news, "alpaca_for", lambda db: Provider())
    since = datetime(2026, 11, 17, 21, 0, tzinfo=UTC)
    path = service.reference_path(None, None, ALIASES["SPX"], since, since + timedelta(days=1))
    assert path == [Decimal("600"), Decimal("601")]


def test_a_feed_link_that_is_not_a_web_url_is_dropped():
    item = news_wire.WireItem("handelsblatt", "1", "h", None, url="javascript:alert(1)")
    assert item.url is None
    assert news_wire.WireItem("handelsblatt", "2", "h", None, url=" https://example.org/a ").url == "https://example.org/a"


def test_a_long_issuer_name_is_cut_to_the_column(no_news):
    db = _memory_db()
    user = make_user(db)
    item = ko_item(KO, lev=4, distance=0.25, issuer="MORGAN STANLEY & CO. INTERNATIONAL PLC")
    sc = FakeSc(standard_handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, {"knockout": [item]},
                                 chart_pts=daily_points()))
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=sc.cli(), now=NOW)
    assert res.position is not None and len(res.position.issuer) <= 32


def test_a_redelivered_webhook_update_is_handled_once():
    from app.interface.api import telegram as webhook

    update = {"update_id": 987654321, "message": {"chat": {"id": 1}, "text": "/idea DAX up"}}
    assert webhook._first_delivery(update) is True
    assert webhook._first_delivery(update) is False
    assert webhook._first_delivery({"message": {}}) is True  # no id: handled
