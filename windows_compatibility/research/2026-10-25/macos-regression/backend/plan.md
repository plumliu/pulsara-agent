# macOS a57ce3b1 backend regression plan

Scope: native macOS shared behavior at a57ce3b1; Windows evidence is context, not a substitute for this run. No OS sleep/wake, production settings edits, user database access, build, or dependency synchronization in this task.

## Static review completed

- Read repository AGENTS and current Windows implementation specification §24.
- Read Windows shared-owner fix/cross-review, writer-loss report, and root Phase B real-provider report.
- Compared typed stale ROOT admission, confirmation/reprepare loop, writer fencing/renewal/close callback, original controller resume, renderer bridge/HTTP defaults, migration resource normalization, and desktop runtime source/tests.

## Execution order

1. After root environment readiness, run focused ROOT/prefix/compaction tests and writer/controller/renderer/HTTP/migration/runtime tests using repository `.venv/bin/python -m pytest`. `CI=true` makes absent private PG prerequisites fail rather than exercise the existing local fixture skip.
2. Inject only root-created private Unix-socket PG admin/runtime DSNs into the two existing test PG variables. Existing test fixtures create UUID databases and drop their own exact names. Do not invoke the dogfood CLI's saved-PG create helper: it requires saved TCP loopback database `pulsara`, incompatible with this isolated Unix-socket cluster.
3. Run full Round5 PostgreSQL long-horizon module (64 calls, >64 tools, ROOT stale/ACK/cancel matrices, late stale-writer results) and deterministic Round5b context/compaction module. Preserve exact assertions and report failures before touching shared production files.
4. Read saved settings only through `LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()`. Inspect only nonsecret connection ID/model/reasoning metadata. In a personal harness, load that snapshot before switching to an isolated temporary home; replace only `postgres` in memory with a newly created private migrated database. Use existing `_ReadOnlySettingsStore` and Round10 `_run(..., scenario="capacity"/"context")` rather than copying orchestration logic.
5. Preserve concrete scenario outputs and scrub actual configured credential values using existing scrub machinery; validate emitted JSON contains no such value. Never print settings documents, credentials, or credential-bearing environment assignments. Drop exact own UUID dogfood DBs in `finally`, restore temporary home env, leave cluster lifecycle to root.

## Suites and evidence

- `tests/test_round5_long_horizon_postgres.py`
- `tests/test_round5b_long_horizon_context_compaction.py`
- `tests/test_round3_1_provider_input_prefix_continuity.py`
- `tests/test_round5_long_horizon_execution_envelope.py` (coordinate terminal physical tests to avoid duplicate ownership)
- `tests/test_host_writer_availability.py`
- `tests/test_desktop_renderer_bridge.py`
- `tests/test_desktop_http_surface.py`
- `tests/test_migration_resource_portability.py`
- `tests/test_desktop_runtime.py`
- New WSL-profile Python tests only as shared-default/security regression, not as native ext4/WSL qualification.

Logs and final report stay in this directory. Root owns unified environment/build and private cluster stop. macOS evidence does not qualify Ubuntu or Windows; no skip/xfail/assertion weakening.
