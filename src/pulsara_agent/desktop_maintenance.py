"""Explicit desktop data maintenance, independent of settings and database schema."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess

import psutil

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.desktop_guest_ops import (
    _lock_file,
    _private_directory,
    _read_private,
)
from pulsara_agent.desktop_runtime import acquire_kernel_lock

# Explicit uninstall/reset physical stop boundary; see archived_docs/DESKTOP_DATA_RESET_IMPLEMENTATION.zh.md.
STOP_SECONDS = 30


def remove_tree(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        directory.unlink()
        return
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


def validate_home(home: Path, resources: Path) -> None:
    user_home = Path.home().resolve()
    if (
        home in {user_home, *user_home.parents}
        or resources == home
        or resources.is_relative_to(home)
        or home.is_relative_to(resources)
    ):
        raise ValueError("应用数据目录范围无效，未执行清理。")
    _private_directory(home)
    if (home / "desktop").exists() or (home / "desktop").is_symlink():
        _private_directory(home / "desktop")


def stop_kernel(resources_base: Path, home: Path) -> None:
    """Only explicit uninstall may stop another verified installed kernel."""
    for process in psutil.process_iter():
        verified = False
        try:
            if process.uids().real != os.getuid():
                continue
            command = process.cmdline()
            if len(command) < 3 or command[1:3] != [
                "-m",
                "pulsara_agent.desktop_runtime",
            ]:
                continue
            if not Path(process.exe()).is_relative_to(resources_base):
                continue
            if process.environ().get("PULSARA_HOME") != str(home):
                continue
            verified = True
            children = process.children(recursive=True)
            process.terminate()
            _, alive = psutil.wait_procs([process, *children], timeout=STOP_SECONDS)
            for child in alive:
                child.kill()  # psutil checks process creation time against PID reuse.
            _, alive = psutil.wait_procs(alive, timeout=STOP_SECONDS)
            if alive:
                raise RuntimeError("Pulsara 运行进程尚未退出，未执行清理。")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if verified:
                raise
            # Unknown ownership never authorizes a signal. The kernel lock still
            # prevents cleanup if the inaccessible process owns this home.
            continue


def stop_postgres(binary: Path, home: Path) -> None:
    data = home / "desktop/postgres"
    if not data.exists() and not data.is_symlink():
        return
    _private_directory(data)
    if not _read_private(data / "PG_VERSION").strip().isdigit():
        raise ValueError("无法验证专用 PostgreSQL 数据目录。")

    def run(args: list[str]):
        return subprocess.run(
            [str(binary / "pg_ctl"), *args],
            capture_output=True,
            text=True,
            timeout=STOP_SECONDS + 5,
            env={**os.environ, "LC_ALL": "C"},
        )

    status = run(["status", "-D", str(data)])
    if status.returncode == 3:
        return
    if status.returncode != 0:
        raise RuntimeError(
            f"无法确认 PostgreSQL 状态：{status.stderr or status.stdout}"
        )
    lines = _read_private(data / "postmaster.pid").splitlines()
    if (
        len(lines) < 5
        or lines[1] != str(data)
        or lines[4] != str(home / "desktop/socket")
    ):
        raise ValueError("运行实例与专用目录不匹配，拒绝停止。")
    for mode in ("fast", "immediate"):
        run(["stop", "-w", "-t", str(STOP_SECONDS), "-D", str(data), "-m", mode])
        status = run(["status", "-D", str(data)])
        if status.returncode == 3:
            return
        if status.returncode != 0:
            break
    raise RuntimeError(
        f"数据库尚未确认停止，未执行清理：{status.stderr or status.stdout}"
    )


class Maintenance:
    """Hold the existing lock inodes throughout stop and data deletion."""

    def __init__(self, home: Path, resources: Path, postgres_bin: Path):
        self.home, self.resources, self.postgres_bin = home, resources, postgres_bin
        self.descriptors: list[int] = []

    def __enter__(self):
        validate_home(self.home, self.resources)
        try:
            descriptor = acquire_kernel_lock()
            self.descriptors.append(descriptor)
            _lock_file(descriptor)
            stop_postgres(self.postgres_bin, self.home)
            descriptor = os.open(
                self.home / "desktop/pg-lifetime.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            self.descriptors.append(descriptor)
            _lock_file(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def clear(self, *, keep_electron: bool = False):
        validate_home(self.home, self.resources)
        for entry in self.home.iterdir():
            if entry.name != "desktop":
                remove_tree(entry)
        for entry in (self.home / "desktop").iterdir():
            if (
                entry.name in {"kernel.lock", "pg-lifetime.lock"}
                or keep_electron
                and entry.name == "electron"
            ):
                continue
            remove_tree(entry)

    def __exit__(self, *_):
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", required=True, type=Path)
    parser.add_argument("--postgres-bin", required=True, type=Path)
    parser.add_argument("--keep-electron", action="store_true")
    args = parser.parse_args()
    with Maintenance(
        require_pulsara_home(), args.resources, args.postgres_bin
    ) as maintenance:
        maintenance.clear(keep_electron=args.keep_electron)


if __name__ == "__main__":
    main()
