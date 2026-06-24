"""FastAPI application entrypoint.

Phase 0 scaffold: app wiring, CORS, and health endpoints that confirm the
existing local LLM (Ollama / llama-server) is reachable.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.llm.engine import engine

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


@app.get("/health")
def health() -> dict:
    """Liveness check for the API itself."""
    return {"status": "ok", "env": settings.app_env}


@app.get("/health/llm")
def health_llm() -> dict:
    """Confirm the local LLM server is reachable and list available models."""
    return engine.health()


@app.get("/")
def root() -> dict:
    return {
        "name": "llm-market-scoring",
        "docs": "/docs",
        "health": "/health",
        "llm_health": "/health/llm",
    }
