"""
tests/conftest.py — pytest session bootstrap (Phase 4.5 Step 4.5.2).

This file runs at pytest module-load time, before any test file is
imported and before any LLM SDK is touched. It performs two jobs:

1. Inserts the project root on ``sys.path`` so test files can import
   ``config.*``, ``agents.*``, and ``mcp_server.*`` without installing
   the package.
2. Calls :func:`config.env_bootstrap.bootstrap_env` so every test process
   has the same environment-bootstrap discipline as the production entry
   points (``mcp_server/server.py``, ``agents/graph.py``).

Per Rule 2.6 in ``agent.md``, dotenv MUST NOT be invoked directly
anywhere in this file or in any other test file — the bootstrap is the
only sanctioned path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.env_bootstrap import bootstrap_env  # noqa: E402
bootstrap_env()
