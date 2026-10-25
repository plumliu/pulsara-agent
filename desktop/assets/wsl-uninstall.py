"""Remove this user's installed resources under the existing maintenance locks."""
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import sys

from pulsara_agent.desktop_guest_ops import PostgresFacts, _lock_file, _private_directory


def remove_tree(directory):
    # Imported attachments can have read-only parent directories.
    for current, _, _ in os.walk(directory, followlinks=False):
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("目录所有者已改变，无法清理。")
        descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
        finally:
            os.close(descriptor)
    shutil.rmtree(directory)


def uninstall(runtime, user_home, mode):
    if mode not in {"keep", "all"}:
        raise ValueError("无效的卸载选项。")
    base = user_home / ".local/share/pulsara/runtime"
    if runtime.parent != base or user_home.resolve() != user_home:
        raise ValueError("无法确认应用资源目录。")
    _private_directory(base)
    lock = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    facts = None
    try:
        _lock_file(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        versions = []
        for entry in base.iterdir():
            if entry.name == ".install.lock":
                continue
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.+_-]*", entry.name):
                raise ValueError("应用资源尚未整理完成，请重新安装后卸载。")
            _private_directory(entry)
            marker = entry / ".pulsara-payload.sha256"
            if marker.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}\s*", marker.read_text()):
                raise ValueError("无法确认应用资源，未执行清理。")
            versions.append(entry)
        if runtime not in versions:
            raise ValueError("当前应用资源不完整。")
        facts = PostgresFacts(runtime / "postgres/bin", "maintenanceCheck")
        # A configured app home must never authorize deleting the Linux home or resource ancestors.
        if facts.home in {user_home, *user_home.parents, base, *base.parents} or facts.home.is_relative_to(base):
            raise ValueError("应用数据目录范围无效，未执行清理。")
        facts.acquire()
        facts.perform({"operation": "maintenanceCheck"})
        if mode == "all" and not facts.empty_home:
            remove_tree(facts.home)
        for entry in sorted(versions, key=lambda entry: entry == runtime):
            remove_tree(entry)
    finally:
        if facts is not None:
            facts.close()
        os.close(lock)


if __name__ == "__main__":
    uninstall(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
