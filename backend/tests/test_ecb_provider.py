"""Tests for the ECB SDW provider's SDMX-JSON parsing.

Regression coverage for the €STR risk-free-rate refresh job silently
returning ok=False in production since 2026-08-24: the parser read
dataset["observations"] directly, but the real ECB SDMX-JSON response nests
observations one level deeper, under dataset["series"]["<key>"]["observations"].
"""
from unittest.mock import MagicMock, patch

from app.foundation.providers.ecb_provider import EcbProvider

# Real ECB Data Portal SDMX-JSON response shape (trimmed): observations live
# under dataSets[0]["series"][seriesKey]["observations"][obsIndex], not
# directly under dataSets[0]["observations"].
REAL_SDMX_RESPONSE = {
    "dataSets": [
        {
            "action": "Information",
            "series": {
                "0:0:0:0:0": {
                    "observations": {
                        "0": [2.19, 0],
                        "1": [2.20, 0],
                        "2": [2.18, 0],
                    }
                }
            },
        }
    ],
    "structure": {"dimensions": {"observation": [{"id": "TIME_PERIOD"}]}},
}


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_sdw_series_parses_real_nested_shape():
    provider = EcbProvider()
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(REAL_SDMX_RESPONSE)
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = provider._fetch_sdw_series("FM.Q.U2.EUR.4F.KR.MRR_RT")

    assert result["ok"] is True
    assert len(result["data"]) == 3
    assert {obs["value"] for obs in result["data"]} == {2.19, 2.20, 2.18}


def test_fetch_sdw_series_no_series_returns_not_ok():
    provider = EcbProvider()
    empty_response = {"dataSets": [{"series": {}}]}
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(empty_response)
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = provider._fetch_sdw_series("FM.Q.U2.EUR.4F.KR.MRR_RT")

    assert result["ok"] is False
    assert result["data"] == []


def test_get_history_returns_ok_true_for_real_response_shape():
    provider = EcbProvider()
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _mock_response(REAL_SDMX_RESPONSE)
        mock_client_cls.return_value.__enter__.return_value = mock_client

        result = provider.get_history("FM.Q.U2.EUR.4F.KR.MRR_RT")

    assert result["ok"] is True
    assert len(result["data"]) == 3
