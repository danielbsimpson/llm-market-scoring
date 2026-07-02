"""Forward return computation and DB persistence for market prices.

Responsibilities:
  - ``align_to_next_session``  — map an arbitrary timestamp to the next
    available trading session (avoids look-ahead bias when aligning articles).
  - ``compute_forward_returns`` — vectorised forward-return computation for a
    single symbol over multiple windows.
  - ``upsert_market_prices``    — persist OHLCV rows to ``market_prices``.
  - ``upsert_forward_returns``  — persist forward-return rows to
    ``forward_returns``.
  - ``refresh_symbol``          — orchestrate all of the above for one symbol.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ForwardReturn, MarketPrice

logger = logging.getLogger(__name__)

_BATCH_SIZE = 2000  # rows per SQLite INSERT … ON CONFLICT batch


# --------------------------------------------------------------------------- #
# Session alignment
# --------------------------------------------------------------------------- #


def align_to_next_session(
    ts: datetime | pd.Timestamp,
    sorted_dates: pd.DatetimeIndex,
) -> pd.Timestamp | None:
    """Return the first trading session *strictly after* ``ts``.

    ``sorted_dates`` is the DatetimeIndex of the price DataFrame (already sorted,
    timezone-naive, daily frequency).  Returns ``None`` when no session exists
    after ``ts`` (e.g. the article is more recent than the last price row).

    This is the canonical entry-point for look-ahead-safe score alignment in
    Phase 5: an article published on Monday is entered at Tuesday's close.
    """
    # Normalise ts to midnight so intraday timestamps compare correctly.
    ts_norm = pd.Timestamp(ts).normalize()
    mask = sorted_dates > ts_norm
    if not mask.any():
        return None
    return sorted_dates[mask][0]


# --------------------------------------------------------------------------- #
# Forward return computation (pure pandas, no DB)
# --------------------------------------------------------------------------- #


def compute_forward_returns(
    symbol: str,
    prices: pd.DataFrame,
    windows: list[int] | None = None,
) -> pd.DataFrame:
    """Compute forward returns for every trading date in ``prices``.

    For each trading date *d* and window *W*:
        fwd_return = (adj_close[d+W sessions] - adj_close[d]) / adj_close[d]

    Returns a long-format DataFrame with columns:
        ``symbol``, ``date`` (Python datetime), ``window`` (int), ``fwd_return`` (float | NaN).

    Tail rows where the exit session doesn't exist have ``NaN`` fwd_return;
    these are stored as NULL in the database.
    """
    if windows is None:
        windows = settings.return_window_list

    if prices.empty or "adj_close" not in prices.columns:
        return pd.DataFrame(columns=["symbol", "date", "window", "fwd_return"])

    adj: pd.Series = prices["adj_close"].sort_index().dropna().astype(float)
    if adj.empty:
        return pd.DataFrame(columns=["symbol", "date", "window", "fwd_return"])

    parts: list[pd.DataFrame] = []
    for w in windows:
        exit_adj = adj.shift(-w)           # shift backward: index[i] → value at index[i+w]
        fwd_ret = (exit_adj - adj) / adj   # vectorised; NaN where exit is out of range

        parts.append(
            pd.DataFrame(
                {
                    "symbol": symbol,
                    "date": adj.index.to_pydatetime(),
                    "window": w,
                    "fwd_return": fwd_ret.values,
                }
            )
        )

    return pd.concat(parts, ignore_index=True)


# --------------------------------------------------------------------------- #
# DB persistence helpers
# --------------------------------------------------------------------------- #


def _nan_to_none(val: Any) -> Any:
    """Convert NaN / numpy NaN to None for SQLAlchemy NULL compatibility."""
    if val is None:
        return None
    try:
        if np.isnan(val):  # type: ignore[arg-type]
            return None
    except (TypeError, ValueError):
        pass
    return val


def upsert_market_prices(
    db: Session,
    symbol: str,
    prices: pd.DataFrame,
) -> int:
    """Upsert OHLCV rows into ``market_prices``.

    Uses SQLite's ``INSERT … ON CONFLICT DO UPDATE`` for idempotent re-runs.
    Returns the number of rows upserted.
    """
    if prices.empty:
        return 0

    records: list[dict] = []
    for ts, row in prices.iterrows():
        records.append(
            {
                "symbol": symbol,
                "date": pd.Timestamp(ts).to_pydatetime(),
                "open": _nan_to_none(row.get("open")),
                "high": _nan_to_none(row.get("high")),
                "low": _nan_to_none(row.get("low")),
                "close": _nan_to_none(row.get("close")),
                "adj_close": _nan_to_none(row.get("adj_close")),
                "volume": _nan_to_none(row.get("volume")),
            }
        )

    for i in range(0, len(records), _BATCH_SIZE):
        batch = records[i : i + _BATCH_SIZE]
        ins = sqlite_insert(MarketPrice)
        db.execute(
            ins.values(batch).on_conflict_do_update(
                index_elements=["symbol", "date"],
                set_={
                    "open": ins.excluded.open,
                    "high": ins.excluded.high,
                    "low": ins.excluded.low,
                    "close": ins.excluded.close,
                    "adj_close": ins.excluded.adj_close,
                    "volume": ins.excluded.volume,
                },
            )
        )
    db.commit()
    return len(records)


def upsert_forward_returns(
    db: Session,
    returns_df: pd.DataFrame,
) -> int:
    """Upsert forward-return rows into ``forward_returns``.

    Returns the number of rows upserted.
    """
    if returns_df.empty:
        return 0

    records: list[dict] = [
        {
            "symbol": row.symbol,
            "date": row.date if isinstance(row.date, datetime) else pd.Timestamp(row.date).to_pydatetime(),
            "window": int(row.window),
            "fwd_return": _nan_to_none(row.fwd_return),
        }
        for row in returns_df.itertuples(index=False)
    ]

    for i in range(0, len(records), _BATCH_SIZE):
        batch = records[i : i + _BATCH_SIZE]
        ins = sqlite_insert(ForwardReturn)
        db.execute(
            ins.values(batch).on_conflict_do_update(
                index_elements=["symbol", "date", "window"],
                set_={"fwd_return": ins.excluded.fwd_return},
            )
        )
    db.commit()
    return len(records)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def refresh_symbol(
    db: Session,
    symbol: str,
    prices: pd.DataFrame,
    windows: list[int] | None = None,
) -> dict[str, int]:
    """Persist price data and compute + persist forward returns for one symbol.

    Returns a dict with keys ``prices_upserted`` and ``returns_upserted``.
    """
    if windows is None:
        windows = settings.return_window_list

    prices_n = upsert_market_prices(db, symbol, prices)
    returns_df = compute_forward_returns(symbol, prices, windows)
    returns_n = upsert_forward_returns(db, returns_df)

    return {"prices_upserted": prices_n, "returns_upserted": returns_n}
