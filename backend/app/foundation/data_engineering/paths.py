"""Resolves the PIT Parquet panel's storage directory.

Same env-var -> DB setting -> default resolution order as
``app.foundation.quant_factors._get_cache_dir``.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_PANEL_DIR = "/tmp/quantfolio_parquet_panel"


def get_panel_dir() -> Path:
    """Resolve the PIT Parquet panel directory: env var first, then DB setting, then default."""
    env_val = os.environ.get("QUANTFOLIO_PARQUET_PANEL_DIR")
    if env_val:
        return Path(env_val)
    try:
        from app.foundation.core.db import SessionLocal
        from app.foundation.settings import get_public_settings

        with SessionLocal() as db:
            settings = get_public_settings(db)
            db_val = settings.get("parquet_panel_dir")
            if db_val:
                return Path(db_val)
    except Exception:
        pass
    return Path(_DEFAULT_PANEL_DIR)
