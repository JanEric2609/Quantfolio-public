"""One-time first-run setup token.

A fresh (or wiped) instance belongs to whoever registers first, and the API is
reachable by anything that can reach the web LXC. So the first registration
must present a token only the operator can see:

* ``SETUP_TOKEN`` from the environment, when set; otherwise
* a random token generated once, logged at WARNING at startup and kept in
  ``<data_dir>/setup_token`` (mode 0600) so it survives restarts until used.

The token is only ever consulted while no user exists, and is discarded as soon
as the first account is created. It is compared in constant time.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from app.foundation.core.config import get_settings

logger = logging.getLogger(__name__)

TOKEN_FILENAME = "setup_token"
_DEFAULT_SYSTEM_DATA_DIR = Path("/var/lib/quantfolio")
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Fallback for a filesystem that refuses the token file: the token then lives
# only in this process (and its log line), which is still enough to register.
_ephemeral_token: str | None = None


def data_dir() -> Path:
    """Directory for the app's small state files (``DATA_DIR`` > /var/lib/quantfolio > <repo>/.quantfolio)."""
    configured = (get_settings().data_dir or "").strip()
    if configured:
        return Path(configured)
    if _DEFAULT_SYSTEM_DATA_DIR.is_dir() and os.access(_DEFAULT_SYSTEM_DATA_DIR, os.W_OK):
        return _DEFAULT_SYSTEM_DATA_DIR
    return _REPO_ROOT / ".quantfolio"


def setup_token_path() -> Path:
    return data_dir() / TOKEN_FILENAME


def _env_token() -> str | None:
    value = (get_settings().setup_token or "").strip()
    return value or None


def _read_token_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def get_or_create_setup_token() -> str:
    """The token the first registration must present (created on first use)."""
    global _ephemeral_token

    env_token = _env_token()
    if env_token:
        return env_token

    path = setup_token_path()
    existing = _read_token_file(path)
    if existing:
        return existing
    if _ephemeral_token:
        return _ephemeral_token

    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two processes starting together must agree on one token.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_token_file(path) or token
    except OSError:
        logger.warning("Could not write the setup token file %s; keeping the token in memory only", path)
        _ephemeral_token = token
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def verify_setup_token(presented: str | None) -> bool:
    """Constant-time comparison of the presented token with the expected one."""
    if not presented:
        return False
    expected = get_or_create_setup_token()
    return hmac.compare_digest(presented.strip().encode("utf-8"), expected.encode("utf-8"))


def discard_setup_token() -> None:
    """Forget the token once an account exists (it has done its job)."""
    global _ephemeral_token
    _ephemeral_token = None
    try:
        setup_token_path().unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove the setup token file %s", setup_token_path())


def announce_setup_token_if_needed(has_users: bool) -> None:
    """Startup hook: show the operator how to finish first-run setup.

    With users present, a stale token file is removed instead.
    """
    if has_users:
        discard_setup_token()
        return
    if _env_token():
        logger.warning(
            "FIRST-RUN SETUP: no account exists yet. Registration requires the SETUP_TOKEN from this "
            "service's environment. Enter it on the registration form."
        )
        return
    token = get_or_create_setup_token()
    logger.warning(
        "FIRST-RUN SETUP: no account exists yet. Registration requires this one-time setup token: %s "
        "(also stored in %s, mode 0600). Enter it on the registration form; it is deleted once the first "
        "account exists. Set SETUP_TOKEN in the environment to choose your own.",
        token,
        setup_token_path(),
    )
