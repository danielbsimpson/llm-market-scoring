"""Tests for the SnacksParser adapter."""
from __future__ import annotations

from datetime import timezone
from pathlib import Path

import pytest

from app.ingestion.parsers.base import ParsedArticle
from app.ingestion.parsers.snacks import SnacksParser

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"


@pytest.fixture(scope="module")
def articles() -> list[ParsedArticle]:
    return SnacksParser().parse(FIXTURE)


def test_returns_two_articles(articles):
    assert len(articles) == 2


def test_article_fields_present(articles):
    a = articles[0]
    assert isinstance(a, ParsedArticle)
    assert a.external_id  # should be the Message-ID
    assert a.title        # decoded subject
    assert a.text         # non-empty cleaned body
    assert len(a.content_hash) == 64  # SHA-256 hex


def test_external_ids_match_message_ids(articles):
    assert articles[0].external_id == "test001@mail.robinhood.com"
    assert articles[1].external_id == "test002@mail.robinhood.com"


def test_titles_are_decoded_subjects(articles):
    assert articles[0].title == "Tech stocks surge on AI optimism"
    assert articles[1].title == "Energy sector pullback"


def test_published_at_is_timezone_aware(articles):
    for a in articles:
        assert a.published_at is not None
        assert a.published_at.tzinfo is not None


def test_published_at_values(articles):
    from datetime import datetime
    assert articles[0].published_at == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert articles[1].published_at == datetime(2024, 1, 16, 10, 30, 0, tzinfo=timezone.utc)


def test_hashes_are_unique(articles):
    hashes = [a.content_hash for a in articles]
    assert len(hashes) == len(set(hashes))


def test_parse_is_idempotent():
    """Parsing the same file twice must return identical results."""
    parser = SnacksParser()
    first = parser.parse(FIXTURE)
    second = parser.parse(FIXTURE)
    assert len(first) == len(second)
    assert [a.external_id for a in first] == [a.external_id for a in second]
    assert [a.content_hash for a in first] == [a.content_hash for a in second]
