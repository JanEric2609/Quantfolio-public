"""Provenance stamps for the evidence ledgers (ADR 0018 §10).

A call the trust page judges must say what produced it, or a later comparison
cannot tell one version of the system from another. This module builds the
pieces every stamp shares:

* :func:`canonical_hash` — SHA-256 of canonical JSON (sorted keys, no
  whitespace), so the same resolved config always hashes the same.
* :func:`code_sha` — the deployed git commit, read from the checkout's
  ``.git`` directly (no subprocess); ``QUANTFOLIO_CODE_SHA`` overrides it.
* :func:`llm_server_identity` — what llama-server says it is serving
  (``GET /props``): model path, build, context size, chat-template hash and the
  server-side sampling parameters. Cached per process; a failure is recorded,
  never raised.
* :func:`cohort_id` and :func:`stamp` — the cohort is the hash of everything
  that changes what is tested; the stamp is the cohort plus the descriptive
  fields around it.

Raw prompt text is never stored here: prompts expire after 24 h by design
(``apply_prompt_retention_policy``), so the stamp keeps hashes only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROVENANCE_SCHEMA_VERSION = 1
#: Rows written before stamps existed form one cohort (ADR 0018 §10).
LEGACY_COHORT = "legacy_unstamped"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROPS_TTL_SECONDS = 600.0
_PROPS_TIMEOUT_SECONDS = 3.0
# Server-side generation parameters that change what the model writes.
_SAMPLING_KEYS = (
    "temperature", "top_k", "top_p", "min_p", "typical_p", "repeat_penalty",
    "presence_penalty", "frequency_penalty", "dry_multiplier", "seed",
)

_props_lock = threading.Lock()
_props_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, ``str`` for the rest."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)


def canonical_hash(value: Any) -> str:
    """Full SHA-256 hex digest of :func:`canonical_json` (a ``str`` is hashed as-is)."""
    data = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 16) -> str:
    return canonical_hash(value)[:length]


def _read_git_sha(root: Path) -> str | None:
    git = root / ".git"
    if git.is_file():  # a worktree: ".git" holds "gitdir: <path>"
        text = git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        git = (root / text.split(":", 1)[1].strip()).resolve()
    head_file = git / "HEAD"
    if not head_file.is_file():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head or None
    ref = head.split(":", 1)[1].strip()
    common = git
    commondir = git / "commondir"
    if commondir.is_file():
        common = (git / commondir.read_text(encoding="utf-8").strip()).resolve()
    for base in (git, common):
        ref_file = base / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip() or None
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(" ")
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


@lru_cache(maxsize=1)
def code_sha() -> str | None:
    """The deployed commit; ``None`` when it cannot be determined (never raises)."""
    override = os.environ.get("QUANTFOLIO_CODE_SHA", "").strip()
    if override:
        return override
    try:
        return _read_git_sha(_REPO_ROOT)
    except OSError as exc:
        logger.debug("code_sha: %s", exc)
        return None


def _probe_props(root: str, headers: dict[str, str]) -> dict[str, Any]:
    import httpx

    response = httpx.get(f"{root}/props", headers=headers, timeout=_PROPS_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _summarise_props(props: dict[str, Any]) -> dict[str, Any]:
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, dict) else {}
    params = settings.get("params")
    params = params if isinstance(params, dict) else settings
    template = props.get("chat_template")
    return {
        "available": True,
        "model_path": props.get("model_path"),
        "build_info": props.get("build_info"),
        "n_ctx": settings.get("n_ctx"),
        "chat_template_sha256": canonical_hash(template) if isinstance(template, str) else None,
        "server_sampling": {k: params[k] for k in _SAMPLING_KEYS if k in params},
    }


def llm_server_identity(db: Session | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """What the local llama-server reports it is serving, cached for ten minutes.

    Returns ``{"available": False, "error": ...}`` when the server cannot be
    asked; the caller records that as a degradation flag instead of failing.
    """
    from app.foundation.settings import llm_auth_headers, probe_root, resolve_llm_base_url

    try:
        root = probe_root(resolve_llm_base_url(db)) if db is not None else probe_root(
            os.environ.get("LLM_LOCAL_URL", "http://127.0.0.1:8080/v1")
        )
    except Exception as exc:  # noqa: BLE001 - settings unavailable
        return {"available": False, "error": f"no llm url: {exc}"}
    now = time.monotonic()
    with _props_lock:
        cached = _props_cache.get(root)
        if cached is not None and not refresh and now - cached[0] < _PROPS_TTL_SECONDS:
            return dict(cached[1])
    try:
        identity = _summarise_props(_probe_props(root, llm_auth_headers(db)))
    except Exception as exc:  # noqa: BLE001 - provenance must never break a run
        identity = {"available": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    with _props_lock:
        _props_cache[root] = (now, identity)
    return dict(identity)


def cohort_id(spec: dict[str, Any]) -> str:
    """Stable id of a cohort spec: any change to the spec is a new cohort (and a new trial)."""
    return f"c{PROVENANCE_SCHEMA_VERSION}-{short_hash(spec, 20)}"


def stamp(
    *,
    source: str,
    cohort_spec: dict[str, Any],
    details: dict[str, Any] | None = None,
    degradation_flags: list[str] | None = None,
) -> dict[str, Any]:
    """The ``provenance_json`` written with a call, in the same transaction.

    ``cohort_spec`` holds what changes what is tested (it is hashed into the
    cohort id and stored verbatim); ``details`` holds descriptive fields that
    must not split cohorts (code sha, the served model for Discover, data
    as-of dates).
    """
    sha = code_sha()
    flags = set(degradation_flags or [])
    if not sha:
        flags.add("code_sha_unknown")
    return {
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "source": source,
        "cohort_id": cohort_id({"source": source, **cohort_spec}),
        "cohort_spec": cohort_spec,
        "code_sha": sha,
        **(details or {}),
        "degradation_flags": sorted(flags),
    }
