"""Tests for the SEC EDGAR insider-transactions loader.

``filing_date`` (not ``transaction_date``) is the point-in-time anchor this
table exists to provide (see module docstring), so the tests that matter
most pin the ``as_of`` filtering and the open-market transaction-code
filter. The loader's input is a three-file relational bundle (not a single
flat CSV, unlike every other loader in this package), so a second cluster of
tests pins the join behaviour: fan-out across joint filers and rejection of
an internally inconsistent extract.
"""
import shutil
import zipfile
from pathlib import Path

import pandas as pd

from app.foundation.data_engineering.insider_trading_loader import (
    load_insider_trading_extract,
    read_insider_trading,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sec_edgar_form345_sample"


def _write_quarter(
    tmp_path: Path,
    name: str,
    submission_rows: list[str],
    owner_rows: list[str],
    trans_rows: list[str],
) -> Path:
    quarter_dir = tmp_path / name
    quarter_dir.mkdir()
    (quarter_dir / "SUBMISSION.tsv").write_text(
        "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\n"
        + "\n".join(submission_rows)
        + "\n"
    )
    (quarter_dir / "REPORTINGOWNER.tsv").write_text(
        "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\n" + "\n".join(owner_rows) + "\n"
    )
    (quarter_dir / "NONDERIV_TRANS.tsv").write_text(
        "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
        "TRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\n"
        + "\n".join(trans_rows)
        + "\n"
    )
    return quarter_dir


def test_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_insider_trading_extract(FIXTURE_DIR, dry_run=True)

    assert result.ok is True, result.error
    assert result.rows_loaded == 3
    assert result.ciks == ["0001111111", "0002222222"]
    assert not (tmp_path / "insider_trading_pit").exists()


def test_apply_writes_the_panel_with_no_cusip_or_cleanse_code(tmp_path, monkeypatch):
    """SEC's raw filings carry neither field -- both are WRDS-derived."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_insider_trading_extract(FIXTURE_DIR, dry_run=False)

    assert result.ok is True, result.error
    written = sorted(p.name for p in (tmp_path / "insider_trading_pit").glob("*.parquet"))
    assert written == ["sec_edgar_form345_sample.parquet"]
    frame = pd.read_parquet(tmp_path / "insider_trading_pit" / written[0])
    assert (frame["source"] == "sec_edgar_form4_nonderivative").all()
    assert frame["cusip"].isna().all()
    assert frame["cleanse_code"].isna().all()


def test_loads_from_a_zip_the_same_as_from_a_directory(tmp_path, monkeypatch):
    """The real SEC download is a .zip -- exercise the extraction path too."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    zip_path = tmp_path / "2023q1_form345.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for tsv in FIXTURE_DIR.glob("*.tsv"):
            zf.write(tsv, arcname=tsv.name)

    result = load_insider_trading_extract(zip_path, dry_run=True)

    assert result.ok is True, result.error
    assert result.rows_loaded == 3


def test_read_as_of_filters_on_filing_date_not_transaction_date(tmp_path, monkeypatch):
    """The Jan 10 trade wasn't public until it was filed on Jan 12."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_insider_trading_extract(FIXTURE_DIR, dry_run=False)

    not_yet_public = read_insider_trading(as_of="2023-01-11")
    public_after_filing = read_insider_trading(as_of="2023-01-12")

    assert not_yet_public.empty
    assert len(public_after_filing) == 1
    assert public_after_filing.iloc[0]["transaction_code"] == "P"


def test_open_market_only_keeps_p_and_s_and_drops_award_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_insider_trading_extract(FIXTURE_DIR, dry_run=False)

    everything = read_insider_trading()
    open_market = read_insider_trading(open_market_only=True)

    assert len(everything) == 3
    assert len(open_market) == 2
    assert set(open_market["transaction_code"]) == {"P", "S"}


def test_joint_filers_produce_one_row_per_reporting_owner(tmp_path, monkeypatch):
    """A single Form 4 can be jointly filed by more than one insider (e.g.
    spouses). Verified against a real 2024 Q1 SEC extract, where ~1 filing in
    14 has 2+ reporting owners. Joining on ACCESSION_NUMBER must fan the
    transaction out to one row per co-filer, not silently pick one."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "joint_filers",
        submission_rows=["0000111111-23-000001\t01-APR-2023\t4\t0000999999\tJoint Co\tJTCO"],
        owner_rows=[
            "0000111111-23-000001\t0003333333\tAlice Spouse\tDirector",
            "0000111111-23-000001\t0004444444\tBob Spouse\tDirector",
        ],
        trans_rows=["0000111111-23-000001\t28-MAR-2023\tP\t100\t10.00\tA\t500\tD"],
    )

    result = load_insider_trading_extract(quarter, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 2
    frame = read_insider_trading()
    assert set(frame["cik"]) == {"0003333333", "0004444444"}
    assert (frame["transaction_date"] == pd.Timestamp("2023-03-28")).all()


def test_same_insider_same_day_different_issuers_are_not_collapsed(tmp_path, monkeypatch):
    """Real 2024 Q1 SEC data: Liberty Media Corp (CIK 0001560385) and Liberty
    Media LLC (CIK 0001082114) -- two distinct legal entities mid corporate
    restructuring -- both traded under ticker "LSXMA" in the same quarter. A
    dedup key keyed on ticker (not issuer_cik) silently merged an insider's
    trades in the two entities whenever share count, price, date and code
    happened to coincide -- verified against the real extract, where this
    collapsed 188 of 3,657 duplicate-looking groups across company lines."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "same_ticker_two_issuers",
        submission_rows=[
            "0000950170-24-005472\t14-MAR-2024\t4\t0001560385\tLiberty Media Corp\tLSXMA",
            "0000950170-24-002271\t14-MAR-2024\t4\t0001082114\tLiberty Media LLC\tLSXMA",
        ],
        owner_rows=[
            "0000950170-24-005472\t0000315090\tMalone John C\tDirector",
            "0000950170-24-002271\t0000315090\tMalone John C\tDirector",
        ],
        trans_rows=[
            "0000950170-24-005472\t12-MAR-2024\tS\t1000\t5.75\tD\t868990\tD",
            "0000950170-24-002271\t12-MAR-2024\tS\t1000\t5.75\tD\t868990\tD",
        ],
    )

    result = load_insider_trading_extract(quarter, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 2
    assert result.duplicate_rows_dropped == 0
    frame = read_insider_trading()
    assert set(frame["issuer_cik"]) == {"0001560385", "0001082114"}


def test_rejects_missing_required_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    shutil.copy(FIXTURE_DIR / "SUBMISSION.tsv", incomplete / "SUBMISSION.tsv")
    shutil.copy(FIXTURE_DIR / "NONDERIV_TRANS.tsv", incomplete / "NONDERIV_TRANS.tsv")

    result = load_insider_trading_extract(incomplete, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required file" in result.error


def test_rejects_transaction_with_no_matching_submission(tmp_path, monkeypatch):
    """An orphan ACCESSION_NUMBER means the extract is internally inconsistent."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "orphan_trans",
        submission_rows=["0000111111-23-000001\t01-APR-2023\t4\t0000999999\tJoint Co\tJTCO"],
        owner_rows=["0000111111-23-000001\t0003333333\tAlice Spouse\tDirector"],
        trans_rows=["0000999999-23-999999\t28-MAR-2023\tP\t100\t10.00\tA\t500\tD"],
    )

    result = load_insider_trading_extract(quarter, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "inconsistent extract" in result.error


def test_malformed_transaction_date_rows_are_dropped_not_fabricated(tmp_path, monkeypatch):
    """Real 2024 Q1 SEC data contains rows with a mistyped 4-digit year in
    TRANS_DATE (e.g. "0024" instead of "2024") -- hand-typed by the filer,
    unlike the SEC-stamped FILING_DATE. Guessing the intended date would be
    fabricating filer intent, so the row is dropped and counted instead of
    failing the whole quarter over a handful of typos."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "bad_date",
        submission_rows=[
            "0000111111-23-000001\t01-APR-2023\t4\t0000999999\tJoint Co\tJTCO",
            "0000111111-23-000002\t01-APR-2023\t4\t0000999999\tJoint Co\tJTCO",
        ],
        owner_rows=[
            "0000111111-23-000001\t0003333333\tAlice Spouse\tDirector",
            "0000111111-23-000002\t0003333333\tAlice Spouse\tDirector",
        ],
        trans_rows=[
            "0000111111-23-000001\t28-MAR-0023\tP\t100\t10.00\tA\t500\tD",
            "0000111111-23-000002\t29-MAR-2023\tP\t50\t11.00\tA\t550\tD",
        ],
    )

    result = load_insider_trading_extract(quarter, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 1
    assert result.malformed_date_rows_dropped == 1
    frame = read_insider_trading()
    assert len(frame) == 1
    assert frame.iloc[0]["shares"] == 50


def test_filing_precedes_transaction_rows_are_dropped_not_fabricated(tmp_path, monkeypatch):
    """Real 2024 Q1 SEC data contains 14 rows where TRANS_DATE parses fine
    but lands after its own FILING_DATE -- e.g. a day/month transposition or
    a year typo'd forward, both individually well-formed dates. SEC ships
    the data "as-filed... without change", so the row is untrustworthy;
    dropping and counting it (rather than failing the whole quarter) mirrors
    the malformed-date handling above."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "backwards",
        submission_rows=[
            "0000111111-23-000001\t01-JAN-2023\t4\t0000999999\tJoint Co\tJTCO",
            "0000111111-23-000002\t15-JAN-2023\t4\t0000999999\tJoint Co\tJTCO",
        ],
        owner_rows=[
            "0000111111-23-000001\t0003333333\tAlice Spouse\tDirector",
            "0000111111-23-000002\t0003333333\tAlice Spouse\tDirector",
        ],
        trans_rows=[
            "0000111111-23-000001\t10-JAN-2023\tP\t100\t10.00\tA\t500\tD",
            "0000111111-23-000002\t10-JAN-2023\tP\t50\t11.00\tA\t550\tD",
        ],
    )

    result = load_insider_trading_extract(quarter, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 1
    assert result.filing_precedes_transaction_rows_dropped == 1
    frame = read_insider_trading()
    assert len(frame) == 1
    assert frame.iloc[0]["shares"] == 50


def test_a_line_without_a_transaction_code_is_dropped_not_the_quarter(tmp_path, monkeypatch):
    """SEC's 2026 Q2 set has 2 of 78,328 lines with no transaction code (and
    blank optional codes); they used to fail the whole quarter as an
    "unknown transaction_code ''"."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "blank_code",
        submission_rows=[
            "0000111111-26-000001\t01-JUN-2026\t4\t0000999999\tJoint Co\tJTCO",
            "0000111111-26-000002\t02-JUN-2026\t4\t0000999999\tJoint Co\tJTCO",
        ],
        owner_rows=[
            "0000111111-26-000001\t0003333333\tAlice Spouse\tDirector",
            "0000111111-26-000002\t0003333333\tAlice Spouse\tDirector",
        ],
        trans_rows=[
            "0000111111-26-000001\t01-JUN-2026\t\t100\t10.00\t\t500\t",
            "0000111111-26-000002\t01-JUN-2026\tP\t50\t11.00\tA\t550\t",
        ],
    )

    result = load_insider_trading_extract(quarter, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 1 and result.blank_code_rows_dropped == 1
    frame = read_insider_trading()
    assert frame.iloc[0]["transaction_code"] == "P" and frame.iloc[0]["ownership_type"] is pd.NA


def test_rejects_unknown_transaction_code(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    quarter = _write_quarter(
        tmp_path,
        "bad_code",
        submission_rows=["0000111111-23-000001\t01-APR-2023\t4\t0000999999\tJoint Co\tJTCO"],
        owner_rows=["0000111111-23-000001\t0003333333\tAlice Spouse\tDirector"],
        trans_rows=["0000111111-23-000001\t28-MAR-2023\tZZ\t100\t10.00\tA\t500\tD"],
    )

    result = load_insider_trading_extract(quarter, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unknown transaction_code" in result.error


def test_read_insider_trading_on_an_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_insider_trading()

    assert frame.empty
    assert "filing_date" in frame.columns
