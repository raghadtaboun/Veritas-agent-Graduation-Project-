"""
frontend/pages/ — Page modules for the Streamlit dashboard.

Each page exposes a single `render()` function called by `frontend/app.py`
based on the sidebar selection. Page modules import from `frontend.api_client`,
`frontend.components`, and `frontend.styles` only — never from `agents/`,
`mcp_server/`, `config/`, or the backend `api/`.
"""
