"""Verify actual chooser result byte-for-byte, without content fingerprints."""

import base64
import json
from pathlib import Path
import subprocess

out = Path(__file__).parent
report = json.loads((out / "files-results.json").read_text(encoding="utf-8"))
imported = [x for x in report["events"] if x["event"] == "imported"]
assert len(imported) == 1
target = imported[0]["path"]
source = (out / "Windows 中文附件.bin").read_bytes()
code = f"""from pathlib import Path
import base64,json,os,subprocess
p=Path({target!r}); b=p.read_bytes()
assert b==base64.b64decode({base64.b64encode(source).decode()!r})
assert p.stat().st_mode&0o777==0o400
assert p.parent.stat().st_mode&0o777==0o500
print(json.dumps({{'pass':True,'bytes':len(b),'path':str(p),'uid':p.stat().st_uid,'mode':oct(p.stat().st_mode&0o777),'directoryMode':oct(p.parent.stat().st_mode&0o777),'mount':subprocess.check_output(['findmnt','-n','-o','FSTYPE,SOURCE','-T',str(p)],text=True).strip()}},ensure_ascii=True))
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
result = json.loads(p.stdout)
result.update(
    {
        "nativeWindowsChooser": True,
        "chooserOperatedBy": "user after tool modal binding failed",
        "productionOwner": "LocalImport",
        "sourceBytesComparedExactly": True,
        "dragDropTested": False,
    }
)
(out / "gui-import-verification.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result))
