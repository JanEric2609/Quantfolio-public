"""Tests for the WRDS Factors loader.

"WRDS Factors" is ambiguous between two structurally different shapes (see
module docstring), so the tests that matter most are the shape-detection
dispatch and that each shape's own point-in-time filtering behaves.
"""
from pathlib import Path

import pandas as pd
import pytest

from app.foundation.data_engineering.wrds_factors_loader import (
    load_wrds_factors_extract,
    read_wrds_factor_characteristics,
    read_wrds_factor_returns,
)

RETURNS_FIXTURE = Path(__file__).parent / "fixtures" / "wrds_factor_returns_sample_extract.csv"
CHARACTERISTICS_FIXTURE = Path(__file__).parent / "fixtures" / "wrds_factor_characteristics_sample_extract.csv"


def test_detects_and_loads_factor_returns_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_wrds_factors_extract(RETURNS_FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    assert result.shape == "factor_returns"
    assert result.rows_loaded == 2


def test_detects_and_loads_factor_characteristics_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_wrds_factors_extract(CHARACTERISTICS_FIXTURE, dry_run=True)

    assert result.ok is True, result.error
    assert result.shape == "factor_characteristics"
    assert result.rows_loaded == 3
    assert result.identifiers == ["001000", "002000"]


def test_ambiguous_header_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad = tmp_path / "ambiguous.csv"
    bad.write_text("foo,bar\n1,2\n")

    result = load_wrds_factors_extract(bad, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "could not determine" in result.error


def test_factor_returns_values_stay_in_percent_uncoverted(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_wrds_factors_extract(RETURNS_FIXTURE, dry_run=False)

    frame = read_wrds_factor_returns()

    row = frame[frame["date"] == pd.Timestamp("2023-01-31")].iloc[0]
    assert row["mkt_rf"] == 2.45
    assert (frame["source"] == "wrds_factor_returns").all()


def test_read_factor_returns_as_of_filters_by_date(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_wrds_factors_extract(RETURNS_FIXTURE, dry_run=False)

    visible = read_wrds_factor_returns(as_of="2023-02-01")

    assert len(visible) == 1
    assert visible.iloc[0]["date"] == pd.Timestamp("2023-01-31")


def test_read_factor_characteristics_as_of_filters_by_eom(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_wrds_factors_extract(CHARACTERISTICS_FIXTURE, dry_run=False)

    visible = read_wrds_factor_characteristics(as_of="2023-01-31")

    assert set(visible["gvkey"]) == {"001000", "002000"}
    assert len(read_wrds_factor_characteristics()) == 3


def test_factor_characteristics_values_stay_in_decimal(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_wrds_factors_extract(CHARACTERISTICS_FIXTURE, dry_run=False)

    frame = read_wrds_factor_characteristics()

    row = frame[(frame["gvkey"] == "001000") & (frame["eom"] == pd.Timestamp("2023-01-31"))].iloc[0]
    assert row["mom_12_1"] == 0.12
    assert (frame["source"] == "wrds_factor_characteristics").all()


def test_read_wrds_factor_returns_on_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_wrds_factor_returns()

    assert frame.empty
    assert "mkt_rf" in frame.columns


def test_unmatched_gvkey_rows_are_dropped_not_fabricated(tmp_path, monkeypatch):
    """A real "search the entire database" extract legitimately contains
    CRSP-only securities with no Compustat match: gvkey comes through blank.
    Two failure modes discovered against a real WRDS extract: (1) str(NaN) is
    the literal "nan", which zfill would otherwise turn into the
    innocuous-looking "000nan" -- these rows must be dropped, not ingested
    under a fabricated identifier; (2) a blank cell in the gvkey column makes
    pandas parse the WHOLE chunk's gvkey column as float64, so every OTHER
    (valid) gvkey in the same chunk comes out "1000.0" instead of "001000"
    once naively stringified -- this must not corrupt sibling rows."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    with_blank_gvkey = tmp_path / "with_blank_gvkey.csv"
    with_blank_gvkey.write_text(
        "gvkey,permno,eom,excntry,size_grp,me\n"
        "001000,10001,2023-01-31,USA,large,9.5\n"
        ",75471,2023-01-31,USA,micro,1.2\n"
    )

    result = load_wrds_factors_extract(with_blank_gvkey, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == 1
    assert result.unmatched_gvkey_rows_dropped == 1
    frame = read_wrds_factor_characteristics()
    assert "nan" not in frame["gvkey"].str.lower().to_numpy()
    assert set(frame["gvkey"]) == {"001000"}
    assert "." not in frame.iloc[0]["gvkey"]


def test_read_wrds_factor_characteristics_on_empty_panel_returns_typed_empty_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    frame = read_wrds_factor_characteristics()

    assert frame.empty
    assert "gvkey" in frame.columns


# --- the widened JKP extract ---------------------------------------------------

_WIDE_HEADER = "id,gvkey,eom,excntry,size_grp,me,ret_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m,ret_6_1,qmj,ivol_capm_252d,gics\n"


def _write_wide(path: Path, gvkey: str = "003000") -> Path:
    path.write_text(
        _WIDE_HEADER
        + f"301,{gvkey},2023-01-31,DEU,large,50.0,0.20,0.60,0.25,0.07,0.01,0.11,0.9,0.02,45102010\n"
    )
    return path


def _write_old_schema_partition(panel: Path, stem: str) -> None:
    """A partition written before the schema widened: only the original columns."""
    from datetime import datetime

    from app.foundation.data_engineering.wrds_factors_schema import FACTOR_CHARACTERISTICS_DTYPES

    old_columns = [
        "gvkey", "permno", "eom", "excntry", "size_grp", "me",
        "mom_12_1", "be_me", "gp_at", "at_gr1", "ret_exc_lead1m", "source", "ingested_at",
    ]
    frame = pd.DataFrame([{
        "gvkey": "001000", "permno": 10001, "eom": pd.Timestamp("2023-01-31"), "excntry": "USA",
        "size_grp": "large", "me": 9.5, "mom_12_1": 0.12, "be_me": 0.45, "gp_at": 0.30,
        "at_gr1": 0.05, "ret_exc_lead1m": 0.02, "source": "wrds_factor_characteristics",
        "ingested_at": datetime(2026, 1, 1),
    }])
    frame = frame.astype({c: FACTOR_CHARACTERISTICS_DTYPES[c] for c in old_columns})
    out = panel / "wrds_factor_characteristics_pit"
    out.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out / f"{stem}.0000.parquet", index=False)


def test_wide_extract_loads_every_theme_column_under_app_names(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_wrds_factors_extract(_write_wide(tmp_path / "wide.csv"), dry_run=False)

    assert result.ok is True, result.error
    row = read_wrds_factor_characteristics().iloc[0]
    assert row["mom_12_1"] == 0.20  # JKP's ret_12_1
    assert row["ret_6_1"] == 0.11
    assert row["qmj"] == 0.9
    assert row["ivol_capm_252d"] == 0.02
    assert row["gics"] == 45102010
    assert pd.isna(row["bev_mev"])  # asked for, not in this extract


def test_missing_characteristics_are_reported_not_silent(tmp_path, monkeypatch):
    from app.foundation.data_engineering.wrds_factors_schema import JKP_CHARACTERISTICS

    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    wide = load_wrds_factors_extract(_write_wide(tmp_path / "wide.csv"), dry_run=True)
    old = load_wrds_factors_extract(CHARACTERISTICS_FIXTURE, dry_run=True)

    for present in ("mom_12_1", "be_me", "ret_6_1", "qmj", "ivol_capm_252d", "gics", "ret_exc_lead1m"):
        assert present not in wide.missing_characteristics
    assert "bev_mev" in wide.missing_characteristics
    assert len(old.missing_characteristics) == len(JKP_CHARACTERISTICS) - 4 + 1  # all new ones, plus gics
    assert "mom_12_1" not in old.missing_characteristics


@pytest.mark.parametrize("old_stem", ["a_old", "z_old"])
def test_old_and_wide_partitions_read_back_with_every_column(tmp_path, monkeypatch, old_stem):
    """pyarrow and DuckDB both default to one file's schema; whichever file
    sorts first, the new columns must survive and old rows read them as null."""
    from app.foundation.data_engineering import _pit_duckdb

    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _write_old_schema_partition(tmp_path, old_stem)
    load_wrds_factors_extract(_write_wide(tmp_path / "m_wide.csv"), dry_run=False)

    frame = read_wrds_factor_characteristics()
    by_gvkey = frame.set_index("gvkey")
    assert by_gvkey.loc["003000", "qmj"] == 0.9
    assert pd.isna(by_gvkey.loc["001000", "qmj"])
    assert by_gvkey.loc["001000", "mom_12_1"] == 0.12
    assert str(frame["gvkey"].dtype) == "string"
    assert str(frame["gics"].dtype) == "Int64"

    pushed = _pit_duckdb.wrds_factor_characteristics_for_gvkeys(tmp_path, ["001000", "003000"])
    pd.testing.assert_frame_equal(pushed, frame[pushed.columns.tolist()])
    assert set(pushed.columns) == set(frame.columns)


def test_gzipped_extract_loads(tmp_path, monkeypatch):
    """WRDS offers compressed output; a 3 GB CSV is easier to move as .csv.gz."""
    import gzip

    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    plain = _write_wide(tmp_path / "wide.csv")
    packed = tmp_path / "wide.csv.gz"
    packed.write_bytes(gzip.compress(plain.read_bytes()))

    result = load_wrds_factors_extract(packed, dry_run=False)

    assert result.ok is True, result.error
    assert read_wrds_factor_characteristics().iloc[0]["qmj"] == 0.9
