#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIST="$ROOT/dist/packages"
VERSION=${VERSION:-s0-v2.0.6}
mkdir -p "$DIST"
rm -f "$DIST"/pulsara-tui-s0-* "$DIST"/SHA256SUMS

for target in darwin/arm64 darwin/amd64 linux/arm64 linux/amd64; do
  GOOS=${target%/*}
  GOARCH=${target#*/}
  output="$DIST/pulsara-tui-s0-${GOOS}-${GOARCH}"
  CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" \
    go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o "$output" ./cmd/pulsara-tui-s0
done

(
  cd "$DIST"
  shasum -a 256 pulsara-tui-s0-* > SHA256SUMS
)

NATIVE="$DIST/pulsara-tui-s0-$(go env GOOS)-$(go env GOARCH)"
"$NATIVE" --version
"$NATIVE" --self-test >/dev/null

for binary in "$DIST"/pulsara-tui-s0-*; do
  basename=$(basename "$binary")
  (
    cd "$DIST"
    file "$basename"
    go version -m "$basename" | grep -E 'charm.land/(bubbletea|bubbles|lipgloss)/v2' >/dev/null
  )
done

cat "$DIST/SHA256SUMS"
