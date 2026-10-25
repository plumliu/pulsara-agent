"""Output-only adapter for the existing terminal dogfood; no provider on help/imports.

Run with the installed Linux Python and current repository tests on sys.path.
The caller supplies a previously approved disposable Unix-socket PostgreSQL.
This adapter never starts/stops/resets that cluster or writes saved settings.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
from tempfile import mkdtemp
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-imports", action="store_true")
    mode.add_argument("--execute", action="store_true", help="Actually call the saved provider")
    result.add_argument("--saved-home", type=Path)
    result.add_argument("--connection-id")
    result.add_argument("--model")
    result.add_argument("--distribution")
    result.add_argument("--user")
    result.add_argument("--expected-uid", type=int)
    result.add_argument("--interop-helper", type=Path)
    result.add_argument("--socket", type=Path)
    result.add_argument("--port", type=int)
    result.add_argument("--admin-role")
    result.add_argument("--runtime-role")
    result.add_argument("--root-database")
    result.add_argument("--expected-data", type=Path)
    result.add_argument("--expected-system-id")
    result.add_argument("--scratch-root", type=Path)
    result.add_argument("--cases", default="directories,interaction,pty,long,truncation,monitor_race,monitor,monitor_long_wake,capacity")
    result.add_argument("--input-closure", action="store_true", help="Adapt interaction/pty test data to explicit close_stdin contracts")
    result.add_argument("--output", type=Path)
    return result


def imports():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from pulsara_agent.desktop_app.linux_storage import require_native_ext4
    from pulsara_agent.settings import LocalSettingsStore
    from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
    from pulsara_agent.windows_interop import configure_interop
    from tests.dogfood import run_terminal_cognitive_dogfood as terminal
    from tests.dogfood import run_model_switch_handover_dogfood as shared
    return SimpleNamespace(psycopg=psycopg, sql=sql, make_conninfo=make_conninfo,
                           require_native_ext4=require_native_ext4, LocalSettingsStore=LocalSettingsStore,
                           PostgresMigrationRunner=PostgresMigrationRunner, configure_interop=configure_interop,
                           terminal=terminal, shared=shared)


def owned_directory(path: Path, uid: int, native) -> Path:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("a canonical existing absolute directory is required")
    facts = path.stat()
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != uid:
        raise ValueError("directory does not belong to the approved Linux user")
    native(path)
    return path


def verify_input_closure(report):
    offset = 0
    checks = []
    for case in report["cases"]:
        count = case.get("tool_call_count")
        if count is None:
            continue
        trace = report["trace"][offset:offset + count]
        offset += count
        starts = [item for item in trace if item["tool_name"] == "terminal"]
        assert len(starts) == 1, "input closure must not restart the program"
        expected_tty = case["case"] == "pty"
        assert starts[0]["arguments"].get("tty") is expected_tty
        process_id = starts[0]["result"]["process_id"]
        closes = [item for item in trace if item["tool_name"] == "terminal_process" and item["arguments"].get("action") == "close_stdin"]
        assert len(closes) == 1 and closes[0]["arguments"]["process_id"] == process_id
        assert all(item["result_state"] == "SUCCESS" for item in trace)
        assert not any(item["arguments"].get("action") == "kill" for item in trace)
        assert any("EOF_SEEN" in item["result"].get("output", "") and item["result"].get("status") == "success"
                   and item["result"].get("exit_code") == 0 for item in trace)
        checks.append({"case": case["case"], "tty": expected_tty, "process_id": process_id, "closed_once": True, "eof_exit0": True})
    report["input_closure_checks"] = checks


def run(args, api) -> int:
    if sys.platform != "linux" or os.getuid() != args.expected_uid or os.getuid() == 0:
        raise ValueError("execute only as the approved non-root Linux user")
    if not 1 <= args.port <= 65535:
        raise ValueError("invalid PostgreSQL port")
    native = api.require_native_ext4
    for path in (args.saved_home, args.socket, args.expected_data, args.scratch_root):
        owned_directory(path, args.expected_uid, native)
    if args.scratch_root == args.saved_home or args.scratch_root.is_relative_to(args.saved_home):
        raise ValueError("scratch root must be outside the saved application home")
    socket_file = args.socket / f".s.PGSQL.{args.port}"
    socket_facts = socket_file.lstat()
    if not stat.S_ISSOCK(socket_facts.st_mode) or socket_facts.st_uid != args.expected_uid:
        raise ValueError("expected PostgreSQL Unix socket is not owned by the approved user")
    if (args.expected_data / "PG_VERSION").read_text().strip() != "17":
        raise ValueError("expected PostgreSQL17 data directory")
    # Credentials are read once, before the existing runner changes PULSARA_HOME.
    saved = api.LocalSettingsStore(args.saved_home / "local-settings.yaml").read()
    matches = [item for item in saved.model_connections if item.id.value == args.connection_id]
    if len(matches) != 1 or matches[0].target.model_id != args.model:
        raise ValueError("saved connection ID and model must identify the same configured connection")
    selected = api.shared._find_connection(replace(saved, model_connections=tuple(matches)), args.model)
    scrub = saved.credential_scrub_set()
    api.configure_interop(helper_path=str(args.interop_helper), distribution=args.distribution, user=args.user)
    admin_root = api.make_conninfo(host=str(args.socket), port=args.port, user=args.admin_role, dbname=args.root_database)
    runtime_root = api.make_conninfo(host=str(args.socket), port=args.port, user=args.runtime_role, dbname=args.root_database)
    created: dict[str, str] = {}
    calls: list[dict] = []
    report = {"phase": "candidate-wsl", "model": args.model, "connection_id": args.connection_id,
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "cases": [], "trace": [],
              "provider_calls": calls, "scope": "original owner runner; not installed Electron GUI qualification"}

    def inspect_target():
        with api.psycopg.connect(admin_root, autocommit=True) as connection:
            data, socket, listen, version, system_id, address, role, start = connection.execute(
                "SELECT current_setting('data_directory'), current_setting('unix_socket_directories'), "
                "current_setting('listen_addresses'), current_setting('server_version_num')::int, "
                "system_identifier::text, inet_server_addr(), current_user, pg_postmaster_start_time() "
                "FROM pg_control_system()"
            ).fetchone()
        if (data != str(args.expected_data) or socket != str(args.socket) or listen != "" or address is not None
                or version != 170009 or system_id != args.expected_system_id or role != args.admin_role):
            raise ValueError("current PostgreSQL facts differ from the approved disposable target")
        with api.psycopg.connect(runtime_root) as connection:
            runtime_role, runtime_address, runtime_start = connection.execute(
                "SELECT current_user, inet_server_addr(), pg_postmaster_start_time()"
            ).fetchone()
        if runtime_role != args.runtime_role or runtime_address is not None or runtime_start != start:
            raise ValueError("runtime role does not reach the same local PostgreSQL instance")
        report["postgres"] = {"system_identifier": system_id, "data": data, "socket": socket,
                              "version_num": version, "admin_root": admin_root, "runtime_root": runtime_root}

    def drop_database(_settings, name):
        if name not in created:
            raise ValueError("refuse to drop a database not created by this invocation")
        inspect_target()
        with api.psycopg.connect(admin_root, autocommit=True) as connection:
            connection.execute(api.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(api.sql.Identifier(name)))
        del created[name]
        report.setdefault("dropped_databases", []).append(name)

    def create_database(_settings):
        inspect_target()
        name = f"pulsara_wsl_dogfood_{uuid4().hex}"
        admin = api.make_conninfo(host=str(args.socket), port=args.port, user=args.admin_role, dbname=name)
        runtime = api.make_conninfo(host=str(args.socket), port=args.port, user=args.runtime_role, dbname=name)
        with api.psycopg.connect(admin_root, autocommit=True) as connection:
            connection.execute(api.sql.SQL("CREATE DATABASE {}").format(api.sql.Identifier(name)))
        created[name] = admin
        try:
            migration = api.PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime)
            first = migration.migrate(deadline_monotonic=monotonic() + 240)
            second = migration.migrate(deadline_monotonic=monotonic() + 240)
            if first.applied_versions != (0,) or second.applied_versions != ():
                raise RuntimeError("disposable clean-v0 was not repeatable")
        except BaseException:
            drop_database(None, name)
            raise
        report["created_database"] = name
        return name, admin_root, admin, runtime

    def save(path, value, _saved):
        if args.input_closure and value["status"] != "failed":
            verify_input_closure(value)
        encoded = json.dumps(scrub.scrub_json(value), ensure_ascii=False, indent=2)
        if scrub.contains(encoded):
            raise ValueError("credential scrub postcondition failed")
        path.write_text(encoded + "\n", encoding="utf-8")

    def recording_runtime(**kwargs):
        return api.shared._RecordingModelRuntime(original_runtime.production(**kwargs), calls, None)

    @contextmanager
    def temporary_directory(**kwargs):
        kwargs.update(dir=args.scratch_root, prefix="Pulsara 中文 验收 ")
        directory = Path(mkdtemp(**kwargs))
        import pwd
        # The fixture workspace may be long, but a managed application's home
        # must fit its existing PG/control Unix socket pathname boundaries.
        test_home = Path(mkdtemp(prefix=".pa-", dir=pwd.getpwuid(os.getuid()).pw_dir))
        native(test_home)
        report["scratch"] = str(directory)
        report["test_home"] = str(test_home)
        report["workspace_utf8_bytes"] = len(os.fsencode(directory / "workspace"))
        report["pg_socket_utf8_bytes"] = len(os.fsencode(test_home / "desktop/socket/.s.PGSQL.5432"))
        report["interop_control_utf8_bytes"] = len(os.fsencode(test_home / "desktop/i-XXXXXXXX/c"))
        if report["pg_socket_utf8_bytes"] >= 104 or report["interop_control_utf8_bytes"] >= 108:
            raise ValueError("test application home exceeds its existing Unix socket boundary")
        try:
            yield str(directory)
        except BaseException:
            # Original core shutdown also runs inside this context. A failed
            # physical close must not silently remove a still-used workspace.
            report["scratch_retained"] = True
            report["test_home_retained"] = True
            raise
        else:
            shutil.rmtree(directory)
            shutil.rmtree(test_home)
            report["scratch_removed"] = True
            report["test_home_removed"] = True

    def read_only_runtime_settings(value):
        # The original runner already allocated its workspace before this
        # supported settings factory. Redirect only application data, retaining
        # that exact long Unicode project path and the same production owners.
        os.environ["PULSARA_HOME"] = report["test_home"]
        if args.input_closure:
            (Path(report["scratch"]) / "workspace/eof_probe.py").write_text(
                "import sys\nprint('EOF_READY',flush=True)\nfor line in sys.stdin:\n print('SEEN:'+line.strip(),flush=True)\nprint('EOF_SEEN',flush=True)\n", encoding="utf-8")
        return api.shared._ReadOnlySettingsStore(value)

    def prompt_content(text):
        if args.input_closure:
            tty = "Send hello without a newline" not in text
            mode = "pty" if tty else "pipe"
            command = shlex.quote(sys.executable) + " -u " + shlex.quote(str(Path(report["scratch"]) / "workspace/eof_probe.py"))
            text = (f"Run exactly {command!r} once with tty={'true' if tty else 'false'} and yield_time_ms=100. "
                    f"Obtain EOF_READY. Submit {mode}-data and observe SEEN:{mode}-data. "
                    "Then use terminal_process action=close_stdin once on this exact process_id. "
                    "Wait for that same process to finish naturally and report EOF_SEEN and exit_code=0. "
                    "For close_stdin use exactly the JSON keys action and process_id: it has NO yield_time_ms option. "
                    "Use only terminal and terminal_process; do not read the fixture file. "
                    "Do not submit quit, kill, rerun, or send manual control characters; this tests the advertised close_stdin action.")
            report.setdefault("actual_prompts", {})[mode] = text
        return original_prompt_content.text(text)

    # Adapt only the test runner's factories; the production owners are unchanged.
    original_runtime = api.terminal.ModelRuntime
    original_prompt_content = api.terminal.PromptContent
    runner_args = SimpleNamespace(model=args.model, cases=args.cases, output=args.output, phase="candidate-wsl")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("use a new output file; previous evidence must not be overwritten")
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PULSARA_HOME": str(args.saved_home)}))
            for name, replacement in {
                "LocalSettingsStore": lambda: api.shared._ReadOnlySettingsStore(saved),
                "_create_database": create_database, "_drop_database": drop_database,
                "_find_connection": lambda _settings, _model: selected,
                "ModelRuntime": SimpleNamespace(production=recording_runtime),
                "TemporaryDirectory": temporary_directory, "save": save,
                "_ReadOnlySettingsStore": read_only_runtime_settings,
                "PromptContent": SimpleNamespace(text=prompt_content),
            }.items():
                stack.enter_context(patch.object(api.terminal, name, replacement))
            asyncio.run(api.terminal.run(runner_args, report))
    except BaseException as error:
        report.update(status="failed", failure_type=type(error).__name__, failure_message=str(error))
    finally:
        report["undropped_databases"] = list(created)
        save(args.output, report, saved)
    print(json.dumps({"status": report["status"], "output": str(args.output), "undropped_databases": list(created)}))
    return 0 if report["status"] == "completed" and not created else 1


def main() -> int:
    arguments = parser()
    args = arguments.parse_args()
    if args.execute:
        required = ("saved_home", "connection_id", "model", "distribution", "user", "expected_uid", "interop_helper",
                    "socket", "port", "admin_role", "runtime_role", "root_database", "expected_data",
                    "expected_system_id", "scratch_root", "output")
        missing = ["--" + name.replace("_", "-") for name in required if getattr(args, name) is None]
        if missing:
            arguments.error("execute requires " + ", ".join(missing))
        if args.input_closure and args.cases != "interaction,pty":
            arguments.error("--input-closure requires --cases interaction,pty")
    api = imports()
    if args.check_imports:
        print("Imports resolved; no settings read, database connection, scope creation, or provider call.")
        return 0
    return run(args, api)


if __name__ == "__main__":
    raise SystemExit(main())
