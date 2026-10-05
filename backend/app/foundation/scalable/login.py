"""Start Scalable's device-code login from the Control Center.

The one ``sc`` command that is not a read: ``login --local-read-only``. It is
kept out of ``ALLOWED_COMMANDS`` (which stays reads only) and runs through the
same root-owned wrapper, which accepts exactly this two-word argv and nothing
else (no plain ``login``, no other flag). The session it creates refuses every
GraphQL mutation, and ``allowed_isins = []`` still refuses every order.

sc prints a verification link and a code, then polls until the person
approves it in the Scalable app (and a possible second factor on their linked
device). This module runs that process in the background, hands the link and
code to the browser, and reports how it ended. Nothing it reads is a secret:
the tokens go to the CLI user's own session file, never through this process.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.foundation.scalable.cli import (
    ScalableCli,
    ScalableCliConfig,
    _scrubbed_env,
)

logger = logging.getLogger(__name__)

LOGIN_ARGV: tuple[str, str] = ("login", "--local-read-only")
# Device codes expire well before this; the process is killed after it.
LOGIN_MAX_SECONDS = 15 * 60
# How long POST /login waits for sc to print the link and code.
PROMPT_WAIT_SECONDS = 20.0
MAX_OUTPUT_BYTES = 64 * 1024
READ_ONLY_CONFIRMATION = "local read-only mode is active"

_URL_RE = re.compile(r"https://[^\s<>\"']+")
_CODE_RE = re.compile(r"verify the code\s+(?:\x1b\[[0-9;]*m)*([A-Za-z0-9-]{4,32})", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def trusted_verification_url(url: str | None) -> str | None:
    """The link only if it is https on scalable.capital; anything else is never shown as a link."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "scalable.capital" or host.endswith(".scalable.capital")):
        return None
    return url


@dataclass
class LoginState:
    state: str = "idle"  # idle | waiting | succeeded | failed | expired | cancelled
    verification_url: str | None = None
    verification_host: str | None = None
    user_code: str | None = None
    message: str | None = None
    error_code: str | None = None
    read_only_confirmed: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    started_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "verification_url": self.verification_url,
            "verification_host": self.verification_host,
            "user_code": self.user_code,
            "message": self.message,
            "error_code": self.error_code,
            "read_only_confirmed": self.read_only_confirmed,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


@dataclass
class _Run:
    proc: subprocess.Popen
    state: LoginState
    output: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    prompt_seen: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False


_LOCK = threading.Lock()
_current: _Run | None = None
_last: LoginState = LoginState()


def login_argv(config: ScalableCliConfig) -> list[str]:
    command = [config.wrapper_path, *LOGIN_ARGV]
    if config.cli_user:
        return ["sudo", "-n", "-H", "-u", config.cli_user, "--", *command]
    return command


def _parse_prompt(text: str) -> tuple[str | None, str | None]:
    clean = _ANSI_RE.sub("", text)
    url = None
    marker = clean.lower().find("open this url")
    if marker >= 0:
        match = _URL_RE.search(clean, marker)
        url = match.group(0).rstrip(".,;)") if match else None
    code_match = _CODE_RE.search(clean)
    return url, (code_match.group(1) if code_match else None)


def _classify_failure(returncode: int | None, stdout: str, stderr: str) -> tuple[str, str]:
    lowered = stderr.lower()
    if "no new privileges" in lowered or "effective uid is not 0" in lowered:
        return ("sandbox_blocks_sudo",
                "The service's systemd sandbox stops sudo (NoNewPrivileges, which ProtectKernelTunables "
                "and similar settings also turn on, or a nosuid mount).")
    if "sudo" in lowered and ("password" in lowered or "not allowed" in lowered or "not in the sudoers" in lowered):
        return "sudo_not_configured", "sudo is not configured to run the sc wrapper for this service user."
    if returncode == 127 or "no such file" in lowered:
        return "not_installed", "The sc wrapper or binary is not installed."
    text = (stdout + "\n" + stderr).lower()
    if "expired" in text:
        return "expired", "The code expired before it was approved. Start the login again."
    if "command_not_allowed" in text:
        return "wrapper_outdated", "The installed wrapper does not allow the login yet; reinstall it (see docs/scalable.md)."
    if "2fa" in text or "second factor" in text:
        return "second_factor_failed", "The second-factor approval on your linked device failed."
    if "rate_limited" in text or "still running" in text:
        return "busy", "Another sc call is running; try again in a minute."
    return "login_failed", f"sc login ended with exit code {returncode}."


def _pump(run: _Run, stream: Any, buf: bytearray, *, parse: bool) -> None:
    try:
        while chunk := stream.readline(4096):
            room = MAX_OUTPUT_BYTES - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            if parse and not run.prompt_seen.is_set():
                url, code = _parse_prompt(buf.decode("utf-8", "replace"))
                if code:
                    with _LOCK:
                        run.state.user_code = code
                        run.state.verification_url = trusted_verification_url(url)
                        run.state.verification_host = (urlsplit(url).hostname if url else None)
                        run.state.message = "Approve the code in the Scalable app or at the link."
                    run.prompt_seen.set()
    finally:
        stream.close()


def _watch(run: _Run) -> None:
    global _last
    try:
        returncode = run.proc.wait(timeout=LOGIN_MAX_SECONDS)
    except subprocess.TimeoutExpired:
        run.proc.kill()
        run.proc.wait()
        returncode = None
    # Let the pumps drain the last lines.
    time.sleep(0.2)
    stdout = run.output.decode("utf-8", "replace")
    stderr = run.stderr.decode("utf-8", "replace")
    logger.info("sc login exited %s (stdout %d B, stderr %d B)", returncode, len(stdout), len(stderr))
    with _LOCK:
        state = run.state
        state.finished_at = datetime.now(UTC)
        state.read_only_confirmed = READ_ONLY_CONFIRMATION in _ANSI_RE.sub("", stdout).lower()
        if run.cancelled:
            state.state, state.error_code, state.message = "cancelled", "cancelled", "Login cancelled."
        elif returncode is None:
            state.state, state.error_code = "expired", "expired"
            state.message = "The code was not approved in time. Start the login again."
        elif returncode == 0:
            state.state, state.error_code = "succeeded", None
            state.message = ("Logged in. The session is read-only." if state.read_only_confirmed else
                             "Logged in. sc did not print its read-only confirmation; run Test to check the session.")
        else:
            code, message = _classify_failure(returncode, stdout, stderr)
            state.state = "expired" if code == "expired" else "failed"
            state.error_code, state.message = code, message
        # The code is single-use and worthless now; don't keep showing it.
        state.user_code = None
        state.verification_url = None
        _last = replace(state)
        _finish(run)
    run.prompt_seen.set()


def _finish(run: _Run) -> None:
    global _current
    if _current is run:
        _current = None


def status() -> dict[str, Any]:
    with _LOCK:
        if _current is not None:
            return replace(_current.state).to_dict()
        return replace(_last).to_dict()


def start(cli: ScalableCli, *, user_id: str, popen: Any = subprocess.Popen,
          wait_seconds: float = PROMPT_WAIT_SECONDS) -> dict[str, Any]:
    """Start ``sc login --local-read-only`` (or return the one already running).

    Raises ``ScalableError`` when the wrapper is untrusted or sc fails its pinned
    hash, exactly like a read would.
    """
    global _current, _last
    cli.check_wrapper()
    cli.check_binary()
    with _LOCK:
        if _current is not None:
            return replace(_current.state).to_dict()
        try:
            proc = popen(
                login_argv(cli.config), shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_scrubbed_env(),
            )
        except FileNotFoundError:
            _last = LoginState(state="failed", error_code="not_installed",
                               message="The sc wrapper is not installed.", finished_at=datetime.now(UTC))
            return _last.to_dict()
        run = _Run(proc=proc, state=LoginState(state="waiting", started_at=datetime.now(UTC), started_by=user_id,
                                               message="Waiting for sc to print the code…"))
        _current = run
    threading.Thread(target=_pump, args=(run, proc.stdout, run.output), kwargs={"parse": True}, daemon=True).start()
    threading.Thread(target=_pump, args=(run, proc.stderr, run.stderr), kwargs={"parse": False}, daemon=True).start()
    threading.Thread(target=_watch, args=(run,), daemon=True).start()
    run.prompt_seen.wait(timeout=wait_seconds)
    return status()


def cancel() -> dict[str, Any]:
    with _LOCK:
        run = _current
        if run is None:
            return replace(_last).to_dict()
        run.cancelled = True
    run.proc.terminate()
    return status()


def _reset_for_tests() -> None:
    global _current, _last
    with _LOCK:
        _current = None
        _last = LoginState()
