# 🪐 Pulsara — An Agent Runtime That Remembers You Better

<p align="center">
  <img src="assets/banner.png" alt="Pulsara" width="100%">
</p>

<p align="center">
  <strong>FEEL FREE TO VIBE WITH PULSARA</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/PostgreSQL-runtime%20truth-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Oxigraph-semantic%20graph-5b21b6?style=for-the-badge" alt="Oxigraph">
  <img src="https://img.shields.io/badge/Python-3.12+-111827?style=for-the-badge&logo=python" alt="Python 3.12+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

**Pulsara** is a _local-first Agent Runtime_ for long-horizon work.
It lets an assistant deeply inhabit real projects, handle long conversations,
preserve essential context, and reconstruct what happened when the task becomes
complex.

It is built for durable workflows: an agent reads files, executes terminal
commands, follows skill logic, resumes historical sessions, compacts context,
and persists long-term memory so every interaction compounds instead of fading
into an old chat window.

**Pulsara is still under active development. It is not a mature product yet, and
you should expect bugs, rough edges, and fast-moving interfaces. Today the main
surface is a terminal REPL; a graphical interface may come later. If you try it,
please open issues, share what breaks, and help us learn together.**

[English](README.md) · [简体中文](README.zh-CN.md) · [Core Idea](#core-idea-redefining-long-horizon-agent-work) · [Architecture](#architecture-layered-replayable-and-auditable) · [Memory](#memory-as-an-asset-for-future-decisions) · [Quick Start](#quick-start)

---

## Core Idea: Redefining Long-Horizon Agent Work

Many agents shine in the first ten minutes. Pulsara is built to protect the next
ten hours.

Long-horizon agent work has become a living runtime lifecycle:

- The model plans, asks for confirmation, and resumes suspended work.
- Toolchains continuously produce logs, files, intermediate state, artifacts,
  and errors.
- Context keeps growing, so the system must compact it while preserving the
  thread of the task.
- Model-visible context must be compiled from structured runtime facts instead
  of blindly replaying an ever-growing transcript.
- Important facts become long-term memory instead of getting buried in a long
  transcript.
- Future operations remain traceable, answering both “what happened?” and “why
  did it happen?”

Pulsara treats this complete lifecycle as the product.

| Common experience | Pulsara experience |
| --- | --- |
| Sessions fade when the window closes | **Persistent Runtime**: sessions, runs, turns, replies, and resume are first-class. |
| Conversation history is hard to reconstruct | **Replayable event stream**: typed runtime facts with inspectable projections. |
| Context gets heavier with every turn | **Context compiler + compaction**: budgeted, sectioned, time-aware context for future model interactions. |
| Memory lacks a source trail | **Grounded long-term memory**: memories are tied to evidence, artifacts, relations, and conflicts. |
| Tool calls are hard to explain | **Auditable tool execution**: permission decisions, artifact creation, skill attribution, and observation timing stay transparent. |
| Capabilities live in static prompts | **Progressive capability surface**: tools, skills, MCP-style interfaces, and local workflows share one runtime surface. |
| Delegation becomes hidden side chat | **Subagent runtime graph**: child agents are runtime sessions with typed parent-child events, not invisible scripts. |

Pulsara goes beyond a conventional model API wrapper and grows toward a full
agent operating system: conversation, tools, memory, permissions, events,
storage, compaction, and recovery fused into a unified runtime.

## Architecture: Layered, Replayable, and Auditable

Agent context deserves structure. Pulsara organizes long-horizon work with a
three-layer storage architecture so runtime facts, execution artifacts, and
semantic memory can each do their job.

```text
┌──────────────────────────────────────────────────────────────┐
│                         Agent Runtime                         │
│  model loop · context compiler · tool execution · subagents    │
│  plan mode · permission gate · capability exposure · resume    │
│  compaction · time-aware observations                          │
└──────────────────────────────┬───────────────────────────────┘
                               │ typed events
┌──────────────────────────────▼───────────────────────────────┐
│                    Layer 1 — Runtime Ledger                   │
│  sessions · runs · turns · replies · tool calls · plan state   │
│  context compaction events · capability decisions · inspector  │
└──────────────────────────────┬───────────────────────────────┘
                               │ artifacts + evidence
┌──────────────────────────────▼───────────────────────────────┐
│                  Layer 2 — Artifact & Evidence Store          │
│  large tool outputs · adaptive previews · archived refs        │
│  compaction summaries · execution evidence · replay anchors    │
└──────────────────────────────┬───────────────────────────────┘
                               │ governed memory writes
┌──────────────────────────────▼───────────────────────────────┐
│                   Layer 3 — Semantic Memory Surface           │
│  canonical memories · search index · conflicts · relations     │
│  PostgreSQL truth · Oxigraph semantic graph · recall traces    │
└──────────────────────────────────────────────────────────────┘
```

### The three storage layers tell one complete story

| Layer | What it stores | Why it exists |
| --- | --- | --- |
| **Runtime Ledger** | Sessions, turns, replies, plan transitions, tool calls, capability decisions, compaction boundaries. | Keeps the agent resumable and explainable. The system can locate the root of an interruption, approval, compaction, or failure. |
| **Artifact & Evidence Store** | Large terminal output, scraped pages, generated files, tool previews, compaction summaries, evidence anchors. | Gives the model compact previews, gives humans full recovery, and gives memory a grounded source trail. |
| **Semantic Memory Surface** | Canonical memories, conflict markers, supersession, search indexes, graph relations, recall traces. | Lets long-term memory be queried by meaning, relation, scope, and provenance, beyond simple nearest-neighbor text. |

Each layer has a clear responsibility. Runtime records facts, artifacts preserve
recoverability, and memory guides future decisions. Stable event IDs and
evidence IDs bind the layers together.

## Event System: The Runtime Skeleton

Pulsara's event system forms the factual skeleton of the agent. The runtime emits
typed events for key transitions:

- model text generation and tool-call streaming;
- tool results, artifacts, adaptive previews, and observation timing;
- permission decisions and capability gate outcomes;
- plan mode entry, questions, revisions, approval, and cancellation;
- context compile attempts, section budgets, render decisions, and diagnostics;
- subagent graph events, task states, child results, and result delivery;
- context compaction started, completed, or failed;
- durable session and run boundaries;
- memory recall traces and governance outcomes.

This event engine powers three essential capabilities:

1. **Resume** — restart a session and rebuild the model-visible conversation
   with precision.
2. **Inspect** — reconstruct historical operations without guesswork or terminal
   archaeology.
3. **Compaction** — summarize long context from real runtime history, keeping the
   handoff coherent and grounded.

## Context Compiler: Model-Visible State Is Built, Not Dumped

Pulsara now treats prompt construction as a runtime subsystem. The context
compiler collects typed facts, partitions them into sections, applies lifecycle
caching, budgets each section, and then lowers the result into the provider
request.

That means model-visible context has a structure:

- stable system instructions remain separate from runtime facts;
- memory projection, capability catalog, active-skill hints, runtime context,
  recovery notes, and subagent results are sectioned and inspectable;
- the current user message is anchored before the current-run tool tail so
  provider-native tool-call ordering stays valid;
- large or historical tool results are rendered through an allocator with
  per-result decisions, artifact-backed previews, and bounded envelopes;
- timing headers tell the model whether a section is current, historical,
  cached, compacted, or a background-process observation;
- `ContextCompiledEvent` records sections, estimates, diagnostics, and
  tool-result render decisions for inspection.

The goal is simple: the model should see the smallest useful version of the
truth, while humans and recovery paths can still explain how that context was
assembled.

## Memory as an Asset for Future Decisions

Pulsara's memory pipeline is grounded in runtime evidence:

```text
conversation → tool results → artifacts → evidence → memories → recall
```

The system focuses on information that changes future behavior:

- durable user preferences and working context;
- decision chains made during a project;
- factual claims supported by explicit evidence;
- contradictions and superseded states;
- relationships between memories, artifacts, and historical work.

### Recall that understands structure

Pulsara recall combines multiple signals and returns structured results:

- **lexical and semantic retrieval** for direct access to relevant facts;
- **contradiction companions** so safety-critical conflicts surface together;
- **multi-hop explicit search** for evidence, dependencies, and derived memory
  paths;
- **trace recording** for evaluation, debugging, and replay;
- **scope and status filtering** to keep hidden, invalid, or stale memories out
  of the wrong context.

Automatic recall stays disciplined so every turn is not flooded with memory.
Explicit search opens the deeper semantic graph when the task needs it.

## Capability Matrix

### Tool flow for local work

Pulsara integrates filesystem operations, terminal execution, long-running
process polling, large-output artifacts, and memory search. Real tool calls flow
through the permission gate, event log, and inspector rather than disappearing
behind the final answer.

### Skills and Capability Surface

Skills in Pulsara are part of progressive capability exposure. The system
resolves capabilities per turn, evaluates them through policy, exposes available
actions to the model, and records the relevant decision as a runtime fact.

When a skill guides a terminal call, or when a capability is hidden, denied,
approved, or unavailable, Pulsara preserves the explanation chain.

### Plan Mode

For high-risk tasks, the agent can first explore in read-only mode, ask
structured human-in-the-loop questions, produce a draft plan, and execute only
after approval. Planning is integrated with the permission model and the event
log.

### Subagent Runtime

Pulsara can delegate bounded subtasks to child agent runtimes. A subagent is a
runtime session with its own event stream, capability profile, lifecycle, and
result, not a function call hidden inside the parent prompt.

The parent records a graph of subagent runs and tasks:

- `spawn_agent`, `wait_agent`, and `stop_agent` expose low-level run control;
- `create_agent_tasks`, `wait_agent_tasks`, and `stop_agent_task` expose a
  task-board layer with dependencies and batch status;
- child-only report tools let workers publish phase updates and explicit
  results;
- child results enter parent context as internal evidence, not as fake user
  messages;
- `list_agents` and inspector projections show running, blocked, completed,
  consumed, and delivered work.

This keeps delegation auditable. The parent can use subagent reports as
secondary evidence, while filesystem reads, terminal runs, artifacts, and event
logs remain the highest-confidence sources of truth.

### Time-Aware Tool Observations

Long-running local work is time-sensitive. Pulsara records tool observation
timing as a runtime fact for tool results, including terminal and
`terminal_process` observations.

Model-visible tool context can include:

- when the observation was seen;
- when the tool result started and ended;
- observation duration;
- whether the output is a current tool result, a background process observation,
  a suspended/resumed interaction, or historical replay;
- terminal-domain timing such as process duration for running commands.

This lets the model reason about streaming logs, background training jobs,
process polling, stale results, and “what happened since the last observation”
without guessing from text alone.

### Context handoff

Pulsara treats compaction as a core runtime event and writes a durable handoff
summary. The summary describes what happened, where execution should continue,
which tools or processes still matter, and which facts deserve preservation.
Failures are surfaced explicitly, avoiding silent state pollution.

## Quick Start

Pulsara is currently source-first. Clone the repository, initialize the
environment with `uv`, start local durable services, and open the REPL.

```bash
git clone <your-pulsara-repo-url>
cd pulsara_agent

uv sync
docker compose up -d postgres oxigraph

cp .env.example .env
$EDITOR .env

uv run pulsara db migrate --env-file .env
uv run pulsara db verify --deep --env-file .env
uv run pulsara config-check --env-file .env
uv run pulsara host repl --env-file .env --workspace .
```

The bundled Docker setup creates an admin database owner and a restricted
runtime role. It does not create pgvector or Pulsara tables during container
initialization. `pulsara db migrate` is the only schema mutation owner; it uses
`PULSARA_POSTGRES_ADMIN_DSN` to install pgvector, apply immutable migrations,
reconcile runtime grants, and record the durable migration ledger.

Keep `PULSARA_POSTGRES_DSN` bound to the restricted runtime role. Host,
Inspector, checkpoint, and benchmark runtime paths are verify-only and fail
before allocating session resources when the database is unmigrated, stale, or
drifted. Existing unmanaged Pulsara tables are deliberately not adopted in V1;
reset the PostgreSQL database (and the corresponding Oxigraph projection) before
the migration cutover.

Inside the REPL:

```text
pulsara> hello
pulsara> :help
pulsara> :plan
pulsara> :compact
pulsara> :close
```

Use the CLI to resume historical sessions or inspect runtime state:

```bash
uv run pulsara host repl --env-file .env --workspace . --continue
uv run pulsara host repl --env-file .env --workspace . --list-sessions
uv run pulsara inspect health --env-file .env
```

## Configuration

Pulsara uses two model roles:

- `pro` handles the main reasoning path;
- `flash` handles fast compaction, governance, and helper work.

Minimal `.env`:

```dotenv
PULSARA_API_KEY=sk-your-api-key
PULSARA_BASE_URL=https://api.openai.com/v1
PULSARA_PRO_MODEL=gpt-5
PULSARA_FLASH_MODEL=gpt-5-mini
PULSARA_MODEL_IDENTITY_POLICY=accept_reported
PULSARA_PRO_TOTAL_CONTEXT_TOKENS=4096
PULSARA_PRO_MAX_INPUT_TOKENS=3584
PULSARA_PRO_MAX_OUTPUT_TOKENS=1024
PULSARA_PRO_DEFAULT_OUTPUT_TOKENS=512
PULSARA_PRO_INPUT_SAFETY_MARGIN_TOKENS=128
PULSARA_FLASH_TOTAL_CONTEXT_TOKENS=4096
PULSARA_FLASH_MAX_INPUT_TOKENS=3584
PULSARA_FLASH_MAX_OUTPUT_TOKENS=1024
PULSARA_FLASH_DEFAULT_OUTPUT_TOKENS=512
PULSARA_FLASH_INPUT_SAFETY_MARGIN_TOKENS=128

PULSARA_POSTGRES_ADMIN_DSN=postgresql://pulsara_admin:pulsara_admin@localhost:5432/pulsara
PULSARA_POSTGRES_DSN=postgresql://pulsara_runtime:pulsara_runtime@localhost:5432/pulsara
PULSARA_OXIGRAPH_URL=http://localhost:7878
```

The admin DSN is read only by `pulsara db migrate`; it is not part of runtime
settings, wiring, logs, or Inspector output. Use `pulsara db status` for a
bounded migration report and `pulsara db verify --deep` for an offline full
catalog check.

The model-limit values above are only internally consistent examples, not a
model catalog. Set them to the documented limits of the exact provider/model
you use. Pulsara validates the contract but does not guess limits from a model
name.

`PULSARA_MODEL_IDENTITY_POLICY` defaults to `accept_reported`, so a provider may
resolve a requested alias to a concrete snapshot while Pulsara records both
identities. Set it to `exact` only when the provider guarantees that response
model IDs exactly echo requested IDs.

The main path targets OpenAI-compatible Responses APIs today. Other compatible
providers can work when their wire behavior matches the configured API mode.

## Local Skills

Pulsara can load local skills from Pulsara / agent skill directories and expose
them as compact routing hints. When a task matches a skill, the agent can read
the full `SKILL.md` with normal read-only file tools, then act through terminal
or other built-in tools.

```bash
uv run pulsara skills status
uv run pulsara skills sync-bundled
uv run pulsara host repl --env-file .env --workspace . --skill hf-cli
```

Skills provide guidance; the runtime remains the source of fact. Pulsara records
tool calls, permission decisions, artifacts, and active-skill attribution.

## Status

Pulsara is early, sharp-edged, active, and moving quickly.

It is built for people comfortable running a local Python project, editing
`.env`, starting Docker services, and reading inspector output when necessary.
If you want real tool execution, semantic recall, resumable long sessions, and
deep explainability, welcome aboard.

## Development

```bash
uv run ruff check src tests
uv run pytest -q
```

Frozen real-provider dogfood suite:

```bash
uv run python -m benchmarks.suites.run_core_dogfood validate

PULSARA_RUN_CORE_DOGFOOD=1 \
uv run python -m benchmarks.suites.run_core_dogfood run \
  --env-file .env \
  --confirm-network
```

## License

Pulsara is released under the [MIT License](LICENSE).
