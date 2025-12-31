#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

version="${ARCA_VERSION:-}"

if [ -z "$version" ]; then
  if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    version="$(git -C "$ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || true)"
    if [ -z "$version" ]; then
      version="$(git -C "$ROOT" tag -l 'v*' --sort=-version:refname | head -n1 || true)"
    fi
  fi
fi

if [ -z "$version" ]; then
  version="${GITHUB_REF_NAME:-}"
fi

if [ -z "$version" ] && [ -n "${GITHUB_REF:-}" ]; then
  version="${GITHUB_REF##*/}"
fi

version="${version#v}"

if [ -z "$version" ]; then
  echo "Unable to determine version; set ARCA_VERSION (e.g. 0.2.2)" >&2
  exit 1
fi

echo "$version"
