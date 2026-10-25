"""User-level WSL project picker; no session, shell, file contents or identities."""
from __future__ import annotations

from contextlib import contextmanager
from heapq import nsmallest
import os
from pathlib import Path
import pwd
import re

from pulsara_agent.desktop_app.linux_storage import require_native_ext4
from pulsara_agent.desktop_app.path_completion import PATH_CANDIDATE_PAGE_SIZE


class WorkspaceDirectoryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(message)


def _leaf(name: str) -> None:
    # Names created by this UI must also be representable in Explorer.
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 or character in '/\\<>:"|?*' for character in name)
            or name.endswith((" ", ".")) or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)):
        raise WorkspaceDirectoryError("WORKSPACE_NAME_INVALID", "请输入可以在资源管理器中使用的单个目录名称。")


class WorkspaceDirectories:
    def __init__(self):
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.root = self.home / "workspaces"

    def _path(self, value: str | None) -> Path:
        if value is not None and not isinstance(value, str):
            raise WorkspaceDirectoryError("WORKSPACE_PATH_INVALID", "目录路径必须是文字。")
        path = self.root if value is None else Path(value)
        if ((value is not None and str(path) != value)
                or not path.is_absolute() or not path.is_relative_to(self.root)
                or any(ord(character) < 32 or ord(character) == 127 for character in str(path))):
            raise WorkspaceDirectoryError("WORKSPACE_PATH_INVALID", "请选择 Linux workspaces 根目录中的目录。")
        return path

    @staticmethod
    def _same_path(path: Path, descriptor: int) -> None:
        bound = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if path.resolve() != path or (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
            raise WorkspaceDirectoryError("WORKSPACE_PATH_CHANGED", "目录已变化，请刷新后重新选择。", 409)

    @contextmanager
    def bind(self, value: str | None):
        path = self._path(value)
        descriptors = []
        try:
            if not self.root.exists():
                raise WorkspaceDirectoryError("WORKSPACE_ROOT_MISSING", "尚未创建 Linux workspaces 目录。", 404)
            if self.home.resolve() != self.home:
                raise WorkspaceDirectoryError("WORKSPACE_PATH_INVALID", "Linux 用户目录身份无效。")
            parent = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(parent)
            for component in path.relative_to(self.home).parts:
                parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
                if os.fstat(parent).st_uid != os.getuid():
                    raise WorkspaceDirectoryError("WORKSPACE_OWNER_INVALID", "工作区目录不属于当前 Linux 用户。")
            self._same_path(path, parent)
            require_native_ext4(path, reject_mounts_within=self.root)
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError("工作区目录需要读、写和执行权限。")
            yield path, parent
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def select(self, value: str) -> dict:
        with self.bind(value) as (path, descriptor):
            self._same_path(path, descriptor)
            return {"path": str(path)}

    def list(self, value: str | None, cursor: str | None) -> dict:
        if cursor is not None and (not isinstance(cursor, str) or "/" in cursor or "\0" in cursor):
            raise WorkspaceDirectoryError("WORKSPACE_CURSOR_INVALID", "目录分页位置无效。")
        with self.bind(value) as (path, descriptor):
            with os.scandir(descriptor) as entries:
                page = nsmallest(PATH_CANDIDATE_PAGE_SIZE + 1, (
                    entry.name for entry in entries
                    if (cursor is None or entry.name > cursor)
                    and not any(ord(character) < 32 or ord(character) == 127 for character in entry.name)
                    and entry.is_dir(follow_symlinks=False)
                ))
            self._same_path(path, descriptor)
            more = len(page) > PATH_CANDIDATE_PAGE_SIZE
            page = page[:PATH_CANDIDATE_PAGE_SIZE]
            return {"root": str(self.root), "path": str(path), "parent": None if path == self.root else str(path.parent),
                    "entries": [{"name": name, "path": str(path / name)} for name in page], "next_cursor": page[-1] if more else None}

    def initialize(self) -> dict:
        if self.home.resolve() != self.home or self.home.stat().st_uid != os.getuid():
            raise WorkspaceDirectoryError("WORKSPACE_OWNER_INVALID", "Linux 用户目录身份无效。")
        require_native_ext4(self.home)
        descriptor = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._same_path(self.home, descriptor)
            try:
                os.mkdir("workspaces", mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                self._same_path(self.home, descriptor)
                return self.select(str(self.root))
            except (OSError, ValueError) as error:
                raise WorkspaceDirectoryError("WORKSPACE_WRITE_UNKNOWN", "创建结果需要重新检查，请刷新目录。", 409) from error
        finally:
            os.close(descriptor)

    def create(self, value: str, name: str) -> dict:
        _leaf(name)
        with self.bind(value) as (path, descriptor):
            self._same_path(path, descriptor)
            require_native_ext4(path, reject_mounts_within=self.root)
            os.mkdir(name, mode=0o755, dir_fd=descriptor)
            try:
                self._same_path(path, descriptor)
                return self.select(str(path / name))
            except (OSError, ValueError) as error:
                # A bound parent can be renamed after mkdir; never repeat or undo it.
                raise WorkspaceDirectoryError("WORKSPACE_WRITE_UNKNOWN", "目录可能已经创建，请刷新后检查。", 409) from error
