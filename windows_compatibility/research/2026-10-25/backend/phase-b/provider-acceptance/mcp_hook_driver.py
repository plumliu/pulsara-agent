"""Finite real-provider MCP/hook data on the existing Round10 owner runner."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import shlex
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import run_terminal_wsl as adapter


def main():
    parser = adapter.parser()
    args = parser.parse_args()
    api = adapter.imports()
    from pulsara_agent.hooks.source import LocalHookSourceProvider
    from pulsara_agent.llm.input import PromptContent
    from pulsara_agent.primitives.permission import PermissionMode
    from pulsara_agent.windows_interop.scope import WindowsInteropScope
    from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient
    from pulsara_agent.settings import LocalPostgresConfig
    from tests.dogfood import run_round10_subagent_dogfood as runner
    from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
    import yaml

    original_terminal = api.terminal
    facade = SimpleNamespace(LocalSettingsStore=api.LocalSettingsStore,
        _create_database=api.shared._create_database, _drop_database=api.shared._drop_database,
        _find_connection=api.shared._find_connection, _ReadOnlySettingsStore=api.shared._ReadOnlySettingsStore,
        ModelRuntime=original_terminal.ModelRuntime, PromptContent=PromptContent,
        TemporaryDirectory=TemporaryDirectory, save=None)

    async def run(_arguments, report):
        saved = facade.LocalSettingsStore().read()
        name, _, admin, runtime = facade._create_database(saved)
        scopes, clients = [], []
        original_scope_init, original_client_init = WindowsInteropScope.__init__, BoundedMcpSdkClient.__init__

        def scope_init(owner, configuration):
            original_scope_init(owner, configuration)
            scopes.append(owner)

        def client_init(owner, *values, **keywords):
            original_client_init(owner, *values, **keywords)
            clients.append(owner)

        http = None
        try:
            with facade.TemporaryDirectory() as temporary:
                base = Path(temporary)
                workspace = base / "中文 空格工程"
                workspace.mkdir()
                report["workspace_utf8_bytes"] = len(str(workspace).encode())
                fixture = Path(__file__).parent / "mcp_execution_fixture.py"
                stdio_journal, http_journal, hook_journal = (base / name for name in ("stdio.jsonl", "http.jsonl", "hook.jsonl"))
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                http = await asyncio.create_subprocess_exec(sys.executable, str(fixture), "http", str(http_journal),
                    "--socket-fd", str(listener.fileno()), pass_fds=(listener.fileno(),),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                listener.close()
                ready = json.loads(await http.stdout.readline())
                assert ready == {"fixture_ready": True}
                report["http_fixture"] = {"pid": http.pid, "endpoint": f"http://127.0.0.1:{port}/mcp", "ready": True}
                configuration = {"servers": {}}
                for label, transport in {
                    "stdio": {"type": "stdio", "command": sys.executable, "args": [str(fixture), "stdio", str(stdio_journal)]},
                    "http": {"type": "streamable_http", "endpoint": f"http://127.0.0.1:{port}/mcp", "allow_http_localhost": True},
                }.items():
                    configuration["servers"][label] = {"enabled": True, "required": True,
                        "catalog_refresh_interval_ms": "DISABLED", "scope_policy": "ROOT_ONLY",
                        "exposure_policy": {"include_tool_names": ["execution_echo"], "invalid_tool_policy": "FAIL_SERVER"}, "transport": transport}
                (workspace / ".pulsara").mkdir()
                (workspace / ".pulsara/mcp.yaml").write_text(yaml.safe_dump(configuration), encoding="utf-8")
                hook = base / "hook.py"
                hook.write_text("import json,sys,subprocess,base64\nfrom pathlib import Path\np=json.load(sys.stdin)\nr=subprocess.run(['/mnt/c/Windows/System32/cmd.exe','/d','/c','echo','HOOK_PE_DONE'],capture_output=True,check=True)\nassert b'HOOK_PE_DONE' in r.stdout\nwith Path(sys.argv[1]).open('a') as f:f.write(json.dumps({'event':p.get('hook_event_name'),'tool':p.get('tool_name'),'windows_pe':r.stdout.decode('utf-8',errors='replace').strip(),'stdout_b64':base64.b64encode(r.stdout).decode('ascii'),'stderr_b64':base64.b64encode(r.stderr).decode('ascii')})+'\\n')\nprint('{}')\n", encoding="utf-8")

                def settings(value):
                    store = facade._ReadOnlySettingsStore(value)
                    from pulsara_agent.capability.pulsara_home import require_pulsara_home
                    home = require_pulsara_home()
                    command = shlex.quote(sys.executable) + " " + shlex.quote(str(hook)) + " " + shlex.quote(str(hook_journal))
                    (home / "hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "^terminal$", "hooks": [{"type": "command", "command": command}]}]}}))
                    provider = LocalHookSourceProvider(workspace_root=None, workspace_kind=None, workspace_state_key=None)
                    snapshot, = provider.discover().source_snapshots
                    provider.trust_store.trust(snapshot.provenance.trust_subject, expected_digest=snapshot.trust.current_definition_digest)
                    return store

                async def exercise(session, _workspace):
                    prompt = ("Use the two configured local MCP servers stdio and http. Call execution_echo on stdio with text STDIO_REAL_PROVIDER_2026, "
                              "then execution_echo on http with text HTTP_REAL_PROVIDER_2026, exactly once each. Discover their advertised exact names if needed. "
                              "Then call terminal once with command 'printf HOOK_MODEL_TASK' and tty=false; wait naturally if running. "
                              "Do not emulate MCP calls, run fixture scripts yourself, read/write journals, change capabilities or spawn agents. Report the two actual fixture results and terminal output.")
                    result = await session.run_turn(PromptContent.text(prompt), requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
                    report["actual_prompts"] = {"mcp_hook": prompt}
                    report["trace"] = await asyncio.to_thread(_tool_trace, session)
                    stdio_rows, http_rows, hook_rows = ([json.loads(line) for line in path.read_text().splitlines()] for path in (stdio_journal, http_journal, hook_journal))
                    report["fixture_calls"] = {"stdio": stdio_rows, "http": http_rows, "hooks": hook_rows}
                    assert len(stdio_rows) == len(http_rows) == len(hook_rows) == 1
                    assert stdio_rows[0]["text"] == "STDIO_REAL_PROVIDER_2026" and "MCP_STDIO_PE_DONE" in stdio_rows[0]["windows_pe"]
                    assert http_rows[0]["text"] == "HTTP_REAL_PROVIDER_2026"
                    assert "HOOK_PE_DONE" in hook_rows[0]["windows_pe"]
                    assert hook_rows[0]["event"] == "PostToolUse" and hook_rows[0]["tool"] == "terminal"
                    terminal_calls = [item for item in report["trace"] if item["tool_name"] == "terminal"]
                    assert len(terminal_calls) == 1 and terminal_calls[0]["arguments"]["command"] == "printf HOOK_MODEL_TASK"
                    assert terminal_calls[0]["arguments"].get("tty") is False
                    assert "HOOK_MODEL_TASK" in terminal_calls[0]["result"].get("output", "")
                    return {"passed": True, "reply": result.final_text, "model_calls": result.model_call_count, "tool_calls": result.tool_call_count}

                runtime_settings = replace(saved, postgres=LocalPostgresConfig(runtime, admin), mcp_credentials=(), mcp_oauth=())
                with ExitStack() as stack:
                    for owner, attribute, value in ((runner, "ModelRuntime", facade.ModelRuntime), (runner, "_ReadOnlySettingsStore", settings),
                        (runner, "_run_last_n_and_message", exercise), (WindowsInteropScope, "__init__", scope_init), (BoundedMcpSdkClient, "__init__", client_init)):
                        stack.enter_context(patch.object(owner, attribute, value))
                    result = await runner._run(runtime_settings, workspace, scenario="context", connection_id=args.connection_id, reasoning_effort=None)
                report["cases"].append(result)
                report["status"] = "completed" if result["status"] == "passed" else "failed"
                report["scopes"] = [{"joined": scope.physically_joined.is_set(), "retired": scope.directory is None,
                    "failure": scope.failure, "cancelled": scope.cancelled.is_set(),
                    "helper_exit": None if scope.helper is None else scope.helper.returncode,
                    "worker_alive": scope._thread is not None and scope._thread.is_alive(), "ready": scope.helper_ready} for scope in scopes]
                report["mcp_clients"] = [{"transport": type(client._transport).__name__, "owner_done": client._owner_task is None or client._owner_task.done()} for client in clients]
                assert scopes and all(row["joined"] and row["retired"] and not row["failure"] and not row["worker_alive"] for row in report["scopes"])
                assert len(clients) == 2 and all(row["owner_done"] for row in report["mcp_clients"])
                http.stdin.close()
                stdout, stderr = await http.communicate()
                report["http_fixture"].update(exit=http.returncode, stdout=stdout.decode(), stderr=stderr.decode())
                assert http.returncode == 0
                http = None
        finally:
            if http is not None and http.returncode is None:
                http.stdin.close()
                stdout, stderr = await http.communicate()
                report["http_fixture"].update(exit=http.returncode, stdout=stdout.decode(), stderr=stderr.decode())
            report["scopes_final"] = [{"joined": scope.physically_joined.is_set(), "retired": scope.directory is None,
                "failure": scope.failure, "cancelled": scope.cancelled.is_set(),
                "helper_exit": None if scope.helper is None else scope.helper.returncode,
                "worker_alive": scope._thread is not None and scope._thread.is_alive(), "ready": scope.helper_ready,
                "joined_result": scope._joined.result() if scope._joined is not None and scope._joined.done() and not scope._joined.cancelled() and scope._joined.exception() is None else None} for scope in scopes]
            report["mcp_clients_final"] = [{"transport": type(client._transport).__name__, "owner_done": client._owner_task is None or client._owner_task.done()} for client in clients]
            facade._drop_database(saved, name)

    facade.run = run
    api.terminal = facade
    if args.check_imports:
        print("Original runner/SDK/owners imported; no settings or provider calls")
        return 0
    return adapter.run(args, api)


if __name__ == "__main__":
    raise SystemExit(main())
