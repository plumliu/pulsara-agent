"""Personal G0 preparation runner; no production imports or recovery state."""
import json
import os
from pathlib import Path
import subprocess
import sys

script = Path(sys.argv[1]).resolve()
user = sys.argv[2] if len(sys.argv) > 2 else "plumwsl"
assert user in {"plumwsl", "root"}
command = [str(Path(os.environ["SystemRoot"]) / "System32/wsl.exe"),
           "-d", "Ubuntu", "-u", user, "--cd", "/home/plumwsl", "--exec", "/bin/bash", "-se"]
environment = {key: value for key, value in os.environ.items() if key.upper() != "WSLENV"}
with script.open("rb") as source, script.with_suffix(".stdout.log").open("wb") as stdout, script.with_suffix(".stderr.log").open("wb") as stderr:
    result = subprocess.run(command, stdin=source, stdout=stdout, stderr=stderr, env=environment)
summary = {"command": command, "script": str(script), "exit_code": result.returncode}
script.with_suffix(".result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary))
for suffix in (".stdout.log", ".stderr.log"):
    print(script.with_suffix(suffix).read_text(encoding="utf-8", errors="replace")[-10000:])
sys.exit(result.returncode)
