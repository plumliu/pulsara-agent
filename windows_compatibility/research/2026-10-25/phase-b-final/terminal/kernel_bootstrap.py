"""Test task injection; original installed desktop_runtime/Application own closing."""
import asyncio
import json
import os
from pathlib import Path
import runpy
import signal
import sys
from threading import Lock
import time

config=json.loads(Path(sys.argv[1]).read_text())
assert os.environ['PULSARA_HOME']==config['home']
signal.signal(signal.SIGHUP,signal.SIG_IGN)
import pulsara_agent
assert '/runtime/0.1.0-alpha.10/' in pulsara_agent.__file__
from pulsara_agent.desktop_app.application import DesktopApplication
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.terminal_process.models import TerminalRequest
from pulsara_agent.conversation_kernel.execution_watchdogs import KernelWatchdogOwner
lock=Lock()
states=[]
def emit(kind,**fields):
    row=dict(timeNs=time.time_ns(),kind=kind,**fields)
    with lock:
        with Path(config['kernelEvents']).open('a') as out:out.write(json.dumps(row,default=str)+'\n')
def facts():
    return [dict(process=s.process_id,linuxRoot=s.process.pid,physicalCompletion=s.physical_completion.is_set(),readerAlive=s.reader.is_alive(),watcherAlive=s.watcher.is_alive(),scopeJoined=s.interop_scope.physically_joined.is_set(),helperExit=s.interop_scope.helper.returncode,workerAlive=s.interop_scope._thread.is_alive(),directory=s.interop_scope.directory,ready=s.interop_scope.helper_ready,diagnosticError=s.interop_scope.diagnostic_error) for s in states]
original_start,original_close=DesktopApplication.start,DesktopApplication.aclose
async def started(application):
    await original_start(application)
    emit('original-application-started',package=pulsara_agent.__file__,kernelPid=os.getpid(),kernelStart=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],home=os.environ['PULSARA_HOME'],origin=application.origin)
    handle=await application.sessions.create_session(workspace_kind='project',workspace_path=config['workspace'])
    session=handle.session
    manager=session._tools._terminal
    result=await asyncio.to_thread(manager.execute,TerminalRequest(command=config['child']+' tail 300000',yield_time_ms=0),owner_host_session_id=session.host_session_id,decision_deadline_monotonic=session._deadlines.deadline(KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION))
    assert result.status.value=='running',result
    state=manager.process_registry._states[result.process_id]
    states.append(state)
    emit('active-original-terminal',states=facts())
async def closed(application):
    emit('original-application-close-enter',states=facts())
    try:await original_close(application)
    except BaseException as error:
        emit('original-application-close-failed',error=repr(error),states=facts())
        raise
    emit('original-application-close-returned',states=facts())
DesktopApplication.start,DesktopApplication.aclose=started,closed
sys.argv=['pulsara_agent.desktop_runtime']
def runtime_frame(frame,event,arg):
    if event=='call' and frame.f_code.co_name=='emit' and frame.f_code.co_filename.endswith('/pulsara_agent/desktop_runtime.py'):
        value=frame.f_locals.get('value',{})
        if value.get('type') in ('failed','stopping','stopped'):emit('original-runtime-emit-attempt',frame=value)
sys.setprofile(runtime_frame)
try:runpy.run_module('pulsara_agent.desktop_runtime',run_name='__main__',alter_sys=True)
except SystemExit as error:
    emit('original-runtime-system-exit',code=error.code)
    raise
