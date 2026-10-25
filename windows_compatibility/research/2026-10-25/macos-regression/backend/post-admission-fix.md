# Post-admission ROOT head-only race

The rebuilt macOS wheel reproduced `CANONICAL_PREFIX_CONFLICT` twice during the real context/mailbox scenario. The second run observed only `provider_input_through_sequence` 10→11 and its existing derived fingerprint fields changing. Canonical items, closures, late outcomes, context binding, permission, workflow facts, freshness/outcome facts, and replay manifests stayed exact. ROOT acceptance already succeeded before the concurrent child admission advanced the session-wide head.

The correction belongs to `ProviderDispatchCoordinator.activate_prospective_root_input`, before any provider open or installed input. It must not reissue ROOT acceptance or create another command/turn.

Exact admission predicate:

1. Current turn model binding equals the immutable prospective candidate binding.
2. Actual canonical head strictly advances the prepared head.
3. Normalize only actual identity and replay-manifest through-sequence back to the prepared head, carrying all other actual fields through `dataclasses.replace`. Existing DTO constructors verify the prepared identity/snapshot/compile/manifest/composite fingerprints against those complete normalized values. The final complete dispatch DTO must equal the prepared dispatch DTO.
4. A constructor mismatch, head equality/backward movement, or any remaining field difference rejects this path with the existing prefix-conflict error. Payload, closure, permission, replay, binding and identity drift remain failures.

On that one predicate, close the exact safe-point handle, close the dry tool-surface borrow, retire any hook reservation, abort the exact continuity planning token, and return the existing `None` outcome into `run_accepted_turn`'s ordinary prepare loop. That loop uses the same already accepted turn and existing continuity owner. It neither rebuilds installed SYSTEM/tools/messages nor reuses the stale wire measurement. Physical cleanup failure must propagate; no success/fallback or duplicate close may hide it. Existing pending-steer/completion retirement should share the same explicit resource accounting.

Counterexample: use a real private migrated PG database and existing active-subagent fixture. After a second ROOT's original repository acceptance returns, admit the child's real turn through `accept_subagent_turn`, then let original activation freeze the current head. Assert one second ROOT acceptance, one child admission, same ROOT turn, exact installed prefix, and all dry resources retired. Keep the pre-fix failure log. Negative cases exercise equal/backward head, changed binding/payload/closure/permission/replay, plus cleanup failure, without altering repository authority.

Separate test-fixture correction: initialize `_history_maintenance=None` in the existing `object.__new__(KernelHostCore)` blob-GC fixture, matching real `__init__`. Before correction, shutdown raised AttributeError before cancelling the intentionally blocked GC, causing the test's cleanup to wait forever. Original interruption log and OS sample are retained; no production shutdown behavior or assertions change.
