"""Loader for a manually-exported WRDS CRSP/Compustat Merged (CCM) link history.

Companion to :mod:`security_identifiers_loader` and :mod:`fundamentals_loader`.
Compustat's ``gvkey`` and CRSP's ``permno``/``permco`` are independent
identifier systems with no shared native key; WRDS's ``ccmxpf_lnkhist`` table
is the standard, curated historical bridge between them, and this loader
ingests exactly that table (never the deprecated ``ccmxpf_linktable`` view).

**Why a link *history*, not a static map.** A gvkey can carry more than one
permno over time (spinoffs, share-class restructuring) and a permno can
switch gvkey after a merger. Every row therefore keeps ``linkdt``/
``linkenddt`` -- the window during which that (gvkey, permno) pairing was
actually valid -- exactly the same point-in-time discipline
``security_identifiers_loader`` applies to gvkey<->ISIN. A null ``linkenddt``
means the link is still active, not that the row is incomplete.

**The standard research-quality filter.** WRDS's own guidance (and every
academic recipe that uses this table) is to keep only
``linktype in ('LC', 'LU')`` and ``linkprim in ('P', 'C')`` before joining --
anything else is either unresearched noise, a secondary/duplicate link, or a
non-standard entry. This loader does not apply that filter itself (a caller
inspecting the raw link history is a legitimate use), but
:func:`read_ccm_link` exposes ``research_quality_only`` to apply it on read,
and :mod:`ccm_link_schema` exports the exact code lists
(``CCM_RESEARCH_QUALITY_LINKTYPES``/``CCM_RESEARCH_QUALITY_LINKPRIMS``) so any
future join helper uses the identical recipe rather than re-deriving it.

**Not yet exercised against real data.** This module ships ready for whenever
a WRDS CCM extract is available; see ``CONTEXT.md``'s Owner-wave notes.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.ccm_link_schema import (
    CCM_LINK_COLUMNS,
    CCM_RESEARCH_QUALITY_LINKPRIMS,
    CCM_RESEARCH_QUALITY_LINKTYPES,
    CcmLinkSchemaError,
    validate_ccm_link_frame,
)
from app.foundation.data_engineering._parquet_combine import read_parquet_partitions
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

# Candidate source headers per target column, highest precedence first.
# WRDS's ccmxpf_lnkhist query tool exports lowercase mnemonics by default;
# the "l"-prefixed spellings (lpermno/lpermco) are the historical SAS names
# still seen in older extracts and some WRDS macro output.
_TARGET_SOURCES: dict[str, tuple[str, ...]] = {
    "gvkey": ("gvkey",),
    "permno": ("permno", "lpermno"),
    "permco": ("permco", "lpermco"),
    "linkdt": ("linkdt",),
    "linkenddt": ("linkenddt",),
    "linktype": ("linktype",),
    "linkprim": ("linkprim",),
}

_REQUIRED_TARGET_COLUMNS = ("gvkey", "permno", "linkdt", "linktype", "linkprim")


@dataclass
class CcmLinkLoadResult:
    ok: bool
    rows_loaded: int = 0
    gvkeys: list[str] = field(default_factory=list)
    error: str | None = None
    duplicate_rows_dropped: int = 0


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    by_key: dict[str, object] = {}
    for col in df.columns:
        by_key.setdefault(str(col).strip().lower(), col)

    rename: dict[object, str] = {}
    claimed: set[object] = set()
    for target, candidates in _TARGET_SOURCES.items():
        for candidate in candidates:
            col = by_key.get(candidate)
            if col is not None and col not in claimed:
                rename[col] = target
                claimed.add(col)
                break
    return df.rename(columns=rename)


def load_ccm_link_extract(file_path: Path | str, dry_run: bool = True) -> CcmLinkLoadResult:
    """Parse, validate and write a WRDS ``ccmxpf_lnkhist`` CSV export.

    Args:
        file_path: the exported CSV.
        dry_run: when True (default) validate and report, writing nothing.

    Returns a :class:`CcmLinkLoadResult`; a malformed extract comes back as
    ``ok=False`` with an explanatory ``error`` rather than a traceback.
    """
    path = Path(file_path)
    try:
        raw = pd.read_csv(path, low_memory=False)
    except Exception as exc:
        return CcmLinkLoadResult(ok=False, error=f"could not read {path}: {exc}")

    normalized = _normalize_columns(raw)
    missing = [c for c in _REQUIRED_TARGET_COLUMNS if c not in normalized.columns]
    if missing:
        return CcmLinkLoadResult(
            ok=False, error=f"extract missing required column(s) after alias mapping: {missing}"
        )

    linkdt = cast(pd.Series, pd.to_datetime(normalized["linkdt"], errors="coerce"))
    if bool(linkdt.isna().any()):
        return CcmLinkLoadResult(
            ok=False, error="one or more rows have an unparseable linkdt value; re-download with YYYY-MM-DD dates"
        )
    # linkenddt is genuinely allowed to be blank (still-active link), so a
    # blank string must not be forced through to_datetime as an error --
    # only a present-but-unparseable value should fail the load. WRDS's own
    # CCM export also uses the literal sentinel "E" for "still active" in
    # place of a blank cell (seen in real extracts even with the query
    # form's date format set to YYYY-MM-DD) -- treat it the same as blank.
    linkenddt_raw = normalized["linkenddt"] if "linkenddt" in normalized.columns else pd.Series([None] * len(normalized))
    linkenddt_stripped = linkenddt_raw.astype(str).str.strip()
    is_active_sentinel = linkenddt_stripped.str.upper() == "E"
    linkenddt_raw = linkenddt_raw.mask(is_active_sentinel, None)
    linkenddt_stripped = linkenddt_stripped.mask(is_active_sentinel, "")
    linkenddt = cast(pd.Series, pd.to_datetime(linkenddt_raw, errors="coerce"))
    unparseable_end = linkenddt_raw.notna() & (linkenddt_stripped != "") & linkenddt.isna()
    if bool(unparseable_end.any()):
        return CcmLinkLoadResult(
            ok=False, error="one or more rows have an unparseable linkenddt value; leave blank for an active link"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    frame = pd.DataFrame({
        "gvkey": normalized["gvkey"].astype(str).str.strip().str.zfill(6),
        "permno": pd.to_numeric(normalized["permno"], errors="coerce"),
        "permco": (
            pd.to_numeric(normalized["permco"], errors="coerce")
            if "permco" in normalized.columns
            else float("nan")
        ),
        "linkdt": linkdt,
        "linkenddt": linkenddt,
        "linktype": normalized["linktype"].astype(str).str.strip().str.upper(),
        "linkprim": normalized["linkprim"].astype(str).str.strip().str.upper(),
        "source": "crsp_compustat_ccm",
        "ingested_at": now,
    })

    before = len(frame)
    frame = frame.drop_duplicates(subset=["gvkey", "permno", "linkdt", "linktype", "linkprim"])
    duplicate_rows_dropped = before - len(frame)

    try:
        validated = validate_ccm_link_frame(frame)
    except CcmLinkSchemaError as exc:
        return CcmLinkLoadResult(ok=False, error=str(exc))

    gvkeys = sorted(cast(list, validated["gvkey"].unique().tolist()))
    if not dry_run:
        out_dir = get_panel_dir() / "ccm_link_pit"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in path.stem) or "extract"
        validated.to_parquet(out_dir / f"{safe_stem}.parquet", index=False)

    return CcmLinkLoadResult(
        ok=True,
        rows_loaded=len(validated),
        gvkeys=gvkeys,
        duplicate_rows_dropped=duplicate_rows_dropped,
    )


def read_ccm_link(
    as_of: pd.Timestamp | str | None = None,
    research_quality_only: bool = False,
) -> pd.DataFrame:
    """Read every loaded CCM link partition back as one frame.

    Args:
        as_of: when given, keep only links whose window
            [``linkdt``, ``linkenddt``] covers this date -- a null
            ``linkenddt`` is treated as open-ended (still active). Leaving it
            ``None`` returns the full link history.
        research_quality_only: when True, apply the standard
            ``linktype in (LC, LU)`` / ``linkprim in (P, C)`` filter every
            WRDS/CCM recipe recommends before joining. Off by default so a
            caller inspecting the raw history sees everything WRDS shipped.
    """
    out_dir = get_panel_dir() / "ccm_link_pit"
    paths = sorted(out_dir.glob("*.parquet")) if out_dir.exists() else []
    if not paths:
        return pd.DataFrame(columns=pd.Index(CCM_LINK_COLUMNS))

    combined = read_parquet_partitions(paths)
    combined = combined.sort_values("ingested_at")
    combined = cast(
        pd.DataFrame,
        combined.drop_duplicates(subset=["gvkey", "permno", "linkdt", "linktype", "linkprim"], keep="last"),
    )

    if research_quality_only:
        combined = cast(
            pd.DataFrame,
            combined[
                combined["linktype"].isin(CCM_RESEARCH_QUALITY_LINKTYPES)
                & combined["linkprim"].isin(CCM_RESEARCH_QUALITY_LINKPRIMS)
            ],
        )

    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        active = (combined["linkdt"] <= cutoff) & (combined["linkenddt"].isna() | (combined["linkenddt"] >= cutoff))
        combined = cast(pd.DataFrame, combined[active])

    return combined.sort_values(["gvkey", "linkdt"]).reset_index(drop=True)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a WRDS CRSP/Compustat Merged (ccmxpf_lnkhist) CSV into the PIT Parquet panel."
    )
    parser.add_argument("csv_path", help="path to the exported CSV")
    parser.add_argument("--apply", action="store_true", help="write the panel (default: dry-run, validate only)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = load_ccm_link_extract(args.csv_path, dry_run=not args.apply)

    if not result.ok:
        logger.error("FAILED: %s", result.error)
        raise SystemExit(1)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    suffix = "" if args.apply else " (pass --apply to write)"
    logger.info("%s: %d rows, %d companies%s", mode, result.rows_loaded, len(result.gvkeys), suffix)
    if result.duplicate_rows_dropped:
        logger.info("  exact-duplicate rows collapsed: %d", result.duplicate_rows_dropped)


if __name__ == "__main__":
    _main()
