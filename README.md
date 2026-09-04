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
├── foreground conversation coordinator
│   ├── exact turn admission + provider dispatch
│   ├── compaction + Plan + Tool batch coordinators
│   └── memory + steer dispatch support
├── provider-neutral structured model-input compiler
├── tool policy + Host-scoped physical tools
├── foreground safe-point compaction + snapshot adoption
├── advisory memory governor + retrieval
├── process-local live event bus
└── renderer-neutral Protocol v3 gateway

PostgreSQL
├── pulsara_v3: 25 product relations
├── selective agent_events occurrence journal
├── public.vector capability
└── public.pulsara_schema_migrations (universe metadata only)
```

The durable boundary is intentionally narrow:

- canonical relational rows own current conversation, tool, and
  coordination truth; accepted memory rows own only the current contents of
  the advisory dataset;
- a closed 29-type `agent_events` journal records accepted occurrences but is
  never replayed to reconstruct execution;
- 24 live event types exist only in memory and may be lost at process exit;
- no durable job handler or job relation remains;
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
- ROOT-orchestrated Host-scoped worker graphs with batch DAG admission,
  dependency scheduling, partial multi-wait, exact task stop, boundary-safe
  ROOT-to-worker messages, explicit/inferred canonical results, and bounded
  `NONE | LAST_N` parent context; workers remain non-recursive leaves;
- Agent Skills-standard bundled and local skills across the exact workspace/user
  `.pulsara/skills` and `.agents/skills` roots, projected through one aggregate,
  append-only `SKILL_CATALOG` source without execution or permission authority;
  catalog routing metadata is complete-or-unavailable, explicit/configured
  activation carries the exact parsed Markdown body, and model-driven
  progressive disclosure uses ordinary `read_file` with a 2,000-line window;
- unified, process-local capability discovery: execution-backed Builtins,
  per-server MCP snapshots, and the aggregate Skill catalog enter one pure
  frozen registry while their original owners retain physical authority;
- Host-scoped MCP over stdio and Streamable HTTP, with bounded discovery,
  cold direct tools, late/native-incompatible meta inspection and invocation,
  typed unavailable gates, catalog/resource/prompt reads, local authorization,
  and CLI lifecycle management;
- advisory PostgreSQL memory with one-candidate `remember`, four closed item
  kinds, global and exact workspace applicability, best-effort
  governance, multilingual sparse recall, optional 1024-dimensional dense
  recall and explicit rerank, direct/reverse relation reads, and at most
  two-hop traversal;
- foreground safe-point context compaction with manual, proactive, and
  mid-turn entry points; summary adoption preserves the canonical transcript,
  keeps a pairing-safe protected tail, and continues in a standard cold
  capability epoch without a durable compaction job;
- canonical inspection and Protocol v3 terminal observation.

## Frontend

The repository now includes the Pulsara local workbench under
[`frontend/`](frontend/). It connects directly to the local Host and renders only
surfaces backed by an existing Kernel contract: overview, durable sessions,
execution traces, Plan interactions, per-turn permissions, queued prompts,
stop, context compaction, subagent tasks, and local configuration. Child work is
visible inline where it was delegated, with its objective, live tool activity,
current status, and Markdown-rendered result. The current-session inspector
keeps the complete task history beside the conversation; there is no separate
task destination to manage. The workbench no longer
uses a demo projection or reserves placeholder navigation for unsupported
features. Session creation has exactly two workspace choices: Quick Start asks
Pulsara to create a durable managed directory, while Specified Directory uses
an existing absolute path. Plan and permission choices live beside the composer
and apply to one turn. The bare loopback URL opens directly without a token,
cookie, OpenAI login, or other website account. See
[`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md`](PULSARA_FRONTEND_APPLICATION_SPEC.zh.md).

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

Pulsara no longer reads product configuration from `.env` or product-specific
environment variables. Start the local app and use **Settings** to:

- save the local PostgreSQL runtime DSN and optional admin DSN;
- explicitly check the runtime database or initialize/migrate it with the
  admin DSN;
- choose a provider and a models.dev-backed model, select Chat Completions or
  Responses, and add the API key;
- optionally add the two independent DashScope keys for embedding and rerank.

Non-secret metadata is stored in `${PULSARA_HOME}/local-settings.yaml` (or the
default Pulsara home). API keys are write-only and live in macOS Keychain. The
app and Settings shell start with zero configuration; sparse memory recall
continues when either DashScope credential is absent.

```sh
uv run pulsara app
uv run pulsara config-check
uv run pulsara db verify --deep
```

The first command starts the loopback-only Web application and opens it in a
browser. Pass `--no-open` to start the service without opening a page. Durable
sessions resume with their exact workspace after the page or service restarts.

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
timeout. At that checkpoint finite durable jobs retained their bounded attempt
totals. Verification is recorded in
[`round5_long_horizon_execution_envelope_activation.json`](benchmarks/suites/core/v1/round5_long_horizon_execution_envelope_activation.json).
Round 5B now adds manual, proactive, and mid-turn safe-point compaction. The
current primary model summarizes the old exact prefix with tools disabled;
active adoption then continues the same run in a new standard cold epoch built
from the snapshot, recent human input, a pairing-safe protected tail, current
Runtime observations, and bounded retained Skill context. Canonical history is
never rewritten, provider-error reactive retry remains unsupported, and the
last durable job machinery has been removed. The Round 5B activation oracle was
28 committed events, 24 live events, 11 subject slots, one append guard, 24
product relations, and zero durable jobs. Verification is recorded in
[`round5b_long_horizon_context_compaction_activation.json`](benchmarks/suites/core/v1/round5b_long_horizon_context_compaction_activation.json).
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
Round 9 replaces the old parallel tool/Skill exposure structures with one pure,
provider-neutral capability registry assembled only from owner-issued Builtin,
MCP, and aggregate-Skill snapshots. Exact target-aware native preflight occurs
before the parent dispatch cut; Tool planning and Skill projection consume
sibling views from that same cut. A cold MCP cohort is either wholly admitted
within both canonical and actual-wire bounds or kept meta-only. Late-ready and
native-wire-incompatible tools use bounded `inspect_new_mcp_tool` then
`use_new_mcp_tool`; a policy/route-bound ref becomes callable only after the
inspection result is installed FULL. Same-epoch SYSTEM and tools remain
byte-stable, and catalog/route changes append messages only. No capability
relation, event, job, receipt, generation, or recovery graph was added.
Verification is recorded in
[`round9_unified_capability_semantics_activation.json`](benchmarks/suites/core/v1/round9_unified_capability_semantics_activation.json).
Round 9.1 replaces the legacy Pulsara Skill frontmatter contract with the
portable Agent Skills core. The Skill owner scans the exact four roots through
one scope-bound policy, resolves global precedence, and contributes one
aggregate `LOCAL_SKILL_CATALOG` snapshot to the existing Round 9 registry.
Invalid individual manifests are omitted with bounded diagnostics; an
unprovable or overbound complete scan publishes one UNAVAILABLE successor and
never a partial catalog. Catalog and active-body changes append messages only;
SYSTEM and tools remain stable. Host-specific fields including `allowed-tools`
are inert data, and Skill text cannot grant tools, permissions, MCP routes, or
execution authority. Ordinary `read_file` is the only progressive-disclosure
path: it has no Skill intent or loaded-state and repeated reads return current
bounded bytes. Verification is recorded in
[`round9_1_agent_skills_standard_activation.json`](benchmarks/suites/core/v1/round9_1_agent_skills_standard_activation.json).
Round 10 upgrades flat children to one ROOT-owned worker task graph. Seven
ROOT-only orchestration tools remain provider-visible in every ROOT permission
mode but execute only under `BYPASS_PERMISSIONS`; workers receive only
`report_agent_result` and cannot create descendants. Stable task/dependency rows
own the logical board, while scheduling, capacity, mailbox delivery, and waits
remain process-local. The four-child limit is physical concurrency rather than
a task-graph lifetime cap: additional admitted work stays `PENDING_START` and
starts when capacity frees. Direct dependency results propagate one edge only;
the main agent keeps doing useful work while children run, and completed work is
automatically folded into the conversation at a safe response boundary. Waiting
is only a synchronization choice, not the result-delivery path. Work that finishes
after an answer remains available for one explicit continuation without rerunning
the child; success, failure, cancellation, and dependency failure share the same
delivery path. Round 5B now hands off the same task board during compaction without
adding a durable inbox, run, receipt, or recovery graph. The current oracle is 29 committed events, 24
live events, 11 subject slots, one append guard, 25 product relations, and zero
durable jobs. Verification is recorded in
[`round10_hierarchical_subagent_orchestration_activation.json`](benchmarks/suites/core/v1/round10_hierarchical_subagent_orchestration_activation.json).
The asynchronous-completion hard cut and real-provider browser evidence are recorded in
[`PULSARA_SUBAGENT_ASYNC_COMPLETION_HARD_CUT_RESEARCH_AND_IMPLEMENTATION_SPEC.zh.md`](PULSARA_SUBAGENT_ASYNC_COMPLETION_HARD_CUT_RESEARCH_AND_IMPLEMENTATION_SPEC.zh.md)
and [`dogfood_evidence/async_completion/README.zh.md`](dogfood_evidence/async_completion/README.zh.md).
The fingerprint-subtraction hard cut removes same-process self hashes,
duplicate child/parent proof fields, fingerprint-based continuity/settlement
lookups, and per-file activation SHA inventories in one incompatible internal
cut. Exact frozen objects, owner slots, nonce/revision checks, and database
constraints now carry process-local authority. Content integrity, durable
confirmation, provider prefix/replay compatibility, stable canonical identity,
and keyed opaque-token digests remain unchanged; provider wire, Protocol v3,
canonical rows, and the architecture oracle do not change. Verification is
recorded in
[`fingerprint_subtraction_hard_cut_activation.json`](benchmarks/suites/core/v1/fingerprint_subtraction_hard_cut_activation.json).
The production-code cleanup hard cut keeps the same foreground state machine
while moving turn admission, provider dispatch, compaction, Plan control, tool
execution, memory, and steer settlement behind narrow owners. The runner now
decides the next phase without reimplementing those authorities; no legacy
facade, compatibility import, new fingerprint, or durable recovery mechanism
remains. Verification is recorded in
[`production_code_cleanup_and_runner_decomposition_activation.json`](benchmarks/suites/core/v1/production_code_cleanup_and_runner_decomposition_activation.json).
Round 8 replaces the old memory durability/recovery graph with an advisory
dataset. `remember` atomically accepts one candidate with its ToolResult, while
governance, cheap-hint prompting, embedding, and reranking remain lossy
process-local work. Accepted items use the closed FACT, USER_PROFILE,
RESPONSE_PREFERENCE, and DECISION taxonomy. Sparse recall is
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
[the clean-baseline runbook](archived_docs/STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md) and never
reset a real endpoint without explicit operator authorization.

## Run

The local Web app is the primary configuration and conversation surface:

```sh
uv run pulsara app --workspace /path/to/project
```

After a session has an explicit model connection selected, the headless REPL
can resume that canonical session without any environment-file path:

```sh
uv run pulsara host repl \
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
