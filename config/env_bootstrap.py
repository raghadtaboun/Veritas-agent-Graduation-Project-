"""
config/env_bootstrap.py — Centralized environment bootstrap (Phase 4.5 Step 4.5.2).

Every Python process entry point in the Veritas Agent project (mcp_server/server.py,
agents/graph.py, api/main.py, scheduler.py, tests/conftest.py) imports and calls
``bootstrap_env()`` *before* any other project import and before any LLM SDK is
imported. This is the only sanctioned way to load environment variables in the
project. Direct ``load_dotenv()`` calls at entry points are forbidden by Rule 2.6
in agent.md.

What this module solves
-----------------------
The ``google.genai`` SDK auto-detects ``GOOGLE_API_KEY`` from the shell at import
time. On a developer machine that already has a system-wide ``GOOGLE_API_KEY``
set (e.g. for an unrelated GCP project), the SDK silently picks up that key and
ignores the project's dedicated ``GEMINI_API_KEY`` from ``.env``. The result is
authentication errors that look like quota problems. ``bootstrap_env()``
prevents this by:

1. Removing the conflicting shell-level keys from ``os.environ`` *before* the
   SDK is imported.
2. Loading ``.env`` with ``override=True`` so project values always win.
3. Validating that all required keys are present and non-empty.
4. Re-setting ``GOOGLE_API_KEY = GEMINI_API_KEY`` so any transitively-imported
   SDK that still auto-detects ``GOOGLE_API_KEY`` ends up using the project key.

The function is idempotent — repeated calls are no-ops, so it is safe to import
this module from multiple entry points within a single process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

_BOOTSTRAPPED: bool = False

_CONFLICTING_SHELL_KEYS: tuple[str, ...] = (
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

_REQUIRED_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "MCP_SERVER_URL",
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "GROQ_MODEL",
)


def bootstrap_env() -> None:
    """Initialise the project environment exactly once per process.

    Order of operations is significant — see the four numbered steps in the
    module docstring. Raises ``RuntimeError`` listing every missing required
    key when any value in :data:`_REQUIRED_KEYS` is empty or unset after
    ``.env`` has been loaded.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    for key in _CONFLICTING_SHELL_KEYS:
        os.environ.pop(key, None)

    project_root = Path(__file__).resolve().parent.parent
    dotenv_path = project_root / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=True)

    missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(
            "bootstrap_env: required environment variables missing or empty: "
            + ", ".join(missing)
            + f" (checked .env at {dotenv_path})"
        )

    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

    # Count secondary Gemini keys for observability. No failure if absent —
    # only slot 1 (GEMINI_API_KEY) is required; slots 2–10 are optional.
    _n_gemini = 1
    for _i in range(2, 11):
        for _name in (f"GEMINI_API_KEY_{_i}", f"Gemini_API_KEY_{_i}"):
            if os.environ.get(_name, "").strip():
                _n_gemini += 1
                break
    logging.getLogger(__name__).info("Bootstrap: detected %d Gemini key(s)", _n_gemini)

    _BOOTSTRAPPED = True


__all__ = ["bootstrap_env"]
