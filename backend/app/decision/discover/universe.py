"""Discover universe builder: deterministic screens + news/sentiment candidates.

Combines static index constituents, EU/US mid-cap niche seeds, liquid UCITS
ETFs, and recent positive-news tickers into a deduplicated candidate list.
Size cap and fundamentals floors are config-driven via the active "universe"
DiscoveryConfig (defaults: 200 candidates, market cap > 250M EUR,
volume > 100k/day).
"""
from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.index_constituents import EUROSTOXX50, SP100

from app.decision.discover.listing import listing_currency
from app.foundation.live_positions import live_positions
from app.foundation.models.entities import Fundamental, Holding, NewsItem, Portfolio
from app.foundation.etf_universe import EtfUniverseProvider
from app.foundation.settings import get_public_settings
from app.foundation.text_safety import ascii_safe

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce value to float, returning default for None/NaN/inf/invalid."""
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result

# Shared provider instance (file-cached, long-lived)
_etf_provider: EtfUniverseProvider | None = None


def _get_etf_provider() -> EtfUniverseProvider:
    global _etf_provider
    if _etf_provider is None:
        _etf_provider = EtfUniverseProvider()
    return _etf_provider

# ---------------------------------------------------------------------------
# Static index constituents (ticker-only; ISIN resolution deferred)
# ---------------------------------------------------------------------------

# Index samples for the Discover universe. Every symbol resolved on Yahoo on
# 2026-09-28; 53 that no longer did were removed or renamed then (delisted,
# acquired, moved to New York, or never a Yahoo symbol: BF.B, STM.MI,
# BOUY.PA, ROG.SW...). A dead symbol costs a provider round trip per run and
# silently shrinks the pool.
_DAX40 = [
    "ADS.DE", "AIR.DE", "ALV.DE", "BAS.DE", "BAYN.DE", "BEI.DE", "BMW.DE",
    "BNR.DE", "CBK.DE", "CON.DE", "DTG.DE", "DBK.DE", "DB1.DE", "DHL.DE",
    "DTE.DE", "EOAN.DE", "FRE.DE", "HNR1.DE", "HEI.DE", "HEN3.DE", "IFX.DE",
    "MBG.DE", "MRK.DE", "MTX.DE", "MUV2.DE", "P911.DE", "PAH3.DE", "QIA.DE",
    "RHM.DE", "RWE.DE", "SAP.DE", "SRT3.DE", "SIE.DE", "ENR.DE", "SHL.DE",
    "SY1.DE", "VOW3.DE", "ZAL.DE", "HFG.DE",
]

_EUROSTOXX50 = EUROSTOXX50

_SP100 = SP100

_NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD",
    "AMGN", "AMZN", "APP", "ASML", "AVGO", "AXON", "AZN", "BIIB", "BKR",
    "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST", "CPRT", "CRWD",
    "CSCO", "CSGP", "CSX", "CTAS", "CTSH", "DASH", "DDOG", "DXCM", "EXC",
    "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD", "GOOGL", "GOOG", "HON",
    "IDXX", "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX",
    "LULU", "MAR", "MCHP", "MDB", "MDLZ", "MELI", "META", "MNST", "MRVL",
    "MSFT", "MU", "NFLX", "NVDA", "NXPI", "ODFL", "ON", "ORLY", "PANW",
    "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM", "REGN", "ROP",
    "ROST", "SBUX", "SNPS", "TEAM", "TMUS", "TSLA", "TTD", "TTWO", "TXN",
    "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

# Niche / small- & mid-cap seed pool (advisor-loop PR1): MDAX/SDAX names plus
# liquid US mid-caps. Deterministic ticker list; the fundamentals screen
# (config-driven floors) still decides who survives, so lowering
# ``min_market_cap_eur`` in the universe config is what actually admits these.
_EU_MIDCAP = [
    "AIXA.DE", "AT1.DE", "BC8.DE", "BOSS.DE", "COK.DE", "DUE.DE", "EVD.DE",
    "EVK.DE", "FIE.DE", "FNTN.DE", "FPE3.DE", "G1A.DE", "GXI.DE", "HLE.DE",
    "HOT.DE", "JEN.DE", "KGX.DE", "KRN.DE", "LEG.DE", "LXS.DE", "NDA.DE",
    "NEM.DE", "PBB.DE", "PUM.DE", "RAA.DE", "RHK.DE", "SAX.DE", "SDF.DE",
    "SIX2.DE", "STM.DE", "SZG.DE", "SZU.DE", "TEG.DE", "TLX.DE", "WAF.DE",
    "WCH.DE",
]

_US_MIDCAP = [
    "ALGM", "AMBA", "APPF", "BJ", "BMI", "BOX", "BRZE", "CELH", "CROX",
    "CVLT", "DOCN", "DUOL", "ENSG", "EXLS", "FN", "FOUR", "HALO", "INSP",
    "ITRI", "KNSL", "LNTH", "LSCC", "MEDP", "MTDR", "NOVT", "ONTO", "PCTY",
    "PLAB", "POWI", "RMBS", "SAIA", "SITM", "SMPL", "SPSC", "TENB", "TMDX",
    "WING", "WTS", "ZWS",
]

# STOXX Europe 600 (broad EU large/mid caps, EUR/GBP/CHF/SEK/DKK/NOk listings)
# Representative selection covering major sectors not fully captured by
# DAX40/EuroStoxx50/EU_MIDCAP. Tickers use local-exchange suffixes.
_STOXX600 = [
    # Core EU large caps
    "ASML.AS", "ADYEN.AS", "INGA.AS", "PHIA.AS", "PRX.AS", "WKL.AS",
    "MC.PA", "OR.PA", "KER.PA", "CAP.PA", "AI.PA", "DSY.PA", "GTT.PA",
    "SAN.PA", "BNP.PA", "ACA.PA", "GLE.PA", "RNO.PA", "SU.PA", "TTE.PA",
    "EL.PA", "VIV.PA", "TEP.PA", "ENEL.MI", "ENI.MI", "ISP.MI", "UCG.MI",
    "MB.MI", "TIT.MI", "MONC.MI", "REC.MI", "SRG.MI", "STMMI.MI", "IBE.MC",
    "ITX.MC", "REP.MC", "SAN.MC", "BBVA.MC", "CABK.MC", "NESN.SW", "RO.SW",
    "NOVN.SW", "ZURN.SW", "UBSG.SW", "ABBN.SW", "SCMN.SW", "LONN.SW",
    "GEBN.SW", "SGSN.SW", "EQNR.OL", "DNB.OL", "TEL.OL", "YAR.OL", "ORK.OL",
    "VOLV-B.ST", "ERIC-B.ST", "HM-B.ST", "SEB-A.ST", "SHB-A.ST",
    "CARL-B.CO", "DSV.CO", "NOVO-B.CO", "MAERSK-B.CO", "ELISA.HE",
    "FORTUM.HE", "KNEBV.HE", "NDA-FI.HE", "SAMPO.HE",
]

# FTSE 100 (UK large caps, LSE .L suffix)
_FTSE100 = [
    "AAL.L", "ABF.L", "ADM.L", "ANTO.L", "AZN.L", "BA.L", "BARC.L",
    "BATS.L", "BTRW.L", "BLND.L", "BNZL.L", "BP.L", "BRBY.L", "BT-A.L",
    "BKG.L", "CCH.L", "CPG.L", "DCC.L", "DGE.L", "ENT.L", "EXPN.L",
    "FRES.L", "GLEN.L", "GSK.L", "HBR.L", "HIK.L", "HSBA.L", "IAG.L",
    "IHG.L", "IMB.L", "INF.L", "ITRK.L", "JD.L", "JMAT.L", "KGF.L",
    "LAND.L", "LGEN.L", "LLOY.L", "LSEG.L", "MNG.L", "MNDI.L", "NG.L",
    "NXT.L", "OCDO.L", "PRU.L", "PSN.L", "PSON.L", "RKT.L", "REL.L",
    "RIO.L", "RR.L", "SGE.L", "SHEL.L", "SMIN.L", "SPX.L", "SSE.L",
    "STAN.L", "STJ.L", "SVT.L", "TSCO.L", "TW.L", "UU.L", "VOD.L", "WTB.L",
    "WPP.L",
]

# CAC 40 (France large caps, Euronext Paris .PA suffix)
_CAC40 = [
    "AC.PA", "AIR.PA", "AI.PA", "ATO.PA", "BN.PA", "EN.PA", "CAP.PA",
    "CA.PA", "CO.PA", "CS.PA", "EL.PA", "ENGI.PA", "ERA.PA", "FGR.PA",
    "GLE.PA", "HO.PA", "KER.PA", "LR.PA", "MC.PA", "ML.PA", "OR.PA",
    "ORA.PA", "PUB.PA", "RMS.PA", "RI.PA", "RNO.PA", "SAF.PA", "SGO.PA",
    "SU.PA", "TEP.PA", "TTE.PA", "VIE.PA", "VIV.PA", "WLN.PA", "DG.PA",
    "SAN.PA", "BNP.PA", "ACA.PA",
]

# S&P 500 (US broad large caps — extends SP100/Nasdaq100)
# Representative selection of major names not already in SP100/Nasdaq100
_SP500 = [
    "ADP", "ALL", "AMCR", "ANET", "APD", "APH", "APTV", "ARE", "ATO",
    "VMRK", "AVY", "AWK", "AXON", "AZO", "BAX", "BDX", "BEN", "BF-B", "BIO",
    "BNY", "BKR", "BLDR", "BRO", "BR", "BSX", "BWA", "BXP", "CAG", "CAH",
    "CARR", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CE", "CEG", "CF", "CFG",
    "CHD", "CHRW", "CHTR", "CI", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG",
    "CMI", "CMS", "CNC", "CNMD", "CNP", "COF", "COO", "COP", "COR", "CPAY",
    "CPB", "CPRT", "CPT", "CRL", "CRM", "CSCO", "CSGP", "CSX", "CTAS",
    "CTSH", "CVS", "D", "DAL", "DD", "DE", "DECK", "DG", "DGX", "DHI",
    "DHR", "DIS", "DLR", "DOC", "DOV", "DPZ", "DRI", "DTE", "DUK", "DVA",
    "DVN", "DXC", "EBAY", "ECL", "ED", "EFX", "EG", "EIX", "EL", "ELV",
    "EMN", "EMR", "ENPH", "EPAM", "EQIX", "ES", "ESS", "ETN", "ETR", "ETSY",
    "EVRG", "EW", "EXC", "EXPD", "EXPE", "EXR", "FANG", "FAST", "FCX",
    "FDS", "FDX", "FE", "FFIV", "FISV", "FIS", "FITB", "FLS", "FMC", "FOX",
    "FOXA", "FRT", "FSLR", "FTNT", "FTV", "GD", "GE", "GEHC", "GEN", "GILD",
    "GIS", "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GAP",
    "GRMN", "GS", "GWW", "HAL", "HAS", "HBAN", "HCA", "HD", "HIG", "HII",
    "HLT", "HON", "HPE", "HPQ", "HRL", "HST", "HSIC", "HSY", "HUBB", "HUM",
    "HWM", "IBM", "ICE", "IDXX", "IEX", "IFF", "ILMN", "INCY", "INFO",
    "INTC", "INTU", "INVH", "IP", "IQV", "IR", "IRM", "ISRG", "IT", "ITW",
    "IVZ", "J", "JBHT", "JCI", "JKHY", "JNJ", "JPM", "KDP", "KEY", "KEYS",
    "KHC", "KIM", "KLAC", "KMB", "KMI", "KMX", "KO", "KR", "L", "LDOS",
    "LEN", "LH", "LHX", "LIN", "LKQ", "LLY", "LMT", "LNC", "LNT", "LOW",
    "LRCX", "LULU", "LUV", "LYB", "LYV", "MAR", "MAS", "MCD", "MCHP", "MCK",
    "MDLZ", "MDT", "MET", "META", "MGM", "MHK", "MKC", "MLM", "MRSH", "MMM",
    "MNST", "MO", "MOS", "MPC", "MPWR", "MRK", "MRNA", "MS", "MSCI", "MSFT",
    "MSI", "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM", "NFLX",
    "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA",
    "NVR", "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON", "ORCL",
    "ORLY", "OTIS", "OXY", "PANW", "PARA", "PAYC", "PAYX", "PCAR", "PCG",
    "PEG", "PEP", "PFE", "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD",
    "PM", "PNC", "PNR", "PNW", "PODD", "POOL", "PPG", "PPL", "PRU", "PSA",
    "PSX", "PTON", "PVH", "PWR", "PYPL", "QCOM", "QRVO", "RCL", "REG",
    "REGN", "RF", "RHI", "RJF", "RL", "RMD", "ROK", "ROL", "ROP", "ROST",
    "RSG", "RTX", "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB", "SLG",
    "SMCI", "SNA", "SNPS", "SO", "SOLV", "SPG", "SPGI", "SRE", "STE",
    "STLD", "STT", "STX", "STZ", "SWK", "SWKS", "SYF", "SYK", "SYY", "T",
    "TAP", "TGT", "TDG", "TDY", "TECH", "TEL", "TER", "TEVA", "TFC", "TFX",
    "TJX", "TMO", "TMUS", "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSLA",
    "TSN", "TT", "TTWO", "TXN", "TXT", "TYL", "UAL", "UDR", "UHS", "ULTA",
    "UNH", "UNP", "UPS", "URI", "USB", "V", "VFC", "VLO", "VMC", "VRSK",
    "VRSN", "VRTX", "VTR", "VTRS", "VZ", "WAB", "WAT", "WDC", "WEC", "WELL",
    "WFC", "WHR", "WM", "WMB", "WMT", "WRB", "WST", "WY", "WYNN", "XEL",
    "XOM", "XRAY", "XYL", "YUM", "ZBH", "ZBRA", "ZTS",
]

# Nikkei 225 (Japan large caps, TSE .T suffix) — representative core
_NIKKEI225 = [
    "6758.T", "9984.T", "7203.T", "6861.T", "8035.T", "9432.T", "6501.T",
    "4063.T", "8306.T", "8316.T", "8058.T", "7267.T", "6902.T", "6954.T",
    "8001.T", "8002.T", "8031.T", "8802.T", "9020.T", "9022.T", "9101.T",
    "9104.T", "9107.T", "9433.T", "9434.T", "9983.T", "9989.T",
]

# Curated fixed-income sleeve (F17): EtfUniverseProvider's justETF scrape can
# only resolve tickers on Xetra (_to_xetra_symbol always appends ".DE"), so
# an ETF with no Xetra listing — like this one, LSE/Amsterdam-only — can
# never surface through the dynamic ETF universe no matter what the
# fundamentals-floor config allows. Seeded explicitly and always included
# (not subject to that floor) since it's a curated, known-good instrument
# rather than a scanned pool. Gives the moderate-growth policy-band
# architecture an actual short-duration bond sleeve to allocate to — the
# book previously held no fixed-income instrument at all.
_FIXED_INCOME_SEED: list[dict[str, str]] = [
    {
        "symbol": "CBE3.L",
        "isin": "IE00B3VTMJ91",
        "name": "iShares € Govt Bond 1-3yr UCITS ETF (Acc)",
        "domicile_country": "IE",
        "distribution_policy": "Accumulating",
        # Matches _derive_tf_class's bond-keyword outcome — a government
        # bond fund gets the same non-"aktien" German tax treatment.
        "tf_class": "other",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_holdings_tickers(db: Session, user_id: str) -> set[str]:
    """Return tickers and ISINs currently held by user (manual + every synced broker)."""
    tickers: set[str] = set()
    portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
    if portfolio:
        for h in db.query(Holding).filter(Holding.portfolio_id == portfolio.id).all():
            if h.ticker:
                tickers.add(h.ticker.upper())
            if h.isin:
                tickers.add(h.isin.upper())
    # Held-back (unreconciled) positions are still held: never suggest them.
    for pos in live_positions(db, user_id, include_unreconciled=True):
        if pos.ticker:
            tickers.add(pos.ticker.upper())
        if pos.isin:
            tickers.add(pos.isin.upper())
    return tickers


def _profile_exclusions(db: Session, user_id: str) -> list[str]:
    """Return comma-separated exclusion keywords from investor profile."""
    settings = get_public_settings(db)
    raw = settings.get("investor_exclusions", "")
    if not raw:
        return []
    return [k.strip().lower() for k in str(raw).split(",") if k.strip()]


def _yf_fundamentals_filter(ticker: str) -> dict[str, Any]:
    """Lightweight yfinance fundamentals probe. Returns {} on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return {
            "market_cap": info.get("marketCap") or 0,
            "volume": info.get("averageVolume") or info.get("volume") or 0,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "isin": info.get("isin"),
            "currency": info.get("currency"),
        }
    except Exception:
        logger.debug("yfinance fundamentals failed for %s", ticker, exc_info=True)
        return {}


def _search_symbol_fallback(symbol: str) -> dict[str, Any] | None:
    """Fallback symbol resolution when registry.search_symbol is unavailable."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        if info and info.get("symbol"):
            return {
                "symbol": info["symbol"].upper(),
                "name": info.get("longName") or info.get("shortName") or symbol,
                "isin": info.get("isin"),
            }
    except Exception:
        logger.debug("yfinance search fallback failed for %s", symbol, exc_info=True)
    return None


def _registry_fundamentals_filter(
    ticker: str,
    registry: Any | None,
) -> dict[str, Any]:
    """Use ProviderRegistry to get fundamentals (market cap, volume).

    Falls back to yfinance if registry is None or fails.
    Registry fundamentals don't include longName, so name is the ticker.
    """
    if registry is not None:
        try:
            result = registry.get_fundamentals(ticker)
            if result and result.get("ok"):
                data = result["data"]
                mcap = (data or {}).get("market_cap")
                if mcap:
                    return {
                        "market_cap": mcap,
                        "volume": data.get("average_volume") or data.get("volume") or 0,
                        "name": data.get("name"),
                        "isin": data.get("isin"),
                        "currency": data.get("currency"),
                    }
        except Exception:
            logger.debug("Registry fundamentals failed for %s", ticker, exc_info=True)
    return _yf_fundamentals_filter(ticker)


# ---------------------------------------------------------------------------
# DB-cache-aware fundamentals helpers for parallel universe build
# ---------------------------------------------------------------------------

_FUNDAMENTALS_TTL = timedelta(days=7)


def _is_fresh_fundamentals(fetched_at: datetime) -> bool:
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return fetched_at >= datetime.now(UTC) - _FUNDAMENTALS_TTL


def _batch_load_fundamental_cache(db: Session, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Load all cached Fundamental rows for the given tickers in one query.

    Returns a mapping ticker -> fundamentals dict (market_cap, volume, name).
    Only includes entries that are still fresh (< 7 days old).
    """
    if not tickers:
        return {}
    rows = db.query(Fundamental).filter(Fundamental.ticker.in_(tickers)).all()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _is_fresh_fundamentals(row.fetched_at):
            continue
        try:
            data = json.loads(row.data_json or "{}")
        except Exception:
            continue
        mcap = _safe_float(data.get("market_cap"), 0.0)
        vol = _safe_float(data.get("average_volume") or data.get("volume"), 0.0)
        name = data.get("name") or data.get("long_name") or data.get("short_name")
        result[row.ticker] = {
            "market_cap": mcap, "volume": vol, "name": name, "isin": data.get("isin"),
            "currency": data.get("currency"),
        }
    return result


def _fetch_live_fundamentals_no_db(
    ticker: str,
    registry: Any | None,
) -> tuple[str, dict[str, Any]]:
    """Fetch fundamentals from provider (no DB access).

    Used in ThreadPoolExecutor workers — safe because it does NOT touch a Session.
    Returns (ticker, fund_dict).
    """
    fund = _registry_fundamentals_filter(ticker, registry)
    return ticker, fund


def _persist_fundamentals_batch(
    db: Session,
    live_data: dict[str, dict[str, Any]],
) -> None:
    """Persist freshly fetched fundamentals into the Fundamental table.

    Called on the main thread after workers return, so single session is fine.
    """
    for ticker, fund in live_data.items():
        if not fund:
            continue
        payload = {
            "market_cap": fund.get("market_cap"),
            "volume": fund.get("volume"),
            "name": fund.get("name"),
            "isin": fund.get("isin"),
            "currency": fund.get("currency"),
        }
        row = db.query(Fundamental).filter(Fundamental.ticker == ticker).one_or_none()
        if row is None:
            row = Fundamental(ticker=ticker)
            db.add(row)
        row.data_json = json.dumps(payload, default=str)
        row.source = "universe_screen"
        row.fetched_at = datetime.now(UTC)
        row.stale = False
    try:
        db.commit()
    except Exception:
        logger.debug("Failed to persist fundamentals batch", exc_info=True)
        db.rollback()


def _eur_per_unit(db: Session, currency: str, cache: dict[str, float | None]) -> float | None:
    """EUR value of one unit of *currency* from cached ``USD<CCY>=X`` closes."""
    if currency == "EUR":
        return 1.0
    if currency in cache:
        return cache[currency]
    from app.foundation.market import history, refresh_stale_bars

    def _usd_to(ccy: str) -> float | None:
        if ccy == "USD":
            return 1.0
        pair = f"USD{ccy}=X"
        try:
            # Stored series are caught up; a pair never stored before falls
            # through to history()'s own live fetch.
            refresh_stale_bars(db, pair)
            rows = history(db, pair, days=10)
        except Exception:
            logger.debug("build_universe: FX lookup failed for %s", pair, exc_info=True)
            return None
        closes = [_safe_float(r.get("close"), 0.0) for r in rows]
        closes = [c for c in closes if c > 0]
        return closes[-1] if closes else None

    usd_eur = _usd_to("EUR")
    usd_ccy = _usd_to(currency)
    rate = usd_eur / usd_ccy if usd_eur and usd_ccy else None
    cache[currency] = rate
    return rate


def _market_cap_eur(
    db: Session, ticker: str, fund: dict[str, Any], cache: dict[str, float | None],
) -> float | None:
    """Market cap in EUR, or None when it or the exchange rate is unknown.

    Providers report market cap in the listing currency; yfinance quotes LSE
    prices in pence ("GBp") but its market cap in pounds (SHEL.L: 3,656p x
    5.70bn shares = GBP 208.5bn = marketCap, 2026-09-28).
    """
    mcap = _safe_float(fund.get("market_cap"), 0.0)
    if mcap <= 0:
        return None
    raw = str(fund.get("currency") or "").strip()
    currency = "GBP" if raw in ("GBp", "GBX") else (raw.upper() or listing_currency(ticker))
    rate = _eur_per_unit(db, currency, cache)
    return mcap * rate if rate is not None else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_universe(
    db: Session,
    user_id: str,
    focus: str | None = None,
    registry: Any | None = None,
) -> list[dict[str, Any]]:
    """Build a deduplicated candidate universe.

    Sources:
      1. Static index constituents (DAX40, EuroStoxx50, S&P100, Nasdaq-100)
         plus EU/US mid-cap niche seeds, filtered by config-driven fundamentals
         floors (``min_market_cap_eur``, ``min_avg_volume``).
      2. Liquid UCITS ETF list.
      3. NewsItem rows with positive sentiment_label in last 14d where ticker
         is not currently held by user.

    Applies profile exclusions (investor_exclusions comma-separated keywords).
    Dedupes by ticker. Caps at ``universe_max_size`` (active "universe"
    DiscoveryConfig, default 200).
    """
    from app.decision.discover.config import DEFAULT_UNIVERSE_CONFIG, get_or_seed_active_config

    try:
        uni_cfg = {**DEFAULT_UNIVERSE_CONFIG, **(get_or_seed_active_config(db, "universe").config_json or {})}
    except Exception:
        logger.warning("build_universe: could not resolve universe config, using defaults", exc_info=True)
        uni_cfg = dict(DEFAULT_UNIVERSE_CONFIG)
    max_size = int(_safe_float(uni_cfg.get("universe_max_size"), 200) or 200)
    min_mcap = _safe_float(uni_cfg.get("min_market_cap_eur"), 250_000_000)
    min_volume = _safe_float(uni_cfg.get("min_avg_volume"), 100_000)
    # Reserve headroom for ETF + news candidates behind the index pool.
    index_cap = max(10, int(max_size * 0.80))
    etf_cap = max(index_cap + 5, int(max_size * 0.90))

    held = _user_holdings_tickers(db, user_id)
    exclusions = _profile_exclusions(db, user_id)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    # --- 1. Index stocks ---
    # Match focus keywords against word tokens (not raw substrings) so a
    # free-form description like "dividend-focused European large caps" still
    # routes to the right index pool, while avoiding false positives such as
    # "us" matching inside "industrials".
    full_pool = (
        _DAX40 + _EUROSTOXX50 + _SP100 + _NASDAQ100 + _EU_MIDCAP + _US_MIDCAP
        + _STOXX600 + _FTSE100 + _CAC40 + _SP500 + _NIKKEI225
    )
    index_pool: list[str] = full_pool
    if focus:
        tokens = {t for t in re.split(r"[^a-z0-9&]+", focus.lower()) if t}
        if tokens & {"europe", "european", "eu", "de", "germany", "german", "dax", "eurostoxx"}:
            index_pool = _DAX40 + _EUROSTOXX50 + _EU_MIDCAP
        elif tokens & {"us", "usa", "america", "american", "sp", "s&p", "nasdaq"}:
            index_pool = _SP100 + _NASDAQ100 + _US_MIDCAP
        elif tokens & {"tech", "technology", "growth", "semiconductor", "semiconductors", "ai"}:
            index_pool = [t for t in (_SP100 + _NASDAQ100) if t in ("AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD", "INTC", "AVGO", "ADBE", "CRM", "ORCL", "SAP.DE")]

    # Dedupe the pool and apply cheap filters before any network I/O.
    pool_tickers: list[str] = []
    pool_set: set[str] = set()
    for ticker in index_pool:
        t = ticker.upper()
        if t in seen or t in held or t in pool_set:
            continue
        if any(excl in t.lower() for excl in exclusions):
            continue
        pool_tickers.append(t)
        pool_set.add(t)

    # --- DB cache batch-load (main thread, single query) ---
    db_cache = _batch_load_fundamental_cache(db, pool_tickers)

    # Separate cache-hits from misses.
    cache_hits: dict[str, dict[str, Any]] = {}
    cache_misses: list[str] = []
    for t in pool_tickers:
        if t in db_cache:
            cache_hits[t] = db_cache[t]
        else:
            cache_misses.append(t)

    # --- Parallel network fetch for cache-misses (no DB access in workers) ---
    live_fetched: dict[str, dict[str, Any]] = {}
    if cache_misses:
        logger.debug(
            "build_universe: %d cache hits, %d live fetches needed",
            len(cache_hits), len(cache_misses),
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_fetch_live_fundamentals_no_db, t, registry): t
                for t in cache_misses
            }
            for future in as_completed(futures):
                try:
                    ticker_result, fund = future.result()
                    live_fetched[ticker_result] = fund
                except Exception:
                    t = futures[future]
                    logger.debug("Live fundamentals failed for %s", t, exc_info=True)
                    live_fetched[t] = {}

        # Persist live data back to DB on the main thread.
        _persist_fundamentals_batch(db, live_fetched)
    else:
        logger.debug("build_universe: all %d tickers served from DB cache", len(cache_hits))

    # Merge results, apply the floors in EUR and keep the largest names. The
    # pool used to be cut at ``index_cap`` in list order, so whichever lists
    # came first filled it: on 2026-09-28 the 240 slots ran out inside the
    # STOXX 600 list, and no FTSE 100, CAC 40-only, S&P 500-only or Nikkei
    # name was ever evaluated. The floor also compared yen, kroner and
    # pounds with a EUR threshold.
    all_fund: dict[str, dict[str, Any]] = {**cache_hits, **live_fetched}
    eur_rates: dict[str, float | None] = {}
    eligible: list[tuple[float, str, dict[str, Any]]] = []
    unpriced: list[str] = []
    for t in pool_tickers:
        fund = all_fund.get(t, {})
        mcap_eur = _market_cap_eur(db, t, fund, eur_rates)
        vol = _safe_float(fund.get("volume"), 0.0)
        if mcap_eur is None:
            if _safe_float(fund.get("market_cap"), 0.0) > 0:
                unpriced.append(t)
            continue
        if mcap_eur < min_mcap or vol < min_volume:
            continue
        eligible.append((mcap_eur, t, fund))
    if unpriced:
        logger.warning(
            "build_universe: no EUR rate for %d listing(s), left out: %s", len(unpriced), ", ".join(unpriced[:20]),
        )
    eligible.sort(key=lambda item: item[0], reverse=True)
    for _mcap_eur, t, fund in eligible[:index_cap]:
        name = ascii_safe(fund.get("name") or t)
        candidates.append({"symbol": t, "isin": fund.get("isin"), "name": name, "source": "screen_index"})
        seen.add(t)

    # --- 2. UCITS ETFs (from justETF via EtfUniverseProvider) ---
    etf_universe = _get_etf_provider().get_universe()
    for etf in etf_universe:
        t = etf["symbol"].upper()
        if t in seen or t in held:
            continue
        if any(excl in t.lower() or excl in etf["name"].lower() for excl in exclusions):
            continue
        candidates.append({
            "symbol": t,
            "isin": etf["isin"],
            "name": ascii_safe(etf["name"]),
            "source": "screen_etf",
            "tf_class": etf.get("tf_class", "aktien"),
            "domicile_country": etf.get("domicile_country", ""),
            "distribution_policy": etf.get("dividends", ""),
        })
        seen.add(t)
        if len(candidates) >= etf_cap:
            break

    # --- 2b. Curated fixed-income seed (F17) — always included, not screened ---
    for etf in _FIXED_INCOME_SEED:
        t = etf["symbol"].upper()
        if t in seen or t in held:
            continue
        if any(excl in t.lower() or excl in etf["name"].lower() for excl in exclusions):
            continue
        candidates.append({
            "symbol": t,
            "isin": etf["isin"],
            "name": etf["name"],
            "source": "screen_etf",
            "tf_class": etf["tf_class"],
            "domicile_country": etf["domicile_country"],
            "distribution_policy": etf["distribution_policy"],
        })
        seen.add(t)

    # --- 3. News sentiment candidates ---
    cutoff = datetime.now(UTC) - timedelta(days=14)
    news_rows = db.execute(
        select(NewsItem)
        .where(
            NewsItem.sentiment_label == "positive",
            NewsItem.published_at >= cutoff,
            NewsItem.ticker.isnot(None),
        )
        .order_by(NewsItem.published_at.desc())
        .limit(50)
    ).scalars().all()

    for news in news_rows:
        if not news.ticker:
            continue
        t = news.ticker.upper()
        if t in seen or t in held:
            continue
        if any(excl in t.lower() or excl in (news.title or "").lower() for excl in exclusions):
            continue
        # Try to resolve ISIN/name via fallback
        resolved = _search_symbol_fallback(t)
        name = ascii_safe(resolved.get("name", news.title or t)) if resolved else ascii_safe(news.title or t)
        isin = resolved.get("isin") if resolved else None
        candidates.append({
            "symbol": t,
            "isin": isin,
            "name": name,
            "source": "news_sentiment",
        })
        seen.add(t)
        if len(candidates) >= max_size:
            break

    logger.info(
        "build_universe user=%s focus=%s: %d candidates (index=%d, etf=%d, news=%d)",
        user_id,
        focus,
        len(candidates),
        sum(1 for c in candidates if c["source"] == "screen_index"),
        sum(1 for c in candidates if c["source"] == "screen_etf"),
        sum(1 for c in candidates if c["source"] == "news_sentiment"),
    )
    return candidates
