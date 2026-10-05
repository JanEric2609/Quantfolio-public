"""Model registry: save/load trained pipeline artefacts with joblib."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "/opt/quantfolio/ml-models"
# Digest recorded next to every artefact at save time and checked before any
# load: joblib.load is pickle, i.e. arbitrary code execution for whoever can
# swap the file.
_DIGEST_SUFFIX = ".sha256"

_SAFE_PREFIXES = (
    "builtins.",
    "collections.",
    "datetime.",
    "numpy.",
    "pandas.",
    "sklearn.",
    "scipy.",
    "lightgbm.",
    "torch.",
    "xgboost.",
)


def _validate_pipeline(obj: Any) -> None:
    from sklearn.pipeline import Pipeline

    if not isinstance(obj, Pipeline):
        raise ValueError(f"Loaded object is not a sklearn Pipeline: {type(obj)}")

    for name, step in obj.steps:
        mod = type(step).__module__
        if not any(mod.startswith(p) for p in _SAFE_PREFIXES):
            raise ValueError(
                f"Pipeline step '{name}' has unsafe module: {mod}"
            )


def model_path(model_id: str, base: str = _DEFAULT_BASE) -> Path:
    return Path(base) / model_id / "pipeline.joblib"


def _digest_path(p: Path) -> Path:
    return p.with_name(p.name + _DIGEST_SUFFIX)


def _file_sha256(p: Path) -> str:
    digest = hashlib.sha256()
    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_model(pipeline: Any, model_id: str, base: str = _DEFAULT_BASE) -> str:
    import joblib
    p = model_path(model_id, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Dump aside, hash, then swap both files in back to back: a reader in another
    # process (API vs worker) should not catch a new artefact next to an old digest.
    staged = p.with_name(p.name + ".tmp")
    joblib.dump(pipeline, staged)
    digest_tmp = _digest_path(p).with_name(_digest_path(p).name + ".tmp")
    digest_tmp.write_text(_file_sha256(staged) + "\n", encoding="utf-8")
    os.replace(staged, p)
    os.replace(digest_tmp, _digest_path(p))
    return str(p)


def _sanitize_model_id(model_id: str) -> str:
    """Guard against path-traversal in model_id."""
    if not model_id or ".." in model_id or model_id.startswith("/"):
        raise ValueError(f"Invalid model_id: {model_id!r}")
    return model_id


def _verified_model_path(model_id: str, base: str) -> Path:
    """Resolve the artefact path and prove it is safe to unpickle.

    Two checks, both before ``joblib.load``:

    * the resolved path (symlinks followed) lies inside the registry directory;
    * the file's SHA-256 equals the digest recorded when it was saved.

    An artefact saved before digests existed has none to compare: its digest is
    recorded now (trust on first use, with a warning) so an upgrade keeps the
    regime and ML models working, and any later swap is caught.
    """
    _sanitize_model_id(model_id)
    p = model_path(model_id, base)
    if not p.exists():
        raise FileNotFoundError(f"Model artefact not found: {p}")
    root = Path(base).resolve()
    resolved = p.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Model artefact {model_id!r} resolves outside the registry directory")
    actual = _file_sha256(resolved)
    digest_file = _digest_path(p)
    try:
        recorded = digest_file.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        logger.warning(
            "Model artefact %s has no recorded SHA-256 (saved before digests); recording it now",
            model_id,
        )
        try:
            digest_file.write_text(actual + "\n", encoding="utf-8")
        except OSError:
            logger.warning("Could not record the SHA-256 for model artefact %s (read-only registry?)", model_id)
        return resolved
    if not hmac.compare_digest(recorded, actual):
        raise ValueError(f"Model artefact {model_id!r} does not match its recorded SHA-256; refusing to load it")
    return resolved


def load_model(
    model_id: str,
    base: str = _DEFAULT_BASE,
) -> Any:
    """Load a model artefact from disk and validate it is a safe Pipeline.

    The model_id is sanitised to prevent path traversal before the on-disk
    path is resolved.  Deserialisation uses ``joblib.load`` (pickle-based);
    after loading, ``_validate_pipeline`` confirms the result is an sklearn
    ``Pipeline`` composed of steps from trusted modules only.

    Args:
        model_id: Identifier for the model.
        base: Base directory containing the model artefact.

    Returns:
        The loaded object.

    Raises:
        ValueError: If the loaded object is not a valid sklearn Pipeline.
        FileNotFoundError: If the artefact does not exist on disk.
    """
    import joblib
    p = _verified_model_path(model_id, base)
    obj = joblib.load(p, mmap_mode="r")
    _validate_pipeline(obj)
    return obj


def _load_model_unvalidated(
    model_id: str,
    base: str = _DEFAULT_BASE,
) -> Any:
    """Load a model artefact without sklearn Pipeline validation.

    *Unsafe* — performs a raw ``joblib.load`` (pickle-based) **without**
    allowlisted-class deserialisation.  ``model_id`` is still guarded against
    path traversal.

    Intended only for non-Pipeline objects (e.g. RegimeHMM) that are not
    subject to the sklearn Pipeline safety check.  Do **not** call this
    with user-provided model identifiers.
    """
    import joblib
    p = _verified_model_path(model_id, base)
    return joblib.load(p, mmap_mode="r")


def delete_model(model_id: str, base: str = _DEFAULT_BASE) -> bool:
    p = model_path(model_id, base)
    if p.exists():
        p.unlink()
        _digest_path(p).unlink(missing_ok=True)
        return True
    return False
