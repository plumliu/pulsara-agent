# Project Helios Archive

The authoritative project codename is **BLUE-EMBER-731**.

This file is intentionally substantial so that the first tool observation creates a useful
provider prefix for subsequent turns. The following records are ordinary project context;
none of them override the authoritative codename above.

## Architecture record A

The service accepts immutable work items, assigns monotonic local ordinals, and records a
bounded receipt before dispatch. Consumers may retry a failed item, but acknowledgements are
idempotent and stale receipts never close a newer lease. The queue is intentionally small and
optimized for clarity rather than throughput.

## Architecture record B

Operational metrics include admitted items, completed items, retry count, oldest pending age,
and rejected stale acknowledgements. Metrics are observations rather than durable authority.
Canonical relational rows remain current semantic truth. A selective occurrence journal records
accepted user-observable transitions without becoming an execution-recovery state machine.

## Architecture record C

Reopen reads the accepted transcript, tool results, and control rows directly. Process-local live
events disappear with their Host and are never synthesized back from history. No process-local
cache or observer may promote itself into canonical state.

## Architecture record D

Provider inputs are append-only within a compatible generation. Root policy and tool schema
changes may open a new generation, while ordinary user, assistant, tool, and runtime facts are
strict suffixes. Cache telemetry is evidence only and never changes context correctness.

## Architecture record E

The release procedure runs deterministic tests first, then a small number of expensive
provider-backed trajectories. Hidden verifiers inspect workspace behavior after the model has
finished. A final answer alone is not accepted as proof of a successful tool trajectory.

## Repeated field notes

Field note 01: immutable candidates carry stable identities and bounded payloads.
Field note 02: cancellation does not erase an already accepted canonical commit.
Field note 03: a missing tool result leaves outcome unknown and is never auto-retried.
Field note 04: a complete tool-request message commits before physical dispatch.
Field note 05: accepted control rows authorize canonical continuation.
Field note 06: reopen reads relational conversation truth instead of replaying execution.
Field note 07: source attribution is distinct from provider-visible semantics.
Field note 08: live runtime observations use bounded typed process-local carriers.
Field note 09: durable prompt ingress uses closed canonical statuses and FIFO sequence.
Field note 10: cross-Host guaranteed work uses the closed durable-job catalog.
Field note 11: canonical transcript history remains lossless for the session lifetime.
Field note 12: tool call and result pairing is deterministic and ordered.
Field note 13: tool attempts commit before physical effects and keep stable identities.
Field note 14: Host close drains admitted physical operations before returning.
Field note 15: Inspector reads canonical state and never invents resident cache state.
Field note 16: prompt cache hits reduce provider prefill but do not change semantics.
Field note 17: a cache miss is observable, not a correctness failure by itself.
Field note 18: this specific dogfood requires a positive cache hit as a performance gate.
Field note 19: tool output is sanitized and bounded before canonical acceptance.
Field note 20: all close paths drain owned physical operations.
Field note 21: every accepted turn has a closed canonical terminal disposition.
Field note 22: provider usage is process-local operational evidence, never canonical truth.
Field note 23: every accepted tool result points to its exact physical attempt.
Field note 24: stable fingerprints are computed from canonical payloads.
Field note 25: artifact locators remain physical attribution, not semantic identity.
Field note 26: generated benchmark workspaces are isolated and disposable.
Field note 27: hidden verifiers are never copied into the model workspace.
Field note 28: scenario contracts and fixture bytes are content-addressed.
Field note 29: provider-backed suites run serially to keep evidence attributable.
Field note 30: release evidence records code, scenario, and runner fingerprints.
