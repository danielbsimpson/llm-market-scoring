"""FastAPI routes for market data operations."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ForwardReturn, MarketPrice
from app.db.session import get_db
from app.market.returns import refresh_symbol
from app.market.universe import get_tradable_symbols
from app.market.yfinance_client import YFinanceClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market", tags=["market"])

_client = YFinanceClient()  # shared; uses the default cache dir from settings


# --------------------------------------------------------------------------- #
# Request / response schemas
# --------------------------------------------------------------------------- #


class RefreshRequest(BaseModel):
    symbols: list[str] | None = Field(
        default=None,
        description=(
            "Symbols to refresh. Defaults to the full tradable universe when omitted."
        ),
    )
    start: str = Field(
        default="2015-01-01",
        description="Start date for the initial fetch (YYYY-MM-DD).",
    )
    skip_returns: bool = Field(
        default=False,
        description="If true, persist prices but skip forward-return computation.",
    )


class SymbolStatus(BaseModel):
    symbol: str
    price_rows: int
    first_date: str | None
    last_date: str | None


class RefreshResponse(BaseModel):
    symbols_processed: int
    total_prices_upserted: int
    total_returns_upserted: int
    errors: dict[str, str]


class StatusResponse(BaseModel):
    total_tradable_symbols: int
    symbols_with_prices: int
    total_price_rows: int
    total_return_rows: int
    earliest_price_date: str | None
    latest_price_date: str | None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post("/refresh", response_model=RefreshResponse, summary="Refresh market prices & returns")
def refresh_market(
    req: RefreshRequest,
    db: Session = Depends(get_db),
) -> RefreshResponse:
    """Incrementally fetch OHLCV data from yfinance and compute forward returns.

    - Existing cached Parquet files are used so only the missing tail is
      downloaded (fast on subsequent calls).
    - Prices and forward returns are upserted into the DB (idempotent).
    - ``skip_returns=true`` is useful for a first-pass price backfill where you
      want to inspect data quality before committing to the return computation.

    .. note::
        This is a **synchronous** endpoint; a full universe refresh (~60 symbols,
        10 years of data) can take 1–3 minutes on the first run.  Subsequent
        incremental calls take only a few seconds.
    """
    if req.symbols:
        symbols = [s.upper() for s in req.symbols]
    else:
        symbols = get_tradable_symbols(db)

    if not symbols:
        raise HTTPException(status_code=404, detail="No tradable symbols found in DB.")

    windows = settings.return_window_list
    total_prices = 0
    total_returns = 0
    errors: dict[str, str] = {}

    import time

    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(0.3)  # light rate-limiting between tickers
        try:
            prices = _client.fetch_incremental(sym, start=req.start)
            if prices.empty:
                logger.warning("%s: no price data returned", sym)
                continue

            if req.skip_returns:
                from app.market.returns import upsert_market_prices

                n = upsert_market_prices(db, sym, prices)
                total_prices += n
            else:
                stats = refresh_symbol(db, sym, prices, windows)
                total_prices += stats["prices_upserted"]
                total_returns += stats["returns_upserted"]
        except Exception as exc:
            logger.error("refresh_market error for %s: %s", sym, exc)
            errors[sym] = str(exc)

    return RefreshResponse(
        symbols_processed=len(symbols) - len(errors),
        total_prices_upserted=total_prices,
        total_returns_upserted=total_returns,
        errors=errors,
    )


@router.get("/symbols", response_model=list[SymbolStatus], summary="List tracked symbols and cache status")
def list_symbols(db: Session = Depends(get_db)) -> list[SymbolStatus]:
    """Return every tradable symbol with its price coverage in the DB."""
    universe = get_tradable_symbols(db)
    result: list[SymbolStatus] = []
    for sym in universe:
        rows = db.execute(
            select(
                func.count(MarketPrice.id),
                func.min(MarketPrice.date),
                func.max(MarketPrice.date),
            ).where(MarketPrice.symbol == sym)
        ).one()
        count, first, last = rows
        result.append(
            SymbolStatus(
                symbol=sym,
                price_rows=count or 0,
                first_date=str(first.date()) if first else None,
                last_date=str(last.date()) if last else None,
            )
        )
    return result


@router.get("/status", response_model=StatusResponse, summary="Overall market data status")
def market_status(db: Session = Depends(get_db)) -> StatusResponse:
    """Return aggregate counts for prices and forward returns in the DB."""
    universe = get_tradable_symbols(db)

    price_stats = db.execute(
        select(
            func.count(MarketPrice.id),
            func.min(MarketPrice.date),
            func.max(MarketPrice.date),
        )
    ).one()
    price_count, price_min, price_max = price_stats

    symbols_with_data = db.scalar(
        select(func.count(func.distinct(MarketPrice.symbol)))
    )

    return_count = db.scalar(select(func.count(ForwardReturn.id))) or 0

    return StatusResponse(
        total_tradable_symbols=len(universe),
        symbols_with_prices=symbols_with_data or 0,
        total_price_rows=price_count or 0,
        total_return_rows=return_count,
        earliest_price_date=str(price_min.date()) if price_min else None,
        latest_price_date=str(price_max.date()) if price_max else None,
    )


@router.get("/prices/{symbol}", summary="Get cached prices for a symbol")
def get_prices(
    symbol: str,
    limit: int = Query(default=30, ge=1, le=2000, description="Most recent N rows."),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return the most recent price rows for a symbol from the DB."""
    rows = db.execute(
        select(MarketPrice)
        .where(MarketPrice.symbol == symbol.upper())
        .order_by(MarketPrice.date.desc())
        .limit(limit)
    ).scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No prices found for {symbol.upper()}.")

    return [
        {
            "date": str(r.date.date()),
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "adj_close": r.adj_close,
            "volume": r.volume,
        }
        for r in rows
    ]


@router.get("/returns/{symbol}", summary="Get forward returns for a symbol")
def get_returns(
    symbol: str,
    window: int | None = Query(default=None, description="Filter by window (trading days)."),
    limit: int = Query(default=100, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return forward-return rows for a symbol, optionally filtered by window."""
    q = (
        select(ForwardReturn)
        .where(ForwardReturn.symbol == symbol.upper())
        .order_by(ForwardReturn.date.desc(), ForwardReturn.window)
        .limit(limit)
    )
    if window is not None:
        q = q.where(ForwardReturn.window == window)

    rows = db.execute(q).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No return data found for {symbol.upper()}.")

    return [
        {
            "date": str(r.date.date()),
            "window": r.window,
            "fwd_return": r.fwd_return,
        }
        for r in rows
    ]
