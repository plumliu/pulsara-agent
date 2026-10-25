"""Current-user installed-runtime inventory, without argv/env disclosure or cleanup."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

home=b'/home/plumwsl/.pulsara'
prefix='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/'
rows=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name)==os.getpid():continue
    try:
        if p.stat().st_uid!=os.getuid():continue
        executable=os.readlink(p/'exe')
        environment=dict(s.split(b'=',1) for s in (p/'environ').read_bytes().split(b'\0') if b'=' in s)
        owns_home=environment.get(b'PULSARA_HOME')==home
        if not executable.startswith(prefix) and not owns_home:continue
        command=(p/'cmdline').read_bytes();arguments=command.split(b'\0')
        fields=(p/'stat').read_text().rsplit(')',1)[1].split()
        rows.append({'pid':int(p.name),'parent':int(fields[1]),'state':fields[0],
            'starttime':fields[19],'executable':executable,'formalHomeEnvironment':owns_home,
            'interopModuleMentioned':b'pulsara_agent.windows_interop' in command,
            'desktopRuntimeMentioned':b'pulsara_agent.desktop_runtime' in command,
            'runtimeWaitOwner':b'--wait-owner' in arguments,
            'inlinePython':b'-c' in arguments})
    except (FileNotFoundError,ProcessLookupError,PermissionError):continue
print(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'processes':rows,
    'observerPidExcluded':os.getpid(),'readOnly':True,'argsOrEnvironmentSerialized':False,
    'scope':'current user visible alpha7 executable path or exact formal PULSARA_HOME; not a complete Windows job membership proof'},indent=2))
