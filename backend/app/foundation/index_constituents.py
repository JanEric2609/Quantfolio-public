"""Index constituent samples shared by Discover's universe and AlphaCrafter's miner.

Yahoo symbols, validated on 2026-09-28 (every symbol resolved then). Plain
data with no imports, so any layer can read it.
"""
from __future__ import annotations

# Constituents as of the September 2025 review (STOXX; cross-checked against
# marketscreener, 2026-09-28): 17 DE, 15 FR, 8 NL, 4 IT, 4 ES, 1 BE, 1 FI.
# The previous list had 43 entries and carried Intel, CRH (primary listing
# moved to New York in 2023) and two Yahoo symbols that do not exist
# (ING.AS, SCHN.PA).
EUROSTOXX50 = [
    "ABI.BR", "AD.AS", "ADS.DE", "ADYEN.AS", "AI.PA", "AIR.PA", "ALV.DE",
    "ARGX.BR", "ASML.AS", "BAS.DE", "BAYN.DE", "BBVA.MC", "BMW.DE", "BN.PA",
    "BNP.PA", "CS.PA", "DB1.DE", "DBK.DE", "DG.PA", "DHL.DE", "DTE.DE",
    "EL.PA", "ENEL.MI", "ENI.MI", "ENR.DE", "IBE.MC", "IFX.DE", "INGA.AS",
    "ISP.MI", "ITX.MC", "MBG.DE", "MC.PA", "MUV2.DE", "NDA-FI.HE", "OR.PA",
    "PRX.AS", "RACE.MI", "RHM.DE", "RMS.PA", "SAF.PA", "SAN.MC", "SAN.PA",
    "SAP.DE", "SGO.PA", "SIE.DE", "SU.PA", "TTE.PA", "UCG.MI", "VOW3.DE",
    "WKL.AS",
]

# The S&P 100 (OEX) as of 2026-09-21 (Wikipedia's component list, every
# symbol checked on Yahoo 2026-09-28), one share class per company (GOOGL,
# not GOOG): the "US top 100" of the AlphaCrafter miner's cross-section.
# Replaced a stale sample that still held AIG, CHTR, CL, DOW, F, KHC, MET,
# NKE, PYPL, SPG and TGT and lacked AMAT, ANET, DE, DELL, GEV, ISRG, LRCX,
# MU, NOW, PANW, PLTR, SNDK and UBER.
SP100 = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMAT", "AMD", "AMGN", "AMT",
    "AMZN", "ANET", "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BNY",
    "BRK-B", "C", "CAT", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO",
    "CVS", "CVX", "DE", "DELL", "DHR", "DIS", "DUK", "EMR", "FDX", "GD",
    "GE", "GEV", "GILD", "GM", "GOOGL", "GS", "HD", "IBM", "INTC", "INTU",
    "ISRG", "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA",
    "MCD", "MDLZ", "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "MU",
    "NEE", "NFLX", "NOW", "NVDA", "ORCL", "PANW", "PEP", "PFE", "PG",
    "PLTR", "PM", "QCOM", "RTX", "SBUX", "SCHW", "SNDK", "SO", "T", "TMO",
    "TMUS", "TSLA", "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V", "VZ",
    "WFC", "WMT", "XOM",
]
