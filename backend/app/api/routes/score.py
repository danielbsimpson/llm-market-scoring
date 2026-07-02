"""FastAPI routes for the LLM scoring pipeline."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.llm.scorer import PromptLoader, Scorer

router = APIRouter(prefix="/score", tags=["scoring"])

_scorer = Scorer()  # module-level singleton; engine is lazy-loaded


class ScoreRequest(BaseModel):
    prompt_name: str = Field(
        default="multi_asset",
        description="Prompt file name without .md extension.",
    )
    model_name: str | None = Field(
        default=None,
        description="LLM model name/ref (e.g. 'qwen2.5:7b'). Defaults to settings.llm_model.",
    )
    article_ids: list[int] | None = Field(
        default=None,
        description="Specific article IDs to score. If omitted, uses the most recent articles up to `limit`.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=500,
        description=(
            "Maximum number of articles to score in this request. "
            "Keeps the endpoint from accidentally kicking off a full 379-article run. "
            "Use the CLI (`python -m app.llm.scorer`) for larger batches."
        ),
    )


class ScoreResponse(BaseModel):
    articles_processed: int
    scores_inserted: int
    scores_skipped: int
    scores_missing: int
    errors: int


class PromptsResponse(BaseModel):
    prompts: list[str]


@router.get("/prompts", response_model=PromptsResponse, summary="List available prompt files")
def list_prompts(db: Session = Depends(get_db)) -> PromptsResponse:
    """Return names of all ``.md`` prompt files in the prompts directory."""
    loader = PromptLoader(db)
    return PromptsResponse(prompts=loader.list_available())


@router.post("", response_model=ScoreResponse, summary="Run LLM scoring")
def run_scoring(
    req: ScoreRequest,
    db: Session = Depends(get_db),
) -> ScoreResponse:
    """Score articles through the configured LLM.

    Already-scored (article, asset, prompt, model) combos are skipped
    automatically — this endpoint is safe to call repeatedly.

    For large batch runs (all 379 articles) use the CLI::

        python -m app.llm.scorer --prompt multi_asset --limit 100
    """
    try:
        stats = _scorer.score_batch(
            db,
            prompt_name=req.prompt_name,
            model_name=req.model_name,
            article_ids=req.article_ids,
            limit=req.limit,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return ScoreResponse(**stats)
