"""Serve a disposable real HTTP/UI corpus; Ctrl-C records results and drops it.

Run with the repository .venv after build:local. Saved production settings are
read-only input; no real conversation is touched and no model turn is sent.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from time import monotonic

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.tool_permission import default_permission_policy
from pulsara_agent.web_app.application import LocalWebApplication
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _create_database, _drop_database,
)
from tests.test_conversation_fork import fork, identity, rows
from tests.test_direct_advisory_memory import _open_direct_memory_session
from tests.test_session_archive import archive


async def run():
    saved = LocalSettingsStore(require_pulsara_home() / 'local-settings.yaml').read()
    scrub = secret_scrubber(saved)
    name, _, admin, runtime = _create_database(saved)
    previous_home = os.environ.get('PULSARA_HOME')
    report_path = Path('output/playwright/archive-bulk/report.json').resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix='pulsara-archive-bulk-') as temporary:
            root = Path(temporary)
            (root / 'home').mkdir()
            project = root / 'project'
            project.mkdir()
            preserved_file = project / 'keep.txt'
            preserved_file.write_text('working directory stays intact\n')
            os.environ['PULSARA_HOME'] = str(root / 'home')
            settings = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(runtime, admin)))
            app = LocalWebApplication(settings=settings, permission_policy=default_permission_policy())
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
            try:
                await app.start()
                assert app.database_state.value == 'ready'
                repo = await app.core._ensure_resources()
                source, _, remember = _open_direct_memory_session(repo, workspace_root=str(project))
                _, fact, _ = remember('Bulk deletion keeps this saved project memory.', project=True)
                sid = source.guard.session_id
                turn_id = rows(repo, "SELECT id FROM pulsara_v3.turns WHERE session_id=%s AND status='RUNNING'", (sid,))[0]['id']
                final_id = identity('entry')
                repo.commit_assistant_message(source.guard,
                    cut=repo.prepare_provider_input_cut(source.guard, turn_id=turn_id, deadline_monotonic=monotonic()+30),
                    entry_id=final_id, parent_content=InlineContent.from_bytes(b'done'),
                    blocks=(AssistantTextBlock(identity('block'), InlineContent.from_bytes(b'done')),),
                    complete_turn=True, occurred_at=datetime.now(timezone.utc), actor_id='model:test', deadline_monotonic=monotonic()+30)
                child = fork(repo, sid, final_id)
                assert child.created
                workspace = rows(repo, 'SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s', (sid,))[0]['workspace_id']
                targets = []
                for index in range(12):
                    lease = source if index == 0 else repo.acquire_host_writer(
                        intent='NEW', session_id=identity('session'), workspace_id=workspace, workspace_root=str(project),
                        writer_owner_id=identity('host'), lease_seconds=30, deadline_monotonic=monotonic()+30)
                    repo.rename_session(session_id=lease.guard.session_id, memory_domain_id='u_local',
                        title=f'归档验收 {index + 1:02d}', deadline_monotonic=monotonic()+30)
                    archive(repo, lease)
                    targets.append(lease.guard.session_id)
                report = {'origin': app.origin, 'database': name, 'targets': targets,
                    'source_session': sid, 'unselected_fork': child.child_session_id, 'memory_id': fact['memory_id']}
                report_path.write_text(scrub.scrub_text(json.dumps(report, ensure_ascii=False, indent=2)))
                print(f'READY {app.origin} — 12 cold archives, shared workspace, memory and unselected fork; Ctrl-C to inspect and clean up.', flush=True)
                await stop.wait()
                remaining = rows(repo, 'SELECT id,lifecycle FROM pulsara_v3.sessions ORDER BY id')
                memory = rows(repo, 'SELECT id,statement,created_by_tool_result_id FROM pulsara_v3.memory_facts WHERE id=%s', (fact['memory_id'],))
                report.update(remaining_sessions=remaining, saved_memory=memory, working_file_preserved=preserved_file.is_file(),
                    runtime_session_ids=[session.session_id for session in app.core._sessions.values()])
                report['checks'] = {
                    'memory_preserved': bool(memory) and memory[0]['statement'] == 'Bulk deletion keeps this saved project memory.',
                    'deleted_source_provenance_cleared': bool(memory) and memory[0]['created_by_tool_result_id'] is None,
                    'unselected_fork_preserved': any(row['id'] == child.child_session_id for row in remaining),
                    'working_file_preserved': preserved_file.is_file(),
                    'archived_targets_not_loaded': not set(targets).intersection(report['runtime_session_ids']),
                }
                report_path.write_text(scrub.scrub_text(json.dumps(report, ensure_ascii=False, indent=2)))
                print('Recorded canonical results: ' + str(report_path), flush=True)
            finally:
                await app.aclose()
    finally:
        if previous_home is None:
            os.environ.pop('PULSARA_HOME', None)
        else:
            os.environ['PULSARA_HOME'] = previous_home
        _drop_database(saved, name)
        print('Disposable database dropped; saved production settings were not changed.', flush=True)


if __name__ == '__main__':
    asyncio.run(run())
