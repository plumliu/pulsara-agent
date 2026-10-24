"""Copy only this output-only probe source to the corresponding ext4 output."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
repo = '/home/plumwsl/workspaces/pulsara_agent'
target = repo + '/output/windows-g0/backend/' + root.name
files = ['queries.py','pg-contract.cjs','pg-helper.py','helper-setup.cjs']
for name in files:
    destination = target + '/' + name
    code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); assert '/output/windows-g0/backend/' in str(p); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(sys.stdin.buffer.read())"
    command = ['wsl','-d','Ubuntu','-u','plumwsl','--exec',repo+'/.venv/bin/python','-I','-c',code,destination]
    subprocess.run(command,input=(root/name).read_bytes(),check=True)
    print(destination)
