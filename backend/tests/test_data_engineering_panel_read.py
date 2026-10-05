"""Tests for reading the combined PIT panel back (panel_read.py)."""
from app.foundation.data_engineering.datastream_loader import load_datastream_extract
from app.foundation.data_engineering.panel_read import read_panel
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "datastream_sample_extract.csv"


def test_read_panel_returns_empty_frame_when_nothing_exported(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    panel = read_panel()

    assert panel.empty


def test_read_panel_reads_back_datastream_partitions(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_datastream_extract(FIXTURE, dry_run=False)

    panel = read_panel()

    assert set(panel["symbol"].unique()) == {"IWDA", "VWCE"}
    assert len(panel) == 3


def test_read_panel_filters_by_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    load_datastream_extract(FIXTURE, dry_run=False)

    panel = read_panel(symbols=["IWDA"])

    assert set(panel["symbol"].unique()) == {"IWDA"}
    assert len(panel) == 2


def _write_partition(panel_dir: Path, sub: str, rows: list[dict]) -> None:
    """Write rows straight into one panel partition, bypassing the loaders."""
    import pandas as pd

    from app.foundation.data_engineering.panel_schema import validate_panel_frame

    frame = pd.DataFrame(rows)
    frame["ingested_at"] = pd.Timestamp("2026-01-10")
    out_dir = panel_dir / sub
    out_dir.mkdir(parents=True, exist_ok=True)
    validate_panel_frame(frame).to_parquet(out_dir / "part.parquet", index=False)


def _row(symbol: str, close: float, currency: str, source: str) -> dict:
    return {
        "symbol": symbol,
        "isin": "GB00B03MLX29",
        "as_of_date": "2026-01-02",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "currency": currency,
        "source": source,
    }


def test_cross_partition_duplicate_resolved_by_source_precedence(tmp_path, monkeypatch):
    """The same bar in both partitions must resolve to the PIT extract, not to
    whichever file happened to be read last."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _write_partition(tmp_path, "internal", [_row("SHEL", 28.50, "EUR", "internal")])
    _write_partition(tmp_path, "datastream_pit", [_row("SHEL", 2850.0, "GBp", "datastream_pit")])

    panel = read_panel()

    assert len(panel) == 1
    assert panel.iloc[0]["close"] == 2850.0
    assert panel.iloc[0]["source"] == "datastream_pit"


def test_cross_partition_duplicate_is_warned_about(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _write_partition(tmp_path, "internal", [_row("SHEL", 28.50, "EUR", "internal")])
    _write_partition(tmp_path, "datastream_pit", [_row("SHEL", 2850.0, "GBp", "datastream_pit")])

    with caplog.at_level("WARNING"):
        read_panel()

    assert "SHEL" in caplog.text
    assert "duplicate" in caplog.text


def test_non_colliding_rows_from_both_partitions_are_all_kept(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _write_partition(tmp_path, "internal", [_row("SAP", 100.0, "EUR", "internal")])
    _write_partition(tmp_path, "datastream_pit", [_row("SHEL", 2850.0, "GBp", "datastream_pit")])

    with caplog.at_level("WARNING"):
        panel = read_panel()

    assert set(panel["symbol"]) == {"SAP", "SHEL"}
    assert "duplicate" not in caplog.text
