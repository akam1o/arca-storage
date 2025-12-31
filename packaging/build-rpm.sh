#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/packaging/out/rpm"
mkdir -p "$OUT"

if ! command -v rpmbuild >/dev/null 2>&1; then
  echo "rpmbuild not found" >&2
  exit 1
fi

VERSION="$(bash "$ROOT/packaging/get-version.sh")"

if [ ! -d "$ROOT/packaging/wheelhouse" ]; then
  echo "Missing packaging/wheelhouse; running ./packaging/vendor-wheels.sh" >&2
  bash "$ROOT/packaging/vendor-wheels.sh"
fi

TOP="$(mktemp -d)"
trap 'rm -rf "$TOP"' EXIT

mkdir -p "$TOP/SOURCES" "$TOP/SPECS"

TAR="$TOP/SOURCES/arca-storage.tar.gz"
# GitHub Actions / container builds sometimes check out the repo with a different
# UID/GID than the build user, and git will refuse to operate unless the
# directory is marked as safe.
# The RPM spec expects the source tree to extract into a top-level
# "arca-storage/" directory (see %autosetup -n arca-storage).
git -c "safe.directory=$ROOT" -C "$ROOT" archive --format=tar.gz --prefix="arca-storage/" -o "$TAR" HEAD

cp "$ROOT/packaging/rpm/arca-storage.spec" "$TOP/SPECS/"
if sed --version >/dev/null 2>&1; then
  sed -i "s/^Version:\\s\\{1,\\}.*/Version:        $VERSION/" "$TOP/SPECS/arca-storage.spec"
else
  sed -i '' "s/^Version:\\s\\{1,\\}.*/Version:        $VERSION/" "$TOP/SPECS/arca-storage.spec"
fi

# Include wheelhouse in SOURCES
tar -C "$ROOT" -czf "$TOP/SOURCES/arca-storage-wheelhouse.tar.gz" packaging/wheelhouse

rpmbuild --define "_topdir $TOP" -ba "$TOP/SPECS/arca-storage.spec"
find "$TOP/RPMS" -type f -name "*.rpm" -exec cp -f {} "$OUT/" \;
find "$TOP/SRPMS" -type f -name "*.src.rpm" -exec cp -f {} "$OUT/" \;
echo "Built rpms in: $OUT"
