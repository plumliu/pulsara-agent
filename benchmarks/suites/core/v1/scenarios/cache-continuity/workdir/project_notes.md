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
The durable record remains the ordered append log and its terminal settlement facts.

## Architecture record C

Recovery begins from the latest confirmed checkpoint and replays a bounded suffix. A missing
checkpoint is a recoverable acceleration miss; a conflicting event identity is an authority
failure. No process-local cache may promote itself into canonical state.

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
Field note 02: cancellation does not erase a physical FULL commit.
Field note 03: UNKNOWN outcomes retain their recovery owner.
Field note 04: terminal projection precedes transcript acceptance.
Field note 05: accepted control disposition authorizes continuation.
Field note 06: exact replay consumes durable references rather than raw guesses.
Field note 07: source attribution is distinct from provider-visible semantics.
Field note 08: runtime observations use a typed user-wire carrier.
Field note 09: human input uses a separate typed user-wire carrier.
Field note 10: long-horizon rewriting requires explicit durable authority.
Field note 11: compaction does not silently delete current-run facts.
Field note 12: tool call and result pairing is deterministic and ordered.
Field note 13: physical reservations include bounded recovery tails.
Field note 14: checkpoint barriers drain admitted producers before freezing.
Field note 15: Inspector reports durable state and never invents resident cache state.
Field note 16: prompt cache hits reduce provider prefill but do not change semantics.
Field note 17: a cache miss is observable, not a correctness failure by itself.
Field note 18: this specific dogfood requires a positive cache hit as a performance gate.
Field note 19: tool output is sanitized and bounded before durable projection.
Field note 20: all close paths drain owned physical operations.
Field note 21: every run has exactly one durable start and terminal outcome.
Field note 22: every model call has a terminal projection and usage status.
Field note 23: every production tool result has a terminal projection reference.
Field note 24: stable fingerprints are computed from canonical payloads.
Field note 25: artifact locators remain physical attribution, not semantic identity.
Field note 26: generated benchmark workspaces are isolated and disposable.
Field note 27: hidden verifiers are never copied into the model workspace.
Field note 28: scenario contracts and fixture bytes are content-addressed.
Field note 29: provider-backed suites run serially to keep evidence attributable.
Field note 30: release evidence records code, scenario, and runner fingerprints.
