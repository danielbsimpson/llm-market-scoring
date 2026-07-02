# LLM Market Scoring — Build Plan

A local-first system that ingests financial newsletters/articles, uses swappable local LLMs
(served by Ollama / `llama-server.exe` via an OpenAI-compatible API) to score assets on a continuous
outlook scale, aligns those scores with future market returns (via `yfinance`), trains swappable
sklearn models on top, and surfaces everything in a React dashboard. The long-term goal is a
human-in-the-loop, self-correcting prompt-optimization loop.

> **Status (2026-07-02):** Phases 0–2 complete and verified. Backend + frontend boot; LLM health
> lists all local Ollama models; SQLite schema created via Alembic; 44 assets + default model seeded
> (`/health/db` confirms); Robinhood Snacks mbox parsed and persisted — 379 `Article` rows in the DB
> under `Source` id=1 (`parser_key=robinhood_snacks`). Pluggable ingestion framework in place
> (`parsers/base.py`, `loader.py`, `normalize.py`); `POST /ingest` API endpoint live; 26 tests pass.
>
> **Immediate next steps when resuming:**
> 1. Move on to Phase 3 (LLM scoring engine) — `snacks_v0.jsonl` is now superseded by the DB;
>    score directly from `Article` rows via `app.db.models.Article`.
> 2. Create starter prompts in `app/llm/prompts/*.md` (multi-asset scorer + a few sector-specific).
> 3. Implement `llm/scorer.py` to run (article × prompt × model) → structured `Score` rows.


---

## 0. Decisions Locked In (from planning Q&A)

| Area | Decision |
|------|----------|
| Backend | Python (FastAPI) |
| Frontend | React (separate folder, monorepo) |
| LLM serving | **OpenAI-compatible HTTP** against existing Ollama / `llama-server.exe` (abstracted; `llama-cpp-python` optional) |
| Article ingestion | **Local file export** — drop `.eml`/`.html`/`.txt`/`.md` into a watched folder (direct newsletter ingestion later) |
| Storage | SQLite (relational/metadata) + Parquet (bulk timeseries/features) |
| Score output | Structured JSON: `score ∈ [-1.0, +1.0]` + `confidence ∈ [0,1]` + `rationale` |
| Return windows | 1d, 1w (5d), 1m (~21d), 3m (~63d) |
| Self-correction | Human-in-the-loop (system proposes prompt edits, user approves) |
| Hardware target | NVIDIA GPU, **8 GB VRAM** → 7–8B models at Q4_K_M (CUDA offload) |
| Repo layout | Monorepo: `backend/` + `frontend/` |

### Initial Asset Universe
**ETFs / Funds:** VGT, SCHX, QQQ, PPA, SPYI, SPY, SCHD, LMT*, VT, XLF, SCHB, MSFT*, XRT,
SCHF, VCR, IFRA, COWZ, SPDW, XLE, SCHH, EUAD
(*LMT and MSFT are single stocks, not funds — kept in the universe.)

**Industries / Sectors (not fully covered by the funds above):** Semiconductors, Software,
Biotech & Pharma, Healthcare Providers, Banks, Insurance, Consumer Staples, Consumer Discretionary,
Retail, Automotive, Aerospace & Defense, Industrials, Materials, Homebuilders, Real Estate,
Utilities, Telecom, Media & Entertainment, Transportation, Agriculture, Metals & Mining,
Renewable Energy, Oil & Gas.
> Industries are scored by the LLM but, where no clean tradable proxy exists, are evaluated against a
> mapped proxy ETF (configurable). See Phase 6.

### Existing Local Assets (REUSE — do not re-download)
Verified on this machine 2026-06-23:
- **llama.cpp** (CUDA prebuilt) at `C:\llama.cpp` — `llama-server.exe`, `llama-cli.exe`, `ggml-cuda.dll`.
- **Ollama** with models already pulled (stored at `C:\Users\simps\.ollama\models` as GGUF blobs):
  - Chat: `llama3.1:8b`, `qwen2.5:7b`, `mistral:7b`, `gemma3:4b`, `llama3.2:3b`, `phi4-mini`
  - Embeddings: `nomic-embed-text`
- Ollama exposes an **OpenAI-compatible API** at `http://localhost:11434/v1`.

### LLM Serving Approach (revised to reuse the above)
- Default backend = OpenAI-compatible HTTP client pointed at **Ollama** (`http://localhost:11434/v1`).
- Alternative backend = `llama-server.exe` (also OpenAI-compatible) for the same GGUF blobs.
- `LLMEngine` interface abstracts the provider so `llama-cpp-python` (in-process) remains a drop-in
  option later — **no CUDA rebuild required now**.
- Model swapping = just change the model name in config / per-scorer (no file management).
- Recommended starters for 8 GB VRAM: `qwen2.5:7b` or `llama3.1:8b` (Q4); embeddings via `nomic-embed-text`.
- Keep `num_ctx` modest (~4096).

---

## Architecture Overview

```
                         ┌────────────────────────────────────────────┐
                         │                Frontend (React)             │
                         │  Dashboard · Prompt Editor · Experiments     │
                         └───────────────▲──────────────┬──────────────┘
                                         │ REST/WebSocket │
                         ┌───────────────┴──────────────▼──────────────┐
                         │              Backend (FastAPI)               │
                         │  Ingestion · LLM Scoring · Market · ML · API │
                         └──┬─────────┬──────────┬─────────┬───────────┘
        Local files ────────┘         │          │         └────────── yfinance
                                      │          │
                            Ollama / llama-server  sklearn
                                      │          │
                         ┌────────────▼──────────▼──────────┐
                         │   Storage: SQLite + Parquet        │
                         └───────────────────────────────────┘
```

### Core data flow
1. **Ingest** exported newsletter/article files (local folder) → normalized `articles` with publish timestamp.
2. **Score** each article through N LLM "scorers" (one per prompt/asset-config) → `scores`.
3. **Market** data pulled from yfinance → compute forward returns over each window.
4. **Align** scores at article time `t` against forward returns `t+window`.
5. **Train** sklearn models on (LLM scores + market features) → predictions + backtests.
6. **Evaluate** prompt/model performance → propose prompt edits (human approves).
7. **Visualize** all of the above in the dashboard.

---

## Repository Layout (target)

```
llm-market-scoring/
├── README.md
├── TODO.md
├── .gitignore
├── .env.example
├── docker-compose.yml            # optional, later
├── backend/
│   ├── pyproject.toml            # or requirements.txt
│   ├── app/
│   │   ├── main.py               # FastAPI entrypoint
│   │   ├── config.py             # settings (pydantic-settings)
│   │   ├── db/
│   │   │   ├── models.py         # SQLAlchemy ORM
│   │   │   ├── session.py
│   │   │   └── migrations/       # alembic
│   │   ├── ingestion/
│   │   │   ├── loader.py          # scan watched folder, read .eml/.html/.txt/.md files
│   │   │   ├── parsers/           # per-source/format parsers (The Snack first)
│   │   │   └── normalize.py       # → Article records
│   │   ├── llm/
│   │   │   ├── engine.py          # LLMEngine interface + OpenAI-compatible client (Ollama/llama-server)
│   │   │   ├── providers.py       # ollama / llama_server / (optional) llama_cpp backends
│   │   │   ├── scorer.py          # runs prompt+article → structured score
│   │   │   ├── schema.py          # pydantic score schema + JSON validation
│   │   │   └── prompts/           # MARKDOWN prompt files (editable)
│   │   ├── market/
│   │   │   ├── yfinance_client.py
│   │   │   ├── returns.py         # forward-return windows
│   │   │   └── universe.py        # asset & industry→proxy mapping
│   │   ├── ml/
│   │   │   ├── features.py        # build feature matrix
│   │   │   ├── models.py          # sklearn model registry (swappable)
│   │   │   ├── train.py
│   │   │   └── backtest.py
│   │   ├── feedback/
│   │   │   └── prompt_optimizer.py # proposes prompt edits (HITL)
│   │   ├── api/
│   │   │   └── routes/            # ingestion, scoring, market, ml, prompts, experiments
│   │   └── services/             # orchestration/pipelines
│   ├── data/                     # SQLite db + parquet (git-ignored)
│   └── tests/
└── frontend/
    ├── package.json
    ├── vite.config.ts
    └── src/
        ├── api/                  # typed client
        ├── pages/                # Dashboard, Prompts, Experiments, Assets, Ingestion
        ├── components/
        └── lib/
```

---

# Phased Task List

## Phase 0 — Project Scaffolding & Tooling  ✅ (done 2026-06-23)
- [x] Create monorepo structure (`backend/`, `frontend/`).
- [x] Add root `.gitignore` (Python, Node, `*.gguf`, `data/`, `.env`, OAuth tokens).
- [x] Add `.env.example` documenting all config keys.
- [x] Backend: init Python project (Python 3.11.9), `requirements.txt` + `requirements-dev.txt`; venv at `backend/.venv`.
- [x] Backend deps installed: `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `sqlalchemy`,
      `alembic`, `pandas`, `pyarrow`, `yfinance`, `scikit-learn`, `numpy`, `openai`,
      `google-api-python-client`, `google-auth-oauthlib`, `beautifulsoup4`, `lxml`, `httpx`,
      `python-multipart`. Dev: `pytest`, `ruff`, `black`, `mypy`. (No `llama-cpp-python` — reuse Ollama.)
- [x] LLM access: `openai` client via `LLMEngine` (`app/llm/engine.py`); **no `llama-cpp-python` CUDA
      build** — reuses the existing Ollama server (`http://localhost:11434/v1`).
- [x] Verify Ollama is reachable and list models (`/health/llm` lists all 7 local models).
- [x] Frontend: scaffolded React + Vite + TypeScript; typed `fetch` client (`src/api/client.ts`) +
      status dashboard (`src/App.tsx`); `npm run build` passes.
  - [ ] Add charting lib (Recharts/Plotly) and component lib (Mantine/shadcn) — deferred to Phase 8.
- [x] Add `config.py` with pydantic-settings (paths, db path, LLM/provider, Gmail, windows).
- [ ] Set up lint/format config + a `Makefile`/`tasks.json` for common commands (dev tools installed; config pending).
- [x] Write initial README with setup/run steps (reuse-Ollama, no model downloads).

## Phase 1 — Storage Layer  ✅ (done 2026-06-23)
- [x] Define SQLite schema (SQLAlchemy 2.0 ORM in `app/db/models.py`) — core tables:
  - [x] `sources` (id, name, type, parser_key, active, created_at).
  - [x] `articles` (id, source_id, external_id, title, url, published_at, ingested_at,
        raw_html_path, clean_text, hash) — unique(source_id, external_id); indexed published_at, hash.
  - [x] `assets` (id, symbol[unique], kind[stock|fund|industry], name, proxy_symbol, active).
  - [x] `prompts` (id, name, asset_scope, markdown_path, version, hash, active, created_at) — unique(name, version).
  - [x] `llm_models` (id, name[unique], provider, ref, params_json, active) — `ref` replaces gguf_path (Ollama).
  - [x] `scores` (id, article_id, asset_id, prompt_id, llm_model_id, score, confidence,
        rationale, raw_json, scored_at) — unique combo for skip-already-scored.
  - [x] `market_prices` (symbol, date, OHLC, adj_close, volume) — unique(symbol, date).
  - [x] `forward_returns` (symbol, date, window, fwd_return) — unique(symbol, date, window).
  - [x] `experiments` (id, name, config_json, created_at).
  - [x] `experiment_results` (id, experiment_id, metric, value, fold, created_at).
  - [x] `prompt_feedback` (id, prompt_id, proposed_markdown, rationale, status, created_at).
- [x] Implemented `app/db/session.py` (engine + SessionLocal + get_db + init_db; resolves relative
      SQLite path against backend root) and Alembic baseline migration (`render_as_batch` for SQLite).
- [x] Seeded `assets` (19 funds + 2 stocks + 23 industries w/ proxy ETFs) + default model via
      `app/db/seed.py` (idempotent). Added `/health/db` endpoint.
- [ ] Decide Parquet vs SQLite split for bulk timeseries — deferred to Phase 4 (tables exist for now).

## Phase 2 — Local File Ingestion  ✅ (complete 2026-07-02)
### Done (2026-06-23)
- [x] **Snacks mbox parser** (`app/ingestion/snacks.py`): parses the Gmail-export mbox
      (`backend/data/Robinhood_Snacks.txt`, ~59 MB / 380 messages) end-to-end.
  - [x] Extracts the `text/html` part (the export's `text/plain` parts are empty), decodes
        quoted-printable + UTF-8, decodes RFC-2047 subjects.
  - [x] **Accurate timestamps**: parses the `Date` header to a timezone-aware `published_at`
        (naive → UTC fallback).
  - [x] **Robust cleaning** (`_clean_lines`) verified across 2019/2021/2023/2025/2026 formats:
        HTML→text with paragraph breaks; strips sponsor `Presented by …` ad blocks, quiz prompts,
        era-specific legal/footers, market-moves index tables, photo credits, subscription CTAs and
        footnotes; keeps legit content (e.g. "Getty Images" the company in earnings calendars).
  - [x] De-dupes by `Message-ID` + content hash; writes `data/processed/snacks_v0.jsonl`
        (379 issues, 2019-03-25 → 2026-06-22, ~959 words avg; fields: `external_id, source, subject,
        sender, published_at, text, word_count, content_hash`).
  - [x] CLI: `python -m app.ingestion.snacks [--input <mbox>] [--output <jsonl>]`.

### Done (2026-07-02)
- [x] **Persist to DB**: `normalize.py` registers a `Source` row and upserts `Article` rows from the
      mbox file (dedupe by `external_id` + `content_hash`); 379 Snacks articles in DB, idempotent.
- [x] **Config**: added `INGEST_DIR` (default `backend/data/inbox/`) to `config.py` + `.env.example`;
      removed unused `GMAIL_*` active keys (kept as commented-out lines for Phase 12 reference).
- [x] **Pluggable framework**: `parsers/base.py` (`ParsedArticle` + `ParserBase`), `parsers/snacks.py`
      (wraps existing mbox logic), `parsers/generic.py` (`GenericTextParser`/`GenericHtmlParser`/
      `GenericEmailParser`), registry in `parsers/__init__.py`; `loader.py` orchestrator.
- [x] **CLI**: `python -m app.ingestion --source NAME --path PATH [--parser-key KEY]`;
      `--list-parsers` shows registered parsers; idempotent re-runs confirmed.
- [x] **API endpoint**: `POST /ingest` + `GET /ingest/parsers` in `app/api/routes/ingest.py`;
      path-traversal guard (must be within `data_dir`); mounted on `app.main`.
- [x] **Tests**: 26 tests passing (`tests/ingestion/`) — snacks parser, normalize upsert, loader
      orchestration, with synthetic mbox fixture and tmp_path directory tests.

### Deferred
- [ ] *(Phase 12)* Optional direct ingestion: Gmail API / IMAP / RSS / paste-text.


## Phase 3 — LLM Scoring Engine
- [ ] `llm/schema.py`: pydantic model for score output:
      `{ "asset": str, "score": float[-1,1], "confidence": float[0,1], "rationale": str }`
      (allow a list for multi-asset prompts). Strict JSON validation + repair/retry on malformed output.
- [ ] `llm/engine.py` + `llm/providers.py`: `LLMEngine` interface over an OpenAI-compatible client;
      default provider = Ollama (`http://localhost:11434/v1`), alt = `llama-server.exe`; expose
      `generate(prompt, **params)`; **hot-swap** models by name; use JSON mode / `format=json`
      (Ollama) or `response_format` to force valid JSON; keep `num_ctx≈4096` for 8 GB.
- [ ] **Prompt-as-markdown**: `llm/prompts/*.md` files with frontmatter
      (name, asset_scope, model hint) + body = system prompt. Editing the file changes behavior.
  - [ ] Create starter prompts: one generic per-article multi-asset scorer, plus a few
        asset/industry-specific prompts (e.g. `semiconductors.md`, `energy.md`, `defense.md`).
  - [ ] Template variables: `{article_text}`, `{asset_list}`, `{published_at}`, score-scale spec.
- [ ] `llm/scorer.py`: orchestrate — for each (article × prompt × model), build prompt, run engine,
      validate JSON, persist `scores`. Support running the **same article through multiple prompts/
      models** (the multi-LLM requirement).
- [ ] Batch runner + progress; skip already-scored (article, prompt, model) combos.
- [ ] Token/length guard: chunk long articles; aggregation strategy (mean/weighted) documented.
- [ ] Tests: mock engine to validate schema enforcement, retry, persistence.

## Phase 4 — Market Data (yfinance)
- [ ] `market/yfinance_client.py`: fetch daily OHLCV for all universe symbols + proxy symbols;
      cache to Parquet; incremental updates; rate-limit/backoff handling.
- [ ] `market/universe.py`: asset metadata + industry→proxy-ETF mapping (configurable).
- [ ] `market/returns.py`: compute forward returns for windows **1d, 1w (5 trading days),
      1m (~21d), 3m (~63d)** from a given article date; handle non-trading days (use next session).
- [ ] Optional baseline features: rolling volatility, prior-window return, relative-to-SPY return.
- [ ] Endpoint/CLI to refresh market data; document scheduling (manual now, cron later).
- [ ] Tests with cached fixture price data (no live network in CI).

## Phase 5 — Score ↔ Return Alignment
- [ ] Build the aligned dataset: join `scores` (at `published_at`) with `forward_returns`
      for the scored asset (or its proxy) across each window.
- [ ] Handle timing carefully: use the first market session **after** article timestamp as entry;
      avoid look-ahead bias.
- [ ] Persist aligned panels to Parquet for fast ML iteration.
- [ ] Quick diagnostics: correlation of score vs forward return per window/asset (sanity signal).

## Phase 6 — ML Layer (sklearn, swappable)
- [ ] `ml/features.py`: assemble feature matrix per (asset, date):
      LLM score(s) + confidence + market baseline features; configurable feature sets.
- [ ] `ml/models.py`: registry of swappable sklearn estimators
      (Ridge/Lasso, RandomForest, GradientBoosting, LogisticRegression for direction,
      SVR, etc.) with a common `fit/predict` interface and hyperparam configs.
- [ ] `ml/train.py`: train per target = forward return (regression) or direction (classification);
      **time-series cross-validation** (walk-forward, no shuffling); persist fitted models.
- [ ] `ml/backtest.py`: walk-forward backtest; metrics — IC/rank-IC, MAE/RMSE, directional accuracy,
      Sharpe of a simple long/short rule, hit rate by window.
- [ ] Experiment abstraction: an experiment = {prompt set, LLM model(s), sklearn model, features,
      windows, universe subset}; store config + results in DB.
- [ ] Make everything reproducible (seed, config hashing).
- [ ] Tests on synthetic data to validate no leakage and metric correctness.

## Phase 7 — Backend API (FastAPI)
- [ ] Routes:
  - [ ] `POST /ingest` — trigger local-file ingestion (params: source, dir).
  - [ ] `GET /articles` — list/filter/paginate; `GET /articles/{id}`.
  - [ ] `GET/POST/PUT /prompts` — list, read markdown, edit/save (versioned), activate.
  - [ ] `GET/POST /models` — list registered GGUF models, register/activate, swap.
  - [ ] `POST /score` — run scoring for article/date-range × prompt × model.
  - [ ] `GET /scores` — query scored results.
  - [ ] `POST /market/refresh`, `GET /market/...` — prices & returns.
  - [ ] `POST /experiments` — define & run; `GET /experiments`, `GET /experiments/{id}`.
  - [ ] `GET /feedback`, `POST /feedback/{id}/approve|reject` — HITL prompt edits.
- [ ] Long-running jobs: background tasks + status polling or WebSocket progress events.
- [ ] Pydantic request/response models; OpenAPI docs; CORS for the React dev server.
- [ ] Generate a typed API client for the frontend (openapi-typescript or manual).

## Phase 8 — Frontend Dashboard (React)
- [ ] App shell, routing, API client, global state (React Query for server state).
- [ ] **Dashboard page**: current scores by asset, score-vs-return charts, model performance
      summary, latest ingested articles.
- [ ] **Prompt Editor page**: list prompt markdown files, edit in-browser (Monaco), save new
      version, activate; show diff vs previous version.
- [ ] **Models page**: list/register GGUF models, set active per scorer, swap.
- [ ] **Experiments page**: pick prompt set + LLM model(s) + sklearn model + features + windows +
      universe subset → run → view metrics, leaderboards, backtest charts; compare experiments.
- [ ] **Assets page**: manage universe + industry→proxy mapping.
- [ ] **Ingestion page**: trigger fetch, see ingestion history, inspect parsed article text.
- [ ] **Feedback page**: review proposed prompt edits, approve/reject (HITL loop UI).
- [ ] Charts: time series, correlation/IC heatmaps, backtest equity curves.

## Phase 9 — Orchestration / Pipelines
- [ ] `services/` pipeline that chains: ingest → score → market refresh → align → (optional) train.
- [ ] CLI entrypoints for each stage + a "run all" command.
- [ ] Idempotency + incremental processing across the whole pipeline.
- [ ] Lightweight scheduling doc (Windows Task Scheduler / manual) — automation later.

## Phase 10 — Human-in-the-Loop Self-Correction
- [ ] `feedback/prompt_optimizer.py`: analyze per-prompt performance (IC/accuracy by asset/window),
      identify weak prompts, and **propose** edited markdown (could use the local LLM to suggest
      rewrites) with a rationale.
- [ ] Store proposals in `prompt_feedback` (status = proposed).
- [ ] Surface in Feedback page; on **approve**, write a new prompt version and re-score going forward.
- [ ] Track prompt-version performance over time to confirm improvements (guard against regressions).
- [ ] Keep a full audit trail (who/when/why a prompt changed).

## Phase 11 — Testing, Quality, Docs
- [ ] Unit tests per module; integration test for the full pipeline on fixtures.
- [ ] Fixtures: sample emails, cached prices, a tiny stub LLM for deterministic tests.
- [ ] CI (GitHub Actions): lint (ruff/black), type-check (mypy), pytest (CPU-only, no GGUF).
- [ ] Expand README: architecture, setup, adding a source, adding a prompt, adding a model,
      running experiments, interpreting metrics.
- [ ] Document data/privacy: exported files and all data stay local; nothing leaves the machine.

## Phase 12 — Stretch / Future
- [ ] Direct/automated ingestion (longer-term): Gmail API / IMAP, RSS feeds, paste-text, more
      newsletters — reducing the manual export step.
- [ ] Expand universe to arbitrary tickers/industries; auto proxy discovery.
- [ ] Confidence-weighted ensembling across multiple LLMs/prompts.
- [ ] Richer features (macro data, options/IV, cross-asset signals).
- [ ] Model registry versioning + experiment tracking (MLflow-style, local).
- [ ] Optional Dockerization; optional `llama-cpp-python` in-process backend if ever needed.
- [ ] Optional: load Ollama GGUF blobs directly via `llama-server.exe` for fine-grained control.
- [ ] Toward fuller autonomy in the self-correction loop (still gated by guardrails).

---

## Open Items / To Revisit
- [x] Pick default scorer model from existing Ollama models — **`qwen2.5:7b`** (set in `backend/.env`).
- [ ] Finalize industry→proxy-ETF mapping table for industries without direct funds.
- [ ] Decide chunking/aggregation policy for long newsletters.
- [ ] Decide primary ML target first: forward return (regression) vs direction (classification).

## Suggested Build Order (milestones)
1. Phase 0–1: scaffold + storage.
2. Phase 2: ingest The Snack end-to-end.
3. Phase 3: score articles with one prompt + one model, JSON-validated.
4. Phase 4–5: market data + aligned dataset.
5. Phase 6: first sklearn baseline + backtest.
6. Phase 7–8: API + dashboard (read-only first, then editing).
7. Phase 9: pipeline glue.
8. Phase 10: HITL self-correction.
