"""SQLAlchemy ORM models for the LLM Market Scoring storage layer.

Relational/metadata lives in SQLite (these tables). Bulk timeseries (prices,
feature matrices) are also modeled here for convenience but may be mirrored to
Parquet for fast analytical iteration in later phases.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class AssetKind(str, enum.Enum):
    """Category of a scored asset."""

    stock = "stock"
    fund = "fund"
    industry = "industry"


class FeedbackStatus(str, enum.Enum):
    """Lifecycle of a proposed prompt edit (human-in-the-loop)."""

    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
class Source(Base):
    """A content source (e.g. a newsletter) and the parser used to read it."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="newsletter")
    parser_key: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    articles: Mapped[list["Article"]] = relationship(back_populates="source")


class Article(Base):
    """A normalized article ingested from a source, timestamped at publish time."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_article_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(1024))
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_html_path: Mapped[str | None] = mapped_column(String(1024))
    clean_text: Mapped[str | None] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    source: Mapped["Source"] = relationship(back_populates="articles")
    scores: Mapped[list["Score"]] = relationship(back_populates="article")


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
class Asset(Base):
    """A scorable asset: a stock, fund, or industry/sector."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kind: Mapped[AssetKind] = mapped_column(
        SAEnum(AssetKind, native_enum=False, length=16), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(256))
    # For industries without a directly tradable symbol, the ETF used to evaluate returns.
    proxy_symbol: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    scores: Mapped[list["Score"]] = relationship(back_populates="asset")


# --------------------------------------------------------------------------- #
# Scoring configuration
# --------------------------------------------------------------------------- #
class Prompt(Base):
    """A versioned, markdown-backed scorer prompt."""

    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_scope: Mapped[str | None] = mapped_column(String(256))
    markdown_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    hash: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    scores: Mapped[list["Score"]] = relationship(back_populates="prompt")
    feedback: Mapped[list["PromptFeedback"]] = relationship(back_populates="prompt")


class LLMModel(Base):
    """A registered LLM scorer model (served by Ollama / llama-server)."""

    __tablename__ = "llm_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="ollama")
    # Model reference passed to the API, e.g. "qwen2.5:7b".
    ref: Mapped[str] = mapped_column(String(128), nullable=False)
    params_json: Mapped[dict | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    scores: Mapped[list["Score"]] = relationship(back_populates="llm_model")


# --------------------------------------------------------------------------- #
# Scores
# --------------------------------------------------------------------------- #
class Score(Base):
    """A single (article x asset x prompt x model) structured outlook score."""

    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint(
            "article_id",
            "asset_id",
            "prompt_id",
            "llm_model_id",
            name="uq_score_combo",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id"), nullable=False)
    llm_model_id: Mapped[int] = mapped_column(ForeignKey("llm_models.id"), nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    article: Mapped["Article"] = relationship(back_populates="scores")
    asset: Mapped["Asset"] = relationship(back_populates="scores")
    prompt: Mapped["Prompt"] = relationship(back_populates="scores")
    llm_model: Mapped["LLMModel"] = relationship(back_populates="scores")


# --------------------------------------------------------------------------- #
# Market data (may be mirrored to Parquet later)
# --------------------------------------------------------------------------- #
class MarketPrice(Base):
    """Daily OHLCV bar for a symbol."""

    __tablename__ = "market_prices"
    __table_args__ = (UniqueConstraint("symbol", "date", name="uq_price_symbol_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class ForwardReturn(Base):
    """Forward return for a symbol from a given date over a trading-day window."""

    __tablename__ = "forward_returns"
    __table_args__ = (
        UniqueConstraint("symbol", "date", "window", name="uq_fwdret_symbol_date_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    window: Mapped[int] = mapped_column(Integer, nullable=False)
    fwd_return: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------- #
# Experiments & feedback
# --------------------------------------------------------------------------- #
class Experiment(Base):
    """A reproducible test config = {prompts, models, features, windows, universe}."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    config_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    results: Mapped[list["ExperimentResult"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan"
    )


class ExperimentResult(Base):
    """A single metric value produced by an experiment (optionally per fold)."""

    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    fold: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    experiment: Mapped["Experiment"] = relationship(back_populates="results")


class PromptFeedback(Base):
    """A system-proposed prompt edit awaiting human approval."""

    __tablename__ = "prompt_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_id: Mapped[int] = mapped_column(ForeignKey("prompts.id"), nullable=False)
    proposed_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[FeedbackStatus] = mapped_column(
        SAEnum(FeedbackStatus, native_enum=False, length=16),
        nullable=False,
        default=FeedbackStatus.proposed,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    prompt: Mapped["Prompt"] = relationship(back_populates="feedback")
