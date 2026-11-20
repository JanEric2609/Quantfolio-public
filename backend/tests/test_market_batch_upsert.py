"""Batch persistence of a symbol's price history (audit D2, scalability).

``market.history`` used to write live history through ``_upsert_price`` once per
bar: one SELECT per bar (~1,250 for a 5-year symbol) plus a currency lookup per
new row. ``_upsert_prices`` does one SELECT, one bulk INSERT and one bulk
UPDATE. These tests pin that the stored result is identical to the sequential
loop and that the query count collapses.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from conftest import _memory_db
from sqlalchemy import event

from app.foundation import market
from app.foundation.data_backbone.listing_currency import record_listing_currency
from app.foundation.models.entities import ListingCurrency, PriceCache


def _bars(n: int, *, start: date = date(2021, 1, 4), currency: str | None = "USD", base: float = 100.0):
    out = []
    for i in range(n):
        item = {
            "date": start + timedelta(days=i),
            "open": base + i,
            "high": base + i + 1,
            "low": base + i - 1,
            "close": base + i + 0.5,
            "volume": 1000 + i,
            "source": "unit",
        }
        if currency:
            item["currency"] = currency
        out.append(item)
    return out


def _stored(db, ticker: str = "AAPL"):
    rows = db.query(PriceCache).filter(PriceCache.ticker == ticker).order_by(PriceCache.date).all()
    return [
        (
            r.date,
            None if r.open is None else float(r.open),
            None if r.high is None else float(r.high),
            None if r.low is None else float(r.low),
            float(r.close),
            None if r.volume is None else float(r.volume),
            r.source,
            r.currency,
            r.stale,
        )
        for r in rows
    ]


def _sequential(db, ticker, items):
    """The previous implementation: one ``_upsert_price`` per bar.

    Committed per bar so a repeated date is a true sequential overwrite (with the
    app's ``autoflush=False`` sessions the old loop raised IntegrityError when a
    provider repeated a date inside one batch).
    """
    for item in items:
        market._upsert_price(db, ticker, item, commit=True, record_currency=False)


def _count_statements(db):
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, executemany):
        seen.append(("MANY " if executemany else "") + statement.lstrip().split()[0].upper())

    event.listen(db.get_bind(), "before_cursor_execute", _record)
    return seen


def test_batch_matches_the_sequential_loop_on_a_mixed_history():
    seed = [
        # A stored row the new batch updates, with a currency the batch does not report.
        {"date": date(2021, 1, 6), "close": 1.0, "currency": "GBP", "source": "old"},
        {"date": date(2021, 1, 9), "close": 2.0, "currency": "GBP", "source": "old"},
    ]
    batch = [
        *_bars(6, currency=None),                                  # Jan 4..9, no currency reported
        {"date": date(2021, 1, 5), "close": 555.0, "source": "dup"},  # repeated date: last wins
        {"date": date(2021, 1, 20), "close": float("nan")},       # invalid: skipped
        {"date": date(2021, 1, 21), "close": None},               # invalid: skipped
        {"date": datetime(2021, 1, 22, 15, 30, tzinfo=UTC), "close": 7.0, "volume": 5},  # datetime date
        {"date": "2021-01-23", "close": 8.0},                     # ISO string date
        {"date": date(2021, 1, 24), "close": 9.0, "currency": "CHF"},  # reports its own currency
    ]

    sequential_db, batch_db = _memory_db(), _memory_db()
    _sequential(sequential_db, "AAPL", seed)
    market._upsert_prices(batch_db, "AAPL", seed)
    # SQLite's Date type only takes date objects, so the per-bar oracle is fed
    # the normalised dates; the batch normalises datetime/ISO-string dates itself.
    _sequential(sequential_db, "AAPL", [{**item, "date": market._as_date(item["date"])} for item in batch])
    market._upsert_prices(batch_db, "AAPL", batch)

    assert _stored(batch_db) == _stored(sequential_db)
    stored = _stored(batch_db)
    # Spot checks that the comparison is not vacuous.
    by_date = {row[0]: row for row in stored}
    assert by_date[date(2021, 1, 5)][4] == 555.0 and by_date[date(2021, 1, 5)][6] == "dup"
    assert by_date[date(2021, 1, 6)][7] == "GBP"          # stored currency kept
    assert by_date[date(2021, 1, 6)][6] == "unit"         # ...but the row was updated
    assert by_date[date(2021, 1, 24)][7] == "CHF"
    assert date(2021, 1, 20) not in by_date and date(2021, 1, 21) not in by_date
    assert by_date[date(2021, 1, 22)][5] == 5.0 and date(2021, 1, 23) in by_date


def test_new_rows_without_a_currency_use_the_resolved_listing_currency_once():
    db = _memory_db()
    record_listing_currency(db, "SHEL.L", "GBp", "test")
    db.commit()
    statements = _count_statements(db)

    market._upsert_prices(db, "SHEL.L", _bars(50, currency=None))

    assert {r.currency for r in db.query(PriceCache).filter(PriceCache.ticker == "SHEL.L")} == {"GBp"}
    # One lookup of the listing currency, not one per bar.
    assert statements.count("SELECT") <= 3
    assert db.query(ListingCurrency).count() == 1


def test_the_batch_leaves_the_listing_currency_to_the_caller():
    db = _memory_db()
    market._upsert_prices(db, "AAPL", _bars(5, currency="USD"))
    db.commit()
    assert db.get(ListingCurrency, "AAPL") is None  # history() records it, as before


def test_five_years_of_bars_cost_a_constant_number_of_statements():
    bars = _bars(1250)

    old_db = _memory_db()
    old_log = _count_statements(old_db)
    _sequential(old_db, "AAPL", bars)
    old = list(old_log)

    new_db = _memory_db()
    new_log = _count_statements(new_db)
    market._upsert_prices(new_db, "AAPL", bars)
    new = list(new_log)

    assert _stored(new_db) == _stored(old_db)
    assert old.count("SELECT") >= 1250
    # One stored-dates SELECT (+ the listing-currency lookup), one executemany INSERT.
    assert new.count("SELECT") <= 2
    assert len(new) <= 6
    assert "MANY INSERT" in new

    # Re-running over the same range updates in one executemany, still one SELECT.
    again_log = _count_statements(new_db)
    market._upsert_prices(new_db, "AAPL", _bars(1250, base=200.0))
    again = list(again_log)
    assert again.count("SELECT") <= 2
    assert "MANY UPDATE" in again
    assert new_db.query(PriceCache).count() == 1250
    assert _stored(new_db)[0][4] == 200.5


def test_new_rows_get_distinct_primary_keys_and_defaults():
    db = _memory_db()
    market._upsert_prices(db, "AAPL", _bars(30))
    rows = db.query(PriceCache).all()
    assert len({r.id for r in rows}) == 30
    assert all(len(r.id) == 36 for r in rows)
    assert all(r.fetched_at is not None and r.stale is False for r in rows)


def test_commit_false_leaves_the_transaction_to_the_caller():
    db = _memory_db()
    market._upsert_prices(db, "AAPL", _bars(3), commit=False)
    db.rollback()
    assert db.query(PriceCache).count() == 0


def test_an_empty_or_all_invalid_batch_writes_nothing():
    db = _memory_db()
    assert market._upsert_prices(db, "AAPL", []) == 0
    assert market._upsert_prices(db, "AAPL", [{"date": date(2021, 1, 4), "close": float("inf")}]) == 0
    assert db.query(PriceCache).count() == 0


def test_history_persists_live_bars_in_one_batch(monkeypatch):
    db = _memory_db()
    live = [
        {**bar, "date": date.today() - timedelta(days=300 - i)}
        for i, bar in enumerate(_bars(300))
    ]

    class _Registry:
        def get_history(self, ticker, days=None):
            return {"ok": True, "data": [{**bar, "source": "fake"} for bar in live], "provider": "fake"}

    monkeypatch.setattr(market, "build_provider_registry", lambda _db: _Registry())
    statements = _count_statements(db)

    rows = market.history(db, "aapl", days=365)

    assert len(rows) == 300
    listing = db.get(ListingCurrency, "AAPL")
    assert listing is not None and listing.currency == "USD" and listing.source == "fake_metadata"
    assert [r["close"] for r in rows] == [bar["close"] for bar in live]
    assert db.query(PriceCache).filter(PriceCache.ticker == "AAPL").count() == 300
    # The per-bar SELECT is gone: stored-date lookup is a single statement.
    price_cache_selects = [s for s in statements if s == "SELECT"]
    assert len(price_cache_selects) < 20
