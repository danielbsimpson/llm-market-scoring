"""Abstract base classes and shared dataclasses for ingestion parsers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(slots=True)
class ParsedArticle:
    """Single article parsed from any source, ready for DB normalization."""

    external_id: str          # unique identifier within the source (e.g. Message-ID)
    title: str | None         # subject / headline
    url: str | None           # canonical URL if known
    published_at: datetime | None  # tz-aware preferred; naive is treated as UTC downstream
    text: str                 # clean editorial body text
    content_hash: str         # SHA-256 hex of *text* for deduplication


class ParserBase(ABC):
    """Abstract base for all ingestion parsers.

    Subclasses **must** set:

    * ``PARSER_KEY`` — unique registry name (e.g. ``"robinhood_snacks"``).
    * ``SUPPORTED_EXTENSIONS`` — lowercase dot-prefixed file extensions handled
      by default auto-detection (e.g. ``(".mbox",)``).  Set to ``()`` if the
      parser should only be used when an explicit ``parser_key`` is supplied.
    """

    PARSER_KEY: str
    SUPPORTED_EXTENSIONS: tuple[str, ...]

    @abstractmethod
    def parse(self, path: Path) -> list[ParsedArticle]:
        """Parse *path* and return zero or more articles."""
        ...
