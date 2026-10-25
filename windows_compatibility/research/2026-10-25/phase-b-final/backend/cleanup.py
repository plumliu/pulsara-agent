"""Retire only the exact, already-settled private roots from this experiment."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat

root = Path(__file__).resolve().parent
cases = ('pre-cancel', 'start-loss', 'stop-loss', 'init-loss', 'readiness', 'first-init-loss')
homes = {name: Path(json.loads((root / name / 'config.json').read_text())['home']) for name in cases}
output = root / 'cleanup.json'
assert not output.exists()

def private(path, prefix):
    assert path.parent == Path('/home/plumwsl') and path.name.startswith(prefix)
    assert path.resolve() == path and not path.is_symlink()
    info = path.stat()
    assert info.st_uid == os.getuid() == 1000 and stat.S_IMODE(info.st_mode) == 0o700

def identity(pid):
    path = Path('/proc') / str(pid)
    return {'pid': pid, 'uid': path.stat().st_uid,
            'start_ticks': path.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]}

known = []
snapshots = {}
for name, home in homes.items():
    private(home, '.pbfpg-')
    assert not (home / 'desktop/postgres').exists()
    assert (home / 'outside').read_text() == 'outside-sentinel'
    locks = {}
    for lock_name in ('kernel.lock', 'pg-lifetime.lock'):
        fd = os.open(home / 'desktop' / lock_name, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks[lock_name] = {'inode': os.fstat(fd).st_ino, 'busy': False}
        finally:
            os.close(fd)
    destination = root / name / 'guest-records'
    destination.mkdir(exist_ok=False)
    collected = []
    for path in sorted(home.iterdir()):
        if path.name.startswith(('helper-', 'children-', 'gate-')):
            assert path.is_file() and not path.is_symlink()
            shutil.copyfile(path, destination / path.name)
            collected.append(path.name)
            if path.name.startswith('helper-'):
                known.append(json.loads(path.read_text()))
            elif path.name.startswith('children-'):
                known.extend(json.loads(line) for line in path.read_text().splitlines())
            elif path.name == 'gate-native':
                _, pid, _ = path.read_text().split()
                # Native identity was captured before resume in the original result.
                known.append(json.loads((root / name / 'result.json').read_text())['nativeGate']['gate'])
    log = home / 'desktop/postgres.log'
    if log.exists():
        shutil.copyfile(log, destination / 'postgres.log')
        collected.append('postgres.log')
    snapshots[name] = {'home': str(home), 'data_absent': True, 'locks': locks,
                       'outside': 'outside-sentinel', 'retained_records': collected}

remaining_known = []
for info in known:
    try:
        current = identity(info['pid'])
        if current == {key: info[key] for key in ('pid', 'uid', 'start_ticks')}:
            remaining_known.append(current)
    except FileNotFoundError:
        pass
assert not remaining_known, remaining_known

remaining, inaccessible = [], []
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit() or int(proc.name) == os.getpid():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        command = proc.joinpath('cmdline').read_bytes()
        environment = proc.joinpath('environ').read_bytes().split(b'\0')
        if any(os.fsencode(home) in command or os.fsencode('PULSARA_HOME=' + str(home)) in environment for home in homes.values()):
            remaining.append(int(proc.name))
    except PermissionError:
        inaccessible.append(int(proc.name))
    except FileNotFoundError:
        pass
assert not remaining, remaining

removed = []
for home in homes.values():
    private(home, '.pbfpg-')
    shutil.rmtree(home)
    removed.append(str(home))
tools = {Path(json.loads(line)['home']) for line in (root / 'tool-homes.jsonl').read_text().splitlines()}
for tool in sorted(tools):
    private(tool, '.pbf-tools-')
    assert sorted(path.name for path in tool.iterdir()) == ['fault.so']
    shutil.rmtree(tool)
    removed.append(str(tool))
result = {'snapshots': snapshots, 'known_actor_same_identity_alive': remaining_known,
          'remaining_accessible_exact_owned': remaining, 'same_uid_proc_inaccessible': inaccessible,
          'removed': removed, 'all_removed': all(not Path(path).exists() for path in removed),
          'qualification': 'Known actor identity checks and accessible same-UID command/environment checks; no claim about unrelated inaccessible processes.'}
output.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps({'all_removed': result['all_removed'], 'private_homes': len(homes), 'tools': len(tools),
                  'same_identity_known_alive': len(remaining_known), 'accessible_exact_owned': len(remaining),
                  'same_uid_proc_inaccessible': inaccessible}))
