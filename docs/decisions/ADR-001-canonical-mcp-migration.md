# ADR-001 — Migration to Canonical MCP Architecture

## Status
Accepted — 2026-04-22

## Context
The initial architecture (Phases 0-4) embedded LLM calls inside MCP server tools 
(classify_bias, extract_entities, extract_facts, check_relevance, get_embedding). 
During Phase 4 integration, three problems emerged:

1. API quota exhaustion propagated across all agents when any single Gemini model 
   was throttled.
2. ReAct loops in Bias/Blindspot/Recommendation/Summary agents consumed ~34K 
   Groq tokens per pipeline run for deterministic task dispatch.
3. The architecture diverged from the canonical MCP pattern documented in 
   Anthropic's specification, where servers expose capabilities and clients 
   contain reasoning.

## Decision
Adopt the canonical MCP architecture:
- **MCP Server:** pure data-access and external-API tools only. Zero LLM calls.
- **Agents (Client):** contain prompts, call LLMs directly, dispatch MCP tools 
  for data operations.
- **LangGraph:** remains the orchestration layer over agents.

## Consequences

### Positive
- Aligns with canonical MCP pattern (GitHub MCP, Asana MCP references).
- Centralized LLM management enables fallback chains and unified caching.
- Cleaner separation of concerns: data plane vs. reasoning plane.
- MCP server becomes reusable by any MCP-compatible client.

### Negative
- Requires rewriting all six agents.
- Invalidates Phase 1 and Phase 4 summaries (LLM tools removed).
- Adds migration overhead (Phase 4.5).

## Alternatives Considered
1. **Keep LLM in server + remove ReAct only.** Rejected — still diverges from 
   canonical MCP; doesn't resolve academic framing concern.
2. **Full ReAct on all agents.** Rejected — documented inefficiency; 
   IngestionAgent already deviates.
3. **Hybrid (data tools + generic llm_generate tool).** Rejected — not canonical; 
   llm_generate as MCP tool is unusual in reference implementations.

## References
- summaries/phase_4/summary.md — documented quota and ReAct issues
- Anthropic MCP specification (https://modelcontextprotocol.io)
- docs/archive/pre_canonical_mcp/ — preserved original architecture