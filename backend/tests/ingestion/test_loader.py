"""Tests for ingestion/loader.py — ingest_path orchestrator."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.loader import ingest_path

FIXTURE_MBOX = Path(__file__).parent / "fixtures" / "sample.mbox"


def test_ingest_single_mbox_file(db):
    result = ingest_path(
        FIXTURE_MBOX,
        source_name="Test Snacks",
        db=db,
        parser_key="robinhood_snacks",
    )
    assert result["files_processed"] == 1
    assert result["articles_inserted"] == 2
    assert result["articles_skipped"] == 0
    assert result["source_id"] > 0


def test_ingest_is_idempotent(db):
    ingest_path(FIXTURE_MBOX, source_name="Test Snacks", db=db, parser_key="robinhood_snacks")
    result = ingest_path(FIXTURE_MBOX, source_name="Test Snacks", db=db, parser_key="robinhood_snacks")
    assert result["articles_inserted"] == 0
    assert result["articles_skipped"] == 2


def test_ingest_preserves_source_id_across_runs(db):
    r1 = ingest_path(FIXTURE_MBOX, source_name="Test Snacks", db=db, parser_key="robinhood_snacks")
    r2 = ingest_path(FIXTURE_MBOX, source_name="Test Snacks", db=db, parser_key="robinhood_snacks")
    assert r1["source_id"] == r2["source_id"]


def test_ingest_directory_skips_unsupported_extensions(db, tmp_path):
    (tmp_path / "data.csv").write_text("col1,col2\nval1,val2")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    result = ingest_path(tmp_path, source_name="Test Source", db=db)
    assert result["files_processed"] == 0
    assert result["articles_inserted"] == 0


def test_ingest_directory_with_markdown_files(db, tmp_path):
    (tmp_path / "2024-01-15-weekly.md").write_text(
        "# Weekly Update\n\nStocks are performing well this week."
    )
    (tmp_path / "2024-01-22-weekly.md").write_text(
        "# Weekly Update 2\n\nMarkets rebounded on strong earnings."
    )
    result = ingest_path(tmp_path, source_name="Weekly Newsletter", db=db)
    assert result["files_processed"] == 2
    assert result["articles_inserted"] == 2


def test_ingest_unknown_parser_key_raises(db):
    with pytest.raises(KeyError, match="No parser registered for key"):
        ingest_path(FIXTURE_MBOX, source_name="Test", db=db, parser_key="nonexistent_parser")


def test_ingest_empty_directory_still_returns_source_id(db, tmp_path):
    result = ingest_path(tmp_path, source_name="Empty Source", db=db, parser_key="robinhood_snacks")
    assert result["source_id"] > 0
    assert result["files_processed"] == 0
    assert result["articles_inserted"] == 0
