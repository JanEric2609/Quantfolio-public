"""Tests for the CCM / WRDS-factor-characteristics / IBES / insider-trading
point-in-time symbol joins."""
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering.ccm_link_loader import load_ccm_link_extract
from app.foundation.data_engineering.ibes_estimates_loader import load_ibes_estimates_extract
from app.foundation.data_engineering.insider_trading_loader import load_insider_trading_extract
from app.foundation.data_engineering.pit_panel_joins import (
    pit_ccm_link_quality_for_symbol,
    pit_ibes_estimate_signal_for_symbol,
    pit_insider_signal_for_symbol,
    pit_wrds_factor_characteristics_for_symbol,
)
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
)
from app.foundation.data_engineering.wrds_factors_loader import load_wrds_factors_extract
from app.foundation.models.entities import Security, SecurityListing


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


def test_pit_wrds_factor_characteristics_for_symbol(tmp_path, monkeypatch):
    _seed_identifiers(tmp_path, monkeypatch)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")

    characteristics_csv = tmp_path / "characteristics.csv"
    characteristics_csv.write_text(
        "gvkey,permno,eom,excntry,size_grp,me,mom_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m\n"
        "1001,9001,2023-12-31,USA,large,5000.0,0.15,0.6,0.25,0.05,0.99\n"
    )
    result = load_wrds_factors_extract(characteristics_csv, dry_run=False)
    assert result.ok is True, result.error

    dates = pd.DatetimeIndex(["2024-01-15"])
    joined = pit_wrds_factor_characteristics_for_symbol(db, "TESTCO", dates)
    assert joined is not None
    row = joined.iloc[0]
    assert row["gvkey"] == "001001"
    assert row["me"] == 5000.0
    assert row["mom_12_1"] == 0.15
    assert row["data_confidence"] == "gvkey_verified"
    assert "ret_exc_lead1m" not in joined.columns

    assert pit_wrds_factor_characteristics_for_symbol(db, "NOPE", dates) is None


def test_pit_ccm_link_quality_for_symbol(tmp_path, monkeypatch):
    _seed_identifiers(tmp_path, monkeypatch)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")

    ccm_csv = tmp_path / "ccm.csv"
    ccm_csv.write_text(
        "gvkey,permno,permco,linkdt,linkenddt,linktype,linkprim\n"
        "1001,9001,8001,2019-01-01,,LC,P\n"
    )
    result = load_ccm_link_extract(ccm_csv, dry_run=False)
    assert result.ok is True, result.error

    quality = pit_ccm_link_quality_for_symbol(db, "TESTCO", "2024-01-15")
    assert quality is not None
    assert quality["gvkey"] == "001001"
    assert quality["permnos"] == [9001]
    assert quality["research_quality_link"] is True
    assert quality["data_confidence"] == "gvkey_verified"

    assert pit_ccm_link_quality_for_symbol(db, "NOPE", "2024-01-15") is None


def test_pit_ibes_estimate_signal_for_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    ibes_csv = tmp_path / "ibes.csv"
    ibes_csv.write_text(
        "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
        "highest,lowest,actual,anndats_act,curr,usfirm\n"
        "0001,,TESTCO,2023-10-15,2024-12-31,1,EPS,5,2.00,2.00,0.10,2.10,1.90,,,USD,1\n"
        "0001,,TESTCO,2023-11-15,2024-12-31,1,EPS,5,2.20,2.20,0.10,2.30,2.10,2.35,2023-11-10,USD,1\n"
    )
    result = load_ibes_estimates_extract(ibes_csv, dry_run=False)
    assert result.ok is True, result.error

    dates = pd.DatetimeIndex(["2023-12-01"])
    joined = pit_ibes_estimate_signal_for_symbol(db, "TESTCO", dates)
    assert joined is not None
    row = joined.iloc[0]
    assert row["data_confidence"] == "ticker_match"
    # meanest rose 2.00 -> 2.20 between the two snapshots.
    assert row["revision_momentum"] == pytest.approx(0.10)
    # sue from the second (actual-populated) snapshot: (2.35 - 2.20) / 0.10
    assert row["sue"] == pytest.approx(1.5)

    assert pit_ibes_estimate_signal_for_symbol(db, "NOPE", dates) is None


def test_pit_ibes_estimate_signal_strips_exchange_suffix(tmp_path, monkeypatch):
    """WRDS's IBES International file gives oftic without the yfinance-style
    exchange suffix (e.g. "RWE", not "RWE.DE") -- regression test for the
    join that silently missed every European candidate as a result."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    ibes_csv = tmp_path / "ibes.csv"
    ibes_csv.write_text(
        "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
        "highest,lowest,actual,anndats_act,curr,usfirm\n"
        "0002,,RWE,2023-10-15,2024-12-31,1,EPS,5,2.00,2.00,0.10,2.10,1.90,,,EUR,0\n"
    )
    result = load_ibes_estimates_extract(ibes_csv, dry_run=False)
    assert result.ok is True, result.error

    dates = pd.DatetimeIndex(["2023-12-01"])
    joined = pit_ibes_estimate_signal_for_symbol(db, "RWE.DE", dates)
    assert joined is not None
    assert joined.iloc[0]["data_confidence"] == "ticker_match"


def _write_insider_tsvs(source_dir: Path, rows: list[dict]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    # AFF10B5ONE only when a row sets "plan": sets before 2023 lack the column.
    with_plan = any("plan" in row for row in rows)
    submission_lines = [
        "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL"
        + ("\tAFF10B5ONE" if with_plan else "")
    ]
    owner_lines = ["ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP"]
    trans_lines = [
        "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
        "TRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP"
    ]
    for row in rows:
        submission_lines.append(
            f"{row['accession']}\t{row['filing_date']}\tForm4\t{row['issuer_cik']}\t"
            f"Testco Inc\t{row['ticker']}" + (f"\t{row.get('plan', '')}" if with_plan else "")
        )
        owner_lines.append(f"{row['accession']}\t{row['cik']}\t{row['insider_name']}\tOfficer")
        trans_lines.append(
            f"{row['accession']}\t{row['transaction_date']}\t{row['code']}\t{row['shares']}\t"
            f"{row['price']}\tA\t1000\tD"
        )

    (source_dir / "SUBMISSION.tsv").write_text("\n".join(submission_lines) + "\n")
    (source_dir / "REPORTINGOWNER.tsv").write_text("\n".join(owner_lines) + "\n")
    (source_dir / "NONDERIV_TRANS.tsv").write_text("\n".join(trans_lines) + "\n")


def test_pit_insider_signal_for_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    source_dir = tmp_path / "sec_extract"
    rows = [
        # Opportunistic cluster buy: two distinct insiders buying within days of each other.
        {
            "accession": "0001",
            "filing_date": "05-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "1",
            "insider_name": "Alice CEO",
            "transaction_date": "03-DEC-2023",
            "code": "P",
            "shares": "1000",
            "price": "10.00",
        },
        {
            "accession": "0002",
            "filing_date": "07-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "2",
            "insider_name": "Bob CFO",
            "transaction_date": "06-DEC-2023",
            "code": "P",
            "shares": "500",
            "price": "10.00",
        },
        # A sale, dollar-weighted against the buys in net_insider_flow_usd.
        {
            "accession": "0003",
            "filing_date": "08-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "3",
            "insider_name": "Carol Director",
            "transaction_date": "07-DEC-2023",
            "code": "S",
            "shares": "200",
            "price": "10.00",
        },
    ]
    _write_insider_tsvs(source_dir, rows)
    result = load_insider_trading_extract(source_dir, dry_run=False)
    assert result.ok is True, result.error

    dates = pd.DatetimeIndex(["2023-12-10"])
    joined = pit_insider_signal_for_symbol(db, "TESTCO", dates)
    assert joined is not None
    row = joined.iloc[0]
    assert row["data_confidence"] == "ticker_match"
    assert row["cluster_buy_score"] == 2
    # (1000*10) + (500*10) - (200*10) = 13000
    assert row["net_insider_flow_usd"] == pytest.approx(13000.0)

    assert pit_insider_signal_for_symbol(db, "NOPE", dates) is None


def test_pit_insider_signal_for_symbol_accepts_tz_aware_dates(tmp_path, monkeypatch):
    """Regression test: stage_insider_signal (pipeline.py) always calls this
    with pd.Timestamp.now(UTC) -- a tz-aware timestamp -- while filing_date
    in the loaded SEC data is tz-naive. Before this fix that raised
    "Cannot compare tz-naive and tz-aware timestamps" for every US ticker,
    silently breaking insider signal entirely."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    source_dir = tmp_path / "sec_extract"
    rows = [
        {
            "accession": "0001",
            "filing_date": "05-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "1",
            "insider_name": "Alice CEO",
            "transaction_date": "03-DEC-2023",
            "code": "P",
            "shares": "1000",
            "price": "10.00",
        },
        {
            "accession": "0002",
            "filing_date": "07-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "2",
            "insider_name": "Bob CFO",
            "transaction_date": "06-DEC-2023",
            "code": "P",
            "shares": "500",
            "price": "10.00",
        },
        {
            "accession": "0003",
            "filing_date": "08-DEC-2023",
            "issuer_cik": "500",
            "ticker": "TESTCO",
            "cik": "3",
            "insider_name": "Carol Director",
            "transaction_date": "07-DEC-2023",
            "code": "S",
            "shares": "200",
            "price": "10.00",
        },
    ]
    _write_insider_tsvs(source_dir, rows)
    result = load_insider_trading_extract(source_dir, dry_run=False)
    assert result.ok is True, result.error

    naive_dates = pd.DatetimeIndex(["2023-12-10"])
    tz_aware_dates = pd.DatetimeIndex(["2023-12-10"], tz="UTC")

    expected = pit_insider_signal_for_symbol(db, "TESTCO", naive_dates)
    joined = pit_insider_signal_for_symbol(db, "TESTCO", tz_aware_dates)

    assert joined is not None
    assert expected is not None
    row = joined.iloc[0]
    assert row["data_confidence"] == "ticker_match"
    assert row["cluster_buy_score"] == expected.iloc[0]["cluster_buy_score"] == 2
    assert row["net_insider_flow_usd"] == pytest.approx(
        expected.iloc[0]["net_insider_flow_usd"]
    )
    assert row["net_insider_flow_usd"] == pytest.approx(13000.0)
    assert joined.index[0] == tz_aware_dates[0]


def test_pit_insider_signal_excludes_routine_trader(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()

    source_dir = tmp_path / "sec_extract"
    rows = []
    # Same insider, same calendar month, same direction, 3 consecutive years:
    # classic Cohen/Malloy/Pomorski "routine" trader -- must be excluded.
    for i, year in enumerate((2021, 2022, 2023)):
        rows.append(
            {
                "accession": f"100{i}",
                "filing_date": f"05-JUN-{year}",
                "issuer_cik": "500",
                "ticker": "TESTCO",
                "cik": "9",
                "insider_name": "Routine Insider",
                "transaction_date": f"03-JUN-{year}",
                "code": "P",
                "shares": "1000",
                "price": "10.00",
            }
        )
    _write_insider_tsvs(source_dir, rows)
    result = load_insider_trading_extract(source_dir, dry_run=False)
    assert result.ok is True, result.error

    dates = pd.DatetimeIndex(["2023-06-10"])
    joined = pit_insider_signal_for_symbol(db, "TESTCO", dates)
    assert joined is not None
    row = joined.iloc[0]
    # The only trade in the window is the excluded routine one.
    assert row["cluster_buy_score"] == 0
    assert row["net_insider_flow_usd"] == 0.0


def test_pit_insider_signal_excludes_10b5_1_plan_trades(tmp_path, monkeypatch):
    """Trades from a filing that ticked the Rule 10b5-1 box were scheduled in
    advance: they leave the signal even without the three years of history
    the routine filter needs. SEC ships the flag as 1/0 and true/false."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()
    base = {"issuer_cik": "500", "ticker": "TESTCO", "price": "10.00"}
    rows = [
        {**base, "accession": "0001", "filing_date": "05-DEC-2023", "cik": "1", "insider_name": "Alice CEO",
         "transaction_date": "03-DEC-2023", "code": "P", "shares": "1000", "plan": "0"},
        {**base, "accession": "0002", "filing_date": "07-DEC-2023", "cik": "2", "insider_name": "Bob CFO",
         "transaction_date": "06-DEC-2023", "code": "P", "shares": "500", "plan": "1"},
        {**base, "accession": "0003", "filing_date": "08-DEC-2023", "cik": "3", "insider_name": "Carol Director",
         "transaction_date": "07-DEC-2023", "code": "S", "shares": "200", "plan": "true"},
        {**base, "accession": "0004", "filing_date": "08-DEC-2023", "cik": "4", "insider_name": "Dan Director",
         "transaction_date": "07-DEC-2023", "code": "S", "shares": "300", "plan": "false"},
    ]
    _write_insider_tsvs(tmp_path / "sec_extract", rows)
    result = load_insider_trading_extract(tmp_path / "sec_extract", dry_run=False)
    assert result.ok is True, result.error

    joined = pit_insider_signal_for_symbol(db, "TESTCO", pd.DatetimeIndex(["2023-12-10"]))

    assert joined is not None
    assert joined.iloc[0]["cluster_buy_score"] == 1  # Bob's plan purchase is out
    assert joined.iloc[0]["net_insider_flow_usd"] == pytest.approx(10000.0 - 3000.0)  # Carol's plan sale is out


def test_pit_insider_signal_reads_partitions_without_the_plan_column(tmp_path, monkeypatch):
    """Partitions written before the column existed read as "unknown", which
    is not a plan: their trades stay in the signal."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    db = _memory_db()
    base = {"issuer_cik": "500", "ticker": "TESTCO", "price": "10.00", "code": "P", "shares": "100"}
    _write_insider_tsvs(tmp_path / "old", [
        {**base, "accession": "0001", "filing_date": "05-DEC-2023", "cik": "1", "insider_name": "A",
         "transaction_date": "03-DEC-2023"},
    ])
    _write_insider_tsvs(tmp_path / "new", [
        {**base, "accession": "0002", "filing_date": "06-DEC-2023", "cik": "2", "insider_name": "B",
         "transaction_date": "04-DEC-2023", "plan": "1"},
    ])
    assert load_insider_trading_extract(tmp_path / "old", dry_run=False).ok
    assert load_insider_trading_extract(tmp_path / "new", dry_run=False).ok
    old_partition = tmp_path / "insider_trading_pit" / "old.parquet"
    pd.read_parquet(old_partition).drop(columns=["plan_10b5_1"]).to_parquet(old_partition, index=False)

    joined = pit_insider_signal_for_symbol(db, "TESTCO", pd.DatetimeIndex(["2023-12-10"]))

    assert joined is not None
    assert joined.iloc[0]["cluster_buy_score"] == 1


def _load_ibes(tmp_path: Path, rows: list[str]) -> None:
    ibes_csv = tmp_path / "ibes.csv"
    ibes_csv.write_text(
        "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
        "highest,lowest,actual,anndats_act,curr,usfirm\n" + "\n".join(rows) + "\n"
    )
    result = load_ibes_estimates_extract(ibes_csv, dry_run=False)
    assert result.ok is True, result.error


def test_ibes_revision_is_not_measured_across_a_fiscal_year_roll(tmp_path, monkeypatch):
    # LRCX, prod 2026-09-28: FPI 1 rolled from the June-2026 year (5.69) to
    # the June-2027 year (9.41) and read as a +65% one-month revision.
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _load_ibes(tmp_path, [
        "LRCX,,LRCX,2026-06-18,2026-06-30,1,EPS,32,5.68,5.68,0.04,5.80,5.59,,,USD,1",
        "LRCX,,LRCX,2026-07-16,2026-06-30,1,EPS,31,5.69,5.69,0.04,5.80,5.62,,,USD,1",
        "LRCX,,LRCX,2026-08-20,2027-06-30,1,EPS,28,9.34,9.41,0.48,10.45,8.44,,,USD,1",
    ])
    db = _memory_db()

    joined = pit_ibes_estimate_signal_for_symbol(db, "LRCX", pd.DatetimeIndex(["2026-07-20", "2026-09-28"]))

    assert joined is not None
    assert joined["revision_momentum"].iloc[0] == pytest.approx(0.01 / 5.68)
    assert pd.isna(joined["revision_momentum"].iloc[1])


def test_ibes_consensus_older_than_a_quarter_is_not_carried_forward(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _load_ibes(tmp_path, [
        "OLDCO,,OLDCO,2023-10-15,2024-12-31,1,EPS,5,2.00,2.00,0.10,2.10,1.90,,,USD,1",
        "OLDCO,,OLDCO,2023-11-15,2024-12-31,1,EPS,5,2.20,2.20,0.10,2.30,2.10,,,USD,1",
    ])
    db = _memory_db()

    joined = pit_ibes_estimate_signal_for_symbol(db, "OLDCO", pd.DatetimeIndex(["2023-12-01", "2026-09-28"]))

    assert joined is not None
    assert joined["revision_momentum"].iloc[0] == pytest.approx(0.10)
    assert pd.isna(joined["revision_momentum"].iloc[1])


def test_insider_windows_past_the_extract_are_unknown_not_zero(tmp_path, monkeypatch):
    # Prod's extract ends 2024-03-29; on 2026-09-28 every matched name read
    # cluster 0 / flow 0 and scored as "no insider trading".
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    source_dir = tmp_path / "sec_extract"
    _write_insider_tsvs(source_dir, [
        {
            "accession": "0001", "filing_date": "05-DEC-2023", "issuer_cik": "500",
            "ticker": "TESTCO", "cik": "1", "insider_name": "Alice CEO",
            "transaction_date": "03-DEC-2023", "code": "P", "shares": "1000", "price": "10.00",
        },
        {
            "accession": "0002", "filing_date": "29-MAR-2024", "issuer_cik": "600",
            "ticker": "ELSECO", "cik": "9", "insider_name": "Dan Director",
            "transaction_date": "27-MAR-2024", "code": "S", "shares": "10", "price": "5.00",
        },
    ])
    assert load_insider_trading_extract(source_dir, dry_run=False).ok
    db = _memory_db()

    joined = pit_insider_signal_for_symbol(
        db, "TESTCO", pd.DatetimeIndex(["2023-12-10", "2024-03-01", "2026-09-28"])
    )

    assert joined is not None
    assert joined["cluster_buy_score"].tolist()[:2] == [1.0, 0.0]
    assert joined["net_insider_flow_usd"].iloc[1] == pytest.approx(10000.0)
    assert pd.isna(joined["cluster_buy_score"].iloc[2])
    assert pd.isna(joined["net_insider_flow_usd"].iloc[2])
