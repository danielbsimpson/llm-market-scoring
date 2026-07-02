"""Tests for normalize.ensure_source and normalize.upsert_articles."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from app.ingestion.normalize import ensure_source, upsert_articles
from app.ingestion.parsers.base import ParsedArticle


def _make_article(n: int) -> ParsedArticle:
    text = f"Article body number {n}. Some financial content here."
    return ParsedArticle(
        external_id=f"ext-{n}",
        title=f"Newsletter Issue {n}",
        url=None,
        published_at=datetime(2024, 1, n + 1, 10, 0, tzinfo=timezone.utc),
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


# ---------------------------------------------------------------------------
# ensure_source
# ---------------------------------------------------------------------------

def test_ensure_source_creates_row(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    db.commit()
    assert source.id is not None
    assert source.name == "Test Newsletter"
    assert source.parser_key == "test_parser"
    assert source.active is True


def test_ensure_source_returns_same_id_on_repeat(db):
    s1 = ensure_source(db, "Test Newsletter", "test_parser")
    db.commit()
    s2 = ensure_source(db, "Test Newsletter", "test_parser")
    assert s1.id == s2.id


def test_ensure_source_default_type_is_newsletter(db):
    source = ensure_source(db, "My Source", "some_parser")
    assert source.type == "newsletter"


def test_ensure_source_custom_type(db):
    source = ensure_source(db, "My Blog", "generic_html", source_type="blog")
    assert source.type == "blog"


# ---------------------------------------------------------------------------
# upsert_articles
# ---------------------------------------------------------------------------

def test_upsert_inserts_new_articles(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    inserted, skipped = upsert_articles(db, [_make_article(0), _make_article(1)], source)
    assert inserted == 2
    assert skipped == 0


def test_upsert_skips_existing_by_external_id(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    upsert_articles(db, [_make_article(0)], source)
    inserted, skipped = upsert_articles(db, [_make_article(0)], source)
    assert inserted == 0
    assert skipped == 1


def test_upsert_skips_existing_by_hash(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    a = _make_article(0)
    upsert_articles(db, [a], source)
    # Same content but different external_id — should still be skipped by hash.
    duplicate = ParsedArticle(
        external_id="different-ext-id",
        title="Same content, different id",
        url=None,
        published_at=a.published_at,
        text=a.text,
        content_hash=a.content_hash,
    )
    inserted, skipped = upsert_articles(db, [duplicate], source)
    assert inserted == 0
    assert skipped == 1


def test_upsert_partial_deduplication(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    upsert_articles(db, [_make_article(0)], source)
    inserted, skipped = upsert_articles(db, [_make_article(0), _make_article(1)], source)
    assert inserted == 1
    assert skipped == 1


def test_upsert_deduplicates_within_batch(db):
    """Duplicates within a single batch call should not both be inserted."""
    source = ensure_source(db, "Test Newsletter", "test_parser")
    a = _make_article(0)
    inserted, skipped = upsert_articles(db, [a, a], source)
    assert inserted == 1
    assert skipped == 1


def test_upsert_empty_list(db):
    source = ensure_source(db, "Test Newsletter", "test_parser")
    inserted, skipped = upsert_articles(db, [], source)
    assert inserted == 0
    assert skipped == 0


def test_upsert_stores_naive_utc_published_at(db):
    from app.db.models import Article
    source = ensure_source(db, "Test Newsletter", "test_parser")
    a = _make_article(5)
    upsert_articles(db, [a], source)
    row = db.query(Article).filter_by(source_id=source.id).first()
    assert row is not None
    # Stored datetime must be naive (no tzinfo) for SQLite compatibility.
    assert row.published_at.tzinfo is None
    # Must be UTC: 2024-01-06 10:00
    assert row.published_at == datetime(2024, 1, 6, 10, 0)
