"""Exact private-cluster Host writer loss; no provider or saved settings."""
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback

import pulsara_agent

installed = '/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/'
assert pulsara_agent.__file__.startswith(installed), pulsara_agent.__file__
sys.path.insert(0, '/mnt/c/python_workspace/pulsara-agent')
from tests.support.model_config import test_model_runtime
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.desktop_runtime import prepare_database
from pulsara_agent.settings import LocalPostgresConfig
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.windows_interop import configure_interop
from pulsara_agent.terminal_process.models import TerminalRequest
import psycopg
from psycopg.conninfo import make_conninfo

config = json.loads(Path(sys.argv[1]).read_text())
home = Path(config['home'])
assert home.parent.name.startswith('.hostlease-') and home.parent.parent == Path('/home/plumwsl')
os.environ['PULSARA_HOME'] = str(home)
runtime_dsn = make_conninfo(host=str(home/'desktop/socket'),port=5432,user='pulsara_runtime',dbname='pulsara')
admin_dsn = make_conninfo(host=str(home/'desktop/socket'),port=5432,user='pulsara_admin',dbname='pulsara')
prepare_database(LocalPostgresConfig(runtime_dsn, admin_dsn), initialize=True)
configure_interop(helper_path='/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe',distribution='Ubuntu',user='plumwsl')

async def run():
    core = KernelHostCore.production(model_runtime=test_model_runtime(postgres_dsn=runtime_dsn))
    session = await core.open_session(HostWorkspaceInput(workspace_kind='project',workspace_root=Path(config['workspace'])))

    guard = session._lease.guard
    with psycopg.connect(admin_dsn) as connection:
        identity = connection.execute('SELECT system_identifier::text,current_setting(\'data_directory\') FROM pg_control_system()').fetchone()
        assert identity[1] == str(home/'desktop/postgres'), identity
        changed = connection.execute("UPDATE pulsara_v3.sessions SET writer_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s AND writer_generation=%s AND writer_lease_owner_id=%s RETURNING writer_generation,writer_lease_owner_id,writer_lease_expires_at",(guard.session_id,guard.writer_generation,guard.writer_owner_id)).fetchone()
        assert changed is not None
    result = dict(package=pulsara_agent.__file__,privateHome=str(home),clusterIdentity=identity,session=guard.session_id,expiredRow=changed)
    try:
        await core.close_session(session.host_session_id)
        result['close']='completed'
    except BaseException as error:
        result.update(close='failed',errorType=type(error).__name__,error=str(error),traceback=traceback.format_exc())
    return result

print(json.dumps(asyncio.run(run()),default=str,ensure_ascii=False))

