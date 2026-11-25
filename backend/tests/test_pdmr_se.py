"""Sweden's managers' transactions register (Finansinspektionen): parsing, storage, paging."""
from __future__ import annotations

from datetime import date

from conftest import _memory_db

from app.foundation.data_engineering import pdmr_se
from app.foundation.models.entities import PdmrTransaction

HEAD = (
    "<tr><th>Publication date</th><th>Issuer</th><th>Person discharging managerial responsibilities</th>"
    "<th>Position</th><th>Closely associated</th><th>Nature of transaction</th><th>Instrument name</th>"
    "<th>Intrument type</th><th>ISIN</th><th>Transaction date</th><th>Volume</th><th>Unit</th>"
    "<th>Price</th><th>Currency</th><th>Status</th><th>Details</th></tr>"
)


def _row(pub, issuer, person, nature, isin, tx, volume, price, closely="", position="Chief Financial Officer (CFO)", status="", kind="Share"):
    return (
        f"<tr><td>{pub}</td><td>{issuer}</td><td>{person}</td><td>{position}</td><td>{closely}</td>"
        f"<td>{nature}</td><td>{issuer} B</td><td>{kind}</td><td>{isin}</td><td>{tx}</td>"
        f"<td>{volume}</td><td>Quantity</td><td>{price}</td><td>SEK</td><td>{status}</td><td><a href='/x'>Notification</a></td></tr>"
    )


def _page(*rows):
    return f"<html><body><table>{HEAD}{''.join(rows)}</table></body></html>"


INTEA = _row("26/10/2026", "Intea Fastigheter AB (publ)", "Magnus Ekström", "Acquisition", "SE0017072259", "23/10/2026", "250", "62.00")
PRISJAKT = _row("27/10/2026", "Prisjakt AB (publ)", "Stina Bergfors", "Disposal", "SE0030361564", "22/10/2026", "18,337", "31.00", "Yes", "Member of the Board of Directors")
ALLOT = _row("26/10/2026", "W5 Solutions AB (publ)", "Jonas Rydin", "Allotment", "SE0016786040", "23/10/2026", "416,666", "24.00", "Yes")


def test_parse_page_reads_the_published_layout():
    rows = pdmr_se.parse_page(_page(PRISJAKT, INTEA, ALLOT))
    assert [r.side for r in rows] == ["sell", "buy", "other"]
    sale = rows[0]
    assert sale.isin == "SE0030361564" and sale.volume == 18337.0 and sale.price == 31.0
    assert sale.transaction_date == date(2026, 10, 22) and sale.publication_date == date(2026, 10, 27)
    assert sale.closely_associated is True and sale.currency == "SEK"
    assert rows[1].closely_associated is False and rows[1].person == "Magnus Ekström"


def test_parse_page_without_the_expected_table_is_empty():
    assert pdmr_se.parse_page("<html><table><tr><td>nothing</td></tr></table></html>") == []
    assert pdmr_se.parse_page("") == []


def test_parse_page_tolerates_column_reordering_and_extra_columns():
    html = (
        "<table><tr><th>Extra</th><th>ISIN</th><th>Issuer</th><th>Person discharging managerial responsibilities</th>"
        "<th>Nature of transaction</th><th>Publication date</th><th>Transaction date</th><th>Volume</th><th>Price</th></tr>"
        "<tr><td>z</td><td>SE0017072259</td><td>Intea</td><td>A B</td><td>Acquisition</td>"
        "<td>26/10/2026</td><td>23/10/2026</td><td>1,000</td><td>5.5</td></tr></table>"
    )
    (row,) = pdmr_se.parse_page(html)
    assert row.side == "buy" and row.volume == 1000.0 and row.price == 5.5 and row.isin == "SE0017072259"


def test_bad_isin_and_numbers_become_none():
    html = _page(_row("26/10/2026", "X AB", "P Q", "Acquisition", "NOT-AN-ISIN", "bad", "n/a", ""))
    (row,) = pdmr_se.parse_page(html)
    assert row.isin is None and row.transaction_date is None and row.volume is None and row.price is None


def test_store_is_idempotent():
    db = _memory_db()
    rows = pdmr_se.parse_page(_page(PRISJAKT, INTEA))
    assert pdmr_se.store(db, rows) == 2
    db.commit()
    assert pdmr_se.store(db, rows) == 0
    assert db.query(PdmrTransaction).count() == 2


def test_refresh_resumes_a_capped_backfill_then_stops_at_a_known_page():
    db = _memory_db()
    old = _row("01/01/2020", "Old AB", "O P", "Acquisition", "SE0000000001", "30/12/2019", "1", "1")
    pages = {1: _page(PRISJAKT, INTEA), 2: _page(ALLOT), 3: _page(old)}
    calls: list[int] = []

    def fetch(p: int) -> str:
        calls.append(p)
        return pages[p]

    today = date(2026, 10, 28)
    first = pdmr_se.refresh_pdmr_se(db, fetch=fetch, sleep=lambda s: None, today=today, max_pages=1)
    assert first["new"] == 2 and first["done"] is False and calls == [1]
    calls.clear()
    second = pdmr_se.refresh_pdmr_se(db, fetch=fetch, sleep=lambda s: None, today=today)
    assert calls == [2, 3] and second["done"] is True  # resumed at the saved page, ended at the cutoff
    calls.clear()
    third = pdmr_se.refresh_pdmr_se(db, fetch=fetch, sleep=lambda s: None, today=today)
    assert calls == [1] and third["new"] == 0 and third["done"] is True  # page 1 is all known


def test_refresh_finishes_a_backfill_at_the_cutoff():
    db = _memory_db()
    old = _row("01/01/2020", "Old AB", "O P", "Acquisition", "SE0000000001", "30/12/2019", "1", "1")
    stats = pdmr_se.refresh_pdmr_se(
        db, fetch=lambda p: _page(old), sleep=lambda s: None, today=date(2026, 10, 28),
    )
    assert stats["done"] is True and stats["pages"] == 1 and stats["new"] == 1


def test_a_changed_layout_is_counted_not_hidden():
    db = _memory_db()
    stats = pdmr_se.refresh_pdmr_se(db, fetch=lambda p: "<html>new design</html>", sleep=lambda s: None)
    assert stats["failed_pages"] == 1 and stats["new"] == 0 and stats["done"] is False


def test_a_network_error_ends_the_run_without_raising():
    db = _memory_db()

    def boom(page: int) -> str:
        raise OSError("down")

    stats = pdmr_se.refresh_pdmr_se(db, fetch=boom, sleep=lambda s: None)
    assert stats["failed_pages"] == 1 and stats["pages"] == 0


def test_recent_buys_filters_side_isin_and_window():
    db = _memory_db()
    pdmr_se.store(db, pdmr_se.parse_page(_page(PRISJAKT, INTEA, ALLOT)))
    db.commit()
    got = pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1))
    assert [g.person for g in got] == ["Magnus Ekström"]
    assert pdmr_se.recent_buys(db, "SE0030361564", days=30, today=date(2026, 11, 1)) == []  # a sale
    assert pdmr_se.recent_buys(db, "SE0017072259", days=3, today=date(2026, 11, 1)) == []  # too old


def test_revised_and_history_copies_are_one_row_and_history_never_wins():
    db = _memory_db()
    args = ("Intea Fastigheter AB (publ)", "Magnus Ekström", "Acquisition", "SE0017072259", "23/10/2026", "250", "62.00")
    history = _row("26/10/2026", *args, status="History")
    revised = _row("28/10/2026", *args, status="Revised")
    # History stored first, Revised arrives on a later night: one row, upgraded
    assert pdmr_se.store(db, pdmr_se.parse_page(_page(history))) == 1
    db.commit()
    assert pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1)) == []
    assert pdmr_se.store(db, pdmr_se.parse_page(_page(revised))) == 0
    db.commit()
    (row,) = db.query(PdmrTransaction).all()
    assert row.status == "Revised" and row.publication_date == date(2026, 10, 28)
    assert len(pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1))) == 1
    # both on one page, either order: still one row
    db2 = _memory_db()
    assert pdmr_se.store(db2, pdmr_se.parse_page(_page(revised, history))) == 1
    db2.commit()
    assert db2.query(PdmrTransaction).one().status == "Revised"


def test_corrected_notification_is_not_counted_next_to_its_original():
    db = _memory_db()
    base = ("Intea Fastigheter AB (publ)", "Magnus Ekström", "Acquisition", "SE0017072259", "23/10/2026")
    original = _row("24/10/2026", *base, "1000", "50.00")
    history = _row("28/10/2026", *base, "1000", "50.00", status="History")
    revised = _row("28/10/2026", *base, "800", "50.00", status="Revised")
    pdmr_se.store(db, pdmr_se.parse_page(_page(original)))
    db.commit()
    # the correction arrives on a later night: Revised (new key) and the History copy of the original
    pdmr_se.store(db, pdmr_se.parse_page(_page(revised, history)))
    db.commit()
    got = pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1))
    assert [g.volume for g in got] == [800.0]


def test_second_amendment_retires_the_first_revised_row():
    db = _memory_db()
    base = ("Intea Fastigheter AB (publ)", "Magnus Ekström", "Acquisition", "SE0017072259", "23/10/2026")
    first = _row("28/10/2026", *base, "800", "50.00", status="Revised")
    pdmr_se.store(db, pdmr_se.parse_page(_page(first)))
    db.commit()
    # amended again: the 800 row's History copy is published later than its Revised row, and 700 is the new Revised
    history = _row("30/10/2026", *base, "800", "50.00", status="History")
    second = _row("30/10/2026", *base, "700", "50.00", status="Revised")
    pdmr_se.store(db, pdmr_se.parse_page(_page(second, history)))
    db.commit()
    got = pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1))
    assert [g.volume for g in got] == [700.0]
    # fetching the same pages again changes nothing
    pdmr_se.store(db, pdmr_se.parse_page(_page(first, second, history)))
    db.commit()
    assert [g.volume for g in pdmr_se.recent_buys(db, "SE0017072259", days=30, today=date(2026, 11, 1))] == [700.0]


def test_subscriptions_and_non_share_instruments_are_not_buys():
    db = _memory_db()
    sub = _row("26/10/2026", "A AB", "P Q", "Subscription", "SE0000000002", "23/10/2026", "10", "1")
    warrant = _row("26/10/2026", "B AB", "R S", "Acquisition", "SE0000000003", "23/10/2026", "10", "1", kind="Warrant")
    assert [r.side for r in pdmr_se.parse_page(_page(sub))] == ["other"]
    pdmr_se.store(db, pdmr_se.parse_page(_page(sub, warrant)))
    db.commit()
    assert pdmr_se.recent_buys(db, "SE0000000002", days=30, today=date(2026, 11, 1)) == []
    assert pdmr_se.recent_buys(db, "SE0000000003", days=30, today=date(2026, 11, 1)) == []
