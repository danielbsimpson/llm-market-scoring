"""Tests for app.market.yfinance_client — no live network calls."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from app.market.yfinance_client import YFinanceClient


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _price_df(dates: list[str], values: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame mimicking yfinance output."""
    idx = pd.DatetimeIndex(dates, name="date")
    close = values
    return pd.DataFrame(
        {
            "Open": close,
            "High": [v * 1.01 for v in close],
            "Low": [v * 0.99 for v in close],
            "Close": close,
            "Adj Close": close,
            "Volume": [1_000_000] * len(dates),
        },
        index=idx,
    )


def _multiindex_price_df(dates: list[str], values: list[float], symbol: str = "SPY") -> pd.DataFrame:
    """Build a yfinance-style MultiIndex DataFrame (yfinance >= 0.2.38)."""
    flat = _price_df(dates, values)
    flat.columns = pd.MultiIndex.from_tuples(
        [(col, symbol) for col in flat.columns]
    )
    return flat


# --------------------------------------------------------------------------- #
# fetch — flat columns
# --------------------------------------------------------------------------- #


def test_fetch_saves_parquet_and_normalises_columns(tmp_path):
    mock_df = _price_df(["2024-01-02", "2024-01-03", "2024-01-04"], [400.0, 405.0, 402.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock_df):
        client = YFinanceClient(cache_dir=tmp_path)
        df = client.fetch("SPY", start="2024-01-01")

    assert len(df) == 3
    assert list(df.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
    assert (tmp_path / "SPY.parquet").exists()


def test_fetch_handles_multiindex_columns(tmp_path):
    mock_df = _multiindex_price_df(["2024-01-02", "2024-01-03"], [400.0, 405.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock_df):
        client = YFinanceClient(cache_dir=tmp_path)
        df = client.fetch("SPY", start="2024-01-01")

    assert "adj_close" in df.columns
    assert len(df) == 2


def test_fetch_empty_returns_empty_df(tmp_path):
    with patch("app.market.yfinance_client.yf.download", return_value=pd.DataFrame()):
        client = YFinanceClient(cache_dir=tmp_path)
        df = client.fetch("UNKNOWN", start="2024-01-01")
    assert df.empty
    assert not (tmp_path / "UNKNOWN.parquet").exists()


# --------------------------------------------------------------------------- #
# load_cached
# --------------------------------------------------------------------------- #


def test_load_cached_returns_none_when_no_file(tmp_path):
    client = YFinanceClient(cache_dir=tmp_path)
    assert client.load_cached("AAPL") is None


def test_load_cached_returns_dataframe_after_fetch(tmp_path):
    mock_df = _price_df(["2024-01-02"], [100.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock_df):
        client = YFinanceClient(cache_dir=tmp_path)
        client.fetch("SPY")

    loaded = client.load_cached("SPY")
    assert loaded is not None
    assert len(loaded) == 1


# --------------------------------------------------------------------------- #
# fetch_incremental
# --------------------------------------------------------------------------- #


def test_fetch_incremental_combines_cached_and_new(tmp_path):
    dates1 = ["2024-01-02", "2024-01-03"]
    mock1 = _price_df(dates1, [400.0, 402.0])

    with patch("app.market.yfinance_client.yf.download", return_value=mock1):
        client = YFinanceClient(cache_dir=tmp_path)
        df1 = client.fetch_incremental("SPY", start="2024-01-01")
    assert len(df1) == 2

    # Second call: only the tail should be fetched.
    dates2 = ["2024-01-04", "2024-01-05"]
    mock2 = _price_df(dates2, [403.0, 405.0])

    download_starts: list[str] = []

    def capture_download(*args, start=None, **kwargs):
        download_starts.append(str(start))
        return mock2

    with patch("app.market.yfinance_client.yf.download", side_effect=capture_download):
        df2 = client.fetch_incremental("SPY", start="2024-01-01")

    assert len(df2) == 4
    # The incremental download must start AFTER the last cached date (2024-01-03).
    assert download_starts[0] > "2024-01-03"


def test_fetch_incremental_skips_download_when_up_to_date(tmp_path):
    """If the cache already covers today, no yf.download call should happen."""
    # Seed cache with today's date.
    today = pd.Timestamp.today().normalize()
    mock_df = _price_df([str(today.date())], [100.0])

    with patch("app.market.yfinance_client.yf.download", return_value=mock_df):
        client = YFinanceClient(cache_dir=tmp_path)
        client.fetch("SPY")

    call_count = 0

    def count_download(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return pd.DataFrame()

    with patch("app.market.yfinance_client.yf.download", side_effect=count_download):
        client.fetch_incremental("SPY")

    assert call_count == 0


def test_fetch_incremental_deduplicates_overlap(tmp_path):
    """Overlapping rows (same date) must not be doubled."""
    mock1 = _price_df(["2024-01-02", "2024-01-03"], [100.0, 101.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock1):
        client = YFinanceClient(cache_dir=tmp_path)
        client.fetch_incremental("SPY")

    # yfinance may include the boundary date again.
    mock2 = _price_df(["2024-01-03", "2024-01-04"], [101.5, 102.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock2):
        df = client.fetch_incremental("SPY")

    assert len(df) == 3  # 2024-01-02, 01-03, 01-04 — no duplicates


# --------------------------------------------------------------------------- #
# fetch_universe
# --------------------------------------------------------------------------- #


def test_fetch_universe_returns_dict(tmp_path):
    mock_df = _price_df(["2024-01-02", "2024-01-03"], [400.0, 401.0])
    with patch("app.market.yfinance_client.yf.download", return_value=mock_df):
        client = YFinanceClient(cache_dir=tmp_path)
        results = client.fetch_universe(["SPY", "QQQ"], delay=0)

    assert set(results.keys()) == {"SPY", "QQQ"}
    assert all(len(df) == 2 for df in results.values())


def test_fetch_universe_continues_after_error(tmp_path):
    """A failing symbol should not abort the rest of the batch."""
    calls: list[str] = []

    def mock_download(symbol, **kwargs):
        calls.append(symbol)
        if symbol == "FAIL":
            raise RuntimeError("simulated error")
        return _price_df(["2024-01-02"], [100.0])

    with patch("app.market.yfinance_client.yf.download", side_effect=mock_download):
        client = YFinanceClient(cache_dir=tmp_path)
        results = client.fetch_universe(["SPY", "FAIL", "QQQ"], delay=0)

    assert results["FAIL"].empty
    assert len(results["SPY"]) == 1
    assert len(results["QQQ"]) == 1
