"""CLI entry-point for the LLM batch scorer.

Usage::

    # Score up to 10 articles with the default prompt and model
    python -m app.llm.scorer --limit 10

    # Score all articles with a specific prompt and model
    python -m app.llm.scorer --prompt multi_asset --model qwen2.5:7b

    # Score specific articles by ID
    python -m app.llm.scorer --article-ids 1 2 3

    # List available prompt files
    python -m app.llm.scorer --list-prompts
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.db.session import SessionLocal
from app.llm.scorer import PromptLoader, Scorer


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.llm.scorer",
        description="Batch-score articles through an LLM prompt.",
    )
    p.add_argument(
        "--prompt",
        default="multi_asset",
        metavar="NAME",
        help="Prompt file name without .md (default: multi_asset).",
    )
    p.add_argument(
        "--model",
        default=None,
        metavar="REF",
        help="Model name or ref (e.g. qwen2.5:7b). Defaults to settings.llm_model.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of articles to process (default: all).",
    )
    p.add_argument(
        "--article-ids",
        nargs="+",
        type=int,
        default=None,
        metavar="ID",
        help="Score only these article IDs.",
    )
    p.add_argument(
        "--list-prompts",
        action="store_true",
        help="List available prompt files and exit.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list_prompts:
        with SessionLocal() as db:
            loader = PromptLoader(db)
        for name in loader.list_available():
            print(f"  {name}")
        return

    scorer = Scorer()
    with SessionLocal() as db:
        try:
            stats = scorer.score_batch(
                db,
                prompt_name=args.prompt,
                model_name=args.model,
                article_ids=args.article_ids,
                limit=args.limit,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)

    print(
        f"Done — articles processed: {stats['articles_processed']}, "
        f"scores inserted: {stats['scores_inserted']}, "
        f"skipped (already scored): {stats['scores_skipped']}, "
        f"missing (LLM omitted): {stats['scores_missing']}, "
        f"errors: {stats['errors']}"
    )


if __name__ == "__main__":
    main()
