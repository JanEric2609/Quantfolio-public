"""LLM batch measurements on fixed sets (plan 2026-10-14, WP-D4.2, D4.3, D4.5 and D5).

Runs on the app LXC with the app's venv and .env, against whatever model the
local endpoint serves. Every database session is READ ONLY: the script reads
settings (endpoint, key, SEC user agent) and, for the dossier set, the inputs of
past Discover runs. It writes nothing to the database.

    # build the fixed sets once (the LAP set is built where yfinance is reachable)
    python scripts/llm_batch_bench.py edgar-build  --out edgar_set.json
    python scripts/llm_batch_bench.py dossier-capture --n 40 --out dossier_set.json
    python scripts/llm_batch_bench.py lap-build    --out lap_set.json

    # run a set against the model the endpoint serves now
    python scripts/llm_batch_bench.py run --task edgar   --set edgar_set.json   --temps 0.7,0.3,0.0 --label 9b --out r.json
    python scripts/llm_batch_bench.py run --task dossier --set dossier_set.json --temps 0.7,0.3,0.0 --label 9b --out r.json
    python scripts/llm_batch_bench.py run --task lap     --set lap_set.json     --temps 0.0         --label 9b --out r.json

    python scripts/llm_batch_bench.py summarize r1.json r2.json ...

The EDGAR set compares numbers read from the filing text with the filer's own
XBRL values (SEC companyfacts) for the same accession.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Read-only database session
# ---------------------------------------------------------------------------


def _readonly_session() -> Any:
    from sqlalchemy import event

    from app.foundation.core.db import SessionLocal, engine

    if engine.dialect.name == "postgresql":
        @event.listens_for(engine, "begin")
        def _ro(conn: Any) -> None:  # every transaction this process opens is read-only
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    return SessionLocal()


# ---------------------------------------------------------------------------
# EDGAR set (D4.3)
# ---------------------------------------------------------------------------

EDGAR_TICKERS = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JNJ", "PG", "KO",
    "PEP", "WMT", "HD", "COST", "MRK", "PFE", "ABBV", "CVX", "XOM", "CSCO",
    "ORCL", "INTC", "NKE", "MCD", "DIS",
)
REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
FIELD_CONCEPTS: dict[str, tuple[tuple[str, ...], str]] = {
    "revenue_usd": (REVENUE_CONCEPTS, "USD"),
    "net_income_usd": (("NetIncomeLoss",), "USD"),
    "eps_diluted_usd": (("EarningsPerShareDiluted",), "USD/shares"),
    "diluted_shares": (("WeightedAverageNumberOfDilutedSharesOutstanding",), "shares"),
}
_STATEMENT_RE = re.compile(
    r"(CONDENSED\s+)?CONSOLIDATED\s+STATEMENTS?\s+OF\s+(OPERATIONS|INCOME|EARNINGS)", re.IGNORECASE
)
STATEMENT_WINDOW_CHARS = 7000


def html_to_text(raw: str) -> str:
    """Flatten filing HTML: one table row per line, cells joined by ' | '."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?i)</t[dh]\s*>", " | ", s)
    s = re.sub(r"(?i)</tr\s*>|<br\s*/?>|</p\s*>|</div\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = []
    for line in s.split("\n"):
        line = re.sub(r"[ \t]+", " ", line)
        line = re.sub(r"(\|\s*){2,}", "| ", line).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def statement_window(text: str) -> str | None:
    """The income statement: the first heading followed by 'per share' and figures, not the TOC."""
    best: tuple[int, int] | None = None
    for m in _STATEMENT_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if (line_end if line_end >= 0 else len(text)) - line_start > 100:
            continue  # a sentence that mentions the statement, not its heading
        head = text[m.start(): m.start() + 1500]
        if not re.search(r"(?i)per\s+share|diluted", text[m.start(): m.start() + 4000]):
            continue
        # A table-of-contents entry is followed by page numbers; the statement by figures at once.
        figures = len(re.findall(r"\d{1,3}(,\d{3})+", head))
        if figures >= 6 and (best is None or figures > best[0]):
            best = (figures, line_start)
    return text[best[1]: best[1] + STATEMENT_WINDOW_CHARS] if best else None


def _fact_for(facts: dict[str, Any], concepts: tuple[str, ...], unit: str, accn: str, end: str,
              form: str) -> float | None:
    lo, hi = (330, 380) if form == "10-K" else (80, 100)
    for concept in concepts:
        node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
        if not node:
            continue
        for row in node.get("units", {}).get(unit, []):
            if row.get("accn") != accn or row.get("end") != end or not row.get("start"):
                continue
            days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
            if lo <= days <= hi:
                return float(row["val"])
    return None


def edgar_build(out: Path) -> None:
    from app.foundation.data_engineering.sec_edgar_insider import SecClient, user_agent

    db = _readonly_session()
    try:
        client = SecClient(user_agent(db))
    finally:
        db.close()
    status, body = client.get("https://www.sec.gov/files/company_tickers.json")
    if status != 200:
        raise SystemExit(f"company_tickers: HTTP {status}")
    ciks = {row["ticker"]: int(row["cik_str"]) for row in json.loads(body).values()}
    items: list[dict[str, Any]] = []
    for ticker in EDGAR_TICKERS:
        cik = ciks[ticker]
        _, sub = client.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        _, fb = client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
        recent = json.loads(sub)["filings"]["recent"]
        facts = json.loads(fb)
        for form in ("10-K", "10-Q"):
            idx = next((i for i, f in enumerate(recent["form"]) if f == form), None)
            if idx is None:
                continue
            accn = recent["accessionNumber"][idx]
            end = recent["reportDate"][idx]
            doc = recent["primaryDocument"][idx]
            truth = {
                field: _fact_for(facts, concepts, unit, accn, end, form)
                for field, (concepts, unit) in FIELD_CONCEPTS.items()
            }
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"
            st, raw = client.get(url)
            window = statement_window(html_to_text(raw.decode("utf-8", "replace"))) if st == 200 else None
            items.append({
                "ticker": ticker, "cik": cik, "form": form, "accn": accn, "period_end": end,
                "url": url, "truth": truth, "text": window,
            })
            print(f"{ticker} {form} {end} text={'yes' if window else 'NO'} "
                  f"truth={sum(v is not None for v in truth.values())}/4", flush=True)
    out.write_text(json.dumps({"built": datetime.now(UTC).isoformat(), "items": items}, indent=1))


EDGAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "revenue_usd": {"type": ["number", "null"]},
        "net_income_usd": {"type": ["number", "null"]},
        "eps_diluted_usd": {"type": ["number", "null"]},
        "diluted_shares": {"type": ["number", "null"]},
    },
    "required": ["revenue_usd", "net_income_usd", "eps_diluted_usd", "diluted_shares"],
}
EDGAR_SYSTEM = (
    "You read financial statements and copy numbers exactly. Reply with one JSON object only."
)


def edgar_prompt(item: dict[str, Any]) -> list[dict[str, str]]:
    period = ("the fiscal year ended " if item["form"] == "10-K" else "the three months ended ") + item["period_end"]
    user = (
        f"Below is the income statement section from {item['ticker']}'s {item['form']}.\n"
        f"For {period}, give:\n"
        "- revenue_usd: total revenue (net sales), in US dollars as a plain number\n"
        "- net_income_usd: net income attributable to the company, in US dollars\n"
        "- eps_diluted_usd: diluted earnings per share, in US dollars\n"
        "- diluted_shares: weighted-average diluted shares outstanding, as a count of shares\n"
        "The table may state amounts in millions or thousands; convert to full units. "
        "Use null for a figure that is not in the text.\n\n"
        f"{item['text']}"
    )
    return [{"role": "system", "content": EDGAR_SYSTEM}, {"role": "user", "content": user}]


def edgar_score(item: dict[str, Any], answer: dict[str, Any] | None) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for field, truth in item["truth"].items():
        if truth is None:
            fields[field] = "no_truth"
            continue
        if field == "diluted_shares" and truth < 1e6:
            # The filer tagged the count in millions (McDonald's: 716.4); the XBRL value is wrong.
            fields[field] = "truth_error"
            continue
        got = (answer or {}).get(field)
        if not isinstance(got, (int, float)):
            fields[field] = "missing"
            continue
        if field == "eps_diluted_usd":
            ok = abs(float(got) - truth) <= 0.005 + 1e-9
        else:
            ok = truth != 0 and abs(float(got) - truth) / abs(truth) <= 0.005
        fields[field] = "correct" if ok else "wrong"
    return fields


# ---------------------------------------------------------------------------
# Dossier set (D4.5 and D5)
# ---------------------------------------------------------------------------


class _Captured(BaseException):
    """Raised from the patched LLM call, past write_dossier's ``except Exception``."""


def dossier_capture(n: int, out: Path) -> None:
    import app.decision.discover.dossier_writer as dw
    from app.decision.discover.config import get_or_seed_active_config
    from app.foundation.models.entities import DiscoverCandidate, DiscoverRun

    db = _readonly_session()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    captured: dict[str, Any] = {}

    def fake_llm(_db: Any, messages: list[dict[str, str]], timeout_s: float = 120.0,
                 json_schema: dict[str, Any] | None = None, sampling: dict[str, Any] | None = None,
                 *, max_tokens: int | None = None, telemetry: dict[str, Any] | None = None) -> str:
        captured.update(messages=messages, json_schema=json_schema, sampling=sampling, max_tokens=max_tokens)
        raise _Captured

    dw._local_llm_sync = fake_llm  # type: ignore[assignment]
    try:
        prompt_cfg = get_or_seed_active_config(db, "prompt_template")
        prompt_json = prompt_cfg.config_json if isinstance(prompt_cfg.config_json, dict) else {}
        macro = dw._macro_facts(db)
        runs = (
            db.query(DiscoverRun).filter(DiscoverRun.status == "completed")
            .order_by(DiscoverRun.created_at.desc()).limit(30).all()
        )
        for run in runs:
            profile = (json.loads(run.params_json or "{}") or {}).get("profile") or {}
            cands = (
                db.query(DiscoverCandidate)
                .filter(DiscoverCandidate.run_id == run.id, DiscoverCandidate.dossier_id.isnot(None))
                .all()
            )
            for cand in cands:
                if cand.symbol in seen or len(items) >= n:
                    continue
                captured.clear()
                cdict = {
                    "id": cand.id, "run_id": cand.run_id, "symbol": cand.symbol, "isin": cand.isin,
                    "name": cand.name, "source": cand.source, "status": cand.status,
                    "scores_json": cand.scores_json, "tradeable_json": cand.tradeable_json,
                }
                try:
                    dw.write_dossier(db, cdict, profile, prompt_config=prompt_json)
                except _Captured:
                    pass
                except Exception as exc:  # noqa: BLE001 - a read-only write attempt or a provider error
                    print(f"{cand.symbol}: not captured ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
                    db.rollback()
                    continue
                if not captured:
                    continue
                context = captured["messages"][-1]["content"]
                m = re.search(r"Direction \(fixed by the system\): (\w+)\.", context)
                items.append({
                    "symbol": cand.symbol, "run_id": run.id, "messages": captured["messages"],
                    "json_schema": captured["json_schema"], "app_sampling": captured["sampling"],
                    "max_tokens": captured["max_tokens"], "direction": m.group(1) if m else "neutral",
                })
                seen.add(cand.symbol)
                print(f"captured {cand.symbol} ({len(items)}/{n})", flush=True)
            db.rollback()
            if len(items) >= n:
                break
    finally:
        db.rollback()
        db.close()
    out.write_text(json.dumps({"built": datetime.now(UTC).isoformat(), "macro": macro, "items": items}, indent=1))


def dossier_score(item: dict[str, Any], raw: str, telemetry: dict[str, Any], macro: Any) -> dict[str, Any]:
    """First-attempt outcome, as the writer would judge it: ok, or the first failing rule."""
    from pydantic import ValidationError

    import app.decision.discover.dossier_writer as dw
    from app.decision.discover.thesis_checks import check_thesis

    parsed = dw._try_parse_json(raw)
    if parsed is None:
        return {"outcome": "truncated" if telemetry.get("truncated") else "unparseable"}
    try:
        validated = dw.DossierOutputModel.model_validate(dw._coerce_dossier(parsed)).model_dump()
    except ValidationError:
        return {"outcome": "schema"}
    check = check_thesis(
        validated.get("thesis") or "", list(validated.get("key_risks") or []),
        facts_text=item["messages"][-1]["content"], macro=macro, direction=item["direction"],
    )
    return {"outcome": "ok" if check.ok else (check.reason or "invalid"), "problems": check.problems[:3]}


# ---------------------------------------------------------------------------
# Look-ahead probe set (D4.2)
# ---------------------------------------------------------------------------

LAP_TICKERS = (
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "JPM", "JNJ",
    "V", "PG", "UNH", "HD", "MA", "XOM", "CVX", "LLY", "ABBV", "MRK",
    "PEP", "KO", "AVGO", "COST", "WMT", "MCD", "CSCO", "ACN", "TMO", "ABT",
    "DHR", "NEE", "LIN", "TXN", "PM", "ORCL", "NKE", "BMY", "UPS", "RTX",
    "QCOM", "HON", "LOW", "AMGN", "IBM", "CAT", "INTC", "SBUX", "GS", "BA",
    "MS", "BLK", "AMD", "INTU", "ISRG", "GE", "MMM", "CVS", "DE", "SPGI",
    "PLD", "MDT", "GILD", "ADP", "C", "BKNG", "AXP", "TJX", "MDLZ", "SYK",
    "CI", "MO", "ZTS", "DUK", "SO", "CL", "PNC", "USB", "BDX", "CSX",
    "NFLX", "ADBE", "CRM", "PYPL", "T", "VZ", "CMCSA", "DIS", "PFE", "WFC",
    "BAC", "MU", "GM", "F", "FDX", "EMR", "TGT", "COP", "SLB", "EOG",
)
LAP_SEED = 20261015
LAP_PRE_PER_DATE = 30
LAP_POST_FROM = date(2025, 7, 1)  # every name from here on, so the post-cutoff sample is not thin


def lap_build(out: Path) -> None:
    import yfinance as yf

    closes = yf.download(list(LAP_TICKERS) + ["SPY"], start="2018-12-01", auto_adjust=True,
                         progress=False)["Close"]
    names: dict[str, str] = {}
    for t in LAP_TICKERS:
        try:
            names[t] = yf.Ticker(t).info.get("shortName") or t
        except Exception:  # noqa: BLE001
            names[t] = t
    rng = random.Random(LAP_SEED)
    items: list[dict[str, Any]] = []
    month = date(2019, 1, 1)
    last_possible = closes.index[-1].date()
    while True:
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        if nxt > last_possible:
            break
        start_rows = closes.loc[closes.index.date >= month]
        end_rows = closes.loc[closes.index.date >= nxt]
        if start_rows.empty or end_rows.empty:
            break
        p0, p1 = start_rows.iloc[0], end_rows.iloc[0]
        spy = float(p1["SPY"] / p0["SPY"] - 1)
        pool = list(LAP_TICKERS) if month >= LAP_POST_FROM else rng.sample(list(LAP_TICKERS), LAP_PRE_PER_DATE)
        for t in pool:
            if not (p0[t] > 0 and p1[t] > 0):
                continue
            r = float(p1[t] / p0[t] - 1)
            items.append({
                "ticker": t, "name": names[t], "as_of": month.isoformat(), "outcome_end": nxt.isoformat(),
                "excess_return": r - spy, "outperformed": r > spy,
            })
        month = nxt
    out.write_text(json.dumps({"built": datetime.now(UTC).isoformat(), "seed": LAP_SEED, "items": items}, indent=1))
    print(f"{len(items)} questions, {items[0]['as_of']}..{items[-1]['as_of']}")


LAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"outperform": {"type": "boolean"}, "confidence": {"type": "integer", "minimum": 0, "maximum": 100}},
    "required": ["outperform", "confidence"],
}


def lap_prompt(item: dict[str, Any]) -> list[dict[str, str]]:
    as_of = date.fromisoformat(item["as_of"]).strftime("%B %-d, %Y")
    return [
        {"role": "system", "content": f"You are an equity analyst. Today is {as_of}. Reply with one JSON object only."},
        {"role": "user", "content": (
            f"Will {item['name']} ({item['ticker']}) stock outperform the S&P 500 over the next month, "
            f"from {as_of} to the first trading day of the following month? "
            "Give outperform (true or false) and your confidence from 0 to 100."
        )},
    ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

BASE_SAMPLING = {"top_p": 0.8, "top_k": 20, "presence_penalty": 1.5}


def _served_model(db: Any) -> str:
    """The model file the endpoint serves now (llama.cpp's /v1/models), else the configured name.

    The configured name only says what the app asks for; llama.cpp answers with
    whatever it loaded, so a benchmark of a second unit must ask the server.
    """
    import httpx

    from app.foundation.settings import resolve_llm_api_key, resolve_llm_base_url, resolve_llm_model

    base = resolve_llm_base_url(db).rstrip("/")
    key = resolve_llm_api_key(db)
    try:
        resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=10.0)
        resp.raise_for_status()
        return str(resp.json()["data"][0]["id"]).rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - the label still names the run
        return resolve_llm_model(db)


def run(task: str, set_path: Path, temps: list[float], label: str, out: Path, limit: int | None) -> None:
    from app.decision.llm_portfolio.review import _local_llm_sync

    data = json.loads(set_path.read_text())
    items = data["items"][:limit] if limit else data["items"]
    db = _readonly_session()
    model = _served_model(db)
    results: list[dict[str, Any]] = []
    t_start = time.time()
    try:
        for temp in temps:
            for i, item in enumerate(items):
                if task == "edgar":
                    if not item.get("text"):
                        continue
                    messages, schema, budget = edgar_prompt(item), EDGAR_SCHEMA, 256
                elif task == "dossier":
                    messages, schema, budget = item["messages"], item["json_schema"], item["max_tokens"] or 1024
                else:
                    messages, schema, budget = lap_prompt(item), LAP_SCHEMA, 64
                telemetry: dict[str, Any] = {}
                t0 = time.time()
                try:
                    raw = _local_llm_sync(db, messages, timeout_s=600.0, json_schema=schema,
                                          sampling={**BASE_SAMPLING, "temperature": temp},
                                          max_tokens=budget, telemetry=telemetry)
                    error = None
                except Exception as exc:  # noqa: BLE001
                    raw, error = "", f"{type(exc).__name__}: {str(exc)[:200]}"
                latency = time.time() - t0
                row: dict[str, Any] = {
                    "i": i, "temp": temp, "latency_s": round(latency, 3), "error": error,
                    "prompt_tokens": telemetry.get("prompt_tokens"),
                    "completion_tokens": telemetry.get("completion_tokens"),
                    "truncated": telemetry.get("truncated"),
                }
                if task == "edgar":
                    try:
                        answer = json.loads(raw) if raw else None
                    except ValueError:
                        answer = None
                    row.update(ticker=item["ticker"], form=item["form"], answer=answer,
                               fields=edgar_score(item, answer))
                elif task == "dossier":
                    row.update(symbol=item["symbol"], **dossier_score(item, raw, telemetry, data.get("macro")))
                else:
                    try:
                        answer = json.loads(raw) if raw else {}
                    except ValueError:
                        answer = {}
                    row.update(ticker=item["ticker"], as_of=item["as_of"], outperformed=item["outperformed"],
                               excess_return=item["excess_return"], answer=answer.get("outperform"),
                               confidence=answer.get("confidence"))
                results.append(row)
                if i % 25 == 0:
                    print(f"[{label} {task} t={temp}] {i}/{len(items)} {time.time() - t_start:.0f}s", flush=True)
            db.rollback()
    finally:
        db.close()
    out.write_text(json.dumps({
        "task": task, "label": label, "model": model, "temps": temps,
        "set_sha256": hashlib.sha256(set_path.read_bytes()).hexdigest(),
        "finished": datetime.now(UTC).isoformat(), "wall_s": round(time.time() - t_start, 1),
        "results": results,
    }, indent=1))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (c - h, c + h)


def _auc(rows: list[dict[str, Any]]) -> float:
    """Area under the ROC curve of the model's P(outperform) against the outcome (ties count half).

    P(outperform) is the stated confidence for a "yes" and 100 minus it for a
    "no". Accuracy alone hides recall when the model nearly always says "no".
    """
    scored = [((r["confidence"] if r["answer"] else 100 - r["confidence"]), r["outperformed"]) for r in rows
              if isinstance(r["answer"], bool) and isinstance(r.get("confidence"), int | float)]
    pos = [s for s, y in scored if y]
    neg = [s for s, y in scored if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def summarize(paths: list[Path], cutoff: str | None) -> None:
    for path in paths:
        doc = json.loads(path.read_text())
        task, rows = doc["task"], doc["results"]
        print(f"\n== {path.name}: {task}, {doc['label']} ({doc['model']}), wall {doc['wall_s']} s")
        for temp in doc["temps"]:
            rs = [r for r in rows if r["temp"] == temp]
            lat = sorted(r["latency_s"] for r in rs)
            toks = sum(r.get("completion_tokens") or 0 for r in rs)
            gen_s = sum(r["latency_s"] for r in rs)
            errors = sum(1 for r in rs if r["error"])
            base = (f"  t={temp}: n={len(rs)} errors={errors} median latency {lat[len(lat) // 2]:.2f} s "
                    f"p90 {lat[int(len(lat) * 0.9)]:.2f} s, {toks / gen_s:.1f} completion tok/s") if rs else f"  t={temp}: none"
            print(base)
            if task == "edgar":
                counts: dict[str, int] = {}
                for r in rs:
                    for v in r["fields"].values():
                        counts[v] = counts.get(v, 0) + 1
                answered = counts.get("correct", 0) + counts.get("wrong", 0)
                lo, hi = _wilson(counts.get("correct", 0), answered)
                print(f"    fields {counts}; accuracy on answered numbers "  # truth_error rows are left out
                      f"{counts.get('correct', 0)}/{answered} = {counts.get('correct', 0) / max(answered, 1):.1%} "
                      f"(95 % {lo:.1%}-{hi:.1%})")
            elif task == "dossier":
                counts = {}
                for r in rs:
                    counts[r.get("outcome") or "error"] = counts.get(r.get("outcome") or "error", 0) + 1
                ok = counts.get("ok", 0)
                lo, hi = _wilson(len(rs) - ok, len(rs))
                print(f"    first-attempt outcomes {counts}; rejection rate {(len(rs) - ok) / max(len(rs), 1):.1%} "
                      f"(95 % {lo:.1%}-{hi:.1%})")
            else:
                cut = cutoff or "2026-02-01"
                for name, sel in (("pre", [r for r in rs if r["as_of"] < cut]),
                                  ("post", [r for r in rs if r["as_of"] >= cut])):
                    valid = [r for r in sel if isinstance(r["answer"], bool)]
                    k = sum(1 for r in valid if r["answer"] == r["outperformed"])
                    lo, hi = _wilson(k, len(valid))
                    base_rate = sum(1 for r in valid if r["outperformed"]) / max(len(valid), 1)
                    said_yes = sum(1 for r in valid if r["answer"]) / max(len(valid), 1)
                    print(f"    {name} (as_of {'<' if name == 'pre' else '>='} {cut}): accuracy {k}/{len(valid)} = "
                          f"{k / max(len(valid), 1):.1%} (95 % {lo:.1%}-{hi:.1%}); outperformed {base_rate:.1%}, "
                          f"model said yes {said_yes:.1%}; AUC {_auc(valid):.3f}")
                by_year: dict[str, list[bool]] = {}
                for r in rs:
                    if isinstance(r["answer"], bool):
                        by_year.setdefault(r["as_of"][:4], []).append(r["answer"] == r["outperformed"])
                print("    by year: " + ", ".join(f"{y} {sum(v) / len(v):.0%} (n={len(v)})" for y, v in sorted(by_year.items())))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("edgar-build", "lap-build"):
        s = sub.add_parser(name)
        s.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("dossier-capture")
    s.add_argument("--n", type=int, default=40)
    s.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("run")
    s.add_argument("--task", choices=("edgar", "dossier", "lap"), required=True)
    s.add_argument("--set", type=Path, required=True)
    s.add_argument("--temps", default="0.7")
    s.add_argument("--label", required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--limit", type=int)
    s = sub.add_parser("summarize")
    s.add_argument("paths", type=Path, nargs="+")
    s.add_argument("--cutoff")
    a = p.parse_args()
    if a.cmd == "edgar-build":
        edgar_build(a.out)
    elif a.cmd == "lap-build":
        lap_build(a.out)
    elif a.cmd == "dossier-capture":
        dossier_capture(a.n, a.out)
    elif a.cmd == "run":
        run(a.task, a.set, [float(t) for t in a.temps.split(",")], a.label, a.out, a.limit)
    else:
        summarize(a.paths, a.cutoff)


if __name__ == "__main__":
    main()
