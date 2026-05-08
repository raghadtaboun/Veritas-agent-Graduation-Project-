# blueprint.md — Veritas Agent: System Architecture

> **Purpose:** This document defines the high-level design of the Veritas Agent system. It describes every core component, how data flows from ingestion to final output, and how the Model Context Protocol (MCP) connects all agents to their tools. This file is the authoritative architectural reference for the entire codebase.

---

## 1. System Overview

Veritas Agent is an autonomous Multi-Agent System (MAS) designed to ingest Arabic-language news from public APIs, classify political bias, detect media blind spots, generate neutral summaries, and recommend articles that expose the reader to alternative perspectives. The system is designed as a graduation research project with three original academic contributions:

- Applying MCP as a unifying tool-access layer across all agents.
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

### Layer 3 — Agent Layer

Six autonomous agents each perform one specialized function. Every agent inherits from a shared `MCPAgent` base class (`agents/base.py`). Every agent runs a ReAct (Reasoning + Acting) loop. The MCPAgent base class provides two capabilities: `think()` which calls Groq directly to decide the next action, and `call_tool()` which executes the chosen tool via MCP Streamable HTTP. The LLM drives every decision — the Python code only executes what the LLM chooses. Agents do not talk to databases, APIs, or infrastructure LLM tools directly. All external tool calls go through the MCP server.

### Layer 4 — MCP Tool Layer

A single FastMCP server (`mcp_server/server.py`) runs as an independent process on port 8000. It exposes 15 tools, organized in two groups: 9 core tools (Tier 1, built in development Phase 1) and 6 advanced tools (Tier 2, built in development Phase 4). Any agent can call any tool by name. The server handles the actual logic: making HTTP requests, querying PostgreSQL, calling LLM APIs, and reading/writing Redis cache.

### Layer 5 — Infrastructure Layer

Composed of three infrastructure components:

- **PostgreSQL 16 + pgvector** — relational database with vector-search capability for storing articles, events, bias scores, and blind-spot reports.
- **Redis** — in-memory cache for LLM responses, rate-limit control, pipeline results, and agent state.
- **GDELT DOC API** — the external news source. Free, unlimited, Arabic-capable.

---

## 3. The Six Agents

| Agent | File | Responsibility |
|---|---|---|
| Ingestion Agent | `agents/ingestion_agent.py` | Fetches articles from GDELT, filters by relevance, scrapes full content using a three-layer strategy, extracts named entities, generates embeddings, stores in PostgreSQL |
| Clustering Agent | `agents/clustering_agent.py` | Groups articles that cover the same real-world event using three conditions: time window ≤ 72h, cosine similarity ≥ 0.82, shared entity count ≥ 2 |
| Bias Agent | `agents/bias_agent.py` | Performs comparative bias analysis: for each event cluster produced by the Clustering Agent, sends all articles in the cluster to Gemini simultaneously so the model can classify each article's political orientation relative to the others covering the same event. Singleton articles not assigned to any cluster are classified individually as a fallback. Stores score, label, confidence, and comparative framing in bias_scores. |
| Blindspot Agent | `agents/blindspot_agent.py` | For each event cluster, detects which political perspective is underrepresented (coverage < 15% of average) |
| Summary Agent | `agents/summary_agent.py` | Extracts shared facts from multi-source coverage and generates a neutral, single-paragraph Arabic summary |
| Recommendation Agent | `agents/recommendation_agent.py` | Suggests semantically similar articles from opposing bias labels to break filter bubbles |

---

## 4. The MCP Server — 15 Tools

The MCP server is the execution backbone of the entire system. It is the only component that talks to external APIs and infrastructure.

### Tier 1 — 9 Core Tools (built in development Phase 1)

| Tool | Purpose |
|---|---|
| `fetch_gdelt` | Queries GDELT DOC API for Arabic news articles by section |
| `check_relevance` | Uses Groq/Llama to verify a title belongs to the target section |
| `scrape_article` | Fetches full article content using three fallback layers: (1) plain HTTP + BeautifulSoup, (2) Playwright headless browser for JavaScript-rendered pages, (3) title-only fallback if both layers fail. On Layer 3, the tool returns `{"success": false, "content": null, "method": "failed"}`; the calling agent must then store the article with `has_full_content: false`. The tool never raises an exception |
| `get_embedding` | Generates a 768-dimension vector using Gemini text-embedding-004 |
| `store_article` | Inserts a new article record into PostgreSQL using `ON CONFLICT (url) DO NOTHING`. If the URL already exists, the existing record — including its embedding and analysis results — is preserved unchanged. `DO UPDATE` is never used for this table |
| `find_similar` | Performs pgvector cosine-similarity search within a time window |
| `extract_entities` | Extracts named entities (people, locations, organizations) via Gemini |
| `classify_bias` | Comparative bias classification: accepts a list of articles covering the same event and a target article ID. Gemini reads all articles in the cluster before classifying the target article relative to the others, enabling detection of relative framing differences across sources covering the same event. Falls back to single-article analysis when cluster size is 1. |
| `cache_set` / `cache_get` | Read/write Redis cache entries with TTL control |

### Tier 2 — 6 Advanced Tools (built in development Phase 4)

| Tool | Purpose |
|---|---|
| `get_source_bias` | Retrieves a source's pre-labeled bias from the `sources` table |
| `get_coverage_stats` | Counts article distribution by bias label for a given event |
| `detect_blindspot` | Identifies which bias perspectives are underrepresented in an event's coverage |
| `extract_facts` | Identifies facts shared across ≥ 2 articles from different sources covering the same event |
| `vector_recommend` | Finds semantically similar articles with a different bias label (filter bubble breaker) |
| `get_user_profile` | Retrieves a user's reading bias profile from Redis |

---

## 5. Data Flow — End to End

The following describes the complete lifecycle of data in the system, from raw news to dashboard output.

```
[Scheduler] — triggers per section based on update_every_hours in config/sections.py
     │
     ▼
[LangGraph Pipeline] — runs agents in sequence with shared NewsState
     │
     ├── [Ingestion Agent]
     │       fetch_gdelt → check_relevance → scrape_article (3 layers)
     │       → extract_entities → get_embedding → store_article
     │       Output: list of stored article IDs
     │       Note: extract_entities runs before store_article so that
     │             the entities column is populated when Clustering runs
     │
     ├── [Clustering Agent]
     │       find_similar → entity overlap check (uses entities from DB)
     │       → create events in DB → link articles to events
     │       Output: list of event IDs + cluster members
     │
     ├── [Bias Agent]
     │       classify_bias → store bias_scores in DB
     │       Output: classification results per article
     │
     ├── [Blindspot Agent]
     │       get_coverage_stats → detect_blindspot
     │       → store blindspot_reports in DB
     │       Output: list of events with missing coverage
     │
     ├── [Summary Agent]
     │       extract_facts → Gemini neutral summary generation
     │       Output: Arabic neutral summaries per event
     │
     └── [Recommendation Agent]
             vector_recommend → cross-bias article suggestions
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
| `events` | Event clusters discovered by the Clustering Agent |
| `article_events` | Many-to-many join table linking articles to events with a relevance score |
| `bias_scores` | Political score (-1.0 to 1.0), label, confidence, and framing per article |
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

## 9. LLM Strategy

Two LLMs are used, chosen to maximize free-tier usage while optimizing for task requirements:

| LLM | Usage | Daily Free Limit |
|---|---|---|
| Gemini 2.0 Flash | Bias classification, entity extraction, fact extraction, neutral summary generation | 1,500 requests/day |
| Groq + Llama 3.3 70B | Relevance checking (high-volume, low-stakes task) | 14,400 requests/day |

Redis caching is applied to all LLM responses to prevent redundant calls on already-processed articles.

**Dual role of Groq / Llama 3.3 70B:** This model serves two distinct purposes in the system: (1) relevance checking via the `check_relevance` MCP tool (executed inside the MCP server), and (2) agent reasoning via the `think()` method in `MCPAgent` (called directly by each agent to decide its next action in the ReAct loop). The first role is an MCP tool call; the second is the agent's own cognitive process and is architecturally separate from tool execution.

---

## 10. API and Frontend

The **FastAPI** backend (`api/main.py`) exposes REST endpoints that the dashboard consumes. All read endpoints follow a cache-first strategy: they check Redis for a result stored under `results:{section}` before querying PostgreSQL. The pipeline stores its final output in Redis at the end of every run with a 6-hour TTL. If the cache is empty and the database has no data, the endpoint returns `{"status": "pending"}`. No read endpoint triggers pipeline execution.

The **Streamlit** frontend (`frontend/app.py`) renders the analysis results: bias distribution charts, neutral summaries, blind-spot alerts, and filter-bubble recommendations. It connects to FastAPI only — never directly to the database or Redis.

---

## 11. Evaluation Framework

The system's accuracy is measured against a manually annotated dataset of 60 Arabic articles (`evaluation/dataset.json`), distributed across all five bias labels (12 articles per label). The primary metric is weighted F1-Score computed via `sklearn`. The academic target is F1 ≥ 0.65, chosen to be honest about current LLM limitations on Arabic-language content rather than overclaiming performance. Articles where the model returns `confidence < 0.6` are flagged separately and excluded from the primary F1 calculation to isolate high-confidence predictions as a distinct result set.
