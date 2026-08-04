#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
client_root="$root/clients/terminal"
schema_root="$root/src/pulsara_agent/terminal_protocol/schema"
tools_root="$root/clients/terminal/spikes/s0/.tools"

PATH="$tools_root:$PATH" protoc \
  -I "$schema_root" \
  --go_out="$client_root" \
  --go_opt=module=github.com/plumliu/pulsara-agent/clients/terminal \
  "$schema_root/terminal_client.proto"

protoc \
  -I "$schema_root" \
  --python_out="$root/src/pulsara_agent/terminal_protocol/generated" \
  "$schema_root/terminal_client.proto"

gofmt -w "$client_root/internal/protocol/terminal_client.pb.go"

uv run python "$root/tools/generate_terminal_protocol_contract.py" --check
