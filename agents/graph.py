"""
agents/graph.py — LangGraph Pipeline (Orchestration Layer)

Defines the NewsState TypedDict, chains all six agents as sequential graph
nodes, and exposes run_pipeline(section) as the single external entry point
for the scheduler and integration tests.

After all agents complete, the final state is serialised to JSON and stored
in Redis under results:{section} with a 6-hour TTL.  This cache is what the
FastAPI backend reads during Phase 5 (Rule 5.1 cache-first strategy).

Agent execution order (strictly sequential — no branching):

    IngestionAgent
        ↓  article_ids
    ClusteringAgent
        ↓  cluster_ids (event_ids), event_clusters
    BiasAgent
        ↓  bias_results
    BlindspotAgent
        ↓  blindspots
    SummaryAgent
        ↓  summaries
    RecommendationAgent
        ↓  recommendations
    ── cache results:{section} in Redis (TTL 6 h) ──

Architecture notes:

  • NewsState TypedDict carries ALL shared data between nodes.  The spec
    lists nine fields; event_clusters (dict[int, list[int]]) is added as a
    tenth because BiasAgent.run() requires it — ClusteringAgent produces it
    and there is no other transport path in the LangGraph pattern.

  • Every node wraps its agent call in try/except and appends errors to the
    shared errors list rather than raising.  This means a failed Ingestion
    run produces an empty article_ids list — all subsequent nodes receive
    empty inputs and return immediately, producing a valid (empty) final
    state.  The pipeline never crashes.

  • Redis result storage uses a temporary MCPAgent instance to call cache_set
    via MCP, preserving the architectural boundary (Rule 2.3).

  • The recommendations dict has int keys (article_id).  JSON requires string
    keys, so _serialise_state converts them before Redis storage.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Phase 4.5 Step 4.5.2: bootstrap the environment before importing any
# project module that ultimately touches an LLM SDK (every agent imports
# agents/llm_client.py which loads google.genai and groq). Replaces the
# prior dotenv-only initialisation.
from config.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()

from langgraph.graph import END, START, StateGraph  # noqa: E402
from typing import TypedDict  # noqa: E402

logger = logging.getLogger(__name__)

_PIPELINE_CACHE_TTL: int = 21_600   # 6 hours — same as recommendation cache TTL


# ─────────────────────────────────────────────────────────────────────────────
# Shared state definition
# ─────────────────────────────────────────────────────────────────────────────

class NewsState(TypedDict):
    """
    Shared state carried through all pipeline nodes.

    Mandatory fields per plan.md Step 4.5:
        section, article_ids, cluster_ids, bias_results, blindspots,
        summaries, recommendations, stats, errors.

    Additional field:
        event_clusters — dict[int, list[int]] produced by ClusteringAgent and
        consumed by BiasAgent.  Not in the nine-field spec but required for
        the Clustering → Bias handoff.
    """

    section:         str
    article_ids:     list[int]
    cluster_ids:     list[int]           # event_ids from ClusteringAgent
    event_clusters:  dict                # event_id → [article_ids]
    bias_results:    list[dict]
    blindspots:      list[dict]
    summaries:       list[dict]
    recommendations: dict                # article_id (int) → list[rec_dict]
    stats:           dict
    errors:          list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline nodes — one per agent
# ─────────────────────────────────────────────────────────────────────────────

async def _ingestion_node(state: NewsState) -> dict[str, Any]:
    """
    Node 1 — IngestionAgent.

    Fetches, filters, and stores Arabic news articles from GDELT.
    Produces article_ids for all downstream nodes.
    """
    from agents.ingestion_agent import IngestionAgent

    section = state["section"]
    logger.info("[Pipeline] IngestionAgent starting — section=%s", section)

    try:
        agent = IngestionAgent()
        result = await agent.run(section)
    except Exception as e:
        logger.error("[Pipeline] IngestionAgent crashed: %s", e)
        return {
            "article_ids": [],
            "stats":       {**state["stats"], "ingestion": {}},
            "errors":      state["errors"] + [f"[ingestion] crash: {e}"],
        }

    # IngestionAgent returns errors as a list[str] (Phase 4.5.5 Part 2
    # contract change), uniform with every other node. The trailing
    # ``or []`` defends against an unexpected None return from a future
    # refactor — Rule 2.4 robustness, costs nothing.
    new_errors: list[str] = list(state["errors"])
    for e in result.get("errors", []) or []:
        new_errors.append(f"[ingestion] {e}")

    logger.info(
        "[Pipeline] IngestionAgent done — fetched=%d relevant=%d stored=%d",
        result.get("fetched", 0),
        result.get("relevant", 0),
        result.get("stored", 0),
    )

    return {
        "article_ids": result.get("article_ids", []),
        "stats": {
            **state["stats"],
            "ingestion": {
                "fetched":            result.get("fetched", 0),
                "relevant":           result.get("relevant", 0),
                "scraped":            result.get("scraped", 0),
                "entities_extracted": result.get("entities_extracted", 0),
                "stored":             result.get("stored", 0),
            },
        },
        "errors": new_errors,
    }


async def _clustering_node(state: NewsState) -> dict[str, Any]:
    """
    Node 2 — ClusteringAgent.

    Groups articles into event clusters.  Passes event_clusters dict to
    subsequent nodes so BiasAgent can use comparative analysis.
    """
    from agents.clustering_agent import ClusteringAgent

    article_ids = state["article_ids"]
    section     = state["section"]

    if not article_ids:
        logger.info("[Pipeline] ClusteringAgent skipped — no articles")
        return {
            "cluster_ids":    [],
            "event_clusters": {},
            "stats":          {**state["stats"], "clustering": {}},
        }

    logger.info(
        "[Pipeline] ClusteringAgent starting — %d articles", len(article_ids)
    )

    try:
        agent = ClusteringAgent()
        result = await agent.run(section, article_ids)
    except Exception as e:
        logger.error("[Pipeline] ClusteringAgent crashed: %s", e)
        return {
            "cluster_ids":    [],
            "event_clusters": {},
            "stats":          {**state["stats"], "clustering": {}},
            "errors":         state["errors"] + [f"[clustering] crash: {e}"],
        }

    new_errors = list(state["errors"])
    for e in result.get("errors", []):
        new_errors.append(f"[clustering] {e}")

    logger.info(
        "[Pipeline] ClusteringAgent done — events=%d clustered=%d singletons=%d",
        len(result.get("event_ids", [])),
        result.get("clustered_count", 0),
        result.get("singleton_count", 0),
    )

    return {
        "cluster_ids":    result.get("event_ids", []),
        "event_clusters": result.get("event_clusters", {}),
        "stats": {
            **state["stats"],
            "clustering": {
                "events_created": len(result.get("event_ids", [])),
                "clustered":      result.get("clustered_count", 0),
                "singletons":     result.get("singleton_count", 0),
            },
        },
        "errors": new_errors,
    }


async def _bias_node(state: NewsState) -> dict[str, Any]:
    """
    Node 3 — BiasAgent.

    Classifies political bias for all articles — comparative mode for
    clustered articles, singleton fallback for unclustered articles.
    """
    from agents.bias_agent import BiasAgent

    article_ids    = state["article_ids"]
    event_clusters = state["event_clusters"]

    if not article_ids:
        logger.info("[Pipeline] BiasAgent skipped — no articles")
        return {
            "bias_results": [],
            "stats":        {**state["stats"], "bias": {}},
        }

    logger.info(
        "[Pipeline] BiasAgent starting — %d articles  %d event clusters",
        len(article_ids),
        len(event_clusters),
    )

    try:
        agent = BiasAgent()
        result = await agent.run(article_ids, event_clusters)
    except Exception as e:
        logger.error("[Pipeline] BiasAgent crashed: %s", e)
        return {
            "bias_results": [],
            "stats":        {**state["stats"], "bias": {}},
            "errors":       state["errors"] + [f"[bias] crash: {e}"],
        }

    new_errors = list(state["errors"])
    for e in result.get("errors", []):
        new_errors.append(f"[bias] {e}")

    bias_results = result.get("bias_results", [])
    logger.info(
        "[Pipeline] BiasAgent done — classified=%d stored=%d",
        len(bias_results),
        result.get("stored_count", 0),
    )

    return {
        "bias_results": bias_results,
        "stats": {
            **state["stats"],
            "bias": {
                "classified": len(bias_results),
                "stored":     result.get("stored_count", 0),
            },
        },
        "errors": new_errors,
    }


async def _blindspot_node(state: NewsState) -> dict[str, Any]:
    """
    Node 4 — BlindspotAgent.

    Detects underrepresented political perspectives per event cluster.
    """
    from agents.blindspot_agent import BlindspotAgent

    cluster_ids = state["cluster_ids"]

    if not cluster_ids:
        logger.info("[Pipeline] BlindspotAgent skipped — no event clusters")
        return {
            "blindspots": [],
            "stats":      {**state["stats"], "blindspot": {}},
        }

    logger.info(
        "[Pipeline] BlindspotAgent starting — %d events", len(cluster_ids)
    )

    try:
        agent = BlindspotAgent()
        result = await agent.run(cluster_ids)
    except Exception as e:
        logger.error("[Pipeline] BlindspotAgent crashed: %s", e)
        return {
            "blindspots": [],
            "stats":      {**state["stats"], "blindspot": {}},
            "errors":     state["errors"] + [f"[blindspot] crash: {e}"],
        }

    new_errors = list(state["errors"])
    for e in result.get("errors", []):
        new_errors.append(f"[blindspot] {e}")

    blindspots = result.get("blindspots", [])
    logger.info(
        "[Pipeline] BlindspotAgent done — blindspots=%d stored=%d",
        len(blindspots),
        result.get("stored_count", 0),
    )

    return {
        "blindspots": blindspots,
        "stats": {
            **state["stats"],
            "blindspot": {
                "events_analyzed":      len(cluster_ids),
                "blindspots_found":     len(blindspots),
                "reports_stored":       result.get("stored_count", 0),
            },
        },
        "errors": new_errors,
    }


async def _summary_node(state: NewsState) -> dict[str, Any]:
    """
    Node 5 — SummaryAgent.

    Generates neutral Arabic summaries and bias assessments per event.
    """
    from agents.summary_agent import SummaryAgent

    cluster_ids = state["cluster_ids"]

    if not cluster_ids:
        logger.info("[Pipeline] SummaryAgent skipped — no event clusters")
        return {
            "summaries": [],
            "stats":     {**state["stats"], "summary": {}},
        }

    logger.info(
        "[Pipeline] SummaryAgent starting — %d events", len(cluster_ids)
    )

    try:
        agent = SummaryAgent()
        result = await agent.run(cluster_ids)
    except Exception as e:
        logger.error("[Pipeline] SummaryAgent crashed: %s", e)
        return {
            "summaries": [],
            "stats":     {**state["stats"], "summary": {}},
            "errors":    state["errors"] + [f"[summary] crash: {e}"],
        }

    new_errors = list(state["errors"])
    for e in result.get("errors", []):
        new_errors.append(f"[summary] {e}")

    summaries = result.get("summaries", [])
    logger.info(
        "[Pipeline] SummaryAgent done — summaries=%d stored=%d",
        len(summaries),
        result.get("stored_count", 0),
    )

    return {
        "summaries": summaries,
        "stats": {
            **state["stats"],
            "summary": {
                "events_processed": len(cluster_ids),
                "summaries_stored": result.get("stored_count", 0),
            },
        },
        "errors": new_errors,
    }


async def _recommendation_node(state: NewsState) -> dict[str, Any]:
    """
    Node 6 — RecommendationAgent.

    Finds semantically similar articles with a different bias label for
    each article (up to 10 per run, per plan.md Step 4.4 spec).
    """
    from agents.recommendation_agent import RecommendationAgent

    article_ids = state["article_ids"]
    section     = state["section"]

    if not article_ids:
        logger.info("[Pipeline] RecommendationAgent skipped — no articles")
        return {
            "recommendations": {},
            "stats":           {**state["stats"], "recommendation": {}},
        }

    logger.info(
        "[Pipeline] RecommendationAgent starting — %d articles (max 10)",
        len(article_ids),
    )

    try:
        agent = RecommendationAgent()
        result = await agent.run(article_ids, section)
    except Exception as e:
        logger.error("[Pipeline] RecommendationAgent crashed: %s", e)
        return {
            "recommendations": {},
            "stats":           {**state["stats"], "recommendation": {}},
            "errors":          state["errors"] + [f"[recommendation] crash: {e}"],
        }

    new_errors = list(state["errors"])
    for e in result.get("errors", []):
        new_errors.append(f"[recommendation] {e}")

    recs = result.get("recommendations", {})
    logger.info(
        "[Pipeline] RecommendationAgent done — articles_with_recs=%d",
        len(recs),
    )

    return {
        "recommendations": recs,
        "stats": {
            **state["stats"],
            "recommendation": {
                "articles_processed":   min(len(article_ids), 10),
                "articles_with_recs":   result.get("stored_count", 0),
            },
        },
        "errors": new_errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Graph construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_pipeline():
    """
    Build and compile the LangGraph pipeline.

    Sequential execution: Ingestion → Clustering → Bias → Blindspot →
    Summary → Recommendation → END.
    """
    workflow = StateGraph(NewsState)

    workflow.add_node("ingestion",       _ingestion_node)
    workflow.add_node("clustering",      _clustering_node)
    workflow.add_node("bias",            _bias_node)
    workflow.add_node("blindspot",       _blindspot_node)
    workflow.add_node("summary",         _summary_node)
    workflow.add_node("recommendation",  _recommendation_node)

    workflow.add_edge(START,            "ingestion")
    workflow.add_edge("ingestion",      "clustering")
    workflow.add_edge("clustering",     "bias")
    workflow.add_edge("bias",           "blindspot")
    workflow.add_edge("blindspot",      "summary")
    workflow.add_edge("summary",        "recommendation")
    workflow.add_edge("recommendation", END)

    return workflow.compile()


# ─────────────────────────────────────────────────────────────────────────────
# Result serialisation
# ─────────────────────────────────────────────────────────────────────────────

def _serialise_state(state: NewsState) -> str:
    """
    Convert the final NewsState to a JSON string safe for Redis storage.

    The recommendations dict uses int keys (article_id) which are not valid
    JSON object keys.  They are converted to strings here.
    """
    serialisable = dict(state)

    # Convert int → str keys in recommendations
    raw_recs: dict = state.get("recommendations", {})
    serialisable["recommendations"] = {str(k): v for k, v in raw_recs.items()}

    return json.dumps(serialisable, ensure_ascii=False, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(section: str) -> NewsState:
    """
    Execute the full Veritas Agent pipeline for *section*.

    Parameters
    ----------
    section : str
        One of "middle_east", "libya", or "world".  Must match a key in
        config/sections.py.

    Returns
    -------
    NewsState
        The fully populated final state after all six agents have run.

    Side effects
    ------------
    Stores the final state as JSON in Redis under ``results:{section}`` with
    a 6-hour TTL via the MCP cache_set tool.  The FastAPI layer reads this
    cache (Rule 5.1 cache-first strategy).

    Never raises — any per-agent errors are captured in state["errors"].
    """
    logger.info("[Pipeline] Starting pipeline — section=%s", section)

    initial_state: NewsState = {
        "section":         section,
        "article_ids":     [],
        "cluster_ids":     [],
        "event_clusters":  {},
        "bias_results":    [],
        "blindspots":      [],
        "summaries":       [],
        "recommendations": {},
        "stats":           {},
        "errors":          [],
    }

    pipeline = _build_pipeline()

    try:
        final_state: NewsState = cast(NewsState, await pipeline.ainvoke(initial_state))
    except Exception as e:
        logger.error("[Pipeline] Pipeline graph crashed: %s", e)
        # Return a valid (empty) state so callers can inspect errors.
        initial_state["errors"] = [f"[pipeline] crash: {e}"]
        return initial_state

    logger.info(
        "[Pipeline] Pipeline complete — section=%s articles=%d events=%d "
        "errors=%d",
        section,
        len(final_state.get("article_ids", [])),
        len(final_state.get("cluster_ids", [])),
        len(final_state.get("errors", [])),
    )

    # ── Store results in Redis via MCP (Rule 2.3) ────────────────────────────
    try:
        from agents.base import MCPAgent

        cache_agent = MCPAgent()
        cache_key   = f"results:{section}"
        payload     = _serialise_state(final_state)

        cache_result = await cache_agent.call_tool(
            "cache_set",
            {"key": cache_key, "value": payload, "ttl": _PIPELINE_CACHE_TTL},
        )

        if cache_result.get("success"):
            logger.info(
                "[Pipeline] Results cached — key=%s  ttl=%ds",
                cache_key,
                _PIPELINE_CACHE_TTL,
            )
        else:
            logger.warning(
                "[Pipeline] cache_set returned non-success: %s", cache_result
            )
    except Exception as e:
        logger.error("[Pipeline] Failed to cache results in Redis: %s", e)
        # Non-fatal — pipeline results are still returned to the caller.

    return final_state
