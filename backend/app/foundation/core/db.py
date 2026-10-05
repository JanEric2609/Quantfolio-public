import os
import sys
from collections.abc import Generator

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.config import get_settings
from app.foundation.core.db_base import Base


def _register_numpy_adapters() -> None:
    """Coerce numpy scalars to native Python types before they reach psycopg2.

    Without this, an ``np.float64``/``np.int64`` value written through the ORM
    (e.g. a sigmoid output from a scikit-learn model) produces invalid SQL and
    raises an unhandled adaptation error deep in the driver.
    See https://github.com/psycopg2/psycopg2/issues/1718.
    """
    try:
        import psycopg2.extensions
    except ImportError:
        return

    psycopg2.extensions.register_adapter(
        np.float64, lambda v: psycopg2.extensions.AsIs(float(v))
    )
    psycopg2.extensions.register_adapter(
        np.int64, lambda v: psycopg2.extensions.AsIs(int(v))
    )


_register_numpy_adapters()


def _engine_args(database_url: str) -> dict:
    if database_url == "sqlite:///:memory:":
        return {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    # client_encoding is psycopg2-specific; skip for SQLite/mem
    if database_url.startswith("postgresql"):
        return {
            "pool_pre_ping": True,
            # SQLAlchemy's default (5 + 10 overflow) is smaller than what the
            # worker's 16 job threads can hold (a tracked job keeps two
            # connections on PostgreSQL); tunable per process via env.
            "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
            "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
            "connect_args": {"client_encoding": "utf8"},
        }
    return {"pool_pre_ping": True}


settings = get_settings()

engine = create_engine(settings.database_url, **_engine_args(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    # Guard: pytest must never use a file-backed database.
    # Checked here (not at module level) to avoid import-order issues.
    if "pytest" in sys.modules and not settings.database_url.startswith("sqlite:///:memory:"):
        raise RuntimeError(
            "pytest detected with a file-backed DATABASE_URL. "
            "Tests must use sqlite:///:memory: to avoid polluting the dev database."
        )
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    from app.foundation.models import entities  # noqa: F401

    Base.metadata.create_all(bind=engine)
