"""
tests/test_api.py — Smoke tests for the FastAPI backend (Phase 5 Steps 5.1a + 5.1b).

These tests exercise the six endpoints in the API:
  * GET /api/health
  * GET /api/home
  * GET /api/statistics
  * GET /api/events                (5.1b)
  * GET /api/events/{event_id}     (5.1b)
  * GET /api/sections/{section}    (5.1b)

PostgreSQL and Redis are mocked at the connection-helper boundary so the
tests run in any environment — no live infrastructure required. This is
the spec's "Tests must run without a running MCP server" rule applied at
maximum strictness: no MCP server, no PostgreSQL, no Redis.

The mocking strategy uses ``unittest.mock.patch`` to substitute:
  * ``api.main.get_db_conn``     → returns a stub conn whose cursor yields
                                   the rows the test wants.
  * ``api.main.get_redis``       → returns a fake redis with controllable
                                   ``get``/``ttl``/``ping``/``exists``.
  * ``api.main.cache_get_json``  → returns None (force cache miss) or a
                                   dict (simulate cache hit).
  * ``api.main.cache_set_json``  → no-op (capture is not needed here).

Each test composes the minimal mock surface for its scenario — the cursor
returns different fetchone/fetchall sequences depending on what the
endpoint will execute.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# tests/conftest.py has already called bootstrap_env() at session start, so
# importing the app here is safe. The bootstrap is idempotent.
from api.main import app


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class _FakeCursor:
    """Cursor stub that returns canned responses in FIFO order.

    Each test appends ``fetchone``/``fetchall`` results to the cursor's
    queues before issuing the request. The cursor pops the next result
    whenever the endpoint calls ``cur.execute`` followed by a fetch.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_q: list[Any] = []
        self._fetchall_q: list[Any] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, sql: str, args: Any = None) -> None:
        self.executed.append((sql, args))

    def fetchone(self) -> Any:
        return self._fetchone_q.pop(0)

    def fetchall(self) -> Any:
        return self._fetchall_q.pop(0)

    def enqueue_one(self, value: Any) -> None:
        self._fetchone_q.append(value)

    def enqueue_all(self, value: Any) -> None:
        self._fetchall_q.append(value)


def _make_conn(cur: _FakeCursor) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.close = MagicMock()
    return conn


def _make_redis(
    *,
    ping_ok: bool = True,
    get_map: dict[str, str] | None = None,
    ttl_map: dict[str, int] | None = None,
    exists_map: dict[str, bool] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.ping = MagicMock(return_value=ping_ok)

    def _get(key: str) -> str | None:
        return (get_map or {}).get(key)

    def _ttl(key: str) -> int:
        return (ttl_map or {}).get(key, -2)

    def _exists(key: str) -> int:
        return 1 if (exists_map or {}).get(key, False) else 0

    r.get = MagicMock(side_effect=_get)
    r.ttl = MagicMock(side_effect=_ttl)
    r.exists = MagicMock(side_effect=_exists)
    return r


# ─────────────────────────────────────────────────────────────────────────────
# /api/health
# ─────────────────────────────────────────────────────────────────────────────

def test_health_ok_when_db_and_redis_reachable(client: TestClient) -> None:
    cur = _FakeCursor()
    cur.enqueue_one({"col": 1})  # SELECT 1
    conn = _make_conn(cur)

    fake_redis = _make_redis(
        ping_ok=True,
        # No results:{section} keys present → last_pipeline_run should be null.
        ttl_map={},
    )

    with (
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
        patch(
            "api.main._get_gemini_pool_status",
            return_value={"total_keys": 8, "quarantined": 0, "available": 8},
        ),
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "status", "db", "redis", "gemini_pool",
        "last_pipeline_run", "version",
    }
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert body["redis"] == "connected"
    assert body["gemini_pool"] == {"total_keys": 8, "quarantined": 0, "available": 8}
    assert body["last_pipeline_run"] is None
    assert body["version"] == "0.5.0"


def test_health_503_when_db_disconnected(client: TestClient) -> None:
    import psycopg2

    fake_redis = _make_redis(ping_ok=True)

    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise psycopg2.OperationalError("could not connect to server")

    with (
        patch("api.main.get_db_conn", side_effect=_raise),
        patch("api.main.get_redis", return_value=fake_redis),
        patch(
            "api.main._get_gemini_pool_status",
            return_value={"available": None, "note": "pool not introspected from API"},
        ),
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "disconnected"


def test_health_last_pipeline_run_from_ttl(client: TestClient) -> None:
    """When results:libya has TTL=21000, last_run should be ~600s ago."""
    cur = _FakeCursor()
    cur.enqueue_one({"col": 1})
    conn = _make_conn(cur)

    fake_redis = _make_redis(
        ping_ok=True,
        ttl_map={"results:libya": 21000, "results:middle_east": -2, "results:world": -2},
    )

    with (
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
        patch(
            "api.main._get_gemini_pool_status",
            return_value={"available": None, "note": "stub"},
        ),
    ):
        resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["last_pipeline_run"] is not None
    # Format check: ISO-8601 with timezone offset present.
    assert "T" in body["last_pipeline_run"]
    assert ("+" in body["last_pipeline_run"]) or body["last_pipeline_run"].endswith("Z")


# ─────────────────────────────────────────────────────────────────────────────
# /api/home
# ─────────────────────────────────────────────────────────────────────────────

def test_home_pending_when_db_empty_and_no_cache(client: TestClient) -> None:
    """Truly empty system: 0 articles, 0 events, no results:* keys → pending."""
    cur = _FakeCursor()
    # _is_system_pending: COUNT(articles), COUNT(events)
    cur.enqueue_one({"n_articles": 0})
    cur.enqueue_one({"n_events": 0})
    conn = _make_conn(cur)

    fake_redis = _make_redis(ping_ok=True, exists_map={})

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/home")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "pending", "message": "Pipeline has not run yet."}


def test_home_serves_cached_when_present(client: TestClient) -> None:
    cached_payload = {
        "sections":      ["libya", "middle_east", "world"],
        "latest_news":   [],
        "latest_events": [],
    }
    with patch("api.main.cache_get_json", return_value=cached_payload):
        resp = client.get("/api/home")

    assert resp.status_code == 200
    assert resp.json() == cached_payload


def test_home_full_payload_shape(client: TestClient) -> None:
    """One article, one event, one bias row → assert top-level keys + types."""
    cur = _FakeCursor()
    # 1. _is_system_pending: COUNT(articles) and COUNT(events)
    cur.enqueue_one({"n_articles": 1})
    cur.enqueue_one({"n_events": 1})

    # 2. _fetch_latest_news returns one row.
    cur.enqueue_all([
        {
            "id": 1, "title": "Test", "url": "https://aljazeera.net/x",
            "source": "aljazeera.net", "section": "libya",
            "published_at": __import__("datetime").datetime(
                2026, 5, 12, 13, 0, 0,
                tzinfo=__import__("datetime").timezone.utc,
            ),
            "bias_label": "pan_arab", "bias_score": 0.8, "bias_framing": "framing",
        }
    ])

    # 3. _fetch_recent_events returns one event.
    cur.enqueue_all([
        {
            "id": 100, "headline": "Event 1", "section": "libya",
            "summary": "neutral summary", "bias_assessment": "assessment",
            "created_at": __import__("datetime").datetime(
                2026, 5, 12, 10, 0, 0,
                tzinfo=__import__("datetime").timezone.utc,
            ),
        }
    ])

    # 4. _fetch_event_articles for event 100: returns the same article.
    cur.enqueue_all([
        {
            "id": 1, "title": "Test", "url": "https://aljazeera.net/x",
            "source": "aljazeera.net",
            "bias_label": "pan_arab", "bias_score": 0.8, "bias_framing": "framing",
        }
    ])

    # 5. _fetch_event_blindspot for event 100.
    cur.enqueue_one({"missing_perspectives": ["pro_government"]})

    conn = _make_conn(cur)

    # Redis: no recommend cache for article 1 → empty recommendations.
    fake_redis = _make_redis(
        ping_ok=True,
        get_map={},
        exists_map={"results:libya": True},  # not pending
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/home")

    assert resp.status_code == 200
    body = resp.json()

    # Top-level keys per spec §8.2.
    assert set(body.keys()) == {"sections", "latest_news", "latest_events"}
    assert body["sections"] == ["libya", "middle_east", "world"]
    assert len(body["latest_news"]) == 1
    assert len(body["latest_events"]) == 1

    # Latest news item shape.
    n0 = body["latest_news"][0]
    assert set(n0.keys()) == {
        "id", "title", "url", "source", "section", "published_at", "bias",
    }
    assert n0["bias"]["label"] == "pan_arab"
    assert n0["bias"]["score"] == 0.8

    # Event shape.
    e0 = body["latest_events"][0]
    for key in (
        "id", "headline", "section", "article_count", "created_at",
        "summary", "bias_assessment", "articles", "blindspot", "recommendations",
    ):
        assert key in e0, f"event missing key: {key}"
    assert e0["article_count"] == 1
    assert e0["blindspot"] == {"missing_perspectives": ["pro_government"]}
    assert e0["recommendations"] == []  # no cache → empty list


# ─────────────────────────────────────────────────────────────────────────────
# /api/statistics
# ─────────────────────────────────────────────────────────────────────────────

def test_statistics_pending_when_empty(client: TestClient) -> None:
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 0})
    cur.enqueue_one({"n_events": 0})
    conn = _make_conn(cur)

    fake_redis = _make_redis(ping_ok=True, exists_map={})

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/statistics")

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "message": "Pipeline has not run yet."}


def test_statistics_shape_with_empty_funnel_note(client: TestClient) -> None:
    """When results:{section} keys are missing, pipeline_funnel_note appears."""
    cur = _FakeCursor()

    # 1. _is_system_pending
    cur.enqueue_one({"n_articles": 5})
    cur.enqueue_one({"n_events": 1})

    # 2. _fetch_kpis (one SELECT returning a single dict)
    cur.enqueue_one({
        "articles": 5, "events": 1, "blindspots": 0, "sources": 3,
    })

    # 3. _fetch_funnel_from_db (4 separate queries)
    cur.enqueue_one({"n": 4})   # scraped
    cur.enqueue_one({"n": 3})   # entities_extracted
    cur.enqueue_one({"n": 2})   # bias_classified
    cur.enqueue_one({"n": 1})   # in_events

    # The fetchall() call order in _build_statistics_payload is:
    # (1) articles_per_section, (2) events_per_section,
    # (3) blindspots_per_section, then inside the StatisticsResponse(...)
    # kwargs, evaluated left-to-right:
    # (4) bias_distribution, (5) articles_per_source,
    # (6) avg_confidence_per_label, (7) articles_per_event_distribution.

    # 4. articles_per_section (returns 2 sections; the third pads to 0)
    cur.enqueue_all([{"section": "libya", "count": 3}, {"section": "world", "count": 2}])

    # 5. events_per_section
    cur.enqueue_all([{"section": "libya", "count": 1}])

    # 6. blindspots_per_section
    cur.enqueue_all([])

    # 7. bias_distribution
    cur.enqueue_all([{"label": "pan_arab", "count": 2}, {"label": "neutral", "count": 1}])

    # 8. articles_per_source
    cur.enqueue_all([{"source": "aljazeera.net", "count": 5}])

    # 9. avg_confidence_per_label
    cur.enqueue_all([{"label": "pan_arab", "avg_conf": 0.85}])

    # 10. articles_per_event_distribution
    cur.enqueue_all([{"bucket": "1", "count": 1}])

    conn = _make_conn(cur)

    # Redis: exists() returns True so we are not pending; get() returns None
    # for every results:* key so the funnel note path fires.
    fake_redis = _make_redis(
        ping_ok=True,
        get_map={},
        exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/statistics")

    assert resp.status_code == 200
    body = resp.json()

    # Top-level keys (with the funnel note present).
    expected_keys = {
        "kpis", "pipeline_funnel", "pipeline_funnel_note",
        "bias_distribution", "articles_per_section", "articles_per_source",
        "events_per_section", "blindspots_per_section",
        "avg_confidence_per_label", "articles_per_event_distribution",
    }
    assert set(body.keys()) == expected_keys

    assert body["kpis"] == {"articles": 5, "events": 1, "blindspots": 0, "sources": 3}

    # Funnel should have 4 entries (no fetched/relevant), note set.
    assert len(body["pipeline_funnel"]) == 4
    assert body["pipeline_funnel_note"]  # non-empty string

    # Each per-section list contains all 3 sections (padding worked).
    assert {s["section"] for s in body["articles_per_section"]} == {
        "libya", "middle_east", "world",
    }
    assert {s["section"] for s in body["events_per_section"]} == {
        "libya", "middle_east", "world",
    }
    assert {s["section"] for s in body["blindspots_per_section"]} == {
        "libya", "middle_east", "world",
    }


# ─────────────────────────────────────────────────────────────────────────────
# /api/events  (Step 5.1b)
# ─────────────────────────────────────────────────────────────────────────────

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> Any:
    """Tiny helper: build a UTC datetime so the mocked rows look like
    psycopg2 ``timestamptz`` values."""
    import datetime as _dt
    return _dt.datetime(year, month, day, hour, minute, tzinfo=_dt.timezone.utc)


def test_events_invalid_section_400(client: TestClient) -> None:
    """An invalid ?section value must return HTTP 400 — no DB touched."""
    with patch("api.main.cache_get_json", return_value=None):
        resp = client.get("/api/events?section=foo")

    assert resp.status_code == 400
    assert resp.json() == {"detail": "invalid section"}


def test_events_pending_when_empty(client: TestClient) -> None:
    """0 articles + 0 events + no Redis results:* keys → pending payload."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 0})
    cur.enqueue_one({"n_events": 0})
    conn = _make_conn(cur)

    fake_redis = _make_redis(ping_ok=True, exists_map={})

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events?section=libya&page=1&per_page=2")

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "message": "Pipeline has not run yet."}


def test_events_shape_and_pagination(client: TestClient) -> None:
    """Successful response: total/page/per_page + 1 expanded event payload."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})

    cur.enqueue_one({"total": 5})

    cur.enqueue_all([
        {
            "id": 100, "headline": "Event 1", "section": "libya",
            "summary": "neutral summary", "bias_assessment": "assessment",
            "created_at": _utc(2026, 5, 12, 10, 0),
        }
    ])

    cur.enqueue_all([
        {
            "id": 1, "title": "Article 1", "url": "https://aljazeera.net/x",
            "source": "aljazeera.net",
            "bias_label": "pan_arab", "bias_score": 0.8, "bias_framing": "framing",
        }
    ])
    cur.enqueue_one({"missing_perspectives": ["pro_government"]})

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True,
        get_map={},
        exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events?section=libya&page=1&per_page=2")

    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"total", "page", "per_page", "events"}
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["per_page"] == 2
    assert len(body["events"]) == 1

    e0 = body["events"][0]
    for key in (
        "id", "headline", "section", "article_count", "created_at",
        "summary", "bias_assessment", "articles", "blindspot", "recommendations",
    ):
        assert key in e0, f"event missing key: {key}"
    assert e0["article_count"] == 1
    assert e0["blindspot"] == {"missing_perspectives": ["pro_government"]}


def test_events_section_filter_passes_libya_to_sql(client: TestClient) -> None:
    """When section=libya, the COUNT/SELECT SQL must filter by section."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one({"total": 0})
    cur.enqueue_all([])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events?section=libya&page=1&per_page=2")

    assert resp.status_code == 200

    events_sql_calls = [
        (sql, args) for sql, args in cur.executed
        if "FROM events" in sql and "n_events" not in sql
    ]
    assert len(events_sql_calls) >= 2
    for sql, args in events_sql_calls:
        assert "section = %s" in sql
        assert "libya" in (args or [])


def test_events_section_all_does_not_filter(client: TestClient) -> None:
    """When section=all (default), no section filter appears in the SQL."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one({"total": 0})
    cur.enqueue_all([])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events")

    assert resp.status_code == 200

    events_sql_calls = [
        (sql, args) for sql, args in cur.executed
        if "FROM events" in sql and "n_events" not in sql
    ]
    for sql, _args in events_sql_calls:
        assert "section = %s" not in sql


# ─────────────────────────────────────────────────────────────────────────────
# /api/events/{event_id}  (Step 5.1b)
# ─────────────────────────────────────────────────────────────────────────────

def test_event_by_id_404_when_missing(client: TestClient) -> None:
    """System has data + requested id does not exist → HTTP 404, no cache write."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one(None)

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    cache_set_mock = MagicMock(return_value=True)
    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", cache_set_mock),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events/999999")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "event not found"}
    cache_set_mock.assert_not_called()


def test_event_by_id_pending_when_empty(client: TestClient) -> None:
    """Pending precedes 404 — empty system returns pending even for a specific id."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 0})
    cur.enqueue_one({"n_events": 0})

    conn = _make_conn(cur)
    fake_redis = _make_redis(ping_ok=True, exists_map={})

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events/100")

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "message": "Pipeline has not run yet."}


def test_event_by_id_shape(client: TestClient) -> None:
    """Existing event id → full expanded EventItem shape."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})

    cur.enqueue_one({
        "id": 100, "headline": "Event 1", "section": "libya",
        "summary": "neutral summary", "bias_assessment": "assessment",
        "created_at": _utc(2026, 5, 12, 10, 0),
    })

    cur.enqueue_all([
        {
            "id": 1, "title": "Article 1", "url": "https://aljazeera.net/x",
            "source": "aljazeera.net",
            "bias_label": "pan_arab", "bias_score": 0.8, "bias_framing": "framing",
        }
    ])
    cur.enqueue_one({"missing_perspectives": []})

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/events/100")

    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "id", "headline", "section", "article_count", "created_at",
        "summary", "bias_assessment", "articles", "blindspot", "recommendations",
    ):
        assert key in body, f"event missing key: {key}"
    assert body["id"] == 100
    assert body["article_count"] == 1
    assert body["blindspot"] == {"missing_perspectives": []}


# ─────────────────────────────────────────────────────────────────────────────
# /api/sections/{section}  (Step 5.1b)
# ─────────────────────────────────────────────────────────────────────────────

def test_sections_invalid_path_400(client: TestClient) -> None:
    """An invalid section path value must return HTTP 400 — no DB touched."""
    with patch("api.main.cache_get_json", return_value=None):
        resp = client.get("/api/sections/invalid_section")

    assert resp.status_code == 400
    assert resp.json() == {"detail": "invalid section"}


def test_sections_pending_when_empty(client: TestClient) -> None:
    """Pending payload for /api/sections/libya when system is empty."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 0})
    cur.enqueue_one({"n_events": 0})

    conn = _make_conn(cur)
    fake_redis = _make_redis(ping_ok=True, exists_map={})

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/sections/libya")

    assert resp.status_code == 200
    assert resp.json() == {"status": "pending", "message": "Pipeline has not run yet."}


def test_sections_date_grouping_with_arabic_labels(client: TestClient) -> None:
    """Today / yesterday / older articles → اليوم / الأمس / ISO labels.

    Articles are timestamped relative to Africa/Tripoli (the project's
    reference timezone per Step 5.1b decision Q4).
    """
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _ZI

    tz_tripoli = _ZI("Africa/Tripoli")
    today_local = __import__("datetime").datetime.now(tz_tripoli).date()
    yesterday_local = today_local - _td(days=1)
    older_local = today_local - _td(days=5)

    def _pub(local_date: Any, hh: int, mm: int = 0) -> Any:
        """Build a UTC-aware datetime that lands on local_date in Tripoli."""
        import datetime as _dt
        local_dt = _dt.datetime(
            local_date.year, local_date.month, local_date.day, hh, mm,
            tzinfo=tz_tripoli,
        )
        return local_dt.astimezone(_dt.timezone.utc)

    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})

    cur.enqueue_one({"total": 3})
    cur.enqueue_all([
        {
            "id": 1, "title": "Today article", "url": "https://aljazeera.net/a",
            "source": "aljazeera.net",
            "published_at": _pub(today_local, 14, 0),
            "bias_label": "pan_arab", "bias_score": 0.7, "bias_framing": "f1",
        },
        {
            "id": 2, "title": "Yesterday article", "url": "https://bbc.com/b",
            "source": "bbc.com",
            "published_at": _pub(yesterday_local, 9, 0),
            "bias_label": "neutral", "bias_score": -0.1, "bias_framing": "f2",
        },
        {
            "id": 3, "title": "Older article", "url": "https://example.com/c",
            "source": "example.com",
            "published_at": _pub(older_local, 18, 0),
            "bias_label": "opposition", "bias_score": 0.5, "bias_framing": "f3",
        },
    ])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/sections/libya?per_page=5")

    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {
        "section", "total", "page", "per_page", "articles_by_date",
    }
    assert body["section"] == "libya"
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["per_page"] == 5

    groups = body["articles_by_date"]
    assert len(groups) == 3

    assert [g["date"] for g in groups] == [
        today_local.isoformat(),
        yesterday_local.isoformat(),
        older_local.isoformat(),
    ]
    assert groups[0]["label"] == "اليوم"
    assert groups[1]["label"] == "الأمس"
    assert groups[2]["label"] == older_local.isoformat()

    a0 = groups[0]["articles"][0]
    assert set(a0.keys()) == {
        "id", "title", "url", "source", "published_at", "bias",
    }
    assert a0["bias"]["label"] == "pan_arab"


def test_sections_bias_labels_filter_uses_inner_join(client: TestClient) -> None:
    """A specific bias_labels filter → INNER JOIN + bs.label = ANY(...)."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one({"total": 0})
    cur.enqueue_all([])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/sections/libya?bias_labels=pan_arab,opposition&per_page=5")

    assert resp.status_code == 200

    article_sql_calls = [
        (sql, args) for sql, args in cur.executed
        if "FROM articles a" in sql
    ]
    assert len(article_sql_calls) == 2
    for sql, args in article_sql_calls:
        assert "INNER JOIN bias_scores" in sql
        assert "LEFT JOIN bias_scores" not in sql
        assert "bs.label = ANY(%s)" in sql
        assert any(
            isinstance(a, list) and set(a) == {"pan_arab", "opposition"}
            for a in (args or [])
        ), f"expected labels list in args, got {args}"


def test_sections_bias_labels_all_invalid_returns_empty(client: TestClient) -> None:
    """All-invalid bias_labels → empty result set (NOT all-articles, NOT 400)."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one({"total": 0})
    cur.enqueue_all([])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/sections/libya?bias_labels=foo,bar")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["articles_by_date"] == []

    article_sql_calls = [
        (sql, args) for sql, args in cur.executed
        if "FROM articles a" in sql
    ]
    for sql, args in article_sql_calls:
        assert "INNER JOIN bias_scores" in sql
        assert any(isinstance(a, list) and a == [] for a in (args or [])), \
            f"expected empty labels list in args, got {args}"


def test_sections_bias_labels_all_uses_left_join(client: TestClient) -> None:
    """bias_labels=all (default) → LEFT JOIN, no label filter in WHERE."""
    cur = _FakeCursor()
    cur.enqueue_one({"n_articles": 10})
    cur.enqueue_one({"n_events": 5})
    cur.enqueue_one({"total": 0})
    cur.enqueue_all([])

    conn = _make_conn(cur)
    fake_redis = _make_redis(
        ping_ok=True, get_map={}, exists_map={"results:libya": True},
    )

    with (
        patch("api.main.cache_get_json", return_value=None),
        patch("api.main.cache_set_json", return_value=True),
        patch("api.main.get_db_conn", return_value=conn),
        patch("api.main.get_redis", return_value=fake_redis),
    ):
        resp = client.get("/api/sections/libya?per_page=5")

    assert resp.status_code == 200

    article_sql_calls = [
        (sql, args) for sql, args in cur.executed
        if "FROM articles a" in sql
    ]
    for sql, _args in article_sql_calls:
        assert "LEFT JOIN bias_scores" in sql
        assert "INNER JOIN bias_scores" not in sql
        assert "bs.label = ANY" not in sql
