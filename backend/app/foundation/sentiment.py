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
SOURCE_FINBERT = "finbert"
SOURCE_LEXICON = "lexicon"
SOURCE_PROVIDER = "provider"
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
    return score_batch_with_source(texts)[0]


def score_batch_with_source(texts: list[str]) -> tuple[list[dict[str, Any] | None], str]:
    """``(scores, "finbert" | "lexicon")``: the scorer that really ran.

    The keyword fallback gives five distinct scores and cannot tell "3 Ultra-High-Yield
    Stocks Screaming Buys" (negative) from a buy signal; callers store the source so
    readers can ignore lexicon labels.
    """
    pair = _ensure_model()
    if pair is None:
        return [_fallback_lexicon_score(t) for t in texts], SOURCE_LEXICON

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

    return results, SOURCE_FINBERT


def scorer_is_fallback() -> bool:
    """True when FinBERT cannot be used (``transformers`` missing or the model failed to load).

    Cheap: checks the import, not the model, unless the model was already tried.
    """
    if _available is False:
        return True
    if _available is True:
        return False
    import importlib.util

    return importlib.util.find_spec("transformers") is None


def enrich_news_items(db: Any, *, ticker: str | None = None, limit: int = 200) -> int:
    """Score recent ``NewsItem`` rows that lack a BERT label.

    Filters for items where ``sentiment_label IS NULL`` (i.e. the provider
    did not supply sentiment).  Updates ``sentiment_label`` and
    ``sentiment_score`` in-place.

    Returns the number of rows enriched.
    """
    from app.foundation.models.entities import NewsItem

    from sqlalchemy import or_

    # Rows the keyword fallback labelled are scored again once FinBERT can run.
    unscored = NewsItem.sentiment_label.is_(None)
    if not scorer_is_fallback():
        unscored = or_(unscored, NewsItem.sentiment_source == SOURCE_LEXICON)
    query = db.query(NewsItem).filter(unscored)
    if ticker:
        query = query.filter(NewsItem.ticker == ticker.upper())
    rows = query.order_by(NewsItem.published_at.desc()).limit(limit).all()
    if not rows:
        return 0

    texts = [f"{r.title or ''} {r.summary or ''}" for r in rows]
    scores, source = score_batch_with_source(texts)

    enriched = 0
    for row, score in zip(rows, scores):
        if score is None:
            continue
        row.sentiment_label = score["label"]
        row.sentiment_score = round(score["score"], 4)
        row.sentiment_source = source
        enriched += 1

    if enriched:
        db.commit()
        if source == SOURCE_LEXICON:
            logger.warning("Enriched %d news items with the keyword fallback (FinBERT unavailable); readers ignore them.", enriched)
        else:
            logger.info("Enriched %d news items with FinBERT sentiment.", enriched)

    return enriched


def real_sentiment_clause() -> Any:
    """SQL predicate: the article's sentiment is real (FinBERT or the provider's), not the keyword fallback."""
    from sqlalchemy import or_

    from app.foundation.models.entities import NewsItem

    return or_(NewsItem.sentiment_source.is_(None), NewsItem.sentiment_source != SOURCE_LEXICON)


def has_real_sentiment(item: Any) -> bool:
    """Whether *item* carries a label a reader may use (not NULL, not the keyword fallback)."""
    return item.sentiment_label is not None and getattr(item, "sentiment_source", None) != SOURCE_LEXICON


MIN_SCORED_FOR_TONE = 3


def net_tone(positive: int, negative: int, scored: int) -> float | None:
    """(positive - negative) / scored, in [-1, 1]; None under :data:`MIN_SCORED_FOR_TONE` scored articles."""
    if scored < MIN_SCORED_FOR_TONE:
        return None
    return (positive - negative) / scored


def tone_unit(rows: list[Any]) -> tuple[float | None, int]:
    """``(tone on 0-1, scored count)`` of news rows: net tone mapped from [-1, 1], None under three scored.

    Only labels FinBERT or the provider gave count; the stored score is the
    *confidence* of the label (a confident negative also scores 0.9), so the
    labels, not the scores, carry the direction.
    """
    real = [r for r in rows if has_real_sentiment(r)]
    pos = sum(1 for r in real if r.sentiment_label == "positive")
    neg = sum(1 for r in real if r.sentiment_label == "negative")
    tone = net_tone(pos, neg, len(real))
    return (None if tone is None else (tone + 1.0) / 2.0), len(real)


def aggregate_sentiment(db: Any, ticker: str, days: int = 21) -> dict[str, Any]:
    """Aggregate sentiment for *ticker* over the headlines of the last *days* days.

    Returns per-label shares, the **net tone** ((positive - negative) / scored,
    ``None`` under three scored articles), the count of scored articles and an
    overall label. Articles whose label came from the keyword fallback count as
    unscored. ``avg_score`` is kept for older clients and is the mean tone of
    the scored articles; the context builder reads ``finbert_score`` (net tone),
    ``news_count`` (scored) and ``headlines``.
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
    empty = {
        "ticker": ticker.upper(),
        "total": 0,
        "scored": 0,
        "positive_pct": 0.0,
        "negative_pct": 0.0,
        "neutral_pct": 0.0,
        "unscored_pct": 0.0,
        "avg_score": 0.0,
        "net_tone": None,
        "finbert_score": None,
        "news_count": 0,
        "headlines": [],
        "overall": "unscored",
        "articles": [],
    }
    if not rows:
        return empty

    # "unscored" is tracked separately from "neutral": a NULL label means no
    # scorer ran, and a keyword-fallback label is not a score either.
    counts = {"positive": 0, "negative": 0, "neutral": 0, "unscored": 0}
    score_sum = 0.0
    score_count = 0
    articles: list[dict[str, Any]] = []

    for r in rows:
        real = has_real_sentiment(r)
        label = r.sentiment_label if real else "unscored"
        if label in counts:
            counts[label] += 1
        score: float | None = None
        if real and r.sentiment_score is not None:
            score = float(r.sentiment_score)
            score_sum += score
            score_count += 1
        articles.append({
            "title": r.title,
            "source": r.source,
            "published_at": str(r.published_at),
            "sentiment_label": label,
            "sentiment_score": score,
        })

    total = len(rows)
    scored = total - counts["unscored"]
    tone = net_tone(counts["positive"], counts["negative"], scored)
    pos_pct, neg_pct = counts["positive"] / total, counts["negative"] / total
    neu_pct, unscored_pct = counts["neutral"] / total, counts["unscored"] / total

    if counts["unscored"] == total:
        overall = "unscored"
    elif tone is None:
        overall = "unscored"
    elif tone > 0.15:
        overall = "positive"
    elif tone < -0.15:
        overall = "negative"
    else:
        overall = "neutral"

    return {
        "ticker": ticker.upper(),
        "total": total,
        "scored": scored,
        "positive_pct": round(pos_pct, 3),
        "negative_pct": round(neg_pct, 3),
        "neutral_pct": round(neu_pct, 3),
        "unscored_pct": round(unscored_pct, 3),
        "avg_score": round(score_sum / score_count, 4) if score_count else 0.0,
        "net_tone": round(tone, 4) if tone is not None else None,
        "finbert_score": round(tone, 4) if tone is not None else None,
        "news_count": scored,
        "headlines": [a["title"] for a in articles if a["sentiment_label"] != "unscored"][:5],
        "overall": overall,
        "articles": articles,
    }
