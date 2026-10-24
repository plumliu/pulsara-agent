"""Read-only WSL default discovery and explicit confirmed-target admission."""

import ctypes
import base64
from ctypes import wintypes  # noqa: F401 -- preserve executed research snapshot
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

BASE = Path(__file__).resolve().parent
WSL = os.environ["SystemRoot"] + r"\System32\wsl.exe"
ENV = {key: value for key, value in os.environ.items() if key.upper() != "WSLENV"}
events = []


def run(args, utf16=False):
    result = subprocess.run(args, env=ENV, capture_output=True, timeout=20)

    def decode(raw):
        encoding = "utf-16-le" if utf16 or b"\0" in raw[:100] else "utf-8"
        return raw.decode(encoding, errors="replace")

    fact = dict(
        args=args,
        exit=result.returncode,
        stdout=decode(result.stdout),
        stderr=decode(result.stderr),
        stdoutBase64=base64.b64encode(result.stdout).decode(),
        stderrBase64=base64.b64encode(result.stderr).decode(),
    )
    events.append(fact)
    return fact


@dataclass(frozen=True)
class Target:
    distro: str
    user: str
    uid: int
    home: str

    def argv(self, *command):
        if self.uid == 0 or self.user == "root":
            raise ValueError("ROOT_TARGET_REJECTED")
        return [
            WSL,
            "-d",
            self.distro,
            "-u",
            self.user,
            "--cd",
            self.home,
            "--exec",
            *command,
        ]


IDENTITY = "import json,os,pwd,platform,subprocess; p=pwd.getpwuid(os.getuid()); print(json.dumps(dict(distro=os.environ['WSL_DISTRO_NAME'],user=p.pw_name,uid=os.getuid(),home=p.pw_dir,pid=os.getpid(),release=platform.freedesktop_os_release(),uname=platform.uname()._asdict(),homeMount=subprocess.check_output(['findmnt','-T',p.pw_dir,'-n','-o','FSTYPE,SOURCE,TARGET'],text=True).strip())))"
version = run([WSL, "--version"], True)
status = run([WSL, "--status"], True)
inventory = run([WSL, "--list", "--verbose"], True)
default = run([WSL, "--exec", "/usr/bin/python3", "-c", IDENTITY])
assert default["exit"] == 0, default
found = json.loads(default["stdout"])
assert found["distro"] == "Ubuntu" and found["user"] == "plumwsl" and found["uid"] != 0
assert found["release"]["ID"] == "ubuntu" and found["homeMount"].startswith("ext4 ")
assert re.search(r"^\s*\*\s+Ubuntu\s+.*\s+2\s*$", inventory["stdout"], re.M), inventory
target = Target(**{key: found[key] for key in ("distro", "user", "uid", "home")})
# Human confirmation of this exact Ubuntu/plumwsl target persists from the
# delegated task. This JSON is evidence, not production host settings.
(BASE / "confirmed-target.json").write_text(
    json.dumps(asdict(target), indent=2), encoding="utf-8"
)
reopened = run(target.argv("/usr/bin/python3", "-c", IDENTITY))
assert reopened["exit"] == 0 and all(
    json.loads(reopened["stdout"])[key] == value
    for key, value in asdict(target).items()
)
root_entry = run(target.argv("/usr/bin/getent", "passwd", "root"))
root_uid = int(root_entry["stdout"].split(":")[2])
assert root_uid == 0
try:
    Target(target.distro, "root", root_uid, "/root").argv("/usr/bin/id")
except ValueError as error:
    root_rejected = str(error)
else:
    raise AssertionError("root admission accepted")
missing = run(
    [
        WSL,
        "-d",
        "PulsaraG0Missing-" + uuid.uuid4().hex,
        "-u",
        target.user,
        "--exec",
        "/usr/bin/id",
    ]
)
unknown = run(
    [
        WSL,
        "-d",
        target.distro,
        "-u",
        "g0-no-such-user-" + uuid.uuid4().hex,
        "--exec",
        "/usr/bin/id",
    ]
)
assert missing["exit"] != 0 and unknown["exit"] != 0
changed_input = dict(distro="A different default", user="root", uid=0, home="/root")
after_changed_input = run(target.argv("/usr/bin/python3", "-c", IDENTITY))
assert after_changed_input["exit"] == 0 and all(
    json.loads(after_changed_input["stdout"])[key] == value
    for key, value in asdict(target).items()
)
capabilities = run(
    target.argv(
        "/usr/bin/python3",
        "-c",
        "import configparser,json,os,subprocess; c=configparser.ConfigParser();c.read('/etc/wsl.conf');print(json.dumps(dict(interop=c.getboolean('interop','enabled',fallback=True),appendWindowsPath=c.getboolean('interop','appendWindowsPath',fallback=True),automount=c.getboolean('automount','enabled',fallback=True),mountFsTab=c.getboolean('automount','mountFsTab',fallback=True),windowsPathPresent=any(p.startswith('/mnt/c/Windows') for p in os.environ.get('PATH','').split(':')),windowsMount=subprocess.check_output(['findmnt','-T','/mnt/c','-n','-o','FSTYPE,SOURCE,TARGET'],text=True).strip())))",
    )
)
interop = run(target.argv("/mnt/c/Windows/System32/cmd.exe", "/c", "ver"))
assert interop["exit"] == 0
end_default = run([WSL, "--exec", "/usr/bin/python3", "-c", IDENTITY])
assert all(
    json.loads(end_default["stdout"])[key] == value
    for key, value in asdict(target).items()
)
is_admin = bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
assert not is_admin, "probe requires the ordinary unelevated Windows token"
result = dict(
    passed=True,
    target=asdict(target),
    windowsIsUserAnAdmin=is_admin,
    rootRejectedBeforeLaunch=root_rejected,
    changedDefaultTest=dict(
        kind="input-only-no-system-default-change",
        input=changed_input,
        selectedRemains=asdict(target),
    ),
    events=events,
)
(BASE / "discovery-results.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps(
        dict(
            passed=True,
            target=asdict(target),
            missingDistroExit=missing["exit"],
            unknownUserExit=unknown["exit"],
            rootRejectedBeforeLaunch=root_rejected,
        )
    )
)
