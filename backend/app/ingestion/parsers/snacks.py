"""Pluggable parser adapter for the Robinhood Snacks mbox export.

Delegates all heavy lifting to :mod:`app.ingestion.snacks` (the existing,
fully-verified implementation) and adapts ``SnackArticle`` records to the
common :class:`~app.ingestion.parsers.base.ParsedArticle` interface.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.ingestion.parsers.base import ParsedArticle, ParserBase


class SnacksParser(ParserBase):
    """Parse the Robinhood Snacks mbox export into :class:`ParsedArticle` records."""

    PARSER_KEY = "robinhood_snacks"
    # The canonical export is a .txt mbox archive — extension alone is ambiguous,
    # so callers should specify parser_key="robinhood_snacks" explicitly for .txt
    # files.  We register .mbox for unambiguous auto-detection.
    SUPPORTED_EXTENSIONS = (".mbox",)

    def parse(self, path: Path) -> list[ParsedArticle]:
        # Late import to avoid circular dependency at module load time.
        from app.ingestion import snacks as _snacks

        raw_articles = _snacks.parse_mbox(path)
        results: list[ParsedArticle] = []
        for a in raw_articles:
            pub: datetime | None = None
            if a.published_at:
                try:
                    pub = datetime.fromisoformat(a.published_at)
                except ValueError:
                    pass
            results.append(
                ParsedArticle(
                    external_id=a.external_id,
                    title=a.subject or None,
                    url=None,
                    published_at=pub,
                    text=a.text,
                    content_hash=a.content_hash,
                )
            )
        return results
