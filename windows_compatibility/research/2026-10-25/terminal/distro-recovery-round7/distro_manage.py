"""Exact-name temporary WSL registration, image verification, and cleanup."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import winreg

BASE = Path(__file__).resolve().parent
ROOT = BASE / "disposable-distro"
STATE = BASE / "distro-state.json"
WSL = str(Path(os.environ["SystemRoot"]) / "System32/wsl.exe")
IMAGE = "ubuntu-26.04.1-wsl-amd64.wsl"
URL = "https://releases.ubuntu.com/26.04.1/"
EXPECTED = "48d56724b5c8e60f24893e83e73bbb58c60b3ca22fba3da977075420acd54104"


def emit(event, **facts):
    row = dict(event=event, time=time.time(), **facts)
    with (BASE / "distro-lifecycle.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row), flush=True)


def registry():
    parent = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Lxss",
    )
    try:
        default = winreg.QueryValueEx(parent, "DefaultDistribution")[0]
        values = []
        for index in range(winreg.QueryInfoKey(parent)[0]):
            key_name = winreg.EnumKey(parent, index)
            with winreg.OpenKey(parent, key_name) as key:
                values.append(
                    dict(
                        id=key_name,
                        name=winreg.QueryValueEx(key, "DistributionName")[0],
                        path=winreg.QueryValueEx(key, "BasePath")[0],
                        version=winreg.QueryValueEx(key, "Version")[0],
                    )
                )
        return dict(default=default, distributions=values)
    finally:
        winreg.CloseKey(parent)


def bounded_path(path):
    resolved = Path(path).resolve()
    assert resolved.is_relative_to(ROOT.resolve()) and resolved != ROOT.resolve()
    return resolved


def state():
    return json.loads(STATE.read_text(encoding="utf-8"))


def guard(config):
    current = registry()
    assert current["default"] == config["before"]["default"]
    original = next(
        value
        for value in config["before"]["distributions"]
        if value["name"] == "Ubuntu"
    )
    assert original in current["distributions"]
    matches = [
        value for value in current["distributions"] if value["name"] == config["name"]
    ]
    assert len(matches) == 1
    target = matches[0]
    assert bounded_path(target["path"]) == bounded_path(config["install"])
    assert target["version"] == 2
    if "registration" in config:
        assert target == config["registration"]
    emit("registered-target-guard", target=target, default=current["default"])
    return target


def command(*args, timeout=180):
    result = subprocess.run([WSL, *args], capture_output=True, timeout=timeout)

    # WSL management output uses UTF16; exec payloads here use UTF8.
    def decode(value):
        return value.decode(
            "utf-16-le" if b"\0" in value else "utf-8", errors="replace"
        )

    emit(
        "management-command",
        args=args,
        exit=result.returncode,
        stdout=decode(result.stdout),
        stderr=decode(result.stderr),
    )
    assert result.returncode == 0
    return result


action = sys.argv[1]
if action == "create":
    assert not STATE.exists(), "Do not silently replace an earlier registration."
    before = registry()
    name = "PulsaraG0-kernel7-20261025-" + uuid.uuid4().hex[:8]
    assert not any(value["name"] == name for value in before["distributions"])
    folder = bounded_path(ROOT / name)
    folder.mkdir(parents=True)
    install = bounded_path(folder / "instance")
    config = dict(name=name, folder=str(folder), install=str(install), before=before)
    STATE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    emit(
        "before-create",
        registry=before,
        freeBytes=shutil.disk_usage(BASE.anchor).free,
        config=config,
    )
    for filename in ("SHA256SUMS", "SHA256SUMS.gpg", IMAGE):
        destination = folder / filename
        with (
            urllib.request.urlopen(URL + filename, timeout=60) as response,
            destination.open("wb") as stream,
        ):
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        emit("official-download", url=URL + filename, bytes=destination.stat().st_size)
    sums = (folder / "SHA256SUMS").read_text(encoding="utf-8")
    actual_expected = next(
        line.split()[0]
        for line in sums.splitlines()
        if line.split()[-1].lstrip("*") == IMAGE
    )
    assert actual_expected == EXPECTED
    digest = hashlib.file_digest((folder / IMAGE).open("rb"), "sha256").hexdigest()
    assert digest == EXPECTED
    emit("image-verified", sha256=digest, bytes=(folder / IMAGE).stat().st_size)
    linux_folder = "/mnt/" + folder.drive[0].lower() + folder.as_posix()[2:]
    command(
        "-d",
        "Ubuntu",
        "-u",
        "plumwsl",
        "--exec",
        "gpgv",
        "--keyring",
        "/usr/share/keyrings/ubuntu-archive-keyring.gpg",
        linux_folder + "/SHA256SUMS.gpg",
        linux_folder + "/SHA256SUMS",
    )
    assert registry() == before
    command(
        "--import",
        name,
        str(install),
        str(folder / IMAGE),
        "--version",
        "2",
        timeout=300,
    )
    config["registration"] = guard(config)
    STATE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    emit("created", config=config, freeBytes=shutil.disk_usage(BASE.anchor).free)
elif action == "guard":
    guard(state())
elif action == "terminate":
    config = state()
    guard(config)
    command("--terminate", config["name"])
    emit("temporary-distro-terminated", name=config["name"], registry=registry())
elif action == "unregister":
    config = state()
    guard(config)
    command("--unregister", config["name"])
    current = registry()
    assert current == config["before"]
    emit(
        "temporary-distro-unregistered",
        registry=current,
        freeBytes=shutil.disk_usage(BASE.anchor).free,
    )
elif action == "audit-retired":
    config = state()
    current = registry()
    assert current == config["before"]
    assert not bounded_path(config["folder"]).exists()
    listed = command("--list", "--verbose")
    listing = listed.stdout.decode("utf-16-le")
    assert config["name"] not in listing
    facts = dict(
        registry=current,
        ownRegistrationAbsent=True,
        ownFolderAbsent=True,
        listing=listing,
        freeBytes=shutil.disk_usage(BASE.anchor).free,
    )
    (BASE / "distro-cleanup.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    emit("retired-target-audit", **facts)
else:
    raise ValueError(action)
