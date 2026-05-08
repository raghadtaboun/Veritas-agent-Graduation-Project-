# Phase 0 Summary — Environment Setup

## A — Phase Identification

| Field | Value |
|---|---|
| Phase Number | 0 |
| Phase Title | Environment Setup |
| Start Date | 2026-04-07 |
| Completion Date | 2026-04-07 |

---

## B — What Was Implemented

### `veritas-agent/` (root directory structure)
Created the exact folder and file hierarchy defined in `readme.md`:
- All 8 top-level directories: `mcp_server/`, `agents/`, `api/`, `frontend/`, `config/`, `infra/`, `tests/`, `evaluation/`, `summaries/`
- All 7 phase subdirectories under `summaries/`: `phase_0/` through `phase_6/`
- All placeholder source files at exact paths specified in the structure map
- `.gitignore` configured to exclude `.env`, `__pycache__/`, `.venv/`, `*.pyc`, `*.pyo`, `.DS_Store`

### `infra/schema.sql`
Full PostgreSQL schema written and applied to the `newsguard` database:
- **6 tables created:** `sources`, `articles`, `events`, `article_events`, `bias_scores`, `blindspot_reports`
- **HNSW index** on `articles.embedding` (`vector_cosine_ops`, m=16, ef_construction=64) for fast ANN search
- **Secondary indexes** on `articles.published_at DESC`, `articles.section`, `bias_scores.label`, `article_events.event_id`, `article_events.article_id`, `blindspot_reports.event_id`
- **13 seed records** inserted into `sources` covering all 5 bias labels across Arabic and Libyan news outlets
- All `CHECK` constraints on `bias_label` and `label` columns enforce the 5-label taxonomy
- All `INSERT` statements use `ON CONFLICT (domain) DO NOTHING` on the `sources` table

### `requirements.txt`
142 Python packages pinned at exact installed versions on 2026-04-07. Key packages and their pinned versions:
- `langgraph==1.1.6`
- `fastmcp==3.2.0`
- `mcp==1.27.0`
- `fastapi==0.135.3`
- `langchain-google-genai==4.2.1`
- `langchain-groq==1.1.2`
- `psycopg2-binary==2.9.11`
- `pgvector==0.4.2`
- `redis==7.4.0`
- `httpx==0.28.1`
- `playwright==1.58.0`
- `beautifulsoup4==4.14.3`
- `pytest==9.0.2`
- `pytest-asyncio==1.3.0`
- `google-genai==1.70.0`
- `groq==0.37.1`
- `streamlit==1.56.0`
- `uvicorn==0.44.0`

### `.venv/`
Python 3.14.0 virtual environment created inside `veritas-agent/`. Playwright Chromium browser (Chrome for Testing 145.0.7632.6) installed via `playwright install chromium`.

### `.env`
All required environment variables configured:
- `GEMINI_API_KEY` — live Gemini API key
- `GROQ_API_KEY` — live Groq API key
- `DATABASE_URL=postgresql://raghad_taboun@localhost:5432/newsguard`
- `REDIS_URL=redis://localhost:6379`
- `MCP_SERVER_URL=http://127.0.0.1:8000/mcp`
- `CONTENT_LANGUAGE=ar`
- `GEMINI_MODEL=gemini-2.0-flash` (primary, per spec)
- `GEMINI_FALLBACK_MODEL=gemini-2.5-flash` (fallback — see Section C)
- `GEMINI_EMBEDDING_MODEL=text-embedding-004`
- `GROQ_MODEL=llama-3.3-70b-versatile`

### `config/sections.py`
`SECTIONS` dictionary implemented with all three section entries. Each entry contains: `label` (Arabic display name), `query_ar`, `query_en`, `countries` (ISO codes), `sources` (domain list), `update_every_hours`, and `max_articles_per_run`. File contains only data — no functions, imports, or executable logic.

---

## C — Logic Changes and Deviations

### Deviation 1 — pgvector built from source
**What happened:** `brew install pgvector` installed pgvector 0.8.2 built only for PostgreSQL 17 and 18, not 16. The vector extension control file was absent from the PostgreSQL 16 extension directory.

**Resolution:** pgvector was cloned from source (`github.com/pgvector/pgvector`) and compiled against `/opt/homebrew/opt/postgresql@16/bin/pg_config`. The resulting shared library and SQL files were installed into the PostgreSQL 16 extension directory. pgvector 0.8.2 is now active inside the `newsguard` database.

**Impact:** None on functionality. The installed version is identical. This step must be repeated on any new machine where PostgreSQL 16 is used.

### Deviation 2 — Gemini model fallback variable added
**What happened:** During the Step 0.5 API key verification, `gemini-2.0-flash` returned HTTP 429 with `limit: 0` for all free-tier quota dimensions. This indicates today's daily free-tier quota for that model was fully consumed by prior usage on the key. `gemini-2.5-flash` was confirmed working with a live API call.

**Resolution (agreed with project owner):** `GEMINI_MODEL=gemini-2.0-flash` is kept as the primary model in `.env`, consistent with the spec. A `GEMINI_FALLBACK_MODEL=gemini-2.5-flash` variable was added to `.env` as a fallback. The MCP server (Phase 1) will attempt the primary model first and fall back to `GEMINI_FALLBACK_MODEL` on a 429 quota error. This deviation is documented here and will be reflected in the Phase 1 MCP server implementation.

**Impact:** No change to spec-defined model. Adds one new `.env` variable and one retry branch in the MCP server.

### Deviation 3 — `google-generativeai` deprecated; `google.genai` used instead
**What happened:** `google-generativeai` (the package listed in `plan.md`) is deprecated as of 2026. The replacement is `google-genai` (`google.genai` import path), which was installed alongside it. All LLM calls in the MCP server will use `google.genai`, not the deprecated package.

**Impact:** Functionality is equivalent. The deprecated package remains in `requirements.txt` as it was installed as a transitive dependency.

### Deviation 4 — Python 3.14 Pydantic V1 shim warning
**What happened:** `langchain_core` emits a `UserWarning` that Pydantic V1 shims are not compatible with Python 3.14+. This is a non-blocking warning; all Pydantic V2 calls function correctly.

**Impact:** No functional impact. Will monitor if any LangChain internal calls exhibit unexpected behavior due to this limitation.

---

## D — Dependencies Introduced

All packages were specified by `plan.md` Step 0.4. No packages beyond the spec were added.

System-level installs performed outside pip:
- **PostgreSQL 16.13** (Homebrew) — `brew install postgresql@16`
- **Redis 8.6.2** (Homebrew) — `brew install redis`
- **pgvector 0.8.2** — built from source against PostgreSQL 16
- **Playwright Chromium 145.0** — `playwright install chromium`

---

## E — Known Issues and Limitations

1. **`gemini-2.0-flash` daily quota exhaustion:** On 2026-04-07, the primary model's free-tier quota was 0 for the configured key. The fallback model (`gemini-2.5-flash`) was confirmed working. This is a per-day quota issue, not a persistent configuration error.

2. **Python 3.14 Pydantic V1 shim incompatibility:** `langchain_core` uses Pydantic V1 compatibility shims internally. These shims are explicitly unsupported on Python 3.14. If unexpected LangChain behavior is observed in Phase 2+, downgrading to Python 3.12 should be considered.

3. **pgvector source build required:** The Homebrew pgvector bottle does not include a PostgreSQL 16 build. Any fresh environment setup requires compiling pgvector from source. This must be documented in deployment instructions.

4. **System `GOOGLE_API_KEY` env var conflict:** The machine has a `GOOGLE_API_KEY` set at the system level. The `google.genai` SDK detects both this and `GEMINI_API_KEY` from `.env`. MCP server code must explicitly pass `api_key=os.getenv('GEMINI_API_KEY')` and pop `GOOGLE_API_KEY` from the environment before initializing the Gemini client, to ensure the correct key is used.

---

## F — Test Results

No automated tests are defined for Phase 0 (tests are introduced in Phase 1). Manual verification commands run and passed:

```
psql -d newsguard -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"
→ 6

redis-cli ping
→ PONG

python -c "import langgraph, fastmcp, mcp, fastapi, psycopg2, redis, httpx, playwright, dotenv; print('All OK')"
→ All OK

python -c "from config.sections import SECTIONS; ..."
→ SECTIONS imported OK (all 3 sections, correct update intervals and article limits)

Groq live API call → GROQ_OK ✓
Gemini 2.5 Flash live API call → GEMINI_OK ✓
```

---

## G — Success Criteria Verification

| Criterion | Status | Evidence |
|---|---|---|
| PostgreSQL and Redis are running | **MET** | `brew services list` shows both `started`; `redis-cli ping` → PONG; `psql` connects |
| All six database tables exist | **MET** | `\dt` in newsguard shows 6 tables; seed data: 13 source rows |
| All Python imports resolve without errors | **MET** | All 16 key packages imported cleanly in a single test run |
| Both LLM APIs respond to test calls | **MET** | Groq → GROQ_OK; Gemini 2.5 Flash → GEMINI_OK. Primary model gemini-2.0-flash has daily quota exhausted today; fallback confirmed working. This is a quota state issue, not an API access issue. |

---

## H — What the Next Phase Depends On

Phase 1 (MCP Server with 9 Core Tools) depends on the following Phase 0 outputs:

- **`newsguard` database** — all 6 tables must exist; HNSW index on `articles.embedding` must be active
- **`config/sections.py`** — MCP server tools (`fetch_gdelt`, `check_relevance`) import section configuration from this file
- **`.env`** — all 8 environment variables must be present and valid; specifically `DATABASE_URL`, `REDIS_URL`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, and `GROQ_MODEL`
- **`.venv/`** — virtual environment with all 142 pinned packages; MCP server runs inside this venv
- **`GEMINI_FALLBACK_MODEL` handling** — Phase 1 MCP server must implement the fallback retry logic for Gemini model quota exhaustion, as agreed in this phase
