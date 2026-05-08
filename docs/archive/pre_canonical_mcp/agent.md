# agent.md — Veritas Agent: AI Coding Agent Instructions and Rules

> **Purpose:** This file defines the mandatory behavioral guidelines for the AI coding agent (Cursor) assisting in the development of Veritas Agent. These rules govern how the agent reads requirements, writes code, validates work, handles errors, and documents each phase. All rules are non-negotiable and must be followed throughout the entire project lifecycle.

---

## Part 1 — Project Orientation

### Rule 1.1 — Read Before Writing

Before writing any code, the agent must read and internalize the following files in this order:

1. `readme.md` — for project overview and file structure
2. `blueprint.md` — for architectural decisions and component relationships
3. `plan.md` — for phase-specific implementation steps

The agent must not generate code that contradicts the architecture defined in `blueprint.md`. If a conflict arises, the agent must pause and report the conflict rather than silently choosing one over the other.

### Rule 1.2 — Understand Phase Boundaries

Each phase in `plan.md` is a self-contained unit. The agent must not implement code belonging to a later phase until the current phase's success criteria are fully met and verified. Phases must be completed in order: 0, 1, 2, 3, 4, 5, 6.

### Rule 1.3 — Respect the File Structure

All files must be placed exactly where the structure in `readme.md` specifies. The agent must not create files outside the defined structure, rename existing paths, or merge components that the architecture separates into distinct files.

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

### Rule 2.3 — MCP Boundary is Sacred

Agents must never directly import or call PostgreSQL, Redis, or LLM clients. The only way an agent accesses infrastructure is through `self.call_tool(tool_name, args)` inherited from `MCPAgent`. This boundary enforces the architectural separation between orchestration and execution. Any violation of this rule defeats the core academic contribution of the project.

**Exception — Agent Reasoning:** The `think()` method in `MCPAgent` calls Groq directly to perform agent reasoning (deciding the next action). This is not a violation of Rule 2.3. Agent reasoning is the agent's own cognitive process — it is not an infrastructure call. The MCP boundary applies to PostgreSQL, Redis, LLM tools (classify_bias, extract_entities, check_relevance), and external APIs. The agent's own reasoning loop is architecturally separate from tool execution.

### Rule 2.4 — No Silent Failures

Every tool in the MCP server and every agent method must handle exceptions explicitly. On error, tools return a structured error response (a dict with an `error` key). They do not raise exceptions that propagate to the pipeline. The pipeline must be able to handle partial failures and continue.

### Rule 2.5 — Type Hints Are Required

All function signatures must include type hints for parameters and return values. This applies to MCP tools, agent methods, pipeline state definitions, and API endpoints. LangGraph state must be defined as a `TypedDict`.

### Rule 2.6 — Environment Variables Only

No API keys, database URLs, or configuration values may be hardcoded in any source file. All such values must be read from environment variables using `os.getenv()`. The `.env` file provides values for local development. The `.env` file must never be committed to version control.

### Rule 2.7 — Caching Is Mandatory for LLM Calls

Every LLM call in the MCP server must check Redis for a cached result before invoking the API. The cache key must be deterministic and derived from the input content. TTL values must follow the policy defined in `blueprint.md`: 24 hours for entity extraction, 72 hours for bias classification, 7 days for embeddings. This is not optional — it is required to stay within free API rate limits.

### Rule 2.8 — Conflict Handling for Database Inserts

Whenever inserting records that have uniqueness constraints, always use explicit conflict handling. The strategy depends on the table:

- **`articles` table:** Use `INSERT ... ON CONFLICT (url) DO NOTHING` exclusively. The `DO UPDATE` strategy is forbidden for this table. A URL conflict means the article already exists with its embedding and analysis results intact — overwriting it would corrupt data from previous pipeline runs.
- **`bias_scores` table:** Use `INSERT ... ON CONFLICT DO NOTHING` exclusively. A bias score that already exists for an article must not be overwritten by a re-run.
- **All other tables:** Use `ON CONFLICT DO NOTHING` as the default unless there is an explicit, documented reason to update.

Never use a bare `INSERT` without conflict handling for any record that has a uniqueness constraint.

### Rule 2.9 — Truncate LLM Inputs

All text inputs to LLMs must be truncated before the API call. The maximum input length is 3,000 characters for bias classification, entity extraction, and fact extraction. Exceeding this limit wastes tokens and may cause inconsistent responses.

### Rule 2.10 — Keep Configuration Separate from Logic

`config/sections.py` contains data only. It must not contain functions, class definitions, or executable logic. All three section configurations (`middle_east`, `libya`, `world`) must remain in this single file.

---

## Part 3 — Testing Standards

### Rule 3.1 — Tests Before Integration

No component may be integrated into the pipeline until it has at least one passing test. MCP tools are tested before agents. Agents are tested individually before the graph integrates them.

### Rule 3.2 — Test Via MCP Transport

Tests for MCP tools must call the tools through a live MCP client session, not by importing and calling the Python function directly. This validates the full transport path, not just the function logic.

### Rule 3.3 — Use Real Infrastructure in Tests

Tests run against the actual local PostgreSQL and Redis instances. Mocking of database or Redis infrastructure is not permitted.

**Exception — LLM calls only:** LLM API calls (Gemini and Groq) may be mocked in `test_tools.py` to avoid exhausting free-tier API quotas during repeated test runs. When mocking an LLM call, the mock must return a response in the exact format the real API would return — a valid JSON string matching the expected schema. Mocks must be clearly marked with a `# MOCK` comment and removed or made optional before the final evaluation run in Phase 6.

If the test environment lacks running PostgreSQL or Redis services, the agent must report this and halt rather than substituting mocks for infrastructure.

### Rule 3.4 — Test Files Location

All tests live in the `tests/` directory. The three test files are `test_tools.py`, `test_agents.py`, and `test_pipeline.py`. No test code lives in source files.

---

## Part 4 — Phase Completion Protocol

### Rule 4.1 — MANDATORY: Generate summary.md After Every Phase

This is the most critical procedural rule. Upon completing any phase (0 through 6), the agent **must** generate a `summary.md` file before any other work begins. Failing to generate this file means the phase is not considered complete, regardless of whether the code works.

The `summary.md` file must be placed in a dedicated subdirectory for that phase: `summaries/phase_N/summary.md` where N is the phase number (0 through 6).

**The summary.md file must contain the following sections:**

#### Section A — Phase Identification

The phase number, phase title (matching the name in `plan.md`), start date, and completion date.

#### Section B — What Was Implemented

A precise enumeration of every file created or modified during the phase. For each file, describe what was implemented in it — not what the file is supposed to do in theory, but what logic was actually written. If a file was partially implemented (e.g., only 4 of 9 tools are done), this must be stated explicitly.

#### Section C — Logic Changes and Deviations

Any decision made during implementation that deviates from the specification in `blueprint.md` or `plan.md` must be documented here. Include the reason for the deviation. If no deviations occurred, state "No deviations from specification."

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

---

## Part 5 — Prohibited Actions

The following actions are explicitly forbidden at all times:

- Do not hardcode any API key or secret in any file.
- Do not create files outside the directory structure defined in `readme.md`.
- Do not write agent code that calls PostgreSQL, Redis, or LLM APIs directly — all such calls must go through the MCP server.
- Do not skip writing tests because the code "looks correct."
- Do not move forward to a new phase without generating the `summary.md` for the completed phase.
- Do not move forward to a new phase if any success criterion in the current phase is marked NOT MET — wait for human confirmation first.
- Do not alter the database schema after Phase 0 without documenting the change in the current phase's `summary.md`.
- Do not add new Python packages without adding them to `requirements.txt` with a pinned version.
- Do not import from one agent's file into another agent's file. All shared logic lives in `agents/base.py`.
- Do not modify `config/sections.py` to include logic or imports — it must remain a pure data configuration file.
- Do not write frontend code that queries the database directly — the frontend must only communicate with the FastAPI backend.
- Do not use `INSERT ... ON CONFLICT DO UPDATE` for the `articles` table or the `bias_scores` table. Use `DO NOTHING` exclusively for these tables. `DO UPDATE` on these tables overwrites embeddings and analysis results from previous pipeline runs and corrupts the dataset.
- Do not call `psycopg2` functions directly inside an `async def` without wrapping them in `asyncio.to_thread()`. Blocking database calls in async context will stall the event loop and cause silent performance degradation.

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

- The MCP boundary between agents and tools is the primary technical contribution. It must be implemented genuinely, not simulated.
- The bias labels are designed for Arabic media context and must not be replaced with generic positive/negative sentiment labels.
- The confidence score in bias classification is a model estimate, not a mathematical calculation. It must not be described as a calculated probability in any generated documentation.
- The evaluation F1-Score must be computed against genuinely human-labeled data. The agent must not generate or pre-label the evaluation dataset.
- All deviations from the original design must be documented honestly so the student can explain them to the graduation committee.
