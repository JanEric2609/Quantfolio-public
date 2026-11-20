"""Migration 0124: confidence_scores dropped, regime_label_history created and backfilled."""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool

_PREVIOUS = "0123_broker_positions"
_REVISION = "0124_trust_ledgers"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_revision_chain_and_length():
    text = Path("alembic/versions/0124_trust_ledgers.py").read_text()
    assert f'revision = "{_REVISION}"' in text
    assert f'down_revision = "{_PREVIOUS}"' in text
    assert len(_REVISION) <= 32
    assert 'has_table("regime_label_history")' in text
    assert 'has_table("confidence_scores")' in text


def test_upgrade_backfills_history_and_downgrade_restores_confidence_scores():
    engine = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    cfg = _config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, _PREVIOUS)

        inspector = sa.inspect(conn)
        assert inspector.has_table("confidence_scores")
        assert inspector.has_table("regime_snapshots")
        assert not inspector.has_table("regime_label_history")

        conn.execute(
            sa.text(
                "INSERT INTO confidence_scores (id, portfolio_id, overall, historical_performance, "
                "live_tracking, risk_profile, regime_adaptability, factors_json, created_at) "
                "VALUES ('c1', 'p1', 50, 50, 50, 50, 50, '[]', '2026-09-01 00:00:00')"
            )
        )
        for n, key in enumerate(
            [
                "verification_benchmark_symbol",
                "verification_confidence_low",
                "verification_underperformance_threshold",
                "verification_drawdown_warning",  # stays
                "verification_concentration_single",  # stays
            ]
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO app_settings (id, key, value_json, updated_at) "
                    "VALUES (:id, :key, '1', '2026-09-01 00:00:00')"
                ),
                {"id": f"s{n}", "key": key},
            )
        snapshots = [
            ("2026-09-28 07:15:00", "bull", "jump", {"probs": {"bull": 0.7, "sideways": 0.2, "bear": 0.1}}),
            # Same UTC day, later run: the last snapshot of the day wins.
            ("2026-09-28 18:00:00", "bear", "hmm", {"probs": {"bull": 0.1, "bear": 0.9}}),
            ("2026-09-29 07:15:00", "sideways", "jump", {"probs": {"sideways": 0.6}}),
            ("2026-09-30 07:15:00", "bull", "jump", {}),  # no probabilities recorded
        ]
        for n, (ts, label, source, payload) in enumerate(snapshots, start=1):
            conn.execute(
                sa.text(
                    "INSERT INTO regime_snapshots (id, ts, label, score, source, payload_json) "
                    "VALUES (:id, :ts, :label, 0.5, :source, :payload)"
                ),
                {"id": n, "ts": ts, "label": label, "source": source, "payload": json.dumps(payload)},
            )

        command.upgrade(cfg, _REVISION)

        remaining = {r[0] for r in conn.execute(sa.text("SELECT key FROM app_settings WHERE key LIKE 'verification_%'"))}
        assert remaining == {"verification_drawdown_warning", "verification_concentration_single"}

        inspector = sa.inspect(conn)
        assert not inspector.has_table("confidence_scores")
        assert inspector.has_table("regime_label_history")
        index = next(i for i in inspector.get_indexes("regime_label_history") if i["name"] == "ix_regime_label_history_as_of")
        assert index["unique"] and index["column_names"] == ["as_of"]

        rows = conn.execute(
            sa.text("SELECT as_of, label, probabilities_json, model FROM regime_label_history ORDER BY as_of")
        ).fetchall()
        assert [str(r[0]) for r in rows] == ["2026-09-28", "2026-09-29", "2026-09-30"]
        assert [r[1] for r in rows] == ["bear", "sideways", "bull"]
        assert [r[3] for r in rows] == ["hmm", "jump", "jump"]
        assert json.loads(rows[0][2]) == {"bull": 0.1, "bear": 0.9}
        assert json.loads(rows[2][2]) == {}

        command.downgrade(cfg, _PREVIOUS)

        inspector = sa.inspect(conn)
        assert not inspector.has_table("regime_label_history")
        assert inspector.has_table("confidence_scores")
        columns = {c["name"] for c in inspector.get_columns("confidence_scores")}
        assert columns == {
            "id",
            "portfolio_id",
            "overall",
            "historical_performance",
            "live_tracking",
            "risk_profile",
            "regime_adaptability",
            "factors_json",
            "created_at",
        }
        index_names = {i["name"] for i in inspector.get_indexes("confidence_scores")}
        assert {"ix_confidence_scores_portfolio_created", "ix_confidence_scores_portfolio_id"} <= index_names

        # Re-running the upgrade after a downgrade is clean (history is rebuilt from snapshots).
        command.upgrade(cfg, _REVISION)
        assert conn.execute(sa.text("SELECT COUNT(*) FROM regime_label_history")).scalar() == 3
