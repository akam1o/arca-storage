"""Authentication helpers for the Arca Storage API."""

from __future__ import annotations

import ipaddress
import os
from typing import Any


API_TOKEN_REQUIRED_MESSAGE = "ARCA_API_TOKEN or ARCA_AUTH_TOKEN is required when binding to a non-loopback host"
UNKNOWN_SERVER_HOST = "<unknown>"


def configured_api_token() -> str:
    """Return the configured bearer token, if any."""
    return os.environ.get("ARCA_API_TOKEN", "") or os.environ.get("ARCA_AUTH_TOKEN", "")


def is_loopback_bind_host(host: str) -> bool:
    """Return whether a bind host is loopback-only."""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def non_loopback_request_server_host(scope: dict[str, Any]) -> str | None:
    """Return the request server host unless ASGI confirms it is loopback."""
    server = scope.get("server")
    if not isinstance(server, (tuple, list)) or not server:
        return UNKNOWN_SERVER_HOST

    if server[0] is None:
        return UNKNOWN_SERVER_HOST

    host = str(server[0]).strip().strip("[]")
    if not host:
        return UNKNOWN_SERVER_HOST

    if is_loopback_bind_host(host):
        return None
    return host
