"""Private PG/source candidate; installed Python, original Core and terminal owners."""
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

repo = Path('/mnt/c/python_workspace/pulsara-agent')
snapshot = Path((repo / 'output/phase-b/terminal/writer-loss-fix/source-path.txt').read_text())
sys.path[:0] = [str(snapshot / 'src'), str(snapshot)]
import pulsara_agent
import psycopg
from psycopg.conninfo import make_conninfo
from tests.support.model_config import test_model_runtime
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelExecutionWatchdogPolicy, KernelWatchdogOwner
from pulsara_agent.conversation_kernel.repository import StaleHostWriter
from pulsara_agent.desktop_runtime import prepare_database
from pulsara_agent.settings import LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.windows_interop import configure_interop
from pulsara_agent.terminal_process.models import TerminalRequest

config = json.loads(Path(sys.argv[1]).read_text())
home = Path(config['home'])
assert home.parent.name.startswith('.hostlease-') and home.parent.parent == Path('/home/plumwsl')
os.environ['PULSARA_HOME'] = str(home)
runtime_dsn = make_conninfo(host=str(home/'desktop/socket'), port=5432, user='pulsara_runtime', dbname='pulsara')
admin_dsn = make_conninfo(host=str(home/'desktop/socket'), port=5432, user='pulsara_admin', dbname='pulsara')
prepare_database(LocalPostgresConfig(runtime_dsn, admin_dsn), initialize=True)
configure_interop(helper_path='/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe', distribution='Ubuntu', user='plumwsl')
phase = Path(sys.argv[1]).with_suffix('.phases.jsonl')

def record(value):
    with phase.open('a') as output:
        output.write(json.dumps(value, default=str) + '\n')

async def scenario(mode):
    core = KernelHostCore.production(
        model_runtime=test_model_runtime(postgres_dsn=runtime_dsn),
        watchdog_policy=KernelExecutionWatchdogPolicy(writer_renew_interval_seconds=0.05),
    )
    session = await core.open_session(HostWorkspaceInput(workspace_kind='project', workspace_root=Path(config['workspace'])))
    record(dict(mode=mode, phase='open', host=session.host_session_id))
    result = dict(mode=mode, session=session.session_id, oldGuard=str(session._lease.guard))
    try:
        state = scope = None
        if mode == 'expired-with-windows':
            initial = await asyncio.to_thread(
                session._tools._terminal.execute,
                TerminalRequest(command='/mnt/c/Windows/System32/cmd.exe /d /c "ping -n 300 127.0.0.1 > nul"', yield_time_ms=0),
                owner_host_session_id=session.host_session_id,
                decision_deadline_monotonic=session._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL),
            )
            assert initial.status.value == 'running', initial
            state = session._tools._terminal.process_registry._states[initial.process_id]
            scope = state.interop_scope
            assert scope is not None and not scope.physically_joined.is_set()
            record(dict(mode=mode, phase='windows-running', process=initial.process_id))
        guard = session._lease.guard
        if mode.startswith('expired'):
            with psycopg.connect(admin_dsn) as connection:
                identity = connection.execute("SELECT system_identifier::text,current_setting('data_directory') FROM pg_control_system()").fetchone()
                assert identity[1] == str(home/'desktop/postgres')
                changed = connection.execute("UPDATE pulsara_v3.sessions SET writer_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s AND writer_generation=%s AND writer_lease_owner_id=%s RETURNING writer_generation,writer_lease_owner_id,writer_lease_expires_at", (guard.session_id, guard.writer_generation, guard.writer_owner_id)).fetchone()
                assert changed is not None
                result['expiredRow'] = changed
            if mode == 'expired-idle':
                await core.close_session(session.host_session_id)
            else:
                await asyncio.gather(session._renewal_task, return_exceptions=True)
                assert session.writer_lost and session.writer_unavailable
                with_error = False
                try:
                    session._require_open()
                except StaleHostWriter:
                    with_error = True
                assert with_error
                await core.close_session(session.host_session_id)
        elif mode == 'renewal-io':
            failure = OSError('controlled renewal connection failure')
            with patch.object(session.repository, 'renew_host_writer', side_effect=failure):
                await asyncio.gather(session._renewal_task, return_exceptions=True)
            assert session.writer_unavailable and not session.writer_lost
            await core.close_session(session.host_session_id)
        assert session._closed and session.host_session_id not in core._sessions
        assert not core._close_attempts
        assert session._lease.guard == guard
        if scope is not None:
            result['physical'] = dict(
                completion=state.physical_completion.is_set(), readerAlive=state.reader.is_alive(),
                watcherAlive=state.watcher.is_alive(), scopeJoined=scope.physically_joined.is_set(),
                scopeWorkerAlive=scope._thread.is_alive(), helperExit=scope.helper.returncode,
                scopeDirectoryRetired=scope.directory is None or not scope.directory.exists(),
            )
            assert result['physical'] == dict(completion=True, readerAlive=False, watcherAlive=False, scopeJoined=True, scopeWorkerAlive=False, helperExit=0, scopeDirectoryRetired=True), result
        if mode != 'expired-idle':
            resumed = await core.resume_session(session.session_id, workspace_input=HostWorkspaceInput(workspace_kind='project', workspace_root=Path(config['workspace'])))
            result['resumedGuard'] = str(resumed._lease.guard)
            assert resumed is not session and resumed.writer_generation > session.writer_generation
            await core.close_session(resumed.host_session_id)
        result['status'] = 'passed'
    except BaseException as error:
        result.update(status='failed', errorType=type(error).__name__, error=str(error), traceback=traceback.format_exc())
    finally:
        try:
            await core.shutdown()
            result['originalCoreShutdown'] = 'completed'
        except BaseException as error:
            result['shutdownError'] = repr(error)
        record(result)
    return result

async def run():
    return dict(package=pulsara_agent.__file__, sourceCandidate=True, cases=[await scenario(mode) for mode in ('expired-idle', 'expired-with-windows', 'renewal-io')])

print(json.dumps(asyncio.run(run()), default=str))
