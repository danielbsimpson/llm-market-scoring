"""Database engine, session factory, and FastAPI dependency."""
from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import BACKEND_ROOT, settings
from app.db.base import Base


def _resolve_database_url(url: str) -> str:
    """Resolve relative SQLite paths against the backend root for CWD safety."""
    match = re.match(r"^sqlite:///(?!/)(.*)$", url)
    if not match:
        return url
    rel = match.group(1)
    abs_path = (BACKEND_ROOT / rel).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{abs_path.as_posix()}"


DATABASE_URL = _resolve_database_url(settings.database_url)

# check_same_thread=False lets the SQLite connection be used across FastAPI threads.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (dev convenience; Alembic owns schema in production)."""
    # Import models so they register on Base.metadata before create_all.
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
