"""CLI for market data operations.

Examples
--------
# Refresh all universe symbols (incremental):
    python -m app.market

# Refresh specific symbols only:
    python -m app.market --symbols SPY QQQ VGT

# Override the start date for a full backfill:
    python -m app.market --start 2018-01-01

# Fetch prices but skip forward-return computation:
    python -m app.market --prices-only

# Show cache status without fetching:
    python -m app.market --status
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.config import settings
from app.db.session import SessionLocal, init_db
from app.market.returns import refresh_symbol
from app.market.universe import get_tradable_symbols
from app.market.yfinance_client import YFinanceClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_status(client: YFinanceClient, symbols: list[str]) -> None:
    """Print a summary of what is cached locally."""
    print(f"{'SYMBOL':<12}  {'ROWS':>6}  {'FIRST':>12}  {'LAST':>12}  {'CACHED'}")
    print("-" * 60)
    for sym in symbols:
        df = client.load_cached(sym)
        if df is None or df.empty:
            print(f"{sym:<12}  {'—':>6}  {'—':>12}  {'—':>12}  no")
        else:
            first = str(df.index.min().date())
            last = str(df.index.max().date())
            print(f"{sym:<12}  {len(df):>6}  {first:>12}  {last:>12}  yes")


def cmd_refresh(
    symbols: list[str],
    start: str,
    prices_only: bool,
) -> None:
    """Fetch prices and (optionally) compute forward returns for each symbol."""
    init_db()
    client = YFinanceClient()
    windows = settings.return_window_list

    total_prices = 0
    total_returns = 0
    errors: list[str] = []

    with SessionLocal() as db:
        for i, sym in enumerate(symbols):
            if i > 0:
                import time
                time.sleep(0.5)
            try:
                logger.info("[%d/%d] Fetching %s …", i + 1, len(symbols), sym)
                prices = client.fetch_incremental(sym, start=start)
                if prices.empty:
                    logger.warning("%s: no price data returned", sym)
                    continue

                if prices_only:
                    from app.market.returns import upsert_market_prices
                    n = upsert_market_prices(db, sym, prices)
                    total_prices += n
                    logger.info("%s: %d price rows upserted", sym, n)
                else:
                    stats = refresh_symbol(db, sym, prices, windows)
                    total_prices += stats["prices_upserted"]
                    total_returns += stats["returns_upserted"]
                    logger.info(
                        "%s: %d prices, %d return rows",
                        sym,
                        stats["prices_upserted"],
                        stats["returns_upserted"],
                    )
            except Exception as exc:
                logger.error("%s: %s", sym, exc)
                errors.append(f"{sym}: {exc}")

    print("\n=== Market refresh complete ===")
    print(f"  Symbols processed : {len(symbols) - len(errors)}")
    print(f"  Prices upserted   : {total_prices:,}")
    if not prices_only:
        print(f"  Returns upserted  : {total_returns:,}")
    if errors:
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.market",
        description="Fetch and cache market prices + forward returns.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="SYM",
        help="Specific symbols to refresh (default: full tradable universe).",
    )
    parser.add_argument(
        "--start",
        default="2015-01-01",
        metavar="YYYY-MM-DD",
        help="Start date for initial fetch (default: 2015-01-01).",
    )
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="Persist prices to DB but skip forward-return computation.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show cache status and exit (no network calls).",
    )
    args = parser.parse_args(argv)

    client = YFinanceClient()

    if args.status:
        # Status mode: derive symbol list without a DB connection if possible.
        try:
            init_db()
            with SessionLocal() as db:
                symbols = get_tradable_symbols(db)
        except Exception:
            symbols = []
        if not symbols:
            print("No universe symbols found in DB.")
            return 0
        cmd_status(client, symbols)
        return 0

    # Resolve symbols.
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        init_db()
        with SessionLocal() as db:
            symbols = get_tradable_symbols(db)
        if not symbols:
            print("No tradable symbols found in DB. Run `python -m app.db.seed` first.")
            return 1

    cmd_refresh(symbols, start=args.start, prices_only=args.prices_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
