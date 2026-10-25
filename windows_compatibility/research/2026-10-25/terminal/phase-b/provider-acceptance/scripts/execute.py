"""Launch exactly one original dogfood scenario with non-secret target argv."""

import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

base = Path(__file__).resolve().parent
prepared = json.loads((base / "prepared.json").read_text(encoding="utf-8"))
target = json.loads(
    (
        base.parents[2]
        / "windows-g0/pg_admission_r3/provider-acceptance/prepared-target.json"
    ).read_text(encoding="utf-8")
)
kind, case = sys.argv[1:3]
if len(sys.argv) == 4:
    assert case != "root-regression", (
        "candidate regression is not an installed-version run"
    )
    version = sys.argv[3]
    assert version.startswith("0.1.0-alpha.") and version.rsplit(".", 1)[1].isdigit()
    runtime_root = "/home/plumwsl/.local/share/pulsara/runtime/" + version + "/"
    prepared["python"] = runtime_root + "python/bin/python3"
    prepared["bootstrap"] = (
        "import sys,runpy,pulsara_agent;assert pulsara_agent.__file__.startswith("
        + repr(runtime_root)
        + "); print('Installed package: '+pulsara_agent.__file__,flush=True);"
        "sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
    )
if case == "root-regression":
    prepared["bootstrap"] = (
        "import sys,runpy;sys.path.insert(0,'/home/plumwsl/workspaces/.provider-tasks-a079alrb/candidate-src');"
        "import pulsara_agent;print('Candidate source: '+pulsara_agent.__file__,flush=True);"
        "sys.path.insert(0,sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    prepared["python"] = "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python"
run = base / (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    + kind
    + "-"
    + case.replace(",", "-")
    + "-"
    + uuid4().hex[:8]
)
run.mkdir()
result_path = (
    "/mnt/c/python_workspace/pulsara-agent/"
    + (run.relative_to(Path.cwd()) / "result.json").as_posix()
)
argv = [
    prepared["python"],
    "-B",
    "-c",
    prepared["bootstrap"],
    prepared["directory"],
    prepared["script"],
    "--execute",
    "--kind",
    kind,
    "--connection-id",
    "connection:oeeiipbn63ytbexaxw74nv5tgy",
    "--model",
    "deepseek-flash",
    "--cases",
    case,
    "--scratch-root",
    prepared["scratch_root"],
    "--output",
    result_path,
]
for key in (
    "saved_home",
    "distribution",
    "user",
    "expected_uid",
    "interop_helper",
    "socket",
    "port",
    "admin_role",
    "runtime_role",
    "root_database",
    "expected_data",
    "expected_system_id",
):
    argv.extend(["--" + key.replace("_", "-"), str(target[key])])
(run / "command.json").write_text(
    json.dumps(argv, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        dict(started=True, kind=kind, case=case, directory=str(run)), ensure_ascii=False
    ),
    flush=True,
)
done = subprocess.run(
    ["C:/Windows/System32/wsl.exe", "-d", "Ubuntu", "-u", "plumwsl", "--exec", *argv],
    capture_output=True,
    text=True,
    encoding="utf-8",
)
(run / "process-result.json").write_text(
    json.dumps(
        dict(exit=done.returncode, stdout=done.stdout, stderr=done.stderr),
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print(
    json.dumps(
        dict(
            exit=done.returncode,
            stdout=done.stdout[-2000:],
            stderr=done.stderr[-3000:],
            directory=str(run),
        ),
        ensure_ascii=False,
    )
)
raise SystemExit(done.returncode)
