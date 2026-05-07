"""
Uvicorn server entrypoint for Arca Storage API.
"""

from __future__ import annotations

import argparse
from typing import Optional

import uvicorn

from arca_storage.config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arca-storage-api", description="Arca Storage REST API server")
    parser.add_argument("--host", default=None, help="Bind host (default: from config or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: from config or 8080)")
    parser.add_argument("--log-level", default="info", help="Uvicorn log level (default: info)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_settings()
    host = args.host or cfg.api.bind
    port = args.port or cfg.api.port
    uvicorn.run("arca_storage.api.main:app", host=host, port=port, log_level=args.log_level)
    return 0
