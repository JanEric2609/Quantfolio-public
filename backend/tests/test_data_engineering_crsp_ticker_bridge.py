"""CRSP ticker-history loader and the ticker -> permno -> gvkey fallback route
into the WRDS factor-characteristics join, plus its staleness cap."""
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering._pit_duckdb import (
    reset_connection,
    security_identifiers_for_isin,
)
from app.foundation.data_engineering.ccm_link_loader import load_ccm_link_extract
from app.foundation.data_engineering.crsp_names_loader import (
    load_crsp_names_extract,
    read_crsp_names,
)
from app.foundation.data_engineering.pit_panel_joins import (
    pit_ccm_link_quality_for_symbol,
    pit_ml_features_for_symbol,
    pit_wrds_factor_characteristics_for_symbol,
)
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
    read_security_identifiers,
)
from app.foundation.data_engineering.wrds_factors_loader import load_wrds_factors_extract


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture
def panel(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    reset_connection()
    yield tmp_path
    reset_connection()


def _load(tmp_path: Path, name: str, text: str, loader) -> None:
    path = tmp_path / name
    path.write_text(text)
    result = loader(path, dry_run=False)
    assert result.ok is True, result.error


_CHARACTERISTICS_HEADER = "gvkey,permno,eom,excntry,size_grp,me,mom_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m\n"


def _seed_us_company(tmp_path: Path) -> None:
    # permno 9001 traded as OLDT until 2020-12-31, then as AAPL (still current).
    _load(tmp_path, "names.csv",
          "PERMNO,SecInfoStartDt,SecInfoEndDt,Ticker,TradingSymbol\n"
          "9001,2000-01-01,2020-12-31,OLDT,OLDT\n"
          "9001,2021-01-01,9999-12-31,AAPL,AAPL\n",
          load_crsp_names_extract)
    _load(tmp_path, "ccm.csv",
          "gvkey,permno,permco,linkdt,linkenddt,linktype,linkprim\n"
          "1690,9001,7,2019-01-01,,LC,P\n",
          load_ccm_link_extract)
    _load(tmp_path, "chars.csv",
          _CHARACTERISTICS_HEADER
          + "1690,9001,2020-05-31,USA,mega,100.0,0.1,0.2,0.3,0.04,0.0\n"
          + "1690,9001,2023-12-31,USA,mega,500.0,0.5,0.6,0.7,0.08,0.0\n",
          load_wrds_factors_extract)


# --- loader -----------------------------------------------------------------


def test_loader_accepts_ciz_export_and_treats_9999_as_current(panel):
    csv = panel / "ciz.csv"
    csv.write_text(
        "PERMNO,SecInfoStartDt,SecInfoEndDt,Ticker,TradingSymbol\n"
        "10107,1986-03-13,9999-12-31,MSFT,MSFT\n"
        "12345,2001-01-01,2003-06-30,,\n"
    )
    dry = load_crsp_names_extract(csv)
    assert dry.ok is True, dry.error
    assert not (panel / "crsp_names_pit").exists()
    assert dry.rows_loaded == 1
    assert dry.rows_without_ticker_dropped == 1

    assert load_crsp_names_extract(csv, dry_run=False).ok is True
    names = read_crsp_names()
    assert names["ticker"].tolist() == ["MSFT"]
    assert pd.isna(names["nameenddt"].iloc[0])


def test_loader_accepts_legacy_dsenames_export(panel):
    csv = panel / "legacy.csv"
    csv.write_text("permno,namedt,nameendt,ticker,tsymbol\n84788,1998-01-01,,brk,brkb\n")
    result = load_crsp_names_extract(csv, dry_run=False)
    assert result.ok is True, result.error
    row = read_crsp_names().iloc[0]
    assert (row["ticker"], row["trading_symbol"]) == ("BRK", "BRKB")


def test_loader_rejects_missing_ticker_columns_and_bad_dates(panel):
    no_ticker = panel / "a.csv"
    no_ticker.write_text("permno,namedt\n1,2000-01-01\n")
    assert "ticker" in (load_crsp_names_extract(no_ticker).error or "")

    bad_date = panel / "b.csv"
    bad_date.write_text("permno,namedt,nameenddt,ticker\n1,not-a-date,,X\n")
    assert load_crsp_names_extract(bad_date).ok is False


# --- CRSP route -------------------------------------------------------------


def test_us_symbol_without_security_master_reaches_wrds_via_crsp(panel):
    _seed_us_company(panel)
    db = _memory_db()
    dates = pd.DatetimeIndex(["2024-01-15", "2018-06-01"], tz="UTC")

    joined = pit_wrds_factor_characteristics_for_symbol(db, "AAPL", dates)

    assert joined is not None
    assert joined.loc[dates[0], "gvkey"] == "001690"
    assert joined.loc[dates[0], "me"] == 500.0
    assert joined.loc[dates[0], "data_confidence"] == "crsp_ticker_ccm"
    # 2018 predates both the AAPL name window and the CCM link: unresolved.
    assert pd.isna(joined.loc[dates[1], "me"])


def test_ticker_history_is_point_in_time(panel):
    _seed_us_company(panel)
    db = _memory_db()
    in_old_window = pd.DatetimeIndex(["2020-06-15"])
    after_rename = pd.DatetimeIndex(["2024-01-15"])

    old = pit_wrds_factor_characteristics_for_symbol(db, "OLDT", in_old_window)
    assert old is not None and old["me"].iloc[0] == 100.0
    assert pit_wrds_factor_characteristics_for_symbol(db, "OLDT", after_rename) is None
    # AAPL did not exist under that name in mid-2020.
    assert pit_wrds_factor_characteristics_for_symbol(db, "AAPL", in_old_window) is None


def test_exchange_suffixed_symbol_never_takes_crsp_route(panel):
    _seed_us_company(panel)
    assert pit_wrds_factor_characteristics_for_symbol(_memory_db(), "AAPL.DE", pd.DatetimeIndex(["2024-01-15"])) is None


def test_share_class_spellings_match_trading_symbol(panel):
    _load(panel, "names.csv", "permno,namedt,nameenddt,ticker,tsymbol\n84788,1998-01-01,,BRK,BRKB\n",
          load_crsp_names_extract)
    _load(panel, "ccm.csv", "gvkey,permno,permco,linkdt,linkenddt,linktype,linkprim\n1000,84788,1,1990-01-01,,LC,P\n",
          load_ccm_link_extract)
    _load(panel, "chars.csv", _CHARACTERISTICS_HEADER + "1000,84788,2023-12-31,USA,mega,9.0,0,0,0,0,0\n",
          load_wrds_factors_extract)
    dates = pd.DatetimeIndex(["2024-01-15"])
    for spelling in ("BRK-B", "BRK.B"):
        joined = pit_wrds_factor_characteristics_for_symbol(_memory_db(), spelling, dates)
        assert joined is not None and joined["me"].iloc[0] == 9.0, spelling


def test_ambiguous_ticker_is_left_unresolved(panel):
    _load(panel, "names.csv",
          "permno,namedt,nameenddt,ticker\n1,2000-01-01,,DUP\n2,2000-01-01,,DUP\n",
          load_crsp_names_extract)
    _load(panel, "ccm.csv",
          "gvkey,permno,permco,linkdt,linkenddt,linktype,linkprim\n"
          "1001,1,1,1990-01-01,,LC,P\n1002,2,2,1990-01-01,,LC,P\n",
          load_ccm_link_extract)
    _load(panel, "chars.csv", _CHARACTERISTICS_HEADER + "1001,1,2023-12-31,USA,mega,1,0,0,0,0,0\n",
          load_wrds_factors_extract)
    assert pit_wrds_factor_characteristics_for_symbol(_memory_db(), "DUP", pd.DatetimeIndex(["2024-01-15"])) is None


def test_ccm_quality_reports_route(panel):
    _seed_us_company(panel)
    quality = pit_ccm_link_quality_for_symbol(_memory_db(), "AAPL", "2024-01-15")
    assert quality is not None
    assert quality["gvkey"] == "001690"
    assert quality["permnos"] == [9001]
    assert quality["data_confidence"] == "crsp_ticker_ccm"


# --- staleness cap ----------------------------------------------------------


def test_stale_characteristics_are_not_carried_forward(panel):
    _seed_us_company(panel)
    dates = pd.DatetimeIndex(["2024-03-15", "2024-06-15"])  # 75 and 167 days after eom

    joined = pit_wrds_factor_characteristics_for_symbol(_memory_db(), "AAPL", dates)
    assert joined is not None
    assert joined["me"].iloc[0] == 500.0
    assert pd.isna(joined["me"].iloc[1])

    features = pit_ml_features_for_symbol(_memory_db(), "AAPL", dates)
    assert features["wrds_factor_available"].tolist() == [1.0, 0.0]


# --- ISIN route pushdown ----------------------------------------------------


def test_isin_pushdown_matches_full_read(panel):
    _load(panel, "ids.csv",
          "gvkey,isin,fic,datadate\n"
          "1001,US0001,USA,2020-01-01\n1001,US0001,USA,2020-01-02\n"
          "1002,US0002,USA,2020-01-01\n1003,US0001,USA,2021-01-01\n1001,US0001,USA,2020-01-01\n",
          load_security_identifiers_extract)
    full = read_security_identifiers()
    expected = full[full["isin"] == "US0001"].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        security_identifiers_for_isin(panel, "US0001")[["gvkey", "as_of_date"]],
        expected[["gvkey", "as_of_date"]],
    )
    assert security_identifiers_for_isin(panel, "NOPE").empty


# --- CCM pushdown + share-class matching on the ticker-match sources --------


def test_ccm_pushdown_matches_full_read(panel):
    from app.foundation.data_engineering._pit_duckdb import ccm_links_for_gvkey, ccm_links_for_permnos
    from app.foundation.data_engineering.ccm_link_loader import read_ccm_link

    header = "gvkey,permno,permco,linkdt,linkenddt,linktype,linkprim\n"
    _load(panel, "a.csv", header + "1001,11,1,1990-01-01,,LC,P\n1002,12,2,1990-01-01,2000-01-01,LU,C\n", load_ccm_link_extract)
    # Later extract supersedes 1001/11's row (same dedup key, new end date).
    _load(panel, "b.csv", header + "1001,11,1,1990-01-01,2010-01-01,LC,P\n1003,13,3,1990-01-01,,LX,P\n", load_ccm_link_extract)
    full = read_ccm_link()
    cols = ["gvkey", "permno", "linkdt", "linkenddt", "linktype", "linkprim"]

    got = ccm_links_for_permnos(panel, [11, 13])[cols].reset_index(drop=True)
    want = full[full["permno"].isin([11, 13])].sort_values(["gvkey", "permno", "linkdt"])[cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(got, want)
    assert ccm_links_for_gvkey(panel, "001001")["linkenddt"].tolist() == [pd.Timestamp("2010-01-01")]
    assert ccm_links_for_permnos(panel, []).empty


def test_ibes_and_insider_match_share_class_spellings(panel):
    from app.foundation.data_engineering.ibes_estimates_loader import load_ibes_estimates_extract
    from app.foundation.data_engineering.insider_trading_loader import load_insider_trading_extract
    from app.foundation.data_engineering.pit_panel_joins import (
        pit_ibes_estimate_signal_for_symbol,
        pit_insider_signal_for_symbol,
    )

    from tests.test_data_engineering_pit_panel_joins import _write_insider_tsvs

    ibes = panel / "ibes.csv"
    ibes.write_text(
        "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
        "highest,lowest,actual,anndats_act,curr,usfirm\n"
        "BRK1,,BRK.B,2024-01-18,2024-12-31,1,EPS,5,20.0,20.0,1.0,21.0,19.0,,,USD,1\n"
    )
    assert load_ibes_estimates_extract(ibes, dry_run=False).ok
    _write_insider_tsvs(panel / "sec", [{
        "accession": "0001", "filing_date": "04-JAN-2024", "issuer_cik": "1067983", "ticker": "BRK.B",
        "cik": "1", "insider_name": "A", "transaction_date": "02-JAN-2024", "code": "P",
        "shares": "100", "price": "350.00",
    }, {
        # Another issuer's later filing, so the extract covers the query
        # date: past its last filing an insider window is unknown, not 0.
        "accession": "0002", "filing_date": "05-FEB-2024", "issuer_cik": "320193", "ticker": "AAPL",
        "cik": "2", "insider_name": "B", "transaction_date": "01-FEB-2024", "code": "S",
        "shares": "10", "price": "180.00",
    }])
    assert load_insider_trading_extract(panel / "sec", dry_run=False).ok

    dates = pd.DatetimeIndex(["2024-02-01"])
    for spelling in ("BRK-B", "BRK.B", "brkb"):
        assert pit_ibes_estimate_signal_for_symbol(_memory_db(), spelling, dates) is not None, spelling
        signal = pit_insider_signal_for_symbol(_memory_db(), spelling, dates)
        assert signal is not None and signal["cluster_buy_score"].iloc[0] == 1, spelling
    assert pit_insider_signal_for_symbol(_memory_db(), "BRK-A", dates) is None


def test_jkp_ret_12_1_loads_as_momentum(panel):
    from app.foundation.data_engineering.wrds_factors_loader import read_wrds_factor_characteristics

    _load(panel, "jkp.csv",
          "gvkey,id,eom,excntry,size_grp,me,ret_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m\n"
          "1690,9001,2025-12-31,USA,mega,1.0,0.25,0.2,0.3,0.04,0.0\n",
          load_wrds_factors_extract)
    assert read_wrds_factor_characteristics()["mom_12_1"].tolist() == [0.25]
