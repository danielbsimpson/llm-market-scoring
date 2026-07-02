"""FastAPI routes for the ingestion pipeline."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.ingestion import parsers as _parsers
from app.ingestion.loader import ingest_path

router = APIRouter(prefix="/ingest", tags=["ingestion"])


class IngestRequest(BaseModel):
    source_name: str = Field(..., description="Human-readable source name (stored in sources table).")
    path: str = Field(
        ...,
        description=(
            "File or directory to ingest.  Must be within the configured data directory.  "
            "Relative paths are resolved against the backend data directory."
        ),
    )
    parser_key: str | None = Field(
        default=None,
        description=(
            "Explicit parser key (e.g. 'robinhood_snacks').  "
            "If omitted, the parser is auto-detected by file extension.  "
            "Use --list-parsers from the CLI to see available keys."
        ),
    )


class IngestResponse(BaseModel):
    source_id: int
    files_processed: int
    articles_inserted: int
    articles_skipped: int


class ParsersResponse(BaseModel):
    parsers: list[dict]


@router.get("/parsers", response_model=ParsersResponse, summary="List registered parsers")
def list_parsers() -> ParsersResponse:
    """Return all registered parser keys and their supported file extensions."""
    result = []
    for key in _parsers.registered_keys():
        p = _parsers.get_by_key(key)
        result.append({
            "parser_key": key,
            "supported_extensions": list(p.SUPPORTED_EXTENSIONS),
        })
    return ParsersResponse(parsers=result)


@router.post("", response_model=IngestResponse, summary="Trigger ingestion")
def trigger_ingest(
    req: IngestRequest,
    db: Session = Depends(get_db),
) -> IngestResponse:
    """Ingest a file or directory into the database.

    The ``path`` must be within the configured data directory
    (``settings.data_dir``).  Ingestion is idempotent: articles already
    present in the database are skipped.
    """
    # Resolve path; accept both absolute and data-dir-relative inputs.
    raw = Path(req.path)
    if not raw.is_absolute():
        raw = settings.data_dir / raw
    resolved = raw.resolve()

    # Security: reject paths outside the data directory (prevents traversal).
    data_dir = settings.data_dir.resolve()
    try:
        resolved.relative_to(data_dir)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Path must be within the data directory ({data_dir}).",
        )

    if not resolved.exists():
        raise HTTPException(status_code=422, detail=f"Path not found: {req.path}")

    # Validate parser_key before doing any work.
    if req.parser_key is not None:
        try:
            _parsers.get_by_key(req.parser_key)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    result = ingest_path(resolved, req.source_name, db, parser_key=req.parser_key)
    return IngestResponse(**result)
