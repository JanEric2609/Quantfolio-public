"""Tests for the WRDS CRSP/Compustat Merged (CCM) link loader.

The table's whole reason for existing is the [linkdt, linkenddt] validity
window, so the tests that matter most pin the point-in-time ``as_of``
filtering and the "null linkenddt means still active" convention.
"""
from pathlib import Path

import pandas as pd

from app.foundation.data_engineering.ccm_link_loader import (
    load_ccm_link_extract,
    read_ccm_link,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wrds_ccm_link_sample_extract.csv"


def test_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_ccm_link_extract(FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    assert result.rows_loaded == 3
    assert result.gvkeys == ["001000", "002000"]
    assert not (tmp_path / "ccm_link_pit").exists()


def test_apply_writes_one_parquet_and_normalizes_lpermno_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_ccm_link_extract(FIXTURE, dry_run=False)

    assert result.ok is True
    written = sorted(p.name for p in (tmp_path / "ccm_link_pit").glob("*.parquet"))
    assert written == ["wrds_ccm_link_sample_extract.parquet"]
    frame = pd.read_parquet(tmp_path / "ccm_link_pit" / written[0])
    assert (frame["source"] == "crsp_compustat_ccm").all()
    assert set(frame["permno"].tolist()) == {10001, 10002, 10003}


def test_null_linkenddt_means_still_active(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ccm_link_extract(FIXTURE, dry_run=False)

    frame = read_ccm_link()
    open_ended = frame[frame["permno"] == 10002]
    assert len(open_ended) == 1
    assert pd.isna(open_ended.iloc[0]["linkenddt"])


def test_literal_e_sentinel_means_still_active(tmp_path, monkeypatch):
    """Real WRDS CCM exports use the literal string "E", not always a blank
    cell, to mark an open-ended (still active) link."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    sentinel = tmp_path / "with_e_sentinel.csv"
    sentinel.write_text(
        "gvkey,lpermno,lpermco,linkdt,linkenddt,linktype,linkprim\n"
        "004000,10006,20003,2000-01-01,E,LC,P\n"
    )

    result = load_ccm_link_extract(sentinel, dry_run=False)

    assert result.ok is True, result.error
    frame = read_ccm_link()
    assert pd.isna(frame.iloc[0]["linkenddt"])


def test_read_ccm_link_as_of_picks_the_link_valid_on_that_date(tmp_path, monkeypatch):
    """gvkey 001000's permno changed from 10001 to 10002 on 2006-01-01."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ccm_link_extract(FIXTURE, dry_run=False)

    before = read_ccm_link(as_of="2005-06-01")
    after = read_ccm_link(as_of="2010-01-01")

    row_before = before[before["gvkey"] == "001000"].iloc[0]
    row_after = after[after["gvkey"] == "001000"].iloc[0]
    assert row_before["permno"] == 10001
    assert row_after["permno"] == 10002


def test_read_ccm_link_as_of_excludes_links_not_yet_started(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_ccm_link_extract(FIXTURE, dry_run=False)

    visible = read_ccm_link(as_of="1985-01-01")

    assert set(visible["gvkey"]) == {"001000"}


def test_research_quality_only_filters_to_lc_lu_and_p_c(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "with_secondary_link.csv"
    bad.write_text(
        "gvkey,lpermno,lpermco,linkdt,linkenddt,linktype,linkprim\n"
        "003000,10004,20002,2000-01-01,,LC,P\n"
        "003000,10005,20002,2000-01-01,,LD,J\n"
    )
    load_ccm_link_extract(bad, dry_run=False)

    everything = read_ccm_link()
    filtered = read_ccm_link(research_quality_only=True)

    assert len(everything) == 2
    assert len(filtered) == 1
    assert filtered.iloc[0]["permno"] == 10004


def test_rejects_missing_required_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "no_linktype.csv"
    bad.write_text("gvkey,lpermno,linkdt,linkprim\n1001,10001,2000-01-01,P\n")

    result = load_ccm_link_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required column" in result.error


def test_rejects_unparseable_linkdt(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_date.csv"
    bad.write_text(
        "gvkey,lpermno,linkdt,linktype,linkprim\n1001,10001,not-a-date,LC,P\n"
    )

    result = load_ccm_link_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unparseable linkdt" in result.error


def test_rejects_unknown_linktype(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "bad_linktype.csv"
    bad.write_text(
        "gvkey,lpermno,linkdt,linktype,linkprim\n1001,10001,2000-01-01,ZZ,P\n"
    )

    result = load_ccm_link_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "unknown linktype" in result.error


def test_read_ccm_link_on_an_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_ccm_link()

    assert frame.empty
    assert "linkenddt" in frame.columns
