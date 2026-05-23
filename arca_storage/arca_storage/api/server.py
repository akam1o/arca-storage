"""
Uvicorn server entrypoint for Arca Storage API.
"""

from __future__ import annotations

import argparse
from typing import Optional

import uvicorn

from arca_storage.api.auth import (
    API_TOKEN_REQUIRED_MESSAGE,
    REMOTE_API_TLS_REQUIRED_MESSAGE,
    configured_api_token,
    insecure_remote_api_allowed,
    is_loopback_bind_host,
    unauthenticated_loopback_allowed,
)
from arca_storage.config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arca-storage-api", description="Arca Storage REST API server"
    )
    parser.add_argument(
        "--host", default=None, help="Bind host (default: from config or 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: from config or 8080)",
    )
    parser.add_argument(
        "--log-level", default="info", help="Uvicorn log level (default: info)"
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Enable Uvicorn access logs (disabled by default to avoid logging request paths)",
    )
    parser.add_argument(
        "--ssl-certfile", default=None, help="TLS certificate file for HTTPS"
    )
    parser.add_argument(
        "--ssl-keyfile", default=None, help="TLS private key file for HTTPS"
    )
    return parser


def _validate_auth_for_bind(parser: argparse.ArgumentParser, host: str) -> None:
    if configured_api_token():
        return
    if is_loopback_bind_host(host) and unauthenticated_loopback_allowed():
        return
    parser.error(API_TOKEN_REQUIRED_MESSAGE)


def _validate_transport_for_bind(
    parser: argparse.ArgumentParser, host: str, ssl_certfile: Optional[str]
) -> None:
    if is_loopback_bind_host(host) or ssl_certfile or insecure_remote_api_allowed():
        return
    parser.error(REMOTE_API_TLS_REQUIRED_MESSAGE)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_settings()
    host = args.host or cfg.api.bind
    port = args.port or cfg.api.port
    ssl_certfile = args.ssl_certfile or getattr(cfg.api, "ssl_certfile", None)
    ssl_keyfile = args.ssl_keyfile or getattr(cfg.api, "ssl_keyfile", None)
    if bool(ssl_certfile) != bool(ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile must be provided together")
    _validate_auth_for_bind(parser, host)
    _validate_transport_for_bind(parser, host, ssl_certfile)
    uvicorn.run(
        "arca_storage.api.main:app",
        host=host,
        port=port,
        log_level=args.log_level,
        access_log=args.access_log,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    return 0
