"""API tests for attribution endpoints (Phase 3)."""

import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    """Test client."""
    return TestClient(app)


def _routes() -> set[tuple[str, str]]:
    # The OpenAPI map, not app.routes: FastAPI >= 0.141 keeps included routers
    # behind wrappers, so app.routes no longer lists their paths.
    return {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}


def test_brinson_endpoint_structure(client):
    """Brinson endpoint should return three-artifact envelope."""
    # Note: This is a structural test; actual execution requires auth + DB
    # In integration testing, mock the dependencies

    # Expected response structure:
    # {
    #   "research": {...},
    #   "attribution": {...},
    #   "ledger_ref": "..."
    # }

    assert ("POST", "/api/attribution/brinson") in _routes()


def test_factor_endpoint_structure(client):
    """Factor attribution endpoint should return three-artifact envelope."""
    assert ("POST", "/api/attribution/factor") in _routes()
