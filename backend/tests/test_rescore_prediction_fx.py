"""The one-time FX re-score (plan 2026-10-14, M12): a dry run writes nothing."""
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.decision.discover import resolution
from app.foundation.core.db import Base
from app.foundation.models.entities import DiscoveryPrediction, User

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rescore_prediction_fx.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rescore_prediction_fx", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(tmp_path):
    # A file database: two sessions must not share one connection, or a commit on one would cover both.
    engine = create_engine(f"sqlite:///{tmp_path / 'fx.db'}", poolclass=NullPool)
    # Every DDL statement would otherwise wait for the disk.
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA synchronous=OFF"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    user = User(username="fx", password_hash="x")
    db.add(user)
    db.flush()
    now = datetime.now(UTC)
    db.add(DiscoveryPrediction(
        id=uuid4().hex, user_id=user.id, run_id=uuid4().hex, symbol="SHEL.L", predicted_at=now - timedelta(days=60),
        horizon_days=21, resolve_at=now - timedelta(days=30), direction="buy", conviction=0.6,
        price_at_prediction=10.0, outcome_status="resolved", realised_return=0.05, excess_return=0.03,
        benchmark_return=0.02, score_json={"hit": 1, "brier": 0.16, "brier_basis": "raw_score", "outcome": {
            "currency": "GBP", "local_return": 0.05, "return_currency": "GBP",
            "entry_date": (now - timedelta(days=60)).date().isoformat(),
            "exit_date": (now - timedelta(days=30)).date().isoformat(),
        }},
    ))
    db.commit()
    db.close()
    return Session


def _run(module, monkeypatch, Session, argv, factor):
    def eur_factor(self, currency, entry, exit_):
        self._db.commit()  # what market.history does after a live fetch
        return factor

    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(resolution._Series, "eur_factor", eur_factor)
    monkeypatch.setattr(sys, "argv", ["rescore_prediction_fx.py", *argv])
    module.main()


def test_a_dry_run_writes_nothing_even_when_prices_commit(tmp_path, monkeypatch):
    Session = _seed(tmp_path)
    _run(_load_script(), monkeypatch, Session, [], 0.5)
    row = Session().query(DiscoveryPrediction).one()
    assert row.realised_return == 0.05 and "fx_basis" not in row.score_json["outcome"]


def test_apply_restates_in_eur_and_recomputes_the_hit(tmp_path, monkeypatch):
    Session = _seed(tmp_path)
    # The pound fell by half against the euro: +5 % in GBP is -47.5 % in EUR.
    _run(_load_script(), monkeypatch, Session, ["--apply"], 0.5)
    row = Session().query(DiscoveryPrediction).one()
    assert row.score_json["outcome"]["fx_basis"] == "eur"
    assert row.excess_return < 0 and row.score_json["hit"] == 0
    assert row.score_json["brier"] == (0.6 - 0) ** 2
    assert row.score_json["before_fx_fix"]["hit"] == 1
