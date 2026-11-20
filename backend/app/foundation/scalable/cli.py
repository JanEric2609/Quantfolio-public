"""The only place Quantfolio runs Scalable Capital's official ``sc`` CLI.

Read-only by construction. A command runs only if its whole argv shape is on
``ALLOWED_COMMANDS``: ``savings-plans``, ``watchlist``, ``price-alerts`` and
``portfolio-groups`` are reads when bare and writes with a subcommand, so a
prefix match would not be enough. The same allow-list is enforced again by the
root-owned wrapper (``infra/scalable/quantfolio-sc-ro``), the session is logged
in with ``--local-read-only`` and the CLI's own ``allowed_isins = []`` refuses
every trade, so no single layer is the only guard.

``sc`` runs as a separate Unix user (``sudo -n -u scalable-cli-user``) that
owns the Scalable session; this process never sees a token or password, and
no Scalable credential is ever stored in the database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Whole-argv allow-list: command path -> flags it may carry (each takes a value).
ALLOWED_COMMANDS: dict[tuple[str, ...], frozenset[str]] = {
    ("whoami",): frozenset(),
    ("capabilities",): frozenset(),
    ("broker", "context", "show"): frozenset(),
    ("broker", "overview"): frozenset({"--portfolio-id"}),
    ("broker", "cash-breakdown"): frozenset({"--portfolio-id"}),
    ("broker", "holdings"): frozenset({"--portfolio-id"}),
    ("broker", "transactions"): frozenset(
        {"--portfolio-id", "--page-size", "--cursor", "--from-time", "--to-time"}
    ),
    ("broker", "transaction", "details"): frozenset({"--portfolio-id", "--transaction-id"}),
    ("broker", "savings-plans"): frozenset({"--portfolio-id"}),
    ("overnight",): frozenset(),
    # Interest credited to the overnight account: capital income for the tax
    # cockpit. The type filter keeps it to INTEREST rows.
    ("overnight", "transactions"): frozenset({"--page-size", "--cursor", "--type-filter", "--from-time"}),
}

# Words that only ever appear in mutating sc commands. A command path holding
# one is refused even if someone widens ALLOWED_COMMANDS by mistake.
MUTATING_WORDS = frozenset(
    {
        "login", "logout", "select", "trade", "buy", "sell", "cancel", "add", "remove",
        "create", "update", "delete", "assign", "unassign", "config", "--confirm",
    }
)

_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "--portfolio-id": re.compile(r"^[A-Za-z0-9_-]{1,128}$"),
    "--transaction-id": re.compile(r"^[A-Za-z0-9_:.-]{1,128}$"),
    "--cursor": re.compile(r"^[A-Za-z0-9_=+/.:-]{1,512}$"),
    "--page-size": re.compile(r"^(?:[1-9]|[1-9][0-9]|100)$"),
    "--type-filter": re.compile(r"^[A-Z_]{1,40}$"),
    "--from-time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"),
    "--to-time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"),
}

MAX_STDOUT_BYTES = 20 * 1024 * 1024
# stderr is only logged by size and scanned for sudo errors; the rest is dropped.
MAX_STDERR_BYTES = 64 * 1024
WRAPPER_NAME = "quantfolio-sc-ro"
# The root-owned wrapper always executes this path (infra/scalable/quantfolio-sc-ro),
# so it is the file whose hash is pinned, never a configurable one.
SC_BINARY = "/usr/local/bin/sc"

# sc error codes that mean a write was attempted and refused locally. None can
# come from a read, so seeing one means something is wrong on our side.
READ_ONLY_VIOLATION_CODES = frozenset(
    {
        "local_read_only",
        "trade_control_isin_not_allowed",
        "trade_control_isin_denied",
        "trade_control_order_notional_exceeded",
    }
)


class ScalableError(Exception):
    """A failed ``sc`` call. ``code`` is sc's own error code when it gave one."""

    def __init__(self, code: str, message: str, hints: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints)


class ScalableNotInstalled(ScalableError):
    """The wrapper or ``sc`` is missing, or sudo is not configured for it."""


class ScalableLoginRequired(ScalableError):
    """Exit 20: no session, or the refresh token expired. A human must log in again."""


class ScalableTransient(ScalableError):
    """Exit 30: network, rate limit or backend error. Worth a retry."""


class ScalableUsageError(ScalableError):
    """Exit 10: bad input or missing broker context."""


class ScalableReadOnlyViolation(ScalableError):
    """sc refused a write. Never expected from a read; the connection is disabled."""


class ScalableProtocolError(ScalableError):
    """Output was not the documented JSON envelope."""


@dataclass(frozen=True)
class ScalableCliConfig:
    wrapper_path: str = "/usr/local/libexec/quantfolio-sc-ro"
    cli_user: str = "scalable-cli-user"
    binary_path: str = SC_BINARY
    binary_sha256: str = ""
    timeout_seconds: int = 60
    max_attempts: int = 3
    # The wrapper path is a Control Center setting any app user can edit, so
    # only a root-owned file named quantfolio-sc-ro that nobody else can
    # write is ever executed. Tests with a fake runner switch this off.
    trusted_wrapper_only: bool = True


Runner = Callable[..., subprocess.CompletedProcess]


def bounded_run(argv: Sequence[str], *, timeout: float, env: dict[str, str] | None = None,
                **_kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True)`` that never holds more than the caps in memory.

    stdout beyond ``MAX_STDOUT_BYTES`` kills the process (the caller then sees
    one byte over the cap and refuses the output); stderr beyond
    ``MAX_STDERR_BYTES`` is drained and dropped so the child never blocks.
    """
    proc = subprocess.Popen(
        list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    out = bytearray()
    err = bytearray()

    def pump(stream: Any, buf: bytearray, cap: int, kill_on_overflow: bool) -> None:
        try:
            while chunk := stream.read(65536):
                room = cap + 1 - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if kill_on_overflow and len(buf) > cap:
                    proc.kill()
                    return
        finally:
            stream.close()

    pumps = [
        threading.Thread(target=pump, args=(proc.stdout, out, MAX_STDOUT_BYTES, True), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err, MAX_STDERR_BYTES, False), daemon=True),
    ]
    for thread in pumps:
        thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        for thread in pumps:
            thread.join(timeout=5)
    return subprocess.CompletedProcess(list(argv), returncode, bytes(out), bytes(err))

# One sc process at a time per API/worker process; the wrapper adds a flock
# on the session owner's home so the two processes never refresh the rotating
# refresh token concurrently (a lost race logs the session out).
_PROCESS_LOCK = threading.Lock()
_hash_cache: dict[tuple[str, int, int], str] = {}


def validate_argv(args: Sequence[str]) -> list[str]:
    """Return *args* if it is an allowed read command, else raise ScalableUsageError."""
    args = list(args)
    words = [a for a in args if not a.startswith("--")]
    path: tuple[str, ...] | None = None
    for candidate in sorted(ALLOWED_COMMANDS, key=len, reverse=True):
        if tuple(args[: len(candidate)]) == candidate:
            path = candidate
            break
    if path is None or any(w in MUTATING_WORDS for w in args[: len(path)]):
        raise ScalableUsageError("command_not_allowed", f"sc command not allowed: {' '.join(words[:3])}")
    rest = args[len(path):]
    allowed_flags = ALLOWED_COMMANDS[path]
    if len(rest) % 2:
        raise ScalableUsageError("command_not_allowed", "every sc flag must carry one value")
    seen: set[str] = set()
    for i in range(0, len(rest), 2):
        flag, value = rest[i], rest[i + 1]
        if flag not in allowed_flags or flag in seen:
            raise ScalableUsageError("command_not_allowed", f"flag not allowed: {flag}")
        if value.startswith("-") or not _VALUE_PATTERNS[flag].fullmatch(value):
            raise ScalableUsageError("invalid_input", f"invalid value for {flag}")
        seen.add(flag)
    if any(a in MUTATING_WORDS for a in rest):
        raise ScalableUsageError("command_not_allowed", "mutating token in sc arguments")
    return args


def _scrubbed_env() -> dict[str, str]:
    # Nothing from our environment (JWT_SECRET, ENCRYPTION_KEY, DATABASE_URL,
    # provider keys) reaches the child.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def binary_sha256(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime_ns), int(st.st_size))
    cached = _hash_cache.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    _hash_cache.clear()
    _hash_cache[key] = value
    return value


def parse_envelope(stdout: str) -> dict[str, Any]:
    """Parse sc's ``{ok, command, data | error, hints}`` envelope."""
    text = stdout.strip()
    candidates = [text] + [line for line in reversed(text.splitlines()) if line.strip().startswith("{")]
    for candidate in candidates:
        try:
            envelope = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(envelope, dict) and isinstance(envelope.get("ok"), bool):
            return envelope
    raise ScalableProtocolError("protocol_error", "sc output was not a JSON envelope")


def wraps_result(args: Sequence[str]) -> bool:
    """True for the commands whose ``data`` sc wraps around a ``result`` object.

    Every broker query goes through ``broker_result_envelope``
    (``src/broker_shared.rs``): ``{account_id, portfolio_id, resolution,
    result}``. The overnight summary is wrapped the same way with
    ``selection`` (``src/overnight_query_execution.rs``). ``broker context
    show`` reads a local file and is not wrapped.
    """
    words = [a for a in args if not a.startswith("--")]
    if not words:
        return False
    if words[0] == "overnight":
        return True
    return words[0] == "broker" and tuple(words[:3]) != ("broker", "context", "show")


def unwrap_result(args: Sequence[str], data: dict[str, Any]) -> dict[str, Any]:
    """The ``result`` object of a wrapped command, carrying the ids sc resolved.

    A wrapped command without a ``result`` object is refused. Reading the
    wrapper itself made every field None and the sync stored zeros while
    reporting success.
    """
    if not wraps_result(args):
        return data
    label = " ".join(a for a in args if not a.startswith("--"))
    result = data.get("result")
    if not isinstance(result, dict):
        raise ScalableProtocolError(
            "unexpected_shape", f"sc {label} returned no result object; the sc output format may have changed",
        )
    out = dict(result)
    for key in ("account_id", "portfolio_id", "savings_account_id"):
        if out.get(key) is None and data.get(key) is not None:
            out[key] = data[key]
    return out


class ScalableCli:
    def __init__(
        self,
        config: ScalableCliConfig,
        *,
        runner: Runner = bounded_run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._runner = runner
        self._sleep = sleep

    def argv(self, args: Sequence[str]) -> list[str]:
        command = [self.config.wrapper_path, *validate_argv(args), "--json"]
        if self.config.cli_user:
            return ["sudo", "-n", "-H", "-u", self.config.cli_user, "--", *command]
        return command

    def check_binary(self) -> None:
        if self.config.trusted_wrapper_only and self.config.binary_path != SC_BINARY:
            # The wrapper runs SC_BINARY whatever is configured; hashing another
            # file would vouch for a binary that never runs.
            raise ScalableError(
                "binary_path_mismatch",
                f"The wrapper always runs {SC_BINARY}; the pinned hash must be checked against that file.",
            )
        expected = (self.config.binary_sha256 or "").strip().lower()
        if not expected:
            return
        actual = binary_sha256(self.config.binary_path)
        if actual is None:
            raise ScalableNotInstalled("not_installed", f"sc binary not found at {self.config.binary_path}")
        if actual != expected:
            raise ScalableError(
                "binary_hash_mismatch",
                "The sc binary does not match the pinned SHA-256; refusing to run it.",
                ["Verify the release (SHA-256 + minisign) and update scalable_binary_sha256."],
            )

    def check_wrapper(self) -> None:
        if not self.config.trusted_wrapper_only:
            return
        path = self.config.wrapper_path
        if os.path.basename(path) != WRAPPER_NAME or not os.path.isabs(path):
            raise ScalableError(
                "wrapper_untrusted",
                f"Refusing to run {path!r}: the wrapper must be an absolute path to {WRAPPER_NAME}.",
            )
        try:
            st = os.stat(path)
        except OSError:
            raise ScalableNotInstalled(
                "not_installed", f"The sc wrapper is not installed at {path}",
                ["Install infra/scalable/quantfolio-sc-ro (see docs/scalable.md)."],
            ) from None
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ScalableError(
                "wrapper_untrusted",
                f"Refusing to run {path}: it must be owned by root and writable by root only.",
                ["install -o root -g root -m 0755 infra/scalable/quantfolio-sc-ro " + path],
            )

    def run(self, *args: str) -> dict[str, Any]:
        """Run one allowed read command and return its payload.

        Broker and overnight queries come back as ``result`` inside sc's own
        envelope (see ``unwrap_result``); every other command returns ``data``.
        """
        argv = self.argv(args)
        self.check_wrapper()
        self.check_binary()
        attempts = max(1, self.config.max_attempts)
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                data = self._run_once(argv, args)
            except ScalableTransient:
                if attempt == attempts:
                    raise
                self._sleep(delay)
                delay *= 2
                continue
            return unwrap_result(args, data)
        raise AssertionError("unreachable")  # pragma: no cover

    def _run_once(self, argv: list[str], args: Sequence[str]) -> dict[str, Any]:
        label = " ".join(a for a in args if not a.startswith("--"))
        with _PROCESS_LOCK:
            try:
                proc = self._runner(
                    argv,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self.config.timeout_seconds,
                    env=_scrubbed_env(),
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ScalableNotInstalled("not_installed", f"cannot execute {argv[0]}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ScalableTransient("timeout", f"sc {label} timed out") from exc

        stdout_raw = proc.stdout or b""
        if len(stdout_raw) > MAX_STDOUT_BYTES:
            raise ScalableProtocolError("output_too_large", f"sc {label} returned more than 20 MB")
        stdout = stdout_raw.decode("utf-8", "replace") if isinstance(stdout_raw, bytes) else str(stdout_raw)
        stderr_raw = (proc.stderr or b"")[:MAX_STDERR_BYTES]
        stderr = stderr_raw.decode("utf-8", "replace") if isinstance(stderr_raw, bytes) else str(stderr_raw)
        # stderr can echo account data; log its size only.
        logger.info("sc %s exited %s (stdout %d B, stderr %d B)", label, proc.returncode, len(stdout), len(stderr))

        try:
            envelope = parse_envelope(stdout)
        except ScalableProtocolError:
            lowered = stderr.lower()
            if "no new privileges" in lowered or "effective uid is not 0" in lowered:
                raise ScalableNotInstalled(
                    "sandbox_blocks_sudo",
                    "The service's systemd sandbox stops sudo (NoNewPrivileges, which ProtectKernelTunables "
                    "and similar settings also turn on, or a nosuid mount)",
                    ["Install the current quantfolio-api and quantfolio-worker units: run quantfolio-update-app "
                     "as root on the app server. Keep NoNewPrivileges, RestrictSUIDSGID, ProtectKernelTunables "
                     "and the other seccomp settings out of any drop-in (see docs/scalable.md, systemd units)."],
                ) from None
            if "sudo" in lowered and ("password" in lowered or "not allowed" in lowered or "not in the sudoers" in lowered):
                raise ScalableNotInstalled(
                    "sudo_not_configured",
                    "sudo is not configured to run the sc wrapper for this service user",
                    ["Install infra/scalable/sudoers.quantfolio-sc (see docs/scalable.md)."],
                ) from None
            if proc.returncode == 127 or "no such file" in lowered or "not found" in lowered:
                raise ScalableNotInstalled("not_installed", "the sc wrapper or binary is not installed") from None
            if proc.returncode == 30:
                raise ScalableTransient("transient", f"sc {label} failed (exit 30)") from None
            raise

        if envelope.get("ok"):
            data = envelope.get("data")
            return data if isinstance(data, dict) else {"value": data}

        error = envelope.get("error") or {}
        if not isinstance(error, dict):
            raise ScalableProtocolError("protocol_error", f"sc {label} failed without an error object")
        code = str(error.get("code") or "internal_error")
        message = str(error.get("message") or "sc reported an error")[:500]
        hints = [str(h)[:300] for h in (envelope.get("hints") or [])][:5]
        if code in READ_ONLY_VIOLATION_CODES:
            raise ScalableReadOnlyViolation(code, message, hints)
        if proc.returncode == 20 or code in {"no_session", "refresh_relogin_required", "secret_storage_unavailable"}:
            raise ScalableLoginRequired(code, message, hints)
        if proc.returncode == 30:
            raise ScalableTransient(code, message, hints)
        if proc.returncode == 10:
            raise ScalableUsageError(code, message, hints)
        raise ScalableError(code, message, hints)
