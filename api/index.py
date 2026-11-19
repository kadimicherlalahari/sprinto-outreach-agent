"""Vercel entry point: the dashboard as one ASGI app. Prospecting runs stay on the operator's machine (CLI)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sprinto_agent.web import create_app  # noqa: E402

app = create_app(mock=False)
