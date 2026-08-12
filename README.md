# 🪐 Pulsara

<p align="center">
  <img src="assets/banner.png" alt="Pulsara" width="100%">
</p>

Pulsara is a local-first agent runtime built around a small, relational
conversation kernel. PostgreSQL stores accepted product facts; provider
streaming, terminal processes, subagents, and UI drafts remain process-local.

Pulsara is under active development. Interfaces can change and the database
uses a reset-only migration universe while the product is still young.

[English](README.md) · [简体中文](README.zh-CN.md) ·
[Long-term contracts](contracts/README.zh.md)

## Architecture

```text
Python KernelHostCore
├── canonical conversation runner
├── provider-neutral structured model-input compiler
├── tool policy + Host-scoped physical tools
├── exact-four durable job executor
├── PostgreSQL-only memory
├── process-local live event bus
└── Terminal Protocol v3 gateway
        └── Go terminal client

PostgreSQL
├── pulsara_v3: 24 product relations
├── selective agent_events occurrence journal
├── public.vector capability
└── public.pulsara_schema_migrations (universe metadata only)
```

The durable boundary is intentionally narrow:

- canonical relational rows own current conversation, tool, job, memory, and
  coordination truth;
- a closed 27-type `agent_events` journal records accepted occurrences but is
  never replayed to reconstruct execution;
- 23 live event types exist only in memory and may be lost at process exit;
- tool requests are committed before dispatch and a physical attempt is
  committed before an effect is invoked;
- a crash interrupts the active turn; reopening rehydrates accepted
  conversation facts rather than resuming a coroutine or provider stream;
- derived UI, audit, search, and notification state cannot veto a canonical
  commit or Host close.

Pulsara does not use a universal EventLog, execution replay, durable model
segments, projection-job framework, Oxigraph, SPARQL, or a generic
runtime-write admission epoch.

## Product surface

The current Kernel supports:

- OpenAI-compatible Responses and Chat Completions transports;
- a provider-neutral structured input compiler over the exact canonical cut,
  with five typed first-party sources, scope-frozen tool schemas, exact target
  token estimation, and deterministic source/tool-result degradation;
- filesystem, todo, `terminal`, `terminal_process`, `terminal_monitor`, and
  scoped `artifact_read` tools;
- real PIPE/PTY terminal output streaming, exact process-local cursors and
  typed GAP, same-Host future monitor observations, and autonomous
  continuation at a provider-safe point;
- default-deny subprocess environments with a bounded login-shell snapshot,
  nearest `.venv/bin`, foreground cwd continuity, and physical process-group
  drain on Host close;
- complete sanitized tool output retention through the shared blob store:
  medium output stays fully visible with an artifact reference, while large
  output uses a UTF-8-safe head/tail preview and bounded on-demand reads;
- bounded Host-scoped subagents;
- bundled and local skills;
- PostgreSQL full-text/vector memory with explicit direct, reverse, and at
  most two-hop relation traversal;
- four named durable background jobs: compaction, post-compaction memory
  extraction, memory governance, and memory-index refresh;
- canonical inspection and Protocol v3 terminal observation.

Configured MCP servers currently fail closed because no MCP execution adapter
is installed in the conversation kernel. They are never silently ignored.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL with `public.vector >= 0.5.0`
- Go only when building the optional terminal client

## Setup

```sh
uv sync
```

Create an environment file with at least:

```dotenv
PULSARA_API_KEY=...
PULSARA_BASE_URL=https://api.openai.com/v1
PULSARA_API=openai_responses
PULSARA_PRO_MODEL=...
PULSARA_FLASH_MODEL=...

PULSARA_POSTGRES_DSN=postgresql://pulsara_runtime:...@localhost:5432/pulsara
PULSARA_POSTGRES_ADMIN_DSN=postgresql://pulsara_admin:...@localhost:5432/pulsara
```

The admin DSN is used only by `db migrate`. Runtime Hosts verify the clean
schema and borrow the runtime role.

```sh
uv run pulsara db migrate --env-file .env
uv run pulsara db verify --deep --env-file .env
```

The only active migration universe is
`pulsara.conversation-kernel.v1`, generation 1, beginning at version 0.
Rounds 1 and 2 keep the schema at exactly 24 product relations while extending
the version-0 `tool_results` and canonical Terminal-observation contracts.
Their baseline/catalog identities and verification results are recorded in
[`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)
and
[`round2_terminal_runtime_activation.json`](benchmarks/suites/core/v1/round2_terminal_runtime_activation.json).
Round 3 does not change the database universe; its process-local compiler and
multi-provider verification are recorded in
[`round3_structured_model_input_compiler_activation.json`](benchmarks/suites/core/v1/round3_structured_model_input_compiler_activation.json).
An old v13 database is rejected with
`schema_migration_universe_reset_required`; Pulsara never imports, translates,
or upgrades it in place. Follow
[the clean-baseline runbook](STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md) and never
reset a real endpoint without explicit operator authorization.

## Run

One prompt:

```sh
uv run pulsara host run \
  --env-file .env \
  --workspace /path/to/project \
  "Explain this repository"
```

Interactive REPL:

```sh
uv run pulsara host repl \
  --env-file .env \
  --workspace /path/to/project
```

Resume the newest conversation for a workspace:

```sh
uv run pulsara host repl \
  --env-file .env \
  --workspace /path/to/project \
  --continue
```

Inspect the available composition without opening a session:

```sh
uv run pulsara host inspect \
  --env-file .env \
  --workspace /path/to/project
```

## Terminal client

Build the Protocol v3 Go client:

```sh
cd clients/terminal
mkdir -p bin
go build -trimpath -o bin/pulsara-tui ./cmd/pulsara-tui
cd ../..

uv run pulsara host tui \
  --env-file .env \
  --workspace /path/to/project \
  --tui-binary "$PWD/clients/terminal/bin/pulsara-tui"
```

Python owns canonical state, commands, policy, secrets, and recovery. The Go
client owns rendering and interaction only. Protocol v2 and Presentation
Foundation are not retained.

## Development

```sh
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src tests
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./... && go vet ./...)
git diff --check
```

PostgreSQL integration tests are marked `postgres`:

```sh
uv run pytest -q -m postgres
```

## Durability contract

The active long-term contracts are indexed in
[contracts/README.zh.md](contracts/README.zh.md). Root-level research and
hard-cut documents explain design history; they are not runtime registries or
compatibility specifications.
