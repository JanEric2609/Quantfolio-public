"""Tests for the Compustat Global fundamentals loader.

The loader exists to remove a lookahead bug (see its module docstring), so the
tests that matter most are the ones pinning ``available_from`` -- the column
that makes a point-in-time read possible.
"""
from pathlib import Path

import pandas as pd
import pytest

from app.foundation.data_engineering.fundamentals_loader import (
    DEFAULT_PUBLICATION_LAG_MONTHS,
    load_fundamentals_extract,
    read_fundamentals,
)

FIXTURE = Path(__file__).parent / "fixtures" / "compustat_funda_sample_extract.csv"


def test_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_fundamentals_extract(FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    # Five fixture rows, less one SUMM_STD and one popsrc=D.
    assert result.rows_loaded == 3
    assert result.gvkeys == ["001001", "001002"]
    assert not (tmp_path / "fundamentals_pit").exists()


def test_non_standard_presentations_are_filtered_and_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_fundamentals_extract(FIXTURE, dry_run=True)

    # Reported rather than silently dropped: a filter that removes most of a
    # file means the wrong extract, and the caller needs to see the counts.
    assert result.dropped_by_flag == {"datafmt": 1, "popsrc": 1}


def test_available_from_lags_the_fiscal_period_end(tmp_path, monkeypatch):
    """The whole point of the module: datadate is not when the data was public."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    load_fundamentals_extract(FIXTURE, dry_run=False)
    frame = read_fundamentals()

    assert DEFAULT_PUBLICATION_LAG_MONTHS == 6
    row = frame[(frame["gvkey"] == "001001") & (frame["datadate"] == pd.Timestamp("2023-12-31"))].iloc[0]
    assert row["available_from"] == pd.Timestamp("2024-06-30")
    # Never equal to datadate -- that equality IS the bug this module removes.
    assert (frame["available_from"] > frame["datadate"]).all()


def test_publication_lag_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    load_fundamentals_extract(FIXTURE, dry_run=False, publication_lag_months=3)
    frame = read_fundamentals()

    row = frame[(frame["gvkey"] == "001001") & (frame["datadate"] == pd.Timestamp("2023-12-31"))].iloc[0]
    assert row["available_from"] == pd.Timestamp("2024-03-31")


def test_negative_publication_lag_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_fundamentals_extract(FIXTURE, dry_run=True, publication_lag_months=-1)

    assert result.ok is False
    assert result.error is not None
    assert "must be >= 0" in result.error


def test_read_fundamentals_as_of_hides_not_yet_published_rows(tmp_path, monkeypatch):
    """A backtest standing in early 2025 cannot see the FY2024 annual report."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_fundamentals_extract(FIXTURE, dry_run=False)

    visible = read_fundamentals(as_of="2025-01-15")

    assert set(visible["datadate"]) == {pd.Timestamp("2023-12-31")}
    assert len(read_fundamentals()) == 3


def test_apply_writes_one_parquet_per_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_fundamentals_extract(FIXTURE, dry_run=False)

    assert result.ok is True
    written = sorted(p.name for p in (tmp_path / "fundamentals_pit").glob("*.parquet"))
    assert written == ["compustat_funda_sample_extract.parquet"]
    frame = pd.read_parquet(tmp_path / "fundamentals_pit" / written[0])
    assert (frame["source"] == "compustat_global_funda").all()
    assert frame["currency"].tolist() == ["EUR", "EUR", "GBP"]


def test_reloading_a_corrected_extract_supersedes_the_earlier_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_fundamentals_extract(FIXTURE, dry_run=False)

    corrected = tmp_path / "corrected.csv"
    corrected.write_text(
        "gvkey,datadate,fyear,indfmt,consol,popsrc,datafmt,curcd,ceq\n"
        "1001,2024-12-31,2024,INDL,C,I,STD,EUR,9999.0\n"
    )
    # Asserted, not assumed: a silently-failed second load would leave the
    # original row in place and make this test pass for the wrong reason.
    assert load_fundamentals_extract(corrected, dry_run=False).ok is True

    frame = read_fundamentals()
    row = frame[(frame["gvkey"] == "001001") & (frame["datadate"] == pd.Timestamp("2024-12-31"))]
    assert len(row) == 1
    assert row.iloc[0]["common_equity"] == 9999.0


def test_rejects_missing_required_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "no_gvkey.csv"
    bad.write_text("datadate,ceq\n2024-12-31,2000.0\n")

    result = load_fundamentals_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required column" in result.error


def test_rejects_extract_with_no_financial_figures(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "identifiers_only.csv"
    bad.write_text("gvkey,datadate,fyear\n1001,2024-12-31,2024\n")

    result = load_fundamentals_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "no financial figures" in result.error


def test_rejects_unparseable_datadate(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_date.csv"
    bad.write_text("gvkey,datadate,ceq\n1001,31/12/2024 or so,2000.0\n")

    result = load_fundamentals_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unparseable datadate" in result.error


def test_duplicate_company_year_is_refused_and_names_indfmt(tmp_path, monkeypatch):
    """INDL and FS presentations of one bank must not silently become one row.

    The loader deliberately does not filter ``indfmt`` itself: keeping only
    INDL would drop every bank and insurer from a European universe.
    """
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    both = tmp_path / "bank.csv"
    both.write_text(
        "gvkey,datadate,fyear,indfmt,consol,popsrc,datafmt,curcd,ceq\n"
        "2001,2024-12-31,2024,INDL,C,I,STD,EUR,4000.0\n"
        "2001,2024-12-31,2024,FS,C,I,STD,EUR,4100.0\n"
    )

    result = load_fundamentals_extract(both, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "002001" in result.error
    assert "indfmt" in result.error
    assert "banks and insurers" in result.error


@pytest.mark.parametrize("column,alias", [("common_equity", "ceqq"), ("revenue", "sale")])
def test_quarterly_and_alternate_spellings_are_accepted(tmp_path, monkeypatch, column, alias):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    csv = tmp_path / f"{alias}.csv"
    csv.write_text(f"gvkey,datadate,{alias}\n1001,2024-12-31,1234.0\n")

    assert load_fundamentals_extract(csv, dry_run=False).ok is True

    assert read_fundamentals().iloc[0][column] == 1234.0


def test_read_fundamentals_on_an_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_fundamentals()

    assert frame.empty
    assert "available_from" in frame.columns
