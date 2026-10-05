"""Keep the insider-trading panel current from SEC EDGAR.

The panel was a one-off extract that ended on 2024-03-29, so from then on
Discover's insider signal was "unknown" for every US name (before 2026-09-28
it was worse: scored as "no insider trading"). SEC publishes everything
needed, free:

* **Quarterly bulk sets** (Form 3/4/5, the source
  :mod:`insider_trading_loader` already reads), posted about a quarter
  late. :func:`backfill_quarters` loads every set since 2024 Q2 that the
  panel lacks, reading the links from SEC's page: the 2026 Q2 set moved to
  a different path.
* **The daily EDGAR index** of every filing by form type. For the days
  after the newest quarterly set, :func:`ingest_day` reads each Form 4 and
  4/A filing's ownership XML into the same schema, one partition per day.
  A day's index lists a few hundred Form 4 filings.

Owner decision 2026-09-29: daily ingestion (insider buying is most
informative in its first weeks) rather than quarterly sets alone.

Point in time: ``filing_date`` is the date EDGAR received the filing (the
daily index's "Date Filed"), never the trade date. When a quarterly set
covering a day is loaded, that day's daily partition is removed; the dedup
key would collapse the overlap anyway.

SEC fair access: at most 10 requests a second, with a User-Agent that names
the requester and a contact (public setting ``sec_edgar_user_agent``). This
module stays at 8 a second and backs off on 429/503.
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

import pandas as pd

from app.foundation.data_engineering.insider_trading_loader import load_insider_trading_extract
from app.foundation.data_engineering.insider_trading_schema import (
    INSIDER_TRADING_COLUMNS,
    TRANSACTION_CODE_VALUES,
    InsiderTradingSchemaError,
    validate_insider_trading_frame,
)
from app.foundation.data_engineering.paths import get_panel_dir

logger = logging.getLogger(__name__)

DATASET_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{day}.idx"
ARCHIVE = "https://www.sec.gov/Archives/{path}"

#: The first quarter the one-off extract did not cover.
BACKFILL_FROM = (2024, 2)

#: SEC's limit is 10 requests a second.
_MIN_INTERVAL_S = 0.125
_RETRY_STATUS = (429, 500, 502, 503, 504)
_RETRY_DELAYS_S = (2.0, 10.0, 30.0)

_DEFAULT_USER_AGENT = "Quantfolio self-hosted research tool (set sec_edgar_user_agent to add a contact)"

_PANEL_SUBDIR = "insider_trading_pit"
_DAILY_PREFIX = "edgar_daily_"
_QUARTER_RE = re.compile(r"(\d{4})q([1-4])_form345\.zip", re.I)
_FORM_TYPES = ("4", "4/A")


@dataclass
class InsiderRefreshReport:
    quarters_loaded: list[str] = field(default_factory=list)
    days_ingested: int = 0
    rows_ingested: int = 0
    filings_failed: int = 0
    errors: list[str] = field(default_factory=list)


class SecClient:
    """A rate-limited GET for sec.gov."""

    def __init__(self, user_agent: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        import httpx

        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=60.0,
            follow_redirects=True,
        )
        self._sleep = sleep
        self._last = 0.0

    def get(self, url: str) -> tuple[int, bytes]:
        """``(status, body)``; retries rate-limit and server errors."""
        response = None
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            wait = self._last + _MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            response = self._client.get(url)
            if response.status_code not in _RETRY_STATUS or attempt == len(_RETRY_DELAYS_S):
                break
            self._sleep(_RETRY_DELAYS_S[attempt])
        assert response is not None
        return response.status_code, response.content


def user_agent(db: Any | None = None) -> str:
    """The ``sec_edgar_user_agent`` public setting, else a generic default."""
    if db is not None:
        try:
            from app.foundation.settings import get_public_settings

            value = str(get_public_settings(db).get("sec_edgar_user_agent") or "").strip()
            if value:
                return value
        except Exception:  # noqa: BLE001
            logger.debug("sec_edgar_insider: user agent setting unreadable", exc_info=True)
    return _DEFAULT_USER_AGENT


# --- quarterly sets -----------------------------------------------------------


def quarterly_links(page_html: str) -> dict[tuple[int, int], str]:
    """``{(year, quarter): absolute URL}`` of the zips linked from SEC's page."""
    links: dict[tuple[int, int], str] = {}
    for href in re.findall(r'href="([^"]+_form345\.zip)"', page_html, flags=re.I):
        match = _QUARTER_RE.search(href)
        if match is None:
            continue
        url = href if href.startswith("http") else f"https://www.sec.gov{href}"
        links[(int(match.group(1)), int(match.group(2)))] = url
    return links


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * quarter - 2, 1)
    end = date(year + (quarter == 4), 1 if quarter == 4 else 3 * quarter + 1, 1) - timedelta(days=1)
    return start, end


def _panel_dir() -> Path:
    return get_panel_dir() / _PANEL_SUBDIR


def loaded_quarters(panel: Path | None = None) -> set[tuple[int, int]]:
    folder = panel or _panel_dir()
    found: set[tuple[int, int]] = set()
    for path in folder.glob("*_form345.parquet") if folder.exists() else []:
        match = re.match(r"(\d{4})q([1-4])_form345", path.stem, re.I)
        if match:
            found.add((int(match.group(1)), int(match.group(2))))
    return found


def backfill_quarters(client: SecClient, report: InsiderRefreshReport) -> None:
    """Load every quarterly set since :data:`BACKFILL_FROM` the panel lacks."""
    status, body = client.get(DATASET_PAGE)
    if status != 200:
        hint = " (SEC refuses a User-Agent without an email: set sec_edgar_user_agent)" if status == 403 else ""
        report.errors.append(f"dataset page: HTTP {status}{hint}")
        return
    links = quarterly_links(body.decode("utf-8", errors="replace"))
    have = loaded_quarters()
    for key in sorted(k for k in links if k >= BACKFILL_FROM and k not in have):
        year, quarter = key
        status, content = client.get(links[key])
        if status != 200 or not content.startswith(b"PK"):
            report.errors.append(f"{year}q{quarter}: HTTP {status}")
            continue
        with tempfile.TemporaryDirectory(prefix="sec_form345_") as tmp:
            zip_path = Path(tmp) / f"{year}q{quarter}_form345.zip"
            zip_path.write_bytes(content)
            result = load_insider_trading_extract(zip_path, dry_run=False)
        if not result.ok:
            report.errors.append(f"{year}q{quarter}: {result.error}")
            continue
        report.quarters_loaded.append(f"{year}q{quarter}")
        start, end = _quarter_bounds(year, quarter)
        for path in _panel_dir().glob(f"{_DAILY_PREFIX}*.parquet"):
            day = _daily_partition_day(path)
            if day is not None and start <= day <= end:
                path.unlink(missing_ok=True)
        logger.info("sec_edgar_insider: loaded %sq%s (%d rows)", year, quarter, result.rows_loaded)


# --- daily filings ------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    form_type: str
    cik: str
    filed: date
    path: str


def parse_form_index(text: str) -> list[IndexEntry]:
    """Form 4 and 4/A rows of an EDGAR daily ``form.YYYYMMDD.idx``, one per filing.

    A filing is listed once per filer (issuer and each reporting owner);
    rows are collapsed on the archive path.
    """
    entries: dict[str, IndexEntry] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in _FORM_TYPES or not parts[-1].startswith("edgar/data/"):
            continue
        path, filed_raw, cik = parts[-1], parts[-2], parts[-3]
        if not (filed_raw.isdigit() and cik.isdigit()):
            continue
        filed = datetime.strptime(filed_raw, "%Y%m%d").date()
        entries.setdefault(path, IndexEntry(parts[0], cik.zfill(10), filed, path))
    return list(entries.values())


def _text(node: ElementTree.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _value(node: ElementTree.Element, path: str) -> str:
    return _text(node, f"{path}/value") or _text(node, path)


def _flag(node: ElementTree.Element | None, path: str) -> bool:
    return _text(node, path).lower() in ("1", "true")


def _number(raw: str) -> float | None:
    """A Form 4 amount rounded half-up to 2 decimals, as SEC's quarterly sets
    store it (273.5516 shares -> 273.55, $300.485 -> 300.49). The same
    transaction then has the same dedup key from either source."""
    try:
        return float(Decimal(raw.replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if raw else None
    except (InvalidOperation, ValueError):
        return None


def parse_ownership_document(filing_text: str, filed: date) -> list[dict[str, Any]]:
    """Schema rows (one per non-derivative transaction and reporting owner)
    from a Form 4 filing's full submission text."""
    match = re.search(r"<ownershipDocument>.*?</ownershipDocument>", filing_text, flags=re.S)
    if match is None:
        return []
    root = ElementTree.fromstring(match.group(0))
    issuer = root.find("issuer")
    form_type = _text(root, "documentType") or "4"
    # The Rule 10b5-1 checkbox (April 2023 on), one per filing; absent before.
    plan_raw = _text(root, "aff10b5One").lower()
    plan = True if plan_raw in ("1", "true") else False if plan_raw in ("0", "false") else None
    owners = []
    for owner in root.findall("reportingOwner"):
        relation = owner.find("reportingOwnerRelationship")
        roles = [
            name for name, tag in (
                ("DIRECTOR", "isDirector"), ("OFFICER", "isOfficer"),
                ("TENPERCENTOWNER", "isTenPercentOwner"), ("OTHER", "isOther"),
            ) if _flag(relation, tag)
        ]
        owners.append({
            "cik": _text(owner, "reportingOwnerId/rptOwnerCik").zfill(10),
            "insider_name": _text(owner, "reportingOwnerId/rptOwnerName"),
            "insider_role": ",".join(roles),
        })
    rows: list[dict[str, Any]] = []
    for trans in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        base = {
            "issuer_cik": _text(issuer, "issuerCik").zfill(10),
            "company_name": _text(issuer, "issuerName"),
            "ticker": _text(issuer, "issuerTradingSymbol").upper(),
            "cusip": None,
            "transaction_date": _value(trans, "transactionDate")[:10],
            "filing_date": filed.isoformat(),
            "transaction_code": _text(trans, "transactionCoding/transactionCode").upper(),
            "acquired_disposed": _value(trans, "transactionAmounts/transactionAcquiredDisposedCode").upper() or None,
            "shares": _number(_value(trans, "transactionAmounts/transactionShares")),
            "price_per_share": _number(_value(trans, "transactionAmounts/transactionPricePerShare")),
            "shares_held_after": _number(_value(trans, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
            "ownership_type": _value(trans, "ownershipNature/directOrIndirectOwnership").upper() or None,
            "cleanse_code": None,
            "form_type": form_type,
            "source": "sec_edgar_form4_nonderivative",
            "plan_10b5_1": plan,
        }
        rows.extend({**base, **owner} for owner in owners)
    return rows


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Rows as a validated schema frame; unusable rows are dropped, as the
    quarterly loader does (hand-typed dates, codes)."""
    frame = pd.DataFrame(rows, columns=pd.Index([c for c in INSIDER_TRADING_COLUMNS if c != "ingested_at"]))
    frame["ingested_at"] = datetime.now(UTC).replace(tzinfo=None)
    frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], format="%Y-%m-%d", errors="coerce")
    frame["filing_date"] = pd.to_datetime(frame["filing_date"], format="%Y-%m-%d", errors="coerce")
    frame = cast(pd.DataFrame, frame[
        frame["transaction_date"].notna()
        & frame["filing_date"].notna()
        & (frame["filing_date"] >= frame["transaction_date"])
        & frame["transaction_code"].isin(TRANSACTION_CODE_VALUES)
        & frame["issuer_cik"].str.strip("0").ne("")
    ])
    frame = frame.drop_duplicates(
        subset=["cik", "issuer_cik", "transaction_date", "transaction_code", "shares", "price_per_share"]
    )
    return validate_insider_trading_frame(frame)


def _daily_partition(day: date) -> Path:
    return _panel_dir() / f"{_DAILY_PREFIX}{day:%Y%m%d}.parquet"


def _daily_partition_day(path: Path) -> date | None:
    try:
        return datetime.strptime(path.stem.removeprefix(_DAILY_PREFIX), "%Y%m%d").date()
    except ValueError:
        return None


def ingest_day(client: SecClient, day: date, report: InsiderRefreshReport) -> bool:
    """Read one day's Form 4 filings into ``edgar_daily_YYYYMMDD.parquet``.

    Returns False when EDGAR has no index for the day yet (or at all: a
    weekend or holiday), so the caller can try again later.
    """
    quarter = (day.month - 1) // 3 + 1
    status, body = client.get(DAILY_INDEX.format(year=day.year, quarter=quarter, day=f"{day:%Y%m%d}"))
    if status != 200:
        return False
    rows: list[dict[str, Any]] = []
    for entry in parse_form_index(body.decode("latin-1")):
        status, filing = client.get(ARCHIVE.format(path=entry.path))
        if status != 200:
            report.filings_failed += 1
            continue
        try:
            rows.extend(parse_ownership_document(filing.decode("utf-8", errors="replace"), entry.filed))
        except ElementTree.ParseError:
            report.filings_failed += 1
    try:
        frame = _frame(rows)
    except InsiderTradingSchemaError as exc:
        report.errors.append(f"{day}: {exc}")
        return False
    target = _daily_partition(day)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(target)
    report.days_ingested += 1
    report.rows_ingested += len(frame)
    return True


def _edgar_closed(start: date, end: date) -> set[date]:
    """US federal holidays in [start, end]: EDGAR publishes no index for them."""
    from pandas.tseries.holiday import USFederalHolidayCalendar

    holidays = pd.DatetimeIndex(USFederalHolidayCalendar().holidays(start=start, end=end))
    return {ts.date() for ts in holidays}


def _days_to_ingest(today: date) -> list[date]:
    """Business days after the newest loaded quarter up to yesterday that
    have no daily partition yet."""
    quarters = loaded_quarters()
    if quarters:
        start = _quarter_bounds(*max(quarters))[1] + timedelta(days=1)
    else:
        start = today - timedelta(days=90)
    have = {_daily_partition_day(p) for p in _panel_dir().glob(f"{_DAILY_PREFIX}*.parquet")} if _panel_dir().exists() else set()
    closed = _edgar_closed(start, today)
    days = []
    day = start
    while day < today:
        if day.weekday() < 5 and day not in closed and day not in have:
            days.append(day)
        day += timedelta(days=1)
    return days


def refresh_insider_trading(
    db: Any | None = None,
    *,
    client: SecClient | None = None,
    today: date | None = None,
) -> InsiderRefreshReport:
    """Load missing quarterly sets, then every missing day since the newest.

    Days are ingested oldest first and stop at the first day EDGAR has no
    index for that is less than a week old: the panel's coverage end is its
    newest filing date, so a gap inside the covered range would read as
    "no insider trading" instead of "unknown".
    """
    sec = client or SecClient(user_agent(db))
    clock = today or datetime.now(UTC).date()
    report = InsiderRefreshReport()
    try:
        backfill_quarters(sec, report)
    except Exception as exc:  # noqa: BLE001 - the daily path can still run
        report.errors.append(f"quarterly backfill: {exc}")
        logger.warning("sec_edgar_insider: quarterly backfill failed: %s", exc)
    for day in _days_to_ingest(clock):
        if not ingest_day(sec, day, report) and clock - day < timedelta(days=7):
            break  # not published yet; a later run picks it up
    logger.info("sec_edgar_insider refresh: %s", report)
    return report
