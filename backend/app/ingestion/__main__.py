"""CLI entry-point for the ingestion pipeline.

Usage::

    # Ingest the Snacks mbox export
    python -m app.ingestion \\
        --source "Robinhood Snacks" \\
        --path data/Robinhood_Snacks.txt \\
        --parser-key robinhood_snacks

    # Ingest all supported files in a directory (auto-detect parser by extension)
    python -m app.ingestion --source "My Newsletter" --path data/inbox/

    # List all registered parsers
    python -m app.ingestion --list-parsers
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.db.session import SessionLocal
from app.ingestion import parsers as _parsers
from app.ingestion.loader import ingest_path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.ingestion",
        description="Ingest newsletter/article files into the database.",
    )
    p.add_argument(
        "--source",
        metavar="NAME",
        help="Human-readable source name (stored in the sources table).",
    )
    p.add_argument(
        "--path",
        type=Path,
        metavar="PATH",
        help="File or directory to ingest.",
    )
    p.add_argument(
        "--parser-key",
        dest="parser_key",
        metavar="KEY",
        default=None,
        help=(
            "Explicit parser key (e.g. 'robinhood_snacks').  "
            "Required for .txt mbox archives where extension is ambiguous."
        ),
    )
    p.add_argument(
        "--list-parsers",
        action="store_true",
        help="Print all registered parser keys and exit.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list_parsers:
        keys = _parsers.registered_keys()
        print("Registered parsers:")
        for key in keys:
            parser = _parsers.get_by_key(key)
            exts = ", ".join(parser.SUPPORTED_EXTENSIONS) or "(none — explicit key only)"
            print(f"  {key:<30} extensions: {exts}")
        return

    if not args.source:
        print("error: --source is required", file=sys.stderr)
        sys.exit(1)
    if not args.path:
        print("error: --path is required", file=sys.stderr)
        sys.exit(1)

    path = args.path.resolve()
    if not path.exists():
        print(f"error: path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    with SessionLocal() as db:
        result = ingest_path(path, args.source, db, parser_key=args.parser_key)

    print(
        f"Source '{args.source}' (id={result['source_id']}) — "
        f"files processed: {result['files_processed']}, "
        f"articles inserted: {result['articles_inserted']}, "
        f"skipped (already exist): {result['articles_skipped']}"
    )


if __name__ == "__main__":
    main()
