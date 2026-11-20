"""CSV imports carry quantity, price and fees so imported trades can seed tax lots.

The parsers used to emit no quantity or price, so ``seed_lots_from_activity`` (which
skips ledger buys without a quantity) could never turn an imported trade into a lot.
Fixtures are small inline CSVs. The comdirect / Trade Republic layouts here are the
ones the existing parsers already assume; no real export of either was available to
pin them, so these tests prove the extraction logic, not conformance to a broker.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from conftest import _memory_db

from app.foundation.imports.commit import commit_import
from app.foundation.imports.parse import ParsedTransaction, parse_csv
from app.foundation.models.entities import ActivityLedgerEntry, TaxLot, User
from app.foundation.tax_cockpit import seed_lots_from_activity

ISIN = "IE00B4L5Y983"

# DKB account statement; the settlement text names quantity, rate and fee.
DKB_CSV = (
    "Buchungstag,Wertstellung,Umsatztyp,Begünstigter / Auftraggeber,Verwendungszweck,Betrag,Saldo\n"
    f'01.09.2026,03.09.2026,Wertpapier,DKB Depot,"Kauf {ISIN} iShares Core MSCI World 10 Stück Kurs 80,00 EUR Gebühr 1,50 EUR","-801,50","5.000,00"\n'
    f'15.09.2026,17.09.2026,Wertpapier,DKB Depot,"Verkauf {ISIN} iShares Core MSCI World Stk. 4 Kurs 90,00 EUR","359,00","5.359,00"\n'
    f'20.09.2026,22.09.2026,Wertpapier,DKB Depot,"Stückzinsen 12,50 ISIN DE0001102580 Bund","-12,50","5.346,50"\n'
).encode("cp1252")


def _user(db) -> User:
    user = User(username="importer", password_hash="x")
    db.add(user)
    db.commit()
    return user


def test_dkb_row_with_labelled_text_yields_quantity_price_and_fees():
    txs, errors = parse_csv(DKB_CSV, "dkb")

    assert not errors
    buy, sell, accrued = txs
    assert (buy.side, buy.isin, buy.amount) == ("buy", ISIN, 801.50)
    assert (buy.quantity, buy.price, buy.fees) == (10.0, 80.0, 1.5)
    assert (sell.side, sell.quantity, sell.price, sell.fees) == ("sell", 4.0, 90.0, None)
    # "Stückzinsen" is accrued interest, not a share count: nothing is guessed.
    assert (accrued.quantity, accrued.price, accrued.fees) == (None, None, None)


def test_price_is_derived_from_the_cash_amount_when_only_a_quantity_is_known():
    csv = (
        "Buchungstag,Wertstellung,Umsatztyp,Verwendungszweck,Betrag,Saldo\n"
        f'01.09.2026,03.09.2026,Wertpapier,"Kauf {ISIN} 12,5 Stück Gebühr 4,90","-1.004,90","0,00"\n'
        f'02.09.2026,04.09.2026,Wertpapier,"Verkauf {ISIN} 12,5 Stück Gebühr 4,90","1.000,00","0,00"\n'
    ).encode("cp1252")

    buy, sell = parse_csv(csv, "dkb")[0]

    # buy: amount = quantity * price + fees; sell: amount = quantity * price - fees
    assert (buy.quantity, buy.fees) == (12.5, 4.9)
    assert round(buy.price * buy.quantity + buy.fees, 6) == 1004.90
    assert round(sell.price * sell.quantity - sell.fees, 6) == 1000.00


def test_german_thousands_separator_in_a_quantity():
    csv = (
        "Buchungstag,Umsatztyp,Verwendungszweck,Betrag\n"
        f'01.09.2026,Wertpapier,"Kauf {ISIN} Stück 1.000 Kurs 2,50","-2.500,00"\n'
    ).encode("cp1252")
    (buy,), _ = parse_csv(csv, "dkb")
    assert (buy.quantity, buy.price) == (1000.0, 2.5)


def test_rows_without_a_quantity_are_unchanged():
    csv = (
        "Buchungstag,Umsatztyp,Verwendungszweck,Betrag\n"
        f'01.09.2026,Wertpapier,"Wertpapierabrechnung {ISIN} Order 4711","-500,00"\n'
    ).encode("cp1252")
    (buy,), errors = parse_csv(csv, "dkb")
    assert not errors
    assert (buy.quantity, buy.price, buy.fees) == (None, None, None)
    assert buy.amount == 500.0


def test_trade_republic_reads_explicit_columns_and_ignores_the_fx_rate_column():
    csv = (
        "Datum;Typ;Wert;Betrag;ISIN;Name;Anzahl;Kurs;Währungskurs;Gebühren\n"
        f"2026-01-15T10:00:00.000Z;Kauf;500.00;-501.00;{ISIN};iShares Core MSCI World;5;100.00;1.08;1.00\n"
        f"2026-02-10T14:30:00.000Z;Verkauf;1200.00;1200.00;IE00B5BMR087;iShares Core S&P 500;-2;600.00;1.08;\n"
    ).encode()

    (buy, sell), errors = parse_csv(csv, "trade_republic")

    assert not errors
    assert (buy.side, buy.quantity, buy.price, buy.fees) == ("buy", 5.0, 100.0, 1.0)  # not the 1.08 FX rate
    assert (sell.side, sell.quantity, sell.price, sell.fees) == ("sell", 2.0, 600.0, None)


def test_comdirect_reads_a_quantity_column_and_derives_the_price():
    csv = (
        "Buchungstag;Wertstellung;Vorgang;Buchungstext;Betrag EUR;Stück;Provision\n"
        f"01.02.2025;03.02.2025;Kauf;Wertpapierkauf ISIN DE0007164600 SAP SE;-1500,50;10;4,90\n"
    ).encode("latin1")

    (buy,), errors = parse_csv(csv, "comdirect")

    assert not errors
    assert (buy.quantity, buy.fees) == (10.0, 4.9)
    assert round(buy.price, 6) == round((1500.50 - 4.90) / 10, 6)


def _txn(**kw) -> ParsedTransaction:
    base = dict(date=datetime(2026, 9, 1), description="MSCI World", amount=801.5, currency="EUR",
                isin=ISIN, ticker=None, side="buy", quantity=10.0, price=80.0, fees=1.5)
    return ParsedTransaction(**{**base, **kw})


def test_commit_stores_quantity_price_and_fees_on_the_ledger_entry():
    db = _memory_db()
    user = _user(db)

    created, errors = commit_import(db, user.id, [_txn()])

    assert not errors and len(created) == 1
    entry = db.query(ActivityLedgerEntry).one()
    assert (entry.activity_type, entry.review_state) == ("buy", "pending")
    assert (entry.quantity, entry.price, entry.fees) == (Decimal("10"), Decimal("80"), Decimal("1.5"))


def test_imported_buy_seeds_a_tax_lot_through_the_ledger_path():
    db = _memory_db()
    user = _user(db)
    txs, _ = parse_csv(DKB_CSV, "dkb")
    commit_import(db, user.id, txs, source="csv_import_dkb")

    created = seed_lots_from_activity(db, user.id)

    assert created == 1  # the sell and the interest line do not open lots
    lot = db.query(TaxLot).one()
    assert lot.isin == ISIN
    assert lot.quantity_initial == lot.quantity_remaining == Decimal("10")
    assert lot.fees_eur == Decimal("1.5")
    assert lot.cost_basis_eur == Decimal("801.5")  # 10 * 80 + 1.5 == what left the account
    assert lot.source == "activity-ledger"
    assert seed_lots_from_activity(db, user.id) == 0  # idempotent


def test_a_buy_without_a_quantity_still_cannot_seed_a_lot():
    db = _memory_db()
    user = _user(db)
    commit_import(db, user.id, [_txn(quantity=None, price=None, fees=None)])
    assert seed_lots_from_activity(db, user.id) == 0


def test_reimporting_a_file_completes_entries_imported_before_quantities_were_read():
    db = _memory_db()
    user = _user(db)
    commit_import(db, user.id, [_txn(quantity=None, price=None, fees=None)])
    assert seed_lots_from_activity(db, user.id) == 0

    created, notes = commit_import(db, user.id, [_txn()])

    assert created == []  # no second entry
    assert notes == ["Updated quantity/price of existing entry: 2026-09-01 00:00:00 MSCI World"]
    assert db.query(ActivityLedgerEntry).count() == 1
    assert seed_lots_from_activity(db, user.id) == 1

    # A third upload of the same file is a plain duplicate again.
    _, notes = commit_import(db, user.id, [_txn()])
    assert notes[0].startswith("Duplicate:")


def test_identical_rows_in_one_file_are_a_duplicate_not_a_failed_commit():
    db = _memory_db()
    user = _user(db)

    created, notes = commit_import(db, user.id, [_txn(), _txn()])

    assert len(created) == 1
    assert len(notes) == 1 and notes[0].startswith("Duplicate:")
    assert db.query(ActivityLedgerEntry).count() == 1


def test_upload_then_seed_through_the_api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.foundation.auth import current_user
    from app.foundation.core.db import get_db
    from app.main import app

    db = _memory_db()
    user = _user(db)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/imports/commit?format_type=dkb",
            files={"file": ("dkb.csv", DKB_CSV, "text/csv")},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["created_ids"]) == 3

        seeded = client.post("/api/tax/lots/seed-from-activity")
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["created"] == 1

        lots = client.get("/api/tax/lots").json()
        assert lots["estimate"] is True and lots["not_tax_advice"] is True
        (lot,) = lots["items"]
        assert (lot["isin"], lot["quantity_initial"], lot["cost_basis_eur"]) == (ISIN, 10.0, 801.5)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(current_user, None)
