#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELHOUSE="$ROOT/packaging/wheelhouse"

mkdir -p "$WHEELHOUSE"

python3 -m pip install -U pip build wheel

# Build arca-storage wheel
(cd "$ROOT/arca_storage" && python3 -m build --wheel)
cp -f "$ROOT/arca_storage/dist/"*.whl "$WHEELHOUSE/"

# Build wheels for runtime deps (avoid sdists at install time)
python3 -m pip wheel --wheel-dir "$WHEELHOUSE" -r "$ROOT/packaging/requirements-runtime.txt"

echo "Wheelhouse ready: $WHEELHOUSE"
