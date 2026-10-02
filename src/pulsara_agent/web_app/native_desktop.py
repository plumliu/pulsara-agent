"""Small adapters to host desktop owners; no shell or desktop emulation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys


class NativeDesktopUnavailable(OSError):
    """A fixed, user-safe explanation of an unavailable desktop action."""


def linux_desktop_command(name: str) -> str:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise NativeDesktopUnavailable("请从 Linux 桌面会话启动 Pulsara 后重试。")
    command = shutil.which(name)
    if command is None:
        package = "zenity" if name == "zenity" else "xdg-utils"
        raise NativeDesktopUnavailable(f"缺少桌面组件，请安装 {package} 后重试。")
    return command


async def open_desktop_path(path: Path, *, reveal_file: bool = False) -> None:
    # Callers own path validation and permission. xdg-open owns desktop routing;
    # its portable contract opens a directory, without selecting a child file.
    if not path.is_absolute():
        raise ValueError("desktop actions require an absolute path")
    if sys.platform == "darwin":
        args = ["/usr/bin/open", *(["-R"] if reveal_file else []), str(path)]
    elif sys.platform == "linux":
        args = [linux_desktop_command("xdg-open"), str(path.parent if reveal_file else path)]
    else:
        raise NativeDesktopUnavailable("系统打开功能目前支持 macOS 和 Linux 桌面。")
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise NativeDesktopUnavailable("无法启动系统打开程序，请检查桌面组件。") from exc
    try:
        if await process.wait() != 0:
            raise NativeDesktopUnavailable("系统暂时无法打开这个位置，请检查桌面会话与默认应用。")
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
