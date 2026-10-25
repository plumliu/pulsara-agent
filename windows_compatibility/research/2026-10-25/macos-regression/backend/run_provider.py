"""macOS bundled-wheel regression; private PG + read-only saved model snapshot."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import fields, is_dataclass, replace
from contextvars import ContextVar
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
from types import SimpleNamespace
from unittest.mock import patch

# Import the product before adding the repository for tests helpers. -I must
# select the rebuilt bundled wheel, never src/ or a PYTHONPATH override.
import pulsara_agent

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PRODUCT = Path(pulsara_agent.__file__).resolve()
assert PRODUCT.is_relative_to(REPO / 'desktop/resources/python')
assert 'site-packages' in PRODUCT.parts
sys.path.append(str(REPO))

import psycopg
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.provider_dispatch import ProviderDispatchCoordinator
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.conversation_kernel.turn_admission import TurnAdmissionCoordinator
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from tests.dogfood import run_model_switch_handover_dogfood as shared
from tests.dogfood import run_round10_subagent_dogfood as runner
from tests.support.postgres_database import (
    create_migrated_postgres_test_database, drop_postgres_test_database,
)


def main():
    global OUT
    scenarios = ('capacity', 'context')
    if len(sys.argv) > 1:
        assert sys.argv[1] in scenarios
        scenarios = (sys.argv[1],)
    if len(sys.argv) > 2:
        OUT = OUT / sys.argv[2]
        OUT.mkdir(exist_ok=False)
    saved = LocalSettingsStore(require_pulsara_home() / 'local-settings.yaml').read()
    scrub = saved.credential_scrub_set()
    selected_id = 'connection:qjdnthf7ygewjmi4ktd4ppbnzq'
    selected = runner._saved_connection(saved, selected_id)
    facts = json.loads((REPO / 'output/macos-regression-20261025/root/private-pg.json').read_text())
    report = {'product_import': str(PRODUCT), 'executable': sys.executable,
              'connection_id': selected_id, 'model': selected.target.model_id,
              'private_cluster': facts['identity'], 'scenarios': {}}

    def save(name, value):
        public = json.dumps(scrub.scrub_json(value), ensure_ascii=False, indent=2, default=str)
        assert not scrub.contains(public)
        (OUT / name).write_text(public + '\n', encoding='utf-8')

    def log(value):
        public = json.dumps(scrub.scrub_json(value), ensure_ascii=False, default=str)
        assert not scrub.contains(public)
        with (OUT / 'provider-stages.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(public + '\n')

    async def scenario(settings, workspace, name, state):
        calls, comparisons, old_inputs = [], [], {}
        original_init = ConversationKernelRunner.__init__
        original_accept = ConversationKernelRepository.accept_root_turn_intent
        original_activate = ProviderDispatchCoordinator.activate_prospective_root_input
        original_read = ProviderDispatchCoordinator.read_dispatch_read
        expected_read = ContextVar('probe_prospective_expected_read', default=None)

        def differences(left, right, prefix=''):
            if left == right:
                return []
            if type(left) is type(right) and is_dataclass(left):
                result = []
                for field in fields(left):
                    result.extend(differences(getattr(left, field.name), getattr(right, field.name), prefix+'.'+field.name))
                return result
            if isinstance(left, tuple) and isinstance(right, tuple) and len(left) == len(right):
                result = []
                for index, (a, b) in enumerate(zip(left, right)):
                    result.extend(differences(a, b, prefix+f'[{index}]'))
                return result
            return [{'field': prefix, 'prepared': repr(left)[:3000], 'actual': repr(right)[:3000]}]

        async def activate(owner, prepared, **kwargs):
            token = expected_read.set(prepared.admission.canonical_read)
            old_view = owner._continuity.current_view(prepared.planning.scope)
            try:
                result = await original_activate(owner, prepared, **kwargs)
                if result is None:
                    assert not prepared.owns_resources
                    assert owner._safe_point._active_handle is None
                    assert owner._continuity.current_view(prepared.planning.scope) is old_view
                    observation = {'turn_id': prepared.admission.candidate.exact_turn_id,
                                   'prospective_resources_retired': True,
                                   'safe_point_handle_retired': True,
                                   'installed_view_object_unchanged': True}
                    state.setdefault('retired_prospectives', []).append(observation)
                    log({'event': 'prospective-retired', 'scenario': name, **observation})
                return result
            finally:
                expected_read.reset(token)

        async def read(owner, cut, **kwargs):
            actual = await original_read(owner, cut, **kwargs)
            expected = expected_read.get()
            if expected is not None and actual != expected:
                diff = differences(expected, actual)
                state.setdefault('activation_read_differences', []).append(diff)
                log({'event': 'activation-read-difference', 'scenario': name, 'differences': diff})
            return actual

        def initialize(owner, *args, **kwargs):
            previous = kwargs.get('provider_input_installed_observer')
            def observe(request):
                compiled = request.compiled_input
                identity = compiled.canonical_input_identity
                scope = ProviderInputContinuityScope(
                    session_id=request.session_id,
                    scope_kind=identity.conversation_scope_kind,
                    scope_subagent_task_id=identity.scope_subagent_task_id,
                )
                view = owner._continuity.current_view(scope)
                assert view is not None
                key = (scope, view.epoch_nonce)
                old = old_inputs.get(key)
                if old is not None:
                    assert compiled.system_prompt == old.system_prompt
                    assert compiled.tools == old.tools
                    assert compiled.messages[:len(old.messages)] == old.messages
                old_inputs[key] = compiled
                comparisons.append({'session_id': request.session_id,
                    'scope': identity.conversation_scope_kind.value,
                    'task_id': identity.scope_subagent_task_id,
                    'epoch': view.epoch_nonce, 'revision': view.epoch_revision,
                    'compared': old is not None, 'messages': len(compiled.messages)})
                log({'event': 'input-installed', 'scenario': name, **comparisons[-1]})
                if previous is not None:
                    previous(request)
            kwargs['provider_input_installed_observer'] = observe
            original_init(owner, *args, **kwargs)

        def accept(*args, **kwargs):
            try:
                return original_accept(*args, **kwargs)
            except BaseException as error:
                state.setdefault('admission_errors', []).append(
                    {'type': type(error).__name__, 'message': str(error)})
                log({'event': 'admission-error', 'scenario': name,
                     'type': type(error).__name__, 'message': str(error)})
                raise

        original_capacity = runner._run_capacity_mcp_compaction
        async def capacity(session, workspace):
            size = await shared._seed_completed_history(session, segments=5, repetitions=800)
            state['history_seed'] = {'bytes': size, 'segments': 5, 'repetitions': 800,
                                    'helper': 'maintained async canonical admission fixture'}
            with patch.object(runner, '_seed_completed_history', lambda *_a, **_k: None):
                return await original_capacity(session, workspace)

        original_context = runner._run_last_n_and_message
        async def context(session, workspace):
            # Existing Windows probe's exact canonical boundary gate: the two
            # accepted steer intents precede the real first provider call.
            admitted, release = asyncio.Event(), asyncio.Event()
            original_admission = TurnAdmissionCoordinator.accept_root_intent
            original_active = runner._active_root_turn_id
            original_steer = session.steer_active_turn
            target, steers = {}, []
            async def admission(owner, intent, **kwargs):
                accepted = await original_admission(owner, intent, **kwargs)
                if intent.command_id == 'command:rl6vdp74wn6i44vyeaigf5sjyi':
                    target['turn_id'] = accepted.turn_id
                    admitted.set()
                    await release.wait()
                return accepted
            async def active(owner, *, timeout_seconds):
                if owner is not session:
                    return await original_active(owner, timeout_seconds=timeout_seconds)
                await asyncio.wait_for(admitted.wait(), timeout=timeout_seconds)
                return target['turn_id']
            async def steer(**kwargs):
                result = await original_steer(**kwargs)
                steers.append(result.status)
                if len(steers) == 2:
                    release.set()
                return result
            try:
                with patch.object(TurnAdmissionCoordinator, 'accept_root_intent', admission), \
                     patch.object(runner, '_active_root_turn_id', active), \
                     patch.object(session, 'steer_active_turn', steer):
                    return await original_context(session, workspace)
            finally:
                release.set()
                state['canonical_context_gate'] = {'target': target, 'steers': steers}

        def production(**kwargs):
            return shared._RecordingModelRuntime(ModelRuntime.production(**kwargs), calls, None)

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(ConversationKernelRunner, '__init__', initialize))
                stack.enter_context(patch.object(ConversationKernelRepository, 'accept_root_turn_intent', accept))
                stack.enter_context(patch.object(ProviderDispatchCoordinator, 'activate_prospective_root_input', activate))
                stack.enter_context(patch.object(ProviderDispatchCoordinator, 'read_dispatch_read', read))
                stack.enter_context(patch.object(runner, 'ModelRuntime', SimpleNamespace(production=production)))
                stack.enter_context(patch.object(runner, '_run_capacity_mcp_compaction', capacity))
                stack.enter_context(patch.object(runner, '_run_last_n_and_message', context))
                state['result'] = await runner._run(settings, workspace, scenario=name,
                    connection_id=selected_id, reasoning_effort=None)
                assert state['result']['status'] == 'passed', state['result']
        finally:
            state['prefix_checks'] = comparisons
            state['provider_call_count'] = len(calls)
            save(f'{name}-provider-records.json', calls)

    with patch.dict(os.environ, {'PULSARA_POSTGRES_ADMIN_DSN': facts['admin_dsn'],
                                  'PULSARA_POSTGRES_DSN': facts['runtime_dsn'], 'CI': 'true'}):
        for name in scenarios:
            state = report['scenarios'][name] = {}
            database = None
            captured = io.StringIO()
            log({'event': 'scenario-start', 'scenario': name})
            start = monotonic()
            try:
                database = create_migrated_postgres_test_database()
                state['database'] = database.database_name
                settings = replace(saved, postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn))
                with TemporaryDirectory(prefix='pb-', dir='/tmp') as temporary:
                    base = Path(temporary).resolve()
                    home, workspace = base / 'h', base / '中文 空格工程'
                    home.mkdir(mode=0o700)
                    workspace.mkdir(mode=0o700)
                    (workspace / 'pyproject.toml').write_text((REPO / 'pyproject.toml').read_text())
                    with patch.dict(os.environ, {'PULSARA_HOME': str(home)}), \
                         redirect_stdout(captured), redirect_stderr(captured):
                        asyncio.run(scenario(settings, workspace, name, state))
                state['status'] = 'passed'
            except BaseException as error:
                state.update(status='failed', error_type=type(error).__name__,
                             error=str(error), traceback=traceback.format_exc())
            finally:
                state['elapsed_seconds'] = monotonic() - start
                state['captured_output'] = captured.getvalue()
                if database is not None:
                    drop_postgres_test_database(facts['admin_dsn'], database.database_name)
                    with psycopg.connect(facts['admin_dsn'], autocommit=True) as connection:
                        row = connection.execute('SELECT count(*) FROM pg_database WHERE datname=%s',
                                                 (database.database_name,)).fetchone()
                    assert row[0] == 0
                    state['database_dropped'] = True
                log({'event': 'scenario-end', 'scenario': name, 'status': state.get('status'),
                     'seconds': state['elapsed_seconds'], 'database_dropped': state.get('database_dropped')})
                save('provider-report.json', report)
    report['status'] = 'passed' if all(s['status'] == 'passed' for s in report['scenarios'].values()) else 'failed'
    save('provider-report.json', report)
    print(json.dumps({'status': report['status'], 'scenarios': {n: s['status'] for n,s in report['scenarios'].items()}}))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
