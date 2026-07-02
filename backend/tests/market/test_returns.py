"""Tests for app.market.returns — pure pandas logic and DB persistence."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.db.models import Asset, AssetKind, ForwardReturn, MarketPrice
from app.market.returns import (
    align_to_next_session,
    compute_forward_returns,
    refresh_symbol,
    upsert_forward_returns,
    upsert_market_prices,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _prices(dates: list[str], adj_close: list[float]) -> pd.DataFrame:
    """Build a minimal price DataFrame with adj_close only."""
    idx = pd.DatetimeIndex(dates, name="date")
    return pd.DataFrame(
        {
            "open": adj_close,
            "high": [v * 1.01 for v in adj_close],
            "low": [v * 0.99 for v in adj_close],
            "close": adj_close,
            "adj_close": adj_close,
            "volume": [1_000_000.0] * len(dates),
        },
        index=idx,
    )


# --------------------------------------------------------------------------- #
# align_to_next_session
# --------------------------------------------------------------------------- #


def test_align_returns_next_session_after_date():
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-05"])
    result = align_to_next_session(datetime(2024, 1, 2, 8, 0), dates)
    assert result == pd.Timestamp("2024-01-03")


def test_align_returns_none_when_no_later_session():
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    result = align_to_next_session(datetime(2024, 1, 3, 23, 59), dates)
    assert result is None


def test_align_skips_weekend_to_monday():
    """Friday → Saturday (weekend) → next session is Monday."""
    dates = pd.DatetimeIndex(["2024-01-05", "2024-01-08"])  # Fri, Mon
    # Article published on Friday, 2024-01-05 at 20:00 (after close)
    result = align_to_next_session(datetime(2024, 1, 5, 20, 0), dates)
    assert result == pd.Timestamp("2024-01-08")


def test_align_uses_strict_after_not_same_day():
    """A timestamp exactly at midnight is treated as before that day's session."""
    dates = pd.DatetimeIndex(["2024-01-02"])
    # Midnight on 2024-01-02 should map to 2024-01-02 (the next session strictly after midnight)
    result = align_to_next_session(datetime(2024, 1, 1, 23, 59), dates)
    assert result == pd.Timestamp("2024-01-02")


# --------------------------------------------------------------------------- #
# compute_forward_returns
# --------------------------------------------------------------------------- #


def test_fwd_return_1d():
    prices = _prices(
        ["2024-01-02", "2024-01-03", "2024-01-04"],
        [100.0, 110.0, 99.0],
    )
    df = compute_forward_returns("SPY", prices, windows=[1])
    assert len(df) == 3
    row = df[df["date"] == pd.Timestamp("2024-01-02").to_pydatetime()].iloc[0]
    assert abs(row["fwd_return"] - 0.10) < 1e-9  # (110-100)/100


def test_fwd_return_last_row_is_nan():
    """The last row has no exit session → fwd_return should be NaN."""
    prices = _prices(["2024-01-02", "2024-01-03"], [100.0, 105.0])
    df = compute_forward_returns("SPY", prices, windows=[1])
    last = df[df["date"] == pd.Timestamp("2024-01-03").to_pydatetime()].iloc[0]
    assert pd.isna(last["fwd_return"])


def test_fwd_return_multiple_windows_row_count():
    prices = _prices(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        [100.0, 101.0, 102.0, 103.0],
    )
    df = compute_forward_returns("SPY", prices, windows=[1, 5, 21])
    # 4 dates × 3 windows = 12 rows
    assert len(df) == 12


def test_fwd_return_empty_prices():
    df = compute_forward_returns("SPY", pd.DataFrame(), windows=[1])
    assert df.empty


def test_fwd_return_missing_adj_close_column():
    prices = pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex(["2024-01-02"]))
    df = compute_forward_returns("SPY", prices, windows=[1])
    assert df.empty


def test_fwd_return_symmetry():
    """Going from 100→105→100 should give +5 % and -4.76 %."""
    prices = _prices(["2024-01-02", "2024-01-03", "2024-01-04"], [100.0, 105.0, 100.0])
    df = compute_forward_returns("SPY", prices, windows=[1])
    row0 = df[df["date"] == pd.Timestamp("2024-01-02").to_pydatetime()].iloc[0]
    row1 = df[df["date"] == pd.Timestamp("2024-01-03").to_pydatetime()].iloc[0]
    assert abs(row0["fwd_return"] - 0.05) < 1e-9
    assert abs(row1["fwd_return"] - (-5 / 105)) < 1e-9


def test_fwd_return_window_5():
    """5-day window on a 10-day series."""
    prices = _prices(
        [f"2024-01-{d:02d}" for d in range(2, 12)],  # 10 dates
        [float(100 + i) for i in range(10)],
    )
    df = compute_forward_returns("SPY", prices, windows=[5])
    # Only the first 5 rows have a valid exit.
    valid = df[~df["fwd_return"].isna()]
    assert len(valid) == 5
    nan_rows = df[df["fwd_return"].isna()]
    assert len(nan_rows) == 5


# --------------------------------------------------------------------------- #
# DB persistence — upsert_market_prices
# --------------------------------------------------------------------------- #


def test_upsert_market_prices_inserts_rows(db):
    prices = _prices(
        ["2024-01-02", "2024-01-03"],
        [400.0, 402.0],
    )
    n = upsert_market_prices(db, "SPY", prices)
    assert n == 2
    rows = db.query(MarketPrice).filter(MarketPrice.symbol == "SPY").all()
    assert len(rows) == 2


def test_upsert_market_prices_is_idempotent(db):
    prices = _prices(["2024-01-02"], [400.0])
    upsert_market_prices(db, "SPY", prices)
    upsert_market_prices(db, "SPY", prices)
    count = db.query(MarketPrice).filter(MarketPrice.symbol == "SPY").count()
    assert count == 1


def test_upsert_market_prices_updates_on_conflict(db):
    prices = _prices(["2024-01-02"], [400.0])
    upsert_market_prices(db, "SPY", prices)

    updated = _prices(["2024-01-02"], [999.0])
    upsert_market_prices(db, "SPY", updated)

    row = db.query(MarketPrice).filter(MarketPrice.symbol == "SPY").one()
    assert row.adj_close == 999.0


def test_upsert_market_prices_empty_noop(db):
    n = upsert_market_prices(db, "SPY", pd.DataFrame())
    assert n == 0
    assert db.query(MarketPrice).count() == 0


# --------------------------------------------------------------------------- #
# DB persistence — upsert_forward_returns
# --------------------------------------------------------------------------- #


def test_upsert_forward_returns_inserts_rows(db):
    prices = _prices(["2024-01-02", "2024-01-03", "2024-01-04"], [100.0, 110.0, 105.0])
    df = compute_forward_returns("SPY", prices, windows=[1])
    n = upsert_forward_returns(db, df)
    assert n == 3
    assert db.query(ForwardReturn).count() == 3


def test_upsert_forward_returns_is_idempotent(db):
    prices = _prices(["2024-01-02", "2024-01-03"], [100.0, 110.0])
    df = compute_forward_returns("SPY", prices, windows=[1])
    upsert_forward_returns(db, df)
    upsert_forward_returns(db, df)
    assert db.query(ForwardReturn).count() == 2


def test_upsert_forward_returns_stores_nan_as_null(db):
    prices = _prices(["2024-01-02"], [100.0])
    df = compute_forward_returns("SPY", prices, windows=[1])  # only 1 row → NaN
    upsert_forward_returns(db, df)
    row = db.query(ForwardReturn).one()
    assert row.fwd_return is None


# --------------------------------------------------------------------------- #
# refresh_symbol orchestration
# --------------------------------------------------------------------------- #


def test_refresh_symbol_returns_counts(db):
    prices = _prices(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
        [100.0, 101.0, 102.0, 103.0],
    )
    stats = refresh_symbol(db, "SPY", prices, windows=[1, 5])
    assert stats["prices_upserted"] == 4
    # 4 dates × 2 windows = 8 return rows
    assert stats["returns_upserted"] == 8


def test_refresh_symbol_is_idempotent(db):
    prices = _prices(
        ["2024-01-02", "2024-01-03", "2024-01-04"],
        [100.0, 101.0, 102.0],
    )
    refresh_symbol(db, "SPY", prices, windows=[1])
    refresh_symbol(db, "SPY", prices, windows=[1])
    assert db.query(MarketPrice).count() == 3
    assert db.query(ForwardReturn).count() == 3
