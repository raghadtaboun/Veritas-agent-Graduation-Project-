# plan.md — Veritas Agent: Development Roadmap

> **Purpose:** This document is the step-by-step implementation guide for building Veritas Agent. It is divided into eight phases (0, 1, 2, 3, 4, **4.5**, 5, 6), each with granular, actionable steps. Follow phases in order. Do not begin a phase until all steps of the previous phase pass their success criteria. After completing each phase, generate a `summary.md` file as specified in `agent.md`.
>
> **Important — read this before editing any phase:** Phases 0–4 document the original architecture that was actually executed on the codebase and therefore remain in this file as the historical record. Phase 4.5 was introduced after Phase 4 integration revealed three problems: quota propagation across agents, ReAct misuse, and divergence from the canonical MCP pattern. Phase 4.5 refactors the system to the canonical MCP pattern described in `blueprint.md`. **Do not rewrite Phases 0–4 to match the new architecture** — they happened as documented, and `summaries/phase_0/` through `summaries/phase_4/` are the authoritative record of what actually occurred. The pre-migration versions of this file and the other documentation files are preserved in `docs/archive/pre_canonical_mcp/`. See `docs/decisions/ADR-001-canonical-mcp-migration.md` for the full migration rationale.

---

## Development Principles

Before starting, internalize these principles for the entire build process:

- Build modularly. Each component must work and be tested before the next one depends on it.
- The MCP server is built before the agents. Agents are built before the pipeline. The pipeline is built before the API. The API is built before the frontend.
- Every phase ends with a testable, demonstrable result — not just code that exists.
- **Post Phase 4.5 principle (applies to all new code from Phase 4.5 onward):** The MCP server contains only pure data-access and external-API tools. No LLM call lives in the server. All LLM reasoning happens in the agents. Agents access infrastructure exclusively via `self.call_tool()`, and access LLMs via `self.call_gemini()` / `self.call_gemini_embedding()` / `self.call_groq()` on the `MCPAgent` base class.

---

## Phase 0 — Environment Setup

**Goal:** A fully configured local environment where all dependencies are installed, services are running, and the project structure exists on disk.

### Step 0.1 — Create Project Directory Structure

Create the exact folder structure defined in `readme.md`. Every directory listed must exist, even if empty. This structure is not to be changed during development. File placement must match the structure map in `readme.md`.

### Step 0.2 — Install System Services

Install and start PostgreSQL 16 with the pgvector extension and Redis. Verify both are running and accepting connections before proceeding. Record the exact startup commands used, as they will be needed for the deployment documentation.

### Step 0.3 — Create the Database

Create a database named `newsguard`. Enable the `vector` extension inside it. Apply the full schema from `infra/schema.sql`. Verify that all six tables are created and that the initial source records are seeded. Confirm the HNSW index on the `embedding` column was created successfully.

### Step 0.4 — Install Python Dependencies

Create a `requirements.txt` that pins the exact versions of all libraries. Install them into a virtual environment. The key packages are: `langgraph`, `langchain-google-genai`, `langchain-groq`, `mcp[cli]`, `fastmcp`, `fastapi`, `uvicorn`, `streamlit`, `psycopg2-binary`, `pgvector`, `redis`, `httpx`, `playwright`, `beautifulsoup4`, `python-dotenv`, `pytest`, `pytest-asyncio`, and `google-generativeai`. Run `playwright install chromium` after pip installs complete.

> **Critical:** Pin every library version on the day you install it. Never upgrade any package mid-development. Version conflicts between LangGraph and the MCP SDK are the highest-risk failure in this project — a pinned `requirements.txt` is your insurance.

### Step 0.5 — Configure Environment Variables

Create a `.env` file in the project root containing all required keys and URLs. The required keys are: `GEMINI_API_KEY`, `GROQ_API_KEY`, `DATABASE_URL` (pointing to the `newsguard` database), `REDIS_URL`, `MCP_SERVER_URL` (default: `http://127.0.0.1:8000/mcp`), and `CONTENT_LANGUAGE` (set to `ar`). Update schedules are not set here — they are defined per section via the `update_every_hours` key in `config/sections.py` (see Step 0.6). Add `.env` to `.gitignore` immediately. Verify that all API keys are valid by making one direct test call to each LLM API before any code is written.

### Step 0.6 — Create config/sections.py

Implement the `SECTIONS` dictionary with entries for `middle_east`, `libya`, and `world`. Each entry must define: `label` (Arabic display name), `query_ar` (Arabic GDELT query string), `query_en` (English GDELT query string), `countries` (list of ISO country codes), `sources` (list of known domain names), `update_every_hours` (Libya: 4, Middle East and World: 6), and `max_articles_per_run` (Libya: 50, Middle East and World: 75). This file is used by the MCP server and the agents. It must be importable and must not contain any logic — only configuration data.

**Phase 0 Success Criteria:** PostgreSQL and Redis are running. All six database tables exist. All Python imports resolve without errors. Both LLM APIs respond to test calls.

---

## Phase 1 — MCP Server with 9 Core Tools

**Goal:** A running MCP server that exposes all nine core tools and passes individual unit tests for each.

> **Note:** The 9 core tools defined in this phase include 5 tools that embed LLM calls (`check_relevance`, `get_embedding`, `extract_entities`, `classify_bias`, plus `extract_facts` in Phase 4). This was the original design. Phase 4.5 later refactors these into client-side LLM calls per the canonical MCP pattern — but Phase 1 remains documented as it was executed.

### Step 1.1 — Create mcp_server/server.py Skeleton

Initialize the FastMCP server instance with `stateless_http=True` and `json_response=True`. Configure all infrastructure clients: Gemini model, Groq async client, Redis client, and the database URL constant. Verify the server starts without errors before adding any tools.

### Step 1.2 — Implement fetch_gdelt

Build the `fetch_gdelt` tool. It must accept a `section` parameter and a `limit` parameter. It queries the GDELT DOC API using the Arabic query string and country filters from `config/sections.py`. It returns a cleaned list of articles containing title, URL, domain, and publication date (`seendate`). Articles with titles shorter than 15 characters are discarded.

### Step 1.3 — Implement check_relevance

Build the `check_relevance` tool using Groq (Llama 3.3 70B). It must check Redis before making an LLM call. The cache key must be deterministic based on the title and section inputs. It returns a JSON object with `relevant` (boolean) and `reason` (string). Cache hits must be returned without touching the LLM.

### Step 1.4 — Implement scrape_article

Build the `scrape_article` tool with three fallback layers. This is required because a significant portion of Arabic news sites rely on JavaScript rendering, and a single-layer HTTP approach will fail silently on most of them.

**Layer 1 — Plain HTTP:** Make a standard `httpx` GET request with Arabic-language headers and a 12-second timeout. Parse the response with BeautifulSoup, strip non-content tags (`script`, `style`, `nav`, `footer`, `header`, `aside`), and attempt to extract the article body using known CSS selectors (`article`, `main`, `.article-body`, `.article-content`, `#content`). Return the extracted text if it exceeds 300 characters.

**Layer 2 — Playwright:** If Layer 1 fails or returns insufficient content, launch a headless Chromium browser, navigate to the URL, wait for `networkidle`, extract the full page HTML, and parse it with BeautifulSoup using the same logic as Layer 1. Close the browser before returning.

**Layer 3 — Title Only:** If both layers fail, return `{"success": false, "content": null, "method": "failed"}`. The calling agent must handle this gracefully by storing the article with `has_full_content = false` rather than discarding it. A stored title with no body is still valuable for clustering and bias-by-source analysis.

The tool must never raise an exception. All errors are caught and returned in the response object.

### Step 1.5 — Implement get_embedding

Build the `get_embedding` tool using Gemini `text-embedding-004`. It generates a 768-dimension float vector for the input text. Input text is truncated to 3,000 characters before embedding. The result is a dict with an `embedding` key containing the float list. Check Redis for a cached result before calling the API. Cache results with a 7-day TTL since embeddings are deterministic for the same input.

### Step 1.6 — Implement store_article

Build the `store_article` tool. It inserts a new article record into the `articles` table using `INSERT ... ON CONFLICT (url) DO NOTHING`. The `DO NOTHING` strategy is intentional: if a URL already exists, the existing record — including its embedding and analysis results — must not be overwritten by a subsequent pipeline run.

The tool must store the following fields: `title`, `content`, `url`, `source_id`, `section`, `embedding` (as a pgvector column), `entities` (as JSONB), `has_full_content` flag, and `published_at` timestamp. The `published_at` field is required and must be parsed from the GDELT `seendate` field before being passed to this tool. Without `published_at`, the Clustering Agent's 72-hour time window condition cannot function.

It returns the article's database ID and an `is_new` boolean.

### Step 1.7 — Implement find_similar

Build the `find_similar` tool. It performs a pgvector cosine-similarity search on the `articles` table. The search is constrained by three filters applied together:

1. `section` must match the input section parameter.
2. The absolute difference between the candidate article's `published_at` and the query article's `published_at` must be less than or equal to 72 hours. This is evaluated in the SQL query using a date arithmetic condition, not in application code.
3. The computed similarity score `1 - (embedding <=> query_embedding)` must meet or exceed the `threshold` parameter.

Results are ordered by descending similarity. This design ensures that the time window check is enforced at the database level, where it is efficient and unambiguous.

### Step 1.8 — Implement extract_entities

Build the `extract_entities` tool using Gemini. It prompts the model to return a structured JSON with three keys: `people`, `locations`, and `organizations`, each containing a list of Arabic-language entity strings. It checks Redis for a cached result before calling the LLM. Results are cached with a 24-hour TTL.

### Step 1.9 — Implement classify_bias

Build the `classify_bias` tool using Gemini. The prompt must instruct the model to return a strict JSON object with four fields: `score` (float -1.0 to 1.0), `label` (one of the five valid labels: `pro_government`, `opposition`, `neutral`, `pan_arab`, `western_aligned`), `confidence` (float 0.0 to 1.0), and `framing` (one-sentence Arabic description).

The `score` and `confidence` values are estimates produced by the language model based on the linguistic signals it learned during training — they are not the result of a deterministic formula. A clearly partisan article will produce a high-confidence score; an ambiguous article will produce a lower confidence value. Articles with confidence below 0.6 should be flagged for manual review in the evaluation phase.

Results are cached with a 72-hour TTL. The tool must never raise an exception — it falls back to a `neutral` default with `confidence: 0.0` on any error.

### Step 1.10 — Implement cache_set and cache_get

Build the two Redis utility tools. `cache_set` accepts a key, a string value, and a TTL in seconds, and returns a boolean success indicator. `cache_get` accepts a key and returns the stored string or `None`.

### Step 1.11 — Start Server and Write Tests

Add the server startup block that runs with `transport="streamable-http"` on `host="127.0.0.1"` and `port=8000`. Create `tests/test_tools.py` and write one test per tool. Tests must call each tool via a live MCP client session (not by importing the Python function directly) to validate the full transport path.

**Phase 1 Success Criteria:** `pytest tests/test_tools.py` passes for all nine tools. The server runs stably on port 8000.

---

## Phase 2 — Ingestion Agent

**Goal:** The first agent that works end-to-end, fetching, filtering, and storing Arabic news articles autonomously.

### Step 2.1 — Implement agents/base.py

Implement the `MCPAgent` base class with three methods:

1. `call_tool(tool_name, args)` — async method that opens a Streamable HTTP connection to the MCP server URL read from the `MCP_SERVER_URL` environment variable, initializes a session, calls the specified tool with the given arguments, and returns the parsed JSON response. This is the only place where MCP connection logic lives.

2. `think(goal, available_tools, observations, step)` — async method that calls Groq (Llama 3.3 70B) directly to decide the next action. This is NOT an MCP tool call — agent reasoning is the agent's own cognitive process, not an infrastructure call. The MCP boundary (Rule 2.3) applies to PostgreSQL, Redis, LLM tools, and external APIs; the agent's reasoning loop is architecturally separate. Returns either `{"tool": "name", "args": {...}, "reason": "..."}` to call a tool, or `{"done": true, "reason": "..."}` to signal completion.

3. `run_react(goal, available_tools, initial_context, max_steps, on_tool_call)` — async method that implements the full ReAct (Reasoning + Acting) loop. At each step, `think()` decides the next action and `call_tool()` executes it. The loop continues until the LLM signals done or `max_steps` is reached. An optional `on_tool_call` hook allows agents to inject working-memory data (e.g. embeddings, scraped content) into tool arguments.

All agents inherit this class.

### Step 2.2 — Implement agents/ingestion_agent.py

Build the `IngestionAgent` class as an autonomous ReAct-driven agent. Its `run(section)` method provides a goal and a list of available tools to `run_react()`, and the LLM decides the tool-call sequence dynamically:

- **Goal:** Ingest Arabic news articles for the given section — fetch articles, verify relevance, scrape content, extract entities, generate embeddings, and store each article.
- **Available tools:** `fetch_gdelt`, `check_relevance`, `scrape_article`, `extract_entities`, `get_embedding`, `store_article`.
- **Working memory:** A pre-tool hook (`on_tool_call`) injects data between tool calls — the LLM decides WHICH tool to call and WHEN; the hook provides large data blobs (embeddings, scraped content, entities) that the LLM cannot reasonably reproduce in its JSON output.
- **max_steps=100:** Sufficient for processing 15+ articles at ~5 tool calls per article plus the initial `fetch_gdelt`.

The LLM decides the sequence; the Python code only executes what the LLM chooses. The agent extracts a statistics dictionary from the observation history for compatibility with LangGraph NewsState: `fetched`, `relevant`, `scraped`, `entities_extracted`, `stored`, `errors`, and `article_ids`. It handles failures at any individual article without stopping the overall run.

### Step 2.3 — Write Ingestion Tests

Create `tests/test_agents.py` with a test for the Ingestion Agent. Run it against the `libya` section, which has the smallest `max_articles_per_run` value. Verify that at least 15 new records appear in the `articles` table after a single run. Verify that stored records include non-null `published_at` and `entities` fields for articles where extraction succeeded.

**Phase 2 Success Criteria:** Running the Ingestion Agent for the `libya` section stores 15 or more articles in the database. The statistics object returned by `run()` accurately reflects the counts at each step. Stored articles have `published_at` populated.

---

## Phase 3 — Clustering Agent and Bias Agent

**Goal:** Articles are grouped into events, and each article's political bias is classified and stored.

### Step 3.1 — Implement agents/clustering_agent.py

Build the `ClusteringAgent`. Its `run(section, article_ids)` method groups articles into event clusters using three conditions that must all be satisfied simultaneously:

**Condition 1 — Time Window:** The difference between the `published_at` timestamps of any two articles in a cluster must not exceed 72 hours. This is evaluated by comparing the `published_at` field of the candidate article against the `published_at` of the cluster's representative article (the first article assigned to the cluster). The comparison is between the two articles' publication timestamps — not between either article and the current time.

**Condition 2 — Semantic Similarity:** The cosine similarity between the two articles' embeddings must be ≥ 0.82. This is retrieved from the `find_similar` tool, which enforces the time window at the SQL level and returns only candidates that already satisfy Condition 1.

**Condition 3 — Entity Overlap:** The two articles must share at least 2 named entities across the combined `people`, `locations`, and `organizations` fields. Entity data must already be present in the `articles.entities` column, populated by the Ingestion Agent in Phase 2.

For every cluster with two or more members, the agent creates an `events` record and populates `article_events`. It must track which article IDs have already been assigned to a cluster to prevent double-assignment.

### Step 3.2 — Implement agents/bias_agent.py

Build the `BiasAgent` as a ReAct-driven autonomous agent inheriting from `MCPAgent`. Its `run(article_ids, event_clusters)` method performs comparative bias analysis:

- `event_clusters` is a dict mapping `event_id → [article_ids]` produced by the Clustering Agent in Phase 3.1.
- For each event cluster, the agent fetches all articles in the cluster from the database, then calls `classify_bias` with the full article list and each article's ID as the target — Gemini classifies each article relative to the others covering the same event.
- For singleton articles not assigned to any cluster, the agent calls `classify_bias` with a single-article list (fallback to absolute analysis).
- All results are stored in `bias_scores` using `ON CONFLICT DO NOTHING` (Rule 2.8).
- The agent exposes a `classify_single(text)` helper method used exclusively by the Phase 6 evaluation script. This method wraps `classify_bias` in single-article fallback mode and accepts raw text rather than a database article dict.

**Why comparative:** Sending all articles that cover the same event to Gemini simultaneously allows the model to detect relative framing — what each source emphasizes versus ignores — producing significantly more accurate and meaningful bias labels than context-free single-article classification.

### Step 3.3 — Write Tests for Both Agents

Add tests in `tests/test_agents.py` for both the Clustering Agent and the Bias Agent. The Clustering Agent test must verify that at least one event is created from a set of seeded articles. The Bias Agent test must verify that a bias score record is created for each processed article.

**Phase 3 Success Criteria:** Manual review of 10 clusters from the Libya section shows grouping accuracy above 70%. The `bias_scores` table is populated with valid labels for all processed articles.

---

## Phase 4 — Remaining Agents and Advanced MCP Tools

**Goal:** All six agents are operational. The six advanced MCP tools are implemented. A complete end-to-end pipeline run succeeds.

### Step 4.1 — Implement Advanced MCP Tools

Add the six Phase 4 tools to `mcp_server/server.py`: `get_source_bias`, `get_coverage_stats`, `detect_blindspot`, `extract_facts`, `vector_recommend`, and `get_user_profile`. Write a test for each new tool in `tests/test_tools.py`. The server must now expose all 15 tools. The test count in `test_tools.py` grows from 9 to 15 after this step.

### Step 4.2 — Implement agents/blindspot_agent.py

Build the `BlindspotAgent`. Its `run(event_ids)` method calls `detect_blindspot` for each event and stores a record in `blindspot_reports` for every event where a blind spot is detected. The agent must handle events with insufficient coverage data (fewer than 3 articles) without marking them as blind spots.

### Step 4.3 — Implement agents/summary_agent.py

Build the `SummaryAgent`. Its `run(event_id, article_ids)` method calls `extract_facts` to retrieve shared facts, then uses Gemini to write a neutral, single-paragraph Arabic summary based only on those facts. The prompt must explicitly prohibit politically framed language.

### Step 4.4 — Implement agents/recommendation_agent.py

Build the `RecommendationAgent`. Its `run(article_ids, section)` method retrieves the bias label for each article and calls `vector_recommend` to find semantically similar articles with a different label. It processes a maximum of 10 articles per run to control API usage.

### Step 4.5 — Implement agents/graph.py

Build the LangGraph pipeline. Define a `NewsState` TypedDict — this name must be used consistently across all files; do not use `PipelineState` — that carries all shared data between agents: `section`, `article_ids`, `cluster_ids`, `bias_results`, `blindspots`, `summaries`, `recommendations`, `stats`, and `errors`.

Define each agent as a graph node. Connect the nodes in sequence. Define a `run_pipeline(section)` function that initializes the state and executes the full graph for a given section. After the pipeline completes, serialize the results and store them in Redis under the key `results:{section}` with a TTL of 6 hours. This cache is what the FastAPI layer reads from — the API must never query the database directly on every request.

### Step 4.6 — Integration Test

Create `tests/test_pipeline.py` with a full end-to-end pipeline test for the `libya` section. The test must verify that the final state object contains non-empty lists for article IDs, cluster IDs, summaries, and recommendations.

**Phase 4 Success Criteria:** `tests/test_pipeline.py` passes. The pipeline completes without errors for all three sections. All 15 MCP tools pass their tests.

---

## Phase 4.5 — Canonical MCP Architecture Migration

> **Naming note — read carefully.** This entire section is "Phase 4.5", a full migration phase with its own seven sub-steps (4.5.1 through 4.5.7, plus 4.5.5b inside 4.5.5). Do not confuse "Phase 4.5" with the historical "Step 4.5" that appears earlier in this document inside Phase 4 (the step that implemented `agents/graph.py` — already completed). Every reference to "Step 4.5.X" in the sub-sections below belongs to Phase 4.5, not Phase 4. When `plan.md` refers to "Step 4.5.5b — Update agents/graph.py", that is a sub-step of Phase 4.5 that amends the graph — it is a different deliverable from the original Phase 4 Step 4.5 which first created it.

**Goal:** Refactor the system to match the canonical MCP pattern described in `blueprint.md`. The MCP server becomes pure data-access and external-API only. All LLM calls move into the agents, where they were intended to live per Anthropic's MCP specification.

**Rationale:** See `docs/decisions/ADR-001-canonical-mcp-migration.md`. Phase 4 integration exposed three problems that the migration resolves: quota propagation across agents, ReAct misuse on deterministic tasks, and divergence from the canonical MCP pattern.

**Important:** Do not retroactively rewrite the Phase 0 through Phase 4 summaries. Those phases happened as documented. Phase 4.5's own `summary.md` is the authoritative record of what was changed and why.

**Multi-session execution protocol:** Phase 4.5 consists of seven sub-steps (4.5.1 through 4.5.7, with 4.5.5b as a sub-sub-step of 4.5.5) that will almost certainly span multiple working sessions. Per Rule 4.1.1 in `agent.md`, a living document at `summaries/phase_4_5/progress_log.md` tracks sub-step completion, deviations, issues, and verification results. The AI coding agent must:

- **At the start of every session on Phase 4.5:** read `progress_log.md` before any code work, and confirm which sub-step is next.
- **At the end of every session:** append a new entry to `progress_log.md` documenting what was done, before closing the session.
- **Do not write `summaries/phase_4_5/summary.md` until Step 4.5.7** (Self-Audit). The final `summary.md` consolidates the progress log entries into the eight-section template from Rule 4.1.

This protocol prevents context loss between Cursor sessions and provides a granular audit trail valuable for the thesis.

### Step 4.5.1 — Archive and Decision Record

Before touching any source file, preserve the pre-migration documentation as historical evidence:

1. Copy the pre-migration `readme.md`, `blueprint.md`, `plan.md`, and `agent.md` into `docs/archive/pre_canonical_mcp/`. These files must remain read-only reference material for the thesis.
2. Confirm `docs/decisions/ADR-001-canonical-mcp-migration.md` is present and describes the decision, context, alternatives considered, and consequences.
3. Update `readme.md`, `blueprint.md`, `plan.md` (this file), and `agent.md` to reflect the canonical architecture. **This step is a prerequisite for all Phase 4.5 code changes per Rule 1.1** — the AI coding agent must not write code that contradicts the architectural documents.

### Step 4.5.2 — Environment Bootstrap and LLM Client

Create `config/env_bootstrap.py` implementing a single `bootstrap_env()` function that:

1. Pops conflicting shell-level keys (`GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`) from `os.environ`.
2. Loads `.env` with `override=True` so `.env` values always win over shell values.
3. Validates that all required keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `DATABASE_URL`, `REDIS_URL`, `MCP_SERVER_URL`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `GROQ_MODEL`) are present; raise `RuntimeError` listing missing keys if any are empty.
4. Re-sets `GOOGLE_API_KEY = GEMINI_API_KEY` so any SDK that auto-detects `GOOGLE_API_KEY` uses the project's dedicated key.
5. Is idempotent (a module-level flag prevents repeated execution).

Create `agents/llm_client.py` implementing three public functions:

- `gemini_generate_with_fallback(prompt, task_type, max_tokens, temperature) -> dict` — attempts each model in the task-specific chain (`bias`, `entities`, `facts`, `summary`, `assessment`, `general`) on 429 / quota errors. Returns `{"text": str, "model_used": str, "attempts": int}` on success or `{"error": str, "attempted": [...]}` when the chain is exhausted.
- `gemini_embed(text) -> dict` — single call to `text-embedding-004`. Returns `{"embedding": list[float], "dimension": 768}` on success.
- `groq_generate(prompt, model, max_tokens, temperature) -> dict` — single Groq call with throttling. Returns `{"text": str, "model_used": str}` on success.

All three functions must enforce:

- Gemini: `asyncio.Semaphore(4)` for concurrency; 250 ms minimum interval between calls.
- Groq: `asyncio.Semaphore(2)` for concurrency; 2.1 s minimum interval between calls (30 RPM free-tier ceiling).
- Truncation of input text to 3,000 characters (Rule 2.9).

Update `agents/base.py` to expose three new methods on `MCPAgent` that delegate to `agents/llm_client.py`: `call_gemini(prompt, task_type, ...)`, `call_gemini_embedding(text)`, and `call_groq(prompt, model, ...)`. The legacy `think()` and `run_react()` methods may remain on the class for reference but are no longer used by any refactored agent.

Add `from config.env_bootstrap import bootstrap_env; bootstrap_env()` at the top of `mcp_server/server.py`, `api/main.py`, `scheduler.py`, and `tests/conftest.py` (create `conftest.py` if it does not yet exist) **before** any `google.genai` or `groq` import.

### Step 4.5.3 — Add New Pure MCP Tools to the Server

Add the following pure (no-LLM) tools to `mcp_server/server.py`. Each tool follows Rule 2.2 (async + `asyncio.to_thread()` for psycopg2) and Rule 2.4 (returns structured error, never raises).

- `get_articles(ids: list[int]) -> dict` — `SELECT id, title, content, url, entities, embedding, published_at, section FROM articles WHERE id = ANY(%s)`. Returns `{"articles": [...], "count": int}`.
- `get_articles_for_event(event_id: int) -> dict` — left-joins `article_events`, `articles`, and `bias_scores` for the given event. Returns `{"articles": [...]}` where each article includes title, URL, source-derived domain, label, confidence, and framing when a bias row exists.
- `insert_event(section, representative_article_id, title, created_at) -> dict` — returns `{"event_id": int}`.
- `link_article_event(event_id: int, article_id: int, relevance_score: float) -> dict` — `ON CONFLICT DO NOTHING`; returns `{"linked": bool}`.
- `update_event_summary(event_id: int, neutral_summary: str, bias_assessment: str) -> dict` — uses `COALESCE(NULLIF(%s, ''), existing_column)` for both fields; returns `{"updated": bool}`. Short-circuits before touching the DB when both inputs are empty.
- `get_event_article_count(event_id: int) -> dict` — returns `{"count": int}`.
- `insert_bias_score(article_id, score, label, confidence, framing) -> dict` — `ON CONFLICT (article_id) DO NOTHING`; returns `{"inserted": bool, "article_id": int}`.
- `insert_blindspot_report(event_id: int, coverage_stats: dict, missing_perspectives: list[str]) -> dict` — uses `INSERT ... SELECT ... WHERE NOT EXISTS`; returns `{"inserted": bool}`.

Keep the following pre-existing tools unchanged: `fetch_gdelt`, `scrape_article`, `store_article`, `find_similar`, `vector_recommend`, `get_coverage_stats`, `detect_blindspot`, `get_source_bias`, `get_user_profile`, `cache_get`, `cache_set`.

### Step 4.5.4 — Remove LLM-Embedded Tools from the Server

Delete the following tools from `mcp_server/server.py` along with their prompts and provider clients:

- `check_relevance`
- `get_embedding`
- `extract_entities`
- `classify_bias`
- `extract_facts`

After deletion, the MCP server must have **zero** imports of `google.genai`, `google.generativeai`, or `groq`. Verify by running `grep -rE "genai|Groq|gemini" mcp_server/` — this must return no matches. The server now exposes **19 pure tools**.

Delete the corresponding test cases from `tests/test_tools.py` (the 5 removed tools). A new test must be written in Step 4.5.6 for each of the 8 newly added tools — the final tool-test count should be at least 19, one per tool.

### Step 4.5.5 — Rewrite the Six Agents

Rewrite each agent to follow the canonical pattern: data via `call_tool()`, LLM via `call_gemini()` / `call_groq()` / `call_gemini_embedding()`, prompts owned by the agent. Remove all direct `psycopg2` imports from agent files — every DB operation must go through an MCP tool. Recommended order (simplest to most complex):

1. **`agents/clustering_agent.py`** — replace direct `psycopg2` DB reads/writes with `get_articles`, `insert_event`, `link_article_event`. Entity-overlap logic remains in Python. No LLM calls.

2. **`agents/blindspot_agent.py`** — remove the ReAct loop. Deterministic loop: for each event, call `detect_blindspot`, then `insert_blindspot_report` when `has_blindspot` is true. No LLM calls. Remove `_fallback_decide` and the `think()`-based orchestration.

3. **`agents/recommendation_agent.py`** — remove the ReAct loop. Deterministic loop: for each article, `cache_get` → `vector_recommend` → `cache_set`. No LLM calls.

4. **`agents/ingestion_agent.py`** — already deterministic. Replace the direct use of the deleted MCP tools `check_relevance`, `extract_entities`, `get_embedding` with `self.call_groq()` and `self.call_gemini()` / `self.call_gemini_embedding()`. Relevance prompt, entity-extraction prompt, and embedding logic move into this file as module-level string templates and helper methods.

5. **`agents/bias_agent.py`** — remove the ReAct loop. Deterministic loop: partition articles into comparative clusters and singletons, fetch payloads via `get_articles`, build the comparative-bias prompt in the agent, call `self.call_gemini(task_type="bias")`, parse JSON, reject results where `label == "neutral"` and `confidence < 0.2` (API-degradation guard), persist via `insert_bias_score`. Implement `classify_single(text)` as a thin wrapper that builds a single-article prompt and calls `self.call_gemini()` directly for Phase 6 evaluation.

6. **`agents/summary_agent.py`** — remove the ReAct loop. Deterministic loop: for each event, call `get_articles_for_event`, build three prompts (facts, neutral summary, bias assessment), call `self.call_gemini()` for each with the appropriate `task_type`, and persist non-empty outputs via `update_event_summary`. The Summary Agent must not write anything to the `events` row unless at least one of `neutral_summary` or `bias_assessment` is non-empty after stripping whitespace. The `update_event_summary` tool already enforces the `COALESCE(NULLIF(...))` guard, so stored rows are never overwritten with empty strings.

For every agent, Rule 2.7 (mandatory caching) applies to every LLM call. Cache keys are deterministic md5 hashes of the prompt content plus the task type. Cache `get` and `set` happen via the MCP tools `cache_get` / `cache_set` — never via direct Redis access.

### Step 4.5.5b — Update agents/graph.py

`agents/graph.py` does not require structural changes — the `NewsState` TypedDict, the six pipeline nodes, the graph edges, `run_pipeline(section)`, and the Redis result-caching logic all remain unchanged. Each node calls `agent.run(...)` and does not depend on the agent's internal implementation, so replacing ReAct with deterministic dispatch inside each agent is transparent to the graph.

Three small updates are required:

1. **Replace `load_dotenv()` with `bootstrap_env()` at the top of the file.** Remove the `from dotenv import load_dotenv` import and the `load_dotenv()` call. Add `from config.env_bootstrap import bootstrap_env` and call `bootstrap_env()` before any project-level import. This ensures the environment is initialized correctly for all the agent modules that `graph.py` imports (each agent in turn imports `agents/llm_client.py`, which requires the bootstrap).

2. **Normalize the Ingestion error handling in `_ingestion_node`.** The current implementation treats `IngestionAgent.run()`'s `errors` field as an integer count and converts it to a single summary string. After Phase 4.5, the rewritten `IngestionAgent` returns `errors` as a `list[str]` — the same shape every other agent uses. Update the node to iterate over the list:

   ```python
   new_errors = list(state["errors"])
   for e in result.get("errors", []):
       new_errors.append(f"[ingestion] {e}")
   ```

   This unifies error handling across all six nodes and simplifies the ingestion-stats dictionary (the `"errors"` count can still be derived as `len(result.get("errors", []))` if needed for logging).

3. **Verify the cache_set call still works.** The Redis result-storage block at the end of `run_pipeline` uses `MCPAgent().call_tool("cache_set", ...)`. Because `cache_set` is kept unchanged in Phase 4.5.3 (it is already a pure Redis tool), this block requires no modification. Confirm the pipeline integration test still observes `results:{section}` in Redis after a successful run.

Do not change `NewsState`, do not change the graph edges, do not change any other node's internal logic. Keeping `graph.py` structurally stable is what allows the Phase 5 FastAPI layer (which reads `results:{section}` from Redis) to continue working without modification after the migration.

### Step 4.5.6 — Rewrite Tests

Rebuild `tests/test_tools.py` with one test per tool for all 19 pure tools, using a live MCP client session (Rule 3.2). No mocking of PostgreSQL or Redis (Rule 3.3) — real infrastructure only.

Rewrite the agent tests in `tests/test_agents.py` to reflect the new architecture. LLM calls inside agents may be mocked with `# MOCK` markers (Rule 3.3 exception) to avoid exhausting free-tier quotas during repeated CI runs. Every mock must be removed or made optional before Phase 6's final evaluation run.

Update `tests/test_pipeline.py` if the graph signature or `NewsState` fields changed during agent rewrites.

### Step 4.5.7 — Self-Audit and Phase Closure

Run the following checks before writing the Phase 4.5 `summary.md`:

1. `grep -rE "genai|Groq|gemini|GROQ_API_KEY|GEMINI_API_KEY" mcp_server/` → zero matches. The server has no LLM awareness.
2. `grep -rE "import psycopg2" agents/` → zero matches (except in `agents/base.py` if any legacy code remains there — the preferred outcome is zero matches in the entire `agents/` directory).
3. `grep -l "load_dotenv()" mcp_server/ api/ agents/ scheduler.py 2>/dev/null` → zero matches. Every entry point uses `bootstrap_env()` instead.
4. `agents/graph.py` imports `bootstrap_env` from `config.env_bootstrap` and calls it before any `agents.*` import. The `_ingestion_node` function treats `errors` as a list (not an int count).
5. `pytest tests/` → all tests pass, including `tests/test_pipeline.py` which exercises the full graph end-to-end.
6. Live pipeline run on the `libya` section completes without unhandled exceptions and stores a value at `results:libya` in Redis.
7. At least one event in the `events` table has non-empty `summary` and `bias_assessment` after the run.
8. A `bias_scores` query shows the distribution is not degenerate (not 100% `neutral` with `confidence = 0`). The confidence gate from Step 4.5.5 (point 5) is doing its job.

Generate `summaries/phase_4_5/summary.md` per Rule 4.1 by **consolidating** the entries in `summaries/phase_4_5/progress_log.md` into the eight-section template. Section C (Logic Changes and Deviations) must reference `docs/decisions/ADR-001-canonical-mcp-migration.md` and note that the deviations from the Phase 0–4 architecture are deliberate per that ADR, not unplanned drift. The `progress_log.md` is preserved alongside the final `summary.md` — it is not deleted.

**Phase 4.5 Success Criteria:**

- `pytest tests/` passes (all tool, agent, and pipeline tests).
- MCP server exposes exactly 19 tools, all pure.
- No LLM import in `mcp_server/`.
- No direct `psycopg2` import in any agent file.
- `agents/graph.py` uses `bootstrap_env()` and normalized error handling; no structural changes to `NewsState` or graph edges.
- A live `libya` pipeline run produces non-empty summaries for at least one event.
- `bias_scores` distribution for the live run is non-degenerate.
- `summaries/phase_4_5/progress_log.md` is present with a COMPLETE entry for every sub-step 4.5.1 through 4.5.7.
- `summaries/phase_4_5/summary.md` is present and documents every change per Rule 4.1, consolidated from the progress log.

---

## Phase 5 — FastAPI Backend and Streamlit Dashboard

**Goal:** The analysis results are accessible via REST API and displayed in a visual dashboard that a non-technical person can understand.

### Step 5.1 — Implement api/main.py

Build the FastAPI application. Add `from config.env_bootstrap import bootstrap_env; bootstrap_env()` at the top before any other imports. Implement the following endpoints as a minimum: `GET /results/{section}`, `GET /blindspots/{section}`, `GET /recommendations/{article_id}`, and `GET /health`.

All read endpoints must follow this strategy: check Redis for a cached result first, and only query PostgreSQL if the cache is empty. This prevents every dashboard page load from triggering a full database query. The `run_pipeline` function already stores results in Redis at the end of each pipeline run — the API simply reads from that cache. If the cache is empty (pipeline has not run yet), the endpoint returns a `{"status": "pending"}` response. No endpoint triggers pipeline execution.

### Step 5.2 — Implement frontend/app.py

Build the Streamlit dashboard. It must include: a section selector (Middle East, Libya, World) rendered as tabs, a bias distribution visualization per event, an expandable neutral summary per event, a blind-spot alert panel, and a recommendation panel for filter bubble bursting. The UI must be readable by a non-technical Arabic-speaking user. All data is fetched from the FastAPI backend, not directly from the database or Redis.

### Step 5.3 — Manual UX Validation

Ask at least one person unfamiliar with the project to use the dashboard without explanation. They must be able to identify what an article's bias label means, what a blind spot is, and how to find recommended articles. If they cannot, revise the UI before proceeding.

**Phase 5 Success Criteria:** A non-technical person can navigate the dashboard and interpret results without assistance.

---

## Phase 6 — Evaluation, Scheduler, and Documentation

**Goal:** The system is evaluated against the annotated dataset, the scheduler is operational, and the project is fully documented.

### Step 6.1 — Build evaluation/dataset.json

Manually annotate 60 Arabic articles, 12 from each of the five bias labels. For each article, record the title, content (or URL), source, human-assigned label, and annotator confidence. This is a research artifact and must be created with care. Articles with annotator confidence below `high` should be excluded from the F1-Score calculation and noted separately.

Flag articles where the Bias Agent returns `confidence < 0.6` for manual review. These low-confidence predictions represent the model's uncertainty and are academically significant — they demonstrate the inherent difficulty of bias detection in Arabic media.

### Step 6.2 — Implement evaluation/evaluate.py

Build the evaluation script. It must load the dataset, run each article through `BiasAgent.classify_single(text)` (which calls Gemini directly via the client-side fallback chain), compare predictions to human labels using `sklearn`, and print a full classification report along with the weighted F1-Score. The script must produce results that are directly citable in the thesis. The target F1-Score for Arabic content is ≥ 0.65 — lower than English-language benchmarks due to the limited Arabic training data in the models used. This gap is itself an academically significant finding and should be documented as such.

Before the evaluation run, remove any `# MOCK` markers introduced in Phase 4.5.6 from the Bias Agent path, per Rule 3.3 ("Mocks must be removed or made optional before the final evaluation run in Phase 6").

### Step 6.3 — Implement scheduler.py

Build the scheduler using `asyncio`. Add `from config.env_bootstrap import bootstrap_env; bootstrap_env()` at the top. It must read the `update_every_hours` value from each section's configuration in `config/sections.py` and trigger `run_pipeline` on the schedule specific to that section — independently per section. It must log each run's start time, end time, and summary statistics. It must not crash on pipeline errors — it logs them and continues to the next scheduled run.

### Step 6.4 — Final Testing Pass

Run the full test suite (`pytest tests/`) and verify all tests pass. Run the pipeline for all three sections and verify the database is populated. Run the evaluation script and record the F1-Score. Verify the Streamlit dashboard displays correct data.

### Step 6.5 — Generate Final Documentation

Ensure `readme.md` is fully up to date. Ensure all `summary.md` files from each phase are present and accurate — including `summaries/phase_4_5/summary.md`. Update `requirements.txt` to reflect the exact installed versions. Write the final `summary.md` for Phase 6.

**Phase 6 Success Criteria:** `pytest tests/` passes fully. F1-Score on the 60-article dataset is computed and documented. The scheduler runs without errors for 24 hours. All phase `summary.md` files are present, including Phase 4.5.

---

## Summary of Phases

| Phase | Focus | Key Deliverable |
|-------|-------|-----------------|
| 0 | Environment Setup | Running infra, project structure, seeded DB |
| 1 | MCP Server (9 tools, original architecture with LLM-embedded tools) | All core tools tested and passing |
| 2 | Ingestion Agent | Articles stored from GDELT with entities and `published_at` |
| 3 | Clustering + Bias Agents | Events created, bias scores stored |
| 4 | All Agents + 6 Advanced Tools (15 total, LLM-embedded) | Full pipeline end-to-end passing (original architecture) |
| **4.5** | **Canonical MCP Migration** | **MCP server = 19 pure tools; all LLM calls moved to agents; ReAct removed from deterministic agents; `config/env_bootstrap.py` and `agents/llm_client.py` introduced** |
| 5 | FastAPI + Streamlit | Working dashboard with real data |
| 6 | Evaluation + Scheduler + Docs | F1-Score computed, system fully documented |
