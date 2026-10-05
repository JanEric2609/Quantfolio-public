"""Tests for ETF API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _override_auth():
    """Override current_user dependency for all tests in this module."""
    from app.main import app
    from app.foundation.models.entities import User
    from app.foundation.auth import current_user

    app.dependency_overrides[current_user] = lambda: User(
        id="test-user", username="testuser"
    )
    yield
    app.dependency_overrides.pop(current_user, None)


@pytest.fixture
def client():
    """Create a fresh TestClient to avoid module-level import issues."""
    from app.main import app
    return TestClient(app)


class TestEtfCompositionEndpoint:
    """Test GET /api/etf/{ticker}/composition endpoint."""

    @patch("app.interface.api.etf.get_etf_composition")
    def test_returns_composition_data(self, mock_get, client):
        """Should return ETF composition data."""
        from app.foundation.etf_lookup import EtfComposition, EtfHolding

        mock_get.return_value = EtfComposition(
            ticker="VWCE.DE",
            name="Vanguard FTSE All-World UCITS ETF",
            holdings=[EtfHolding(ticker="AAPL", weight=0.04, name="Apple Inc.")],
            sectors={"Technology": 0.25},
            regions={"North America": 0.55},
        )

        response = client.get("/api/etf/VWCE.DE/composition")
        
        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "VWCE.DE"
        assert data["name"] == "Vanguard FTSE All-World UCITS ETF"
        assert len(data["holdings"]) == 1
        assert data["holdings"][0]["ticker"] == "AAPL"
        assert data["sectors"]["Technology"] == 0.25
        assert data["regions"]["North America"] == 0.55

    @patch("app.interface.api.etf.get_etf_composition")
    def test_returns_404_for_unknown_ticker(self, mock_get, client):
        """Should return 404 for unknown ETF ticker."""
        mock_get.return_value = None

        response = client.get("/api/etf/UNKNOWN_TICKER/composition")
        
        assert response.status_code == 404
        # App wraps errors as {"error": {"code": 404, "message": "..."}}
        body = response.json()
        msg = body.get("error", {}).get("message", body.get("detail", ""))
        assert "not found" in msg.lower()

    @patch("app.interface.api.etf.get_etf_composition")
    def test_handles_empty_composition(self, mock_get, client):
        """Should handle ETF with no holdings data."""
        from app.foundation.etf_lookup import EtfComposition

        mock_get.return_value = EtfComposition(
            ticker="TEST.ETF",
            name="Test ETF",
            holdings=[],
            sectors={},
            regions={},
        )

        response = client.get("/api/etf/TEST.ETF/composition")
        
        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "TEST.ETF"
        assert data["holdings"] == []
        assert data["sectors"] == {}
        assert data["regions"] == {}
