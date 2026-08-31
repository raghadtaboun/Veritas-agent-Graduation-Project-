"""
frontend/ — Veritas Agent Streamlit dashboard (Phase 5 Step 5.2).

The dashboard is a pure HTTP client of the FastAPI backend on port 8001.
It uses only httpx for network I/O and has no direct access to the
project's data layer or agent code. All architecture constraints are
documented in `phase_5_ui_spec.md` §1 and verified by the greps in the
Step 5.2 task spec.

Entry point: `streamlit run frontend/app.py`.
"""
