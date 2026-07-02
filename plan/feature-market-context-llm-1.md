---
goal: Inject Per-Asset Recent Market Data into LLM Scoring Prompts
version: 1.0
date_created: 2026-07-02
last_updated: 2026-07-02
owner: llm-market-scoring
status: 'Planned'
tags: [feature, llm, market-data, scoring, context-enrichment, phase4-adjacent]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan adds a **market context injection layer** to the LLM scoring pipeline. Before each LLM
call, the scorer fetches the most recent price history for the assets being evaluated — using the
article's `published_at` as the cutoff so no look-ahead bias is introduced — and appends a compact
performance table to the prompt.

**Problem being solved:** Without recent price context, the LLM scores a headline like
*"Semiconductor stocks rally on AI chip demand"* without knowing whether SOXX already surged 8%
in the week before the article, or is flat. Supplying that context lets the model calibrate whether
catalysts are already priced in and produce better-calibrated `score` and `confidence` values.

**Scope:** The feature is **per-asset-batch** (not per-article). Each LLM call already groups
assets by kind (fund / stock / industry). The market context block is built once per kind group
and injected as a `$market_context` Template variable — fully backwards-compatible with prompt
files that do not yet reference the variable (they will silently ignore it via
`Template.safe_substitute`).

**Data source:** `yfinance` (adjusted close prices) with a per-symbol Parquet cache under
`data/market_cache/`. No new runtime dependencies are required. This module operates independently
of Phase 4's `market_prices` DB table but is designed to be swappable with it once Phase 4 is live.

---

## 1. Requirements & Constraints

- **REQ-001**: For each LLM scoring call, compute and inject window returns (default: 1d, 5d, 21d) for every asset in the batch, using the last trading day **on or before** the article's `published_at` date as the reference point.
- **REQ-002**: For `industry` assets (e.g. `Semiconductors`), use the asset's `proxy_symbol` (e.g. `SOXX`) as the ticker for price lookup. For `fund` and `stock` assets, use the asset's own `symbol`.
- **REQ-003**: Price data must be fetched via `yfinance.download(symbol, start=start_date, end=end_date, auto_adjust=True)` with `adj_close` as the price column.
- **REQ-004**: Each symbol's price history must be cached as a Parquet file at `{market_cache_dir}/{SYMBOL}.parquet` with columns `["date", "adj_close"]`. The cache is written on first fetch and updated when the most recent cached date is earlier than `article_date - 1 business day`.
- **REQ-005**: If a symbol's data cannot be fetched (network error, unknown ticker, empty response), its row in the context table must display `"N/A"` for all windows. The scorer must never raise an exception or skip a batch due to missing market data.
- **REQ-006**: The formatted context string must be passed to `Template.safe_substitute` as keyword argument `market_context=...`. Prompt templates that omit `$market_context` are unaffected.
- **REQ-007**: The context string must include: a header line stating the reference date, a table with one row per asset showing `symbol`, `name/proxy`, and one column per window. Industries must show both the industry name and the proxy ticker used.
- **REQ-008**: The entire market context block (all assets in one kind group) must not exceed **800 characters** to protect the 4096-token context budget. If the formatted table exceeds this limit, truncate to the first N rows that fit.
- **REQ-009**: Configurable settings must be added to `app/config.py`: `market_cache_dir: Path` (default `BACKEND_ROOT / "data" / "market_cache"`) and `market_context_windows: str` (default `"1,5,21"`; parsed as a list of ints).
- **REQ-010**: All four existing prompt files (`multi_asset.md`, `semiconductors.md`, `energy.md`, `defense.md`) must be updated to include the `$market_context` variable in a clearly labelled section.
- **CON-001**: No new Python runtime packages. Only `yfinance`, `pandas`, `pyarrow` (all already in `requirements.txt`).
- **CON-002**: `yfinance.download` calls must be batched where possible (one call for multiple symbols) to reduce network round-trips and respect rate limits. Use `yfinance.download(tickers=" ".join(symbols), ...)` with `group_by="ticker"`.
- **CON-003**: The 4096-token context budget is shared with the system prompt (~400 tokens), article text (up to 12,000 chars ≈ 3,000 tokens), and asset list (~200 tokens). The market context block must fit within the remaining ~500 tokens; REQ-008's 800-character cap is derived from this budget.
- **CON-004**: The article's `published_at` is stored as naive UTC in the DB. The last trading day lookup must use a US market calendar (NYSE). Use `pandas.bdate_range` as a dependency-free approximation; do NOT use `pandas_market_calendars` (new dependency).
- **CON-005**: `Scorer._call_llm()` signature change must be backwards-compatible. Add `market_context: str = ""` as a keyword argument — existing callers passing positional args are unaffected.
- **GUD-001**: Cache files should be written atomically (write to a `.tmp` file then rename) to prevent corrupt Parquet files if the process is killed mid-write.
- **GUD-002**: Log a `DEBUG` message when a cache hit is used and a `INFO` message when a network fetch occurs, including symbol and date range.
- **GUD-003**: The context table should use a fixed-width text format (aligned columns) so the LLM can parse it easily without relying on markdown tables which some models render inconsistently.
- **PAT-001**: Follow the existing pattern in `app/market/` (once created): pure functions at module level, `from __future__ import annotations`, typed with stdlib types.
- **PAT-002**: The `Scorer` class must remain testable with a mock engine and no yfinance calls. Market context injection must be injectable as a callable parameter `context_builder` on `Scorer.__init__` (default `build_market_context`; tests pass a stub that returns a fixed string).

---

## 2. Implementation Steps

### Implementation Phase 1 — Market Context Core Module

- GOAL-001: Create `app/market/context.py` with all price fetching, caching, window-return computation, and context-string formatting logic.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `backend/app/market/__init__.py` (empty package marker). | | |
| TASK-002 | Add `market_cache_dir: Path = BACKEND_ROOT / "data" / "market_cache"` and `market_context_windows: str = "1,5,21"` to `Settings` in `backend/app/config.py`. Add a `@property market_context_window_list(self) -> list[int]` that parses `market_context_windows`. Add `settings.market_cache_dir.mkdir(parents=True, exist_ok=True)` at the bottom of `config.py` alongside the existing directory creation calls. | | |
| TASK-003 | Create `backend/app/market/context.py`. Add module-level imports: `from __future__ import annotations`, `import logging`, `import pandas as pd`, `import numpy as np`, `import yfinance as yf`, `from datetime import date, timedelta`, `from pathlib import Path`, `from app.db.models import Asset, AssetKind`. Define `log = logging.getLogger(__name__)`. | | |
| TASK-004 | Implement `_last_business_day_on_or_before(d: date) -> date` in `context.py`. Uses `pd.bdate_range(end=d, periods=1)[0].date()` to find the most recent business day ≤ `d`. This is the "as-of date" used for all price lookups for a given article. | | |
| TASK-005 | Implement `_cache_path(symbol: str, cache_dir: Path) -> Path` in `context.py`. Returns `cache_dir / f"{symbol.upper()}.parquet"`. | | |
| TASK-006 | Implement `_load_cache(symbol: str, cache_dir: Path) -> pd.DataFrame \| None` in `context.py`. Reads `_cache_path(symbol, cache_dir)` if it exists, returns a DataFrame with columns `["date", "adj_close"]` indexed by `date` (as `datetime.date`). Returns `None` if file does not exist or read fails. | | |
| TASK-007 | Implement `_save_cache(df: pd.DataFrame, symbol: str, cache_dir: Path) -> None` in `context.py`. Writes atomically: write to `_cache_path(...).with_suffix(".tmp")`, then rename to final path. `df` must have columns `["date", "adj_close"]`. | | |
| TASK-008 | Implement `fetch_prices(symbols: list[str], as_of_date: date, cache_dir: Path, lookback_days: int = 60) -> dict[str, pd.DataFrame]` in `context.py`. For each symbol: (a) load cache; (b) if cache is `None` or cache's max date < `as_of_date - timedelta(days=1)`: call `yf.download(tickers=" ".join(symbols_to_fetch), start=(as_of_date - timedelta(days=lookback_days + 30)).isoformat(), end=(as_of_date + timedelta(days=1)).isoformat(), auto_adjust=True, progress=False, group_by="ticker")`; parse the response into per-symbol DataFrames with `["date", "adj_close"]` columns; call `_save_cache` for each; (c) return dict mapping symbol → DataFrame. On any exception: log a WARNING and return an empty dict for affected symbols. Batch all cache-missing symbols into a single `yf.download` call. | | |
| TASK-009 | Implement `compute_window_returns(prices: pd.DataFrame, as_of_date: date, windows: list[int]) -> dict[int, float \| None]` in `context.py`. For each window `w`: find the row at `as_of_date` (the reference price `p_now`) and the row at the trading day closest to `as_of_date - w business days` (using `pd.bdate_range`). Return `(p_now - p_then) / p_then` as a float, or `None` if either date is missing from the DataFrame. | | |
| TASK-010 | Implement `resolve_price_symbol(asset: Asset) -> str` in `context.py`. Returns `asset.proxy_symbol` if `asset.kind == AssetKind.industry` and `asset.proxy_symbol` is not `None`; otherwise returns `asset.symbol`. | | |
| TASK-011 | Implement `_format_return(value: float \| None) -> str` in `context.py`. Returns `f"{value:+.1%}"` (e.g. `"+2.4%"`, `"-0.8%"`) if value is not `None`; returns `"  N/A "` otherwise. Pads to 7 characters for column alignment. | | |
| TASK-012 | Implement `build_market_context(assets: list[Asset], article_date: date, cache_dir: Path, windows: list[int]) -> str` in `context.py`. Steps: (a) call `_last_business_day_on_or_before(article_date)` to get `as_of`; (b) call `resolve_price_symbol` for each asset to collect the list of unique ticker symbols to fetch; (c) call `fetch_prices` with the collected symbols; (d) build a fixed-width text table: header row `"Asset" | "Ticker" | returns per window`, one row per asset; (e) prepend a one-line header: `f"Recent price performance as of {as_of} (last trading day before article):"` and append a one-line footer: `"(Consider whether catalysts described in the article may already be priced in.)"` ; (f) if the total string length exceeds 800 characters, truncate the table rows (keeping header + footer) and append `"... ({n} more assets omitted)"`. Returns the complete context string. Returns the empty string `""` if `assets` is empty. | | |
| TASK-013 | Implement `build_market_context_disabled() -> str` that returns `""`. Used as the `context_builder` stub in tests that do not need market data. | | |

### Implementation Phase 2 — Scorer Integration

- GOAL-002: Thread `build_market_context` through `Scorer` so each LLM call receives the pre-built context string as the `$market_context` Template variable.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Modify `Scorer.__init__` in `backend/app/llm/scorer.py`. Add parameter `context_builder: Callable[[list[Asset], date, Path, list[int]], str] \| None = None`. Store as `self._context_builder = context_builder or build_market_context`. Add import: `from app.market.context import build_market_context` and `from datetime import date as date_type`. | | |
| TASK-015 | Modify `Scorer.score_article` in `backend/app/llm/scorer.py`. Before the per-kind loop, add: `article_date = article.published_at.date() if article.published_at else date_type.today()`. Inside the loop (before `_call_llm`), add: `market_ctx = self._context_builder(group_assets, article_date, settings.market_cache_dir, settings.market_context_window_list)`. | | |
| TASK-016 | Modify `Scorer._call_llm` signature in `backend/app/llm/scorer.py`. Add parameter `market_context: str = ""`. Update the `Template.safe_substitute` call to include `market_context=market_context`. Update the single call-site in `score_article` to pass `market_context=market_ctx`. | | |

### Implementation Phase 3 — Prompt Template Updates

- GOAL-003: Update all four prompt `.md` files to include the `$market_context` variable in the correct position — between the article text and the asset list — with a clear label. The variable renders to an empty string when data is unavailable, so no behavioural change occurs if the module is disabled.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-017 | Update `backend/app/llm/prompts/multi_asset.md`. After the `ARTICLE:` section and before `ASSETS TO SCORE:`, insert exactly the following block (no indentation changes to surrounding text): `\nMARKET CONTEXT:\n$market_context\n`. | | |
| TASK-018 | Update `backend/app/llm/prompts/semiconductors.md`. Apply the same `MARKET CONTEXT:\n$market_context\n` block insertion as TASK-017. | | |
| TASK-019 | Update `backend/app/llm/prompts/energy.md`. Apply the same `MARKET CONTEXT:\n$market_context\n` block insertion as TASK-017. | | |
| TASK-020 | Update `backend/app/llm/prompts/defense.md`. Apply the same `MARKET CONTEXT:\n$market_context\n` block insertion as TASK-017. | | |

### Implementation Phase 4 — Tests

- GOAL-004: Full test coverage for `app/market/context.py` and the scorer integration using synthetic price data — no live yfinance or network calls in the test suite.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-021 | Create `backend/tests/market/__init__.py` (empty). | | |
| TASK-022 | Create `backend/tests/market/test_context.py`. Define helper `_make_price_df(symbol, dates, prices)` that returns a DataFrame with columns `["date", "adj_close"]`. | | |
| TASK-023 | Test `_last_business_day_on_or_before`: assert `date(2024, 1, 13)` (Saturday) returns `date(2024, 1, 12)` (Friday); assert `date(2024, 1, 12)` (Friday) returns itself. | | |
| TASK-024 | Test `compute_window_returns`: build a 30-row synthetic price DataFrame with known values; assert 1d return matches `(p[-1] - p[-2]) / p[-2]`; assert returns `None` for a window larger than the DataFrame. | | |
| TASK-025 | Test `_format_return`: assert `_format_return(0.0241)` returns `"+2.4%"`; assert `_format_return(-0.008)` returns `"-0.8%"`; assert `_format_return(None)` returns `"  N/A "`. | | |
| TASK-026 | Test `_load_cache` and `_save_cache` round-trip: write a synthetic DataFrame to a tmp_path, read it back, assert equality. | | |
| TASK-027 | Test `_save_cache` atomicity: verify the `.tmp` intermediate file is not present after a successful write. | | |
| TASK-028 | Test `build_market_context` with a mock `fetch_prices` (monkeypatched). Provide two fund assets (SPY, QQQ) with synthetic DataFrames. Assert the output string: (a) contains `"Recent price performance"`, (b) contains `"SPY"` and `"QQQ"`, (c) contains the formatted 1d/5d/21d return columns, (d) contains `"already be priced in"`. | | |
| TASK-029 | Test `build_market_context` for an industry asset: use a `Semiconductors` asset with `proxy_symbol="SOXX"`. Assert the output string contains both `"Semiconductors"` and `"SOXX"`. | | |
| TASK-030 | Test `build_market_context` fallback when `fetch_prices` returns empty dict for a symbol: assert the row shows `"N/A"` for all return columns. | | |
| TASK-031 | Test `build_market_context` truncation: provide a 30-asset list; verify output length ≤ 800 characters and the truncation message `"... ("` is present. | | |
| TASK-032 | Test `build_market_context` returns `""` when called with an empty asset list. | | |
| TASK-033 | Test `fetch_prices` cache-hit path (monkeypatch `yf.download` to raise `AssertionError` if called): pre-populate the cache for a symbol with today's date; verify `yf.download` is NOT called. | | |
| TASK-034 | Test scorer integration: create a `Scorer` with a mock `LLMEngine` and `context_builder=lambda *a, **kw: "MOCKED CONTEXT"`. Verify the string `"MOCKED CONTEXT"` appears in the prompt passed to `engine.generate`. Access via `eng.generate.call_args[0][0][0].content`. | | |
| TASK-035 | Test scorer backwards-compatibility: create a `Scorer` with `context_builder=build_market_context_disabled`. Confirm `engine.generate` is called and returns valid scores (the empty context string does not break Template substitution). | | |

---

## 3. Alternatives

- **ALT-001**: Use the `market_prices` DB table (Phase 4) as the data source instead of a Parquet cache. Cleaner architecture; no separate cache directory. Rejected for this plan because Phase 4 is not yet implemented and blocking this feature on it would delay scoring quality improvements for all 379 articles.
- **ALT-002**: Fetch prices inline, without a cache, on every scoring call. Rejected: 379 articles × 3 kind-groups × 19–23 assets per group would require thousands of yfinance API calls per full batch run, risking rate-limiting and taking several minutes of network I/O.
- **ALT-003**: Inject a single SPY/market-level context block for all assets instead of per-asset returns. Simpler but lower quality — the LLM cannot distinguish between a sector-specific move (e.g. SOXX +8% while SPY was flat) and a broad market move.
- **ALT-004**: Use `pandas_market_calendars` for accurate NYSE holiday handling. More precise than `pd.bdate_range` (which approximates without holiday knowledge). Rejected because it adds a new runtime dependency; `pd.bdate_range` is acceptable for a 1-day approximation error that rarely falls on a US holiday.
- **ALT-005**: Place the `build_market_context` function inside `app/llm/scorer.py` rather than a separate `app/market/` module. Rejected: keeps concerns separate; the `app/market/` module will grow in Phase 4 and beyond, and the fetching/caching logic is independent of the LLM pipeline.

---

## 4. Dependencies

- **DEP-001**: `yfinance >= 0.2.40` — price history download (already in `requirements.txt`).
- **DEP-002**: `pandas >= 2.2` — DataFrame operations, `bdate_range`, Parquet I/O (already in `requirements.txt`).
- **DEP-003**: `pyarrow >= 16.0` — Parquet engine for `DataFrame.to_parquet` / `read_parquet` (already in `requirements.txt`).
- **DEP-004**: `app/db/models.py` — `Asset`, `AssetKind` ORM classes imported by `context.py` for `resolve_price_symbol`.
- **DEP-005**: `app/config.py` — `settings.market_cache_dir` and `settings.market_context_window_list` must be added before this module can be imported.
- **DEP-006**: `app/llm/scorer.py` — `Scorer._call_llm` and `Scorer.score_article` are modified in Phase 2. These changes are backwards-compatible.
- **DEP-007**: Internet access or a pre-populated cache is required for the first scoring run. Subsequent runs on the same date range are served from cache.

---

## 5. Files

- **FILE-001**: `backend/app/market/__init__.py` — new; empty package marker.
- **FILE-002**: `backend/app/market/context.py` — new; contains `build_market_context`, `fetch_prices`, `compute_window_returns`, `resolve_price_symbol`, `_last_business_day_on_or_before`, `_format_return`, `_load_cache`, `_save_cache`, `_cache_path`, `build_market_context_disabled`.
- **FILE-003**: `backend/app/config.py` — modified; add `market_cache_dir: Path` and `market_context_windows: str` settings fields and `market_cache_dir.mkdir` call.
- **FILE-004**: `backend/app/llm/scorer.py` — modified; add `context_builder` param to `Scorer.__init__`; add `market_ctx` pre-fetch in `score_article`; add `market_context` param to `_call_llm` and pass to `safe_substitute`.
- **FILE-005**: `backend/app/llm/prompts/multi_asset.md` — modified; insert `MARKET CONTEXT:\n$market_context\n` block.
- **FILE-006**: `backend/app/llm/prompts/semiconductors.md` — modified; insert `MARKET CONTEXT:\n$market_context\n` block.
- **FILE-007**: `backend/app/llm/prompts/energy.md` — modified; insert `MARKET CONTEXT:\n$market_context\n` block.
- **FILE-008**: `backend/app/llm/prompts/defense.md` — modified; insert `MARKET CONTEXT:\n$market_context\n` block.
- **FILE-009**: `backend/tests/market/__init__.py` — new; empty test package marker.
- **FILE-010**: `backend/tests/market/test_context.py` — new; 13 unit tests (TASK-022 through TASK-035).

---

## 6. Testing

- **TEST-001**: `test_last_business_day_saturday` — Saturday maps to preceding Friday (TASK-023).
- **TEST-002**: `test_last_business_day_friday` — Friday maps to itself (TASK-023).
- **TEST-003**: `test_compute_window_returns_values` — 1d return formula verified against synthetic data (TASK-024).
- **TEST-004**: `test_compute_window_returns_none_for_missing_window` — window > data length returns `None` (TASK-024).
- **TEST-005**: `test_format_return_positive` — positive float formatted with `+` sign and 1 decimal (TASK-025).
- **TEST-006**: `test_format_return_negative` — negative float formatted correctly (TASK-025).
- **TEST-007**: `test_format_return_none` — `None` returns padded `"  N/A "` (TASK-025).
- **TEST-008**: `test_cache_roundtrip` — save + load produces identical DataFrame (TASK-026).
- **TEST-009**: `test_cache_atomic_write` — no `.tmp` file remains after successful write (TASK-027).
- **TEST-010**: `test_build_context_fund_assets` — output contains asset symbols, returns, and footer (TASK-028).
- **TEST-011**: `test_build_context_industry_shows_proxy` — proxy ticker appears alongside industry name (TASK-029).
- **TEST-012**: `test_build_context_fallback_na` — unavailable symbol shows N/A (TASK-030).
- **TEST-013**: `test_build_context_truncated` — 30-asset list truncated to ≤ 800 chars with message (TASK-031).
- **TEST-014**: `test_build_context_empty_assets` — empty list returns `""` (TASK-032).
- **TEST-015**: `test_fetch_prices_uses_cache` — no yfinance call when cache is fresh (TASK-033).
- **TEST-016**: `test_scorer_receives_market_context` — mock context string appears in LLM prompt (TASK-034).
- **TEST-017**: `test_scorer_empty_context_backward_compat` — disabled context does not break scorer (TASK-035).

---

## 7. Risks & Assumptions

- **RISK-001**: yfinance API stability. yfinance has historically experienced breaking changes from Yahoo Finance API updates. Mitigate by pinning `yfinance >= 0.2.40` and catching all exceptions in `fetch_prices` (REQ-005 fallback). If yfinance is unavailable, scoring continues with empty context.
- **RISK-002**: Cache staleness for historical articles. Articles from 2019 require 2018 price data. The first batch run must fetch ~7 years of history per symbol. yfinance handles this but the initial download may take 30–60 seconds per symbol group. Subsequent runs are instant (cache hit). Log clearly when a full historical fetch is underway.
- **RISK-003**: Context window inflation. The 800-character cap (REQ-008) protects the 4096-token budget, but may need tuning. If a 7B model consistently produces poor scores when the combined prompt exceeds ~3,500 tokens, reduce `market_context_windows` to `"1,5"` (two windows instead of three) via config.
- **RISK-004**: Prompt template hash invalidation. Editing the 4 `.md` files in Phase 3 will change their SHA-256 hash, causing `PromptLoader` to deactivate the existing `Prompt` DB row and create a new version. This means prior `Score` rows for old prompt versions will not be re-scored automatically. This is the correct behavior (prompt versioning), but operators should be aware that running `score_batch` after this change will re-score all articles under the new prompt version.
- **RISK-005**: `pd.bdate_range` does not account for US market holidays (e.g. Thanksgiving, Christmas). A Friday returned by `_last_business_day_on_or_before` may be a market holiday. The impact is minimal: the price lookup will use the most recent available date before the holiday from the DataFrame. This is acceptable for a ±1 day precision.
- **ASSUMPTION-001**: Articles' `published_at` timestamps are in UTC (stored as naive UTC in the DB per Phase 2 design). The `.date()` call will return the UTC calendar date. For pre-market articles (e.g. published at 07:00 UTC = 02:00 EST), the reference date is the UTC date, not the local US trading-day date. This is a minor approximation.
- **ASSUMPTION-002**: All universe symbols (fund and stock) and all proxy ETF symbols for industries are fetchable from yfinance. Symbols like `EUAD` (European market ETF) and `CARZ` (automotive ETF) should be available but may have limited history pre-2020.
- **ASSUMPTION-003**: The `context_builder` callable passed to `Scorer.__init__` has the signature `(assets: list[Asset], article_date: date, cache_dir: Path, windows: list[int]) -> str`. Tests and production code must adhere to this signature.

---

## 8. Related Specifications / Further Reading

- [TODO.md — Phase 3 LLM Scoring Engine](../TODO.md#phase-3--llm-scoring-engine--complete-2026-07-02) — defines the `Scorer` class and `_call_llm` method being modified in Phase 2 of this plan.
- [TODO.md — Phase 4 Market Data](../TODO.md#phase-4--market-data-yfinance) — the future `app/market/yfinance_client.py` that will supersede the Parquet cache used here; `fetch_prices` in this plan is designed to be replaced with a DB query when Phase 4 is live.
- [plan/feature-importance-ml-1.md](feature-importance-ml-1.md) — related plan for feature importance in the ML layer; the `window_returns` computed here become ML features there.
- [app/llm/scorer.py](../backend/app/llm/scorer.py) — `Scorer._call_llm` and `score_article` methods modified by this plan.
- [app/llm/prompts/](../backend/app/llm/prompts/) — the four prompt files updated in Phase 3 of this plan.
- [yfinance documentation](https://ranaroussi.github.io/yfinance/) — `yf.download` parameters and multi-ticker batching behavior.
