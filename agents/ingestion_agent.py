"""
agents/ingestion_agent.py — Ingestion Agent (Agent 1) [Canonical MCP rewrite]

Deterministic pipeline that ingests Arabic news articles for one section:

    fetch_gdelt → for each article:
        call_groq(relevance_prompt)              ← direct Groq call
        call_tool("scrape_article")
        call_gemini(entity_prompt, task=entities) ← direct Gemini call
        call_gemini_embedding(text)              ← direct Gemini embedding
        call_tool("store_article")

Architecture (Phase 4.5 canonical):
  - No ReAct loop, no ``think()``, no ``_fallback_decide``. The six-step
    sequence is fixed.
  - All data / cache / scrape operations go through ``self.call_tool()``
    (Rule 2.3 canonical boundary). No direct psycopg2, redis, or httpx imports.
  - All three LLM calls go through the agent-side dispatch methods
    (``self.call_groq`` / ``self.call_gemini`` / ``self.call_gemini_embedding``)
    which delegate to ``agents/llm_client.py`` (Rule 2.3).
  - Rule 2.7 — every LLM call is cached in Redis via ``cache_get`` /
    ``cache_set`` using deterministic ``<task>:<md5(prompt)>`` keys.
  - Public ``run(section)`` signature is unchanged. The ``errors`` return
    value is a ``list[str]`` per Rule 2.4 — the legacy integer counter
    is retired (graph-side handling of this field is Step 4.5.5b).

Per-article failures (scrape timeout, JSON parse error, embedding failure,
store conflict) are wrapped in try/except and appended to ``errors``; the
loop continues so a single bad article never crashes the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.base import MCPAgent
from config.sections import SECTIONS

logger = logging.getLogger(__name__)

# ── Cache TTLs (seconds) — Rule 2.7 / blueprint.md §9 ────────────────────────
_TTL_RELEVANCE: int = 86_400     # 24 h — title relevance stable per day
_TTL_ENTITIES:  int = 86_400     # 24 h
_TTL_EMBEDDING: int = 604_800    # 7 d — embeddings fully deterministic

# ── LLM budgets (see Step 4.5.2 handoff on gemini-2.5-flash thinking tokens) ─
_RELEVANCE_MAX_TOKENS: int = 2048
_ENTITIES_MAX_TOKENS:  int = 2048
_ENTITIES_TEMPERATURE: float = 0.0
_RELEVANCE_TEMPERATURE: float = 0.0

# ── Canonical empty-entity structure — returned on any extract_entities fail ─
_EMPTY_ENTITIES: dict[str, list[str]] = {
    "people": [], "locations": [], "organizations": [],
}


# ── Prompt templates (moved from mcp_server/server.py in Step 4.5.4) ─────────
# PRESERVED VERBATIM from the pre-migration tools `check_relevance` and
# `extract_entities`. Arabic-media domain knowledge is encoded here and
# MUST NOT be paraphrased. Source: git commit c977c09 (pre-4.5.4 server).

_RELEVANCE_PROMPT_TEMPLATE = (
    "You are a news relevance classifier for an Arabic media analysis system.\n"
    "News section: '{section_label}' (key: {section})\n"
    "Article title: {title}\n\n"
    "Does this title belong to the news section above?\n"
    "Reply with a JSON object and nothing else — no markdown, no extra text:\n"
    '{{"relevant": true, "reason": "one sentence in English"}}\n'
    "or\n"
    '{{"relevant": false, "reason": "one sentence in English"}}'
)

_ENTITY_PROMPT_TEMPLATE = """\
You are a named-entity extraction system for Arabic news articles (the output should be in Arabic).
Extract all named entities from the text below and return ONLY a JSON object
with exactly these three keys. Values are lists of Arabic-language strings.
Return empty lists if no entities are found for a category.
Do not include markdown, code fences, or any text outside the JSON.

Required format:
{{"people": ["name1", "name2"], "locations": ["loc1"], "organizations": ["org1"]}}

Text:
{text}"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _md5(s: str) -> str:
    """Hex-md5 of a UTF-8 string. Used for deterministic LLM cache keys."""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _strip_code_fences(raw: str) -> str:
    """Remove leading ```json / trailing ``` from an LLM JSON response."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s.strip())
    return s.strip()


class IngestionAgent(MCPAgent):
    """
    Agent 1 — Ingestion Agent (canonical MCP, deterministic).

    ``run(section)`` fetches articles from GDELT and processes each one
    through a fixed sequence of MCP tool calls and direct LLM calls. No
    ReAct loop, no reasoning fallback.

    Returns a stats dict compatible with LangGraph ``NewsState`` (graph.py).
    """

    # ── Public API ────────────────────────────────────────────────────────

    async def run(self, section: str) -> dict[str, Any]:
        """
        Execute the deterministic ingestion pipeline for *section*.

        Returns
        -------
        dict with keys:
            fetched, relevant, scraped, entities_extracted, stored (int),
            errors (list[str]),
            article_ids (list[int]).
        """
        stats: dict[str, int] = {
            "fetched": 0,
            "relevant": 0,
            "scraped": 0,
            "entities_extracted": 0,
            "stored": 0,
        }
        article_ids: list[int] = []
        errors: list[str] = []
        skipped: int = 0

        # ── Step 1: Fetch article list from GDELT (no LLM) ──────────────────
        fetch_result = await self.call_tool("fetch_gdelt", {"section": section})
        if "error" in fetch_result:
            msg = f"fetch_gdelt failed: {fetch_result['error']}"
            logger.error("[IngestionAgent] %s", msg)
            errors.append(msg)
            return {**stats, "errors": errors, "article_ids": article_ids}

        articles = fetch_result.get("articles", []) or []
        stats["fetched"] = fetch_result.get("count", len(articles))

        if not articles:
            logger.info("[IngestionAgent] No articles from GDELT for '%s'", section)
            return {**stats, "errors": errors, "article_ids": article_ids}

        logger.info(
            "[IngestionAgent] Fetched %d articles for '%s' — processing",
            len(articles), section,
        )

        # ── Step 2: Process each article deterministically ───────────────────
        for i, article in enumerate(articles, 1):
            title = article.get("title", "") or ""
            url   = article.get("url", "") or ""

            try:
                # ── 2a. Relevance check (direct Groq via agent-side dispatch)
                rel = await self._check_relevance(title, section)

                if "error" in rel:
                    # Transport / parse error — do NOT skip the article,
                    # mirror legacy behaviour (a misclassification caused by
                    # Groq downtime must not drop otherwise-valid articles).
                    errors.append(
                        f"relevance error for '{url}': {rel['error']}"
                    )
                elif not rel.get("relevant", False):
                    reason = str(rel.get("reason", ""))
                    low = reason.lower()
                    # Tolerance carried forward from legacy: a "403"/"error"
                    # reason comes from upstream failure, not a true negative.
                    if "error" not in low and "403" not in reason:
                        skipped += 1
                        continue

                stats["relevant"] += 1

                # ── 2b. Scrape article content (MCP tool) ────────────────────
                scrape = await self.call_tool("scrape_article", {"url": url})

                content: str | None = None
                has_full_content = False
                if "error" in scrape:
                    errors.append(
                        f"scrape_article error for '{url}': {scrape['error']}"
                    )
                elif scrape.get("success"):
                    content = scrape.get("content")
                    has_full_content = bool(content)
                    if has_full_content:
                        stats["scraped"] += 1

                text = content or title

                # ── 2c. Extract entities (direct Gemini via agent-side) ──────
                entities, entities_ok = await self._extract_entities(text)
                if entities_ok:
                    stats["entities_extracted"] += 1

                # ── 2d. Generate embedding (direct Gemini embed) ─────────────
                embedding = await self._generate_embedding(text)
                if embedding is None:
                    errors.append(
                        f"embedding failed for article {i}/{len(articles)} ({url})"
                    )
                    skipped += 1
                    continue

                # ── 2e. Store in database (MCP tool) ─────────────────────────
                store = await self.call_tool("store_article", {
                    "title":            title,
                    "url":              url,
                    "section":          section,
                    "published_at":     article.get("seendate", "") or "",
                    "content":          content,
                    "entities":         entities,
                    "embedding":        embedding,
                    "has_full_content": has_full_content,
                })

                if "error" in store:
                    errors.append(
                        f"store_article error for '{url}': {store['error']}"
                    )
                elif "id" in store:
                    stats["stored"] += 1
                    article_ids.append(int(store["id"]))

            except Exception as e:
                errors.append(
                    f"unexpected exception for article {i}/{len(articles)} "
                    f"({url}): {e}"
                )
                skipped += 1
                continue

        logger.info(
            "[IngestionAgent] Done — fetched=%d relevant=%d scraped=%d "
            "entities_extracted=%d stored=%d errors=%d skipped=%d",
            stats["fetched"], stats["relevant"], stats["scraped"],
            stats["entities_extracted"], stats["stored"], len(errors),
            skipped,
        )

        return {**stats, "errors": errors, "article_ids": article_ids}

    # ── Step 2a — relevance (Groq) ────────────────────────────────────────

    async def _check_relevance(self, title: str, section: str) -> dict[str, Any]:
        """
        Decide whether *title* belongs to *section*.

        Returns a dict with one of these shapes:
          {"relevant": bool, "reason": str}   — normal result
          {"error": str}                      — transport / parse failure
        Never raises (Rule 2.4). Results are cached with a 24 h TTL
        keyed by section + md5(title) so repeated runs within a day do
        not consume Groq quota (Rule 2.7).
        """
        if section not in SECTIONS:
            return {"relevant": False, "reason": f"Unknown section: '{section}'"}

        section_label = SECTIONS[section]["label"]
        safe_title = (title or "")[:500]

        # Deterministic cache key (Rule 2.7)
        cache_key = f"relevance:{section}:{_md5(safe_title)}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            try:
                parsed = json.loads(cached["value"])
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError):
                pass  # fall through to LLM call

        prompt = _RELEVANCE_PROMPT_TEMPLATE.format(
            section_label=section_label,
            section=section,
            title=safe_title,
        )

        groq_result = await self.call_groq(
            prompt=prompt,
            max_tokens=_RELEVANCE_MAX_TOKENS,
            temperature=_RELEVANCE_TEMPERATURE,
        )

        if "error" in groq_result:
            return {"error": groq_result["error"]}

        raw = _strip_code_fences(groq_result.get("text", ""))
        try:
            parsed = json.loads(raw) if raw else {}
        except (TypeError, ValueError) as e:
            return {"error": f"relevance JSON parse failed: {e}"}

        if not isinstance(parsed, dict):
            return {"error": "relevance response was not a JSON object"}

        # Normalise: ensure 'relevant' is a genuine bool and 'reason' exists.
        if not isinstance(parsed.get("relevant"), bool):
            parsed["relevant"] = (
                str(parsed.get("relevant", "false")).lower() == "true"
            )
        parsed.setdefault("reason", "")

        try:
            payload = json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = json.dumps({"relevant": bool(parsed.get("relevant")), "reason": ""})

        await self.call_tool("cache_set", {
            "key":   cache_key,
            "value": payload,
            "ttl":   _TTL_RELEVANCE,
        })
        return parsed

    # ── Step 2c — entities (Gemini) ───────────────────────────────────────

    async def _extract_entities(
        self,
        text: str,
    ) -> tuple[dict[str, list[str]], bool]:
        """
        Extract people / locations / organizations from Arabic *text*.

        Returns ``(entities, success)``. ``entities`` is always a dict
        with the three canonical keys (empty lists on any failure path);
        ``success`` is True when the LLM/cache round-trip returned a
        valid JSON object (even if its lists are empty). Results are
        cached with a 24 h TTL keyed by md5(text[:3000]) (Rule 2.7).
        Never raises (Rule 2.4).
        """
        truncated = (text or "")[:3000]
        if not truncated:
            return dict(_EMPTY_ENTITIES), False

        cache_key = f"entities:{_md5(truncated)}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            try:
                parsed = json.loads(cached["value"])
                if isinstance(parsed, dict):
                    return self._normalise_entities(parsed), True
            except (TypeError, ValueError):
                pass

        prompt = _ENTITY_PROMPT_TEMPLATE.format(text=truncated)
        gen = await self.call_gemini(
            prompt=prompt,
            task_type="entities",
            max_tokens=_ENTITIES_MAX_TOKENS,
            temperature=_ENTITIES_TEMPERATURE,
        )

        if "error" in gen:
            logger.warning(
                "[IngestionAgent._extract_entities] LLM error: %s", gen["error"],
            )
            return dict(_EMPTY_ENTITIES), False

        raw = _strip_code_fences(gen.get("text", ""))
        try:
            parsed = json.loads(raw) if raw else {}
        except (TypeError, ValueError) as e:
            logger.warning(
                "[IngestionAgent._extract_entities] JSON parse failed: %s", e,
            )
            return dict(_EMPTY_ENTITIES), False

        if not isinstance(parsed, dict):
            return dict(_EMPTY_ENTITIES), False

        entities = self._normalise_entities(parsed)

        try:
            payload = json.dumps(entities, ensure_ascii=False)
            await self.call_tool("cache_set", {
                "key":   cache_key,
                "value": payload,
                "ttl":   _TTL_ENTITIES,
            })
        except (TypeError, ValueError):
            pass

        return entities, True

    @staticmethod
    def _normalise_entities(raw: dict) -> dict[str, list[str]]:
        """Coerce an LLM response into the canonical entities shape."""
        return {
            "people":        [str(e) for e in raw.get("people",        []) if e],
            "locations":     [str(e) for e in raw.get("locations",     []) if e],
            "organizations": [str(e) for e in raw.get("organizations", []) if e],
        }

    # ── Step 2d — embedding (Gemini) ──────────────────────────────────────

    async def _generate_embedding(self, text: str) -> list[float] | None:
        """
        Return a 768-dim embedding for *text*, or ``None`` on failure.

        Results are cached with a 7-day TTL keyed by md5(text[:3000])
        (Rule 2.7). Embeddings are fully deterministic for a fixed model,
        so the long TTL is safe.
        """
        truncated = (text or "")[:3000]
        if not truncated:
            return None

        cache_key = f"embedding:{_md5(truncated)}"

        cached = await self.call_tool("cache_get", {"key": cache_key})
        if not cached.get("error") and cached.get("value"):
            try:
                parsed = json.loads(cached["value"])
                if isinstance(parsed, list) and parsed:
                    return [float(x) for x in parsed]
            except (TypeError, ValueError):
                pass

        result = await self.call_gemini_embedding(truncated)
        if "error" in result:
            logger.warning(
                "[IngestionAgent._generate_embedding] %s", result["error"],
            )
            return None

        emb = result.get("embedding")
        if not isinstance(emb, list) or not emb:
            return None

        try:
            payload = json.dumps(emb)
            await self.call_tool("cache_set", {
                "key":   cache_key,
                "value": payload,
                "ttl":   _TTL_EMBEDDING,
            })
        except (TypeError, ValueError):
            pass

        return emb
