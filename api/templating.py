"""Jinja2 environment shared across admin routes."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def humanize_status(status: str) -> str:
    return {
        "rely_on_groups": "via group",
        "direct_active": "direct",
        "pending": "pending",
        "pending_oauth_headless": "OAuth (queued)",
        "pending_oauth_manual": "OAuth (manual needed)",
        "oauth_active": "OAuth",
        "invalid_credentials": "bad creds",
        "unknown_provider": "unknown provider",
        "no_path": "no path",
    }.get(status, status)


templates.env.filters["humanize_status"] = humanize_status
