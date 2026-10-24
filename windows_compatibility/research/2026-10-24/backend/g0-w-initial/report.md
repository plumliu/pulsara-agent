# G0-W-P backend evidence — 2026-10-24

**Partial G0-W-P evidence passed; the whole gate remains pending.** No Phase B production helper or second PG owner was implemented. Production code/spec/settings were not changed, no credentials/settings were read, no provider was called, and no commit was made by this agent.

## Environment and scope

- Windows repository: `C:\python_workspace\pulsara-agent`. Initial inventory commit was `3ac7fc2b5db8206c9f2ca2847d029e639a4c83ef`; root subsequently synchronized the tested Linux clone at `da397a2bb701c6371ed19155f66328334684bf38`.
- Every Linux invocation explicitly selected `wsl -d Ubuntu -u plumwsl`; UID/GID 1000, `/home/plumwsl`, Ubuntu 26.04.1 LTS, x86_64, kernel `6.18.33.2-microsoft-standard-WSL2`, glibc 2.43. Actual `/home/plumwsl` storage was ext4 on `/dev/sdd`.
- Linux repo: `/home/plumwsl/workspaces/pulsara_agent`. Root alone installed OS/tool dependencies, synchronized source, and built dist/resources. This agent staged only its output probes to the corresponding ext4 `output/windows-g0/backend/20261024-2323/`.
- Initial missing tools were cc/gcc/g++/make/bison/flex/pkg-config/patchelf, OpenBLAS and Linux mise/Node/uv/protoc. Before setup npm resolved to Windows `/mnt/c/Program Files/nodejs/npm`. The current recipe needs `ca-certificates curl git build-essential bison flex pkg-config binutils patchelf libopenblas0 xz-utils`. After root refreshed APT, Ubuntu 26.04 retained these package names: `libopenblas0`/`libopenblas0-pthread 0.3.32+ds-5`, `patchelf 0.18.0-1.4build1`. No readline/zlib/ICU development packages are needed by the recipe's explicit `--without-*` build.
- Tested real payload paths: `desktop/resources/postgres/bin/{initdb,pg_ctl,psql}`, `desktop/resources/python/bin/python3` (3.12.15), and Linux Node `/home/plumwsl/.local/share/mise/installs/node/22.23.3/bin/node`. Root separately owns the complete resource/Chromium/rg relocation smoke evidence.

## Passed production-owner PG scenarios

`pg-contract.json` has `passed: true`; `pg-command.json` records the complete Windows finite `wsl` argv and exit 0. `pg-contract.log` retains stdout/stderr. The probe imported current `desktop/dist/postgres.js` and `postgres-posix.js`, with `BundledPostgres` retaining lifecycle state.

1. Nonroot initdb used production `--auth-local=trust --auth-host=reject`, a new private `/tmp/g0p-ArbdKN` home and 0700 Unix socket. Real PG facts showed 17.9, the exact data/socket, empty `listen_addresses`, `pulsara_admin`, and the restricted `pulsara_runtime` role.
2. Production `prepare_database` installed clean-v0, was repeatable with `initialize=False`, and production `database_diagnostic(...deep=True)` returned verified. Installed extension versions were vector 0.8.1 and pg_search 0.26.1.
3. Real BM25 queries reused production lexical predicate/parameters/tokenizers. `数据库`, `running`, and `prepare_database` each returned the intended document and nonempty original UTF-8 snippet positions. Vector distance to the same `[1,2,3]` was 0.
4. Startup reused the exact running cluster with `startedHere=false`. Deliberately changed socket and raw PID directory spelling were rejected by reset; the same postmaster start time, query access and sentinel remained. Startup accepted the normalized `/.` directory while reset required the exact raw spelling.
5. A stopped directory marked PostgreSQL 16 was rejected while control inode/size and sentinel were retained; the probe restored only its own metadata edit.
6. Normal stop returned real numeric status 3; reopen reused existing data. Explicit reset removed only the verified data directory, retained socket/outside sentinel, then fresh init/start/stop succeeded and the exact home was removed. After data removal `pg_ctl status` correctly returned **4** (directory absent), distinct from **3** (existing stopped data).

### Readiness window preserved

An output-only adapter appended `-t 0` to a real `pg_ctl start -w` as a deliberate fault input, retaining all other production start arguments. PG returned code 1 and `server did not start in time`; `BundledPostgres.startedHere` remained false, while the immediately subsequent real status already reported running (PID 19887). The original error/stdout/stderr are preserved. After a 200ms observation delay, exact data/socket facts were checked and that exact server was stopped. This demonstrates an outcome-observation window for the fault input; it does not establish its frequency with the normal production wait. No fake readiness result or production fix was introduced.

The first PG run wrongly expected status 3 after successful removal. Its original failed harness output remains in `pg-contract.first-run.*`; `first-run-cleanup.json` proves the old home had no data directory or matching PostgreSQL process before its removal. The corrected run expects code 4 after removal and passes.

## Passed finite-helper subscenarios

`helper-harness.json` records `probeAssertionsPassed: true`, with all Windows `wsl` command/operation input, ready/result frames, stderr, exit codes and cleanup. `pg-helper.py` is exclusively an output probe: it reuses production `acquire_kernel_lock`, validates exact UID/mode/realpaths/version, obtains the existing `kernel.lock` with nonblocking flock, announces ready before reading a single operation frame, invokes real pg_ctl, waits/readbacks while still locked, and closes the lock. The fd is not inheritable; PG daemon did not retain it.

- Stdin EOF sent before a deliberate 200ms pre-lock scheduling delay produced no operation; status remained 3.
- Ready followed by EOF produced no operation. A concurrent helper returned typed busy/exit 73 rather than queuing a future command.
- After start acceptance, the Windows caller closed its stdout read end during a deliberate 500ms pre-command delay. A competing helper remained busy. The original helper completed real start/readback before releasing the lock; a subsequent helper could obtain the lock and observed real running PG (PID 573). The host's result at reply loss remains unknown; the probe did not replay start. A subsequent exact stop ended at status 3.

## Helper death remains unqualified

The final capture-pipe experiment deliberately paused a spawned physical-command `/bin/sh` **before exec of real pg_ctl**, SIGKILLed only exact helper PID 635, observed that another helper acquired the original lock and returned status 3, then SIGCONTed only child PID 636. The child was still T/stopped after helper death with `ShdPnd=1` (a pending SIGHUP); after continuation it disappeared, later status remained 3, and the carrier exited 9. **No late PG start was observed.** This is distinct from ordinary EOF/reply loss and does not cover death during every actual pg_ctl fork/start stage; `helperDeathMechanismPassed` remains null, not true or false.

`helper-harness.first-run.*` retains the first capture-pipe run whose harness incorrectly required a late start; `helper-harness.file-variant.*` retains a separate attempted variant that redirected physical-command output into its own regular file. That variant also observed later status 3 and failed the same expectation. Its additional regular-file output premise is not evidence about the normal capture-pipe mechanism. The final probe exposes that variation only through the explicit `--fault-command-output-file` switch; it is not used by the final harness. Neither earlier failed expectation is reported as a production mechanism failure.

## Cleanup and remaining scope

`cleanup-audit.json` exit 0 verifies that all five exact homes from these attempts no longer exist and that no PostgreSQL argv references their data directories: `/tmp/g0p-0hagLa`, `/tmp/g0p-ArbdKN`, `/tmp/g0h-3ffkFZ`, `/tmp/g0h-ak4iXn`, `/tmp/g0h-73Nv1B`. Own servers were stopped with exact verified data/socket facts and true pg_ctl; no shared/user PG was used. No `wsl --terminate`, `--shutdown`, `--unregister`, Windows data deletion, root initdb, global user configuration change or saved-settings edit occurred.

Remaining: helper death at additional real pg_ctl execution stages and corresponding safe maintenance admission, complete G0-W-C/D/F/N, and the separately specified native Ubuntu/GUI qualification. Full resource relocation is root's evidence, not inferred from this PG probe. These results do not activate Windows support or permit Phase B.
