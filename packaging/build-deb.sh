#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/packaging/out/deb"
mkdir -p "$OUT"

if ! command -v dpkg-buildpackage >/dev/null 2>&1; then
  echo "dpkg-buildpackage not found" >&2
  exit 1
fi

if [ ! -d "$ROOT/packaging/wheelhouse" ]; then
  echo "Missing packaging/wheelhouse; running ./packaging/vendor-wheels.sh" >&2
  bash "$ROOT/packaging/vendor-wheels.sh"
fi

VERSION="$(bash "$ROOT/packaging/get-version.sh")"
DEB_VERSION="${VERSION}-1"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

rsync -a --delete \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude 'arca_storage/.arca-state' \
  "$ROOT/" "$WORK/src/"

cp -R "$ROOT/packaging/debian/debian" "$WORK/src/debian"
chmod +x "$WORK/src/debian/rules" || true
if sed --version >/dev/null 2>&1; then
  sed -i "1s/^arca-storage (.*)/arca-storage (${DEB_VERSION})/" "$WORK/src/debian/changelog"
else
  sed -i '' "1s/^arca-storage (.*)/arca-storage (${DEB_VERSION})/" "$WORK/src/debian/changelog"
fi
mkdir -p "$WORK/src/packaging"
cp -R "$ROOT/packaging/wheelhouse" "$WORK/src/packaging/"

pushd "$WORK/src" >/dev/null
dpkg-buildpackage -us -uc
popd >/dev/null

cp -f "$WORK/"*.deb "$OUT/" 2>/dev/null || true
cp -f "$WORK/"*.buildinfo "$OUT/" 2>/dev/null || true
cp -f "$WORK/"*.changes "$OUT/" 2>/dev/null || true

echo "Built debs in: $OUT"
