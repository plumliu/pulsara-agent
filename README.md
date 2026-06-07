# Pulsara

Pulsara is a backend runtime for a Python agent built around JSON-LD grounded memory.

The MVP combines:

- a Claude Code-style hand-written main loop and tool execution boundary;
- a narrowed Hermes-style tool/provider registry;
- a pro/flash LLM runtime with an OpenAI Responses-compatible adapter;
- an AgentScope-like `AgentEvent` / message reducer runtime stream;
- Pulsara's own `Execution Evidence Ledger` and durable semantic memory model.

The first milestone is not a frontend and not a full task graph. It is the smallest memory substrate that can prove:

```text
ToolResult -> Artifact -> Evidence -> Claim / Decision
```

## MVP Layout

```text
src/pulsara_agent/
  jsonld/
    entity.py
    iri.py
    namespace.py
    node_ref.py
    term.py
    value.py
  ontology/
    memory.py
  runtime/
    state.py
  event/
    events.py
    log.py
  message/
    blocks.py
    message.py
    reducer.py
  llm/
    config.py
    factory.py
    input.py
    models.py
    registry.py
    request.py
    runtime.py
    transport.py
    usage.py
    adapters/
      mock.py
      openai/
        responses.py
  settings.py
  tools/
    base.py
    registry.py
  memory/
    archive.py
    graph.py
    ledger.py
    entities/
      artifact.py
      claim.py
      evidence.py
      tool_result.py
      turn.py
    records.py
    write_gate.py
  cli.py
tests/
  test_event_message_system.py
  test_execution_evidence_ledger.py
  test_llm_runtime.py
  test_real_llm_integration.py
  test_settings.py
```

## Run

```bash
uv run pulsara --version
uv run pulsara demo-ledger
uv run pulsara config-check
uv run python -m pytest
```

Run the opt-in real LLM harness smoke test:

```bash
PULSARA_RUN_REAL_LLM=1 uv run python -m pytest -m real_llm
```

## LLM Configuration

Pulsara exposes two model slots:

- `pro`: the main reasoning model;
- `flash`: the cheaper/faster side model for compacting, projection, and small helper work.

For the MVP, users provide one credential set and two model names:

```bash
export PULSARA_API_KEY="..."
export PULSARA_BASE_URL="https://api.openai.com/v1"
export PULSARA_PRO_MODEL="gpt-5"
export PULSARA_FLASH_MODEL="gpt-5-mini"
```

Or create a local `.env` file from the example:

```bash
cp .env.example .env
```

Then edit `.env`:

```dotenv
PULSARA_API_KEY=sk-your-api-key
PULSARA_BASE_URL=https://api.openai.com/v1
PULSARA_PRO_MODEL=gpt-5
PULSARA_FLASH_MODEL=gpt-5-mini
```

Verify that the runtime can see the configuration:

```bash
uv run pulsara config-check
```

For `.env`:

```bash
uv run pulsara config-check --env-file .env
```

The command prints redacted configuration metadata and never prints the API key.

The current real adapter is OpenAI Responses-compatible and lives under
`llm/adapters/openai/responses.py`. Other wire formats, such as Anthropic or
Google, should be added as separate adapter packages rather than leaking their
event names into Pulsara's main loop.

LLM adapters emit Pulsara `AgentEvent` objects, not provider-native stream
events and not a separate public `LLMEvent` protocol.

## Current Boundary

`Turn`, `ToolResult`, `Artifact`, and `Evidence` are runtime provenance and can be appended by the runtime.

`Claim` and `Decision` are conclusion nodes. They must pass `MemoryWriteGate` before becoming active or durable.

`AgentEvent` is the public runtime stream. `InMemoryEventLog` can replay a
`reply_id` into a `Msg` through `MessageReducer`. Memory and projection changes
are first-class events, but only accepted semantic facts are promoted into
`GraphStore`.

The LLM layer does not know about `GraphStore`, `ArchiveStore`, memory gates, or
tool execution internals. It only translates provider APIs into Pulsara's
internal request objects and runtime events.

## Design Notes

See [PULSARA_MVP_BOOTSTRAP.zh.md](PULSARA_MVP_BOOTSTRAP.zh.md) for the Chinese MVP bootstrap rationale.
