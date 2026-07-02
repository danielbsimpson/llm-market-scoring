---
goal: Parallel LLM Scoring Layer over Webz.io Free News Datasets
version: 1.0
date_created: 2026-07-02
last_updated: 2026-07-02
owner: llm-market-scoring
status: 'Planned'
tags: [feature, ingestion, llm, data, parallel-layer, webz]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan adds a **second, parallel LLM prediction layer** driven by the
[Webz.io free-news-datasets](https://github.com/Webhose/free-news-datasets) repository. Instead of
scoring only the Robinhood Snacks newsletter, the system will ingest general financial news articles
from Webz.io's weekly datasets and run the **same** LLM scoring pipeline over them — producing a
separate, comparable stream of asset outlook scores.

**Data format (verified 2026-07-02):** The repository's `News_Datasets/` directory contains ZIP
archives named `{Category}_{sentiment}_{timestamp}.zip` (e.g.
`Economy, Business and Finance_positive_20240128131642.zip`). Each ZIP contains ~1,000 individual
JSON files, one per article, in the standard **Webz.io/Webhose news JSON format** with fields:
`uuid`, `url`, `published` (ISO-8601), `title`, `text` (article body), `language`,
`thread` (nested site metadata), `entities` (persons/organizations/locations), `sentiment`,
`categories`. Only the `Economy, Business and Finance` category is relevant to asset scoring.

**Architectural fit:** The existing pipeline already keys every `Score` row by
`(article_id, asset_id, prompt_id, llm_model_id)`, and every `Article` carries a `source_id`.
A "parallel layer" therefore requires **no schema change** — Webz articles are ingested under a new
`Source` row (`parser_key="webz_news"`), and the existing `Scorer` scores them exactly as it scores
Snacks articles. To make the two streams independently queryable, this plan adds an **optional
source filter** to `Scorer.score_batch`, the scoring CLI, and the `POST /score` endpoint.

**Data volume note:** Webz datasets are large (~1,000 articles per ZIP). This plan focuses on the
`Economy, Business and Finance` category and supports a configurable per-run article cap so local
7B-model scoring remains tractable on the 8-GB machine.

---

## 1. Requirements & Constraints

- **REQ-001**: Implement a `WebzNewsParser` (subclass of `app.ingestion.parsers.base.ParserBase`) that reads a Webz.io dataset **ZIP archive** and yields one `ParsedArticle` per contained JSON file.
- **REQ-002**: `WebzNewsParser.PARSER_KEY` must equal `"webz_news"`. `SUPPORTED_EXTENSIONS` must equal `(".zip",)`. Because `.zip` is ambiguous, callers must pass `parser_key="webz_news"` explicitly (mirroring the `robinhood_snacks` `.txt` precedent).
- **REQ-003**: For each article JSON, map Webz fields to `ParsedArticle` as follows: `external_id = json["uuid"]`; `title = json.get("title")`; `url = json.get("url")`; `published_at = parse(json["published"])` (ISO-8601, tz-aware); `text = json.get("text", "")`; `content_hash = sha256(text)`.
- **REQ-004**: Articles with an empty or missing `text` field, or `language` not equal to `"english"`, must be skipped (not yielded).
- **REQ-005**: The parser must deduplicate within a single ZIP by `uuid` and by `content_hash`, consistent with `SnacksParser` behavior.
- **REQ-006**: Register `WebzNewsParser()` in `app/ingestion/parsers/__init__.py` alongside the existing built-in parsers.
- **REQ-007**: Implement a dataset acquisition helper `app/ingestion/webz_download.py` that downloads selected dataset ZIPs from the GitHub repository into `settings.ingest_dir / "webz"`. It must default to the `Economy, Business and Finance` category and must NOT re-download a ZIP that already exists on disk (idempotent).
- **REQ-008**: The download helper must fetch the directory listing via the GitHub REST API endpoint `https://api.github.com/repos/Webhose/free-news-datasets/contents/News_Datasets`, filter entries whose `name` starts with the configured category prefix, and download each matching entry's `download_url`.
- **REQ-009**: Add an optional `source_name: str | None = None` parameter to `Scorer.score_batch`. When provided, only articles whose `Article.source_id` matches the `Source` row with that `name` are scored.
- **REQ-010**: Add `--source NAME` argument to the scoring CLI (`app/llm/__main__.py`) and a `source_name: str | None` field to the `POST /score` request body, both wired to `Scorer.score_batch(source_name=...)`.
- **REQ-011**: Add config settings to `app/config.py`: `webz_category: str` (default `"Economy, Business and Finance"`) and `webz_max_datasets: int` (default `5`, caps how many ZIPs the downloader fetches per run).
- **REQ-012**: All parsing and scoring logic must be unit-testable with a synthetic in-memory ZIP fixture — no network calls in the test suite.
- **CON-001**: No new Python runtime packages. Use stdlib `zipfile`, `json`, `io`, `urllib.request` (or the already-present `httpx`). `httpx` is already in `requirements.txt` and is preferred for HTTP calls.
- **CON-002**: The GitHub API listing endpoint returns file names containing spaces and commas (e.g. `"Economy, Business and Finance_positive_20240128131642.zip"`). URL-encoding is handled by the API's `download_url`; do NOT hand-construct raw URLs.
- **CON-003**: Unauthenticated GitHub API requests are rate-limited to 60/hour per IP. The download helper must issue at most one listing request per run and log a clear WARNING if it receives an HTTP 403 rate-limit response.
- **CON-004**: ZIP archives may contain non-JSON files or nested directories. The parser must only process entries whose name ends with `.json` (case-insensitive) and must skip directory entries.
- **CON-005**: The `Article.external_id` column is `String(256)`. Webz `uuid` values are ~40-char hex-ish strings and fit comfortably. No truncation needed.
- **CON-006**: The `webz_news` source articles must NOT collide with Snacks articles: uniqueness is enforced by `UniqueConstraint(source_id, external_id)`, and Webz articles carry a different `source_id`, so no collision is possible even if hashes coincide.
- **GUD-001**: Log an INFO line per ZIP processed: filename, articles parsed, articles skipped (non-English / empty).
- **GUD-002**: Store the original ZIP filename on each `Article` via the existing `raw_html_path` column (repurposed as "source file path") for provenance — set it in the parser as a `ParsedArticle` field only if the field exists; otherwise leave for the loader.
- **GUD-003**: Download files atomically: stream to a `.part` file, then rename on completion, to avoid corrupt partial ZIPs.
- **PAT-001**: Follow the `SnacksParser` adapter pattern — the parser returns `list[ParsedArticle]`; DB persistence is handled by the existing `app/ingestion/normalize.py` + `loader.py`, unchanged.
- **PAT-002**: Reuse the existing `ingest_path` orchestrator and `POST /ingest` endpoint for DB persistence. No new ingestion API is needed — only a new parser and a downloader.

---

## 2. Implementation Steps

### Implementation Phase 1 — Webz News Parser

- GOAL-001: Create `app/ingestion/parsers/webz.py` with `WebzNewsParser` that converts a Webz.io dataset ZIP into `ParsedArticle` records, and register it in the parser registry.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `backend/app/ingestion/parsers/webz.py`. Imports: `from __future__ import annotations`, `import hashlib`, `import json`, `import logging`, `import zipfile`, `from datetime import datetime, timezone`, `from pathlib import Path`, `from app.ingestion.parsers.base import ParsedArticle, ParserBase`. Define `log = logging.getLogger(__name__)`. | | |
| TASK-002 | Implement `_parse_published(raw: str \| None) -> datetime \| None` in `webz.py`. Parses ISO-8601 strings (Webz uses e.g. `"2024-01-28T13:16:42.000+02:00"`). Use `datetime.fromisoformat` after normalizing a trailing `"Z"` to `"+00:00"`. Naive results are set to UTC. Returns `None` on failure. | | |
| TASK-003 | Implement `_sha256(text: str) -> str` in `webz.py` returning `hashlib.sha256(text.encode("utf-8")).hexdigest()`. | | |
| TASK-004 | Implement `WebzNewsParser(ParserBase)` in `webz.py` with class attributes `PARSER_KEY = "webz_news"` and `SUPPORTED_EXTENSIONS = (".zip",)`. | | |
| TASK-005 | Implement `WebzNewsParser._parse_one(self, raw_bytes: bytes) -> ParsedArticle \| None`. Decode bytes as UTF-8 (errors="replace"), `json.loads`, then: skip (return `None`) if `text` is empty/missing OR `language` (lowercased) is not `"english"`; else build and return a `ParsedArticle` per REQ-003. | | |
| TASK-006 | Implement `WebzNewsParser.parse(self, path: Path) -> list[ParsedArticle]`. Open `path` with `zipfile.ZipFile`. Iterate `namelist()`; skip entries not ending in `.json` (case-insensitive) and skip directory entries (names ending in `/`). For each JSON entry, read bytes via `zf.read(name)` and call `_parse_one`. Deduplicate by `external_id` and `content_hash` (per REQ-005). Log an INFO summary per ZIP (per GUD-001). Return the deduplicated list. | | |
| TASK-007 | Handle a corrupt/non-ZIP file gracefully: wrap the `zipfile.ZipFile(path)` call in try/except `zipfile.BadZipFile`; on failure log an ERROR and return `[]`. | | |
| TASK-008 | Modify `backend/app/ingestion/parsers/__init__.py`: add `from app.ingestion.parsers import webz as _webz_mod` and `register(_webz_mod.WebzNewsParser())` in the auto-registration block. | | |

### Implementation Phase 2 — Dataset Downloader

- GOAL-002: Create `app/ingestion/webz_download.py` to fetch Webz dataset ZIPs (filtered by category) from GitHub into the ingest directory, idempotently.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Add `webz_category: str = "Economy, Business and Finance"` and `webz_max_datasets: int = 5` to `Settings` in `backend/app/config.py`. | | |
| TASK-010 | Create `backend/app/ingestion/webz_download.py`. Imports: `from __future__ import annotations`, `import logging`, `import httpx`, `from pathlib import Path`, `from app.config import settings`. Define `log = logging.getLogger(__name__)` and the constant `_API_URL = "https://api.github.com/repos/Webhose/free-news-datasets/contents/News_Datasets"`. | | |
| TASK-011 | Implement `list_datasets(category: str) -> list[dict]` in `webz_download.py`. GET `_API_URL` with header `{"Accept": "application/vnd.github+json"}` and a 30s timeout. On HTTP 403, log a WARNING about rate-limiting and return `[]`. Filter the JSON array to entries where `entry["name"].startswith(category)` and `entry["type"] == "file"`. Return the filtered list (each dict has `name` and `download_url`). | | |
| TASK-012 | Implement `download_dataset(entry: dict, dest_dir: Path) -> Path \| None` in `webz_download.py`. Target path = `dest_dir / entry["name"]`. If the target already exists, log DEBUG "cache hit" and return it (idempotent, REQ-007). Otherwise stream `entry["download_url"]` to `target.with_suffix(target.suffix + ".part")` using `httpx.stream`, then rename to the final path (atomic, GUD-003). Return the final path. On any exception, log ERROR, remove any `.part` file, return `None`. | | |
| TASK-013 | Implement `download_category(category: str \| None = None, max_datasets: int \| None = None, dest_dir: Path \| None = None) -> list[Path]` in `webz_download.py`. Resolve defaults from `settings` (`webz_category`, `webz_max_datasets`, `settings.ingest_dir / "webz"`). Create `dest_dir` if absent. Call `list_datasets`, cap the list to `max_datasets`, call `download_dataset` for each, and return the list of successfully downloaded paths. Log an INFO summary. | | |
| TASK-014 | Add a `__main__` guard / CLI to `webz_download.py`: `python -m app.ingestion.webz_download [--category NAME] [--max N] [--dest DIR]` that calls `download_category` and prints the downloaded file paths. | | |

### Implementation Phase 3 — Source-Aware Scoring

- GOAL-003: Enable scoring to be scoped to a single source so the Snacks and Webz layers can be run and queried independently.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-015 | Modify `Scorer.score_batch` in `backend/app/llm/scorer.py`: add keyword parameter `source_name: str \| None = None`. When not `None`, resolve the `Source` row by `name`; if not found, raise `ValueError(f"Source '{source_name}' not found")`; else add `.filter(Article.source_id == source.id)` to the article query. Import `Source` from `app.db.models`. | | |
| TASK-016 | Update the `score_batch` return dict docstring to note that counts are scoped to the source when `source_name` is provided. No structural change to the return dict. | | |
| TASK-017 | Modify `backend/app/llm/__main__.py`: add argument `--source` (metavar `NAME`, default `None`, help "Only score articles from this source name.") and pass `source_name=args.source` to `scorer.score_batch(...)`. | | |
| TASK-018 | Modify `backend/app/api/routes/score.py`: add field `source_name: str \| None = Field(default=None, description="Only score articles from this source name (e.g. 'Webz News').")` to `ScoreRequest`, and pass `source_name=req.source_name` to `_scorer.score_batch(...)`. | | |

### Implementation Phase 4 — Tests

- GOAL-004: Full unit test coverage for the parser, downloader (mocked HTTP), and source-scoped scoring, using synthetic in-memory data — no network or live Ollama.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-019 | Create `backend/tests/ingestion/fixtures/make_webz_zip.py` helper (or a pytest fixture in the test file) that builds an in-memory ZIP (`io.BytesIO` + `zipfile.ZipFile`) containing N synthetic Webz-format JSON article files, writes it to a `tmp_path` file, and returns the path. Each JSON has `uuid`, `title`, `url`, `published`, `text`, `language` fields. | | |
| TASK-020 | Create `backend/tests/ingestion/test_webz_parser.py`. Test `WebzNewsParser.parse` on a 3-article synthetic ZIP returns 3 `ParsedArticle` objects with correct `external_id`, `title`, `text`, and tz-aware `published_at`. | | |
| TASK-021 | Test `WebzNewsParser` skips non-English articles: include one article with `language="spanish"`; assert it is not returned. | | |
| TASK-022 | Test `WebzNewsParser` skips empty-text articles: include one article with `text=""`; assert it is not returned. | | |
| TASK-023 | Test `WebzNewsParser` deduplicates by `uuid`: include two articles with the same `uuid`; assert only one is returned. | | |
| TASK-024 | Test `WebzNewsParser` skips non-JSON entries: add a `readme.txt` entry to the ZIP; assert it is ignored and does not raise. | | |
| TASK-025 | Test `WebzNewsParser.parse` returns `[]` (no exception) for a corrupt/non-ZIP file (write random bytes to a `.zip` path). | | |
| TASK-026 | Test the parser is registered: `from app.ingestion import parsers; assert "webz_news" in parsers.registered_keys()` and `parsers.get_by_key("webz_news")` returns a `WebzNewsParser` instance. | | |
| TASK-027 | Test end-to-end ingestion via the existing orchestrator: call `ingest_path(zip_path, "Webz News", db, parser_key="webz_news")` using the in-memory `db` fixture; assert `articles_inserted == 3` and a `Source` row named "Webz News" exists with `parser_key="webz_news"`. | | |
| TASK-028 | Create `backend/tests/ingestion/test_webz_download.py`. Test `list_datasets` filters by category prefix: monkeypatch `httpx.get` to return a fake JSON listing with mixed category names; assert only matching entries are returned. | | |
| TASK-029 | Test `download_dataset` idempotency: create an existing target file in `tmp_path`; call `download_dataset` with `httpx.stream` monkeypatched to raise if called; assert the existing file is returned and no download occurs. | | |
| TASK-030 | Test `list_datasets` returns `[]` on HTTP 403 (monkeypatch `httpx.get` to return a 403 response). | | |
| TASK-031 | Create `backend/tests/llm/test_source_scoped_scoring.py`. Seed two sources (Snacks + Webz) each with articles, seed assets + model, and score with `source_name="Webz News"`; assert only Webz articles were processed (`articles_processed` equals the Webz article count). Use a mock `LLMEngine` and `context_builder` stub. | | |
| TASK-032 | Test `score_batch(source_name="Nonexistent")` raises `ValueError`. | | |

---

## 3. Alternatives

- **ALT-001**: Add a dedicated `news_articles` table separate from `articles`. Rejected — the existing `articles` table with `source_id` already models multiple sources cleanly; a second table would duplicate the scoring join logic and require a schema migration.
- **ALT-002**: Add a `layer` or `stream` enum column to `scores` to tag Snacks vs. Webz predictions. Rejected — the layer is fully derivable via `scores.article_id → articles.source_id → sources.name`; a redundant column risks drifting out of sync.
- **ALT-003**: Clone the entire Webz repository with `git clone` and read from disk. Rejected — the repo is large (hundreds of multi-MB ZIPs across all categories); the GitHub Contents API lets us download only the finance-relevant subset.
- **ALT-004**: Parse ZIPs eagerly into `snacks_v0.jsonl`-style intermediate JSONL. Rejected — the DB (`articles` table) is now the canonical store (per Phase 2 completion); intermediate JSONL is no longer needed.
- **ALT-005**: Use `urllib.request` from stdlib instead of `httpx`. Rejected — `httpx` is already a dependency, supports streaming downloads and timeouts more ergonomically, and is used elsewhere in the codebase (`app/llm/engine.py`).

---

## 4. Dependencies

- **DEP-001**: `httpx >= 0.27` — GitHub API listing + ZIP download (already in `requirements.txt`).
- **DEP-002**: Python stdlib `zipfile`, `json`, `io`, `hashlib`, `datetime` — no install required.
- **DEP-003**: `app/ingestion/parsers/base.py` — `ParsedArticle`, `ParserBase` (already defined).
- **DEP-004**: `app/ingestion/parsers/__init__.py` — parser registry `register()` / `registered_keys()` / `get_by_key()` (already defined).
- **DEP-005**: `app/ingestion/loader.py` + `app/ingestion/normalize.py` — reused unchanged for DB persistence.
- **DEP-006**: `app/llm/scorer.py` — `Scorer.score_batch` modified in Phase 3 (backwards-compatible).
- **DEP-007**: `app/config.py` — `settings.ingest_dir` (exists), plus new `webz_category` / `webz_max_datasets` (added in TASK-009).
- **DEP-008**: Network access to `api.github.com` and `raw.githubusercontent.com` for the download step (production only; tests mock all HTTP).
- **DEP-009**: Webz.io Terms of Use — datasets are free for academic/research/journalistic use per the repository [TOU](https://github.com/Webhose/free-news-datasets/blob/master/tou.MD). Compliance is an operational assumption (ASSUMPTION-003).

---

## 5. Files

- **FILE-001**: `backend/app/ingestion/parsers/webz.py` — new; `WebzNewsParser`, `_parse_published`, `_sha256`.
- **FILE-002**: `backend/app/ingestion/parsers/__init__.py` — modified; register `WebzNewsParser`.
- **FILE-003**: `backend/app/ingestion/webz_download.py` — new; `list_datasets`, `download_dataset`, `download_category`, CLI.
- **FILE-004**: `backend/app/config.py` — modified; add `webz_category` and `webz_max_datasets` settings.
- **FILE-005**: `backend/app/llm/scorer.py` — modified; add `source_name` param to `score_batch`.
- **FILE-006**: `backend/app/llm/__main__.py` — modified; add `--source` CLI argument.
- **FILE-007**: `backend/app/api/routes/score.py` — modified; add `source_name` to `ScoreRequest`.
- **FILE-008**: `backend/tests/ingestion/test_webz_parser.py` — new; parser tests (TASK-020 through TASK-027).
- **FILE-009**: `backend/tests/ingestion/test_webz_download.py` — new; downloader tests (TASK-028 through TASK-030).
- **FILE-010**: `backend/tests/llm/test_source_scoped_scoring.py` — new; source-scoped scoring tests (TASK-031, TASK-032).
- **FILE-011**: `backend/tests/ingestion/fixtures/make_webz_zip.py` — new (optional); synthetic Webz ZIP builder helper.

---

## 6. Testing

- **TEST-001**: `test_parse_three_articles` — 3-article ZIP → 3 `ParsedArticle` with correct fields (TASK-020).
- **TEST-002**: `test_skips_non_english` — Spanish article excluded (TASK-021).
- **TEST-003**: `test_skips_empty_text` — empty-text article excluded (TASK-022).
- **TEST-004**: `test_dedupes_by_uuid` — duplicate `uuid` collapsed to one (TASK-023).
- **TEST-005**: `test_ignores_non_json_entries` — `readme.txt` in ZIP ignored (TASK-024).
- **TEST-006**: `test_corrupt_zip_returns_empty` — non-ZIP file → `[]`, no raise (TASK-025).
- **TEST-007**: `test_parser_registered` — `"webz_news"` in registry (TASK-026).
- **TEST-008**: `test_end_to_end_ingest` — `ingest_path` inserts 3 articles + creates Source (TASK-027).
- **TEST-009**: `test_list_datasets_filters_category` — only matching category entries returned (TASK-028).
- **TEST-010**: `test_download_idempotent` — existing file returned, no re-download (TASK-029).
- **TEST-011**: `test_list_datasets_rate_limited` — HTTP 403 → `[]` (TASK-030).
- **TEST-012**: `test_score_batch_source_filter` — only Webz articles scored when `source_name="Webz News"` (TASK-031).
- **TEST-013**: `test_score_batch_unknown_source_raises` — unknown source → `ValueError` (TASK-032).

---

## 7. Risks & Assumptions

- **RISK-001**: GitHub unauthenticated API rate limit (60 req/hour). A single `list_datasets` call per run is well within budget, but repeated dev iterations could hit the limit. Mitigate: the downloader makes exactly one listing call per run and caches downloaded ZIPs on disk (idempotent). CON-003 handles the 403 case gracefully.
- **RISK-002**: Webz JSON schema drift. The `published`, `text`, `language`, `uuid` field names are stable in the Webz.io format, but future dataset revisions could rename fields. Mitigate: `_parse_one` uses `.get()` with skip-on-missing semantics (REQ-004), so unknown/missing fields degrade gracefully rather than crashing.
- **RISK-003**: Article relevance dilution. General news (even finance-category) contains many articles irrelevant to the 44-asset universe. The LLM will assign `score=0.0, confidence=0.0` to unmentioned assets (per existing prompt rules), producing many zero-signal rows. Mitigate: acceptable for a first pass; a relevance pre-filter (keyword/entity match against the universe) is noted as a future enhancement.
- **RISK-004**: Scoring cost/time. ~1,000 articles per ZIP × 3 kind-calls each is a large local-LLM workload. Mitigate: `webz_max_datasets` caps downloads (default 5) and `score_batch(limit=N)` caps scoring per run; the `POST /score` endpoint retains its default `limit=10` guard.
- **RISK-005**: Duplicate content across categories. The same article may appear in multiple Webz datasets. Mitigate: within a ZIP, dedup is by `uuid`/hash; across ZIPs under the same source, the `UniqueConstraint(source_id, external_id)` plus `normalize.upsert_articles` hash/external-id checks prevent duplicate `Article` rows.
- **ASSUMPTION-001**: All Webz articles are ingested under a single `Source` named consistently (e.g. `"Webz News"`) so the source filter in Phase 3 works. The operator must pass the same `--source` / `source_name` value at ingest and score time.
- **ASSUMPTION-002**: The Webz `published` timestamp reflects the article's true publication time and is suitable as the `published_at` used for forward-return alignment (Phase 5).
- **ASSUMPTION-003**: Use of the Webz.io datasets complies with their Terms of Use (academic/research use). This is an operational/legal assumption, not enforced in code.
- **ASSUMPTION-004**: The GitHub Contents API returns all `News_Datasets` entries in a single response (the directory has < 1,000 entries, under the API's per-page cap). If pagination becomes necessary, `list_datasets` must be extended to follow `Link` headers — noted but not implemented in v1.

---

## 8. Related Specifications / Further Reading

- [TODO.md — Phase 2 Local File Ingestion](../TODO.md#phase-2--local-file-ingestion--complete-2026-07-02) — defines the pluggable parser framework (`ParserBase`, registry, `loader.py`, `normalize.py`) that this plan extends with `WebzNewsParser`.
- [TODO.md — Phase 3 LLM Scoring Engine](../TODO.md#phase-3--llm-scoring-engine--complete-2026-07-02) — defines the `Scorer.score_batch` method modified in Phase 3 of this plan.
- [plan/feature-market-context-llm-1.md](feature-market-context-llm-1.md) — related plan; the market-context injection applies equally to Webz-sourced scoring calls.
- [app/ingestion/parsers/snacks.py](../backend/app/ingestion/parsers/snacks.py) — the adapter pattern `WebzNewsParser` follows.
- [Webz.io free-news-datasets repository](https://github.com/Webhose/free-news-datasets) — the data source.
- [Webz.io datasets Terms of Use](https://github.com/Webhose/free-news-datasets/blob/master/tou.MD) — usage terms.
- [GitHub Contents API](https://docs.github.com/en/rest/repos/contents#get-repository-content) — used by `list_datasets` to enumerate dataset ZIPs.
