"""One-off after the 2026-11 news fixes (plan 2026-11-10, WP5): re-attribute and re-tag stored news.

Run on the app host after the migration:  python scripts/retag_news.py
"""
from __future__ import annotations

from app.foundation.core.db import SessionLocal
from app.foundation.news import retag_news


def main() -> None:
    with SessionLocal() as db:
        print(retag_news(db))


if __name__ == "__main__":
    main()
