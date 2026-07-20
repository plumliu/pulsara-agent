# Pulsara Core Dogfood Suites

`benchmarks/suites` contains the small, frozen set of expensive real-provider
trajectories used as release evidence. It is intentionally separate from
`evals/`: deterministic model-quality datasets belong in `evals`, while these
scenarios exercise Pulsara as a running product with PostgreSQL, Oxigraph,
provider calls, workspace tools, close drain, and durable replay.

The design borrows three useful properties from DeepSeek-Reasonix's E2E bench:

1. every task runs in a fresh copied or deterministically generated workspace;
2. the model cannot see the post-run verifier;
3. the runner emits attributed JSON evidence instead of treating final prose as
   proof.

## Core v1

| Scenario | Product boundary frozen |
|---|---|
| `workspace-patch` | ordinary Host tool loop and behavioral patch verification |
| `cache-continuity` | three strict-suffix turns and provider-reported cache use |
| `durable-resume` | detach, process replacement, exact durable reopen |
| `manual-compaction-trail` | long linked evidence, manual compaction, no-reread recall |
| `subagent-delegation` | one child task, explicit result delivery, parent isolation |
| `plan-workflow` | typed question, exit, approval, implementation continuation |

Scenario JSON, seed files, generated-fixture recipes, and hidden verifier bytes
are content-addressed. `manifest.json` freezes each resulting fingerprint.
Changing a task therefore requires an explicit manifest update and creates a
new piece of release evidence.

Five trajectories use the lower-cost `flash` role; `workspace-patch` uses
`pro`, so the frozen suite exercises both production model-resolution lanes.

## Commands

Offline validation does not load an API key or call a provider:

```bash
uv run python -m benchmarks.suites.run_core_dogfood validate
uv run python -m benchmarks.suites.run_core_dogfood list
```

Real execution is serial and requires two explicit acknowledgements:

```bash
PULSARA_RUN_CORE_DOGFOOD=1 \
uv run python -m benchmarks.suites.run_core_dogfood run \
  --env-file .env \
  --confirm-network
```

Run one scenario while developing:

```bash
PULSARA_RUN_CORE_DOGFOOD=1 \
uv run python -m benchmarks.suites.run_core_dogfood run \
  --scenario workspace-patch \
  --env-file .env \
  --confirm-network
```

By default results are written beneath `/tmp`. Every completed scenario is
appended to `results.jsonl` immediately, followed by per-scenario JSON and a
suite `summary.json`/`summary.md`. Failed workspaces are retained; passed ones
are removed unless `--keep-workspaces` is supplied.

## Evidence boundary

The runner uses only public execution boundaries (`HostCore`, `HostSession`,
typed plan resolutions, and `InspectorService`). It does not synthesize Agent
events, mutate reducers, or read `session.wiring`.

The generic grader requires balanced run/model/tool/reservation/checkpoint/
generation lifecycles, finished durable runs, no Inspector error diagnostics,
scenario event minima, bounded calls/tokens, and a passing hidden verifier.
Scenario-specific gates additionally constrain tool use, compaction,
subagent delivery, plan interactions, and prompt-cache telemetry.

These dogfoods are provider-facing evidence, not deterministic correctness
proof. Normal offline tests remain the PR-level safety gate. Legacy real-provider
pytest cases have been removed: release-critical trajectories belong in a frozen
suite here, while narrow regressions belong in deterministic offline tests.
