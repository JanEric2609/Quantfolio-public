"""Tests for the gvkey<->ISIN<->app-symbol point-in-time fundamentals join."""
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering.fundamentals_loader import load_fundamentals_extract
from app.foundation.data_engineering.pit_fundamentals import (
    pit_fundamentals_for_symbol,
    resolve_isin_for_symbol,
)
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
)
from app.foundation.models.entities import Security, SecurityAlias, SecurityListing
from app.foundation.security_master import _alias_sha256


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_security(db, isin: str, symbol: str) -> None:
    security = Security(isin=isin, canonical_name=f"{symbol} Inc", base_currency="EUR")
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id, mic="XETR", exchange_label="XETRA", symbol=symbol, currency="EUR"
        )
    )
    db.commit()


def _seed_panels(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    identifiers_csv = tmp_path / "identifiers.csv"
    identifiers_csv.write_text("gvkey,isin,fic,datadate\n1001,DE0001,DEU,2024-01-01\n")
    result = load_security_identifiers_extract(identifiers_csv, dry_run=False)
    assert result.ok is True, result.error

    fundamentals_csv = tmp_path / "fundamentals.csv"
    fundamentals_csv.write_text(
        "gvkey,datadate,fyear,indfmt,consol,popsrc,datafmt,curcd,at,ceq,ni,revt,csho,epspx,dltt\n"
        "1001,2023-12-31,2023,INDL,C,I,STD,EUR,500.0,100.0,20.0,300.0,50.0,2.0,80.0\n"
    )
    result2 = load_fundamentals_extract(fundamentals_csv, dry_run=False)
    assert result2.ok is True, result2.error


def test_resolve_isin_for_symbol_found_and_not_found():
    db = _memory_db()
    _seed_security(db, "DE0001", "TESTCO")

    assert resolve_isin_for_symbol(db, "TESTCO") == "DE0001"
    assert resolve_isin_for_symbol(db, "NOPE") is None
    assert resolve_isin_for_symbol(db, "") is None


def test_resolve_isin_for_symbol_falls_back_to_alias_cache_without_listing():
    """An OpenFIGI-resolved listing may be keyed by OpenFIGI's own ticker
    spelling (e.g. "ADS") rather than this app's own symbol (e.g. "ADS.DE").
    The SecurityAlias permanent cache is keyed by the exact alias string
    resolve_security() was called with, so it survives that mismatch --
    verify the fallback finds the ISIN via the alias when no SecurityListing
    row matches the app symbol at all."""
    db = _memory_db()
    security = Security(isin="DE0005190003", canonical_name="Adidas AG", base_currency="EUR")
    db.add(security)
    db.flush()
    db.add(
        SecurityAlias(
            alias_sha256=_alias_sha256("ADS.DE"),
            source="discover_pipeline",
            alias_value="ADS.DE",
            security_id=security.id,
        )
    )
    db.commit()

    assert resolve_isin_for_symbol(db, "ADS.DE") == "DE0005190003"
    assert resolve_isin_for_symbol(db, "UNRELATED") is None


def test_pit_fundamentals_for_symbol_returns_none_without_isin(tmp_path, monkeypatch):
    _seed_panels(tmp_path, monkeypatch)
    db = _memory_db()

    dates = pd.DatetimeIndex(["2024-07-01"])
    assert pit_fundamentals_for_symbol(db, "UNRESOLVED", dates) is None


def test_pit_fundamentals_for_symbol_joins_identifier_and_fundamentals_pointintime(
    tmp_path, monkeypatch
):
    _seed_panels(tmp_path, monkeypatch)
    db = _memory_db()
    _seed_security(db, "DE0001", "TESTCO")

    dates = pd.DatetimeIndex(["2024-01-01", "2024-07-01", "2024-12-01"])
    frame = pit_fundamentals_for_symbol(db, "TESTCO", dates)

    assert frame is not None
    assert list(frame.index) == list(dates)
    # gvkey link is live from 2024-01-01 on for every row.
    assert (frame["gvkey"] == "001001").all()
    # Fundamentals only become knowable at available_from = datadate + 6mo = 2024-06-30.
    assert pd.isna(frame.loc["2024-01-01", "eps"])
    assert frame.loc["2024-07-01", "eps"] == 2.0
    assert frame.loc["2024-12-01", "eps"] == 2.0
    assert frame.loc["2024-07-01", "common_equity"] == 100.0
    assert frame.loc["2024-07-01", "net_income"] == 20.0
    assert frame.loc["2024-07-01", "shares_outstanding"] == 50.0


def test_pit_fundamentals_for_symbol_none_when_isin_outside_extract(tmp_path, monkeypatch):
    _seed_panels(tmp_path, monkeypatch)
    db = _memory_db()
    _seed_security(db, "FR9999", "OTHERCO")

    dates = pd.DatetimeIndex(["2024-07-01"])
    assert pit_fundamentals_for_symbol(db, "OTHERCO", dates) is None
