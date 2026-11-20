"""Canonical schema for the CRSP/Compustat Merged (CCM) linking table.

Sibling of :mod:`security_identifiers_schema`. That module answers "which
ISIN(s) did this gvkey carry on date X" from Compustat's own identifier
history; this one answers a different question -- "which CRSP security
(permno/permco) *is* this Compustat company (gvkey), and during which
window" -- since CRSP and Compustat use entirely independent identifier
systems with no native shared key. A gvkey can span multiple permnos over
time (spinoffs, share-class changes) and a permno can switch gvkey after a
merger, so -- exactly like the identifiers table -- this keeps one row per
(``gvkey``, ``permno``) link *validity window* rather than a single static
mapping.

``linkenddt`` is deliberately nullable: WRDS's own convention is that a null
end date means the link is still active today, not that the row is
incomplete. Consumers must treat ``linkenddt.isna()`` as "open-ended", never
as a missing value to drop.
"""
from __future__ import annotations

from typing import cast

import pandas as pd

CCM_LINK_COLUMNS: tuple[str, ...] = (
    "gvkey",
    "permno",
    "permco",
    "linkdt",
    "linkenddt",
    "linktype",
    "linkprim",
    "source",
    "ingested_at",
)

CCM_LINK_DTYPES: dict[str, str] = {
    "gvkey": "string",
    "permno": "Int64",
    "permco": "Int64",
    "linkdt": "datetime64[ns]",
    "linkenddt": "datetime64[ns]",
    "linktype": "string",
    "linkprim": "string",
    "source": "string",
    "ingested_at": "datetime64[ns]",
}

CCM_LINK_SOURCES: tuple[str, ...] = ("crsp_compustat_ccm",)

# WRDS's own link-quality/primacy codes (ccmxpf_lnkhist). Kept as an explicit
# allow-list -- rather than accepting anything -- because a code outside this
# set almost always means the extract came from the deprecated
# ccmxpf_linktable view or a hand-edited file, not the current lnkhist table.
CCM_LINKTYPE_VALUES: tuple[str, ...] = (
    "LC",  # research complete -- highest quality
    "LU",  # unresearched but valid
    "LS",  # valid for this permno only
    "LD",  # duplicate -- avoid double counting
    "LX",  # inactive
    "LN",  # no prices in Compustat
    "LO",
    "NP",
    "NR",
    "NU",
)

CCM_LINKPRIM_VALUES: tuple[str, ...] = ("P", "C", "J", "N")

# The standard "primary, research-quality" filter every WRDS/CCM guide
# recommends before joining: linktype in (LC, LU) and linkprim in (P, C).
# Exposed here so read_ccm_link's as_of filtering and any future join helper
# apply the identical recipe rather than each re-deriving it.
CCM_RESEARCH_QUALITY_LINKTYPES: tuple[str, ...] = ("LC", "LU")
CCM_RESEARCH_QUALITY_LINKPRIMS: tuple[str, ...] = ("P", "C")


class CcmLinkSchemaError(ValueError):
    """Raised when a frame cannot be coerced to the CCM link schema."""


def validate_ccm_link_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return *df* in canonical column order with enforced dtypes.

    Same fail-loud contract as ``validate_security_identifiers_frame``. Only
    ``linkdt`` is required non-null -- ``linkenddt`` null is the documented
    "still active" convention, not a data-quality failure.
    """
    missing = [c for c in CCM_LINK_COLUMNS if c not in df.columns]
    if missing:
        raise CcmLinkSchemaError(f"CCM link frame missing column(s): {missing}")

    out = cast(pd.DataFrame, df[list(CCM_LINK_COLUMNS)]).copy()

    unknown = sorted(set(out["source"].dropna().unique()) - set(CCM_LINK_SOURCES))
    if unknown:
        raise CcmLinkSchemaError(f"unknown source value(s): {unknown}; expected one of {CCM_LINK_SOURCES}")

    unknown_linktype = sorted(set(out["linktype"].dropna().unique()) - set(CCM_LINKTYPE_VALUES))
    if unknown_linktype:
        raise CcmLinkSchemaError(
            f"unknown linktype value(s): {unknown_linktype}; expected one of {CCM_LINKTYPE_VALUES}"
        )

    unknown_linkprim = sorted(set(out["linkprim"].dropna().unique()) - set(CCM_LINKPRIM_VALUES))
    if unknown_linkprim:
        raise CcmLinkSchemaError(
            f"unknown linkprim value(s): {unknown_linkprim}; expected one of {CCM_LINKPRIM_VALUES}"
        )

    try:
        for col, dtype in CCM_LINK_DTYPES.items():
            out[col] = out[col].astype(dtype)
    except (TypeError, ValueError) as exc:
        raise CcmLinkSchemaError(f"CCM link frame failed dtype coercion: {exc}") from exc

    # linkdt anchors every point-in-time join in this table; linkenddt is
    # allowed to be null (see module docstring) so it is deliberately not
    # checked here.
    if bool(out["linkdt"].isna().any()):
        raise CcmLinkSchemaError("one or more rows have a null linkdt")

    if bool((out["linkenddt"] < out["linkdt"]).fillna(False).any()):
        raise CcmLinkSchemaError("one or more rows have linkenddt before linkdt")

    return out
