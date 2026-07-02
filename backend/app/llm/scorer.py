"""LLM scoring orchestrator.

Loads prompt markdown files, registers them in the database (hash-versioned),
and runs the LLM scoring pipeline:

    article × prompt × model × asset-batch → Score rows

Assets are batched by *kind* (fund / stock / industry), producing at most three
LLM calls per (article, prompt, model) triple.  Already-scored combinations are
skipped automatically, making every run fully idempotent.

Usage (CLI)::

    python -m app.llm --prompt multi_asset --limit 10

Usage (Python)::

    from app.llm.scorer import Scorer
    from app.db.session import SessionLocal

    scorer = Scorer()
    with SessionLocal() as db:
        stats = scorer.score_batch(db, limit=5)
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from string import Template

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Article, Asset, LLMModel, Prompt, Score
from app.llm.engine import ChatMessage, LLMEngine
from app.llm.schema import AssetScore, parse_score_response

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Prompt file parsing
# ---------------------------------------------------------------------------

def parse_prompt_file(path: Path) -> tuple[dict[str, str], str]:
    """Parse a ``.md`` prompt file into ``(metadata, body)``.

    Format::

        name: my_prompt
        description: What it does
        asset_scope: all
        ---
        Prompt body text with $variable placeholders.

    The metadata block is all ``key: value`` lines before the first
    ``\\n---\\n`` separator.  Everything after is the body.
    """
    content = path.read_text(encoding="utf-8")
    sep = "\n---\n"
    if sep in content:
        meta_text, _, body = content.partition(sep)
    else:
        meta_text, body = "", content

    metadata: dict[str, str] = {}
    for line in meta_text.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            metadata[key.strip().lower()] = val.strip()

    return metadata, body.strip()


# ---------------------------------------------------------------------------
# PromptLoader
# ---------------------------------------------------------------------------

class PromptLoader:
    """Loads prompt ``.md`` files and keeps :class:`~app.db.models.Prompt` rows
    in the database synchronised (hash-based versioning).

    Each unique file content hash gets exactly one DB row.  When the file
    changes, the old row is deactivated and a new version is created.
    """

    def __init__(self, db: Session, prompts_dir: Path = _PROMPTS_DIR) -> None:
        self._db = db
        self._dir = prompts_dir
        self._cache: dict[str, tuple[Prompt, str]] = {}

    def get(self, name: str) -> tuple[Prompt, str]:
        """Return ``(Prompt ORM row, body text)`` for *name*, caching after first load."""
        if name not in self._cache:
            self._cache[name] = self._load(name)
        return self._cache[name]

    def list_available(self) -> list[str]:
        """Names of all ``.md`` files in the prompts directory."""
        return sorted(p.stem for p in self._dir.glob("*.md"))

    # ------------------------------------------------------------------
    def _load(self, name: str) -> tuple[Prompt, str]:
        path = self._dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt file not found: {path}. "
                f"Available: {self.list_available()}"
            )

        metadata, body = parse_prompt_file(path)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # Check for an existing active row with the same content hash.
        existing = (
            self._db.query(Prompt)
            .filter_by(name=name, hash=body_hash, active=True)
            .first()
        )
        if existing:
            return existing, body

        # Compute the next version number.
        latest = (
            self._db.query(Prompt)
            .filter_by(name=name)
            .order_by(Prompt.version.desc())
            .first()
        )
        next_version = (latest.version + 1) if latest else 1

        # Deactivate all previous versions for this prompt name.
        if latest:
            self._db.query(Prompt).filter_by(name=name, active=True).update({"active": False})

        prompt = Prompt(
            name=name,
            asset_scope=metadata.get("asset_scope"),
            markdown_path=str(path),
            version=next_version,
            hash=body_hash,
            active=True,
        )
        self._db.add(prompt)
        self._db.flush()
        log.info("Registered prompt '%s' version %d (hash %s…)", name, next_version, body_hash[:8])
        return prompt, body


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class Scorer:
    """Orchestrates LLM scoring for articles × prompts × models.

    Assets are grouped by *kind* so each LLM call covers a coherent batch:

    * **fund** — 19 ETFs / funds
    * **stock** — individual stocks (LMT, MSFT)
    * **industry** — 23 sector/industry names

    Three calls are made per (article, prompt, model) triple.  Already-scored
    combos are skipped at the asset level, making re-runs safe and incremental.
    """

    DEFAULT_MAX_RETRIES = 3
    DEFAULT_MAX_ARTICLE_CHARS = 12_000

    def __init__(
        self,
        engine: LLMEngine | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_article_chars: int = DEFAULT_MAX_ARTICLE_CHARS,
    ) -> None:
        self._engine = engine or LLMEngine()
        self.max_retries = max_retries
        self.max_article_chars = max_article_chars

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def score_article(
        self,
        article: Article,
        prompt: Prompt,
        prompt_body: str,
        llm_model: LLMModel,
        assets: list[Asset],
        db: Session,
    ) -> dict:
        """Score one article for *assets*, batched by kind.

        Returns ``{"inserted": int, "missing": int, "errors": int}``.
        """
        # Group by kind.
        groups: dict[str, list[Asset]] = {}
        for asset in assets:
            groups.setdefault(asset.kind.value, []).append(asset)

        inserted = missing = errors = 0

        for kind, group_assets in groups.items():
            scores = self._call_llm(
                article=article,
                prompt_body=prompt_body,
                assets=group_assets,
                kind=kind,
                model=llm_model,
            )

            if scores is None:
                errors += len(group_assets)
                continue

            # Map symbol/name → Asset (case-insensitive).
            asset_map: dict[str, Asset] = {}
            for a in group_assets:
                asset_map[a.symbol.lower()] = a
                if a.name:
                    asset_map[a.name.lower()] = a

            scored_ids: set[int] = set()
            for item in scores:
                asset = asset_map.get(item.asset.lower())
                if asset is None:
                    log.debug("Unknown asset '%s' in LLM response — skipping", item.asset)
                    continue
                if asset.id in scored_ids:
                    continue  # duplicate in response
                db.add(
                    Score(
                        article_id=article.id,
                        asset_id=asset.id,
                        prompt_id=prompt.id,
                        llm_model_id=llm_model.id,
                        score=item.score,
                        confidence=item.confidence,
                        rationale=item.rationale,
                        raw_json=item.model_dump(),
                    )
                )
                scored_ids.add(asset.id)
                inserted += 1

            missing += len(group_assets) - len(scored_ids)

        db.commit()
        return {"inserted": inserted, "missing": missing, "errors": errors}

    def score_batch(
        self,
        db: Session,
        *,
        prompt_name: str | None = None,
        model_name: str | None = None,
        article_ids: list[int] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Score a batch of articles, skipping already-scored combos.

        Args:
            db: Active SQLAlchemy session.
            prompt_name: Name of the prompt file (without ``.md``).
                         Defaults to ``"multi_asset"``.
            model_name: LLM model ``name`` or ``ref`` (e.g. ``"qwen2.5:7b"``).
                        Defaults to ``settings.llm_model``.
            article_ids: Restrict to these article IDs; ``None`` = all.
            limit: Cap total articles processed (applied after ``article_ids``
                   filter, ordered by ``published_at``).

        Returns:
            ``{"articles_processed", "scores_inserted", "scores_skipped", "scores_missing", "errors"}``
        """
        # --- Resolve model ---
        model_ref = model_name or settings.llm_model
        llm_model = (
            db.query(LLMModel)
            .filter(
                or_(LLMModel.name == model_ref, LLMModel.ref == model_ref),
                LLMModel.active.is_(True),
            )
            .first()
        )
        if llm_model is None:
            raise ValueError(
                f"Model '{model_ref}' not found in llm_models (active=True). "
                "Run `python -m app.db.seed` first."
            )

        # --- Resolve prompt ---
        loader = PromptLoader(db)
        prompt_orm, prompt_body = loader.get(prompt_name or "multi_asset")

        # --- Load active assets ---
        assets = db.query(Asset).filter_by(active=True).all()
        asset_ids_all = {a.id for a in assets}

        # --- Query articles ---
        q = db.query(Article).order_by(Article.published_at)
        if article_ids:
            q = q.filter(Article.id.in_(article_ids))
        if limit:
            q = q.limit(limit)
        articles: list[Article] = q.all()

        total_inserted = total_skipped = total_missing = total_errors = 0

        for i, article in enumerate(articles, 1):
            # Which asset IDs already have a score for this (article, prompt, model)?
            already_scored: set[int] = {
                row[0]
                for row in db.query(Score.asset_id)
                .filter_by(
                    article_id=article.id,
                    prompt_id=prompt_orm.id,
                    llm_model_id=llm_model.id,
                )
                .all()
            }

            remaining = [a for a in assets if a.id not in already_scored]
            total_skipped += len(already_scored)

            if not remaining:
                log.debug("Article %d (%s): all assets already scored, skipping", article.id, article.published_at)
                continue

            log.info(
                "[%d/%d] Scoring article id=%d published=%s (%d assets to score)",
                i, len(articles), article.id,
                article.published_at.date() if article.published_at else "?",
                len(remaining),
            )
            result = self.score_article(
                article=article,
                prompt=prompt_orm,
                prompt_body=prompt_body,
                llm_model=llm_model,
                assets=remaining,
                db=db,
            )
            total_inserted += result["inserted"]
            total_missing += result["missing"]
            total_errors += result["errors"]

        return {
            "articles_processed": len(articles),
            "scores_inserted": total_inserted,
            "scores_skipped": total_skipped,
            "scores_missing": total_missing,
            "errors": total_errors,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        article: Article,
        prompt_body: str,
        assets: list[Asset],
        kind: str,
        model: LLMModel,
    ) -> list[AssetScore] | None:
        """Run one LLM call for a batch of same-kind assets, with repair + retry."""
        asset_lines = []
        for a in assets:
            label = f"- {a.symbol} ({a.name})" if a.name and a.name != a.symbol else f"- {a.symbol}"
            asset_lines.append(label)

        rendered = Template(prompt_body).safe_substitute(
            article_text=(article.clean_text or "")[: self.max_article_chars],
            asset_list="\n".join(asset_lines),
            published_at=article.published_at.date().isoformat() if article.published_at else "unknown",
            kind=kind,
        )
        messages = [ChatMessage(role="user", content=rendered)]

        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._engine.generate(messages, model=model.ref, json_mode=True)
            except Exception:
                log.exception("LLM call failed (article=%d, kind=%s, attempt=%d)", article.id, kind, attempt)
                continue

            scores = parse_score_response(raw)
            if scores is not None:
                return scores

            log.warning(
                "parse_score_response failed (article=%d, kind=%s, attempt=%d/%d)",
                article.id, kind, attempt, self.max_retries,
            )

        log.error(
            "All %d attempts failed for article=%d kind=%s — skipping",
            self.max_retries, article.id, kind,
        )
        return None
