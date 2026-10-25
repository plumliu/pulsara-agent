"""Real private PG: original renewal callback/Core/controller cold recovery."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback
from unittest.mock import patch

import pulsara_agent
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PRODUCT = Path(pulsara_agent.__file__).resolve()
assert PRODUCT.is_relative_to(REPO / 'desktop/resources/python')
assert 'site-packages' in PRODUCT.parts
sys.path.append(str(REPO))

import psycopg
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.repository import StaleHostWriter
from pulsara_agent.desktop_app.session_controller import LocalSessionController
from pulsara_agent.tool_permission import default_permission_policy
from tests.support.model_config import test_model_runtime
from tests.support.postgres_database import create_migrated_postgres_test_database, drop_postgres_test_database


async def experiment(database, workspace):
    runtime = test_model_runtime(postgres_dsn=database.runtime_dsn)
    core = KernelHostCore.production(model_runtime=runtime)
    controller = LocalSessionController(core=core, permission_policy=default_permission_policy(),
                                        active_skill_names=frozenset())
    report = {'product_import': str(PRODUCT), 'physical_close_calls': 0}
    try:
        handle = await controller.create_session(workspace_kind='project', workspace_path=str(workspace))
        old = handle.session
        guard = old._lease.guard
        original_close = old.aclose
        async def observed_close(**kwargs):
            report['physical_close_calls'] += 1
            return await original_close(**kwargs)
        with patch.object(old, 'aclose', observed_close):
            with psycopg.connect(database.admin_dsn) as connection:
                row = connection.execute(
                    "UPDATE pulsara_v3.sessions SET writer_lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE id=%s AND writer_generation=%s AND writer_lease_owner_id=%s RETURNING writer_generation",
                    (guard.session_id, guard.writer_generation, guard.writer_owner_id)).fetchone()
                assert row is not None and row[0] == guard.writer_generation
            try:
                await asyncio.wait_for(asyncio.shield(old._renewal_task), timeout=20)
            except StaleHostWriter:
                report['actual_renewal_failure'] = 'StaleHostWriter'
            else:
                raise AssertionError('expired guard renewal unexpectedly succeeded')
            assert old.writer_unavailable and old.writer_lost
            try:
                old._require_open()
            except StaleHostWriter:
                report['old_admission_rejected'] = True
            else:
                raise AssertionError('expired Host remained available')
            one, two = await asyncio.gather(controller.resume_session(handle.session_id),
                                             controller.resume_session(handle.session_id))
            assert one is two
            assert one.session_id == handle.session_id
            assert one.host_session_id != handle.host_session_id
            assert one.session._lease.guard.writer_generation == guard.writer_generation + 1
            assert old._lease.guard == guard and old._closed
            assert handle.host_session_id not in core._sessions
            assert handle.host_session_id not in controller._by_host
            assert controller._by_session[handle.session_id] is one
            assert handle.session_id not in controller._operations
            assert report['physical_close_calls'] == 1
            await one.session._io.run(one.session.repository.read_session_model_call_binding,
                                     one.session._lease.guard, deadline_monotonic=core._canonical_deadline())
            report.update(session_id=handle.session_id, old_host=handle.host_session_id,
                          new_host=one.host_session_id, old_generation=guard.writer_generation,
                          new_generation=one.session._lease.guard.writer_generation,
                          same_concurrent_resume_handle=True, new_guard_read_succeeded=True,
                          old_guard_unchanged=True, old_physical_closed=True)
    finally:
        await controller.aclose()
        await core.shutdown()
        report['controller_and_core_closed'] = True
    return report


def main():
    facts = json.loads((REPO/'output/macos-regression-20261025/root/private-pg.json').read_text())
    report = {'private_cluster': facts['identity']}
    database = None
    with patch.dict(os.environ, {'PULSARA_POSTGRES_ADMIN_DSN': facts['admin_dsn'],
                                  'PULSARA_POSTGRES_DSN': facts['runtime_dsn'], 'CI': 'true'}):
        try:
            database = create_migrated_postgres_test_database()
            report['database'] = database.database_name
            with TemporaryDirectory(prefix='pw-', dir='/tmp') as temporary:
                root = Path(temporary).resolve()
                home, workspace = root/'h', root/'中文 工程'
                home.mkdir(mode=0o700)
                workspace.mkdir(mode=0o700)
                with patch.dict(os.environ, {'PULSARA_HOME': str(home)}):
                    report.update(asyncio.run(experiment(database, workspace)))
            report['status'] = 'passed'
        except BaseException as error:
            report.update(status='failed', error_type=type(error).__name__, error=str(error),
                          traceback=traceback.format_exc())
        finally:
            if database is not None:
                drop_postgres_test_database(facts['admin_dsn'], database.database_name)
                with psycopg.connect(facts['admin_dsn'], autocommit=True) as connection:
                    count = connection.execute('SELECT count(*) FROM pg_database WHERE datname=%s',
                                               (database.database_name,)).fetchone()[0]
                assert count == 0
                report['database_dropped'] = True
            encoded = json.dumps(report, ensure_ascii=False, indent=2)
            (OUT/'writer-recovery.json').write_text(encoded+'\n', encoding='utf-8')
            print(encoded)
    return 0 if report['status'] == 'passed' else 2

if __name__ == '__main__':
    raise SystemExit(main())
