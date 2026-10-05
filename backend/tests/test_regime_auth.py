"""Test that the /api/quant/regime/current endpoint requires authentication."""
from fastapi.testclient import TestClient


def test_regime_current_requires_auth():
    """Unauthenticated request to regime/current must return 401."""
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/quant/regime/current")
    assert response.status_code == 401
