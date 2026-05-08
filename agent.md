# agent.md — Veritas Agent: AI Coding Agent Instructions and Rules

> **Purpose:** This file defines the mandatory behavioral guidelines for the AI coding agent (Cursor) assisting in the development of Veritas Agent. These rules govern how the agent reads requirements, writes code, validates work, handles errors, and documents each phase. All rules are non-negotiable and must be followed throughout the entire project lifecycle.
>
> **Version:** Canonical MCP (post Phase 4.5 migration). The pre-migration version of this file is preserved in `docs/archive/pre_canonical_mcp/agent.md`. The key rule that changed is **Rule 2.3 (MCP Boundary)** — under the canonical pattern, agents call LLMs directly from the client, and the MCP server contains no LLM calls. The boundary now protects PostgreSQL, Redis, GDELT, and scraping — not LLM dispatch.

---

## Part 1 — Project Orientation

### Rule 1.1 — Read Before Writing

Before writing any code, the agent must read and internalize the following files in this order:

1. `readme.md` — for project overview and file structure
2. `blueprint.md` — for architectural decisions and component relationships
3. `plan.md` — for phase-specific implementation steps
4. `docs/decisions/ADR-001-canonical-mcp-migration.md` — for the rationale behind Phase 4.5 and the canonical MCP pattern the project now follows

The agent must not generate code that contradicts the architecture defined in `blueprint.md`. If a conflict arises, the agent must pause and report the conflict rather than silently choosing one over the other.

### Rule 1.2 — Understand Phase Boundaries

Each phase in `plan.md` is a self-contained unit. The agent must not implement code belonging to a later phase until the current phase's success criteria are fully met and verified. Phases must be completed in order: **0, 1, 2, 3, 4, 4.5, 5, 6**.

Phases 0 through 4 document the original architecture that was executed before the canonical migration. The AI coding agent does not need to re-run Phases 0–4 on a codebase that has already completed them. If resuming a project that has completed through Phase 4, begin at Phase 4.5.

### Rule 1.3 — Respect the File Structure

All files must be placed exactly where the structure in `readme.md` specifies. The agent must not create files outside the defined structure, rename existing paths, or merge components that the architecture separates into distinct files.

The `docs/archive/pre_canonical_mcp/` directory contains preserved historical files (pre-migration `readme.md`, `blueprint.md`, `plan.md`, `agent.md`). These files are **read-only reference material**. The agent must never edit files inside `docs/archive/`. The current working versions are the files at the repository root.

---

## Part 2 — Coding Standards

### Rule 2.1 — Python Only

The backend, agents, MCP server, scheduler, and tests are all written in Python. No other backend language is permitted. The frontend is Streamlit Python only.

### Rule 2.2 — Async by Default

All agent methods, all MCP tool functions, and all API endpoint handlers must be implemented as `async` functions. Use `asyncio` patterns throughout.

`psycopg2` is a synchronous library and cannot be awaited directly. To comply with this rule while using `psycopg2`, all database calls inside async functions must be wrapped with `asyncio.to_thread()` to run them in a separate thread and avoid blocking the event loop. The pattern is:

```python
import asyncio
import psycopg2

async def example_db_call():
    def _sync():
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        cur.execute("SELECT 1")
        result = cur.fetchone()
        conn.close()
        return result
    return await asyncio.to_thread(_sync)
```

This pattern must be applied consistently in every MCP tool that performs a database operation. Direct blocking `psycopg2` calls inside `async def` functions without `asyncio.to_thread()` are forbidden.

### Rule 2.3 — Canonical MCP Boundary

The MCP boundary in the canonical architecture is bidirectional and strict. It operates in both directions:

**From the agent side:**
- Agents must never directly import or use `psycopg2`, `redis`, `httpx` (for GDELT), or Playwright. Any access to PostgreSQL, Redis, GDELT, scraping, or vector search must go through `self.call_tool(name, args)` inherited from `MCPAgent`.
- Agents call LLMs directly via three methods on `MCPAgent`: `call_gemini(prompt, task_type, ...)`, `call_gemini_embedding(text)`, and `call_groq(prompt, ...)`. These delegate to the centralized `agents/llm_client.py` module which owns the fallback chain, rate limiting, and model selection.
- The domain-specific prompt for every LLM call lives in the agent that uses it — never in the MCP server, never in `agents/llm_client.py`, never in a shared helper file. Each agent owns its own prompts as module-level string templates or dedicated `_build_*_prompt()` methods.

**From the server side:**
- The MCP server (`mcp_server/server.py`) must never import `google.genai`, `google.generativeai`, `groq`, or any other LLM SDK. Every tool in the server is a pure data-access, external-API, or Redis operation.
- A mechanical verification: `grep -rE "genai|Groq|gemini|GROQ_API_KEY|GEMINI_API_KEY" mcp_server/` must return zero matches in the canonical architecture. If this grep produces output, a violation exists.

**Architectural intent:** This separation matches the canonical MCP pattern documented in Anthropic's MCP specification and implemented in reference servers (GitHub MCP, Asana MCP, Filesystem MCP). The server is a reusable capability surface; the client contains the reasoning that decides when and how to use the capabilities. Violating the boundary in either direction defeats the primary academic contribution of the project.

**Historical note:** In Phases 0–4 (pre-migration), the MCP server contained five LLM-embedded tools (`check_relevance`, `get_embedding`, `extract_entities`, `classify_bias`, `extract_facts`). These were removed in Phase 4.5. The pre-migration version of this rule allowed an exception for `think()` calling Groq from the agent's ReAct loop. ReAct-based reasoning is deprecated in the canonical architecture, and that exception no longer applies.

### Rule 2.4 — No Silent Failures

Every tool in the MCP server and every agent method must handle exceptions explicitly. On error, tools return a structured error response (a dict with an `error` key). They do not raise exceptions that propagate to the pipeline. The pipeline must be able to handle partial failures and continue.

Agent-side LLM calls made via `call_gemini()` / `call_groq()` / `call_gemini_embedding()` also return structured error dicts when the fallback chain is exhausted or the request fails. The agent must check for `"error"` in the returned dict before accessing `"text"` or `"embedding"`, and degrade gracefully (skip the item, log the error into the agent's `errors` list, continue with the next task).

### Rule 2.5 — Type Hints Are Required

All function signatures must include type hints for parameters and return values. This applies to MCP tools, agent methods, pipeline state definitions, and API endpoints. LangGraph state must be defined as a `TypedDict`.

### Rule 2.6 — Environment Variables Only

No API keys, database URLs, or configuration values may be hardcoded in any source file. All such values must be read from environment variables using `os.getenv()`. The `.env` file provides values for local development. The `.env` file must never be committed to version control.

**Environment bootstrap is mandatory.** Every process entry point (`mcp_server/server.py`, `api/main.py`, `scheduler.py`, `agents/graph.py`, `tests/conftest.py`) must import `config/env_bootstrap.py` at the very top, before any other project import and before any LLM SDK import:

```python
# MUST be first line of imports
from config.env_bootstrap import bootstrap_env
bootstrap_env()
# Now safe to import google.genai, groq, fastmcp, etc.
```

This prevents the conflict where a shell-level `GOOGLE_API_KEY` is auto-detected by `google.genai` and silently overrides the project's dedicated key from `.env`.

### Rule 2.7 — Caching Is Mandatory for LLM Calls

Every LLM call made from an agent must check Redis for a cached result before invoking the API. The cache key must be deterministic and derived from the input content (typically `task_type + md5(prompt)`). TTL values must follow the policy defined in `blueprint.md`: 24 hours for entity extraction, 72 hours for bias classification, 7 days for embeddings. This is not optional — it is required to stay within free API rate limits.

Cache access is itself an MCP tool call. The agent uses `self.call_tool("cache_get", {"key": ...})` before any LLM call and `self.call_tool("cache_set", {"key": ..., "value": ..., "ttl": ...})` after a successful parse. The Redis server is infrastructure and stays behind the MCP boundary — agents never import `redis` directly.

### Rule 2.8 — Conflict Handling for Database Inserts

Whenever inserting records that have uniqueness constraints, always use explicit conflict handling. The strategy depends on the table:

- **`articles` table:** Use `INSERT ... ON CONFLICT (url) DO NOTHING` exclusively. The `DO UPDATE` strategy is forbidden for this table. A URL conflict means the article already exists with its embedding and analysis results intact — overwriting it would corrupt data from previous pipeline runs.
- **`bias_scores` table:** Use `INSERT ... ON CONFLICT (article_id) DO NOTHING` exclusively. A bias score that already exists for an article must not be overwritten by a re-run.
- **`events.summary` and `events.bias_assessment` columns:** Use `COALESCE(NULLIF(%s, ''), existing_column)` when calling `update_event_summary`. An empty-string input (caused by an LLM failure) must not overwrite a prior non-empty value. This guard is enforced inside the `update_event_summary` MCP tool itself.
- **`article_events` table:** Use `ON CONFLICT DO NOTHING` (idempotent re-linking).
- **`blindspot_reports` table:** Use `INSERT ... SELECT ... WHERE NOT EXISTS` keyed on `event_id`. This table has no UNIQUE constraint on `event_id`, so the `WHERE NOT EXISTS` guard is required to keep re-runs idempotent.
- **All other tables:** Use `ON CONFLICT DO NOTHING` as the default unless there is an explicit, documented reason to update.

Never use a bare `INSERT` without conflict handling for any record that has a uniqueness constraint.

### Rule 2.9 — Truncate LLM Inputs

All text inputs to LLMs must be truncated before the API call. The maximum input length is 3,000 characters for bias classification, entity extraction, and fact extraction. Exceeding this limit wastes tokens and may cause inconsistent responses. For comparative bias prompts that concatenate multiple articles, each individual article contribution must be truncated (e.g., title to 200 chars, content to 1,500 chars) and the final prompt must not exceed 8,000 characters.

### Rule 2.10 — Keep Configuration Separate from Logic

`config/sections.py` contains data only. It must not contain functions, class definitions, or executable logic. All three section configurations (`middle_east`, `libya`, `world`) must remain in this single file.

`config/env_bootstrap.py` is the sole exception: it contains the `bootstrap_env()` function because environment bootstrap is configuration-adjacent infrastructure. No other logic belongs in `config/`.

---

## Part 3 — Testing Standards

### Rule 3.1 — Tests Before Integration

No component may be integrated into the pipeline until it has at least one passing test. MCP tools are tested before agents. Agents are tested individually before the graph integrates them.

### Rule 3.2 — Test Via MCP Transport

Tests for MCP tools must call the tools through a live MCP client session, not by importing and calling the Python function directly. This validates the full transport path, not just the function logic.

### Rule 3.3 — Use Real Infrastructure in Tests

Tests run against the actual local PostgreSQL and Redis instances. Mocking of database or Redis infrastructure is not permitted.

**Exception — LLM calls only:** LLM API calls (Gemini and Groq) may be mocked in `tests/test_agents.py` to avoid exhausting free-tier API quotas during repeated test runs. When mocking an LLM call, the mock must return a response in the exact format the real API would return — a valid JSON string matching the expected schema. Mocks must be clearly marked with a `# MOCK` comment and removed or made optional before the final evaluation run in Phase 6.

If the test environment lacks running PostgreSQL or Redis services, the agent must report this and halt rather than substituting mocks for infrastructure.

### Rule 3.4 — Test Files Location

All tests live in the `tests/` directory. The four test files are `conftest.py`, `test_tools.py`, `test_agents.py`, and `test_pipeline.py`. No test code lives in source files. `conftest.py` must call `bootstrap_env()` at import time so every test process has a correctly initialized environment.

---

## Part 4 — Phase Completion Protocol

### Rule 4.1 — MANDATORY: Generate summary.md After Every Phase

This is the most critical procedural rule. Upon completing any phase (0 through 6, including the intermediate Phase 4.5), the agent **must** generate a `summary.md` file before any other work begins. Failing to generate this file means the phase is not considered complete, regardless of whether the code works.

The `summary.md` file must be placed in a dedicated subdirectory for that phase: `summaries/phase_N/summary.md` where N is the phase number. For Phase 4.5, the directory is `summaries/phase_4_5/summary.md`.

**The summary.md file must contain the following sections:**

#### Section A — Phase Identification

The phase number, phase title (matching the name in `plan.md`), start date, and completion date.

#### Section B — What Was Implemented

A precise enumeration of every file created or modified during the phase. For each file, describe what was implemented in it — not what the file is supposed to do in theory, but what logic was actually written. If a file was partially implemented (e.g., only 4 of 9 tools are done), this must be stated explicitly.

#### Section C — Logic Changes and Deviations

Any decision made during implementation that deviates from the specification in `blueprint.md` or `plan.md` must be documented here. Include the reason for the deviation. If no deviations occurred, state "No deviations from specification."

For Phase 4.5 specifically, Section C must reference `docs/decisions/ADR-001-canonical-mcp-migration.md` and note that the departures from the Phase 0–4 architecture (removal of LLM-embedded tools, removal of ReAct from four agents, introduction of client-side LLM dispatch) are deliberate per that ADR, not unplanned drift.

#### Section D — Dependencies Introduced

Any new Python packages or system dependencies added during this phase that were not in the original `requirements.txt`. Include the exact version pinned.

#### Section E — Known Issues and Limitations

Any bugs discovered but not fixed, any edge cases not handled, any known fragility in the implementation. Be honest. Do not omit issues because they feel minor. This section feeds into the thesis's limitations chapter.

#### Section F — Test Results

A plain-text record of the test command run and its output (pass/fail count). If tests failed, document which tests failed and why, and whether they were fixed before the phase was closed.

#### Section G — Success Criteria Verification

Copy the success criteria from `plan.md` for this phase and mark each one as MET or NOT MET with a one-line explanation.

If any criterion is marked NOT MET, the agent must not proceed to the next phase. Instead, it must document the blocking issue in Section E, propose a resolution, and wait for explicit human confirmation before continuing. Partial completion that is honestly documented is acceptable. Silently marking a phase as done when criteria are not met is not acceptable.

#### Section H — What the Next Phase Depends On

A brief statement of what this phase produced that the next phase will rely on. This makes handoff between phases explicit and traceable.

### Rule 4.1.1 — Progress Log for Multi-Step Phases

For phases that consist of multiple independently-executable sub-steps (currently Phase 4.5, but this rule applies to any future phase with the same structure), a **living** `progress_log.md` file is maintained alongside the eventual `summary.md`. This complements — not replaces — Rule 4.1.

**Location:** `summaries/phase_<N>/progress_log.md` (e.g., `summaries/phase_4_5/progress_log.md`).

**Purpose:** A session-to-session handoff document that preserves sub-step-level detail. Cursor context windows are finite; a single phase may span several working sessions. The progress log ensures no sub-step completion detail is lost between sessions.

**Update protocol:** After completing any sub-step, the AI coding agent appends a new section to `progress_log.md` **before closing the session**. Each sub-step entry contains:

- Sub-step identifier (e.g., `Step 4.5.2`)
- Status: `COMPLETE`, `PARTIAL`, or `BLOCKED`
- Date completed
- Files created and files modified
- Deviations from the sub-step spec in `plan.md` (with reason)
- Issues encountered (honest reporting per Rule 6.2)
- Verification check results (paste command outputs)
- Handoff notes for the next sub-step

**Relationship to `summary.md`:** When the final Self-Audit sub-step closes the phase, the AI coding agent consolidates all `progress_log.md` entries into the final `summary.md` following the eight-section template in Rule 4.1. The `progress_log.md` is **preserved as-is** — not deleted — because it provides a granular audit trail that is valuable for the thesis and for any future debugging.

**Session-start protocol:** At the start of every new Cursor session working on a multi-step phase, the agent **must read `progress_log.md` first**, before beginning any code work, and confirm in writing which sub-step is next and what prior sub-steps produced.

**Do not update `summary.md` during the phase.** `summary.md` is written once at phase closure, not incrementally. Incremental updates belong in `progress_log.md`.

---

## Part 5 — Prohibited Actions

The following actions are explicitly forbidden at all times:

- Do not hardcode any API key or secret in any file.
- Do not create files outside the directory structure defined in `readme.md`.
- Do not edit any file inside `docs/archive/` — those are preserved historical artifacts.
- Do not import `google.genai`, `google.generativeai`, `groq`, or any other LLM SDK inside `mcp_server/`. The MCP server is LLM-free per Rule 2.3.
- Do not import `psycopg2`, `redis`, or `httpx` (for GDELT) directly in any agent file. All infrastructure access goes through `self.call_tool()`.
- Do not skip writing tests because the code "looks correct."
- Do not move forward to a new phase without generating the `summary.md` for the completed phase.
- Do not move forward to a new phase if any success criterion in the current phase is marked NOT MET — wait for human confirmation first.
- Do not alter the database schema after Phase 0 without documenting the change in the current phase's `summary.md`.
- Do not add new Python packages without adding them to `requirements.txt` with a pinned version.
- Do not import from one agent's file into another agent's file. All shared logic lives in `agents/base.py` or `agents/llm_client.py`.
- Do not modify `config/sections.py` to include logic or imports — it must remain a pure data configuration file.
- Do not write frontend code that queries the database directly — the frontend must only communicate with the FastAPI backend.
- Do not use `INSERT ... ON CONFLICT DO UPDATE` for the `articles` table or the `bias_scores` table. Use `DO NOTHING` exclusively for these tables. `DO UPDATE` on these tables overwrites embeddings and analysis results from previous pipeline runs and corrupts the dataset.
- Do not write to `events.summary` or `events.bias_assessment` without the `COALESCE(NULLIF(...))` guard. An empty LLM output must never overwrite a prior good value.
- Do not call `psycopg2` functions directly inside an `async def` without wrapping them in `asyncio.to_thread()`. Blocking database calls in async context will stall the event loop and cause silent performance degradation.
- Do not add a ReAct loop to a new agent whose task is deterministic ("for each item in a known list, call one tool"). ReAct is justified only when the LLM must choose among multiple tools or branch based on observations. See Phase 4.5 rationale for why ReAct was removed from Bias, Blindspot, Recommendation, and Summary agents.
- Do not bypass `config/env_bootstrap.py` at any process entry point. Direct calls to `load_dotenv()` or direct reads of environment variables in the shell pattern are forbidden.
- Do not write or update `summary.md` incrementally during a multi-step phase. Sub-step progress is recorded in `progress_log.md` per Rule 4.1.1. `summary.md` is written once at phase closure.
- Do not skip reading `progress_log.md` at the start of a new session on a multi-step phase. Rule 4.1.1 session-start protocol is mandatory.

---

## Part 6 — Communication Protocol

### Rule 6.1 — Report Before Implementing Ambiguous Requirements

If a step in `plan.md` is ambiguous or contradicts a rule in this file or a design decision in `blueprint.md`, the agent must surface the ambiguity and propose a resolution before writing code.

### Rule 6.2 — Report Incomplete Phases Honestly

If a phase cannot be fully completed (for example, due to a failing test or an unresolvable API issue), the agent must document exactly what was and was not completed in the `summary.md` and flag it clearly. Partial completion that is honestly documented is acceptable. Silently marking a phase as done when it is not is not acceptable.

### Rule 6.3 — Issue Reporting Format

When reporting a single issue, describe it with: the file affected, the specific line or function, the expected behavior, the actual behavior, and the proposed fix.

When multiple issues exist in the same file and are causally related — for example, a missing field that cascades into failures in two other functions — they may be reported together in a single structured list, with each item following the format above. Unrelated issues from different files must be reported separately.

---

## Part 7 — Academic Integrity Reminders

The agent is assisting with a graduation project that will be academically evaluated. The following must be kept in mind:

- The canonical MCP boundary (server = pure data/API tools, agents = reasoning and LLM calls) is the primary technical contribution. It must be implemented genuinely, not simulated. The mechanical check `grep -rE "genai|Groq|gemini" mcp_server/` returning zero is the concrete proof.
- The bias labels are designed for Arabic media context and must not be replaced with generic positive/negative sentiment labels.
- The confidence score in bias classification is a model estimate, not a mathematical calculation. It must not be described as a calculated probability in any generated documentation.
- The evaluation F1-Score must be computed against genuinely human-labeled data. The agent must not generate or pre-label the evaluation dataset.
- All deviations from the original design must be documented honestly so the student can explain them to the graduation committee.
- The architectural migration from the original design (Phases 0–4) to the canonical MCP pattern (Phase 4.5) is itself an academic deliverable. It demonstrates that the student recognized an alignment problem during integration, researched the canonical pattern, designed a disciplined migration, and executed it while preserving historical documentation. This is engineering maturity, not a design defect — the `summaries/` directory and `docs/decisions/ADR-001-canonical-mcp-migration.md` collectively prove this.
