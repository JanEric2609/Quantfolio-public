import copy
import itertools
import json
import logging
import os
import threading
import time
import weakref
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, object_session

from app.foundation.core.security import SecretBox
from app.foundation.models.entities import ApiKey, AppSetting

logger = logging.getLogger(__name__)

DEFAULT_PUBLIC_SETTINGS: dict[str, Any] = {
    "currency": "EUR",
    "dkb_fints_url": "https://fints.dkb.de/fints",
    "dkb_blz": "12030000",
    "dkb_provider": "fints",
    "dkb_tan_security_function": None,
    "dkb_tan_medium": None,
    "dkb_product_id": None,
    "dkb_push_timeout_seconds": 120,
    "dkb_poll_interval_seconds": 5,
    "dkb_debug_fints_logging": False,
    # Scalable Capital, read-only through the official sc CLI (foundation/scalable).
    "scalable_enabled": False,
    "scalable_portfolio_id": "",
    "scalable_sync_hours": 6,
    "scalable_cli_user": "scalable-cli-user",
    "scalable_wrapper_path": "/usr/local/libexec/quantfolio-sc-ro",
    "scalable_binary_sha256": "",
    "scalable_timeout_seconds": 60,
    "scalable_require_read_only_guard": True,
    "frontend_origin": "http://localhost:5173",
    "webauthn_rp_id": "localhost",
    "webauthn_rp_name": "QuantFolio",
    "llm_base_url": "http://127.0.0.1:8080/v1",
    "llm_model": "qwen3.5-9b",
    "llm_cloud_fallback_enabled": False,
    "anthropic_model": "claude-opus-4-8",
    "openai_model": "gpt-4o",
    "openbb_enabled": False,
    "openbb_api_url": "http://127.0.0.1:6900",
    "openbb_default_provider": "yfinance",
    "obsidian_enabled": False,
    "obsidian_rest_url": "http://127.0.0.1:27123",
    "prompt_raw_retention_hours": 24,
    "prompt_delete_after_days": 14,
    "scheduled_recommendation_refresh": True,
    "scheduled_quant_experiment_refresh": True,
    "tax_residency_country": "DE",
    "church_tax": "none",
    "freistellungsauftrag_amount": 1000,
    "freistellungsauftrag_used": 0,
    # The Freistellungsauftrag on file at each synced bank (EUR; None = not entered).
    # Each bank applies only its own (Sec. 44a EStG); the Tax Cockpit plans the split.
    "freistellungsauftrag_dkb_eur": None,
    "freistellungsauftrag_scalable_eur": None,
    # Total of the orders on file at banks other than DKB and Scalable (None = none
    # entered; then the allowance used elsewhere stands in for it).
    "freistellungsauftrag_other_banks_eur": None,
    # DKB Tagesgeld interest rate (fraction), for projecting the year's interest.
    "tax_interest_rate_dkb": None,
    "tax_estimation_enabled": True,
    "tax_spouse_allowance": False,
    # Nichtveranlagungsbescheinigung (NV certificate): banks withhold no tax,
    # and the tax cockpit suggests tax-free gain harvesting within the room.
    "tax_nv_certificate": False,
    "tax_nv_valid_until": None,
    # Which banks have a copy of it: a bank without one still withholds tax.
    # None = not answered yet (then every bank is assumed to have one, and the
    # Tax Cockpit asks); False = that bank has no copy.
    "tax_nv_filed_dkb": None,
    "tax_nv_filed_scalable": None,
    "tax_other_income_eur": 0,
    # "de_family" (German Familienversicherung: income limit applies),
    # "de_own", "foreign" (e.g. Austrian ÖGK), or "unknown".
    "tax_health_insurance": "unknown",
    "tax_bafoeg": False,
    # Regime model (ADR 0004 amendment, 2026-09-28): the jump model is live,
    # the HMM (regime_model_id) stays fitted as its fallback. Defaults mirror
    # lab/regime/classifier.py DEFAULT_MODEL_KIND / _JUMP_PENALTY / _JUMP_MODEL_ID.
    "regime_model_kind": "jump",
    "regime_jump_penalty": 10.0,
    "regime_jump_model_id": "regime_jump",
    "regime_model_id": "regime_hmm",
    "regime_index_symbol": "^STOXX50E",
    "regime_crisis_vix_threshold": 30.0,
    "regime_crisis_credit_spread_threshold": 3.0,
    "regime_crisis_drawdown_threshold": -0.15,
    # Index-bar freshness guard (DEC-G skip-write on persistent staleness):
    # classification/refit refuse windows whose newest bar is older than this
    # many calendar days; one bounded backfill of regime_index_symbol is
    # attempted first. Guards against frozen snapshots when index bars go
    # unrefreshed (production served 23 identical 'sideways' rows).
    "regime_max_bar_age_days": 7,
    # Regime-snapshot staleness TTL in hours (audit/universe-regime): snapshots
    # older than this are flagged stale by /api/data/regime/current so a dead
    # pipeline cannot masquerade as live analysis. Distinct from
    # regime_max_bar_age_days (bar-vintage tolerance, owned elsewhere).
    "regime_stale_ttl_hours": 72,
    # AlphaCrafter (experimental factor pipeline). Basket re-targeted to liquid
    # accumulating UCITS ETFs (audit-fixes-2026-08 todo 21; owner-adjustable —
    # a stored DB row still wins, see docs/archive/runbooks/post-audit-ops.md §7).
    # Empty: the miner's built-in Euro Stoxx 50 + S&P 100 cross-section
    # (lab/alphacrafter/universe.py). A basket under 30 names is ignored.
    "alphacrafter_index_basket": "",
    # SEC fair access asks automated clients to name themselves and a
    # contact, e.g. "Jane Doe jane@example.com" (data_engineering.
    # sec_edgar_insider). Empty: a generic Quantfolio agent string.
    "sec_edgar_user_agent": "",
    "alphacrafter_ic_horizon_days": 5,
    "alphacrafter_min_ic": 0.02,
    "alphacrafter_min_icir": 0.3,
    "alphacrafter_propose_llm": False,
    # Trader sweep parameters (audit-fixes-2026-08 todo 18). CSV/int strings
    # parsed at consumption; defaults EXACTLY equal the former hardcoded
    # fallbacks in services/alphacrafter/trader.py so untouched installs
    # sweep the identical grid.
    "alphacrafter_rebalance_freqs": "W,M",
    "alphacrafter_position_sizes": "0.05,0.1,0.2",
    "alphacrafter_commissions": "0.001",
    "alphacrafter_max_positions": "5",
    # Quant factor cache directory (defaults to /tmp/quantfolio_ff_cache).
    "quant_factors_ff_cache_dir": "/tmp/quantfolio_ff_cache",
    # PIT Parquet panel directory (ADR 0015 Phase 3), defaults to /tmp/quantfolio_parquet_panel.
    "parquet_panel_dir": "/tmp/quantfolio_parquet_panel",
    # DuckDB resources for the PIT-join Parquet reads (_pit_duckdb.py). Empty
    # temp dir = DuckDB's own default.
    "duckdb_memory_limit": "2GB",
    "duckdb_threads": "2",
    "duckdb_temp_directory": "",
    # Quant Lab signal batch (ADR 0015 Phase 4): IC/ICIR gate thresholds mirror
    # alphacrafter_min_ic/_min_icir's defaults, and the Parquet output directory.
    "quant_lab_min_ic": 0.02,
    "quant_lab_min_icir": 0.3,
    "quant_lab_signal_batch_dir": "/tmp/quantfolio_signal_batch",
    # ISIN → ticker overrides for single-asset / DKB ISIN resolution.
    "isin_ticker_overrides": {},
    # ISIN -> index key for ETF currency look-through beyond the curated
    # list in foundation.etf_currency.ETF_INDEX (e.g. {"IE00...": "msci_world"}).
    "etf_index_map": {},
    # RSS feed URLs (comma-separated). Empty string = use built-in defaults.
    "rss_feed_urls": "",
    # Market data provider priority chain (JSON array of provider names).
    # yfinance ahead of openbb (audit-fixes-2026-08 todo 6, OpenBB per-purpose
    # routing): quotes/history/fundamentals must keep resolving via yfinance
    # first even once openbb_enabled is turned on for its macro-indicators
    # capability — openbb only sees quote/history/fundamentals traffic as a
    # fallback if yfinance itself fails. justetf ahead of twelvedata/tiingo so
    # non-US listings resolve before the paid providers that 404 on them
    # (audit-fixes-2026-08 todo 5).
    "provider_chain_json": json.dumps([
        "finnhub", "yfinance", "openbb", "alpaca", "databento", "justetf",
        "twelvedata", "tiingo", "alphavantage", "fred", "eod", "ecb_sdw", "massive"
    ]),
    # Bar-history vintage tolerance in calendar days (audit F16/F21): newest bar
    # older than this flags market.history rows stale. Clamped to [1, 30].
    "history_stale_days": 7,
    # SMTP email delivery for nudge/alert notifications.
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_use_tls": True,
    "smtp_from_address": "noreply@quantfolio.local",
    "smtp_username": "",
    # Refuse to send (and to log in) over an unencrypted SMTP connection unless
    # this is explicitly true — a plaintext hop exposes the password and the mail.
    "smtp_allow_plaintext": False,
    # How the Telegram bot receives messages: "polling" (the worker long-polls
    # getUpdates; no public URL needed) or "webhook" (Telegram calls the API).
    "telegram_mode": "polling",
    # Investor profile (Discover / LLM Portfolio).
    # The monthly amount is ``monthly_contribution_eur`` (one number for the
    # plan and the LLM portfolio context; migration 0121 dropped the old
    # ``discover_investor_monthly_budget`` duplicate).
    "discover_investor_horizon": "medium",
    "discover_investor_risk_appetite": "medium",
    "discover_investor_exclusions": "",
    # Recency-penalty decay window (audit fix — same-ticker re-recommendation):
    # exponential-decay half-life in days for the composite-score penalty
    # applied to individual-stock candidates re-suggested shortly after a
    # prior recommendation (pipeline.py:_apply_recency_penalty). ETFs and
    # strong-conviction candidates are exempt regardless of this value.
    "discover_recency_penalty_halflife_days": 30,
    # Expected-return methodology rollback switch (M5 ladder, blueprint
    # docs/archive/audits/2026-08-discover-maths/blueprint_appendix.md). Allowed values:
    # "trailing" (current linear-dampened trailing anchor — the only mode with
    # behaviour today), "blocks" and "bl" are FUTURE modes (M5 Options 1/2,
    # building-blocks / Black-Litterman) registered here so flipping the key
    # rolls back without a deploy. Unknown/missing values normalize to
    # "trailing" in the dispatcher (dossier_writer._resolve_expected_return).
    "er_mode": "trailing",
    # Portfolio -> Risk alert thresholds (audit todo 11): defaults EXACTLY equal
    # the code fallbacks in decision/verification/alerts.py. The underperformance,
    # volatility, confidence and benchmark settings went with the confidence
    # score (migration 0124_trust_ledgers deletes any stored values).
    "verification_drawdown_warning": -0.10,
    "verification_drawdown_critical": -0.20,
    "verification_concentration_single": 0.30,
    "verification_concentration_top3": 0.60,
    # ADR 0015 Phase 2 ("passive core" silent period): while no strategy
    # clears the 5-condition graduation gate, real-money guidance defaults to
    # a single global equity ETF rather than stock-level picks. A manual kill
    # switch independent of the statistical gate — set to "live" only once a
    # champion has actually graduated (evaluate_graduation still gates
    # /api/graduation/recommendations regardless of this value).
    "decision_loop_mode": "passive_core",
    # iShares Core MSCI World (XETRA, EUR; same fund as IWDA) — the usual core
    # holding, so following it needs no taxable disposal/repurchase versus
    # switching ETFs (ADR 0015 ruling #23). The XETRA line is the EUR quote (DEC-A).
    "passive_core_ticker": "EUNL.DE",
    # Benchmark of every risk and performance figure: MSCI World, net total
    # return, EUR, unhedged (iShares Core MSCI World on Xetra).
    "benchmark_ticker": "EUNL.DE",
    # Monte Carlo planner (foundation/wealth_planner.py): a published long-run
    # real compound return, with where it comes from and its date.
    "mc_cma_real_return": 0.042,
    "mc_cma_source": "AQR Capital Market Assumptions 2026, global developed equities, real compound return",
    "mc_cma_as_of": "2025-12-31",
    "mc_goal_eur": None,
    "passive_core_isin": "IE00B4L5Y983",
    # "This month" plan (decision/monthly_plan.py). Contribution-first
    # allocation across core / factor tilt / stock picks; the caps are the
    # owner's "tight" tracking-error budget (audit 2026-09-24, section 7).
    "monthly_contribution_eur": 1000,
    # How new money is invested and where (decision/monthly_plan.py).
    "plan_contribution_mode": "savings_plan",
    # "auto" = the cheapest connected broker for each buy (Scalable when its
    # depot is synced, DKB otherwise).
    "plan_broker": "auto",
    "plan_tilt_max_pct": 15,
    "plan_satellite_max_pct": 10,
    "plan_min_order_eur": 1000,
    # Sell only when an unlocked sleeve sits this many percentage points above
    # its target and a year of contributions cannot bring it back (Phase 4).
    "plan_drift_band_pp": 5,
    "plan_core_isins": "",
    "plan_tilt_isins": "",
    # Fixed cash kept aside (Tagesgeld and broker cash), never suggested for
    # investing. 0 = not set: the plan then suggests nothing from cash.
    "emergency_reserve_eur": 0,
}


SENSITIVE_INTEGRATIONS = {
    "alphavantage", "finnhub", "fred", "eod", "obsidian", "dkb", "smtp",
    "llm", "anthropic", "openai", "massive",
    "twelvedata", "databento", "alpaca", "tiingo", "telegram",
}


# ---------------------------------------------------------------------------
# Read cache
# ---------------------------------------------------------------------------
# ``get_public_settings`` used to SELECT the whole ``app_settings`` table on
# every call (~60 call sites, several per request). The raw ``{key: value_json}``
# rows are now cached per database engine for a few seconds:
#
# * Every write path in this module invalidates the cache after its commit, and
#   an ORM-level hook invalidates it after *any* commit that flushed an
#   ``AppSetting`` (other modules and tests write the table directly), so a
#   write is visible to the next read in the same process at once.
# * The TTL is what lets the API and worker processes converge on each other's
#   writes (the worker heartbeat, a setting changed in the Control Center).
# * The cache holds JSON *strings* and every read re-parses them, so a caller
#   can never mutate the cached state through a returned dict or list.
# * A session that has flushed or queued ``AppSetting`` changes in its open
#   transaction bypasses the cache: it must read its own uncommitted writes and
#   must not publish them to other sessions.
# * The cache is keyed on the engine (weakly), never a global, so each test's
#   in-memory database gets its own entry.
SETTINGS_CACHE_TTL_SECONDS = 5.0

_SETTINGS_DIRTY_KEY = "_app_settings_dirty"


class _SettingsCacheEntry:
    __slots__ = ("rows", "loaded_at")

    def __init__(self, rows: dict[str, str], loaded_at: float) -> None:
        self.rows = rows
        self.loaded_at = loaded_at


_settings_cache: "weakref.WeakKeyDictionary[Engine, _SettingsCacheEntry]" = weakref.WeakKeyDictionary()
_settings_cache_lock = threading.Lock()
# Bumped by every invalidation, so a load that raced a write is not stored.
_settings_generation = 0


def _now() -> float:
    return time.monotonic()


def settings_cache_generation() -> int:
    """Counter that changes whenever the settings cache is invalidated.

    Lets derived caches (e.g. the CORS origin list) notice a settings write
    in this process without waiting for their own TTL.
    """
    return _settings_generation


def invalidate_settings_cache() -> None:
    """Drop every cached settings table. Call after any write to ``app_settings``."""
    global _settings_generation
    with _settings_cache_lock:
        _settings_generation += 1
        _settings_cache.clear()


def _engine_for(db: Session) -> Engine | None:
    try:
        bind = db.get_bind()
    except Exception:
        return None
    if isinstance(bind, Connection):
        return bind.engine
    return bind if isinstance(bind, Engine) else None


def _has_pending_settings_changes(db: Session) -> bool:
    if db.info.get(_SETTINGS_DIRTY_KEY):
        return True
    return any(
        isinstance(obj, AppSetting)
        for obj in itertools.chain(db.new, db.dirty, db.deleted)
    )


def _load_settings_rows(db: Session) -> dict[str, str]:
    return {key: value_json for key, value_json in db.query(AppSetting.key, AppSetting.value_json)}


def _settings_rows(db: Session) -> dict[str, str]:
    """Raw ``{key: value_json}`` for the whole table; the caller must not mutate it."""
    engine = _engine_for(db)
    if engine is None or _has_pending_settings_changes(db):
        return _load_settings_rows(db)
    now = _now()
    with _settings_cache_lock:
        entry = _settings_cache.get(engine)
        generation = _settings_generation
    if entry is not None and now - entry.loaded_at < SETTINGS_CACHE_TTL_SECONDS:
        return entry.rows
    rows = _load_settings_rows(db)
    with _settings_cache_lock:
        if generation == _settings_generation:
            _settings_cache[engine] = _SettingsCacheEntry(rows, now)
    return rows


@event.listens_for(AppSetting, "after_insert")
@event.listens_for(AppSetting, "after_update")
@event.listens_for(AppSetting, "after_delete")
def _mark_settings_session_dirty(_mapper: Any, _connection: Any, target: AppSetting) -> None:
    session = object_session(target)
    if session is not None:
        session.info[_SETTINGS_DIRTY_KEY] = True


@event.listens_for(Session, "after_commit")
def _invalidate_after_settings_commit(session: Session) -> None:
    if session.info.pop(_SETTINGS_DIRTY_KEY, False):
        invalidate_settings_cache()


@event.listens_for(Session, "after_soft_rollback")
def _forget_settings_dirty_on_rollback(session: Session, previous_transaction: Any) -> None:
    if getattr(previous_transaction, "parent", None) is None:
        session.info.pop(_SETTINGS_DIRTY_KEY, None)


def get_public_settings(db: Session) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_PUBLIC_SETTINGS)
    for key, value_json in _settings_rows(db).items():
        result[key] = json.loads(value_json)
    return _normalize_public_settings(result)


def _normalize_public_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Apply catalog value normalizers so legacy stored forms read canonically."""
    from app.foundation.settings_catalog import normalize_church_tax

    if "church_tax" in values:
        values["church_tax"] = normalize_church_tax(values["church_tax"])
    return values


def get_setting_row(db: Session, key: str) -> Any | None:
    """Return the parsed JSON value stored for *key*, or None if not found."""
    value_json = _settings_rows(db).get(key)
    if value_json is None:
        return None
    return json.loads(value_json)


def resolve_setting(db: Session, key: str, env_var: str | None, default: str) -> str:
    """
    Resolve a setting with three-tier precedence:
    1. Explicit DB row (if present and non-empty)
    2. Environment variable (if provided and set)
    3. Built-in default
    """
    db_value = get_setting_row(db, key)
    if db_value is not None and str(db_value):
        return str(db_value)

    if env_var and env_var in os.environ:
        env_value = os.environ[env_var]
        if env_value:
            return env_value

    return default


def normalize_base_url(url: str) -> str:
    """Strip trailing /; append /v1 if not already present."""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def probe_root(url: str) -> str:
    """Strip trailing /; remove /v1 suffix if present."""
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def resolve_llm_base_url(db: Session) -> str:
    """Resolve LLM base URL with /v1 normalization."""
    return normalize_base_url(
        resolve_setting(db, "llm_base_url", "LLM_LOCAL_URL", "http://127.0.0.1:8080/v1")
    )


def resolve_llm_api_key(db: Session | None = None) -> str | None:
    """Bearer key for a llama-server started with ``--api-key`` / ``LLAMA_API_KEY``.

    The Local LLM credential saved in Control Center wins (the setup wizard stores
    the placeholder ``local``, which is not a key), then ``LLM_API_KEY`` from the
    environment, which is also what code paths without a database session use.
    ``None`` means the server is unauthenticated.
    """
    if db is not None:
        try:
            saved = get_secret(db, "llm")
        except Exception:
            saved = None
        if saved and saved[0].strip() and saved[0].strip() != "local":
            return saved[0].strip()
    return os.environ.get("LLM_API_KEY", "").strip() or None


def llm_auth_headers(db: Session | None = None) -> dict[str, str]:
    """``Authorization`` header for direct llama-server calls; empty when no key is configured."""
    key = resolve_llm_api_key(db)
    return {"Authorization": f"Bearer {key}"} if key else {}


def resolve_llm_model(db: Session) -> str:
    """Resolve LLM model identifier."""
    return resolve_setting(db, "llm_model", "LLM_MODEL", "qwen3.5-9b")


def resolve_openbb_url(db: Session) -> str:
    """Resolve OpenBB API URL."""
    return resolve_setting(db, "openbb_api_url", "OPENBB_API_URL", "http://127.0.0.1:6900")


def resolve_openbb_default_provider(db: Session) -> str:
    """Resolve the ODP default provider name (DB → env → 'yfinance')."""
    return resolve_setting(db, "openbb_default_provider", "OPENBB_DEFAULT_PROVIDER", "yfinance")


def resolve_obsidian_url(db: Session) -> str:
    """Resolve Obsidian REST API URL."""
    return resolve_setting(db, "obsidian_rest_url", "OBSIDIAN_REST_URL", "http://127.0.0.1:27123")


# Approximate €STR level as of plan authorship — used ONLY when the daily
# ECB SDW refresh job (app/worker.py:register_risk_free_rate_refresh_job)
# has never successfully populated the "risk_free_rate_pct" cache. Every
# risk-adjusted metric in this codebase (Sharpe, Sortino, alpha, Treynor,
# skfolio's MAXIMIZE_RATIO objective) is silently wrong if the risk-free
# rate is 0.0 — it isn't, and treating it as such makes low-vol/cash-like
# instruments look artificially attractive on a risk-adjusted basis (F12/F13).
FALLBACK_RISK_FREE_RATE = 0.022


def get_risk_free_rate(db: Session) -> float:
    """Annualised risk-free rate as a fraction (e.g. ``0.022`` == 2.2%).

    Three-tier resolution: DB cache (refreshed daily from ECB SDW €STR) →
    hardcoded fallback. Deliberately has no env-var tier and never returns
    0.0 — unlike ``resolve_setting``, a missing/empty rate here is not a
    reasonable default, it's a correctness bug (see FALLBACK_RISK_FREE_RATE).
    """
    cached = get_setting_row(db, "risk_free_rate_pct")
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    return FALLBACK_RISK_FREE_RATE


def resolve_frontend_origins(db: Session) -> list[str]:
    """
    Resolve frontend origin CSV, split on comma, strip whitespace, drop empties.
    Returns a list of origin strings.
    """
    value = resolve_setting(db, "frontend_origin", "FRONTEND_ORIGIN", "http://localhost:5173")
    origins = [origin.strip() for origin in value.split(",")]
    return [origin for origin in origins if origin]


def upsert_public_settings(db: Session, values: dict[str, Any]) -> dict[str, Any]:
    from app.foundation.settings_catalog import normalize_church_tax

    for key, value in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
        if value is None:
            # A cleared field: drop the override so the key falls back to its
            # default rather than storing JSON null over it.
            if row is not None:
                db.delete(row)
            continue
        if key == "church_tax":
            value = normalize_church_tax(value)
        payload = json.dumps(value)
        if row is None:
            db.add(AppSetting(key=key, value_json=payload))
        else:
            row.value_json = payload
    db.commit()
    invalidate_settings_cache()
    return get_public_settings(db)


def delete_public_setting(db: Session, key: str) -> None:
    """Delete a plaintext AppSetting row if it exists (e.g. a legacy
    ``dkb_username`` mirror that should only live in encrypted meta)."""
    row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
    if row is not None:
        db.delete(row)
        db.commit()
        invalidate_settings_cache()


def set_secret(db: Session, service: str, secret: str, meta: dict[str, Any] | None = None) -> None:
    if service not in SENSITIVE_INTEGRATIONS:
        raise ValueError(f"Unknown integration service: {service}")
    box = SecretBox.from_settings()
    row = db.query(ApiKey).filter(ApiKey.service == service).one_or_none()
    public_meta, sensitive_updates = _split_sensitive_meta(service, meta or {})
    sensitive = _merged_meta_values(_stored_sensitive_meta(service, row, box), sensitive_updates)
    encrypted = box.encrypt(_pack_secret(service, secret, sensitive))
    merged_meta = _strip_sensitive_meta(service, _merged_meta(row, public_meta))
    if row is None:
        db.add(ApiKey(service=service, key_encrypted=encrypted, meta_json=json.dumps(merged_meta)))
    else:
        row.key_encrypted = encrypted
        row.meta_json = json.dumps(merged_meta)
    db.commit()


def update_secret_meta(db: Session, service: str, meta: dict[str, Any]) -> None:
    if service not in SENSITIVE_INTEGRATIONS:
        raise ValueError(f"Unknown integration service: {service}")
    row = db.query(ApiKey).filter(ApiKey.service == service).one_or_none()
    if row is None:
        return
    public_meta, sensitive_updates = _split_sensitive_meta(service, meta)
    if sensitive_updates:
        box = SecretBox.from_settings()
        key, _stored = _unpack_secret(service, box.decrypt(row.key_encrypted))
        sensitive = _merged_meta_values(_stored_sensitive_meta(service, row, box), sensitive_updates)
        row.key_encrypted = box.encrypt(_pack_secret(service, key, sensitive))
    row.meta_json = json.dumps(_strip_sensitive_meta(service, _merged_meta(row, public_meta)))
    db.commit()


def get_secret(db: Session, service: str) -> tuple[str, dict[str, Any]] | None:
    """Return ``(credential, meta)``; sensitive meta is decrypted into ``meta``.

    A plaintext copy of a now-sensitive meta key (written before it moved into
    the encrypted value, e.g. the DKB username) is folded into the encrypted
    payload here, so an upgraded install converges on its first read.
    """
    row = db.query(ApiKey).filter(ApiKey.service == service).one_or_none()
    if row is None:
        return None
    box = SecretBox.from_settings()
    key, sensitive = _unpack_secret(service, box.decrypt(row.key_encrypted))
    meta = json.loads(row.meta_json or "{}")
    legacy = {k: v for k, v in meta.items() if k in _sensitive_meta_keys(service)}
    if legacy:
        sensitive = {**legacy, **sensitive}
        public_meta = _strip_sensitive_meta(service, meta)
        _encrypt_legacy_meta(db, service, row, box, key, sensitive, public_meta)
        meta = public_meta
    return key, {**meta, **sensitive}


def _encrypt_legacy_meta(
    db: Session,
    service: str,
    row: ApiKey,
    box: SecretBox,
    key: str,
    sensitive: dict[str, Any],
    public_meta: dict[str, Any],
) -> None:
    """Move plaintext sensitive meta into the encrypted value in one commit.

    The encrypted copy is built before the plaintext is dropped and both change
    in the same transaction, so a failure leaves the legacy row exactly as it was.
    """
    try:
        row.key_encrypted = box.encrypt(_pack_secret(service, key, sensitive))
        row.meta_json = json.dumps(public_meta)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not move %s credential meta into the encrypted value", service, exc_info=True)


# -- Sensitive meta ----------------------------------------------------------
# A connection's meta field marked ``sensitive`` in the catalog (Alpaca's
# secret key) is a credential: it is stored inside the encrypted value as
# JSON {"key": <credential>, <meta_key>: <value>, ...}, never in meta_json.
# get_secret unpacks it back into meta, so consumers read it unchanged.


def _sensitive_meta_keys(service: str) -> set[str]:
    from app.foundation.settings_catalog import sensitive_meta_keys

    return sensitive_meta_keys(service)


def _split_sensitive_meta(service: str, meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    keys = _sensitive_meta_keys(service)
    public = {k: v for k, v in meta.items() if k not in keys}
    sensitive = {k: v for k, v in meta.items() if k in keys}
    return public, sensitive


def _strip_sensitive_meta(service: str, meta: dict[str, Any]) -> dict[str, Any]:
    keys = _sensitive_meta_keys(service)
    return {k: v for k, v in meta.items() if k not in keys}


def _stored_sensitive_meta(service: str, row: ApiKey | None, box: SecretBox) -> dict[str, Any]:
    """Sensitive meta already stored for *row*, including legacy plaintext copies."""
    keys = _sensitive_meta_keys(service)
    if row is None or not keys:
        return {}
    legacy = {k: v for k, v in json.loads(row.meta_json or "{}").items() if k in keys}
    try:
        _key, packed = _unpack_secret(service, box.decrypt(row.key_encrypted))
    except Exception:
        # Undecryptable (e.g. ENCRYPTION_KEY rotated): the caller is replacing
        # the credential anyway, so only the legacy plaintext copy survives.
        logger.warning("Stored %s credential could not be decrypted; keeping legacy meta only", service)
        packed = {}
    return {**legacy, **packed}


def _merged_meta_values(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Same rules as ``_merged_meta``: None deletes, "" leaves unchanged."""
    merged = dict(current)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        elif value != "":
            merged[key] = value
    return merged


def _pack_secret(service: str, key: str, sensitive: dict[str, Any]) -> str:
    if not _sensitive_meta_keys(service):
        return key
    return json.dumps({"key": key, **sensitive})


def _unpack_secret(service: str, raw: str) -> tuple[str, dict[str, Any]]:
    """Split a stored value into (credential, sensitive meta).

    A value written before packing existed is a bare credential string; one
    that happens to parse as JSON but is not a packed dict is kept verbatim.
    """
    if not _sensitive_meta_keys(service):
        return raw, {}
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, {}
    if not isinstance(data, dict) or "key" not in data:
        return raw, {}
    return str(data["key"]), {k: v for k, v in data.items() if k != "key"}


# Last result of a Control Center "Test" per service, so status survives a
# reload. Internal state, not a setting: no catalog entry, no default.
CONNECTION_TESTS_KEY = "connection_tests_json"


def record_connection_test(db: Session, service: str, ok: bool, message: str) -> str:
    """Store the outcome of testing *service*'s saved configuration; returns the timestamp."""
    from datetime import UTC, datetime

    tested_at = datetime.now(UTC).isoformat()
    tests = get_connection_tests(db)
    tests[service] = {"ok": ok, "message": message[:500], "tested_at": tested_at}
    row = db.query(AppSetting).filter(AppSetting.key == CONNECTION_TESTS_KEY).one_or_none()
    if row is None:
        db.add(AppSetting(key=CONNECTION_TESTS_KEY, value_json=json.dumps(tests)))
    else:
        row.value_json = json.dumps(tests)
    db.commit()
    invalidate_settings_cache()
    return tested_at


def get_connection_tests(db: Session) -> dict[str, dict[str, Any]]:
    stored = get_setting_row(db, CONNECTION_TESTS_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def integration_presence(db: Session) -> dict[str, bool]:
    present = {service: False for service in SENSITIVE_INTEGRATIONS}
    for row in db.query(ApiKey.service).all():
        present[row.service] = True
    return present


def _merged_meta(row: ApiKey | None, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored meta.

    - An explicit ``None`` value **deletes** the key (lets the UI clear a stale
      pinned ``tan_security_function`` / ``tan_medium``).
    - An empty string ``""`` is ignored (treated as "leave unchanged").
    - Any other value updates the key.
    """
    current = json.loads(row.meta_json or "{}") if row is not None else {}
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        elif value == "":
            continue
        else:
            current[key] = value
    return current
