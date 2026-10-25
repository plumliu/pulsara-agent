# Native POSIX scope fix cross-review

2026-10-25. Read-only review of terminal agent's final hook/MCP changes and actual evidence; no duplicate build or test run.

Conclusion: no source/evidence blocker in this change. Final bundled-wheel provider reruns remain a separate activation gate.

- `hooks/executor.py::_process_group_live` treats `PermissionError` as live. `_signal_process_group` catches signal denial and leaves the existing join owner active. `conversation_kernel/mcp/sdk_facade.py::_terminate_stdio_group` does the same for liveness and TERM/KILL, including stderr-reader failure cleanup. This matches existing `terminal_process/manager.py` and `environment.py`: only ESRCH establishes group absence, EPERM cannot prove physical retirement.
- The change preserves the original process group, original child wait, readers, worker tasks, repeated-close/cancel owner and permits. It does not infer completion from root exit, replace the SDK stdio transport, release capacity early, introduce a new process registry or add a total task lifetime cap. The existing two-second TERM→KILL escalation remains the same finite physical action boundary.
- Windows interop has a separate scope branch and is not redirected through this POSIX predicate.
- New tests use real unreaped zombie groups, actual `killpg(0)`/TERM/KILL behavior, a natural forked tail after leader exit and original HookCommandExecutor settlement. The before evidence is 2 failed/1 passed; the after focused suite is 68 passed including original hooks/MCP/interop tests. Actual macOS probe repeats 30 natural-tail runs (60 records), plus exact MCP repeated close/cancel, hook natural tail and hook cancellation, with final group absence, worker completion and permit restoration. Initial failed attempts remain evidence rather than being overwritten.

Checked artifacts: terminal `zombie-before.log`, `zombie-after-focused.log`, `hook-focused-after.json`, `real-scope-after.json`, and corresponding source/new `tests/test_posix_process_group_settlement.py`. I did not count `hook-focused-results.json` or earlier `real-scope-results.json` as the final rerun.

Desktop cross-topic evidence was also read: full frontend 1372 tests, typecheck/lint and native startup-cover hidden-path correction remain separately owned. Their report distinguishes jsdom synthetic IME/Meta events from physical GUI evidence and preserves original visible-path timing assertions; no backend authority/prefix change is implied.
