"""Tests for pit_panel_joins.pit_ml_features_for_symbol — the Phase 5
composition of the WRDS-factor/IBES/insider PIT joins into one per-row ML
feature frame for app.lab.quant_ml.build_features' extra_features param.

Fixture style mirrors test_data_engineering_pit_panel_joins.py exactly."""
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering.ibes_estimates_loader import load_ibes_estimates_extract
from app.foundation.data_engineering.insider_trading_loader import load_insider_trading_extract
from app.foundation.data_engineering.pit_panel_joins import pit_ml_features_for_symbol
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
)
from app.foundation.data_engineering.wrds_factors_loader import load_wrds_factors_extract
from app.foundation.models.entities import Security, SecurityListing

_WRDS_COLUMNS = ("me", "mom_12_1", "be_me", "gp_at", "at_gr1")
_IBES_COLUMNS = ("sue", "revision_momentum", "dispersion")
_INSIDER_COLUMNS = ("cluster_buy_score", "net_insider_flow_usd")


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_security(db, isin: str, symbol: str) -> None:
    security = Security(isin=isin, canonical_name=f"{symbol} Inc", base_currency="USD")
    db.add(security)
    db.flush()
    db.add(
        SecurityListing(
            security_id=security.id, mic="XNAS", exchange_label="NASDAQ", symbol=symbol, currency="USD"
        )
    )
    db.commit()


def _seed_identifiers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    identifiers_csv = tmp_path / "identifiers.csv"
    identifiers_csv.write_text("gvkey,isin,fic,datadate\n1001,US0001,USA,2020-01-01\n")
    result = load_security_identifiers_extract(identifiers_csv, dry_run=False)
    assert result.ok is True, result.error


def _seed_wrds_factors(tmp_path: Path) -> None:
    characteristics_csv = tmp_path / "characteristics.csv"
    characteristics_csv.write_text(
        "gvkey,permno,eom,excntry,size_grp,me,mom_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m\n"
        "1001,9001,2023-12-31,USA,large,5000.0,0.15,0.6,0.25,0.05,0.99\n"
    )
    result = load_wrds_factors_extract(characteristics_csv, dry_run=False)
    assert result.ok is True, result.error


def _seed_ibes(tmp_path: Path) -> None:
    ibes_csv = tmp_path / "ibes.csv"
    ibes_csv.write_text(
        "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
        "highest,lowest,actual,anndats_act,curr,usfirm\n"
        "0001,,TESTCO,2023-10-15,2024-12-31,1,EPS,5,2.00,2.00,0.10,2.10,1.90,,,USD,1\n"
        "0001,,TESTCO,2023-11-15,2024-12-31,1,EPS,5,2.20,2.20,0.10,2.30,2.10,2.35,2023-11-10,USD,1\n"
    )
    result = load_ibes_estimates_extract(ibes_csv, dry_run=False)
    assert result.ok is True, result.error


def _write_insider_tsvs(source_dir: Path, rows: list[dict]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    submission_lines = ["ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL"]
    owner_lines = ["ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP"]
    trans_lines = [
        "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
        "TRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP"
    ]
    for row in rows:
        submission_lines.append(
            f"{row['accession']}\t{row['filing_date']}\tForm4\t{row['issuer_cik']}\t"
            f"Testco Inc\t{row['ticker']}"
        )
        owner_lines.append(f"{row['accession']}\t{row['cik']}\t{row['insider_name']}\tOfficer")
        trans_lines.append(
            f"{row['accession']}\t{row['transaction_date']}\t{row['code']}\t{row['shares']}\t"
            f"{row['price']}\tA\t1000\tD"
        )

    (source_dir / "SUBMISSION.tsv").write_text("\n".join(submission_lines) + "\n")
    (source_dir / "REPORTINGOWNER.tsv").write_text("\n".join(owner_lines) + "\n")
    (source_dir / "NONDERIV_TRANS.tsv").write_text("\n".join(trans_lines) + "\n")


def _seed_insider(tmp_path: Path) -> None:
    source_dir = tmp_path / "sec_extract"
    rows = [
        {
            "accession": "0001", "filing_date": "05-DEC-2023", "issuer_cik": "500",
            "ticker": "TESTCO", "cik": "1", "insider_name": "Alice CEO",
            "transaction_date": "03-DEC-2023", "code": "P", "shares": "1000", "price": "10.00",
        },
        # Another issuer's filing: the extract runs to late January 2024, so
        # a TESTCO window ending 2024-01-15 is covered (empty, not unknown).
        {
            "accession": "0002", "filing_date": "25-JAN-2024", "issuer_cik": "600",
            "ticker": "ELSECO", "cik": "9", "insider_name": "Dan Director",
            "transaction_date": "23-JAN-2024", "code": "S", "shares": "10", "price": "5.00",
        },
    ]
    _write_insider_tsvs(source_dir, rows)
    result = load_insider_trading_extract(source_dir, dry_run=False)
    assert result.ok is True, result.error


def test_all_three_sources_present(tmp_path, monkeypatch):
    _seed_identifiers(tmp_path, monkeypatch)
    _seed_wrds_factors(tmp_path)
    _seed_ibes(tmp_path)
    _seed_insider(tmp_path)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")

    dates = pd.DatetimeIndex(["2023-12-10", "2024-01-15"])
    out = pit_ml_features_for_symbol(db, "TESTCO", dates)

    assert isinstance(out, pd.DataFrame)
    assert list(out.index) == list(dates)
    for col in (*_WRDS_COLUMNS, *_IBES_COLUMNS, *_INSIDER_COLUMNS):
        assert col in out.columns

    # 2024-01-15 postdates all three sources' first observations. The
    # insider filing (03-Dec-2023) is outside the 30-day cluster window by
    # then but still inside the 90-day flow window.
    row = out.loc["2024-01-15"]
    assert row["me"] == 5000.0
    assert row["cluster_buy_score"] == 0
    assert row["net_insider_flow_usd"] == pytest.approx(10000.0)
    assert row["wrds_factor_available"] == 1.0
    assert row["ibes_estimate_available"] == 1.0
    assert row["insider_signal_available"] == 1.0


def test_some_sources_missing_are_nan_with_zero_flag(tmp_path, monkeypatch):
    """Only WRDS seeded -- IBES/insider never loaded at all for this panel
    dir, so their columns must come back NaN + a 0.0 availability flag, not
    a raised exception or a dropped column."""
    _seed_identifiers(tmp_path, monkeypatch)
    _seed_wrds_factors(tmp_path)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")

    dates = pd.DatetimeIndex(["2024-01-15"])
    out = pit_ml_features_for_symbol(db, "TESTCO", dates)

    assert out["wrds_factor_available"].iloc[0] == 1.0
    assert out["me"].iloc[0] == 5000.0

    for col in _IBES_COLUMNS:
        assert pd.isna(out[col].iloc[0])
    assert out["ibes_estimate_available"].iloc[0] == 0.0

    for col in _INSIDER_COLUMNS:
        assert pd.isna(out[col].iloc[0])
    assert out["insider_signal_available"].iloc[0] == 0.0


def test_all_sources_missing_never_raises_never_returns_none(tmp_path, monkeypatch):
    """A symbol with zero coverage anywhere (no identifiers loaded, no
    ticker match anywhere) must still come back as a full-shaped, all-NaN
    DataFrame -- pit_ml_features_for_symbol's whole contract (unlike the
    three functions it composes) is that it never returns None."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    dates = pd.DatetimeIndex(["2024-01-15", "2024-02-15"])
    out = pit_ml_features_for_symbol(db, "NOPE", dates)

    assert isinstance(out, pd.DataFrame)
    assert len(out) == 2
    for col in (*_WRDS_COLUMNS, *_IBES_COLUMNS, *_INSIDER_COLUMNS):
        assert out[col].isna().all()
    for flag_col in ("wrds_factor_available", "ibes_estimate_available", "insider_signal_available"):
        assert (out[flag_col] == 0.0).all()


def test_train_and_serve_callers_get_identical_values(tmp_path, monkeypatch):
    """The whole point of resolving this per-row from PIT history (rather
    than a broadcast snapshot) is that two different callers asking for the
    same (symbol, date) get the same answer -- simulates a training-time
    caller and a serve-time caller both hitting this function independently."""
    _seed_identifiers(tmp_path, monkeypatch)
    _seed_wrds_factors(tmp_path)
    _seed_ibes(tmp_path)
    _seed_insider(tmp_path)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")

    shared_date = pd.DatetimeIndex(["2024-01-15"])

    # "training" caller: a wide multi-date array (mirrors build_panel /
    # train_and_validate_ticker_model's 400+ row history).
    training_dates = pd.DatetimeIndex(["2023-12-01", "2023-12-15", "2024-01-15"])
    train_out = pit_ml_features_for_symbol(db, "TESTCO", training_dates)

    # "serve" caller: a single as-of-now timestamp (mirrors stage_ml_signal).
    serve_out = pit_ml_features_for_symbol(db, "TESTCO", shared_date)

    pd.testing.assert_series_equal(
        train_out.loc["2024-01-15"], serve_out.loc["2024-01-15"], check_names=False,
    )
