"""Tests for the LLM scoring pipeline — all using a mock LLM engine (no Ollama required)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Article, Asset, AssetKind, LLMModel, Prompt, Score
from app.ingestion.normalize import ensure_source
from app.llm.schema import AssetScore, parse_score_response
from app.llm.scorer import Scorer, PromptLoader, parse_prompt_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).parent.parent.parent / "app" / "llm" / "prompts"


def _make_article(db, source_id: int, n: int) -> Article:
    text = f"Markets moved today. Tech stocks gained. Article number {n}."
    article = Article(
        source_id=source_id,
        external_id=f"test-{n}",
        title=f"Test Article {n}",
        published_at=datetime(2024, 1, n + 1, 10, 0),
        clean_text=text,
        hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    db.add(article)
    db.flush()
    return article


def _make_assets(db) -> list[Asset]:
    assets = [
        Asset(symbol="SPY", kind=AssetKind.fund, name="SPDR S&P 500 ETF", active=True),
        Asset(symbol="QQQ", kind=AssetKind.fund, name="Invesco QQQ", active=True),
        Asset(symbol="MSFT", kind=AssetKind.stock, name="Microsoft", active=True),
        Asset(symbol="Semiconductors", kind=AssetKind.industry, name="Semiconductors", active=True),
    ]
    for a in assets:
        db.add(a)
    db.flush()
    return assets


def _make_llm_model(db) -> LLMModel:
    m = LLMModel(name="qwen2.5:7b", provider="ollama", ref="qwen2.5:7b", active=True)
    db.add(m)
    db.flush()
    return m


def _make_prompt(db, body: str) -> Prompt:
    h = hashlib.sha256(body.encode()).hexdigest()
    p = Prompt(
        name="test_prompt",
        asset_scope="all",
        markdown_path="test_prompt.md",
        version=1,
        hash=h,
        active=True,
    )
    db.add(p)
    db.flush()
    return p


def _mock_engine(responses: list[str]) -> MagicMock:
    """Build a mock LLMEngine whose generate() returns items from *responses* in order."""
    eng = MagicMock()
    eng.generate.side_effect = responses
    return eng


def _valid_json_for(assets: list[Asset]) -> str:
    scores = [
        {"asset": a.symbol, "score": 0.1, "confidence": 0.5, "rationale": "test"}
        for a in assets
    ]
    return json.dumps({"scores": scores})


# ---------------------------------------------------------------------------
# parse_score_response
# ---------------------------------------------------------------------------

class TestParseScoreResponse:
    def test_valid_json(self):
        raw = '{"scores": [{"asset": "SPY", "score": 0.5, "confidence": 0.8, "rationale": "bullish"}]}'
        result = parse_score_response(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].asset == "SPY"
        assert result[0].score == 0.5

    def test_strips_markdown_code_fence(self):
        raw = '```json\n{"scores": [{"asset": "QQQ", "score": -0.3, "confidence": 0.6, "rationale": "bearish"}]}\n```'
        result = parse_score_response(raw)
        assert result is not None
        assert result[0].asset == "QQQ"

    def test_strips_plain_code_fence(self):
        raw = '```\n{"scores": [{"asset": "SPY", "score": 0.0, "confidence": 0.0, "rationale": "neutral"}]}\n```'
        result = parse_score_response(raw)
        assert result is not None

    def test_extracts_json_from_preamble(self):
        raw = 'Here are the scores as requested:\n{"scores": [{"asset": "MSFT", "score": 0.7, "confidence": 0.9, "rationale": "strong"}]}\nDone.'
        result = parse_score_response(raw)
        assert result is not None
        assert result[0].asset == "MSFT"

    def test_returns_none_for_garbage(self):
        result = parse_score_response("I cannot score these assets.")
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = parse_score_response("")
        assert result is None

    def test_validates_score_bounds(self):
        # score outside [-1, 1] should fail Pydantic validation → returns None
        raw = '{"scores": [{"asset": "SPY", "score": 5.0, "confidence": 0.8, "rationale": "?"}]}'
        result = parse_score_response(raw)
        assert result is None

    def test_multiple_assets(self):
        raw = '{"scores": [{"asset": "SPY", "score": 0.5, "confidence": 0.8, "rationale": "up"}, {"asset": "QQQ", "score": -0.2, "confidence": 0.4, "rationale": "down"}]}'
        result = parse_score_response(raw)
        assert result is not None
        assert len(result) == 2


# ---------------------------------------------------------------------------
# parse_prompt_file
# ---------------------------------------------------------------------------

class TestParsePromptFile:
    def test_parses_metadata_and_body(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("name: my_prompt\nasset_scope: all\n---\nBody text here with $variable.")
        meta, body = parse_prompt_file(f)
        assert meta["name"] == "my_prompt"
        assert meta["asset_scope"] == "all"
        assert body == "Body text here with $variable."

    def test_no_separator_returns_full_body(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Just a body with no frontmatter.")
        meta, body = parse_prompt_file(f)
        assert meta == {}
        assert body == "Just a body with no frontmatter."

    def test_real_multi_asset_prompt_parses(self):
        path = PROMPTS_DIR / "multi_asset.md"
        if not path.exists():
            pytest.skip("Prompt file not found")
        meta, body = parse_prompt_file(path)
        assert "name" in meta
        assert "$article_text" in body
        assert "$asset_list" in body


# ---------------------------------------------------------------------------
# PromptLoader
# ---------------------------------------------------------------------------

class TestPromptLoader:
    def test_loads_real_prompt(self, db):
        loader = PromptLoader(db, prompts_dir=PROMPTS_DIR)
        prompt, body = loader.get("multi_asset")
        assert prompt.id is not None
        assert prompt.name == "multi_asset"
        assert prompt.active is True
        assert "$article_text" in body

    def test_idempotent_load(self, db):
        loader = PromptLoader(db, prompts_dir=PROMPTS_DIR)
        p1, _ = loader.get("multi_asset")
        loader2 = PromptLoader(db, prompts_dir=PROMPTS_DIR)
        p2, _ = loader2.get("multi_asset")
        assert p1.id == p2.id

    def test_missing_prompt_raises(self, db):
        loader = PromptLoader(db, prompts_dir=PROMPTS_DIR)
        with pytest.raises(FileNotFoundError):
            loader.get("nonexistent_prompt_xyz")

    def test_new_content_increments_version(self, db, tmp_path):
        f = tmp_path / "versioned.md"
        f.write_text("name: versioned\n---\nVersion one body $article_text $asset_list $published_at $kind")
        loader = PromptLoader(db, prompts_dir=tmp_path)
        p1, _ = loader.get("versioned")
        assert p1.version == 1

        # Change the body → should get version 2
        f.write_text("name: versioned\n---\nVersion two body $article_text $asset_list $published_at $kind")
        loader2 = PromptLoader(db, prompts_dir=tmp_path)
        p2, _ = loader2.get("versioned")
        assert p2.version == 2
        assert p2.id != p1.id

    def test_list_available(self, db):
        loader = PromptLoader(db, prompts_dir=PROMPTS_DIR)
        names = loader.list_available()
        assert "multi_asset" in names
        assert "energy" in names
        assert "defense" in names
        assert "semiconductors" in names


# ---------------------------------------------------------------------------
# Scorer — score_article
# ---------------------------------------------------------------------------

class TestScorerScoreArticle:
    def _setup(self, db):
        source = ensure_source(db, "test", "test_parser")
        db.commit()
        article = _make_article(db, source.id, 0)
        assets = _make_assets(db)
        model = _make_llm_model(db)
        db.commit()
        return article, assets, model

    def _prompt_body(self):
        return "Score these assets: $asset_list for article: $article_text published $published_at kind=$kind"

    def test_inserts_scores(self, db):
        article, assets, model = self._setup(db)

        # Build mock responses: one per kind group (fund, stock, industry)
        fund_assets = [a for a in assets if a.kind == AssetKind.fund]
        stock_assets = [a for a in assets if a.kind == AssetKind.stock]
        industry_assets = [a for a in assets if a.kind == AssetKind.industry]

        responses = [
            _valid_json_for(fund_assets),
            _valid_json_for(stock_assets),
            _valid_json_for(industry_assets),
        ]
        eng = _mock_engine(responses)

        prompt = _make_prompt(db, self._prompt_body())
        scorer = Scorer(engine=eng)
        result = scorer.score_article(article, prompt, self._prompt_body(), model, assets, db)

        assert result["inserted"] == len(assets)
        assert result["errors"] == 0
        assert db.query(Score).count() == len(assets)

    def test_three_llm_calls_for_four_assets(self, db):
        """Verifies batching by kind: fund + stock + industry = 3 calls."""
        article, assets, model = self._setup(db)

        fund_assets = [a for a in assets if a.kind == AssetKind.fund]
        stock_assets = [a for a in assets if a.kind == AssetKind.stock]
        industry_assets = [a for a in assets if a.kind == AssetKind.industry]

        responses = [
            _valid_json_for(fund_assets),
            _valid_json_for(stock_assets),
            _valid_json_for(industry_assets),
        ]
        eng = _mock_engine(responses)

        prompt = _make_prompt(db, self._prompt_body())
        Scorer(engine=eng).score_article(article, prompt, self._prompt_body(), model, assets, db)
        assert eng.generate.call_count == 3

    def test_retries_on_bad_json(self, db):
        article, assets, model = self._setup(db)
        fund_assets = [a for a in assets if a.kind == AssetKind.fund]
        stock_assets = [a for a in assets if a.kind == AssetKind.stock]
        industry_assets = [a for a in assets if a.kind == AssetKind.industry]

        # First call returns garbage, second returns valid JSON
        responses = [
            "not json at all",
            _valid_json_for(fund_assets),
            _valid_json_for(stock_assets),
            _valid_json_for(industry_assets),
        ]
        eng = _mock_engine(responses)

        prompt = _make_prompt(db, self._prompt_body())
        result = Scorer(engine=eng, max_retries=3).score_article(
            article, prompt, self._prompt_body(), model, assets, db
        )
        assert result["inserted"] == len(assets)

    def test_all_retries_fail_counts_errors(self, db):
        article, assets, model = self._setup(db)
        # Always return garbage
        eng = _mock_engine(["bad"] * 9)
        prompt = _make_prompt(db, self._prompt_body())
        result = Scorer(engine=eng, max_retries=3).score_article(
            article, prompt, self._prompt_body(), model, assets, db
        )
        assert result["errors"] > 0
        assert db.query(Score).count() == 0

    def test_unknown_asset_in_response_ignored(self, db):
        article, assets, model = self._setup(db)
        fund_assets = [a for a in assets if a.kind == AssetKind.fund]
        stock_assets = [a for a in assets if a.kind == AssetKind.stock]
        industry_assets = [a for a in assets if a.kind == AssetKind.industry]

        # Inject an unknown asset symbol in the funds response
        resp_with_unknown = json.dumps({"scores": [
            {"asset": "UNKNOWN_TICKER", "score": 0.5, "confidence": 0.8, "rationale": "?"},
            *[{"asset": a.symbol, "score": 0.1, "confidence": 0.5, "rationale": "ok"} for a in fund_assets],
        ]})
        responses = [resp_with_unknown, _valid_json_for(stock_assets), _valid_json_for(industry_assets)]
        eng = _mock_engine(responses)

        prompt = _make_prompt(db, self._prompt_body())
        result = Scorer(engine=eng).score_article(article, prompt, self._prompt_body(), model, assets, db)
        # UNKNOWN_TICKER should not produce a Score row
        assert db.query(Score).filter_by(article_id=article.id).count() == len(assets)


# ---------------------------------------------------------------------------
# Scorer — score_batch
# ---------------------------------------------------------------------------

class TestScorerScoreBatch:
    def _setup_db(self, db):
        source = ensure_source(db, "test", "test_parser")
        db.commit()
        articles = [_make_article(db, source.id, i) for i in range(3)]
        assets = _make_assets(db)
        _make_llm_model(db)
        db.commit()
        return articles, assets

    def _responses_for(self, assets: list[Asset]) -> list[str]:
        """One valid response per kind group for all provided assets."""
        groups: dict[str, list] = {}
        for a in assets:
            groups.setdefault(a.kind.value, []).append(a)
        return [_valid_json_for(group) for group in groups.values()]

    def test_scores_all_articles(self, db):
        articles, assets = self._setup_db(db)
        # 3 articles × 3 kind calls = 9 responses needed
        responses = self._responses_for(assets) * 3
        eng = _mock_engine(responses)

        with patch("app.llm.scorer.LLMEngine", return_value=eng):
            scorer = Scorer(engine=eng)
        scorer._engine = eng

        with patch("app.api.routes.score._scorer", scorer):
            pass  # not testing route here

        stats = scorer.score_batch(db, prompt_name="multi_asset", model_name="qwen2.5:7b")
        assert stats["articles_processed"] == 3
        assert stats["scores_inserted"] == 3 * len(assets)

    def test_skips_already_scored(self, db):
        articles, assets = self._setup_db(db)
        responses = self._responses_for(assets) * 6  # generous buffer
        eng = _mock_engine(responses)
        scorer = Scorer(engine=eng)

        scorer.score_batch(db, prompt_name="multi_asset", model_name="qwen2.5:7b")
        first_count = db.query(Score).count()

        stats = scorer.score_batch(db, prompt_name="multi_asset", model_name="qwen2.5:7b")
        assert stats["scores_inserted"] == 0
        assert stats["scores_skipped"] == first_count

    def test_limit_respected(self, db):
        articles, assets = self._setup_db(db)
        responses = self._responses_for(assets) * 2  # only need 1 article × 3 kinds
        eng = _mock_engine(responses)
        scorer = Scorer(engine=eng)

        stats = scorer.score_batch(db, prompt_name="multi_asset", model_name="qwen2.5:7b", limit=1)
        assert stats["articles_processed"] == 1

    def test_article_ids_filter(self, db):
        articles, assets = self._setup_db(db)
        responses = self._responses_for(assets)
        eng = _mock_engine(responses)
        scorer = Scorer(engine=eng)

        stats = scorer.score_batch(
            db,
            prompt_name="multi_asset",
            model_name="qwen2.5:7b",
            article_ids=[articles[0].id],
        )
        assert stats["articles_processed"] == 1

    def test_invalid_model_raises(self, db):
        _, _ = self._setup_db(db)
        scorer = Scorer()
        with pytest.raises(ValueError, match="not found in llm_models"):
            scorer.score_batch(db, model_name="nonexistent_model_xyz")
