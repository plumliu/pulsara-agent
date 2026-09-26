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

from aiohttp import BodyPartReader, web

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
        raise ValueError("文件夹包含非法路径组件。")
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
    """One unpublished tree held by descriptors until the request succeeds."""

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
        self.entries: dict[tuple[str, ...], os.stat_result] = {}
        self.directories: dict[tuple[str, ...], os.stat_result] = {
            (): os.fstat(self.fd)
        }
        self.targets: set[tuple[str, ...]] = set()
        self.bytes = 0
        self.files = 0

    async def write(self, parts: tuple[str, ...], reader) -> None:
        normalized = tuple(unicodedata.normalize("NFC", p) for p in parts)
        if normalized in self.targets:
            raise ValueError("文件夹包含重复目标路径。")
        self.targets.add(normalized)
        parent = os.dup(self.fd)
        try:
            for index, component in enumerate(parts[:-1]):
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
                key = parts[: index + 1]
                actual = os.fstat(child)
                previous = self.directories.get(key)
                if previous is not None and not os.path.samestat(actual, previous):
                    raise ValueError("导入子目录已被替换。")
                # Existing OS aliases must not silently merge two source names.
                if previous is None and any(
                    os.path.samestat(actual, known)
                    for known in self.directories.values()
                ):
                    raise ValueError("文件夹包含重复规范化目录。")
                self.directories[key] = actual
            descriptor = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as target:
                while chunk := await reader(64 * 1024):
                    target.write(chunk)
                    self.bytes += len(chunk)
                target.flush()
                os.fchmod(target.fileno(), 0o400)
                os.fsync(target.fileno())
                self.entries[parts] = os.fstat(target.fileno())
            self.files += 1
        finally:
            os.close(parent)

    def _verify(self, location: str) -> None:
        # A path returned to the browser must still address the descriptors we
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
        children: dict[tuple[str, ...], set[str]] = {
            key: set() for key in self.directories
        }
        for parts in (*self.directories, *self.entries):
            if parts:
                children[parts[:-1]].add(parts[-1])
        for parts, expected in self.directories.items():
            parent = os.dup(self.fd)
            try:
                for component in parts:
                    child = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=parent)
                    os.close(parent)
                    parent = child
                if not os.path.samestat(os.fstat(parent), expected):
                    raise ValueError("导入子目录已被替换。")
                # Compare actual FS identities below rather than spelling:
                # macOS can return decomposed Unicode names from listdir.
                if len(os.listdir(parent)) != len(children[parts]):
                    raise ValueError("导入目录内容已被修改。")
                for leaf in children[parts]:
                    expected_file = self.entries.get((*parts, leaf))
                    if expected_file is None:
                        continue
                    actual = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(
                        actual, expected_file
                    ):
                        raise ValueError("导入文件已被替换。")
            finally:
                os.close(parent)

    def publish(self, name: str) -> dict[str, object]:
        self._verify(self.stage)
        for parts, expected in self.directories.items():
            if not parts:
                continue
            descriptor = self._open_directory(parts)
            try:
                if not os.path.samestat(os.fstat(descriptor), expected):
                    raise ValueError("导入子目录已被替换。")
                os.fchmod(descriptor, 0o500)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
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
            "file_count": self.files,
        }

    def _open_directory(self, parts: tuple[str, ...]) -> int:
        descriptor = os.dup(self.fd)
        try:
            for component in parts:
                child = os.open(component, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        try:
            if not self.published:
                try:
                    current = os.stat(
                        self.stage, dir_fd=self.root_fd, follow_symlinks=False
                    )
                    if os.path.samestat(current, os.fstat(self.fd)):
                        # Sealing can precede a failed publication. Restore only
                        # directories whose identity this import actually owns.
                        for parts, expected in self.directories.items():
                            try:
                                descriptor = self._open_directory(parts)
                            except OSError:
                                continue
                            try:
                                if os.path.samestat(os.fstat(descriptor), expected):
                                    os.fchmod(descriptor, 0o700)
                            finally:
                                os.close(descriptor)
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
    await pending.write((name,), request.content.read)
    return name


async def receive_directory(request: web.Request, pending: LocalImport) -> str:
    if request.content_type != "multipart/form-data":
        raise ValueError("文件夹导入需要 multipart 字节流。")
    # clone's public client_max_size override also reaches MultipartReader in
    # current aiohttp. The ordinary JSON API retains its existing 8 MiB bound.
    reader = await request.clone(client_max_size=0).multipart()
    root: str | None = None
    while path_part := await reader.next():
        if not isinstance(path_part, BodyPartReader) or path_part.name != "path":
            raise ValueError("文件夹请求需要相对路径字段。")
        # Relative paths are JSON text fields: multipart filename parsing strips
        # leading separators, so it cannot preserve the value we must validate.
        encoded_path = bytearray()
        maximum_path = os.fpathconf(pending.fd, "PC_PATH_MAX")
        while chunk := await path_part.read_chunk(64 * 1024):
            encoded_path.extend(chunk)
            if len(encoded_path) > maximum_path * 6 + 2:
                raise ValueError("相对路径超过文件系统路径边界。")
        relative_path = json.loads(encoded_path)
        if not isinstance(relative_path, str):
            raise ValueError("文件夹相对路径必须为文字。")
        part = await reader.next()
        if not isinstance(part, BodyPartReader) or part.name != "file":
            raise ValueError("文件夹请求必须逐项包含路径和文件。")
        if part.headers.get("Content-Transfer-Encoding") or part.headers.get(
            "Content-Encoding"
        ):
            raise ValueError("文件夹请求必须传输原始字节。")
        parts = tuple(_component(p) for p in relative_path.split("/"))
        if (
            len(os.fsencode(str(pending.root / pending.final / relative_path)))
            >= maximum_path
        ):
            raise ValueError("副本路径超过文件系统路径边界。")
        if len(parts) < 2 or (root is not None and root != parts[0]):
            raise ValueError("文件必须位于同一个文件夹内。")
        root = parts[0]
        await pending.write(parts, part.read_chunk)
    if root is None:
        raise ValueError("没有可导入的文件；可直接输入已有本机目录路径。")
    return root
