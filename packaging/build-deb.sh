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

PKG_VERSION="$(bash "$ROOT/packaging/get-version.sh")"
DEB_DIST="${ARCA_DEB_DIST:-}"
if [ -z "$DEB_DIST" ] && [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [ -n "${ID:-}" ] && [ -n "${VERSION_ID:-}" ]; then
    # e.g. debian12, ubuntu24.04
    DEB_DIST="${ID}${VERSION_ID}"
  elif [ -n "${ID:-}" ] && [ -n "${VERSION_CODENAME:-}" ]; then
    DEB_DIST="${ID}.${VERSION_CODENAME}"
  elif [ -n "${VERSION_CODENAME:-}" ]; then
    DEB_DIST="$VERSION_CODENAME"
  else
    DEB_DIST="${ID:-}"
  fi
fi

# Disambiguate artifacts across distros (Debian/Ubuntu) so release uploads don't overwrite.
if [ -n "$DEB_DIST" ]; then
  # Keep it dpkg-version friendly.
  DEB_DIST="$(echo "$DEB_DIST" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9.+~' )"
  DEB_VERSION="${PKG_VERSION}-1.${DEB_DIST}"
else
  DEB_VERSION="${PKG_VERSION}-1"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

rsync -a --delete \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude 'arca_storage/.arca-state' \
  "$ROOT/" "$WORK/src/"

# For "3.0 (quilt)" builds, dpkg-source expects an upstream orig tarball at
# ../<source>_<upstream>.orig.tar.gz. Create it before adding debian/ metadata.
tar -C "$WORK/src" -czf "$WORK/arca-storage_${PKG_VERSION}.orig.tar.gz" \
  --transform "s,^,arca-storage-$PKG_VERSION/," \
  .

cp -R "$ROOT/packaging/debian/debian" "$WORK/src/debian"
chmod +x "$WORK/src/debian/rules" || true
if sed --version >/dev/null 2>&1; then
  sed -i "1s/^arca-storage ([^)]*)/arca-storage (${DEB_VERSION})/" "$WORK/src/debian/changelog"
else
  sed -i '' "1s/^arca-storage ([^)]*)/arca-storage (${DEB_VERSION})/" "$WORK/src/debian/changelog"
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
