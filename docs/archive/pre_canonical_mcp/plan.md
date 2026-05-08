# plan.md — Veritas Agent: Development Roadmap

> **Purpose:** This document is the step-by-step implementation guide for building Veritas Agent. It is divided into six phases, each with granular, actionable steps. Follow phases in order. Do not begin a phase until all steps of the previous phase pass their success criteria. After completing each phase, generate a `summary.md` file as specified in `agent.md`.

---

## Development Principles

Before starting, internalize these principles for the entire build process:

- Build modularly. Each component must work and be tested before the next one depends on it.
- The MCP server is built before the agents. Agents are built before the pipeline. The pipeline is built before the API. The API is built before the frontend.
- No component should bypass the MCP layer to access infrastructure directly (except the MCP server itself and database schema setup).
- Every phase ends with a testable, demonstrable result — not just code that exists.

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
- The agent exposes a `classify_single(text)` helper method used exclusively by the Phase 6 evaluation script. This method wraps `classify_bias` in single-article fallback mode and accepts raw text rather than a database article dict. Note: `classify_single()` is a Phase 3.2 deliverable and is NOT implemented in the current change.

**Why comparative:** Sending all articles that cover the same event to Gemini simultaneously allows the model to detect relative framing — what each source emphasizes versus ignores — producing significantly more accurate and meaningful bias labels than context-free single-article classification.

### Step 3.3 — Write Tests for Both Agents

Add tests in `tests/test_agents.py` for both the Clustering Agent and the Bias Agent. The Clustering Agent test must verify that at least one event is created from a set of seeded articles. The Bias Agent test must verify that a bias score record is created for each processed article.

**Phase 3 Success Criteria:** Manual review of 10 clusters from the Libya section shows grouping accuracy above 70%. The `bias_scores` table is populated with valid labels for all processed articles.

---

## Phase 4 — Remaining Agents and Advanced MCP Tools (Days 24–35)

**Goal:** All six agents are operational. The six advanced MCP tools are implemented. A complete end-to-end pipeline run succeeds.

### Step 4.1 — Implement Advanced MCP Tools

Add the six Phase 2 tools to `mcp_server/server.py`: `get_source_bias`, `get_coverage_stats`, `detect_blindspot`, `extract_facts`, `vector_recommend`, and `get_user_profile`. Write a test for each new tool in `tests/test_tools.py`. The server must now expose all 15 tools. The test count in `test_tools.py` grows from 9 to 15 after this step.

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

## Phase 5 — FastAPI Backend and Streamlit Dashboard (Days 36–46)

**Goal:** The analysis results are accessible via REST API and displayed in a visual dashboard that a non-technical person can understand.

### Step 5.1 — Implement api/main.py

Build the FastAPI application. Implement the following endpoints as a minimum: `GET /results/{section}`, `GET /blindspots/{section}`, `GET /recommendations/{article_id}`, and `GET /health`.

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

Flag articles where `classify_bias` returns `confidence < 0.6` for manual review. These low-confidence predictions represent the model's uncertainty and are academically significant — they demonstrate the inherent difficulty of bias detection in Arabic media.

### Step 6.2 — Implement evaluation/evaluate.py

Build the evaluation script. It must load the dataset, run each article through the Bias Agent's `classify_single` method, compare predictions to human labels using `sklearn`, and print a full classification report along with the weighted F1-Score. The script must produce results that are directly citable in the thesis. The target F1-Score for Arabic content is ≥ 0.65 — lower than English-language benchmarks due to the limited Arabic training data in the models used. This gap is itself an academically significant finding and should be documented as such.

### Step 6.3 — Implement scheduler.py

Build the scheduler using `asyncio`. It must read the `update_every_hours` value from each section's configuration in `config/sections.py` and trigger `run_pipeline` on the schedule specific to that section — independently per section. It must log each run's start time, end time, and summary statistics. It must not crash on pipeline errors — it logs them and continues to the next scheduled run.

### Step 6.4 — Final Testing Pass

Run the full test suite (`pytest tests/`) and verify all tests pass. Run the pipeline for all three sections and verify the database is populated. Run the evaluation script and record the F1-Score. Verify the Streamlit dashboard displays correct data.

### Step 6.5 — Generate Final Documentation

Ensure `readme.md` is fully up to date. Ensure all `summary.md` files from each phase are present and accurate. Update `requirements.txt` to reflect the exact installed versions. Write the final `summary.md` for Phase 6.

**Phase 6 Success Criteria:** `pytest tests/` passes fully. F1-Score on the 60-article dataset is computed and documented. The scheduler runs without errors for 24 hours. All phase `summary.md` files are present.

---

## Summary of Phases

| Phase | Focus | Key Deliverable |
|-------|-------|-----------------|
| 0 | Environment Setup | Running infra, project structure, seeded DB |
| 1 | MCP Server (9 tools) | All core tools tested and passing |
| 2 | Ingestion Agent | Articles stored from GDELT with entities and published_at |
| 3 | Clustering + Bias Agents | Events created, bias scores stored |
| 4 | All Agents + 6 Advanced Tools | Full pipeline end-to-end passing |
| 5 | FastAPI + Streamlit | Working dashboard with real data |
| 6 | Evaluation + Scheduler + Docs | F1-Score computed, system fully documented |
