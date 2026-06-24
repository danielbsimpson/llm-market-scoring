"""Seed data for the asset universe, industry proxies, and default LLM model.

Idempotent: running multiple times will not create duplicates. Edit the lists
below to expand the universe; re-run the seed to apply.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Asset, AssetKind, LLMModel
from app.db.session import SessionLocal, init_db

# (symbol, kind, human-readable name | None)
FUNDS: list[tuple[str, str | None]] = [
    ("VGT", "Vanguard Information Technology ETF"),
    ("SCHX", "Schwab U.S. Large-Cap ETF"),
    ("QQQ", "Invesco QQQ (Nasdaq-100)"),
    ("PPA", "Invesco Aerospace & Defense ETF"),
    ("SPYI", "NEOS S&P 500 High Income ETF"),
    ("SPY", "SPDR S&P 500 ETF"),
    ("SCHD", "Schwab U.S. Dividend Equity ETF"),
    ("VT", "Vanguard Total World Stock ETF"),
    ("XLF", "Financial Select Sector SPDR"),
    ("SCHB", "Schwab U.S. Broad Market ETF"),
    ("XRT", "SPDR S&P Retail ETF"),
    ("SCHF", "Schwab International Equity ETF"),
    ("VCR", "Vanguard Consumer Discretionary ETF"),
    ("IFRA", "iShares U.S. Infrastructure ETF"),
    ("COWZ", "Pacer U.S. Cash Cows 100 ETF"),
    ("SPDW", "SPDR Portfolio Developed World ex-US ETF"),
    ("XLE", "Energy Select Sector SPDR"),
    ("SCHH", "Schwab U.S. REIT ETF"),
    ("EUAD", None),
]

# Single stocks kept in the universe.
STOCKS: list[tuple[str, str | None]] = [
    ("LMT", "Lockheed Martin"),
    ("MSFT", "Microsoft"),
]

# Industry/sector name -> proxy ETF used to evaluate forward returns.
# These proxies are sensible, liquid defaults and are configurable.
INDUSTRY_PROXIES: list[tuple[str, str]] = [
    ("Semiconductors", "SOXX"),
    ("Software", "IGV"),
    ("Biotech & Pharma", "XBI"),
    ("Healthcare Providers", "XLV"),
    ("Banks", "KBE"),
    ("Insurance", "KIE"),
    ("Consumer Staples", "XLP"),
    ("Consumer Discretionary", "XLY"),
    ("Retail", "XRT"),
    ("Automotive", "CARZ"),
    ("Aerospace & Defense", "ITA"),
    ("Industrials", "XLI"),
    ("Materials", "XLB"),
    ("Homebuilders", "XHB"),
    ("Real Estate", "XLRE"),
    ("Utilities", "XLU"),
    ("Telecom", "XTL"),
    ("Media & Entertainment", "XLC"),
    ("Transportation", "IYT"),
    ("Agriculture", "MOO"),
    ("Metals & Mining", "XME"),
    ("Renewable Energy", "ICLN"),
    ("Oil & Gas", "XOP"),
]


def _upsert_asset(
    db: Session,
    symbol: str,
    kind: AssetKind,
    name: str | None,
    proxy_symbol: str | None = None,
) -> bool:
    """Insert the asset if its symbol is new. Returns True if created."""
    existing = db.scalar(select(Asset).where(Asset.symbol == symbol))
    if existing:
        return False
    db.add(
        Asset(
            symbol=symbol,
            kind=kind,
            name=name,
            proxy_symbol=proxy_symbol,
            active=True,
        )
    )
    return True


def seed_assets(db: Session) -> int:
    """Seed funds, stocks, and industries. Returns number of assets created."""
    created = 0
    for symbol, name in FUNDS:
        created += _upsert_asset(db, symbol, AssetKind.fund, name)
    for symbol, name in STOCKS:
        created += _upsert_asset(db, symbol, AssetKind.stock, name)
    for industry, proxy in INDUSTRY_PROXIES:
        created += _upsert_asset(db, industry, AssetKind.industry, industry, proxy)
    db.commit()
    return created


def seed_default_model(db: Session) -> int:
    """Register the configured default LLM scorer model. Returns number created."""
    name = settings.llm_model
    existing = db.scalar(select(LLMModel).where(LLMModel.name == name))
    if existing:
        return 0
    db.add(
        LLMModel(
            name=name,
            provider=settings.llm_provider,
            ref=settings.llm_model,
            params_json={"num_ctx": settings.llm_num_ctx, "temperature": settings.llm_temperature},
            active=True,
        )
    )
    db.commit()
    return 1


def seed_all(db: Session) -> dict[str, int]:
    """Run all seeders and return a summary of created rows."""
    return {
        "assets": seed_assets(db),
        "models": seed_default_model(db),
    }


def main() -> None:
    """Create tables (if missing) and seed reference data."""
    init_db()
    with SessionLocal() as db:
        summary = seed_all(db)
    print(f"Seed complete: {summary}")


if __name__ == "__main__":
    main()
