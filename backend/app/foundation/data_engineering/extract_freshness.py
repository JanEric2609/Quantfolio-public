"""How current each research extract in the Parquet panel is.

Three of Discover's inputs came from WRDS exports that nobody refreshes, and
went stale without a visible sign: the JKP characteristics end 2025-12, the
IBES consensus goes blank after 92 days, and the insider extract ended
2024-03-29 (automated since 2026-09-29, ``sec_edgar_insider``). This report
backs the "Research extracts" card in Settings -> Data (owner decision
2026-09-29: keep the WRDS exports, and make a missed one visible).

``stale_after_days`` follows how often the source publishes, so "stale" means
a newer release exists that is not loaded yet. It is not the join's own
tolerance: JKP rows older than 92 days are still left out of the joins
(``pit_panel_joins._WRDS_MAX_STALENESS_DAYS``). Between JKP's yearly releases
those features are therefore empty, and nothing on our side can fill them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.foundation.data_engineering._pit_duckdb import panel_newest
from app.foundation.data_engineering.paths import get_panel_dir

_WRDS = "Manual WRDS export (docs/runbooks/wrds-jkp-extract.md)"


@dataclass(frozen=True)
class ExtractSpec:
    key: str
    label: str
    subdir: str
    date_column: str
    refresh: str
    stale_after_days: int
    used_for: str


EXTRACTS: tuple[ExtractSpec, ...] = (
    ExtractSpec(
        "insider_trading", "SEC insider transactions (Form 4)", "insider_trading_pit", "filing_date",
        "Automatic, daily 04:15 UTC (SEC EDGAR)", 10, "Discover insider signal (US stocks)",
    ),
    ExtractSpec(
        "ibes_estimates", "IBES analyst consensus", "ibes_estimates_pit", "statpers",
        _WRDS, 92, "Discover estimate revision (live Yahoo consensus is used when this is stale)",
    ),
    # JKP publishes once a year: the release of 2026-05-21 runs to 2025-12-31.
    # The next one (to 2026-12) is due around May 2027, so a December month
    # plus 18 months means a release was missed.
    ExtractSpec(
        "jkp_characteristics", "JKP stock characteristics", "wrds_factor_characteristics_pit", "eom",
        f"{_WRDS}; JKP publishes yearly, about five months after December", 548,
        "Pooled ML features, factor evidence (empty in live joins once the newest month is 92 days old)",
    ),
    # Compustat updates the security file daily, but a gvkey's ISIN rarely
    # changes and the join carries the newest mapping forward with no age
    # limit. An old export only misses securities listed after it, so it is
    # re-exported once a year, in the same WRDS session as the JKP pull.
    ExtractSpec(
        "security_identifiers", "Compustat security identifiers", "security_identifiers_pit", "as_of_date",
        f"{_WRDS}; re-export yearly with the JKP pull", 730,
        "ISIN to gvkey links for the WRDS joins (ISINs listed after the newest date are not linked)",
    ),
    ExtractSpec(
        "crsp_names", "CRSP ticker history", "crsp_names_pit", "ingested_at",
        _WRDS, 183, "Ticker to permno links for the WRDS joins",
    ),
    ExtractSpec(
        "ccm_link", "CRSP/Compustat link", "ccm_link_pit", "ingested_at",
        _WRDS, 183, "permno to gvkey links for the WRDS joins",
    ),
)


def research_extract_freshness(panel_dir: Path | None = None, *, today: date | None = None) -> list[dict[str, Any]]:
    """One entry per extract: newest date, rows, days old, and whether it is stale."""
    folder = panel_dir or get_panel_dir()
    clock = today or datetime.now(UTC).date()
    out: list[dict[str, Any]] = []
    for spec in EXTRACTS:
        try:
            newest, rows = panel_newest(folder, spec.subdir, spec.date_column)
            error = None
        except Exception as exc:  # noqa: BLE001 - one unreadable panel must not hide the others
            newest, rows, error = None, 0, str(exc)
        newest_date = newest.date() if newest is not None else None
        age = (clock - newest_date).days if newest_date is not None else None
        entry = {
            **{k: v for k, v in asdict(spec).items() if k not in ("subdir", "date_column")},
            "newest": newest_date.isoformat() if newest_date else None,
            "rows": rows,
            "age_days": age,
            "stale": age is None or age > spec.stale_after_days,
            "error": error,
        }
        out.append(entry)
    return out
