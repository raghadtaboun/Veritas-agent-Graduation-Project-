"""
agents/recommendation_agent.py — Recommendation Agent (Agent 6) [Canonical MCP rewrite]

Filter-bubble-bursting recommendations via the `vector_recommend` MCP tool.
For each input article (up to `_MAX_ARTICLES_PER_RUN`), the agent returns
semantically similar articles with a DIFFERENT bias label, caching the
per-article result in Redis for 6 hours.

Architecture (Phase 4.5 canonical):
  - Deterministic — no LLM calls, no ReAct loop, no reasoning fallback.
  - Every DB and cache operation goes through `self.call_tool()` (Rule 2.3
    canonical boundary). No direct psycopg2, redis, or httpx imports.
  - Caching is mandatory per Rule 2.7: `cache_get` is attempted before every
    `vector_recommend` call; successful recommendations are persisted via
    `cache_set` with a 6-hour TTL (blueprint.md Section 9).
  - Public `run(article_ids, section)` signature unchanged from the legacy
    agent, so `agents/graph.py` needs no modification.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents.base import MCPAgent

logger = logging.getLogger(__name__)

_MAX_ARTICLES_PER_RUN: int   = 10     # plan.md Step 4.4 budget
_RECOMMEND_LIMIT:      int   = 5      # max recommendations per article
_MIN_SIMILARITY:       float = 0.5    # cosine-similarity floor
_RECOMMEND_CACHE_TTL:  int   = 21_600  # 6 hours — blueprint.md Section 9


class RecommendationAgent(MCPAgent):
    """
    Agent 6 — Recommendation Agent (canonical MCP, deterministic, cache-aware).

    For each article (up to 10), consults the Redis cache first and falls
    back to `vector_recommend` on a miss, then writes the result back to
    Redis. Never raises.
    """

    async def run(
        self,
        article_ids: list[int],
        section: str,
    ) -> dict[str, Any]:
        """
        Find cross-bias recommendations for each article in *article_ids*.

        Parameters
        ----------
        article_ids :
            Article IDs to generate recommendations for. Truncated to
            ``_MAX_ARTICLES_PER_RUN`` (10) per plan.md Step 4.4 spec.
        section :
            Pipeline section — must match a key in `config/sections.py`.

        Returns
        -------
        dict with keys:
            recommendations : dict[int, list[dict]]
                article_id → recommendation list. Each rec dict contains
                id, title, bias_label, similarity (per `vector_recommend`).
            stored_count : int
                Number of articles whose recommendation list is non-empty.
            errors : list[str]
                Non-fatal error strings.
        """
        if not article_ids:
            return {"recommendations": {}, "stored_count": 0, "errors": []}

        limited_ids: list[int] = [int(a) for a in article_ids[:_MAX_ARTICLES_PER_RUN]]

        recommendations: dict[int, list[dict]] = {}
        errors: list[str] = []
        cache_hits = 0

        for aid in limited_ids:
            cache_key = f"recommend:{aid}"

            # ── Cache-first check (Rule 2.7) ─────────────────────────────────
            cached_recs = await self._read_cache(cache_key)
            if cached_recs is not None:
                recommendations[aid] = cached_recs
                cache_hits += 1
                continue

            # ── Cache miss → vector_recommend ────────────────────────────────
            vec_result = await self.call_tool(
                "vector_recommend",
                {
                    "article_id":     aid,
                    "section":        section,
                    "limit":          _RECOMMEND_LIMIT,
                    "min_similarity": _MIN_SIMILARITY,
                },
            )

            if "error" in vec_result:
                errors.append(
                    f"vector_recommend error for article {aid}: "
                    f"{vec_result['error']}"
                )
                # Store empty list so the key exists in the output map.
                recommendations[aid] = []
                continue

            recs = vec_result.get("recommendations", []) or []
            recommendations[aid] = list(recs)

            # ── Write-through cache ──────────────────────────────────────────
            try:
                payload = json.dumps(recs, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                errors.append(
                    f"recommendations not JSON-serialisable for article {aid}: {e}"
                )
                continue

            set_result = await self.call_tool(
                "cache_set",
                {"key": cache_key, "value": payload, "ttl": _RECOMMEND_CACHE_TTL},
            )
            if "error" in set_result or not set_result.get("success", False):
                # Non-fatal — agent already has the result in-memory for the
                # caller. Log and continue.
                errors.append(
                    f"cache_set error for article {aid}: "
                    f"{set_result.get('error', 'success=False')}"
                )

        stored_count = sum(1 for v in recommendations.values() if v)

        logger.info(
            "[RecommendationAgent] articles=%d  cache_hits=%d  "
            "with_recs=%d  errors=%d",
            len(limited_ids),
            cache_hits,
            stored_count,
            len(errors),
        )

        return {
            "recommendations": recommendations,
            "stored_count":    stored_count,
            "errors":          errors,
        }

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _read_cache(self, key: str) -> list[dict] | None:
        """
        Look up *key* in Redis via `cache_get`. Returns the decoded
        recommendation list on a hit, or ``None`` on miss / parse error.

        A malformed cache entry is treated as a miss — the caller will
        overwrite it with a fresh `vector_recommend` result.
        """
        result = await self.call_tool("cache_get", {"key": key})
        if "error" in result:
            logger.debug("[RecommendationAgent] cache_get error for %s: %s",
                         key, result["error"])
            return None

        raw = result.get("value")
        if not raw:
            return None

        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None

        if not isinstance(parsed, list):
            return None
        return parsed
