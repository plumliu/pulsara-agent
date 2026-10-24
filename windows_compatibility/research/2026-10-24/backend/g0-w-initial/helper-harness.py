"""Windows-host finite WSL helper probe; owns only a new /tmp/g0h-* cluster."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

repo, linux_output, node = sys.argv[1:]
output = Path(__file__).resolve().parent
python = repo + '/desktop/resources/python/bin/python3'
base = ['wsl','-d','Ubuntu','-u','plumwsl','--cd',repo,'--exec']
records = []
children = []
environment = {key: os.environ[key] for key in ('PATH','SystemRoot','WINDIR','LOCALAPPDATA','USERPROFILE','TEMP','TMP') if key in os.environ}

def record(scenario, **values):
    item = {'scenario':scenario, **values}
    records.append(item)
    print(json.dumps(item, ensure_ascii=False), flush=True)

def finite(command, data=None):
    result = subprocess.run(command, input=data, capture_output=True, env=environment)
    record('finite-call', command=command, exit=result.returncode,
           stdout=result.stdout.decode('utf-8','replace'), stderr=result.stderr.decode('utf-8','replace'))
    return result

setup = finite(base + [node, linux_output + '/helper-setup.cjs', repo])
assert setup.returncode == 0
scope = json.loads(setup.stdout.decode().splitlines()[-1])
home = scope['home']
assert home.startswith('/tmp/g0h-') and scope['uid'] == 1000
record('scope', **scope)

def spawn(*extra):
    command = base + [python,'-I',linux_output+'/pg-helper.py',repo,home,*extra]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    child.probe_command = command
    child.probe_errors = []
    child.probe_queue = queue.Queue()
    def drain():
        for line in iter(child.stderr.readline,b''):
            child.probe_errors.append(line)
            child.probe_queue.put(line)
    child.probe_drain = threading.Thread(target=drain,daemon=True)
    child.probe_drain.start()
    children.append(child)
    return child

def ready(child):
    line = child.stdout.readline()
    parsed = json.loads(line)
    record('helper-frame', command=child.probe_command, frame=parsed)
    return parsed

def send(child, operation):
    child.stdin.write((json.dumps({'operation':operation})+'\n').encode())
    child.stdin.flush()
    accepted = json.loads(child.probe_queue.get(timeout=15))
    assert accepted['type'] == 'accepted' and accepted['operation'] == operation
    record('operation-accepted', frame=accepted)

def finished(child, *, read=True):
    if child.stdin and not child.stdin.closed:
        child.stdin.close()
    remainder = child.stdout.read() if read else b''
    code = child.wait(timeout=30)
    child.probe_drain.join(timeout=1)
    record('helper-finished', command=child.probe_command, exit=code,
           stdout=remainder.decode('utf-8','replace'), stderr=b''.join(child.probe_errors).decode('utf-8','replace'))
    return code, remainder

def operation(name):
    child = spawn()
    assert ready(child)['type'] == 'ready'
    send(child,name)
    code, remainder = finished(child)
    assert code == 0
    reply = json.loads(remainder)
    assert reply['type'] == 'result'
    return reply

failure = None
helper_death_mechanism_passed = None
try:
    child = spawn('--before-lock-ms','200')
    child.stdin.close()
    assert ready(child)['type'] == 'ready'
    code, reply = finished(child)
    assert code == 0 and json.loads(reply)['type'] == 'no-operation'
    assert operation('status')['status_exit'] == 3
    record('eof-before-ready', passed=True, noOperation=True)

    holder = spawn()
    assert ready(holder)['type'] == 'ready'
    busy = spawn()
    assert ready(busy) == {'type':'busy'}
    assert finished(busy)[0] == 73
    holder.stdin.close()
    code, reply = finished(holder)
    assert code == 0 and json.loads(reply)['type'] == 'no-operation'
    assert operation('status')['status_exit'] == 3
    record('ready-eof-and-busy', passed=True, noOperation=True)

    lost = spawn('--after-frame-ms','500')
    assert ready(lost)['type'] == 'ready'
    send(lost,'start')
    lost.stdout.close()
    during = spawn()
    assert ready(during) == {'type':'busy'}
    assert finished(during)[0] == 73
    code, _ = finished(lost,read=False)
    assert code == 0
    observed = operation('status')
    assert observed['exit'] == 0 and observed['status_exit'] == 0
    record('lost-reply-start', passed=True, outcomeAtLoss='unknown', laterStatus=observed, lockRetainedUntilSettle=True, daemonDidNotInheritLock=True)
    stopped = operation('stop')
    assert stopped['exit'] == 0 and stopped['status_exit'] == 3
    record('stop', passed=True, result=stopped)

    victim = spawn('--pause-command-before-exec')
    frame = ready(victim)
    assert frame['type'] == 'ready'
    helper_pid = frame['pid']
    send(victim,'start')
    physical = json.loads(victim.probe_queue.get(timeout=15))
    assert physical['type'] == 'child-started' and physical['paused_before_exec'] is True
    child_pid = physical['pid']
    # Verify the stopped child and exact helper's command lines before fault injection.
    inspect = "import pathlib,sys; p=pathlib.Path('/proc')/sys.argv[1]; s=(p/'status').read_text(); assert 'State:\\tT' in s,s; c=(p/'cmdline').read_bytes(); assert sys.argv[2].encode() in c and b'pg_ctl' in c; print(s.splitlines()[:6])"
    assert finite(base+[python,'-I','-c',inspect,str(child_pid),home]).returncode == 0
    kill = "import os,pathlib,signal,sys; c=(pathlib.Path('/proc')/sys.argv[1]/'cmdline').read_bytes(); assert b'pg-helper.py' in c and sys.argv[2].encode() in c; os.kill(int(sys.argv[1]),signal.SIGKILL); print('exact probe helper killed')"
    assert finite(base+[python,'-I','-c',kill,str(helper_pid),home]).returncode == 0
    child_state = "import pathlib,sys; p=pathlib.Path('/proc')/sys.argv[1]; print((p/'status').read_text() if p.exists() else 'child no longer exists')"
    finite(base+[python,'-I','-c',child_state,str(child_pid)])
    # Carrier completion alone does not authorize reset; inspect the original Linux lock.
    premature = operation('status')
    assert premature['status_exit'] == 3
    resume = "import os,pathlib,signal,sys; c=(pathlib.Path('/proc')/sys.argv[1]/'cmdline').read_bytes(); assert b'pg_ctl' in c and sys.argv[2].encode() in c; os.kill(int(sys.argv[1]),signal.SIGCONT); print('exact probe child resumed')"
    assert finite(base+[python,'-I','-c',resume,str(child_pid),home]).returncode == 0
    time.sleep(.2)  # Probe observation delay after deliberate scheduler release.
    late = operation('status')
    assert late['status_exit'] in (0,3)
    helper_death_mechanism_passed = False if late['status_exit'] == 0 else None
    record('helper-death-delayed-command', lateStartObserved=late['status_exit'] == 0, mechanismPassed=helper_death_mechanism_passed,
           helperPid=helper_pid,physicalChildPid=child_pid,
           lockAcquiredAndStatus3WhileChildPaused=True,laterStatus=late,
           commandOutput='normal subprocess captured stdout/stderr pipes',
           meaning='This pre-exec SIGSTOP/SIGKILL input does not cover every real pg_ctl fork/start phase; no overall safety inference')
    if late['status_exit'] == 0:
        stopped = operation('stop')
        assert stopped['exit'] == 0 and stopped['status_exit'] == 3
    # Reap the local carrier only after the exact physical child has settled.
    victim.stdin.close()
    victim.stdout.read()
    victim.wait(timeout=30)
    victim.probe_drain.join(timeout=1)
    record('fault-carrier-settled',exit=victim.returncode,stderr=b''.join(victim.probe_errors).decode('utf-8','replace'))
    finite(base+[python,'-I','-c',child_state,str(child_pid)])
except BaseException as error:
    failure = error
    record('failure', error=repr(error))
finally:
    # No timeout authorizes a kill, reset or delete. Preserve unresolved children.
    unresolved = [child.pid for child in children if child.poll() is None]
    if not unresolved:
        result = operation('status')
        if result['status_exit'] == 0:
            result = operation('stop')
        if result['status_exit'] == 3:
            cleanup = "import os,pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); assert str(p).startswith('/tmp/g0h-') and p.resolve(strict=True)==p and p.stat().st_uid==os.getuid(); shutil.rmtree(p); print('exact owned home removed')"
            removed = finite(base+[python,'-I','-c',cleanup,home])
            assert removed.returncode == 0
            record('cleanup', home=home, removed=True, finalStatus=3)
    else:
        record('cleanup-preserved', home=home, unresolvedWindowsCarriers=unresolved)
    (output/'helper-harness.json').write_text(json.dumps({'probeAssertionsPassed':failure is None and not unresolved,'helperDeathMechanismPassed':helper_death_mechanism_passed,'records':records},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if failure:
    raise failure
