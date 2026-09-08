# Pulsara repository agent instructions

This file applies to the entire repository. Human instructions and the active implementation specification for a task take precedence. Historical or superseded documents are context, not authority.

## 1. Preserve provider-input prefix continuity

- A new cold epoch and an explicitly adopted compaction successor are the only currently approved boundaries that may rebuild provider input roots.
- Within an existing epoch, keep `SYSTEM` and provider `tools` byte-identical and keep `messages` append-only by suffix.
- Runtime discovery, reconnects, capability changes, memory changes, permission changes, Skills, MCP, hooks, UI needs, or implementation convenience must not silently rebase or rewrite the installed prefix.
- If a future product needs another rebase boundary, stop and specify that product path explicitly before implementing it. Do not infer a new exception locally.

## 2. Keep durability deliberately small

- Prefer canonical relational rows for current semantic truth, selective committed occurrences for accepted user-visible transitions, typed process-local live observations for current execution, and disposable projections for derived data.
- Do not add a durable event, live event kind, subject slot, append guard, product relation, or durable job merely to prove that another mechanism worked.
- Do not introduce receipts, checkpoints, reducers, replay-based execution recovery, repair graphs, delivery acknowledgements, leases, generations, or projection authorities unless the active product specification demonstrates an independent necessity.
- Treat the current event/subject/guard/relation/job oracle as a strong subtraction guard. Keep every category unchanged where possible; any increase requires an explicit product reason, authority owner, transaction boundary, failure path, and specification revision.
- Process-local loss after crash is acceptable wherever the product contract is advisory or best-effort. Do not silently promote weak completeness into durable execution recovery.

## 3. Preserve long-horizon availability; do not invent total caps

- Do not add arbitrary total-history, total-task, total-graph, worker-lifetime, model-call-count, tool-call-count, turn-count, retry-count, or wall-clock lifetime caps.
- A concurrency limit is a physical scheduling bound, not permission to reject additional logical work; prefer queueing or pending state when the product contract allows it.
- Every new cap or hard bound must be justified in the active specification by a concrete product, protocol, safety, resource, or provider boundary. The implementation and tests must cite that reason.
- Prefer pagination, streaming, bounded per-operation admission, deterministic degradation, queueing, compaction, or typed resource-boundary outcomes over imposing a hidden lifetime limit.
- Preserve existing validated bounds unless the task explicitly changes them. Do not add “defensive” constants during coding or review without first closing their product semantics.
- Long-running tasks must remain operable across many turns, tools, terminal observations, compactions, queued subagents, and user messages within the explicitly specified physical boundaries.

## 4. Use fingerprints only at real boundaries

- Follow `PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md` for the authoritative subtraction contract.
- Do not record per-file code SHA, document SHA, activation-evidence SHA, or ordinary working-tree aggregate hashes. Git owns repository content identity; tests and dogfood prove behavior.
- Do not add a DTO fingerprint when the complete frozen value is already carried to the consumer. Use exact typed values, owner slots, object identity/equality, existing nonce/revision/generation, and database constraints.
- A hash must not stand in for owner authenticity, permission, inventory completeness, physical liveness, or execution authority.
- Retain a digest or MAC only when it crosses a real boundary: immutable content verification, durable/cross-restart semantic confirmation, provider prefix/source/replay compatibility, stable canonical identity derivation, or keyed opaque-token authentication.
- Do not create fingerprint registries, `fingerprint -> object` maps, proof graphs, compatibility dual fields, generic fingerprint base classes, or fallback paths for removed fingerprints.
- If an existing stable external or canonical ID depends on a removed DTO field, compute the same digest locally in the unique ID builder so observable output remains unchanged; do not preserve the redundant field.

## 5. Keep dogfood observable; do not over-redact

- Use the user's saved production configuration in the effective Pulsara home for real-provider dogfood and diagnostics. Read it through `LocalSettingsStore` and the existing `require_pulsara_home()` resolver: the default is `~/.pulsara/local-settings.yaml`, while `PULSARA_HOME` selects another home directory. Reuse the saved model connections, routes, credentials, and PostgreSQL configuration through the ordinary production owners; do not require `PULSARA_API_KEY`, source `.env` files, or add a legacy environment-credential fallback. If the required saved configuration is missing, report the missing setting rather than inventing a replacement.
- Treat saved production settings as read-only input. Do not overwrite them or export their secrets into the shell environment merely to run dogfood. When isolation is needed, load the saved settings before switching to a temporary Pulsara home and use the existing read-only settings injection with in-memory overrides, such as a verified disposable local database.
- Treat actual configured credential values as secrets, including model/service API keys, MCP secrets, OAuth access/refresh tokens, and OAuth client secrets. Never print them or copy them into reports, traces, fixtures, or committed files; do not dump the raw settings document. Redact the actual secret values wherever they occur in diagnostic output, not entire configuration objects or surrounding evidence.
- Do not broadly redact prompts, provider-visible messages, model replies, provider-returned response payloads, tool results, error bodies, traces, memory text, or other diagnostic context beyond actual credential values. Inspecting the actual prompt and actual model response is often the fastest way to diagnose a real-provider failure.
- PostgreSQL DSNs are intentionally inspectable in this repository and may be read or shown during diagnostics. Do not replace them with placeholders merely for presentation.
- The configured local PostgreSQL development/dogfood database is disposable and may be reset whenever a clean-v0, migration, dogfood, or debugging run needs it. Resolve and verify the exact local target before resetting it; this permission does not extend to an unverified or remote database.
- Evidence should remain useful rather than performative: retain the concrete inputs, outputs, and failure details needed to reproduce a problem, while excluding the actual credential values loaded from the saved configuration.

## 6. Hard-cut product and architecture changes during development

- This repository is still in active development. When an authorized change modifies product semantics, architecture, schema shape, provider contract, or an internal ownership boundary, implement it as one complete hard cut.
- Remove the superseded path in the same change. Do not add migration-period compatibility, legacy aliases, dual reads or writes, shadow state, old/new negotiation, deprecated wrappers, fallback branches, or transitional repair machinery unless the user or active specification explicitly requires compatibility with an external deployed state.
- Update the clean-v0 baseline, active specifications, tests, dogfood, and documentation to describe only the new truth. Historical documents may remain as history, but production code must not continue implementing their superseded contract.
- Prefer resetting the verified local disposable database and rebuilding canonical development state over creating an online migration chain solely for an unreleased internal design.
- A hard cut does not relax product correctness: preserve every behavior the active specification keeps, prove the new single path end to end, and delete compatibility code rather than hiding regressions behind it.

## 7. Reuse existing machinery; define ownership before implementation

- Do not reinvent functionality already provided by a suitable, maintained SDK, library, platform, or existing repository owner. This applies across Pulsara, not only to MCP or networking.
- Before implementing an integration, state the boundary: what the dependency owns, what Pulsara owns for its product semantics, and what minimal adaptation connects them. Inspect the actual dependency API and implementation before claiming a gap.
- Prefer supported configuration, injection points, and small adapters over copying parsers, protocol state machines, transport lifecycles, storage engines, or schedulers. A custom default or a security/resource requirement does not by itself justify replacing the mechanism that implements it.
- Custom machinery requires a concrete unmet requirement and evidence that the existing extension points cannot satisfy it. Limit the exception to that gap; do not grow a parallel framework. Record the reason beside the affected code or in the active specification, not in a new registry or approval system.
- Reuse does not authorize silently dropping product semantics, credential boundaries, resource protections, cancellation, or side-effect settlement. Verify those behaviors at the integration boundary. If they conflict with the dependency, identify the exact conflict and revise the boundary explicitly rather than hiding it behind fallback or generic hardening.
- When adopting a dependency-owned path, remove the superseded implementation in the same hard cut. Do not wrap a copied implementation and call it SDK reuse. Keep tests focused on Pulsara's observable contract and real integration gaps, rather than duplicating the dependency's entire conformance suite.

## Execution discipline

- Read the active specification and current production code before changing behavior. Do not treat historical implementation details as current authority.
- Keep product changes scoped. Do not use a requested feature or cleanup to add unrelated mechanisms, bounds, durability, or abstractions.
- Prefer the repository-root `.venv/` managed by `uv` for Python commands and tests. Use system Python only when no usable repository environment exists.
- Use focused tests while iterating and proportionate final verification. Run required real-provider or long-horizon dogfood when the active specification makes it part of activation.
- Never weaken assertions, add skips/xfails, or rewrite evidence merely to obtain a green result.
