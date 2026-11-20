"""CSV schema detection and validation for broker statements."""

import io
import logging
from dataclasses import dataclass

import pandas as pd

log = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


@dataclass
class ValidationResult:
    """Result of CSV validation."""

    valid: bool
    format: str | None
    message: str
    columns: list[str] | None
    row_count: int | None


def validate_csv(file_content: bytes) -> ValidationResult:
    """Validate CSV format and detect broker type.

    Supports:
    - DKB CSV (German banking CSV with specific columns)
    - Comdirect (future)
    - Trade Republic (future)
    """
    if len(file_content) > _MAX_UPLOAD_BYTES:
        return ValidationResult(
            valid=False,
            format=None,
            message=f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            columns=None,
            row_count=None,
        )

    try:
        df = pd.read_csv(
            io.BytesIO(file_content),
            encoding="cp1252",
            encoding_errors="replace",
            nrows=5,
        )
    except Exception:
        log.exception("Failed to read CSV for validation")
        return ValidationResult(valid=False, format=None, message="Failed to parse CSV", columns=None, row_count=None)

    columns = list(df.columns)
    row_count = len(df)

    # DKB detection: look for specific column pattern
    if _is_dkb_format(columns):
        return ValidationResult(
            valid=True,
            format="dkb",
            message="DKB CSV format detected",
            columns=columns,
            row_count=row_count,
        )

    # Comdirect detection (future)
    if _is_comdirect_format(columns):
        return ValidationResult(
            valid=True,
            format="comdirect",
            message="Comdirect CSV format detected",
            columns=columns,
            row_count=row_count,
        )

    # Trade Republic detection (future)
    if _is_trade_republic_format(columns):
        return ValidationResult(
            valid=True,
            format="trade_republic",
            message="Trade Republic CSV format detected",
            columns=columns,
            row_count=row_count,
        )

    return ValidationResult(
        valid=False,
        format=None,
        message="Unknown CSV format. Supported: DKB, Comdirect, Trade Republic",
        columns=columns,
        row_count=row_count,
    )


def _is_dkb_format(columns: list[str]) -> bool:
    """Check if CSV has DKB format markers."""
    dkb_markers = {"Buchungstag", "Wertstellung", "Umsatztyp", "Begünstigter / Auftraggeber"}
    return any(marker in columns for marker in dkb_markers)


def _is_comdirect_format(columns: list[str]) -> bool:
    """Check if CSV has Comdirect format markers."""
    # Placeholder for Comdirect detection
    return False


def _is_trade_republic_format(columns: list[str]) -> bool:
    """Check if CSV has Trade Republic format markers."""
    # Placeholder for Trade Republic detection
    return False
