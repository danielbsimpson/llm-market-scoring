"""Pydantic schema for the structured score the LLM must return per asset,
plus JSON repair utilities used by the scorer.
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# JSON repair utilities
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers."""
    text = re.sub(r"```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _extract_json_block(text: str) -> str | None:
    """Return the first substring that looks like a complete JSON object."""
    # Look for a top-level { ... } that contains a "scores" key.
    match = re.search(r'\{[^{}]*"scores"\s*:\s*\[[\s\S]*?\]\s*\}', text)
    if match:
        return match.group()
    # Fallback: first { ... } block at all.
    match = re.search(r'\{[\s\S]*\}', text)
    return match.group() if match else None


def parse_score_response(text: str) -> list[AssetScore] | None:
    """Parse LLM text output into validated :class:`AssetScore` objects.

    Applies a two-stage repair heuristic before giving up:

    1. Direct ``json.loads`` + Pydantic validation.
    2. Strip markdown code fences, then retry.
    3. Regex-extract the first JSON object block, then retry.

    Returns ``None`` if all three stages fail (caller should retry or skip).
    """
    candidates: list[str] = [text]
    stripped = _strip_code_fences(text)
    if stripped != text:
        candidates.append(stripped)
    extracted = _extract_json_block(stripped or text)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            return ScoreResponse.model_validate(data).scores
        except (json.JSONDecodeError, ValidationError, KeyError):
            continue

    log.debug("parse_score_response: all candidates failed; raw=%r", text[:200])
    return None
