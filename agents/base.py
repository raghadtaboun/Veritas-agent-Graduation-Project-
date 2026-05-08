"""
agents/base.py — MCPAgent Base Class (canonical MCP, post Phase 4.5)

All six Veritas agents inherit from this class. It provides exactly four
capabilities — no more:

  1. call_tool(tool_name, args)
     Execute one MCP tool via Streamable HTTP. This is the ONLY way agents
     access infrastructure (PostgreSQL, Redis, GDELT, scraping, pgvector).
     Transparent 429 retry with exponential backoff so downstream-API rate
     limits never surface as agent observations.

  2. call_gemini(prompt, task_type, max_tokens, temperature)
  3. call_gemini_embedding(text)
  4. call_groq(prompt, model, max_tokens, temperature)
     Thin async delegations to ``agents/llm_client.py``. Every agent LLM
     call flows through one of these three methods — never a direct SDK
     import in any agent file (Rule 2.3).

Architectural history: prior to Phase 4.5 this class also exposed a ReAct
loop (``think`` / ``run_react`` / ``_fallback_decide``) that drove agent
decision-making by calling Groq for tool-selection reasoning. The
canonical-MCP migration (ADR-001) retired that pattern — every rewritten
agent is a deterministic loop — so the ReAct block, its supporting Groq
client, and its rate-limit state were removed in Step 4.5.5 Part 2. The
pre-migration implementation lives in ``docs/archive/pre_canonical_mcp/``
and in the git history prior to commit ``c977c09``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession
from mcp.types import TextContent

from mcp.client.streamable_http import streamable_http_client

_MCP_SERVER_URL: str = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")

logger = logging.getLogger(__name__)

# ── Transport-level 429 retry configuration ──────────────────────────────────
_TOOL_429_RETRIES:    int   = 3    # transparent 429 retries for call_tool()
_TOOL_429_BASE_DELAY: float = 2.0  # base backoff — doubles each attempt


def _is_429_error(text: str) -> bool:
    """Return True if *text* indicates an HTTP 429 / rate-limit error."""
    low = text.lower()
    return (
        "429" in text
        or "rate limit" in low
        or "rate_limit" in low
        or "too many requests" in low
    )


class MCPAgent:
    """
    Base class for all Veritas agents in the canonical-MCP architecture.

    Subclasses must not import or call PostgreSQL, Redis, httpx, Playwright,
    or any LLM SDK directly. All infrastructure access goes through
    ``call_tool``; all LLM access goes through ``call_gemini`` /
    ``call_gemini_embedding`` / ``call_groq`` (Rule 2.3).
    """

    # ── Tool Execution ────────────────────────────────────────────────────

    async def call_tool(
        self,
        tool_name: str,
        args: dict | None = None,
    ) -> dict:
        """
        Open a Streamable HTTP connection to the MCP server, initialise a
        ClientSession, invoke the named tool with args, and return the parsed
        JSON response.

        429 errors from downstream APIs (e.g. GDELT) are retried transparently
        with exponential backoff so they never reach the agent as observations.

        Parameters
        ----------
        tool_name : str
            The exact name of the MCP tool to call (e.g. "fetch_gdelt").
        args : dict | None
            Arguments to pass to the tool.  Defaults to an empty dict.

        Returns
        -------
        dict
            Parsed JSON response from the tool.
            On any transport or parse error, returns {"error": str} rather
            than raising an exception (Rule 2.4).
        """
        last_result: dict = {}
        for attempt in range(_TOOL_429_RETRIES + 1):
            try:
                async with streamable_http_client(_MCP_SERVER_URL) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, args or {})

                        if not result.content:
                            return {"error": f"MCP tool '{tool_name}' returned empty content"}

                        first_block = result.content[0]
                        if not isinstance(first_block, TextContent):
                            return {
                                "error": (
                                    f"Expected TextContent from MCP tool '{tool_name}', "
                                    f"got {type(first_block).__name__}"
                                )
                            }

                        parsed = json.loads(first_block.text)
                        last_result = parsed

                        err = parsed.get("error", "")
                        if isinstance(err, str) and _is_429_error(err) and attempt < _TOOL_429_RETRIES:
                            delay = _TOOL_429_BASE_DELAY * (2 ** attempt)
                            logger.warning(
                                "[MCPAgent.call_tool] Tool '%s' hit 429 — "
                                "retrying in %.1fs (%d/%d)",
                                tool_name, delay, attempt + 1, _TOOL_429_RETRIES,
                            )
                            await asyncio.sleep(delay)
                            continue

                        return parsed

            except Exception as e:
                err_str = str(e)
                if _is_429_error(err_str) and attempt < _TOOL_429_RETRIES:
                    delay = _TOOL_429_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "[MCPAgent.call_tool] Transport 429 for '%s' — "
                        "retrying in %.1fs (%d/%d)",
                        tool_name, delay, attempt + 1, _TOOL_429_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                return {"error": f"MCP transport error calling '{tool_name}': {e}"}

        return last_result or {"error": f"Tool '{tool_name}': 429 retries exhausted"}

    # ── Canonical-MCP LLM Dispatch (Phase 4.5 Step 4.5.2) ────────────────
    # Thin async delegations to agents.llm_client. Every reasoning agent
    # rewritten in Step 4.5.5 uses these three methods exclusively for
    # its LLM access. The MCPAgent class intentionally owns no prompts
    # and no provider-SDK imports — both live in agents/llm_client.py
    # and in the per-agent prompt strings (Rule 2.3).

    async def call_gemini(
        self,
        prompt: str,
        task_type: str = "general",
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> dict:
        """Generate text via Gemini with the per-task fallback chain.

        Returns the dict produced by
        :func:`agents.llm_client.gemini_generate_with_fallback` —
        ``{"text", "model_used", "attempts"}`` on success or
        ``{"error", "attempted"}`` on failure. Never raises.
        """
        from agents import llm_client
        return await llm_client.gemini_generate_with_fallback(
            prompt=prompt,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def call_gemini_embedding(self, text: str) -> dict:
        """Generate a 768-dim Gemini embedding.

        Returns the dict produced by :func:`agents.llm_client.gemini_embed` —
        ``{"embedding", "dimension", "model_used"}`` on success or
        ``{"error"}`` on failure. Never raises.
        """
        from agents import llm_client
        return await llm_client.gemini_embed(text)

    async def call_groq(
        self,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> dict:
        """Generate text via Groq with concurrency + interval throttling.

        Returns the dict produced by :func:`agents.llm_client.groq_generate` —
        ``{"text", "model_used"}`` on success or
        ``{"error", "model_used"}`` on failure. Never raises.
        """
        from agents import llm_client
        return await llm_client.groq_generate(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
