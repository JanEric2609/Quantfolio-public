"""Imports API endpoints for CSV broker statement uploads."""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.foundation.models.entities import User
from app.foundation.auth import current_user
from app.foundation.core.db import get_db
from app.foundation.imports import commit_import, parse_csv, validate_csv

log = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

FormatType = Literal["dkb", "comdirect", "trade_republic"]

router = APIRouter(prefix="/api/imports", tags=["imports"])


class ValidationResponse(BaseModel):
    valid: bool
    format: str | None
    message: str
    columns: list[str] | None
    row_count: int | None


class ImportResponse(BaseModel):
    created_ids: list[str]
    errors: list[str]


@router.post("/validate", response_model=ValidationResponse)
async def validate_csv_import(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Validate CSV file format."""
    try:
        # Check UploadFile.size if available
        if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )
        # Bounded read: read up to limit + 1 to detect overflow
        content = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )
        result = validate_csv(content)
        return {
            "valid": result.valid,
            "format": result.format,
            "message": result.message,
            "columns": result.columns,
            "row_count": result.row_count,
        }
    except HTTPException:
        raise
    except Exception:
        log.exception("CSV validation failed for file %s", file.filename)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Validation failed")


@router.post("/commit", response_model=ImportResponse)
async def commit_csv_import(
    file: UploadFile = File(...),
    format_type: Annotated[FormatType, "Broker CSV format"] = "dkb",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Parse and commit CSV transactions to the database."""
    try:
        # Check UploadFile.size if available
        if file.size is not None and file.size > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )
        # Bounded read: read up to limit + 1 to detect overflow
        content = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File exceeds maximum allowed size of {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            )

        # Validate first
        validation = validate_csv(content)
        if not validation.valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid CSV: {validation.message}",
            )

        # Parse
        transactions, parse_errors = parse_csv(content, format_type)
        if parse_errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Parse errors: {parse_errors}",
            )

        # Commit
        created_ids, commit_errors = commit_import(
            db,
            user_id=user.id,
            transactions=transactions,
            source=f"csv_import_{format_type}",
        )

        return {"created_ids": created_ids, "errors": commit_errors}

    except HTTPException:
        raise
    except Exception:
        log.exception("CSV import failed for file %s (format=%s)", file.filename, format_type)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Import failed",
        )
