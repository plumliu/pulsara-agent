"""Check CLI/imports only; never reads settings or connects to DB/provider."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
repository = root.parents[3]
script = root / "run_terminal_wsl.py"
guest_repository = "/mnt/c/python_workspace/pulsara-agent"
commands = {
    "help": [str(repository / ".venv/Scripts/python.exe"), str(script), "--help"],
    "lint": [str(repository / ".venv/Scripts/python.exe"), "-m", "ruff", "check", str(script)],
    "imports": ["C:/Windows/System32/wsl.exe", "-d", "Ubuntu", "-u", "plumwsl", "--exec",
                "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python", "-c",
                "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(0,sys.argv.pop(1));"
                "sys.argv=[sys.argv[1], '--check-imports'];runpy.run_path(sys.argv[0],run_name='__main__')",
                guest_repository, guest_repository + "/src",
                guest_repository + "/output/windows-g0/pg_admission_r3/provider-acceptance/run_terminal_wsl.py"],
}
facts = {}
for name, command in commands.items():
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", cwd=repository)
    facts[name] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
(root / "preparation-result.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({name: fact["exit"] for name, fact in facts.items()}))
raise SystemExit(0 if all(fact["exit"] == 0 for fact in facts.values()) else 1)
