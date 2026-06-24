"""Pydantic schema for the structured score the LLM must return per asset."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AssetScore(BaseModel):
    """A single asset outlook score produced by an LLM scorer."""

    asset: str = Field(..., description="Asset symbol or industry name being scored.")
    score: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Outlook from -1.0 (very bearish) to +1.0 (very bullish).",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in this score, 0.0 to 1.0.",
    )
    rationale: str = Field(
        default="",
        description="Short justification grounded in the article text.",
    )


class ScoreResponse(BaseModel):
    """Top-level container the model returns: a list of per-asset scores."""

    scores: list[AssetScore] = Field(default_factory=list)
