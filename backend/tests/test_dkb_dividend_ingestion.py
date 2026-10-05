"""Task 25 (audit-fixes-2026-08): DKB dividend ingestion MVP into tax_ledger_events.

Validation-first contract (see .omo/evidence/audit-fixes-2026-08/task-25-dkb-field-inventory.md):

- Only dividend-keyword references WITH a parseable ISIN, a positive amount and
  EUR currency are ingested; anything else is skipped with a logged warning.
- Amounts are never fabricated: gross_eur is the booked (net) cash credit,
  withholding fields stay 0, notes document the limitation.
- Rows are written through the EXISTING create_event writer and deduped on
  source_ref = "dkb:<DkbTransaction.id>" backed by a unique index.
- Ingestion failures are isolated per event AND as a whole: persist_snapshot
  must complete even when ingestion blows up.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import DkbAccount, DkbTransaction, TaxLedgerEvent, User
from app.foundation.tax_calc.jurisdictions.de.teilfreistellung import apply_teilfreistellung


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# Cache-only etf_universe evidence: matched record classifies as "aktien"
# (no bond/money-market keywords) -> 30 % Teilfreistellung per §20 InvStG.
AKTIEN_ETF_INDEX = {
    "IE00B4L5Y983": {"isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF"},
}


def _add_tx(db, account, *, reference, amount=Decimal("25.30"), currency="EUR"):
    tx = DkbTransaction(
        account_id=account.id,
        date=date(2026, 6, 15),
        amount=amount,
        currency=currency,
        reference=reference,
        source="dkb",
        dedupe_hash=uuid.uuid4().hex,
    )
    db.add(tx)
    db.commit()
    return tx


@pytest.fixture
def depot():
    """User + depot account with one parseable EUR dividend transaction."""
    db = _memory_db()
    user = User(username="div-user", password_hash="hash")
    db.add(user)
    db.commit()
    account = DkbAccount(
        user_id=user.id, type="depot", iban="DE02120300000000202051", balance=Decimal("100")
    )
    db.add(account)
    db.commit()
    tx = _add_tx(
        db,
        account,
        reference="DIVIDENDE GUTSCHRIFT ISIN IE00B4L5Y983 120,5 STK",
    )
    return db, user.id, account, tx


def _ingest(db, user_id, monkeypatch):
    from app.foundation.dkb import dividend_ingestion

    monkeypatch.setattr(dividend_ingestion, "_safe_etf_index", lambda: dict(AKTIEN_ETF_INDEX))
    return dividend_ingestion.ingest_dkb_dividends(db, user_id)


# ---------------------------------------------------------------------------
# Happy path: seeded transaction -> correct ledger row (amount/class/TF math)
# ---------------------------------------------------------------------------


def test_seeded_dividend_ingested_with_fund_class_and_tf_math(depot, monkeypatch):
    db, user_id, _account, _tx = depot

    result = _ingest(db, user_id, monkeypatch)

    assert result["ingested"] == 1
    events = db.query(TaxLedgerEvent).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.user_id == user_id
    assert ev.event_type == "dividend"
    assert ev.event_date == date(2026, 6, 15)
    assert ev.isin == "IE00B4L5Y983"
    # Booked cash credit recorded verbatim — never fabricated.
    assert ev.gross_eur == Decimal("25.30")
    assert ev.withheld_eur == Decimal("0")
    assert ev.foreign_wht_eur == Decimal("0")
    # Fund class from todo 22's classifier drives Teilfreistellung.
    assert ev.fund_class == "aktien"
    assert ev.teilfreistellung_pct == Decimal("0.30")
    # TF math: taxable portion after 30 % partial exemption.
    assert apply_teilfreistellung(ev.gross_eur, ev.teilfreistellung_pct) == Decimal("17.71")
    assert str(ev.source_ref).startswith("dkb:")
    assert ev.source == "dkb_sync"
    assert ev.confidence == "estimate"
    assert "net" in (ev.notes or "").lower()


# ---------------------------------------------------------------------------
# Idempotency: rerun produces ZERO duplicates
# ---------------------------------------------------------------------------


def test_rerun_produces_zero_duplicates(depot, monkeypatch):
    db, user_id, _account, _tx = depot

    first = _ingest(db, user_id, monkeypatch)
    second = _ingest(db, user_id, monkeypatch)

    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert second["skipped_duplicates"] == 1
    assert db.query(TaxLedgerEvent).count() == 1


# ---------------------------------------------------------------------------
# Unparseable dividend keyword without ISIN -> skipped WITH warning, no crash
# ---------------------------------------------------------------------------


def test_unparseable_dividend_skipped_with_warning(depot, monkeypatch, caplog):
    db, user_id, account, tx = depot
    db.delete(tx)
    db.commit()
    _add_tx(db, account, reference="DIVIDENDE GUTSCHRIFT ABRECHNUNG NR 4711")

    with caplog.at_level("WARNING", logger="app.foundation.dkb.dividend_ingestion"):
        result = _ingest(db, user_id, monkeypatch)

    assert result["ingested"] == 0
    assert result["skipped_unparseable"] == 1
    assert any("ISIN" in rec.message for rec in caplog.records)
    assert db.query(TaxLedgerEvent).count() == 0


def test_non_eur_dividend_skipped_without_amount_fabrication(depot, monkeypatch, caplog):
    db, user_id, account, tx = depot
    db.delete(tx)
    db.commit()
    _add_tx(
        db,
        account,
        reference="DIVIDENDE ISIN US0378331005 APPLE INC",
        amount=Decimal("12.40"),
        currency="USD",
    )

    with caplog.at_level("WARNING", logger="app.foundation.dkb.dividend_ingestion"):
        result = _ingest(db, user_id, monkeypatch)

    assert result["ingested"] == 0
    assert result["skipped_unparseable"] == 1
    assert db.query(TaxLedgerEvent).count() == 0


# ---------------------------------------------------------------------------
# Scope guard: non-dividend credits and debits are never ingested
# ---------------------------------------------------------------------------


def test_non_dividend_and_debit_transactions_ignored(depot, monkeypatch):
    db, user_id, account, tx = depot
    db.delete(tx)
    db.commit()
    _add_tx(db, account, reference="GEHALT SEPTEMBER ARBEITGEBER XY")
    _add_tx(db, account, reference="KAUF ISIN IE00B4L5Y983 10 STK", amount=Decimal("-850.00"))

    result = _ingest(db, user_id, monkeypatch)

    assert result["ingested"] == 0
    assert result["skipped_unparseable"] == 0
    assert db.query(TaxLedgerEvent).count() == 0


# ---------------------------------------------------------------------------
# Failure isolation: sync NEVER fails because of ingestion
# ---------------------------------------------------------------------------


def test_persist_snapshot_survives_ingestion_failure(monkeypatch):
    from app.foundation.dkb.service import DkbSyncService

    db = _memory_db()
    user = User(username="boom-user", password_hash="hash")
    db.add(user)
    db.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("ingestion exploded")

    from app.foundation.dkb import dividend_ingestion

    monkeypatch.setattr(dividend_ingestion, "_safe_etf_index", lambda: {})
    monkeypatch.setattr(dividend_ingestion, "ingest_dkb_dividends", _boom)

    service = DkbSyncService()
    snapshot = {
        "accounts": [
            {"iban": "DE-DEPOT-BOOM", "type": "depot", "balance": Decimal("0"), "currency": "EUR"},
        ],
        "transactions": [
            {
                "iban": "DE-DEPOT-BOOM",
                "date": date(2026, 6, 15),
                "amount": Decimal("25.30"),
                "currency": "EUR",
                "reference": "DIVIDENDE ISIN IE00B4L5Y983",
            }
        ],
        "positions": [],
    }

    service.persist_snapshot(db, user.id, snapshot)  # must not raise

    assert db.query(DkbTransaction).count() == 1  # transactions intact
    assert db.query(TaxLedgerEvent).count() == 0
