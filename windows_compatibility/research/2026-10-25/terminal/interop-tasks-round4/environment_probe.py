import json
import platform
import subprocess
import sys

import windows_probe as W

code = """import json,sys,subprocess
print(json.dumps(dict(python=sys.version,gitHead=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),filesystem=subprocess.check_output(['findmnt','-T','/home/plumwsl','-no','FSTYPE'],text=True).strip())))
"""
completed = subprocess.run(
    W.guest("-c", code),
    env=W.ENV,
    capture_output=True,
    text=True,
    check=True,
    timeout=10,
)
result = dict(
    windowsPython=sys.version,
    executable=sys.executable,
    platform=platform.platform(),
    gitHead=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    linux=json.loads(completed.stdout),
)
(W.BASE / "environment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
