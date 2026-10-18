"""User-requested local copies; aiohttp owns streaming, OS owns publication.

No conversation rows or attachment identities are created. Existing no-follow
directory and exclusive publication primitives own the filesystem boundary.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import stat
import unicodedata
from uuid import uuid4

from aiohttp import web

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.exclusive_publish import PlatformExclusiveDirectoryPublisher
from pulsara_agent.local_source_binding import (
    DIRECTORY_NOFOLLOW_FLAGS,
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
    prepare_local_source_path,
)


def _component(name: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or any(c in name for c in "/\\:")
        or any(unicodedata.category(c).startswith("C") for c in name)
    ):
        raise ValueError("文件名包含非法字符。")
    return name


def safe_filename(name: str, name_max: int) -> str:
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = "".join(c for c in leaf if not unicodedata.category(c).startswith("C"))
    leaf = leaf.replace(":", "_")
    _component(leaf)
    if len(os.fsencode(leaf)) > name_max:
        suffix = Path(leaf).suffix
        if len(os.fsencode(suffix)) >= name_max:
            suffix = ""
        stem = leaf[: -len(suffix)] if suffix else leaf
        while len(os.fsencode(stem + suffix)) > name_max:
            stem = stem[:-1]
        leaf = stem + suffix
    return leaf


class LocalImport:
    """One unpublished file held by descriptors until the request succeeds."""

    def __init__(self) -> None:
        self.root = prepare_local_source_path(require_pulsara_home()) / "imports"
        self.root_fd = open_or_create_absolute_directory_nofollow(self.root)
        self.final = uuid4().hex
        self.stage = ".import-" + self.final
        self.published = False
        try:
            os.mkdir(self.stage, 0o700, dir_fd=self.root_fd)
            self.fd = os.open(self.stage, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=self.root_fd)
        except BaseException:
            os.close(self.root_fd)
            raise
        self.entry: tuple[str, os.stat_result] | None = None
        self.bytes = 0

    async def write(self, name: str, reader) -> None:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=self.fd,
        )
        with os.fdopen(descriptor, "wb") as target:
            while chunk := await reader(64 * 1024):
                target.write(chunk)
                self.bytes += len(chunk)
            target.flush()
            os.fchmod(target.fileno(), 0o400)
            os.fsync(target.fileno())
            self.entry = (name, os.fstat(target.fileno()))

    def _verify(self, location: str) -> None:
        # A path returned to the renderer must still address the descriptors we
        # held, including after an await in a slow upload.
        current = open_absolute_directory_nofollow(self.root)
        try:
            if not os.path.samestat(os.fstat(current), os.fstat(self.root_fd)):
                raise ValueError("导入目录已被替换。")
        finally:
            os.close(current)
        if not os.path.samestat(
            os.stat(location, dir_fd=self.root_fd, follow_symlinks=False),
            os.fstat(self.fd),
        ):
            raise ValueError("导入临时目录已被替换。")
        if self.entry is None or len(os.listdir(self.fd)) != 1:
            raise ValueError("导入目录内容已被修改。")
        name, expected = self.entry
        actual = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(actual, expected):
            raise ValueError("导入文件已被替换。")

    def publish(self, name: str) -> dict[str, object]:
        self._verify(self.stage)
        PlatformExclusiveDirectoryPublisher().publish(
            self.root_fd, self.stage, self.final
        )
        # Keep cleanup scoped to our moved inode if post-publication validation
        # fails. A replaced leaf is never removed by this request.
        self.stage = self.final
        # Darwin's exclusive directory rename requires a writable source
        # wrapper. Seal that wrapper after rename, before returning its path.
        os.fchmod(self.fd, 0o500)
        os.fsync(self.fd)
        os.fsync(self.root_fd)
        self._verify(self.final)
        self.published = True
        return {
            "path": str(self.root / self.final / name),
            "name": name,
            "bytes": self.bytes,
        }

    def close(self) -> None:
        try:
            if not self.published:
                try:
                    current = os.stat(
                        self.stage, dir_fd=self.root_fd, follow_symlinks=False
                    )
                    if os.path.samestat(current, os.fstat(self.fd)):
                        # Publication may have sealed this same owned directory.
                        os.fchmod(self.fd, 0o700)
                        shutil.rmtree(self.stage, dir_fd=self.root_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(self.fd)
            os.close(self.root_fd)


async def receive_file(request: web.Request, pending: LocalImport) -> str:
    if request.content_type != "application/octet-stream":
        raise ValueError("文件导入需要原始字节流。")
    name = json.loads(request.headers.get("X-Pulsara-Filename", "null"))
    if not isinstance(name, str):
        raise ValueError("文件名缺失。")
    name = safe_filename(name, os.fpathconf(pending.fd, "PC_NAME_MAX"))
    await pending.write(name, request.content.read)
    return name
