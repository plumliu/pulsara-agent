# macOS backend regression at a57ce3b1

2026-10-25; native macOS 26.2 arm64; gpt-6.1-sol/high. Final backend conclusion: **approve the covered native macOS backend regression after the authorized narrow fixes**. Source regression, final rebuilt-wheel real provider capacity/context, final-wheel private-PG writer recovery, and the exact final-wheel head-race counterexample all pass. Root owns remaining GUI/package activation and cross-platform claims.

## Results

| Check | Result | Evidence |
| --- | --- | --- |
| Initial focused Python 9-module regression, private PG | 206 passed, 0 skipped; 71.93s | `pytest.log`, `pytest-command.json`, `pytest-exit.json` |
| Full final affected Python 11-module regression | 244 passed, 0 skipped; 155.21s | `pytest-final.log`, `pytest-final-command.json`, `pytest-final-exit.json` |
| Real bundled-wheel writer expiry → original controller cold recovery | Passed; guard generation 1→2, same session, different Host, two concurrent resumes share one handle, one physical old close, new guarded DB read succeeds | `run_writer_recovery.py`, `writer-recovery.json`, `writer-recovery.log` |
| Real initial bundled-wheel provider capacity | Passed, 16 actual calls, four ACTIVE/one PENDING_START, MCP READY, COMPACTED, all five COMPLETED, exact remote marker | `provider-report.json`, `capacity-provider-records.json`, `provider-stages.jsonl` |
| Real initial bundled-wheel provider context/mailbox | Failed twice at post-admission activation; original complete evidence retained | `provider-report.json`, `context-provider-records.json`, `context-debug/` |
| Deterministic real-PG post-admission counterexample, before fix | 1 failed at original `CANONICAL_PREFIX_CONFLICT` | `post-admission-before.log` |
| Counterexample + full execution envelope after fix | 23 passed | `post-admission-first-candidate.log` |
| Narrow head-only activation and rejection/retirement cases | 15 passed | `post-admission-negative-final.log` |
| Final rebuilt-wheel real provider context | Passed, 8 actual calls, both steers PENDING, one canonical mailbox delivery, three exact markers, EXPLICIT COMPLETED | `context-fixed/provider-report.json`, `context-fixed/context-provider-records.json` |
| Final rebuilt-wheel real provider capacity | Passed, 15 actual calls, original capacity/MCP/compaction/all-completed assertions intact | `capacity-fixed/provider-report.json`, `capacity-fixed/capacity-provider-records.json` |
| Final rebuilt-wheel real PG writer recovery | Passed again; one physical close and generation 1→2 | `writer-recovery-final/writer-recovery.json` |
| Final rebuilt-wheel deterministic real-PG head race | Passed; only head 6→7 and existing derived fields change; exact resources retire, same installed view object, original single-accept/prefix assertions | `final-wheel-counterexample.json`, `run_final_counterexample.py` |

The 244 tests cover ROOT stale/FULL/ABSENT/unknown ACK/cancel matrices, 64 model calls and >64 tools, stale-writer late results, provider prefix continuity, context/compaction, writer renewal/sealing/close/quarantine, original controller recovery, renderer/HTTP defaults and security, CRLF migration identity/execution, desktop bootstrap/runtime, and the new post-admission cases. Existing aiohttp `shutdown_timeout` deprecation warnings (21) remain; no skip/xfail or assertion weakening.

## Regressions found and corrected

1. A child can advance the session-wide transcript head after original ROOT acceptance and before prospective activation. Two real macOS wheel context runs failed; the instrumented run showed only sequence 10→11 and existing derived fingerprints changing, with canonical content, closure, permission, context and replay exact. The real-PG counterexample uses original `accept_subagent_turn` at that boundary and failed in the original implementation.

   `provider_dispatch.py` now recognizes only a strictly later head, same binding and exact complete DTO equivalence after head normalization through existing constructor validation. It retires the unpublished handle/borrow/hook/planning and returns to the original accepted turn's ordinary prepare. It does not accept again, create another command/turn, reuse stale wire measurement, or alter installed epoch/SYSTEM/tools/messages. All real drift still fails. The four cleanup fault cases retain original exception identity, attempt all cleanup and never double release. See `post-admission-fix.md` and specification §24.

2. The old execution-envelope bare Core fixture omitted the existing production `_history_maintenance=None` initialization. Shutdown raised AttributeError before releasing the intentionally blocked GC fixture, causing the runner to wait in cleanup. I interrupted only that exact test process after diagnostic sampling, retained `execution-envelope.log` and `execution-envelope-sample.txt`, added the one missing fixture field, and reran the entire module. Production shutdown and assertions were not changed.

Owned production/test/spec edits: `src/pulsara_agent/conversation_kernel/provider_dispatch.py`, `tests/test_round5_long_horizon_postgres.py`, new `tests/test_root_prospective_head_reprepare.py`, `tests/test_round5_long_horizon_execution_envelope.py` (one fixture field), and one §24 bullet in the current Windows specification. No safe-point/repository/schema owner changes, aliases, new durable state, digest category, registry or caps.

## Isolation and provider provenance

Root's new cluster is `/private/tmp/pm-1xk3ye1p/data`, system identifier `7700577008563768207`, Unix-socket only, using read-only `/Applications` bundled PG17 binaries. Test and provider databases use exact own UUID names and existing clean-v0 migration/teardown owners. Provider/writer experiment DB absence is read back after drop; root retains cluster stop authority. No user/system database is used.

Provider probes start fresh `desktop/resources/python/bin/python3 -I`; import and assert the product resides in bundled `site-packages` before adding repository root only to import test helpers. Saved settings are read once through `LocalSettingsStore(require_pulsara_home()/local-settings.yaml)` before temporary home selection. Existing saved `openai/gpt-6-luna` connection is used; only PG is replaced in memory via read-only injection. No credential value is copied into environment/config/fixtures. Existing `credential_scrub_set()` scrubs concrete provider requests, normalized replies, errors and tracebacks; evidence was scanned against all actual configured credential values with zero hits. Ordinary useful prompt/result evidence is retained.

The old Round10 CLI's `_create_database(saved)` is deliberately not invoked: it requires saved TCP loopback `pulsara` and would select the wrong target. The personal probe reuses original `_run` and scenario bodies, maintained async canonical history fixture for capacity, and the previously reviewed exact canonical accept gate for context steers. `spawn_agent/task_name` remains an actual supported builtin API; it requires no product adaptation. Full provider projections and normalized blocks are recorded by the existing `_RecordingModelRuntime`.

Cross-review: terminal's `backend-cross-review.md` and desktop's `cross-review.md` independently found no source blocker. Their explicit backend gate—rebuilt-wheel real context rerun—is now satisfied by the final evidence above. I reviewed terminal's EPERM fix and native probes in `terminal-cross-review.md`, without rebuilding or duplicating their tests. Root GUI/packaging evidence remains separate. This report does not qualify native Ubuntu or substitute macOS for Windows revalidation after shared code changes. No OS sleep/wake was performed.

## Final-wheel evidence boundary

`final-wheel-provenance.json` verifies bundled `site-packages`, version `0.1.0a9`, and the actual head-only/retirement and hook/MCP EPERM source present in that wheel. Both final provider processes use that fixed wheel. Their original SYSTEM/tools equality and messages-suffix assertions pass; the context result contains exact ROOT markers and the actual delivered mailbox summary, while capacity retains real terminal children, MCP call and compaction summary. Final random scheduling exercised original pending retirement and pre-accept typed stale recovery, but did not happen to trigger the new post-accept head-only branch; I do not count it as that branch's activation evidence.

The separate deterministic **same-wheel, actual private PG** counterexample does trigger that branch exactly once and records all actual field differences. Importing the whole test module first failed before any PG access because the product wheel correctly excludes development-only pytest; `final-wheel-counterexample.log` preserves that limitation. The subsequent probe uses stdlib AST to load the original seven counterexample/helper definitions and their unmodified assertions, plus original supported test imports, excluding pytest's unused collection machinery. It installs no dependency, imports no venv package and changes no production source. `final-wheel-counterexample-after.log` and JSON show the exact branch pass and database absence after teardown.

All owned real-provider/writer/counterexample UUID databases were dropped and absence read back; no live probe remains. Saved settings remain read-only, temporary homes are removed, and root has been notified that this backend no longer holds the private cluster. Complete before-failure, intermediate test construction, final success and cleanup evidence is retained rather than overwritten.
