"""Tests for the WRDS IBES Summary Statistics loader.

``statpers`` is the point-in-time anchor this table exists to provide (see
module docstring), so the tests that matter most pin the ``as_of`` filtering
and the fpi/measure filters every real IBES query needs.
"""
from pathlib import Path

import pandas as pd

from app.foundation.data_engineering.ibes_estimates_loader import (
    load_ibes_estimates_extract,
    read_ibes_estimates,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ibes_statsum_epsus_sample_extract.csv"


def test_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_ibes_estimates_extract(FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    assert result.rows_loaded == 3
    assert result.tickers == ["0001AA", "0002BB"]
    assert not (tmp_path / "ibes_estimates_pit").exists()


def test_apply_writes_one_parquet_per_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_ibes_estimates_extract(FIXTURE, dry_run=False)

    assert result.ok is True
    written = sorted(p.name for p in (tmp_path / "ibes_estimates_pit").glob("*.parquet"))
    assert written == ["ibes_statsum_epsus_sample_extract.0000.parquet"]
    frame = pd.read_parquet(tmp_path / "ibes_estimates_pit" / written[0])
    assert (frame["source"] == "ibes_summary_epsus").all()


def test_read_as_of_hides_later_consensus_snapshots(tmp_path, monkeypatch):
    """Two statpers snapshots exist for ticker 0001AA: Jan 31 and Feb 28."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ibes_estimates_extract(FIXTURE, dry_run=False)

    visible = read_ibes_estimates(as_of="2023-02-01")
    row = visible[visible["ticker"] == "0001AA"].iloc[0]

    assert len(visible[visible["ticker"] == "0001AA"]) == 1
    assert row["statpers"] == pd.Timestamp("2023-01-31")
    assert len(read_ibes_estimates()) == 3


def test_read_filters_by_fpi_and_measure(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ibes_estimates_extract(FIXTURE, dry_run=False)

    frame = read_ibes_estimates(fpi="1", measure="eps")

    assert len(frame) == 3
    assert (frame["fpi"] == "1").all()
    assert (frame["measure"] == "EPS").all()


def test_actual_and_anndats_act_stay_null_before_announcement(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ibes_estimates_extract(FIXTURE, dry_run=False)

    frame = read_ibes_estimates()
    unannounced = frame[frame["ticker"] == "0001AA"]
    announced = frame[frame["ticker"] == "0002BB"].iloc[0]

    assert unannounced["actual"].isna().all()
    assert announced["actual"] == 2.60
    assert announced["anndats_act"] == pd.Timestamp("2024-02-15")


def test_reloading_a_corrected_extract_supersedes_the_earlier_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ibes_estimates_extract(FIXTURE, dry_run=False)

    corrected = tmp_path / "corrected.csv"
    corrected.write_text(
        "ticker,statpers,fpedats,fpi,measure,medest\n"
        "0001AA,2023-01-31,2023-12-31,1,EPS,9.99\n"
    )
    assert load_ibes_estimates_extract(corrected, dry_run=False).ok is True

    frame = read_ibes_estimates()
    row = frame[
        (frame["ticker"] == "0001AA")
        & (frame["statpers"] == pd.Timestamp("2023-01-31"))
    ]
    assert len(row) == 1
    assert row.iloc[0]["medest"] == 9.99


def test_rejects_missing_required_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "no_statpers.csv"
    bad.write_text("ticker,fpedats,fpi,measure\n0001AA,2023-12-31,1,EPS\n")

    result = load_ibes_estimates_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required column" in result.error


def test_rejects_unparseable_statpers(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_date.csv"
    bad.write_text("ticker,statpers,fpedats,fpi,measure\n0001AA,not-a-date,2023-12-31,1,EPS\n")

    result = load_ibes_estimates_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unparseable statpers" in result.error


def test_rejects_unknown_fpi(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_fpi.csv"
    bad.write_text("ticker,statpers,fpedats,fpi,measure\n0001AA,2023-01-31,2023-12-31,ZZ,EPS\n")

    result = load_ibes_estimates_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unknown fpi" in result.error


def test_fpi_and_measure_overrides_fill_a_missing_column(tmp_path, monkeypatch):
    """WRDS's Summary Statistics query tool applies fpi/measure as filters,
    not output columns -- a real extract legitimately has no such columns."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    no_fpi_measure = tmp_path / "no_fpi_measure_columns.csv"
    no_fpi_measure.write_text("ticker,statpers,fpedats,medest\n0001AA,2023-01-31,2023-12-31,9.99\n")

    result = load_ibes_estimates_extract(no_fpi_measure, dry_run=False, fpi="1", measure="eps")

    assert result.ok is True, result.error
    frame = read_ibes_estimates()
    assert frame.iloc[0]["fpi"] == "1"
    assert frame.iloc[0]["measure"] == "EPS"


def test_rejects_missing_fpi_column_without_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    no_fpi = tmp_path / "no_fpi_column.csv"
    no_fpi.write_text("ticker,statpers,fpedats,measure\n0001AA,2023-01-31,2023-12-31,EPS\n")

    result = load_ibes_estimates_extract(no_fpi, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "no fpi column" in result.error


def test_rejects_missing_measure_column_without_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    no_measure = tmp_path / "no_measure_column.csv"
    no_measure.write_text("ticker,statpers,fpedats,fpi\n0001AA,2023-01-31,2023-12-31,1\n")

    result = load_ibes_estimates_extract(no_measure, dry_run=True, fpi="1")

    assert result.ok is False
    assert result.error is not None
    assert "no measure column" in result.error


def test_read_ibes_estimates_on_an_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_ibes_estimates()

    assert frame.empty
    assert "statpers" in frame.columns
