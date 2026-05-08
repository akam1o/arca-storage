"""Authentication helpers for the Arca Storage API."""

from __future__ import annotations

import os


AUTH_EXEMPT_PATHS = {"/docs", "/redoc", "/openapi.json"}


def configured_api_token() -> str:
    """Return the configured bearer token, if any."""
    return os.environ.get("ARCA_API_TOKEN", "") or os.environ.get("ARCA_AUTH_TOKEN", "")
