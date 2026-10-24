import json
import os
from pathlib import Path
import subprocess
import sys
import socket_probe as S

base = Path(__file__).resolve().parent
node = r"C:\Users\plumlocal\AppData\Local\mise\installs\node\22.23.3\node.exe"
electron = str(Path("desktop/node_modules/electron/dist/electron.exe").resolve())
env = os.environ.copy()
env["ELECTRON_RUN_AS_NODE"] = "1"
guest = "import json,os,platform,sys; print(json.dumps(dict(python=sys.version,executable=sys.executable,uid=os.getuid(),uname=platform.uname()._asdict(),interop=os.environ.get('WSL_INTEROP'))))"
data = dict(
    gitCommit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    windowsPython=sys.version,
    windowsPythonExecutable=sys.executable,
    nodeExecutable=node,
    node=json.loads(
        subprocess.check_output(
            [node, "-e", "console.log(JSON.stringify(process.versions))"], text=True
        )
    ),
    electronExecutable=electron,
    electron=json.loads(
        subprocess.check_output(
            [electron, "-e", "console.log(JSON.stringify(process.versions))"],
            env=env,
            text=True,
        )
    ),
    guest=json.loads(subprocess.check_output(S.command(guest), text=True)),
    wslVersion=subprocess.check_output([S.WSL, "--version"])
    .decode("utf-16-le")
    .strip(),
)
(base / "environment.json").write_text(
    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        {
            "gitCommit": data["gitCommit"],
            "windowsPython": sys.version.split()[0],
            "node": data["node"]["node"],
            "electron": data["electron"]["electron"],
            "electronNode": data["electron"]["node"],
            "guestPython": data["guest"]["python"].split()[0],
        }
    )
)
