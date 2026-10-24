"""Read-only verification of the native chooser and user-confirmed drag copies."""

import base64
import json
from pathlib import Path
import subprocess

out = Path(__file__).parent
report = json.loads((out / "files-results.json").read_text(encoding="utf-8"))
imports = [x for x in report["events"] if x["event"] == "imported"]
assert len(imports) == 3
expected = base64.b64encode((out / "Windows 中文附件.bin").read_bytes()).decode()
code = f"""import base64,json,os,subprocess
from pathlib import Path
results=[]
for item in {imports!r}:
    p=Path(item['path']); data=p.read_bytes()
    assert data==base64.b64decode({expected!r})
    assert p.stat().st_mode&0o777==0o400 and p.parent.stat().st_mode&0o777==0o500
    assert p.stat().st_uid==1000
    mount=subprocess.check_output(['findmnt','-n','-o','FSTYPE,SOURCE','-T',str(p)],text=True).strip()
    assert mount=='ext4 /dev/sdd'
    results.append(dict(path=str(p),bytes=len(data),mode='0400',directoryMode='0500',uid=1000,mount=mount))
print(json.dumps(results))
"""
p = subprocess.run(
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
    ],
    capture_output=True,
)
assert p.returncode == 0, p.stderr
result = {
    "copies": json.loads(p.stdout),
    "allBytesExactlyEqual": True,
    "nativeChooser": "user operated after modal tool failed",
    "dragDrop": "user confirmed this turn; server received copies",
    "inputEventIdsNotRecorded": True,
}
(out / "gui-final-import-verification.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps({"copies": len(imports), "allBytesExactlyEqual": True}))
