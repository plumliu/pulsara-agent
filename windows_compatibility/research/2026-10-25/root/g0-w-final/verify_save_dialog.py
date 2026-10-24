"""Read saved native-dialog facts and clean only its stopped Linux fixture."""

import json
from pathlib import Path
import subprocess

out = Path(__file__).parent
report = json.loads((out / "save-dialog-results.json").read_text(encoding="utf-8"))
exited = json.loads((out / "save-dialog-exit.json").read_text(encoding="utf-8-sig"))
assert report["summary"] == {"passed": 3, "failed": 0}
assert report["stage"] == "complete" and report["carrierExit"]["code"] == 0
assert exited["exit"] == 0 and (out / "save-dialog-host.stderr.log").stat().st_size == 0
assert [row["detail"]["state"] for row in report["tests"]] == [
    "cancelled",
    "cancelled",
    "completed",
]
assert (
    out / "save-dialog-existing.txt"
).read_bytes() == "中文预览 / Windows WSL2\n".encode()
assert not (out / "save-dialog-cancelled.txt").exists()
ready = next(e for e in report["events"] if e["event"] == "ready")
stopped = next(e for e in report["events"] if e["event"] == "stopped")
assert stopped["home"] == ready["home"] and stopped["slots"] == stopped["pending"] == 0
code = """import json,os,pathlib,shutil,sys
p=pathlib.Path(sys.argv[1]); pid=sys.argv[2]
assert p.parent==pathlib.Path('/home/plumwsl') and p.name.startswith('.g0-http-') and not p.is_symlink()
assert p.stat().st_uid==os.getuid()==1000 and (p/'workspace/sample.pdf').read_bytes().startswith(b'%PDF-1.4')
proc=pathlib.Path('/proc')/pid
if proc.exists():
    assert str(p).encode() not in (proc/'environ').read_bytes()
assert not any((p/'imports').glob('*/*'))
shutil.rmtree(p)
print(json.dumps({'path':str(p),'existsAfter':p.exists(),'historicalPidCurrentlyExists':proc.exists()}))
"""
result = subprocess.run(
    [
        "C:/Windows/System32/wsl.exe",
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--exec",
        "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python",
        "-c",
        code,
        ready["home"],
        str(ready["pid"]),
    ],
    capture_output=True,
    text=True,
    check=True,
)
proof = {
    "passed": True,
    "hostExit": exited,
    "carrierExit": report["carrierExit"],
    "tests": report["tests"],
    "cleanup": json.loads(result.stdout),
    "profile": "idle output cache retained; excluded from archive",
}
(out / "save-dialog-verification.json").write_text(
    json.dumps(proof, indent=2, ensure_ascii=False), encoding="utf-8"
)
print(
    json.dumps(
        {
            "passed": True,
            "cases": 3,
            "hostExit": exited["exit"],
            "linuxHomeRemoved": not proof["cleanup"]["existsAfter"],
        }
    )
)
