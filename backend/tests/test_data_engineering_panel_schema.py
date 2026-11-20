"""Tests for the PIT Parquet panel's shared schema (app.foundation.data_engineering)."""
from datetime import datetime, UTC

import pandas as pd
import pytest

from app.foundation.data_engineering.panel_schema import (
    PANEL_COLUMNS,
    PanelSchemaError,
    validate_panel_frame,
)


def _valid_frame(source: str = "internal") -> pd.DataFrame:
    now = datetime.now(UTC).replace(tzinfo=None)
    return pd.DataFrame({
        "symbol": ["IWDA.AS"],
        "isin": ["IE00B4L5Y983"],
        "as_of_date": [now],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [1000.0],
        "currency": ["EUR"],
        "source": [source],
        "ingested_at": [now],
    })


def test_validate_panel_frame_accepts_a_conforming_frame():
    out = validate_panel_frame(_valid_frame())
    assert list(out.columns) == list(PANEL_COLUMNS)
    assert out.iloc[0]["symbol"] == "IWDA.AS"


def test_validate_panel_frame_rejects_missing_columns():
    df = _valid_frame().drop(columns=["close"])
    with pytest.raises(PanelSchemaError, match="missing required columns"):
        validate_panel_frame(df)


def test_validate_panel_frame_rejects_unknown_source():
    with pytest.raises(PanelSchemaError, match="unknown panel source"):
        validate_panel_frame(_valid_frame(source="made_up_source"))


def test_validate_panel_frame_accepts_datastream_pit_source():
    out = validate_panel_frame(_valid_frame(source="datastream_pit"))
    assert out.iloc[0]["source"] == "datastream_pit"
