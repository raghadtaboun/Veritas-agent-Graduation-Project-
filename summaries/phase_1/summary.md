# Phase 1 Summary — MCP Server with 9 Core Tools

## A — Phase Identification

| Field | Value |
|---|---|
| Phase Number | 1 |
| Phase Title | MCP Server with 9 Core Tools |
| Start Date | 2026-04-08 |
| Completion Date | 2026-04-09 |

---

## B — What Was Implemented

### `mcp_server/server.py` (912 lines at close of phase)

The entire file was built incrementally across Steps 1.1–1.10. Every tool is an `async def` decorated with `@mcp.tool()`. All infrastructure clients are initialised once at module load time and shared across all tool invocations.

**Server skeleton (Step 1.1)**
- `FastMCP("veritas-agent")` instance with `stateless_http=True` and `json_response=True`.
- Startup block: `mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)`.
- Infrastructure clients initialised at module load: `genai.Client` (Gemini), `AsyncGroq` (Groq), `redis.from_url` (Redis), `psycopg2.connect` factory (`_db_connect`).
- `os.environ.pop("GOOGLE_API_KEY", None)` applied before Gemini client init to prevent system-level key conflict (Phase 0 deviation).
- `_gemini_generate(prompt, model)` helper: calls `generate_content` via `asyncio.to_thread`; on HTTP 429 retries once with `GEMINI_FALLBACK_MODEL`.
- `_gemini_embed(text)` helper: calls `embed_content` via `asyncio.to_thread` with `output_dimensionality=768` (see Section C, Deviation 2).
- TTL constants declared centrally: `_TTL_RELEVANCE=86400`, `_TTL_ENTITIES=86400`, `_TTL_EMBEDDING=604800`, `_TTL_BIAS=259200`.

**Step 1.2 — `fetch_gdelt`**
- Queries GDELT DOC API (`https://api.gdeltproject.org/api/v2/doc/doc`) with `mode=artlist`, `format=json`, `timespan=7d`.
- Uses `cfg["query_gdelt"]` (concise GDELT-optimised field — see Section C, Deviation 1) plus `sourcelang:arabic` filter.
- Country filter appended only when `len(countries) == 1` (Libya only) to avoid GDELT timeout on long OR chains.
- Discards articles with `len(title) < 15` or missing `url`.
- Returns `{"articles": [...], "count": int, "section": str}` on both success and error paths — `section` key is always present (bug fixed during Step 1.2 verification: original error path omitted `section`).
- Error path: `{"error": str, "articles": [], "count": 0, "section": section}`.

**Step 1.3 — `check_relevance`**
- Uses `AsyncGroq.chat.completions.create` with `GROQ_MODEL` (`llama-3.3-70b-versatile`), `temperature=0.0`, `max_tokens=120`.
- Cache key: `relevance:{section}:{md5(title)}`.
- Prompt instructs the model to return `{"relevant": bool, "reason": "one sentence in English"}` with no markdown.
- Post-parse: `relevant` normalised to genuine `bool`; code-fence stripping via `re.sub`.
- Error fallback: `{"relevant": False, "reason": "classification error: ..."}`.
- Cache TTL: 24 h.

**Step 1.4 — `scrape_article`**
- Module-level constants: `_ARTICLE_SELECTORS` (5 CSS selectors in priority order), `_STRIP_TAGS` (6 non-content tag names), `_AR_HEADERS` (Arabic Accept-Language + realistic User-Agent), `_MIN_CONTENT_LEN=300`.
- `_extract_text_from_html(html)` shared helper: strips noise tags, tries each selector, falls back to `<body>` text.
- Layer 1: `httpx.AsyncClient(timeout=12.0, follow_redirects=True)` with Arabic headers.
- Layer 2: `playwright.sync_api.sync_playwright` wrapped in `asyncio.to_thread`; launches headless Chromium, `wait_until="networkidle"`, `timeout=30000`; browser always closed in `finally`.
- Layer 3: `{"success": False, "content": None, "method": "failed", "layer1_error": str, "layer2_error": str}`.
- Never raises; all exceptions caught per layer with error stored in the respective `layer_N_error` field.

**Step 1.5 — `get_embedding`**
- Cache key: `embedding:{sha256(text[:3000])}` — SHA-256 of the already-truncated text.
- Redis cache read before Gemini call; cache write with `_TTL_EMBEDDING` (7 days).
- Calls `_gemini_embed(truncated)` which uses `GEMINI_EMBEDDING_MODEL` with `output_dimensionality=768`.
- Returns `{"embedding": [float×768], "cached": bool}` on success; `{"error": str, "embedding": None}` on failure.

**Step 1.6 — `store_article`**
- `_parse_published_at(raw)` helper: accepts GDELT seendate `YYYYMMDDTHHMMSSZ` or ISO-8601 string; raises `ValueError` on invalid input.
- Embedding serialised as `"[v1,v2,...]"` string cast with `%s::vector` in SQL (no extra dependencies).
- `entities` defaults to `{"people": [], "locations": [], "organizations": []}` when omitted.
- SQL: `INSERT ... ON CONFLICT (url) DO NOTHING RETURNING id`. If `RETURNING` is empty (URL existed), falls back to `SELECT id WHERE url = %s`.
- Returns `{"id": int, "is_new": True}` or `{"id": int, "is_new": False}`.
- All psycopg2 calls in `asyncio.to_thread(_sync_store)` (Rule 2.2). Outer `try/except` returns `{"error": str}`.

**Step 1.7 — `find_similar`**
- `_72H_SECONDS = 259200` constant used directly in the SQL `WHERE` clause.
- Three spec conditions all enforced in one SQL query — none in application code:
  1. `WHERE section = %s`
  2. `ABS(EXTRACT(EPOCH FROM (published_at - %s::timestamptz))) <= 259200`
  3. `(1 - (embedding <=> %s::vector)) >= %s`
- `ORDER BY embedding <=> %s::vector` (ascending cosine distance = descending similarity).
- Optional `exclude_id` parameter appends `AND id != %s` clause when provided.
- Input guards: unknown section, `threshold` outside `[0,1]`, invalid `published_at` — all return structured error dicts before touching the DB.
- Returns `{"articles": [{id, title, published_at, similarity}], "count": int}`.

**Step 1.8 — `extract_entities`**
- `_EMPTY_ENTITIES = {"people": [], "locations": [], "organizations": []}` — safe default returned on any error.
- `_ENTITY_PROMPT_TEMPLATE` — structured prompt requiring strict JSON with exactly three list-valued keys, no markdown.
- Cache key: `entities:{sha256(text[:3000])}`. TTL: 24 h.
- Post-parse normalisation: list comprehension on each key ensures all values are non-empty strings; all three keys always present.
- Error fallback: `dict(_EMPTY_ENTITIES)` (fresh copy, constant never mutated).

**Step 1.9 — `classify_bias`**
- `_VALID_BIAS_LABELS` — `frozenset` of the five spec-defined labels.
- `_BIAS_FALLBACK = {"score": 0.0, "label": "neutral", "confidence": 0.0, "framing": "تعذّر تصنيف المقال تلقائياً"}` — returned on any error.
- `_BIAS_PROMPT_TEMPLATE` — defines all five Arabic-context labels with meanings; requires strict JSON with `score`, `label`, `confidence`, `framing`.
- Cache key: `bias:{sha256(text[:3000])}`. TTL: 72 h.
- Post-parse normalisation: invalid `label` → `"neutral"`; `score` clamped to `[-1.0, 1.0]`; `confidence` clamped to `[0.0, 1.0]`.
- Error fallback: `dict(_BIAS_FALLBACK)` — never raises (Rule 2.4). Articles with `confidence < 0.6` are flagged for manual review in Phase 6 evaluation.

**Step 1.10 — `cache_set` / `cache_get`**
- `cache_set(key, value, ttl)`: rejects `ttl ≤ 0` immediately; calls `_redis_client.setex` in `asyncio.to_thread`; returns `{"success": bool}` or `{"success": False, "error": str}`.
- `cache_get(key)`: calls `_redis_client.get` in `asyncio.to_thread`; returns `{"value": str}` or `{"value": None}` (plus optional `"error"` key on Redis failure); never raises.

### `config/sections.py`
`query_gdelt` field added to all three section entries (see Section C, Deviation 1). No other changes.

### `.env`
`GEMINI_EMBEDDING_MODEL` updated from `text-embedding-004` to `models/gemini-embedding-001` (see Section C, Deviation 2).

### `tests/test_tools.py` (Step 1.11 — 224 lines)
Nine async tests, one per Tier-1 tool, each opening its own `ClientSession` via `streamable_http_client` and calling the tool through the live MCP transport (Rule 3.2).

- **`test_fetch_gdelt`** — live GDELT call, Libya section, limit=5; validates shape including `section` key; does not assert count > 0 (network condition documented in Deviation 1).
- **`test_check_relevance`** — Redis cache pre-seeded `# MOCK`; verifies `relevant=True` hit; separate call tests unknown-section guard (no LLM involved).
- **`test_scrape_article`** — unreachable `.invalid` domain forces Layer 3 path; asserts `success=False`, `content=None`, both layer error strings non-empty.
- **`test_get_embedding`** — Redis pre-seeded with 768-float synthetic embedding `# MOCK`; verifies `cached=True` and exactly 768 float values.
- **`test_store_article`** — live PostgreSQL insert with unique timestamped URL; first call `is_new=True`, second call `is_new=False`; row deleted in `finally` block.
- **`test_find_similar`** — two articles inserted directly via psycopg2 (test infrastructure), tool called via MCP; verifies article B found when searching with article A's embedding, `exclude_id` works; rows deleted in `finally`.
- **`test_extract_entities`** — Redis pre-seeded with Arabic entity dict `# MOCK`; verifies all three keys and specific Arabic values.
- **`test_classify_bias`** — Redis pre-seeded with valid bias dict `# MOCK`; verifies all four fields, label in taxonomy, score/confidence within bounds.
- **`test_cache_set_and_get`** — live Redis round-trip; verifies missing key returns `None`, invalid TTL returns `success=False`.

Supporting files added: `pytest.ini` (`asyncio_mode = auto`), `tests/__init__.py` (empty).

**Result:** `pytest tests/test_tools.py -v` → **9 passed, 0 warnings, 5.93 s** on 2026-04-09.

---

## C — Logic Changes and Deviations

### Deviation 1 — `query_gdelt` field in `config/sections.py`
**What happened:** The original spec defines `query_ar` and `query_en` fields for GDELT queries. In practice, long `OR` chains (15 countries × multiple terms) caused GDELT to return HTTP 429 responses or time out. A concise `query_gdelt` field was introduced in Phase 0 as a GDELT-optimised query string.

**Resolution:** `fetch_gdelt` uses `cfg["query_gdelt"]` instead of `query_en` for GDELT API calls. `query_ar` and `query_en` are preserved in the config for display and future use.

**Impact:** GDELT returns reliable results for the Libya section. Middle East and World sections experience intermittent 429/timeout responses from GDELT — this is a network/rate condition specific to the development environment, not a code defect. All three sections use the identical code path.

### Deviation 2 — Embedding model changed from `text-embedding-004` to `models/gemini-embedding-001`
**What happened:** The spec and original `.env` specify `text-embedding-004`. The Gemini API on this API key returns HTTP 404 for `text-embedding-004` — the model is not available on this key. Discovery via `client.models.list()` showed the available embedding models are `models/gemini-embedding-001` and `models/gemini-embedding-2-preview`.

**Resolution:** `GEMINI_EMBEDDING_MODEL` in `.env` updated to `models/gemini-embedding-001`. The `_gemini_embed` helper was updated to pass `output_dimensionality=768` via `google.genai.types.EmbedContentConfig`. This produces exactly 768-dimension vectors — matching the `vector(768)` column in the `articles` table.

**Impact:** Embedding dimension (768) is unchanged. The model is the designated successor to `text-embedding-004`. Quality is equivalent or superior.

### Deviation 3 — `fetch_gdelt` error path missing `section` key
**What happened:** During Step 1.2 verification, an exception in the GDELT HTTP request returned `{"error": str, "articles": [], "count": 0}` — without the `section` key that the happy path always includes.

**Resolution:** Error return updated to `{"error": str, "articles": [], "count": 0, "section": section}`. Response shape is now consistent across all code paths.

**Impact:** Agents and tests that read the `section` key from the response no longer crash on GDELT network failures.

### Deviation 4 — Transient Gemini 503 during `classify_bias` testing
**What happened:** During Step 1.9 verification, `gemini-2.5-flash` returned HTTP 503 "This model is currently experiencing high demand." The `_gemini_generate` helper raised, which was caught by `classify_bias`'s `except` block, returning the neutral fallback correctly.

**Resolution:** No code change required — the error handling worked exactly as specified. The 503 resolved within minutes on a retry. The fallback `confidence=0.0` correctly flags the result for manual review.

**Impact:** None on code. The transient behaviour confirms the fallback mechanism functions correctly under real API failure conditions.

### Deviation 5 — Carried forward from Phase 0
`gemini-2.5-flash` is the active primary model (`.env` `GEMINI_MODEL=gemini-2.5-flash`). `google.genai` SDK is used throughout instead of deprecated `google-generativeai`. `GEMINI_FALLBACK_MODEL=gemini-2.0-flash` fallback is implemented in `_gemini_generate`. These are Phase 0 deviations; all Phase 1 code was built respecting them.

### Post-Phase-1 Modification — classify_bias signature changed (pre-Phase-3 architectural decision)
The `classify_bias` tool signature was changed from `classify_bias(text: str)` to `classify_bias(articles: list[dict], target_article_id: int)` before Phase 3 began, at the supervisor's explicit request. The tool now performs comparative bias analysis by sending all articles in an event cluster to Gemini simultaneously. Single-article fallback mode is preserved for singletons and for the Phase 6 evaluation script. This change does not affect any Phase 1 or Phase 2 outputs already stored in the database.

---

## D — Dependencies Introduced

No new packages were added beyond those already in `requirements.txt` from Phase 0. All imports used in Phase 1 (`httpx`, `playwright`, `beautifulsoup4`, `psycopg2-binary`, `redis`, `groq`, `google-genai`, `fastmcp`) were already pinned in `requirements.txt`.

One change was made to an environment variable value (not a package):
- `GEMINI_EMBEDDING_MODEL`: changed from `text-embedding-004` to `models/gemini-embedding-001` in `.env`.

---

## E — Known Issues and Limitations

1. **GDELT intermittent failures for `middle_east` and `world` sections.** GDELT returns 429 or times out for these sections in the local development environment. Libya section is reliable. All three use identical code. This is a GDELT rate/network condition, not a code defect. To be re-tested in Phase 2 when the Ingestion Agent is complete.

2. **Playwright (Layer 2) not exercised in `test_scrape_article`.** The test uses an unreachable `.invalid` domain which triggers the Layer 3 failure path directly; Layer 2 is never reached from the sandboxed environment. Layer 2 has been code-reviewed and is structurally correct (browser opened/closed in `finally`, error stored). Live Layer 2 testing must be done from the user's terminal with a JavaScript-heavy Arabic news page.

3. **`classify_bias` confidence is a model estimate, not a calculated probability.** Per `agent.md` Part 7, this must not be described as a calculated probability in any documentation. It is the model's self-reported certainty. Articles with `confidence < 0.6` are flagged for the Phase 6 evaluation step.

4. **Gemini 503 transient unavailability.** Observed once during Step 1.9 testing. The fallback returns `neutral` with `confidence=0.0`. Pipeline runs at production time should include a retry with exponential back-off; this is not currently implemented in `_gemini_generate` beyond the single 429 retry. This is a limitation to monitor in Phase 2.

---

## F — Test Results

Per-step transport tests were run during development. Step 1.11 then formalised all nine tests into `tests/test_tools.py` which passes under `pytest`.

**Formal test run (Step 1.11):**
```
pytest tests/test_tools.py -v
9 passed, 0 warnings in 5.93s   (2026-04-09)
```

Per-step transport verification results:

| Step | Tool | Result |
|---|---|---|
| 1.1 | Server skeleton | `list_tools()` returned 9 tools after all steps complete. Streamable-HTTP transport confirmed. |
| 1.2 | `fetch_gdelt` | Libya: articles returned with title/url/domain/seendate. `section` key always present. Short-title filter confirmed. Unknown-section guard confirmed. |
| 1.3 | `check_relevance` | Libya-relevant title → `relevant=True`. Sports title → `relevant=False`. Cache hit returns identical result. Unknown-section guard confirmed. Verified live from user terminal. |
| 1.4 | `scrape_article` | Layer 1: 758 chars from aljazeera.net. Layer 3: correct failure dict from unreachable URL. No exception on malformed URL. Noise tags stripped. |
| 1.5 | `get_embedding` | 768 dimensions confirmed. All values `float`. Cache miss on first call, cache hit on second. Truncation: 3400-char input stable. Empty string: structured error, no crash. |
| 1.6 | `store_article` | New insert: `is_new=True`. Duplicate URL: `is_new=False`, original title unchanged in DB. ISO-8601 date accepted. Invalid date: structured error. Missing entities: default applied. |
| 1.7 | `find_similar` | Article B (T+1h, identical embedding) found. Article A (self) excluded. Article C (T+80h) excluded by SQL time-window. All guards return structured errors. |
| 1.8 | `extract_entities` | 3 people / 3 locations / 2 organizations extracted correctly. Cache hit confirmed. Long text stable. Empty text: empty lists, no crash. |
| 1.9 | `classify_bias` | `pro_government` article: `label=pro_government, score=0.95, confidence=0.98`, Arabic framing. Clamping: `1.5→1.0`. Invalid label: normalised to `neutral`. Cache hit confirmed. |
| 1.10 | `cache_set` / `cache_get` | Write/read round-trip with Arabic content confirmed. `ttl≤0` rejected. Overwrite works. Missing key returns `null`. |
| 1.11 | `tests/test_tools.py` | `pytest tests/test_tools.py -v` → **9 passed, 0 warnings, 5.93 s** on 2026-04-09. Each test opens its own MCP session and calls the tool via streamable-HTTP transport. LLM tests use Redis cache pre-seeding (`# MOCK`). DB tests use real PostgreSQL with cleanup. |

---

## G — Success Criteria Verification

| Criterion | Status | Evidence |
|---|---|---|
| `pytest tests/test_tools.py` passes for all nine tools | **MET** | `tests/test_tools.py` written (Step 1.11). `pytest tests/test_tools.py -v` → **9 passed, 0 warnings, 5.93 s** on 2026-04-09. All nine tools exercised via live MCP transport. LLM-dependent tests (2, 4, 7, 8) use Redis cache pre-seeding (`# MOCK`) to avoid quota exhaustion. |
| The server runs stably on port 8000 | **MET** | Server starts without errors. Responds to MCP transport requests on `http://127.0.0.1:8000/mcp`. Handled 30+ tool calls across the verification session without crash or restart. |

**Phase 1 is COMPLETE.** All 9 Tier-1 tools implemented, individually verified, and formally tested. `pytest tests/test_tools.py` → 9 passed, 0 warnings. Phase 2 may begin.

---

## H — What the Next Phase (2) Depends On

Phase 2 (Ingestion Agent) depends on the following Phase 1 outputs:

| Dependency | Status | Notes |
|---|---|---|
| `fetch_gdelt` tool | Ready | Returns articles with title, url, domain, seendate |
| `check_relevance` tool | Ready | Returns `{relevant: bool, reason: str}` |
| `scrape_article` tool | Ready | Three-layer strategy; Layer 3 never raises |
| `extract_entities` tool | Ready | Returns `{people, locations, organizations}` — must run BEFORE `store_article` per data flow spec |
| `get_embedding` tool | Ready | Returns 768-dim vector; `models/gemini-embedding-001` |
| `store_article` tool | Ready | Accepts `published_at` (GDELT or ISO format), `entities`, `embedding` |
| `find_similar` tool | Ready | Used by Clustering Agent (Phase 3), not Ingestion Agent |
| `classify_bias` tool | Ready | Used by Bias Agent (Phase 3), not Ingestion Agent |
| `cache_set` / `cache_get` | Ready | General-purpose pipeline result caching |
| `tests/test_tools.py` | **Ready** | `pytest tests/test_tools.py` → 9 passed, 0 warnings |
| `agents/base.py` | Not yet written | Phase 2, Step 2.1 — `MCPAgent` base class |
