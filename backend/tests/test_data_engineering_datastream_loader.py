"""Tests for the (unexercised-against-real-data, pending owner Datastream
access) Datastream extract loader -- app.foundation.data_engineering.datastream_loader.
"""
from pathlib import Path

import pandas as pd

from app.foundation.data_engineering.datastream_loader import load_datastream_extract

FIXTURE = Path(__file__).parent / "fixtures" / "datastream_sample_extract.csv"


def test_load_datastream_extract_dry_run_parses_and_validates_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_datastream_extract(FIXTURE, dry_run=True)

    assert result.ok is True
    assert result.rows_loaded == 3
    assert result.symbols == ["IWDA", "VWCE"]
    assert not (tmp_path / "datastream_pit").exists()


def test_load_datastream_extract_apply_writes_parquet_per_symbol(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_datastream_extract(FIXTURE, dry_run=False)

    assert result.ok is True
    written = sorted(p.name for p in (tmp_path / "datastream_pit").glob("*.parquet"))
    assert written == ["IWDA.parquet", "VWCE.parquet"]

    frame = pd.read_parquet(tmp_path / "datastream_pit" / "IWDA.parquet")
    assert len(frame) == 2
    assert (frame["source"] == "datastream_pit").all()
    assert frame["isin"].iloc[0] == "IE00B4L5Y983"
    assert frame["currency"].iloc[0] == "EUR"


def test_load_datastream_extract_rejects_missing_required_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("Code,Date,Open,High,Low\nIWDA,2026-01-02,90.1,90.5,89.8\n")

    result = load_datastream_extract(bad_csv, dry_run=True)

    assert result.ok is False
    assert result.error is not None
    assert "missing required column" in result.error


def test_load_datastream_extract_rejects_unparseable_date(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    bad_csv = tmp_path / "bad_date.csv"
    bad_csv.write_text(
        "Code,Date,Open,High,Low,Close\nIWDA,not-a-date,90.1,90.5,89.8,90.2\n"
    )

    result = load_datastream_extract(bad_csv, dry_run=True)

    assert result.ok is False
    assert "unparseable" in (result.error or "")


def test_load_datastream_extract_rejects_nonexistent_file(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    result = load_datastream_extract(tmp_path / "does_not_exist.csv", dry_run=True)

    assert result.ok is False
    assert result.error is not None


# --- Export-dialect aliases -------------------------------------------------
#
# Datastream's Excel add-in and WRDS Compustat Global each emit their own column
# names. Before these aliases an extract had to be renamed by hand in Excel
# first, and an extract carrying two spellings of one column crashed outright.


def _load(tmp_path, monkeypatch, csv_text: str):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    csv_file = tmp_path / "extract.csv"
    csv_file.write_text(csv_text)
    return load_datastream_extract(csv_file, dry_run=True)




def test_accepts_datastream_mnemonic_headers(tmp_path, monkeypatch):
    result = _load(
        tmp_path,
        monkeypatch,
        "Code,Date,PO,PH,PL,P,VO\nASML,2026-01-02,680.1,689.4,678.0,687.2,1200000\n",
    )

    assert result.ok is True, result.error
    assert result.rows_loaded == 1
    assert result.symbols == ["ASML"]


def test_accepts_unpadded_hash_t_mnemonics(tmp_path, monkeypatch):
    """``#T`` variants are the ones EDSC's guide recommends for delisted names."""
    result = _load(
        tmp_path,
        monkeypatch,
        "Code,Date,PO#T,PH#T,PL#T,P#T,VO#T\nASML,2026-01-02,680.1,689.4,678.0,687.2,1200000\n",
    )

    assert result.ok is True, result.error
    assert result.rows_loaded == 1


def test_accepts_compustat_global_column_names(tmp_path, monkeypatch):
    result = _load(
        tmp_path,
        monkeypatch,
        "gvkey,tic,isin,datadate,prcod,prchd,prcld,prccd,cshtrd,curcdd\n"
        "015576,ASML,NL0010273215,2026-01-02,680.1,689.4,678.0,687.2,1200000,EUR\n",
    )

    assert result.ok is True, result.error
    assert result.rows_loaded == 1
    # `tic` outranks `gvkey`, so the readable ticker becomes the panel symbol.
    assert result.symbols == ["ASML"]


def test_duplicate_spellings_of_one_column_do_not_crash(tmp_path, monkeypatch):
    """Regression: ``Close`` + ``P`` both mapped to "close" and raised a bare
    ValueError out of DataFrame construction, bypassing LoadResult entirely."""
    result = _load(
        tmp_path,
        monkeypatch,
        "Code,Date,Open,High,Low,Close,P\nASML,2026-01-02,680.1,689.4,678.0,687.2,999.9\n",
    )

    assert result.ok is True, result.error
    assert result.rows_loaded == 1


def test_unpadded_close_wins_over_padded_close(tmp_path, monkeypatch):
    """``P`` pads the last price forward past a delisting; ``P#T`` does not, so
    when an extract carries both, the unpadded series must be the one kept."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    csv_file = tmp_path / "both.csv"
    csv_file.write_text(
        "Code,Date,PO,PH,PL,P,P#T\nDEAD,2026-01-02,10.0,10.0,10.0,42.0,7.5\n"
    )

    result = load_datastream_extract(csv_file, dry_run=False)

    assert result.ok is True, result.error
    frame = pd.read_parquet(tmp_path / "datastream_pit" / "DEAD.parquet")
    assert frame["close"].iloc[0] == 7.5


def test_compustat_ajexdi_split_adjusts_prices_and_volume(tmp_path, monkeypatch):
    """``prccd`` is unadjusted; the vendor conversion is price / ajexdi and
    shares * ajexdi. A 3-for-1 split would otherwise read as a -67% day."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    csv_file = tmp_path / "compustat.csv"
    csv_file.write_text(
        "tic,datadate,prcod,prchd,prcld,prccd,cshtrd,curcdd,ajexdi\n"
        "SAP,2026-01-02,300.0,306.0,297.0,303.0,1000,EUR,3.0\n"
        "SAP,2026-01-05,101.0,102.0,99.0,100.5,3000,EUR,1.0\n"
    )

    result = load_datastream_extract(csv_file, dry_run=False)

    assert result.ok is True, result.error
    assert result.split_adjusted is True
    frame = pd.read_parquet(tmp_path / "datastream_pit" / "SAP.parquet").sort_values("as_of_date")
    # Pre-split bar divided back onto the post-split scale: 303 / 3 = 101.
    assert frame["close"].tolist() == [101.0, 100.5]
    assert frame["open"].tolist() == [100.0, 101.0]
    assert frame["volume"].tolist() == [3000.0, 3000.0]


def test_extract_without_an_adjustment_factor_is_left_untouched(tmp_path, monkeypatch):
    """Datastream's ``P`` is already adjusted for capital changes and ships no
    factor column, so nothing may be scaled for those extracts."""
    result = _load(tmp_path, monkeypatch, "Code,Date,P,PO,PH,PL\nSAP,2026-01-02,100.0,99.0,101.0,98.0\n")

    assert result.ok is True, result.error
    assert result.split_adjusted is False


def test_missing_adjustment_factor_fails_loud(tmp_path, monkeypatch):
    """A partly-adjusted panel is the silent corruption this exists to stop, so
    a blank factor fails the load instead of leaving one row unadjusted."""
    result = _load(
        tmp_path,
        monkeypatch,
        "tic,datadate,prcod,prchd,prcld,prccd,ajexdi\n"
        "SAP,2026-01-02,300.0,306.0,297.0,303.0,3.0\n"
        "BAYN,2026-01-02,50.0,51.0,49.0,50.5,\n",
    )

    assert result.ok is False
    assert "BAYN" in (result.error or "")
    assert "adjustment factor" in (result.error or "")


def test_non_positive_adjustment_factor_fails_loud(tmp_path, monkeypatch):
    result = _load(
        tmp_path,
        monkeypatch,
        "tic,datadate,prcod,prchd,prcld,prccd,ajexdi\nSAP,2026-01-02,300.0,306.0,297.0,303.0,0\n",
    )

    assert result.ok is False
    assert "non-positive" in (result.error or "")


def test_cli_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    csv_file = tmp_path / "extract.csv"
    csv_file.write_text("Code,Date,Open,High,Low,Close\nSAP,2026-01-02,99,101,98,100\n")
    monkeypatch.setattr("sys.argv", ["datastream_loader", str(csv_file)])

    from app.foundation.data_engineering.datastream_loader import _main

    _main()

    assert not (tmp_path / "datastream_pit").exists()


def test_cli_exits_nonzero_on_a_bad_extract(tmp_path, monkeypatch):
    import pytest

    csv_file = tmp_path / "bad.csv"
    csv_file.write_text("Wrong,Headers\n1,2\n")
    monkeypatch.setattr("sys.argv", ["datastream_loader", str(csv_file)])

    from app.foundation.data_engineering.datastream_loader import _main

    with pytest.raises(SystemExit) as exc:
        _main()
    assert exc.value.code == 1


def test_two_issues_of_one_company_on_the_same_day_fail_loud(tmp_path, monkeypatch):
    """A Compustat extract carrying every ``iid`` of a company yields two rows
    for one (tic, datadate); silently keeping one of them is the bug."""
    result = _load(
        tmp_path,
        monkeypatch,
        "tic,datadate,prcod,prchd,prcld,prccd\n"
        "RDS,2026-01-02,100.0,101.0,99.0,100.5\n"
        "RDS,2026-01-02,102.0,103.0,101.0,102.5\n",
    )

    assert result.ok is False
    assert "RDS" in (result.error or "")
    assert "more than one series per symbol per day" in (result.error or "")


def test_loads_a_universe_wide_extract_with_one_partition_per_symbol(tmp_path, monkeypatch):
    """Many symbols must not degrade into a per-symbol rescan of the frame.

    The write path used to be ``validated[validated["symbol"] == s]`` inside a
    loop over symbols, i.e. O(rows x symbols). Measured on this machine: 0.77s
    for 250 symbols, 3.0s for 500, 11.2s for 1,000, 45.3s for 2,000 -- roughly
    4x per doubling, extrapolating to ~17 hours for one year of Compustat
    Global (~74,000 securities), for byte-identical output.

    No wall-clock assertion here: the blow-up only shows at a scale far beyond
    what a unit test should build, so a timing bound at this size would be
    noise. This pins the correctness half -- every symbol gets its own
    partition with its own rows -- and the grouping is what makes it tractable.
    """
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    n_symbols, n_days = 400, 12
    dates = pd.date_range("2024-01-02", periods=n_days).strftime("%Y-%m-%d")
    lines = ["Code,Date,Open,High,Low,Close,Volume,Currency"]
    for i in range(n_symbols):
        for j, day in enumerate(dates):
            lines.append(f"SYM{i:05d},{day},10.0,11.0,9.0,{10 + j}.0,1000,EUR")
    extract = tmp_path / "universe.csv"
    extract.write_text("\n".join(lines) + "\n")

    result = load_datastream_extract(extract, dry_run=False)

    assert result.ok is True, result.error
    assert result.rows_loaded == n_symbols * n_days
    assert len(result.symbols) == n_symbols

    written = sorted((tmp_path / "datastream_pit").glob("*.parquet"))
    assert len(written) == n_symbols
    # Spot-check that grouping put the right rows in the right file, which a
    # naive groupby-with-wrong-key would silently get wrong.
    sample = pd.read_parquet(tmp_path / "datastream_pit" / "SYM00042.parquet")
    assert len(sample) == n_days
    assert set(sample["symbol"]) == {"SYM00042"}
    assert sample["close"].tolist() == [float(10 + j) for j in range(n_days)]


def test_unmapped_columns_are_ignored_rather_than_read(tmp_path, monkeypatch):
    """A real Compustat export carries columns this panel has no use for.

    They are dropped at read time via ``usecols`` -- on a universe-wide extract
    that is the difference between a few GB of unused Python strings and none.
    """
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    extract = tmp_path / "with_extras.csv"
    extract.write_text(
        "gvkey,iid,conm,exchg,tic,datadate,prcod,prchd,prcld,prccd,cshtrd,curcdd\n"
        "001001,01W,Some Company PLC,194,ACME,2024-01-02,10.0,11.0,9.0,10.5,5000,GBP\n"
    )

    result = load_datastream_extract(extract, dry_run=False)

    assert result.ok is True, result.error
    frame = pd.read_parquet(tmp_path / "datastream_pit" / "ACME.parquet")
    # tic won the symbol slot over gvkey; conm/exchg/iid never reached the panel.
    assert frame["symbol"].iloc[0] == "ACME"
    assert frame["currency"].iloc[0] == "GBP"
    assert "conm" not in frame.columns
    assert "iid" not in frame.columns
