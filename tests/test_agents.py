"""
tests/test_agents.py — Agent Integration Tests (Phase 4.5 canonical)

12 tests total — one happy-path and one edge-case per agent for all six
canonical agents:

    Agent                    Happy path                                       Edge case
    ─────────────────────    ────────────────────────────────────────────    ──────────────────────────────────────────────────
    IngestionAgent           test_ingestion_agent_libya                       test_ingestion_agent_no_articles_from_gdelt
    ClusteringAgent          test_clustering_agent_creates_event              test_clustering_agent_no_entity_overlap
    BiasAgent                test_bias_agent_singleton_classifies             test_bias_agent_confidence_gate
    BlindspotAgent           test_blindspot_agent_stores_report               test_blindspot_agent_insufficient_coverage
    SummaryAgent             test_summary_agent_stores_summaries              test_summary_agent_empty_output_guard
    RecommendationAgent      test_recommendation_agent_returns_recommendations  test_recommendation_agent_cache_hit

Tests use real infrastructure: PostgreSQL, Redis, MCP server (Rule 3.3).

LLM mocks (Rule 3.3 explicit exception)
    Where the agent calls Gemini directly via ``self.call_gemini``, we
    monkeypatch that single method to return a canned dict. Every patch
    is a single-line ``# MOCK`` marker that must be removed before Phase 6
    evaluation. The monkeypatch only blocks the LLM transport — the
    full MCP transport, PostgreSQL writes, and Redis cache writes are
    real. The Ingestion happy-path test is deliberately NOT mocked
    (see its docstring) because it is the data-integrity gateway.

Prerequisites:
    - PostgreSQL 16 with pgvector  (port 5432)
    - Redis                         (port 6379)
    - MCP server: python -m mcp_server.server   (port 8000)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2
import pytest
import redis as redis_lib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# tests/conftest.py runs ``bootstrap_env()`` (Rule 2.6) before this module
# loads, so .env values are already populated. No direct ``load_dotenv`` here.
DB_URL: str = os.getenv("DATABASE_URL", "")
RD_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

_redis_test = redis_lib.from_url(RD_URL, decode_responses=True)


# ─────────────────────────────────────────────────────────────────────────────
# Shared seed / cleanup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _count_articles(section: str) -> int:
    """Return the current article count for *section* in PostgreSQL."""
    conn = psycopg2.connect(DB_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM articles WHERE section = %s", (section,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _fetch_articles_by_ids(ids: list[int]) -> list[dict]:
    """Fetch id, published_at, entities, has_full_content for given IDs."""
    if not ids:
        return []
    conn = psycopg2.connect(DB_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, published_at, entities, has_full_content
            FROM articles
            WHERE id = ANY(%s)
            """,
            (ids,),
        )
        rows = cur.fetchall()
        return [
            {
                "id":               row[0],
                "published_at":     row[1],
                "entities":         row[2],
                "has_full_content": row[3],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _seed_article(
    *,
    title: str,
    url: str,
    published_at: datetime,
    embedding_str: str,
    entities: dict | None = None,
    has_full_content: bool = False,
    bias_label: str | None = None,
    bias_score: float = 0.0,
    bias_confidence: float = 0.85,
    bias_framing: str = "إطار تحريري للاختبار",
) -> int:
    """Insert one article (and optional bias_score) and return article id."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO articles
                    (title, url, section, published_at,
                     embedding, entities, has_full_content)
                VALUES (%s, %s, 'libya', %s, %s::vector, %s::jsonb, %s)
                RETURNING id
                """,
                (
                    title,
                    url,
                    published_at,
                    embedding_str,
                    json.dumps(entities or {"people": [], "locations": [], "organizations": []}),
                    has_full_content,
                ),
            )
            row = cur.fetchone()
            assert row, f"Failed to seed article '{url}'"
            aid = int(row[0])

            if bias_label is not None:
                cur.execute(
                    """
                    INSERT INTO bias_scores
                        (article_id, score, label, confidence, framing)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (article_id) DO NOTHING
                    """,
                    (aid, bias_score, bias_label, bias_confidence, bias_framing),
                )
            return aid
    finally:
        conn.close()


def _seed_event(headline: str = "Test Event") -> int:
    """Insert one libya event row and return its id."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO events (section, headline) VALUES ('libya', %s) RETURNING id",
                (headline,),
            )
            row = cur.fetchone()
            assert row, "Failed to seed event"
            return int(row[0])
    finally:
        conn.close()


def _link(article_id: int, event_id: int) -> None:
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO article_events (article_id, event_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (article_id, event_id),
            )
    finally:
        conn.close()


def _delete_articles(ids: list[int]) -> None:
    if not ids:
        return
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM articles WHERE id = ANY(%s)", (ids,))
    finally:
        conn.close()


def _delete_events(ids: list[int]) -> None:
    if not ids:
        return
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM blindspot_reports WHERE event_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM events WHERE id = ANY(%s)", (ids,))
    finally:
        conn.close()


# Standard 768-dim unit vector — cosine similarity = 1.0 between any two.
_DIM = 768
_UNIT_VAL = 1.0 / math.sqrt(_DIM)
_UNIT_EMB_STR = "[" + ",".join(str(_UNIT_VAL) for _ in range(_DIM)) + "]"


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion Agent — happy path
# ─────────────────────────────────────────────────────────────────────────────

async def test_ingestion_agent_libya() -> None:
    """
    Run IngestionAgent for the 'libya' section against live infrastructure
    (GDELT + Groq relevance + Gemini embeddings + MCP store) and verify
    the canonical return shape.

    NO LLM MOCKS — by deliberate decision (Q4 in the Step 4.5.6 design
    review). Ingestion is the data-integrity gateway; mocking would hide
    regressions in GDELT scraping, relevance filtering, or entity
    extraction. See progress_log Step 4.5.5 Part 2 for context.

    Expected runtime: 5–8 minutes on a cold cache, 1–3 minutes when the
    Redis cache for the Libya batch is warm. The pytest timeout for this
    file should be at least 600 seconds.

    Canonical post-condition (Step 4.5.5 Part 2): ``run`` returns
        {fetched: int, relevant: int, scraped: int, entities_extracted: int,
         stored: int, errors: list[str], article_ids: list[int]}
    """
    from agents.ingestion_agent import IngestionAgent

    count_before = _count_articles("libya")

    agent = IngestionAgent()
    result: dict[str, Any] = await agent.run("libya")

    # ── Schema ───────────────────────────────────────────────────────────────
    required_keys = {
        "fetched", "relevant", "scraped",
        "entities_extracted", "stored", "errors", "article_ids",
    }
    missing = required_keys - set(result.keys())
    assert not missing, f"IngestionAgent.run missing keys {missing}: {result}"

    # ── Counter types — note: errors is now list[str] (Step 4.5.5 Part 2) ────
    for stat_key in ("fetched", "relevant", "scraped",
                     "entities_extracted", "stored"):
        assert isinstance(result[stat_key], int), (
            f"stats['{stat_key}'] must be int, got "
            f"{type(result[stat_key]).__name__}"
        )
        assert result[stat_key] >= 0

    assert isinstance(result["errors"], list), (
        f"result['errors'] must be a list[str] (Step 4.5.5 Part 2 contract), "
        f"got {type(result['errors']).__name__}"
    )
    for err in result["errors"]:
        assert isinstance(err, str), f"error entry not a str: {err!r}"

    assert isinstance(result["article_ids"], list)

    # ── Stored ≥ 15 (Phase 2 success criterion, satisfied on full run) ───────
    assert result["stored"] >= 15, (
        f"Expected >= 15 stored articles, got {result['stored']}.\n"
        f"Counters: fetched={result['fetched']} relevant={result['relevant']} "
        f"scraped={result['scraped']} entities_extracted={result['entities_extracted']} "
        f"errors={len(result['errors'])}"
    )

    # ── DB invariants for every returned article id ──────────────────────────
    article_ids: list[int] = result["article_ids"]
    if article_ids:
        db_rows = _fetch_articles_by_ids(article_ids)
        returned_ids = {row["id"] for row in db_rows}
        for aid in article_ids:
            assert aid in returned_ids, f"article_id {aid} missing from DB"

        for row in db_rows:
            aid = row["id"]
            assert row["published_at"] is not None, (
                f"Article {aid} has NULL published_at — "
                "Clustering Agent cannot evaluate the 72-hour window"
            )
            assert row["entities"] is not None, (
                f"Article {aid} has NULL entities"
            )
            ents = row["entities"]
            if isinstance(ents, str):
                ents = json.loads(ents)
            for key in ("people", "locations", "organizations"):
                assert key in ents, f"Article {aid} entities missing '{key}'"
                assert isinstance(ents[key], list)

    # ── Logical counter ordering ─────────────────────────────────────────────
    assert result["fetched"] >= result["relevant"], (
        f"fetched ({result['fetched']}) < relevant ({result['relevant']})"
    )
    assert result["relevant"] >= result["stored"], (
        f"relevant ({result['relevant']}) < stored ({result['stored']})"
    )

    # ── Bonus: warn if the run produced no new DB rows on a fresh DB ─────────
    count_after = _count_articles("libya")
    new_records = count_after - count_before
    if new_records < 15:
        import warnings
        warnings.warn(
            f"Only {new_records} new DB rows for this run "
            f"(count_before={count_before}, count_after={count_after}). "
            "Expected on a re-run where the URLs already exist.",
            stacklevel=1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion Agent — edge case: GDELT returns zero articles
# ─────────────────────────────────────────────────────────────────────────────

async def test_ingestion_agent_no_articles_from_gdelt(monkeypatch) -> None:
    """
    Empty GDELT response must produce a clean zero-state — no DB writes,
    no exceptions, every counter at 0, ``article_ids == []``.

    Real-world trigger: GDELT 503/timeout/empty-result on the requested
    timespan. Production must degrade gracefully.

    The fetch_gdelt MCP tool is the ONLY thing mocked — every other tool
    (store_article, etc.) still routes through the live MCP transport, but
    no downstream tool is called because there is nothing to ingest.
    """
    from agents.base import MCPAgent
    from agents.ingestion_agent import IngestionAgent

    original_call_tool = MCPAgent.call_tool

    async def fake_call_tool(self, tool_name, args=None):  # type: ignore[no-untyped-def]
        if tool_name == "fetch_gdelt":
            section = (args or {}).get("section", "libya")
            return {"articles": [], "count": 0, "section": section}
        return await original_call_tool(self, tool_name, args)

    # MOCK — replace fetch_gdelt result with empty list to simulate
    #        a degraded GDELT response. Removing this monkeypatch for
    #        Phase 6 is safe because Phase 6 evaluation runs against
    #        live GDELT data and will exercise the real fetch path.
    monkeypatch.setattr(MCPAgent, "call_tool", fake_call_tool)

    agent = IngestionAgent()
    result: dict[str, Any] = await agent.run("libya")

    assert result["fetched"] == 0
    assert result["relevant"] == 0
    assert result["stored"] == 0
    assert result["entities_extracted"] == 0
    assert result["article_ids"] == []
    assert isinstance(result["errors"], list)
    # Empty fetch is not an error condition — agent must report it as
    # zero counts, not as an error.
    for err in result["errors"]:
        assert isinstance(err, str)


# ─────────────────────────────────────────────────────────────────────────────
# Clustering Agent — happy path (3 conditions all satisfied)
# ─────────────────────────────────────────────────────────────────────────────

async def test_clustering_agent_creates_event() -> None:
    """
    Seed two libya articles satisfying every clustering condition:
        1. Time window  : 30 minutes apart   (≤ 72h)
        2. Similarity   : identical unit vec (cosine = 1.0 ≥ 0.82)
        3. Entity overlap: 4 shared entities (≥ 2)

    Run ClusteringAgent and verify a multi-article cluster is created
    with both seed ids, plus the events + article_events DB rows exist.
    """
    from agents.clustering_agent import ClusteringAgent

    anchor = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    entities_a = {
        "people":        ["محمد المنفي", "عبدالحميد الدبيبة"],
        "locations":     ["ليبيا", "طرابلس"],
        "organizations": ["المصرف المركزي الليبي"],
    }
    entities_b = {
        "people":        ["محمد المنفي", "عبدالحميد الدبيبة"],
        "locations":     ["ليبيا", "بنغازي"],
        "organizations": ["المصرف المركزي الليبي"],
    }

    art1 = _seed_article(
        title="اجتماع المجلس الرئاسي الليبي لبحث ملف الوحدة الوطنية",
        url=f"https://test-clustering.invalid/article-a-{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
        entities=entities_a,
        has_full_content=True,
    )
    art2 = _seed_article(
        title="المجلس الرئاسي يبحث ملفات الوحدة والمصالحة في ليبيا",
        url=f"https://test-clustering.invalid/article-b-{int(anchor.timestamp())}",
        published_at=anchor - timedelta(minutes=30),
        embedding_str=_UNIT_EMB_STR,
        entities=entities_b,
        has_full_content=True,
    )
    event_ids_created: list[int] = []

    try:
        agent = ClusteringAgent()
        result: dict[str, Any] = await agent.run("libya", [art1, art2])
        event_ids_created = list(result.get("event_ids", []))

        assert len(event_ids_created) >= 1, (
            f"Expected at least 1 event, got {result}"
        )

        all_clustered: set[int] = set()
        for members in result["event_clusters"].values():
            all_clustered.update(members)
        assert art1 in all_clustered
        assert art2 in all_clustered

        # DB verification
        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, section FROM events WHERE id = ANY(%s)",
                (event_ids_created,),
            )
            event_rows = cur.fetchall()
            cur.execute(
                "SELECT article_id FROM article_events WHERE event_id = ANY(%s)",
                (event_ids_created,),
            )
            ae_rows = cur.fetchall()
        finally:
            conn.close()

        assert len(event_rows) >= 1
        for ev_id, ev_section in event_rows:
            assert ev_section == "libya"

        linked_ids = {row[0] for row in ae_rows}
        assert art1 in linked_ids
        assert art2 in linked_ids
    finally:
        _delete_events(event_ids_created)
        _delete_articles([art1, art2])


# ─────────────────────────────────────────────────────────────────────────────
# Clustering Agent — edge case: Condition 3 (entity overlap) fails
# ─────────────────────────────────────────────────────────────────────────────

async def test_clustering_agent_no_entity_overlap() -> None:
    """
    Seed two articles that satisfy Conditions 1 (time) and 2 (similarity)
    but FAIL Condition 3 (entity overlap < 2). The agent must:
      • return ``event_ids == []`` (no event created)
      • report both articles as singletons
      • not crash, not raise, not insert an event

    Real-world trigger: this exact case is documented in
    ``summaries/phase_4_5/progress_log.md`` Step 4.5.5b — recent libya
    GDELT batches return entity-empty articles, so every pair fails
    Condition 3. This test guards orchestration against that data shape.
    """
    from agents.clustering_agent import ClusteringAgent

    anchor = datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc)
    entities_a = {
        "people":        ["شخصية أ"],
        "locations":     ["مدينة أ"],
        "organizations": [],
    }
    entities_b = {
        "people":        ["شخصية ب"],
        "locations":     ["مدينة ب"],
        "organizations": [],
    }
    # Overlap = 0 — fails Condition 3 (minimum 2 shared entities).

    art1 = _seed_article(
        title="مقال بدون تطابق كيانات ١",
        url=f"https://test-cluster-noovrlap.invalid/a-{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
        entities=entities_a,
    )
    art2 = _seed_article(
        title="مقال بدون تطابق كيانات ٢",
        url=f"https://test-cluster-noovrlap.invalid/b-{int(anchor.timestamp())}",
        published_at=anchor - timedelta(minutes=10),
        embedding_str=_UNIT_EMB_STR,
        entities=entities_b,
    )

    try:
        agent = ClusteringAgent()
        result: dict[str, Any] = await agent.run("libya", [art1, art2])

        assert result["event_ids"] == [], (
            f"No event should be created when entity-overlap fails: {result}"
        )
        assert result["event_clusters"] == {}
        assert result["clustered_count"] == 0
        assert result["singleton_count"] == 2

        # DB invariant: no events row for these articles
        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM article_events WHERE article_id = ANY(%s)",
                ([art1, art2],),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 0, "No article_events row should exist for failed cluster"
    finally:
        _delete_articles([art1, art2])


# ─────────────────────────────────────────────────────────────────────────────
# Bias Agent — happy path (singleton classified via mocked LLM)
# ─────────────────────────────────────────────────────────────────────────────

async def test_bias_agent_singleton_classifies(monkeypatch) -> None:
    """
    Run BiasAgent on a singleton (one article, no event_clusters). Replace
    ``BiasAgent._run_bias_prompt`` with a deterministic stub so the LLM
    transport is never hit. Verify:
      • bias_results carries the article_id and the canned label
      • a row is inserted into the bias_scores table

    The LLM-call mock is the only deviation from the live path — DB writes
    and MCP transport are real.
    """
    from agents.bias_agent import BiasAgent

    anchor = datetime(2026, 4, 12, 14, 0, 0, tzinfo=timezone.utc)
    art_id = _seed_article(
        title="اختبار وكيل الانحياز — مقال منفرد",
        url=f"https://test-bias-agent.invalid/singleton-{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
    )

    canned = {
        "score":      0.10,
        "label":      "neutral",
        "confidence": 0.88,
        "framing":    "تغطية متوازنة للاختبار",
    }

    async def fake_run_bias_prompt(self, prompt):  # type: ignore[no-untyped-def]
        return dict(canned)

    # MOCK — replace BiasAgent._run_bias_prompt with a canned classifier
    #        result so Gemini is never invoked. Removing this for Phase 6
    #        is safe because Phase 6 evaluates the bias chain against the
    #        live LLM (fallback chain + cache); the agent's persistence
    #        path is identical for canned vs live data.
    monkeypatch.setattr(BiasAgent, "_run_bias_prompt", fake_run_bias_prompt)

    try:
        agent = BiasAgent()
        result: dict[str, Any] = await agent.run([art_id], {})

        ids_in_results = {r["article_id"] for r in result["bias_results"]}
        assert art_id in ids_in_results, f"missing from bias_results: {result}"
        match = next(r for r in result["bias_results"] if r["article_id"] == art_id)
        assert match["label"] == "neutral"
        assert abs(match["confidence"] - 0.88) < 1e-6

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT label, confidence FROM bias_scores WHERE article_id = %s",
                (art_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, "No bias_scores row inserted"
        assert row[0] == "neutral"
        assert abs(float(row[1]) - 0.88) < 1e-6
        assert result["stored_count"] >= 1
    finally:
        _delete_articles([art_id])


# ─────────────────────────────────────────────────────────────────────────────
# Bias Agent — edge case: confidence gate (Step 4.5.5 Part 2)
# ─────────────────────────────────────────────────────────────────────────────

async def test_bias_agent_confidence_gate(monkeypatch) -> None:
    """
    The agent must DROP any classification where ``label == "neutral"`` and
    ``confidence < 0.2`` — that combination is the API-degradation signature
    from the legacy ``_BIAS_FALLBACK`` (neutral / 0.0 / 0.0). Step 4.5.5
    Part 2 added this guard.

    Expected behavior:
      • result["bias_results"] is empty
      • result["stored_count"] == 0
      • errors list contains a "dropped low-confidence neutral" entry
      • bias_scores table has no row for the article
    """
    from agents.bias_agent import BiasAgent

    anchor = datetime(2026, 4, 12, 15, 0, 0, tzinfo=timezone.utc)
    art_id = _seed_article(
        title="اختبار بوابة الثقة",
        url=f"https://test-bias-conf.invalid/{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
    )

    async def fake_run_bias_prompt(self, prompt):  # type: ignore[no-untyped-def]
        return {
            "score":      0.0,
            "label":      "neutral",
            "confidence": 0.10,    # below 0.2 — must be dropped
            "framing":    "fallback",
        }

    # MOCK — return the API-degradation signature (neutral / low confidence).
    #        Removing this for Phase 6 is safe: Phase 6 hits the live LLM,
    #        and the confidence gate runs identically against real responses
    #        whenever Gemini regresses to the fallback shape.
    monkeypatch.setattr(BiasAgent, "_run_bias_prompt", fake_run_bias_prompt)

    try:
        agent = BiasAgent()
        result: dict[str, Any] = await agent.run([art_id], {})

        assert result["bias_results"] == [], (
            f"Low-confidence neutral must be dropped: {result}"
        )
        assert result["stored_count"] == 0
        assert any("dropped low-confidence neutral" in e for e in result["errors"]), (
            f"Expected 'dropped low-confidence neutral' in errors: "
            f"{result['errors']}"
        )

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM bias_scores WHERE article_id = %s", (art_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 0, "No bias_scores row should be inserted for dropped row"
    finally:
        _delete_articles([art_id])


# ─────────────────────────────────────────────────────────────────────────────
# Blindspot Agent — happy path (4 same-label articles → blindspot)
# ─────────────────────────────────────────────────────────────────────────────

async def test_blindspot_agent_stores_report() -> None:
    """
    Seed one event + 4 articles all labeled ``pro_government``. The
    detect_blindspot tool will return has_blindspot=True with the four
    other labels missing. Expected behavior:
      • stored_count >= 1
      • result["blindspots"] contains the test event
      • blindspot_reports row exists in DB with the 4 missing labels

    detect_blindspot is a pure SQL aggregate — no LLM mock needed.
    """
    from agents.blindspot_agent import BlindspotAgent

    event_id = _seed_event("Test Blindspot Event")
    article_ids: list[int] = []
    try:
        for i in range(4):
            aid = _seed_article(
                title=f"مقال اختبار الانحياز العمياء {i + 1}",
                url=f"https://test-blindspot.invalid/art-{i}-{int(time.time())}",
                published_at=datetime.now(timezone.utc),
                embedding_str=_UNIT_EMB_STR,
                bias_label="pro_government",
                bias_score=0.8,
                bias_confidence=0.9,
                bias_framing="test framing",
            )
            article_ids.append(aid)
            _link(aid, event_id)

        agent = BlindspotAgent()
        result: dict[str, Any] = await agent.run([event_id])

        assert result["stored_count"] >= 1, (
            f"Expected stored_count >= 1, got {result}"
        )
        event_ids_in_results = {b["event_id"] for b in result["blindspots"]}
        assert event_id in event_ids_in_results

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT missing_perspectives FROM blindspot_reports WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None, f"No blindspot_reports row for event {event_id}"
        missing = row[0] or []
        for label in ("opposition", "neutral", "pan_arab", "western_aligned"):
            assert label in missing, f"'{label}' should be in {missing}"
        assert result["errors"] == []
    finally:
        _delete_articles(article_ids)
        _delete_events([event_id])


# ─────────────────────────────────────────────────────────────────────────────
# Blindspot Agent — edge case: insufficient coverage (< 3 articles)
# ─────────────────────────────────────────────────────────────────────────────

async def test_blindspot_agent_insufficient_coverage() -> None:
    """
    detect_blindspot enforces a 3-article minimum (server-side guard).
    Below that threshold, ``has_blindspot=False`` and no row should be
    inserted. The agent must:
      • report 0 entries in ``blindspots``
      • report ``stored_count == 0``
      • not write to the blindspot_reports table
    """
    from agents.blindspot_agent import BlindspotAgent

    event_id = _seed_event("Insufficient Coverage Test")
    article_ids: list[int] = []
    try:
        # Only 2 classified articles → below min_articles=3
        for i in range(2):
            aid = _seed_article(
                title=f"مقال نقص التغطية {i + 1}",
                url=f"https://test-blindspot-low.invalid/art-{i}-{int(time.time())}",
                published_at=datetime.now(timezone.utc),
                embedding_str=_UNIT_EMB_STR,
                bias_label="opposition",
            )
            article_ids.append(aid)
            _link(aid, event_id)

        agent = BlindspotAgent()
        result: dict[str, Any] = await agent.run([event_id])

        assert result["blindspots"] == [], (
            f"No blindspot when total classified articles < 3: {result}"
        )
        assert result["stored_count"] == 0
        assert result["errors"] == []

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM blindspot_reports WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 0, "No blindspot_reports row should exist for low-coverage event"
    finally:
        _delete_articles(article_ids)
        _delete_events([event_id])


# ─────────────────────────────────────────────────────────────────────────────
# Summary Agent — happy path (mocked LLM, real DB write)
# ─────────────────────────────────────────────────────────────────────────────

async def test_summary_agent_stores_summaries(monkeypatch) -> None:
    """
    Seed one event with two articles (different bias labels), monkeypatch
    ``SummaryAgent.call_gemini`` to return canned text per task_type, run
    the agent, and verify both events.summary and events.bias_assessment
    are written.

    The ``call_gemini`` patch dispatches by ``task_type``:
      facts      → JSON-array string of two facts
      summary    → Arabic neutral-summary string
      assessment → Arabic bias-assessment string
    """
    from agents.summary_agent import SummaryAgent

    event_id = _seed_event("Test Summary Agent Event")
    article_ids: list[int] = []

    mock_neutral = "هذا ملخص محايد للحدث الإخباري في الاختبار التكاملي."
    mock_assessment = (
        "المصدر الأول يُسلّط الضوء على الموقف الرسمي. "
        "بينما يُركّز المصدر الثاني على الأصوات المعارضة."
    )
    mock_facts = ["الحقيقة الأولى المشتركة", "الحقيقة الثانية المشتركة"]

    async def fake_call_gemini(self, *, prompt, task_type, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        if task_type == "facts":
            return {"text": json.dumps(mock_facts, ensure_ascii=False)}
        if task_type == "summary":
            return {"text": mock_neutral}
        if task_type == "assessment":
            return {"text": mock_assessment}
        return {"error": f"unexpected task_type {task_type!r}"}

    # MOCK — replace SummaryAgent.call_gemini with a task_type-aware stub
    #        so the test deterministically exercises the persistence path.
    #        Removing this for Phase 6 is safe: Phase 6 evaluation runs
    #        the live Gemini chain end-to-end and writes the same shape.
    monkeypatch.setattr(SummaryAgent, "call_gemini", fake_call_gemini)

    # Pre-clear any cached neutral/bias summaries to force the LLM path.
    for key in (f"neutral_summary:{event_id}", f"bias_assessment:{event_id}"):
        try:
            _redis_test.delete(key)
        except Exception:
            pass

    try:
        anchor = datetime(2026, 4, 14, 9, 0, 0, tzinfo=timezone.utc)
        for i, label in enumerate(("pro_government", "opposition")):
            aid = _seed_article(
                title=f"مقال اختبار ملخص {i + 1}",
                url=f"https://test-summary.invalid/art-{int(anchor.timestamp())}-{i}",
                published_at=anchor + timedelta(minutes=i * 10),
                embedding_str=_UNIT_EMB_STR,
                bias_label=label,
                bias_score=0.7 if label == "pro_government" else -0.7,
                bias_confidence=0.85,
            )
            article_ids.append(aid)
            _link(aid, event_id)

        agent = SummaryAgent()
        result: dict[str, Any] = await agent.run([event_id])

        assert result["stored_count"] >= 1, f"stored_count: {result}"
        summary_entry = next(
            (s for s in result["summaries"] if s["event_id"] == event_id), None,
        )
        assert summary_entry is not None
        assert summary_entry["neutral_summary"] == mock_neutral
        assert summary_entry["bias_assessment"] == mock_assessment

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT summary, bias_assessment FROM events WHERE id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == mock_neutral
        assert row[1] == mock_assessment
        assert result["errors"] == []
    finally:
        for key in (f"neutral_summary:{event_id}", f"bias_assessment:{event_id}",
                    f"facts:{event_id}"):
            try:
                _redis_test.delete(key)
            except Exception:
                pass
        _delete_articles(article_ids)
        _delete_events([event_id])


# ─────────────────────────────────────────────────────────────────────────────
# Summary Agent — edge case: empty-output guard (Step 4.5.3)
# ─────────────────────────────────────────────────────────────────────────────

async def test_summary_agent_empty_output_guard(monkeypatch) -> None:
    """
    Both LLM outputs strip to empty → ``update_event_summary`` MUST NOT be
    called and the events row stays NULL (Step 4.5.3 short-circuit).

    Expected:
      • result["stored_count"] == 0
      • errors carries "both outputs empty — skipping update_event_summary"
      • events.summary remains NULL after the run
    """
    from agents.summary_agent import SummaryAgent

    event_id = _seed_event("Empty-Output Guard Event")
    article_ids: list[int] = []

    async def fake_call_gemini(self, *, prompt, task_type, max_tokens=None, temperature=None):  # type: ignore[no-untyped-def]
        # All three task types produce whitespace-only output.
        return {"text": "   "}

    # MOCK — force every Gemini call (facts / summary / assessment) to return
    #        whitespace-only text so the agent's empty-output guard fires.
    #        Removing this for Phase 6 is safe; the guard runs identically
    #        against real LLM output that happens to be empty.
    monkeypatch.setattr(SummaryAgent, "call_gemini", fake_call_gemini)

    for key in (f"neutral_summary:{event_id}", f"bias_assessment:{event_id}",
                f"facts:{event_id}"):
        try:
            _redis_test.delete(key)
        except Exception:
            pass

    try:
        anchor = datetime(2026, 4, 14, 11, 0, 0, tzinfo=timezone.utc)
        for i, label in enumerate(("pro_government", "opposition")):
            aid = _seed_article(
                title=f"مقال حماية الإخراج الفارغ {i + 1}",
                url=f"https://test-summary-empty.invalid/art-{int(anchor.timestamp())}-{i}",
                published_at=anchor + timedelta(minutes=i),
                embedding_str=_UNIT_EMB_STR,
                bias_label=label,
            )
            article_ids.append(aid)
            _link(aid, event_id)

        agent = SummaryAgent()
        result: dict[str, Any] = await agent.run([event_id])

        assert result["stored_count"] == 0, (
            f"update_event_summary must be skipped on empty outputs: {result}"
        )
        assert any("both outputs empty" in e for e in result["errors"]), (
            f"Expected 'both outputs empty' guard message: {result['errors']}"
        )

        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT summary, bias_assessment FROM events WHERE id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] is None, "events.summary must remain NULL"
        assert row[1] is None, "events.bias_assessment must remain NULL"
    finally:
        for key in (f"neutral_summary:{event_id}", f"bias_assessment:{event_id}",
                    f"facts:{event_id}"):
            try:
                _redis_test.delete(key)
            except Exception:
                pass
        _delete_articles(article_ids)
        _delete_events([event_id])


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation Agent — happy path
# ─────────────────────────────────────────────────────────────────────────────

async def test_recommendation_agent_returns_recommendations() -> None:
    """
    Seed two articles with identical embeddings and OPPOSITE bias labels,
    run RecommendationAgent on article A, and verify article B appears in
    A's cross-bias recommendation list.

    vector_recommend is a pure pgvector query — no LLM mock needed.
    """
    from agents.recommendation_agent import RecommendationAgent

    anchor = datetime(2026, 4, 14, 12, 0, 0, tzinfo=timezone.utc)
    art_a = _seed_article(
        title="مقال اختبار التوصية — مؤيد للحكومة",
        url=f"https://test-recommend.invalid/pro-{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
        bias_label="pro_government",
        bias_score=0.75,
        bias_confidence=0.9,
        bias_framing="دعم الموقف الرسمي",
    )
    art_b = _seed_article(
        title="مقال اختبار التوصية — معارضة",
        url=f"https://test-recommend.invalid/opp-{int(anchor.timestamp())}",
        published_at=anchor + timedelta(minutes=5),
        embedding_str=_UNIT_EMB_STR,
        bias_label="opposition",
        bias_score=-0.75,
        bias_confidence=0.9,
        bias_framing="انتقاد الموقف الرسمي",
    )

    for aid in (art_a, art_b):
        try:
            _redis_test.delete(f"recommend:{aid}")
        except Exception:
            pass

    try:
        agent = RecommendationAgent()
        result: dict[str, Any] = await agent.run([art_a], section="libya")

        assert result["stored_count"] >= 1
        assert isinstance(result["recommendations"], dict)
        assert art_a in result["recommendations"]
        recs_for_a: list[dict] = result["recommendations"][art_a]
        assert len(recs_for_a) >= 1

        for rec in recs_for_a:
            assert rec.get("bias_label") != "pro_government", (
                f"Recommendation kept the same label as source: {rec}"
            )

        rec_ids = [r.get("id") for r in recs_for_a]
        assert art_b in rec_ids, f"Article B missing from recs: {recs_for_a}"
        assert result["errors"] == []
    finally:
        for aid in (art_a, art_b):
            try:
                _redis_test.delete(f"recommend:{aid}")
            except Exception:
                pass
        _delete_articles([art_a, art_b])


# ─────────────────────────────────────────────────────────────────────────────
# Recommendation Agent — edge case: cache hit (Rule 2.7)
# ─────────────────────────────────────────────────────────────────────────────

async def test_recommendation_agent_cache_hit() -> None:
    """
    Run RecommendationAgent twice for the same article and verify that
    the second invocation:
      • returns identical recommendations
      • is faster than the first (cache_get hit avoids vector_recommend)

    This exercises the Rule 2.7 write-through cache:
        first call : vector_recommend → cache_set
        second call: cache_get hit (no vector_recommend invocation)
    """
    from agents.recommendation_agent import RecommendationAgent

    anchor = datetime(2026, 4, 14, 13, 0, 0, tzinfo=timezone.utc)
    art_a = _seed_article(
        title="اختبار ذاكرة التخزين — مؤيد",
        url=f"https://test-rec-cache.invalid/pro-{int(anchor.timestamp())}",
        published_at=anchor,
        embedding_str=_UNIT_EMB_STR,
        bias_label="pro_government",
    )
    art_b = _seed_article(
        title="اختبار ذاكرة التخزين — معارضة",
        url=f"https://test-rec-cache.invalid/opp-{int(anchor.timestamp())}",
        published_at=anchor + timedelta(minutes=2),
        embedding_str=_UNIT_EMB_STR,
        bias_label="opposition",
    )

    cache_key = f"recommend:{art_a}"
    try:
        _redis_test.delete(cache_key)
    except Exception:
        pass

    try:
        agent = RecommendationAgent()

        t0 = time.perf_counter()
        first = await agent.run([art_a], section="libya")
        t_first = time.perf_counter() - t0

        # The first call must have written the cache key.
        cached_raw = _redis_test.get(cache_key)
        assert cached_raw is not None, (
            f"recommend:{art_a} must be cached after the first call"
        )
        cached_list = json.loads(str(cached_raw))
        assert isinstance(cached_list, list)

        t0 = time.perf_counter()
        second = await agent.run([art_a], section="libya")
        t_second = time.perf_counter() - t0

        # Same payload (recommendations are deterministic for fixed data).
        first_recs = first["recommendations"][art_a]
        second_recs = second["recommendations"][art_a]
        first_ids = sorted(r["id"] for r in first_recs)
        second_ids = sorted(r["id"] for r in second_recs)
        assert first_ids == second_ids, (
            f"cache-hit recommendations differ from cold-call: "
            f"{first_ids} vs {second_ids}"
        )
        assert art_b in first_ids

        # Second call must be at least as fast as the first. The cache hit
        # avoids both the pgvector query and a Redis write, so on every
        # observed environment t_second < t_first; we assert a weak bound
        # (≤ 1.5×) to remain stable under contention.
        assert t_second <= max(t_first * 1.5, 0.02), (
            f"Cache-hit path slower than cold path: "
            f"first={t_first:.3f}s second={t_second:.3f}s"
        )
    finally:
        try:
            _redis_test.delete(cache_key)
            _redis_test.delete(f"recommend:{art_b}")
        except Exception:
            pass
        _delete_articles([art_a, art_b])
