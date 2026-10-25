import json
import os
from pathlib import Path
import sys

repo = Path('/mnt/c/python_workspace/pulsara-agent')
snapshot = Path((repo / 'output/phase-b/terminal/writer-loss-fix/source-path.txt').read_text())
sys.path[:0] = [str(snapshot / 'src'), str(snapshot)]
import pytest
from psycopg.conninfo import make_conninfo
from pulsara_agent.desktop_runtime import prepare_database
from pulsara_agent.settings import LocalPostgresConfig

config = json.loads(Path(sys.argv[1]).read_text())
home = Path(config['home'])
assert home.parent.name.startswith('.hostlease-') and home.parent.parent == Path('/home/plumwsl')
runtime = make_conninfo(host=str(home/'desktop/socket'), port=5432, user='pulsara_runtime', dbname='pulsara')
admin = make_conninfo(host=str(home/'desktop/socket'), port=5432, user='pulsara_admin', dbname='pulsara')
prepare_database(LocalPostgresConfig(runtime, admin), initialize=True)
os.environ.update(PULSARA_POSTGRES_DSN=runtime, PULSARA_POSTGRES_ADMIN_DSN=admin)
os.chdir(snapshot)
sys.exit(pytest.main(['tests/test_host_writer_availability.py', '-q', '-o', 'faulthandler_timeout=20']))
