---
goal: Live Web Article Ingestion via newspaper3k with an Evolving Source Registry
version: 1.0
date_created: 2026-07-02
last_updated: 2026-07-02
owner: llm-market-scoring
status: 'Planned'
tags: [feature, ingestion, web-scraping, newspaper3k, data, architecture]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan adds a **live web article ingestion layer** using
[`newspaper3k`](https://github.com/codelucas/newspaper). Unlike the existing file-based parsers
(Snacks mbox, Webz ZIP), this layer pulls **current** news directly from configured news-website
URLs, extracts clean article text, and persists it into the same `articles` table used by the rest
of the pipeline. The goal is a **living, growing corpus** that keeps the system current and lets
operators register new sources over time without code changes.

**How newspaper3k works (verified 2026-07-02):**
- `newspaper.build(base_url, memoize_articles=False)` discovers article URLs across a news site,
  returning a source object with `.articles` (list of `Article`) and `.category_urls()`.
- `Article(url).download(); article.parse()` populates `.title`, `.text`, `.publish_date`,
  `.authors`, `.top_image`, `.canonical_link`.
- Basic text extraction requires only `lxml` (already a dependency) and `requests`. The NLTK corpora
  download is **only** needed for `.nlp()` (keyword/summary extraction), which this plan does NOT
  use — so no corpora download is required.

**Architectural fit:** newspaper3k operates on **URLs, not files**, so it does not fit the existing
`ParserBase.parse(path)` interface (which is file-based). This plan introduces a **parallel web
fetcher** (`app/ingestion/web/`) that produces the same `ParsedArticle` records and reuses the
existing `app/ingestion/normalize.py` (`ensure_source`, `upsert_articles`) for DB persistence.
The `sources` table is extended with a nullable `url` column so website sources are persisted and
crawlable on demand — this is the mechanism by which new sources are added over time.

**Scoring integration:** Because web articles land in the same `articles` table under their own
`Source` rows, the existing `Scorer` (and the `source_name` filter proposed in the Webz plan) scores
them with no further changes.

---

## 1. Requirements & Constraints

- **REQ-001**: Add `newspaper3k` to `backend/requirements.txt`. Text extraction must function without downloading NLTK corpora (do NOT call `Article.nlp()`).
- **REQ-002**: Add a nullable `url` column (`String(1024)`) to the `sources` table via a new Alembic migration, storing the base URL of a website source.
- **REQ-003**: Implement `app/ingestion/web/fetcher.py` with `fetch_article(url: str, config: NewspaperConfig) -> ParsedArticle | None` that downloads and parses a single article URL into a `ParsedArticle`.
- **REQ-004**: `fetch_article` field mapping: `external_id = canonical_url` (fallback to the input `url`); `title = article.title or None`; `url = canonical_url`; `published_at = article.publish_date` (tz-aware; naive → UTC; `None` → `None`); `text = article.text`; `content_hash = sha256(text)`.
- **REQ-005**: Articles with empty/whitespace-only `text` (fewer than `min_text_chars`, default 200) must be skipped (return `None`).
- **REQ-006**: Implement `discover_article_urls(base_url: str, config: NewspaperConfig, limit: int) -> list[str]` using `newspaper.build(base_url, memoize_articles=False)` and returning up to `limit` article URLs.
- **REQ-007**: Implement `crawl_source(base_url: str, limit: int, seen_urls: set[str], config: NewspaperConfig) -> list[ParsedArticle]` that discovers URLs, skips any already in `seen_urls`, fetches each new URL via `fetch_article`, and returns the parsed articles. Deduplicate by `external_id` and `content_hash` within the batch.
- **REQ-008**: Implement `register_web_source(db, name: str, url: str) -> Source` in `app/ingestion/web/registry.py`. Creates or updates a `Source` row with `type="website"`, `parser_key="newspaper_web"`, and the given `url`. Idempotent by `name`.
- **REQ-009**: Implement `crawl_registered_source(db, source_name: str, limit: int, config: NewspaperConfig | None = None) -> dict` in `app/ingestion/web/registry.py`. Loads the `Source` row by `name`, builds `seen_urls` from existing `Article.external_id` values for that source (incremental), crawls, and persists via `upsert_articles`. Returns `{"source_id", "discovered", "fetched", "inserted", "skipped", "errors"}`.
- **REQ-010**: Add a CLI `python -m app.ingestion.web` supporting subcommands: `add-source --name NAME --url URL`; `crawl --name NAME [--limit N]`; `list-sources`.
- **REQ-011**: Add API routes under the existing ingest router prefix: `POST /ingest/web/sources` (register a source), `GET /ingest/web/sources` (list website sources), `POST /ingest/web/crawl` (crawl a named source with an optional limit).
- **REQ-012**: Add config settings to `app/config.py`: `newspaper_user_agent: str` (default a real browser UA string), `newspaper_request_timeout: int` (default 20), `newspaper_crawl_limit: int` (default 25), `newspaper_min_text_chars: int` (default 200).
- **REQ-013**: All fetching/crawling logic must be unit-testable with `newspaper` mocked — no network calls in the test suite.
- **SEC-001**: The `POST /ingest/web/crawl` and `POST /ingest/web/sources` endpoints accept operator-supplied URLs. Validate that submitted URLs use the `http` or `https` scheme only (reject `file:`, `ftp:`, etc.) to prevent SSRF-style local-file access.
- **SEC-002**: Set a bounded `request_timeout` (REQ-012) and a hard `limit` cap (max 200 per crawl) to prevent unbounded resource consumption from a single API call.
- **SEC-003**: Do not log full article HTML or operator credentials. Log only URLs, counts, and error types.
- **CON-001**: `newspaper3k` pulls in transitive dependencies (`requests`, `lxml`, `Pillow`, `feedparser`, `tldextract`, `beautifulsoup4`). `lxml` and `beautifulsoup4` are already present. Others are acceptable additions (no CUDA/model downloads).
- **CON-002**: newspaper3k does NOT respect `robots.txt` by default and can be blocked by anti-scraping measures (403/captcha). This plan targets low-volume, respectful crawling of RSS-friendly finance news sites. Rate limiting between article fetches must be configurable (`newspaper_request_timeout` governs per-request timeout, not delay; add `newspaper_crawl_delay_sec: float = 1.0` for inter-request politeness).
- **CON-003**: `newspaper.build()` with `memoize_articles=False` re-discovers all URLs each run; incremental behavior is achieved by the DB `seen_urls` filter (REQ-009), not by newspaper's memoization cache.
- **CON-004**: The `sources` table migration must use Alembic autogenerate + `render_as_batch=True` (SQLite requirement, per existing project convention in `alembic/env.py`).
- **CON-005**: `Article.external_id` is `String(256)`. Canonical URLs may exceed 256 chars. If a URL exceeds 256 chars, use `sha256(url)` (64 chars) as `external_id` and store the full URL in the `url` column. This rule is implemented in `fetch_article`.
- **GUD-001**: Wrap every network operation (`download`, `build`, `parse`) in try/except; on failure, log a WARNING with the URL and continue — a single bad article must never abort a crawl.
- **GUD-002**: Log an INFO summary per crawl: source name, discovered count, fetched count, inserted count, skipped count, error count.
- **GUD-003**: Reuse the shared `NewspaperConfig` (a `newspaper.Config` instance built once from `settings`) across all article fetches in a crawl to avoid rebuilding it per URL.
- **PAT-001**: Reuse `app/ingestion/normalize.py` (`ensure_source`, `upsert_articles`) unchanged for persistence. The web layer produces `ParsedArticle` objects identical in shape to file parsers.
- **PAT-002**: Keep the web fetcher decoupled from `newspaper` import at module top-level via a thin wrapper so tests can inject a mock. Import `newspaper` inside functions or accept a `builder`/`article_factory` injectable, mirroring the `Scorer(context_builder=...)` pattern.

---

## 2. Implementation Steps

### Implementation Phase 1 — Dependencies & Configuration

- GOAL-001: Add the `newspaper3k` dependency and all web-ingestion configuration settings.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `newspaper3k>=0.2.8` to `backend/requirements.txt` under a new `# Web article extraction` section. | | |
| TASK-002 | Install the dependency into the venv: run `.\.venv\Scripts\python.exe -m pip install newspaper3k` from `backend/`. Validation: `python -c "import newspaper; print(newspaper.__version__)"` exits 0. | | |
| TASK-003 | Add to `Settings` in `backend/app/config.py`: `newspaper_user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"`, `newspaper_request_timeout: int = 20`, `newspaper_crawl_limit: int = 25`, `newspaper_min_text_chars: int = 200`, `newspaper_crawl_delay_sec: float = 1.0`. | | |
| TASK-004 | Add the corresponding keys to the root `.env.example` under a new `# ---- Web article ingestion (newspaper3k) ----` section. | | |

### Implementation Phase 2 — Schema Extension

- GOAL-002: Add a nullable `url` column to the `sources` table so website sources persist their base URL.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Modify `Source` in `backend/app/db/models.py`: add `url: Mapped[str \| None] = mapped_column(String(1024))` after the `parser_key` column. | | |
| TASK-006 | Generate the migration: `.\.venv\Scripts\alembic.exe revision --autogenerate -m "add sources.url"` from `backend/`. Verify the generated migration adds the `url` column using a batch operation (`op.batch_alter_table`). | | |
| TASK-007 | Apply the migration: `.\.venv\Scripts\alembic.exe upgrade head`. Validation: `PRAGMA table_info(sources)` includes a `url` column. | | |
| TASK-008 | Update `app/ingestion/normalize.py` `ensure_source` to accept an optional `url: str \| None = None` parameter and set it on newly created `Source` rows (existing callers unaffected — default `None`). | | |

### Implementation Phase 3 — Newspaper Fetcher Core

- GOAL-003: Create `app/ingestion/web/fetcher.py` with single-article and crawl functions, fully mockable.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Create `backend/app/ingestion/web/__init__.py` (empty package marker). | | |
| TASK-010 | Create `backend/app/ingestion/web/fetcher.py`. Imports: `from __future__ import annotations`, `import hashlib`, `import logging`, `from datetime import datetime, timezone`, `from app.ingestion.parsers.base import ParsedArticle`, `from app.config import settings`. Define `log = logging.getLogger(__name__)`. | | |
| TASK-011 | Implement `build_config() -> "newspaper.Config"` in `fetcher.py`. Imports `newspaper` inside the function. Returns a `newspaper.Config` with `browser_user_agent=settings.newspaper_user_agent`, `request_timeout=settings.newspaper_request_timeout`, `memoize_articles=False`, `fetch_images=False`. | | |
| TASK-012 | Implement `_normalize_dt(dt) -> datetime \| None` in `fetcher.py`. Returns `None` if `dt` is falsy; if `dt.tzinfo is None`, set UTC; else return as-is. | | |
| TASK-013 | Implement `_resolve_external_id(url: str) -> str` in `fetcher.py`. Returns `url` if `len(url) <= 256`, else `hashlib.sha256(url.encode()).hexdigest()` (per CON-005). | | |
| TASK-014 | Implement `fetch_article(url, config=None, *, article_factory=None) -> ParsedArticle \| None` in `fetcher.py`. Use `article_factory or (lambda u, c: newspaper.Article(u, config=c))` (import newspaper inside). Call `.download()` then `.parse()`. On any exception, log WARNING and return `None`. Skip if `len(article.text.strip()) < settings.newspaper_min_text_chars` (REQ-005). Build `ParsedArticle` per REQ-004, using `article.canonical_link or url` as the canonical URL and `_resolve_external_id`. | | |
| TASK-015 | Implement `discover_article_urls(base_url, config=None, limit=None, *, builder=None) -> list[str]` in `fetcher.py`. Use `builder or (lambda u, c: newspaper.build(u, config=c))`. Return up to `limit` (default `settings.newspaper_crawl_limit`) URLs from `source.articles`. On exception, log WARNING and return `[]`. | | |
| TASK-016 | Implement `crawl_source(base_url, limit, seen_urls, config=None, *, builder=None, article_factory=None, delay_sec=None) -> list[ParsedArticle]` in `fetcher.py`. Discover URLs; filter out any in `seen_urls`; for each remaining URL call `fetch_article`; sleep `delay_sec or settings.newspaper_crawl_delay_sec` between fetches (use `time.sleep`); dedupe by `external_id` + `content_hash`; return the list. Log per GUD-002 (partial — full summary logged by registry). | | |

### Implementation Phase 4 — Source Registry, Orchestration, CLI & API

- GOAL-004: Persist website sources, orchestrate incremental crawls, and expose CLI + API surfaces.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-017 | Create `backend/app/ingestion/web/registry.py`. Imports SQLAlchemy `Session`, `Source`, `Article` from `app.db.models`, `ensure_source`/`upsert_articles` from `app.ingestion.normalize`, and fetcher functions. | | |
| TASK-018 | Implement `register_web_source(db, name, url) -> Source` in `registry.py`. Validate `url` scheme is `http`/`https` (raise `ValueError` otherwise, SEC-001). Call `ensure_source(db, name, parser_key="newspaper_web", url=url)`; if the source exists but its `url` differs, update it. Set `type="website"`. `db.commit()`. Return the `Source`. | | |
| TASK-019 | Implement `list_web_sources(db) -> list[Source]` in `registry.py` returning all `Source` rows with `type == "website"`. | | |
| TASK-020 | Implement `crawl_registered_source(db, source_name, limit=None, config=None) -> dict` in `registry.py`. Load `Source` by name (raise `ValueError` if missing or `url` is `None`). Build `seen_urls` = set of `Article.external_id` for that `source_id`. Call `crawl_source(source.url, limit or settings.newspaper_crawl_limit, seen_urls, config)`. Persist via `upsert_articles`. Log INFO summary (GUD-002). Return `{"source_id", "discovered", "fetched", "inserted", "skipped", "errors"}`. | | |
| TASK-021 | Create `backend/app/ingestion/web/__main__.py` CLI with subcommands `add-source --name --url`, `crawl --name [--limit]`, `list-sources`. Each opens a `SessionLocal()` and calls the corresponding registry function; prints a summary. | | |
| TASK-022 | Modify `backend/app/api/routes/ingest.py`: add Pydantic models `WebSourceRequest(name: str, url: str)`, `WebSourceItem(id, name, url)`, `CrawlRequest(source_name: str, limit: int = Field(default=25, ge=1, le=200))`, `CrawlResponse(source_id, discovered, fetched, inserted, skipped, errors)`. | | |
| TASK-023 | Add route `POST /ingest/web/sources` in `ingest.py` calling `register_web_source`; return the created `WebSourceItem`. Map `ValueError` (bad scheme) to HTTP 422. | | |
| TASK-024 | Add route `GET /ingest/web/sources` in `ingest.py` calling `list_web_sources`; return `list[WebSourceItem]`. | | |
| TASK-025 | Add route `POST /ingest/web/crawl` in `ingest.py` calling `crawl_registered_source`; map `ValueError` to HTTP 422; return `CrawlResponse`. Enforce the `limit` cap (SEC-002) via the Pydantic `le=200` bound. | | |

### Implementation Phase 5 — Tests

- GOAL-005: Full unit test coverage with `newspaper` fully mocked — no network, no live DB beyond the in-memory fixture.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-026 | Create `backend/tests/ingestion/web/__init__.py` (empty). | | |
| TASK-027 | Create `backend/tests/ingestion/web/test_fetcher.py`. Define a `FakeArticle` class with `download()`, `parse()` no-ops and attributes `title`, `text`, `publish_date`, `canonical_link`. | | |
| TASK-028 | Test `fetch_article` maps fields correctly using an `article_factory` that returns a `FakeArticle` with known values. Assert `external_id`, `title`, `text`, tz-aware `published_at`, and `content_hash` length 64. | | |
| TASK-029 | Test `fetch_article` returns `None` when `text` is shorter than `min_text_chars`. | | |
| TASK-030 | Test `fetch_article` returns `None` (no raise) when the `article_factory` raises an exception (simulated network error). | | |
| TASK-031 | Test `_resolve_external_id`: URL ≤ 256 chars returned as-is; URL > 256 chars returns a 64-char hex hash (CON-005). | | |
| TASK-032 | Test `discover_article_urls` returns URLs from a mock `builder` and respects `limit`. | | |
| TASK-033 | Test `crawl_source` skips URLs already in `seen_urls` and dedupes by content hash, using mock `builder` + `article_factory` and `delay_sec=0`. | | |
| TASK-034 | Create `backend/tests/ingestion/web/test_registry.py`. Test `register_web_source` creates a `Source` with `type="website"`, `parser_key="newspaper_web"`, and the URL (using the in-memory `db` fixture). | | |
| TASK-035 | Test `register_web_source` raises `ValueError` for a `file://` URL (SEC-001). | | |
| TASK-036 | Test `register_web_source` updates the URL when called again with the same name and a new URL. | | |
| TASK-037 | Test `crawl_registered_source` end-to-end with mocked fetcher functions (monkeypatch `crawl_source` to return two `ParsedArticle`s): assert two articles inserted, correct return dict, and idempotent second call inserts zero (via `seen_urls`). | | |
| TASK-038 | Test `crawl_registered_source` raises `ValueError` for an unknown source name. | | |

---

## 3. Alternatives

- **ALT-001**: Use `newspaper4k` (the maintained community fork) instead of `newspaper3k`. `newspaper4k` has active maintenance and fewer install issues. Rejected as the default because the user explicitly referenced `codelucas/newspaper` (newspaper3k); noted here as a drop-in swap if newspaper3k proves unstable (same API surface).
- **ALT-002**: Use `feedparser` on RSS feeds instead of newspaper's site crawling. More reliable and polite (RSS is designed for machine consumption) but yields only summaries, not full article text. Rejected as primary; could complement newspaper by seeding article URLs from RSS in a future enhancement.
- **ALT-003**: Force web articles through the existing file-based `ParserBase`/`ingest_path` by first saving downloaded HTML to disk. Rejected — adds a needless disk round-trip and conflates the file-loader's responsibilities; a dedicated web path is cleaner (PAT-001).
- **ALT-004**: Store website source configs in a YAML/py file instead of the DB. Rejected — a DB-backed source registry (with the new `url` column) lets operators add sources at runtime via the API, directly serving the "evolve with time / add new sources" goal.
- **ALT-005**: Call `Article.nlp()` to capture keywords/summary. Rejected — requires the NLTK corpora download and adds no value to LLM scoring (the LLM reads the full `text`). Avoiding it keeps setup lightweight (REQ-001).
- **ALT-006**: Use a background task queue (Celery/RQ) for crawls. Rejected for now — crawls are bounded (≤ 200 articles) and run synchronously with a hard timeout; the existing FastAPI request model suffices. Async job orchestration is deferred to Phase 9 of the main TODO.

---

## 4. Dependencies

- **DEP-001**: `newspaper3k >= 0.2.8` — article discovery, download, and text extraction (new; added in TASK-001).
- **DEP-002**: `lxml`, `beautifulsoup4` — HTML parsing backends for newspaper3k (already in `requirements.txt`).
- **DEP-003**: `requests` — HTTP layer used internally by newspaper3k (transitively installed).
- **DEP-004**: `app/ingestion/parsers/base.py` — `ParsedArticle` dataclass (reused, unchanged).
- **DEP-005**: `app/ingestion/normalize.py` — `ensure_source` (modified in TASK-008 to accept `url`), `upsert_articles` (unchanged).
- **DEP-006**: `app/db/models.py` — `Source` (schema change in TASK-005), `Article` (unchanged).
- **DEP-007**: Alembic migration tooling configured in `backend/alembic/` (existing; used in TASK-006/007).
- **DEP-008**: `app/api/routes/ingest.py` — existing ingest router (extended with web routes in Phase 4).
- **DEP-009**: Network access to target news sites (production only; all tests mock `newspaper`).

---

## 5. Files

- **FILE-001**: `backend/requirements.txt` — modified; add `newspaper3k>=0.2.8`.
- **FILE-002**: `.env.example` — modified; add newspaper config keys.
- **FILE-003**: `backend/app/config.py` — modified; add 5 newspaper settings (TASK-003).
- **FILE-004**: `backend/app/db/models.py` — modified; add `Source.url` column.
- **FILE-005**: `backend/alembic/versions/<hash>_add_sources_url.py` — new; autogenerated migration.
- **FILE-006**: `backend/app/ingestion/normalize.py` — modified; `ensure_source(url=...)` parameter.
- **FILE-007**: `backend/app/ingestion/web/__init__.py` — new; package marker.
- **FILE-008**: `backend/app/ingestion/web/fetcher.py` — new; `build_config`, `fetch_article`, `discover_article_urls`, `crawl_source`, helpers.
- **FILE-009**: `backend/app/ingestion/web/registry.py` — new; `register_web_source`, `list_web_sources`, `crawl_registered_source`.
- **FILE-010**: `backend/app/ingestion/web/__main__.py` — new; CLI (`add-source`, `crawl`, `list-sources`).
- **FILE-011**: `backend/app/api/routes/ingest.py` — modified; add web source + crawl routes and Pydantic models.
- **FILE-012**: `backend/tests/ingestion/web/__init__.py` — new; test package marker.
- **FILE-013**: `backend/tests/ingestion/web/test_fetcher.py` — new; fetcher tests (TASK-027 through TASK-033).
- **FILE-014**: `backend/tests/ingestion/web/test_registry.py` — new; registry tests (TASK-034 through TASK-038).

---

## 6. Testing

- **TEST-001**: `test_fetch_article_maps_fields` — field mapping from a `FakeArticle` (TASK-028).
- **TEST-002**: `test_fetch_article_skips_short_text` — text below `min_text_chars` → `None` (TASK-029).
- **TEST-003**: `test_fetch_article_handles_network_error` — factory raises → `None`, no exception (TASK-030).
- **TEST-004**: `test_resolve_external_id_short_and_long` — passthrough vs. hash at the 256-char boundary (TASK-031).
- **TEST-005**: `test_discover_urls_respects_limit` — mock builder, `limit` honored (TASK-032).
- **TEST-006**: `test_crawl_source_skips_seen_and_dedupes` — `seen_urls` filter + content-hash dedup (TASK-033).
- **TEST-007**: `test_register_web_source_creates_row` — `type="website"`, `parser_key`, `url` set (TASK-034).
- **TEST-008**: `test_register_web_source_rejects_bad_scheme` — `file://` → `ValueError` (TASK-035).
- **TEST-009**: `test_register_web_source_updates_url` — re-register updates URL (TASK-036).
- **TEST-010**: `test_crawl_registered_source_end_to_end` — 2 inserted, idempotent re-run inserts 0 (TASK-037).
- **TEST-011**: `test_crawl_unknown_source_raises` — unknown name → `ValueError` (TASK-038).

---

## 7. Risks & Assumptions

- **RISK-001**: newspaper3k maintenance status. The library is lightly maintained and can break on modern site structures. Mitigate: wrap all calls in try/except (GUD-001); ALT-001 provides `newspaper4k` as a drop-in fallback with the same API.
- **RISK-002**: Anti-scraping blocks (403, captcha, rate limits). Many news sites block scrapers. Mitigate: configurable browser UA (REQ-012), inter-request delay (CON-002), and graceful per-article failure. Target RSS-friendly finance sites first.
- **RISK-003**: Legal/ToS compliance. Scraping some sites violates their Terms of Service. Mitigate: this is an operator responsibility; the plan targets sites that permit non-commercial research use. Document a caution in the README. Consider honoring `robots.txt` in a future enhancement.
- **RISK-004**: `publish_date` frequently `None`. newspaper3k cannot always extract a publish date. Impact: `published_at` may be `None`, which downstream forward-return alignment (Phase 5) requires. Mitigate: articles with `published_at is None` are still ingested but must be filtered out of the aligned dataset; log a count of dateless articles per crawl. (Consider defaulting to crawl date as a last resort — deferred decision.)
- **RISK-005**: Duplicate content across crawls. The same article may be re-discovered. Mitigate: incremental `seen_urls` filter (REQ-009) plus `UniqueConstraint(source_id, external_id)` and hash dedup in `upsert_articles`.
- **RISK-006**: Transitive dependency weight. newspaper3k installs `Pillow`, `feedparser`, `tldextract`, etc. Mitigate: acceptable for a local research tool; none require CUDA or large model downloads. `fetch_images=False` avoids Pillow work at runtime.
- **RISK-007**: Synchronous crawl latency in the API. A 25-article crawl with a 1s delay + 20s timeout could take 30–60s, exceeding typical HTTP client patience. Mitigate: keep default `limit` modest; document that large crawls should use the CLI; async jobs deferred to Phase 9 (ALT-006).
- **ASSUMPTION-001**: Operators register only news-site base URLs they are authorized to crawl, using `http`/`https` schemes (enforced by SEC-001).
- **ASSUMPTION-002**: The canonical URL (`article.canonical_link`) is a stable unique identifier per article; where absent, the input URL is used.
- **ASSUMPTION-003**: Full article `text` extracted by newspaper3k is sufficient input for LLM scoring without further cleaning (unlike the Snacks mbox, which needed heavy boilerplate removal).
- **ASSUMPTION-004**: The Alembic baseline is at `head` before TASK-006; the autogenerate diff will contain only the new `url` column.

---

## 8. Related Specifications / Further Reading

- [TODO.md — Phase 2 Local File Ingestion](../TODO.md#phase-2--local-file-ingestion--complete-2026-07-02) — the pluggable parser framework and `normalize.py` persistence helpers reused here.
- [TODO.md — Phase 12 Stretch / Future](../TODO.md#phase-12--stretch--future) — lists "Direct/automated ingestion (RSS feeds, more newsletters)"; this plan is a concrete implementation of that goal.
- [plan/feature-webz-news-layer-1.md](feature-webz-news-layer-1.md) — companion plan adding the Webz.io dataset layer; the `source_name` scoring filter it introduces applies to web sources too.
- [app/ingestion/loader.py](../backend/app/ingestion/loader.py) — the file-based orchestrator this web layer runs parallel to.
- [app/ingestion/normalize.py](../backend/app/ingestion/normalize.py) — `ensure_source` / `upsert_articles` reused for persistence.
- [newspaper3k repository](https://github.com/codelucas/newspaper) — the extraction library.
- [newspaper3k documentation](https://newspaper.readthedocs.io/) — full API guide.
- [newspaper4k fork](https://github.com/AndyTheFactory/newspaper4k) — maintained alternative (ALT-001).
