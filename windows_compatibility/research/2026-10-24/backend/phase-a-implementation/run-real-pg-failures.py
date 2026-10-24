"""Run the independent Node experiment without saved settings or PG environment."""
import json
import os
from pathlib import Path
import subprocess

directory = Path(__file__).resolve().parent
repo = directory.parents[3]
command = ["node", str(directory / "real-pg-failures.cjs")]
environment = {name: os.environ[name] for name in ("PATH", "TMPDIR") if name in os.environ}
environment.update({"LANG": "C", "LC_ALL": "C"})
with (directory / "real-pg-failures.log").open("w") as log:
    log.write(json.dumps({"command": command, "cwd": str(repo), "environment_names": sorted(environment)}) + "\n")
    log.flush()
    result = subprocess.run(command, cwd=repo, env=environment, stdout=log, stderr=subprocess.STDOUT)
    log.write(f"exit_code={result.returncode}\n")
raise SystemExit(result.returncode)
