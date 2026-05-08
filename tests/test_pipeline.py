"""
tests/test_pipeline.py — End-to-End Pipeline Integration Test (Phase 4.5)

Verifies that ``run_pipeline('libya')`` completes without crashing and that
the resulting ``NewsState`` carries the canonical schema. This is the
**architecture-level** integration test for Phase 4.5: it asserts the
orchestration contract (every agent runs, every key is populated with the
correct type, the Redis cache is written), but it does NOT assert the
quality of the live LLM/GDELT outputs.

Why "shape-only" assertions?
    Phase 4.5 verifies the canonical-MCP rewrite. Two environmental data
    issues are currently documented in
    ``summaries/phase_4_5/progress_log.md`` Step 4.5.5b:

      1. The bias-chain head ``gemini-1.5-flash`` returns 404 in the
         current environment, leaving ``bias_results`` and downstream
         ``summaries`` empty on most batches.
      2. Recent Libya GDELT batches return entity-empty articles, so
         Condition 3 (entity overlap ≥ 2) fails for every pair and
         ``cluster_ids`` ends empty.

    Both are tracked for Phase 6 — they are NOT a Phase 4.5 regression.
    The pipeline still completes end-to-end without crashing, which is
    the contract this test enforces.

Prerequisites (must all be running before invoking pytest):
    - PostgreSQL 16 with pgvector  (port 5432)
    - Redis                         (port 6379)
    - MCP server:  python -m mcp_server.server   (port 8000)

Runtime:
    Ingestion alone takes 5–8 minutes (live GDELT + scraping + LLM
    relevance filter + embeddings). Full pipeline 8–15 minutes on a
    cold cache. Run with a generous timeout:

        pytest tests/test_pipeline.py -v --timeout=900
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest
import redis as redis_lib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# tests/conftest.py runs ``bootstrap_env()`` at session start (Rule 2.6),
# so REDIS_URL / DATABASE_URL are already populated.
RD_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

_redis_test = redis_lib.from_url(RD_URL, decode_responses=True)

_SECTION = "libya"

# The 10 keys NewsState must carry after ``run_pipeline`` returns. Source:
# ``agents/graph.py``. Adding a key here without updating ``NewsState``
# (or vice versa) means this test will fail loudly — that is intentional.
_REQUIRED_NEWSSTATE_KEYS: tuple[str, ...] = (
    "section",
    "article_ids",
    "cluster_ids",
    "event_clusters",
    "bias_results",
    "blindspots",
    "summaries",
    "recommendations",
    "stats",
    "errors",
)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4.5 — Orchestration / shape integration test
# ─────────────────────────────────────────────────────────────────────────────

async def test_pipeline_libya_end_to_end() -> None:
    """
    Run the full pipeline for the 'libya' section and verify the final
    ``NewsState`` shape and the Redis-cache contract.

    Architecture-level checks (this test guarantees these):
      1.  ``run_pipeline`` returns a dict.
      2.  All 10 ``NewsState`` keys are present.
      3.  Every key has the correct Python type.
      4.  ``state["section"] == "libya"``.
      5.  ``state["article_ids"]`` is non-empty (proves Ingestion ran).
      6.  Every ``article_ids`` entry is an ``int``.
      7.  ``state["stats"]`` contains an ``ingestion`` block.
      8.  Redis key ``results:libya`` was written and is valid JSON.
      9.  Cached payload is non-empty (proves Redis storage ran).
      10. The pipeline did not crash (no exception escaped run_pipeline).

    Data-level checks the test deliberately does NOT make:
      - Non-empty ``cluster_ids``  — depends on entity-richness of GDELT.
      - Non-empty ``summaries``    — depends on bias chain succeeding.
      - Non-empty ``bias_results`` — depends on bias chain succeeding.

    See ``summaries/phase_4_5/progress_log.md`` Step 4.5.5b verification
    for the live-probe evidence and the Phase 6 follow-up plan.
    """
    from agents.graph import run_pipeline, NewsState

    # Clear any previous cache so we get a fresh storage verification.
    try:
        _redis_test.delete(f"results:{_SECTION}")
    except Exception:
        pass

    # ── Check 10: pipeline does not crash ────────────────────────────────────
    state: NewsState = await run_pipeline(_SECTION)

    # ── Check 1: returned object is a dict (NewsState is a TypedDict) ────────
    assert isinstance(state, dict), (
        f"run_pipeline must return a dict (NewsState), got "
        f"{type(state).__name__}"
    )

    # ── Check 2: every NewsState key is present ──────────────────────────────
    for key in _REQUIRED_NEWSSTATE_KEYS:
        assert key in state, (
            f"NewsState missing required key '{key}'. "
            f"Present keys: {sorted(state.keys())}"
        )

    # ── Check 3: type correctness for every NewsState key ────────────────────
    type_specs: dict[str, type | tuple[type, ...]] = {
        "section":         str,
        "article_ids":     list,
        "cluster_ids":     list,
        "event_clusters":  dict,
        "bias_results":    list,
        "blindspots":      list,
        "summaries":       list,
        "recommendations": dict,
        "stats":           dict,
        "errors":          list,
    }
    for key, expected_type in type_specs.items():
        assert isinstance(state[key], expected_type), (
            f"state['{key}'] expected {expected_type}, "
            f"got {type(state[key]).__name__}"
        )

    # ── Check 4: section preserved ────────────────────────────────────────────
    assert state["section"] == _SECTION, (
        f"state['section'] expected '{_SECTION}', got {state['section']!r}"
    )

    # ── Check 5 & 6: article_ids non-empty list of ints ──────────────────────
    article_ids = state["article_ids"]
    assert len(article_ids) >= 1, (
        f"Expected at least 1 article_id (Ingestion ran), got "
        f"{len(article_ids)}.\nPipeline errors: {state.get('errors', [])}"
    )
    for aid in article_ids:
        assert isinstance(aid, int), (
            f"All article_ids must be int, found "
            f"{type(aid).__name__}: {aid!r}"
        )

    # ── Check 7: stats dict contains the ingestion block ─────────────────────
    stats = state["stats"]
    assert "ingestion" in stats, (
        f"state['stats'] missing 'ingestion' key. "
        f"Present: {sorted(stats.keys())}"
    )

    # ── Errors list is tolerated non-empty (pipeline is fault-tolerant) ──────
    errors = state["errors"]
    if errors:
        import warnings
        warnings.warn(
            f"Pipeline completed with {len(errors)} non-fatal errors: "
            f"{errors[:5]}",
            stacklevel=1,
        )

    # ── Check 8 & 9: Redis cache was written with non-empty payload ──────────
    cached = _redis_test.get(f"results:{_SECTION}")
    assert cached is not None, (
        f"Expected Redis key 'results:{_SECTION}' to be set after the run"
    )
    assert isinstance(cached, str), (
        f"Expected Redis value to be a str (decode_responses=True), got "
        f"{type(cached).__name__}"
    )
    cached_state: dict[str, Any] = json.loads(cached)
    assert isinstance(cached_state, dict), (
        "Cached pipeline result is not a JSON object"
    )
    assert cached_state, "Cached pipeline result is an empty dict"

    cached_article_ids = cached_state.get("article_ids", [])
    assert isinstance(cached_article_ids, list)
    assert len(cached_article_ids) >= 1, (
        f"Cached state has empty article_ids: keys={sorted(cached_state.keys())}"
    )

    # ── Documented carry-overs (data-quality, not architecture) ──────────────
    # Empty cluster_ids / summaries / bias_results are tracked in
    # summaries/phase_4_5/progress_log.md Step 4.5.5b. They are NOT a
    # Phase 4.5 regression — Phase 6 evaluation will exercise the full
    # data path with stable bias-chain heads.
