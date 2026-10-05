"""How current each research extract is (data_engineering.extract_freshness).

The WRDS exports (JKP to 2025-12, IBES) and the insider extract (to
2024-03-29) went stale without a visible sign; Settings -> Data now lists
each with its newest date.
"""
from datetime import date

import pandas as pd

from app.foundation.data_engineering.extract_freshness import research_extract_freshness
from app.interface.api.data import get_research_extracts


def _panel(tmp_path, subdir, column, values):
    folder = tmp_path / subdir
    folder.mkdir(parents=True)
    pd.DataFrame({column: pd.to_datetime(values), "x": range(len(values))}).to_parquet(folder / "part.parquet")


def test_each_extract_reports_its_newest_date_and_staleness(tmp_path):
    _panel(tmp_path, "insider_trading_pit", "filing_date", ["2026-09-01", "2026-09-25"])
    _panel(tmp_path, "wrds_factor_characteristics_pit", "eom", ["2025-11-30", "2025-12-31"])

    report = {e["key"]: e for e in research_extract_freshness(tmp_path, today=date(2026, 9, 29))}

    insider = report["insider_trading"]
    assert insider["newest"] == "2026-09-25" and insider["age_days"] == 4 and insider["rows"] == 2
    assert insider["stale"] is False and "Automatic" in insider["refresh"]
    jkp = report["jkp_characteristics"]
    assert jkp["newest"] == "2025-12-31" and jkp["age_days"] == 272 and jkp["stale"] is True
    assert "WRDS" in jkp["refresh"]
    missing = report["ibes_estimates"]
    assert missing["newest"] is None and missing["rows"] == 0 and missing["stale"] is True


def test_the_endpoint_returns_every_extract(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTFOLIO_PARQUET_PANEL_DIR", str(tmp_path))
    _panel(tmp_path, "ibes_estimates_pit", "statpers", ["2026-09-18"])

    rows = get_research_extracts(_user=None)  # type: ignore[arg-type]

    keys = [r.key for r in rows]
    assert keys == [
        "insider_trading", "ibes_estimates", "jkp_characteristics",
        "security_identifiers", "crsp_names", "ccm_link",
    ]
    assert next(r for r in rows if r.key == "ibes_estimates").newest == "2026-09-18"
