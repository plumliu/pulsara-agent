"""Installed alpha10 owners; bounded test gates, never a product lifetime cap."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from threading import Event
from time import monotonic, sleep
import traceback
from unittest.mock import patch

OUT = Path('/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal')
REPO = Path('/mnt/c/python_workspace/pulsara-agent')
INSTALLED = Path('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10')
HELPER = '/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe'
CHILD = '/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/terminal/interop-child.exe'
FAULT = str(OUT/'fault.exe')
import pulsara_agent
assert Path(pulsara_agent.__file__).is_relative_to(INSTALLED), pulsara_agent.__file__
from pulsara_agent.windows_interop import configure_interop
from pulsara_agent.windows_interop.scope import WindowsInteropScope, InteropScopeError
from pulsara_agent.windows_interop.relay import InteropRelay
from pulsara_agent.terminal_process import manager as M
configure_interop(helper_path=HELPER, distribution='Ubuntu', user='plumwsl')
os.umask(0o077)
root = Path(tempfile.mkdtemp(prefix='.bfin-', dir='/home/plumwsl'))
home = root/'home'
home.mkdir(mode=0o700)
os.environ['PULSARA_HOME'] = str(home)
case = sys.argv[1]
result = dict(case=case, root=str(root), package=pulsara_agent.__file__, python=sys.executable)

def scope_fact(scope):
    return dict(ready=scope.helper_ready, joined=scope.physically_joined.is_set(),
                failure=scope.failure, helperExit=None if scope.helper is None else scope.helper.returncode,
                directory=None if scope.directory is None else str(scope.directory),
                workerAlive=scope._thread is not None and scope._thread.is_alive(),
                tasks=None if scope.relay is None else len(scope.relay.tasks),
                writers=None if scope.relay is None else len(scope.relay.writers))

def launch(registry, argv):
    owner = 'host:phase-b-final'
    registry.activate_owner(owner)
    return registry.exec_with_yield(command='isolated installed fixture', cwd=root,
        yield_time_ms=0, tty=False, max_lifetime_seconds=None, owner_host_session_id=owner,
        shell_argv=tuple(argv), env=dict(os.environ), decision_deadline_monotonic=monotonic()+15)[0], owner

def terminate_owner(scope):
    ready = scope.helper_ready
    subprocess.run([CHILD,'terminate',str(ready['pid']),ready['creation']], check=True, timeout=10)

async def members(scope):
    async with asyncio.timeout(10):
        while True:
            ids = json.loads((await asyncio.to_thread(subprocess.check_output,[FAULT,'members',scope.helper_ready['job']],text=True)))
            ids = [pid for pid in ids if pid != scope.helper_ready['anchorPid']]
            if ids:
                return ids
            await asyncio.sleep(.005)

def pin_cancel():
    entered, release = Event(), Event()
    scopes = []
    original_create = M.create_scope
    original_server = asyncio.start_unix_server
    anchor_record = root/'anchor.json'
    wrapper = root/'python-anchor'
    wrapper.write_text('#!'+sys.executable+'\n'+f'''
import json,os,sys,time
from pathlib import Path
import pulsara_agent.windows_interop.anchor as anchor
assert str(anchor.__file__).startswith({str(INSTALLED)!r})
record=Path({str(anchor_record)!r})
def trace(frame,event,arg):
 if event=='line' and frame.f_code is anchor.main.__code__ and 'descriptor' in frame.f_locals and frame.f_lineno==anchor.main.__code__.co_firstlineno+6:
  record.write_text(json.dumps(dict(pid=os.getpid(),starttime=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],descriptor=frame.f_locals['descriptor'])))
  while True: time.sleep(.01)
 return trace
sys.argv=['pulsara_agent.windows_interop.anchor',sys.argv[-1]]
sys.settrace(trace)
anchor.main()
''')
    wrapper.chmod(0o700)
    def capture():
        scope = original_create()
        if case == 'pin-before-send':
            scope.configuration = replace(scope.configuration, python_executable=wrapper)
        scopes.append(scope)
        return scope
    async def server(*args, **kwargs):
        if scopes and str(kwargs.get('path')) == str(scopes[0].relay.path):
            result['heldDescriptor'] = scopes[0].relay.descriptor
            result['peer'] = scopes[0].relay.peer_identity
            entered.set()
            await asyncio.Future()
        return await original_server(*args, **kwargs)
    registry = M.ProcessRegistry(max_live_processes=1)
    pidfd = None
    with patch.object(M,'create_scope',capture), patch.object(asyncio,'start_unix_server',server if case=='received-before-bind' else original_server):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(launch,registry,[CHILD,'sleep','30000'])
            try:
                deadline = monotonic()+10
                while case=='pin-before-send' and not anchor_record.exists() and monotonic()<deadline:
                    sleep(.005)
                if case=='pin-before-send':
                    info=json.loads(anchor_record.read_text())
                    pidfd=os.pidfd_open(info['pid'])
                    assert Path(f"/proc/{info['pid']}/stat").read_text().rsplit(')',1)[1].split()[19]==info['starttime']
                    result['anchorPin']=info
                else:
                    assert entered.wait(10)
                assert registry._launching_by_owner['host:phase-b-final']==1
                attempt=next(iter(registry._decision_attempts))
                assert registry.abort_foreground_decision(attempt)
                try:
                    pending.result(timeout=15)
                except (RuntimeError,asyncio.CancelledError):
                    pass
                else:
                    raise AssertionError('cancelled launch published a process')
                assert not registry._states and not registry._launching_by_owner
                scope=scopes[0]
                assert scope.physically_joined.is_set() and scope.directory is None
                if pidfd is not None:
                    assert select.select([pidfd],[],[],10)[0], 'exact pinned anchor still lives'
                result['scope']=scope_fact(scope)
            finally:
                release.set()
                registry.release_owner('host:phase-b-final',timeout_seconds=15)
                if pidfd is not None:os.close(pidfd)

def both_lost():
    registry=M.ProcessRegistry(max_live_processes=1)
    state,owner=launch(registry,[CHILD,'tail','30000'])
    scope=state.interop_scope
    deadline=monotonic()+10
    while not M._snapshot(state,1000).output.strip() and monotonic()<deadline:sleep(.005)
    child=int(M._snapshot(state,1000).output.strip())
    ready=scope.helper_ready
    fault=subprocess.run([FAULT,'pair',str(ready['pid']),ready['creation'],str(ready['monitorPid']),ready['monitorCreation'],str(child)],text=True,capture_output=True,timeout=15,check=True)
    result['externalExactWait']=json.loads(fault.stdout)
    state.process.wait(timeout=10)
    state.reader.join(10);state.watcher.join(10)
    assert not state.reader.is_alive() and not state.watcher.is_alive()
    assert scope.failed.is_set() and not scope.physically_joined.is_set()
    assert not state.physical_completion.is_set() and registry.live_count(owner_host_session_id=owner)==1
    failures=[]
    for _ in range(2):
        try:registry.release_owner(owner,timeout_seconds=1)
        except Exception as error:failures.append(type(error).__name__+':'+str(error))
        else:raise AssertionError('unknown scope reported clean')
    assert state.process_id in registry._states and not scope.physically_joined.is_set()
    assert failures[0]==failures[1]
    result.update(scope=scope_fact(scope),closeErrors=failures,physicalCompletion=False,retainedCapacity=1)
    # This observer's held HANDLE proves only safe fixture cleanup, not product join.
    assert result['externalExactWait']['childWait']==0
    assert not M._process_group_exists(state.process.pid)
    assert not scope._thread.is_alive() and not scope.relay.tasks and not scope.relay.writers
    result['externalCleanupOnly']=True
    for name in ('s','c'):
        path=scope.directory/name
        if os.path.lexists(path):path.unlink()
    scope.directory.rmdir()

async def mcp_lost():
    from pulsara_agent.conversation_kernel.mcp.supervisor import McpHostSupervisor,McpServerState
    from pulsara_agent.mcp_config import _parse_server
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    server=str(REPO/'tests/fixtures/round6_mcp_server.py')
    script="import subprocess,os,sys;subprocess.run([sys.argv[1],'tail','30000'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True);os.execv(sys.executable,[sys.executable,sys.argv[2]])"
    config=_parse_server('loss',dict(transport=dict(type='stdio',command=sys.executable,args=['-c',script,CHILD,server])))
    supervisor=McpHostSupervisor(session_id='session:final-loss',workspace_root=root,configs=(config,),credential_boundary=ProcessCredentialBoundary())
    await supervisor.start()
    assert await supervisor.wait_for_server_settlement('loss',timeout_seconds=10) is McpServerState.READY
    client=supervisor._slots['loss'].client
    scope=client._transport._interop_scope
    await members(scope)
    pending=asyncio.create_task(client.session.call_tool('fixture_delay',dict(delay_ms=1000)))
    await asyncio.sleep(.05)
    await asyncio.to_thread(terminate_owner,scope)
    outcome=await asyncio.gather(pending,return_exceptions=True)
    errors=[]
    for _ in range(2):
        try:await supervisor.aclose()
        except InteropScopeError as error:errors.append(error)
        else:raise AssertionError('critical-loss MCP close reported clean')
    assert errors[0] is errors[1] and supervisor._all_slots
    assert scope.physically_joined.is_set() and scope.directory is None
    result.update(scope=scope_fact(scope),callOutcome=repr(outcome),closeError=str(errors[0]),sameCloseError=True,failedSlotRetained=True)

async def hook_lost():
    from pulsara_agent.hooks import executor as H
    from pulsara_agent.hooks.contracts import (FrozenHookDefinition,FrozenHookSourceProvenance,LocalFileHookSourceIdentity,LocalFileHookTrustSubject,HookSourceKind,HookVisibilityScope,HookEventType,FrozenHookMatcherFact,HookDispatchScopeRef,HookScopeKind)
    from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
    provenance=FrozenHookSourceProvenance(LocalFileHookSourceIdentity(HookSourceKind.USER_FILE,root/'hooks.json',HookVisibilityScope.USER,None),LocalFileHookTrustSubject(HookSourceKind.USER_FILE,None),None,'USER')
    definition=FrozenHookDefinition(provenance,0,0,0,0,HookEventType.USER_PROMPT_SUBMIT_EVENT,FrozenHookMatcherFact('',True),CHILD+' sleep 30000',None,30,False,None,0)
    request=H.HookExecutionRequest(definition,{},HookDispatchScopeRef(object(),object(),HookScopeKind.ROOT,None),0,0,root,None,None)
    executor=H.HookCommandExecutor(credential_boundary=ProcessCredentialBoundary())
    task=asyncio.create_task(executor.execute(request))
    async with asyncio.timeout(15):
        while not executor._attempts or next(iter(executor._attempts)).process is None:await asyncio.sleep(.005)
        attempt=next(iter(executor._attempts));scope=attempt.scope
        await members(scope)
        await asyncio.to_thread(terminate_owner,scope)
        outcome=await task
        assert outcome.failure_code=='HOOK_EXECUTOR_FAILURE',outcome
        assert attempt.joined.is_set() and not executor._attempts
        assert executor._sync_slots._value==H.SYNCHRONOUS_COMMAND_SLOTS
        await executor.aclose()
    result.update(scope=scope_fact(scope),failureCode=outcome.failure_code,joined=attempt.joined.is_set(),originalPermitRestored=True)

try:
    if case in ('pin-before-send','received-before-bind'):pin_cancel()
    elif case=='both-observers-lost':both_lost()
    elif case=='mcp-owner-lost':asyncio.run(mcp_lost())
    elif case=='hook-owner-lost':asyncio.run(hook_lost())
    else:raise ValueError(case)
    result['status']='passed'
except BaseException as error:
    result.update(status='failed',error=repr(error),traceback=traceback.format_exc())
finally:
    result['remainingScopePaths']=[str(p) for p in home.glob('desktop/i-*')]
    (OUT/(case+'-result.json')).write_text(json.dumps(result,default=str,indent=2))
    print(json.dumps(result,default=str),flush=True)
sys.exit(0 if result['status']=='passed' else 1)
