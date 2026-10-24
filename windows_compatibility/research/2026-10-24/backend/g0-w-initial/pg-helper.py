"""G0 probe only: one locked physical pg_ctl call, never lifecycle ownership."""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('repo', type=Path)
parser.add_argument('home', type=Path)
parser.add_argument('--before-lock-ms', type=int, default=0)
parser.add_argument('--after-frame-ms', type=int, default=0)
parser.add_argument('--pause-command-before-exec', action='store_true')
parser.add_argument('--fault-command-output-file', action='store_true')
args = parser.parse_args()
home = args.home
assert home.is_absolute() and home.resolve(strict=True) == home and str(home).startswith('/tmp/g0h-')
assert home.stat().st_uid == os.getuid() and stat.S_IMODE(home.stat().st_mode) == 0o700
os.environ['PULSARA_HOME'] = str(home)
# Import only after selecting this probe's isolated home.
from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402

def emit(item):
    try:
        print(json.dumps(item), flush=True)
    except BrokenPipeError:
        # The physical child has already settled before result emission.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())

if args.before_lock_ms:
    time.sleep(args.before_lock_ms / 1000)
try:
    descriptor = acquire_kernel_lock()
except BlockingIOError:
    emit({'type': 'busy'})
    raise SystemExit(73)
try:
    assert not os.get_inheritable(descriptor)
    data = home / 'desktop/postgres'
    socket = home / 'desktop/socket'
    for directory in (data, socket):
        assert directory.resolve(strict=True) == directory and directory.stat().st_uid == os.getuid()
        assert not stat.S_IMODE(directory.stat().st_mode) & 0o077
    assert (data / 'PG_VERSION').read_text().strip() == '17'
    emit({'type': 'ready', 'pid': os.getpid(), 'lock_cloexec': True, 'uid': os.getuid(), 'data': str(data), 'socket': str(socket)})
    line = sys.stdin.buffer.readline()
    if not line:
        emit({'type': 'no-operation', 'reason': 'stdin-eof'})
        raise SystemExit(0)
    frame = json.loads(line)
    assert isinstance(frame, dict) and set(frame) == {'operation'} and frame['operation'] in ('start','stop','status')
    operation = frame['operation']
    print(json.dumps({'type': 'accepted', 'operation': operation, 'pid': os.getpid()}), file=sys.stderr, flush=True)
    if args.after_frame_ms:
        time.sleep(args.after_frame_ms / 1000)
    pg_ctl = args.repo / 'desktop/resources/postgres/bin/pg_ctl'
    command = [str(pg_ctl), operation, '-D', str(data)]
    if operation == 'start':
        options = f"-c listen_addresses='' -k '{socket}' -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries=pg_search"
        command += ['-w', '-l', str(home / 'desktop/postgres.log'), '-o', options]
    elif operation == 'stop':
        pid = (data / 'postmaster.pid').read_text().splitlines()
        assert pid[1] == str(data) and pid[4] == str(socket)
        command += ['-w','-m','fast']
    if args.pause_command_before_exec:
        assert operation == 'start'
        # Explicit scheduling fault: spawned physical-command child pauses before exec.
        command = ['/bin/sh','-c','kill -STOP "$$"; exec "$@"','/bin/sh',*command]
    command_log = None
    if args.fault_command_output_file:
        # Keep command output observable after this deliberate helper death.
        assert args.pause_command_before_exec
        command_log = (home / 'physical-command.log').open('w+')
    child = subprocess.Popen(command, stdout=command_log or subprocess.PIPE, stderr=subprocess.STDOUT if command_log else subprocess.PIPE, text=True, env={**os.environ, 'LC_ALL':'C'}, close_fds=True)
    print(json.dumps({'type':'child-started','pid':child.pid,'helper':os.getpid(),'paused_before_exec':args.pause_command_before_exec}),file=sys.stderr,flush=True)
    stdout, stderr = child.communicate()
    if command_log:
        command_log.seek(0)
        stdout, stderr = command_log.read(), ''
        command_log.close()
    # Readback is performed while still holding the same kernel flock.
    observed = subprocess.run([str(pg_ctl),'status','-D',str(data)], capture_output=True, text=True, env={**os.environ,'LC_ALL':'C'}, close_fds=True)
    reply = {'type':'result','operation':operation,'exit':child.returncode,'stdout':stdout,'stderr':stderr,'status_exit':observed.returncode,'status_stdout':observed.stdout,'status_stderr':observed.stderr}
    print(json.dumps({'type':'settled', **reply}), file=sys.stderr, flush=True)
    emit(reply)
finally:
    os.close(descriptor)
