# blueprint.md — Veritas Agent: System Architecture

> **Purpose:** This document defines the high-level design of the Veritas Agent system. It describes every core component, how data flows from ingestion to final output, and how the Model Context Protocol (MCP) connects all agents to their tools. This file is the authoritative architectural reference for the entire codebase.
>
> **Architecture version:** Canonical MCP (post Phase 4.5 migration). See `docs/decisions/ADR-001-canonical-mcp-migration.md` for the migration rationale and `docs/archive/pre_canonical_mcp/` for the preserved original architecture.

---

## 1. System Overview

Veritas Agent is an autonomous Multi-Agent System (MAS) designed to ingest Arabic-language news from public APIs, classify political bias, detect media blind spots, generate neutral summaries, and recommend articles that expose the reader to alternative perspectives. The system is designed as a graduation research project with three original academic contributions:

- Applying the **canonical MCP pattern** as a unifying tool-access layer across all agents — the MCP server exposes pure data and infrastructure tools; agents contain all reasoning and LLM calls.
- Using LangGraph to build a stateful, inspectable multi-agent pipeline.
- Evaluating LLM bias-classification performance specifically on Arabic media content.

The system serves three fixed news sections: **Middle East**, **Libya**, and **World**, each with its own data sources, query configuration, and update schedule. Each section has an independent `update_every_hours` value defined in `config/sections.py`. The scheduler reads this value per section and triggers the pipeline on the schedule specific to that section — Middle East and World every 6 hours, Libya every 4 hours.

---

## 2. Core Architectural Layers

The system is composed of five distinct layers. Each layer has a single, well-defined responsibility, and communication between layers follows strict boundaries.

### Layer 1 — Scheduling Layer

A background scheduler (`scheduler.py`) triggers the full analysis pipeline on a per-section schedule. It reads the `update_every_hours` value from each section's configuration in `config/sections.py` and initiates the LangGraph pipeline accordingly. It acts as the sole entry point for automated execution. It does not contain any business logic.

### Layer 2 — Orchestration Layer (LangGraph Pipeline)

The pipeline (`agents/graph.py`) is a directed stateful graph built with LangGraph. It chains six specialized agents in sequence. Each agent is a node in the graph. The graph maintains a shared `NewsState` object that accumulates results as data flows from one agent to the next. This layer is responsible for controlling execution order, passing outputs between agents, and handling partial failures gracefully.

### Layer 3 — Agent Layer (The Reasoning Plane)

Six specialized agents each perform one defined function. Every agent inherits from a shared `MCPAgent` base class (`agents/base.py`). The `MCPAgent` base class provides three capabilities:

1. `call_tool(name, args)` — dispatches an MCP tool over Streamable HTTP. This is the only channel the agent uses to access PostgreSQL, Redis, GDELT, or any external infrastructure.
2. `call_gemini(prompt, task_type, ...)` — calls Gemini directly from the client for text generation, with a centralized fallback chain across multiple Gemini models.
3. `call_groq(prompt, ...)` — calls Groq directly from the client for fast, low-stakes text generation (relevance checking).

**Where intelligence lives:** each agent that performs reasoning owns its domain-specific prompts, constructs them from data fetched via MCP tools, calls the LLM directly, parses the structured output, and writes the result back through MCP tools. Three agents (Clustering, Blindspot, Recommendation) are fully deterministic and call no LLM at all — their logic is statistical or algorithmic.

This design matches the canonical MCP pattern as documented in Anthropic's MCP specification and as implemented in reference servers (GitHub MCP, Asana MCP, Filesystem MCP): servers expose capabilities, clients contain the reasoning that decides when and how to use them.

### Layer 4 — MCP Tool Layer (The Data Plane)

A single FastMCP server (`mcp_server/server.py`) runs as an independent process on port 8000. It exposes **19 pure tools** — all data-access, external-API, or cache operations. **No tool calls any LLM.** Any agent can call any tool by name. The server's sole responsibilities are making HTTP requests to external APIs, querying PostgreSQL, and reading/writing Redis.

This is the canonical-MCP boundary: the server is a reusable capability surface that any MCP-compatible client (including Claude Desktop, Cursor, or another LangGraph application) could drive.

### Layer 5 — Infrastructure Layer

Composed of three infrastructure components:

- **PostgreSQL 16 + pgvector** — relational database with vector-search capability for storing articles, events, bias scores, and blind-spot reports.
- **Redis** — in-memory cache for LLM responses, pipeline results, and user reading profiles.
- **GDELT DOC API** — the external news source. Free, unlimited, Arabic-capable.

---

## 3. The Six Agents

| Agent | File | Calls LLM? | Responsibility |
|---|---|---|---|
| Ingestion Agent | `agents/ingestion_agent.py` | **Yes** (Groq + Gemini) | Fetches articles via `fetch_gdelt`, checks relevance by calling Groq directly with its own prompt, scrapes full content via `scrape_article`, extracts named entities by calling Gemini directly, generates embeddings by calling Gemini's embedding API directly, and stores results via `store_article`. |
| Clustering Agent | `agents/clustering_agent.py` | **No** | Groups articles that cover the same real-world event using three deterministic conditions: time window ≤ 72h, cosine similarity ≥ 0.82 (via `find_similar`), shared entity count ≥ 2. Creates events via `insert_event` and links membership via `link_article_event`. |
| Bias Agent | `agents/bias_agent.py` | **Yes** (Gemini) | For each event cluster, fetches all member articles via `get_articles`, builds a comparative-bias prompt, calls Gemini directly to classify each article relative to the others, parses the JSON response, and persists results via `insert_bias_score`. Singleton articles are classified individually as a fallback. |
| Blindspot Agent | `agents/blindspot_agent.py` | **No** | Purely statistical. For each event cluster, uses `get_coverage_stats` + `detect_blindspot` to identify bias perspectives represented below 15% of the per-event coverage average, then stores findings via `insert_blindspot_report`. |
| Summary Agent | `agents/summary_agent.py` | **Yes** (Gemini) | For each event, fetches shared content via `get_articles_for_event`, calls Gemini directly to (a) extract facts shared across ≥ 2 sources, (b) write a neutral Arabic paragraph from those facts, and (c) write a comparative framing analysis. Persists both outputs via `update_event_summary`. |
| Recommendation Agent | `agents/recommendation_agent.py` | **No** | Uses `vector_recommend` to surface semantically similar articles with a different bias label. Caches per-article results via `cache_get` / `cache_set`. |

---

## 4. The MCP Server — 19 Pure Tools

The MCP server exposes 19 tools, all of which perform only data access, external API calls, or Redis operations. **No tool invokes Gemini, Groq, or any other LLM.** This is the canonical-MCP boundary enforced in Phase 4.5.

### Group A — External APIs (2 tools)

| Tool | Purpose |
|---|---|
| `fetch_gdelt` | Queries the GDELT DOC API for Arabic news articles by section. Pure HTTP call. |
| `scrape_article` | Fetches full article content using three fallback layers: (1) plain HTTP + BeautifulSoup, (2) Playwright headless browser for JavaScript-rendered pages, (3) title-only fallback. On Layer 3 failure, returns `{"success": false, "content": null, "method": "failed"}` — never raises. |

### Group B — Article Operations (3 tools)

| Tool | Purpose |
|---|---|
| `store_article` | Inserts a new article into PostgreSQL using `ON CONFLICT (url) DO NOTHING`. The embedding, entities, and all fields are passed in by the caller. `DO UPDATE` is never used. |
| `get_articles` | Fetches rows from `articles` by a list of IDs. Returns title, content, URL, entities, embedding, `published_at`, and section. |
| `get_articles_for_event` | Fetches all articles linked to an event via `article_events`, joined with `bias_scores` when present. Returns a list of dicts with title, URL, source-derived domain, bias label, confidence, and framing. |

### Group C — Event Operations (4 tools)

| Tool | Purpose |
|---|---|
| `insert_event` | Creates a new row in the `events` table for a discovered cluster. Returns the new `event_id`. |
| `link_article_event` | Inserts a row into `article_events` using `ON CONFLICT DO NOTHING`. Idempotent. |
| `update_event_summary` | Writes `neutral_summary` and `bias_assessment` to the `events` row. Uses `COALESCE(NULLIF(%s, ''), existing_column)` to preserve prior non-empty values when the new value is empty — prevents API-failure outputs from overwriting good data. |
| `get_event_article_count` | Returns the count of articles linked to a given event. Helper used by agents to size batches. |

### Group D — Bias Storage (1 tool)

| Tool | Purpose |
|---|---|
| `insert_bias_score` | Inserts a row into `bias_scores` using `ON CONFLICT (article_id) DO NOTHING` per Rule 2.8. Returns `{"inserted": bool, "article_id": int}`. |

### Group E — Analysis & Vector Search (4 tools)

| Tool | Purpose |
|---|---|
| `find_similar` | pgvector cosine-similarity search within a 72-hour time window and a section filter. All three filters enforced at the SQL level for efficiency. |
| `vector_recommend` | pgvector search filtered to return articles whose `bias_scores.label` differs from the source article's label — the filter-bubble-breaker. Pure SQL + vector math, no LLM. |
| `get_coverage_stats` | Aggregates article count per bias label for a given event. Pure SQL aggregation. |
| `detect_blindspot` | Applies the 15%-of-average threshold to coverage stats and returns the list of underrepresented labels. Pure arithmetic — no LLM. Returns `has_blindspot: false` if the event has fewer than 3 classified articles (insufficient coverage). |

### Group F — Blindspot Storage (1 tool)

| Tool | Purpose |
|---|---|
| `insert_blindspot_report` | Inserts a row into `blindspot_reports` with a `WHERE NOT EXISTS` guard on `event_id` (idempotent across pipeline re-runs). |

### Group G — Source & User Lookup (2 tools)

| Tool | Purpose |
|---|---|
| `get_source_bias` | Retrieves a pre-labeled bias for a known source domain from the `sources` table. |
| `get_user_profile` | Retrieves a user's reading bias profile (dominant label + per-label read counts) from Redis. |

### Group H — Redis Cache (2 tools)

| Tool | Purpose |
|---|---|
| `cache_get` | Read a string value by key. Returns `{"value": str \| null}`. |
| `cache_set` | Write a string value with a TTL in seconds. Returns `{"success": bool}`. |

---

## 5. Data Flow — End to End

The following describes the complete lifecycle of data in the system. Note how LLM calls now occur **inside the agents**, while MCP tool calls flow to the server for data operations only.

```
[Scheduler] — triggers per section based on update_every_hours in config/sections.py
     │
     ▼
[LangGraph Pipeline] — runs agents in sequence with shared NewsState
     │
     ├── [Ingestion Agent]   (LLM calls inside agent)
     │       call_tool("fetch_gdelt")
     │       for each article:
     │         call_groq(relevance_prompt)            ← direct Groq call
     │         call_tool("scrape_article")
     │         call_gemini(entity_prompt)              ← direct Gemini call
     │         call_gemini_embedding(text)             ← direct Gemini embedding
     │         call_tool("store_article")
     │       Output: list of stored article IDs
     │
     ├── [Clustering Agent]   (NO LLM — deterministic)
     │       call_tool("get_articles")
     │       call_tool("find_similar")
     │       Python: entity overlap check (≥ 2)
     │       call_tool("insert_event")
     │       call_tool("link_article_event")
     │       Output: event IDs + cluster members
     │
     ├── [Bias Agent]   (LLM calls inside agent)
     │       for each (cluster or singleton):
     │         call_tool("get_articles")               ← fetch payloads
     │         Build comparative bias prompt in agent
     │         call_gemini(bias_prompt)                 ← direct Gemini call
     │         Parse JSON, reject low-conf neutrals
     │         call_tool("insert_bias_score")
     │       Output: bias results per article
     │
     ├── [Blindspot Agent]   (NO LLM — statistical)
     │       for each event:
     │         call_tool("detect_blindspot")            ← server-side arithmetic
     │         call_tool("insert_blindspot_report")
     │       Output: list of events with missing coverage
     │
     ├── [Summary Agent]   (LLM calls inside agent)
     │       for each event:
     │         call_tool("get_articles_for_event")
     │         call_gemini(facts_prompt)                ← direct Gemini call
     │         call_gemini(neutral_summary_prompt)      ← direct Gemini call
     │         call_gemini(assessment_prompt)           ← direct Gemini call
     │         call_tool("update_event_summary")
     │       Output: Arabic neutral summaries per event
     │
     └── [Recommendation Agent]   (NO LLM — vector only)
             for each article (up to 10):
               call_tool("cache_get")                   ← cache check first
               call_tool("vector_recommend")            ← pure pgvector
               call_tool("cache_set")
             Output: recommended article IDs per article
     │
     ▼
[PostgreSQL] — persistent storage for all outputs
     │
     ▼
[Redis Cache] — pipeline results stored under results:{section} with 6-hour TTL
     │
     ▼
[FastAPI Backend] — REST endpoints read from Redis cache first;
                    falls back to PostgreSQL only if cache is empty;
                    returns {"status": "pending"} if neither has data
     │
     ▼
[Streamlit Dashboard] — visual display of results, bias indicators, blind spots
```

---

## 6. MCP Transport Choice — Streamable HTTP

The MCP server uses **Streamable HTTP** transport, not `stdio`. The rationale is architectural: `stdio` would launch a new server subprocess per client connection, resulting in six separate processes for six agents. Streamable HTTP runs a single independent server process that concurrently serves all six agents. This aligns precisely with a Multi-Agent architecture where shared infrastructure is essential.

```
stdio:           1 client  →  1 server subprocess  (scales poorly)
Streamable HTTP: 6 agents  →  1 shared server       (correct for MAS)
```

---

## 7. Database Schema Summary

Six tables in PostgreSQL serve the system's persistent storage needs:

| Table | Role |
|---|---|
| `sources` | Pre-seeded records of known Arabic news sources with their bias labels |
| `articles` | Core content store: title, content, URL, `published_at` timestamp, 768-dim embedding, entities (JSONB), `has_full_content` flag, section |
| `events` | Event clusters discovered by the Clustering Agent. Includes `summary` and `bias_assessment` columns populated by the Summary Agent. |
| `article_events` | Many-to-many join table linking articles to events with a relevance score |
| `bias_scores` | Political score (-1.0 to 1.0), label, confidence, and framing per article. Unique on `article_id`. |
| `blindspot_reports` | Coverage distribution and missing-side analysis per event |

The `articles` table includes a `published_at` timestamp column. This field is required by the Clustering Agent to evaluate the 72-hour time window condition. It is populated during ingestion from the GDELT `seendate` field and must be present for any article to be considered in clustering. Articles missing `published_at` are excluded from the clustering step.

A HNSW index on the `embedding` column enables fast approximate nearest-neighbor search via pgvector. A secondary index on `published_at DESC` supports efficient time-window filtering in the `find_similar` tool.

---

## 8. Bias Label Taxonomy

The system uses five bias labels designed for the Arabic media context. These replace Western-centric labels that do not map accurately to Arab political discourse.

| Label | Meaning |
|---|---|
| `pro_government` | Supports or amplifies the official government/authority position |
| `opposition` | Critiques or counters the government/authority |
| `neutral` | Balanced coverage without a detectable political lean |
| `pan_arab` | Reflects pan-Arab nationalist framing (e.g., Al Jazeera's editorial voice) |
| `western_aligned` | Reflects Western institutional narratives (e.g., France 24 Arabic, RT Arabic) |

---

## 9. LLM Strategy — Client-Side Dispatch with Fallback Chain

**All LLM calls originate from the agents (client side), never from the MCP server.** A centralized `agents/llm_client.py` module provides three entry points that every reasoning agent uses:

- `gemini_generate_with_fallback(prompt, task_type, ...)` — text generation with automatic cascade through a chain of Gemini models.
- `gemini_embed(text)` — 768-dim embedding via `text-embedding-004`.
- `groq_generate(prompt, model, ...)` — fast relevance and classification tasks via Groq/Llama.

### Fallback chains per task

When a Gemini model returns a 429 / quota-exhausted error, the client automatically retries with the next model in the chain. This protects the pipeline from a single-model outage.

| Task type | Primary model | Fallback chain |
|---|---|---|
| `bias` | `gemini-2.0-flash` | → `gemini-2.5-flash` → `gemini-1.5-flash` |
| `entities` | `gemini-2.0-flash` | → `gemini-2.5-flash` → `gemini-1.5-flash` |
| `facts` | `gemini-2.0-flash` | → `gemini-2.5-flash` |
| `summary` | `gemini-2.0-flash` | → `gemini-2.5-flash` → `gemini-1.5-flash` |
| `assessment` | `gemini-2.5-flash` | → `gemini-2.0-flash` |
| relevance (Groq) | `llama-3.3-70b-versatile` | — |

### Rate limiting

Client-side semaphores and minimum-interval throttles prevent concurrent agents from overwhelming either provider:

- Gemini: max 4 concurrent calls, ≥ 250 ms between calls.
- Groq: max 2 concurrent calls, ≥ 2.1 s between calls (30 RPM free-tier ceiling).

### Caching

Redis caching of LLM results is **mandatory** per Rule 2.7. The cache check is performed by the agent before any LLM call, and the cache write happens after a successful parse. Both operations use the MCP tools `cache_get` and `cache_set` — the cache itself remains on the data plane.

### Environment key isolation

A single bootstrap module `config/env_bootstrap.py` is imported at every process entry point (MCP server, FastAPI, scheduler, agent tests) before any SDK import. It pops conflicting shell-level keys (`GOOGLE_API_KEY`, `GOOGLE_GENAI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`), loads `.env` with `override=True`, validates required keys, and re-sets `GOOGLE_API_KEY = GEMINI_API_KEY` so every transitively-imported SDK uses the project's dedicated key.

---

## 10. API and Frontend

The **FastAPI** backend (`api/main.py`) exposes REST endpoints that the dashboard consumes. All read endpoints follow a cache-first strategy: they check Redis for a result stored under `results:{section}` before querying PostgreSQL. The pipeline stores its final output in Redis at the end of every run with a 6-hour TTL. If the cache is empty and the database has no data, the endpoint returns `{"status": "pending"}`. No read endpoint triggers pipeline execution.

The **Streamlit** frontend (`frontend/app.py`) renders the analysis results: bias distribution charts, neutral summaries, blind-spot alerts, and filter-bubble recommendations. It connects to FastAPI only — never directly to the database or Redis.

---

## 11. Evaluation Framework

The system's accuracy is measured against a manually annotated dataset of 60 Arabic articles (`evaluation/dataset.json`), distributed across all five bias labels (12 articles per label). The primary metric is weighted F1-Score computed via `sklearn`. The academic target is F1 ≥ 0.65, chosen to be honest about current LLM limitations on Arabic-language content rather than overclaiming performance. Articles where the model returns `confidence < 0.6` are flagged separately and excluded from the primary F1 calculation to isolate high-confidence predictions as a distinct result set.

The Bias Agent exposes a `classify_single(text)` method used exclusively by `evaluation/evaluate.py`. It constructs a single-article prompt, calls Gemini directly through the fallback chain, parses the response, and returns the classification without touching the database — the same code path production uses, minus the comparative cluster context.

---

## 12. Architecture Evolution

The current architecture is the result of a deliberate migration executed in Phase 4.5. The original design (Phases 0 through 4) embedded LLM calls inside MCP server tools (`classify_bias`, `extract_entities`, `extract_facts`, `check_relevance`, `get_embedding`). During Phase 4 integration, three problems emerged that drove the migration:

1. **API quota propagation:** When any single Gemini model was throttled, every agent that depended on an LLM-embedded tool failed simultaneously. There was no room for per-task fallback because the fallback logic sat inside the server, invisible to the agent.
2. **Misuse of ReAct:** Four agents (Bias, Blindspot, Recommendation, Summary) used ReAct loops for fundamentally deterministic tasks ("for each item in a known queue, call one tool"). Each ReAct step invoked Groq for tool-selection reasoning, consuming ~34K Groq tokens per pipeline run for no semantic benefit.
3. **Divergence from canonical MCP:** The original design did not follow the pattern documented in Anthropic's MCP specification and implemented in reference servers (GitHub MCP, Asana MCP, Filesystem MCP), where servers expose capabilities and clients contain reasoning.

The canonical migration (Phase 4.5) resolved all three issues by relocating LLM calls from the server into the agents, centralizing them in `agents/llm_client.py` with a fallback chain, and replacing ReAct in the four affected agents with deterministic tool dispatch. The original architecture is preserved for reference in `docs/archive/pre_canonical_mcp/`. The full rationale is documented in `docs/decisions/ADR-001-canonical-mcp-migration.md`.

This evolution is an explicit academic deliverable of the project: it demonstrates that production MCP-based systems benefit from aligning with the canonical pattern, and that deviating from it introduces observable operational problems (quota fragility, token waste, loss of reusability).
