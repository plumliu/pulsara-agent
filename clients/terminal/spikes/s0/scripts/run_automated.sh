#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
mkdir -p dist evidence

gofmt -w cmd internal
go test ./...
go vet ./...
go build -trimpath -ldflags '-X main.version=s0-v2.0.6' -o dist/pulsara-tui-s0 ./cmd/pulsara-tui-s0

uv run python probe/parent_probe.py \
  --binary dist/pulsara-tui-s0 \
  --mode all \
  --output evidence/darwin-arm64-pty.json
scripts/tmux_smoke.sh dist/pulsara-tui-s0 | tee evidence/darwin-arm64-tmux.json
scripts/package_smoke.sh | tee evidence/cross-build.txt
scripts/ssh_smoke.sh | tee evidence/docker-ssh-arm64.json
