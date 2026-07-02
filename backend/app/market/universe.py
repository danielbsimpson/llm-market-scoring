"""Asset universe helpers — which symbols to fetch prices for.

The universe is derived from the ``assets`` table seeded in Phase 1:
  - Funds and stocks are directly tradable → fetch their own prices.
  - Industries are not directly tradable → fetch their proxy ETF's prices.

All functions accept an open SQLAlchemy ``Session`` so they work inside any
request/CLI context without managing their own DB lifecycle.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, AssetKind


def get_tradable_symbols(db: Session) -> list[str]:
    """Return the sorted set of symbols that have directly-tradable prices.

    Includes:
      - All active fund and stock symbols.
      - All proxy ETF symbols used to evaluate industry forward returns.

    Duplicates are removed (e.g. XRT appears as both a fund and an industry proxy).
    """
    assets = db.scalars(select(Asset).where(Asset.active.is_(True))).all()
    symbols: set[str] = set()
    for asset in assets:
        if asset.kind in (AssetKind.fund, AssetKind.stock):
            symbols.add(asset.symbol)
        if asset.proxy_symbol:
            symbols.add(asset.proxy_symbol)
    return sorted(symbols)


def get_symbol_to_proxy(db: Session) -> dict[str, str]:
    """Return a mapping of industry symbol → proxy ETF symbol for active industries."""
    rows = db.scalars(
        select(Asset).where(
            Asset.kind == AssetKind.industry,
            Asset.active.is_(True),
            Asset.proxy_symbol.is_not(None),
        )
    ).all()
    return {asset.symbol: asset.proxy_symbol for asset in rows}  # type: ignore[index]


def get_price_symbol(asset_symbol: str, symbol_to_proxy: dict[str, str]) -> str:
    """Resolve the price-fetchable symbol for any asset (returns proxy for industries)."""
    return symbol_to_proxy.get(asset_symbol, asset_symbol)
