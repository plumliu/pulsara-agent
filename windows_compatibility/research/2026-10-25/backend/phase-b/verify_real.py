import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
node = "C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe"
guest = ["C:/Windows/System32/wsl.exe", "-d", "Ubuntu", "-u", "plumwsl", "--exec", "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python"]
remote = "/mnt/c/python_workspace/pulsara-agent/output/windows-g0/pg_admission_r3/phase-b-pg/"
prepared = subprocess.run([*guest, remote + "prepare_guest.py"], capture_output=True, text=True, encoding="utf-8", check=True)
configuration = json.loads(prepared.stdout)
(root / "config.json").write_text(json.dumps(configuration), encoding="utf-8")
facts = {"configuration": configuration}
try:
    result = subprocess.run([node, str(root / "real_owner.cjs"), str(root / "config.json")], capture_output=True, text=True, encoding="utf-8")
    facts.update(exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
finally:
    cleanup = subprocess.run([*guest, remote + "cleanup_guest.py"], capture_output=True, text=True, encoding="utf-8")
    facts["cleanup"] = {"exit": cleanup.returncode, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
    (root / "real-run.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
print(json.dumps({"exit": facts["exit"], "cleanup_exit": cleanup.returncode}))
assert facts["exit"] == 0 and cleanup.returncode == 0
