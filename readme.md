# Veritas Agent
## An Autonomous Multi-Agent System for Arabic News Bias Analysis and Media Transparency Using Model Context Protocol (MCP)

> **نظام وكلاء الذكاء الاصطناعي للتحقق من الأخبار العربية وتحليل الانحياز الإعلامي**
>
> Graduation Project — Multi-Agent System + Model Context Protocol

---

## Project Summary

Veritas Agent is an autonomous pipeline that continuously ingests Arabic-language news, analyzes media bias, detects blind spots in coverage, and recommends articles from opposing perspectives to break filter bubbles. It covers three sections: Middle East, Libya, and World News.

The system follows the **canonical MCP pattern**: a single MCP server exposes pure data-access and external-API tools, while six specialized agents (the clients) contain all prompts, LLM calls, and domain reasoning. Agents collaborate through a shared LangGraph state and dispatch MCP tools for every data operation.

---

## Academic Contributions

**Contribution 1 — Canonical MCP in a Multi-Agent System:** The six agents interact with all external resources (databases, APIs, caches) through a shared MCP server using the Streamable HTTP transport. The server exposes 19 pure data/infrastructure tools and contains **no LLM calls**. All LLM reasoning happens in the agents (client side). This structure matches Anthropic's MCP specification and reference implementations such as GitHub MCP and Asana MCP, and it keeps the server reusable by any MCP-compatible client.

**Contribution 2 — Stateful Multi-Agent Graph:** LangGraph is used to define the six agents as nodes in a directed graph with a shared `NewsState` object. This makes each stage independently testable, observable, and restartable.

**Contribution 3 — Arabic Media Bias Analysis:** The system classifies political bias using five labels designed for the Arab media context, rather than applying Western-centric political categories. The classification accuracy is formally evaluated against a 60-article manually annotated Arabic dataset with accuracy and macro-F1 as the primary metrics.

---

## System Objectives

| Objective | Description |
|---|---|
| Agentic Retrieval | Automatically fetch Arabic news from GDELT, filter for relevance, and store with semantic embeddings |
| Bias Classification | Classify each article's political orientation using Arabic-context labels |
| Blindspot Detection | Identify events where one political perspective is systematically absent from coverage |
| Filter Bubble Bursting | Recommend articles from opposing bias perspectives using vector similarity |

---

## Technical Stack

| Layer | Technology | Purpose |
|---|---|---|
| Agent Orchestration | LangGraph | Stateful directed agent graph with shared `NewsState` |
| MCP Protocol | FastMCP (Python SDK) | Streamable HTTP server exposing 19 pure tools |
| Primary LLM (client-side) | `gemma-4-31b-it` (served via the Gemini / Google GenAI API) | Bias classification, entity extraction, summarization, fact extraction |
| Fast LLM (client-side) | Groq + Llama 3.3 70B | Relevance checking (high-volume, low-cost) |
| Embeddings (client-side) | Gemini `models/gemini-embedding-001` | 768-dimension semantic vectors |
| Database | PostgreSQL 16 + pgvector | Relational + vector search in one system |
| Cache | Redis | LLM response caching, pipeline results, rate limit control |
| News Source | GDELT DOC API | Free, unlimited Arabic news aggregation |
| Backend API | FastAPI | REST API — reads from Redis cache first, falls back to PostgreSQL |
| Frontend | Streamlit | Interactive analysis dashboard — connects to FastAPI only |

All LLM services use free-tier APIs. A centralized client-side dispatch layer (in `agents/llm_client.py`) routes every generation task through a per-task fallback chain that currently resolves to `gemma-4-31b-it`; resilience on 429 / quota errors is provided by an eight-key round-robin pool with per-key quarantine. Redis caching (via MCP tools `cache_get` / `cache_set`) is used aggressively to stay within daily request limits.

---

## Repository File Structure Map

This is the authoritative map of every file in the project. All components must reside at exactly these paths. The AI coding agent must use this map to determine where every file belongs before creating or modifying anything.

```
veritas-agent/
│
├── readme.md                        ← This file. Repository entry point.
├── blueprint.md                     ← System architecture. Read before writing any code.
├── plan.md                          ← Development roadmap. Phases and steps.
├── agent.md                         ← AI agent behavioral rules and constraints.
├── requirements.txt                 ← Pinned Python dependencies.
├── .env                             ← Environment variables (not committed to git).
├── .gitignore                       ← Must include .env and __pycache__.
├── mcp_server/
│   └── server.py                    ← FastMCP server. All 19 PURE tools defined here.
│                                       No LLM calls live in this file — data access
│                                       and external APIs only. Canonical MCP pattern.
│
├── agents/
│   ├── base.py                      ← MCPAgent base class. Provides call_tool() (MCP),
│   │                                   call_gemini() / call_gemini_embedding() /
│   │                                   call_groq() (direct LLM). All LLM reasoning
│   │                                   originates here — never in the server.
│   ├── llm_client.py                ← Centralized LLM dispatch module. Gemini fallback
│   │                                   chain, Groq throttling, rate-limit semaphores.
│   │                                   Imported by MCPAgent methods.
│   ├── ingestion_agent.py           ← Agent 1 (LLM): Fetch → relevance (Groq) → scrape →
│   │                                   entities (Gemini) → embedding (Gemini) → store.
│   ├── clustering_agent.py          ← Agent 2 (no LLM): Group articles into events using
│   │                                   3-condition deterministic logic.
│   ├── bias_agent.py                ← Agent 3 (LLM): Comparative bias classification via
│   │                                   direct Gemini calls with agent-owned prompts.
│   ├── blindspot_agent.py           ← Agent 4 (no LLM): Statistical underrepresentation
│   │                                   detection. Pure arithmetic.
│   ├── summary_agent.py             ← Agent 5 (LLM): Facts + neutral summary + bias
│   │                                   assessment via direct Gemini calls.
│   ├── recommendation_agent.py      ← Agent 6 (no LLM): Cross-bias vector recommendations.
│   └── graph.py                     ← LangGraph pipeline: NewsState TypedDict +
│                                       run_pipeline(). The shared state object is named
│                                       NewsState throughout this project. Do not use
│                                       PipelineState — that name is incorrect and conflicts
│                                       with all other files. Imports bootstrap_env() at
│                                       the top and calls it before any agent import.
│
├── api/
│   └── main.py                      ← FastAPI app. All read endpoints check Redis cache
│                                       first; fall back to PostgreSQL only if cache is empty.
│                                       Returns {"status": "pending"} if neither has data.
│
├── frontend/
│   └── app.py                       ← Streamlit dashboard. Reads from FastAPI only.
│                                       Never queries the database or Redis directly.
│
├── config/
│   ├── sections.py                  ← SECTIONS dict: query configs for all 3 news sections.
│   └── env_bootstrap.py             ← Centralized environment bootstrap. Pops conflicting
│                                       GOOGLE_API_KEY / GOOGLE_GENAI_API_KEY from the
│                                       shell, loads .env with override=True, validates
│                                       required keys. Imported at every process entry point
│                                       (mcp_server/server.py, api/main.py,
│                                       tests/conftest.py) BEFORE any google.genai or groq
│                                       import.
│
├── infra/
│   └── schema.sql                   ← PostgreSQL schema: 6 tables + indexes + seed data.
│
├── docs/
│   ├── decisions/
│   │   └── ADR-001-canonical-mcp-migration.md   ← Phase 4.5 architectural decision record.
│   └── archive/
│       └── pre_canonical_mcp/                   ← Preserved original architecture files
│                                                    from Phases 0-4 (blueprint, plan, readme,
│                                                    agent.md as they existed pre-migration).
│                                                    Kept as historical evidence for the thesis.
│
├── tests/
│   ├── conftest.py                  ← pytest fixtures. Imports bootstrap_env() at the top
│   │                                   before any SDK is imported.
│   ├── test_tools.py                ← MCP tool tests. 9 tests after Phase 1 (Tier 1 tools);
│   │                                   grows to 15 tests after Phase 4. After Phase 4.5
│   │                                   migration, the test set is rebuilt to cover the
│   │                                   19 pure tools (5 LLM-embedded tool tests removed,
│   │                                   8 new data-tool tests added).
│   ├── test_agents.py               ← One test per agent (6 tests total). After Phase 4.5,
│   │                                   agent tests exercise direct LLM calls with # MOCK
│   │                                   markers per Rule 3.3.
│   └── test_pipeline.py             ← End-to-end pipeline test for all 3 sections.
│
├── evaluation/
│   ├── dataset.json                 ← 60 manually annotated Arabic articles (research artifact).
│   ├── clustering_ground_truth.json ← 10 same-event groups over 23 articles (research artifact).
│   ├── evaluate_bias.py             ← Bias model-size comparison. Reuses the production bias
│   │                                   prompt (imported from agents/bias_agent.py) and runs it
│   │                                   across multiple Gemma sizes + a hosted gemini-3.5-flash
│   │                                   reference; reports accuracy + macro-F1 vs human labels.
│   └── evaluate_clustering.py       ← Clustering correctness (pairwise P/R/F1) vs ground truth.
│
└── summaries/
    ├── phase_0/
    │   └── summary.md               ← Generated after Phase 0 completion.
    ├── phase_1/
    │   └── summary.md               ← Generated after Phase 1 completion.
    ├── phase_2/
    │   └── summary.md               ← Generated after Phase 2 completion.
    ├── phase_3/
    │   └── summary.md               ← Generated after Phase 3 completion.
    ├── phase_4/
    │   └── summary.md               ← Generated after Phase 4 completion.
    ├── phase_4_5/
    │   ├── progress_log.md          ← Living document per Rule 4.1.1 in agent.md.
    │   │                                Updated by the AI coding agent after every
    │   │                                sub-step (4.5.1 through 4.5.7). Read FIRST at
    │   │                                the start of any new session working on
    │   │                                Phase 4.5. Never edited during summary.md
    │   │                                consolidation — preserved as audit trail.
    │   └── summary.md               ← Generated at Step 4.5.7 by consolidating
    │                                    progress_log.md into the eight-section
    │                                    template from agent.md Rule 4.1.
    ├── phase_5/
    │   └── summary.md               ← Generated after Phase 5 completion.
    └── phase_6/
        └── summary.md               ← Generated after Phase 6 completion.
```

---

## The Six Agents

| # | Agent | File | Calls LLM? | What It Does |
|---|---|---|---|---|
| 1 | Ingestion Agent | `agents/ingestion_agent.py` | Yes (Groq + Gemini) | Fetches from GDELT via `fetch_gdelt`, calls Groq directly for relevance checks, scrapes full text via `scrape_article`, calls Gemini directly for entity extraction and embedding generation, stores via `store_article` |
| 2 | Clustering Agent | `agents/clustering_agent.py` | **No** | Groups articles by same event using time window ≤ 72h + cosine similarity ≥ 0.82 + entity overlap ≥ 2. Deterministic — no LLM |
| 3 | Bias Agent | `agents/bias_agent.py` | Yes (Gemini) | Builds comparative-bias prompt from cluster members, calls Gemini directly, parses JSON, rejects low-confidence neutrals, persists via `insert_bias_score` |
| 4 | Blindspot Agent | `agents/blindspot_agent.py` | **No** | Pure statistics — detects events where a label has less than 15% of average coverage. Uses `detect_blindspot` + `insert_blindspot_report` |
| 5 | Summary Agent | `agents/summary_agent.py` | Yes (Gemini) | Builds facts/summary/assessment prompts, calls Gemini directly for each, persists non-empty outputs via `update_event_summary` |
| 6 | Recommendation Agent | `agents/recommendation_agent.py` | **No** | Uses `vector_recommend` to find semantically similar articles from opposing bias labels. Pure pgvector |

---

## The 19 MCP Tools (Canonical — All Pure)

The MCP server contains **no LLM calls**. Every tool performs only data access, external API calls, or Redis operations. Agents call these tools for data, and make their own LLM calls directly.

### Group A — External APIs (2)

| Tool | What It Does |
|---|---|
| `fetch_gdelt` | Queries GDELT API for Arabic news by section and returns cleaned article list |
| `scrape_article` | Fetches full article text using three fallback layers: HTTP → Playwright → title-only. Never raises |

### Group B — Article Operations (3)

| Tool | What It Does |
|---|---|
| `store_article` | Inserts a new article record with `ON CONFLICT (url) DO NOTHING`. Never overwrites existing records |
| `get_articles` | Fetches article rows by list of IDs. Returns all stored fields including entities and embedding |
| `get_articles_for_event` | Fetches articles linked to an event, joined with their bias scores when present |

### Group C — Event Operations (4)

| Tool | What It Does |
|---|---|
| `insert_event` | Creates a new cluster row in the events table; returns the new `event_id` |
| `link_article_event` | Inserts an `article_events` join row with `ON CONFLICT DO NOTHING` |
| `update_event_summary` | Writes `summary` + `bias_assessment` using `COALESCE`/`NULLIF` to preserve existing non-empty values |
| `get_event_article_count` | Returns the count of articles linked to a given event |

### Group D — Bias Storage (1)

| Tool | What It Does |
|---|---|
| `insert_bias_score` | Inserts a bias row with `ON CONFLICT (article_id) DO NOTHING` |

### Group E — Analysis & Vector Search (4)

| Tool | What It Does |
|---|---|
| `find_similar` | Vector similarity search within a section and 72-hour publication time window |
| `vector_recommend` | Finds similar articles with a different bias label (filter bubble breaker). Pure pgvector + SQL filter |
| `get_coverage_stats` | Counts articles by bias label for a given event |
| `detect_blindspot` | Applies the 15%-of-average threshold and returns underrepresented labels. Pure arithmetic |

### Group F — Blindspot Storage (1)

| Tool | What It Does |
|---|---|
| `insert_blindspot_report` | Inserts a blindspot row with a `WHERE NOT EXISTS` guard on `event_id` |

### Group G — Source & User Lookup (2)

| Tool | What It Does |
|---|---|
| `get_source_bias` | Retrieves a news source's pre-labeled bias from the sources table |
| `get_user_profile` | Retrieves a user's dominant reading bias from Redis |

### Group H — Redis Cache (2)

| Tool | What It Does |
|---|---|
| `cache_get` | Reads a string value from Redis by key |
| `cache_set` | Writes a string value to Redis with a TTL in seconds |

---

## The Three News Sections

| Section Key | Label | Update Interval | Articles per Run |
|---|---|---|---|
| `middle_east` | أخبار الشرق الأوسط | Every 6 hours | Up to 75 |
| `libya` | أخبار ليبيا | Every 4 hours | Up to 50 |
| `world` | أخبار العالم | Every 6 hours | Up to 75 |

Each section's update interval is defined in the `update_every_hours` key of its entry in `config/sections.py`. This value is informational — a recommended refresh cadence. The pipeline is triggered manually via `run_pipeline(section)`; there is no automated scheduler.

---

## Bias Label System

Five labels are used, designed specifically for Arabic media political discourse:

| Label | Meaning |
|---|---|
| `pro_government` | Supports or amplifies the official authority position |
| `opposition` | Critiques or counters the government |
| `neutral` | Balanced coverage without a detectable lean |
| `pan_arab` | Pan-Arab nationalist framing |
| `western_aligned` | Reflects Western institutional narratives |

---

## Database Tables

| Table | Description |
|---|---|
| `sources` | Known Arabic news sources with pre-labeled bias |
| `articles` | Core store: title, content, URL, `published_at` timestamp, 768-dim embedding, entities (JSONB), `has_full_content` flag, section |
| `events` | Event clusters with `summary` and `bias_assessment` columns populated by Summary Agent |
| `article_events` | Many-to-many join between articles and events |
| `bias_scores` | Political score, label, confidence, and framing per article (unique on article_id) |
| `blindspot_reports` | Coverage distribution and missing-side analysis per event |

The `published_at` field in the `articles` table is required. The Clustering Agent uses it to enforce the 72-hour time window condition. It is populated from the GDELT `seendate` field during ingestion. Articles without `published_at` are excluded from the clustering step.

---

## Key Architectural Rule — MCP Boundary

> **The MCP server does not call any LLM. Ever.**
>
> All LLM reasoning (Gemini generation, Gemini embedding, Groq classification) happens in the agents, via the methods `call_gemini()`, `call_gemini_embedding()`, and `call_groq()` on the `MCPAgent` base class. These methods delegate to `agents/llm_client.py` which owns the fallback chain, rate limiting, and key isolation.
>
> Agents access every other piece of infrastructure (PostgreSQL, Redis, GDELT, scraping, vector search) **exclusively** through `self.call_tool(name, args)` which dispatches an MCP tool over Streamable HTTP.
>
> This separation is the canonical MCP pattern and the primary academic contribution of the project.

---

## Getting Started

### Prerequisites

- Python 3.11 or later
- PostgreSQL 16 with pgvector extension
- Redis server
- Gemini API key (free tier)
- Groq API key (free tier)

### Setup Order

Follow the phases in `plan.md` in strict sequence. Do not skip Phase 0. The correct startup order for a development session is:

1. Start PostgreSQL
2. Start Redis
3. Start the MCP server: `python mcp_server/server.py`
4. Trigger the pipeline manually for a section:

```bash
python -c "
import asyncio
from agents.graph import run_pipeline
asyncio.run(run_pipeline('libya'))
"
```

### First-Time Environment Setup

The project uses a centralized environment bootstrap to prevent conflicts with system-level `GOOGLE_API_KEY` variables. Every Python entry point must import it **before** any SDK:

```python
# At the very top of mcp_server/server.py, api/main.py,
# agents/graph.py, and tests/conftest.py
from config.env_bootstrap import bootstrap_env
bootstrap_env()
# ... now safe to import google.genai, groq, etc.
```

---

## Evaluation

The system is evaluated on two dimensions in Phase 6:

**Bias classification accuracy** — `evaluation/evaluate_bias.py` runs the 60-article annotated dataset (`evaluation/dataset.json`) through the production bias prompt (imported verbatim from `agents/bias_agent.py`) and computes accuracy and macro-F1. The evaluation spans multiple Gemma model sizes (plus a hosted `gemini-3.5-flash` reference) to produce a model-size comparison. The academic target is F1 ≥ 0.65, which is the honest, achievable threshold given current LLM limitations on Arabic media content.

**Clustering correctness** — `evaluation/evaluate_clustering.py` runs the same dataset articles through the production clustering logic and verifies that the known same-event groups in `evaluation/clustering_ground_truth.json` are placed into a single cluster despite differing bias labels. Special attention is given to hard cases where same-event articles have different titles, testing semantic clustering over surface title matching.

---

## Important Files to Read First

If you are the AI coding agent, read these files in this order before writing any code:

1. `readme.md` — you are here
2. `blueprint.md` — architecture, data flow, and component boundaries
3. `plan.md` — step-by-step implementation instructions
4. `agent.md` — behavioral rules, coding standards, and the mandatory summary.md protocol
5. `docs/decisions/ADR-001-canonical-mcp-migration.md` — why the architecture was restructured in Phase 4.5

---

## Project Status

Track implementation progress by checking the `summaries/` directory. Each completed phase has a `summary.md` file documenting what was built, any deviations, test results, and known issues. The absence of a `summary.md` for a phase means that phase is either in progress or not yet started.

Phases that span multiple working sessions (currently Phase 4.5) additionally maintain a living `progress_log.md` alongside their eventual `summary.md` — see Rule 4.1.1 in `agent.md`. The progress log is the authoritative session-to-session handoff record during the phase; the `summary.md` is consolidated from it at phase closure.

Phases 0 through 4 followed the original architecture (preserved in `docs/archive/pre_canonical_mcp/`). Phase 4.5 migrates the system to the canonical MCP pattern described above. Phases 5 and 6 continue from the canonical architecture.
