# Veritas Agent
## An Autonomous Multi-Agent System for Arabic News Bias Analysis and Media Transparency Using Model Context Protocol (MCP)

> **نظام وكلاء الذكاء الاصطناعي للتحقق من الأخبار العربية وتحليل الانحياز الإعلامي**
>
> Graduation Project — Multi-Agent System + Model Context Protocol

---

## Project Summary

Veritas Agent is an autonomous pipeline that continuously ingests Arabic-language news, analyzes media bias, detects blind spots in coverage, and recommends articles from opposing perspectives to break filter bubbles. It covers three sections: Middle East, Libya, and World News.

The system is built on a Multi-Agent architecture where six specialized AI agents collaborate through a shared LangGraph state. Every agent accesses external tools exclusively through a single Model Context Protocol (MCP) server — a design that enforces strict separation between orchestration logic and execution logic, and constitutes the primary academic contribution of the project.

---

## Academic Contributions

**Contribution 1 — MCP in a Multi-Agent System:** Each of the six agents interacts with all external resources (databases, LLMs, APIs) through a shared MCP server using the Streamable HTTP transport. This creates a genuine decoupling between the orchestration layer and the execution layer. New tools can be added to the server without modifying any agent.

**Contribution 2 — Stateful Multi-Agent Graph:** LangGraph is used to define the six agents as nodes in a directed graph with a shared `NewsState` object. This makes each stage independently testable, observable, and restartable.

**Contribution 3 — Arabic Media Bias Analysis:** The system classifies political bias using five labels designed for the Arab media context, rather than applying Western-centric political categories. The classification accuracy is formally evaluated against a 60-article manually annotated Arabic dataset with weighted F1-Score as the primary metric.

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
| MCP Protocol | FastMCP (Python SDK) | Streamable HTTP server exposing 15 tools |
| Primary LLM | Gemini 2.0 Flash | Bias classification, entity extraction, summarization |
| Fast LLM | Groq + Llama 3.3 70B | Relevance checking (high-volume, low-cost) |
| Embeddings | Gemini text-embedding-004 | 768-dimension semantic vectors |
| Database | PostgreSQL 16 + pgvector | Relational + vector search in one system |
| Cache | Redis | LLM response caching, pipeline results, rate limit control |
| News Source | GDELT DOC API | Free, unlimited Arabic news aggregation |
| Backend API | FastAPI | REST API — reads from Redis cache first, falls back to PostgreSQL |
| Frontend | Streamlit | Interactive analysis dashboard — connects to FastAPI only |

All LLM services use free-tier APIs. Redis caching is used aggressively to stay within daily request limits.

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
├── scheduler.py                     ← Automated pipeline trigger. Each section runs on its
│                                       own update_every_hours schedule defined in
│                                       config/sections.py (Libya: 4h, others: 6h).
│
├── mcp_server/
│   └── server.py                    ← FastMCP server. All 15 tools defined here.
│
├── agents/
│   ├── base.py                      ← MCPAgent base class. Shared MCP connection logic.
│   ├── ingestion_agent.py           ← Agent 1: Fetch, filter, scrape, extract entities,
│   │                                   embed, store.
│   ├── clustering_agent.py          ← Agent 2: Group articles into events (3-condition logic).
│   ├── bias_agent.py                ← Agent 3: Classify political orientation per article.
│   ├── blindspot_agent.py           ← Agent 4: Detect underrepresented coverage perspectives.
│   ├── summary_agent.py             ← Agent 5: Generate neutral Arabic summaries per event.
│   ├── recommendation_agent.py      ← Agent 6: Recommend cross-bias articles.
│   └── graph.py                     ← LangGraph pipeline: NewsState TypedDict +
│                                       run_pipeline(). The shared state object is named
│                                       NewsState throughout this project. Do not use
│                                       PipelineState — that name is incorrect and conflicts
│                                       with all other files.
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
│   └── sections.py                  ← SECTIONS dict: query configs for all 3 news sections.
│
├── infra/
│   └── schema.sql                   ← PostgreSQL schema: 6 tables + indexes + seed data.
│
├── tests/
│   ├── test_tools.py                ← MCP tool tests. 9 tests after Phase 1 (Tier 1 tools);
│   │                                   grows to 15 tests after Phase 4 when Tier 2 advanced
│   │                                   tools are added. Do not write Tier 2 tests during Phase 1.
│   ├── test_agents.py               ← One test per agent (6 tests total).
│   └── test_pipeline.py             ← End-to-end pipeline test for all 3 sections.
│
├── evaluation/
│   ├── dataset.json                 ← 60 manually annotated Arabic articles (research artifact).
│   └── evaluate.py                  ← Computes F1-Score against human labels via sklearn.
│
└── summaries/
    ├── phase_0/
    │   └── summary.md               ← Generated by agent after Phase 0 completion.
    ├── phase_1/
    │   └── summary.md               ← Generated by agent after Phase 1 completion.
    ├── phase_2/
    │   └── summary.md               ← Generated by agent after Phase 2 completion.
    ├── phase_3/
    │   └── summary.md               ← Generated by agent after Phase 3 completion.
    ├── phase_4/
    │   └── summary.md               ← Generated by agent after Phase 4 completion.
    ├── phase_5/
    │   └── summary.md               ← Generated by agent after Phase 5 completion.
    └── phase_6/
        └── summary.md               ← Generated by agent after Phase 6 completion.
```

---

## The Six Agents

| # | Agent | File | What It Does |
|---|---|---|---|
| 1 | Ingestion Agent | `agents/ingestion_agent.py` | Fetches from GDELT, filters by relevance, scrapes full text using three fallback layers (HTTP → Playwright → title-only), extracts named entities, generates embeddings, stores in DB |
| 2 | Clustering Agent | `agents/clustering_agent.py` | Groups articles by same event using time window ≤ 72h + cosine similarity ≥ 0.82 + entity overlap ≥ 2. Depends on the entities column populated by the Ingestion Agent |
| 3 | Bias Agent | `agents/bias_agent.py` | Classifies political orientation into 5 Arabic-context labels; stores score and confidence |
| 4 | Blindspot Agent | `agents/blindspot_agent.py` | Detects events where a political perspective has less than 15% of average coverage |
| 5 | Summary Agent | `agents/summary_agent.py` | Extracts shared facts and writes a neutral one-paragraph Arabic summary |
| 6 | Recommendation Agent | `agents/recommendation_agent.py` | Finds semantically similar articles from opposing bias perspectives |

---

## The 15 MCP Tools

### Tier 1 — Core Tools (9)

These 9 tools are implemented in development Phase 1. `test_tools.py` will contain 9 tests after this phase completes.

| Tool | What It Does |
|---|---|
| `fetch_gdelt` | Queries GDELT API for Arabic news by section and returns cleaned article list |
| `check_relevance` | Verifies a title belongs to the target section using Groq/Llama |
| `scrape_article` | Fetches full article text using three layers: (1) HTTP + BeautifulSoup, (2) Playwright headless browser for JavaScript-rendered pages, (3) title-only fallback if both layers fail. On Layer 3, the tool returns `{"success": false, "content": null, "method": "failed"}`; the calling agent stores the article with `has_full_content: false`. Never raises an exception |
| `get_embedding` | Returns a 768-dimension Gemini embedding for input text |
| `store_article` | Inserts a new article record in PostgreSQL using `ON CONFLICT (url) DO NOTHING`. If the URL already exists, the existing record is preserved unchanged. Never overwrites an existing article |
| `find_similar` | Vector similarity search within a section and a 72-hour publication time window |
| `extract_entities` | Extracts people, locations, and organizations from Arabic text via Gemini |
| `classify_bias` | Returns political score, label, confidence, and framing for an article |
| `cache_set` / `cache_get` | Redis read/write with TTL |

### Tier 2 — Advanced Tools (6)

These 6 tools are added in development Phase 4. `test_tools.py` grows to 15 tests after this phase completes.

| Tool | What It Does |
|---|---|
| `get_source_bias` | Retrieves a news source's pre-labeled bias from the sources table |
| `get_coverage_stats` | Counts articles by bias label for a given event |
| `detect_blindspot` | Identifies which bias perspectives are underrepresented in an event |
| `extract_facts` | Finds facts shared across multiple articles covering the same event |
| `vector_recommend` | Finds similar articles with a different bias label (filter bubble breaker) |
| `get_user_profile` | Retrieves a user's dominant reading bias from Redis |

---

## The Three News Sections

| Section Key | Label | Update Interval | Articles per Run |
|---|---|---|---|
| `middle_east` | أخبار الشرق الأوسط | Every 6 hours | Up to 75 |
| `libya` | أخبار ليبيا | Every 4 hours | Up to 50 |
| `world` | أخبار العالم | Every 6 hours | Up to 75 |

Each section's update interval is defined in the `update_every_hours` key of its entry in `config/sections.py`. The scheduler reads this value independently for each section and triggers the pipeline on the schedule specific to that section.

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
| `events` | Event clusters discovered by the Clustering Agent |
| `article_events` | Many-to-many join between articles and events |
| `bias_scores` | Political score, label, confidence, and framing per article |
| `blindspot_reports` | Coverage distribution and missing-side analysis per event |

The `published_at` field in the `articles` table is required. The Clustering Agent uses it to enforce the 72-hour time window condition. It is populated from the GDELT `seendate` field during ingestion. Articles without `published_at` are excluded from the clustering step.

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
4. Run the scheduler for continuous operation, or trigger the pipeline manually for a single section during development and testing:

```bash
# Manual single-section trigger (useful during development and testing)
python -c "
import asyncio
from agents.graph import run_pipeline
asyncio.run(run_pipeline('libya'))
"
```

### Key Design Constraint

Agents do not call databases or LLMs directly. Every external interaction flows through the MCP server. This is both an architectural requirement and the central academic claim of the project. Any deviation from this constraint must be documented in the relevant phase `summary.md`.

---

## Evaluation

The system is evaluated against a 60-article manually annotated dataset in `evaluation/dataset.json`. The evaluation script computes a weighted F1-Score using `sklearn`. The academic target is F1 ≥ 0.65, which is the honest, achievable threshold given current LLM limitations on Arabic media content — not an inflated claim.

---

## Important Files to Read First

If you are the AI coding agent, read these files in this order before writing any code:

1. `readme.md` — you are here
2. `blueprint.md` — architecture, data flow, and component boundaries
3. `plan.md` — step-by-step implementation instructions
4. `agent.md` — behavioral rules, coding standards, and the mandatory summary.md protocol

---

## Project Status

Track implementation progress by checking the `summaries/` directory. Each completed phase has a `summary.md` file documenting what was built, any deviations, test results, and known issues. The absence of a `summary.md` for a phase means that phase is either in progress or not yet started.
