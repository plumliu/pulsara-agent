#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${1:?usage: real_ssh_smoke.sh user@host [linux-amd64-binary] [output-json]}
BINARY=${2:-"$ROOT/dist/packages/pulsara-tui-s0-linux-amd64"}
OUTPUT=${3:-"$ROOT/evidence/real-ssh-wsl2-amd64.json"}

cd "$ROOT"
if [[ ! -f "$BINARY" ]]; then
  scripts/package_smoke.sh >/dev/null
fi

uv run python probe/remote_ssh_probe.py \
  --target "$TARGET" \
  --binary "$BINARY" \
  --output "$OUTPUT"
