"""Remove this user's installed resources under the existing maintenance locks."""
import fcntl
import os
from pathlib import Path
import re
import sys

from pulsara_agent.desktop_guest_ops import _lock_file, _private_directory
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.desktop_maintenance import Maintenance, remove_tree, stop_kernel


def uninstall(runtime, user_home, mode):
    if mode not in {"keep", "all"}:
        raise ValueError("无效的卸载选项。")
    base = user_home / ".local/share/pulsara/runtime"
    if runtime.parent != base or user_home.resolve() != user_home:
        raise ValueError("无法确认应用资源目录。")
    _private_directory(base)
    lock = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    maintenance = None
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
        home = require_pulsara_home()
        if home in {user_home, *user_home.parents, base, *base.parents} or home.is_relative_to(base):
            raise ValueError("应用数据目录范围无效，未执行清理。")
        if home.exists() or home.is_symlink():
            stop_kernel(base, home)
            maintenance = Maintenance(home, base, runtime / "postgres/bin")
            maintenance.__enter__()
            if mode == "all":
                maintenance.clear()
        for entry in sorted(versions, key=lambda entry: entry == runtime):
            remove_tree(entry)
        if mode == "all" and maintenance is not None:
            remove_tree(home)
    finally:
        if maintenance is not None:
            maintenance.__exit__(None, None, None)
        os.close(lock)


if __name__ == "__main__":
    uninstall(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
