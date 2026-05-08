"""
Uvicorn server entrypoint for Arca Storage API.
"""

from __future__ import annotations

import argparse
import ipaddress
from typing import Optional

import uvicorn

from arca_storage.api.auth import configured_api_token
from arca_storage.config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arca-storage-api", description="Arca Storage REST API server")
    parser.add_argument("--host", default=None, help="Bind host (default: from config or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: from config or 8080)")
    parser.add_argument("--log-level", default="info", help="Uvicorn log level (default: info)")
    return parser


def _is_loopback_bind_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_auth_for_bind(parser: argparse.ArgumentParser, host: str) -> None:
    if _is_loopback_bind_host(host):
        return
    if configured_api_token():
        return
    parser.error("ARCA_API_TOKEN or ARCA_AUTH_TOKEN is required when binding to a non-loopback host")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_settings()
    host = args.host or cfg.api.bind
    port = args.port or cfg.api.port
    _validate_auth_for_bind(parser, host)
    uvicorn.run("arca_storage.api.main:app", host=host, port=port, log_level=args.log_level)
    return 0
