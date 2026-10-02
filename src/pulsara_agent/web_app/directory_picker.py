"""Native directory selection on the host desktop; no file import."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from .native_desktop import NativeDesktopUnavailable, linux_desktop_command


# Standard Additions owns the native panel and cancellation. Pass the starting
# directory as argv, never interpolate a filesystem path into AppleScript.
_CHOOSE_DIRECTORY = '''on run argv
    activate
    try
        set selectedFolder to choose folder with prompt "选择 Pulsara 会话的工作目录" default location (POSIX file (item 1 of argv))
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run'''


class NativeDirectoryPicker:
    def __init__(self) -> None:
        # One native panel at a time on the host desktop; additional requests wait.
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._closed = False

    async def choose(self, initial_path: str | None) -> str | None:
        if sys.platform not in {"darwin", "linux"}:
            raise NotImplementedError("native directory selection requires macOS or Linux")
        async with self._lock:
            if self._closed:
                raise OSError("directory picker is closed")
            initial = Path(initial_path) if initial_path else Path.home()
            if not initial.is_absolute() or not initial.is_dir():
                initial = Path.home()
            environment = None
            if sys.platform == "darwin":
                args = ["/usr/bin/osascript", "-e", _CHOOSE_DIRECTORY, str(initial)]
            else:
                args = [
                    linux_desktop_command("zenity"), "--file-selection", "--directory",
                    "--title=选择 Pulsara 会话的工作目录", f"--filename={initial}/",
                ]
                # Zenity owns GtkFileChooserNative (including portal selection).
                # Its supported per-child exit overrides separate Cancel/Escape
                # from GTK startup errors, which can also exit with status 1.
                environment = dict(os.environ, ZENITY_OK="0", ZENITY_CANCEL="3",
                                   ZENITY_ESC="3", ZENITY_ERROR="255")
            process = await asyncio.create_subprocess_exec(
                *args, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._process = process
            try:
                if self._closed:
                    raise OSError("directory picker is closed")
                stdout, _stderr = await process.communicate()
                if sys.platform == "linux" and process.returncode == 3:
                    return None
                if process.returncode != 0:
                    raise NativeDesktopUnavailable("无法打开目录选择窗口，请检查桌面会话与目录选择组件。")
                # Remove only the owner's output terminator, not filename whitespace.
                selected = os.fsdecode(stdout).removesuffix("\n")
                if not selected and sys.platform == "darwin":
                    return None
                if not selected:
                    raise NativeDesktopUnavailable("目录选择窗口没有返回本地目录，请重试。")
                path = Path(selected)
                if not path.is_absolute() or not path.is_dir():
                    raise OSError("selected directory is unavailable")
                return str(path.resolve())
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                self._process = None

    async def aclose(self) -> None:
        self._closed = True
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
