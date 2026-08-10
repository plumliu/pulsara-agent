# Pulsara Terminal Client

This directory contains the renderer-owned Go consumer for Terminal Protocol
v3. Python remains the authority for canonical conversation state, command
outcomes, policy, secrets, queue coordination, and recovery. Go owns only
client state, rendering, input, and parent/child supervision.

The client consumes:

- a repeatable-read canonical snapshot;
- committed occurrence observations;
- process-local live blocks and control state;
- explicit canonical/live/control GAP responses.

Canonical committed identity always replaces a matching live draft. Live data
is disposable and has no ACK, durable cursor, replay, or recovery authority.
Protocol v2, Presentation Foundation, persistent history roots, and the former
S1/S2/S3 client state graph are not retained.

Build and run:

```sh
mkdir -p bin
go build -trimpath -o bin/pulsara-tui ./cmd/pulsara-tui

cd ../..
uv run pulsara host tui \
  --env-file .env \
  --workspace /path/to/project \
  --tui-binary "$PWD/clients/terminal/bin/pulsara-tui"
```

`--clear-scrollback` remains an explicit Python-launcher option. It is off by
default and irreversibly clears the primary screen before the first child
launch.

Generate the v3 Protobuf binding and contract artifacts with:

```sh
scripts/generate_protocol.sh
```

Run the Go gates with:

```sh
go test ./...
go vet ./...
go mod verify
```

Do not add a Protocol v2 adapter, old-presentation decoder, dual reader, or
client-side canonical semantics.
