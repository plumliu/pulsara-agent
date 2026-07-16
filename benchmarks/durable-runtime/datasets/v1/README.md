# Pulsara Durable Runtime Dataset v1

This directory contains deterministic, offline benchmark scenarios for the
durable runtime performance program.

The dataset never calls a model provider or another external network API. Local
PostgreSQL is the only allowed service. The dataset describes inputs for two
local benchmark layers:

- `writer-scenarios/`: canonical event-write, materialization-account, batching,
  contention, and stable-confirmation workloads against PostgreSQL.
- `context-scenarios/`: generated canonical ledgers and logical compile points
  used to measure authority reads, transcript evidence, checkpoint restore,
  artifact hydration, and context preparation.

The Reasonix-like agent trajectories live under `../../suites/`. They validate
real task behavior and may use a real provider. They are deliberately separate
from this deterministic dataset.

## Runner

The dataset is a collection of workload recipes, not pre-generated stored
events. The benchmark runner must bind each recipe to production DTO builders,
seed an isolated PostgreSQL ledger, execute the requested writer or context
operation, and grade the declared invariants.

The initial runner exposes contract validation, deterministic case expansion,
and a process-isolation smoke test:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py validate

uv run python benchmarks/durable-runtime/runners/run_dataset.py plan \
  --group context

uv run python benchmarks/durable-runtime/runners/run_dataset.py smoke \
  --jobs 4 \
  --iterations 2 \
  --output /tmp/pulsara-durable-runtime-smoke.jsonl
```

`smoke` does not claim to measure runtime performance. It verifies that every
expanded case can be loaded deterministically by isolated worker processes and
that only the coordinator writes the result file. PostgreSQL writer and context
adapters are separate bindings built on top of this runner protocol.

The first executable production adapter is the model semantic batching matrix:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-writer \
  --postgres-dsn "$PULSARA_POSTGRES_DSN" \
  --postgres-admin-dsn "$PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN" \
  --template-database pulsara \
  --output .benchmarks/model-semantic-batch.jsonl
```

This command is serial by contract and runs only the `batch-16` production
baseline. Every sample clones a clean PostgreSQL
database before the timed region, runs the real
`LLMRuntime -> RuntimeSession -> PostgreSQL` path, and drops the clone after
grading. The manifest's 5 warmups and 30 measured iterations define the
production baseline.

Production acceptance additionally requires a clean Git worktree. The result
contract records the exact commit and rejects `dirty=true` samples even when
all timing iterations and semantic graders pass.

The application DSN should use the normal Pulsara database role. The separate
admin DSN is used only for `CREATE DATABASE ... TEMPLATE ...` and `DROP
DATABASE`; it must be local and have `CREATEDB`. If it is omitted, the runner
tries the application DSN and fails before timing when that role lacks the
privilege.

For a quick wiring check, use the explicitly non-acceptance diagnostic
overrides:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-writer \
  --case-id batch-16 \
  --diagnostic-warmup-iterations 0 \
  --diagnostic-measured-iterations 1 \
  --output .benchmarks/model-semantic-batch-diagnostic.jsonl
```

Diagnostic iteration or case filters are recorded and cannot pass production
acceptance. Batch 16 is the only production-valid baseline. Batch 4 and 8 are
executable sensitivity cases:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-writer \
  --case-kind sensitivity_analysis \
  --output .benchmarks/model-semantic-batch-sensitivity.jsonl
```

Sensitivity results can explain batching behavior but can never pass production
acceptance. Batch sizes 1, 32, and 64 are counterfactual analysis only and are
not executable through the production writer adapter. The runner writes one
JSONL row per measured case and a sibling `.summary.json` with
environment identity, the raw-vector hash, and per-case median, nearest-rank
p95, minimum, and maximum.

The primary timing metrics are `model_stream_wall_seconds` and
`semantic_commit_port_wall_seconds`. Counts are explicitly logical commit-port
batches, not claimed PostgreSQL transaction counts. The cluster WAL LSN delta
is retained as a diagnostic trend only and is not an acceptance metric.

The executable context-preparation adapter covers all six context scenario
families:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-context \
  --scenario long-plan-prefix-growth \
  --postgres-dsn "$PULSARA_POSTGRES_DSN" \
  --postgres-admin-dsn "$PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN" \
  --template-database pulsara \
  --output .benchmarks/long-plan-context.jsonl
```

Run one scenario per invocation. Every adapter uses real production DTO
builders, PostgreSQL ledgers, terminal projections, materialization accounts,
checkpoint services, and context preparation. The common hot-path scenarios
then time:

```text
prepare_live_context_snapshot
-> terminal projection hydration
-> normalized transcript projection
-> tool-result rendering/projection
-> pure context compile
```

`process_cold` closes and reopens the `RuntimeSession` before each measured
compile point. `steady_state` keeps the session-owned reducers and caches.
`verified_artifact_cache_warm` performs an untimed preparation at each point
before measuring the repeated preparation. Fixture writes, database cloning,
and cleanup stay outside the recorded context timings.

The specialized adapters preserve their defining lifecycle:

- `single-long-compaction` runs a real
  `ContextWindowCompactionService` summarizer call, source/input/summary
  artifacts, rollout settlement, and atomic window close/open before measuring
  the compacted baseline.
- `subagent-two-children` runs two real task-backed child agents with isolated
  PostgreSQL ledgers, child-native terminal batches, explicit result handoff,
  dependency ordering, and a durable graph checkpoint.
- `checkpoint-rebase-and-restart` writes two real transcript checkpoints,
  optionally removes the preferred checkpoint's newly written COW artifacts,
  closes the process-local session, and measures deterministic restore or
  rebase from the same runtime session.

For a quick production-wiring diagnostic:

```bash
uv run python benchmarks/durable-runtime/runners/run_dataset.py \
  benchmark-context \
  --scenario incremental-active-window \
  --mode steady_state \
  --diagnostic-warmup-iterations 0 \
  --diagnostic-measured-iterations 1 \
  --postgres-dsn "$PULSARA_POSTGRES_DSN" \
  --postgres-admin-dsn "$PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN" \
  --template-database pulsara \
  --output .benchmarks/incremental-context-diagnostic.jsonl
```

Parallel functional smoke runs may share one PostgreSQL server, but final
latency baselines must run serially or use one
PostgreSQL instance per worker. Separate databases in one server still share
CPU, WAL, and storage latency. Scenario-defined concurrency, such as
`multi-session-contention`, is always controlled inside the scenario and should
run with one outer worker.

## Dataset rules

- Every scenario has a stable `scenario_id`, schema version, and integer seed.
- Every scenario binds a versioned production generator and executable semantic
  grader contract.
- Scenario content is declarative. Generators must use Pulsara production DTOs
  and event builders rather than hand-constructing stored event JSON.
- Generator-owned workload timestamps derive from `2026-01-01T00:00:00Z`
  plus deterministic logical offsets. Production-owned lifecycle timestamps
  may use the runtime clock; they are not benchmark inputs or timing metrics.
- IDs derive from the scenario ID, seed, owner kind, and logical ordinal.
- Text and binary payloads are synthetic and secret-free.
- Logical compile points are expressed by phase or model-call ordinal. Runners
  resolve them to actual committed ledger sequences.
- Performance expectations do not belong in scenario files. Baseline results
  are environment-specific and are written under `../../baselines/`.
- Every measured iteration starts from a fresh database cloned from a frozen
  template. Setup and cleanup are outside the timed region.
- `production_valid` cases may participate in acceptance gates.
  `counterfactual_analysis` cases are reported separately and cannot satisfy or
  fail production acceptance.
- A scenario update requires a new dataset version when it changes workload
  semantics rather than merely fixing invalid metadata.

## Writer scenarios

- `model-semantic-batch-matrix`: isolates caller-observed durable writer time,
  logical batching, and account-charge cost across deterministic batch sizes.
- `model-semantic-structural-grouping`: compares immediate structural flushes
  with pairing-safe grouping.
- `multi-session-contention`: measures shared pool and critical writer behavior
  across concurrent sessions.
- `stable-confirmation-faults`: verifies stable candidate retry and uncertain
  commit handling.
- `mixed-runtime-accounting`: exercises model, tool, subagent, context-audit,
  and terminal facts through one accounted session.

## Context scenarios

- `long-plan-prefix-growth`: reproduces the repeated `base -> H_i` evidence-read
  shape observed in the long Plan dogfood. Dataset v1 uses 4,096 durable
  semantic deltas across 19 steps so the workload measures cumulative prefix
  cost without deliberately tripping the current 16 MiB physical authority
  circuit breaker.
- `incremental-active-window`: isolates many small high-water advances.
- `single-long-compaction`: compares pre-compaction, compacted baseline, and
  bounded post-compaction delta.
- `subagent-two-children`: freezes parent plus two child ledger authority.
- `artifact-heavy-tools`: stresses terminal projection document hydration while
  ensuring large canonical result text is represented by bounded checkpoint
  references rather than duplicated into the transcript tree.
- `checkpoint-rebase-and-restart`: covers preferred checkpoint, rebase, and
  cold session reopen.
