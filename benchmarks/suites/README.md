# Pulsara Kernel real-LLM dogfood

`benchmarks/suites` contains the small, frozen set of expensive provider-facing
trajectories used to exercise the active conversation Kernel. It is separate
from `evals/`: deterministic model-quality datasets belong there, while this
suite proves that a real provider can traverse Pulsara's current public product
boundaries.

The logical suite is `pulsara-kernel-dogfood-v2`. Its files remain physically
under `core/v1/` because that directory also holds immutable Stage 0–5
activation evidence; the manifest and every emitted result carry the v2
identity explicitly.

## Frozen scenarios

| Scenario | Current product boundary |
|---|---|
| `cache-continuity` | three canonical turns plus provider-reported process-local cache usage |
| `canonical-resume` | detach, new Host writer generation, and reopen from canonical rows |
| `long-context-continuity` | lossless canonical transcript continuity without compaction or memory |
| `prompt-queue-fifo` | durable ingress FIFO and two canonical completed turns |
| `subagent-delegation` | Host-scoped child task, canonical child result, and parent acceptance |
| `workspace-patch` | ordinary Kernel tool loop and hidden behavioral verification |

Each task receives a fresh workspace. The model cannot see the post-run
verifier. Scenario contracts and fixture/verifier bytes are content-addressed
by `manifest.json`, so changing a task creates an explicit evidence change.

Five trajectories use the `flash` role; `workspace-patch` uses `pro`. Execution
is serial so scenarios do not compete for provider or database capacity.

## Commands

Offline validation neither loads an API key nor calls a provider:

```bash
uv run python -m benchmarks.suites.run_core_dogfood validate
uv run python -m benchmarks.suites.run_core_dogfood list
```

The full real-provider run requires two explicit acknowledgements:

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

Results go beneath `/tmp` unless `--results-dir` is supplied. Every completed
scenario is appended immediately to `results.jsonl`, with one JSON result per
scenario and final `summary.json`/`summary.md`. Failed workspaces are retained;
passed workspaces are removed unless `--keep-workspaces` is set.

## Evidence boundary

The runner uses `KernelHostCore`/`KernelHostSession`, the bounded canonical
query port, and a first-party operational hook for provider usage. It does not
write evidence rows, synthesize committed events, inspect private session
wiring, or replay execution.

Provider usage—including cached input tokens—is an operational observation:
it is process-local, bounded, failure-isolated, and absent from the selective
durable journal. Canonical rows prove accepted conversation/tool/subagent/queue
state; selective events prove accepted occurrences. The hidden verifier proves
workspace behavior.

This suite is release evidence, not a deterministic correctness proof. Normal
offline tests remain the PR-level gate. It intentionally has no projection,
universal EventLog, segment-recovery, manual-compaction, or Oxigraph path.
