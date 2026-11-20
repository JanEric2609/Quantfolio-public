"""
Test CORS origins resolution and middleware configuration.
"""


def test_cors_allows_resolved_origins(monkeypatch):
    """
    Test that CORS middleware allows multiple origins resolved from env.

    Verifies that:
    1. Origin http://a.example gets echo in access-control-allow-origin
    2. Origin http://b.example gets echo in access-control-allow-origin
    """
    # Set environment with multiple origins (comma-separated)
    monkeypatch.setenv("FRONTEND_ORIGIN", "http://a.example, http://b.example")

    # Import create_app after setting env
    from app.main import create_app
    from fastapi.testclient import TestClient

    # Create app and client
    app = create_app()
    client = TestClient(app)

    # Test first origin
    response_a = client.get("/health", headers={"Origin": "http://a.example"})
    assert response_a.status_code == 200
    assert response_a.headers.get("access-control-allow-origin") == "http://a.example"

    # Test second origin
    response_b = client.get("/health", headers={"Origin": "http://b.example"})
    assert response_b.status_code == 200
    assert response_b.headers.get("access-control-allow-origin") == "http://b.example"
