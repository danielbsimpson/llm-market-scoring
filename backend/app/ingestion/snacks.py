"""Parse the Robinhood "Snacks" newsletter export into clean, LLM-ready records.

The export (e.g. ``data/Robinhood_Snacks.txt``) is a standard **mbox** file: a
concatenation of raw RFC-822 email messages, each beginning with a ``From `` line.
Every message carries the newsletter as a quoted-printable ``text/html`` part (the
``text/plain`` parts in this export are empty), so we extract and clean the HTML.

For "version 0" we produce one record per newsletter with:

* ``external_id``  -- the email ``Message-ID`` (used for de-duplication)
* ``published_at`` -- an accurate, timezone-aware timestamp from the ``Date`` header
* ``subject``      -- the RFC-2047 decoded subject line
* ``sender``       -- the ``From`` header
* ``text``         -- cleaned editorial body text (footer/legal boilerplate removed)

Run as a script::

    python -m app.ingestion.snacks \
        --input data/Robinhood_Snacks.txt \
        --output data/processed/snacks_v0.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mailbox
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

# Tags whose text content should never appear in the output.
_DROP_TAGS = ("style", "script", "head", "title", "noscript")

# Block-level tags after which we insert a line break so paragraphs survive
# the flattening done by BeautifulSoup.get_text().
_BLOCK_TAGS = (
    "p", "div", "br", "li", "tr", "table", "ul", "ol",
    "blockquote", "section", "header", "footer", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
)

# Once any of these markers is seen, the rest of the email is legal/footer/CTA
# boilerplate and is discarded. Matched case-insensitively as a substring of a
# line. Covers both the modern "Sherwood" footer and the older "Robinhood" one.
_FOOTER_MARKERS = (
    "advertiser's disclosures",
    "advertisers disclosures",
    "was this email forwarded to you",
    "sherwood media, llc produces",
    "sherwood terms and conditions",
    "do not necessarily reflect the views",
    "reflect the opinions of only the authors",
    "authors of this snacks own shares",
    "disclosure: the authors of this snacks",
    "robinhood terms and conditions",
    "to unsubscribe from all commercial emails",
)

# Whole lines (after normalisation) that are pure navigation/legal/CTA chrome.
_NOISE_EXACT = frozenset(
    s.lower()
    for s in (
        "unsubscribe", "privacy policy", "contact us", "advertise with us",
        "our editorial standards", "sherwood terms and conditions",
        "see more", "read more", "see the pics", "presented by", "disclosures",
        "subscribe", "subscribe to the daily", "the daily newsletter",
        "want your snacks daily?", "last week's market moves", "his tips for young investors",
    )
)

# Substrings that mark a line as subscription/promo chrome to drop in place.
_NOISE_SUBSTR = (
    "manage your subscription",
    "want to start getting snacks",
    "want to start snacking",
    "want your snacks daily",
    "subscribe to the daily",
    "subscribe to entrypoint",
    "sign up here",
    "sign up for our daily",
    "try a sample",
    "get fresh takes on financial news",
    "for more market trends and trading insights",
    "to unsubscribe",
    "check your answer",
)

# Section labels (normalised) used both to keep structure and to bound ad blocks.
_SECTION_LABELS = (
    "what else we're snackin", "snacks shots", "snack fact", "this week",
    "zoom out", "the takeaway", "the best thing we read today", "icymi",
    "events", "highs", "lows", "shades", "whoa", "bitten",
)

# Index names that appear as standalone cells in the "Market Moves" table.
_INDEX_NAMES = frozenset(
    s.lower()
    for s in (
        "dow jones", "s&p 500", "nasdaq", "nasdaq 100", "bitcoin",
        "russell 2000", "10-yr us treasury", "10-year us treasury",
    )
)

_AD_START_RE = re.compile(r"^[\W\s]*(presented by|sponsored by)\b", re.IGNORECASE)
# A number followed by a (+/-x%) change, e.g. "25,764 (-0.69%)" or "$7,964 (+14.07%)".
_MARKET_MOVE_RE = re.compile(r"^\$?[\d,]+\.?\d*\s*\([+\-−]?\d+\.?\d*%\)")
_PCT_ONLY_RE = re.compile(r"^\$?[\d,]+\.?\d*%$")
# A line wholly wrapped in matching () or [] -> usually a photo credit / disclosure.
_WRAPPED_RE = re.compile(r"^[\(\[].*[\)\]]$")
# A trailing image-credit parenthetical, e.g. "(Justin Sullivan/Getty Images)".
_CREDIT_RE = re.compile(
    r"[\(\[][^\)\]]*("
    r"getty|reuters|bloomberg|shutterstock|associated press|/[A-Za-z ]*images"
    r")[^\)\]]*[\)\]]\s*$",
    re.IGNORECASE,
)
# A "sample article" promo link, e.g. "Amazon's privacy fines • Jun 2, 2023".
_SAMPLE_LINK_RE = re.compile(r"•\s*[A-Z][a-z]{2,8}\.?\s\d{1,2},?\s\d{4}\s*$")


def _norm(text: str) -> str:
    """Normalise curly punctuation so marker matching is reliable."""
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')


def _is_section_header(line: str) -> bool:
    """True if the line is a short section heading (used to bound ad blocks)."""
    s = line.strip()
    if not s:
        return False
    low = _norm(s.lower()).strip(" '\".")
    if any(low.startswith(label) for label in _SECTION_LABELS):
        return True
    letters = [c for c in s if c.isalpha()]
    if letters and len(s) <= 60 and len(s.split()) <= 8:
        if sum(c.isupper() for c in letters) / len(letters) >= 0.8:
            return True
    return False


def _is_noise(line: str) -> bool:
    """True if the line is non-editorial chrome that should be dropped."""
    s = line.strip()
    if not s:
        return True
    low = _norm(s.lower())
    if low in _NOISE_EXACT:
        return True
    if any(sub in low for sub in _NOISE_SUBSTR):
        return True
    if s.startswith("*"):
        return True
    if _WRAPPED_RE.match(s):
        return True
    if _CREDIT_RE.search(s) and len(s) <= 140:
        return True
    if _MARKET_MOVE_RE.match(s) or _PCT_ONLY_RE.match(s):
        return True
    if low.endswith("market moves") and len(low.split()) <= 4:  # table header
        return True
    if low in _INDEX_NAMES:
        return True
    if _SAMPLE_LINK_RE.search(s):
        return True
    if re.fullmatch(r"\d{1,2}", s):  # stray advertiser-disclosure footnote number
        return True
    return False


@dataclass(slots=True)
class SnackArticle:
    """A single cleaned newsletter issue, ready to feed to the LLM scorer."""

    external_id: str
    source: str
    subject: str
    sender: str
    published_at: str  # ISO-8601 with timezone offset
    text: str
    word_count: int
    content_hash: str


def _decode_subject(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # Normalise naive datetimes to UTC so every record is timezone-aware.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_html(msg: Message) -> str | None:
    """Return the decoded ``text/html`` body of an email, if present."""
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in disposition.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return None


def _html_to_lines(html: str) -> list[str]:
    """Convert HTML to a list of clean text lines, preserving paragraph breaks."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    # Force a line break around block-level elements before flattening.
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text(separator=" ")
    text = text.replace("\xa0", " ").replace("\u200c", "").replace("\u200b", "")
    # Treat all Unicode line/paragraph separators as newlines...
    text = re.sub(r"[\r\u2028\u2029\x0b\x0c\x1c-\x1e\x85]", "\n", text)
    # ...and drop any remaining control characters that aren't tab/newline.
    text = re.sub(r"[\x00-\x08\x0e-\x1a\x1f\x7f-\x9f]", "", text)

    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        # Tidy stray spaces left before punctuation by inline-tag flattening.
        line = re.sub(r"\s+([,.;:!?%])", r"\1", line)
        if line:
            lines.append(line)
    return lines


def _clean_lines(lines: list[str]) -> list[str]:
    """Drop boilerplate, sponsor/ad blocks, quizzes and chrome from the body.

    Performs a single forward scan so that multi-line blocks (sponsor ads, quiz
    prompts) can be skipped as a unit:

    * a ``Presented by ...`` line starts an ad block that runs until the next
      section header;
    * a line announcing the quiz ("...the first question:") runs until the
      matching "Check your answer" line;
    * the first footer/legal marker truncates everything after it.
    """
    kept: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        low = _norm(line.lower())

        if any(marker in low for marker in _FOOTER_MARKERS):
            break  # rest of the email is legal/footer boilerplate

        if "first question" in low:  # quiz block
            i += 1
            while i < n and "check your answer" not in _norm(lines[i].lower()):
                i += 1
            i += 1  # also skip the "Check your answer" line
            continue

        if _AD_START_RE.match(line):  # sponsor/ad block until next section header
            i += 1
            steps = 0
            while i < n and steps < 16 and not _is_section_header(lines[i]):
                if any(m in _norm(lines[i].lower()) for m in _FOOTER_MARKERS):
                    break
                i += 1
                steps += 1
            continue  # leave the boundary line for the next iteration to handle

        if _is_noise(line):
            i += 1
            continue

        if kept and kept[-1] == line:  # collapse adjacent duplicates
            i += 1
            continue

        kept.append(line)
        i += 1
    return kept


def clean_html_body(html: str) -> str:
    """Full HTML -> clean editorial text pipeline."""
    lines = _clean_lines(_html_to_lines(html))
    return "\n".join(lines).strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_mbox(path: Path, source: str = "robinhood_snacks") -> list[SnackArticle]:
    """Parse an mbox export into de-duplicated, cleaned ``SnackArticle`` records."""
    mbox = mailbox.mbox(str(path))
    articles: list[SnackArticle] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()

    for msg in mbox:
        html = _extract_html(msg)
        if not html:
            continue
        text = clean_html_body(html)
        if not text:
            continue

        content_hash = _content_hash(text)
        message_id = (msg.get("Message-ID") or "").strip().strip("<>")
        external_id = message_id or content_hash

        if external_id in seen_ids or content_hash in seen_hashes:
            continue
        seen_ids.add(external_id)
        seen_hashes.add(content_hash)

        published = _parse_date(msg.get("Date"))
        articles.append(
            SnackArticle(
                external_id=external_id,
                source=source,
                subject=_decode_subject(msg.get("Subject")),
                sender=(msg.get("From") or "").strip(),
                published_at=published.isoformat() if published else "",
                text=text,
                word_count=len(text.split()),
                content_hash=content_hash,
            )
        )

    articles.sort(key=lambda a: a.published_at)
    return articles


def write_jsonl(articles: list[SnackArticle], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        for article in articles:
            fh.write(json.dumps(asdict(article), ensure_ascii=False) + "\n")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input", type=Path, default=Path("data/Robinhood_Snacks.txt"),
        help="Path to the mbox export file.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/processed/snacks_v0.jsonl"),
        help="Where to write the cleaned JSONL dataset.",
    )
    parser.add_argument(
        "--source", default="robinhood_snacks",
        help="Source label stored on each record.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    articles = parse_mbox(args.input, source=args.source)
    write_jsonl(articles, args.output)

    total_words = sum(a.word_count for a in articles)
    dated = [a for a in articles if a.published_at]
    span = ""
    if dated:
        span = f" | dates {dated[0].published_at[:10]} -> {dated[-1].published_at[:10]}"
    avg = round(total_words / len(articles)) if articles else 0
    print(
        f"Parsed {len(articles)} unique newsletters from {args.input} "
        f"(avg {avg} words){span}"
    )
    print(f"Wrote -> {args.output}")


if __name__ == "__main__":
    main()
