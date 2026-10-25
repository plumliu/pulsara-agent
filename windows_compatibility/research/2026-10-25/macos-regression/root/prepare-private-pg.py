"""Own one private Unix-socket-only cluster; never load saved settings or .env."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

workspace = Path(__file__).resolve().parent
binaries = Path('/Applications/Pulsara.app/Contents/Resources/postgres/bin')
home = Path(tempfile.mkdtemp(prefix='pm-', dir='/tmp')).resolve()
home.chmod(0o700)
data, socket, log = home / 'data', home / 's', home / 'postgres.log'
socket.mkdir(mode=0o700)

def run(name, args, **kwargs):
    return subprocess.run([str(binaries / name), *map(str, args)],
                          capture_output=True, text=True, check=True, **kwargs)

run('initdb', ['-D', data, '-U', 'pulsara_admin', '--auth-local=trust',
              '--auth-host=reject', '--encoding=UTF8', '--locale=C'])
try:
    run('pg_ctl', ['start', '-w', '-D', data, '-l', log, '-o',
                  f"-c listen_addresses='' -k {socket} -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search"])
    run('psql', ['-XAt', '-v', 'ON_ERROR_STOP=1', '-h', socket, '-U', 'pulsara_admin',
                 '-d', 'postgres', '-c', 'CREATE ROLE pulsara_runtime LOGIN'])
    run('psql', ['-XAt', '-v', 'ON_ERROR_STOP=1', '-h', socket, '-U', 'pulsara_admin',
                 '-d', 'postgres', '-c', 'CREATE DATABASE pulsara OWNER pulsara_admin TEMPLATE template0'])
    identity = run('psql', ['-XAt', '-v', 'ON_ERROR_STOP=1', '-h', socket, '-U',
                            'pulsara_admin', '-d', 'postgres', '-c',
                            "SELECT current_setting('data_directory'), current_setting('listen_addresses'), system_identifier FROM pg_control_system()"])
    assert identity.stdout.split('|')[0] == str(data), identity.stdout
    admin = f"host='{socket}' port=5432 dbname=pulsara user=pulsara_admin"
    runtime = f"host='{socket}' port=5432 dbname=pulsara user=pulsara_runtime"
    info = {'home': str(home), 'data': str(data), 'socket': str(socket),
            'binaries': str(binaries), 'pid': int((data / 'postmaster.pid').read_text().splitlines()[0]),
            'admin_dsn': admin, 'runtime_dsn': runtime, 'identity': identity.stdout.strip(),
            'source': 'existing read-only bundled PG17 SDK, separate newly-created cluster'}
    (workspace / 'private-pg.json').write_text(json.dumps(info, indent=2) + '\n')
    print(json.dumps(info, indent=2))
except BaseException:
    run('pg_ctl', ['stop', '-w', '-D', data, '-m', 'fast'])
    raise
