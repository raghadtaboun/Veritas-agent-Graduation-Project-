"""
frontend/api_client.py — Thin httpx wrapper around the FastAPI backend.

This is the ONLY module in ``frontend/`` that performs network I/O. Pages
import from here exclusively — they never import httpx themselves. Every
function returns a plain ``dict`` and never raises:

  * Successful responses are returned as parsed JSON.
  * Pending responses (``{"status": "pending", "message": …}``) are
    passed through unchanged. The UI inspects ``data.get("status")``
    and renders the §10.3 yellow banner.
  * Network / HTTP / parsing errors are normalised to
    ``{"status": "error", "detail": "<message>"}`` per the task spec.
    The UI renders the §10.2 red error banner.

Architecture constraints (Step 5.2 spec §1.1):
  * No direct database driver imports.
  * No agent or MCP-server imports.
  * No LLM SDK imports.
  * No environment-bootstrap call — the dashboard only needs the
    ``API_BASE_URL`` environment variable.

The base URL is read from the ``API_BASE_URL`` environment variable and
defaults to ``http://localhost:8001`` to match the api/main.py entry
point used in development.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# The FastAPI backend URL. Set via ``API_BASE_URL`` env var in production /
# CI; falls back to localhost:8001 for local development. The trailing
# slash is significant for ``urljoin``.
API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8001").rstrip("/") + "/"

# Per task spec: 10s timeout on every call. The FastAPI cache typically
# answers in <50 ms; the long timeout is a safety net for cold DB / Redis.
_TIMEOUT_SECS: float = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _error(detail: str) -> dict[str, Any]:
    """Build the canonical error payload consumed by ``components.error_banner``."""
    return {"status": "error", "detail": detail}


def _request(
    path: str,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Perform a GET request and normalise any failure to an error dict.

    ``params`` values that are ``None`` are stripped before the request so
    the FastAPI side sees them as "absent" (i.e., the endpoint's default
    kicks in) rather than the literal string ``"None"``.

    Two FastAPI responses are surfaced to the caller unchanged:
      * 200 with a normal payload   → returned as-is
      * 200 with ``{"status":"pending",…}`` → returned as-is (the UI
        renders the §10.3 banner)

    Everything else (network error, parse error, 4xx, 5xx, including the
    503 ``{"status":"error",…}`` body the API emits on DB outages) is
    mapped to ``_error(detail)``.
    """
    url = urljoin(API_BASE_URL, path.lstrip("/"))

    clean_params: dict[str, Any] = {}
    if params:
        for k, v in params.items():
            if v is None:
                continue
            clean_params[k] = v

    try:
        with httpx.Client(timeout=_TIMEOUT_SECS) as client:
            response = client.get(url, params=clean_params)
    except httpx.TimeoutException as exc:
        logger.warning("api_client: timeout calling %s: %s", url, exc)
        return _error(f"انتهت مهلة الاتصال بالخادم ({_TIMEOUT_SECS:.0f} ث).")
    except httpx.HTTPError as exc:
        logger.warning("api_client: transport error calling %s: %s", url, exc)
        return _error(f"تعذّر الوصول إلى الخادم: {exc}")
    except OSError as exc:
        logger.warning("api_client: OS error calling %s: %s", url, exc)
        return _error(f"خطأ في الشبكة: {exc}")

    if response.status_code >= 400:
        # The FastAPI backend already emits {"status":"error","detail":…}
        # bodies for its own 5xx paths (api/main.py /api/home etc.). Surface
        # the backend's detail when possible; otherwise fall back to the
        # status code so the user is never shown a blank error.
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                return _error(str(body["detail"]))
        except (ValueError, httpx.HTTPError):
            pass
        return _error(f"HTTP {response.status_code} من الخادم.")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("api_client: invalid JSON from %s: %s", url, exc)
        return _error("استجابة غير صالحة من الخادم.")

    if not isinstance(payload, dict):
        return _error("شكل غير متوقع للاستجابة من الخادم.")

    return payload


def _refresh_flag(refresh: bool) -> Optional[str]:
    """Return ``"true"`` when refresh requested, else ``None``.

    ``None`` causes ``_request`` to strip the query param entirely, which
    matches the FastAPI side: ``_is_refresh`` only fires on the literal
    string ``"true"`` (api/main.py:312), so any other value is a no-op.
    Keeping the param absent reduces cache-key noise downstream.
    """
    return "true" if refresh else None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_health() -> dict[str, Any]:
    """Fetch ``GET /api/health``. Used for last_pipeline_run + degraded banner.

    Per additional decision (a): ``frontend/app.py`` calls this exactly
    once per rerun and caches the result in ``st.session_state["_health"]``.
    No FastAPI-side cache exists for /api/health (spec §1.3), so this is
    always live.
    """
    return _request("/api/health")


def get_home(refresh: bool = False) -> dict[str, Any]:
    """Fetch ``GET /api/home``. See api/main.py:726 for the response shape."""
    return _request("/api/home", params={"refresh": _refresh_flag(refresh)})


def get_events(
    section: str = "all",
    page: int = 1,
    per_page: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch ``GET /api/events`` with optional section + date filters.

    ``date_from`` / ``date_to`` are ISO-format strings (``YYYY-MM-DD``).
    The FastAPI side parses them via ``Query(date)`` and anchors the
    comparison to ``Africa/Tripoli`` per the Step 5.1b timezone convention.
    """
    return _request(
        "/api/events",
        params={
            "section":   section,
            "page":      page,
            "per_page":  per_page,
            "date_from": date_from,
            "date_to":   date_to,
            "refresh":   _refresh_flag(refresh),
        },
    )


def get_event(event_id: int, refresh: bool = False) -> dict[str, Any]:
    """Fetch ``GET /api/events/{event_id}``. 404 is normalised to an error dict."""
    return _request(
        f"/api/events/{event_id}",
        params={"refresh": _refresh_flag(refresh)},
    )


def get_section(
    section: str,
    bias_labels: Optional[list[str]] = None,
    page: int = 1,
    per_page: int = 20,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch ``GET /api/sections/{section}``.

    ``bias_labels=None`` translates to ``?bias_labels=all`` (the FastAPI
    sentinel that disables the filter via a LEFT JOIN — api/main.py:1079).
    An empty list translates to the literal string ``""`` which the API
    interprets as "no valid labels → empty result set" per Step 5.1b
    decision 4b — that is the intended UX when the user deselects every
    label in the multi-select.
    """
    if bias_labels is None:
        labels_param: str = "all"
    elif len(bias_labels) == 0:
        labels_param = ""
    else:
        labels_param = ",".join(bias_labels)

    return _request(
        f"/api/sections/{section}",
        params={
            "bias_labels": labels_param,
            "page":        page,
            "per_page":    per_page,
            "refresh":     _refresh_flag(refresh),
        },
    )


def get_statistics(refresh: bool = False) -> dict[str, Any]:
    """Fetch ``GET /api/statistics``. See api/main.py:994 for the response shape.

    The endpoint returns either:
      * The full statistics payload (KPIs + 8 chart sections) when the
        system has data — see ``StatisticsResponse`` in api/main.py:271.
      * ``{"status": "pending", "message": "..."}`` when the pipeline has
        never run (no articles, no events, no Redis ``results:{section}``
        keys) — see ``_pending_payload`` in api/main.py:424.

    The frontend (frontend.pages.statistics) inspects ``status`` and
    branches to ``pending_banner`` accordingly. Network/HTTP errors are
    normalised to ``{"status": "error", ...}`` here per Rule 2.4 (no
    silent failures).
    """
    return _request("/api/statistics", params={"refresh": _refresh_flag(refresh)})
