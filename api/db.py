"""
api/db.py — PostgreSQL connection helper for the FastAPI backend.

A single ``get_db_conn()`` opens a fresh psycopg2 connection per request,
using ``DATABASE_URL`` from the environment. No pooling — thesis-scale
traffic does not justify a pool, and a fresh connection per request keeps
isolation simple and avoids stale-connection bugs.

Rows come back as plain dicts via ``psycopg2.extras.RealDictCursor``, which
matches the JSON shape the API returns.

The ``SOURCE_SQL_EXPR`` constant is the single source of truth for deriving
a source domain from an article URL. Used in three places per Q3 in the
pre-work confirmation:
  * statistics §7.7 (articles per source, GROUP BY)
  * /api/home.latest_news[].source
  * /api/home.latest_events[].articles[].source
The source domain is always derived from the article URL — there is no
separate sources table.
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras


# split_part(regexp_replace(url, '^https?://(www\.)?', ''), '/', 1)
#   1. regexp_replace strips the scheme and optional 'www.' prefix.
#   2. split_part takes the substring up to the first '/' — the bare domain.
# Handles URLs like:
#   https://www.aljazeera.net/news/2026/...   -> aljazeera.net
#   http://bbc.com/arabic/...                 -> bbc.com
#   https://example.com                       -> example.com
SOURCE_SQL_EXPR: str = (
    "split_part(regexp_replace(url, '^https?://(www\\.)?', ''), '/', 1)"
)


def get_db_conn() -> psycopg2.extensions.connection:
    """Open a fresh psycopg2 connection.

    Caller is responsible for closing the connection (use a context manager
    or ``try/finally``). The connection's default cursor factory is set to
    ``RealDictCursor`` so ``cur.fetchall()`` returns ``list[dict]``.

    Raises ``psycopg2.OperationalError`` if the database is unreachable —
    callers must catch this and translate to an HTTP 503 per Rule 2.4.
    """
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    return conn
