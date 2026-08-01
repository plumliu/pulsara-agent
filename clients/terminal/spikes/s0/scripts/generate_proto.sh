#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TOOLS=$(mktemp -d)
cleanup() {
  rm -rf "$TOOLS"
}
trap cleanup EXIT

cd "$ROOT"
GOBIN="$TOOLS" go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.11
PATH="$TOOLS:$PATH" protoc \
  -I . \
  --go_out=. \
  --go_opt=module=pulsara.local/terminal-s0 \
  --python_out=. \
  probe_wire/probe.proto
gofmt -w internal/probeproto/probe.pb.go
uv run ruff format probe_wire/probe_pb2.py
