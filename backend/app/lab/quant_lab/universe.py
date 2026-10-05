"""Practical universe for the lab's first signal batch (ADR 0015 ruling #15).

The ADR names "STOXX Europe 600" as the target universe, but nothing in the
app actually ingests that broadly today -- ``bar_prices`` coverage is purely
demand-driven by the user's real DKB holdings
(``price_backfill.py::backfill_user_prices``). The only STOXX-600-shaped
list in the repo is ``app.decision.discover.universe::_STOXX600``, a
68-ticker multi-country European large-cap "representative selection" used
for discovery candidate scoring -- real, liquid, already-resolvable tickers,
just not the full 600 constituents.

Duplicated here (not imported from ``discover``) because ``discover`` is a
decision-loop package and ``quant_lab`` is foundation-tier -- importing it
would invert the required dependency direction. Keep in sync by hand if
``discover/universe.py``'s ``_STOXX600`` list changes; a future Datastream
PIT extract (deferred in ADR 0015 Phase 3 pending the owner's access) is the
eventual path to the ADR's full-breadth aspiration.
"""
from __future__ import annotations

LAB_UNIVERSE: list[str] = [
    # Core EU large caps
    "ASML.AS", "ADYEN.AS", "INGA.AS", "PHIA.AS", "PRX.AS", "WKL.AS",
    "MC.PA", "OR.PA", "KER.PA", "CAP.PA", "AI.PA", "DSY.PA", "GTT.PA",
    "SAN.PA", "BNP.PA", "ACA.PA", "GLE.PA", "RNO.PA", "SU.PA", "TTE.PA",
    "EL.PA", "VIV.PA", "TEP.PA", "ENEL.MI", "ENI.MI", "ISP.MI", "UCG.MI",
    "MB.MI", "TIT.MI", "MONC.MI", "REC.MI", "SRG.MI", "STMMI.MI",
    "IBE.MC", "ITX.MC", "REP.MC", "SAN.MC", "BBVA.MC", "CABK.MC",
    "NESN.SW", "RO.SW", "NOVN.SW", "ZURN.SW", "UBSG.SW", "ABBN.SW",
    "SCMN.SW", "LONN.SW", "GEBN.SW", "SGSN.SW",
    "EQNR.OL", "DNB.OL", "TEL.OL", "YAR.OL", "ORK.OL",
    "VOLV-B.ST", "ERIC-B.ST", "HM-B.ST", "SEB-A.ST", "SHB-A.ST",
    "CARL-B.CO", "DSV.CO", "NOVO-B.CO", "MAERSK-B.CO",
    "ELISA.HE", "FORTUM.HE", "KNEBV.HE", "NDA-FI.HE", "SAMPO.HE",
]
