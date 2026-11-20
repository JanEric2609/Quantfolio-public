"""Ensure no personal/hardcoded host IPs remain in config."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.settings import upsert_public_settings


def test_no_personal_ip():
    """Assert that personal IP 10.0.0.34 and localhost:8001 are not hardcoded.

    Checks:
    - app/foundation/llm/local_llama.py
    - app/foundation/settings.py
    - app/foundation/llm/router.py
    - app/decision/ai.py
    """
    backend_dir = Path(__file__).resolve().parents[1]
    files_to_check = [
        backend_dir / "app" / "foundation" / "llm" / "local_llama.py",
        backend_dir / "app" / "foundation" / "settings.py",
        backend_dir / "app" / "foundation" / "llm" / "router.py",
        backend_dir / "app" / "decision" / "ai.py",
    ]

    for file_path in files_to_check:
        content = file_path.read_text()
        assert "10.0.0.34" not in content, f"Found personal IP in {file_path}"
        assert "localhost:8001" not in content, f"Found localhost:8001 in {file_path}"


def test_configured_llm_uses_resolver():
    """Assert that configured_llm uses resolvers instead of hardcoded settings."""
    # Create in-memory DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    # Set up test settings
    upsert_public_settings(db, {"llm_base_url": "http://z:8080", "llm_model": "mz"})

    # Import and call configured_llm
    from app.decision.ai import configured_llm

    client = configured_llm(db)

    # Assert resolver outputs are used
    assert client.base_url == "http://z:8080/v1", f"Expected 'http://z:8080/v1', got '{client.base_url}'"
    assert client.model == "mz", f"Expected 'mz', got '{client.model}'"
