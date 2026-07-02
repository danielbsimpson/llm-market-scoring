"""Ingestion orchestrator: resolve the right parser, parse file(s), upsert to DB.

The single public entry point is :func:`ingest_path`.  It handles both single
files and directories, dispatches to the correct parser, and delegates
DB persistence to :mod:`app.ingestion.normalize`.

How to add a new source
-----------------------
1. Create a subclass of :class:`~app.ingestion.parsers.base.ParserBase` with
   a unique ``PARSER_KEY`` and the ``parse()`` method implemented.
2. Register it::

       from app.ingestion import parsers
       parsers.register(MyParser())

3. Call ``ingest_path`` with ``parser_key="my_parser_key"``, or drop files with
   a matching extension into the inbox directory for auto-detection.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.ingestion import parsers as _parsers
from app.ingestion.normalize import ensure_source, upsert_articles

log = logging.getLogger(__name__)


def ingest_path(
    path: Path,
    source_name: str,
    db: Session,
    *,
    parser_key: str | None = None,
) -> dict:
    """Ingest one file or every supported file under a directory.

    Args:
        path: A file or directory to ingest.
        source_name: Human-readable source name stored in the ``sources`` table.
        db: An active SQLAlchemy session.
        parser_key: Use this specific parser.  If ``None``, auto-detect by
            file extension.  **Required** when the extension is ambiguous
            (e.g. a ``.txt`` mbox archive should specify
            ``parser_key="robinhood_snacks"``).

    Returns:
        A ``dict`` with keys:
        ``source_id``, ``files_processed``, ``articles_inserted``,
        ``articles_skipped``.

    Raises:
        KeyError: if *parser_key* is given but not registered.
    """
    if path.is_dir():
        files = sorted(f for f in path.rglob("*") if f.is_file())
    else:
        files = [path]

    total_inserted = 0
    total_skipped = 0
    files_processed = 0
    source = None

    for file in files:
        # Resolve parser: explicit key takes priority; otherwise auto-detect.
        if parser_key is not None:
            parser = _parsers.get_by_key(parser_key)  # raises KeyError if unknown
        else:
            parser = _parsers.get_by_extension(file.suffix)
            if parser is None:
                log.debug("No parser registered for extension '%s', skipping %s", file.suffix, file)
                continue

        log.info("Ingesting %s with parser '%s'", file, parser.PARSER_KEY)
        try:
            parsed = parser.parse(file)
        except Exception:
            log.exception("Parser '%s' raised an exception on %s", parser.PARSER_KEY, file)
            continue

        files_processed += 1
        if not parsed:
            log.debug("Parser returned no articles for %s", file)
            continue

        # Lazy-create the Source row on first successful parse.
        if source is None:
            source = ensure_source(db, source_name, parser.PARSER_KEY)

        ins, skp = upsert_articles(db, parsed, source)
        total_inserted += ins
        total_skipped += skp
        log.info("  → %d inserted, %d skipped", ins, skp)

    # If no files matched any parser, still register the source row so the
    # caller always gets a valid source_id back.
    if source is None:
        if parser_key is not None:
            effective_key = _parsers.get_by_key(parser_key).PARSER_KEY
        else:
            effective_key = "unknown"
        source = ensure_source(db, source_name, effective_key)
        db.commit()

    return {
        "source_id": source.id,
        "files_processed": files_processed,
        "articles_inserted": total_inserted,
        "articles_skipped": total_skipped,
    }
