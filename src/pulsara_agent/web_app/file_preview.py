"""Ephemeral, user-activated file reading; never a model or durable authority.

See PULSARA_SESSION_FILE_LINK_PREVIEW_IMPLEMENTATION_SPEC.zh.md. aiohttp owns
HTTP streaming; this module binds OS descriptors and bounded reading operations.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
import errno
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import quote, unquote, urlsplit

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.local_source_binding import (
    DIRECTORY_NOFOLLOW_FLAGS,
    open_absolute_directory_nofollow,
    prepare_local_source_path,
)

PAGE_BYTES = STAGE2_LIMITS.content_chunk_hard_bytes
# JSON escapes can expand a source byte sixfold; reserve space for page metadata.
TEXT_BYTES = PAGE_BYTES // 8
RASTER = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}
HTML = {".html", ".htm"}
OFFICE = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
}
TEXT = {
    ".txt",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".sql",
    ".sh",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".ini",
}
RESOURCE_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    **{
        ext: mimetypes.guess_type("x" + ext)[0] or "application/octet-stream"
        for ext in RASTER
    },
}


class FilePreviewError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def expired() -> FilePreviewError:
    return FilePreviewError("PREVIEW_EXPIRED", "预览已关闭或被替换，请重新打开。", 409)


def local_path(value: str, base: Path) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or re.search(r"[\x00-\x1f\x7f\\]", value)
    ):
        raise FilePreviewError("FILE_ADDRESS_INVALID", "无法识别这个文件地址。")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise FilePreviewError("FILE_ADDRESS_INVALID", "文件地址编码不正确。")
    parsed = urlsplit(value)
    if parsed.scheme and (
        parsed.scheme.lower() != "file"
        or parsed.netloc.lower() not in {"", "localhost"}
    ):
        raise FilePreviewError("FILE_ADDRESS_INVALID", "无法识别这个文件地址。")
    if not parsed.scheme and parsed.netloc:
        raise FilePreviewError("FILE_ADDRESS_INVALID", "此地址不是本地文件。")
    try:
        raw = unquote(parsed.path, errors="strict")
    except UnicodeError as exc:
        raise FilePreviewError("FILE_ADDRESS_INVALID", "文件地址编码不正确。") from exc
    if not raw or re.search(r"[\x00-\x1f\x7f\\]", raw):
        raise FilePreviewError("FILE_ADDRESS_INVALID", "文件地址包含不支持的字符。")
    path = Path(raw).expanduser()
    return prepare_local_source_path(path if path.is_absolute() else base / path)


def identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def classify(path: Path, fd: int) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext in HTML:
        return "html", "text/html"
    if ext in RASTER:
        return "image", RESOURCE_TYPES[ext]
    if ext == ".pdf":
        return "pdf", "application/pdf"
    if ext in {".csv", ".tsv"}:
        return "table", RESOURCE_TYPES[ext]
    if ext in {".md", ".markdown"}:
        return "markdown", "text/plain"
    if ext in TEXT:
        return "text", "text/plain"
    if ext not in OFFICE:
        sample = os.pread(fd, 4096, 0)
        if b"\x00" not in sample:
            try:
                import codecs

                codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
            except (UnicodeError, TypeError):
                pass
            else:
                return "text", "text/plain"
    return "file", "application/octet-stream"


class BudgetLines:
    """Bound each csv.reader.next input, including multiline/empty-field rows."""

    def __init__(self, fd: int):
        self.stream = io.TextIOWrapper(
            os.fdopen(os.dup(fd), "rb"), encoding="utf-8-sig", newline=""
        )
        self.stream.seek(0)
        self.remaining = PAGE_BYTES

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(self.remaining + 1)
        if not line:
            raise StopIteration
        self.remaining -= len(line.encode("utf-8"))
        if self.remaining < 0:
            raise FilePreviewError(
                "FILE_PREVIEW_UNSUPPORTED",
                "单条记录过大，无法以表格预览；可查看源文本或下载原文件。",
            )
        return line


@dataclass
class Preview:
    connection_id: str
    path: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    fd: int | None = None
    root_fd: int | None = None
    info: os.stat_result | None = None
    kind: str = "file"
    mime: str = "application/octet-stream"
    notice: str | None = None
    closed: bool = False
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lines: BudgetLines | None = None
    reader: object | None = None
    pending_row: list[str] | None = None
    row_index: int = 0
    last_page: tuple[str, int, dict] | None = None
    table_error: str | None = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.page_lock.locked():
            self.close_handles()

    def close_handles(self):
        if self.lines:
            self.lines.stream.close()
        for fd in (self.fd, self.root_fd):
            if fd is not None:
                os.close(fd)
        self.fd = self.root_fd = None

    def check(self):
        if self.closed:
            raise expired()
        if (
            self.fd is not None
            and self.info is not None
            and identity(os.fstat(self.fd)) != identity(self.info)
        ):
            raise FilePreviewError(
                "FILE_CHANGED", "文件已发生变化，请重新打开预览。", 409
            )

    def metadata(self) -> dict:
        base = "/api/file-previews/" + self.token
        return {
            "read_token": self.token,
            "path": str(self.path),
            "name": self.path.name or str(self.path),
            "kind": self.kind,
            "size": self.info.st_size if self.info else None,
            "notice": self.notice,
            "content_url": base + "/content" if self.fd is not None else None,
            "document_url": base + "/resources/" + quote(self.path.name, safe="")
            if self.kind == "html"
            else None,
            "images_url": base + "/images/" if self.kind == "markdown" else None,
            "can_open": self.kind in {"image", "pdf"}
            or (self.path.suffix.lower() in OFFICE and self.fd is not None),
        }

    def page(self, cursor: int, mode: str) -> dict:
        self.check()
        if self.fd is None:
            raise FilePreviewError("FILE_PREVIEW_UNSUPPORTED", "此文件无法预览。")
        if self.last_page and self.last_page[:2] == (mode, cursor):
            return self.last_page[2]
        if mode == "table" and self.kind == "table":
            result = self._table_page(cursor)
        elif mode == "text" and self.kind in {"text", "markdown", "table", "html"}:
            if cursor < 0 or cursor > self.info.st_size:
                raise expired()
            raw = os.pread(self.fd, TEXT_BYTES, cursor)
            import codecs

            decoder = codecs.getincrementaldecoder(
                "utf-8-sig" if cursor == 0 else "utf-8"
            )()
            try:
                text = decoder.decode(raw, final=cursor + len(raw) >= self.info.st_size)
            except UnicodeError as exc:
                raise FilePreviewError(
                    "FILE_PREVIEW_UNSUPPORTED",
                    "此文件不是受支持的 UTF-8 文本，可下载或使用系统应用打开。",
                ) from exc
            consumed = len(raw) - len(decoder.getstate()[0])
            end = cursor + consumed
            result = {
                "mode": "text",
                "text": text,
                "cursor": cursor,
                "next_cursor": end if end < self.info.st_size else None,
            }
        else:
            raise FilePreviewError("FILE_PREVIEW_UNSUPPORTED", "此类型没有文本预览。")
        self.check()
        self.last_page = (mode, cursor, result)
        return result

    def _table_page(self, cursor: int) -> dict:
        if self.table_error:
            raise FilePreviewError("FILE_PREVIEW_UNSUPPORTED", self.table_error)
        if cursor == 0 and self.row_index != 0:
            if self.lines:
                self.lines.stream.close()
            self.lines = self.reader = self.pending_row = None
            self.row_index = 0
        if cursor != self.row_index:
            raise expired()
        if self.reader is None:
            self.lines = BudgetLines(self.fd)
            self.reader = csv.reader(
                self.lines,
                delimiter="\t" if self.path.suffix.lower() == ".tsv" else ",",
                strict=True,
            )
        rows, size, ended = [], 128, False
        try:
            while True:
                self.lines.remaining = PAGE_BYTES
                row = (
                    self.pending_row
                    if self.pending_row is not None
                    else next(self.reader, None)
                )
                self.pending_row = None
                if row is None:
                    ended = True
                    break
                row_bytes = (
                    len(
                        json.dumps(
                            row, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    + 1
                )
                if row_bytes + 128 > PAGE_BYTES:
                    raise FilePreviewError(
                        "FILE_PREVIEW_UNSUPPORTED",
                        "单条记录过大，无法以表格预览；可查看源文本或下载原文件。",
                    )
                if size + row_bytes > PAGE_BYTES:
                    self.pending_row = row
                    break
                rows.append(row)
                size += row_bytes
        except (csv.Error, UnicodeError, FilePreviewError) as exc:
            self.table_error = (
                exc.message
                if isinstance(exc, FilePreviewError)
                else "表格记录无法解析，可查看源文本或下载原文件。"
            )
            self.lines.stream.close()
            self.lines = self.reader = self.pending_row = None
            raise FilePreviewError(
                "FILE_PREVIEW_UNSUPPORTED", self.table_error
            ) from exc
        self.row_index += len(rows)
        return {
            "mode": "table",
            "rows": rows,
            "cursor": cursor,
            "next_cursor": None if ended else self.row_index,
        }

    def resource(self, relative: str, *, images: bool) -> tuple[int, str]:
        self.check()
        if self.root_fd is None or self.kind != ("markdown" if images else "html"):
            raise expired()
        # aiohttp has already decoded match_info once. Never unquote it again.
        parts = relative.split("/")
        if not parts or any(
            not p or p.startswith(".") or "\\" in p or re.search(r"[\x00-\x1f\x7f]", p)
            for p in parts
        ):
            raise FilePreviewError(
                "RESOURCE_OUTSIDE_ROOT", "资源不在允许的目录范围内。", 403
            )
        ext = Path(parts[-1]).suffix.lower()
        if ext not in (RASTER if images else RESOURCE_TYPES):
            raise FilePreviewError(
                "FILE_PREVIEW_UNSUPPORTED", "此资源类型不支持内嵌预览。", 415
            )
        if not images and parts == [self.path.name]:
            return os.dup(self.fd), self.mime
        directory = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, DIRECTORY_NOFOLLOW_FLAGS, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise FilePreviewError(
                    "FILE_PREVIEW_UNSUPPORTED", "资源不是普通文件。", 415
                )
            return fd, RESOURCE_TYPES[ext]
        except OSError as exc:
            raise file_error(exc) from exc
        finally:
            os.close(directory)

    def action_path(self, action: str) -> Path:
        self.check()
        if action not in {"reveal", "open"} or (
            action == "open" and not self.metadata()["can_open"]
        ):
            raise FilePreviewError("FILE_PREVIEW_UNSUPPORTED", "此文件不支持该操作。")
        if self.info is not None:
            check = open_preview(self.connection_id, self.path)
            try:
                if check.info is None or identity(check.info) != identity(self.info):
                    raise FilePreviewError(
                        "FILE_CHANGED", "文件已发生变化，请重新打开预览。", 409
                    )
            finally:
                check.close()
        return self.path


def file_error(exc: OSError) -> FilePreviewError:
    if exc.errno == errno.ENOENT:
        return FilePreviewError("FILE_NOT_FOUND", "文件已移动或不存在。", 404)
    return FilePreviewError(
        "FILE_UNREADABLE", "无法读取此文件，请检查路径与访问权限。", 403
    )


def open_preview(connection_id: str, path: Path) -> Preview:
    item = Preview(connection_id, path)
    try:
        item.root_fd = open_absolute_directory_nofollow(path.parent)
        try:
            item.fd = os.open(
                path.name or ".",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=item.root_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                item.notice = "符号链接暂不支持内容预览，可在 Finder 中定位。"
                return item
            raise
        item.info = os.fstat(item.fd)
        if stat.S_ISDIR(item.info.st_mode):
            item.kind = "directory"
            os.close(item.fd)
            item.fd = None
        elif stat.S_ISREG(item.info.st_mode):
            item.kind, item.mime = classify(path, item.fd)
        else:
            raise FilePreviewError(
                "FILE_PREVIEW_UNSUPPORTED", "只支持普通文件或文件夹。", 415
            )
        return item
    except OSError as exc:
        item.close()
        if exc.errno in {errno.ELOOP, errno.ENOTDIR} and any(
            parent.is_symlink() for parent in path.parents
        ):
            return Preview(
                connection_id,
                path,
                notice="路径包含符号链接，暂不支持内容预览，可在 Finder 中定位。",
            )
        raise file_error(exc) from exc
    except BaseException:
        item.close()
        raise


class FilePreviews:
    """One current view per browser connection, released by that existing owner."""

    def __init__(self):
        self.slots: dict[str, Preview] = {}
        self.pending: dict[str, object] = {}

    def revoke(self, connection_id: str):
        self.pending.pop(connection_id, None)
        item = self.slots.pop(connection_id, None)
        if item:
            item.close()

    def current(self, connection_id: str, token: str, *, check_content=True) -> Preview:
        item = self.slots.get(connection_id)
        if item is None or item.token != token:
            raise expired()
        if check_content:
            item.check()
        return item

    def by_token(self, token: str) -> Preview:
        item = next(
            (s for s in self.slots.values() if secrets.compare_digest(s.token, token)),
            None,
        )
        if item is None:
            raise expired()
        item.check()
        return item
