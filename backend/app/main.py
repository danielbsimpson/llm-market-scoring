"""FastAPI application entrypoint.

Phase 0 scaffold: app wiring, CORS, and health endpoints that confirm the
existing local LLM (Ollama / llama-server) is reachable.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.config import settings
from app.db.models import Asset
from app.db.session import SessionLocal
from app.llm.engine import engine
from app.api.routes.ingest import router as ingest_router
from app.api.routes.market import router as market_router
from app.api.routes.score import router as score_router

app = FastAPI(
    title="LLM Market Scoring",
    version="0.1.0",
    description="Local newsletter ingestion + LLM asset scoring + ML market prediction.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest_router)
app.include_router(market_router)
app.include_router(score_router)


@app.get("/health")
def health() -> dict:
    """Liveness check for the API itself."""
    return {"status": "ok", "env": settings.app_env}


@app.get("/health/llm")
def health_llm() -> dict:
    """Confirm the local LLM server is reachable and list available models."""
    return engine.health()


@app.get("/health/db")
def health_db() -> dict:
    """Confirm the database is reachable and report asset counts by kind."""
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(Asset.kind, func.count()).group_by(Asset.kind)
            ).all()
        counts = {kind.value: count for kind, count in rows}
        return {"ok": True, "assets_by_kind": counts, "total": sum(counts.values())}
    except Exception as exc:  # noqa: BLE001 — surfaced to the health endpoint
        return {"ok": False, "error": str(exc)}


@app.get("/")
def root() -> dict:
    return {
        "name": "llm-market-scoring",
        "docs": "/docs",
        "health": "/health",
        "llm_health": "/health/llm",
        "db_health": "/health/db",
        "ingest": "/ingest",
        "score": "/score",
    }
