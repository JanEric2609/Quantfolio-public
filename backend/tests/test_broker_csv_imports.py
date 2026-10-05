from datetime import datetime
from app.foundation.imports.parse import parse_csv


def test_parse_comdirect_csv():
    csv_content = """Buchungstag;Wertstellung;Vorgang;Buchungstext;Betrag EUR
01.02.2025;03.02.2025;Kauf;Wertpapierkauf WKN 870747 / ISIN DE0007164600 SAP SE; -1500,50
05.02.2025;07.02.2025;Verkauf;Wertpapierverkauf WKN 710000 / ISIN DE0007100000 Mercedes-Benz Group; 2300,00
""".encode("latin1")

    txs, errors = parse_csv(csv_content, "comdirect")
    assert not errors
    assert len(txs) == 2
    assert txs[0].side == "buy"
    assert txs[0].amount == 1500.50
    assert txs[0].isin == "DE0007164600"
    assert txs[1].side == "sell"
    assert txs[1].amount == 2300.00
    assert txs[1].isin == "DE0007100000"


def test_parse_trade_republic_csv():
    csv_content = """Datum;Typ;Wert;Betrag;ISIN;Name
2025-01-15T10:00:00.000Z;Kauf;500.00;-500.00;IE00B4L5Y983;iShares Core MSCI World UCITS ETF
2025-02-10T14:30:00.000Z;Verkauf;1200.00;1200.00;IE00B5BMR087;iShares Core S&P 500 UCITS ETF
""".encode("utf-8")

    txs, errors = parse_csv(csv_content, "trade_republic")
    assert not errors
    assert len(txs) == 2
    assert txs[0].side == "buy"
    assert txs[0].amount == 500.00
    assert txs[0].isin == "IE00B4L5Y983"
    assert txs[1].side == "sell"
    assert txs[1].amount == 1200.00
    assert txs[1].isin == "IE00B5BMR087"


def test_commit_records_the_trade_side_as_activity_type():
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.foundation.core.db import Base
    from app.foundation.imports.commit import commit_import
    from app.foundation.imports.parse import ParsedTransaction
    from app.foundation.models.entities import ActivityLedgerEntry, User

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()

    def txn(side: str, day: int) -> ParsedTransaction:
        return ParsedTransaction(date=datetime(2026, 9, day), description="MSCI World", amount=500.0,
                                 currency="EUR", isin="IE00B4L5Y983", ticker=None, side=side, quantity=None)

    created, errors = commit_import(db, user.id, [txn("buy", 1), txn("sell", 2)])

    assert not errors and len(created) == 2
    # the amount is stored unsigned, so without this the side is lost; lot
    # seeding (tax_cockpit.seed_lots_from_activity) reads "buy"
    types = {e.date.day: e.activity_type for e in db.query(ActivityLedgerEntry).all()}
    assert types == {1: "buy", 2: "sell"}
