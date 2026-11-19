"""Vercel entry point (FastAPI preset auto-detects `app` here and keeps request paths intact).
The dashboard only; prospecting runs stay on the operator's machine (python -m sprinto_agent run)."""
from sprinto_agent.web import create_app

app = create_app(mock=False)
