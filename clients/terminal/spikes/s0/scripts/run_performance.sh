#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
mkdir -p dist evidence

gofmt -w cmd internal
go test ./...
go vet ./...
uv run pytest -q probe/tests/test_performance_probe.py
go build -trimpath -ldflags '-X main.version=s0-v2.0.6' -o dist/pulsara-tui-s0 ./cmd/pulsara-tui-s0

PLATFORM=$(go env GOOS)-$(go env GOARCH)
uv run python probe/performance_probe.py \
  --binary dist/pulsara-tui-s0 \
  --rates 20 100 \
  --repetitions 20 \
  --warmup-seconds 1 \
  --measurement-seconds 3 \
  --sample-interval-seconds 0.1 \
  --key-probes 20 \
  --cooldown-seconds 0.25 \
  --output-json "evidence/${PLATFORM}-performance.json" \
  --output-markdown "evidence/${PLATFORM}-performance.md"
