"""PR05 installed-package provider/browser evidence with explicit test barriers.

Saved settings are read before selecting an isolated home. The only persistent
objects below are test evidence, never a production approval/recovery mechanism.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import traceback

import pulsara_agent
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.tool_permission import preset_to_policy
from pulsara_agent.web_app.application import LocalWebApplication
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_model_switch_handover_dogfood import _ReadOnlySettingsStore, _binding, _create_database, _drop_database
from run_pr03_user_control_dogfood import _Pr03RecordingRuntime, _rows
from run_pr04_prompt_queue_dogfood import _canonical, _secrets, _write


class DecisionProbe:
    def __init__(self, session, directory, report):
        self.session = session
        self.directory = directory
        self.report = report
        self.writer = session.repository.accept_tool_interaction_decision
        self.reader = session.repository.confirm_tool_interaction_decision
        self.append = session.repository._append_events
        self.local = threading.local()
        self.mode = {}
        session.repository.accept_tool_interaction_decision = self.write
        session.repository.confirm_tool_interaction_decision = self.read
        session.repository._append_events = self.append_events

    def hold(self, phase, kwargs):
        self.report['decision_io'].append({'phase': phase, 'at_utc': datetime.now(timezone.utc).isoformat(),
            **{key: kwargs[key] for key in ('command_id', 'decision_id', 'assistant_entry_id', 'tool_call_id',
                'actor_id', 'decision', 'attempt_id', 'result_id', 'result_entry_id')}})
        (self.directory/'decision-waiting.json').write_text(json.dumps(self.report['decision_io'][-1]))
        while not (self.directory/'release-decision').exists():
            time.sleep(.02)
        (self.directory/'release-decision').unlink()

    def append_events(self, *args, **kwargs):
        result = self.append(*args, **kwargs)
        if getattr(self.local, 'rollback', False):
            raise OSError('PR05 injected SQL rollback after inserts')
        return result

    def write(self, guard, **kwargs):
        mode_path = self.directory/'decision-mode.json'
        self.mode = json.loads(mode_path.read_text()) if mode_path.exists() else {}
        if mode_path.exists():
            mode_path.unlink()
        self.report['decision_writes'].append({'command_id': kwargs['command_id'], 'decision': kwargs['decision']})
        if self.mode.get('hold') == 'write':
            self.hold('writing', kwargs)
        self.local.rollback = self.mode.get('rollback', False)
        try:
            result = self.writer(guard, **kwargs)
        finally:
            self.local.rollback = False
        if self.mode.get('lose_response'):
            raise OSError('PR05 injected response loss after PostgreSQL commit')
        return result

    def read(self, guard, **kwargs):
        self.report['decision_reads'].append({'command_id': kwargs['command_id'], 'decision': kwargs['decision']})
        if self.mode.get('hold') == 'confirm':
            self.hold('confirming', kwargs)
        result = self.reader(guard, **kwargs)
        self.report['decision_io'].append({'phase': 'confirmed', 'command_id': kwargs['command_id'],
            'found': result is not None, 'attempt_id': None if result is None else result.attempt_id,
            'result_id': None if result is None else result.result_id})
        return result


def instrument(session, directory, report):
    # Core sessions share the repository. Wrap that physical writer only once;
    # per-session wrappers would count the same invocation multiple times.
    if not isinstance(getattr(session.repository.accept_tool_interaction_decision, '__self__', None), DecisionProbe):
        DecisionProbe(session, directory, report)
    original = session._runner._provider_input_installed_observer
    def observed(request):
        compiled = request.compiled_input
        report['provider_inputs'].append({'session_id': session.session_id, 'turn_id': request.turn_id,
            'context_binding_revision_id': request.cut.context_binding_revision_id,
            'model_call_index': request.model_call_index, 'system': compiled.system_prompt,
            'tools': repr(compiled.tools), 'messages': [repr(item) for item in compiled.messages]})
        if original:
            original(request)
    session._runner._provider_input_installed_observer = observed
    settle = session._runner._assistant_settlements.settle
    async def held(candidate):
        if (directory/'hold-assistant').exists():
            (directory/'assistant-waiting').write_text(candidate.cut.turn_id)
            while (directory/'hold-assistant').exists():
                await asyncio.sleep(.02)
        return await settle(candidate)
    session._runner._assistant_settlements.settle = held
    settle_tool = session._runner._tool_batches._settle_known_tool_result
    async def held_tool(**kwargs):
        if (directory/'hold-tool-result').exists():
            (directory/'tool-effect-waiting.json').write_text(json.dumps({
                key: kwargs[key] for key in ('turn_id', 'assistant_entry_id', 'tool_call_id', 'attempt_id', 'result_entry_id')
            }))
            while (directory/'hold-tool-result').exists():
                await asyncio.sleep(.02)
        return await settle_tool(**kwargs)
    session._runner._tool_batches._settle_known_tool_result = held_tool


async def run(args, saved, secrets):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    connection = next(item for item in saved.model_connections if item.id.value == args.connection_id)
    saved_home = require_pulsara_home()
    manifest_path = args.output_dir/'test-environment.json'
    if args.resume:
        manifest = json.loads(manifest_path.read_text())
        database, admin, runtime_dsn = manifest['database'], manifest['admin_dsn'], manifest['runtime_dsn']
    else:
        database, _, admin, runtime_dsn = _create_database(saved)
        manifest = {'database': database, 'admin_dsn': admin, 'runtime_dsn': runtime_dsn}
        manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    home = args.output_dir/'isolated-home'
    home.mkdir(exist_ok=True)
    workspace = args.output_dir/'workspace'
    workspace.mkdir(exist_ok=True)
    original_home = os.environ.get('PULSARA_HOME')
    os.environ['PULSARA_HOME'] = str(home)
    report = {'status': 'running', 'pid': os.getpid(), 'saved_home': str(saved_home),
        'package_path': str(Path(pulsara_agent.__file__).resolve()), 'postgres_runtime_dsn': runtime_dsn,
        'provider_calls': [], 'provider_inputs': [], 'decision_io': [], 'decision_writes': [], 'decision_reads': [],
        'sessions': [], 'snapshots': [], 'started_at_utc': datetime.now(timezone.utc).isoformat(), 'resumed_existing_database': args.resume}
    store = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin)))
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    delegate = ModelRuntime.production(settings=store, catalog=catalog)
    runtime = _Pr03RecordingRuntime(delegate, report['provider_calls'], None)
    core = KernelHostCore.production(model_runtime=runtime)
    sessions = []
    original_open = core.open_session
    async def opened(*a, **kw):
        session = await original_open(*a, **kw)
        instrument(session, args.output_dir, report)
        sessions.append(session)
        return session
    core.open_session = opened
    app = LocalWebApplication(settings=store, catalog=catalog, model_runtime=runtime, core=core,
        workspace_input=HostWorkspaceInput(workspace_kind='project', workspace_root=workspace, trust_workspace_mcp_config=False),
        permission_policy=preset_to_policy(PermissionMode.ASK_PERMISSIONS), port=args.port)
    try:
        await app.start()
        if args.resume:
            for session_id in manifest['session_ids']:
                handle = await app.sessions.resume_session(session_id)
                if handle.session not in sessions:
                    instrument(handle.session, args.output_dir, report)
                    sessions.append(handle.session)
        else:
            for _ in range(2):
                handle = await app.sessions.create_session(workspace_kind='project', workspace_path=str(workspace))
                await handle.session.update_model_call_binding(_binding(delegate, connection))
            manifest['session_ids'] = [session.session_id for session in sessions]
            manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
        report.update(origin=app.origin, static_root=str(app.static_root), session_ids=manifest['session_ids'], workspace=str(workspace))
        _write(args.output_dir/'ready.json', {key: report[key] for key in ('origin', 'pid', 'package_path', 'static_root', 'session_ids', 'workspace')}, secrets)
        print(json.dumps({key: report[key] for key in ('origin','pid','package_path','static_root')}), flush=True)
        while not (args.output_dir/'stop-server').exists():
            if (args.output_dir/'expire-now').exists():
                for session in sessions:
                    candidate = session._interactions._pending
                    if candidate:
                        candidate.deadline_monotonic = asyncio.get_running_loop().time() - 1
                (args.output_dir/'expire-now').unlink()
            report['sessions'] = []
            for session in sessions:
                canonical = await _canonical(session)
                for key, table in [('decisions','interaction_decisions'), ('attempts','tool_execution_attempts'), ('results','tool_results')]:
                    canonical[key] = await asyncio.to_thread(_rows, session,
                        f'SELECT * FROM pulsara_v3.{table} WHERE session_id=%s', (session.session_id,))
                candidate = session._interactions._pending
                canonical['pending'] = None if candidate is None else {
                    'interaction_id': candidate.interaction_id, 'turn_id': candidate.turn_id,
                    'assistant_entry_id': candidate.assistant_entry_id, 'tool_call_id': candidate.tool_call_id,
                    'expires_at_utc': candidate.expires_at_utc, 'resolving': candidate.resolving,
                    'visible': candidate.visible, 'admission_completed': candidate.admission_completed}
                canonical['dormant'] = [item.interaction_id for item in session._interactions._dormant]
                report['sessions'].append(canonical)
            _write(args.output_dir/'browser-runtime.json', report, secrets)
            await asyncio.sleep(.5)
        report['status'] = 'graceful-close'
    except BaseException:
        report['status'] = 'failed'
        report['failure'] = traceback.format_exc()
        raise
    finally:
        (args.output_dir/'hold-assistant').unlink(missing_ok=True)
        (args.output_dir/'hold-tool-result').unlink(missing_ok=True)
        (args.output_dir/'release-decision').touch()
        closing_started = time.monotonic()
        await app.aclose()
        await core.shutdown()
        report['close_elapsed_seconds'] = time.monotonic() - closing_started
        report['closed_sessions'] = [{'session_id': session.session_id,
            'pending': session._interactions._pending is not None,
            'dormant_count': len(session._interactions._dormant)} for session in sessions]
        report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        _write(args.output_dir/'browser-runtime.json', report, secrets)
        if original_home is None:
            os.environ.pop('PULSARA_HOME', None)
        else:
            os.environ['PULSARA_HOME'] = original_home
        if not args.keep_database:
            _drop_database(saved, database)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--connection-id', required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--keep-database', action='store_true')
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    saved = LocalSettingsStore().read()
    asyncio.run(run(args, saved, _secrets(saved)))


if __name__ == '__main__':
    main()
