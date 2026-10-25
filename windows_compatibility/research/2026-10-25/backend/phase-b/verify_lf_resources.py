"""Run the same portability regression against old/current isolated code."""
import json
from pathlib import Path
import subprocess

repository = Path("/mnt/c/python_workspace/pulsara-agent")
output = repository / "output/windows-g0/pg_admission_r3/phase-b-pg"
config = json.loads((output / "lf-fresh-config.json").read_text())
source = Path(config["directory"]) / "source/src"
registry = source / "pulsara_agent/storage/migrations/registry.py"
current = (repository / "src/pulsara_agent/storage/migrations/registry.py").read_bytes()
original = subprocess.run(["git", "show", "HEAD:src/pulsara_agent/storage/migrations/registry.py"], cwd=repository, capture_output=True, check=True).stdout
facts = {}
try:
    for name, payload in (("before", original), ("after", current)):
        registry.write_bytes(payload)
        command = ["/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python", "-B", "-c",
                   "import sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));import pytest;raise SystemExit(pytest.main(sys.argv[1:]))",
                   str(source), str(repository), str(repository / "tests/test_migration_resource_portability.py"), "-q", "-p", "no:cacheprovider"]
        result = subprocess.run(command, cwd=config["directory"], capture_output=True, text=True)
        facts[name] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
finally:
    registry.write_bytes(current)
    (output / "lf-resource-tests.json").write_text(json.dumps(facts, indent=2))
print(json.dumps({name: value["exit"] for name, value in facts.items()}))
assert facts["before"]["exit"] == 1 and facts["after"]["exit"] == 0
