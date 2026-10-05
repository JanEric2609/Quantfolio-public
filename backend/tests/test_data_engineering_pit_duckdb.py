"""Parity tests for the DuckDB predicate-pushdown read path (``_pit_duckdb``).

Golden comparisons against the pre-migration path -- the loaders' full-panel
``read_*`` functions, filtered in pandas -- both at the query-function level
and end-to-end through the public ``pit_*_for_symbol`` joins. Fixtures are
written through the real loaders into a ``tmp_path`` panel dir.
"""
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_engineering import _pit_duckdb
from app.foundation.data_engineering import pit_panel_joins as joins
from app.foundation.data_engineering.ibes_estimates_loader import (
    load_ibes_estimates_extract,
    read_ibes_estimates,
)
from app.foundation.data_engineering.insider_trading_loader import (
    load_insider_trading_extract,
    read_insider_trading,
)
from app.foundation.data_engineering.security_identifiers_loader import (
    load_security_identifiers_extract,
)
from app.foundation.data_engineering.wrds_factors_loader import (
    load_wrds_factors_extract,
    read_wrds_factor_characteristics,
)
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


_WRDS_HEADER = "gvkey,permno,eom,excntry,size_grp,me,mom_12_1,be_me,gp_at,at_gr1,ret_exc_lead1m\n"
_IBES_HEADER = (
    "ticker,cusip,oftic,statpers,fpedats,fpi,measure,numest,medest,meanest,stdev,"
    "highest,lowest,actual,anndats_act,curr,usfirm\n"
)


def _load_wrds(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_text(_WRDS_HEADER + body)
    result = load_wrds_factors_extract(path, dry_run=False)
    assert result.ok is True, result.error


def _load_ibes(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_text(_IBES_HEADER + body)
    result = load_ibes_estimates_extract(path, dry_run=False)
    assert result.ok is True, result.error


def _load_insider(source_dir: Path, rows: list[tuple[str, ...]]) -> None:
    """rows: (accession, filing_date, issuer_cik, ticker, cik, transaction_date, code, shares, price)."""
    source_dir.mkdir(parents=True, exist_ok=True)
    submission = ["ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL"]
    owners = ["ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP"]
    trans = [
        "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
        "TRANS_ACQUIRED_DISP_CD\tSHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP"
    ]
    for acc, filed, issuer, ticker, cik, traded, code, shares, price in rows:
        submission.append(f"{acc}\t{filed}\tForm4\t{issuer}\tIssuer {issuer}\t{ticker}")
        owners.append(f"{acc}\t{cik}\tInsider {cik}\tOfficer")
        trans.append(f"{acc}\t{traded}\t{code}\t{shares}\t{price}\tA\t1000\tD")
    (source_dir / "SUBMISSION.tsv").write_text("\n".join(submission) + "\n")
    (source_dir / "REPORTINGOWNER.tsv").write_text("\n".join(owners) + "\n")
    (source_dir / "NONDERIV_TRANS.tsv").write_text("\n".join(trans) + "\n")
    result = load_insider_trading_extract(source_dir, dry_run=False)
    assert result.ok is True, result.error


def _seed_panel(tmp_path: Path, monkeypatch) -> None:
    """Two symbols per source, a superseding re-load per source, and noise rows."""
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))

    identifiers = tmp_path / "identifiers.csv"
    identifiers.write_text("gvkey,isin,fic,datadate\n1001,US0001,USA,2020-01-01\n1002,US0002,USA,2020-01-01\n")
    assert load_security_identifiers_extract(identifiers, dry_run=False).ok

    _load_wrds(
        tmp_path,
        "chars_a.csv",
        "1001,9001,2023-11-30,USA,large,4900.0,0.10,0.6,0.25,0.05,0.99\n"
        "1001,9001,2023-12-31,USA,large,5000.0,0.15,0.6,0.25,0.05,0.99\n"
        "1002,9002,2023-12-31,USA,small,100.0,,0.9,0.10,0.01,0.50\n",
    )
    # Later extract supersedes 1001's December row.
    _load_wrds(tmp_path, "chars_b.csv", "1001,9001,2023-12-31,USA,large,5200.0,0.17,0.6,0.25,0.05,0.99\n")

    _load_ibes(
        tmp_path,
        "ibes_a.csv",
        "0001,,TESTCO,2023-10-15,2024-12-31,1,EPS,5,2.00,2.00,0.10,2.10,1.90,,,USD,1\n"
        "0001,,TESTCO,2023-11-15,2024-12-31,1,EPS,5,2.20,2.20,0.10,2.30,2.10,2.35,2023-11-10,USD,1\n"
        "0001,,TESTCO,2023-11-15,2024-12-31,2,EPS,5,9.00,9.00,0.10,9.30,9.10,,,USD,1\n"
        "0002,,OTHER,2023-11-15,2024-12-31,1,EPS,5,1.00,1.00,0.10,1.10,0.90,,,USD,1\n",
    )
    # Later extract re-tags IBES ticker 0001's November snapshot to a new oftic:
    # the superseded TESTCO row must disappear on both paths, not linger.
    _load_ibes(
        tmp_path,
        "ibes_b.csv",
        "0001,,NEWCO,2023-11-15,2024-12-31,1,EPS,5,2.40,2.40,0.10,2.50,2.30,2.35,2023-11-10,USD,1\n",
    )

    _load_insider(
        tmp_path / "sec_a",
        [
            ("0001", "05-DEC-2023", "500", "TESTCO", "1", "03-DEC-2023", "P", "1000", "10.00"),
            ("0002", "07-DEC-2023", "500", "TESTCO", "2", "06-DEC-2023", "P", "500", "10.00"),
            ("0003", "08-DEC-2023", "500", "TESTCO", "3", "07-DEC-2023", "S", "200", "10.00"),
            ("0004", "08-DEC-2023", "500", "TESTCO", "4", "07-DEC-2023", "A", "999", "0.00"),
            ("0005", "06-DEC-2023", "600", "OTHER", "5", "04-DEC-2023", "P", "10", "5.00"),
        ],
    )
    # Re-filed under a lower-case ticker: same dedup key, later ingestion wins.
    _load_insider(
        tmp_path / "sec_b",
        [("0006", "09-DEC-2023", "500", "testco", "2", "06-DEC-2023", "P", "500", "10.00")],
    )


def _legacy_reads(monkeypatch) -> None:
    """Point pit_panel_joins back at the pre-migration full-panel read + pandas filter."""

    def wrds(panel_dir, gvkeys):
        frame = read_wrds_factor_characteristics()
        return frame[frame["gvkey"].isin(gvkeys)]

    monkeypatch.setattr(joins, "wrds_factor_characteristics_for_gvkeys", wrds)
    monkeypatch.setattr(
        joins, "ibes_estimates_for_oftic", lambda panel_dir, oftic, fpi, measure: read_ibes_estimates(fpi=fpi, measure=measure)
    )
    monkeypatch.setattr(
        joins, "insider_trading_for_ticker", lambda panel_dir, ticker: read_insider_trading(open_market_only=True)
    )
    monkeypatch.setattr(joins, "pit_insider_signal_for_symbol", _legacy_pit_insider_signal_for_symbol)


def _legacy_insider_frame(opportunistic: pd.DataFrame, dates) -> pd.DataFrame:
    """The pre-Track-B per-date numpy loop, verbatim."""
    filing_dates = opportunistic["filing_date"].to_numpy()
    is_buy = (opportunistic["transaction_code"] == "P").to_numpy()
    is_sell = (opportunistic["transaction_code"] == "S").to_numpy()
    signed_dollars = (
        opportunistic["shares"].to_numpy(dtype="float64")
        * opportunistic["price_per_share"].to_numpy(dtype="float64")
        * (is_buy.astype("float64") - is_sell.astype("float64"))
    )
    cik_values = opportunistic["cik"].to_numpy()

    original_index = pd.DatetimeIndex(dates)
    naive_dates = (
        original_index.tz_localize(None) if original_index.tz is not None else original_index
    )

    rows: list[dict[str, Any]] = []
    for orig_date, date in zip(original_index, naive_dates, strict=False):
        cluster_start = date - pd.Timedelta(days=joins._CLUSTER_WINDOW_DAYS)
        flow_start = date - pd.Timedelta(days=joins._FLOW_WINDOW_DAYS)

        cluster_mask = is_buy & (filing_dates > cluster_start) & (filing_dates <= date)
        flow_mask = (filing_dates > flow_start) & (filing_dates <= date)

        rows.append(
            {
                "orig_date": orig_date,
                "cluster_buy_score": int(pd.Series(cik_values[cluster_mask]).nunique()),
                "net_insider_flow_usd": float(signed_dollars[flow_mask].sum()) if flow_mask.any() else 0.0,
                "data_confidence": "ticker_match",
            }
        )

    return pd.DataFrame(rows).set_index("orig_date")


def _legacy_pit_insider_signal_for_symbol(db, symbol, dates):
    """The pre-Track-B ``pit_insider_signal_for_symbol`` (same filtering, legacy loop)."""
    if not symbol:
        return None
    raw = joins.insider_trading_for_ticker(None, symbol)
    ticker_match = raw[raw["ticker"].astype("string").str.upper() == symbol.upper()]
    if ticker_match.empty:
        return None
    ticker_match = ticker_match.dropna(subset=["filing_date", "transaction_date"]).copy()
    routine = joins._classify_routine_insiders(ticker_match)
    opportunistic = ticker_match[~routine.reindex(ticker_match.index, fill_value=False)]
    return _legacy_insider_frame(opportunistic, dates)


def test_query_functions_match_loader_reads(tmp_path, monkeypatch):
    _seed_panel(tmp_path, monkeypatch)

    legacy = read_wrds_factor_characteristics()
    expected = legacy[legacy["gvkey"].isin(["001001"])].reset_index(drop=True)
    actual = _pit_duckdb.wrds_factor_characteristics_for_gvkeys(tmp_path, ["001001"])
    pd.testing.assert_frame_equal(actual, expected)
    assert actual.loc[actual["eom"] == "2023-12-31", "me"].tolist() == [5200.0]

    legacy = read_ibes_estimates(fpi="1", measure="EPS")
    group = legacy.loc[legacy["oftic"].str.upper() == "TESTCO", "ticker"]
    expected = legacy[legacy["ticker"].isin(group)].reset_index(drop=True)
    actual = _pit_duckdb.ibes_estimates_for_oftic(tmp_path, "testco", "1", "eps")
    pd.testing.assert_frame_equal(actual, expected)

    legacy = read_insider_trading(open_market_only=True)
    group = legacy.loc[legacy["ticker"].str.upper() == "TESTCO", "issuer_cik"]
    expected = legacy[legacy["issuer_cik"].isin(group)].reset_index(drop=True)
    actual = _pit_duckdb.insider_trading_for_ticker(tmp_path, "TestCo")
    pd.testing.assert_frame_equal(actual, expected)
    assert set(actual["transaction_code"]) == {"P", "S"}


@pytest.mark.parametrize("symbol", ["TESTCO", "OTHER", "NEWCO", "NOPE"])
def test_pit_joins_match_legacy_full_read_path(tmp_path, monkeypatch, symbol):
    _seed_panel(tmp_path, monkeypatch)
    # Read-path parity only; masking dates past the extract is tested in
    # test_data_engineering_pit_panel_joins.
    monkeypatch.setattr(joins, "insider_trading_coverage", lambda panel_dir: None)
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")
    _seed_security(db, "US0002", "OTHER")
    dates = pd.DatetimeIndex(["2023-11-20", "2023-12-10", "2024-01-15"], tz="UTC")

    names = [
        "pit_wrds_factor_characteristics_for_symbol",
        "pit_ibes_estimate_signal_for_symbol",
        "pit_insider_signal_for_symbol",
        "pit_ml_features_for_symbol",
    ]
    new = [getattr(joins, name)(db, symbol, dates) for name in names]
    with monkeypatch.context() as m:
        _legacy_reads(m)
        old = [getattr(joins, name)(db, symbol, dates) for name in names]

    for got, want in zip(new, old, strict=True):
        if want is None:
            assert got is None
        else:
            pd.testing.assert_frame_equal(got, want)


def test_superseded_ibes_row_does_not_leak(tmp_path, monkeypatch):
    _seed_panel(tmp_path, monkeypatch)
    db = _memory_db()
    joined = joins.pit_ibes_estimate_signal_for_symbol(db, "TESTCO", pd.DatetimeIndex(["2023-12-01"]))
    assert joined is not None
    # Only the October snapshot still carries TESTCO: no SUE, no prior to revise from.
    assert pd.isna(joined.iloc[0]["sue"])
    assert pd.isna(joined.iloc[0]["revision_momentum"])


def test_empty_panel_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    identifiers = tmp_path / "identifiers.csv"
    identifiers.write_text("gvkey,isin,fic,datadate\n1001,US0001,USA,2020-01-01\n")
    assert load_security_identifiers_extract(identifiers, dry_run=False).ok
    db = _memory_db()
    _seed_security(db, "US0001", "TESTCO")
    dates = pd.DatetimeIndex(["2024-01-15"])

    assert joins.pit_wrds_factor_characteristics_for_symbol(db, "TESTCO", dates) is None
    assert joins.pit_ibes_estimate_signal_for_symbol(db, "TESTCO", dates) is None
    assert joins.pit_insider_signal_for_symbol(db, "TESTCO", dates) is None

    features = joins.pit_ml_features_for_symbol(db, "TESTCO", dates)
    assert features[["wrds_factor_available", "ibes_estimate_available", "insider_signal_available"]].eq(0.0).to_numpy().all()


def test_duckdb_config_resolution(monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_MEMORY_LIMIT", "512MB")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_THREADS", "1")
    monkeypatch.setenv("QUANTFOLIO_DUCKDB_TMPDIR", "/tmp/duck-spill")
    assert _pit_duckdb._duckdb_config() == {
        "memory_limit": "512MB",
        "threads": "1",
        "temp_directory": "/tmp/duck-spill",
    }

    _pit_duckdb.reset_connection()
    try:
        cursor = _pit_duckdb._connection().cursor()
        assert cursor.execute("SELECT current_setting('threads')").fetchone() == (1,)
    finally:
        _pit_duckdb.reset_connection()


def _random_opportunistic(rng: np.random.Generator, n: int) -> pd.DataFrame:
    shares = rng.integers(1, 5000, n).astype("float64")
    shares[rng.random(n) < 0.05] = np.nan
    return pd.DataFrame(
        {
            "cik": pd.array([f"C{c}" for c in rng.integers(0, 12, n)], dtype="string"),
            "transaction_code": rng.choice(["P", "S"], n),
            "filing_date": pd.Timestamp("2023-01-01") + pd.to_timedelta(rng.integers(0, 500, n), unit="D"),
            "shares": shares,
            "price_per_share": rng.uniform(1, 400, n).round(2),
        },
        index=rng.permutation(n) * 3,
    )


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("tz", [None, "UTC"])
def test_insider_range_join_matches_legacy_loop(seed, tz):
    rng = np.random.default_rng(seed)
    opportunistic = _random_opportunistic(rng, int(rng.integers(0, 60)))
    # Duplicates, window-edge hits (filing_date + 30/90 days exactly) and NaT.
    edges = list(opportunistic["filing_date"].head(3) + pd.Timedelta(days=30))
    edges += list(opportunistic["filing_date"].head(3) + pd.Timedelta(days=90))
    raw = list(pd.Timestamp("2022-12-01") + pd.to_timedelta(rng.integers(0, 620, 40), unit="D"))
    dates = pd.DatetimeIndex(raw + raw[:5] + edges + [pd.NaT], tz=tz)

    naive = dates.tz_localize(None) if dates.tz is not None else dates
    cluster, flow = _pit_duckdb.insider_trailing_window_signal(
        opportunistic, naive, joins._CLUSTER_WINDOW_DAYS, joins._FLOW_WINDOW_DAYS
    )
    want = _legacy_insider_frame(opportunistic, dates)

    assert cluster == want["cluster_buy_score"].tolist()
    # Kahan vs numpy pairwise summation: equal up to rounding, NaN where NaN.
    np.testing.assert_allclose(flow, want["net_insider_flow_usd"].to_numpy(), rtol=1e-12, atol=1e-6)


def test_insider_signal_frame_matches_legacy_on_random_panel(monkeypatch):
    rng = np.random.default_rng(42)
    opportunistic = _random_opportunistic(rng, 50)
    opportunistic["shares"] = opportunistic["shares"].fillna(10.0)
    frame = opportunistic.assign(ticker="RND", transaction_date=opportunistic["filing_date"])
    monkeypatch.setattr(joins, "insider_trading_for_ticker", lambda panel_dir, ticker: frame)
    monkeypatch.setattr(joins, "get_panel_dir", lambda: None)
    monkeypatch.setattr(joins, "insider_trading_coverage", lambda panel_dir: None)
    dates = pd.date_range("2023-01-01", periods=30, freq="W", tz="Europe/Berlin")

    got = joins.pit_insider_signal_for_symbol(None, "rnd", dates)
    want = _legacy_pit_insider_signal_for_symbol(None, "rnd", dates)
    pd.testing.assert_frame_equal(got, want, rtol=1e-12)
