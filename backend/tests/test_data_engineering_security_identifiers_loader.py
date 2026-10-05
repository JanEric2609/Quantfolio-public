"""Tests for the Compustat Global security-identifiers (gvkey<->ISIN) loader."""
from pathlib import Path

import pandas as pd

from app.foundation.data_engineering.security_identifiers_loader import (
    _CHUNK_ROWS,
    load_security_identifiers_extract,
    read_security_identifiers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "compustat_security_sample_extract.csv"


def test_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_security_identifiers_extract(FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    assert result.rows_loaded == 6
    assert result.gvkeys == ["001166", "001932"]
    assert not (tmp_path / "security_identifiers_pit").exists()


def test_blank_isin_is_preserved_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    load_security_identifiers_extract(FIXTURE, dry_run=False)
    frame = read_security_identifiers()

    row = frame[(frame["gvkey"] == "001166") & (frame["isin"].isna())]
    assert len(row) == 1


def test_apply_writes_one_parquet_per_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_security_identifiers_extract(FIXTURE, dry_run=False)

    assert result.ok is True
    # One chunk for a fixture this small (default chunk size is 500k rows).
    written = sorted(p.name for p in (tmp_path / "security_identifiers_pit").glob("*.parquet"))
    assert written == ["compustat_security_sample_extract.0000.parquet"]
    frame = pd.read_parquet(tmp_path / "security_identifiers_pit" / written[0])
    assert (frame["source"] == "compustat_global_security").all()


def test_read_as_of_returns_latest_identifier_on_or_before_cutoff(tmp_path, monkeypatch):
    """A company's ISIN as of a date is whatever was last observed by then."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_security_identifiers_extract(FIXTURE, dry_run=False)

    visible = read_security_identifiers(as_of="2024-01-01")

    # Only the first day's rows are visible, and 1932 still shows both ISINs
    # for that day (a company can carry more than one security).
    assert set(visible[visible["gvkey"] == "001932"]["isin"]) == {
        "GB0002875804",
        "US1104481072",
    }
    assert len(read_security_identifiers()) == 6


def test_as_of_reduction_spans_partitions_and_skips_ones_entirely_after_cutoff(tmp_path, monkeypatch):
    """The memory-bounded as_of path must give the same answer the naive one would.

    Three chunks, three different dates, and a gvkey that changes ISIN
    partway through -- ``as_of`` must pick each gvkey's latest identifier on
    or before the cutoff, from whichever partition(s) that requires, while
    never touching a partition entirely after it.
    """
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    monkeypatch.setattr(
        "app.foundation.data_engineering.security_identifiers_loader._CHUNK_ROWS", 1
    )
    csv = tmp_path / "reissue.csv"
    csv.write_text(
        "gvkey,isin,fic,datadate\n"
        "1001,NL0000000001,NLD,2024-01-01\n"
        "1001,NL0000000002,NLD,2024-06-01\n"
        "1002,NL0000000003,NLD,2024-12-01\n"
    )

    result = load_security_identifiers_extract(csv, dry_run=False)
    assert result.ok is True
    assert result.chunks_processed == 3

    visible = read_security_identifiers(as_of="2024-07-01")

    # 1001's mid-year reissue is visible, its January ISIN is superseded, and
    # 1002's December identifier (after the cutoff) does not appear at all.
    assert dict(zip(visible["gvkey"], visible["isin"])) == {"001001": "NL0000000002"}


def test_reloading_a_corrected_extract_collapses_exact_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    dup = tmp_path / "dup.csv"
    dup.write_text(
        "gvkey,isin,fic,datadate\n"
        "1001,NL0000334118,NLD,2024-01-01\n"
        "1001,NL0000334118,NLD,2024-01-01\n"
    )

    result = load_security_identifiers_extract(dup, dry_run=True)

    assert result.ok is True
    assert result.rows_loaded == 1
    assert result.duplicate_rows_dropped == 1


def test_rejects_missing_required_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "no_gvkey.csv"
    bad.write_text("datadate,isin\n2024-12-31,NL0000334118\n")

    result = load_security_identifiers_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required column" in result.error


def test_rejects_unparseable_date(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_date.csv"
    bad.write_text("gvkey,isin,datadate\n1001,NL0000334118,31/12/2024 or so\n")

    result = load_security_identifiers_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unparseable" in result.error


def test_extract_spanning_multiple_chunks_is_fully_loaded(tmp_path, monkeypatch):
    """A universe-wide extract is read/written in bounded-size pieces, not as one frame."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    big = tmp_path / "big.csv"
    row_count = int(_CHUNK_ROWS * 2.5)
    with big.open("w") as fh:
        fh.write("gvkey,isin,fic,datadate\n")
        for i in range(row_count):
            # isin unique per row so no (gvkey, isin, fic, datadate) tuple
            # repeats -- the test is about chunk boundaries, not dedup.
            fh.write(f"{1000 + (i % 50)},ISIN{i:010d},NLD,2024-01-01\n")

    result = load_security_identifiers_extract(big, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == row_count
    assert result.chunks_processed == 3
    written = sorted((tmp_path / "security_identifiers_pit").glob("*.parquet"))
    assert len(written) == 3
    assert len(read_security_identifiers()) == row_count


def test_read_security_identifiers_on_an_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_security_identifiers()

    assert frame.empty
    assert "as_of_date" in frame.columns
