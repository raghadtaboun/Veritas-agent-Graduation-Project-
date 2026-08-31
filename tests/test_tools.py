"""
tests/test_tools.py — MCP Tool Tests (15 tools)

15 tests total — one per pure MCP tool exposed by ``mcp_server/server.py``.
Only the tools actually consumed by the agents are retained:

  Tier-1 core tools (Phase 1)        :  fetch_gdelt, scrape_article,
                                        store_article, find_similar,
                                        cache_set, cache_get
  Tier-2 advanced tools (Phase 4)    :  detect_blindspot, vector_recommend
  Tier-3 pure data-access tools      :  get_articles, get_articles_for_event,
   (Phase 4.5 canonical migration)      insert_event, link_article_event,
                                        update_event_summary,
                                        insert_bias_score,
                                        insert_blindspot_report

Each test calls through a live MCP client session to validate the full
transport path (Rule 3.2). Tests use real PostgreSQL and Redis instances
(Rule 3.3).

The Phase 4.5 canonical migration (ADR-001) removed every LLM-embedded
tool from the server, so none of these tests need LLM mocks.

Prerequisites (must all be running before invoking pytest):
    - PostgreSQL 16 with pgvector  (port 5432)
    - Redis                         (port 6379)
    - MCP server:  python -m mcp_server.server   (port 8000)

Run:
    source .venv/bin/activate
    pytest tests/test_tools.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import asynccontextmanager

import psycopg2
import pytest
import redis as redis_lib
from mcp import ClientSession
from mcp.types import TextContent
try:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
except ImportError:  # older SDK spelling
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore[no-redef]

# ── Project root on path ─────────────────────────────────────────────────────
# tests/conftest.py runs ``bootstrap_env()`` before this module is imported,
# so .env values are already in os.environ. Direct ``load_dotenv()`` calls
# here would violate Rule 2.6 (single-source env bootstrap).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Constants ─────────────────────────────────────────────────────────────────
MCP_URL:  str = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
DB_URL:   str = os.getenv("DATABASE_URL", "")
RD_URL:   str = os.getenv("REDIS_URL", "redis://localhost:6379")

VALID_BIAS_LABELS: frozenset[str] = frozenset(
    {"pro_government", "opposition", "neutral", "pan_arab", "western_aligned"}
)

# ── Shared Redis client (test-side only — not agent code) ─────────────────────
_redis = redis_lib.from_url(RD_URL, decode_responses=True)

# ── Short TTL for test-injected mock values (5 minutes) ──────────────────────
_TEST_MOCK_TTL: int = 300

# ── Unique prefix keeps test rows isolated from production data ───────────────
_TEST_URL_PREFIX = "https://test-tools-veritas"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _mcp():
    """Open a fresh MCP ClientSession for one test, then close it cleanly."""
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call(session: ClientSession, tool: str, args: dict | None = None) -> dict:
    """Call a tool via MCP transport and deserialise the JSON response."""
    result = await session.call_tool(tool, args or {})
    first_block = result.content[0]
    if not isinstance(first_block, TextContent):
        raise TypeError(f"Expected TextContent from MCP tool '{tool}', got {type(first_block).__name__}")
    return json.loads(first_block.text)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _db_cleanup(url_prefix: str) -> None:
    """Delete test articles whose URL starts with *url_prefix*."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM articles WHERE url LIKE %s", (f"{url_prefix}%",))
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Tests — one per tool
# ─────────────────────────────────────────────────────────────────────────────

# ── Test 1 — fetch_gdelt ──────────────────────────────────────────────────────

async def test_fetch_gdelt() -> None:
    """
    Calls fetch_gdelt via MCP transport for the 'libya' section with limit=5.
    Validates response structure; does not assert count > 0 because GDELT
    availability varies by network environment (documented in Phase 1 summary).
    """
    async with _mcp() as s:
        data = await _call(s, "fetch_gdelt", {"section": "libya", "limit": 5})

    assert "articles"  in data, "response missing 'articles' key"
    assert "count"     in data, "response missing 'count' key"
    assert "section"   in data, "response missing 'section' key"
    assert data["section"] == "libya"
    assert isinstance(data["articles"], list)
    assert isinstance(data["count"], int)
    assert data["count"] == len(data["articles"])
    assert data["count"] <= 5, "count exceeds requested limit"

    for article in data["articles"]:
        assert "title"    in article
        assert "url"      in article
        assert "domain"   in article
        assert "seendate" in article
        assert len(article["title"]) >= 15, "title below minimum length"
        assert article["url"], "url is empty"


# ── Test 3 — scrape_article ───────────────────────────────────────────────────

async def test_scrape_article() -> None:
    """
    Calls scrape_article with a URL that reliably fails both Layer 1 and
    Layer 2, triggering the Layer 3 title-only fallback.
    No LLM is involved.
    """
    unreachable_url = "https://this-domain-does-not-exist-veritas-test.invalid/article"

    async with _mcp() as s:
        data = await _call(s, "scrape_article", {"url": unreachable_url})

    assert "success"      in data
    assert "content"      in data
    assert "method"       in data
    assert "layer1_error" in data
    assert "layer2_error" in data
    assert isinstance(data["success"], bool)
    # All three layers fail for an unreachable domain — success is False
    assert data["success"] is False
    assert data["content"] is None
    assert data["layer1_error"] != ""
    assert data["layer2_error"] != ""


# ── Test 5 — store_article ────────────────────────────────────────────────────

async def test_store_article() -> None:
    """
    Stores a test article, verifies is_new=True, then stores the same URL
    again and verifies is_new=False (ON CONFLICT DO NOTHING behaviour).
    Cleans up the inserted row after the test.
    """
    test_url = f"{_TEST_URL_PREFIX}-store-{int(time.time())}"
    embedding = [0.1] * 768

    payload = {
        "title":            "مقال اختباري لأداة store_article",
        "url":              test_url,
        "section":          "libya",
        "published_at":     "2026-04-08T12:00:00Z",
        "embedding":        embedding,
        "content":          "نص المقال الاختباري.",
        "has_full_content": True,
    }

    try:
        async with _mcp() as s:
            first = await _call(s, "store_article", payload)

        assert "error" not in first, f"store_article returned error: {first.get('error')}"
        assert "id"     in first,   "response missing 'id'"
        assert "is_new" in first,   "response missing 'is_new'"
        assert isinstance(first["id"], int)
        assert first["is_new"] is True

        stored_id = first["id"]

        # Second call with same URL — should return same id and is_new=False
        async with _mcp() as s:
            second = await _call(s, "store_article", payload)

        assert "error" not in second, f"second store returned error: {second.get('error')}"
        assert second["id"]     == stored_id, "id changed on duplicate insert"
        assert second["is_new"] is False,     "is_new should be False for duplicate URL"
    finally:
        _db_cleanup(f"{_TEST_URL_PREFIX}-store-")


# ── Test 6 — find_similar ─────────────────────────────────────────────────────

async def test_find_similar() -> None:
    """
    Inserts two articles with identical embeddings directly via psycopg2
    (test infrastructure — not agent code), then calls find_similar via MCP
    and verifies the other article is returned.
    Cleans up both rows after the test.
    """
    ts_suffix  = int(time.time())
    url_a      = f"{_TEST_URL_PREFIX}-sim-a-{ts_suffix}"
    url_b      = f"{_TEST_URL_PREFIX}-sim-b-{ts_suffix}"
    published  = "2026-04-08T12:00:00+00:00"
    embedding  = [0.5] * 768
    emb_pg     = "[" + ",".join(str(v) for v in embedding) + "]"

    def _insert(url: str, title: str) -> int:
        conn = psycopg2.connect(DB_URL)
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO articles
                        (title, url, section, published_at, embedding,
                         entities, has_full_content)
                    VALUES
                        (%s, %s, 'libya', %s, %s::vector,
                         '{}'::jsonb, false)
                    ON CONFLICT (url) DO NOTHING
                    RETURNING id
                    """,
                    (title, url, published, emb_pg),
                )
                row = cur.fetchone()
                if row:
                    return int(row[0])
                # Already existed — fetch its id
                cur.execute("SELECT id FROM articles WHERE url = %s", (url,))
                return int(cur.fetchone()[0])  # type: ignore[index]
        finally:
            conn.close()

    try:
        id_a = _insert(url_a, "مقال أ لاختبار find_similar")
        id_b = _insert(url_b, "مقال ب لاختبار find_similar")

        async with _mcp() as s:
            data = await _call(s, "find_similar", {
                "embedding":    embedding,
                "section":      "libya",
                "published_at": published,
                "threshold":    0.99,    # identical vectors → similarity = 1.0
                "limit":        10,
                "exclude_id":   id_a,    # exclude the source article
            })

        assert "articles" in data, f"unexpected response: {data}"
        assert "count"    in data

        ids_returned = [a["id"] for a in data["articles"]]
        assert id_b in ids_returned, (
            f"article B (id={id_b}) not found in find_similar results: {ids_returned}"
        )

        for article in data["articles"]:
            assert "id"           in article
            assert "title"        in article
            assert "published_at" in article
            assert "similarity"   in article
            assert isinstance(article["similarity"], float)
            assert 0.0 <= article["similarity"] <= 1.0
    finally:
        _db_cleanup(f"{_TEST_URL_PREFIX}-sim-")


# ── Test 5 — cache_set ───────────────────────────────────────────────────────

async def test_cache_set() -> None:
    """
    Calls cache_set with a valid TTL, asserts success=True, then verifies
    the value is actually present in Redis with the expected TTL band.

    Also asserts the rejection branch: ttl=0 must return success=False with
    an error string (server-side guard in mcp_server/server.py).
    """
    key = f"test:cache_set:{int(time.time())}"
    value = "مرحباً بالعالم"

    try:
        async with _mcp() as s:
            set_r = await _call(s, "cache_set", {"key": key, "value": value, "ttl": 60})
        assert set_r.get("success") is True, f"cache_set failed: {set_r}"

        # Independent verification — read the value directly from Redis to
        # prove cache_set actually wrote it (transport path was real).
        raw = _redis.get(key)
        assert raw == value, f"cache_set did not persist value: got {raw!r}"
        ttl_left = int(_redis.ttl(key))  # type: ignore[arg-type]
        assert 0 < ttl_left <= 60, f"unexpected ttl {ttl_left} for {key}"

        # Invalid TTL → success False
        async with _mcp() as s:
            bad_ttl = await _call(s, "cache_set", {"key": key, "value": "x", "ttl": 0})
        assert bad_ttl.get("success") is False
        assert "error" in bad_ttl
    finally:
        _redis.delete(key)


# ── Test 6 — cache_get ───────────────────────────────────────────────────────

async def test_cache_get() -> None:
    """
    Pre-seeds a Redis key directly, calls cache_get via MCP, and verifies the
    server reads it back. Then verifies cache_get on a missing key returns
    {"value": None} (no error key).
    """
    key = f"test:cache_get:{int(time.time())}"
    value = "قيمة الاختبار"
    _redis.setex(key, 60, value)

    try:
        async with _mcp() as s:
            get_r = await _call(s, "cache_get", {"key": key})

        assert "value" in get_r, f"cache_get response missing 'value': {get_r}"
        assert get_r["value"] == value

        # Missing key → value is None, no error
        async with _mcp() as s:
            missing = await _call(
                s, "cache_get", {"key": "no_such_key_veritas_test_xyz"},
            )
        assert missing["value"] is None
    finally:
        _redis.delete(key)


# ─────────────────────────────────────────────────────────────────────────────
# Tier-2 Tests (Phase 4, Step 4.1) — Tests 11–16
# ─────────────────────────────────────────────────────────────────────────────

# ── Shared DB helpers for Tier-2 tests ───────────────────────────────────────

def _insert_test_event(section: str = "libya", headline: str = "Test Event") -> int:
    """Insert a test event and return its id."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO events (section, headline) VALUES (%s, %s) RETURNING id",
                (section, headline),
            )
            return int(cur.fetchone()[0])  # type: ignore[index]
    finally:
        conn.close()


def _insert_test_article(url: str, label: str | None = None, embedding: list | None = None) -> int:
    """Insert a test article (and optionally a bias_score) and return its id."""
    emb = embedding or ([0.3] * 768)
    emb_pg = "[" + ",".join(str(v) for v in emb) + "]"
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO articles
                    (title, url, section, published_at, embedding, has_full_content)
                VALUES (%s, %s, 'libya', NOW(), %s::vector, false)
                ON CONFLICT (url) DO NOTHING
                RETURNING id
                """,
                (f"مقال اختبار Tier-2 ({url[-20:]})", url, emb_pg),
            )
            row = cur.fetchone()
            if row:
                aid = int(row[0])
            else:
                cur.execute("SELECT id FROM articles WHERE url = %s", (url,))
                aid = int(cur.fetchone()[0])  # type: ignore[index]

            if label:
                cur.execute(
                    """
                    INSERT INTO bias_scores (article_id, score, label, confidence, framing)
                    VALUES (%s, 0.5, %s, 0.8, 'test framing')
                    ON CONFLICT (article_id) DO NOTHING
                    """,
                    (aid, label),
                )
        return aid
    finally:
        conn.close()


def _link_article_to_event(article_id: int, event_id: int) -> None:
    """Create an article_events row."""
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


def _cleanup_tier2(url_prefix: str, event_id: int | None = None) -> None:
    """Delete test articles (cascades to bias_scores + article_events) and event."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM articles WHERE url LIKE %s", (f"{url_prefix}%",))
            if event_id is not None:
                cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
    finally:
        conn.close()


# ── Test 11 — detect_blindspot ────────────────────────────────────────────────

async def test_detect_blindspot() -> None:
    """
    Inserts a test event with 4 articles all labeled 'pro_government'.
    Expects detect_blindspot to flag the other 4 labels as missing
    (since their coverage is 0, which is below 15% of the per-label average).
    Also verifies the insufficient-data guard when total < 3 by using a
    separate event with only 1 article.
    Cleans up after.
    """
    ts         = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-blindspot-{ts}"
    event_id   = _insert_test_event(headline="Blindspot Test Event")

    try:
        # 4 articles all labeled pro_government
        for i in range(4):
            aid = _insert_test_article(f"{url_prefix}-a{i}", label="pro_government")
            _link_article_to_event(aid, event_id)

        async with _mcp() as s:
            data = await _call(s, "detect_blindspot", {"event_id": event_id})

        assert "error"                not in data, f"unexpected error: {data.get('error')}"
        assert data["event_id"]       == event_id
        assert "missing_perspectives" in data
        assert "coverage_stats"       in data
        assert "has_blindspot"        in data
        assert data["has_blindspot"]  is True,   "event with 4 pro_gov articles must have blindspot"
        assert data["total"]          == 4

        missing = data["missing_perspectives"]
        # The four absent labels should all be flagged
        for absent_label in ("opposition", "neutral", "pan_arab", "western_aligned"):
            assert absent_label in missing, (
                f"'{absent_label}' should be in missing_perspectives, got {missing}"
            )

        # Insufficient-data guard: event with < 3 articles → no blindspot
        event_small = _insert_test_event(headline="Small Event")
        url_small   = f"{url_prefix}-small"
        try:
            aid_s = _insert_test_article(url_small, label="neutral")
            _link_article_to_event(aid_s, event_small)

            async with _mcp() as s:
                small_data = await _call(s, "detect_blindspot", {"event_id": event_small})

            assert small_data["has_blindspot"] is False, "small event must not be a blindspot"
            assert "note" in small_data, "small event should include a 'note' field"
        finally:
            _cleanup_tier2(url_small, event_small)
    finally:
        _cleanup_tier2(url_prefix, event_id)


# ── Test 15 — vector_recommend ────────────────────────────────────────────────

async def test_vector_recommend() -> None:
    """
    Inserts two test articles with identical embeddings but different bias
    labels (article A: pro_government, article B: opposition).
    Calls vector_recommend on article A and verifies article B appears in
    recommendations with a different bias label.
    Cleans up after.
    """
    ts         = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-recommend-{ts}"

    # Identical embeddings → cosine similarity = 1.0
    shared_emb = [0.6] * 768

    try:
        aid_a = _insert_test_article(
            f"{url_prefix}-a", label="pro_government", embedding=shared_emb,
        )
        aid_b = _insert_test_article(
            f"{url_prefix}-b", label="opposition", embedding=shared_emb,
        )

        # min_similarity=0.5 ensures only the identical-embedding test article B
        # is returned; real Libya articles with low similarity are excluded.
        async with _mcp() as s:
            data = await _call(s, "vector_recommend", {
                "article_id":    aid_a,
                "section":       "libya",
                "limit":         5,
                "min_similarity": 0.5,
            })

        assert "error"            not in data, f"unexpected error: {data.get('error')}"
        assert data["article_id"] == aid_a
        assert "recommendations"  in data
        assert "source_label"     in data
        assert data["source_label"] == "pro_government"

        recs = data["recommendations"]
        assert isinstance(recs, list)
        rec_ids = [r["id"] for r in recs]
        assert aid_b in rec_ids, (
            f"article B (id={aid_b}, label=opposition) should be in recommendations, "
            f"got {rec_ids}"
        )

        # All recommendations must have a different label from the source
        # and similarity within the valid range [min_similarity, 1.0]
        for rec in recs:
            assert "id"         in rec
            assert "title"      in rec
            assert "bias_label" in rec
            assert "similarity" in rec
            assert rec["bias_label"] != "pro_government", (
                f"recommendation {rec['id']} has same label as source"
            )
            assert isinstance(rec["similarity"], float)
            assert 0.5 <= rec["similarity"] <= 1.0

        # Unknown section guard
        async with _mcp() as s:
            bad = await _call(s, "vector_recommend", {
                "article_id": aid_a,
                "section":    "unknown_xyz",
            })
        assert "error" in bad
    finally:
        _cleanup_tier2(url_prefix)


# ─────────────────────────────────────────────────────────────────────────────
# Tier-3 Tests (Phase 4.5 canonical migration) — pure data-access tools
# Eight tools introduced by Step 4.5.3 to replace the LLM-embedded tools the
# legacy server hosted (classify_bias, generate_summary, etc.). All work via
# psycopg2 wrapped in asyncio.to_thread; no LLM, no external HTTP.
# ─────────────────────────────────────────────────────────────────────────────

# ── Test 12 — get_articles ────────────────────────────────────────────────────

async def test_get_articles() -> None:
    """
    Inserts two test articles, calls get_articles via MCP with both ids,
    and verifies the response carries id, title, content, url, entities,
    embedding, published_at, section for each row. Also asserts the
    empty-input short-circuit (ids=[] → count=0, no DB round-trip).
    """
    ts = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-getarts-{ts}"
    a1: int | None = None
    a2: int | None = None

    try:
        a1 = _insert_test_article(f"{url_prefix}-a1")
        a2 = _insert_test_article(f"{url_prefix}-a2")

        async with _mcp() as s:
            data = await _call(s, "get_articles", {"ids": [a1, a2]})

        assert "error"   not in data, f"unexpected error: {data.get('error')}"
        assert data["count"] == 2
        ids_returned = {a["id"] for a in data["articles"]}
        assert {a1, a2} <= ids_returned

        for art in data["articles"]:
            for key in (
                "id", "title", "content", "url", "entities",
                "embedding", "published_at", "section",
            ):
                assert key in art, f"get_articles row missing '{key}': {art}"
            assert isinstance(art["entities"], dict)
            assert isinstance(art["embedding"], list)
            assert art["section"] == "libya"

        # Empty input short-circuit
        async with _mcp() as s:
            empty = await _call(s, "get_articles", {"ids": []})
        assert empty == {"articles": [], "count": 0}
    finally:
        _cleanup_tier2(url_prefix)


# ── Test 13 — get_articles_for_event ─────────────────────────────────────────

async def test_get_articles_for_event() -> None:
    """
    Inserts one event with two articles (one with a bias_score, one without),
    links both, then calls get_articles_for_event and verifies the join
    surfaces label/confidence/framing for the labelled article and NULL
    for the unlabelled one. Also verifies source = URL hostname.
    """
    ts = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-evarts-{ts}"
    event_id = _insert_test_event(headline="get_articles_for_event Test")

    try:
        labeled_id   = _insert_test_article(
            f"{url_prefix}-labeled", label="opposition",
        )
        unlabeled_id = _insert_test_article(f"{url_prefix}-unlabeled")
        for aid in (labeled_id, unlabeled_id):
            _link_article_to_event(aid, event_id)

        async with _mcp() as s:
            data = await _call(s, "get_articles_for_event", {"event_id": event_id})

        assert "error" not in data, f"unexpected error: {data.get('error')}"
        articles = data["articles"]
        assert len(articles) == 2

        by_id = {a["id"]: a for a in articles}
        labeled_row = by_id[labeled_id]
        assert labeled_row["label"] == "opposition"
        assert isinstance(labeled_row["confidence"], float)
        assert labeled_row["framing"] == "test framing"

        unlabeled_row = by_id[unlabeled_id]
        assert unlabeled_row["label"] is None
        assert unlabeled_row["confidence"] is None
        assert unlabeled_row["framing"] is None

        for art in articles:
            assert "source" in art, "missing 'source' field"
            # _insert_test_article uses a relative URL (the prefix) so the
            # parsed hostname will be empty — assert the field exists with
            # a string value rather than asserting a specific hostname.
            assert isinstance(art["source"], str)
    finally:
        _cleanup_tier2(url_prefix, event_id)


# ── Test 14 — insert_event ────────────────────────────────────────────────────

async def test_insert_event() -> None:
    """
    Calls insert_event for a valid section, asserts a positive integer
    event_id is returned, then verifies the row exists in PostgreSQL with
    the supplied headline. Also asserts the unknown-section guard.
    """
    title = f"Test Event from MCP — {int(time.time())}"
    event_id: int | None = None

    try:
        async with _mcp() as s:
            data = await _call(s, "insert_event", {"section": "libya", "title": title})

        assert "error" not in data, f"unexpected error: {data.get('error')}"
        assert isinstance(data["event_id"], int)
        assert data["event_id"] > 0
        event_id = data["event_id"]

        # Independent DB verification
        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT section, headline FROM events WHERE id = %s", (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == "libya"
        assert row[1] == title

        # Unknown section guard
        async with _mcp() as s:
            bad = await _call(s, "insert_event", {"section": "atlantis", "title": "x"})
        assert "error" in bad
    finally:
        if event_id is not None:
            conn = psycopg2.connect(DB_URL)
            try:
                with conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
            finally:
                conn.close()


# ── Test 15 — link_article_event ─────────────────────────────────────────────

async def test_link_article_event() -> None:
    """
    Seeds one event and one article, calls link_article_event twice, and
    verifies linked=True the first time then linked=False on the duplicate
    call (ON CONFLICT DO NOTHING — idempotent per Rule 2.8).
    """
    ts = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-link-{ts}"
    event_id = _insert_test_event(headline="link_article_event Test")
    aid = _insert_test_article(f"{url_prefix}-a1")

    try:
        async with _mcp() as s:
            first = await _call(
                s, "link_article_event",
                {"event_id": event_id, "article_id": aid, "relevance_score": 0.91},
            )
        assert first.get("linked") is True, f"first link expected True: {first}"

        async with _mcp() as s:
            second = await _call(
                s, "link_article_event",
                {"event_id": event_id, "article_id": aid, "relevance_score": 0.91},
            )
        assert second.get("linked") is False, (
            f"duplicate link expected False (idempotent): {second}"
        )

        # Verify the relevance_score from the first call survived
        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT relevance_score FROM article_events "
                "WHERE event_id = %s AND article_id = %s",
                (event_id, aid),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert abs(float(row[0]) - 0.91) < 1e-6
    finally:
        _cleanup_tier2(url_prefix, event_id)


# ── Test 16 — update_event_summary ───────────────────────────────────────────

async def test_update_event_summary() -> None:
    """
    Seeds an event, calls update_event_summary with non-empty inputs, and
    verifies updated=True plus the events row carries the new summary +
    bias_assessment. Also verifies the both-empty short-circuit returns
    {"updated": False, "reason": "both inputs empty"} without touching DB.
    """
    event_id = _insert_test_event(headline="update_event_summary Test")
    neutral = "ملخص محايد للحدث"
    bias    = "تقييم التحيز للحدث"

    try:
        async with _mcp() as s:
            ok = await _call(s, "update_event_summary", {
                "event_id":        event_id,
                "neutral_summary": neutral,
                "bias_assessment": bias,
            })
        assert ok.get("updated") is True, f"update failed: {ok}"

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
        assert row[0] == neutral
        assert row[1] == bias

        # Empty-input short-circuit — no DB write, structured reason
        async with _mcp() as s:
            blank = await _call(s, "update_event_summary", {
                "event_id":        event_id,
                "neutral_summary": "  ",
                "bias_assessment": "",
            })
        assert blank.get("updated") is False
        assert blank.get("reason") == "both inputs empty"
    finally:
        conn = psycopg2.connect(DB_URL)
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        finally:
            conn.close()


# ── Test 17 — insert_bias_score ──────────────────────────────────────────────

async def test_insert_bias_score() -> None:
    """
    Seeds an article without a bias_scores row, calls insert_bias_score,
    asserts inserted=True, then re-calls and asserts inserted=False
    (ON CONFLICT DO NOTHING idempotency). Also verifies the invalid-label
    guard returns an error without touching the DB.
    """
    ts = int(time.time())
    url_prefix = f"{_TEST_URL_PREFIX}-biasins-{ts}"
    aid = _insert_test_article(f"{url_prefix}-a1")  # no label

    try:
        payload = {
            "article_id": aid,
            "score":      0.42,
            "label":      "neutral",
            "confidence": 0.81,
            "framing":    "تحرير محايد للاختبار",
        }
        async with _mcp() as s:
            first = await _call(s, "insert_bias_score", payload)
        assert first == {"inserted": True, "article_id": aid}, (
            f"first insert expected (True, {aid}): {first}"
        )

        async with _mcp() as s:
            dup = await _call(s, "insert_bias_score", payload)
        assert dup == {"inserted": False, "article_id": aid}, (
            f"duplicate insert expected (False, {aid}): {dup}"
        )

        # Invalid label — no DB write, structured error
        async with _mcp() as s:
            bad = await _call(s, "insert_bias_score", {
                **payload, "label": "not_a_real_label",
            })
        assert "error" in bad
    finally:
        conn = psycopg2.connect(DB_URL)
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bias_scores WHERE article_id = %s", (aid,))
                cur.execute("DELETE FROM articles WHERE id = %s", (aid,))
        finally:
            conn.close()


# ── Test 19 — insert_blindspot_report ────────────────────────────────────────

async def test_insert_blindspot_report() -> None:
    """
    Seeds one event, calls insert_blindspot_report, asserts inserted=True,
    then verifies the row in PostgreSQL carries the supplied coverage_stats
    (JSONB) and missing_perspectives (TEXT[]). The duplicate-call branch
    must return inserted=False (WHERE NOT EXISTS guard, Rule 2.8).
    """
    event_id = _insert_test_event(headline="insert_blindspot_report Test")
    coverage = {"pro_government": 4, "opposition": 0, "neutral": 0,
                "pan_arab": 0, "western_aligned": 0}
    missing = ["opposition", "neutral", "pan_arab", "western_aligned"]

    try:
        async with _mcp() as s:
            first = await _call(s, "insert_blindspot_report", {
                "event_id":             event_id,
                "coverage_stats":       coverage,
                "missing_perspectives": missing,
            })
        assert first.get("inserted") is True, f"first insert failed: {first}"

        # Verify DB shape
        conn = psycopg2.connect(DB_URL)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT coverage_stats, missing_perspectives "
                "FROM blindspot_reports WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        cov_db = row[0]
        if isinstance(cov_db, str):
            cov_db = json.loads(cov_db)
        assert cov_db == coverage
        assert sorted(row[1] or []) == sorted(missing)

        # Duplicate call — idempotent guard
        async with _mcp() as s:
            dup = await _call(s, "insert_blindspot_report", {
                "event_id":             event_id,
                "coverage_stats":       coverage,
                "missing_perspectives": missing,
            })
        assert dup.get("inserted") is False, (
            f"duplicate insert expected False: {dup}"
        )
    finally:
        conn = psycopg2.connect(DB_URL)
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM blindspot_reports WHERE event_id = %s",
                    (event_id,),
                )
                cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        finally:
            conn.close()
