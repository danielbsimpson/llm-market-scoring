"""yfinance-backed OHLCV fetcher with Parquet caching and incremental updates.

This module is intentionally free of database imports — it reads/writes only
Parquet files so it can be tested without a live DB and reused from notebooks.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from app.config import settings

logger = logging.getLogger(__name__)

_DEFAULT_START = "2015-01-01"
_RATE_LIMIT_SLEEP = 0.5  # seconds between successive ticker downloads


class YFinanceClient:
    """Download and cache daily OHLCV data to Parquet files.

    Each symbol is cached in ``<cache_dir>/<SYMBOL>.parquet``.
    ``fetch_incremental`` only downloads the tail not already cached, making
    repeated runs fast.
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (settings.data_dir / "prices")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Cache helpers
    # ------------------------------------------------------------------ #

    def cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol.upper()}.parquet"

    def load_cached(self, symbol: str) -> pd.DataFrame | None:
        """Load previously saved Parquet prices. Returns None on cache miss or error."""
        path = self.cache_path(symbol)
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s", symbol, exc)
            return None

    def _save(self, symbol: str, df: pd.DataFrame) -> None:
        df.to_parquet(self.cache_path(symbol))

    # ------------------------------------------------------------------ #
    # Download helpers
    # ------------------------------------------------------------------ #

    def _download(
        self,
        symbol: str,
        start: str | date,
        end: str | date | None = None,
    ) -> pd.DataFrame:
        """Download OHLCV from yfinance. Returns a normalised DataFrame indexed by date.

        Handles both flat and MultiIndex column layouts produced by different
        yfinance versions (MultiIndex was introduced around 0.2.38).
        """
        df: pd.DataFrame = yf.download(
            symbol,
            start=str(start),
            end=str(end) if end is not None else None,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if df.empty:
            return df

        # yfinance >= 0.2.38 wraps single-ticker columns in a MultiIndex.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Normalise column names.
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]

        # Ensure timezone-naive DatetimeIndex named "date".
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index.name = "date"

        # Keep only the price/volume columns we care about.
        wanted = ["open", "high", "low", "close", "adj_close", "volume"]
        df = df[[c for c in wanted if c in df.columns]].copy()

        return df

    # ------------------------------------------------------------------ #
    # Public fetch API
    # ------------------------------------------------------------------ #

    def fetch(
        self,
        symbol: str,
        start: str | date = _DEFAULT_START,
        end: str | date | None = None,
    ) -> pd.DataFrame:
        """Download full price history and overwrite the cache."""
        df = self._download(symbol, start=start, end=end)
        if not df.empty:
            self._save(symbol, df)
        return df

    def fetch_incremental(
        self,
        symbol: str,
        start: str | date = _DEFAULT_START,
    ) -> pd.DataFrame:
        """Return full price history, fetching only the missing tail from yfinance.

        Algorithm:
        1. Load existing Parquet cache.
        2. Compute ``fetch_start`` = day after the last cached date (or ``start``).
        3. Download ``[fetch_start, today)`` from yfinance.
        4. Concatenate, deduplicate, sort, save, and return.
        """
        cached = self.load_cached(symbol)

        if cached is not None and not cached.empty:
            last_date = pd.Timestamp(cached.index.max()).date()
            fetch_start = last_date + timedelta(days=1)
            if fetch_start > date.today():
                logger.debug("%s: already up to date (last=%s)", symbol, last_date)
                return cached
        else:
            cached = None
            fetch_start = start

        logger.info("Fetching %s from %s …", symbol, fetch_start)
        new_data = self._download(symbol, start=fetch_start)

        if cached is not None and not new_data.empty:
            combined = pd.concat([cached, new_data])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
        elif not new_data.empty:
            combined = new_data
        else:
            # Nothing new — return whatever we had (may be empty).
            return cached if cached is not None else pd.DataFrame()

        self._save(symbol, combined)
        return combined

    def fetch_universe(
        self,
        symbols: list[str],
        start: str | date = _DEFAULT_START,
        delay: float = _RATE_LIMIT_SLEEP,
    ) -> dict[str, pd.DataFrame]:
        """Incrementally refresh a list of symbols with basic rate-limit spacing.

        Returns a ``symbol → DataFrame`` mapping. Symbols that fail are logged
        and returned as empty DataFrames so the caller can continue.
        """
        results: dict[str, pd.DataFrame] = {}
        for i, symbol in enumerate(symbols):
            if i > 0:
                time.sleep(delay)
            try:
                df = self.fetch_incremental(symbol, start=start)
                results[symbol] = df
                logger.info(
                    "%s: %d rows (last=%s)",
                    symbol,
                    len(df),
                    df.index.max().date() if not df.empty else "n/a",
                )
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", symbol, exc)
                results[symbol] = pd.DataFrame()
        return results
