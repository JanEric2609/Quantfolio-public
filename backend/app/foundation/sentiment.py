"""FinBERT sentiment analysis service.

Uses ``ProsusAI/finbert`` (3-class: positive / negative / neutral) to
score financial news headlines.  The model is loaded lazily on first use
and kept as a module-level singleton to avoid re-loading overhead.

When ``transformers`` is not installed, every public function degrades
gracefully — returns ``None`` for individual scores and empty dicts for
aggregations so callers never crash.

Deployment note: this runs in the **worker process only**.  The API server
never loads the model into memory.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model singleton
# ---------------------------------------------------------------------------

_model: Any = None
_tokenizer: Any = None
_available: bool | None = None  # None = not checked yet


def _ensure_model() -> tuple[Any, Any] | None:
    """Return (model, tokenizer) or ``None`` if transformers is missing."""
    global _model, _tokenizer, _available

    if _available is False:
        return None

    if _model is not None:
        return _model, _tokenizer

    try:
        import torch  # noqa: F401 — ensure torch is importable
        from transformers import AutoModelForSequenceClassification, AutoTokenizer  # type: ignore[import-untyped]

        model_name = "ProsusAI/finbert"
        logger.info("Loading FinBERT model (%s)…", model_name)
        _tokenizer = AutoTokenizer.from_pretrained(model_name)
        _model = AutoModelForSequenceClassification.from_pretrained(model_name)
        _model.eval()
        _available = True
        logger.info("FinBERT model loaded successfully.")
        return _model, _tokenizer
    except ImportError:
        logger.warning("transformers package not installed — FinBERT unavailable.")
        _available = False
        return None
    except Exception as exc:
        logger.warning("Failed to load FinBERT model: %s", exc)
        _available = False
        return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LABELS = ["positive", "negative", "neutral"]
_MAX_LENGTH = 512
_BATCH_SIZE = 16


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """Return True if the FinBERT model was loaded successfully."""
    return _ensure_model() is not None


def score_text(text: str) -> dict[str, Any] | None:
    """Score a single text string.

    Returns ``{"label": "positive"|"negative"|"neutral", "score": float}``
    or ``None`` if the model is unavailable.
    """
    results = score_batch([text])
    return results[0] if results else None


def _fallback_lexicon_score(text: str) -> dict[str, Any]:
    """Lightweight financial lexicon scorer used when FinBERT is not loaded."""
    text_lower = (text or "").lower()
    pos_keywords = {
        "surge", "jump", "gain", "gains", "profit", "profits", "record", "beat", "beats",
        "bullish", "growth", "upgrade", "upgrades", "outperform", "rally", "rise", "rises", "strong", "higher",
    }
    neg_keywords = {
        "drop", "drops", "slump", "slumps", "fall", "falls", "loss", "losses", "miss", "misses",
        "warning", "downgrade", "downgrades", "plunge", "plunges", "crisis", "bearish", "decline", "declines", "weak", "lower", "cut",
    }
    pos_matches = sum(1 for kw in pos_keywords if kw in text_lower)
    neg_matches = sum(1 for kw in neg_keywords if kw in text_lower)
    if pos_matches > neg_matches:
        return {"label": "positive", "score": round(min(0.95, 0.55 + pos_matches * 0.1), 4)}
    elif neg_matches > pos_matches:
        return {"label": "negative", "score": round(min(0.95, 0.55 + neg_matches * 0.1), 4)}
    return {"label": "neutral", "score": 0.5}


def score_batch(texts: list[str]) -> list[dict[str, Any] | None]:
    """Score a list of texts. Returns one dict per input (or fallback)."""
    pair = _ensure_model()
    if pair is None:
        return [_fallback_lexicon_score(t) for t in texts]

    model, tokenizer = pair
    import torch

    results: list[dict[str, Any] | None] = []
    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        # Truncate / guard against empty strings
        cleaned = [t[:_MAX_LENGTH] if t else "empty" for t in batch]
        inputs = tokenizer(cleaned, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            outputs = model(**inputs)
        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        for row in probs:
            idx = int(row.argmax())
            results.append({"label": _LABELS[idx], "score": float(row[idx])})

    return results


def enrich_news_items(db: Any, *, ticker: str | None = None, limit: int = 200) -> int:
    """Score recent ``NewsItem`` rows that lack a BERT label.

    Filters for items where ``sentiment_label IS NULL`` (i.e. the provider
    did not supply sentiment).  Updates ``sentiment_label`` and
    ``sentiment_score`` in-place.

    Returns the number of rows enriched.
    """
    from app.foundation.models.entities import NewsItem

    query = db.query(NewsItem).filter(NewsItem.sentiment_label.is_(None))
    if ticker:
        query = query.filter(NewsItem.ticker == ticker.upper())
    rows = query.order_by(NewsItem.published_at.desc()).limit(limit).all()
    if not rows:
        return 0

    texts = [f"{r.title or ''} {r.summary or ''}" for r in rows]
    scores = score_batch(texts)

    enriched = 0
    for row, score in zip(rows, scores):
        if score is None:
            continue
        row.sentiment_label = score["label"]
        row.sentiment_score = round(score["score"], 4)
        enriched += 1

    if enriched:
        db.commit()
        logger.info("Enriched %d news items with FinBERT sentiment.", enriched)

    return enriched


def aggregate_sentiment(db: Any, ticker: str, days: int = 21) -> dict[str, Any]:
    """Aggregate sentiment scores for *ticker* over recent news.

    Returns a dict with per-label counts, average score, and an overall
    weighted label — suitable for direct JSON serialisation.
    """
    from datetime import UTC, datetime, timedelta

    from app.foundation.models.entities import NewsItem

    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        db.query(NewsItem)
        .filter(NewsItem.ticker == ticker.upper(), NewsItem.published_at >= cutoff)
        .order_by(NewsItem.published_at.desc())
        .all()
    )
    if not rows:
        return {
            "ticker": ticker.upper(),
            "total": 0,
            "positive_pct": 0.0,
            "negative_pct": 0.0,
            "neutral_pct": 0.0,
            "unscored_pct": 0.0,
            "avg_score": 0.0,
            "overall": "neutral",
            "articles": [],
        }

    # "unscored" is tracked separately from "neutral": a NULL sentiment_label
    # means FinBERT never ran on this row (e.g. the model/dependency was
    # unavailable), not that it scored the article as neutral. Collapsing the
    # two silently misrepresented "100% unscored" as "100% neutral".
    counts = {"positive": 0, "negative": 0, "neutral": 0, "unscored": 0}
    score_sum = 0.0
    score_count = 0
    articles: list[dict[str, Any]] = []

    for r in rows:
        label = r.sentiment_label if r.sentiment_label is not None else "unscored"
        if label in counts:
            counts[label] += 1
        score: float | None
        if r.sentiment_score is not None:
            score = float(r.sentiment_score)
            score_sum += score
            score_count += 1
        else:
            score = None
        articles.append({
            "title": r.title,
            "source": r.source,
            "published_at": str(r.published_at),
            "sentiment_label": label,
            "sentiment_score": score,
        })

    total = len(rows)
    pos_pct = counts["positive"] / total
    neg_pct = counts["negative"] / total
    neu_pct = counts["neutral"] / total
    unscored_pct = counts["unscored"] / total
    avg = score_sum / score_count if score_count else 0.0

    if counts["unscored"] == total:
        overall = "unscored"
    elif pos_pct > neg_pct and pos_pct > neu_pct:
        overall = "positive"
    elif neg_pct > pos_pct and neg_pct > neu_pct:
        overall = "negative"
    else:
        overall = "neutral"

    return {
        "ticker": ticker.upper(),
        "total": total,
        "positive_pct": round(pos_pct, 3),
        "negative_pct": round(neg_pct, 3),
        "neutral_pct": round(neu_pct, 3),
        "unscored_pct": round(unscored_pct, 3),
        "avg_score": round(avg, 4),
        "overall": overall,
        "articles": articles,
    }
