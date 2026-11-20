from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_JWT_SECRET = "local-dev-change-me"
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_DB_URL = f"sqlite:///{_BACKEND_DIR / 'quantfolio.db'}"


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str = _DEFAULT_DB_URL
    redis_url: str = "redis://localhost:6379/0"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "info"
    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_expiry_hours: int = 24
    encryption_key: str | None = None
    # cookie_secure defaults to True; explicitly set False only for local dev
    cookie_secure: bool = True
    password_reset_token: str | None = None
    reset_token_expire_minutes: int = 30
    # One-time token the first registration must present (foundation/setup_token.py).
    # Unset: a random one is generated, logged at WARNING and written to data_dir.
    setup_token: str | None = None
    # Where the app keeps small state files (the setup token). Unset: /var/lib/quantfolio
    # when writable, else <repo>/.quantfolio.
    data_dir: str | None = None
    # Comma-separated Host headers the API answers to (TrustedHostMiddleware).
    # Unset/empty: any host (off). Example: 192.0.2.23,quantfolio.local
    allowed_hosts: str | None = None
    dkb_fints_url: str = "https://fints.dkb.de/fints"
    dkb_blz: str = "12030000"
    frontend_origin: str = "http://localhost:5173"
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "QuantFolio"
    openbb_api_url: str = "http://127.0.0.1:6900"
    finrl_enabled: bool = True
    qlib_enabled: bool = False
    qlib_provider_uri: str = "/opt/quantfolio/qlib_data"
    mlflow_tracking_dir: str = "/opt/quantfolio/mlruns"
    obsidian_rest_url: str = "http://127.0.0.1:27123"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_local(self) -> bool:
        # Fail closed: only the literal "local" (case/space-insensitive) is local.
        # A misspelled or empty APP_ENV is treated as a real environment.
        return self.app_env.strip().lower() == "local"

    @property
    def is_postgres(self) -> bool:
        # A Postgres DSN is a real-deployment signal independent of APP_ENV: the
        # weak dev defaults (JWT-derived encryption key, default JWT secret) must
        # never run against it. See issue #129.
        return self.database_url.strip().lower().startswith("postgres")


@lru_cache
def get_settings() -> Settings:
    return Settings()
