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
├── exact-one durable job executor
├── advisory memory governor + retrieval
├── process-local live event bus
└── renderer-neutral Protocol v3 gateway

PostgreSQL
├── pulsara_v3: 26 product relations
├── selective agent_events occurrence journal
├── public.vector capability
└── public.pulsara_schema_migrations (universe metadata only)
```

The durable boundary is intentionally narrow:

- canonical relational rows own current conversation, tool, job, and
  coordination truth; accepted memory rows own only the current contents of
  the advisory dataset;
- a closed 31-type `agent_events` journal records accepted occurrences but is
  never replayed to reconstruct execution;
- 24 live event types exist only in memory and may be lost at process exit;
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
- explicit provider-neutral `COMPLETED | OUTPUT_INCOMPLETE | PROVIDER_ERROR`
  model terminals, whole-response atomic assistant acceptance, and exact
  Chat/Responses native replay for accepted responses across Host and process
  restart without provider-held response state or remote response IDs;
- a provider-neutral structured input compiler over the exact canonical cut,
  with closed typed first-party sources, scope-frozen tool schemas, exact target
  token estimation, deterministic source/tool-result degradation, and one
  frozen semantic-plus-actual-wire continuity proof;
- bounded, redacted previous-turn outcome guidance plus append-only tool
  freshness frontiers; each accepted tool result carries an immutable observed
  time, monotonic duration disposition, execution origin, and optional trusted
  duration that the tool body cannot forge;
- filesystem, `terminal`, `terminal_process`, `terminal_monitor`, and scoped
  `artifact_read` tools;
- an exact-run, process-local `todo(items=[...])` tool that atomically replaces
  one bounded pending/in-progress/completed snapshot; an empty list clears it,
  ROOT and child runs remain isolated, and Host replacement intentionally starts
  without recovered TODO state;
- real PIPE/PTY terminal output streaming, exact process-local cursors and
  typed GAP, same-Host future monitor observations, and autonomous
  continuation at a provider-safe point;
- run-bound permission selection with an immutable admission snapshot, plus a
  canonical Plan workflow with Runtime-enforced read-only planning,
  structured questions, draft approve/revise/cancel, and Host-owned automatic
  continuation;
- default-deny subprocess environments with a bounded login-shell snapshot,
  nearest `.venv/bin`, foreground cwd continuity, and physical process-group
  drain on Host close;
- complete sanitized tool output retention through the shared blob store:
  a provider-neutral ToolResult logical message up to 40,000 UTF-8 bytes may
  remain FULL (independent of adapter wire bytes), while larger output uses a
  UTF-8-safe 8,000-character head/tail preview and bounded on-demand reads;
- bounded Host-scoped subagents;
- bundled and local skills;
- Host-scoped MCP over stdio and Streamable HTTP, with bounded discovery,
  scope-filtered direct typed tools, catalog/resource/prompt reads, local
  authorization, and CLI lifecycle management;
- advisory PostgreSQL memory with one-candidate `remember`, five closed item
  kinds, USER/domain and exact WORKSPACE scope isolation, best-effort
  governance, multilingual sparse recall, optional 1024-dimensional dense
  recall and explicit rerank, direct/reverse relation reads, and at most
  two-hop traversal;
- one named durable background job for compaction; memory governance,
  reflection, embedding, and recall remain Host-owned best-effort work and are
  never recovered or replayed;
- canonical inspection and Protocol v3 terminal observation.

Round 6 intentionally does not add durable MCP connection or request recovery.
Host replacement reconnects from configuration. Form/private-URL elicitation,
OAuth, MCP-backed skill activation, server-initiated Sampling/Roots, Apps/Tasks,
and a bundled terminal UI remain explicit non-goals. A future Web or desktop
client may consume Protocol v3 without becoming a second canonical authority.
Workspace-owned MCP entries remain disabled during ordinary Host startup unless
the user explicitly passes `--trust-workspace-mcp`; merely opening a repository
can never execute its stdio command or resolve its HTTP secret references.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL with `public.vector >= 0.5.0`

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

# Optional advisory-memory data egress. Sparse recall remains available when
# these are absent or disabled.
PULSARA_EMBEDDING_API_KEY=...
PULSARA_RERANK_API_KEY=...

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
Round 3.1 adds a Host-scoped, process-local provider-input continuity epoch:
within one exact ROOT or child scope, the system prompt and tool surface remain
stable while canonical conversation facts and typed runtime observations are
appended as a strict message suffix. Busy `Enter` steers the exact active ROOT
turn; `Tab` queues a future new turn. A replacement Host cold-starts from
canonical rows. Round 5A.2 persists only an accepted assistant entry's bounded,
private Chat/Responses native replay carrier; it does not persist a compiled
provider conversation, remote response identity, or in-flight stream. Steer-prefix planning shares the installed prefix
estimate, observes one absolute cooperative deadline, and is capped by a
process-local unique-work quote that never recharges the same immutable base
for each nested-prefix trial. Provider open additionally requires the exact
one-shot permit object sealed by the Host continuity owner, and the compiler
enforces exactly one value-or-absence branch for every first-party source.
Plan handoff display text is
separate from its exact canonical transition identity. Verification is recorded in
[`round3_1_provider_input_prefix_continuity_activation.json`](benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json).
Cross-process replay verification is recorded in
[`round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json`](benchmarks/suites/core/v1/round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json).
Round 4 extends clean-v0 to 26 product relations and 34 selective occurrences;
its Plan workflow, run-bound permission, Protocol v3, and real-provider
verification are recorded in
[`round4_plan_workflow_and_run_permission_activation.json`](benchmarks/suites/core/v1/round4_plan_workflow_and_run_permission_activation.json).
Round 5A removes fixed model/tool-call counts and the turn-wide wall-clock
deadline from ROOT and child turns. Each provider-dispatch plan, canonical
operation, provider transport, physical tool call, writer renewal, Terminal
decision, and close owner instead has its own closed watchdog. Foreground
provider streams have connect/write/pool/read-idle bounds but no total response
timeout; finite durable jobs retain their 30/45-second attempt totals. This is
an execution-envelope change only: automatic compaction, summary adoption, and
provider-input rebase remain deferred to Round 5B. Verification is recorded in
[`round5_long_horizon_execution_envelope_activation.json`](benchmarks/suites/core/v1/round5_long_horizon_execution_envelope_activation.json).
Round 7 extends the existing `tool_results` relation with immutable observation
timing/origin facts and adds two provider-neutral compiler sources for the
immediate predecessor outcome and per-turn freshness frontier. Within one
compatible Host/scope epoch, old provider messages are never rewritten: late
results and changed freshness only appear as a newly appended suffix. Pulsara-
owned provider carriers now expose product semantics and lifecycle only;
internal contract versions, fingerprints, generations, schema markers, and
delimiter-based Plan carriers are removed from model input. Verification is
recorded in
[`round7_model_visible_failure_and_tool_observation_activation.json`](benchmarks/suites/core/v1/round7_model_visible_failure_and_tool_observation_activation.json).
Round 7.1 gives every tool origin one provider-visible projection ladder. The
40,000-byte cap applies only to the provider-neutral logical ToolResult;
Chat/Responses physical request bytes remain owned by the exact wire plan.
Compiler variants may begin at COMPACT/REF_ONLY/OMITTED when FULL is
ineligible, while successful `artifact_read` pages require exact FULL delivery
or stop before provider open. Artifact guidance is conditional, canonical
results are never rewritten for budget, and installed same-epoch messages
remain append-only. Verification is recorded in
[`round7_1_provider_visible_tool_result_projection_activation.json`](benchmarks/suites/core/v1/round7_1_provider_visible_tool_result_projection_activation.json).
Round 8 replaces the old memory durability/recovery graph with an advisory
dataset. `remember` atomically accepts one candidate with its ToolResult, while
governance, cheap-hint reflection, embedding, and reranking remain lossy
process-local work. Accepted items use the closed FACT, USER_PROFILE,
RESPONSE_PREFERENCE, ACTION_RULE, and DECISION taxonomy. Sparse recall is
always local; automatic dense recall and explicit rerank are optional remote
data egress. Memory enters model input only through bounded append-only
`MEMORY_RECALL` and `MEMORY_RESPONSE_PREFERENCE_HEAD` observations, never by
rewriting an installed prefix or granting permission authority. Verification
uses the same tokenizer-v2 sparse terms for indexing and queries, preserving
negation, ordering words, code/path tokens, and lexical contractions. A
command that forbids saving the current entry disables `remember` for that
ROOT run while leaving recall visible; an explicit “do not use saved memory
for this answer” clears both memory observations and denies all four memory
tools without changing the advertised tool surface. Short-input recall
skipping remains independent and does not disable explicit memory tools.
Verification
is recorded in
[`round8_advisory_memory_subsystem_activation.json`](benchmarks/suites/core/v1/round8_advisory_memory_subsystem_activation.json).
The lightweight TODO refinement replaces the old Host-global action protocol
with one bounded full-snapshot call and an exact ROOT/child process-local owner.
Canonical ToolResult success is settled before the snapshot is installed;
clients receive one atomic live projection and resynchronize from the current
Host owner after a live gap. TODO is advisory, never durable, and Round 5B may
only consume its read-only actionable handoff. Verification is recorded in
[`lightweight_todo_tool_refinement_activation.json`](benchmarks/suites/core/v1/lightweight_todo_tool_refinement_activation.json).
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

## Client boundary

The bundled Go TUI has been removed. Protocol v3 remains the renderer-neutral
transport boundary for a future Web or desktop client. Python continues to own
canonical state, commands, policy, secrets, and recovery; a client may only
render snapshots, consume live events, and submit the typed commands admitted
by the gateway. Protocol v2 and Presentation Foundation are not retained.

## Development

```sh
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src tests
uv run python tools/generate_terminal_protocol_contract.py --check
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
