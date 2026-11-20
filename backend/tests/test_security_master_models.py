"""Model tests for the staged security-master persistence layer (T2.1).

Covers round-trip persistence of Security + listings + alias, ON DELETE
CASCADE behaviour, and the uniqueness contracts that make listings and the
permanent alias cache idempotent.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import Security, SecurityAlias, SecurityListing


def _fk_pragma_on(dbapi_connection, _connection_record) -> None:
    # SQLite enforces foreign keys (and thus ON DELETE CASCADE) only when
    # explicitly asked; PostgreSQL does so by default. Enable it so cascade
    # behaves in tests as it does in production.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", _fk_pragma_on)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _make_security() -> Security:
    return Security(
        isin="US0378331005",
        canonical_name="Apple Inc.",
        asset_type="stock",
        base_currency="EUR",
        ucits=False,
        domicile_country="United States",
        ter=None,
        provider_meta_json='{"figi": "BBG000B9XRY4"}',
    )


def test_security_with_listings_and_alias_round_trips() -> None:
    db = _memory_db()
    security = _make_security()
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XETR",
            exchange_label="XETRA",
            symbol="APC.DE",
            currency="EUR",
            is_primary=True,
        )
    )
    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XNAS",
            exchange_label="NASDAQ",
            symbol="AAPL",
            currency="USD",
            is_primary=False,
        )
    )
    db.commit()

    loaded = db.query(Security).filter_by(isin="US0378331005").one()
    assert loaded.canonical_name == "Apple Inc."
    assert loaded.asset_type == "stock"
    assert loaded.base_currency == "EUR"
    assert loaded.ucits is False
    assert loaded.domicile_country == "United States"
    assert loaded.provider_meta_json == '{"figi": "BBG000B9XRY4"}'
    assert loaded.created_at is not None
    assert loaded.updated_at is not None

    listings = {listing.mic: listing for listing in db.query(SecurityListing).filter_by(security_id=loaded.id)}
    assert set(listings) == {"XETR", "XNAS"}
    assert listings["XETR"].exchange_label == "XETRA"
    assert listings["XETR"].symbol == "APC.DE"
    assert listings["XETR"].is_primary is True
    assert listings["XNAS"].currency == "USD"

    alias = SecurityAlias(
        alias_sha256=_sha256("apple inc. 850"),
        source="dkb_wire",
        alias_value="APPLE INC. 850",
        security_id=loaded.id,
    )
    db.add(alias)
    db.commit()

    cached = db.query(SecurityAlias).filter_by(source="dkb_wire").one()
    assert cached.alias_sha256 == _sha256("apple inc. 850")
    assert cached.alias_value == "APPLE INC. 850"
    assert cached.security_id == loaded.id
    assert cached.resolved_at is not None


def test_deleting_security_cascades_to_listings_and_aliases() -> None:
    db = _memory_db()
    security = _make_security()
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XETR",
            exchange_label="XETRA",
            symbol="APC.DE",
            currency="EUR",
            is_primary=True,
        )
    )
    db.add(
        SecurityAlias(
            alias_sha256=_sha256("apple"),
            source="isin",
            alias_value="US0378331005",
            security_id=security.id,
        )
    )
    db.commit()

    target = db.query(Security).filter_by(isin="US0378331005").one()
    db.delete(target)
    db.commit()

    assert db.query(SecurityListing).count() == 0
    assert db.query(SecurityAlias).count() == 0


def test_duplicate_mic_symbol_listing_raises_integrity_error() -> None:
    db = _memory_db()
    security = _make_security()
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XETR",
            exchange_label="XETRA",
            symbol="APC.DE",
            currency="EUR",
            is_primary=True,
        )
    )
    db.commit()

    db.add(
        SecurityListing(
            security_id=security.id,
            mic="XETR",
            exchange_label="XETRA",
            symbol="APC.DE",
            currency="EUR",
            is_primary=False,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_duplicate_source_alias_hash_raises_integrity_error() -> None:
    db = _memory_db()
    security = _make_security()
    db.add(security)
    db.flush()
    digest = _sha256("apple")
    db.add(
        SecurityAlias(
            alias_sha256=digest,
            source="dkb_wire",
            alias_value="APPLE INC. 850",
            security_id=security.id,
        )
    )
    db.commit()

    db.add(
        SecurityAlias(
            alias_sha256=digest,
            source="dkb_wire",
            alias_value="APPLE 850 O.N.",
            security_id=security.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_migration_module_imports_with_correct_revision_chain() -> None:
    migration_path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0102_security_master_tables.py"
    spec = importlib.util.spec_from_file_location("_mig_0102_security_master_tables", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0102_security_master_tables"
    assert module.down_revision == "0100_ac_tuning_trials"
