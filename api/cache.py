"""
api/cache.py — Redis helpers for the FastAPI backend.

Exposes:

- ``get_redis()`` — module-level singleton redis client.
- ``cache_get_json(key, refresh=False)`` — fetches and JSON-decodes a key.
  When ``refresh=True`` the key is deleted before lookup and ``None`` is
  returned, forcing the caller to recompute and write fresh data via
  ``cache_set_json``.
- ``cache_set_json(key, value, ttl=300)`` — JSON-encodes and stores a value.

The Redis URL is read from ``REDIS_URL``. Per plan.md Step 5.1, the API
uses redis-py directly — the MCP server is for agents only.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis as redis_lib

logger = logging.getLogger(__name__)

_redis_client: redis_lib.Redis | None = None


def get_redis() -> redis_lib.Redis:
    """Return the singleton Redis client.

    Uses ``decode_responses=True`` so values are returned as ``str`` (we
    operate on JSON strings, never on raw bytes). The connection is lazy —
    the first call constructs the client; subsequent calls reuse it.

    Connection errors propagate at command time (PING, GET, …) rather than
    at construction, so this function never raises in normal operation.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(
            os.environ["REDIS_URL"],
            decode_responses=True,
        )
    return _redis_client


def cache_get_json(key: str, refresh: bool = False) -> Any | None:
    """Read a JSON-encoded value from Redis, returning ``None`` on miss.

    When ``refresh`` is ``True``, the key is unconditionally deleted and
    ``None`` is returned, so the caller will recompute and overwrite via
    ``cache_set_json`` — implementing the ``?refresh=true`` semantics from
    spec §1.3.

    Redis outages are non-fatal: the call returns ``None`` and the caller
    falls back to the database. The error is logged for observability.
    """
    try:
        r = get_redis()
        if refresh:
            r.delete(key)
            return None
        raw: Any = r.get(key)
        if not isinstance(raw, (str, bytes, bytearray)):
            return None
        return json.loads(raw)
    except (redis_lib.RedisError, ValueError, TypeError) as exc:
        logger.warning("cache_get_json failed for key=%s: %s", key, exc)
        return None


def cache_set_json(key: str, value: Any, ttl: int = 300) -> bool:
    """Write a JSON-serialisable value to Redis with the given TTL.

    Returns ``True`` on success, ``False`` if Redis is unreachable or the
    value cannot be encoded. Failures are logged and never raise — caching
    is an optimisation, not a correctness requirement (Rule 2.4).
    """
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("cache_set_json: value not JSON-serialisable for key=%s: %s", key, exc)
        return False

    try:
        get_redis().setex(key, ttl, payload)
        return True
    except redis_lib.RedisError as exc:
        logger.warning("cache_set_json failed for key=%s: %s", key, exc)
        return False
