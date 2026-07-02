"""Convert :class:`~app.ingestion.parsers.base.ParsedArticle` records to ORM
rows and upsert them into the database.

The two public functions here are intentionally low-level — they do not open
or close sessions.  Callers are responsible for session lifecycle.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Article, Source
from app.ingestion.parsers.base import ParsedArticle


def ensure_source(
    db: Session,
    name: str,
    parser_key: str,
    source_type: str = "newsletter",
) -> Source:
    """Return the existing :class:`Source` row, creating one if it doesn't exist.

    Uses ``db.flush()`` so the returned object has a valid ``id`` without
    committing the surrounding transaction.
    """
    source = db.query(Source).filter_by(name=name).first()
    if source is None:
        source = Source(name=name, type=source_type, parser_key=parser_key)
        db.add(source)
        db.flush()
    return source


def _to_naive_utc(dt: datetime) -> datetime:
    """Strip timezone info after converting to UTC.

    SQLite's :class:`~sqlalchemy.types.DateTime` stores naive datetimes; we
    normalise to UTC so all stored timestamps are comparable.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def upsert_articles(
    db: Session,
    articles: list[ParsedArticle],
    source: Source,
) -> tuple[int, int]:
    """Insert :class:`Article` rows that don't already exist in the database.

    Deduplication is checked against:

    * ``(source_id, external_id)`` — the natural unique key; and
    * ``(source_id, hash)`` — catches re-ingestion of renamed/moved files.

    Duplicates within the *articles* batch are also skipped.

    Args:
        db: An active SQLAlchemy session.
        articles: Parsed articles to persist.
        source: The :class:`Source` row that owns these articles.

    Returns:
        A ``(inserted, skipped)`` tuple.
    """
    if not articles:
        return 0, 0

    # Bulk-fetch existing keys for this source to avoid N+1 queries.
    existing_ext_ids: set[str] = {
        row[0]
        for row in db.query(Article.external_id).filter_by(source_id=source.id).all()
    }
    existing_hashes: set[str] = {
        row[0]
        for row in db.query(Article.hash).filter_by(source_id=source.id).all()
    }

    inserted = 0
    skipped = 0

    for a in articles:
        if a.external_id in existing_ext_ids or a.content_hash in existing_hashes:
            skipped += 1
            continue

        pub_at = _to_naive_utc(a.published_at) if a.published_at else datetime.utcnow()

        db.add(
            Article(
                source_id=source.id,
                external_id=a.external_id,
                title=a.title,
                url=a.url,
                published_at=pub_at,
                clean_text=a.text,
                hash=a.content_hash,
            )
        )
        # Update local sets so duplicates within the same batch are also caught.
        existing_ext_ids.add(a.external_id)
        existing_hashes.add(a.content_hash)
        inserted += 1

    db.commit()
    return inserted, skipped
