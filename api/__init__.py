"""api/ — Veritas Agent FastAPI backend (Phase 5).

The FastAPI app is exposed as `api.main:app`. It reads from PostgreSQL and
Redis only — no LLM calls, no MCP tool calls, no pipeline triggering. Per
plan.md Step 5.1 and phase_5_ui_spec.md §1.
"""
