import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.recommendation_engine.context_builder import _build_ticker_fundamentals
from app.foundation.core.db import Base
from app.foundation.models.entities import Fundamental
from app.foundation.piotroski import compute_piotroski


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed(db):
    db.add(Fundamental(ticker="SAP", source="test", data_json=json.dumps(
        {"roe": 0.18, "profit_margin": 0.2, "pe_ratio": 30, "debt_equity": 40, "gross_margin": 0.7}
    )))
    db.commit()


def test_score_is_under_the_key_every_reader_uses():
    db = _memory_db()
    _seed(db)
    result = compute_piotroski(db, "sap")
    # the StockDetail card reads `score`; it used to be returned as `total`
    assert result["score"] == 5
    assert result["details"]["roe"] == 0.18


def test_recommendation_context_receives_fundamentals():
    db = _memory_db()
    _seed(db)
    available, failed, degraded = [], [], []
    fundamentals = _build_ticker_fundamentals(db, ["SAP"], available, failed, degraded)
    # it looked for "piotroski_score"/"details" keys that never existed, so
    # every ticker landed in `degraded` with empty fundamentals
    assert fundamentals["SAP"].piotroski_score == 5
    assert fundamentals["SAP"].pe_ratio == 30
    assert "fundamentals.SAP" in available and not degraded
