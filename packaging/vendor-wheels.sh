#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELHOUSE="$ROOT/packaging/wheelhouse"

mkdir -p "$WHEELHOUSE"

# Debian/Ubuntu images may enforce PEP 668 (externally-managed-environment),
# so avoid installing build tooling into the system Python.
VENV="$(mktemp -d)"
trap 'rm -rf "$VENV"' EXIT
python3 -m venv "$VENV"
"$VENV/bin/python" -m ensurepip --upgrade
"$VENV/bin/python" -m pip install -U pip build wheel

# Build arca-storage wheel
(cd "$ROOT/arca_storage" && "$VENV/bin/python" -m build --wheel)
cp -f "$ROOT/arca_storage/dist/"*.whl "$WHEELHOUSE/"

# Build wheels for runtime deps (avoid sdists at install time)
"$VENV/bin/python" -m pip wheel --wheel-dir "$WHEELHOUSE" -r "$ROOT/packaging/requirements-runtime.txt"

echo "Wheelhouse ready: $WHEELHOUSE"
