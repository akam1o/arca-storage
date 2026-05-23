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

# Prefer the release tag version when building the wheel.
PKG_VERSION="$(bash "$ROOT/packaging/get-version.sh")"
export SETUPTOOLS_SCM_PRETEND_VERSION="$PKG_VERSION"

# Build arca-storage wheel
rm -rf "$ROOT/arca_storage/dist"
(cd "$ROOT/arca_storage" && "$VENV/bin/python" -m build --wheel)
cp -f "$ROOT/arca_storage/dist/"*.whl "$WHEELHOUSE/"

# Build wheels for runtime deps (avoid sdists at install time).
"$VENV/bin/python" -m pip wheel --wheel-dir "$WHEELHOUSE" -r "$ROOT/arca_storage/requirements.txt"

# Validate the offline wheelhouse against the built package metadata. Use
# --ignore-installed so build-tool dependencies in this temporary venv cannot
# hide missing runtime wheels.
"$VENV/bin/python" -m pip install --dry-run --ignore-installed --no-index --find-links "$WHEELHOUSE" arca-storage >/dev/null

echo "Wheelhouse ready: $WHEELHOUSE"
