"""The sole Darwin/Linux exclusive no-replace namespace primitive."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Protocol


class ExclusivePublishPrimitiveUnavailable(RuntimeError):
    pass


class ExclusiveDirectoryPublisher(Protocol):
    def publish(self, root_fd: int, staging_name: str, final_name: str) -> None: ...


class PlatformExclusiveDirectoryPublisher:
    def publish(self, root_fd: int, staging_name: str, final_name: str) -> None:
        if sys.platform == "darwin":
            self._darwin(root_fd, staging_name, final_name)
            return
        if sys.platform.startswith("linux"):
            self._linux(root_fd, staging_name, final_name)
            return
        raise ExclusivePublishPrimitiveUnavailable

    @staticmethod
    def _darwin(root_fd: int, staging_name: str, final_name: str) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise ExclusivePublishPrimitiveUnavailable from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(
            root_fd,
            os.fsencode(staging_name),
            root_fd,
            os.fsencode(final_name),
            0x00000004,
        ) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), final_name)

    @staticmethod
    def _linux(root_fd: int, staging_name: str, final_name: str) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise ExclusivePublishPrimitiveUnavailable from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        if rename(
            root_fd,
            os.fsencode(staging_name),
            root_fd,
            os.fsencode(final_name),
            0x00000001,
        ) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), final_name)


__all__ = [
    "ExclusiveDirectoryPublisher",
    "ExclusivePublishPrimitiveUnavailable",
    "PlatformExclusiveDirectoryPublisher",
]
