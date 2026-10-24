"""Windows finite carrier for the output-only Linux production-owner PG probe."""
import json
from pathlib import Path
import subprocess

output = Path(__file__).resolve().parent
repo = '/home/plumwsl/workspaces/pulsara_agent'
linux_output = repo + '/output/windows-g0/backend/' + output.name
node = '/home/plumwsl/.local/share/mise/installs/node/22.23.3/bin/node'
command = ['wsl','-d','Ubuntu','-u','plumwsl','--cd',repo,'--exec',node,linux_output+'/pg-contract.cjs',repo]
result = subprocess.run(command,capture_output=True)
(output/'pg-contract.log').write_bytes(result.stdout+b'\nSTDERR:\n'+result.stderr)
(output/'pg-command.json').write_text(json.dumps({'command':command,'exit':result.returncode,'stdoutFile':'pg-contract.log'},indent=2)+'\n',encoding='utf-8')
evidence = subprocess.run(['wsl','-d','Ubuntu','-u','plumwsl','--exec','/bin/cat',linux_output+'/pg-contract.json'],capture_output=True)
if evidence.returncode == 0:
    (output/'pg-contract.json').write_bytes(evidence.stdout)
print(json.dumps({'exit':result.returncode,'evidenceExit':evidence.returncode,'log':str(output/'pg-contract.log')}))
raise SystemExit(result.returncode)
