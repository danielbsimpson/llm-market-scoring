"""Generic single-file parsers for plain text, Markdown, HTML, and email (.eml).

Each file maps to exactly one :class:`~app.ingestion.parsers.base.ParsedArticle`.
``published_at`` is resolved from (in order of preference):

1. YAML-style front-matter ``date:`` field (first 512 bytes of the file).
2. ISO-ish date embedded in the filename (e.g. ``2024-01-15-article.md``).
3. File modification time (mtime), converted to UTC.
"""
from __future__ import annotations

import email as _email_lib
import hashlib
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

from app.ingestion.parsers.base import ParsedArticle, ParserBase

# Matches ISO-style date in a filename: 2024-01-15, 2024_01_15, 20240115
_FILENAME_DATE_RE = re.compile(r"(\d{4})[_-]?(\d{2})[_-]?(\d{2})")
# Matches YAML front-matter date line anywhere in the first 512 bytes.
_FRONTMATTER_DATE_RE = re.compile(
    r"^date\s*:\s*(\d{4}[_-]\d{2}[_-]\d{2})", re.MULTILINE | re.IGNORECASE
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _date_from_filename(path: Path) -> datetime | None:
    m = _FILENAME_DATE_RE.search(path.stem)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _date_from_frontmatter(text: str) -> datetime | None:
    m = _FRONTMATTER_DATE_RE.search(text[:512])
    if not m:
        return None
    raw = m.group(1).replace("_", "-")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class GenericTextParser(ParserBase):
    """Plain text and Markdown files — each file is one article."""

    PARSER_KEY = "generic_text"
    SUPPORTED_EXTENSIONS = (".md", ".text")
    # NOTE: .txt is intentionally excluded here because it is ambiguous (could
    # be an mbox archive).  Register it only if you are sure no mbox files will
    # be dropped in the inbox.

    def parse(self, path: Path) -> list[ParsedArticle]:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return []
        pub = _date_from_frontmatter(text) or _date_from_filename(path) or _mtime_utc(path)
        return [
            ParsedArticle(
                external_id=f"file:{path.name}",
                title=path.stem.replace("-", " ").replace("_", " "),
                url=None,
                published_at=pub,
                text=text,
                content_hash=_sha256(text),
            )
        ]


class GenericHtmlParser(ParserBase):
    """HTML files — each file is one article; body text is stripped via BeautifulSoup."""

    PARSER_KEY = "generic_html"
    SUPPORTED_EXTENSIONS = (".html", ".htm")

    def parse(self, path: Path) -> list[ParsedArticle]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        raw_title = soup.title.string if soup.title and soup.title.string else None
        title = raw_title.strip() if raw_title else path.stem
        text = soup.get_text(separator="\n").strip()
        if not text:
            return []
        pub = _date_from_filename(path) or _mtime_utc(path)
        return [
            ParsedArticle(
                external_id=f"file:{path.name}",
                title=title,
                url=None,
                published_at=pub,
                text=text,
                content_hash=_sha256(text),
            )
        ]


class GenericEmailParser(ParserBase):
    """Individual email files (.eml) — each file is one article."""

    PARSER_KEY = "generic_email"
    SUPPORTED_EXTENSIONS = (".eml",)

    def parse(self, path: Path) -> list[ParsedArticle]:
        msg = _email_lib.message_from_bytes(path.read_bytes())

        # Title from Subject header.
        raw_subject = msg.get("Subject") or ""
        try:
            title: str | None = str(make_header(decode_header(raw_subject))).strip() or None
        except Exception:
            title = raw_subject.strip() or None

        # Date.
        pub: datetime | None = None
        raw_date = msg.get("Date")
        if raw_date:
            try:
                dt = parsedate_to_datetime(raw_date)
                pub = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        if pub is None:
            pub = _mtime_utc(path)

        # External ID from Message-ID header, else filename.
        mid = (msg.get("Message-ID") or "").strip().strip("<>")
        external_id = mid or f"file:{path.name}"

        text = self._extract_text(msg)
        if not text:
            return []
        return [
            ParsedArticle(
                external_id=external_id,
                title=title,
                url=None,
                published_at=pub,
                text=text,
                content_hash=_sha256(text),
            )
        ]

    @staticmethod
    def _extract_text(msg: _email_lib.message.Message) -> str:
        """Extract body text, preferring text/plain, falling back to text/html."""
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if ct == "text/plain":
                plain_parts.append(decoded)
            elif ct == "text/html":
                html_parts.append(decoded)

        if plain_parts:
            return "\n".join(plain_parts).strip()
        if html_parts:
            soup = BeautifulSoup("\n".join(html_parts), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            return soup.get_text(separator="\n").strip()
        return ""
