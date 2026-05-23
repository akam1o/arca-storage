#!/usr/bin/env python3
"""Fail when govulncheck reports findings outside the configured allowlist."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load_allowlist(path: Path) -> set[str]:
    allowed: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            allowed.add(line)
    return allowed


def _decode_json_stream(data: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    index = 0
    objects: list[dict[str, Any]] = []
    while index < len(data):
        while index < len(data) and data[index].isspace():
            index += 1
        if index >= len(data):
            break
        obj, index = decoder.raw_decode(data, index)
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: filter-govulncheck.py <allowlist>", file=sys.stderr)
        return 2

    allowed = _load_allowlist(Path(sys.argv[1]))
    objects = _decode_json_stream(sys.stdin.read())
    findings = sorted(
        {
            finding["osv"]
            for obj in objects
            if isinstance((finding := obj.get("finding")), dict) and isinstance(finding.get("osv"), str)
        }
    )
    unexpected = [finding for finding in findings if finding not in allowed]
    if unexpected:
        print("Unexpected govulncheck findings:")
        for finding in unexpected:
            print(f"- {finding}")
        return 1

    print(f"No unexpected govulncheck findings ({len(findings)} ignored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
