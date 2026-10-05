"""Config registry CRUD for Discovery self-improvement loop (P3).

Provides ``get_or_seed_active_config`` as the primary entry point — callers
resolve their config once at pipeline start and use it for the duration of a
run.  Configs are versioned, immutable-after-activation records that let the
P3 loop compare challenger performance against the current champion.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.foundation.models.entities import DiscoveryConfig
from app.foundation.models.entities._core import now_utc, uuid_pk

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_WEIGHTS: dict[str, Any] = {
    # Advisor-loop PR1: the deterministic quant screen (momentum, risk,
    # benchmark-relative, portfolio fit) is load-bearing. The AlphaCrafter
    # ic_icir signal (the candidate's exposure to validated library factors)
    # is explicitly advisory — it drops out until such factors exist
    # (composite.ic_signal_usable). The candidate-
    # invariant "regime" signal was removed (2026-09-28, see composite.py).
    "weights": {
        "ic_icir": 0.10,
        "analyst": 0.05,
        "sentiment": 0.05,
        "portfolio": 0.15,
        # fundamentals/momentum/benchmark rebalanced by ADR 0017 (see
        # composite.WEIGHTS); migration 0112 carries the change to an
        # already-seeded, never-customised prod row.
        "fundamentals": 0.15,
        "momentum": 0.15,
        "risk": 0.10,
        "benchmark": 0.05,
        # Track D1c — advisory, cold-start-gated ML prediction signal;
        # mirrors composite.py's WEIGHTS exactly (kept in sync — this dict
        # seeds the persisted DiscoveryConfig row that
        # compute_weighted_composite's `weights=` override actually uses in
        # production, not composite.WEIGHTS directly).
        "ml_signal": 0.05,
        # Ticker-matched IBES estimate-revision / SEC insider-trading
        # signals — also mirrors composite.py's WEIGHTS exactly, same
        # sync obligation as ml_signal above.
        "estimate_revision": 0.05,
        "insider_signal": 0.05,
    },
    "threshold_ic_obs": 20,
}

DEFAULT_UNIVERSE_CONFIG: dict[str, Any] = {
    # Universe breadth (advisor-loop PR1): raise the cap well past the old ~60
    # large-cap-only set and lower the market-cap floor so small/mid-cap niche
    # names survive the fundamentals screen. Volume floor stays at 100k/day to
    # preserve tradeability.
    "universe_max_size": 300,
    "min_market_cap_eur": 250_000_000,
    "min_avg_volume": 100_000,
}

DEFAULT_PROMPT_TEMPLATE: dict[str, Any] = {
    "system_prompt": (
        "You are a quantitative research analyst covering equities, ETFs, bonds, and "
        "money-market/cash-equivalent instruments. Your task is to produce a research "
        "hypothesis as structured JSON — no tools, no chain-of-thought, no markdown.\n"
        "The input includes signal_breakdown.expected_return_anchor_pct, a BACKWARD-LOOKING "
        "ANNUALIZED trailing return (3-year annualized CAGR, 1-month annualized rate for "
        "money-market instruments, or 12-1 month momentum as fallback) — it describes what "
        "already happened, not a forecast, and it is always an ANNUAL rate regardless of "
        "horizon_months. The system computes the dossier's expected_return from this anchor "
        "deterministically — you do NOT produce that number yourself. Instead, your `thesis` "
        "must explicitly justify or critique the anchor as a forward-looking estimate: state "
        "whether the trailing figure is likely to persist over horizon_months, and why, given "
        "the qualitative context (fundamentals, sentiment, regime, portfolio fit). Treat a "
        "large trailing anchor (e.g. >25% annualized) as a rally that is unlikely to repeat at "
        "the same pace unless the thesis states a specific forward catalyst that justifies "
        "more. For money-market/cash-equivalent instruments (name contains e.g. 'overnight "
        "rate', 'money market', '€STR'/ESTR/EONIA/SONIA/SOFR) or any candidate whose own "
        "signals show near-zero volatility, the anchor already reflects the short-term policy "
        "rate the instrument is mechanically bound to — do not project equity-style momentum "
        "or catalysts onto it; direction should rarely be anything but neutral/long, and "
        "key_risks should reflect rate-path risk and issuer/counterparty risk, not liquidity "
        "or benchmark-underperformance framing borrowed from equities.\n"
        "Output exactly one JSON object matching this schema. The numbers below are "
        "illustrative placeholders only — compute your own value for each candidate, "
        "never copy them verbatim:\n"
        '{"direction": "long"|"short"|"neutral", '
        '"conviction": <float 0-1>, '
        '"horizon_months": <int 1-12>, '
        '"thesis": "1-3 sentences that justify or critique signal_breakdown.expected_return_anchor_pct '
        'as a forward estimate over horizon_months", '
        '"key_risks": ["risk1", "risk2"]}\n'
        "Do not include any text outside the JSON object."
    ),
    "temperature": 0.2,
    "max_tokens": 4096,
}


def _auto_version(config_json: dict[str, Any]) -> str:
    """Generate a short version label from a sha256 digest of the config JSON."""
    raw = json.dumps(config_json, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


CODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "signal_weights": DEFAULT_SIGNAL_WEIGHTS,
    "prompt_template": DEFAULT_PROMPT_TEMPLATE,
    "universe": DEFAULT_UNIVERSE_CONFIG,
}


def code_default_hash(config_type: str) -> str | None:
    """Hash of the current code-level default for *config_type*, or ``None``
    if this config_type has no code default (e.g. a custom manual type)."""
    default = CODE_DEFAULTS.get(config_type)
    return _auto_version(default) if default is not None else None


def is_config_stale(cfg: DiscoveryConfig) -> bool:
    """True when the code default for ``cfg.config_type`` has changed since
    this row was seeded from it or last approved against it at activation
    (see ``activate_config``). A row that has never been checked against a
    code default (``code_default_hash`` is ``None``, e.g. a manual challenger
    never promoted) is not flagged — there's nothing to compare drift against
    yet, and every fresh challenger would otherwise render as stale.
    """
    if cfg.code_default_hash is None:
        return False
    current = code_default_hash(cfg.config_type)
    return current is not None and current != cfg.code_default_hash


def _seed_default_configs(db: Session) -> None:
    """Create system-seeded default configs if none exist."""
    existing = db.query(DiscoveryConfig).filter(DiscoveryConfig.source == "system").first()
    if existing is not None:
        return

    seed_rows = [
        DiscoveryConfig(
            id=uuid_pk(),
            config_type="signal_weights",
            version_label=_auto_version(DEFAULT_SIGNAL_WEIGHTS),
            description="System-default signal weights (IC/ICIR-heavy)",
            config_json=DEFAULT_SIGNAL_WEIGHTS,
            status="active",
            source="system",
            created_at=now_utc(),
            code_default_hash=code_default_hash("signal_weights"),
        ),
        DiscoveryConfig(
            id=uuid_pk(),
            config_type="prompt_template",
            version_label=_auto_version(DEFAULT_PROMPT_TEMPLATE),
            description="System-default LLM prompt template",
            config_json=DEFAULT_PROMPT_TEMPLATE,
            status="active",
            source="system",
            created_at=now_utc(),
            code_default_hash=code_default_hash("prompt_template"),
        ),
    ]
    for row in seed_rows:
        db.add(row)
    db.commit()
    logger.info("Seeded %d default DiscoveryConfig rows", len(seed_rows))


def get_active_config(db: Session, config_type: str) -> DiscoveryConfig | None:
    """Return the active config for *config_type*, or ``None``."""
    return (
        db.query(DiscoveryConfig)
        .filter(
            DiscoveryConfig.config_type == config_type,
            DiscoveryConfig.status == "active",
        )
        .first()
    )


def get_or_seed_active_config(db: Session, config_type: str) -> DiscoveryConfig:
    """Return the active config, seeding defaults on first access.

    Guaranteed to return a row — if no active config exists after seeding, a
    new one is created from the matching default constant.
    """
    cfg = get_active_config(db, config_type)
    if cfg is not None:
        return cfg

    _seed_default_configs(db)
    cfg = get_active_config(db, config_type)
    if cfg is not None:
        return cfg

    defaults = {
        "signal_weights": DEFAULT_SIGNAL_WEIGHTS,
        "prompt_template": DEFAULT_PROMPT_TEMPLATE,
        "universe": DEFAULT_UNIVERSE_CONFIG,
    }
    payload = defaults.get(config_type)
    if payload is None:
        raise ValueError(f"No default config known for config_type={config_type!r}")

    cfg = DiscoveryConfig(
        id=uuid_pk(),
        config_type=config_type,
        version_label=_auto_version(payload),
        description=f"Fallback default for {config_type}",
        config_json=payload,
        status="active",
        source="system",
        created_at=now_utc(),
        code_default_hash=code_default_hash(config_type),
    )
    db.add(cfg)
    db.commit()
    logger.info("Created fallback active config for %s", config_type)
    return cfg


def sync_config_to_default(db: Session, config_id: str) -> DiscoveryConfig:
    """Overwrite *config_id*'s payload with the current code-level default.

    Unlike ``activate_config``'s implicit approval (which only refreshes
    ``code_default_hash`` for a challenger being promoted, since a challenger
    is deliberately different from the default), this actually pulls the
    current code default's content into the row — the correct fix when a
    system-sourced config has drifted because the code default changed
    underneath it (stale badge with no other way to clear it short of a
    manual DB edit).

    Args:
        db: Database session.
        config_id: The config row to sync.

    Returns:
        The updated config, with ``config_json``/``version_label`` matching
        the code default and ``code_default_hash`` refreshed so the row no
        longer reads as stale.
    """
    cfg = db.get(DiscoveryConfig, config_id)
    if cfg is None:
        raise ValueError(f"DiscoveryConfig {config_id} not found")

    default = CODE_DEFAULTS.get(cfg.config_type)
    if default is None:
        raise ValueError(f"No code default known for config_type={cfg.config_type!r}")

    cfg.config_json = default
    cfg.version_label = _auto_version(default)
    cfg.code_default_hash = _auto_version(default)
    db.commit()
    db.refresh(cfg)
    logger.info("Synced config %s (type=%s) to current code default", cfg.id, cfg.config_type)
    return cfg


def get_champion_config(db: Session, config_type: str) -> DiscoveryConfig | None:
    """Return the most recent champion config for *config_type*, or ``None``."""
    return (
        db.query(DiscoveryConfig)
        .filter(
            DiscoveryConfig.config_type == config_type,
            DiscoveryConfig.status == "champion",
        )
        .order_by(DiscoveryConfig.champion_at.desc())
        .first()
    )


def get_config(db: Session, config_id: str) -> DiscoveryConfig | None:
    """Return a single config by its primary key."""
    return db.get(DiscoveryConfig, config_id)


def list_configs(
    db: Session,
    config_type: str | None = None,
    status: str | None = None,
) -> list[DiscoveryConfig]:
    """List configs, optionally filtered by type and/or status."""
    q = db.query(DiscoveryConfig)
    if config_type is not None:
        q = q.filter(DiscoveryConfig.config_type == config_type)
    if status is not None:
        q = q.filter(DiscoveryConfig.status == status)
    return q.order_by(DiscoveryConfig.created_at.desc()).all()


def create_config(
    db: Session,
    config_type: str,
    config_json: dict[str, Any],
    version_label: str | None = None,
    description: str | None = None,
    source: str = "manual",
    parent_config_id: str | None = None,
) -> DiscoveryConfig:
    """Create a new challenger config.

    Args:
        db: Database session.
        config_type: ``"signal_weights"`` or ``"prompt_template"``.
        config_json: The full config payload.
        version_label: Optional short label.  Auto-generated from sha256 of
            ``config_json`` if not provided.
        description: Optional human-readable description.
        source: Origin of the config (default ``"manual"``).
        parent_config_id: Optional parent config id.

    Returns:
        The newly created ``DiscoveryConfig`` (status ``"challenger"``).
    """
    if version_label is None:
        version_label = _auto_version(config_json)

    cfg = DiscoveryConfig(
        id=uuid_pk(),
        config_type=config_type,
        version_label=version_label,
        description=description,
        config_json=config_json,
        status="challenger",
        source=source,
        parent_config_id=parent_config_id,
        created_at=now_utc(),
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    logger.info("Created config %s (%s, type=%s)", cfg.id, version_label, config_type)
    return cfg


def activate_config(db: Session, config_id: str) -> DiscoveryConfig:
    """Promote a challenger config to active.

    * The current active config (same ``config_type``) is demoted to
      ``"champion"``.
    * The previous champion is set to ``"retired"``.
    * The target config is set to ``"active"``.

    Args:
        db: Database session.
        config_id: The challenger config to activate.

    Returns:
        The promoted config.
    """
    cfg = db.get(DiscoveryConfig, config_id)
    if cfg is None:
        raise ValueError(f"DiscoveryConfig {config_id} not found")
    if cfg.status != "challenger":
        raise ValueError(
            f"DiscoveryConfig {config_id} cannot be activated (status={cfg.status!r}); only challengers may be promoted"
        )

    now_val = now_utc()
    same_type = cfg.config_type

    current_active = get_active_config(db, same_type)
    current_champion = get_champion_config(db, same_type)

    if current_champion is not None and current_champion.id != cfg.id:
        current_champion.status = "retired"

    if current_active is not None and current_active.id != cfg.id:
        current_active.status = "champion"
        current_active.champion_at = now_val

    cfg.status = "active"
    cfg.champion_at = now_val
    # Promotion is a human/automated review decision — treat it as an
    # implicit approval against whatever the code default looks like right
    # now, clearing any staleness badge this row would otherwise carry.
    cfg.code_default_hash = code_default_hash(same_type)
    db.commit()
    db.refresh(cfg)
    logger.info(
        "Activated config %s (%s, type=%s); previous active → champion",
        cfg.id,
        cfg.version_label,
        same_type,
    )
    return cfg
