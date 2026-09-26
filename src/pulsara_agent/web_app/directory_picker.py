"""Native directory selection for the local macOS host; no file import."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys


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
        if sys.platform != "darwin":
            raise NotImplementedError("native directory selection requires macOS")
        async with self._lock:
            if self._closed:
                raise OSError("directory picker is closed")
            initial = Path(initial_path) if initial_path else Path.home()
            if not initial.is_absolute() or not initial.is_dir():
                initial = Path.home()
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript", "-e", _CHOOSE_DIRECTORY, str(initial),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._process = process
            try:
                if self._closed:
                    raise OSError("directory picker is closed")
                stdout, _stderr = await process.communicate()
                if process.returncode != 0:
                    raise OSError("native directory selection failed")
                # Remove only osascript's output terminator, not valid name spaces.
                selected = stdout.decode("utf-8").removesuffix("\n")
                if not selected:
                    return None
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
